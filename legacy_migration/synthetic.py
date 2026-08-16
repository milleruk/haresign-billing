"""A synthetic monolith-shaped billing source.

Every migration exercise in this phase runs against this, and **no live billing
record has been read**. The point is not to approximate the monolith's data; it is
to reproduce the monolith's *schema* exactly — the same three tables, the same
column names and types, the same status vocabulary — so that the exporter's schema
validation, its allowlist and its refusals are exercised against the real contract
rather than against a convenient fiction.

The DDL below is transcribed from the monolith's two billing migrations, read-only.
Everything in the rows is invented: organisation ids are sequential integers,
provider identifiers are obviously fake (`sub_synthetic_…`), and nothing resembles
a real customer.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.utils import timezone

# Exactly the monolith's schema for the three billing tables. Deliberately
# includes every column, including the ones the allowlist does not extract, so a
# test can prove the exporter takes only what it is entitled to.
SOURCE_DDL = """
CREATE TABLE billing_stripe_customer (
    id                      bigserial PRIMARY KEY,
    user_id                 integer NOT NULL,
    stripe_customer_id      varchar(255) NOT NULL UNIQUE,
    created_at              timestamptz NOT NULL
);

CREATE TABLE billing_subscription (
    id                      bigserial PRIMARY KEY,
    user_id                 integer NOT NULL,
    practice_id             integer NULL,
    pcn_id                  integer NULL,
    plan_key                varchar(50) NOT NULL,
    stripe_subscription_id  varchar(255) NOT NULL UNIQUE,
    stripe_customer_id      varchar(255) NOT NULL,
    stripe_price_id         varchar(255) NOT NULL DEFAULT '',
    status                  varchar(30) NOT NULL,
    current_period_end      timestamptz NULL,
    cancel_at_period_end    boolean NOT NULL DEFAULT false,
    created_at              timestamptz NOT NULL,
    updated_at              timestamptz NOT NULL
);

