"""The source-side exporter.

**This process never runs inside the Billing application.** It is an
operator-controlled one-off with a read-only connection to the monolith and no
route to the Billing database, and the Billing importer in turn has no route to
the monolith. The encrypted artifact is the only bridge, and that separation is
what makes "no runtime cross-database dependency" a property of the deployment
rather than a promise in a comment.

The order of operations:

1. Assert the connection is genuinely read-only, by trying to write and requiring
   the attempt to fail. A `SET TRANSACTION READ ONLY` that somebody removed six
   months ago looks identical to one that is working.
2. Validate the source schema against `schema.py` — exactly, both directions.
3. Read only allowlisted columns from only allowlisted tables.
4. Resolve each monolith practice/PCN to an **Identity organisation UUID**, using
   the mapping the completed Phase 3 Identity migration already produced. A row
   whose organisation cannot be resolved is *refused*, never guessed.
5. Write an authenticated-encrypted artifact at mode 600, with an aggregate,
   keyed manifest.

No plaintext ever reaches disk: the payload is built in memory and encrypted
before a single byte is written.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .artifacts import ArtifactError, write_encrypted_artifact
from .digests import artifact_sha256, canonical_json, identity_digest
from .schema import (
    ALLOWLISTED_COLUMNS,
    EXPORT_SCHEMA_VERSION,
    EXPORTER_VERSION,
    SOURCE_PLAN_KEYS,
    SOURCE_STATUS_MAP,
    SOURCE_SYSTEM,
    SchemaMismatch,
    validate_source_schema,
)

logger = logging.getLogger('haresign.billing')


class ExportRefused(RuntimeError):
    """The export cannot proceed safely. Never worked around, only fixed."""


def assert_read_only(connection) -> None:
    """Prove the source connection cannot write. Refuses if the proof fails.

    Actively tested rather than assumed. The failure this catches is an operator
    who connected with the application's own credentials because the read-only
    role's password had expired — at which point every safety property of this
    migration rests on the exporter containing no INSERT, which is not a property
    anybody can verify by reading it.
    """
    with connection.cursor() as cursor:
        try:
            cursor.execute('CREATE TEMP TABLE haresign_billing_readonly_probe (probe integer)')
        except Exception:
            return
    raise ExportRefused(
        'The source connection accepted a write. The exporter requires a '
        'technically read-only connection and will not run without one.'
    )


def observed_schema(connection) -> dict[str, set[str]]:
    """Read the source's actual columns for the contracted tables."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ANY(%s)
            """,
            [list(ALLOWLISTED_COLUMNS)],
        )
        rows = cursor.fetchall()

    schema: dict[str, set[str]] = {}
    for table, column in rows:
        schema.setdefault(table, set()).add(column)
    return schema


def _select(connection, table: str) -> list[dict]:
    """Read exactly the allowlisted columns of one table."""
    columns = ALLOWLISTED_COLUMNS[table]
    # Column and table names come from a module-level constant, never from input.
    statement = f'SELECT {", ".join(columns)} FROM {table} ORDER BY id'  # noqa: S608
    with connection.cursor() as cursor:
        cursor.execute(statement)
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return str(value)


def build_payload(
    connection,
    *,
    organization_uuid_for: dict[tuple[str, str], str],
    member_links: list[tuple[str, str]] | None = None,
) -> dict:
    """Read the source and build the artifact payload.

    `organization_uuid_for` maps `(kind, source_record_id)` — e.g.
    `('practice', '42')` — to an Identity organisation UUID. It is supplied by the
    operator from the completed Phase 3 Identity migration's own mapping table;
    this exporter does not derive it, because deriving it would mean reading
    Identity's database from the monolith side, which nothing is allowed to do.
    """
    assert_read_only(connection)
    validate_source_schema(observed_schema(connection))

    customers = _select(connection, 'billing_stripe_customer')
    subscriptions = _select(connection, 'billing_subscription')
    grants = _select(connection, 'billing_access_grant')

    refusals: list[str] = []

    def resolve(row, what: str) -> str | None:
        """The Identity organisation UUID for a source row, or a refusal."""
        if row.get('practice_id'):
            key = ('practice', str(row['practice_id']))
        elif row.get('pcn_id'):
            key = ('pcn', str(row['pcn_id']))
        else:
            # A user-scoped row. The monolith allowed a subscription with no
            # workspace and a grant made to a person; neither has an organisation
            # to key to. Refused rather than guessed — see docs/entitlements.md
            # decision D-2.
            refusals.append(f'{what}:{row["id"]}:no_organization')
            return None

        organization_id = organization_uuid_for.get(key)
        if not organization_id:
            refusals.append(f'{what}:{row["id"]}:unmapped_organization')
            return None
        return organization_id

    exported_subscriptions = []
    for row in subscriptions:
        status = (row.get('status') or '').strip().lower()
        if status not in SOURCE_STATUS_MAP:
            refusals.append(f'subscription:{row["id"]}:unsupported_status')
            continue
        plan_key = (row.get('plan_key') or '').strip()
        if plan_key not in SOURCE_PLAN_KEYS:
            refusals.append(f'subscription:{row["id"]}:unknown_plan')
            continue
        organization_id = resolve(row, 'subscription')
        if organization_id is None:
            continue

        exported_subscriptions.append(
            {
                'source_id': str(row['id']),
                'organization_id': organization_id,
                'plan_key': plan_key,
                'state': SOURCE_STATUS_MAP[status],
                'provider': 'stripe',
                'provider_subscription_id': row['stripe_subscription_id'],
                'provider_customer_id': row.get('stripe_customer_id') or '',
                'provider_price_id': row.get('stripe_price_id') or '',
                'current_period_end': _iso(row.get('current_period_end')),
                'cancel_at_period_end': bool(row.get('cancel_at_period_end')),
                'created_at': _iso(row.get('created_at')),
            }
        )

    exported_grants = []
    for row in grants:
        plan_key = (row.get('plan_key') or '').strip()
        if plan_key not in SOURCE_PLAN_KEYS:
            refusals.append(f'grant:{row["id"]}:unknown_plan')
            continue
        organization_id = resolve(row, 'grant')
        if organization_id is None:
            continue

        exported_grants.append(
            {
                'source_id': str(row['id']),
                'organization_id': organization_id,
                'plan_key': plan_key,
                'expires_at': _iso(row.get('expires_at')),
                'revoked_at': _iso(row.get('revoked_at')),
                # Operator free text, length-capped here and scrubbed again on
                # import. It is the only free-text field crossing the boundary.
                'reason': (row.get('note') or '')[:255],
                'created_at': _iso(row.get('created_at')),
            }
        )

    # `billing_stripe_customer` crosses only as a customer-id lookup, keyed by the
    # customer id itself. The user_id it maps from is *not* exported: Billing keys
    # customers to organisations, and a person→customer table here would be a copy
    # of monolith identity data with no purpose.
    customer_ids = sorted(
        {row['stripe_customer_id'] for row in customers if row.get('stripe_customer_id')}
    )

    payload = {
        'schema_version': EXPORT_SCHEMA_VERSION,
        'exporter_version': EXPORTER_VERSION,
        'source_system': SOURCE_SYSTEM,
        'exported_at': datetime.now(tz=UTC).isoformat(),
        'subscriptions': exported_subscriptions,
        'grants': exported_grants,
        'provider_customer_ids': customer_ids,
        'member_organization_links': [
            {'parent_organization_id': parent, 'child_organization_id': child}
            for parent, child in (member_links or [])
        ],
        'refusals': sorted(refusals),
    }
    payload['manifest'] = build_manifest(payload)
    return payload


def build_manifest(payload: dict) -> dict:
    """Aggregate, privacy-safe counts plus a keyed checksum.

    Counts only. A manifest that listed which organisations were exported would
    be a customer list, and manifests get pasted into tickets.

    The checksum is a **keyed** HMAC, not a bare hash: a plain SHA-256 over a
    small structured document is guessable by an attacker who can enumerate
    plausible count combinations, which would let them confirm how many customers
    Haresign has from the manifest alone.
    """
    counts = {
        'subscriptions': len(payload['subscriptions']),
        'grants': len(payload['grants']),
        'provider_customers': len(payload['provider_customer_ids']),
        'member_links': len(payload['member_organization_links']),
        'refusals': len(payload['refusals']),
        'organizations': len(
            {row['organization_id'] for row in payload['subscriptions']}
            | {row['organization_id'] for row in payload['grants']}
        ),
    }
    by_state: dict[str, int] = {}
    for row in payload['subscriptions']:
        by_state[row['state']] = by_state.get(row['state'], 0) + 1
    by_plan: dict[str, int] = {}
    for row in payload['subscriptions']:
        by_plan[row['plan_key']] = by_plan.get(row['plan_key'], 0) + 1

    manifest = {
        'counts': counts,
        'subscriptions_by_state': dict(sorted(by_state.items())),
        'subscriptions_by_plan': dict(sorted(by_plan.items())),
    }
    manifest['checksum'] = identity_digest(manifest, 'manifest')
    return manifest


def export(
    connection,
    *,
    destination,
    key: bytes,
    organization_uuid_for: dict[tuple[str, str], str],
    member_links: list[tuple[str, str]] | None = None,
) -> dict:
    """Build, encrypt and write the artifact. Returns the manifest and digest."""
    payload = build_payload(
        connection,
        organization_uuid_for=organization_uuid_for,
        member_links=member_links,
    )
    try:
        encrypted = write_encrypted_artifact(destination, payload, key)
    except ArtifactError:
        raise
    return {
        'manifest': payload['manifest'],
        'artifact_sha256': artifact_sha256(encrypted),
        'bytes': len(encrypted),
        'refusals': len(payload['refusals']),
    }


def payload_digest(payload: dict) -> str:
    return artifact_sha256(canonical_json(payload))


__all__ = [
    'ExportRefused',
    'SchemaMismatch',
    'assert_read_only',
    'build_manifest',
    'build_payload',
    'export',
    'observed_schema',
]
