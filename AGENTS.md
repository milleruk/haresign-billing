# AGENTS.md — working agreement for haresign-billing

Read this before changing anything here.

## What this is

`haresign-billing` is the repository. **Haresign Billing** is the product. It is
the authority for organisation subscriptions, plans, product entitlements and
provider (Stripe) reconciliation, intended to be served at
`https://billing.haresign.net`.

It is **served and it does not take money.** Since Phase 4B.2 the host resolves,
the router is enabled, a production OIDC client exists at Identity and the live
billing migration has been applied. Nothing here processes a payment: the
provider is the deterministic fake, no Stripe credential exists in any
environment, checkout is off and no price is purchasable. The Stripe cutover is a
separate, explicitly authorised phase and has not happened.

## Structure

```text
config/             settings, URLs, WSGI/ASGI, environment readers, secret entrypoint
catalog/            products, plans, prices — the sellable surface
billing/            billing accounts, subscriptions, contacts, entitlement engine
providers/          provider boundary, Stripe adapter, fake provider, webhook ledger
identity/           OIDC relying-party client, sessions, organisation authorization
api/                versioned internal entitlement API and its service credentials
audit/              append-only, privacy-safe billing audit events
legacy_migration/   monolith-shaped billing exporter/importer, mappings, reconciliation
web/                the Billing shell: base template, design system, health/readiness
docs/               architecture, security, domain, integration, entitlements, migration,
                    deployment, Stripe cutover, threat model, recovery runbook
```

Dependency direction: `billing` → `catalog`. `providers` → `billing` → `catalog`.
`api` → `billing`. `legacy_migration` → `billing`, `catalog`, `providers`.
`identity` imports no domain app. `audit` is written to by all and imports none
of their business logic. Never reverse one of these.

## Commands

```bash
python manage.py check
python manage.py check --deploy
python manage.py makemigrations --check --dry-run
python manage.py test
ruff check .
ruff format --check .
```

Under Docker, prefix with `docker compose exec haresign_billing`. Migrations are
run deliberately, never from an entrypoint.

## Naming

* User-facing: **Haresign Billing**. A test asserts that no rendered page
  contains "haresign-billing"; if you break it, fix the copy, not the test.
* Internal: `haresign-billing` in the repository, image and compose service names.

## Absolute production boundary

`app.haresign.net` is **live production**, served by the Haresign monolith, and
it is where billing actually happens today. `identity.haresign.net` is the live
Haresign Identity preview.

Do not, from this repository:

* modify `milleruk/haresign.net`, `haresign-core` or any repository other than
  this one;
* change DNS, Cloudflare, Traefik or any deployed container;
* route, alias or expose `billing.haresign.net`;
* register, update or revoke a production OIDC client at Haresign Identity;
* connect to, read from or write to the monolith's or Identity's database except
  through the controlled billing-migration exception below;
* call the Stripe API at all — see the Stripe boundary below;
* redirect, alias or disable the monolith's existing `/billing/*` routes;
* remove or alter the monolith's legacy billing models;
* push commits or open pull requests unless asked.

`web/tests/test_boundary.py` enforces the configuration half of this. Comments
and `docs/` may name the legacy hosts, because explaining the boundary requires
naming them; values may not.

## The Stripe boundary

Until a named cutover phase is explicitly authorised by the repository owner,
**no code path in this repository may reach Stripe's API**, in any environment.

* `providers/stripe_provider.py` is written against the pinned official SDK and
  is unreachable unless `STRIPE_SECRET_KEY` is set, which no environment sets.
* `PROVIDER_BACKEND` defaults to the deterministic fake, and the production
  overlay does not change it.
* **The credential and the backend are separate on purpose.** Setting
  `STRIPE_SECRET_KEY` makes the adapter reachable for the audited read-only
  discovery commands; only `PROVIDER_BACKEND=stripe` puts webhooks and checkout
  on it. That separation is what lets the live catalogue be read with a
  restricted read key without the runtime moving onto Stripe — it is not a way
  to reach Stripe outside the exception, and both still require the phase's
  authorisation.
* **The fake does not verify webhooks on a deployed environment.** Its signing
  secret is a published constant, so verification is refused unless
  `FAKE_PROVIDER_WEBHOOKS_ENABLED` is set — by the test runner, or by the
  isolated rehearsal stack, and by nothing else.
* Tests use the fake provider only. A test that would perform network I/O to
  Stripe is a bug in the test, not a reason to add a network allowance.
* Never create, read or mutate live Stripe customers, prices, products,
  subscriptions, checkout sessions, portal sessions, webhook endpoints or
  metadata from here.

## Controlled billing-migration exception

The repository owner may explicitly authorise a named Billing migration phase.
That authorisation permits only a read-only export of the allowlisted monolith
billing records needed to populate Haresign Billing: subscription rows, plan
keys, provider customer/subscription/price identifiers, complimentary access
grants, and the organisation and user references needed to key them.

