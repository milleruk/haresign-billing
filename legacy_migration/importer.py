"""The Billing-side importer.

Runs inside the Billing application, with the Billing database and an encrypted
artifact. It has **no connection to the monolith** and no way to acquire one —
there is no source-database configuration in this service's settings at all.

The contract, in order:

* **Decrypt and authenticate.** AES-GCM with the magic string as associated data,
  so a tampered artifact fails to decrypt rather than importing altered rows.
* **Refuse an unknown schema version.** Best-effort parsing of a format we were
  not written for is how a plan key silently becomes a state.
* **Dry-run first, always.** `apply()` refuses unless a successful dry-run for
  this exact artifact digest is already on record.
* **Conflicts stop the run.** Every conflict is counted and the whole transaction
  is rolled back. There is no "skip the bad rows and carry on" mode, because the
  rows that conflict are precisely the ones somebody needs to look at.
* **Transactional.** One `atomic` block for the whole apply. A partial billing
  import is worse than none: half an organisation's subscriptions is a state
  nobody can reason about.
* **Idempotent.** A second apply of the same artifact finds every mapping already
  present, changes nothing and records a no-op.
* **Aggregate output only.** Counts, never rows.
"""

from __future__ import annotations

import logging
from datetime import datetime

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from audit import events as audit_events
from audit.services import record
from billing.models import (
    BillingAccount,
    ComplimentaryGrant,
    MemberOrganizationLink,
    Subscription,
)
from catalog.models import Plan, PlanPrice

from .artifacts import ArtifactError, decrypt_payload
from .digests import artifact_sha256, identity_digest
from .models import (
    ImportRun,
    LegacyAccountMapping,
    LegacyGrantMapping,
    LegacySubscriptionMapping,
)
from .schema import EXPORT_SCHEMA_VERSION, SOURCE_SYSTEM

logger = logging.getLogger('haresign.billing')


class ImportConflict(RuntimeError):
    """The artifact cannot be applied without destroying or inventing information."""


class DryRunRequired(RuntimeError):
    """An apply was attempted without a successful dry-run of the same artifact."""


def _dt(value) -> datetime | None:
    if not value:
        return None
    parsed = parse_datetime(value) if isinstance(value, str) else value
    if parsed is None:
        return None
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


def _fingerprint(row: dict) -> str:
    """A keyed digest of a source row. Detects change without storing the row."""
    return identity_digest(row, 'source_fingerprint')


def load(data: bytes, key: bytes) -> tuple[dict, str]:
    """Decrypt, authenticate and version-check. Returns `(payload, artifact_sha256)`."""
    digest = artifact_sha256(data)
    payload = decrypt_payload(data, key)

    version = payload.get('schema_version')
    if version != EXPORT_SCHEMA_VERSION:
        raise ArtifactError(
            f'Artifact schema version {version!r} is not the version this importer '
            f'was written for ({EXPORT_SCHEMA_VERSION}).'
        )
    if payload.get('source_system') != SOURCE_SYSTEM:
        raise ArtifactError('Artifact was produced for a different source system.')
    return payload, digest


def _plan_cache() -> dict[str, Plan]:
    return {plan.key: plan for plan in Plan.objects.prefetch_related('products')}


def _price_cache() -> dict[str, PlanPrice]:
    return {
        price.provider_price_id: price
        for price in PlanPrice.objects.exclude(provider_price_id='').select_related('plan')
    }


