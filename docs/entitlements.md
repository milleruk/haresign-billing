# Entitlements

What an organisation may use, how that is decided, and what nobody has decided
yet.

## The one rule

An entitlement is a **derived answer** about one organisation, one product and
one moment. It is computed by `billing/entitlements.py` and stored nowhere. There
is no `is_entitled` column, and `billing/tests/test_ownership.py` walks the model
registry to make sure one never appears — a stored answer would be a second
source of truth, and it would be the one that goes stale.

Three properties hold, and each has tests asserting the *absence* of the bad
outcome rather than the presence of the good one.

**Derived, never stored.** Everything below is a pure function of subscription
state, plan configuration, complimentary grants and the clock.

**Roles never grant entitlements.** Being an Identity organisation administrator
lets you *manage* billing. It gives you nothing paid. A platform administrator
gets separately-audited support access and, again, nothing paid.

**Fail closed.** Every path that cannot establish entitlement answers "not
entitled": an unknown provider state, a plan with no products, a subscription
whose period lapsed while the provider went quiet, a missing billing account, a
Billing outage, an expired cache. A paid feature that opens when the billing
system is confused is not a paid feature.

## Lifecycle behaviour

Transcribed from the monolith, which granted access for `active` and `trialing`
only and additionally required that `current_period_end` had not passed.

| State | Entitled? | Why |
|---|---|---|
| No subscription | **No** | Nothing to grant. Every catalogue product answers an explicit `false`, so a consumer never has to interpret a missing key. |
| `trialing` | **Yes** | A trial is access. `trial_end` is surfaced separately so the UI can say when it ends. |
| `active` | **Yes** | Provided the period has not lapsed — see below. |
| `past_due` | **No** | The monolith grants nothing here and defines no grace period, so neither does this service. See **D-3**. |
| `unpaid` | **No** | |
| `paused` | **No** | |
| `canceled` | **No** | Access has already ended. |
| Cancelled at period end | **Yes, until the period ends** | The state is still `active` and the customer has paid for the rest of the period. `cancel_at_period_end` is deliberately *not* consulted by the entitlement check; what ends access is the period ending. |
| `incomplete` | **No** | Payment was never completed. |
| `incomplete_expired` | **No** | |
| `unknown` | **No** | Not a provider state. Recorded when a provider reports a status this service does not recognise, so an unfamiliar status fails closed and is visible in the UI rather than being coerced into something that grants access. |
| Refund or dispute | **No effect, today** | Neither changes a subscription's state at the provider, so neither changes entitlement here. See **D-5**. |

**A lapsed period always closes access**, whatever the state says. A row reading
`active` with `current_period_end` in the past means the provider stopped telling
us things, not that the customer has permanent access.

**No known period end does not close access.** A subscription whose provider has
never given us an end date passes the period check — that is the honest reading
of "no end date supplied", and it is the state a fresh trial is in.

## Provider events arriving twice or out of order

Handled in `providers/webhooks.py`, not here, but the entitlement consequence is
the point.

* **Twice** — the `WebhookEvent` unique constraint on the provider event id means
  the second delivery finds the row, increments a counter, and applies nothing.
  Entitlement does not move.
* **Out of order** — an event whose sequence is strictly below the stored one is
  recorded as `out_of_order` and discarded. Without this, a late `active` event
  arriving after a `canceled` one would silently re-open a cancelled
  organisation's paid tools.
* **Equal sequence** — a duplicate, not an ordering incident. It applies, finds
  every field identical, and reports no change.

## Complimentary grants

Carried over from the monolith's `AccessGrant`, and kept separate from
`Subscription` for the reason the monolith gives: "who is actually paying us"
must stay answerable from the subscription table alone.

A live grant (not revoked, not expired) entitles its plan's products exactly as a
subscription does. Expiry is required — an open-ended grant is indistinguishable
from a permission bug six months later.

## Member organisations

The monolith's rule is that a PCN subscription covers its member practices. It is
reproduced by `Plan.covers_member_organizations` plus
`billing.MemberOrganizationLink`, and it only flows **downwards**: a practice
paying does not entitle its PCN, and a parent holding a plan that does not cover
members entitles nobody but itself.

`MemberOrganizationLink` is a **cache of Identity's organisation graph and is not
authoritative**. See **D-4**.

## Consuming the entitlement API

`GET /api/v1/organizations/<uuid>/entitlements/`, authenticated with the internal
service credential described in `docs/identity-integration.md`.

The response carries the organisation UUID, every active product key with an
explicit `entitled` boolean and an `effective_until`, and version and freshness
metadata. It carries **no** payment detail, invoice, provider identifier, plan,
price, amount, contact or personal data, and `api/tests/` asserts each absence.

### Caching, expiry and unavailability — the consumer's contract

