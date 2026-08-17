"""Receiving provider events.

The order of operations is the security design, and it does not change:

1. **Verify the signature.** Before parsing, before a database write, before an
   audit row. An unverified body is attacker-controlled input and nothing is
   allowed to depend on it.
2. **Claim the event id.** A unique-constrained insert, so two concurrent
   deliveries of the same event race for one row and exactly one wins. A
   check-then-act on "have I seen this?" is a race, and the losing branch applies
   the event twice.
3. **Apply transactionally.** The billing write and the ledger update commit
   together, so there is no window in which an event is marked processed but its
   effect was rolled back.

The HTTP contract matters as much. A signature failure is **400 and never
retried** — retrying a forgery is pointless. An event we understand but cannot
resolve is **200**, because the provider retrying will not make an unknown price
known and an endpoint that 500s gets disabled. Only a genuine internal failure is
**500**, which is the one case where a retry can succeed.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from audit import events as audit_events
from audit.services import record
from billing.models import BillingAccount
from billing.services import BillingConflict, apply_subscription_snapshot
from catalog.models import PlanPrice
from web.throttling import Throttled, throttle

from .base import ProviderError, SignatureError, get_provider
from .mapping import subscription_state
from .models import WebhookEvent

logger = logging.getLogger('haresign.billing')

HANDLED_PREFIXES = ('customer.subscription.', 'checkout.session.')


def _declared_length(request) -> int:
    """The request's own claim about its size. Absent or malformed reads as zero."""
    try:
        return int(request.META.get('CONTENT_LENGTH') or 0)
    except (TypeError, ValueError):
        return 0


def _too_large(request) -> JsonResponse:
    """413, recorded. Never retried by a well-behaved provider, which is right —
    a payload that is too big will still be too big on the second attempt."""
    record(
        audit_events.WEBHOOK_REJECTED,
        request=request,
        metadata={'reason': 'payload_too_large'},
    )
    logger.warning('webhook: refused an oversized payload')
    return JsonResponse({'error': 'payload_too_large'}, status=413)


@csrf_exempt
@require_POST
def webhook(request):
    """The provider's delivery endpoint.

    CSRF-exempt because the provider cannot carry a Django CSRF token — the
    signature is the authentication, and it is checked first and unconditionally.
    """
    try:
        throttle(request, 'webhook')
    except Throttled:
        # 429 rather than a refusal: the provider will retry, which is correct
        # behaviour for a rate-limited delivery.
        return HttpResponse(status=429)

    # Size before anything else, and before the body is materialised.
    #
    # Verifying a signature means reading the whole payload into memory, so an
    # endpoint that does it without a ceiling is one anybody can use to make a
    # worker allocate as much as they care to send. The limit is generous against
    # a real provider event and small against that.
    #
    # Enforced here rather than only at the proxy. A proxy limit protects this
    # endpoint for traffic arriving through the proxy; this one protects it
    # always, including from anything already inside the network.
    if _declared_length(request) > settings.WEBHOOK_MAX_BODY_BYTES:
        return _too_large(request)

    provider = get_provider()
    signature = request.headers.get('Stripe-Signature') or request.headers.get(
        'X-Provider-Signature', ''
    )

    # And again on the real body, because a request may understate its length.
    if len(request.body) > settings.WEBHOOK_MAX_BODY_BYTES:
        return _too_large(request)

    try:
        event = provider.verify_webhook(request.body, signature)
    except SignatureError:
        record(
            audit_events.WEBHOOK_REJECTED,
            request=request,
            metadata={'reason': 'signature_verification_failed'},
        )
        logger.warning('webhook: signature verification failed')
        # Never 500. A forgery must not be retried, and a 400 tells an honest
        # provider that its signing secret has drifted.
        return JsonResponse({'error': 'invalid_signature'}, status=400)
    except ProviderError:
        record(
            audit_events.WEBHOOK_REJECTED, request=request, metadata={'reason': 'malformed_event'}
        )
        return JsonResponse({'error': 'invalid_payload'}, status=400)

    if not event.event_id:
        return JsonResponse({'error': 'invalid_payload'}, status=400)

    # Claim the id. The unique constraint is the idempotency mechanism; the
    # IntegrityError branch is the replay path, not an error path.
    try:
        with transaction.atomic():
            ledger = WebhookEvent.objects.create(
                provider=provider.name,
                provider_event_id=event.event_id,
                event_type=event.event_type,
                provider_created_at=event.created_at,
                outcome=WebhookEvent.Outcome.IGNORED,
                payload_digest=event.payload_digest,
            )
    except IntegrityError:
        return _replay(request, provider.name, event)

    record(
        audit_events.WEBHOOK_RECEIVED,
        request=request,
        provider_event_id=event.event_id,
        metadata={'event_type': event.event_type},
    )

    if not event.event_type.startswith(HANDLED_PREFIXES):
        _finish(ledger, WebhookEvent.Outcome.IGNORED, 'event type not acted on')
        return JsonResponse({'status': 'ignored'})

    try:
        return _apply(request, ledger, event)
    except Exception:
        logger.exception('webhook: failed to process %s', event.event_type)
        _finish(ledger, WebhookEvent.Outcome.FAILED, 'internal error')
        record(
            audit_events.WEBHOOK_FAILED,
            request=request,
            provider_event_id=event.event_id,
            metadata={'event_type': event.event_type},
        )
        # 500 so the provider retries. The ledger row already exists, so the
        # retry takes the replay path and is reconciled there — see _replay.
        return JsonResponse({'error': 'processing_failed'}, status=500)


