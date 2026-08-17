"""Stable audit event keys.

Dotted, past tense, and never renamed once shipped: a support query written
against ``subscription.state.changed`` must keep working after the code that
emits it has been refactored twice.
"""

# --- Identity / session -------------------------------------------------------
SESSION_STARTED = 'session.started'
SESSION_ENDED = 'session.ended'
SESSION_REJECTED = 'session.rejected'
# The relying-party half of OIDC failed validation — bad issuer, bad audience,
# bad signature, replayed state, missing nonce. Recorded because a sudden run of
# these is either a broken deployment or somebody probing the callback.
OIDC_VALIDATION_FAILED = 'oidc.validation.failed'

# --- Authorization ------------------------------------------------------------
ACCESS_GRANTED = 'access.granted'
ACCESS_REFUSED = 'access.refused'
# Retained, never emitted. Phase 4A had a platform-administrator support bypass
# into any organisation's billing; Phase 4B removed it, because a role granting
# billing access is the shape the ownership contract forbids. The key stays
# because audit keys are a contract and a support query written against it must
# not start erroring — it simply matches nothing new. `billing/services.py` still
# marks complimentary-grant rows with the `support_access` flag, which is a
# different and still-live fact.
SUPPORT_ACCESS_USED = 'access.support.used'

# --- Organisation graph -------------------------------------------------------
# Billing's held projection of Identity's organisation graph.
GRAPH_REFRESHED = 'organization_graph.refreshed'
GRAPH_REFRESH_FAILED = 'organization_graph.refresh_failed'
# The set of containment edges moved between two versions. Counts only: naming
# the organisations would put the estate's structure into the audit trail on
# every reorganisation.
GRAPH_RELATIONSHIPS_CHANGED = 'organization_graph.relationships_changed'

# --- Entitlement allocations --------------------------------------------------
# Who a subscription is *for*, as distinct from who pays for it.
ALLOCATION_CREATED = 'allocation.created'
# Withdrawn deliberately by the paying organisation's administrator.
ALLOCATION_RELEASED = 'allocation.released'
# The relationship that justified a sponsored allocation was removed. Carries
# `provider_action: none` explicitly, because the absence of a provider call is
# the property being asserted.
ALLOCATION_LAPSED = 'allocation.lapsed'

# --- Checkout and portal ------------------------------------------------------
CHECKOUT_STARTED = 'checkout.started'
CHECKOUT_REFUSED = 'checkout.refused'
PORTAL_OPENED = 'portal.opened'
PORTAL_REFUSED = 'portal.refused'

# --- Billing domain -----------------------------------------------------------
BILLING_ACCOUNT_CREATED = 'billing_account.created'
BILLING_CONTACT_CHANGED = 'billing_contact.changed'
SUBSCRIPTION_CREATED = 'subscription.created'
SUBSCRIPTION_STATE_CHANGED = 'subscription.state.changed'
SUBSCRIPTION_ITEMS_CHANGED = 'subscription.items.changed'
COMPLIMENTARY_GRANT_CREATED = 'complimentary_grant.created'
COMPLIMENTARY_GRANT_REVOKED = 'complimentary_grant.revoked'

# --- Provider -----------------------------------------------------------------
WEBHOOK_RECEIVED = 'provider.webhook.received'
WEBHOOK_REJECTED = 'provider.webhook.rejected'
WEBHOOK_REPLAYED = 'provider.webhook.replayed'
WEBHOOK_OUT_OF_ORDER = 'provider.webhook.out_of_order'
WEBHOOK_FAILED = 'provider.webhook.failed'
RECONCILIATION_RUN = 'provider.reconciliation.run'

# --- Entitlement API ----------------------------------------------------------
ENTITLEMENT_QUERIED = 'entitlement.queried'
ENTITLEMENT_API_REJECTED = 'entitlement.api.rejected'

# --- Migration ----------------------------------------------------------------
MIGRATION_RUN_STARTED = 'migration.run.started'
MIGRATION_RUN_COMPLETED = 'migration.run.completed'
MIGRATION_RUN_CONFLICT = 'migration.run.conflict'
MIGRATION_RUN_FAILED = 'migration.run.failed'
