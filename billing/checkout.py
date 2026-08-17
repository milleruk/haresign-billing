"""Starting hosted checkout, and opening the hosted customer portal.

The two methods Phase 4A left as `501 not_implemented`. Both are now real, and
both are still behind `BILLING_CHECKOUT_ENABLED`, which is off in every
environment including production.

**Nothing on this path is decided by the browser.** That is the whole design. The
browser sends a plan key and an interval — an identifier for something in *our*
catalogue — and everything that has consequences is resolved server-side:

* the **price** comes from a verified `PlanPrice` row, never from the request. A
  price id from a form is a price id an attacker picks, and Stripe will happily
  charge whatever it is told;
* the **payer** is the organisation the session administers, re-checked at the
  moment of creation rather than trusted from the URL that reached the view;
* the **beneficiary** is either the payer or an organisation the *current*
  organisation graph confirms it may sponsor, refreshed immediately beforehand;
* the **customer** is the payer's own `provider_customer_id`, never one supplied.

The gate ordering matters and is deliberate: authorization is rechecked before
the graph is refreshed, and the graph is refreshed before the duplicate-coverage
check, so a request that will be refused is refused before it costs an outbound
call to Identity.
"""

from __future__ import annotations

import hashlib
import logging

from django.conf import settings
from django.utils import timezone

from audit import events
from audit.services import record
from catalog.models import Plan, PlanPrice
from providers.base import ProviderError, get_provider

from .entitlements import entitlements_for_organization
from .models import BillingAccount
from .services import get_or_create_account

logger = logging.getLogger('haresign.billing')


class CheckoutRefused(RuntimeError):
    """A checkout that must not proceed. The message is safe to show an administrator."""


def _refuse(request, organization_id, reason: str, detail: str) -> CheckoutRefused:
    record(
        events.CHECKOUT_REFUSED,
        request=request,
        organization_id=organization_id,
        metadata={'reason': reason},
    )
    return CheckoutRefused(detail)


def idempotency_key(*, payer_id, beneficiary_id, price_id, actor_user_id) -> str:
    """A stable key for one intent, so a double-click does not become two sessions.

    Derived rather than random: the point of an idempotency key is that the
    *retry* carries the same one, and a random key regenerated per request is a
    key that never matches. It is a digest so that no organisation or user UUID
    travels to the provider inside a header value.

    The hour is in the material deliberately. A key that lasted forever would mean
    an administrator who genuinely wants to buy the same thing again next month
    silently receives last month's session.
    """
    material = '|'.join(
        [
            str(payer_id),
            str(beneficiary_id),
            str(price_id),
            str(actor_user_id or ''),
            timezone.now().strftime('%Y-%m-%dT%H'),
        ]
    )
    return f'hsbill_{hashlib.sha256(material.encode()).hexdigest()[:40]}'


def resolve_price(plan_key: str, interval: str) -> PlanPrice:
    """The one price row this purchase may use. Verified, active and purchasable.

    A price with no `provider_price_id` is displayable but not purchasable, which
    is the state every price is in until Phase 4B.2's read-only Stripe
    verification populates them from the catalogue that already exists. Refusing
    here rather than passing an empty id to the provider means the failure is ours
    and legible, not a provider error nobody can act on.
    """
    price = (
        PlanPrice.objects.filter(
            plan__key=plan_key,
            interval=interval,
            is_active=True,
            plan__is_active=True,
        )
        .select_related('plan')
        .first()
    )
    if price is None:
        raise CheckoutRefused('That plan and billing interval is not available.')
    if not price.is_purchasable:
        raise CheckoutRefused(
            'That plan cannot be purchased yet — it has no verified price at the payment provider.'
        )
    return price


def check_not_already_covered(beneficiary_organization_id, plan: Plan) -> None:
    """Refuse a purchase for a beneficiary that already holds everything it grants.

    Not a nicety. Two subscriptions covering the same practice for the same
    product is a customer paying twice for one thing, and it is the commonest way
    a PCN and a practice both end up buying the same tool in the same week.

    Deliberately checks the *products*, not the plan: a practice already covered
    by its PCN's bundle does not need a second, smaller plan whose every product
    it already holds. A plan that would add even one new product is allowed
    through — that is an upgrade, not a duplicate.
    """
    granted = {product.key for product in plan.products.all() if product.is_active}
    if not granted:
        return

    held = entitlements_for_organization(beneficiary_organization_id)
    missing = {key for key in granted if not held.holds(key)}
    if not missing:
        raise CheckoutRefused(
            'That organisation already has everything this plan provides, through '
            'a subscription it holds or one sponsoring it. Buying it again would '
            'charge twice for the same access.'
        )


