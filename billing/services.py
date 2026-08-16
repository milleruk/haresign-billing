"""Billing state transitions.

Everything that writes a `BillingAccount`, `Subscription`, `SubscriptionItem`,
`ComplimentaryGrant` or `InvoiceReference` goes through here. Views and webhook
handlers call in; nothing else writes those models directly. That is where the
audit events live, and a second write path would be a second set of rules.
"""

from __future__ import annotations

import logging
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from audit import events
from audit.services import record
from catalog.models import Plan, PlanPrice

from .models import (
    BillingAccount,
    BillingContact,
    ComplimentaryGrant,
    InvoiceReference,
    Subscription,
    SubscriptionItem,
)

logger = logging.getLogger('haresign.billing')


class BillingConflict(RuntimeError):
    """A state change that would silently destroy information. Never resolved here."""


# --- Accounts -----------------------------------------------------------------


def get_or_create_account(
    organization_id,
    *,
    name: str = '',
    organization_type: str = '',
    request=None,
) -> tuple[BillingAccount, bool]:
    """The billing account for an Identity organisation, creating it if needed.

    `name` and `organization_type` are display copies refreshed from the caller's
    OIDC session. They are updated on every call because a renamed organisation
    should not show its old name on an invoice screen — and they are *only*
    display, so refreshing them decides nothing.
    """
    account, created = BillingAccount.objects.get_or_create(
        organization_id=organization_id,
        defaults={'organization_name': name, 'organization_type': organization_type},
    )
    if created:
        record(
            events.BILLING_ACCOUNT_CREATED,
            request=request,
            organization_id=organization_id,
            metadata={'organization_type': organization_type},
        )
    elif name and (
        account.organization_name != name or account.organization_type != organization_type
    ):
        account.organization_name = name
        account.organization_type = organization_type or account.organization_type
        account.save(update_fields=['organization_name', 'organization_type', 'updated_at'])
    return account, created


def set_billing_contact(
    account: BillingAccount,
    *,
    role: str = BillingContact.Role.PRIMARY,
    identity_user_id=None,
    email: str = '',
    display_name: str = '',
    request=None,
) -> BillingContact:
    """Set or replace one billing contact role for an account."""
    if identity_user_id is None and not email:
        raise BillingConflict('A billing contact needs an Identity user UUID or an email address.')

    contact, _ = BillingContact.objects.update_or_create(
        account=account,
        role=role,
        defaults={
            'identity_user_id': identity_user_id,
            'email': email,
            'display_name': display_name,
        },
    )
    record(
        events.BILLING_CONTACT_CHANGED,
        request=request,
        organization_id=account.organization_id,
        # No address, no name: the audit scrubber would drop them anyway, and
        # saying "which role changed" answers the support question without
        # accumulating a second copy of the contact list.
        metadata={'role': role, 'has_identity_user': identity_user_id is not None},
    )
    return contact


# --- Subscriptions ------------------------------------------------------------


@transaction.atomic
def apply_subscription_snapshot(
    *,
    account: BillingAccount,
    provider: str,
    provider_subscription_id: str,
    state: str,
    plan: Plan,
    prices: list[tuple[PlanPrice, int]] | None = None,
    provider_customer_id: str = '',
    current_period_start: datetime | None = None,
    current_period_end: datetime | None = None,
    trial_end: datetime | None = None,
    cancel_at_period_end: bool = False,
    canceled_at: datetime | None = None,
    ended_at: datetime | None = None,
    sequence: int = 0,
    provider_event_id: str = '',
    request=None,
) -> tuple[Subscription, bool]:
    """Write one complete, normalized view of a provider subscription.

    A **snapshot**, not a patch: the caller has read the whole object from the
    provider (or from an event that carries the whole object), so every field is
    replaced together. A partial update would let a webhook that mentions only the
    status leave a stale period end behind, and the period end is half of the
    entitlement decision.

    Returns `(subscription, changed)`. `changed` is False for a re-delivery that
    would write exactly what is already there, so a duplicate event produces no
    audit noise and no spurious "state changed" record.

    Ordering is the caller's responsibility to *detect* and this function's to
    *enforce*: an event whose `sequence` is at or below the stored one raises
    `BillingConflict`, because applying it would move the row backwards.
    """
    existing = (
        Subscription.objects.select_for_update()
        .filter(provider=provider, provider_subscription_id=provider_subscription_id)
        .first()
    )

    if existing is not None:
        if existing.account_id != account.id:
            # The same provider subscription arriving for a second organisation
            # is either a provider mistake or a migration mapping error. Either
            # way, silently re-pointing it would move a paid subscription between
            # customers.
            raise BillingConflict(
                'Provider subscription is already held by a different billing account.'
            )
        if sequence and sequence < existing.provider_sequence:
            raise BillingConflict('Provider event is older than the state already applied.')
        # Equality is deliberately *not* a conflict. A provider re-describing the
        # same version of an object — a second event type about one change, a
        # replay under a fresh event id — carries the same sequence, and refusing
        # it would report an ordinary duplicate as an ordering incident. It
        # applies, finds every field identical, and reports `changed=False`.

    before_state = existing.state if existing else None

    values = {
        'account': account,
        'plan': plan,
        'state': state,
        'provider_customer_id': provider_customer_id,
        'current_period_start': current_period_start,
        'current_period_end': current_period_end,
        'trial_end': trial_end,
        'cancel_at_period_end': cancel_at_period_end,
        'canceled_at': canceled_at,
        'ended_at': ended_at,
        'provider_synced_at': timezone.now(),
    }
    if sequence:
        values['provider_sequence'] = sequence

    if existing is None:
        subscription = Subscription.objects.create(
            provider=provider,
            provider_subscription_id=provider_subscription_id,
            **values,
        )
        created = True
        fields_changed = True
    else:
        subscription = existing
        # `provider_synced_at` always moves, so it is excluded from the
        # comparison: a re-delivery that changes nothing a customer would notice
        # must not be reported as a change.
        fields_changed = any(
            _current_value(subscription, key) != _incoming_value(key, value)
            for key, value in values.items()
            if key != 'provider_synced_at'
        )
        for key, value in values.items():
            setattr(subscription, key, value)
        subscription.save()
        created = False

    items_changed = _sync_items(subscription, prices or [])
    changed = created or fields_changed or items_changed

    if created:
        record(
            events.SUBSCRIPTION_CREATED,
            request=request,
            organization_id=account.organization_id,
            provider_event_id=provider_event_id,
            metadata={'plan': plan.key, 'state': state, 'provider': provider},
        )
    elif changed:
        record(
            events.SUBSCRIPTION_STATE_CHANGED,
            request=request,
            organization_id=account.organization_id,
            provider_event_id=provider_event_id,
            metadata={
                'plan': plan.key,
                'state_from': before_state,
                'state_to': state,
                'cancel_at_period_end': cancel_at_period_end,
                'items_changed': items_changed,
            },
        )

    return subscription, changed


