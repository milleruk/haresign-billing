# Deployment

## Command shapes

```bash
# Permanent deployment. Traefik router disabled; nothing is served publicly.
docker compose -f docker-compose.yml -f docker-compose.production.yml \
  -p haresign_billing up -d

# Isolated rehearsal. Always with its own -p, always torn down with --volumes.
docker compose -f docker-compose.yml -f docker-compose.test.yml \
  -p haresign_billing_phase4a up -d --build
docker compose -f docker-compose.yml -f docker-compose.test.yml \
  -p haresign_billing_phase4a down --volumes
```

`docker-compose.yml` is never used alone. Its `name:` is deliberately
`haresign_billing_no_overlay_selected`, so a bare `docker compose up` here
produces something obviously wrong in `docker compose ls` rather than a plausible
second stack fighting the deployed one for container names.

## Images are immutable

`BILLING_IMAGE_TAG` is required and has no default — the production overlay uses
`${BILLING_IMAGE_TAG:?...}`, so Compose refuses rather than falling back to
`latest`. Tag with the source commit:

```bash
docker build -t haresign-billing:$(git rev-parse --short HEAD) .
docker build -f backup/Dockerfile -t haresign-billing-backup:$(git rev-parse --short HEAD) .
```

## Migrations are deliberate

Never run from an entrypoint. An entrypoint that migrates on boot means every
replica races to alter the schema during a rollout, and a failed migration
becomes a crash loop instead of a decision.

```bash
docker compose -p haresign_billing exec haresign_billing python manage.py migrate
```

The catalogue is seeded by a **data migration**, not a management command: a
deployment that has migrated and has no products cannot answer the entitlement
API, and "remember to run the seed command" is not a step anybody remembers at
2am.

## Secrets

Individual read-only files under `/opt/docker/secrets/`, mounted as Docker
secrets and resolved by `config/secret_entrypoint.py` from an exhaustive
allowlist of `<NAME>_FILE` variables. The entrypoint then drops to uid 10001
before exec'ing the application, so no secret is ever an environment variable in
an unprivileged process listing.

| File | Contents |
|---|---|
| `haresign-billing-django-secret.txt` | `SECRET_KEY` |
| `haresign-billing-postgres-password.txt` | Database password |
| `haresign-billing-oidc-client-secret.txt` | Relying-party secret (unused until a client exists) |
| `haresign-billing-entitlement-api-keys.txt` | `key_id:secret` pairs, comma-separated |
| `haresign-billing-backup-recipient.txt` | An `age` **public** key |
| `haresign-billing.env` | Non-secret environment |

Never in git. Never in an image. Never in a log.

## What ships disabled, and why

| | State | Enabling it is |
|---|---|---|
| Traefik router | **Enabled** since 4B.2. `billing.haresign.net` resolves and is served. | — |
| OIDC | **Enabled** since 4B.2. A production relying-party client exists at Identity. | — |
| Payment provider | `PROVIDER_BACKEND=fake` | A cutover step. The production overlay declares no Stripe secret at all, and `web/tests/test_boundary.py` asserts it. |
| Fake webhook verification | `FAKE_PROVIDER_WEBHOOKS_ENABLED` unset | Never, on a deployed environment. The fake's signing secret is published in this repository, so the served webhook endpoint refuses every delivery until a real Stripe credential exists. |
| Hosted checkout / portal | `BILLING_CHECKOUT_ENABLED=0` | A cutover step. The pages show plan and state without a purchase route. |

Each is independent. Enabling one does not enable another.

## Networks

Two, and the split matters. `t3_proxy` (external, shared) is how Traefik would
reach the application. `billing_internal` is `internal: true` with no route out,
and is the only network the database and Redis are on. Putting PostgreSQL on the
shared proxy network would put the billing database on the same L2 as every other
container on the host.

No published database or Redis port, in any overlay.

## Backups

`billing_backup` dumps daily, encrypting **before the bytes reach the volume** —
`pg_dump` writes to a pipe and `age` writes the file, so no plaintext copy ever
exists on disk.

The recipient is a **public** key: this host can create a backup and cannot read
one. The private half is held separately, off this host, by whoever may perform a
restore. That is what makes the backups survive a compromise of the billing
service.

Its healthcheck goes unhealthy if no backup has succeeded in 25 hours, so a
silently failing backup is visible rather than discovered during a restore.

Restore is in `docs/recovery.md`, and it restores into a **separate** PostgreSQL
16 — restoring beside the original proves the dump is readable, not that it is
recoverable.

## Scheduled work on the host

Two root cron entries specific to Billing, each a script kept in `ops/` and
installed to `/usr/local/bin` as `root:root` mode `700`. The `01:00` off-host
mirror is generic to the host and is not one of them — it simply carries whatever
the `00:45` stage has left for it.

| When | Script | Does |
|---|---|---|
| `00:45` | `billing-backup-stage.sh` | Stages the encrypted backup volume into the generic backup root, after Identity's `00:40` stage and before the `01:00` off-host mirror. Every failure path refuses to stage *and* refuses to prune, because the mirror runs `rsync --delete`. |
| every 10 min | `billing-graph-refresh.sh` | `manage.py refresh_organization_graph`. Comfortably inside `IDENTITY_GRAPH_MAX_AGE`, so one failed refresh closes nothing. |

The graph refresh applies a new projection promptly when Identity's estate
changes, and does nothing at all when it has not. It does **not** make a stale
projection fresh — see `docs/stripe-cutover.md`, "The projection that cannot
become fresh", which is an open decision rather than a scheduling problem.

Neither script decrypts anything, and neither has key material on its path.

## The isolated rehearsal

`docker-compose.test.yml`. What makes it safe to run on the same host as
production:

* Its own project name and its own container names (`hsbill-test-*`), so it
  cannot adopt or evict a deployed container.
* `t3_proxy` overridden from `external: true` to a project-local bridge, so
  Traefik never sees it and nothing is routed.
* Throwaway secrets written in the file on purpose — a rehearsal that needed a
  real secret is one somebody would be tempted to point at real data.
* `tmpfs` data directories, so it leaves nothing behind even if somebody forgets
  `--volumes`.
* A synthetic OIDC provider and a synthetic monolith-shaped source, both seeded
  from this repository. No live Identity, no live monolith, no Stripe.
* The source sits on `source_only`, a network the application container is not
  attached to. The importer's isolation from the source is topology, not
  restraint.

Tear it down with its own `-p` name and `--volumes`.

## Health and readiness

`/health/` is liveness only and deliberately shallow — it touches neither
PostgreSQL nor Redis. A health check that queries the database conflates "this
process is alive" with "its dependencies are healthy", and an orchestrator that
believes them the same will kill every healthy container during a database blip.

`/ready/` checks both, because the webhook endpoint and the entitlement API both
fail closed without the cache: an instance that cannot reach Redis would refuse
every provider delivery, which is a correct refusal and an incorrect thing to
route traffic to.

Both are exempt from the HTTPS redirect, because container probes speak loopback
HTTP.

## Verification before any deploy

```bash
python manage.py check
python manage.py check --deploy
python manage.py makemigrations --check --dry-run
python manage.py test
ruff check . && ruff format --check .
docker build -t haresign-billing:$(git rev-parse --short HEAD) .
docker compose -f docker-compose.yml -f docker-compose.production.yml config -q
```
