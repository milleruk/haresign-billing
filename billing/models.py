"""The billing domain, keyed to Identity organisation UUIDs.

The single largest change from the monolith is what a subscription belongs to.
There, a `Subscription` had a required foreign key to a *user* and an optional,
informational practice/PCN — so "who is paying" was a person and "who gets
access" was worked out at read time from whichever workspace the reader happened
to be in. Here a subscription belongs to a `BillingAccount`, a billing account
belongs to exactly one Identity organisation, and there is no user column at all.
A person's only presence in this schema is as a `BillingContact` UUID reference
and as the actor on an audit row.

Nothing in this module is a foreign key into Identity. Every reference to a user
or an organisation is a bare `UUIDField`. That is the ownership contract made
physical: there is no join that could tempt a later change into reading Identity's
tables, and no CASCADE that could let a change over there delete money state over
here.
"""

from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone


class BillingAccount(models.Model):
    """One organisation's billing, keyed to its permanent Identity UUID.

    ``organization_id`` is unique and is the join key for the whole service. It is
    never reissued and never renumbered, because Identity guarantees the same of
    the UUID it came from.

    ``name`` is a **display copy**, refreshed opportunistically from the OIDC
    session, and it is not authoritative for anything. It exists so an invoice
    screen and an admin list can say which organisation a row is about without a
    call to Identity on every render. Nothing branches on it.
    """

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        # Billing closed by us — the organisation is archived or the relationship
        # ended. A closed account keeps its history and its UUID; it stops being
        # able to hold a live subscription.
        CLOSED = 'closed', 'Closed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization_id = models.UUIDField(unique=True, editable=False)
    # Copies from Identity, for display only. See the class docstring.
    organization_name = models.CharField(max_length=255, blank=True, default='')
    organization_type = models.CharField(max_length=20, blank=True, default='')

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)

    # The provider's customer object. One per billing account, so a second
    # subscription for the same organisation reuses it rather than creating a
    # duplicate customer — the one thing the monolith's `StripeCustomer` got
    # right, moved from the person to the organisation.
    provider = models.CharField(max_length=32, default='stripe')
    provider_customer_id = models.CharField(max_length=255, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['organization_name', 'organization_id']
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'provider_customer_id'],
                condition=~models.Q(provider_customer_id=''),
                name='billing_provider_customer_unique',
            ),
        ]
        indexes = [models.Index(fields=['status'])]

    def __str__(self) -> str:
        return self.organization_name or str(self.organization_id)


class BillingContact(models.Model):
    """Who to reach about this organisation's billing.

    Referenced by Identity user UUID wherever possible. ``email`` is present
    because a billing contact is sometimes a finance mailbox that is not a
    Haresign account — but it is nullable, it is never used for authentication,
    and it is never written into an audit record (the audit scrubber drops any
    key containing "email" for exactly this reason).
    """

    class Role(models.TextChoices):
        PRIMARY = 'primary', 'Primary billing contact'
        FINANCE = 'finance', 'Finance'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(BillingAccount, on_delete=models.CASCADE, related_name='contacts')

    # Preferred form: the person is a Haresign account and we hold only their UUID.
    identity_user_id = models.UUIDField(null=True, blank=True, db_index=True)
    # Fallback for a shared finance mailbox with no Haresign account.
    email = models.EmailField(blank=True, default='')
    display_name = models.CharField(max_length=200, blank=True, default='')

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.PRIMARY)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['role', 'created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'role'], name='billing_contact_account_role_unique'
            ),
            # A contact that identifies nobody is a row that cannot be actioned.
            models.CheckConstraint(
                condition=models.Q(identity_user_id__isnull=False) | ~models.Q(email=''),
                name='billing_contact_identifiable',
            ),
        ]

    def __str__(self) -> str:
        return self.display_name or str(self.identity_user_id or self.email)


