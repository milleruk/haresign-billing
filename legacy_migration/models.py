"""Migration run history and durable legacy mappings.

Two jobs.

**The run ledger** (`ImportRun`) records every dry-run, apply and reconciliation:
which artifact, which schema version, which exporter, and aggregate counts. It is
what makes "has this artifact already been applied?" answerable, and it is why a
re-run is a no-op rather than a duplicate import.

**The mappings** are the durable link between a monolith row and what it became
here. They are what make a *delta* possible: without them, a second run has no
way to tell "this subscription already exists" from "this is a new subscription
that happens to look similar", and would either duplicate or overwrite.

Both fingerprints are **keyed digests**, never the source values. A mapping table
that stored Stripe subscription ids in plaintext alongside organisation UUIDs
would be a re-identification dataset; the digests answer "has this changed?"
without answering "what is it?".
"""

from __future__ import annotations

import uuid

from django.db import models


class ImportRun(models.Model):
    class Operation(models.TextChoices):
        DRY_RUN = 'dry_run', 'Dry run'
        APPLY = 'apply', 'Apply'
        RECONCILE = 'reconcile', 'Reconcile'

    class Status(models.TextChoices):
        SUCCEEDED = 'succeeded', 'Succeeded'
        CONFLICT = 'conflict', 'Conflict'
        FAILED = 'failed', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_system = models.CharField(max_length=64)
    operation = models.CharField(max_length=16, choices=Operation.choices)
    status = models.CharField(max_length=16, choices=Status.choices)

    export_schema_version = models.PositiveSmallIntegerField()
    exporter_version = models.CharField(max_length=32)
    # SHA-256 of the encrypted artifact. The apply path refuses an artifact whose
    # digest already has a successful apply, which is what makes a re-run a no-op
    # even before a single row is compared.
    artifact_sha256 = models.CharField(max_length=64, db_index=True)
    # A keyed digest over the aggregate manifest. Privacy-safe: it proves two runs
    # saw the same source shape without recording what the source contained.
    manifest_checksum = models.CharField(max_length=64)

    source_counts = models.JSONField(default=dict)
    result_counts = models.JSONField(default=dict)
    conflict_counts = models.JSONField(default=dict)

    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']
        indexes = [models.Index(fields=['artifact_sha256', 'operation', 'status'])]

    def __str__(self) -> str:
        return f'{self.operation} {self.status} {self.started_at:%Y-%m-%d %H:%M}'


class LegacyAccountMapping(models.Model):
    """A monolith organisation (practice or PCN) → a Billing account."""

    class Kind(models.TextChoices):
        PRACTICE = 'practice', 'Practice'
        PCN = 'pcn', 'PCN'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_system = models.CharField(max_length=64)
    source_kind = models.CharField(max_length=16, choices=Kind.choices)
    # The monolith's own row id for the practice or PCN.
    source_record_id = models.CharField(max_length=64)

    account = models.ForeignKey(
        'billing.BillingAccount', on_delete=models.PROTECT, related_name='legacy_mappings'
    )

    first_imported_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField()
    last_import_run = models.ForeignKey(
        ImportRun, null=True, on_delete=models.SET_NULL, related_name='account_mappings'
    )

    source_fingerprint = models.CharField(max_length=64)
    target_fingerprint = models.CharField(max_length=64)
    # Set when a later run no longer finds this row at the source. Never deleted:
    # a source record that disappears is a fact worth keeping, and deleting the
    # mapping would make the next run re-import it as new.
    source_missing = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['source_system', 'source_kind', 'source_record_id'],
                name='legacy_account_source_unique',
            ),
            models.UniqueConstraint(
                fields=['source_system', 'account'], name='legacy_account_target_unique'
            ),
        ]

    def __str__(self) -> str:
        return f'{self.source_kind}:{self.source_record_id}'


class LegacySubscriptionMapping(models.Model):
    """A monolith `billing_subscription` row → a Billing subscription."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_system = models.CharField(max_length=64)
    source_record_id = models.CharField(max_length=64)

    subscription = models.ForeignKey(
        'billing.Subscription', on_delete=models.PROTECT, related_name='legacy_mappings'
    )
    # Keyed digest of the provider subscription id. Lets a collision be detected
    # without the identifier itself living in a second table.
    provider_reference_digest = models.CharField(max_length=64, db_index=True)

    first_imported_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField()
    last_import_run = models.ForeignKey(
        ImportRun, null=True, on_delete=models.SET_NULL, related_name='subscription_mappings'
    )

    source_fingerprint = models.CharField(max_length=64)
    target_fingerprint = models.CharField(max_length=64)
    source_missing = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['source_system', 'source_record_id'],
                name='legacy_subscription_source_unique',
            ),
            models.UniqueConstraint(
                fields=['source_system', 'subscription'],
                name='legacy_subscription_target_unique',
            ),
        ]

    def __str__(self) -> str:
        return f'subscription:{self.source_record_id}'


class LegacyGrantMapping(models.Model):
    """A monolith `billing_access_grant` row → a Billing complimentary grant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_system = models.CharField(max_length=64)
    source_record_id = models.CharField(max_length=64)

    grant = models.ForeignKey(
        'billing.ComplimentaryGrant', on_delete=models.PROTECT, related_name='legacy_mappings'
    )

    first_imported_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField()
    last_import_run = models.ForeignKey(
        ImportRun, null=True, on_delete=models.SET_NULL, related_name='grant_mappings'
    )

    source_fingerprint = models.CharField(max_length=64)
    target_fingerprint = models.CharField(max_length=64)
    source_missing = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['source_system', 'source_record_id'],
                name='legacy_grant_source_unique',
            ),
            models.UniqueConstraint(
                fields=['source_system', 'grant'], name='legacy_grant_target_unique'
            ),
        ]

    def __str__(self) -> str:
        return f'grant:{self.source_record_id}'
