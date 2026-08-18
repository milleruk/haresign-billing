"""A deterministic provider for tests and the isolated rehearsal.

Not a mock. It has a real store, real signature verification over a real HMAC,
real event ids and a real sequence counter — so the code paths exercised against
it are the same ones Stripe drives, including replay, out-of-order delivery and
signature refusal. The only thing it does not do is open a socket.

Signatures use the same shape as Stripe's `t=…,v1=…` scheme with an HMAC-SHA256
over `timestamp.payload`, for one reason: a fake whose signature check is
`return True` proves nothing about the webhook endpoint, and the endpoint is the
one piece of this service the public internet can reach.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

from django.conf import settings

from .base import (
    Provider,
    ProviderCataloguePrice,
    ProviderCustomerRef,
    ProviderError,
    ProviderEvent,
    ProviderPrice,
    ProviderSubscription,
    SignatureError,
)

# Fixed and unmistakable. Never a value that could be confused for a real key.
FAKE_WEBHOOK_SECRET = 'whsec_fake_provider_not_a_real_secret'  # noqa: S105

# Tolerated age of a signed payload, matching Stripe's own default. An unbounded
# window would make a captured webhook replayable forever.
SIGNATURE_TOLERANCE_SECONDS = 300


def _ts(value) -> datetime | None:
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromtimestamp(int(value), tz=UTC)


def sign(payload: bytes, secret: str = FAKE_WEBHOOK_SECRET, timestamp: int | None = None) -> str:
    """Produce a signature header for `payload`. Used by tests and the rehearsal."""
    timestamp = timestamp or int(time.time())
    signed = f'{timestamp}.'.encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f't={timestamp},v1={digest}'


class FakeProvider(Provider):
    """In-process provider with a class-level store, reset between tests."""

    name = 'fake'

    # Class-level so a test can seed state before the view under test constructs
    # its own instance through `get_provider()`.
    subscriptions: dict[str, dict] = {}
    checkout_urls: dict[str, str] = {}
    # Seeded catalogue and customer stores, so discovery and pre-cutover
    # reconciliation are exercised end to end without a network.
    catalogue: dict[str, dict] = {}
    customers: dict[str, dict] = {}

    # --- Test/rehearsal seeding ------------------------------------------------

    @classmethod
    def reset(cls) -> None:
        cls.subscriptions = {}
        cls.checkout_urls = {}
        cls.checkout_calls = []
        cls.portal_calls = []
        cls.catalogue = {}
        cls.customers = {}

    @classmethod
    def seed_price(cls, price_id: str, **fields) -> dict:
        record = {
            'price_id': price_id,
            'product_id': fields.get('product_id', 'prod_fake'),
            'product_name': fields.get('product_name', 'Fake product'),
            'currency': fields.get('currency', 'GBP'),
            'unit_amount_minor': fields.get('unit_amount_minor', 1000),
            'interval': fields.get('interval', 'month'),
            'interval_count': fields.get('interval_count', 1),
            'active': fields.get('active', True),
            'product_active': fields.get('product_active', True),
            'livemode': fields.get('livemode', False),
        }
        cls.catalogue[price_id] = record
        return record

    @classmethod
    def seed_customer(cls, customer_id: str, **fields) -> dict:
        record = {
            'customer_id': customer_id,
            'livemode': fields.get('livemode', False),
            'organization_id': fields.get('organization_id', ''),
            'deleted': fields.get('deleted', False),
        }
        cls.customers[customer_id] = record
        return record

    @classmethod
    def seed_subscription(cls, subscription_id: str, **fields) -> dict:
        record = {
            'subscription_id': subscription_id,
            'customer_id': fields.get('customer_id', ''),
            'status': fields.get('status', 'active'),
            'prices': fields.get('prices', []),
            'current_period_start': fields.get('current_period_start'),
            'current_period_end': fields.get('current_period_end'),
            'trial_end': fields.get('trial_end'),
            'cancel_at_period_end': fields.get('cancel_at_period_end', False),
            'canceled_at': fields.get('canceled_at'),
            'ended_at': fields.get('ended_at'),
            'sequence': fields.get('sequence', 1),
            'organization_id': fields.get('organization_id', ''),
        }
        cls.subscriptions[subscription_id] = record
        return record

    @classmethod
    def build_event(
        cls,
        event_type: str,
        subscription_id: str,
        *,
        event_id: str = '',
        created: int | None = None,
        **overrides,
    ) -> bytes:
        """Serialise an event body exactly as the webhook endpoint will parse it."""
        record = {**cls.subscriptions.get(subscription_id, {}), **overrides}
        record.setdefault('subscription_id', subscription_id)
        body = {
            'id': event_id or f'evt_fake_{subscription_id}_{record.get("sequence", 1)}',
            'type': event_type,
            'created': created or int(time.time()),
            'data': {'object': _serialisable(record)},
        }
        return json.dumps(body, sort_keys=True, separators=(',', ':')).encode()

    # --- Provider interface ----------------------------------------------------

    def verify_webhook(self, payload: bytes, signature: str) -> ProviderEvent:
        # The signing secret below is a published constant. Verification is
        # therefore only meaningful where the endpoint is unreachable from
        # outside — the test process and the isolated rehearsal — and is refused
        # everywhere else. See settings.FAKE_PROVIDER_WEBHOOKS_ENABLED.
        if not settings.FAKE_PROVIDER_WEBHOOKS_ENABLED:
            raise SignatureError('The fake provider does not verify webhooks in this environment.')

        secret = FAKE_WEBHOOK_SECRET

        parts = dict(piece.split('=', 1) for piece in signature.split(',') if '=' in piece)
        timestamp = parts.get('t', '')
        supplied = parts.get('v1', '')
        if not timestamp or not supplied:
            raise SignatureError('Malformed signature header.')

        try:
            age = abs(time.time() - int(timestamp))
        except ValueError as exc:
            raise SignatureError('Signature timestamp is not an integer.') from exc
        if age > SIGNATURE_TOLERANCE_SECONDS:
            raise SignatureError('Signature timestamp is outside the tolerance window.')

        expected = hmac.new(
            secret.encode(), f'{timestamp}.'.encode() + payload, hashlib.sha256
        ).hexdigest()
        # Constant time. A timing-variable comparison on a signature is a slow but
        # real forgery oracle.
        if not hmac.compare_digest(expected, supplied):
            raise SignatureError('Signature does not match.')

        try:
            body = json.loads(payload)
        except Exception as exc:
            raise ProviderError('Event body is not JSON.') from exc

        return _to_event(body, payload)

    def fetch_subscription(self, subscription_id: str) -> ProviderSubscription:
        record = self.subscriptions.get(subscription_id)
        if record is None:
            raise ProviderError('No such subscription at the provider.')
        return _to_subscription(record)

    def list_subscriptions(self, customer_id: str = '') -> list[ProviderSubscription]:
        return [
            _to_subscription(record)
            for record in self.subscriptions.values()
            if not customer_id or record.get('customer_id') == customer_id
        ]

    def list_catalogue(self) -> list[ProviderCataloguePrice]:
        return [ProviderCataloguePrice(**record) for record in self.catalogue.values()]

    def list_customers(self) -> list[ProviderCustomerRef]:
        return [ProviderCustomerRef(**record) for record in self.customers.values()]

    # Every checkout and portal call is recorded, so a test can assert on what the
    # caller actually asked for rather than only on the URL it got back. That is
    # what makes "the price came from the catalogue and not from the browser" a
    # testable claim instead of a comment.
    checkout_calls: list[dict] = []
    portal_calls: list[dict] = []

    def create_checkout_session(self, **kwargs) -> str:
        # The arguments a real hosted checkout cannot do without. Asserted here
        # rather than trusted, so a caller that stopped passing one fails in the
        # test suite instead of at the provider with real money involved.
        for required in ('provider_price_id', 'success_url', 'cancel_url', 'idempotency_key'):
            if not kwargs.get(required):
                raise ProviderError(f'create_checkout_session needs {required}.')

        type(self).checkout_calls.append(dict(kwargs))
        key = kwargs['idempotency_key']
        # Idempotency, modelled honestly: the same key returns the same session
        # rather than making a second one. This is the behaviour the real provider
        # gives us, and a fake that minted a fresh session each time would let a
        # double-submit bug pass every test.
        return self.checkout_urls.setdefault(
            f'checkout:{key}', f'https://checkout.invalid/session/{key}'
        )

    def create_portal_session(self, **kwargs) -> str:
        if not kwargs.get('customer_id'):
            raise ProviderError('create_portal_session needs customer_id.')
        if not kwargs.get('return_url'):
            raise ProviderError('create_portal_session needs return_url.')

        type(self).portal_calls.append(dict(kwargs))
        return self.checkout_urls.get('portal', 'https://portal.invalid/fake-session')


def _serialisable(record: dict) -> dict:
    out = {}
    for key, value in record.items():
        out[key] = int(value.timestamp()) if isinstance(value, datetime) else value
    return out


def _to_subscription(record: dict) -> ProviderSubscription:
    return ProviderSubscription(
        subscription_id=record['subscription_id'],
        customer_id=record.get('customer_id', ''),
        status=record.get('status', ''),
        prices=[
            ProviderPrice(
                price_id=price['price_id'],
                quantity=price.get('quantity', 1),
                item_id=price.get('item_id', ''),
            )
            for price in record.get('prices', [])
        ],
        current_period_start=_ts(record.get('current_period_start')),
        current_period_end=_ts(record.get('current_period_end')),
        trial_end=_ts(record.get('trial_end')),
        cancel_at_period_end=bool(record.get('cancel_at_period_end')),
        canceled_at=_ts(record.get('canceled_at')),
        ended_at=_ts(record.get('ended_at')),
        sequence=int(record.get('sequence') or 0),
        organization_id=record.get('organization_id', ''),
    )


def _to_event(body: dict, payload: bytes) -> ProviderEvent:
    obj = body.get('data', {}).get('object') or {}
    subscription = _to_subscription(obj) if obj.get('subscription_id') else None
    return ProviderEvent(
        event_id=str(body.get('id') or ''),
        event_type=str(body.get('type') or ''),
        created_at=_ts(body.get('created')) or datetime.now(tz=UTC),
        subscription=subscription,
        payload_digest=hashlib.sha256(payload).hexdigest(),
    )