class Subscription(models.Model):
    """One provider subscription, normalized.

    ``state`` is *our* vocabulary, mapped from the provider's in
    `providers/mapping.py`. The values happen to match Stripe's today because
    Stripe's are the ones the monolith already stored and a migration that
    renamed them would make the two systems impossible to compare during
    cutover — but the mapping is a function with a test, not an assumption, so a
    second provider or a Stripe vocabulary change is a change in one file.
    """

    class State(models.TextChoices):
        TRIALING = 'trialing', 'Trialing'
        ACTIVE = 'active', 'Active'
        PAST_DUE = 'past_due', 'Past due'
        UNPAID = 'unpaid', 'Unpaid'
        PAUSED = 'paused', 'Paused'
        CANCELED = 'canceled', 'Cancelled'
        INCOMPLETE = 'incomplete', 'Incomplete'
        INCOMPLETE_EXPIRED = 'incomplete_expired', 'Incomplete (expired)'
        # Not a provider state. Recorded when a provider reports a status this
        # service does not recognise, so an unknown state fails closed and is
        # visible, rather than being coerced to something that grants access.
        UNKNOWN = 'unknown', 'Unknown'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        BillingAccount, on_delete=models.PROTECT, related_name='subscriptions'
    )
    plan = models.ForeignKey('catalog.Plan', on_delete=models.PROTECT, related_name='subscriptions')

    state = models.CharField(max_length=32, choices=State.choices, default=State.INCOMPLETE)

    provider = models.CharField(max_length=32, default='stripe')
    provider_subscription_id = models.CharField(max_length=255)
    provider_customer_id = models.CharField(max_length=255, blank=True, default='', db_index=True)

    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    trial_end = models.DateTimeField(null=True, blank=True)
    # Set when the customer has cancelled but the paid period has not ended.
    cancel_at_period_end = models.BooleanField(default=False)
    canceled_at = models.DateTimeField(null=True, blank=True)
    # When the subscription actually stopped granting anything. Distinct from
    # `canceled_at`: a cancellation requested on the 3rd for a period ending on
    # the 30th has two different dates, and quoting the wrong one to a customer
    # is either a refund conversation or a support ticket.
    ended_at = models.DateTimeField(null=True, blank=True)

    # The provider's own monotonic ordering signal for this object. Out-of-order
    # delivery is normal for webhooks, so an event carrying a `sequence` at or
    # below the stored one is recorded and ignored rather than applied. See
    # providers/webhooks.py.
    provider_sequence = models.BigIntegerField(default=0)
    # When the state we hold was last confirmed by the provider — by a webhook or
    # by reconciliation. Read by the UI to say how fresh the answer is, and by
    # reconciliation to find rows nobody has heard about.
    provider_synced_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'provider_subscription_id'],
                name='billing_provider_subscription_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['account', 'state']),
            models.Index(fields=['state', 'current_period_end']),
        ]

    def __str__(self) -> str:
        return f'{self.account} · {self.plan.key} ({self.get_state_display()})'

    @property
    def period_has_lapsed(self) -> bool:
        """True when the paid period we know about has already ended.

        Read alongside `state`, never instead of it. A provider that has gone
        quiet leaves a row saying `active` with a period end in the past, and
        treating that as entitlement is how access outlives payment.
        """
        return bool(self.current_period_end and self.current_period_end <= timezone.now())


