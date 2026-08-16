# Recovery runbook

## Before you start

You need three things, and they are deliberately not in the same place:

1. The encrypted backup, from the `haresign_billing_backups` volume.
2. The `age` **private** key, held off this host by whoever may perform a
   restore. The backup container has only the public half and cannot decrypt its
   own output.
3. A separate PostgreSQL 16.

If you have the backup but not the key, you cannot restore, and that is the
design working as intended rather than a problem to route around.

## Restore

Always into a **separate** database first. Restoring over the live one turns a
readable-dump question into an outage, and restoring beside the original proves
the dump is readable rather than that it is recoverable.

```bash
# 1. A separate PostgreSQL 16 on the billing network.
docker run -d --name billing-restore \
  --network haresign_billing_billing_internal \
  -e POSTGRES_DB=haresign_billing_restore \
  -e POSTGRES_USER=haresign_billing \
  -e POSTGRES_PASSWORD=<throwaway> \
  postgres:16-alpine

# 2. Decrypt and restore in one pipe — no plaintext dump on disk.
docker run --rm --network haresign_billing_billing_internal \
  -v haresign_billing_backups:/backups \
  -v /path/to/backup.key:/key:ro \
  haresign-billing-backup:<tag> \
  sh -c 'export PGPASSWORD=<throwaway>; \
    age --decrypt --identity /key /backups/billing-<timestamp>.sql.age \
    | pg_restore --host billing-restore --username haresign_billing \
                 --dbname haresign_billing_restore --no-owner --no-privileges'
```

## Verify

Compare **aggregate counts** against the source. Never dump rows to compare them.

```sql
SELECT 'billing accounts',    count(*) FROM billing_billingaccount
UNION ALL SELECT 'subscriptions',        count(*) FROM billing_subscription
UNION ALL SELECT 'complimentary grants', count(*) FROM billing_complimentarygrant
UNION ALL SELECT 'member links',         count(*) FROM billing_memberorganizationlink
UNION ALL SELECT 'webhook events',       count(*) FROM providers_webhookevent
UNION ALL SELECT 'import runs',          count(*) FROM legacy_migration_importrun
UNION ALL SELECT 'audit events',         count(*) FROM audit_auditevent
UNION ALL SELECT 'products',             count(*) FROM catalog_product
UNION ALL SELECT 'plans',                count(*) FROM catalog_plan;
```

Then check the subscription state distribution, which is the thing a customer
would notice being wrong:

```sql
SELECT state, count(*) FROM billing_subscription GROUP BY state ORDER BY state;
```

## After a restore, before serving

**Reconcile against the provider.** A restore returns the database to a point in
the past, and the provider has moved on since. Every webhook delivered between
the backup and the restore is lost.

```bash
python manage.py reconcile_provider          # report only
python manage.py reconcile_provider --apply  # after reading the report
```

The webhook ledger is what makes this safe: events already applied are in the
restored ledger, so a provider re-delivery after the restore is recognised as a
duplicate rather than applied twice.

**Do not re-run the migration importer** to "catch up". It imports monolith
state, not provider state, and running it against a restored database with stale
mappings is how a conflict becomes a duplicate.

## Incident checklist

| Situation | Do |
|---|---|
| Backups silently failing | The backup container's healthcheck goes unhealthy after 25 hours without a success. Check its logs; it never deletes a backup it has not replaced. |
| Webhook endpoint has been down | Nothing is lost — the provider retries, and the ledger makes each retry idempotent. Run `reconcile_provider` to confirm. |
| Signing secret rotated in the wrong order | Every delivery 400s and the ledger stays empty for the period. Fix the secret, then reconcile; the provider's retries will cover most of it. |
| Entitlement API credential compromised | Remove that `key_id:secret` pair from `ENTITLEMENT_API_KEYS` and redeploy. Other pairs keep working, so consumers using them are unaffected. |
| Suspected unauthorised billing change | `audit_auditevent` is append-only and carries actor UUID, organisation, request id and `support_access`. Join to the application logs on `request_id`. |
| Database compromised | Audit events are tamper-evident against the *application*, not against the database. Treat the audit trail as suspect and reconcile against the provider, which is the independent record. |
