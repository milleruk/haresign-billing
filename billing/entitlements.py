"""Deriving effective entitlements. The single source of the answer.

Three rules govern this module and none of them bends.

**Derived, never stored.** There is no writable `is_entitled` column anywhere in
the schema. Everything below is a pure function of subscription state, plan
configuration, complimentary grants and the clock. A cached *response* is a
performance decision made by the caller; a cached *answer* stored as truth would
be a second source of it.

**Roles do not grant entitlements.** Being an Identity organisation administrator
lets you *manage* billing. It does not give you anything paid. The monolith's
`user.is_staff or user.is_superuser → every entitlement` bypass is deliberately
not reproduced; see docs/entitlements.md decision D-1.

**Fail closed.** Every path that cannot establish entitlement returns "not
entitled". An unknown provider state, a plan with no products, a subscription
whose period lapsed while the provider went quiet, a missing billing account —
all of them are `False`. A paid feature that opens when the billing system is
confused is not a paid feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from django.utils import timezone

from .models import BillingAccount, ComplimentaryGrant, MemberOrganizationLink, Subscription

# --- The state table ----------------------------------------------------------
# Which normalized subscription states grant their plan's products.
#
# Transcribed from the monolith, which granted access for `active` and `trialing`
# only (`Subscription.ACCESS_STATUSES`) and additionally required that
# `current_period_end` had not passed. Every other state — `past_due`, `unpaid`,
# `paused`, `canceled`, `incomplete`, `incomplete_expired` — granted nothing.
#
# In particular **there is no grace period on `past_due`**. The monolith does not
# define one, so this service does not invent one. Whether a customer whose card
# failed should keep access for some days is a commercial decision and is listed
# as D-3 in docs/entitlements.md, unanswered.

GRANTING_STATES = frozenset(
    {
        Subscription.State.ACTIVE,
        Subscription.State.TRIALING,
    }
)

# Stated explicitly rather than left as "everything else", so that adding a state
# to the enum without deciding what it means fails a test instead of silently
# joining the non-granting set by default.
NON_GRANTING_STATES = frozenset(
    {
        Subscription.State.PAST_DUE,
        Subscription.State.UNPAID,
        Subscription.State.PAUSED,
        Subscription.State.CANCELED,
        Subscription.State.INCOMPLETE,
        Subscription.State.INCOMPLETE_EXPIRED,
        Subscription.State.UNKNOWN,
    }
)


@dataclass(frozen=True)
class ProductEntitlement:
    """One product, and why it is or is not held."""

    product_key: str
    entitled: bool
    # 'subscription' | 'complimentary_grant' | 'member_organization' | 'none'
    source: str = 'none'
    # When the entitlement is currently known to end. None means "no end date is
    # known", which is not the same as "forever" — a subscription whose provider
    # has never told us a period end has None here and the UI says so.
    effective_until: datetime | None = None


@dataclass(frozen=True)
class OrganizationEntitlements:
    """The complete effective answer for one organisation at one moment."""

    organization_id: str
    products: dict[str, ProductEntitlement] = field(default_factory=dict)
    evaluated_at: datetime | None = None

    def holds(self, product_key: str) -> bool:
        entry = self.products.get(product_key)
        return bool(entry and entry.entitled)

    @property
    def entitled_keys(self) -> list[str]:
        return sorted(key for key, entry in self.products.items() if entry.entitled)


def subscription_grants(subscription: Subscription, *, now: datetime | None = None) -> bool:
    """Does this subscription currently grant its plans' products?

    Two independent conditions, both required.

    The **state** must be one that grants. `cancel_at_period_end` is deliberately
    not consulted here: a subscription cancelled for the end of the period is
    still `active` and the customer has paid for the rest of it. What ends their
    access is the period ending, which is the second condition.

    The **period must not have lapsed.** A row saying `active` with a
    `current_period_end` in the past means the provider stopped telling us things,
    not that the customer has permanent access. A subscription with no known
    period end passes this check — that is the honest reading of "the provider has
    not given us an end date", and it is the state a fresh trial is in.
    """
    now = now or timezone.now()

    if subscription.state not in GRANTING_STATES:
        return False
    return not (subscription.current_period_end and subscription.current_period_end <= now)


def _apply(
    products: dict[str, ProductEntitlement],
    keys,
    *,
    source: str,
    until: datetime | None,
) -> None:
    """Record entitlement for `keys`, keeping the *latest* known end date.

    An organisation covered both by its own monthly subscription and by its PCN's
    annual one should be told the later date — quoting the earlier one would warn
    a customer about an expiry that will not happen.
    """
    for key in keys:
        existing = products.get(key)
        if existing and existing.entitled:
            # None means "no known end". A known date must never overwrite it: a
            # source that runs indefinitely outlasts one that expires in March.
            if existing.effective_until is None or until is None:
                until_value = None
            else:
                until_value = max(existing.effective_until, until)
            products[key] = ProductEntitlement(
                product_key=key,
                entitled=True,
                source=existing.source,
                effective_until=until_value,
            )
        else:
            products[key] = ProductEntitlement(
                product_key=key, entitled=True, source=source, effective_until=until
            )


def _plan_products(plan) -> list[str]:
    return sorted(product.key for product in plan.products.all() if product.is_active)


def _covering_account_ids(organization_id, *, now) -> list:
    """Organisations whose subscriptions may cover `organization_id`.

    Itself, plus any parent it is a member of. The parent's subscription only
    counts if that parent's plan says it covers members — checked at the point of
    use, not here, because a PCN may hold both a covering plan and a
    non-covering one.
    """
    parents = list(
        MemberOrganizationLink.objects.filter(child_organization_id=organization_id).values_list(
            'parent_organization_id', flat=True
        )
    )
    return [organization_id, *parents]


def entitlements_for_organization(
    organization_id, *, now: datetime | None = None
) -> OrganizationEntitlements:
    """The effective entitlement set for one Identity organisation.

    Returns an answer for *every* active product in the catalogue, entitled or
    not, so a consumer asking about a product this organisation does not hold gets
    an explicit `False` rather than a missing key it has to interpret.
    """
    from catalog.models import Product

    now = now or timezone.now()

    products: dict[str, ProductEntitlement] = {}

    account = BillingAccount.objects.filter(organization_id=organization_id).first()
    covering_ids = _covering_account_ids(organization_id, now=now)

    subscriptions = (
        Subscription.objects.filter(
            account__organization_id__in=covering_ids,
            account__status=BillingAccount.Status.ACTIVE,
            state__in=GRANTING_STATES,
        )
        .select_related('account', 'plan')
        .prefetch_related('plan__products', 'items__price__plan__products')
    )

    for subscription in subscriptions:
        if not subscription_grants(subscription, now=now):
            continue

        own = str(subscription.account.organization_id) == str(organization_id)
        if not own and not subscription.plan.covers_member_organizations:
            # A parent's subscription on a plan that does not cover members is
            # simply not this organisation's business.
            continue

        # The union of every item's plan, falling back to the subscription's own
        # plan when it has no items. A multi-line subscription grants everything
        # its lines are for — the monolith read only the first line and lost the
        # rest.
        plans = {subscription.plan}
        plans.update(item.price.plan for item in subscription.items.all())

        for plan in plans:
            _apply(
                products,
                _plan_products(plan),
                source='subscription' if own else 'member_organization',
                until=subscription.current_period_end,
            )

    if account is not None:
        grants = (
            ComplimentaryGrant.objects.filter(
                account=account, revoked_at__isnull=True, expires_at__gt=now
            )
            .select_related('plan')
            .prefetch_related('plan__products')
        )
        for grant in grants:
            _apply(
                products,
                _plan_products(grant.plan),
                source='complimentary_grant',
                until=grant.expires_at,
            )

    # Everything else in the catalogue is explicitly not held.
    for key in Product.objects.filter(is_active=True).values_list('key', flat=True):
        products.setdefault(key, ProductEntitlement(product_key=key, entitled=False))

    return OrganizationEntitlements(
        organization_id=str(organization_id), products=products, evaluated_at=now
    )


def organization_holds(organization_id, product_key: str, *, now: datetime | None = None) -> bool:
    """Convenience for a single question. Fails closed on anything unexpected."""
    try:
        return entitlements_for_organization(organization_id, now=now).holds(product_key)
    except Exception:
        # An exception here is a bug, and the correct behaviour of a paid gate in
        # the presence of a bug is to close. Logged by the caller's own handler;
        # swallowing it into `True` is the failure this whole module exists to
        # prevent.
        return False
