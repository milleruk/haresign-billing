# The billing migration

A controlled contract comparable to the completed Identity migration, adapted to
money. **Phase 4A used synthetic data only; no live billing record has been
read.**

## The shape

Two processes that never meet.

```
  monolith                                    Haresign Billing
  ┌──────────────┐                            ┌──────────────┐
  │ billing_*    │ ── read-only ──▶ exporter  │              │
  └──────────────┘                     │      │  importer ◀──┼── artifact
                                       │      └──────────────┘
                                  encrypted artifact (AES-GCM, mode 600)
```

The exporter runs as an operator process with a read-only connection to the
monolith and **no route to the Billing database**. The importer runs inside
Billing with **no route to the monolith** — there is no source-database setting
in this service at all, and `legacy_migration/importer.py` imports neither the
exporter nor a database driver, which a test asserts from its import statements.
In the rehearsal stack the source sits on its own Docker network that the
application container is not attached to.

The encrypted artifact is the only thing that crosses.

## Source contract

`legacy_migration/schema.py`, transcribed from the monolith's billing models and
its two migrations, read-only.

Three tables: `billing_stripe_customer`, `billing_subscription`,
`billing_access_grant`.

**Schema validation is exact, in both directions.** A *missing* column means the
exporter would read a field that is not there. An *extra* column means the source
has grown something nobody has reviewed, and the right response to "the monolith
now stores a field we have never seen" is to stop and look at it, not to shrug
and export the columns we recognise.

**The allowlist is a strict subset of the contract.** A column may be present and
still not be ours to take. Not taken: anything from `auth_user` beyond an id;
`billing_stripe_customer.user_id` beyond its use as a lookup; anything not
explicitly listed.

**The read-only connection is proved, not assumed.** The exporter attempts a
write and requires it to fail. The failure this catches is an operator who
connected with the application's own credentials because the read-only role's
password had expired — at which point every safety property rests on the exporter
containing no INSERT, which nobody can verify by reading it.

## Refusals — where the shape change bites

The exporter refuses, counts, and never guesses:

| Refusal | Why |
|---|---|
| A subscription or grant with no practice and no PCN | No organisation to key to. Attributing it would assign somebody's money to an organisation they may not represent. See D-2. |
| A grant made to a *user* | Same, and it is the "person gets paid access without a subscription" shape the ownership contract forbids. |
| A status not in the contract's enumeration | Deciding at import time what an unknown money state means is exactly the guess this contract exists to prevent. |
| A plan key not in the contract | |
| An organisation the operator's Phase 3 mapping does not cover | The mapping comes from the completed Identity migration. Deriving it here would mean reading Identity's database from the monolith side. |

## Artifacts

AES-GCM, 32-byte key from a mode-600 file (a group-readable key file is refused),
with the format magic as associated data — so a tampered artifact fails to
*decrypt* rather than importing altered rows. Written through a dot-prefixed
temporary renamed only on success, at mode 600. An existing artifact is never
replaced.

**No plaintext ever reaches disk.** The payload is built in memory and encrypted
before a byte is written.

The manifest carries **aggregate counts only** — subscriptions by state, by plan,
organisation count, refusal count. Its checksum is a **keyed HMAC**, not a bare
hash: a plain digest over a small structured document of counts is guessable by
enumerating plausible combinations, which would let somebody confirm the customer
count from the manifest alone.

## Import

**Dry-run is mandatory.** `apply` refuses unless a *successful* dry-run for this
exact artifact digest is on record.

**A dry run and an apply do the same work.** Identical code path, identical
counts; the dry run rolls its transaction back at the end. A dry run that took a
different path would be testing something other than the apply, and a test
asserts the counts match.

**Conflicts abort the whole run**, apply and dry-run alike, with the transaction
rolled back. There is deliberately no skip-the-bad-rows mode: the rows that
conflict are precisely the ones somebody needs to look at, and half an
organisation's subscriptions is a state nobody can reason about.

Conflicts detected:

* `provider_identifier_collision` — two source rows claiming one Stripe
  subscription.
* `organization_uuid_collision` — a provider subscription that now names a
  different organisation. Re-pointing it would move a paid subscription between
  customers.
* `unmapped_existing_subscription` — the subscription exists here but was not
  imported by us. Adopting it would silently claim a row somebody else created.
* `member_link_self_reference`, `grant_without_expiry`, `unknown_plan`.

**Re-runs are no-ops.** Durable mappings with keyed source fingerprints mean an
unchanged row is recognised, touched for `last_seen_at`, and counted as
`unchanged`.

**Deltas are supported.** A changed fingerprint is an update; a new source id is a
create.

**A removed source record is flagged, never deleted, and never revokes
anything.** A subscription disappearing from the monolith is not the same fact as
a subscription being cancelled, and cancelling a paying customer on an ambiguous
signal is the worst available reading of it.

## Reconciliation

Aggregate counts: source subscriptions, mapped, state matches, state mismatches,
missing locally, source grants, mapped grants. An exact reconciliation is zero
mismatches and zero missing.

## Commands

```bash
# Generate a key. Outside git, mode 600, never beside the artifact.
python manage.py generate_migration_key /opt/docker/secrets/billing-migration.key

# Import. Dry run first — this is enforced, not advisory.
python manage.py import_monolith_billing artifact.hsbill \
    --key-file /opt/docker/secrets/billing-migration.key
python manage.py import_monolith_billing artifact.hsbill \
    --key-file /opt/docker/secrets/billing-migration.key --apply

# Reconcile an applied artifact. Read-only.
python manage.py reconcile_monolith_billing artifact.hsbill \
    --key-file /opt/docker/secrets/billing-migration.key
```

All output is aggregate counts. These commands are run by operators and their
output ends up in tickets and chat, which is no place for a customer list with
subscription state attached.

## Exercised in Phase 4A

Against synthetic monolith-shaped data, all passing: initial import; exact
reconciliation; unchanged no-op; plan and subscription delta; cancellation;
payment failure; duplicate provider event; out-of-order event; organisation UUID
collision; provider identifier collision; source record removal; unsupported
state; transaction rollback; artifact tampering; schema mismatch.
