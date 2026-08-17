# Stripe cutover

**Nothing in this document has been done.** Phase 4A built the foundation and
Phase 4B.1 built the rest of the application, both against a deterministic fake.
This is the list of gates that must be passed before Haresign Billing touches
money, in order, each requiring explicit owner authorisation.

Phase 4B.1 closed the *implementation* gaps — checkout and portal are written,
the organisation graph is real, payer and beneficiary are separated, the router
has its production labels. It deliberately closed **no gate**. No live database
was read, no Stripe API was called, no production OIDC client exists and the
router is still disabled.

## Where things stand

| | Now |
|---|---|
| `billing.haresign.net` | Not served. Traefik router disabled, no DNS record. |
| OIDC | Disabled. No production relying-party client exists at Identity. |
| Payment provider | The deterministic fake. No Stripe credential exists in any environment. |
| Checkout / portal | **Implemented and disabled.** `BILLING_CHECKOUT_ENABLED` is off everywhere, and the Stripe client cannot be constructed without a secret key no environment sets. |
| Organisation graph | Implemented. Identity serves `GET /organizations/graph/v1/`; Billing holds a versioned projection that expires. No credential is configured, so nothing is fetched. |
| Billing data | Synthetic only. No live billing record has been read. |
| Monolith `/billing/*` | Untouched, live, and still the system of record. |
| Legacy billing models | Untouched. Nothing removed. |

## Gates

### G1 — Aggregate counts from live billing *(blocking, and not yet done)*

Every count in the Phase 4A report is synthetic. Nobody knows how many live
subscriptions exist, how they are distributed across states, or — most
importantly — how many rows the migration would **refuse**.

`haresign-core/AGENTS.md` permits reading the monolith database only through the
controlled *identity* migration exception, which explicitly excludes
subscriptions. Reading billing tables, even for counts, is outside it.

**Needs:** explicit owner authorisation to run count-only, `GROUP BY`-only queries
over `billing_subscription`, `billing_access_grant` and
`billing_stripe_customer`, through a read-only role, with no row data extracted.

Until then, the following are unknown and cannot be planned around:

* subscriptions by state, and by plan;
* organisations with and without a subscription;
* **subscriptions with no practice and no PCN** — these are D-2 refusals;
* **access grants made to a user** — also D-2 refusals;
* live grants, and how many are expired;
* whether any live status falls outside the eight the contract enumerates.

### G2 — Decisions D-1 to D-8 answered

`docs/entitlements.md` carries eight commercial and lifecycle decisions the
monolith does not define. **D-1** (what replaces the staff bypass), **D-2** (what
happens to user-scoped rows) and **D-7** (what is sold, and at what price) are
blocking; the rest can be answered alongside.

### G3 — Identity organisation mapping supplied

The exporter needs `(kind, source_record_id) → Identity organisation UUID` from
the completed Phase 3 Identity migration's mapping table. It does not derive it,
because deriving it would mean reading Identity's database from the monolith side.

### G4 — A production OIDC client at Identity

Not created. The exact registration, for when it is:

| | |
|---|---|
| Issuer | `https://identity.haresign.net` |
| Flow | Authorization Code, PKCE **S256 required** |
| Client type | Confidential, `client_secret_basic` |
| Redirect URI | Exact, single: `https://billing.haresign.net/auth/callback/` |
| Post-logout redirect | Exact, single: `https://billing.haresign.net/` |
| Scopes | `openid profile email haresign:memberships` |
| ID token signing | RS256, verified against the discovery document's JWKS |
| Secret storage | Hashed in Identity; plaintext only in Billing's protected secret file |

**The claim shape was verified against Identity's source in Phase 4B.1, not
assumed**, and the Phase 4A expectations were wrong in three ways worth stating
so nobody re-derives them from the old rehearsal:

* the claim key is **`haresign:memberships`** — with a colon — not
  `haresign_memberships`;
* its value is an **object**, `{"version": 1, "memberships": [...]}`, not a bare
  list. An unrecognised `version` is refused outright;