* `cache_max_age` in the body, and a `Cache-Control: private` header, state how
  long an answer may be held. It is `private` because a shared cache would serve
  one organisation's entitlements in answer to another's request.
* A cached answer that has passed `cache_max_age` **is not usable**. It must be
  refetched, and if it cannot be, the feature closes.
* **Billing unavailable means not entitled.** A 5xx, a timeout, a refused
  credential and an unparseable body are all "no". A consumer that falls back to
  "assume entitled" during an outage has made the paywall optional; a consumer
  that falls back to a stale cache has made it optional for the length of the
  outage.
* A 503 from this service is deliberately *not* an empty 200. An empty 200 would
  be indistinguishable from a genuine "this organisation holds nothing" and would
  therefore be cached.

---

## Unresolved decisions

**These are not for an implementer to choose.** Each needs an answer from the
repository owner, recorded here, before the live migration or the Stripe cutover.

### D-1 — What replaces the staff entitlement bypass?

The monolith grants **every** entitlement to any account with `is_staff` or
`is_superuser` (`billing/services.py: user_entitlements`). That is a role
creating a payment entitlement, which this service's ownership contract forbids,
so it is not reproduced.

Consequence: staff who today open any paid tool for QA will find them closed
after cutover.

Options: a complimentary grant on an internal organisation (already supported, no
code needed); an explicit, audited, time-limited "support impersonation" mode; or
accept the loss and QA against the rehearsal stack.

**Status: unanswered. Nothing is implemented in its place.**

### D-2 — What happens to user-scoped subscriptions and grants?

The monolith allows a `Subscription` with no practice and no PCN, and an
`AccessGrant` made to a *person*. Under an organisation-keyed model neither has
an organisation to belong to, and inventing one would attribute somebody's money
to an organisation they may not represent.

The exporter **refuses** these rows and counts the refusals. Nothing is guessed.

Needed before the live migration: the actual count in production (see gate G1),
and a decision per case — attribute to a named organisation, convert to a
complimentary grant, or let it lapse with the person told first.

**Status: unanswered. The migration will stop on them.**

### D-3 — Is there a grace period on payment failure?

The monolith grants nothing on `past_due` or `unpaid`, so a card that fails
closes the practice's paid tools at the moment Stripe moves the subscription.
Whether that is intended or simply never considered is a commercial question.

If a grace period is wanted, it needs a length, a starting point (the failure, or
the period end), and a decision about what the customer is told.

**Status: unanswered. No grace period is implemented.**

### D-4 — Where does the organisation graph come from?

Reproducing "a PCN subscription covers its member practices" needs to know which
organisations a PCN contains. Identity owns that graph and exposes no API for it.

`MemberOrganizationLink` currently holds edges populated **only by the migration
importer** from allowlisted source data, stamped with a source and an observation
time. Stale edges are still *used* — revoking a customer's access because a sync
is late would be worse than the staleness — but they are named in reconciliation.

Needed before cutover: either an Identity organisation-graph API (the clean
answer), or an explicit decision that Billing may hold this cache with a stated
refresh mechanism and staleness bound.

**Status: unanswered. Implemented as a documented cache, listed as gate G6.**

### D-5 — What should a refund or dispute do?

Neither changes a Stripe subscription's status, so today neither changes
entitlement. A disputed payment on an otherwise `active` subscription leaves the
practice with full access.

Needed: whether a dispute should suspend entitlement immediately, at some
threshold, or never.

**Status: unanswered. No refund or dispute handling is implemented.**

### D-6 — What is the permanent service-to-service credential?

The entitlement API uses a signed internal credential (`api/auth.py`) because
Identity advertises no client-credentials grant and inventing one would
pre-commit Phase 4B.

Needed before Intelligence connects: either Identity gains a client-credentials
grant and Billing becomes a resource server, or this mechanism is accepted as
permanent with a documented rotation schedule and an owner.

**Status: a documented stopgap, with a stated replacement path.**

### D-7 — What is sold, and at what price, after cutover?

`catalog/seed.py` transcribes the monolith's current plans and displayed amounts
(£10/£110 practice, £49/£490 PCN). No `PlanPrice` carries a provider price
reference, because those live in the monolith's environment and this repository
must not read them.

Needed before cutover: whether prices, plans and intervals change, and whether
existing subscriptions are migrated onto new prices or grandfathered.

**Status: unanswered. Every price is displayable and none is purchasable.**

### D-8 — Who is a billing contact, by default?

The monolith has no billing-contact concept; the Stripe customer is attached to
whichever person clicked buy. `BillingContact` here supports an Identity user
UUID or a bare finance address, and the migration sets none.

Needed: whether the migrating buyer becomes the primary contact automatically, or
whether each organisation nominates one.

**Status: unanswered. Contacts are empty after migration.**
</content>
