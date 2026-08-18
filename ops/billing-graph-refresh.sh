#!/usr/bin/env bash
# Refresh Billing's projection of Identity's organisation graph.
#
# Installed at /usr/local/bin/billing-graph-refresh.sh, root:root, mode 700,
# run from root cron every 10 minutes — comfortably inside IDENTITY_GRAPH_MAX_AGE
# (one hour), so a single failed refresh closes nothing on its own.
#
# The command is idempotent and reads only: it fetches Identity's graph document,
# refuses one it cannot validate, and applies a new version only when the content
# digest has moved. An unchanged estate produces no write at all.
#
# It does **not** currently make a stale projection fresh — Identity answers an
# unchanged estate with "your version is still current" and Billing declines to
# re-stamp the document's age. See docs/stripe-cutover.md, "The projection that
# cannot become fresh". This schedule is what applies a change promptly when one
# happens; the freshness question is an open decision, not something cron fixes.
set -Eeuo pipefail

CONTAINER="HaresignBilling"
LOG_FILE="/var/log/billing-graph-refresh.log"

umask 077

log() { echo "[$(date '+%F %T')] $*" >>"$LOG_FILE"; }

# A container that is not running is not an error worth a mail every ten minutes
# during a deployment; it is a fact worth a log line.
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
    log "SKIP: ${CONTAINER} is not running"
    exit 0
fi

if output=$(docker exec "$CONTAINER" python manage.py refresh_organization_graph 2>&1); then
    # Counts and a version digest. No organisation name, no membership, no person.
    log "OK: ${output}"
else
    status=$?
    log "FAIL(${status}): ${output}"
    exit "$status"
fi
