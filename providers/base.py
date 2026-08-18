"""The provider boundary.

Everything above this line speaks in normalized `ProviderSubscription` and
`ProviderEvent` objects. Everything below it speaks Stripe. The point of the seam
is not future provider-switching — it is that the entire test suite, the isolated
rehearsal and every migration exercise run against a deterministic in-process
implementation, so a billing state machine can be tested exhaustively without a
single network call to anybody's payment API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from django.conf import settings


class ProviderError(RuntimeError):
    """Any provider failure. Never carries a secret or a full payload."""


class SignatureError(ProviderError):
    """A webhook whose signature did not verify. Always a refusal, never a retry."""


@dataclass(frozen=True)
class ProviderPrice:
    """One priced line as the provider reports it."""

    price_id: str
    quantity: int = 1
    item_id: str = ''


@dataclass(frozen=True)
class ProviderSubscription:
    """A complete provider subscription, normalized.

    A **snapshot**. Every field the billing state machine reads is present, so a
    caller cannot apply half of one and leave the other half stale.
    """

    subscription_id: str
    customer_id: str
    status: str
    prices: list[ProviderPrice] = field(default_factory=list)
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    trial_end: datetime | None = None
    cancel_at_period_end: bool = False
    canceled_at: datetime | None = None
    ended_at: datetime | None = None
    # Opaque, provider-stamped ordering signal. Stripe does not guarantee webhook
    # ordering, so this is what lets a late-arriving older event be discarded.
    sequence: int = 0
    # Only the keys this service stamped itself at checkout. Never the provider's
    # whole metadata bag.
    organization_id: str = ''


@dataclass(frozen=True)
class ProviderCataloguePrice:
    """One price at the provider, with the product it belongs to.

    Read-only discovery output. It carries what is needed to decide *which*
    catalogue row a provider price corresponds to — currency, amount, recurrence
    — and nothing that identifies a person. A provider price is a product fact,
    not a customer fact, so this is the one discovery shape that may name ids.

    `livemode` is read from the object rather than inferred from the key, so a
    report can state which Stripe mode it actually read instead of which mode
    somebody believed they were pointing at.
    """

    price_id: str
    product_id: str
    product_name: str
    currency: str
    unit_amount_minor: int | None
    # 'month' | 'year' | '' when the price is not recurring.
    interval: str = ''
    interval_count: int = 1
    active: bool = True
    product_active: bool = True
    livemode: bool = False

    @property
    def is_recurring(self) -> bool:
        return bool(self.interval)


@dataclass(frozen=True)
class ProviderCustomerRef:
    """A provider customer, reduced to what reconciliation may know about one.

    Deliberately **not** a customer record. No name, no email, no address, no
    payment method, no balance. Reconciliation asks "does this customer map to
    exactly one billing account", and answering that needs an id, our own stamped
    organisation metadata and whether the provider considers the row deleted.
    """

    customer_id: str
    livemode: bool = False
    # Only the key this service stamps itself. Never the provider's metadata bag.
    organization_id: str = ''
    deleted: bool = False


@dataclass(frozen=True)
class ProviderWebhookEndpoint:
    """A webhook endpoint configured at the provider.

    Infrastructure, not customer data: where the provider is currently sending
    events, whether that destination is enabled, which API version it sends and
    which event types it is subscribed to. Retrieved so that adding a second
    endpoint for Billing is a decision made with the first one in view.

    **The signing secret is never read.** Stripe returns it only at creation and
    never on a list, and no field here would hold it if it did.
    """

    url: str
    status: str = ''
    api_version: str = ''
    enabled_events: list[str] = field(default_factory=list)
    livemode: bool = False

    @property
    def host(self) -> str:
        from urllib.parse import urlsplit

        return urlsplit(self.url).netloc

    @property
    def path(self) -> str:
        from urllib.parse import urlsplit

        return urlsplit(self.url).path


@dataclass(frozen=True)
class ProviderEvent:
    """A verified webhook event, reduced to what this service acts on."""

    event_id: str
    event_type: str
    created_at: datetime
    subscription: ProviderSubscription | None = None
    # A digest of the raw body, so a re-delivery can be proven identical without
    # the body itself ever being stored.
    payload_digest: str = ''


class Provider:
    """The interface. Both implementations satisfy exactly this."""

    name = 'base'

    def verify_webhook(self, payload: bytes, signature: str) -> ProviderEvent:
        raise NotImplementedError

    def fetch_subscription(self, subscription_id: str) -> ProviderSubscription:
        raise NotImplementedError

    def list_subscriptions(self, customer_id: str = '') -> list[ProviderSubscription]:
        raise NotImplementedError

    def list_catalogue(self) -> list[ProviderCataloguePrice]:
        """Every price the provider holds, with its product. Read-only."""
        raise NotImplementedError

    def list_customers(self) -> list[ProviderCustomerRef]:
        """Every customer reference. Read-only, and never a personal detail."""
        raise NotImplementedError

    def list_webhook_endpoints(self) -> list[ProviderWebhookEndpoint]:
        """Where the provider currently sends events. Read-only, never a secret."""
        raise NotImplementedError

    def create_checkout_session(self, **kwargs) -> str:
        raise NotImplementedError

    def create_portal_session(self, **kwargs) -> str:
        raise NotImplementedError


def get_provider() -> Provider:
    """The configured provider.

    `fake` is the default and is what every environment in this phase runs. The
    Stripe adapter is reachable only by naming it *and* supplying a secret key,
    which settings validation requires — so there is no configuration that
    accidentally reaches Stripe.
    """
    backend = settings.PROVIDER_BACKEND

    if backend == 'fake':
        from .fake import FakeProvider

        return FakeProvider()

    if backend == 'stripe':
        from .stripe_provider import StripeProvider

        return StripeProvider()

    raise ProviderError(f'Unknown PROVIDER_BACKEND {backend!r}.')