* entries are keyed `organization_id`, `organization_type`, `role` and sometimes
  `organization_code`. **There is no organisation name**, and the role key is
  `organization.admin` — dotted — not `organization_admin`.

There is **no platform-administrator claim**, and Billing no longer looks for
one. Identity's own architecture notes say platform-administrator state is never
a claim, and Phase 4B removed the support bypass that depended on it.

Identity emits only *active* memberships of *active* organisations, so a pending,
rejected, revoked or suspended membership never reaches Billing and can never
become billing access. That is enforced at the source and Billing does not
re-derive it.

### G5 — DNS, TLS and the Traefik router

`billing.haresign.net` created, certificate issued, and
`BILLING_TRAEFIK_ENABLED=true`. This is the **last** infrastructure gate, not the
first: everything above should be provable with the service unrouted.

### G6 — The organisation-graph question answered (D-4) *(met in Phase 4B.1)*

Identity gained `GET /organizations/graph/v1/` and Billing holds a versioned
projection that expires. Stale means sponsored entitlements and new sponsored
purchases fail closed; direct entitlement is never affected. See
`docs/entitlements.md` D-4.

**What remains for 4B.2** is configuration, not design: generate the service
credential, set `ORGANIZATION_GRAPH_API_KEYS` at Identity and
`IDENTITY_GRAPH_URL` / `IDENTITY_GRAPH_KEY_ID` / `IDENTITY_GRAPH_SECRET` at
Billing, and schedule `manage.py refresh_organization_graph`. Until then no
projection is fetched, so **every** sponsored entitlement fails closed — which is
the correct state for a service holding no live data.

### G7 — The permanent service credential decided (D-6)

Before Intelligence connects. Either Identity gains a client-credentials grant
and Billing becomes a resource server, or the current mechanism is accepted with
a documented rotation schedule and a named owner.

### G8 — Stripe account preparation

Products and prices existing in Stripe, and their price ids written into
`PlanPrice.provider_price_id`. **Every price is currently blank**, so nothing is
purchasable and no webhook naming a price can resolve to a plan.

A webhook endpoint pointing at `https://billing.haresign.net/providers/webhook/`,
with its signing secret in `/opt/docker/secrets/`.

Subscription metadata: the monolith stamps `user_id`, `plan_key`, `practice_ods`
and `pcn_code`. Billing reads `haresign_organization_id`. Existing subscriptions
have no such key, so the resolver falls back to the customer id and then to a
local row — which works for migrated subscriptions and should still be verified
before, not after, cutover.

### G9 — The live billing migration

Only after G1–G3. Full run: export → dry-run → read reconciliation → apply →
reconcile. Conflicts stop it. A pre-import backup and a proven restore first.

### G10 — Enable Stripe

`PROVIDER_BACKEND=stripe`, `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` as
secret files, and the corresponding `secrets:` entries added to
`docker-compose.production.yml` — which currently declares none.

Verify `STRIPE_API_VERSION` matches what the Stripe dashboard's webhook endpoint
is configured to send. A mismatch changes the shape of every event body.

### G11 — Enable checkout

`BILLING_CHECKOUT_ENABLED=1`. Both methods are **implemented** as of Phase 4B.1
— `StripeProvider.create_checkout_session` and `create_portal_session` are
written against the pinned SDK and exercised in full against the fake. The gate
is now purely a decision, not outstanding work.

Before flipping it, confirm every `PlanPrice` that should be purchasable carries
a `provider_price_id` verified against live Stripe (G8). A price without one is
refused by name, which is legible but is not a state to launch in.

### G12 — Cut the monolith over

Written out in full as a reversible procedure below, because "move the webhook"
is one sentence and about six decisions.

---

## The reversible cutover procedure

Each step names what it changes, how to undo it, and what is lost if it is undone
late. **Every step needs its own confirmation**; passing one does not authorise
the next.

### C1 — Add the Billing webhook endpoint *without removing the monolith's*

Create a **second** Stripe webhook endpoint pointing at
`https://billing.haresign.net/providers/webhook/`. The monolith's existing
endpoint stays enabled and stays receiving.