def create_checkout_session(
    *,
    access,
    plan_key: str,
    interval: str,
    beneficiary_organization_id=None,
    request=None,
    actor_user_id=None,
) -> str:
    """Create a provider-hosted checkout session and return its URL.

    `access` is the `OrganizationAccess` the view's decorator resolved — the payer.
    Passing it rather than an organisation UUID is the point: this function cannot
    be called with an organisation the caller has not proved they administer.
    """
    if not settings.BILLING_CHECKOUT_ENABLED:
        # The production gate. Off everywhere, including production, until the
        # cutover gate in docs/stripe-cutover.md is passed with a human present.
        raise _refuse(
            request,
            access.organization_id,
            'checkout_disabled',
            'Subscriptions are not yet managed here.',
        )

    payer_id = str(access.organization_id)
    beneficiary_id = str(beneficiary_organization_id or payer_id)

    # --- Authorization, rechecked here rather than trusted from the view --------
    if beneficiary_id != payer_id:
        if not access.may_sponsor:
            raise _refuse(
                request,
                payer_id,
                'not_a_sponsor',
                'Only a PCN can purchase on behalf of another organisation.',
            )
        # Refresh before deciding. A sponsored purchase is exactly the decision the
        # projection exists to inform, and taking it against an hour-old graph
        # would be buying for a practice that may have left this morning.
        from identity.graph import GraphError, sponsorship_is_valid

        try:
            from identity.graph import refresh

            refresh(request=request)
        except GraphError:
            # A failed refresh is not fatal on its own — a fresh projection may
            # already be held — but it must not be silent.
            logger.warning(
                'checkout: organisation graph refresh failed before a sponsored purchase'
            )

        if not sponsorship_is_valid(payer_id, beneficiary_id):
            raise _refuse(
                request,
                payer_id,
                'sponsorship_not_confirmed',
                'That practice is not currently a member of this PCN, or the '
                'organisation directory could not be confirmed. Nothing has been '
                'charged.',
            )

    price = resolve_price(plan_key, interval)

    if beneficiary_id != payer_id and not price.plan.covers_member_organizations:
        raise _refuse(
            request,
            payer_id,
            'plan_does_not_sponsor',
            'That plan cannot be bought on behalf of another organisation.',
        )

    check_not_already_covered(beneficiary_id, price.plan)

    account, _ = get_or_create_account(
        payer_id,
        name=access.organization_name,
        organization_type=access.organization_type,
        request=request,
    )
    if account.status != BillingAccount.Status.ACTIVE:
        raise _refuse(
            request, payer_id, 'account_closed', 'This organisation’s billing account is closed.'
        )

    provider = get_provider()
    try:
        url = provider.create_checkout_session(
            # Server-resolved, every one of them.
            provider_price_id=price.provider_price_id,
            quantity=1,
            customer_id=account.provider_customer_id,
            organization_id=payer_id,
            beneficiary_organization_id=beneficiary_id,
            success_url=f'{settings.SITE_BASE_URL}/organizations/{payer_id}/checkout/complete/',
            cancel_url=f'{settings.SITE_BASE_URL}/organizations/{payer_id}/',
            idempotency_key=idempotency_key(
                payer_id=payer_id,
                beneficiary_id=beneficiary_id,
                price_id=price.provider_price_id,
                actor_user_id=actor_user_id,
            ),
        )
    except ProviderError as exc:
        logger.warning('checkout: provider refused a session (%s)', type(exc).__name__)
        raise _refuse(
            request,
            payer_id,
            'provider_error',
            'The payment provider could not start a checkout. Nothing has been charged.',
        ) from exc

    record(
        events.CHECKOUT_STARTED,
        request=request,
        organization_id=payer_id,
        actor_user_id=actor_user_id,
        metadata={
            'plan': price.plan.key,
            'interval': price.interval,
            # Whether it is sponsored, not who for: the beneficiary UUID goes on
            # the allocation when the subscription arrives, which is where it
            # belongs. No amount, no currency, no provider identifier here.
            'sponsored': beneficiary_id != payer_id,
        },
    )
    return url


def create_portal_session(*, access, request=None, actor_user_id=None) -> str:
    """Open the provider's hosted customer portal for the paying organisation.

    Only ever for the organisation whose administration was just proved, and only
    when that organisation is actually a provider customer. An organisation with
    no `provider_customer_id` has never paid us anything, and sending it to a
    portal would either error at the provider or — worse, depending on how the
    call were built — open somebody else's.
    """
    if not settings.BILLING_CHECKOUT_ENABLED:
        record(
            events.PORTAL_REFUSED,
            request=request,
            organization_id=access.organization_id,
            metadata={'reason': 'portal_disabled'},
        )
        raise CheckoutRefused('The billing portal is not yet available.')

    account = BillingAccount.objects.filter(organization_id=access.organization_id).first()
    if account is None or not account.provider_customer_id:
        record(
            events.PORTAL_REFUSED,
            request=request,
            organization_id=access.organization_id,
            metadata={'reason': 'no_provider_customer'},
        )
        raise CheckoutRefused('There is no payment account for this organisation yet.')

    provider = get_provider()
    try:
        url = provider.create_portal_session(
            customer_id=account.provider_customer_id,
            return_url=(f'{settings.SITE_BASE_URL}/organizations/{access.organization_id}/'),
        )
    except ProviderError as exc:
        logger.warning('portal: provider refused a session (%s)', type(exc).__name__)
        raise CheckoutRefused('The payment provider could not open the portal.') from exc

    record(
        events.PORTAL_OPENED,
        request=request,
        organization_id=access.organization_id,
        actor_user_id=actor_user_id,
    )
    return url
