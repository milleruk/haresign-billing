"""The ownership contract, asserted rather than described.

Each of these would pass silently if it were only written in a document, and each
would fail the moment somebody added the "obvious" convenience that breaks the
boundary — a foreign key to a user, a role that grants an entitlement, a stored
`is_entitled` column.
"""

from __future__ import annotations

from datetime import timedelta

from django.apps import apps
from django.db import models
from django.test import TestCase
from django.utils import timezone

from billing.entitlements import entitlements_for_organization
from catalog.seed import PRO_TOOLS

from . import factories

# The apps that hold billing state. `identity` is excluded: it is the OIDC
# relying party and legitimately holds the local user shell.
DOMAIN_APPS = ('billing', 'catalog', 'providers', 'legacy_migration', 'audit')


class NoIdentityForeignKeysTests(TestCase):
    def test_no_domain_model_has_a_foreign_key_to_a_user(self):
        """Every person reference is a bare UUID, so there is no join to abuse."""
        offenders = []
        for app_label in DOMAIN_APPS:
            for model in apps.get_app_config(app_label).get_models():
                for field in model._meta.get_fields():
                    if not isinstance(field, models.ForeignKey):
                        continue
                    related = field.related_model
                    if related._meta.app_label == 'identity':
                        offenders.append(f'{model._meta.label}.{field.name}')
        self.assertEqual(
            offenders,
            [],
            'A billing model gained a foreign key into the identity app. Identity '
            'owns people; Billing holds UUID references only.',
        )

    def test_no_domain_model_stores_a_password_or_role(self):
        forbidden = ('password', 'role_id', 'membership', 'is_staff', 'is_superuser')
        offenders = []
        for app_label in DOMAIN_APPS:
            for model in apps.get_app_config(app_label).get_models():
                for field in model._meta.fields:
                    if any(part in field.name for part in forbidden):
                        offenders.append(f'{model._meta.label}.{field.name}')
        self.assertEqual(offenders, [], 'Billing must not duplicate Identity credentials or roles.')


class EntitlementsAreDerivedTests(TestCase):
    def test_no_model_stores_a_writable_entitlement_flag(self):
        """There is exactly one answer to 'is this organisation entitled', and it
        is computed. A stored flag would be a second one."""
        offenders = []
        for app_label in DOMAIN_APPS:
            for model in apps.get_app_config(app_label).get_models():
                for field in model._meta.fields:
                    name = field.name.lower()
                    if name in ('is_entitled', 'entitled', 'has_access', 'entitlements'):
                        offenders.append(f'{model._meta.label}.{field.name}')
        self.assertEqual(offenders, [])


class RolesDoNotGrantEntitlementsTests(TestCase):
    def test_a_platform_administrator_holds_no_paid_entitlement(self):
        """The monolith granted every entitlement to `is_staff`. Deliberately not
        reproduced — see docs/entitlements.md, D-1."""
        account = factories.account()
        admin = factories.identity_user(platform_admin=True)
        del admin  # existing at all must change nothing about the organisation

        result = entitlements_for_organization(account.organization_id)
        self.assertEqual(result.entitled_keys, [])

    def test_an_organization_administrator_holds_no_paid_entitlement(self):
        account = factories.account()
        user = factories.identity_user()
        client = factories.sign_in(
            self.client,
            user,
            [{'organization_id': account.organization_id, 'role': 'organization_admin'}],
        )
        del client
        self.assertEqual(entitlements_for_organization(account.organization_id).entitled_keys, [])

    def test_paying_does_not_create_a_membership(self):
        """The reverse direction: a subscription must not conjure an Identity role."""
        from identity.models import SessionMembership

        account = factories.account()
        factories.subscription(account_obj=account)
        self.assertTrue(entitlements_for_organization(account.organization_id).holds(PRO_TOOLS))
        self.assertEqual(SessionMembership.objects.count(), 0)


class AuditMinimisationTests(TestCase):
    def test_audit_metadata_drops_credential_and_payment_shaped_keys(self):
        from audit.models import AuditEvent
        from audit.services import record

        record(
            'test.event',
            metadata={
                'plan': 'practice',
                'stripe_secret': 'sk_live_should_never_land',
                'card_last4': '4242',
                'customer_email': 'person@example.invalid',
                'raw_payload': {'everything': 'about a customer'},
                'signature': 'v1=deadbeef',
                'iban': 'GB00TEST00000000000000',
            },
        )
        stored = AuditEvent.objects.get(event='test.event').metadata
        self.assertEqual(stored['plan'], 'practice')
        for key in (
            'stripe_secret',
            'card_last4',
            'customer_email',
            'raw_payload',
            'signature',
            'iban',
        ):
            with self.subTest(key=key):
                self.assertEqual(stored[key], '[redacted]')

    def test_audit_events_cannot_be_modified_or_deleted(self):
        from audit.models import AuditEvent, AuditEventImmutableError
        from audit.services import record

        event = record('test.event', metadata={'plan': 'practice'})
        event.event = 'test.rewritten'
        with self.assertRaises(AuditEventImmutableError):
            event.save()
        with self.assertRaises(AuditEventImmutableError):
            event.delete()
        self.assertEqual(AuditEvent.objects.get(pk=event.pk).event, 'test.event')


class IdentityUserShellTests(TestCase):
    def test_setting_a_password_is_refused(self):
        user = factories.identity_user()
        with self.assertRaises(RuntimeError):
            user.set_password('a real password nobody should be able to store')

    def test_users_are_created_without_a_usable_password(self):
        self.assertFalse(factories.identity_user().has_usable_password())

    def test_session_memberships_expire_with_the_session(self):
        """They are a claim about this login, not a copy of Identity's table."""
        from identity.models import SessionMembership

        account = factories.account()
        user = factories.identity_user()
        factories.sign_in(self.client, user, [{'organization_id': account.organization_id}])
        self.assertEqual(SessionMembership.objects.count(), 1)

        stale = SessionMembership.objects.first()
        stale.captured_at = timezone.now() - timedelta(days=7)
        stale.save(update_fields=['captured_at'])

        from identity.authorization import resolve_organization

        request = type('R', (), {})()
        request.user = user
        request.session = self.client.session
        self.assertIsNone(resolve_organization(request, account.organization_id))