def _replay(request, provider_name: str, event) -> JsonResponse:
    """A second delivery of an event id we already hold."""
    ledger = WebhookEvent.objects.filter(
        provider=provider_name, provider_event_id=event.event_id
    ).first()
    if ledger is None:
        # Lost a race and then lost the row: vanishingly unlikely, and a retry is
        # the honest answer.
        return JsonResponse({'error': 'processing_failed'}, status=500)

    ledger.delivery_count += 1
    ledger.save(update_fields=['delivery_count'])

    record(
        audit_events.WEBHOOK_REPLAYED,
        request=request,
        provider_event_id=event.event_id,
        organization_id=ledger.organization_id,
        metadata={
            'event_type': event.event_type,
            'delivery_count': ledger.delivery_count,
            'first_outcome': ledger.outcome,
            # A differing digest for the same event id means the provider changed
            # the body under a stable id, which is worth knowing about loudly.
            'digest_matches': ledger.payload_digest == event.payload_digest,
        },
    )

    if ledger.outcome == WebhookEvent.Outcome.FAILED:
        # The first attempt failed internally, so this retry is the provider doing
        # exactly the right thing. Re-run it.
        try:
            return _apply(request, ledger, event)
        except Exception:
            logger.exception('webhook: retry of %s failed again', event.event_id)
            return JsonResponse({'error': 'processing_failed'}, status=500)

    return JsonResponse({'status': 'duplicate'})


