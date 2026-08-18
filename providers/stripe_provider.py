"""The Stripe adapter.

**Unreachable in this phase.** `PROVIDER_BACKEND` defaults to `fake` and
`STRIPE_SECRET_KEY` is unset in every environment, so nothing constructs this
class. It is written now, against the pinned SDK, so that cutover is a
configuration change with a reviewed implementation behind it rather than a
rushed piece of code written under time pressure with real money moving.

Three decisions worth keeping.

**The API version is pinned in code, not inherited from the SDK.** A Stripe SDK
upgrade can change the default API version, and the API version determines the
shape of every webhook body this service parses. Inheriting it means an
unattended dependency bump silently changes how subscriptions are read.

**Only allowlisted fields are extracted.** `_to_subscription` names every field it
reads. A `dict(stripe_object)` would drag the customer's address, payment method
and tax ids into our process and, eventually, into a log line.

**`sequence` combines the object and the event.** Stripe does not guarantee
webhook ordering and gives a subscription no version counter, so the ordering
signal is assembled: the object's own period start, and — when the snapshot came
from a webhook — the event's creation time, whichever is larger. A late-arriving
older event then carries a smaller sequence than the state already applied and is
discarded instead of overwriting it.

This is the third version of that rule and the first one that works. The original
added `created` to a top-level `current_period_start` that Stripe **no longer
sends** — it moved onto the subscription item — so the sum collapsed to `created`,
which is constant for the life of a subscription. Every event compared equal,
equality is deliberately not a conflict, and the guard silently protected nothing.
Found in Phase 4B.3 by computing the sequence against the live account twice and
getting the same number.
"""

from __future__ import annotations

import logging
from dataclasses import replace
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
    ProviderWebhookEndpoint,
    SignatureError,
)

logger = logging.getLogger('haresign.billing')

# The subscription lifecycle events this service acts on. Everything else is
# acknowledged and ignored — a webhook endpoint that 500s on an event type it does
# not know is a webhook endpoint Stripe eventually disables.
HANDLED_EVENTS = frozenset(
    {
        'customer.subscription.created',
        'customer.subscription.updated',
        'customer.subscription.deleted',
        'customer.subscription.paused',
        'customer.subscription.resumed',
        'checkout.session.completed',
        'invoice.paid',
        'invoice.payment_failed',
    }
)


def _stripe():
    """The configured SDK module. Raises rather than returning an unusable one."""
    if not settings.STRIPE_SECRET_KEY:
        raise ProviderError(
            'The Stripe provider requires STRIPE_SECRET_KEY. No environment in this '
            'phase sets it — see AGENTS.md, "The Stripe boundary".'
        )
    import stripe

    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.api_version = settings.STRIPE_API_VERSION
    # Bound, so a provider outage becomes a failed request rather than a worker
    # blocked until the gunicorn timeout kills it.
    stripe.max_network_retries = 2
    return stripe


def _ts(value) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(int(value), tz=UTC)


