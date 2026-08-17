"""The organisation billing pages.

Every view here is gated by `require_organization_admin`, which resolves the
URL's organisation UUID against the memberships Identity reported for *this
session* and hands back an `access` object. Views use `access.organization_id`,
never the raw URL kwarg — the two are the same string today, and the day they are
not is the day the URL won.

Nothing here mutates provider state. Checkout and portal are links out to
provider-hosted pages, disabled entirely in this phase.
"""

from __future__ import annotations

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from catalog.models import Plan
from identity.authorization import administered_memberships, require_organization_admin

from .checkout import CheckoutRefused, create_checkout_session, create_portal_session
from .entitlements import entitlements_for_organization
from .models import BillingAccount, InvoiceReference, Subscription
from .services import get_or_create_account
from .sponsorship import live_allocations_for, sponsored_allocations_by


@require_GET
@never_cache
def home(request):
    """Pick an organisation, or explain that there is nothing to pick.

    Lists only organisations this session may *administer*. Offering one the
    authorization check would refuse produces a menu whose items 404, which reads
    as a broken product rather than as a boundary.
    """
    if not request.user.is_authenticated:
        return redirect(f'/auth/login/?next={request.path}')

    return render(
        request,
        'billing/home.html',
        {'memberships': administered_memberships(request)},
    )


def _subscription_summary(account: BillingAccount | None) -> dict:
    """The one shape both the page and the summary endpoint read from.

    A single derivation, so the card in Identity and the page in Billing can never
    disagree about what state an organisation is in.
    """
    if account is None:
        return {'state': 'none', 'state_label': 'No subscription', 'plan_name': '', 'plan_key': ''}

    subscription = (
        Subscription.objects.filter(account=account)
        .exclude(state=Subscription.State.INCOMPLETE_EXPIRED)
        .select_related('plan')
        .order_by('-created_at')
        .first()
    )
    if subscription is None:
        return {'state': 'none', 'state_label': 'No subscription', 'plan_name': '', 'plan_key': ''}

    # Two dates that are not the same thing and are never merged. A subscription
    # cancelling at period end *renews* on no date and *ends* on one; quoting the
    # wrong one to a customer is either a surprise charge or a surprise lockout.
    renews_at = None
    ends_at = None
    if subscription.cancel_at_period_end:
        ends_at = subscription.current_period_end
    elif subscription.state in (Subscription.State.ACTIVE, Subscription.State.TRIALING):
        renews_at = subscription.current_period_end

    return {
        'state': subscription.state,
        'state_label': subscription.get_state_display(),
        'plan_name': subscription.plan.name,
        'plan_key': subscription.plan.key,
        'renews_at': renews_at,
        'ends_at': ends_at,
        'trial_end': subscription.trial_end,
        'cancel_at_period_end': subscription.cancel_at_period_end,
        'synced_at': subscription.provider_synced_at,
        'subscription': subscription,
    }


@require_GET
@never_cache
@require_organization_admin
def organization_billing(request, organization_id, access):
    """The organisation's billing overview.

    Everything on this page is scoped to the organisation whose administration
    was just proved, and the scoping is the privacy boundary:

    * **Invoices** are filtered to this organisation's own billing account. A
      practice sponsored by its PCN sees none of the PCN's, because they are not
      its invoices — they are the PCN's, and they name what the PCN pays.
    * **Sponsorships received** are shown as a fact, without a subscription, an
      amount, an invoice or a cancel button. "This is available through your PCN"
      is what a sponsored practice needs to know and is the whole of what it may
      be told.
    * **Sponsorships given** are shown only to the payer, which is the only
      organisation that may manage them.
    """
    account = BillingAccount.objects.filter(organization_id=access.organization_id).first()
    summary = _subscription_summary(account)
    entitlements = entitlements_for_organization(access.organization_id)

    # Allocations *to* this organisation that somebody else pays for. Deliberately
    # carries the plan name and nothing financial: no amount, no invoice, no
    # provider reference, no state the payer would consider theirs.
    sponsored_by_others = [
        {
            'plan_name': allocation.subscription.plan.name,
            'sponsor_organization_id': str(allocation.subscription.account.organization_id),
            'sponsor_name': allocation.subscription.account.organization_name,
        }
        for allocation in live_allocations_for(access.organization_id)
        if str(allocation.subscription.account.organization_id) != str(access.organization_id)
    ]

    return render(
        request,
        'billing/organization.html',
        {
            'access': access,
            'account': account,
            'organization_name': (
                access.organization_name
                or (account.organization_name if account else '')
                or 'This organisation'
            ),
            'summary': summary,
            'products': sorted(entitlements.products.values(), key=lambda e: e.product_key),
            'contacts': list(account.contacts.all()) if account else [],
            'invoices': (
                list(InvoiceReference.objects.filter(account=account)[:12]) if account else []
            ),
            'sponsored_by_others': sponsored_by_others,
            'sponsorships_given': (
                list(sponsored_allocations_by(access.organization_id)) if access.may_sponsor else []
            ),
            # Off in this phase. The template renders the plan and state without a
            # purchase route rather than showing a button that cannot work.
            'checkout_enabled': settings.BILLING_CHECKOUT_ENABLED,
            'plans': Plan.objects.filter(is_active=True).prefetch_related('products', 'prices'),
        },
    )


