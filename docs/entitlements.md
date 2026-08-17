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
gets nothing at all — not entitlement, and since Phase 4B not billing access
either.

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

## Payer and beneficiary

Who pays and who is covered are separate, recorded facts.

`BillingAccount` is the **paying** organisation; a subscription belongs to one.
`EntitlementAllocation` names the **beneficiary**. A practice purchase produces
one allocation whose beneficiary is the practice itself. A PCN purchase produces
one per organisation the PCN chose — itself, some of its member practices, or
both.

Effective entitlement is the **deterministic union** of valid direct and
sponsored allocations, plus complimentary grants. Direct means payer and
beneficiary are the same organisation. Sponsored means they are not, and it is
honoured only while `Plan.covers_member_organizations` permits it **and** the
organisation-graph projection is fresh **and** currently reports the
relationship. All three, every time: an allocation records a decision that was
valid when it was made, not a standing permission.

Coverage flows **downwards only**. A practice paying does not entitle its PCN.

### What a sponsored practice may see

The access, and none of the money. Its billing page says a product is available
through its PCN and names the PCN. It shows no invoice, no subscription
reference, no renewal date, no payment method and no cancel route — the
subscription is not theirs, and offering an action that must fail reads as a
broken product. A PCN administrator, conversely, sees the PCN's own subscriptions
and allocations and **not** a member practice's independently purchased ones.

### When a relationship is removed

The allocation becomes `INELIGIBLE`, the inherited entitlement stops, and
**nothing at the provider is touched** — not cancelled, not refunded, not
modified. An `OperationalAlert` is raised for a person to decide what happens to
the money. That decision belongs to a human: cancelling would take away something
the PCN is still paying for and may still want, refunding would be a commercial
decision made by a graph sync, and doing nothing silently would leave a PCN
paying for a practice that left. The audit row records `provider_action: none`
explicitly, because the absence of the call is the property being asserted.

### Preventing duplicate coverage

Checkout refuses a purchase for a beneficiary that already holds **every**
product the plan grants, through any source. Two subscriptions covering one
practice for one product is a customer paying twice, and a PCN and a practice
buying the same tool in the same week is the commonest way it happens. A plan
that would add even one product not already held is allowed through — that is an
upgrade, not a duplicate.

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

**Status: answered in Phase 4B — do not reproduce it.** Nothing replaces it.
Phase 4B went further and removed the platform-administrator *support bypass*
as well, so `is_staff`, `is_superuser` and platform administration now grant
neither entitlement nor billing access. Staff who need a paid tool for QA get a
complimentary grant on an internal organisation, which needs no code.

### D-2 — What happens to user-scoped subscriptions and grants?

The monolith allows a `Subscription` with no practice and no PCN, and an
`AccessGrant` made to a *person*. Under an organisation-keyed model neither has
an organisation to belong to, and inventing one would attribute somebody's money
to an organisation they may not represent.

The exporter **refuses** these rows and counts the refusals. Nothing is guessed.

Needed before the live migration: the actual count in production (see gate G1),
and a decision per case — attribute to a named organisation, convert to a
complimentary grant, or let it lapse with the person told first.

**Status: answered in Phase 4B — never guess.** The exporter still refuses
user-scoped rows and counts the refusals, and the rule now extends to
allocations: a migrated PCN subscription gets a **self-allocation only**. The
monolith recorded no decision about *which* practices a PCN subscription was for
— coverage was a read-time rule — so minting one allocation per current member
would attribute a named practice's paid access to a purchase nobody recorded
making for them. A PCN administrator re-establishes the reach deliberately.

The live counts are still needed (gate G1) before the migration runs.

### D-3 — Is there a grace period on payment failure?

The monolith grants nothing on `past_due` or `unpaid`, so a card that fails
closes the practice's paid tools at the moment Stripe moves the subscription.
Whether that is intended or simply never considered is a commercial question.

If a grace period is wanted, it needs a length, a starting point (the failure, or
the period end), and a decision about what the customer is told.

**Status: deliberately unchanged in Phase 4B.** No grace period is implemented,
and the Phase 4A state matrix above is retained exactly. `past_due` and `unpaid`
grant nothing. This is a commercial decision and Phase 4B was not the place to
invent one.

### D-4 — Where does the organisation graph come from?

**Answered in Phase 4B: from Identity, over an API, as a projection that expires.**

`MemberOrganizationLink` is gone. Identity gained
`GET /organizations/graph/v1/` — organisation UUIDs, types, active status and
active containment edges, and nothing else; no user, no membership, no role, no
name. Billing holds the result as a versioned `identity.OrganizationGraph` row
with the content digest Identity computed, refreshed on a schedule and again
immediately before any sponsored purchase.

The old behaviour is **inverted**, deliberately. Phase 4A kept using stale edges
on the grounds that withdrawing access was worse than the staleness. It is not:
an entitlement inherited from a relationship nobody can currently confirm is an
entitlement nobody can justify, and the failure mode of continuing is a practice
keeping a paid tool because a sync stopped. So a projection older than
`IDENTITY_GRAPH_MAX_AGE` (default one hour) closes **sponsored entitlements and
new sponsored purchases**.

The asymmetry is the important part: **direct entitlement never consults the
graph**. A practice that bought its own subscription keeps it whether or not
Identity is reachable, so failing closed can never cost anybody what they paid
for themselves.

Relationship changes are diffed on each refresh. A removed edge lapses the
sponsored allocation and raises an `OperationalAlert` — and touches nothing at
the provider. See "Payer and beneficiary" below.

**Status: answered and implemented. Gate G6 is met.**

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

**Answered in Phase 4B: keep what exists.** Existing Stripe products and prices
are retained through the migration and pricing is not redesigned. `PlanPrice`
rows are populated only from *verified existing* Stripe configuration, read
through the read-only catalogue command in Phase 4B.2; nothing is created,
updated or archived at Stripe. A price with no verified `provider_price_id` is
displayable and **not purchasable**, and checkout refuses it by name rather than
passing an empty id to the provider.

Whether existing subscriptions are later moved onto different prices is a
separate commercial decision and is not part of the migration.

The original question, for the record:

`catalog/seed.py` transcribes the monolith's current plans and displayed amounts
(£10/£110 practice, £49/£490 PCN). No `PlanPrice` carries a provider price
reference, because those live in the monolith's environment and this repository
must not read them.

**Status: answered — retain existing products and prices.** Every price is
still displayable and none is purchasable, because no `provider_price_id` has
been verified yet; that happens in Phase 4B.2 through the read-only Stripe
catalogue command.

### D-8 — Who is a billing contact, by default?

The monolith has no billing-contact concept; the Stripe customer is attached to
whichever person clicked buy. `BillingContact` here supports an Identity user
UUID or a bare finance address, and the migration sets none.

Needed: whether the migrating buyer becomes the primary contact automatically, or
whether each organisation nominates one.

**Status: unanswered. Contacts are empty after migration.**
</content>