_RELATION_FIELDS = ('account', 'plan')


def _current_value(subscription: Subscription, key: str):
    """The stored value, comparing relations by id rather than by instance."""
    return getattr(subscription, f'{key}_id' if key in _RELATION_FIELDS else key)


def _incoming_value(key: str, value):
    return value.pk if key in _RELATION_FIELDS else value


def _sync_items(subscription: Subscription, prices: list[tuple[PlanPrice, int]]) -> bool:
    """Replace the subscription's items with exactly `prices`. Returns True if changed."""
    if not prices:
        # No item information in this snapshot. Deliberately a no-op rather than a
        # delete: a webhook that omits `items` is not a webhook saying the
        # subscription has no lines, and clearing them would drop entitlements.
        return False

    wanted = {price.id: quantity for price, quantity in prices}
    existing = {item.price_id: item for item in subscription.items.all()}

    changed = False
    for price, quantity in prices:
        item = existing.get(price.id)
        if item is None:
            SubscriptionItem.objects.create(
                subscription=subscription, price=price, quantity=quantity
            )
            changed = True
        elif item.quantity != quantity:
            item.quantity = quantity
            item.save(update_fields=['quantity', 'updated_at'])
            changed = True

    for price_id, item in existing.items():
        if price_id not in wanted:
            item.delete()
            changed = True

    return changed


# --- Complimentary grants -----------------------------------------------------


def grant_complimentary(
    *,
    account: BillingAccount,
    plan: Plan,
    expires_at: datetime,
    reason: str = '',
    granted_by_user_id=None,
    request=None,
) -> ComplimentaryGrant:
    """Give an organisation time-limited access without payment."""
    if expires_at <= timezone.now():
        raise BillingConflict('A complimentary grant must expire in the future.')

    grant = ComplimentaryGrant.objects.create(
        account=account,
        plan=plan,
        expires_at=expires_at,
        reason=reason[:255],
        granted_by_user_id=granted_by_user_id,
    )
    record(
        events.COMPLIMENTARY_GRANT_CREATED,
        request=request,
        organization_id=account.organization_id,
        actor_user_id=granted_by_user_id,
        support_access=True,
        metadata={'plan': plan.key, 'expires_at': expires_at.isoformat(), 'reason': reason[:255]},
    )
    return grant


def revoke_complimentary(
    grant: ComplimentaryGrant, *, revoked_by_user_id=None, request=None
) -> ComplimentaryGrant:
    if grant.revoked_at is not None:
        return grant
    grant.revoked_at = timezone.now()
    grant.revoked_by_user_id = revoked_by_user_id
    grant.save(update_fields=['revoked_at', 'revoked_by_user_id', 'updated_at'])
    record(
        events.COMPLIMENTARY_GRANT_REVOKED,
        request=request,
        organization_id=grant.account.organization_id,
        actor_user_id=revoked_by_user_id,
        support_access=True,
        metadata={'plan': grant.plan.key},
    )
    return grant


# --- Invoices -----------------------------------------------------------------


def record_invoice_reference(
    *,
    account: BillingAccount,
    provider: str,
    provider_invoice_id: str,
    status: str,
    number: str = '',
    total_minor: int = 0,
    currency: str = 'GBP',
    issued_at: datetime | None = None,
    hosted_url: str = '',
    subscription: Subscription | None = None,
) -> InvoiceReference:
    reference, _ = InvoiceReference.objects.update_or_create(
        provider=provider,
        provider_invoice_id=provider_invoice_id,
        defaults={
            'account': account,
            'subscription': subscription,
            'status': status,
            'number': number,
            'total_minor': total_minor,
            'currency': currency,
            'issued_at': issued_at,
            'hosted_url': hosted_url,
        },
    )
    return reference
