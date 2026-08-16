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
