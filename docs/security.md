# Security

Controls, and what each is actually defending against. Everything here has a test.

## Authentication

There is **no local password in this service**. `IdentityUser.set_password`
raises unless called with `None`, every row is created with an unusable password,
and the only authentication backend refuses to authenticate anything. Sign-in
happens at Haresign Identity over OIDC and `login()` is called with the backend
named explicitly after every validation has passed.

### The relying-party checks

`identity/client.py`. Each has a test supplying a token that fails exactly that
check and asserting no session was created.

| Check | Defends against |
|---|---|
| `state`, single-use, held server-side, constant-time compare | An attacker completing their own authorization and handing the victim a callback URL that signs the victim into the attacker's account. |
| PKCE verifier, never sent to the browser | A leaked authorization code — from a redirect log, a referrer header, browser history — being redeemable. |
| `nonce`, bound to the pending request | A token minted for a different session of the same client being replayed. |
| Issuer, compared **exactly** | Trusting `https://identity.haresign.net.attacker.example`. A prefix or host comparison is how that happens. |
| Audience, and `azp` where present | A token issued to a *different* relying party of the same provider — perfectly valid, and says nothing about our session. |
| Signature, asymmetric algorithms **allow-listed** | `alg: none`, and HMAC verified against the public key. A deny list has to anticipate every dangerous algorithm and only needs to miss one. |
| Expiry and `iat`, bounded skew | Indefinite token reuse. |
| Discovery document's issuer equals the issuer asked (RFC 8414 §3.3) | Being pointed at somebody else's provider. This is the check that makes discovery safe to trust. |
| HTTPS on every provider endpoint | Tokens on the wire in clear. Relaxed only by explicit opt-in, and only for the loopback rehearsal. |

Session keys are cycled on sign-in, so a session fixated beforehand does not
survive it. Sign-out is POST-only — a GET sign-out is fired by any prefetch,
image tag or link scanner pointed at the URL.

The refusal page names no specific check. Telling somebody probing the callback
which validation failed is free reconnaissance; the audit row carries the detail.

## Authorization

**An organisation UUID from the browser is never trusted.**
`require_organization_admin` resolves it against the memberships Identity
reported for *this session* and hands the view an `access` object, so a view
cannot re-read the URL and win.

A refusal is **404, not 403**. A 403 confirms the organisation exists, which
turns the endpoint into an oracle for enumerating customer UUIDs.

**Stale membership.** Claims carry a capture time. Older than
`IDENTITY_MEMBERSHIP_MAX_AGE` (default 900s) and they are not relied on; the
request re-authorizes through the protocol. A background sync against Identity's
database would have been an undocumented runtime dependency, which the ownership
contract forbids.

**There is no support bypass.** Phase 4A let a platform administrator open any
organisation's billing without a membership. Phase 4B removed it, along with the
`is_platform_admin` field and every dependency on the `haresign_platform_admin`
claim — which Identity does not emit, so it was reachable only from the synthetic
rehearsal. Active `organization.admin` membership is the sole route to an
organisation's billing, and Django's `is_staff` grants nothing there.

The `support_access` audit column stays, because audit columns are a contract and
a support query written against it must keep working. It is still set by
complimentary-grant operations, which are a genuine staff action.

**Paying never creates a role; a role never creates an entitlement.** Both
directions are tested.

## Webhooks

The only route the public internet is expected to reach.

1. **Signature first**, before parsing and before any write. An unverified body is
   attacker-controlled input.
2. **Claim the event id** with a unique-constrained insert, so two concurrent
   deliveries race for one row and exactly one wins. A check-then-act on "have I
   seen this?" is a race whose losing branch applies the event twice.
3. **Apply transactionally**, so there is no window where an event is marked
   processed but its effect rolled back.

Signature verification is constant-time and the signed payload carries a
timestamp with a tolerance window, so a captured webhook is not replayable
forever.

The HTTP contract: a bad signature is **400 and never retried** (retrying a
forgery is pointless); an understood-but-unresolvable event is **200** (the
provider retrying will not make an unknown price known, and an endpoint that
500s gets disabled); only a genuine internal failure is **500**.