class SubscriptionItem(models.Model):
    """One priced line on a subscription.

    The monolith stored a single `stripe_price_id` on the subscription and read
    `items.data[0]` from the provider object — so a subscription with two lines
    silently became a subscription with one, and the second line's plan was lost.
    A subscription grants the union of its items' plans' products.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name='items')
    price = models.ForeignKey(
        'catalog.PlanPrice', on_delete=models.PROTECT, related_name='subscription_items'
    )

    provider_item_id = models.CharField(max_length=255, blank=True, default='')
    quantity = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['subscription', 'price'], name='billing_subscription_price_unique'
            ),
        ]

    def __str__(self) -> str:
        return f'{self.subscription_id} · {self.price}'


class ComplimentaryGrant(models.Model):
    """Time-limited access given without payment, by a platform administrator.

    Carried over from the monolith's `AccessGrant`, and kept deliberately separate
    from `Subscription` for the same reason: "who is actually paying us" must stay
    answerable from the subscription table alone.

    Two of the monolith's three grant targets survive. A grant to a *practice* or
    a *PCN* becomes a grant to that Identity organisation. A grant to an
    individual *user* does not: it granted entitlement to a person regardless of
    organisation, which under an organisation-keyed model has no meaning, and
    reproducing it would be exactly the "a person gets paid access without a
    subscription" shape the ownership contract forbids. See docs/entitlements.md,
    decision D-2 — the migration refuses user-scoped grants rather than guessing.

    Expiry is required. An open-ended grant is indistinguishable from a permission
    bug six months later, so "indefinite" is spelled as a far-future date.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(BillingAccount, on_delete=models.PROTECT, related_name='grants')
    plan = models.ForeignKey('catalog.Plan', on_delete=models.PROTECT, related_name='grants')

    expires_at = models.DateTimeField()
    reason = models.CharField(max_length=255, blank=True, default='')

    granted_by_user_id = models.UUIDField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by_user_id = models.UUIDField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['account', 'revoked_at', 'expires_at'])]

    def __str__(self) -> str:
        return f'{self.account} · {self.plan.key} until {self.expires_at:%Y-%m-%d}'

    @property
    def is_live(self) -> bool:
        return self.revoked_at is None and self.expires_at > timezone.now()


class EntitlementAllocation(models.Model):
    """Who a subscription's entitlement is *for*, as distinct from who pays.

    This is the model Phase 4A did not have, and its absence was the reason a PCN
    subscription's reach had to be inferred from a cached graph at read time.
    Payer and beneficiary are now separate, explicit and stored:

    * a **practice purchase** produces one allocation whose beneficiary is the
      paying practice itself;
    * a **PCN purchase** produces one allocation per organisation the PCN chose —
      the PCN itself, some of its member practices, or both.

    Two consequences follow, and both are the point.

    **A sponsored practice cannot manage what it did not buy.** The subscription
    belongs to the PCN's billing account; the practice holds an allocation. The
    practice's own billing page can say "available through your PCN" and can say
    nothing else about it — no invoice, no payment method, no cancel button — for
    the same reason a tenant cannot cancel their landlord's insurance.

    **Losing the relationship does not cancel the subscription.** When a practice
    leaves a PCN the allocation becomes `INELIGIBLE` and the inherited entitlement
    stops. The subscription is untouched: nothing here cancels, refunds or
    modifies anything at the provider because an organisation chart changed. An
    operational alert is raised for a human to decide, which is the only correct
    place for that decision. See `billing.OperationalAlert`.
    """

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        # The relationship that justified a sponsored allocation is gone. Kept,
        # not deleted: "this PCN used to cover this practice" is the fact a
        # support conversation and a refund decision both start from.
        INELIGIBLE = 'ineligible', 'Ineligible'
        # Deliberately withdrawn by the paying organisation's administrator.
        RELEASED = 'released', 'Released'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name='allocations'
    )
    # The Identity organisation that receives the entitlement. A bare UUID, like
    # every other organisation reference here.
    beneficiary_organization_id = models.UUIDField(db_index=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    status_reason = models.CharField(max_length=255, blank=True, default='')
    status_changed_at = models.DateTimeField(null=True, blank=True)

    # Which organisation-graph version was current when this allocation was made.
    # Recorded so that "was this a reasonable allocation at the time" is
    # answerable later without re-deriving a graph that has since moved on.
    created_under_graph_version = models.CharField(max_length=64, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['beneficiary_organization_id', 'created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['subscription', 'beneficiary_organization_id'],
                name='billing_allocation_unique',
            ),
        ]
        indexes = [
            models.Index(fields=['beneficiary_organization_id', 'status']),
        ]

    def __str__(self) -> str:
        return f'{self.subscription_id} → {self.beneficiary_organization_id} ({self.status})'

    @property
    def is_live(self) -> bool:
        return self.status == self.Status.ACTIVE


class OperationalAlert(models.Model):
    """Something a person needs to decide, that no code may decide for them.

    The alert this model exists for is the one raised when a practice leaves a
    PCN that was paying for it. The entitlement stops immediately — that part is
    mechanical and safe. What happens to the *money* is not: cancelling the PCN's
    subscription would take away something they are still paying for and may still
    want, refunding would be a commercial decision made by a graph sync, and doing
    nothing silently would leave a PCN paying for a practice that left.

    So nothing is done, and this row is raised instead. It carries no amount, no
    payment detail and no person — just enough to find the subscription and the
    two organisations involved.
    """

    class Kind(models.TextChoices):
        # A sponsored allocation lost the relationship that justified it.
        SPONSORSHIP_LAPSED = 'sponsorship_lapsed', 'Sponsorship relationship removed'
        # The organisation-graph projection has aged past its maximum and
        # sponsored entitlements are now failing closed.
        GRAPH_STALE = 'graph_stale', 'Organisation graph is stale'
        # A beneficiary organisation the graph no longer reports at all.
        BENEFICIARY_UNKNOWN = 'beneficiary_unknown', 'Beneficiary organisation not in graph'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=40, choices=Kind.choices, db_index=True)

    # The paying organisation, where there is one.
    organization_id = models.UUIDField(null=True, blank=True, db_index=True)
    beneficiary_organization_id = models.UUIDField(null=True, blank=True, db_index=True)
    subscription = models.ForeignKey(
        Subscription, null=True, blank=True, on_delete=models.SET_NULL, related_name='alerts'
    )

    # A short operational sentence. Never an amount, never a person, never a
    # provider identifier.
    detail = models.CharField(max_length=255, blank=True, default='')

    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by_user_id = models.UUIDField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['kind', 'acknowledged_at'])]

    def __str__(self) -> str:
        return f'{self.get_kind_display()} · {self.beneficiary_organization_id or ""}'

    @property
    def is_open(self) -> bool:
        return self.acknowledged_at is None


