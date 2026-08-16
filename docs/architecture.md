# Architecture

## What this service is

Haresign Billing is the authority for **organisation subscriptions, plans,
product entitlements and payment-provider reconciliation**. It is one of four
services the Haresign platform is being split into, and its boundary is the
reason it exists rather than an implementation detail.

It is not live. `billing.haresign.net` is not served, no production OIDC client
exists, and no code path reaches Stripe.

## The estate, and who owns what

| Service | Owns |
|---|---|
| **Haresign Identity** (`identity.haresign.net`) | Users and permanent user UUIDs, organisations and permanent organisation UUIDs, memberships, roles, invitations, authentication and OIDC, platform-administrator status. |
| **Haresign Billing** (this) | Billing accounts keyed to an Identity organisation UUID, plans, prices and the product catalogue, subscription lifecycle, billing contacts, provider customer/subscription/price references, invoice references, derived entitlements, billing audit, verified idempotent webhook processing. |
| **Haresign Intelligence** (later) | Application data, practice datasets, data-processing agreements, tool-specific permissions and features. |
| **MCP** (last) | Personal MCP tokens, their rotation and revocation, MCP-specific permissions. |

Three consequences worth stating because each is a thing somebody will otherwise
try to add.

**Identity stores no billing.** No plan, no subscription state, no Stripe
identifier, no invoice, no payment detail. Its organisation page shows a
subscription *card* filled from this service's summary endpoint and persists none
of it.

**Subscription status is not in OIDC claims.** An ID token is a long-lived
snapshot handed to a relying party; putting payment state in one means every
consumer holds a stale copy and the entitlement API becomes advisory.

**Data-processing agreements stay with Intelligence.** They are about processing
practice data, not about money.

## Internal structure

```text
config/             settings, URLs, WSGI/ASGI, environment readers, secret entrypoint
catalog/            products, plans, prices — the sellable surface
billing/            billing accounts, subscriptions, contacts, entitlement engine
providers/          provider boundary, Stripe adapter, fake provider, webhook ledger
identity/           OIDC relying-party client, sessions, organisation authorization
api/                versioned internal entitlement API and its service credentials
audit/              append-only, privacy-safe billing audit events
legacy_migration/   monolith-shaped exporter/importer, mappings, reconciliation
web/                the Billing shell, design system, health and readiness
```

Dependency direction, and it is never reversed:

```
providers → billing → catalog
api       → billing
legacy_migration → billing, catalog
identity  → (no domain app)
audit     ← everything; imports none of their business logic
```

`identity` importing no domain app is what keeps authentication independent of
what is being authorised. `audit` importing nothing is what stops an audit write
from being able to change domain state.

## Where the boundaries are physical, not documentary

**No foreign key into Identity.** Every reference to a person or organisation in
`billing`, `catalog`, `providers` and `legacy_migration` is a bare `UUIDField`.
There is no join a later change could reach through, and no `CASCADE` that could
let a deletion in Identity remove money state here.
`billing/tests/test_ownership.py` walks the model registry and asserts it.

**No source database.** `settings.DATABASES` has exactly one entry and there is
no configuration naming the monolith. The importer's isolation from the source is
a property of the topology and the settings, not of the importer's restraint.

**No writable entitlement.** There is no column anywhere that stores whether an
organisation is entitled. The answer is computed.

**No Stripe reachability.** `PROVIDER_BACKEND` defaults to a deterministic fake;
the Stripe adapter raises unless a secret key is set; no environment sets one;
the production overlay declares no Stripe secret at all.

## Request paths

Three, and they are deliberately separate at the URL root so that middleware and
authentication concerns do not blur.

**Browser** — `/`, `/auth/*`, `/organizations/*`, `/admin/`. Session-backed,
CSRF-protected, OIDC-authenticated, strict CSP.

**Provider** — `/providers/webhook/`. No session, CSRF-exempt, signature-verified
before anything else happens. The only route a third party is expected to reach.

**Service** — `/api/v1/*`. No session, signed internal credential, no personal
data in or out.

Plus `/health/` and `/ready/`, which are for the orchestrator and are exempt from
the HTTPS redirect because container probes speak loopback HTTP.

## The provider boundary

`providers/base.py` defines normalized `ProviderSubscription` and `ProviderEvent`
types. Above the line, nothing knows what Stripe is; below it, `stripe_provider.py`
extracts an explicit allowlist of fields — a `dict(stripe_object)` would drag a
customer's address, payment method and tax ids into this process and eventually
into a log line.

The seam's real value is not future provider-switching. It is that the whole test
suite, every migration exercise and the entire isolated rehearsal run against a
deterministic in-process implementation, so a billing state machine can be
exercised exhaustively — replay, out-of-order, signature forgery, tampering —
without a single network call to anybody's payment API.

## Sessions and the membership snapshot

Billing holds no memberships. It holds `SessionMembership` rows: what Identity
said about this person *at this login*, keyed to the session, deleted when the
session ends, and stamped with a capture time. A claim older than
`IDENTITY_MEMBERSHIP_MAX_AGE` is not relied on and the request re-authorizes.

That is the difference between a snapshot and a copy, and it is why this is not a
quiet duplication of Identity's membership table.

## What is deliberately absent

* **Email.** Pinned to the dummy backend. Dunning and receipts are the provider's
  job and stay there.
* **Invoice rendering.** `InvoiceReference` holds a number, a status, a total and
  the provider's hosted URL. The PDF, the line detail and the customer address
  stay at the provider, where they are already stored correctly.
* **A preview deployment.** Identity has one; Billing will not. A preview of a
  billing service shows somebody a subscription state, and a plausible-but-wrong
  subscription state is worse than no page.
* **Card data, bank details, checkout secrets, client secrets and full provider
  payloads.** None is stored anywhere, including in the webhook ledger, which
  keeps a digest instead.
</content>
