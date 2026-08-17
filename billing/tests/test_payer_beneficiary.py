"""Payer and beneficiary, and the boundary between them.

The single largest change in Phase 4B: who pays for a subscription and who
receives its entitlement are separate, recorded facts. Everything here asserts
one of the two properties that separation exists to give:

* a sponsored organisation gets the **access** and none of the **money** — no
  invoice, no subscription detail, no payment information, no cancel route;
* a sponsorship is valid only while Identity says the relationship is, and when
  it stops being valid **nothing at the provider is touched**.

The second is the one worth being loud about. A billing system that cancelled or
refunded a customer's subscription because somebody edited an organisation chart
would be a billing system nobody could trust with a card, so the test that no
provider call is made is written as an assertion about the fake provider's call
log rather than as a comment.
"""

from __future__ import annotations

import json
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from audit import events
from audit.models import AuditEvent
from billing.entitlements import entitlements_for_organization
from billing.models import EntitlementAllocation, InvoiceReference, OperationalAlert
from billing.sponsorship import AllocationRefused, allocate, invalidate_lapsed_allocations
from catalog.seed import PRO_TOOLS
from providers.fake import FakeProvider

from . import factories


class PayerAndBeneficiaryTests(TestCase):
    """A practice pays for itself; a PCN pays through its own account."""

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

    def test_a_practice_purchase_has_the_same_payer_and_beneficiary(self):
        subscription = factories.subscription(account_obj=self.practice)
        allocation = subscription.allocations.get()
        self.assertEqual(
            str(allocation.beneficiary_organization_id), str(self.practice.organization_id)
        )

    def test_a_pcn_subscription_belongs_to_the_pcns_billing_account(self):
        subscription = factories.subscription(account_obj=self.pcn, plan_key='pcn')
        factories.allocate(subscription, self.practice.organization_id)
        self.assertEqual(subscription.account_id, self.pcn.id)
        # And the practice holds no subscription of its own.
        self.assertEqual(self.practice.subscriptions.count(), 0)

    def test_a_beneficiary_practice_is_entitled_without_owning_a_subscription(self):
        subscription = factories.subscription(account_obj=self.pcn, plan_key='pcn')
        factories.allocate(subscription, self.practice.organization_id)
        result = entitlements_for_organization(self.practice.organization_id)
        self.assertTrue(result.holds(PRO_TOOLS))
        self.assertEqual(result.products[PRO_TOOLS].source, 'sponsored_allocation')
        self.assertEqual(self.practice.subscriptions.count(), 0)

    def test_a_practice_plan_cannot_be_allocated_to_another_organization(self):
        """`covers_member_organizations` is permission to sponsor. Without it,
        allocating elsewhere would make every plan a sponsoring plan by accident."""
        subscription = factories.subscription(account_obj=self.pcn, plan_key='practice')
        with self.assertRaises(AllocationRefused):
            allocate(
                subscription=subscription,
                beneficiary_organization_id=self.practice.organization_id,
            )

    def test_the_union_of_direct_and_sponsored_is_deterministic(self):
        """A practice covered both ways gets one stable answer, not one that
        depends on row order."""
        sponsored = factories.subscription(account_obj=self.pcn, plan_key='pcn')
        factories.allocate(sponsored, self.practice.organization_id)
        factories.subscription(account_obj=self.practice, plan_key='practice')

        answers = {
            tuple(entitlements_for_organization(self.practice.organization_id).entitled_keys)
            for _ in range(5)
        }
        self.assertEqual(len(answers), 1)
        self.assertIn(PRO_TOOLS, next(iter(answers)))

    def test_the_later_end_date_wins_across_payers(self):
        """Quoting the earlier date would warn a practice about an expiry that
        will not happen, because its PCN's annual plan runs past it."""
        soon = timezone.now() + timedelta(days=5)
        later = timezone.now() + timedelta(days=200)
        factories.subscription(account_obj=self.practice, period_end=soon)
        sponsored = factories.subscription(account_obj=self.pcn, plan_key='pcn', period_end=later)
        factories.allocate(sponsored, self.practice.organization_id)

        result = entitlements_for_organization(self.practice.organization_id)
        self.assertEqual(result.products[PRO_TOOLS].effective_until, later)


