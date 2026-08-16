"""Fixtures shared across the suite.

Deliberately explicit rather than a factory library: every test in this repository
is about a boundary or a state machine, and a fixture that quietly fills in a
plausible default is a fixture that can hide the very thing under test.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.utils import timezone

from billing.models import BillingAccount, MemberOrganizationLink, Subscription, SubscriptionItem
from catalog.models import Plan, PlanPrice
from identity.models import IdentityUser, SessionMembership


def organization_id() -> uuid.UUID:
    return uuid.uuid4()


def account(*, organization=None, name='Test Practice', org_type='practice') -> BillingAccount:
    return BillingAccount.objects.create(
        organization_id=organization or organization_id(),
        organization_name=name,
        organization_type=org_type,
    )


def plan(key='practice') -> Plan:
    return Plan.objects.get(key=key)


def price(plan_key='practice', interval='month', provider_price_id='') -> PlanPrice:
    row = PlanPrice.objects.get(plan__key=plan_key, interval=interval)
    if provider_price_id and row.provider_price_id != provider_price_id:
        row.provider_price_id = provider_price_id
        row.save(update_fields=['provider_price_id'])
    return row


# A sentinel distinct from None, because `period_end=None` is a meaningful value
# here — "the provider has given us no end date" — and must not collapse into the
# default the way a plain `None` default would.
UNSET = object()


def subscription(
    *,
    account_obj=None,
    plan_key='practice',
    state=Subscription.State.ACTIVE,
    period_end=UNSET,
    cancel_at_period_end=False,
    provider_subscription_id='',
    with_item=True,
) -> Subscription:
    account_obj = account_obj or account()
    plan_obj = plan(plan_key)
    row = Subscription.objects.create(
        account=account_obj,
        plan=plan_obj,
        state=state,
        provider='fake',
        provider_subscription_id=provider_subscription_id or f'sub_{uuid.uuid4().hex[:12]}',
        current_period_end=(
            timezone.now() + timedelta(days=30) if period_end is UNSET else period_end
        ),
        cancel_at_period_end=cancel_at_period_end,
    )
    if with_item:
        SubscriptionItem.objects.create(
            subscription=row, price=price(plan_key, 'month'), quantity=1
        )
    return row


def member_link(parent, child) -> MemberOrganizationLink:
    return MemberOrganizationLink.objects.create(
        parent_organization_id=parent,
        child_organization_id=child,
        observed_at=timezone.now(),
    )


def identity_user(*, platform_admin=False, name='Test Person') -> IdentityUser:
    user = IdentityUser.objects.create_user(identity_user_id=uuid.uuid4(), display_name=name)
    if platform_admin:
        user.is_platform_admin = True
        user.save(update_fields=['is_platform_admin'])
    return user


def sign_in(client, user, memberships=()):
    """Establish a session the way the OIDC callback does.

    Deliberately mirrors `identity.views` rather than calling it: the tests that
    exercise the OIDC flow itself are in `identity/tests/`, and every *other* test
    needs a signed-in session without standing up a provider.
    """
    client.force_login(user, backend='identity.backends.IdentityOIDCBackend')
    session_key = client.session.session_key
    for entry in memberships:
        SessionMembership.objects.create(
            session_key=session_key,
            user=user,
            organization_id=entry['organization_id'],
            organization_name=entry.get('name', ''),
            organization_type=entry.get('type', 'practice'),
            role=entry.get('role', 'organization_admin'),
            is_administrator=entry.get('role', 'organization_admin') == 'organization_admin',
            captured_at=entry.get('captured_at', timezone.now()),
        )
    return client
