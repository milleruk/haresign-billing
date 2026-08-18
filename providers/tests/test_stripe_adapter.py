"""The Stripe adapter's extraction, against the object shapes Stripe actually sends.

No network: `_to_subscription` and `_to_event` take mappings, so the shapes below
are the real thing rather than a mock of it. That distinction matters here — the
defect these tests exist for survived because the shape it depended on was never
written down anywhere a test could see it.
"""

from __future__ import annotations

import json
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from billing.models import Subscription
from billing.services import BillingConflict, apply_subscription_snapshot
from billing.tests import factories
from providers.stripe_provider import StripeProvider

JULY = 1783701077  # 2026-07-10T16:31:17Z — creation
AUGUST = 1786379486  # 2026-08-10T16:31:26Z — first renewal


def subscription_payload(*, period_start, period_end, on_item=True, created=JULY, status='active'):
    """A subscription as Stripe serves it: period boundaries on the item."""
    item = {'id': 'si_1', 'price': {'id': 'price_1'}, 'quantity': 1}
    payload = {
        'id': 'sub_1',
        'customer': 'cus_1',
        'status': status,
        'created': created,
        'cancel_at_period_end': False,
        'metadata': {},
        'items': {'data': [item]},
    }
    if on_item:
        item['current_period_start'] = period_start
        item['current_period_end'] = period_end
    else:
        payload['current_period_start'] = period_start
        payload['current_period_end'] = period_end
    return payload


def event_payload(
    subscription, *, created, event_id='evt_1', event_type='customer.subscription.updated'
):
    return {
        'id': event_id,
        'type': event_type,
        'created': created,
        'data': {'object': subscription},
    }


class SequenceTests(TestCase):
    """The ordering marker must actually move. It did not, for two versions."""

    def setUp(self):
        self.provider = StripeProvider()

    def test_the_marker_advances_with_the_billing_cycle(self):
        july = self.provider._to_subscription(
            subscription_payload(period_start=JULY, period_end=AUGUST)
        )
        august = self.provider._to_subscription(
            subscription_payload(period_start=AUGUST, period_end=AUGUST + 2678400)
        )
        # The regression in one line: the old formula added `created` to a
        # subscription-level period start Stripe no longer sends, so both of these
        # came out as `created` and compared equal.
        self.assertNotEqual(july.sequence, august.sequence)
        self.assertGreater(august.sequence, july.sequence)

    def test_the_marker_is_read_from_the_item(self):
        snapshot = self.provider._to_subscription(
            subscription_payload(period_start=AUGUST, period_end=AUGUST + 2678400)
        )
        self.assertEqual(snapshot.sequence, AUGUST)
        self.assertEqual(snapshot.current_period_start, _dt(AUGUST))

    def test_a_subscription_level_period_still_works(self):
        snapshot = self.provider._to_subscription(
            subscription_payload(period_start=AUGUST, period_end=AUGUST + 2678400, on_item=False)
        )
        self.assertEqual(snapshot.sequence, AUGUST)

    def test_an_object_with_no_period_falls_back_to_creation(self):
        payload = subscription_payload(period_start=None, period_end=None)
        payload['items']['data'][0].pop('current_period_start', None)
        payload['items']['data'][0].pop('current_period_end', None)
        self.assertEqual(self.provider._to_subscription(payload).sequence, JULY)

    def test_the_event_raises_the_marker_to_its_own_creation_time(self):
        # A mid-period change — a scheduled cancellation, a pause — moves nothing
        # about the period, so without this the sequence would not move either.
        payload = subscription_payload(period_start=AUGUST, period_end=AUGUST + 2678400)
        later = AUGUST + 86400
        event = self.provider._to_event(
            event_payload(payload, created=later),
            json.dumps(event_payload(payload, created=later)).encode(),
        )
        self.assertEqual(event.subscription.sequence, later)

    def test_an_event_older_than_the_object_does_not_lower_the_marker(self):
        payload = subscription_payload(period_start=AUGUST, period_end=AUGUST + 2678400)
        earlier = AUGUST - 86400
        event = self.provider._to_event(
            event_payload(payload, created=earlier),
            json.dumps(event_payload(payload, created=earlier)).encode(),
        )
        self.assertEqual(event.subscription.sequence, AUGUST)