class InvoicePrivacyTests(TestCase):
    """A beneficiary practice sees the access, never the money."""

    def setUp(self):
        self.pcn = factories.account(name='Paying PCN', org_type='pcn')
        self.practice = factories.account(name='Sponsored Practice')
        factories.graph(
            edges=[(self.pcn.organization_id, self.practice.organization_id)],
            organizations=[
                (self.pcn.organization_id, 'pcn'),
                (self.practice.organization_id, 'practice'),
            ],
        )
        self.subscription = factories.subscription(account_obj=self.pcn, plan_key='pcn')
        factories.allocate(self.subscription, self.practice.organization_id)

        self.invoice = InvoiceReference.objects.create(
            account=self.pcn,
            subscription=self.subscription,
            provider='fake',
            provider_invoice_id='in_pcn_secret_reference',
            number='PCN-INV-0001',
            status=InvoiceReference.Status.PAID,
            total_minor=49000,
            hosted_url='https://provider.invalid/invoice/pcn-0001',
        )

        self.user = factories.identity_user()
        factories.sign_in(
            self.client,
            self.user,
            [
                {
                    'organization_id': self.practice.organization_id,
                    'name': 'Sponsored Practice',
                    'type': 'practice',
                }
            ],
        )
        self.url = reverse('billing:organization', args=[self.practice.organization_id])

    def test_the_practice_page_does_not_show_the_pcns_invoice(self):
        body = self.client.get(self.url).content.decode()
        for forbidden in ('PCN-INV-0001', 'in_pcn_secret_reference', 'provider.invalid/invoice'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)

    def test_the_practice_page_does_not_show_the_pcns_subscription_reference(self):
        body = self.client.get(self.url).content.decode()
        self.assertNotIn(self.subscription.provider_subscription_id, body)

    def test_the_practice_page_reports_no_subscription_of_its_own(self):
        """Sponsorship is not ownership. The subscription card must not adopt
        somebody else's subscription just because it grants us something."""
        self.assertContains(self.client.get(self.url), 'doesn’t have a subscription')

    def test_the_practice_is_told_the_product_is_available_through_its_pcn(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'Provided by another organisation')
        self.assertContains(response, 'Paying PCN')

    def test_the_practice_summary_endpoint_reports_no_subscription(self):
        url = reverse('billing:summary', args=[self.practice.organization_id])
        body = json.loads(self.client.get(url).content)
        self.assertEqual(body['state'], 'none')

    def test_the_practice_cannot_open_the_pcns_billing_page(self):
        url = reverse('billing:organization', args=[self.pcn.organization_id])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_practice_cannot_open_a_portal_for_the_pcn(self):
        url = reverse('billing:portal', args=[self.pcn.organization_id])
        self.assertEqual(self.client.post(url).status_code, 404)

    def test_the_pcn_sees_its_own_invoice(self):
        """The other half: the boundary is about who the invoice belongs to, not
        about hiding invoices generally."""
        factories.sign_in(
            self.client,
            self.user,
            [{'organization_id': self.pcn.organization_id, 'name': 'Paying PCN', 'type': 'pcn'}],
        )
        response = self.client.get(reverse('billing:organization', args=[self.pcn.organization_id]))
        self.assertContains(response, 'PCN-INV-0001')


class CrossPcnRefusalTests(TestCase):
    """A PCN administrator reaches its own PCN and its own members. Nothing else."""

    def setUp(self):
        self.pcn = factories.account(name='Our PCN', org_type='pcn')
        self.member = factories.account(name='Our Member Practice')
        self.other_pcn = factories.account(name='Other PCN', org_type='pcn')
        self.other_member = factories.account(name='Their Member Practice')

        factories.graph(
            edges=[
                (self.pcn.organization_id, self.member.organization_id),
                (self.other_pcn.organization_id, self.other_member.organization_id),
            ],
            organizations=[
                (self.pcn.organization_id, 'pcn'),
                (self.member.organization_id, 'practice'),
                (self.other_pcn.organization_id, 'pcn'),
                (self.other_member.organization_id, 'practice'),
            ],
        )
        self.user = factories.identity_user()
        factories.sign_in(
            self.client,
            self.user,
            [{'organization_id': self.pcn.organization_id, 'name': 'Our PCN', 'type': 'pcn'}],
        )

    def test_a_pcn_admin_cannot_open_another_pcns_billing(self):
        url = reverse('billing:organization', args=[self.other_pcn.organization_id])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_a_pcn_admin_cannot_open_a_member_practices_billing(self):
        """Sponsoring a practice does not make you its billing administrator. The
        practice may hold its own subscription, and that is its business."""
        url = reverse('billing:organization', args=[self.member.organization_id])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_a_pcn_admin_cannot_see_a_member_practices_own_subscription(self):
        factories.subscription(account_obj=self.member, plan_key='practice')
        url = reverse('billing:summary', args=[self.member.organization_id])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_sponsorship_across_pcns_is_refused_by_the_graph(self):
        from identity.graph import sponsorship_is_valid

        self.assertTrue(sponsorship_is_valid(self.pcn.organization_id, self.member.organization_id))
        self.assertFalse(
            sponsorship_is_valid(self.pcn.organization_id, self.other_member.organization_id)
        )

    def test_an_allocation_to_another_pcns_member_grants_nothing(self):
        """Even a recorded allocation is only honoured while the graph agrees, so
        writing one directly does not buy access."""
        subscription = factories.subscription(account_obj=self.pcn, plan_key='pcn')
        factories.allocate(subscription, self.other_member.organization_id)
        self.assertEqual(
            entitlements_for_organization(self.other_member.organization_id).entitled_keys, []
        )

    def test_the_refusal_is_404_and_audited(self):
        url = reverse('billing:organization', args=[self.other_pcn.organization_id])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertTrue(AuditEvent.objects.filter(event=events.ACCESS_REFUSED).exists())


