"""Organisation-level authorization, and the IDOR refusals.

The organisation UUID in a URL is a lookup key, never an authority. These tests
are the proof of that: each one supplies a UUID the session has no claim to and
asserts the *absence* of the bad outcome.
"""

from __future__ import annotations

import json
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from audit import events
from audit.models import AuditEvent

from . import factories


class OrganizationPageTests(TestCase):
    def setUp(self):
        self.account = factories.account(name='Willow Practice')
        self.user = factories.identity_user()
        self.url = reverse('billing:organization', args=[self.account.organization_id])

    def _sign_in(self, role='organization_admin', organization=None):
        return factories.sign_in(
            self.client,
            self.user,
            [
                {
                    'organization_id': organization or self.account.organization_id,
                    'name': 'Willow Practice',
                    'role': role,
                }
            ],
        )

    def test_an_administrator_sees_the_page(self):
        self._sign_in()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Willow Practice')

    def test_an_ordinary_member_is_refused(self):
        self._sign_in(role='member')
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_a_member_of_a_different_organization_is_refused(self):
        """Cross-organisation IDOR. The URL names one organisation, the session
        holds another, and the URL must lose."""
        other = factories.account(name='Elsewhere Practice')
        self._sign_in(organization=other.organization_id)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_an_anonymous_visitor_is_sent_to_sign_in(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/auth/login/', response['Location'])

    def test_refusal_is_404_not_403(self):
        """A 403 confirms the organisation exists, which makes this an oracle for
        enumerating customer UUIDs."""
        other = factories.account()
        self._sign_in(organization=other.organization_id)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_a_refusal_is_audited(self):
        other = factories.account()
        self._sign_in(organization=other.organization_id)
        self.client.get(self.url)
        self.assertTrue(AuditEvent.objects.filter(event=events.ACCESS_REFUSED).exists())

    def test_a_stale_membership_is_not_relied_on(self):
        """Somebody removed from an organisation this morning must not still be
        managing its billing this afternoon."""
        self._sign_in()
        from identity.models import SessionMembership

        SessionMembership.objects.update(captured_at=timezone.now() - timedelta(days=1))
        self.assertEqual(self.client.get(self.url).status_code, 404)


class PlatformAdminSupportAccessTests(TestCase):
    def setUp(self):
        self.account = factories.account(name='Supported Practice')
        self.url = reverse('billing:organization', args=[self.account.organization_id])

    def test_a_platform_administrator_may_open_an_organization_they_are_not_in(self):
        admin = factories.identity_user(platform_admin=True)
        factories.sign_in(self.client, admin, [])
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_support_access_is_audited_every_time(self):
        admin = factories.identity_user(platform_admin=True)
        factories.sign_in(self.client, admin, [])
        self.client.get(self.url)
        self.client.get(self.url)
        self.assertEqual(AuditEvent.objects.filter(event=events.SUPPORT_ACCESS_USED).count(), 2)

    def test_support_access_is_marked_on_the_audit_row(self):
        admin = factories.identity_user(platform_admin=True)
        factories.sign_in(self.client, admin, [])
        self.client.get(self.url)
        row = AuditEvent.objects.get(event=events.SUPPORT_ACCESS_USED)
        self.assertTrue(row.support_access)
        self.assertEqual(str(row.organization_id), str(self.account.organization_id))

    def test_support_access_is_declared_on_the_page(self):
        admin = factories.identity_user(platform_admin=True)
        factories.sign_in(self.client, admin, [])
        self.assertContains(self.client.get(self.url), 'platform administrator')

    def test_a_member_who_is_also_a_platform_admin_is_not_recorded_as_support(self):
        """Otherwise every staff member's own organisation buries the events that
        actually matter."""
        admin = factories.identity_user(platform_admin=True)
        factories.sign_in(
            self.client,
            admin,
            [{'organization_id': self.account.organization_id, 'role': 'organization_admin'}],
        )
        self.client.get(self.url)
        self.assertFalse(AuditEvent.objects.filter(event=events.SUPPORT_ACCESS_USED).exists())

    def test_platform_admin_status_confers_no_entitlement(self):
        from billing.entitlements import entitlements_for_organization

        admin = factories.identity_user(platform_admin=True)
        factories.sign_in(self.client, admin, [])
        self.client.get(self.url)
        self.assertEqual(
            entitlements_for_organization(self.account.organization_id).entitled_keys, []
        )


class MutationTests(TestCase):
    def setUp(self):
        self.account = factories.account()
        self.user = factories.identity_user()
        factories.sign_in(
            self.client,
            self.user,
            [{'organization_id': self.account.organization_id, 'role': 'organization_admin'}],
        )

    def test_checkout_refuses_a_get(self):
        response = self.client.get(reverse('billing:checkout', args=[self.account.organization_id]))
        self.assertEqual(response.status_code, 405)

    def test_portal_refuses_a_get(self):
        response = self.client.get(reverse('billing:portal', args=[self.account.organization_id]))
        self.assertEqual(response.status_code, 405)

    def test_checkout_is_csrf_protected(self):
        enforcing = self.client_class(enforce_csrf_checks=True)
        factories.sign_in(
            enforcing,
            self.user,
            [{'organization_id': self.account.organization_id, 'role': 'organization_admin'}],
        )
        response = enforcing.post(reverse('billing:checkout', args=[self.account.organization_id]))
        self.assertEqual(response.status_code, 403)

    def test_checkout_is_disabled_in_this_phase(self):
        response = self.client.post(
            reverse('billing:checkout', args=[self.account.organization_id])
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)['error'], 'checkout_disabled')

    def test_a_stranger_cannot_start_checkout_for_someone_else(self):
        other = factories.account()
        response = self.client.post(reverse('billing:checkout', args=[other.organization_id]))
        self.assertEqual(response.status_code, 404)


