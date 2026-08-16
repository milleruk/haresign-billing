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
# A platform administrator opened an organisation's billing without holding a
# membership in it. Always recorded, never silent — this is the event the
# support-access policy exists to produce.
SUPPORT_ACCESS_USED = 'access.support.used'

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
