# Stripe cutover

Phase 4A built the foundation, 4B.1 built the rest of the application against a
deterministic fake, 4B.2 stood the service up — DNS, TLS, a production OIDC
client, the live billing migration and a proven encrypted backup — and 4B.3 built
the tooling that a Stripe cutover needs and stopped at the credential.

**Haresign Billing still touches no money.** `PROVIDER_BACKEND` is `fake`, no
Stripe credential exists in any environment, checkout is disabled, no Stripe
webhook endpoint has been created, and the monolith is still the system of record
for billing.

## Where things stand

| | Now |
|---|---|
| `billing.haresign.net` | **Served.** Traefik router enabled, DNS resolving, certificate issued. |
| OIDC | **Live.** A production relying-party client exists at Identity and sign-in works. |
| Payment provider | The deterministic fake. **No Stripe credential exists in any environment.** |
| Checkout / portal | Implemented and disabled. `BILLING_CHECKOUT_ENABLED=0`; the Stripe client cannot be constructed without a secret key nothing sets. |
| Organisation graph | Live projection held from Identity. **Stale** — see "The projection that cannot become fresh" below. |
| Billing data | **Live, migrated.** Four organisations, four complimentary grants, and — the number that shapes this whole phase — **zero subscriptions**. |
| Catalogue | Two plans, four prices, **no provider price reference on any of them**. Nothing is purchasable. |
| Stripe webhook endpoint | **Not created.** Nothing at Stripe points here. |
| Monolith `/billing/*` | Untouched, live, and still the system of record. |
| Legacy billing models | Untouched. Nothing removed. |

## The number that shapes the phase

The live migration read three monolith tables through the restricted role and
found **no subscriptions at all** — four organisations, four complimentary access
grants, three provider customer references, and zero subscription rows, with zero
refusals.

That changes the risk shape of the cutover completely, and it should be said
plainly rather than discovered halfway through:

* **No customer is currently paying through the monolith's Stripe integration**,
  so there is no population of live subscriptions to migrate, reconcile or lose.
* The C1–C7 shadow-soak procedure below was written for an estate with a running
  subscription base. With none, "run for a full billing cycle in shadow" has
  nothing to observe, and the soak protects nothing.
* What remains genuinely at risk is the **four complimentary grants** — the only
  live entitlement this service holds — and they do not depend on Stripe at all.

The procedure is left as written because it is the procedure that applies if any
subscription exists at cutover time, and because the current count is a fact
about today that must be re-established immediately before cutover, not assumed
from this document.

## Gates

### G1 — Aggregate counts from live billing *(met in Phase 4B.2)*

Run under the Phase 4B exception, through the restricted `billing_migration_ro`
role, against exactly three tables. The result is in "The number that shapes the
phase" above: four organisations, four grants, three provider customer
references, **zero subscriptions**, zero refusals.

The role was set `NOLOGIN` immediately afterwards. The counts are a fact about
the day they were read and are re-established, not assumed, before cutover.

### G2 — Decisions D-1 to D-8 answered

`docs/entitlements.md` carries eight commercial and lifecycle decisions the
monolith does not define. **D-1** (what replaces the staff bypass), **D-2** (what
happens to user-scoped rows) and **D-7** (what is sold, and at what price) are
blocking; the rest can be answered alongside.

### G3 — Identity organisation mapping supplied *(met in Phase 4B.2)*

Supplied as an encrypted minimum mapping artifact and verified by the importer.
All four organisations resolved; nothing was refused for a missing or conflicting
mapping.

### G4 — A production OIDC client at Identity *(met in Phase 4B.2)*

Created and in use. The registration, as it stands:

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

### G5 — DNS, TLS and the Traefik router *(met in Phase 4B.2)*

`billing.haresign.net` resolves, the certificate is issued and
`BILLING_TRAEFIK_ENABLED=true`. The service is served.

It being served is what makes the webhook endpoint publicly reachable, which is
why Phase 4B.3 closed the fake provider's verification path — see "The fake
provider on a routed deployment" below.

### G6 — The organisation-graph question answered (D-4) *(met in Phase 4B.1)*

Identity gained `GET /organizations/graph/v1/` and Billing holds a versioned
projection that expires. Stale means sponsored entitlements and new sponsored
purchases fail closed; direct entitlement is never affected. See
`docs/entitlements.md` D-4.

The configuration was completed in Phase 4B.2: the service credential exists at
both ends and a projection of 7,509 organisations and 6,149 relationships is
held. Phase 4B.3 added the schedule that was still missing.