class StripeProvider(Provider):
    name = 'stripe'

    def verify_webhook(self, payload: bytes, signature: str) -> ProviderEvent:
        if not settings.STRIPE_WEBHOOK_SECRET:
            # Refusing is the only safe reading. An endpoint that accepts
            # unverified events is an endpoint anyone can use to cancel a
            # customer's subscription or grant themselves one.
            raise SignatureError('No webhook signing secret is configured.')

        stripe = _stripe()
        try:
            event = stripe.Webhook.construct_event(
                payload, signature, settings.STRIPE_WEBHOOK_SECRET
            )
        except ValueError as exc:
            raise ProviderError('Event body is not JSON.') from exc
        except Exception as exc:
            # Includes SignatureVerificationError. The exception text can quote
            # the payload, so only the fact of failure is propagated.
            raise SignatureError('Signature verification failed.') from exc

        return self._to_event(event, payload)

    def _to_event(self, event, payload: bytes) -> ProviderEvent:
        import hashlib

        obj = event['data']['object']
        subscription = None
        if event['type'].startswith('customer.subscription.'):
            subscription = self._to_subscription(obj)
        elif event['type'] == 'checkout.session.completed' and obj.get('subscription'):
            # The session carries only the subscription id, so the full object is
            # read back. This is the one place a webhook triggers an outbound
            # call, and it is why the handler is transactional around the apply
            # rather than around the fetch.
            subscription = self.fetch_subscription(obj['subscription'])

        created = int(event.get('created') or 0)
        if subscription is not None and created > subscription.sequence:
            # The event half of the ordering signal. Stripe stamps `created` when
            # the change happened, so it advances with every delivery even when
            # nothing about the object's period moved — a cancellation scheduled
            # mid-period, a price change, a pause. Taking the larger of the two
            # means the sequence never goes backwards for a genuinely newer event.
            subscription = replace(subscription, sequence=created)

        return ProviderEvent(
            event_id=str(event['id']),
            event_type=str(event['type']),
            created_at=_ts(event['created']) or datetime.now(tz=UTC),
            subscription=subscription,
            payload_digest=hashlib.sha256(payload).hexdigest(),
        )

    def fetch_subscription(self, subscription_id: str) -> ProviderSubscription:
        stripe = _stripe()
        try:
            obj = stripe.Subscription.retrieve(subscription_id, expand=['items.data.price'])
        except Exception as exc:
            raise ProviderError('Unable to read the subscription from Stripe.') from exc
        return self._to_subscription(obj)

    def list_subscriptions(self, customer_id: str = '') -> list[ProviderSubscription]:
        """Every subscription, for reconciliation. Paginated explicitly.

        `auto_paging_iter` is used rather than a single `list` call: a
        reconciliation that silently stopped at the first hundred subscriptions
        would report the rest as missing and invite somebody to "fix" them.
        """
        stripe = _stripe()
        params = {'status': 'all', 'limit': 100, 'expand': ['data.items.data.price']}
        if customer_id:
            params['customer'] = customer_id
        try:
            return [
                self._to_subscription(obj)
                for obj in stripe.Subscription.list(**params).auto_paging_iter()
            ]
        except Exception as exc:
            raise ProviderError('Unable to list subscriptions from Stripe.') from exc

    def list_catalogue(self) -> list[ProviderCataloguePrice]:
        """Every price and its product. Read-only, and the only catalogue call.

        Prices are listed with their product expanded rather than products listed
        and prices fetched per product: one paginated pass, and a price that
        belongs to an archived product is still seen — which is precisely the
        case somebody needs to be told about before they map it to a live plan.
        """
        stripe = _stripe()
        try:
            return [
                self._to_catalogue_price(obj)
                for obj in stripe.Price.list(
                    limit=100, active=None, expand=['data.product']
                ).auto_paging_iter()
            ]
        except Exception as exc:
            raise ProviderError('Unable to list prices from Stripe.') from exc

    def _to_catalogue_price(self, obj) -> ProviderCataloguePrice:
        product = obj.get('product')
        if isinstance(product, str):
            product_id, product_name, product_active = product, '', True
        else:
            product = product or {}
            product_id = str(product.get('id') or '')
            # A product *name* is a catalogue label, not a customer detail. It is
            # read for the operator's benefit and never used to decide a mapping —
            # see `catalog.price_mapping`, which matches on ids only.
            product_name = str(product.get('name') or '')
            product_active = bool(product.get('active', True))

        recurring = obj.get('recurring') or {}
        return ProviderCataloguePrice(
            price_id=str(obj.get('id') or ''),
            product_id=product_id,
            product_name=product_name,
            currency=str(obj.get('currency') or '').upper(),
            unit_amount_minor=(
                int(obj['unit_amount']) if obj.get('unit_amount') is not None else None
            ),
            interval=str(recurring.get('interval') or ''),
            interval_count=int(recurring.get('interval_count') or 1),
            active=bool(obj.get('active', True)),
            product_active=product_active,
            livemode=bool(obj.get('livemode', False)),
        )

    def list_customers(self) -> list[ProviderCustomerRef]:
        """Every customer id, with our own stamped organisation metadata.

        Nothing else is read from the customer object. The SDK returns the whole
        record — name, email, address, invoice settings, default payment method —
        and every one of those fields is dropped here rather than downstream,
        because a field that never enters the process cannot leave it in a log.
        """
        stripe = _stripe()
        try:
            return [
                ProviderCustomerRef(
                    customer_id=str(obj.get('id') or ''),
                    livemode=bool(obj.get('livemode', False)),
                    organization_id=str(
                        (obj.get('metadata') or {}).get('haresign_organization_id') or ''
                    ),
                    deleted=bool(obj.get('deleted', False)),
                )
                for obj in stripe.Customer.list(limit=100).auto_paging_iter()
            ]
        except Exception as exc:
            raise ProviderError('Unable to list customers from Stripe.') from exc

    def list_webhook_endpoints(self) -> list[ProviderWebhookEndpoint]:
        """Every configured webhook endpoint. Read-only.

        Retrieved before a Billing endpoint is added, because "create a second
        endpoint" is only safe advice if somebody has looked at the first one —
        its API version decides the shape of every body it receives, and its
        event list decides what the existing system is relying on.

        `secret` is never read. Stripe returns it on creation and not on a list,
        and this extraction names its fields rather than copying the object.
        """
        stripe = _stripe()
        try:
            return [
                ProviderWebhookEndpoint(
                    url=str(obj.get('url') or ''),
                    status=str(obj.get('status') or ''),
                    api_version=str(obj.get('api_version') or ''),
                    enabled_events=[str(event) for event in (obj.get('enabled_events') or [])],
                    livemode=bool(obj.get('livemode', False)),
                )
                for obj in stripe.WebhookEndpoint.list(limit=100).auto_paging_iter()
            ]
        except Exception as exc:
            raise ProviderError('Unable to list webhook endpoints from Stripe.') from exc

    def _to_subscription(self, obj) -> ProviderSubscription:
        """Extract exactly the allowlisted fields. Nothing else crosses the boundary."""
        customer = obj.get('customer')
        customer_id = customer if isinstance(customer, str) else (customer or {}).get('id', '')

        prices = []
        for item in (obj.get('items') or {}).get('data') or []:
            price = item.get('price') or {}
            price_id = price if isinstance(price, str) else price.get('id', '')
            if price_id:
                prices.append(
                    ProviderPrice(
                        price_id=price_id,
                        quantity=int(item.get('quantity') or 1),
                        item_id=str(item.get('id') or ''),
                    )
                )

        metadata = obj.get('metadata') or {}

        # Stripe moved period boundaries onto the item in recent API versions and
        # kept them on the subscription in older ones. Both are read, the
        # subscription-level value winning, so the adapter survives the version
        # pin being moved forward.
        period_start = obj.get('current_period_start')
        period_end = obj.get('current_period_end')
        if period_end is None and prices:
            first = ((obj.get('items') or {}).get('data') or [{}])[0]
            period_start = first.get('current_period_start')
            period_end = first.get('current_period_end')

        return ProviderSubscription(
            subscription_id=str(obj.get('id') or ''),
            customer_id=str(customer_id or ''),
            status=str(obj.get('status') or ''),
            prices=prices,
            current_period_start=_ts(period_start),
            current_period_end=_ts(period_end),
            trial_end=_ts(obj.get('trial_end')),
            cancel_at_period_end=bool(obj.get('cancel_at_period_end')),
            canceled_at=_ts(obj.get('canceled_at')),
            ended_at=_ts(obj.get('ended_at')),
            # The object half of the ordering signal: the current period's start,
            # which advances every cycle, falling back to creation for an object
            # that has no period yet. `period_start` is already resolved above
            # from the item when the subscription level does not carry it — which
            # is every subscription, under every API version Stripe currently
            # serves.
            #
            # This alone does not move when a subscription changes mid-period, so
            # `_to_event` raises it to the event's creation time for anything that
            # arrived by webhook. Reconciliation, which has no event, deliberately
            # passes `sequence=0` and skips the guard entirely.
            sequence=int(period_start or obj.get('created') or 0),
            organization_id=str(metadata.get('haresign_organization_id') or ''),
        )

    def create_checkout_session(self, **kwargs) -> str:
        """Create a Stripe-hosted Checkout Session and return its URL.

        Written against the pinned SDK and **unreachable**: constructing this
        class at all requires `STRIPE_SECRET_KEY`, which no environment sets, and
        reaching this method additionally requires `BILLING_CHECKOUT_ENABLED`,
        which is off everywhere including production. Creating a live session is
        behind the human-confirmed cutover gate in docs/stripe-cutover.md.

        Two properties are worth naming because they are the reason this is a few
        lines rather than a form.

        **Stripe-hosted only.** `mode='subscription'` with a `line_items` price
        reference; no payment method is collected here, no card detail crosses
        this application, and there is no embedded or custom flow to get wrong.

        **The idempotency key is the caller's.** Passed through rather than
        generated here, so a retry of the same intent — a double-clicked button, a
        repeated POST — reaches Stripe with the same key and yields the same
        session instead of a second one.
        """
        stripe = _stripe()

        session = stripe.checkout.Session.create(
            mode='subscription',
            line_items=[
                {'price': kwargs['provider_price_id'], 'quantity': kwargs.get('quantity', 1)}
            ],
            success_url=kwargs['success_url'],
            cancel_url=kwargs['cancel_url'],
            # An existing customer is reused; a first purchase lets Stripe create
            # one. Never a customer id that arrived from a browser.
            **({'customer': kwargs['customer_id']} if kwargs.get('customer_id') else {}),
            # Stamped so the webhook can attribute the resulting subscription
            # without guessing. Read back in `_to_subscription`. Organisation
            # UUIDs only — no name, no person, no amount.
            subscription_data={
                'metadata': {
                    'haresign_organization_id': str(kwargs.get('organization_id') or ''),
                    'haresign_beneficiary_organization_id': str(
                        kwargs.get('beneficiary_organization_id') or ''
                    ),
                }
            },
            idempotency_key=kwargs['idempotency_key'],
        )
        url = getattr(session, 'url', '')
        if not url:
            raise ProviderError('Stripe returned a checkout session with no URL.')
        return url

    def create_portal_session(self, **kwargs) -> str:
        """Create a Stripe-hosted Billing Portal session and return its URL.

        Unreachable for the same two reasons as checkout above. The customer id is
        always read from our own billing account by the caller and never accepted
        from a request: a portal session is a session for whoever the customer id
        names, so accepting one from a browser would be handing somebody else's
        invoices and payment methods to whoever asked.
        """
        stripe = _stripe()

        session = stripe.billing_portal.Session.create(
            customer=kwargs['customer_id'],
            return_url=kwargs['return_url'],
        )
        url = getattr(session, 'url', '')
        if not url:
            raise ProviderError('Stripe returned a portal session with no URL.')
        return url
