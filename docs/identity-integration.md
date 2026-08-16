# Identity integration

How Billing authenticates people, how it decides what they may do, and how it
does **not** talk to Haresign Identity.

## The relying-party design

Billing is an OIDC client of Identity, using the one flow Identity supports.

| | |
|---|---|
| Flow | Authorization Code |
| PKCE | **Mandatory**, S256. Identity requires it (`PKCE_REQUIRED`), and discovery is checked to advertise it. |
| `state` | Required, single-use, server-side, constant-time compare |
| `nonce` | Required, bound to the pending request, compared on the ID token |
| Redirect URI | Exact, absolute, from `OIDC_REDIRECT_URI` — never built from the request Host header |
| Client authentication | `client_secret_basic`; the secret is a file, never a query parameter |
| ID token | RS256, validated against the provider's JWKS |
| Sessions | Server-side (`django.contrib.sessions.backends.db`), host-only cookie |
| Scopes | `openid profile email haresign:memberships` |

Endpoints are discovered from the issuer rather than configured individually, so
a provider that moves an endpoint needs no Billing deployment — safe because the
issuer *inside* the discovery document must equal the issuer we asked (RFC 8414
§3.3).

## Validation

See `docs/security.md` for the table of every check and what it defends against.
The short version: issuer as an identifier not a prefix, audience and `azp`,
signature with the asymmetric algorithms allow-listed, expiry with bounded skew,
nonce, and single-use state.

## Logout

POST-only. Ends the local session, deletes that session's membership rows, writes
an audit event, and then redirects to Identity's `end_session_endpoint` with an
`id_token_hint` so the Identity session ends too. The ID token is held in the
session for that one purpose and is never rendered, logged or audited.

## Memberships: a snapshot, not a copy

`SessionMembership` holds what Identity said **at this login**: organisation
UUID, display name and type, and the role key exactly as Identity named it. Rows
are keyed to the session and deleted when it ends.

Three properties make this a snapshot rather than a quiet duplication of
Identity's membership table:

* **Session-scoped.** It expires when the session does. There is no per-user
  membership table.
* **Timestamped.** `captured_at` is what makes staleness visible.
* **Never authoritative.** Nothing in Billing reads it except the authorization
  check, and the check knows it is reading a claim.

### Stale membership claims

An ID token is a snapshot. Somebody removed from an organisation at 10:00 must
not still be managing its billing at 17:00 because they signed in at 09:00.

So a membership older than `IDENTITY_MEMBERSHIP_MAX_AGE` (default 900 seconds) is
**not relied on**. The request is refused and the person re-authorizes through
Identity, which issues a current claim.

Deliberately a short maximum age rather than a background sync. A sync would mean
Billing polling Identity — an undocumented runtime dependency the ownership
contract forbids — or reading Identity's database, which nothing may do.
Re-authorization goes through the front door the protocol already provides, and
costs the person a redirect they will not notice.

An unrecognised role key is **stored** (so support can see it) with
`is_administrator=False`. Anything the claim does not explicitly say is an
administrator role is not treated as one.

## Who may manage billing

Only an **active organisation administrator** of that organisation, established
from the session's memberships. Never from the URL.

A **platform administrator** may open an organisation they are not a member of.
Membership is checked first, so a platform administrator who genuinely belongs to
an organisation is treated as a member — otherwise every staff member's own
organisation would be recorded as support access and bury the events that matter.
Every genuine use writes an audit row with `support_access=True` and the page
tells the person they are using it.

Neither grants a paid entitlement. See `docs/entitlements.md` D-1.

## The subscription card in Identity

Identity's organisation page already renders a Subscription panel whose context
is `None` and whose note reads *"Held by Haresign billing, not by this service."*

`GET /organizations/<uuid>/summary.json` is the shape that fills it: state, state
label, plan name, renewal date, end date, a manage URL and an `as_of` timestamp.
Nothing else — no provider identifier, no amount, no invoice, no contact, and a
test asserts each absence.

Two rules for the consumer:

* **Identity displays it and links here. It must not persist it.** Billing is the
  authority, and a second stored copy is a second answer that will eventually be
  the wrong one.
* **The card says what it was told and when.** If Billing is unreachable, the
  card says so; it does not show a remembered state.

This endpoint is authorised by the same session-membership check as the page — it
is a browser endpoint for a signed-in administrator, not the service-to-service
API, which lives at `/api/v1/` behind a signed credential.

## Service-to-service authentication

Identity's discovery document advertises `grant_types_supported:
["authorization_code"]` and its token endpoint refuses anything else. **There is
no client-credentials grant**, and this repository does not pretend otherwise.

So `api/auth.py` implements a narrowly scoped internal credential:

```
Authorization: Haresign-Service <key_id>:<timestamp>:<hmac-sha256>
```

signed over `METHOD\npath\ntimestamp`. Properties, and why each:

* **A signature over the request, not a bearer token** — the path is signed, so a
  captured header cannot be replayed against a different organisation.
* **A bounded timestamp inside the signature** — a captured request expires.
* **Constant-time comparison**, and an unknown key id does the same work as a bad
  signature, so the header cannot enumerate valid key ids.
* **Overlap-capable rotation** — several pairs may be configured at once.
* **Key ids in logs, secrets never.**
* **Closed by default** — no configured credentials means every request is
  refused.

This is a documented stopgap with a stated replacement path, not a pretend
standard. See `docs/entitlements.md` D-6 and gate G7.

## What Billing never does

* Read Identity's database. There is no configuration for one.
* Poll Identity at runtime for memberships, roles or organisations.
* Store an Identity password, membership row, role or user account.
* Create, update or revoke an OIDC client at Identity.
* Write anything to Identity, ever.
