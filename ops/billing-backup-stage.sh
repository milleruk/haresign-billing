#!/usr/bin/env bash
# Stage the permanent Billing encrypted backup volume into the generic backup
# root, so the 01:00 storagebox mirror carries every Billing artifact off-host.
#
# Installed at /usr/local/bin/billing-backup-stage.sh, root:root, mode 700,
# run from root cron at 00:45 — after the Identity stage at 00:40 and ahead of
# the 01:00 generic mirror, which this script deliberately does not touch.
#
# The generic mirror runs rsync --delete. An empty or stale stage would
# therefore delete the off-host copies, so every failure path below refuses to
# stage *and* refuses to prune, leaving the previous stage intact.
#
# Ciphertext only: the age-encrypted artifacts are copied as they are. No
# decryption, no key material and no plaintext ever passes through here. The
# private half of the backup key is not on this host's backup path at all.
set -Eeuo pipefail

VOLUME_DIR="/var/lib/docker/volumes/haresign_billing_haresign_billing_backups/_data"
STAGE_DIR="/opt/docker/backups/haresign-billing"
RETENTION_DAYS=14
MAX_MARKER_AGE_MIN=1500          # matches the billing_backup health check
LOG_FILE="/var/log/billing-backup-stage.log"

umask 077

log() { echo "[$(date '+%F %T')] $*" >>"$LOG_FILE"; }

fail() { log "FAIL: $1"; exit "$2"; }

[[ -d "$VOLUME_DIR" ]] || fail "backup volume missing: ${VOLUME_DIR}" 2

# The backup container writes .last-success only after an atomic rename of a
# completed artifact, so a current marker is what makes the source trustworthy.
[[ -f "$VOLUME_DIR/.last-success" ]] || fail "no .last-success marker; not staging or pruning" 3

if [[ -z "$(find "$VOLUME_DIR/.last-success" -mmin "-${MAX_MARKER_AGE_MIN}" -print -quit)" ]]; then
    fail "backup marker stale (>${MAX_MARKER_AGE_MIN}m); not staging or pruning" 4
fi

# In-progress dumps are written as .billing-<ts>.sql.age.tmp, so this glob
# matches completed artifacts only.
shopt -s nullglob
artifacts=("$VOLUME_DIR"/billing-*.sql.age)
shopt -u nullglob
(( ${#artifacts[@]} > 0 )) || fail "no completed artifacts in volume; not staging or pruning" 5

install -d -m 700 -o root -g root "$STAGE_DIR"

staged=0
for artifact in "${artifacts[@]}"; do
    name="$(basename "$artifact")"
    target="${STAGE_DIR}/${name}"
    if [[ -f "$target" ]] && cmp -s "$artifact" "$target"; then
        continue
    fi
    # Stage through a temporary name and rename, so the mirror can never pick
    # up a half-written artifact.
    tmp="${STAGE_DIR}/.${name}.staging"
    install -m 600 -o root -g root "$artifact" "$tmp"
    mv -f "$tmp" "$target"
    staged=$((staged + 1))
done

# Mirror the container's own retention so the off-host copy does not grow
# without bound. Only reached once the source has proved healthy above.
pruned="$(find "$STAGE_DIR" -type f -name 'billing-*.sql.age' -mtime "+${RETENTION_DAYS}" -print -delete | wc -l)"
present="$(find "$STAGE_DIR" -type f -name 'billing-*.sql.age' | wc -l)"

log "OK: staged=${staged} pruned=${pruned} present=${present}"
