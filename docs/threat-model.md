# Threat model

Scoped to what this service actually holds: subscription state, provider
references, derived entitlements and a billing audit trail. It holds no card
data, no bank details, no passwords and no patient data — which removes several
categories of threat entirely and is the first control, not an accident.

## Assets

| Asset | Why it matters |
|---|---|
| The entitlement answer | It decides whether a practice's paid tools open. Wrong in one direction is lost revenue; wrong in the other is a customer locked out. |
| Subscription state | Money state. Wrong state means wrong access and wrong support answers. |
| Provider references | `cus_*` / `sub_*` are not secrets, but with a Stripe key they are a map of the customer base. |
| The billing audit trail | Answers "who did what". Its value is entirely in being unaltered. |
| The organisation UUID ↔ billing mapping | Reveals which organisations pay for what. |
| Service credentials | The entitlement API keys and, after cutover, the Stripe keys. |

## Actors

| Actor | Capability |
|---|---|
| Anonymous internet | Reaches `/providers/webhook/`, `/health/`, `/ready/` and (after cutover) the sign-in redirect. |
| An authenticated Haresign user | A valid Identity session with some set of memberships. |
| An organisation administrator | May manage that organisation's billing. |
| A platform administrator | Support access to any organisation. |
| A compromised consumer | Holds a valid entitlement-API credential. |
| Someone with the database | Full read/write on billing state. |
| Someone with the host | Everything. |

## Threats and controls

### T1 — Cross-organisation access (IDOR)

Change the UUID in the URL and read somebody else's billing.

**Controls.** The URL UUID is never the authority; authorization resolves it
against this session's memberships. Refusals are 404, not 403, so the endpoint is
not an oracle for which organisation UUIDs exist. Views receive a resolved
`access` object rather than re-reading the kwarg. Membership claims expire.

**Residual.** A platform administrator can read any organisation. Mitigated by
mandatory auditing and an on-page declaration, not prevented.

### T2 — Forged or replayed webhooks

Grant yourself a subscription, or cancel a competitor's, by posting to the
webhook.

**Controls.** Signature verification before parsing and before any write.
Constant-time comparison. A timestamp inside the signed payload with a bounded
tolerance, so a captured delivery is not replayable forever. Idempotency by a
database unique constraint on the provider event id. A bad signature is 400 and
never retried.

**Residual.** Anyone with the signing secret can forge an event. The secret is a
file, and rotation is in the runbook.

### T3 — Out-of-order provider events

A late `active` event arriving after a `canceled` one silently re-opens a
cancelled organisation's paid tools.

**Controls.** `provider_sequence`; a strictly older event is recorded as
out-of-order and discarded. This is the monolith's live bug, and it is why the
control exists.

### T4 — Entitlement escalation

Get a paid entitlement without paying.

**Controls.** Entitlements are derived, never stored, so there is nothing to
write. Roles grant nothing — neither organisation administrator nor platform
administrator. Complimentary grants are the only non-payment route, they are
audited, and they must expire. Every unresolvable case fails closed.

### T5 — Entitlement API abuse

Use the internal API to enumerate customers or read another organisation's state.

**Controls.** Signed request rather than a bearer token, so a captured header
cannot be replayed at a different path. Bounded timestamp. Unknown key id costs
the same time as a bad signature. One generic refusal body. Closed by default
when no credentials are configured. An unknown organisation answers "holds
nothing" rather than 404, so the endpoint does not confirm existence.

**Residual.** A valid credential can query any organisation UUID it can guess —
but UUIDs are not guessable and the response carries no personal data. Rotation
is overlap-capable.

### T6 — Session and sign-in attacks

Session fixation, CSRF, an open redirect on the callback, a token minted for
another relying party.

**Controls.** Session key cycled at sign-in. Single-use `state`. PKCE verifier
never sent to the browser. `nonce` bound to the request. Audience and `azp`
checked. Issuer compared exactly. `next` must be a same-site path or it is
discarded rather than sanitised. CSRF on every mutation, POST-only sign-out and
mutations, `SameSite=Lax`, host-only cookies.

### T7 — Data leakage into logs, audits and reports

The commonest real-world billing failure: financial and personal data
accumulating in places nobody classified.

**Controls.** The audit scrubber drops credential- *and* payment-shaped keys by
name. The webhook ledger holds a digest, never the payload. The entitlement API
returns no personal or payment data, with tests asserting each absence.
Reconciliation and migration output is aggregate counts. A test scans every
logging call's interpolated arguments. Migration manifests use a keyed HMAC, so
a count-based document is not guessable.

### T8 — Migration mishandling

Reading too much from the monolith, writing it somewhere it should not go, or
importing a tampered artifact.

**Controls.** Exact schema validation in both directions. A read-only connection
proved by attempting a write. An explicit field allowlist. Authenticated
encryption, so a tampered artifact fails to decrypt. Mode 600, no plaintext on
disk. Mandatory dry-run. Conflicts abort the whole transaction. The importer has
no route to the source, by topology.

### T9 — Reaching Stripe before anybody has agreed to

**Controls.** Four independent things would each have to change: `PROVIDER_BACKEND`,
a Stripe secret key, the production overlay's secret declarations, and the
adapter's own refusal to construct without a key. Tests assert each.

### T10 — Compromise of the billing host

**Controls.** Backups are encrypted to a public key whose private half is held
elsewhere, so the history does not fall with the host. The database is on an
internal network with no published port. The container runs unprivileged with
nothing writable in the image.

**Residual.** Someone with the host has everything current. The audit trail is
tamper-evident against the application, not against the database; the provider
is the independent record, and reconciliation is how it is used.

## Explicitly out of scope

* **Card data, bank details, checkout and client secrets.** Never stored. Payment
  is handled entirely on provider-hosted pages.
* **Invoice documents.** References only; the provider holds the documents.
* **Identity credentials.** No password path exists.
* **Patient or clinical data.** Belongs to Intelligence and never reaches here.
