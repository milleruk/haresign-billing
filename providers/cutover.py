"""The pre-cutover reconciliation.

`providers/reconciliation.py` answers "does local subscription state still match
the provider's" — a running-system question, asked repeatedly, and it can be told
to correct what it finds. This module answers a different and one-directional
question: **is it safe to cut over at all?**

It compares three populations that must agree before Billing becomes the system
of record:

* what the provider holds — customers and subscriptions;
* what Billing holds — accounts, subscriptions and catalogue price references;
* what Identity holds — the organisation graph projection that says an
  organisation exists and is active.

It is **read-only, always.** There is no apply flag and no correcting branch. A
reconciliation that can write is a reconciliation that can make its own report
come out clean, and the entire value of this one is that it cannot.

Its output is counts. Not one customer id, subscription id, organisation name or
email address appears in the report, at any verbosity, because this report is
written to end up in a ticket and a chat message. Conflicts are counted by *kind*;
finding out which row is which is a job for the audit trail and someone
authorised to ask.

**Any conflict stops the cutover.** The caller is expected to treat a non-zero
`conflicts` total as a hard stop, and the management command exits non-zero so
that a script cannot proceed past one by accident.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from audit import events as audit_events
from audit.services import record
from billing.models import BillingAccount, ComplimentaryGrant, Subscription
from catalog.models import PlanPrice
from identity.graph import current_graph
from identity.graph_models import GraphOrganization

from .discovery import discovery_provider
from .mapping import subscription_state

logger = logging.getLogger('haresign.billing')


@dataclass
class CutoverReconciliation:
    """The full picture, in counts. Safe to paste anywhere."""

    provider: str

    # --- Provider side --------------------------------------------------------
    provider_customers: int = 0
    provider_customers_deleted: int = 0
    provider_subscriptions: int = 0
    provider_subscriptions_by_status: dict[str, int] = field(default_factory=dict)

    # --- Mapped to Billing ----------------------------------------------------
    customers_mapped_to_billing: int = 0
    subscriptions_mapped_to_billing: int = 0
    billing_accounts: int = 0
    billing_accounts_with_customer: int = 0
    billing_subscriptions: int = 0

    # --- Mapped to Identity ---------------------------------------------------
    # Whether the organisation graph projection could be consulted at all. When it
    # could not, Identity coverage is reported as unknown rather than zero: zero
    # would read as "no organisation is known to Identity", which is a very
    # different and much more alarming claim than "we could not ask".
    identity_projection: str = 'missing'
    accounts_mapped_to_identity: int = 0
    accounts_not_in_identity: int = 0

    # --- Unmatched ------------------------------------------------------------
    provider_customers_unmatched: int = 0
    provider_subscriptions_unmatched: int = 0
    billing_subscriptions_unmatched: int = 0

    # --- Conflicts, by kind ---------------------------------------------------
    conflicts_by_kind: dict[str, int] = field(default_factory=dict)

    # --- Catalogue readiness --------------------------------------------------
    plan_prices: int = 0
    plan_prices_purchasable: int = 0

    # --- Declared exceptions --------------------------------------------------
    # Rows an operator has already looked at and knowingly accepted. Counted, and
    # counted separately, so an exception is a decision on the record rather than
    # a number quietly missing from a total.
    declared_exceptions: int = 0
    complimentary_grants_active: int = 0

    @property
    def conflicts(self) -> int:
        return sum(self.conflicts_by_kind.values())

    @property
    def blocks_cutover(self) -> bool:
        return bool(self.conflicts)

    @property
    def counts(self) -> dict[str, object]:
        return {
            'provider': self.provider,
            'provider_customers': self.provider_customers,
            'provider_customers_deleted': self.provider_customers_deleted,
            'provider_subscriptions': self.provider_subscriptions,
            'provider_subscriptions_by_status': dict(
                sorted(self.provider_subscriptions_by_status.items())
            ),
            'customers_mapped_to_billing': self.customers_mapped_to_billing,
            'subscriptions_mapped_to_billing': self.subscriptions_mapped_to_billing,
            'billing_accounts': self.billing_accounts,
            'billing_accounts_with_customer': self.billing_accounts_with_customer,
            'billing_subscriptions': self.billing_subscriptions,
            'identity_projection': self.identity_projection,
            'accounts_mapped_to_identity': self.accounts_mapped_to_identity,
            'accounts_not_in_identity': self.accounts_not_in_identity,
            'provider_customers_unmatched': self.provider_customers_unmatched,
            'provider_subscriptions_unmatched': self.provider_subscriptions_unmatched,
            'billing_subscriptions_unmatched': self.billing_subscriptions_unmatched,
            'conflicts': self.conflicts,
            'conflicts_by_kind': dict(sorted(self.conflicts_by_kind.items())),
            'plan_prices': self.plan_prices,
            'plan_prices_purchasable': self.plan_prices_purchasable,
            'declared_exceptions': self.declared_exceptions,
            'complimentary_grants_active': self.complimentary_grants_active,
        }


def reconcile_for_cutover(
    *, exceptions: set[str] | None = None, request=None
) -> CutoverReconciliation:
    """Compare provider, Billing and Identity. Reads only, and writes no state."""
    exceptions = {value.strip() for value in (exceptions or set()) if value.strip()}
    provider = discovery_provider()
    report = CutoverReconciliation(provider=provider.name)

    remote_customers = provider.list_customers()
    remote_subscriptions = provider.list_subscriptions()

    accounts = list(BillingAccount.objects.all())
    local_subscriptions = list(Subscription.objects.select_related('account').all())

    accounts_by_customer = {}
    conflicts: Counter[str] = Counter()
    for account in accounts:
        if not account.provider_customer_id:
            continue
        # The database's unique constraint makes this impossible; it is checked
        # anyway, because a reconciliation that assumes its own invariants holds
        # cannot report that one has been broken.
        if account.provider_customer_id in accounts_by_customer:
            conflicts['billing_accounts_sharing_a_customer'] += 1
        accounts_by_customer[account.provider_customer_id] = account

    organizations = {str(account.organization_id) for account in accounts}

    report.billing_accounts = len(accounts)
    report.billing_accounts_with_customer = len(accounts_by_customer)
    report.billing_subscriptions = len(local_subscriptions)

    # --- Customers ------------------------------------------------------------
    report.provider_customers = len(remote_customers)
    report.provider_customers_deleted = sum(1 for c in remote_customers if c.deleted)

    claimed_organizations: Counter[str] = Counter()
    for customer in remote_customers:
        if customer.customer_id in exceptions:
            report.declared_exceptions += 1
            continue

        account = accounts_by_customer.get(customer.customer_id)
        if account is None:
            # A customer at the provider that Billing does not own. Before
            # cutover that is an unmatched record to explain, not a licence to
            # create an account for it — see AGENTS.md: nothing is created to make
            # a reconciliation pass.
            report.provider_customers_unmatched += 1
        else:
            report.customers_mapped_to_billing += 1
            if customer.organization_id and customer.organization_id != str(
                account.organization_id
            ):
                # The provider's own metadata disagrees with which organisation
                # Billing says owns this customer. Exactly the shape of a
                # duplicate-ownership bug, and exactly what must not be guessed at.
                conflicts['customer_organization_metadata_disagrees'] += 1

        if customer.organization_id:
            claimed_organizations[customer.organization_id] += 1

    for _organization, count in claimed_organizations.items():
        if count > 1:
            conflicts['organization_claimed_by_multiple_customers'] += count - 1

    # --- Subscriptions --------------------------------------------------------
    report.provider_subscriptions = len(remote_subscriptions)
    report.provider_subscriptions_by_status = dict(
        Counter(subscription.status or 'unknown' for subscription in remote_subscriptions)
    )

    local_by_provider_id = {
        subscription.provider_subscription_id: subscription
        for subscription in local_subscriptions
        if subscription.provider_subscription_id
    }
    known_price_ids = set(
        PlanPrice.objects.exclude(provider_price_id='').values_list('provider_price_id', flat=True)
    )

    for subscription in remote_subscriptions:
        if subscription.subscription_id in exceptions:
            report.declared_exceptions += 1
            continue

        local = local_by_provider_id.get(subscription.subscription_id)
        if local is None:
            report.provider_subscriptions_unmatched += 1
            # An unmatched subscription that is *granting access right now* is a
            # different severity from an unmatched cancelled one: cutting over
            # would drop a paying customer's access.
            if subscription_state(subscription.status) in ('active', 'trialing'):
                conflicts['granting_subscription_not_in_billing'] += 1
            continue

        report.subscriptions_mapped_to_billing += 1
        if subscription_state(subscription.status) != local.state:
            conflicts['subscription_state_disagrees'] += 1
        if (
            subscription.customer_id
            and local.account.provider_customer_id
            and subscription.customer_id != local.account.provider_customer_id
        ):
            conflicts['subscription_customer_disagrees'] += 1
        if any(price.price_id not in known_price_ids for price in subscription.prices):
            # A price the catalogue cannot resolve means this subscription has no
            # plan and therefore grants nothing — silently, after cutover.
            conflicts['subscription_price_not_in_catalogue'] += 1

    remote_ids = {subscription.subscription_id for subscription in remote_subscriptions}
    report.billing_subscriptions_unmatched = sum(
        1
        for subscription in local_subscriptions
        if subscription.provider_subscription_id
        and subscription.provider_subscription_id not in remote_ids
        and subscription.provider_subscription_id not in exceptions
    )
    if report.billing_subscriptions_unmatched:
        conflicts['billing_subscription_absent_at_provider'] += (
            report.billing_subscriptions_unmatched
        )

    # --- Identity -------------------------------------------------------------
    graph = current_graph()
    if graph is None:
        report.identity_projection = 'missing'
    elif not graph.is_fresh:
        # Reported rather than trusted. A stale projection is exactly the state in
        # which "this organisation does not exist at Identity" is not a safe
        # conclusion to draw.
        report.identity_projection = 'stale'
    else:
        report.identity_projection = 'fresh'

    if graph is not None:
        known = {
            str(value)
            for value in GraphOrganization.objects.filter(graph=graph).values_list(
                'organization_id', flat=True
            )
        }
        report.accounts_mapped_to_identity = len(organizations & known)
        report.accounts_not_in_identity = len(organizations - known)
        if report.identity_projection == 'fresh' and report.accounts_not_in_identity:
            conflicts['billing_account_organization_unknown_to_identity'] += (
                report.accounts_not_in_identity
            )

    # --- Catalogue and grants -------------------------------------------------
    prices = list(PlanPrice.objects.select_related('plan').all())
    report.plan_prices = len(prices)
    report.plan_prices_purchasable = sum(1 for price in prices if price.is_purchasable)
    report.complimentary_grants_active = sum(
        1 for grant in ComplimentaryGrant.objects.all() if grant.is_live
    )

    report.conflicts_by_kind = dict(conflicts)

    record(
        audit_events.CUTOVER_RECONCILIATION_RUN,
        request=request,
        metadata=report.counts,
    )
    logger.info(
        'cutover reconciliation: %s conflicts across %s provider subscriptions',
        report.conflicts,
        report.provider_subscriptions,
    )
    return report