Both systems now see every event. That is deliberate and it is safe here,
because Billing is not yet the system of record and its writes affect nothing
anyone can see. It is the only way to prove Billing handles real event traffic
without a window in which nobody does.

Verify `STRIPE_API_VERSION` matches what the new endpoint is configured to send.
A mismatch changes the shape of every event body.

**Undo:** disable the Billing endpoint. Nothing is lost — the monolith never
stopped.

### C2 — Reconcile the shadow events

Run Billing for at least one full billing cycle in shadow. Then compare, for the
period:

* events received by each endpoint, by type and count;
* subscription states each system holds, per subscription;
* every state Billing recorded as `unresolved` or `out_of_order`.

An unexplained difference **stops the cutover**. The commonest legitimate
difference is a subscription the migration refused; those should already be named
in the migration report, and one that is not is a finding.

**Undo:** nothing to undo. This step only reads.

### C3 — Enable Billing checkout and portal

`BILLING_CHECKOUT_ENABLED=1` (G11). From here a customer can start a purchase in
Billing.

**Undo:** set it back to `0`. Subscriptions already created stay at Stripe and
keep being reported to both endpoints; they are real and must not be cancelled to
tidy up.

### C4 — Stop the monolith creating new billing state

Prevent *new* billing mutations in the monolith — new checkout sessions, new
portal sessions, plan changes. Its billing pages keep rendering and its
entitlement reads keep working; it simply stops being a place where new
subscriptions begin.

This is the first genuinely awkward step to reverse, because between C4 and C6
the two systems disagree about who may start a purchase.

**Undo:** re-enable. Any subscription created in Billing meanwhile is real, and
the monolith will not know about it until the delta migration in C6.

### C5 — Redirect the monolith's billing routes

Redirect **only** `/billing/*` to `billing.haresign.net`. Everything else in the
monolith is untouched — the tools, the dashboards, the marketing site, the auth
host. The monolith's legacy billing models stay in place and stay readable.

**Undo:** remove the redirect. It is a routing change and reverses cleanly.

### C6 — Final delta migration

Export → dry-run → reconcile → apply, for everything that changed since G9.
Conflicts stop it.

### C7 — Retire the legacy webhook endpoint *(separate approval)*

Only after the new endpoint has run a full cycle with clean reconciliation.
**This needs its own approval and is not covered by any earlier one.**

Disable the monolith's endpoint; do not delete it. A disabled endpoint can be
re-enabled in one click, and Stripe keeps its delivery history. **No remote
Stripe object is deleted at any point in this phase.**

**Undo:** re-enable it. This is why it is disabled rather than deleted.

### The rollback shape, summarised

| Step | Reverses by | Cost of a late reversal |
|---|---|---|
| C1 | Disable the Billing endpoint | None |
| C2 | — (read-only) | None |
| C3 | `BILLING_CHECKOUT_ENABLED=0` | Subscriptions bought meanwhile are real |
| C4 | Re-enable monolith mutations | Two systems accepting purchases |
| C5 | Remove the redirect | None |
| C6 | Re-run | None |
| C7 | Re-enable the legacy endpoint | Events split across a gap; reconcile by hand |

The soak between C1 and C7 is the whole safety property. Shortening it converts a
reversible migration into an irreversible one.

## What must not happen before G10

* Any Stripe API call from this repository.
* Creating or changing Stripe customers, prices, products, subscriptions,
  checkout sessions, portal sessions, webhook endpoints or metadata.
* Importing a live billing record.
* Creating a production OIDC client.
* Exposing `billing.haresign.net`.
* Redirecting the monolith's billing routes.
* Removing the monolith's legacy billing models.

## Rollback

Until C1, rollback is: turn off the Traefik router. The monolith is still the
system of record and has not been changed.

The old version of this section said the point of no easy return was moving the
webhook. The C1–C7 procedure above removes that cliff: the endpoint is *added*
rather than moved, both run together through the soak, and the legacy one is
disabled — not deleted — only after a clean cycle and a separate approval. What
remains irreversible is time: a subscription somebody bought in Billing exists,
and no rollback makes it not exist.
