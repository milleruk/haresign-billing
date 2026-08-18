"""Mapping catalogue plan prices to provider price identifiers.

A wrong mapping here sells the wrong plan at the wrong price, and the mistake is
only discovered once somebody has been charged. So every one of these asserts a
refusal, and the two that assert a write also assert that nothing else moved.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings

from audit import events as audit_events
from audit.models import AuditEvent
from catalog.models import PlanPrice
from catalog.price_mapping import MappingEntry, MappingRefused, map_prices
from providers.fake import FakeProvider


class MappingEntryTests(TestCase):
    def test_parses_the_stated_form(self):
        entry = MappingEntry.parse('practice:month=price_abc')
        self.assertEqual(
            (entry.plan_key, entry.interval, entry.price_id), ('practice', 'month', 'price_abc')
        )

    def test_refuses_anything_it_cannot_read_exactly(self):
        for text in ('practice:month', 'practice=price_abc', ':month=price_abc', ''):
            with self.assertRaises(MappingRefused):
                MappingEntry.parse(text)


@override_settings(STRIPE_SECRET_KEY='sk_test_abc')
class PriceMappingTests(TestCase):
    def setUp(self):
        FakeProvider.reset()
        self.provider = FakeProvider()
        self.row = PlanPrice.objects.select_related('plan').get(
            plan__key='practice', interval='month'
        )
        self.provider.seed_price(
            'price_practice_month',
            currency=self.row.currency,
            unit_amount_minor=self.row.amount_minor,
            interval='month',
            livemode=False,
        )

    def run_mapping(self, statements, **kwargs):
        entries = [MappingEntry.parse(text) for text in statements]
        with patch('catalog.price_mapping.discovery_provider', return_value=self.provider):
            return map_prices(entries, expect_mode='test', **kwargs)

    def assert_refused(self, statements, **kwargs):
        with self.assertRaises(MappingRefused) as caught:
            self.run_mapping(statements, apply=True, **kwargs)
        self.row.refresh_from_db()
        self.assertEqual(self.row.provider_price_id, '')
        return caught.exception

    # --- Verification ---------------------------------------------------------

    def test_verifies_without_writing_by_default(self):
        outcomes = self.run_mapping(['practice:month=price_practice_month'])
        self.assertEqual(outcomes[0].outcome, 'mapped')
        self.row.refresh_from_db()
        self.assertEqual(self.row.provider_price_id, '')

    def test_applies_a_verified_mapping(self):
        self.run_mapping(['practice:month=price_practice_month'], apply=True)
        self.row.refresh_from_db()
        self.assertEqual(self.row.provider_price_id, 'price_practice_month')
        self.assertTrue(self.row.is_purchasable)

    def test_reapplying_the_same_mapping_is_unchanged_and_not_a_second_write(self):
        self.run_mapping(['practice:month=price_practice_month'], apply=True)
        outcomes = self.run_mapping(['practice:month=price_practice_month'], apply=True)
        self.assertEqual(outcomes[0].outcome, 'unchanged')
        self.assertEqual(
            AuditEvent.objects.filter(event=audit_events.PROVIDER_PRICE_MAPPED).count(), 1
        )

    # --- Refusals -------------------------------------------------------------

    def test_refuses_a_price_the_provider_does_not_have(self):
        exception = self.assert_refused(['practice:month=price_nonexistent'])
        self.assertEqual(exception.outcomes[0].reason, 'price_not_found_at_provider')

    def test_refuses_a_plan_price_that_does_not_exist(self):
        self.assert_refused(['nonexistent:month=price_practice_month'])

    def test_refuses_a_price_from_the_wrong_stripe_mode(self):
        self.provider.seed_price(
            'price_practice_month',
            currency=self.row.currency,
            unit_amount_minor=self.row.amount_minor,
            interval='month',
            livemode=True,
        )
        exception = self.assert_refused(['practice:month=price_practice_month'])
        self.assertEqual(exception.outcomes[0].reason, 'price_mode_mismatch')

    def test_refuses_an_amount_that_disagrees_with_the_catalogue(self):
        # Neither side is corrected. One of them is wrong and this code does not
        # get to decide which.
        self.provider.seed_price(
            'price_practice_month',
            currency=self.row.currency,
            unit_amount_minor=self.row.amount_minor + 1,
            interval='month',
        )
        exception = self.assert_refused(['practice:month=price_practice_month'])
        self.assertEqual(exception.outcomes[0].reason, 'amount_mismatch')

    def test_refuses_a_currency_mismatch(self):
        self.provider.seed_price(
            'price_practice_month',
            currency='USD',
            unit_amount_minor=self.row.amount_minor,
            interval='month',
        )
        exception = self.assert_refused(['practice:month=price_practice_month'])
        self.assertEqual(exception.outcomes[0].reason, 'currency_mismatch')

    def test_refuses_an_interval_mismatch(self):
        self.provider.seed_price(
            'price_practice_month',
            currency=self.row.currency,
            unit_amount_minor=self.row.amount_minor,
            interval='year',
        )
        exception = self.assert_refused(['practice:month=price_practice_month'])
        self.assertEqual(exception.outcomes[0].reason, 'interval_mismatch')

    def test_refuses_a_non_recurring_price(self):
        self.provider.seed_price(
            'price_practice_month',
            currency=self.row.currency,
            unit_amount_minor=self.row.amount_minor,
            interval='',
        )
        exception = self.assert_refused(['practice:month=price_practice_month'])
        self.assertEqual(exception.outcomes[0].reason, 'price_is_not_recurring')

    def test_refuses_an_archived_price_or_product(self):
        self.provider.seed_price(
            'price_practice_month',
            currency=self.row.currency,
            unit_amount_minor=self.row.amount_minor,
            interval='month',
            active=False,
        )
        self.assertEqual(
            self.assert_refused(['practice:month=price_practice_month']).outcomes[0].reason,
            'price_archived_at_provider',
        )

    def test_refuses_a_price_already_mapped_to_another_plan(self):
        other = PlanPrice.objects.get(plan__key='pcn', interval='month')
        other.provider_price_id = 'price_practice_month'
        other.save(update_fields=['provider_price_id'])
        exception = self.assert_refused(['practice:month=price_practice_month'])
        self.assertEqual(exception.outcomes[0].reason, 'price_already_mapped_to_another_plan')

    def test_refuses_to_replace_an_existing_reference_without_force(self):
        self.row.provider_price_id = 'price_previous'
        self.row.save(update_fields=['provider_price_id'])
        with self.assertRaises(MappingRefused) as caught:
            self.run_mapping(['practice:month=price_practice_month'], apply=True)
        self.assertEqual(caught.exception.outcomes[0].reason, 'already_mapped_to_a_different_price')
        self.row.refresh_from_db()
        self.assertEqual(self.row.provider_price_id, 'price_previous')

    def test_force_replaces_and_says_so_in_the_audit_trail(self):
        self.row.provider_price_id = 'price_previous'
        self.row.save(update_fields=['provider_price_id'])
        self.run_mapping(['practice:month=price_practice_month'], apply=True, force=True)
        self.row.refresh_from_db()
        self.assertEqual(self.row.provider_price_id, 'price_practice_month')
        event = AuditEvent.objects.get(event=audit_events.PROVIDER_PRICE_MAPPED)
        self.assertTrue(event.metadata['replaced_existing'])

    def test_refuses_the_same_price_stated_for_two_plans(self):
        self.assert_refused(
            ['practice:month=price_practice_month', 'practice:year=price_practice_month']
        )

    def test_one_refusal_prevents_every_write_in_the_run(self):
        # The property that stops a half-launched catalogue: the valid mapping in
        # this pair is not written either.
        self.assert_refused(
            ['practice:month=price_practice_month', 'practice:year=price_nonexistent']
        )
        self.assertEqual(PlanPrice.objects.exclude(provider_price_id='').count(), 0)

    def test_a_refusal_is_audited(self):
        self.assert_refused(['practice:month=price_nonexistent'])
        self.assertTrue(
            AuditEvent.objects.filter(event=audit_events.PROVIDER_PRICE_MAPPING_REFUSED).exists()
        )

    def test_a_mode_must_be_stated(self):
        entries = [MappingEntry.parse('practice:month=price_practice_month')]
        with patch('catalog.price_mapping.discovery_provider', return_value=self.provider):
            with self.assertRaises(MappingRefused):
                map_prices(entries, expect_mode='', apply=True)