Phase 4A used **synthetic monolith-shaped data only**. No live billing record has
been read. Each future phase requires its own explicit owner authorisation before
production data is read or applied.

Every authorised run must:

* use a source connection that is technically read-only and never write to or
  modify the monolith;
* use an explicit table and field allowlist, refuse unknown source schemas and
  refuse unexpected relationships, and extract no field merely because it is
  present;
* keep the Billing runtime disconnected from the monolith — extraction and import
  are separate operator-controlled processes on separate networks, never a runtime
  cross-database dependency, and the importer must have no route to the source;
* encrypt artifacts immediately with migration-specific protected material, keep
  them outside git at mode `600`, and never place key material beside ciphertext;
* keep personal data, card data, bank details, provider secrets and full provider
  payloads out of terminal output, logs, git, manifests, audit metadata and
  reconciliation reports;
* complete a mandatory dry-run and privacy-safe reconciliation before live apply,
  with conflicts stopping the run rather than being resolved silently;
* take a pre-import Billing backup and prove recovery in a separate database;
* be re-runnable as a no-op and support deltas without duplicating state.

The exception never permits card numbers, bank details, checkout or client
secrets, full Stripe payloads, patient or clinical data, Identity passwords,
sessions or tokens, arbitrary production database copies, monolith modifications,
runtime cross-database dependencies, or any migration phase that has not received
explicit owner authorisation.

## Phase 4B exception — production billing migration and Stripe cutover

Authorised by the repository owner for the named phase **4B** only. It is an
addition to the controlled billing-migration exception above and to the Stripe
boundary, not a replacement for either: every requirement in both still applies,
and anything not listed below remains prohibited.

This exception is **bounded to five things**.

1. **Aggregate read-only discovery** against exactly three monolith tables —
   `billing_stripe_customer`, `billing_subscription`, `billing_access_grant`. No
   other table may be read for any reason. Discovery output is counts,
   distributions and refusal reasons; it never contains a personal detail or a
   provider identifier.
2. **Encrypted export through a dedicated restricted PostgreSQL role.** The role
   is `billing_migration_ro`, created with `LOGIN NOSUPERUSER NOCREATEDB
   NOCREATEROLE`, granted connect, schema usage and `SELECT` on those three
   tables and nothing else, and set to `NOLOGIN` immediately after extraction.
   The Billing runtime never holds this credential.
3. **A minimum Identity organisation-mapping export** — the smallest artifact
   that translates an explicit legacy practice or PCN reference into a permanent
   Identity organisation UUID. Billing verifies it, refuses missing or
   conflicting mappings, and never copies an Identity table.
4. **Read-only Stripe catalogue and subscription metadata retrieval**, through a
   single dedicated audited command: products, prices, currencies, recurrence
   intervals, subscription states, customer and subscription references,
   subscription items and price-to-plan mappings, and — added by the owner during
   4B.3, because a second webhook endpoint cannot be planned without seeing the
   first — **webhook endpoint destinations, status, API version and subscribed
   event types**. Never a webhook signing secret. Nothing else is retrieved.
5. **Stripe mutations only after the later explicit human-confirmed cutover
   gate.** Until that confirmation is given, in this phase, no code path may
   create, update, archive or delete any Stripe object, and no live checkout or
   portal session may be created.

The exception still prohibits, without exception:

* unrestricted or ad-hoc database access, in either direction, to any database
  this service does not own;
* raw payment details — card numbers, bank details, payment methods, full
  customer payloads;
* arbitrary Stripe API calls, including any call outside the read-only set in
  (4) before the cutover gate and any mutation after it that has not been named
  in the approved cutover plan;
* cross-database access at runtime — extraction and import remain separate
  operator-controlled processes and the Billing runtime keeps no route to either
  source;
* plaintext migration artifacts, at any point, anywhere, including a temporary
  one "just to check";
* deleting a remote Stripe object, which this phase never does;
* disabling or redirecting the monolith's billing routes before its own separate
  approval.

**Where the phase actually stands.**

*4B.1* was the offline half and took none of this authorisation: everything in it
was exercised against synthetic data and the deterministic fake.

*4B.2* used items 1, 2 and 3. The monolith was read through the restricted
`billing_migration_ro` role, which was set `NOLOGIN` afterwards; the live billing
migration was applied — four organisations, four complimentary grants, **zero
subscriptions**, zero conflicts; a production OIDC client was created; DNS, TLS
and the Traefik router were enabled; and an encrypted backup was proven by
restoring it into a separate PostgreSQL 16.

*4B.3* built the Stripe tooling and **took neither item 4 nor item 5**. No Stripe
API has been reached, in any mode, from any environment. `PROVIDER_BACKEND` is
`fake`, no `STRIPE_SECRET_KEY` or `STRIPE_WEBHOOK_SECRET` exists anywhere,
`docker-compose.production.yml` still declares no Stripe secret, no webhook
endpoint has been created at Stripe, checkout is off and every
`PlanPrice.provider_price_id` is still blank.