CREATE TABLE billing_access_grant (
    id                      bigserial PRIMARY KEY,
    user_id                 integer NULL,
    practice_id             integer NULL,
    pcn_id                  integer NULL,
    plan_key                varchar(50) NOT NULL DEFAULT 'practice',
    expires_at              timestamptz NOT NULL,
    note                    varchar(255) NOT NULL DEFAULT '',
    granted_by_id           integer NULL,
    revoked_at              timestamptz NULL,
    revoked_by_id           integer NULL,
    created_at              timestamptz NOT NULL,
    updated_at              timestamptz NOT NULL
);
"""


class SyntheticSource:
    """A monolith-shaped source held in memory, with a cursor the exporter accepts.

    Not a database. The exporter needs three things from a connection — a failing
    write probe, an `information_schema` query and three `SELECT`s — and providing
    those directly keeps the test suite free of a second live PostgreSQL.

    The runtime rehearsal uses a real PostgreSQL loaded from `SOURCE_DDL` instead,
    so both the in-memory and the on-the-wire paths are exercised.
    """

    def __init__(self, *, read_only: bool = True):
        self.read_only = read_only
        self.schema = {
            'billing_stripe_customer': {'id', 'user_id', 'stripe_customer_id', 'created_at'},
            'billing_subscription': {
                'id',
                'user_id',
                'practice_id',
                'pcn_id',
                'plan_key',
                'stripe_subscription_id',
                'stripe_customer_id',
                'stripe_price_id',
                'status',
                'current_period_end',
                'cancel_at_period_end',
                'created_at',
                'updated_at',
            },
            'billing_access_grant': {
                'id',
                'user_id',
                'practice_id',
                'pcn_id',
                'plan_key',
                'expires_at',
                'note',
                'granted_by_id',
                'revoked_at',
                'revoked_by_id',
                'created_at',
                'updated_at',
            },
        }
        self.rows: dict[str, list[dict]] = {
            'billing_stripe_customer': [],
            'billing_subscription': [],
            'billing_access_grant': [],
        }

    # --- The connection surface the exporter uses ------------------------------

    def cursor(self):
        return _Cursor(self)

    # --- Seeding ---------------------------------------------------------------

    def add_customer(self, *, user_id: int, customer_id: str) -> dict:
        row = {
            'id': len(self.rows['billing_stripe_customer']) + 1,
            'user_id': user_id,
            'stripe_customer_id': customer_id,
            'created_at': timezone.now(),
        }
        self.rows['billing_stripe_customer'].append(row)
        return row

    def add_subscription(
        self,
        *,
        practice_id: int | None = None,
        pcn_id: int | None = None,
        plan_key: str = 'practice',
        status: str = 'active',
        user_id: int = 1,
        subscription_id: str = '',
        customer_id: str = '',
        price_id: str = '',
        period_end=None,
        cancel_at_period_end: bool = False,
    ) -> dict:
        index = len(self.rows['billing_subscription']) + 1
        row = {
            'id': index,
            'user_id': user_id,
            'practice_id': practice_id,
            'pcn_id': pcn_id,
            'plan_key': plan_key,
            'stripe_subscription_id': subscription_id or f'sub_synthetic_{index:04d}',
            'stripe_customer_id': customer_id or f'cus_synthetic_{index:04d}',
            'stripe_price_id': price_id,
            'status': status,
            'current_period_end': period_end or (timezone.now() + timedelta(days=30)),
            'cancel_at_period_end': cancel_at_period_end,
            'created_at': timezone.now(),
            'updated_at': timezone.now(),
        }
        self.rows['billing_subscription'].append(row)
        return row

    def add_grant(
        self,
        *,
        practice_id: int | None = None,
        pcn_id: int | None = None,
        user_id: int | None = None,
        plan_key: str = 'practice',
        days: int = 30,
        note: str = 'synthetic pilot',
        revoked: bool = False,
    ) -> dict:
        index = len(self.rows['billing_access_grant']) + 1
        row = {
            'id': index,
            'user_id': user_id,
            'practice_id': practice_id,
            'pcn_id': pcn_id,
            'plan_key': plan_key,
            'expires_at': timezone.now() + timedelta(days=days),
            'note': note,
            'granted_by_id': 1,
            'revoked_at': timezone.now() if revoked else None,
            'revoked_by_id': 1 if revoked else None,
            'created_at': timezone.now(),
            'updated_at': timezone.now(),
        }
        self.rows['billing_access_grant'].append(row)
        return row


class _Cursor:
    """The narrow cursor protocol the exporter relies on."""

    def __init__(self, source: SyntheticSource):
        self.source = source
        self._result: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, statement: str, params=None):
        text = ' '.join(statement.split())

        if text.startswith('CREATE TEMP TABLE'):
            if self.source.read_only:
                # What a genuinely read-only connection does. The exporter's
                # write probe requires this to raise.
                raise RuntimeError('cannot execute CREATE TABLE in a read-only transaction')
            self._result = []
            return

        if 'information_schema.columns' in text:
            self._result = [
                (table, column)
                for table, columns in self.source.schema.items()
                for column in sorted(columns)
            ]
            return

        for table in self.source.rows:
            if text.endswith(f'FROM {table} ORDER BY id'):
                columns = text[len('SELECT ') : text.index(' FROM ')].split(', ')
                self._result = [
                    tuple(row[column] for column in columns)
                    for row in sorted(self.source.rows[table], key=lambda r: r['id'])
                ]
                return

        raise RuntimeError(f'Synthetic source received an unexpected statement: {text[:80]}')

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


def organization_uuids(*, practices: list[int], pcns: list[int]) -> dict[tuple[str, str], str]:
    """Deterministic synthetic Identity organisation UUIDs.

    Derived from a fixed namespace so the same practice id yields the same UUID
    across runs, which is what makes the no-op and delta exercises meaningful.
    In a real migration these come from the Phase 3 Identity mapping table.
    """
    namespace = uuid.UUID('00000000-4a00-4a00-8a00-000000000000')
    mapping = {}
    for practice_id in practices:
        mapping[('practice', str(practice_id))] = str(
            uuid.uuid5(namespace, f'practice:{practice_id}')
        )
    for pcn_id in pcns:
        mapping[('pcn', str(pcn_id))] = str(uuid.uuid5(namespace, f'pcn:{pcn_id}'))
    return mapping
