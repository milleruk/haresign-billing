# haresign-billing

**Haresign Billing** — the authority for organisation subscriptions, plans,
product entitlements and payment-provider reconciliation.

> **Not live.** `billing.haresign.net` is not served, no production OIDC client
> exists, no Stripe credential exists in any environment, and no live billing
> record has been read. Billing today is still the monolith's, at
> `app.haresign.net`. See `docs/stripe-cutover.md` for the gates.

## What it owns

Billing accounts keyed to a permanent Identity organisation UUID, the product
catalogue, plans and prices, the subscription lifecycle, billing contacts,
provider customer/subscription/price references, invoice references, derived
product entitlements, billing audit events, and verified idempotent webhook
processing.

## What it does not own

Users, organisations, memberships, roles and authentication — those are Haresign
Identity's. Application data, practice datasets and data-processing agreements —
Haresign Intelligence's. MCP tokens — MCP's.

Billing stores no password, membership row or role, and holds only the minimum
Identity UUID references needed for authorization and billing contacts. Identity
in turn stores no plan, subscription state, Stripe identifier or payment detail.

## Running it

```bash
# Everything runs in the container. Migrations are deliberate, never on boot.
docker compose -p haresign_billing exec haresign_billing python manage.py migrate

python manage.py check
python manage.py check --deploy
python manage.py makemigrations --check --dry-run
python manage.py test
ruff check . && ruff format --check .
```

Deployment command shapes are in `docs/deployment.md`.

## Routes

| Route | Who | Notes |
|---|---|---|
| `/` | Anyone | Landing page |
| `/auth/login/` `/auth/callback/` `/auth/logout/` | Anyone / signed in | OIDC relying party. Logout is POST-only. |
| `/organizations/` | Signed in | Organisations you administer |
| `/organizations/<uuid>/` | Organisation admin | Subscription, products, contact, invoices |
| `/organizations/<uuid>/summary.json` | Organisation admin | The shape Identity's subscription card reads |
| `/organizations/<uuid>/checkout/` | Organisation admin | POST. 503 until cutover. |
| `/organizations/<uuid>/portal/` | Organisation admin | POST. 503 until cutover. |
| `/api/v1/organizations/<uuid>/entitlements/` | Signed service credential | The entitlement answer |
| `/api/v1/products/` | Signed service credential | Stable product keys |
| `/providers/webhook/` | The payment provider | Signature-verified, CSRF-exempt |
| `/health/` `/ready/` | Orchestrator | Liveness / readiness |
| `/admin/` | Local operator | Django admin |

## Environment

See `.env.example`, which documents every variable. The four that decide what
this service can do:

| Variable | Ships as | Effect |
|---|---|---|
| `OIDC_ENABLED` | `0` | Sign-in answers 503 |
| `PROVIDER_BACKEND` | `fake` | No Stripe reachability |
| `BILLING_CHECKOUT_ENABLED` | `0` | No purchase route |
| `BILLING_TRAEFIK_ENABLED` | `false` | Nothing served publicly |

No secret has a usable default. The application refuses to start instead.

## Documentation

| | |
|---|---|
| `docs/architecture.md` | Boundaries, structure, request paths |
| `docs/security.md` | Every control and what it defends against |
| `docs/billing-domain.md` | Each model, and where it differs from the monolith |
| `docs/identity-integration.md` | The OIDC design, membership staleness, the summary card |
| `docs/entitlements.md` | The state table, the API contract, **and the unresolved decisions** |
| `docs/migration.md` | The billing migration contract |
| `docs/deployment.md` | Command shapes, secrets, what ships disabled |
| `docs/stripe-cutover.md` | The gates before this service touches money |
| `docs/threat-model.md` | Assets, actors, threats, residual risk |
| `docs/recovery.md` | Restore and incident runbook |

## Before changing anything

Read `AGENTS.md`. It is the working agreement, and its production and Stripe
boundaries are not negotiable from inside this repository.