The three commands that will use item 4 — `stripe_discovery`,
`map_provider_prices`, `cutover_reconciliation` — exist, are tested against the
fake, assert the Stripe mode they were told to expect, and have never been run
against Stripe. **Item 5 remains untouched and still needs its own explicit
human confirmation.**

## Ownership contract

These boundaries are the reason this service exists. Enforced by
`billing/tests/test_ownership.py`.

**Identity owns** users and permanent user UUIDs, organisations and permanent
organisation UUIDs, memberships and roles, invitations, authentication and OIDC,
and platform-administrator status.

**Billing owns** billing accounts keyed to an Identity organisation UUID, plans,
prices and product catalogue references, the subscription lifecycle, billing
contacts, provider customer/subscription/price references, invoice references,
derived product entitlements, billing audit events, and verified idempotent
webhook processing.

**Intelligence owns** application data, practice datasets, data-processing
agreements, and tool-specific permissions and features.

**MCP owns** personal MCP tokens, their rotation and revocation, and MCP-specific
permissions.

Billing must **not** duplicate Identity passwords, memberships, roles or user
accounts. It retains only the minimum Identity UUID references needed for
authorization and billing contacts. It must never store a plan, a subscription
state, a Stripe identifier or a payment detail *in Identity*, and Identity must
never persist Billing's authoritative state.

## Security constraints

These are decisions, not preferences. Changing one is a deliberate act with a
reason recorded in the commit message.

* **The organisation UUID is the key.** `BillingAccount.organization_id` is an
  Identity organisation UUID, unique, and never reissued or renumbered.
* **An organisation UUID from the browser is never trusted.** Every
  organisation-scoped view resolves authorization from the authenticated
  Identity session's memberships, not from the URL.
* **Paying never creates a role, and a role never creates an entitlement.** An
  Identity organisation administrator with no subscription holds no paid
  entitlement. A platform administrator's support access is separate, explicit
  and audited.
* **Entitlements are derived, never stored as truth.** There is no writable
  "is_entitled" column. `billing/entitlements.py` is the single derivation, and
  it is pure.
* **Paid features fail closed.** When entitlement cannot be established — Billing
  unreachable, credential rejected, cache expired — the answer is "not entitled".
* **Webhooks are verified and idempotent.** Signature first, then a unique
  provider event id in `providers.WebhookEvent`, then a transactional apply. An
  event older than the state it would overwrite is recorded and ignored.
* **No raw provider payloads.** The event ledger stores type, id, timestamps,
  the extracted allowlisted fields and a digest — never the full body, never a
  secret, never a card or bank detail.
* **Audit events are append-only** in the model, not merely in admin, and their
  metadata is scrubbed of anything credential-shaped or monetary-personal.
* **Host-only cookies.** `SESSION_COOKIE_DOMAIN` is never set.
* **The CSP is strict and enforced**, with no `'unsafe-inline'` or
  `'unsafe-eval'` on any route including `/admin/`. No inline `<style>`,
  `<script>` or `style` attribute in any template — tests fail on all three.
* **No third-party runtime requests.** No CDN, fonts, analytics or tag managers.
  Assets are self-hosted. The only outbound host this service will ever have is
  Stripe's API, and only after cutover.
* **Every page is `noindex`.** Nothing here should be in a search index.
* **No secrets in the repository**, in logs, or in audit metadata. No production
  fallback value for any secret; the application refuses to start instead.

## Adding to the catalogue

* Products carry a **stable internal product key**. Once shipped, a key never
  changes — it is what Intelligence asks for over the entitlement API.
* Plans are seeded by an idempotent data migration keyed on the stable plan
  `key`. Editing `catalog/seed.py` alone changes nothing already deployed; write
  a new migration.
* Provider price references live in `PlanPrice` rows, never hard-coded and never
  in code constants.

## What "done" means

A change is finished when all of the following are true:

1. `python manage.py check` is clean.
2. `python manage.py check --deploy` is clean under production flags.
3. `python manage.py makemigrations --check --dry-run` reports no changes.
4. `python manage.py test` passes.
5. `ruff check .` and `ruff format --check .` pass.
6. New behaviour has a test that would fail without it — for security behaviour,
   a test that asserts the *absence* of the bad outcome (no cross-organisation
   read, no entitlement without a subscription, no unsigned webhook accepted).
7. Documentation that has gone stale is updated in the same change.
8. No secret, no production hostname in a value, no Stripe call, and no new
   external network dependency.
9. Runtime changes are exercised against the isolated stack
   (`docker-compose.test.yml`, project `haresign_billing_phase4a`) — never
   against a deployed one — and the temporary project is torn down afterwards
   with its own `-p` name and `--volumes`.

## Unresolved decisions

`docs/entitlements.md` carries a numbered list of commercial and lifecycle
decisions the monolith does not define. **Do not silently choose one.** If a
change needs an answer, get it from the repository owner and record it there.