def run(
    payload: dict,
    artifact_digest: str,
    *,
    operation: str,
    request=None,
) -> ImportRun:
    """Execute one dry-run or apply. Returns the recorded `ImportRun`.

    Both operations do exactly the same work and produce exactly the same counts.
    A dry run differs in one respect: the transaction is rolled back at the end.
    That is deliberate — a dry run that took a *different* code path would be
    testing something other than the apply.
    """
    if operation not in (ImportRun.Operation.DRY_RUN, ImportRun.Operation.APPLY):
        raise ValueError('operation must be dry_run or apply')

    if operation == ImportRun.Operation.APPLY:
        proven = ImportRun.objects.filter(
            artifact_sha256=artifact_digest,
            operation=ImportRun.Operation.DRY_RUN,
            status=ImportRun.Status.SUCCEEDED,
        ).exists()
        if not proven:
            raise DryRunRequired(
                'Refusing to apply an artifact that has not completed a successful '
                'dry-run. Run the dry-run first and read its reconciliation.'
            )

    manifest = payload.get('manifest') or {}
    source_counts = manifest.get('counts', {})

    record(
        audit_events.MIGRATION_RUN_STARTED,
        request=request,
        metadata={'operation': operation, 'artifact_sha256': artifact_digest},
    )

    result: dict[str, int] = {}
    conflicts: dict[str, int] = {}
    status = ImportRun.Status.SUCCEEDED

    class _Rollback(Exception):
        """Internal signal used to roll a dry run back without reporting failure."""

    try:
        with transaction.atomic():
            result, conflicts = _apply_payload(payload, request=request)
            if conflicts:
                # Conflicts abort the whole run, apply and dry-run alike. There is
                # deliberately no partial-apply mode.
                status = ImportRun.Status.CONFLICT
                raise _Rollback
            if operation == ImportRun.Operation.DRY_RUN:
                raise _Rollback
    except _Rollback:
        pass
    except Exception:
        logger.exception('billing migration: import failed')
        status = ImportRun.Status.FAILED

    import_run = ImportRun.objects.create(
        source_system=SOURCE_SYSTEM,
        operation=operation,
        status=status,
        export_schema_version=payload['schema_version'],
        exporter_version=payload.get('exporter_version', ''),
        artifact_sha256=artifact_digest,
        manifest_checksum=manifest.get('checksum', ''),
        source_counts=source_counts,
        result_counts=result,
        conflict_counts=conflicts,
        completed_at=timezone.now(),
    )

    event = {
        ImportRun.Status.SUCCEEDED: audit_events.MIGRATION_RUN_COMPLETED,
        ImportRun.Status.CONFLICT: audit_events.MIGRATION_RUN_CONFLICT,
        ImportRun.Status.FAILED: audit_events.MIGRATION_RUN_FAILED,
    }[status]
    record(
        event,
        request=request,
        metadata={
            'operation': operation,
            'artifact_sha256': artifact_digest,
            'counts': result,
            'conflicts': conflicts,
        },
    )
    return import_run


