"""Webhook verification, idempotency and ordering.

The webhook endpoint is the only route the public internet is expected to reach,
so most of these assert the *absence* of a bad outcome: an unsigned event that did
not apply, a replay that did not double-apply, a stale event that did not move
state backwards.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from audit import events as audit_events
from audit.models import AuditEvent
from billing.models import Subscription
from billing.tests import factories
from providers.fake import FakeProvider, sign
from providers.mapping import subscription_state
from providers.models import WebhookEvent


def _ts(moment) -> int:
    return int(moment.timestamp())


class WebhookTestCase(TestCase):
    def setUp(self):
        FakeProvider.reset()
        self.url = reverse('providers:webhook')
        self.account = factories.account()
        self.price = factories.price('practice', 'month', provider_price_id='price_synthetic_pm')
        self.account.provider = 'fake'
        self.account.provider_customer_id = 'cus_synthetic_0001'
        self.account.save(update_fields=['provider', 'provider_customer_id'])

    def seed(self, subscription_id='sub_synthetic_0001', **fields):
        return FakeProvider.seed_subscription(
            subscription_id,
            customer_id='cus_synthetic_0001',
            prices=[{'price_id': self.price.provider_price_id, 'quantity': 1}],
            current_period_end=_ts(timezone.now() + timedelta(days=30)),
            organization_id=str(self.account.organization_id),
            **fields,
        )

    def deliver(self, body: bytes, signature: str | None = None):
        return self.client.post(
            self.url,
            data=body,
            content_type='application/json',
            HTTP_STRIPE_SIGNATURE=signature if signature is not None else sign(body),
        )


class SignatureTests(WebhookTestCase):
    def test_a_valid_signature_is_accepted(self):
        self.seed()
        body = FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0001')
        self.assertEqual(self.deliver(body).status_code, 200)

    def test_an_unsigned_event_is_refused_and_applies_nothing(self):
        self.seed()
        body = FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0001')
        response = self.deliver(body, signature='')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Subscription.objects.count(), 0)
        self.assertEqual(WebhookEvent.objects.count(), 0)

    def test_a_forged_signature_is_refused(self):
        self.seed()
        body = FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0001')
        forged = sign(body, secret='whsec_not_the_real_one')
        self.assertEqual(self.deliver(body, signature=forged).status_code, 400)
        self.assertEqual(Subscription.objects.count(), 0)

    def test_a_tampered_body_is_refused(self):
        """Signed one payload, delivered another."""
        self.seed()
        body = FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0001')
        signature = sign(body)
        tampered = body.replace(b'"status":"active"', b'"status":"trialing"')
        self.assertEqual(self.deliver(tampered, signature=signature).status_code, 400)
        self.assertEqual(Subscription.objects.count(), 0)

    def test_a_stale_signature_timestamp_is_refused(self):
        """A captured webhook must not be replayable forever."""
        self.seed()
        body = FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0001')
        old = sign(body, timestamp=int(time.time()) - 3600)
        self.assertEqual(self.deliver(body, signature=old).status_code, 400)

    def test_a_refusal_is_never_a_500(self):
        """A 500 makes the provider retry a forgery, and eventually disables us."""
        body = b'{"not":"an event"}'
        self.assertLess(self.deliver(body, signature='garbage').status_code, 500)

    def test_a_refusal_is_audited(self):
        self.seed()
        body = FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0001')
        self.deliver(body, signature='t=1,v1=deadbeef')
        self.assertTrue(AuditEvent.objects.filter(event=audit_events.WEBHOOK_REJECTED).exists())


class ApplyTests(WebhookTestCase):
    def test_a_created_event_creates_the_subscription(self):
        self.seed()
        body = FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0001')
        response = self.deliver(body)
        self.assertEqual(json.loads(response.content)['status'], 'applied')

        subscription = Subscription.objects.get()
        self.assertEqual(subscription.account_id, self.account.id)
        self.assertEqual(subscription.state, Subscription.State.ACTIVE)
        self.assertEqual(subscription.plan.key, 'practice')
        self.assertEqual(subscription.items.count(), 1)

    def test_an_update_moves_the_state(self):
        self.seed()
        self.deliver(
            FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0001')
        )
        self.seed(status='past_due', sequence=2)
        self.deliver(
            FakeProvider.build_event('customer.subscription.updated', 'sub_synthetic_0001')
        )
        self.assertEqual(Subscription.objects.get().state, Subscription.State.PAST_DUE)

    def test_an_unknown_price_is_unresolved_not_defaulted(self):
        """Defaulting would grant a plan's products because a price id was mistyped."""
        FakeProvider.seed_subscription(
            'sub_synthetic_0002',
            customer_id='cus_synthetic_0001',
            prices=[{'price_id': 'price_nobody_configured', 'quantity': 1}],
            organization_id=str(self.account.organization_id),
        )
        body = FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0002')
        response = self.deliver(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)['status'], 'unresolved')
        self.assertEqual(Subscription.objects.count(), 0)
        self.assertEqual(WebhookEvent.objects.get().outcome, WebhookEvent.Outcome.UNRESOLVED)

    def test_an_unattributable_subscription_is_unresolved_and_answered_200(self):
        """The provider retrying will not make an unknown organisation known."""
        FakeProvider.seed_subscription(
            'sub_synthetic_0003',
            customer_id='cus_nobody',
            prices=[{'price_id': self.price.provider_price_id, 'quantity': 1}],
        )
        body = FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0003')
        self.assertEqual(self.deliver(body).status_code, 200)
        self.assertEqual(Subscription.objects.count(), 0)

    def test_an_unhandled_event_type_is_acknowledged(self):
        body = FakeProvider.build_event('invoice.upcoming', 'sub_synthetic_0001')
        response = self.deliver(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(WebhookEvent.objects.get().outcome, WebhookEvent.Outcome.IGNORED)


class IdempotencyTests(WebhookTestCase):
    def test_a_replayed_event_applies_once(self):
        self.seed()
        body = FakeProvider.build_event(
            'customer.subscription.created', 'sub_synthetic_0001', event_id='evt_fixed_1'
        )
        self.deliver(body)
        response = self.deliver(body)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)['status'], 'duplicate')
        self.assertEqual(Subscription.objects.count(), 1)
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.assertEqual(WebhookEvent.objects.get().delivery_count, 2)

    def test_a_replay_is_audited(self):
        self.seed()
        body = FakeProvider.build_event(
            'customer.subscription.created', 'sub_synthetic_0001', event_id='evt_fixed_2'
        )
        self.deliver(body)
        self.deliver(body)
        self.assertTrue(AuditEvent.objects.filter(event=audit_events.WEBHOOK_REPLAYED).exists())

    def test_a_re_delivery_of_identical_state_records_no_state_change(self):
        """Different event ids, same content — the customer saw nothing change."""
        self.seed()
        self.deliver(
            FakeProvider.build_event(
                'customer.subscription.created', 'sub_synthetic_0001', event_id='evt_a'
            )
        )
        before = AuditEvent.objects.filter(event=audit_events.SUBSCRIPTION_STATE_CHANGED).count()
        self.deliver(
            FakeProvider.build_event(
                'customer.subscription.updated', 'sub_synthetic_0001', event_id='evt_b'
            )
        )
        self.assertEqual(
            AuditEvent.objects.filter(event=audit_events.SUBSCRIPTION_STATE_CHANGED).count(),
            before,
        )
        self.assertEqual(
            WebhookEvent.objects.get(provider_event_id='evt_b').outcome,
            WebhookEvent.Outcome.DUPLICATE,
        )

    def test_the_event_ledger_never_holds_the_payload(self):
        self.seed()
        body = FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0001')
        self.deliver(body)
        ledger = WebhookEvent.objects.get()
        self.assertEqual(len(ledger.payload_digest), 64)
        for field in ledger._meta.fields:
            value = getattr(ledger, field.name)
            if isinstance(value, str):
                self.assertNotIn('cus_synthetic', value, f'{field.name} leaked provider data')


