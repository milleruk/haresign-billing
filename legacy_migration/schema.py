"""The exact source contract, and the allowlist.

This module is the whole reason the migration is safe to authorise. It names
every table and every column the exporter may read, and the exporter refuses to
run against a source whose schema does not match — not "contains at least these
columns", but *exactly* these, so a monolith that has grown a
`billing_subscription.card_last4` since this was written stops the run rather
than quietly carrying it across.

Transcribed from the monolith's `modules/core/billing/models.py` and its two
migrations, read-only. Three tables exist there and all three are covered:
`billing_stripe_customer`, `billing_subscription`, `billing_access_grant`.

**What is deliberately not allowlisted**, though it exists in the source:

* `billing_stripe_customer.user_id` beyond its use as a join key — the customer
  row maps a *person* to a Stripe customer, and Billing keys customers to
  organisations. The mapping is used to resolve a subscription's customer id and
  is then discarded.
* Anything from `auth_user` other than the id needed to resolve a billing
  contact. No names, no addresses, no password hashes. Identity owns people, and
  the Identity migration has already moved them.
* `billing_access_grant.note` is carried as `reason`; it is operator free text
  and is length-capped and scrubbed on import.
"""

from __future__ import annotations

# Bumped whenever the shape of an artifact changes. An importer refuses an
# artifact whose version it was not written for, rather than best-effort parsing
# a format it does not understand.
EXPORT_SCHEMA_VERSION = 1
EXPORTER_VERSION = '4a.1'
SOURCE_SYSTEM = 'haresign-monolith-billing'

# Exact expected columns, per table. `information_schema` is compared against
# these as a set; any difference in either direction stops the run.
SOURCE_TABLES = {
    'billing_stripe_customer': {
        'id',
        'user_id',
        'stripe_customer_id',
        'created_at',
    },
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

# Columns actually extracted. A strict subset of the above: a column may be
# present in the source and still not be ours to take.
ALLOWLISTED_COLUMNS = {
    'billing_stripe_customer': ['id', 'user_id', 'stripe_customer_id'],
    'billing_subscription': [
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
    ],
    'billing_access_grant': [
        'id',
        'user_id',
        'practice_id',
        'pcn_id',
        'plan_key',
        'expires_at',
        'note',
        'revoked_at',
        'created_at',
    ],
}

# The monolith's subscription statuses, and what each becomes. Deliberately a
# complete enumeration with no default: a status the source holds that is not
# listed here stops the run, because deciding at import time what an unknown
# money state means is exactly the guess this contract exists to prevent.
SOURCE_STATUS_MAP = {
    'active': 'active',
    'trialing': 'trialing',
    'past_due': 'past_due',
    'canceled': 'canceled',
    'incomplete': 'incomplete',
    'incomplete_expired': 'incomplete_expired',
    'unpaid': 'unpaid',
    'paused': 'paused',
}

# The monolith's plan keys. Must exist in the Billing catalogue on import.
SOURCE_PLAN_KEYS = {'practice', 'pcn'}


class SchemaMismatch(RuntimeError):
    """The source is not the schema this exporter was written against."""


def validate_source_schema(observed: dict[str, set[str]]) -> None:
    """Compare an observed schema against the contract. Raises on any difference.

    Both directions matter. A **missing** column means the exporter would read a
    field that is not there; an **extra** column means the source has grown
    something nobody has reviewed, and the correct response to "the monolith now
    stores a field we have never seen" is to stop and look at it, not to shrug and
    export the columns we recognise.
    """
    missing_tables = sorted(set(SOURCE_TABLES) - set(observed))
    if missing_tables:
        raise SchemaMismatch(f'Source is missing tables: {", ".join(missing_tables)}')

    problems = []
    for table, expected in SOURCE_TABLES.items():
        found = observed.get(table, set())
        if extra := sorted(found - expected):
            problems.append(f'{table}: unexpected columns {", ".join(extra)}')
        if absent := sorted(expected - found):
            problems.append(f'{table}: missing columns {", ".join(absent)}')

    if problems:
        raise SchemaMismatch(
            'The source billing schema does not match the migration contract. '
            + '; '.join(problems)
        )
