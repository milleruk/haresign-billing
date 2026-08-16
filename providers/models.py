"""The webhook event ledger.

The monolith had none. Its webhook verified the signature and then called
`update_or_create` — which is *naturally* idempotent for a re-delivery of the same
state, and completely wrong for a re-delivery of an *older* state, which it would
happily apply over newer state. It also had no record that an event had ever
arrived, so "did Stripe tell us about this?" was unanswerable.

Every verified event gets exactly one row here, keyed on the provider's own event
id, and that uniqueness constraint is what makes processing idempotent — not a
convention, not a cache, a database constraint.

What is deliberately **not** stored: the payload. A digest proves a re-delivery is
byte-identical without this table becoming a copy of every customer's billing
history, complete with addresses and payment methods, on a second system.
"""

from __future__ import annotations

import uuid

from django.db import models


class WebhookEvent(models.Model):
    """One verified provider event, and what became of it."""

    class Outcome(models.TextChoices):
        # Applied and changed something.
        APPLIED = 'applied', 'Applied'
        # Verified, understood, and produced no change — the ordinary result of a
        # provider re-delivering an event we have already acted on.
        DUPLICATE = 'duplicate', 'Duplicate'
        # Older than the state already applied. Recorded, never applied.
        OUT_OF_ORDER = 'out_of_order', 'Out of order'
        # An event type this service does not act on.
        IGNORED = 'ignored', 'Ignored'
        # Understood but not applicable — no billing account, unknown price.
        UNRESOLVED = 'unresolved', 'Unresolved'
        FAILED = 'failed', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    provider = models.CharField(max_length=32, default='stripe')
    # The provider's own event id. The uniqueness constraint below is the
    # idempotency mechanism for the whole service.
    provider_event_id = models.CharField(max_length=255)
    event_type = models.CharField(max_length=100, db_index=True)

    # When the provider says it happened, and when we first saw it. Both, because
    # the gap between them is what a delivery-delay incident looks like.
    provider_created_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    outcome = models.CharField(max_length=20, choices=Outcome.choices, db_index=True)
    # Short, fixed strings — never a provider error body, never a payload excerpt.
    detail = models.CharField(max_length=255, blank=True, default='')

    # SHA-256 of the raw body. Lets a re-delivery be proven identical without the
    # body being kept.
    payload_digest = models.CharField(max_length=64, blank=True, default='')

    # How many times the provider has re-delivered this event id. A high count is
    # the signal that our endpoint has been answering non-2xx.
    delivery_count = models.PositiveIntegerField(default=1)

    organization_id = models.UUIDField(null=True, blank=True, db_index=True)
    subscription = models.ForeignKey(
        'billing.Subscription',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='webhook_events',
    )

    class Meta:
        ordering = ['-received_at']
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'provider_event_id'],
                name='providers_event_unique',
            ),
        ]
        indexes = [models.Index(fields=['outcome', '-received_at'])]

    def __str__(self) -> str:
        return f'{self.event_type} {self.provider_event_id} ({self.outcome})'


class ReconciliationRun(models.Model):
    """One comparison of local state against the provider's.

    Aggregate counts only. A reconciliation report that listed which customers
    disagreed would be a customer list with financial state attached, emailed
    around and archived; the counts answer "is anything wrong" and the audit trail
    answers "which one" for whoever is authorised to ask.
    """

    class Status(models.TextChoices):
        MATCHED = 'matched', 'Matched'
        DRIFTED = 'drifted', 'Drifted'
        FAILED = 'failed', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=32, default='stripe')
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)

    # {'checked': n, 'matched': n, 'state_mismatch': n, 'period_mismatch': n,
    #  'missing_locally': n, 'missing_at_provider': n}
    counts = models.JSONField(default=dict)
    # True when the run was allowed to write corrections, False for a report-only
    # pass. Report-only is the default: a reconciliation that silently rewrites
    # state removes the evidence of what drifted.
    applied = models.BooleanField(default=False)

    class Meta:
        ordering = ['-started_at']

    def __str__(self) -> str:
        return f'{self.started_at:%Y-%m-%d %H:%M} {self.status}'
