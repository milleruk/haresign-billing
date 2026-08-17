"""Checkout and the customer portal.

The two methods Phase 4A left unimplemented. They are implemented now and they
are still **off**: `BILLING_CHECKOUT_ENABLED` is False in every environment, so
the first block of tests here is the gate, and it is the block that must never be
deleted without a human decision behind it.

Everything after the gate runs with checkout force-enabled *against the fake
provider only*. No test in this repository may reach Stripe, and the fake is not
a mock — it records what it was asked for, so "the price came from the catalogue
and not from the browser" is a claim about a call log rather than a comment.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from django.urls import reverse

from audit import events
from audit.models import AuditEvent
from billing.checkout import CheckoutRefused, idempotency_key
from billing.entitlements import entitlements_for_organization
from catalog.models import PlanPrice
from catalog.seed import PRO_TOOLS
from providers.fake import FakeProvider

from . import factories

# Every purchasable price needs a provider reference. Applied per test class
# rather than globally: the "no verified price" refusal is itself a test.
PRACTICE_PRICE = 'price_test_practice_month'
PCN_PRICE = 'price_test_pcn_month'


def _make_prices_purchasable():
    factories.price('practice', 'month', provider_price_id=PRACTICE_PRICE)
    factories.price('pcn', 'month', provider_price_id=PCN_PRICE)


class CheckoutIsDisabledTests(TestCase):
    """The production gate. Off everywhere, including production.

    Deleting one of these means somebody has decided to enable live purchasing,
    which is a human-confirmed cutover step — see docs/stripe-cutover.md.
    """

    def setUp(self):
        FakeProvider.reset()
        _make_prices_purchasable()
        self.account = factories.account()
        self.user = factories.identity_user()
        factories.sign_in(
            self.client,
            self.user,
            [{'organization_id': self.account.organization_id}],
        )

    def test_the_setting_ships_off(self):
        from django.conf import settings

        self.assertFalse(settings.BILLING_CHECKOUT_ENABLED)

    def test_checkout_answers_503(self):
        response = self.client.post(
            reverse('billing:checkout', args=[self.account.organization_id]),
            {'plan': 'practice', 'interval': 'month'},
        )
        self.assertEqual(response.status_code, 503)

    def test_portal_answers_503(self):
        response = self.client.post(reverse('billing:portal', args=[self.account.organization_id]))
        self.assertEqual(response.status_code, 503)

    def test_no_provider_session_is_created_while_disabled(self):
        self.client.post(
            reverse('billing:checkout', args=[self.account.organization_id]),
            {'plan': 'practice', 'interval': 'month'},
        )
        self.client.post(reverse('billing:portal', args=[self.account.organization_id]))
        self.assertEqual(FakeProvider.checkout_calls, [])
        self.assertEqual(FakeProvider.portal_calls, [])

    def test_the_service_refuses_even_when_called_directly(self):
        """The gate is in the service, not only in the view, so a second entry
        point cannot bypass it by forgetting to check."""
        from billing.checkout import create_checkout_session
        from identity.authorization import OrganizationAccess

        access = OrganizationAccess(
            organization_id=str(self.account.organization_id),
            organization_name='',
            organization_type='practice',
        )
        with self.assertRaises(CheckoutRefused):
            create_checkout_session(access=access, plan_key='practice', interval='month')


@override_settings(BILLING_CHECKOUT_ENABLED=True)
class CheckoutTests(TestCase):
    """With the gate forced open, against the fake provider only."""

    def setUp(self):
        FakeProvider.reset()
        _make_prices_purchasable()
        self.account = factories.account(name='Buying Practice')
        self.user = factories.identity_user()
        factories.sign_in(
            self.client,
            self.user,
            [{'organization_id': self.account.organization_id, 'type': 'practice'}],
        )
        self.url = reverse('billing:checkout', args=[self.account.organization_id])

    def test_a_practice_administrator_can_start_checkout(self):
        response = self.client.post(self.url, {'plan': 'practice', 'interval': 'month'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(FakeProvider.checkout_calls), 1)

    def test_the_price_comes_from_the_catalogue_not_the_request(self):
        """A price id from a form is a price id an attacker picks, and the
        provider will charge whatever it is told."""
        self.client.post(
            self.url,
            {
                'plan': 'practice',
                'interval': 'month',
                # All of these are ignored. Every one of them is a real parameter
                # name somebody might hope is passed through.
                'price': 'price_attacker_chosen',
                'provider_price_id': 'price_attacker_chosen',
                'amount': '1',
                'amount_minor': '1',
                'currency': 'XXX',
            },
        )
        call = FakeProvider.checkout_calls[0]
        self.assertEqual(call['provider_price_id'], PRACTICE_PRICE)

    def test_the_customer_comes_from_our_own_account_not_the_request(self):
        self.account.provider_customer_id = 'cus_ours'
        self.account.save(update_fields=['provider_customer_id'])
        self.client.post(
            self.url,
            {'plan': 'practice', 'interval': 'month', 'customer_id': 'cus_somebody_else'},
        )
        self.assertEqual(FakeProvider.checkout_calls[0]['customer_id'], 'cus_ours')

    def test_the_payer_comes_from_the_session_not_the_url(self):
        call_organization = None
        self.client.post(self.url, {'plan': 'practice', 'interval': 'month'})
        call_organization = FakeProvider.checkout_calls[0]['organization_id']
        self.assertEqual(call_organization, str(self.account.organization_id))

    def test_the_return_urls_are_exact_and_absolute(self):
        from django.conf import settings

        self.client.post(self.url, {'plan': 'practice', 'interval': 'month'})
        call = FakeProvider.checkout_calls[0]
        self.assertEqual(
            call['success_url'],
            f'{settings.SITE_BASE_URL}/organizations/'
            f'{self.account.organization_id}/checkout/complete/',
        )
        self.assertEqual(
            call['cancel_url'],
            f'{settings.SITE_BASE_URL}/organizations/{self.account.organization_id}/',
        )
        for key in ('success_url', 'cancel_url'):
            self.assertTrue(call[key].startswith(settings.SITE_BASE_URL))

    def test_an_idempotency_key_is_sent(self):
        self.client.post(self.url, {'plan': 'practice', 'interval': 'month'})
        self.assertTrue(FakeProvider.checkout_calls[0]['idempotency_key'])

    def test_a_repeated_submission_reuses_the_session(self):
        """A double-clicked button must not become two subscriptions."""
        first = self.client.post(self.url, {'plan': 'practice', 'interval': 'month'})
        second = self.client.post(self.url, {'plan': 'practice', 'interval': 'month'})
        self.assertEqual(first['Location'], second['Location'])

    def test_the_idempotency_key_carries_no_organization_uuid(self):
        """It is a digest so that no organisation or user UUID travels to the
        provider inside a header value."""
        key = idempotency_key(
            payer_id=self.account.organization_id,
            beneficiary_id=self.account.organization_id,
            price_id=PRACTICE_PRICE,
            actor_user_id=self.user.identity_user_id,
        )
        self.assertNotIn(str(self.account.organization_id), key)
        self.assertNotIn(str(self.user.identity_user_id), key)

    def test_a_price_with_no_provider_reference_is_refused(self):
        PlanPrice.objects.filter(plan__key='practice', interval='month').update(
            provider_price_id=''
        )
        response = self.client.post(self.url, {'plan': 'practice', 'interval': 'month'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(FakeProvider.checkout_calls, [])

    def test_an_unknown_plan_is_refused(self):
        response = self.client.post(self.url, {'plan': 'not-a-plan', 'interval': 'month'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(FakeProvider.checkout_calls, [])

    def test_a_stranger_cannot_start_checkout_for_someone_else(self):
        other = factories.account()
        response = self.client.post(
            reverse('billing:checkout', args=[other.organization_id]),
            {'plan': 'practice', 'interval': 'month'},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(FakeProvider.checkout_calls, [])

    def test_checkout_still_refuses_a_get(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_checkout_is_still_csrf_protected(self):
        enforcing = self.client_class(enforce_csrf_checks=True)
        factories.sign_in(enforcing, self.user, [{'organization_id': self.account.organization_id}])
        self.assertEqual(enforcing.post(self.url).status_code, 403)

    def test_the_audit_event_carries_no_money_or_provider_identifier(self):
        self.client.post(self.url, {'plan': 'practice', 'interval': 'month'})
        row = AuditEvent.objects.get(event=events.CHECKOUT_STARTED)
        serialised = str(row.metadata)
        for forbidden in ('price_test', 'cus_', 'amount', 'currency'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialised)


@override_settings(BILLING_CHECKOUT_ENABLED=True)
class SponsoredCheckoutTests(TestCase):
    """A PCN buying for a member practice."""

    def setUp(self):
        FakeProvider.reset()
        _make_prices_purchasable()
        self.pcn = factories.account(name='Buying PCN', org_type='pcn')
        self.member = factories.account(name='Member Practice')
        self.stranger = factories.account(name='Unrelated Practice')
        factories.graph(
            edges=[(self.pcn.organization_id, self.member.organization_id)],
            organizations=[
                (self.pcn.organization_id, 'pcn'),
                (self.member.organization_id, 'practice'),
                (self.stranger.organization_id, 'practice'),
            ],
        )
        self.user = factories.identity_user()
        factories.sign_in(
            self.client,
            self.user,
            [{'organization_id': self.pcn.organization_id, 'name': 'Buying PCN', 'type': 'pcn'}],
        )
        self.url = reverse('billing:checkout', args=[self.pcn.organization_id])

    def test_a_pcn_can_buy_for_a_current_member(self):
        response = self.client.post(
            self.url,
            {
                'plan': 'pcn',
                'interval': 'month',
                'beneficiary': str(self.member.organization_id),
            },
        )
        self.assertEqual(response.status_code, 302)
        call = FakeProvider.checkout_calls[0]
        self.assertEqual(call['organization_id'], str(self.pcn.organization_id))
        self.assertEqual(call['beneficiary_organization_id'], str(self.member.organization_id))

    def test_a_pcn_cannot_buy_for_an_unrelated_practice(self):
        response = self.client.post(
            self.url,
            {
                'plan': 'pcn',
                'interval': 'month',
                'beneficiary': str(self.stranger.organization_id),
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(FakeProvider.checkout_calls, [])

    def test_a_practice_cannot_buy_for_another_organization(self):
        """Only a PCN sponsors. A practice pays for itself and nothing else."""
        practice = factories.account(name='Ordinary Practice')
        factories.sign_in(
            self.client,
            self.user,
            [{'organization_id': practice.organization_id, 'type': 'practice'}],
        )
        response = self.client.post(
            reverse('billing:checkout', args=[practice.organization_id]),
            {
                'plan': 'pcn',
                'interval': 'month',
                'beneficiary': str(self.member.organization_id),
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(FakeProvider.checkout_calls, [])

    def test_a_stale_graph_refuses_a_sponsored_purchase(self):
        """Fail closed for new purchases. Buying for a practice whose membership
        cannot be confirmed is buying for a practice that may have left."""
        from datetime import timedelta

        from django.utils import timezone

        factories.graph(
            edges=[(self.pcn.organization_id, self.member.organization_id)],
            generated_at=timezone.now() - timedelta(days=2),
        )
        response = self.client.post(
            self.url,
            {
                'plan': 'pcn',
                'interval': 'month',
                'beneficiary': str(self.member.organization_id),
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(FakeProvider.checkout_calls, [])

    def test_a_plan_that_does_not_sponsor_is_refused(self):
        response = self.client.post(
            self.url,
            {
                'plan': 'practice',
                'interval': 'month',
                'beneficiary': str(self.member.organization_id),
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(FakeProvider.checkout_calls, [])


@override_settings(BILLING_CHECKOUT_ENABLED=True)
class DuplicateCoverageTests(TestCase):
    """Two subscriptions for one product is a customer paying twice."""

    def setUp(self):
        FakeProvider.reset()
        _make_prices_purchasable()
        self.pcn = factories.account(name='Paying PCN', org_type='pcn')
        self.practice = factories.account(name='Covered Practice')
        factories.graph(
            edges=[(self.pcn.organization_id, self.practice.organization_id)],
            organizations=[
                (self.pcn.organization_id, 'pcn'),
                (self.practice.organization_id, 'practice'),
            ],
        )
        self.user = factories.identity_user()

    def _sign_in_as(self, account, org_type='practice'):
        factories.sign_in(
            self.client,
            self.user,
            [{'organization_id': account.organization_id, 'type': org_type}],
        )
        return reverse('billing:checkout', args=[account.organization_id])

    def test_a_practice_already_holding_a_subscription_cannot_buy_it_again(self):
        factories.subscription(account_obj=self.practice, plan_key='practice')
        url = self._sign_in_as(self.practice)
        response = self.client.post(url, {'plan': 'practice', 'interval': 'month'})
        self.assertEqual(response.status_code, 409)
        self.assertIn('already', response.json()['detail'].lower())
        self.assertEqual(FakeProvider.checkout_calls, [])

    def test_a_practice_covered_by_its_pcn_cannot_buy_the_same_products(self):
        """The commonest real duplicate: a PCN buys the bundle on Monday and the
        practice buys it again on Wednesday."""
        sponsored = factories.subscription(account_obj=self.pcn, plan_key='pcn')
        factories.allocate(sponsored, self.practice.organization_id)
        self.assertTrue(
            entitlements_for_organization(self.practice.organization_id).holds(PRO_TOOLS)
        )

        url = self._sign_in_as(self.practice)
        response = self.client.post(url, {'plan': 'practice', 'interval': 'month'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(FakeProvider.checkout_calls, [])

    def test_a_pcn_cannot_allocate_the_same_product_twice_to_one_practice(self):
        sponsored = factories.subscription(account_obj=self.pcn, plan_key='pcn')
        factories.allocate(sponsored, self.practice.organization_id)

        url = self._sign_in_as(self.pcn, org_type='pcn')
        response = self.client.post(
            url,
            {
                'plan': 'pcn',
                'interval': 'month',
                'beneficiary': str(self.practice.organization_id),
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(FakeProvider.checkout_calls, [])

    def test_an_uncovered_practice_is_allowed_through(self):
        """The check must refuse duplicates without refusing first purchases."""
        url = self._sign_in_as(self.practice)
        response = self.client.post(url, {'plan': 'practice', 'interval': 'month'})
        self.assertEqual(response.status_code, 302)

    def test_a_lapsed_subscription_does_not_block_a_new_purchase(self):
        factories.subscription(account_obj=self.practice, plan_key='practice', state='canceled')
        url = self._sign_in_as(self.practice)
        response = self.client.post(url, {'plan': 'practice', 'interval': 'month'})
        self.assertEqual(response.status_code, 302)


@override_settings(BILLING_CHECKOUT_ENABLED=True)
class PortalTests(TestCase):
    def setUp(self):
        FakeProvider.reset()
        self.account = factories.account(name='Portal Practice')
        self.account.provider_customer_id = 'cus_ours'
        self.account.save(update_fields=['provider_customer_id'])
        self.user = factories.identity_user()
        factories.sign_in(
            self.client, self.user, [{'organization_id': self.account.organization_id}]
        )
        self.url = reverse('billing:portal', args=[self.account.organization_id])

    def test_an_administrator_can_open_the_portal(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(FakeProvider.portal_calls), 1)

    def test_the_customer_is_our_own_never_the_requests(self):
        self.client.post(self.url, {'customer_id': 'cus_somebody_else'})
        self.assertEqual(FakeProvider.portal_calls[0]['customer_id'], 'cus_ours')

    def test_the_return_url_is_exact(self):
        from django.conf import settings

        self.client.post(self.url)
        self.assertEqual(
            FakeProvider.portal_calls[0]['return_url'],
            f'{settings.SITE_BASE_URL}/organizations/{self.account.organization_id}/',
        )

    def test_an_organization_with_no_provider_customer_is_refused(self):
        never_paid = factories.account(name='Never Paid')
        factories.sign_in(self.client, self.user, [{'organization_id': never_paid.organization_id}])
        response = self.client.post(reverse('billing:portal', args=[never_paid.organization_id]))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(FakeProvider.portal_calls, [])

    def test_a_stranger_cannot_open_a_portal(self):
        other = factories.account()
        other.provider_customer_id = 'cus_theirs'
        other.save(update_fields=['provider_customer_id'])
        response = self.client.post(reverse('billing:portal', args=[other.organization_id]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(FakeProvider.portal_calls, [])

    def test_the_portal_refuses_a_get(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_opening_the_portal_is_audited(self):
        self.client.post(self.url)
        self.assertTrue(AuditEvent.objects.filter(event=events.PORTAL_OPENED).exists())