The projection is nevertheless **stale**, and cannot become fresh on its own —
see "The projection that cannot become fresh" below. No entitlement depends on it
today (there are no sponsored allocations at all), so this is a correctness
problem waiting for its first sponsored purchase rather than a live fault.

### G7 — The permanent service credential decided (D-6)

Before Intelligence connects. Either Identity gains a client-credentials grant
and Billing becomes a resource server, or the current mechanism is accepted with
a documented rotation schedule and a named owner.

### G8 — Stripe account preparation *(blocked on a credential; tooling built in 4B.3)*

**Nothing at Stripe has been read or created.** No credential exists in any
environment, so this gate has not been opened — but everything needed to open it
in one sitting now exists and is tested.

What is still true and blocking:

* every `PlanPrice.provider_price_id` is **blank**, so nothing is purchasable and
  no webhook naming a price can resolve to a plan;
* no webhook endpoint at Stripe points here;
* nobody has confirmed which Stripe account, or which mode, is the production one.

What Phase 4B.3 added, all of it exercised against the deterministic fake and
none of it ever run against Stripe:

| Command | Does | Writes |
|---|---|---|
| `manage.py stripe_discovery --expect-mode {live,test}` | The single audited read-only retrieval permitted by the Phase 4B exception: products, prices, currencies, recurrence, aggregate customer and subscription counts by status. `--show-catalogue` adds price and product ids, which is what a mapping is written from. | Nothing. |
| `manage.py map_provider_prices --expect-mode … --map plan:interval=price_id` | Verifies each stated mapping against Stripe — exists, active, product active, recurring, interval, interval count, currency, amount, mode — and writes them all or writes none. | `PlanPrice.provider_price_id`, only with `--apply`. |
| `manage.py cutover_reconciliation` | Provider against Billing against Identity, in counts. Exits non-zero on any conflict. | Nothing, ever — there is no apply flag. |

Three properties of that tooling are the point of it:

**A credential does not move the runtime onto Stripe.** `STRIPE_SECRET_KEY` being
set is what makes the adapter reachable for discovery; `PROVIDER_BACKEND` is what
puts webhooks and checkout on it. They are deliberately separate, so the live
catalogue can be read with a **restricted read-only key** while every runtime path
stays on the fake.

**The mode is asserted on every command.** The credential's prefix and the
`livemode` flag on every object returned must both agree with the mode the
operator stated. A test key read while expecting live is a refusal, not a report.

**Mapping is by id and never by name.** A product renamed in the Stripe dashboard
cannot re-point a plan, and a price whose amount disagrees with the catalogue is
refused rather than reconciled — one of the two is wrong and this code does not
get to decide which.

Subscription metadata: the monolith stamps `user_id`, `plan_key`, `practice_ods`
and `pcn_code`. Billing reads `haresign_organization_id`. Existing subscriptions
have no such key, so the resolver falls back to the customer id and then to a
local row. With zero live subscriptions this fallback currently covers nothing,
and it must still be verified before, not after, cutover.

### G9 — The live billing migration *(met in Phase 4B.2)*

Run in full: export → dry-run → reconcile → apply → reconcile, with a pre-import
backup and a restore proven in a separate PostgreSQL 16. Four organisations and
four grants applied, zero subscriptions, zero conflicts. Re-running the same
artifact is a no-op, which is what the second dry-run recorded.

### G10 — Enable Stripe *(not opened)*

`PROVIDER_BACKEND=stripe`, `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` as
secret files, and the corresponding entries in `docker-compose.production.yml` —
**which still declares none, deliberately.** `web/tests/test_boundary.py` asserts
their absence, so opening this gate means changing a test that exists to make the
change deliberate. That is the gate.

The exact diff, written out so it is reviewed rather than improvised:

```yaml
# services.haresign_billing.environment
  PROVIDER_BACKEND: stripe
  STRIPE_SECRET_KEY: null
  STRIPE_SECRET_KEY_FILE: /run/secrets/stripe_secret_key
  STRIPE_WEBHOOK_SECRET: null
  STRIPE_WEBHOOK_SECRET_FILE: /run/secrets/stripe_webhook_secret

# services.haresign_billing.secrets
  - stripe_secret_key
  - stripe_webhook_secret

# top-level secrets
  stripe_secret_key:
    file: /opt/docker/secrets/haresign-billing/stripe-secret-key.txt
  stripe_webhook_secret:
    file: /opt/docker/secrets/haresign-billing/stripe-webhook-secret.txt
```

Both files mode `600`, owned by root, and never in git.

