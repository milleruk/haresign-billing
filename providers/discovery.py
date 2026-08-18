"""Read-only discovery against the payment provider.

The single place this repository is permitted to *read* Stripe before cutover —
AGENTS.md, "Phase 4B exception", item 4. It retrieves the catalogue (products,
prices, currencies, recurrence) and aggregate subscription and customer
references, and it does nothing else. There is no code path here that creates,
updates, archives or deletes anything.

Three properties make it safe to run against a live account.

**The mode is asserted, never assumed.** The caller states which Stripe mode it
expects. The credential's own prefix and the `livemode` flag on every object
returned must both agree with it. A key that says live against an expectation of
test — or objects that disagree with their own key — is a refusal, not a warning,
because every subsequent decision (which price ids to write into the catalogue,
which customers to reconcile) is wrong in a way that is invisible afterwards.

**No personal detail is retrieved.** Customers are reduced at the adapter to an
id, a mode flag and our own stamped organisation metadata. Names, emails,
addresses, payment methods and balances are never read into this process.

**The report is aggregate.** Counts and distributions. Price and product ids
appear only when explicitly asked for, because mapping a plan to a price requires
knowing the price id and a product catalogue is not personal data — but customer
and subscription ids never appear at any verbosity.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from django.conf import settings

from audit import events as audit_events
from audit.services import record

from .base import (
    ProviderCataloguePrice,
    ProviderError,
    ProviderWebhookEndpoint,
    get_provider,
)

logger = logging.getLogger('haresign.billing')

LIVE = 'live'
TEST = 'test'
UNKNOWN = 'unknown'


class DiscoveryRefused(ProviderError):
    """Discovery stopped before reading anything, or before reporting what it read."""


def credential_mode(key: str = '') -> str:
    """Which Stripe mode a credential is for, from its prefix alone.

    Covers both secret (`sk_`) and restricted (`rk_`) keys, and a restricted key
    is the one that should be used here: discovery needs read scopes only, and a
    credential that *cannot* mutate is a stronger guarantee than a credential that
    merely is not asked to.
    """
    key = (key or settings.STRIPE_SECRET_KEY or '').strip()
    if not key:
        return UNKNOWN
    if key.startswith(('sk_live_', 'rk_live_')):
        return LIVE
    if key.startswith(('sk_test_', 'rk_test_')):
        return TEST
    return UNKNOWN


@dataclass
class DiscoveryReport:
    """What discovery found. Aggregate, and safe to paste into a ticket."""

    provider: str
    expected_mode: str
    credential_mode: str
    # What the objects themselves said. 'mixed' when they disagreed with each
    # other, which is a refusal rather than a report.
    observed_mode: str = UNKNOWN

    products: int = 0
    prices_total: int = 0
    prices_active: int = 0
    prices_recurring: int = 0
    prices_on_archived_products: int = 0
    currencies: dict[str, int] = field(default_factory=dict)
    intervals: dict[str, int] = field(default_factory=dict)

    customers_total: int = 0
    customers_with_organization_metadata: int = 0
    customers_deleted: int = 0

    subscriptions_total: int = 0
    subscriptions_by_status: dict[str, int] = field(default_factory=dict)
    subscriptions_with_organization_metadata: int = 0
    subscriptions_with_unknown_price: int = 0

    webhook_endpoints_total: int = 0
    webhook_endpoints_enabled: int = 0

    # Catalogue rows, for the operator who has to map plans to prices. Populated
    # only when the caller asks; never customer or subscription identifiers.
    catalogue: list[ProviderCataloguePrice] = field(default_factory=list)
    # Always populated. A webhook endpoint is infrastructure — a destination and
    # an event list — and deciding whether to add a second one requires seeing
    # the first. No signing secret is read, at any verbosity.
    webhook_endpoints: list[ProviderWebhookEndpoint] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, object]:
        """The report as a plain dictionary, with no provider identifier in it."""
        return {
            'provider': self.provider,
            'mode': self.observed_mode,
            'products': self.products,
            'prices_total': self.prices_total,
            'prices_active': self.prices_active,
            'prices_recurring': self.prices_recurring,
            'prices_on_archived_products': self.prices_on_archived_products,
            'currencies': dict(sorted(self.currencies.items())),
            'intervals': dict(sorted(self.intervals.items())),
            'customers_total': self.customers_total,
            'customers_with_organization_metadata': self.customers_with_organization_metadata,
            'customers_deleted': self.customers_deleted,
            'subscriptions_total': self.subscriptions_total,
            'subscriptions_by_status': dict(sorted(self.subscriptions_by_status.items())),
            'subscriptions_with_organization_metadata': (
                self.subscriptions_with_organization_metadata
            ),
            'subscriptions_with_unknown_price': self.subscriptions_with_unknown_price,
            'webhook_endpoints_total': self.webhook_endpoints_total,
            'webhook_endpoints_enabled': self.webhook_endpoints_enabled,
        }


def discovery_provider():
    """The provider discovery reads through.

    A Stripe credential being present is what makes the Stripe adapter reachable,
    **not** `PROVIDER_BACKEND`. That separation is deliberate: pre-cutover
    discovery wants a restricted read key configured while the runtime backend
    stays on the fake, so that reading the live catalogue does not simultaneously
    put every webhook and checkout path onto Stripe.
    """
    if settings.STRIPE_SECRET_KEY:
        from .stripe_provider import StripeProvider

        return StripeProvider()
    return get_provider()


def discover(*, expect_mode: str, include_catalogue: bool = False, request=None) -> DiscoveryReport:
    """Read the provider's catalogue, customers and subscriptions. Never writes."""
    if expect_mode not in (LIVE, TEST):
        raise DiscoveryRefused("Discovery needs an explicit expected mode: 'live' or 'test'.")

    provider = discovery_provider()
    key_mode = credential_mode()

    # The fake has no credential and no mode, so it can never satisfy an
    # expectation of live or test. This is what stops a discovery report that
    # claims to describe a Stripe account from actually describing an in-process
    # store with nothing in it.
    if provider.name != 'stripe':
        raise DiscoveryRefused(
            f'Discovery expected Stripe in {expect_mode} mode but the configured provider '
            f'is {provider.name!r}. Set STRIPE_SECRET_KEY to a restricted read key.'
        )
    if key_mode == UNKNOWN:
        raise DiscoveryRefused('The Stripe credential does not name a mode. Refusing to guess.')
    if key_mode != expect_mode:
        raise DiscoveryRefused(
            f'The Stripe credential is a {key_mode} key and {expect_mode} was expected.'
        )

    report = DiscoveryReport(
        provider=provider.name, expected_mode=expect_mode, credential_mode=key_mode
    )

    catalogue = provider.list_catalogue()
    customers = provider.list_customers()
    subscriptions = provider.list_subscriptions()
    endpoints = provider.list_webhook_endpoints()

    observed = {price.livemode for price in catalogue} | {
        customer.livemode for customer in customers
    }
    if len(observed) > 1:
        # One account cannot return both. Two accounts can, and being told which
        # objects came from where afterwards is not possible.
        raise DiscoveryRefused('The provider returned both live and test objects. Refusing.')
    report.observed_mode = (LIVE if next(iter(observed)) else TEST) if observed else key_mode
    if observed and report.observed_mode != expect_mode:
        raise DiscoveryRefused(
            f'The provider returned {report.observed_mode} objects and {expect_mode} was expected.'
        )

    report.products = len({price.product_id for price in catalogue if price.product_id})
    report.prices_total = len(catalogue)
    report.prices_active = sum(1 for price in catalogue if price.active)
    report.prices_recurring = sum(1 for price in catalogue if price.is_recurring)
    report.prices_on_archived_products = sum(1 for price in catalogue if not price.product_active)
    report.currencies = dict(Counter(price.currency for price in catalogue if price.currency))
    report.intervals = dict(Counter(price.interval or 'one_off' for price in catalogue))

    report.customers_total = len(customers)
    report.customers_with_organization_metadata = sum(
        1 for customer in customers if customer.organization_id
    )
    report.customers_deleted = sum(1 for customer in customers if customer.deleted)

    known_prices = {price.price_id for price in catalogue}
    report.subscriptions_total = len(subscriptions)
    report.subscriptions_by_status = dict(
        Counter(subscription.status or 'unknown' for subscription in subscriptions)
    )
    report.subscriptions_with_organization_metadata = sum(
        1 for subscription in subscriptions if subscription.organization_id
    )
    report.subscriptions_with_unknown_price = sum(
        1
        for subscription in subscriptions
        if any(price.price_id not in known_prices for price in subscription.prices)
    )

    report.webhook_endpoints = sorted(endpoints, key=lambda endpoint: endpoint.url)
    report.webhook_endpoints_total = len(endpoints)
    report.webhook_endpoints_enabled = sum(
        1 for endpoint in endpoints if endpoint.status == 'enabled'
    )

    if include_catalogue:
        report.catalogue = sorted(catalogue, key=lambda price: (price.product_name, price.price_id))

    record(
        audit_events.PROVIDER_DISCOVERY_RUN,
        request=request,
        metadata=report.counts,
    )
    logger.info('provider discovery: %s mode, %s prices', report.observed_mode, report.prices_total)
    return report
