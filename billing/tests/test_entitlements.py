"""Entitlement derivation.

The state table is the commercial contract made executable, so every state gets a
test — including, and especially, the ones that must grant nothing. A test that
only checks the happy path leaves the entire fail-closed property unverified.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from billing.entitlements import (
    GRANTING_STATES,
    NON_GRANTING_STATES,
    entitlements_for_organization,
    organization_holds,
    subscription_grants,
)
from billing.models import BillingAccount, ComplimentaryGrant, Subscription
from catalog.models import Product
from catalog.seed import PCN_DASHBOARDS, PRACTICE_DASHBOARDS, PRO_TOOLS

from . import factories


class StateTableTests(TestCase):
    """Every declared state has a decided meaning."""

    def test_every_state_is_classified(self):
        declared = {value for value, _ in Subscription.State.choices}
        classified = set(GRANTING_STATES) | set(NON_GRANTING_STATES)
        self.assertEqual(
            declared,
            classified,
            'A subscription state exists that neither grants nor refuses. Adding a '
            'state without deciding what it means is exactly what this asserts against.',
        )

    def test_granting_and_non_granting_do_not_overlap(self):
        self.assertEqual(set(GRANTING_STATES) & set(NON_GRANTING_STATES), set())

    def test_only_active_and_trialing_grant(self):
        # Transcribed from the monolith's ACCESS_STATUSES. If this changes,
        # somebody has made a commercial decision and it belongs in
        # docs/entitlements.md, not in a diff.
        self.assertEqual(
            set(GRANTING_STATES), {Subscription.State.ACTIVE, Subscription.State.TRIALING}
        )


class SubscriptionGrantsTests(TestCase):
    def test_active_within_period_grants(self):
        sub = factories.subscription(state=Subscription.State.ACTIVE)
        self.assertTrue(subscription_grants(sub))

    def test_trialing_grants(self):
        sub = factories.subscription(state=Subscription.State.TRIALING)
        self.assertTrue(subscription_grants(sub))

    def test_active_with_lapsed_period_does_not_grant(self):
        """A provider that went quiet must not become permanent free access."""
        sub = factories.subscription(
            state=Subscription.State.ACTIVE, period_end=timezone.now() - timedelta(days=1)
        )
        self.assertFalse(subscription_grants(sub))

    def test_active_with_no_known_period_end_grants(self):
        """'No end date supplied' is not 'the period has ended'."""
        sub = factories.subscription(state=Subscription.State.ACTIVE, period_end=None)
        self.assertTrue(subscription_grants(sub))

    def test_cancel_at_period_end_still_grants_until_the_period_ends(self):
        """The customer paid for the rest of the period. Ending it early is theft."""
        sub = factories.subscription(
            state=Subscription.State.ACTIVE,
            cancel_at_period_end=True,
            period_end=timezone.now() + timedelta(days=10),
        )
        self.assertTrue(subscription_grants(sub))

    def test_no_grace_period_on_past_due(self):
        """The monolith defines none, so this service invents none. See D-3."""
        sub = factories.subscription(state=Subscription.State.PAST_DUE)
        self.assertFalse(subscription_grants(sub))

    def test_every_non_granting_state_refuses(self):
        for state in NON_GRANTING_STATES:
            with self.subTest(state=state):
                sub = factories.subscription(state=state)
                self.assertFalse(subscription_grants(sub))

    def test_unknown_provider_state_fails_closed(self):
        sub = factories.subscription(state=Subscription.State.UNKNOWN)
        self.assertFalse(subscription_grants(sub))


class OrganizationEntitlementTests(TestCase):
    def test_no_billing_account_holds_nothing(self):
        result = entitlements_for_organization(factories.organization_id())
        self.assertEqual(result.entitled_keys, [])
        # Still answers for every product, so a consumer gets an explicit False.
        self.assertEqual(len(result.products), Product.objects.filter(is_active=True).count())

    def test_active_practice_subscription_grants_its_plan_products(self):
        account = factories.account()
        factories.subscription(account_obj=account, plan_key='practice')
        result = entitlements_for_organization(account.organization_id)
        self.assertEqual(result.entitled_keys, sorted([PRO_TOOLS, PRACTICE_DASHBOARDS]))
        self.assertFalse(result.holds(PCN_DASHBOARDS))

    def test_source_is_reported(self):
        account = factories.account()
        factories.subscription(account_obj=account)
        result = entitlements_for_organization(account.organization_id)
        self.assertEqual(result.products[PRO_TOOLS].source, 'subscription')

    def test_closed_billing_account_grants_nothing(self):
        account = factories.account()
        factories.subscription(account_obj=account)
        account.status = BillingAccount.Status.CLOSED
        account.save(update_fields=['status'])
        self.assertEqual(entitlements_for_organization(account.organization_id).entitled_keys, [])

    def test_complimentary_grant_entitles_without_a_subscription(self):
        account = factories.account()
        ComplimentaryGrant.objects.create(
            account=account,
            plan=factories.plan('practice'),
            expires_at=timezone.now() + timedelta(days=14),
        )
        result = entitlements_for_organization(account.organization_id)
        self.assertTrue(result.holds(PRO_TOOLS))
        self.assertEqual(result.products[PRO_TOOLS].source, 'complimentary_grant')

    def test_expired_grant_entitles_nothing(self):
        account = factories.account()
        ComplimentaryGrant.objects.create(
            account=account,
            plan=factories.plan('practice'),
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.assertEqual(entitlements_for_organization(account.organization_id).entitled_keys, [])

    def test_revoked_grant_entitles_nothing(self):
        account = factories.account()
        ComplimentaryGrant.objects.create(
            account=account,
            plan=factories.plan('practice'),
            expires_at=timezone.now() + timedelta(days=30),
            revoked_at=timezone.now(),
        )
        self.assertEqual(entitlements_for_organization(account.organization_id).entitled_keys, [])


class SponsoredAllocationTests(TestCase):
    """A PCN paying for a member practice — payer and beneficiary separated.

    The monolith reached member practices by a rule applied at read time. Here it
    is a recorded allocation, honoured only while the organisation graph confirms
    the relationship, so every one of these tests installs a graph first. A test
    that forgot to would find the entitlement closed, which is the behaviour.
    """

    def setUp(self):
        self.pcn = factories.account(name='Test PCN', org_type='pcn')
        self.practice = factories.account(name='Member Practice')
        factories.graph(
            edges=[(self.pcn.organization_id, self.practice.organization_id)],
            organizations=[
                (self.pcn.organization_id, 'pcn'),
                (self.practice.organization_id, 'practice'),
            ],
        )

    def test_a_pcn_subscription_allocated_to_a_practice_covers_it(self):
        subscription = factories.subscription(account_obj=self.pcn, plan_key='pcn')
        factories.allocate(subscription, self.practice.organization_id)
        result = entitlements_for_organization(self.practice.organization_id)
        self.assertTrue(result.holds(PRO_TOOLS))
        self.assertEqual(result.products[PRO_TOOLS].source, 'sponsored_allocation')

    def test_membership_alone_does_not_cover_a_practice(self):
        """The relationship is *necessary*, never sufficient. Without an
        allocation the PCN has not decided this practice is covered, and the
        monolith's read-time rule is exactly what this replaces."""
        factories.subscription(account_obj=self.pcn, plan_key='pcn')
        self.assertEqual(
            entitlements_for_organization(self.practice.organization_id).entitled_keys, []
        )

    def test_a_practice_plan_cannot_reach_another_organization(self):
        """`covers_member_organizations` is False on the practice plan."""
        subscription = factories.subscription(account_obj=self.pcn, plan_key='practice')
        factories.allocate(subscription, self.practice.organization_id)
        self.assertEqual(
            entitlements_for_organization(self.practice.organization_id).entitled_keys, []
        )

    def test_coverage_does_not_flow_upwards(self):
        """A practice paying does not entitle its PCN."""
        factories.subscription(account_obj=self.practice, plan_key='practice')
        self.assertEqual(entitlements_for_organization(self.pcn.organization_id).entitled_keys, [])

    def test_an_unrelated_organization_is_never_covered(self):
        stranger = factories.account(name='Unrelated Practice')
        subscription = factories.subscription(account_obj=self.pcn, plan_key='pcn')
        factories.allocate(subscription, stranger.organization_id)
        # Allocated but not related: the allocation exists, the graph refuses it.
        self.assertEqual(entitlements_for_organization(stranger.organization_id).entitled_keys, [])

    def test_the_payer_keeps_its_own_entitlement(self):
        subscription = factories.subscription(account_obj=self.pcn, plan_key='pcn')
        factories.allocate(subscription, self.practice.organization_id)
        self.assertTrue(entitlements_for_organization(self.pcn.organization_id).holds(PRO_TOOLS))


