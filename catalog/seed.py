"""The initial catalogue, transcribed from the monolith's `billing/catalog.py`.

Nothing here is invented. Product keys, plan keys, scopes, the plan→product
mapping and the displayed amounts are exactly what the live monolith offers
today, so a migrated subscription lands on a plan that grants the same tools.

Two things the monolith has that are deliberately *not* transcribed:

* Stripe price ids. They live in the monolith's environment, this repository must
  not read them, and a `PlanPrice` with no `provider_price_id` is displayable but
  not purchasable — which is the correct state before cutover.
* The staff/superuser bypass, which granted every entitlement to any `is_staff`
  account. That is a *role* creating an *entitlement*, which the ownership
  contract forbids. See docs/entitlements.md, decision D-1.

This module is data read by a migration. Editing it changes nothing already
deployed; write a new migration.
"""

from __future__ import annotations

# --- Products -----------------------------------------------------------------
# Keys match the monolith's entitlement strings exactly (`ENT_PRO_TOOLS` was the
# *constant* name; 'pro_tools' was the value, and the value is the contract).

PRO_TOOLS = 'pro_tools'
PRACTICE_DASHBOARDS = 'practice_dashboards'
PCN_DASHBOARDS = 'pcn_dashboards'

PRODUCTS = [
    {
        'key': PRO_TOOLS,
        'name': 'Premium tools',
        'description': (
            'Practice Stock Take, Vaccine Campaign Planner, IPC Audit, CQC Readiness '
            'Checker and Returning to Good & Outstanding.'
        ),
    },
    {
        'key': PRACTICE_DASHBOARDS,
        'name': 'Practice dashboards',
        'description': 'Full practice-level benchmarking dashboards.',
    },
    {
        'key': PCN_DASHBOARDS,
        'name': 'PCN dashboards',
        'description': 'Full PCN-level benchmarking dashboards.',
    },
]


# --- Plans --------------------------------------------------------------------
# `covers_member_organizations` on the PCN plan encodes the monolith's rule that
# "a PCN subscription covers its member practices". The monolith could read that
# from `Practice.pcn_id` in its own database; Billing cannot, so the flag is the
# switch; which organisations it actually reaches is recorded per beneficiary
# in `billing.EntitlementAllocation` and confirmed against Identity's graph.

PLANS = [
    {
        'key': 'practice',
        'name': 'Practice',
        'scope': 'practice',
        'best_for': 'A single GP practice',
        'products': [PRO_TOOLS, PRACTICE_DASHBOARDS],
        'covers_member_organizations': False,
        'prices': [
            {'interval': 'month', 'amount_minor': 1000, 'currency': 'GBP'},
            {'interval': 'year', 'amount_minor': 11000, 'currency': 'GBP'},
        ],
    },
    {
        'key': 'pcn',
        'name': 'PCN',
        'scope': 'pcn',
        'best_for': 'A Primary Care Network and its member practices',
        'products': [PRO_TOOLS, PRACTICE_DASHBOARDS, PCN_DASHBOARDS],
        'covers_member_organizations': True,
        'prices': [
            {'interval': 'month', 'amount_minor': 4900, 'currency': 'GBP'},
            {'interval': 'year', 'amount_minor': 49000, 'currency': 'GBP'},
        ],
    },
]


def apply(apps, schema_editor=None):
    """Idempotently create or update the catalogue. Safe to run repeatedly.

    Matched on the stable ``key``, so re-running rewrites names and descriptions
    in place and never orphans a subscription. Nothing is ever deleted here —
    retiring a plan is `is_active=False`, applied deliberately.
    """
    Product = apps.get_model('catalog', 'Product')
    Plan = apps.get_model('catalog', 'Plan')
    PlanPrice = apps.get_model('catalog', 'PlanPrice')

    products = {}
    for spec in PRODUCTS:
        product, _ = Product.objects.update_or_create(
            key=spec['key'],
            defaults={'name': spec['name'], 'description': spec['description']},
        )
        products[spec['key']] = product

    for spec in PLANS:
        plan, _ = Plan.objects.update_or_create(
            key=spec['key'],
            defaults={
                'name': spec['name'],
                'scope': spec['scope'],
                'best_for': spec['best_for'],
                'covers_member_organizations': spec['covers_member_organizations'],
            },
        )
        plan.products.set([products[key] for key in spec['products']])

        for price in spec['prices']:
            # `provider_price_id` is deliberately absent from `defaults`: a
            # re-seed must never blank a price reference an operator has set.
            PlanPrice.objects.update_or_create(
                plan=plan,
                interval=price['interval'],
                provider='stripe',
                defaults={
                    'amount_minor': price['amount_minor'],
                    'currency': price['currency'],
                },
            )
