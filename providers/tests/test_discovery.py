"""Read-only provider discovery.

The point of these is the *refusals*. Discovery is the first thing in this
repository permitted to reach Stripe, and the failure that matters is not an
exception — it is a report that looks fine and describes the wrong account.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from audit import events as audit_events
from audit.models import AuditEvent
from providers.discovery import (
    LIVE,
    TEST,
    UNKNOWN,
    DiscoveryRefused,
    credential_mode,
    discover,
    discovery_provider,
)
from providers.fake import FakeProvider


class CredentialModeTests(SimpleTestCase):
    def test_secret_and_restricted_keys_name_their_mode(self):
        self.assertEqual(credential_mode('sk_live_abc'), LIVE)
        self.assertEqual(credential_mode('rk_live_abc'), LIVE)
        self.assertEqual(credential_mode('sk_test_abc'), TEST)
        self.assertEqual(credential_mode('rk_test_abc'), TEST)

    def test_an_unrecognised_key_names_no_mode(self):
        # Never guessed. An unknown prefix is a refusal upstream, not a default.
        self.assertEqual(credential_mode('pk_live_abc'), UNKNOWN)
        self.assertEqual(credential_mode(''), UNKNOWN)


class DiscoveryProviderTests(SimpleTestCase):
    @override_settings(STRIPE_SECRET_KEY='', PROVIDER_BACKEND='fake')
    def test_without_a_credential_discovery_uses_the_configured_backend(self):
        self.assertEqual(discovery_provider().name, 'fake')

    @override_settings(STRIPE_SECRET_KEY='rk_test_abc', PROVIDER_BACKEND='fake')
    def test_a_credential_reaches_stripe_without_moving_the_runtime_backend(self):
        # The property that makes pre-cutover discovery safe: a read key can be
        # configured for discovery while webhooks and checkout stay on the fake.
        self.assertEqual(discovery_provider().name, 'stripe')


class DiscoveryRefusalTests(TestCase):
    def setUp(self):
        FakeProvider.reset()

    @override_settings(STRIPE_SECRET_KEY='')
    def test_the_fake_cannot_satisfy_an_expectation_of_a_stripe_mode(self):
        with self.assertRaises(DiscoveryRefused):
            discover(expect_mode=TEST)

    @override_settings(STRIPE_SECRET_KEY='')
    def test_a_mode_must_be_stated(self):
        with self.assertRaises(DiscoveryRefused):
            discover(expect_mode='')

    @override_settings(STRIPE_SECRET_KEY='sk_live_abc')
    def test_a_live_key_is_refused_when_test_was_expected(self):
        with patch('providers.discovery.discovery_provider', return_value=_stub()):
            with self.assertRaises(DiscoveryRefused):
                discover(expect_mode=TEST)

    @override_settings(STRIPE_SECRET_KEY='sk_unknown_prefix')
    def test_a_key_that_names_no_mode_is_refused(self):
        with patch('providers.discovery.discovery_provider', return_value=_stub()):
            with self.assertRaises(DiscoveryRefused):
                discover(expect_mode=LIVE)

    @override_settings(STRIPE_SECRET_KEY='sk_test_abc')
    def test_objects_that_disagree_with_the_key_are_refused(self):
        # A test key returning live objects cannot happen at one account, so it
        # means the report would describe something other than what it claims to.
        provider = _stub()
        provider.seed_price('price_1', livemode=True)
        with patch('providers.discovery.discovery_provider', return_value=provider):
            with self.assertRaises(DiscoveryRefused):
                discover(expect_mode=TEST)

    @override_settings(STRIPE_SECRET_KEY='sk_test_abc')
    def test_mixed_live_and_test_objects_are_refused(self):
        provider = _stub()
        provider.seed_price('price_1', livemode=False)
        provider.seed_customer('cus_1', livemode=True)
        with patch('providers.discovery.discovery_provider', return_value=provider):
            with self.assertRaises(DiscoveryRefused):
                discover(expect_mode=TEST)


class DiscoveryReportTests(TestCase):
    def setUp(self):
        FakeProvider.reset()

    def _discover(self, **kwargs):
        provider = _stub()
        provider.seed_price('price_pm', product_id='prod_a', currency='GBP', interval='month')
        provider.seed_price(
            'price_py',
            product_id='prod_a',
            currency='GBP',
            interval='year',
            unit_amount_minor=11000,
        )
        provider.seed_price(
            'price_one_off', product_id='prod_b', interval='', active=False, product_active=False
        )
        provider.seed_customer('cus_1', organization_id='11111111-1111-1111-1111-111111111111')
        provider.seed_customer('cus_2')
        provider.seed_subscription(
            'sub_1',
            customer_id='cus_1',
            status='active',
            prices=[{'price_id': 'price_pm', 'quantity': 1}],
        )
        provider.seed_subscription(
            'sub_2',
            customer_id='cus_2',
            status='past_due',
            prices=[{'price_id': 'price_gone', 'quantity': 1}],
        )
        with patch('providers.discovery.discovery_provider', return_value=provider):
            return discover(expect_mode=TEST, **kwargs)

    @override_settings(STRIPE_SECRET_KEY='sk_test_abc')
    def test_counts_and_distributions(self):
        report = self._discover()
        self.assertEqual(report.observed_mode, TEST)
        self.assertEqual(report.products, 2)
        self.assertEqual(report.prices_total, 3)
        self.assertEqual(report.prices_active, 2)
        self.assertEqual(report.prices_recurring, 2)
        self.assertEqual(report.prices_on_archived_products, 1)
        self.assertEqual(report.intervals, {'month': 1, 'year': 1, 'one_off': 1})
        self.assertEqual(report.customers_total, 2)
        self.assertEqual(report.customers_with_organization_metadata, 1)
        self.assertEqual(report.subscriptions_total, 2)
        self.assertEqual(report.subscriptions_by_status, {'active': 1, 'past_due': 1})
        # The subscription on a price the catalogue has never seen is counted,
        # because after cutover it would grant nothing and say nothing.
        self.assertEqual(report.subscriptions_with_unknown_price, 1)

    @override_settings(STRIPE_SECRET_KEY='sk_test_abc')
    def test_the_report_carries_no_customer_or_subscription_identifier(self):
        report = self._discover(include_catalogue=True)
        rendered = repr(report.counts)
        for identifier in ('cus_1', 'cus_2', 'sub_1', 'sub_2'):
            self.assertNotIn(identifier, rendered)
        # Catalogue ids are product facts and are allowed, but only when asked for.
        self.assertTrue(report.catalogue)

    @override_settings(STRIPE_SECRET_KEY='sk_test_abc')
    def test_the_catalogue_is_withheld_unless_requested(self):
        self.assertEqual(self._discover().catalogue, [])

    @override_settings(STRIPE_SECRET_KEY='sk_test_abc')
    def test_the_run_is_audited_with_counts_only(self):
        self._discover()
        event = AuditEvent.objects.filter(event=audit_events.PROVIDER_DISCOVERY_RUN).first()
        self.assertIsNotNone(event)
        self.assertNotIn('cus_1', repr(event.metadata))


def _stub() -> FakeProvider:
    """A fake that answers to the name Stripe.

    Discovery refuses any provider not called `stripe`, and the point of these
    tests is the mode and reporting logic — not the SDK, which is exercised
    nowhere in this repository because reaching it would mean a network call to a
    payment API.
    """
    provider = FakeProvider()
    provider.name = 'stripe'
    return provider