class InvoiceReference(models.Model):
    """A pointer to an invoice the provider holds. Not a copy of one.

    Deliberately thin: number, status, totals, and the provider's hosted URL. The
    PDF, the line detail, the customer address and the payment method stay at the
    provider, where they are already stored correctly and where we are not the
    ones who have to keep them safe. The UI links out; it does not render money
    documents from our own copy of the facts.
    """

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        OPEN = 'open', 'Open'
        PAID = 'paid', 'Paid'
        UNCOLLECTIBLE = 'uncollectible', 'Uncollectible'
        VOID = 'void', 'Void'
        UNKNOWN = 'unknown', 'Unknown'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(BillingAccount, on_delete=models.PROTECT, related_name='invoices')
    subscription = models.ForeignKey(
        Subscription, null=True, blank=True, on_delete=models.SET_NULL, related_name='invoices'
    )

    provider = models.CharField(max_length=32, default='stripe')
    provider_invoice_id = models.CharField(max_length=255)
    number = models.CharField(max_length=100, blank=True, default='')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UNKNOWN)

    total_minor = models.IntegerField(default=0)
    currency = models.CharField(max_length=3, default='GBP')
    issued_at = models.DateTimeField(null=True, blank=True)

    # The provider's own hosted page. Stored rather than constructed: these are
    # signed, provider-controlled URLs and building one by string concatenation
    # is how a link starts 404ing after a provider change nobody was told about.
    hosted_url = models.URLField(max_length=1000, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-issued_at', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'provider_invoice_id'],
                name='billing_provider_invoice_unique',
            ),
        ]

    def __str__(self) -> str:
        return self.number or self.provider_invoice_id