@require_GET
@never_cache
@require_organization_admin
def organization_summary(request, organization_id, access):
    """A small JSON summary, shaped for the subscription card Identity renders.

    Identity **displays** this and links here. It must not persist any of it:
    Billing is the authority, and a second stored copy is a second answer that
    will eventually be the wrong one. The card says what it was told and when.

    Authorized by the same session-membership check as the page — this is a
    browser endpoint for a signed-in administrator, not the service-to-service
    API, which lives at `/api/v1/` behind a signed credential.
    """
    account = BillingAccount.objects.filter(organization_id=access.organization_id).first()
    summary = _subscription_summary(account)

    return JsonResponse(
        {
            'organization_id': access.organization_id,
            'state': summary['state'],
            'state_label': summary['state_label'],
            'plan_name': summary['plan_name'],
            'renews_at': summary.get('renews_at').isoformat() if summary.get('renews_at') else None,
            'ends_at': summary.get('ends_at').isoformat() if summary.get('ends_at') else None,
            'manage_url': request.build_absolute_uri(f'/organizations/{access.organization_id}/'),
            'as_of': timezone.now().isoformat(),
        }
    )


@require_POST
@never_cache
@require_organization_admin
def start_checkout(request, organization_id, access):
    """Begin hosted checkout.

    POST-only and CSRF-protected: the endpoint's shape is part of the contract,
    and a GET that starts a purchase is fired by any link scanner pointed at it.

    The view is thin on purpose. Every decision with a consequence — price, payer,
    beneficiary, sponsorship, duplicate coverage, the gate — is made in
    `billing/checkout.py`, so there is one place where one of them could be missed
    rather than one place per entry point.
    """
    get_or_create_account(
        access.organization_id,
        name=access.organization_name,
        organization_type=access.organization_type,
        request=request,
    )

    if not settings.BILLING_CHECKOUT_ENABLED:
        # Answered here as well as in the service, so the disabled case keeps its
        # documented 503 shape rather than becoming a generic refusal.
        return JsonResponse(
            {'error': 'checkout_disabled', 'detail': 'Subscriptions are not yet managed here.'},
            status=503,
        )

    try:
        url = create_checkout_session(
            access=access,
            plan_key=request.POST.get('plan', ''),
            interval=request.POST.get('interval', 'month'),
            beneficiary_organization_id=request.POST.get('beneficiary') or None,
            request=request,
            actor_user_id=getattr(request.user, 'identity_user_id', None),
        )
    except CheckoutRefused as exc:
        # 409, not 400 and not 500: the request was well-formed and the caller was
        # authorised; the *state* refuses it. The sentence is written to be safe to
        # show to the administrator reading it.
        return JsonResponse({'error': 'checkout_refused', 'detail': str(exc)}, status=409)

    # A redirect to a provider-hosted page, deliberately rather than a
    # cross-origin form post — which is what lets `form-action 'self'` stay closed
    # in the CSP.
    return redirect(url)


@require_POST
@never_cache
@require_organization_admin
def open_portal(request, organization_id, access):
    """Open the provider's hosted customer portal for the paying organisation."""
    if not settings.BILLING_CHECKOUT_ENABLED:
        return JsonResponse(
            {'error': 'portal_disabled', 'detail': 'The billing portal is not yet available.'},
            status=503,
        )

    try:
        url = create_portal_session(
            access=access,
            request=request,
            actor_user_id=getattr(request.user, 'identity_user_id', None),
        )
    except CheckoutRefused as exc:
        return JsonResponse({'error': 'portal_refused', 'detail': str(exc)}, status=409)

    return redirect(url)