class OrderingTests(WebhookTestCase):
    def test_a_stale_event_does_not_move_state_backwards(self):
        """Providers do not guarantee ordering. An older event arriving after a
        newer one must be recorded and discarded, not applied."""
        self.seed(status='canceled', sequence=5)
        self.deliver(
            FakeProvider.build_event(
                'customer.subscription.updated', 'sub_synthetic_0001', event_id='evt_new'
            )
        )
        self.assertEqual(Subscription.objects.get().state, Subscription.State.CANCELED)

        self.seed(status='active', sequence=2)
        response = self.deliver(
            FakeProvider.build_event(
                'customer.subscription.updated', 'sub_synthetic_0001', event_id='evt_old'
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)['status'], 'out_of_order')
        self.assertEqual(Subscription.objects.get().state, Subscription.State.CANCELED)
        self.assertEqual(
            WebhookEvent.objects.get(provider_event_id='evt_old').outcome,
            WebhookEvent.Outcome.OUT_OF_ORDER,
        )

    def test_out_of_order_delivery_is_audited(self):
        self.seed(status='canceled', sequence=5)
        self.deliver(
            FakeProvider.build_event(
                'customer.subscription.updated', 'sub_synthetic_0001', event_id='evt_1'
            )
        )
        self.seed(status='active', sequence=1)
        self.deliver(
            FakeProvider.build_event(
                'customer.subscription.updated', 'sub_synthetic_0001', event_id='evt_2'
            )
        )
        self.assertTrue(AuditEvent.objects.filter(event=audit_events.WEBHOOK_OUT_OF_ORDER).exists())

    def test_a_subscription_cannot_be_moved_to_another_organization(self):
        """The same provider subscription arriving for a second organisation is a
        conflict, never a re-point."""
        from billing.services import BillingConflict, apply_subscription_snapshot

        self.seed()
        self.deliver(
            FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0001')
        )

        other = factories.account()
        with self.assertRaises(BillingConflict):
            apply_subscription_snapshot(
                account=other,
                provider='fake',
                provider_subscription_id='sub_synthetic_0001',
                state=Subscription.State.ACTIVE,
                plan=factories.plan('practice'),
            )