class SummaryEndpointTests(TestCase):
    """The shape Identity's subscription card reads."""

    def setUp(self):
        self.account = factories.account(name='Card Practice')
        self.user = factories.identity_user()
        self.url = reverse('billing:summary', args=[self.account.organization_id])

    def _sign_in(self, organization=None):
        factories.sign_in(
            self.client,
            self.user,
            [
                {
                    'organization_id': organization or self.account.organization_id,
                    'role': 'organization_admin',
                }
            ],
        )

    def test_no_subscription_reports_none(self):
        self._sign_in()
        body = json.loads(self.client.get(self.url).content)
        self.assertEqual(body['state'], 'none')
        self.assertEqual(body['plan_name'], '')

    def test_an_active_subscription_reports_plan_and_renewal(self):
        self._sign_in()
        factories.subscription(account_obj=self.account)
        body = json.loads(self.client.get(self.url).content)
        self.assertEqual(body['state'], 'active')
        self.assertEqual(body['plan_name'], 'Practice')
        self.assertIsNotNone(body['renews_at'])
        self.assertIsNone(body['ends_at'])

    def test_cancelling_reports_an_end_date_and_no_renewal(self):
        self._sign_in()
        factories.subscription(account_obj=self.account, cancel_at_period_end=True)
        body = json.loads(self.client.get(self.url).content)
        self.assertIsNone(body['renews_at'])
        self.assertIsNotNone(body['ends_at'])

    def test_the_summary_carries_no_provider_or_payment_detail(self):
        self._sign_in()
        factories.subscription(account_obj=self.account)
        raw = self.client.get(self.url).content.decode()
        for forbidden in ('cus_', 'sub_', 'provider_customer_id', 'amount', 'invoice', 'email'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, raw)

    def test_a_stranger_cannot_read_the_summary(self):
        other = factories.account()
        self._sign_in(organization=other.organization_id)
        self.assertEqual(self.client.get(self.url).status_code, 404)


class OrganizationListTests(TestCase):
    def test_only_administered_organizations_are_offered(self):
        administered = factories.account(name='Administered Practice')
        member_only = factories.account(name='Member Only Practice')
        user = factories.identity_user()
        factories.sign_in(
            self.client,
            user,
            [
                {'organization_id': administered.organization_id, 'name': 'Administered Practice'},
                {
                    'organization_id': member_only.organization_id,
                    'name': 'Member Only Practice',
                    'role': 'member',
                },
            ],
        )
        response = self.client.get(reverse('billing:home'))
        self.assertContains(response, 'Administered Practice')
        self.assertNotContains(response, 'Member Only Practice')
