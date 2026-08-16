"""Provider vocabulary → this service's normalized vocabulary.

A function with a test, not an assumption. The values coincide with Stripe's
today because Stripe's are the ones the monolith already stored and renaming them
during a cutover would make the two systems impossible to compare — but the
mapping being explicit is what makes a second provider, or a Stripe vocabulary
change, a change in one file.

The important half is the **default**. An unrecognised provider status maps to
`UNKNOWN`, which is in `NON_GRANTING_STATES`, so a status this service has never
seen fails closed and is visible in the UI as "Unknown" rather than being coerced
into something that grants access.
"""

from __future__ import annotations

from billing.models import Subscription

STRIPE_SUBSCRIPTION_STATES = {
    'trialing': Subscription.State.TRIALING,
    'active': Subscription.State.ACTIVE,
    'past_due': Subscription.State.PAST_DUE,
    'unpaid': Subscription.State.UNPAID,
    'paused': Subscription.State.PAUSED,
    'canceled': Subscription.State.CANCELED,
    'incomplete': Subscription.State.INCOMPLETE,
    'incomplete_expired': Subscription.State.INCOMPLETE_EXPIRED,
}

STRIPE_INVOICE_STATES = {
    'draft': 'draft',
    'open': 'open',
    'paid': 'paid',
    'uncollectible': 'uncollectible',
    'void': 'void',
}


def subscription_state(provider_status: str) -> str:
    """Normalize a provider subscription status. Unknown → UNKNOWN, never a guess."""
    return STRIPE_SUBSCRIPTION_STATES.get(
        (provider_status or '').strip().lower(), Subscription.State.UNKNOWN
    )


def invoice_status(provider_status: str) -> str:
    return STRIPE_INVOICE_STATES.get((provider_status or '').strip().lower(), 'unknown')