class MappingTests(TestCase):
    def test_an_unknown_provider_status_maps_to_unknown(self):
        """Fails closed and stays visible, rather than being coerced to something
        that grants access."""
        self.assertEqual(subscription_state('some_new_stripe_status'), Subscription.State.UNKNOWN)
        self.assertEqual(subscription_state(''), Subscription.State.UNKNOWN)

    def test_every_known_stripe_status_maps_to_a_declared_state(self):
        declared = {value for value, _ in Subscription.State.choices}
        for status in (
            'trialing',
            'active',
            'past_due',
            'unpaid',
            'paused',
            'canceled',
            'incomplete',
            'incomplete_expired',
        ):
            with self.subTest(status=status):
                self.assertIn(subscription_state(status), declared)


class ReconciliationTests(WebhookTestCase):
    def test_a_report_only_run_finds_drift_and_writes_nothing(self):
        from providers.reconciliation import reconcile

        self.seed()
        self.deliver(
            FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0001')
        )

        # The provider moved on and we never heard about it.
        FakeProvider.subscriptions['sub_synthetic_0001']['status'] = 'canceled'

        run = reconcile(apply=False)
        self.assertEqual(run.status, run.Status.DRIFTED)
        self.assertEqual(run.counts['state_mismatch'], 1)
        self.assertEqual(run.counts['corrected'], 0)
        self.assertEqual(Subscription.objects.get().state, Subscription.State.ACTIVE)

    def test_an_applying_run_corrects_the_drift(self):
        from providers.reconciliation import reconcile

        self.seed()
        self.deliver(
            FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0001')
        )
        FakeProvider.subscriptions['sub_synthetic_0001']['status'] = 'canceled'

        run = reconcile(apply=True)
        self.assertEqual(run.counts['corrected'], 1)
        self.assertEqual(Subscription.objects.get().state, Subscription.State.CANCELED)

    def test_a_matching_run_reports_matched(self):
        from providers.reconciliation import reconcile

        self.seed()
        self.deliver(
            FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0001')
        )
        run = reconcile(apply=False)
        self.assertEqual(run.status, run.Status.MATCHED)
        self.assertEqual(run.counts['matched'], 1)

    def test_the_run_record_holds_counts_and_no_customer_detail(self):
        from providers.reconciliation import reconcile

        self.seed()
        self.deliver(
            FakeProvider.build_event('customer.subscription.created', 'sub_synthetic_0001')
        )
        run = reconcile(apply=False)
        self.assertNotIn('cus_synthetic_0001', json.dumps(run.counts))
        self.assertTrue(all(isinstance(value, int) for value in run.counts.values()))
