# Stripe cutover

**Nothing in this document has been done.** Phase 4A built the implementation and
proved it against a deterministic fake. This is the list of gates that must be
passed before Haresign Billing touches money, in order, each requiring explicit
owner authorisation.

## Where things stand

| | Now |
|---|---|
| `billing.haresign.net` | Not served. Traefik router disabled, no DNS record. |
| OIDC | Disabled. No production relying-party client exists at Identity. |
| Payment provider | The deterministic fake. No Stripe credential exists in any environment. |
| Checkout / portal | Disabled. The Stripe adapter refuses both outright. |
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

Authorization Code, PKCE S256 required, exact redirect URI
`https://billing.haresign.net/auth/callback/`, scopes `openid profile email
haresign:memberships`, `client_secret_basic`.

Identity must also expose the platform-administrator and membership claims this
service reads (`haresign_platform_admin`, `haresign_memberships`) — verify against
Identity's actual claim shapes before enabling, because the rehearsal used a
synthetic provider that emits them by construction.

### G5 — DNS, TLS and the Traefik router

`billing.haresign.net` created, certificate issued, and
`BILLING_TRAEFIK_ENABLED=true`. This is the **last** infrastructure gate, not the
first: everything above should be provable with the service unrouted.

### G6 — The organisation-graph question answered (D-4)

`MemberOrganizationLink` is a cache with no refresh mechanism. Either Identity
gains an organisation-graph API, or the cache is explicitly accepted with a
stated refresh and staleness bound. Without one, "a PCN subscription covers its
member practices" drifts silently.

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

`BILLING_CHECKOUT_ENABLED=1`, **and implement it**:
`StripeProvider.create_checkout_session` and `create_portal_session` currently
raise, deliberately.

### G12 — Cut the monolith over

The last step, and reversible only in one direction:

1. Point the monolith's entitlement gates at Billing's entitlement API.
2. Redirect the monolith's `/billing/*` routes to `billing.haresign.net`.
3. Move the Stripe webhook endpoint from the monolith to Billing — **not both**,
   or two systems will apply the same events with different rules.
4. Run a final delta migration.
5. Leave the monolith's billing models in place, read-only, until the new system
   has run a full billing cycle.

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

Until G12, rollback is: turn off the Traefik router. The monolith is still the
system of record and has not been changed.

After G12 step 3, rollback means moving the webhook endpoint back and re-pointing
the routes — and any subscription change Stripe reported in between must be
reconciled by hand, because each system will have half the events. That step is
the point of no easy return, and it should be taken on a low-traffic day with
somebody watching.
