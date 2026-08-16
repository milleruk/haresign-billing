"""Comparing local subscription state against the provider's.

Webhooks are best-effort. Deliveries are missed, endpoints are misconfigured for
an afternoon, and a signing-secret rotation done in the wrong order silently
drops every event until somebody notices. Reconciliation is how that is noticed.

**Report-only by default.** A reconciliation that quietly rewrites local state to
match the provider destroys the evidence of what drifted and how long it was
wrong — which is exactly the information an incident needs. `apply=True` is a
deliberate, audited act.

**Aggregate counts out.** The run record holds how many rows matched and how many
disagreed, never which customers. The audit trail carries the per-subscription
detail for whoever is authorised to ask.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from audit import events as audit_events
from audit.services import record
from billing.models import Subscription
from billing.services import apply_subscription_snapshot

from .base import ProviderError, get_provider
from .mapping import subscription_state
from .models import ReconciliationRun
from .webhooks import _resolve_plan, _resolve_prices

logger = logging.getLogger('haresign.billing')


def reconcile(*, apply: bool = False, request=None) -> ReconciliationRun:
    """Compare every local subscription with the provider. Returns the run record."""
    provider = get_provider()
    run = ReconciliationRun.objects.create(
        provider=provider.name, status=ReconciliationRun.Status.MATCHED, applied=apply
    )

    counts = {
        'checked': 0,
        'matched': 0,
        'state_mismatch': 0,
        'period_mismatch': 0,
        'missing_locally': 0,
        'missing_at_provider': 0,
        'corrected': 0,
    }

    try:
        remote = {sub.subscription_id: sub for sub in provider.list_subscriptions()}
    except ProviderError:
        logger.exception('reconciliation: could not list subscriptions')
        run.status = ReconciliationRun.Status.FAILED
        run.completed_at = timezone.now()
        run.counts = counts
        run.save(update_fields=['status', 'completed_at', 'counts'])
        return run

    local = list(
        Subscription.objects.select_related('account', 'plan').filter(provider=provider.name)
    )
    seen = set()

    for subscription in local:
        counts['checked'] += 1
        snapshot = remote.get(subscription.provider_subscription_id)
        if snapshot is None:
            counts['missing_at_provider'] += 1
            continue
        seen.add(snapshot.subscription_id)

        expected_state = subscription_state(snapshot.status)
        state_differs = expected_state != subscription.state
        period_differs = snapshot.current_period_end != subscription.current_period_end

        if not state_differs and not period_differs:
            counts['matched'] += 1
            # Confirmed by the provider even though nothing changed, which is the
            # freshness signal the UI reads.
            subscription.provider_synced_at = timezone.now()
            subscription.save(update_fields=['provider_synced_at', 'updated_at'])
            continue

        if state_differs:
            counts['state_mismatch'] += 1
        if period_differs:
            counts['period_mismatch'] += 1

        record(
            audit_events.RECONCILIATION_RUN,
            request=request,
            organization_id=subscription.account.organization_id,
            metadata={
                'drift': 'state' if state_differs else 'period',
                'local_state': subscription.state,
                'provider_state': expected_state,
                'applied': apply,
            },
        )

        if apply:
            prices = _resolve_prices(snapshot)
            plan = _resolve_plan(prices) or subscription.plan
            apply_subscription_snapshot(
                account=subscription.account,
                provider=provider.name,
                provider_subscription_id=snapshot.subscription_id,
                state=expected_state,
                plan=plan,
                prices=prices,
                provider_customer_id=snapshot.customer_id,
                current_period_start=snapshot.current_period_start,
                current_period_end=snapshot.current_period_end,
                trial_end=snapshot.trial_end,
                cancel_at_period_end=snapshot.cancel_at_period_end,
                canceled_at=snapshot.canceled_at,
                ended_at=snapshot.ended_at,
                # Reconciliation reads the provider's *current* state, so it is by
                # definition newer than anything stored. Passing no sequence skips
                # the ordering guard, which would otherwise refuse the correction.
                sequence=0,
                request=request,
            )
            counts['corrected'] += 1

    counts['missing_locally'] = len([key for key in remote if key not in seen])

    drifted = any(
        counts[key]
        for key in (
            'state_mismatch',
            'period_mismatch',
            'missing_locally',
            'missing_at_provider',
        )
    )
    run.status = ReconciliationRun.Status.DRIFTED if drifted else ReconciliationRun.Status.MATCHED
    run.counts = counts
    run.completed_at = timezone.now()
    run.save(update_fields=['status', 'counts', 'completed_at'])
    return run