def _apply_payload(payload: dict, *, request=None) -> tuple[dict, dict]:
    """Do the work. Returns `(result_counts, conflict_counts)`."""
    now = timezone.now()
    plans = _plan_cache()
    prices = _price_cache()

    result = {
        'accounts_created': 0,
        'accounts_matched': 0,
        'subscriptions_created': 0,
        'subscriptions_updated': 0,
        'subscriptions_unchanged': 0,
        'grants_created': 0,
        'grants_unchanged': 0,
        'member_links_created': 0,
        'source_removed': 0,
    }
    conflicts: dict[str, int] = {}

    def conflict(kind: str) -> None:
        conflicts[kind] = conflicts.get(kind, 0) + 1

    # --- Accounts, derived from the organisations the rows name -----------------
    accounts: dict[str, BillingAccount] = {}

    for row in [*payload['subscriptions'], *payload['grants']]:
        organization_id = row['organization_id']
        if organization_id in accounts:
            continue
        account, created = BillingAccount.objects.get_or_create(organization_id=organization_id)
        accounts[organization_id] = account
        result['accounts_created' if created else 'accounts_matched'] += 1

    # --- Member organisation links ---------------------------------------------
    for link in payload.get('member_organization_links', []):
        parent = link['parent_organization_id']
        child = link['child_organization_id']
        if parent == child:
            conflict('member_link_self_reference')
            continue
        _, created = MemberOrganizationLink.objects.get_or_create(
            parent_organization_id=parent,
            child_organization_id=child,
            defaults={'source': 'legacy_migration', 'observed_at': now},
        )
        if created:
            result['member_links_created'] += 1

    # --- Subscriptions -----------------------------------------------------------
    seen_subscription_ids: set[str] = set()

    for row in payload['subscriptions']:
        source_id = row['source_id']
        seen_subscription_ids.add(source_id)

        plan = plans.get(row['plan_key'])
        if plan is None:
            conflict('unknown_plan')
            continue

        account = accounts[row['organization_id']]
        provider_reference = row['provider_subscription_id']
        reference_digest = identity_digest(provider_reference, 'provider_reference')

        mapping = LegacySubscriptionMapping.objects.filter(
            source_system=SOURCE_SYSTEM, source_record_id=source_id
        ).first()

        # A provider subscription id already held by a *different* source row is a
        # collision. Two monolith rows claiming one Stripe subscription is a
        # data-quality incident, not a state to resolve by picking one.
        collision = (
            LegacySubscriptionMapping.objects.filter(
                source_system=SOURCE_SYSTEM, provider_reference_digest=reference_digest
            )
            .exclude(source_record_id=source_id)
            .exists()
        )
        if collision:
            conflict('provider_identifier_collision')
            continue

        existing = Subscription.objects.filter(
            provider=row['provider'], provider_subscription_id=provider_reference
        ).first()
        if existing is not None and mapping is None:
            # The subscription exists here but was not imported by us. Adopting it
            # would silently claim a row somebody else created.
            conflict('unmapped_existing_subscription')
            continue
        if existing is not None and existing.account_id != account.id:
            # The same provider subscription now names a different organisation.
            # Re-pointing it would move a paid subscription between customers.
            conflict('organization_uuid_collision')
            continue

        fingerprint = _fingerprint(row)
        if mapping is not None and mapping.source_fingerprint == fingerprint:
            mapping.last_seen_at = now
            mapping.source_missing = False
            mapping.save(update_fields=['last_seen_at', 'source_missing'])
            result['subscriptions_unchanged'] += 1
            continue

        price = prices.get(row.get('provider_price_id') or '')
        values = {
            'account': account,
            'plan': price.plan if price else plan,
            'state': row['state'],
            'provider_customer_id': row.get('provider_customer_id') or '',
            'current_period_end': _dt(row.get('current_period_end')),
            'cancel_at_period_end': bool(row.get('cancel_at_period_end')),
        }

        if existing is None:
            subscription = Subscription.objects.create(
                provider=row['provider'],
                provider_subscription_id=provider_reference,
                **values,
            )
            result['subscriptions_created'] += 1
        else:
            for field, value in values.items():
                setattr(existing, field, value)
            existing.save()
            subscription = existing
            result['subscriptions_updated'] += 1

        if price is not None:
            subscription.items.update_or_create(price=price, defaults={'quantity': 1})

        LegacySubscriptionMapping.objects.update_or_create(
            source_system=SOURCE_SYSTEM,
            source_record_id=source_id,
            defaults={
                'subscription': subscription,
                'provider_reference_digest': reference_digest,
                'last_seen_at': now,
                'source_fingerprint': fingerprint,
                'target_fingerprint': identity_digest(
                    {
                        'state': subscription.state,
                        'plan': subscription.plan.key,
                        'organization_id': str(account.organization_id),
                    },
                    'target_fingerprint',
                ),
                'source_missing': False,
            },
        )

        # Keyed on the organisation alone. `source_kind` is recorded once, on
        # creation, and is never part of the lookup: an organisation that moves
        # from a practice plan to a PCN plan is the same organisation, and keying
        # on the plan would try to mint a second mapping for one account and hit
        # the target-unique constraint.
        LegacyAccountMapping.objects.get_or_create(
            source_system=SOURCE_SYSTEM,
            source_record_id=str(account.organization_id),
            defaults={
                'account': account,
                'source_kind': (
                    LegacyAccountMapping.Kind.PCN
                    if plan.key == 'pcn'
                    else LegacyAccountMapping.Kind.PRACTICE
                ),
                'last_seen_at': now,
                'source_fingerprint': identity_digest(
                    str(account.organization_id), 'source_fingerprint'
                ),
                'target_fingerprint': identity_digest(str(account.id), 'target_fingerprint'),
            },
        )

    # --- Complimentary grants ----------------------------------------------------
    for row in payload['grants']:
        source_id = row['source_id']
        plan = plans.get(row['plan_key'])
        if plan is None:
            conflict('unknown_plan')
            continue

        expires_at = _dt(row.get('expires_at'))
        if expires_at is None:
            conflict('grant_without_expiry')
            continue

        account = accounts[row['organization_id']]
        fingerprint = _fingerprint(row)

        mapping = LegacyGrantMapping.objects.filter(
            source_system=SOURCE_SYSTEM, source_record_id=source_id
        ).first()
        if mapping is not None and mapping.source_fingerprint == fingerprint:
            mapping.last_seen_at = now
            mapping.source_missing = False
            mapping.save(update_fields=['last_seen_at', 'source_missing'])
            result['grants_unchanged'] += 1
            continue

        if mapping is None:
            grant = ComplimentaryGrant.objects.create(
                account=account,
                plan=plan,
                expires_at=expires_at,
                revoked_at=_dt(row.get('revoked_at')),
                reason=(row.get('reason') or '')[:255],
            )
            result['grants_created'] += 1
        else:
            grant = mapping.grant
            grant.plan = plan
            grant.expires_at = expires_at
            grant.revoked_at = _dt(row.get('revoked_at'))
            grant.reason = (row.get('reason') or '')[:255]
            grant.save()

        LegacyGrantMapping.objects.update_or_create(
            source_system=SOURCE_SYSTEM,
            source_record_id=source_id,
            defaults={
                'grant': grant,
                'last_seen_at': now,
                'source_fingerprint': fingerprint,
                'target_fingerprint': identity_digest(
                    {'plan': plan.key, 'expires_at': expires_at.isoformat()},
                    'target_fingerprint',
                ),
                'source_missing': False,
            },
        )

    # --- Source removals ---------------------------------------------------------
    # A mapping whose source row is no longer in the artifact is *flagged*, never
    # deleted and never used to revoke anything. A subscription disappearing from
    # the monolith is not the same fact as a subscription being cancelled, and
    # cancelling a paying customer because a row moved would be the worst possible
    # reading of an ambiguous signal.
    stale = LegacySubscriptionMapping.objects.filter(
        source_system=SOURCE_SYSTEM, source_missing=False
    ).exclude(source_record_id__in=seen_subscription_ids)
    result['source_removed'] = stale.update(source_missing=True)

    return result, conflicts


def reconcile(payload: dict) -> dict:
    """Compare the artifact against what was imported. Aggregate counts only."""
    counts = {
        'source_subscriptions': len(payload['subscriptions']),
        'mapped_subscriptions': 0,
        'state_matches': 0,
        'state_mismatches': 0,
        'missing_locally': 0,
        'source_grants': len(payload['grants']),
        'mapped_grants': 0,
    }

    for row in payload['subscriptions']:
        mapping = (
            LegacySubscriptionMapping.objects.filter(
                source_system=SOURCE_SYSTEM, source_record_id=row['source_id']
            )
            .select_related('subscription')
            .first()
        )
        if mapping is None:
            counts['missing_locally'] += 1
            continue
        counts['mapped_subscriptions'] += 1
        if mapping.subscription.state == row['state']:
            counts['state_matches'] += 1
        else:
            counts['state_mismatches'] += 1

    counts['mapped_grants'] = LegacyGrantMapping.objects.filter(
        source_system=SOURCE_SYSTEM,
        source_record_id__in=[row['source_id'] for row in payload['grants']],
    ).count()

    return counts