## Entitlement API

Signed internal credential, not a bearer token: the signature covers the request
path, so a captured `Authorization` header cannot be replayed against a different
organisation. Constant-time comparison; an unknown key id does the same work as a
bad signature so the header cannot enumerate valid key ids; a bounded timestamp;
one generic refusal body for every cause.

**No configured credentials means the API is closed, not open.** The opposite
default would turn a forgotten deployment variable into an unauthenticated
entitlement oracle.

Rotation is overlap-capable: several `key_id:secret` pairs may be configured at
once, so a rotation is add-new, deploy consumers, remove-old, with no window
where every consumer is broken.

## Transport and headers

Strict enforced CSP on **every** response including 404s, 429s and error pages —
those are often the responses that echo input. No `unsafe-inline`, no
`unsafe-eval`, on any route including `/admin/`. `frame-ancestors 'none'`,
`object-src 'none'`, `form-action 'self'`, `base-uri 'self'`.

Not one template contains an inline `<style>`, an inline `<script>` or a `style`
attribute, and a test asserts it, so the policy cannot be quietly outgrown.

HSTS with subdomains and preload, `nosniff`, `strict-origin-when-cross-origin`,
`X-Frame-Options: DENY` in every environment, `noindex` on every response
including JSON ones via `X-Robots-Tag`.

**Host-only cookies.** `SESSION_COOKIE_DOMAIN` is never set. The monolith shares
one cookie across `*.haresign.net`; Billing must not join that arrangement.

**No third-party runtime requests.** No CDN, fonts, analytics or tag managers.
Assets are self-hosted, which also keeps the pages working on locked-down NHS
networks.

## Throttling

Two independent counters per attempt — client IP and a scope identifier. Cache
keys are keyed digests, never raw addresses: a Redis instance that can be read
should not be a directory of who has been calling.

Consumed *before* validation, so a rejected request still costs its allowance — a
limiter that only counts successes limits nothing.

**Production fails closed.** A cache outage refuses rather than silently removing
the only rate protection the OIDC and webhook endpoints have.

## Data minimisation

**Audit metadata** is scrubbed of anything credential- *or* payment-shaped:
password, secret, token, signature, card, PAN, CVC, IBAN, sort code, account
number, bank, payment method, client secret, payload, email, phone, address.
Dropped rather than masked — a masked secret is still a statement about a secret.
Values are length-bounded with a visible ellipsis.

**Audit events are append-only in the model.** `save()` raises on update,
`delete()` raises outright. Not tamper-proof — anything with the database password
can rewrite rows — but tamper-evident against the application, which is the layer
where mistakes and misuse happen.

**The webhook ledger holds a digest, never the payload.**

**Reconciliation and migration output is aggregate counts only.**

**The entitlement API returns no payment detail, invoice, provider identifier,
plan, price, contact or personal data**, and each absence is a test.

**Nothing logs** a request body, a signature, a secret or an ID token; a test
scans every logging call's interpolated arguments.

## Secrets

No secret has a usable default; the application refuses to start instead. Secrets
are read from files by `config/secret_entrypoint.py`, which resolves an
**exhaustive allowlist** of names — a loop over "anything ending in `_FILE`" would
read whatever path an attacker who could set one variable chose — and then drops
to uid 10001 before exec'ing the application.

A static scan asserts no `sk_live_`, `sk_test_`, `whsec_live`, `rk_live_` or PEM
private key is committed.

## Backups

Encrypted with `age` **before the bytes reach the volume**: `pg_dump` writes to a
pipe and `age` writes the file, so no plaintext copy of the billing database ever
exists on disk to be snapshotted or read from the volume.

The recipient is a **public** key. The backup container can create a backup and
cannot read one; the private half is held separately, off this host, by whoever
may perform a restore. Compromising the billing service does not hand over its
history.

## The Stripe boundary

No code path may reach Stripe's API until an authorised cutover. The adapter
raises without a secret key, `PROVIDER_BACKEND` defaults to the fake, no
environment sets a key, and the production overlay declares no Stripe secret at
all. Four independent things would each have to change.