class GraphFreshnessTests(TestCase):
    """Sponsored entitlement fails closed; direct entitlement never does.

    This is decision D-4 made concrete, and the asymmetry is the whole point. An
    entitlement inherited from a relationship nobody can currently confirm is one
    nobody can justify — but a practice that bought its own subscription must
    keep it whether or not Identity is reachable.
    """

    def setUp(self):
        self.pcn = factories.account(name='Test PCN', org_type='pcn')
        self.practice = factories.account(name='Member Practice')
        self.subscription = factories.subscription(account_obj=self.pcn, plan_key='pcn')
        factories.allocate(self.subscription, self.practice.organization_id)

    def test_with_no_graph_at_all_sponsored_entitlement_is_closed(self):
        self.assertEqual(
            entitlements_for_organization(self.practice.organization_id).entitled_keys, []
        )

    def test_with_a_stale_graph_sponsored_entitlement_is_closed(self):
        factories.graph(
            edges=[(self.pcn.organization_id, self.practice.organization_id)],
            generated_at=timezone.now() - timedelta(days=2),
        )
        self.assertEqual(
            entitlements_for_organization(self.practice.organization_id).entitled_keys, []
        )

    def test_with_a_fresh_graph_sponsored_entitlement_is_open(self):
        factories.graph(edges=[(self.pcn.organization_id, self.practice.organization_id)])
        self.assertTrue(
            entitlements_for_organization(self.practice.organization_id).holds(PRO_TOOLS)
        )

    def test_a_stale_graph_does_not_touch_a_practices_own_subscription(self):
        """The asymmetry. Failing closed must never cost somebody what they bought."""
        own = factories.account(name='Independent Practice')
        factories.subscription(account_obj=own, plan_key='practice')
        factories.graph(edges=[], generated_at=timezone.now() - timedelta(days=2))
        self.assertTrue(entitlements_for_organization(own.organization_id).holds(PRO_TOOLS))

    def test_no_graph_does_not_touch_a_practices_own_subscription(self):
        own = factories.account(name='Independent Practice')
        factories.subscription(account_obj=own, plan_key='practice')
        self.assertTrue(entitlements_for_organization(own.organization_id).holds(PRO_TOOLS))

    def test_an_edge_the_graph_no_longer_reports_closes_the_entitlement(self):
        """The allocation row is still ACTIVE here — this is the read-time check,
        which is independent of the write-time lapse handling."""
        factories.graph(edges=[])
        self.assertEqual(
            entitlements_for_organization(self.practice.organization_id).entitled_keys, []
        )


