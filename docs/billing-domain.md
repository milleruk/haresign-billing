# The billing domain

Derived from reading the monolith's `modules/core/billing/` read-only. What
follows says what each model is for and, where it differs from the monolith, why.

## The shape change

The monolith's `Subscription` has a **required foreign key to a user** and an
optional, informational `practice`/`pcn`. So "who is paying" is a person, and
"who gets access" is worked out at read time by `workspace_entitlements()` from
whichever workspace the reader happens to be in.

Here, a `Subscription` belongs to a `BillingAccount`, a `BillingAccount` belongs
to exactly one Identity organisation, and **there is no user column at all**. A
person's only presence in this schema is as a `BillingContact` UUID reference and
as the actor on an audit row.

That is the single largest change, and everything below follows from it.

## Models

### `catalog.Product`

A capability a subscription can grant. `key` is the stable internal identifier
Haresign Intelligence asks for over the entitlement API, and once shipped it
never changes — a rename changes `name`.

Deliberately not called "entitlement": an entitlement is the *derived answer*
about one organisation and one product at one moment, and conflating the
catalogue row with the derived answer is how a system ends up with a writable
`is_entitled` column.

Retired with `is_active=False`, never deleted. Deleting would silently revoke
live access.

### `catalog.Plan`

A thing a customer subscribes to; grants a set of products; carries no price. A
plan offered monthly and annually is one plan, so switching interval keeps the
plan and the entitlements.

`covers_member_organizations` encodes the monolith's rule that a PCN plan covers
its member practices. See `docs/entitlements.md` D-4 for why the member set is
supplied separately.

### `catalog.PlanPrice`

One interval of one plan, carrying the provider's price reference.

**The row the monolith does not have.** There, price ids live in
`settings.STRIPE_PRICES`, keyed by plan and interval — so switching test to live
prices is an environment change with no record of what was sold at which price,
and a subscription's price id cannot be resolved back to a plan without
re-reading the environment. A retired price cannot be resolved at all.

Amounts are minor units, because money is never a float.

### `billing.BillingAccount`

One organisation's billing, keyed uniquely to its permanent Identity
organisation UUID. That UUID is the join key for the whole service.

`organization_name` and `organization_type` are **display copies**, refreshed
opportunistically from the OIDC session so an admin screen can say which
organisation a row is about without calling Identity on every render. Nothing
branches on them.

`provider_customer_id` is one per account, so a second subscription for the same
organisation reuses the customer rather than creating a duplicate. This is the
one thing the monolith's `StripeCustomer` gets right, moved from the person to
the organisation.

### `billing.BillingContact`

Who to reach about billing, referenced by Identity user UUID wherever possible,
with a bare address as the fallback for a finance mailbox with no Haresign
account. Never used for authentication. Never written into an audit record — the
scrubber drops any key containing "email" for exactly this reason.

A database constraint requires either a UUID or an address: a contact that
identifies nobody is a row nobody can action.

### `billing.Subscription`

One provider subscription, normalized. `state` is this service's vocabulary,
mapped from the provider's in `providers/mapping.py`.

The values coincide with Stripe's because Stripe's are what the monolith already
stores, and renaming them during a cutover would make the two systems impossible
to compare — but the mapping is a function with a test rather than an assumption,
and its default is `UNKNOWN`, which grants nothing. A status this service has
never seen fails closed and stays visible.

Two additions over the monolith:

* **`provider_sequence`** — the provider's ordering signal. Webhook delivery is
  not ordered, and without this an old `active` event arriving after a `canceled`
  one silently re-opens a cancelled organisation's tools.
* **`provider_synced_at`** — when the state was last confirmed. Read by the UI to
  say how fresh the answer is, and by reconciliation to find rows nobody has
  heard about.

`canceled_at` and `ended_at` are kept separate: a cancellation requested on the
3rd for a period ending on the 30th has two dates, and quoting the wrong one is
either a refund conversation or a support ticket.

### `billing.SubscriptionItem`

One priced line. The monolith stores a single `stripe_price_id` on the
subscription and reads `items.data[0]` from the provider object — so a
subscription with two lines silently becomes a subscription with one, and the
second line's plan is lost. A subscription here grants the union of its items'
plans' products.

### `billing.ComplimentaryGrant`

Time-limited access given without payment, carried over from the monolith's
`AccessGrant`. Kept separate from `Subscription` for the monolith's own stated
reason: "who is actually paying us" must stay answerable from the subscription
table alone.

Two of the monolith's three grant targets survive. Practice and PCN grants become
grants to that Identity organisation. A grant to an individual **user** does not
survive — it granted entitlement to a person regardless of organisation, which
under an organisation-keyed model has no meaning and would be exactly the "a
person gets paid access without a subscription" shape the ownership contract
forbids. The migration refuses those rows rather than guessing; see D-2.

Expiry is required. An open-ended grant is indistinguishable from a permission
bug six months later, so "indefinite" is spelled as a far-future date.

### `billing.MemberOrganizationLink`

A parent→child organisation edge, **cached and not authoritative**. Identity owns
the organisation graph; Billing needs one fact from it. Populated only by the
migration importer, stamped with a source and an observation time. See D-4.

### `billing.InvoiceReference`

A pointer to an invoice the provider holds — number, status, total, hosted URL.
Deliberately thin. The PDF, the line detail, the customer address and the payment
method stay at the provider, where they are already stored correctly and where we
are not the ones who have to keep them safe. The UI links out; it does not render
money documents from a local mirror, which is how a customer ends up with two
invoices that disagree.

### `providers.WebhookEvent`

One row per verified provider event, unique on the provider's event id. **That
constraint is the idempotency mechanism** — not a convention, not a cache.

Stores type, ids, timestamps, outcome, delivery count and a SHA-256 of the body.
Never the body: a digest proves a re-delivery is byte-identical without this table
becoming a second copy of every customer's billing history.

### `providers.ReconciliationRun`

One comparison of local state against the provider's, holding aggregate counts. A
report that listed which customers disagreed would be a customer list with
financial state attached, and reports get pasted into tickets.

### `legacy_migration.*`

`ImportRun` plus three mapping tables. The mappings are what make a *delta*
possible: without them a second run cannot tell "this already exists" from "this
is new", and would either duplicate or overwrite. Both fingerprints are keyed
digests, never the source values — a mapping table holding Stripe subscription
ids beside organisation UUIDs would be a re-identification dataset.

## What is never stored

Raw card details, bank information, checkout secrets, client secrets, full
provider payloads, Identity passwords, memberships, roles or user accounts.
