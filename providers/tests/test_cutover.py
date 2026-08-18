"""The pre-cutover reconciliation.

Every test here is about a way the cutover should *stop*. The clean run is one
test; the rest are the conditions under which proceeding would drop somebody's
access, charge somebody twice, or silently grant nothing.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from audit import events as audit_events
from audit.models import AuditEvent
from billing.models import Subscription
from billing.tests import factories
from identity.graph_models import GraphOrganization, OrganizationGraph
from providers.cutover import reconcile_for_cutover
from providers.fake import FakeProvider


class CutoverReconciliationTests(TestCase):
    def setUp(self):
        FakeProvider.reset()
        self.provider = FakeProvider()
        self.account = factories.account()
        self.account.provider_customer_id = 'cus_one'
        self.account.save(update_fields=['provider_customer_id'])
        self.price = factories.price('practice', 'month', provider_price_id='price_pm')

    def reconcile(self, **kwargs):
        with patch('providers.cutover.discovery_provider', return_value=self.provider):
            return reconcile_for_cutover(**kwargs)

    def seed_remote(self, subscription_id='sub_one', status='active', **fields):
        return self.provider.seed_subscription(
            subscription_id,
            customer_id=fields.pop('customer_id', 'cus_one'),
            status=status,
            prices=fields.pop('prices', [{'price_id': 'price_pm', 'quantity': 1}]),
            **fields,
        )

    def seed_local(self, provider_subscription_id='sub_one', state=Subscription.State.ACTIVE):
        subscription = factories.subscription(
            account_obj=self.account,
            state=state,
            provider_subscription_id=provider_subscription_id,
        )
        subscription.provider = 'fake'
        subscription.provider_customer_id = 'cus_one'
        subscription.save(update_fields=['provider', 'provider_customer_id'])
        return subscription

    # --- The clean case -------------------------------------------------------

    def test_a_matched_estate_reports_no_conflict(self):
        self.provider.seed_customer('cus_one')
        self.seed_remote()
        self.seed_local()
        report = self.reconcile()
        self.assertEqual(report.conflicts, 0)
        self.assertFalse(report.blocks_cutover)
        self.assertEqual(report.customers_mapped_to_billing, 1)
        self.assertEqual(report.subscriptions_mapped_to_billing, 1)
        self.assertEqual(report.provider_subscriptions_by_status, {'active': 1})

    def test_it_writes_no_state(self):
        # Read-only is the whole safety property: a reconciliation that could
        # write could make its own report come out clean.
        self.provider.seed_customer('cus_one')
        self.seed_remote()
        before = list(Subscription.objects.values_list('id', 'state'))
        self.reconcile()
        self.assertEqual(list(Subscription.objects.values_list('id', 'state')), before)

    # --- Stops ----------------------------------------------------------------

    def test_a_granting_subscription_missing_from_billing_stops_the_cutover(self):
        # The one that would drop a paying customer's access at cutover.
        self.provider.seed_customer('cus_one')
        self.seed_remote('sub_unknown', status='active')
        report = self.reconcile()
        self.assertTrue(report.blocks_cutover)
        self.assertEqual(report.conflicts_by_kind['granting_subscription_not_in_billing'], 1)
        self.assertEqual(report.provider_subscriptions_unmatched, 1)

    def test_a_cancelled_subscription_missing_from_billing_is_unmatched_but_not_a_conflict(self):
        self.provider.seed_customer('cus_one')
        self.seed_remote('sub_old', status='canceled')
        report = self.reconcile()
        self.assertEqual(report.provider_subscriptions_unmatched, 1)
        self.assertFalse(report.blocks_cutover)

    def test_a_state_disagreement_stops_the_cutover(self):
        self.provider.seed_customer('cus_one')
        self.seed_remote(status='past_due')
        self.seed_local(state=Subscription.State.ACTIVE)
        report = self.reconcile()
        self.assertEqual(report.conflicts_by_kind['subscription_state_disagrees'], 1)

    def test_a_price_the_catalogue_cannot_resolve_stops_the_cutover(self):
        # After cutover this subscription would have no plan and grant nothing,
        # without saying so.
        self.provider.seed_customer('cus_one')
        self.seed_remote(prices=[{'price_id': 'price_unmapped', 'quantity': 1}])
        self.seed_local()
        report = self.reconcile()
        self.assertEqual(report.conflicts_by_kind['subscription_price_not_in_catalogue'], 1)

    def test_customer_metadata_naming_a_different_organisation_stops_the_cutover(self):
        self.provider.seed_customer('cus_one', organization_id=str(uuid.uuid4()))
        report = self.reconcile()
        self.assertEqual(report.conflicts_by_kind['customer_organization_metadata_disagrees'], 1)

    def test_two_customers_claiming_one_organisation_stops_the_cutover(self):
        organization = str(self.account.organization_id)
        self.provider.seed_customer('cus_one', organization_id=organization)
        self.provider.seed_customer('cus_two', organization_id=organization)
        report = self.reconcile()
        self.assertEqual(report.conflicts_by_kind['organization_claimed_by_multiple_customers'], 1)

    def test_a_local_subscription_absent_at_the_provider_stops_the_cutover(self):
        self.provider.seed_customer('cus_one')
        self.seed_local('sub_only_local')
        report = self.reconcile()
        self.assertEqual(report.billing_subscriptions_unmatched, 1)
        self.assertEqual(report.conflicts_by_kind['billing_subscription_absent_at_provider'], 1)

    # --- Unmatched, but not conflicts ----------------------------------------

    def test_an_unowned_provider_customer_is_reported_and_never_adopted(self):
        # Nothing is created to make the numbers agree.
        self.provider.seed_customer('cus_stranger')
        report = self.reconcile()
        self.assertEqual(report.provider_customers_unmatched, 1)
        self.assertEqual(report.customers_mapped_to_billing, 0)
        self.assertEqual(report.billing_accounts, 1)

    def test_a_declared_exception_is_counted_separately_rather_than_hidden(self):
        self.provider.seed_customer('cus_stranger')
        report = self.reconcile(exceptions={'cus_stranger'})
        self.assertEqual(report.declared_exceptions, 1)
        self.assertEqual(report.provider_customers_unmatched, 0)
        # Still visible in the provider total, so accepting one leaves a mark.
        self.assertEqual(report.provider_customers, 1)

    # --- Identity -------------------------------------------------------------

    def test_a_missing_identity_projection_is_reported_as_unknown_not_as_zero(self):
        report = self.reconcile()
        self.assertEqual(report.identity_projection, 'missing')
        self.assertEqual(report.accounts_mapped_to_identity, 0)
        self.assertEqual(report.accounts_not_in_identity, 0)
        self.assertFalse(report.blocks_cutover)

    def test_a_fresh_projection_that_does_not_know_an_organisation_stops_the_cutover(self):
        graph = _graph()
        GraphOrganization.objects.create(
            graph=graph, organization_id=uuid.uuid4(), organization_type='practice'
        )
        report = self.reconcile()
        self.assertEqual(report.identity_projection, 'fresh')
        self.assertEqual(report.accounts_not_in_identity, 1)
        self.assertTrue(report.blocks_cutover)

    def test_a_known_organisation_is_counted_as_mapped(self):
        graph = _graph()
        GraphOrganization.objects.create(
            graph=graph,
            organization_id=self.account.organization_id,
            organization_type='practice',
        )
        report = self.reconcile()
        self.assertEqual(report.accounts_mapped_to_identity, 1)
        self.assertFalse(report.blocks_cutover)

    def test_a_stale_projection_does_not_by_itself_stop_the_cutover(self):
        # Stale means "we could not ask", which is not the same as "the
        # organisation does not exist" — and only the latter is a conflict.
        graph = _graph(age=timedelta(days=365))
        GraphOrganization.objects.create(
            graph=graph, organization_id=uuid.uuid4(), organization_type='practice'
        )
        report = self.reconcile()
        self.assertEqual(report.identity_projection, 'stale')
        self.assertFalse(report.blocks_cutover)

    # --- Reporting ------------------------------------------------------------

    def test_the_report_names_no_customer_subscription_or_organisation(self):
        self.provider.seed_customer('cus_one', organization_id=str(self.account.organization_id))
        self.seed_remote()
        self.seed_local()
        rendered = repr(self.reconcile().counts)
        for identifier in ('cus_one', 'sub_one', str(self.account.organization_id)):
            self.assertNotIn(identifier, rendered)

    def test_the_run_is_audited(self):
        self.reconcile()
        self.assertTrue(
            AuditEvent.objects.filter(event=audit_events.CUTOVER_RECONCILIATION_RUN).exists()
        )

    def test_catalogue_readiness_is_reported(self):
        report = self.reconcile()
        self.assertEqual(report.plan_prices, 4)
        # Only the one the fixture mapped.
        self.assertEqual(report.plan_prices_purchasable, 1)


def _graph(age=timedelta(minutes=1)) -> OrganizationGraph:
    now = timezone.now()
    return OrganizationGraph.objects.create(
        graph_version=f'v-{uuid.uuid4()}',
        source=OrganizationGraph.Source.IDENTITY_API,
        generated_at=now - age,
        fetched_at=now - age,
        is_current=True,
    )