class OutOfOrderProtectionTests(TestCase):
    """Newer state must survive an older event arriving late."""

    def setUp(self):
        self.provider = StripeProvider()
        self.account = factories.account()
        self.price = factories.price('practice', 'month', provider_price_id='price_1')
        self.plan = self.price.plan

    def _apply(self, snapshot):
        return apply_subscription_snapshot(
            account=self.account,
            provider='stripe',
            provider_subscription_id=snapshot.subscription_id,
            state='active' if snapshot.status == 'active' else 'past_due',
            plan=self.plan,
            prices=[(self.price, 1)],
            provider_customer_id=snapshot.customer_id,
            current_period_start=snapshot.current_period_start,
            current_period_end=snapshot.current_period_end,
            sequence=snapshot.sequence,
        )

    def _snapshot(self, *, period_start, event_created, status='active'):
        payload = subscription_payload(
            period_start=period_start, period_end=period_start + 2678400, status=status
        )
        body = event_payload(payload, created=event_created)
        return self.provider._to_event(body, json.dumps(body).encode()).subscription

    def test_an_older_event_cannot_overwrite_newer_state(self):
        self._apply(self._snapshot(period_start=AUGUST, event_created=AUGUST))
        stale = self._snapshot(period_start=JULY, event_created=JULY, status='past_due')
        with self.assertRaises(BillingConflict):
            self._apply(stale)
        subscription = Subscription.objects.get()
        self.assertEqual(subscription.state, Subscription.State.ACTIVE)
        self.assertEqual(subscription.current_period_start, _dt(AUGUST))

    def test_a_mid_period_change_is_not_refused_as_stale(self):
        # Same period, later event. The object marker is equal; the event marker
        # is greater, so this must apply rather than be discarded.
        self._apply(self._snapshot(period_start=AUGUST, event_created=AUGUST))
        _, changed = self._apply(
            self._snapshot(period_start=AUGUST, event_created=AUGUST + 3600, status='past_due')
        )
        self.assertTrue(changed)
        self.assertEqual(Subscription.objects.get().state, Subscription.State.PAST_DUE)

    def test_a_redelivery_of_the_same_event_is_a_duplicate_not_a_conflict(self):
        snapshot = self._snapshot(period_start=AUGUST, event_created=AUGUST)
        self._apply(snapshot)
        _, changed = self._apply(snapshot)
        self.assertFalse(changed)
        self.assertEqual(Subscription.objects.count(), 1)

    def test_reconciliation_bypasses_the_guard_deliberately(self):
        # Reconciliation reads current provider state, which is by definition
        # newer than anything stored, and passes no sequence.
        self._apply(self._snapshot(period_start=AUGUST, event_created=AUGUST))
        subscription, changed = apply_subscription_snapshot(
            account=self.account,
            provider='stripe',
            provider_subscription_id='sub_1',
            state='past_due',
            plan=self.plan,
            prices=[(self.price, 1)],
            current_period_start=_dt(JULY),
            current_period_end=_dt(AUGUST),
            sequence=0,
        )
        self.assertTrue(changed)
        self.assertEqual(subscription.state, Subscription.State.PAST_DUE)


def _dt(epoch):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, tz=UTC)


class ExtractionTests(TestCase):
    """The rest of the snapshot, against the same shapes."""

    def test_every_field_the_state_machine_reads_is_extracted(self):
        payload = subscription_payload(period_start=AUGUST, period_end=AUGUST + 2678400)
        payload['metadata'] = {'haresign_organization_id': 'org-uuid'}
        payload['trial_end'] = AUGUST + 3600
        snapshot = StripeProvider()._to_subscription(payload)
        self.assertEqual(snapshot.subscription_id, 'sub_1')
        self.assertEqual(snapshot.customer_id, 'cus_1')
        self.assertEqual(snapshot.status, 'active')
        self.assertEqual([price.price_id for price in snapshot.prices], ['price_1'])
        self.assertEqual(snapshot.prices[0].quantity, 1)
        self.assertEqual(snapshot.organization_id, 'org-uuid')
        self.assertEqual(snapshot.current_period_end, _dt(AUGUST + 2678400))
        self.assertIsNotNone(snapshot.trial_end)

    def test_an_expanded_customer_object_is_reduced_to_its_id(self):
        payload = subscription_payload(period_start=AUGUST, period_end=AUGUST + 2678400)
        payload['customer'] = {'id': 'cus_expanded', 'email': 'never-read@example.invalid'}
        self.assertEqual(StripeProvider()._to_subscription(payload).customer_id, 'cus_expanded')

    def test_a_stale_period_is_still_reported_rather_than_dropped(self):
        old = int((timezone.now() - timedelta(days=90)).timestamp())
        snapshot = StripeProvider()._to_subscription(
            subscription_payload(period_start=old, period_end=old + 2678400)
        )
        self.assertIsNotNone(snapshot.current_period_end)
