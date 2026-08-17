"""Allocations: who a subscription is for, and what happens when that changes.

The payer/beneficiary split lives here. `billing/services.py` owns subscription
state; this module owns the question of *which organisations* a subscription's
entitlement reaches, and the consequences of that set changing underneath it.

The rule that shapes everything below: **a relationship change never touches
money.** When a practice leaves a PCN the inherited entitlement stops, because
the justification for it has gone. The PCN's subscription is not cancelled, not
refunded, not modified, and no provider call is made. An operational alert is
raised so a person decides what happens to the money, and that is the only
correct place for the decision — the alternative is a graph sync issuing refunds.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from audit import events
from audit.services import record

from .models import EntitlementAllocation, OperationalAlert, Subscription

logger = logging.getLogger('haresign.billing')


class AllocationRefused(RuntimeError):
    """An allocation that must not be created. The message is safe to show an admin."""


def allocate(
    *,
    subscription: Subscription,
    beneficiary_organization_id,
    graph_version: str = '',
    request=None,
) -> EntitlementAllocation:
    """Point a subscription's entitlement at one beneficiary organisation.

    Callers are responsible for having checked that the sponsorship is *allowed* —
    `identity.graph.sponsorship_is_valid` — before calling. This function records
    the decision and refuses only the things that are wrong regardless of the
    graph.
    """
    payer = str(subscription.account.organization_id)
    beneficiary = str(beneficiary_organization_id)

    if payer != beneficiary and not subscription.plan.covers_member_organizations:
        # The plan is not one that reaches beyond the organisation that bought it.
        # Allowing this would make every plan a sponsoring plan by accident.
        raise AllocationRefused(
            'This plan does not extend to other organisations, so it cannot be allocated to one.'
        )

    allocation, created = EntitlementAllocation.objects.get_or_create(
        subscription=subscription,
        beneficiary_organization_id=beneficiary,
        defaults={
            'status': EntitlementAllocation.Status.ACTIVE,
            'created_under_graph_version': graph_version,
        },
    )
    if not created and allocation.status != EntitlementAllocation.Status.ACTIVE:
        # Re-allocating something previously withdrawn or lapsed is a legitimate
        # act — a practice rejoins its PCN — and it revives the row rather than
        # creating a second one, so the history stays in one place.
        allocation.status = EntitlementAllocation.Status.ACTIVE
        allocation.status_reason = ''
        allocation.status_changed_at = timezone.now()
        allocation.created_under_graph_version = graph_version
        allocation.save(
            update_fields=[
                'status',
                'status_reason',
                'status_changed_at',
                'created_under_graph_version',
                'updated_at',
            ]
        )
        created = True

    if created:
        record(
            events.ALLOCATION_CREATED,
            request=request,
            organization_id=subscription.account.organization_id,
            metadata={
                'plan': subscription.plan.key,
                # Whether it is sponsored is the interesting fact; the beneficiary
                # UUID is on the row itself and does not need repeating into the
                # audit metadata.
                'sponsored': payer != beneficiary,
                'graph_version': graph_version,
            },
        )
    return allocation


def release(
    allocation: EntitlementAllocation, *, reason: str = '', actor_user_id=None, request=None
) -> EntitlementAllocation:
    """Withdraw an allocation deliberately, by the paying administrator.

    Distinct from lapsing. "The PCN decided to stop covering this practice" and
    "the practice left the PCN" are different facts with different conversations
    attached, and collapsing them into one status would lose the difference at
    exactly the moment somebody asks.
    """
    if allocation.status == EntitlementAllocation.Status.RELEASED:
        return allocation

    allocation.status = EntitlementAllocation.Status.RELEASED
    allocation.status_reason = reason[:255]
    allocation.status_changed_at = timezone.now()
    allocation.save(update_fields=['status', 'status_reason', 'status_changed_at', 'updated_at'])
    record(
        events.ALLOCATION_RELEASED,
        request=request,
        organization_id=allocation.subscription.account.organization_id,
        actor_user_id=actor_user_id,
        metadata={'plan': allocation.subscription.plan.key, 'reason': reason[:255]},
    )
    return allocation


@transaction.atomic
def invalidate_lapsed_allocations(*, removed_edges: set, graph, request=None) -> int:
    """Mark sponsored allocations ineligible where the relationship has gone.

    Called from the graph refresh, with the exact set of edges that disappeared
    between the previous projection and this one.

    **Nothing here calls a provider.** Not cancel, not refund, not modify. The
    subscription is left exactly as it is and an `OperationalAlert` is raised
    instead. A billing system that cancelled a customer's subscription because
    somebody edited an organisation chart would be a billing system nobody could
    trust with a card.
    """
    if not removed_edges:
        return 0

    # `removed_edges` is {(parent, child)}. A sponsored allocation is at risk when
    # its payer is the parent and its beneficiary is the child.
    affected = 0
    for parent, child in sorted(removed_edges):
        allocations = EntitlementAllocation.objects.select_related(
            'subscription__account', 'subscription__plan'
        ).filter(
            beneficiary_organization_id=child,
            status=EntitlementAllocation.Status.ACTIVE,
            subscription__account__organization_id=parent,
        )
        for allocation in allocations:
            allocation.status = EntitlementAllocation.Status.INELIGIBLE
            allocation.status_reason = 'The sponsoring relationship was removed.'
            allocation.status_changed_at = timezone.now()
            allocation.save(
                update_fields=['status', 'status_reason', 'status_changed_at', 'updated_at']
            )

            OperationalAlert.objects.create(
                kind=OperationalAlert.Kind.SPONSORSHIP_LAPSED,
                organization_id=parent,
                beneficiary_organization_id=child,
                subscription=allocation.subscription,
                detail=(
                    'A sponsored allocation lapsed because the organisation '
                    'relationship was removed. The subscription has not been '
                    'changed; decide whether it should be.'
                ),
            )
            record(
                events.ALLOCATION_LAPSED,
                request=request,
                organization_id=allocation.subscription.account.organization_id,
                metadata={
                    'plan': allocation.subscription.plan.key,
                    'graph_version': graph.graph_version,
                    # Stated explicitly in the audit trail, because the absence of
                    # a provider call is the property being asserted.
                    'provider_action': 'none',
                },
            )
            affected += 1

    if affected:
        logger.info(
            'sponsorship: %d allocation(s) lapsed under graph %s; no provider calls made',
            affected,
            graph.graph_version,
        )
    return affected


def live_allocations_for(beneficiary_organization_id):
    """Every active allocation pointing at this organisation, payer included.

    Deterministically ordered, because the entitlement derivation reads it and an
    entitlement answer whose `source` depends on row order is an answer that
    changes without anything changing.
    """
    return (
        EntitlementAllocation.objects.filter(
            beneficiary_organization_id=beneficiary_organization_id,
            status=EntitlementAllocation.Status.ACTIVE,
        )
        .select_related('subscription', 'subscription__account', 'subscription__plan')
        .order_by('subscription__created_at', 'subscription_id')
    )


def sponsored_allocations_by(payer_organization_id):
    """Allocations this organisation pays for that benefit somebody else."""
    return (
        EntitlementAllocation.objects.filter(
            subscription__account__organization_id=payer_organization_id
        )
        .exclude(beneficiary_organization_id=payer_organization_id)
        .select_related('subscription', 'subscription__plan')
        .order_by('beneficiary_organization_id')
    )