class LatestExpiryTests(TestCase):
    def test_two_sources_report_the_later_end_date(self):
        """Quoting the earlier date would warn about an expiry that will not happen."""
        account = factories.account()
        soon = timezone.now() + timedelta(days=5)
        later = timezone.now() + timedelta(days=200)
        factories.subscription(account_obj=account, period_end=soon)
        ComplimentaryGrant.objects.create(
            account=account, plan=factories.plan('practice'), expires_at=later
        )
        result = entitlements_for_organization(account.organization_id)
        self.assertEqual(result.products[PRO_TOOLS].effective_until, later)

    def test_an_open_ended_source_beats_a_dated_one(self):
        account = factories.account()
        factories.subscription(account_obj=account, period_end=None)
        ComplimentaryGrant.objects.create(
            account=account,
            plan=factories.plan('practice'),
            expires_at=timezone.now() + timedelta(days=5),
        )
        result = entitlements_for_organization(account.organization_id)
        self.assertIsNone(result.products[PRO_TOOLS].effective_until)


class FailClosedTests(TestCase):
    def test_organization_holds_returns_false_on_an_unparseable_organization(self):
        self.assertFalse(organization_holds('not-a-uuid', PRO_TOOLS))

    def test_organization_holds_returns_false_for_an_unknown_product(self):
        account = factories.account()
        factories.subscription(account_obj=account)
        self.assertFalse(organization_holds(account.organization_id, 'no_such_product'))
