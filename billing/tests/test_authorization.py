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

    def _sign_in(self, role=factories.ADMIN_ROLE, organization=None):
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


class PlatformAdministrationGrantsNothingTests(TestCase):
    """The Phase 4A support bypass is gone, and these assert its absence.

    Phase 4A let a platform administrator open any organisation's billing without
    a membership, recording an audit event each time. Phase 4B removed it: a role
    that grants billing access is exactly the shape the ownership contract exists
    to forbid, and Identity does not emit a platform-administrator claim at all,
    so the bypass could only ever have fired in the synthetic rehearsal.

    These tests are deliberately the *inverse* of the ones they replace. Anyone
    reinstating the bypass has to delete an assertion that says, in words, that it
    must not exist.
    """

    def setUp(self):
        self.account = factories.account(name='Supported Practice')
        self.url = reverse('billing:organization', args=[self.account.organization_id])

    def test_a_platform_administrator_cannot_open_an_organization_they_are_not_in(self):
        user = factories.identity_user()
        factories.sign_in(self.client, user, [])
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_a_django_staff_flag_does_not_open_an_organization(self):
        """`is_staff` is Django admin access here and nothing else. It must not
        become a second route into a customer's billing."""
        user = factories.identity_user()
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=['is_staff', 'is_superuser'])
        factories.sign_in(self.client, user, [])
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_no_support_access_event_is_ever_emitted(self):
        user = factories.identity_user()
        user.is_staff = True
        user.save(update_fields=['is_staff'])
        factories.sign_in(self.client, user, [])
        self.client.get(self.url)
        self.assertFalse(AuditEvent.objects.filter(event=events.SUPPORT_ACCESS_USED).exists())

    def test_the_user_model_holds_no_platform_administrator_flag(self):
        """The field is gone, not merely unread. A flag nobody reads is a flag
        somebody reads again in six months."""
        field_names = {field.name for field in factories.identity_user()._meta.get_fields()}
        self.assertNotIn('is_platform_admin', field_names)

    def test_a_platform_administrator_who_is_an_org_admin_gets_in_that_way(self):
        """The one route that still works, and it is the ordinary one."""
        user = factories.identity_user()
        user.is_staff = True
        user.save(update_fields=['is_staff'])
        factories.sign_in(
            self.client,
            user,
            [{'organization_id': self.account.organization_id, 'role': factories.ADMIN_ROLE}],
        )
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertFalse(AuditEvent.objects.filter(event=events.SUPPORT_ACCESS_USED).exists())

    def test_the_organization_page_never_mentions_support_access(self):
        user = factories.identity_user()
        factories.sign_in(
            self.client,
            user,
            [{'organization_id': self.account.organization_id, 'role': factories.ADMIN_ROLE}],
        )
        self.assertNotContains(self.client.get(self.url), 'platform administrator')


class RoleKeyTests(TestCase):
    """The role key Identity actually emits, and the one Phase 4A guessed."""

    def setUp(self):
        self.account = factories.account()
        self.url = reverse('billing:organization', args=[self.account.organization_id])
        self.user = factories.identity_user()

    def test_identitys_dotted_admin_key_is_accepted(self):
        factories.sign_in(
            self.client,
            self.user,
            [{'organization_id': self.account.organization_id, 'role': 'organization.admin'}],
        )
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_the_phase_4a_underscored_key_is_not_accepted(self):
        """`organization_admin` is not a role Identity has ever emitted. Accepting
        it would mean accepting a role key from somewhere other than Identity."""
        factories.sign_in(
            self.client,
            self.user,
            [{'organization_id': self.account.organization_id, 'role': 'organization_admin'}],
        )
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_the_ordinary_member_key_is_not_accepted(self):
        factories.sign_in(
            self.client,
            self.user,
            [{'organization_id': self.account.organization_id, 'role': 'organization.member'}],
        )
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_a_role_containing_admin_as_a_substring_is_not_accepted(self):
        """Compared exactly, never by substring. "administrator" as a substring
        also matches roles that are not one."""
        for role in ('organization.admin.readonly', 'not.organization.admin', 'admin'):
            with self.subTest(role=role):
                factories.sign_in(
                    self.client,
                    factories.identity_user(),
                    [{'organization_id': self.account.organization_id, 'role': role}],
                )
                self.assertEqual(self.client.get(self.url).status_code, 404)


class MutationTests(TestCase):
    def setUp(self):
        self.account = factories.account()
        self.user = factories.identity_user()
        factories.sign_in(
            self.client,
            self.user,
            [{'organization_id': self.account.organization_id, 'role': factories.ADMIN_ROLE}],
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
            [{'organization_id': self.account.organization_id, 'role': factories.ADMIN_ROLE}],
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
                    'role': factories.ADMIN_ROLE,
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