class RelationshipRemovalTests(TestCase):
    """A practice leaves its PCN. Access stops; the subscription is untouched."""

    def setUp(self):
        FakeProvider.reset()
        self.pcn = factories.account(name='Paying PCN', org_type='pcn')
        self.practice = factories.account(name='Departing Practice')
        self.graph = factories.graph(
            edges=[(self.pcn.organization_id, self.practice.organization_id)],
            organizations=[
                (self.pcn.organization_id, 'pcn'),
                (self.practice.organization_id, 'practice'),
            ],
        )
        self.subscription = factories.subscription(account_obj=self.pcn, plan_key='pcn')
        self.allocation = factories.allocate(self.subscription, self.practice.organization_id)

    def _remove_the_relationship(self):
        newer = factories.graph(edges=[])
        invalidate_lapsed_allocations(
            removed_edges={(str(self.pcn.organization_id), str(self.practice.organization_id))},
            graph=newer,
        )
        return newer

    def test_the_practice_is_entitled_before_the_removal(self):
        self.assertTrue(
            entitlements_for_organization(self.practice.organization_id).holds(PRO_TOOLS)
        )

    def test_the_allocation_becomes_ineligible(self):
        self._remove_the_relationship()
        self.allocation.refresh_from_db()
        self.assertEqual(self.allocation.status, EntitlementAllocation.Status.INELIGIBLE)
        self.assertIn('relationship', self.allocation.status_reason.lower())

    def test_the_inherited_entitlement_is_removed(self):
        self._remove_the_relationship()
        self.assertEqual(
            entitlements_for_organization(self.practice.organization_id).entitled_keys, []
        )

    def test_the_subscription_is_not_cancelled(self):
        self._remove_the_relationship()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.state, 'active')
        self.assertIsNone(self.subscription.canceled_at)
        self.assertIsNone(self.subscription.ended_at)
        self.assertFalse(self.subscription.cancel_at_period_end)

    def test_no_provider_call_is_made(self):
        """The assertion this whole model exists to make. A graph sync must never
        cancel, refund or modify anything at the payment provider."""
        self._remove_the_relationship()
        self.assertEqual(FakeProvider.checkout_calls, [])
        self.assertEqual(FakeProvider.portal_calls, [])

    def test_an_operational_alert_is_raised_for_a_person(self):
        self._remove_the_relationship()
        alert = OperationalAlert.objects.get()
        self.assertEqual(alert.kind, OperationalAlert.Kind.SPONSORSHIP_LAPSED)
        self.assertEqual(str(alert.organization_id), str(self.pcn.organization_id))
        self.assertEqual(str(alert.beneficiary_organization_id), str(self.practice.organization_id))
        self.assertTrue(alert.is_open)

    def test_the_alert_carries_no_money_and_no_person(self):
        self._remove_the_relationship()
        alert = OperationalAlert.objects.get()
        for forbidden in ('£', 'amount', 'card', 'email'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, alert.detail.lower())

    def test_the_lapse_is_audited_as_taking_no_provider_action(self):
        self._remove_the_relationship()
        row = AuditEvent.objects.filter(event=events.ALLOCATION_LAPSED).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.metadata['provider_action'], 'none')

    def test_the_payer_keeps_its_own_entitlement(self):
        self._remove_the_relationship()
        self.assertTrue(entitlements_for_organization(self.pcn.organization_id).holds(PRO_TOOLS))

    def test_rejoining_revives_the_same_allocation_row(self):
        """History stays in one place rather than accumulating a row per departure."""
        self._remove_the_relationship()
        factories.graph(edges=[(self.pcn.organization_id, self.practice.organization_id)])
        allocate(
            subscription=self.subscription,
            beneficiary_organization_id=self.practice.organization_id,
        )
        self.assertEqual(self.subscription.allocations.count(), 2)  # self + practice
        self.allocation.refresh_from_db()
        self.assertEqual(self.allocation.status, EntitlementAllocation.Status.ACTIVE)
        self.assertTrue(
            entitlements_for_organization(self.practice.organization_id).holds(PRO_TOOLS)
        )

    def test_an_unrelated_allocation_is_not_touched(self):
        other = factories.account(name='Still A Member')
        factories.graph(
            edges=[
                (self.pcn.organization_id, self.practice.organization_id),
                (self.pcn.organization_id, other.organization_id),
            ]
        )
        kept = factories.allocate(self.subscription, other.organization_id)
        self._remove_the_relationship()
        kept.refresh_from_db()
        self.assertEqual(kept.status, EntitlementAllocation.Status.ACTIVE)
