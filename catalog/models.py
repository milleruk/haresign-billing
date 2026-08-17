"""The sellable surface: products, plans, and provider price references.

Three ideas that the monolith kept in one Python dictionary and which are
deliberately separated here.

A **Product** is a capability a customer can hold — "the premium tool bundle",
"practice benchmarking dashboards". Its ``key`` is the stable internal identifier
Haresign Intelligence asks for over the entitlement API. Once shipped, a product
key never changes; a rename changes ``name``, never ``key``.

A **Plan** is a thing a customer subscribes to. It grants a set of products. It
is not a price — a plan offered monthly and annually is one plan, so a customer
switching interval keeps their plan and their entitlements.

A **PlanPrice** is one interval of one plan, carrying the *provider's* price
reference. This is the row the monolith did not have: it kept price ids in
environment variables keyed by plan and interval, which meant switching from test
to live prices was an environment change with no record of what had been sold at
which price, and a subscription's price id could not be resolved back to a plan
without re-reading the environment.

Rows here are seeded by idempotent data migrations keyed on the stable ``key``.
Editing ``catalog/seed.py`` alone changes nothing already deployed.
"""

from __future__ import annotations

import uuid

from django.core.validators import MinValueValidator, RegexValidator
from django.db import models

# Product and plan keys are lowercase, ASCII, and stable forever. Constrained at
# the field level rather than by convention: a key with a space or a capital in it
# reaches Intelligence's configuration and a customer's entitlements, and it can
# never be corrected afterwards without breaking both.
KEY_VALIDATOR = RegexValidator(
    r'^[a-z][a-z0-9_]{1,62}$',
    'Keys are lowercase ASCII, start with a letter, and use only letters, digits and underscores.',
)


class Product(models.Model):
    """A capability a subscription can grant.

    The unit the entitlement API speaks in. Deliberately not called "feature" or
    "entitlement": an entitlement is the *derived answer* about one organisation
    and one product at one moment, and conflating the catalogue row with the
    derived answer is how a system ends up with a writable ``is_entitled`` column.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The contract with Haresign Intelligence. Immutable once shipped.
    key = models.CharField(max_length=64, unique=True, validators=[KEY_VALIDATOR])
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')

    # A retired product stops being sold but keeps answering the entitlement API
    # for anyone who still holds it. Deleting the row would silently revoke live
    # access, which is exactly the failure mode a catalogue must not have.
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['key']

    def __str__(self) -> str:
        return f'{self.name} ({self.key})'


class Plan(models.Model):
    """Something a customer subscribes to. Grants products; carries no price."""

    class Scope(models.TextChoices):
        # Which kind of Identity organisation the plan is sold for. Informational
        # for entitlement purposes — the gate is product-based — but it is what
        # stops a PCN plan being offered to a single practice in the UI.
        PRACTICE = 'practice', 'GP practice'
        PCN = 'pcn', 'Primary Care Network'
        CLIENT = 'client', 'Consulting client'
        INTERNAL = 'internal', 'Internal'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    key = models.CharField(max_length=64, unique=True, validators=[KEY_VALIDATOR])
    name = models.CharField(max_length=200)
    scope = models.CharField(max_length=20, choices=Scope.choices, db_index=True)
    best_for = models.CharField(max_length=255, blank=True, default='')

    products = models.ManyToManyField(Product, related_name='plans', blank=True)

    # Whether a subscription on this plan may be *allocated* to an organisation
    # other than the one paying for it — the monolith's rule that a PCN plan
    # covers its member practices, made explicit.
    #
    # It is permission, not reach. Which practices a PCN's subscription actually
    # entitles is recorded per beneficiary in `billing.EntitlementAllocation`, and
    # each sponsored allocation is honoured only while Identity's organisation
    # graph confirms the relationship. See docs/entitlements.md, decision D-4.
    covers_member_organizations = models.BooleanField(default=False)

    # A retired plan stops being sold. Live subscriptions on it keep working and
    # keep granting their products, for the same reason a product is retired
    # rather than deleted.
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['key']

    def __str__(self) -> str:
        return f'{self.name} ({self.key})'

    @property
    def product_keys(self) -> set[str]:
        return {product.key for product in self.products.all()}


class PlanPrice(models.Model):
    """One interval of one plan, and the provider's reference for it.

    ``provider_price_id`` is the only provider identifier in the catalogue, and it
    is stored rather than configured so that a subscription's price can always be
    resolved back to a plan — including for a price that has since been retired
    and would no longer appear in any environment variable.
    """

    class Interval(models.TextChoices):
        MONTH = 'month', 'Monthly'
        YEAR = 'year', 'Annual'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name='prices')
    interval = models.CharField(max_length=16, choices=Interval.choices)

    # Minor units (pence), because money is never a float. Stored so the plan
    # page renders without a provider round-trip; the provider remains the
    # authority for what is actually charged, and reconciliation reports a
    # mismatch rather than silently trusting either side.
    amount_minor = models.PositiveIntegerField(validators=[MinValueValidator(0)])
    currency = models.CharField(max_length=3, default='GBP')

    provider = models.CharField(max_length=32, default='stripe', db_index=True)
    # Blank until a price exists at the provider. A price with no provider
    # reference is displayable but not purchasable, which is exactly the state
    # every price is in during this phase.
    provider_price_id = models.CharField(max_length=255, blank=True, default='')

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['plan__key', 'interval']
        constraints = [
            # One live price per plan per interval per provider. Two would make
            # "what does this plan cost monthly" ambiguous at the moment somebody
            # is being charged.
            models.UniqueConstraint(
                fields=['plan', 'interval', 'provider'],
                name='catalog_plan_interval_provider_unique',
            ),
            # A provider price id maps to exactly one row, so a webhook naming a
            # price resolves to one plan and never to two.
            models.UniqueConstraint(
                fields=['provider', 'provider_price_id'],
                condition=~models.Q(provider_price_id=''),
                name='catalog_provider_price_unique',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.plan.key} · {self.get_interval_display()}'

    @property
    def is_purchasable(self) -> bool:
        return self.is_active and bool(self.provider_price_id)

    @property
    def display_amount(self) -> str:
        symbol = {'GBP': '£', 'EUR': '€', 'USD': '$'}.get(self.currency, '')
        major, minor = divmod(self.amount_minor, 100)
        return f'{symbol}{major}' if minor == 0 else f'{symbol}{major}.{minor:02d}'
