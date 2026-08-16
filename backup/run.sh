#!/bin/sh
#
# Encrypted PostgreSQL backups for Haresign Billing.
#
# Two properties matter and both are structural rather than procedural.
#
# The dump is **encrypted before it reaches the volume**: `pg_dump` writes to a
# pipe and `age` writes the file, so there is no moment at which a plaintext copy
# of the billing database exists on disk to be snapshotted, backed up again, or
# read by anything that gets the volume.
#
# The **recipient is a public key**. This container can create a backup and
# cannot read one. The private key is held separately, off this host, by whoever
# is authorised to perform a restore — so compromising the billing service does
# not hand over its history.
set -eu

backup_dir=/backups
interval=${BACKUP_INTERVAL_SECONDS:-86400}
retention=${BACKUP_RETENTION_DAYS:-14}

run_backup() {
    timestamp=$(date -u +%Y%m%dT%H%M%SZ)
    temporary="${backup_dir}/.billing-${timestamp}.sql.age.tmp"
    destination="${backup_dir}/billing-${timestamp}.sql.age"
    # Written to a dot-prefixed temporary and renamed only on success, so a
    # restore never picks up a half-written dump from an interrupted run.
    trap 'rm -f "$temporary"' EXIT HUP INT TERM

    export PGPASSWORD
    PGPASSWORD=$(sed -n '1p' "$POSTGRES_PASSWORD_FILE")
    pg_dump \
        --host "$POSTGRES_HOST" \
        --username "$POSTGRES_USER" \
        --dbname "$POSTGRES_DB" \
        --format custom \
        --no-owner \
        --no-privileges \
        | age --encrypt --recipients-file "$BACKUP_RECIPIENT_FILE" --output "$temporary"
    unset PGPASSWORD

    mv "$temporary" "$destination"
    touch "${backup_dir}/.last-success"
    find "$backup_dir" -type f -name 'billing-*.sql.age' -mtime "+$retention" -delete
    trap - EXIT HUP INT TERM
    echo "Billing database backup completed at ${timestamp}."
}

while true; do
    run_backup
    sleep "$interval"
done