**Order matters, and the order is not the obvious one.** Move
`PROVIDER_BACKEND` to `stripe` **before** mapping any price into the production
catalogue, not after. While the backend is `fake`, the webhook endpoint verifies
against a signing secret published in this repository; a mapped price plus that
endpoint is the only combination in which a forged event could resolve to a plan.
4B.3 closed that path (see below), and the ordering rule stays as a second line.

Verify `STRIPE_API_VERSION` matches what the Stripe dashboard's webhook endpoint
is configured to send. A mismatch changes the shape of every event body.

### G11 — Enable checkout

`BILLING_CHECKOUT_ENABLED=1`. Both methods are **implemented** as of Phase 4B.1
— `StripeProvider.create_checkout_session` and `create_portal_session` are
written against the pinned SDK and exercised in full against the fake. The gate
is now purely a decision, not outstanding work.

Before flipping it, confirm every `PlanPrice` that should be purchasable carries
a `provider_price_id` verified against live Stripe (G8) — which now means
`map_provider_prices --expect-mode live --apply` having succeeded, since that
command refuses everything it cannot verify. A price without a reference is
refused by name, which is legible but is not a state to launch in.

**D-7 is still unanswered.** The catalogue carries £10 and £110 for a practice and
£49 and £490 for a PCN because those numbers had to be something; nobody has
confirmed they are what is sold. Mapping a price whose amount disagrees with the
catalogue is a refusal, so this must be settled before G8, not after.

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

## Two findings from Phase 4B.3

### The fake provider on a routed deployment

`billing.haresign.net` has been served since Phase 4B.2, which makes
`/providers/webhook/` reachable from the internet. The deployed service runs
`PROVIDER_BACKEND=fake`, and the fake signs with `whsec_fake_provider_...`, a
constant written in plain sight in `providers/fake.py` — deliberately, so that
the endpoint's real HMAC path is exercised by tests rather than stubbed out.

Those two facts together meant anyone who had read this repository could present
a **validly-signed** event to the live endpoint.

The blast radius was bounded — no `PlanPrice` carries a provider price id and no
billing account carries a provider customer id, so every crafted event resolved
to `unresolved` and applied nothing — but the bound was a coincidence of the
current data, and mapping the first price would have removed half of it.

Phase 4B.3 closed it: the fake refuses to verify a webhook at all unless
`FAKE_PROVIDER_WEBHOOKS_ENABLED` is set, which the test runner sets for itself
and the isolated rehearsal stack sets explicitly. **No deployed environment sets
it**, and `web/tests/test_boundary.py` asserts the production overlay does not.
The deployed webhook endpoint now refuses every delivery until a real Stripe
credential exists, which is the correct state for a service that is not taking
money.

### The projection that cannot become fresh

Billing holds Identity's organisation graph and treats a projection older than
`IDENTITY_GRAPH_MAX_AGE` (one hour) as stale, failing sponsored entitlements
closed. Freshness is measured from the document's `generated_at`.

When the estate has not changed, Identity answers a refresh with "your version is
still current" and Billing — correctly, by its own written reasoning — declines to
re-stamp the age, because a 304 means the content is unchanged and not that the
document is younger. The consequence is that **an estate that stops changing
becomes permanently stale**: the held projection ages past an hour and no
successful refresh can ever make it fresh again.

Nothing depends on it today. There are no `EntitlementAllocation` rows at all, so
no sponsored entitlement exists to fail closed, and direct entitlement never
consults the graph. The four live complimentary grants are unaffected.

It is left as a **decision for the repository owner**, recorded here rather than
fixed unilaterally, because the two available fixes are both changes to a stated
decision and one of them is in another repository:

* treat a verified "still current" answer as confirmation and measure freshness
  from the confirmation rather than the generation — a change to `identity/graph.py`
  and to the reasoning written in it; or
* have Identity stamp `generated_at` at build time on every response — a change to
  `haresign-core`, which this repository may not make.

The first sponsored purchase is the deadline, not the next phase.

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

Until C1, rollback is: redeploy the previous image tag, and if necessary turn off
the Traefik router. The monolith is still the system of record and has not been
changed. Every Billing deployment is pinned to an immutable source-commit tag in
`BILLING_IMAGE_TAG`, so the rollback position is always the tag that was
deployed before — recorded in the phase report, not inferred later.

The old version of this section said the point of no easy return was moving the
webhook. The C1–C7 procedure above removes that cliff: the endpoint is *added*
rather than moved, both run together through the soak, and the legacy one is
disabled — not deleted — only after a clean cycle and a separate approval. What
remains irreversible is time: a subscription somebody bought in Billing exists,
and no rollback makes it not exist.