@transaction.atomic
def _apply(request, ledger: WebhookEvent, event) -> JsonResponse:
    """Apply one subscription snapshot. Billing write and ledger update commit together."""
    snapshot = event.subscription
    if snapshot is None or not snapshot.subscription_id:
        _finish(ledger, WebhookEvent.Outcome.IGNORED, 'no subscription in event')
        return JsonResponse({'status': 'ignored'})

    account = _resolve_account(snapshot)
    if account is None:
        # Not an error and not retryable: the provider has a subscription we
        # cannot attribute to an organisation. Recorded so reconciliation finds
        # it; answered 200 so the provider stops retrying something that will
        # never resolve on its own.
        _finish(ledger, WebhookEvent.Outcome.UNRESOLVED, 'no billing account for subscription')
        return JsonResponse({'status': 'unresolved'})

    prices = _resolve_prices(snapshot)
    plan = _resolve_plan(prices)
    if plan is None:
        _finish(ledger, WebhookEvent.Outcome.UNRESOLVED, 'no catalogue plan for price')
        ledger.organization_id = account.organization_id
        ledger.save(update_fields=['organization_id'])
        return JsonResponse({'status': 'unresolved'})

    try:
        subscription, changed = apply_subscription_snapshot(
            account=account,
            provider=ledger.provider,
            provider_subscription_id=snapshot.subscription_id,
            state=subscription_state(snapshot.status),
            plan=plan,
            prices=prices,
            provider_customer_id=snapshot.customer_id,
            current_period_start=snapshot.current_period_start,
            current_period_end=snapshot.current_period_end,
            trial_end=snapshot.trial_end,
            cancel_at_period_end=snapshot.cancel_at_period_end,
            canceled_at=snapshot.canceled_at,
            ended_at=snapshot.ended_at,
            sequence=snapshot.sequence,
            provider_event_id=event.event_id,
            request=request,
        )
    except BillingConflict as exc:
        # The commonest cause is out-of-order delivery, which is normal and not a
        # failure. It is recorded and the event is acknowledged: asking the
        # provider to redeliver an event that is genuinely stale achieves nothing.
        _finish(ledger, WebhookEvent.Outcome.OUT_OF_ORDER, str(exc)[:255])
        ledger.organization_id = account.organization_id
        ledger.save(update_fields=['organization_id'])
        record(
            audit_events.WEBHOOK_OUT_OF_ORDER,
            request=request,
            provider_event_id=event.event_id,
            organization_id=account.organization_id,
            metadata={'event_type': event.event_type, 'reason': str(exc)[:255]},
        )
        return JsonResponse({'status': 'out_of_order'})

    ledger.organization_id = account.organization_id
    ledger.subscription = subscription
    _finish(
        ledger,
        WebhookEvent.Outcome.APPLIED if changed else WebhookEvent.Outcome.DUPLICATE,
        '',
        extra=['organization_id', 'subscription'],
    )
    return JsonResponse({'status': 'applied' if changed else 'duplicate'})


def _finish(ledger: WebhookEvent, outcome: str, detail: str, extra: list | None = None) -> None:
    ledger.outcome = outcome
    ledger.detail = detail[:255]
    ledger.processed_at = timezone.now()
    ledger.save(update_fields=['outcome', 'detail', 'processed_at', *(extra or [])])


def _resolve_account(snapshot) -> BillingAccount | None:
    """Find the billing account this subscription belongs to.

    Three routes, most trustworthy first. The organisation UUID we stamped into
    provider metadata at checkout is preferred; the provider customer id is the
    fallback; an existing local subscription row is the last resort, which covers
    a migrated subscription whose metadata we never wrote.
    """
    from billing.models import Subscription

    if snapshot.organization_id:
        account = BillingAccount.objects.filter(organization_id=snapshot.organization_id).first()
        if account:
            return account

    if snapshot.customer_id:
        account = BillingAccount.objects.filter(provider_customer_id=snapshot.customer_id).first()
        if account:
            return account

    existing = (
        Subscription.objects.filter(provider_subscription_id=snapshot.subscription_id)
        .select_related('account')
        .first()
    )
    return existing.account if existing else None


def _resolve_prices(snapshot) -> list[tuple[PlanPrice, int]]:
    """Map provider price ids to catalogue rows. Unknown ids are dropped, not guessed."""
    ids = [price.price_id for price in snapshot.prices if price.price_id]
    if not ids:
        return []
    known = {
        row.provider_price_id: row
        for row in PlanPrice.objects.filter(provider_price_id__in=ids).select_related('plan')
    }
    return [
        (known[price.price_id], price.quantity)
        for price in snapshot.prices
        if price.price_id in known
    ]


def _resolve_plan(prices):
    """The plan a subscription is on: its first known priced line.

    A subscription whose every price is unknown to the catalogue has no plan, and
    that is recorded as unresolved rather than defaulted — defaulting would grant
    somebody a plan's products because a price id had been mistyped.
    """
    return prices[0][0].plan if prices else None
