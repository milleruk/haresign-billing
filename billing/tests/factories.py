"""Fixtures shared across the suite.

Deliberately explicit rather than a factory library: every test in this repository
is about a boundary or a state machine, and a fixture that quietly fills in a
plausible default is a fixture that can hide the very thing under test.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.utils import timezone

from billing.models import BillingAccount, EntitlementAllocation, Subscription, SubscriptionItem
from catalog.models import Plan, PlanPrice
from identity.models import IdentityUser, SessionMembership


def organization_id() -> uuid.UUID:
    return uuid.uuid4()


def account(*, organization=None, name='Test Practice', org_type='practice') -> BillingAccount:
    return BillingAccount.objects.create(
        organization_id=organization or organization_id(),
        organization_name=name,
        organization_type=org_type,
    )


def plan(key='practice') -> Plan:
    return Plan.objects.get(key=key)


def price(plan_key='practice', interval='month', provider_price_id='') -> PlanPrice:
    row = PlanPrice.objects.get(plan__key=plan_key, interval=interval)
    if provider_price_id and row.provider_price_id != provider_price_id:
        row.provider_price_id = provider_price_id
        row.save(update_fields=['provider_price_id'])
    return row


# A sentinel distinct from None, because `period_end=None` is a meaningful value
# here — "the provider has given us no end date" — and must not collapse into the
# default the way a plain `None` default would.
UNSET = object()


def subscription(
    *,
    account_obj=None,
    plan_key='practice',
    state=Subscription.State.ACTIVE,
    period_end=UNSET,
    cancel_at_period_end=False,
    provider_subscription_id='',
    with_item=True,
    with_allocation=True,
) -> Subscription:
    account_obj = account_obj or account()
    plan_obj = plan(plan_key)
    row = Subscription.objects.create(
        account=account_obj,
        plan=plan_obj,
        state=state,
        provider='fake',
        provider_subscription_id=provider_subscription_id or f'sub_{uuid.uuid4().hex[:12]}',
        current_period_end=(
            timezone.now() + timedelta(days=30) if period_end is UNSET else period_end
        ),
        cancel_at_period_end=cancel_at_period_end,
    )
    if with_item:
        SubscriptionItem.objects.create(
            subscription=row, price=price(plan_key, 'month'), quantity=1
        )
    if with_allocation:
        # A subscription with no allocation entitles nobody, which is correct but
        # is almost never what a test means. Tests about allocation itself pass
        # `with_allocation=False` and say so.
        allocate(row)
    return row


def graph(*, edges=(), organizations=(), generated_at=None, source='identity_api'):
    """Install one organisation-graph projection as the current one.

    Tests that involve a PCN need a graph, because sponsored entitlement fails
    closed without one — which is the behaviour, not an inconvenience to work
    around. `generated_at` is settable so a test can age a projection past its
    maximum and assert that it closes.
    """
    from identity.graph_models import GraphOrganization, GraphRelationship, OrganizationGraph

    edges = [(str(parent), str(child)) for parent, child in edges]
    known = {org_id for edge in edges for org_id in edge}
    known.update(str(org_id) for org_id, _ in organizations)

    generated_at = generated_at or timezone.now()
    OrganizationGraph.objects.filter(is_current=True).update(is_current=False)
    row = OrganizationGraph.objects.create(
        graph_version=uuid.uuid4().hex[:32],
        schema_version=1,
        source=source,
        generated_at=generated_at,
        is_current=True,
        organization_count=len(known),
        relationship_count=len(edges),
    )
    types = {str(org_id): org_type for org_id, org_type in organizations}
    for org_id in sorted(known):
        GraphOrganization.objects.create(
            graph=row,
            organization_id=org_id,
            organization_type=types.get(org_id, ''),
            is_active=True,
        )
    for parent, child in edges:
        GraphRelationship.objects.create(
            graph=row, parent_organization_id=parent, child_organization_id=child
        )
    return row


def allocate(subscription, beneficiary=None) -> EntitlementAllocation:
    """Point a subscription at a beneficiary. Defaults to the payer itself."""
    return EntitlementAllocation.objects.create(
        subscription=subscription,
        beneficiary_organization_id=beneficiary or subscription.account.organization_id,
    )


def identity_user(*, name='Test Person') -> IdentityUser:
    """A signed-in person.

    There is deliberately no `platform_admin=` argument any more. The flag it set
    is gone from the model, and a fixture that still offered it would let a test
    look as though it were exercising a bypass that no longer exists.
    """
    return IdentityUser.objects.create_user(identity_user_id=uuid.uuid4(), display_name=name)


# Identity's real organisation-administrator role key, dotted and namespaced,
# exactly as `haresign-core/organizations/roles.py` defines it. Phase 4A's
# fixtures used `organization_admin`, which matches nothing Identity emits.
ADMIN_ROLE = 'organization.admin'


def sign_in(client, user, memberships=()):
    """Establish a session the way the OIDC callback does.

    Deliberately mirrors `identity.views` rather than calling it: the tests that
    exercise the OIDC flow itself are in `identity/tests/`, and every *other* test
    needs a signed-in session without standing up a provider.
    """
    client.force_login(user, backend='identity.backends.IdentityOIDCBackend')
    session_key = client.session.session_key
    for entry in memberships:
        SessionMembership.objects.create(
            session_key=session_key,
            user=user,
            organization_id=entry['organization_id'],
            organization_name=entry.get('name', ''),
            organization_type=entry.get('type', 'practice'),
            role=entry.get('role', ADMIN_ROLE),
            is_administrator=entry.get('role', ADMIN_ROLE) == ADMIN_ROLE,
            captured_at=entry.get('captured_at', timezone.now()),
        )
    return client
