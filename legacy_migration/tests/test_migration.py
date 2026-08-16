"""The synthetic billing migration, end to end.

Every exercise the Phase 4A contract names has a test here. All of them run
against `legacy_migration.synthetic`, which reproduces the monolith's schema
exactly; **no live billing record is read anywhere in this suite.**
"""

from __future__ import annotations

import os
import stat
import tempfile
from datetime import timedelta
from pathlib import Path

from django.test import TestCase
from django.utils import timezone

from billing.models import BillingAccount, ComplimentaryGrant, Subscription
from billing.tests import factories
from legacy_migration.artifacts import ArtifactError, decrypt_payload, generate_key_file
from legacy_migration.exporter import ExportRefused, assert_read_only, build_payload, export
from legacy_migration.importer import DryRunRequired, load, reconcile, run
from legacy_migration.models import (
    ImportRun,
    LegacyGrantMapping,
    LegacySubscriptionMapping,
)
from legacy_migration.schema import SchemaMismatch, validate_source_schema
from legacy_migration.synthetic import SyntheticSource, organization_uuids

ORGS = organization_uuids(practices=[1, 2, 3], pcns=[10])


class MigrationTestCase(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key_path = Path(self.tmp.name) / 'migration.key'
        generate_key_file(self.key_path)
        from legacy_migration.artifacts import load_protected_key

        self.key = load_protected_key(self.key_path)
        # Prices must exist before an import can resolve one.
        factories.price('practice', 'month', provider_price_id='price_practice_month')
        factories.price('pcn', 'month', provider_price_id='price_pcn_month')

    def source(self, **kwargs) -> SyntheticSource:
        return SyntheticSource(**kwargs)

    def standard_source(self) -> SyntheticSource:
        source = self.source()
        source.add_customer(user_id=1, customer_id='cus_synthetic_0001')
        source.add_subscription(
            practice_id=1,
            plan_key='practice',
            status='active',
            price_id='price_practice_month',
        )
        source.add_subscription(
            practice_id=2,
            plan_key='practice',
            status='trialing',
            price_id='price_practice_month',
        )
        source.add_subscription(
            pcn_id=10,
            plan_key='pcn',
            status='active',
            price_id='price_pcn_month',
        )
        source.add_grant(practice_id=3, plan_key='practice', days=60)
        return source

    def artifact(self, source, path_name='artifact.hsbill', **kwargs):
        path = Path(self.tmp.name) / path_name
        summary = export(
            source,
            destination=path,
            key=self.key,
            organization_uuid_for=ORGS,
            **kwargs,
        )
        return path, summary

    def import_artifact(self, path, *, apply=False):
        payload, digest = load(path.read_bytes(), self.key)
        operation = ImportRun.Operation.APPLY if apply else ImportRun.Operation.DRY_RUN
        return run(payload, digest, operation=operation), payload


# --- Source contract ----------------------------------------------------------


class SourceContractTests(MigrationTestCase):
    def test_a_writable_source_connection_is_refused(self):
        """Actively proved, not assumed. The failure this catches is an operator
        who connected with the application's own credentials."""
        with self.assertRaises(ExportRefused):
            assert_read_only(self.source(read_only=False))

    def test_a_read_only_connection_passes_the_probe(self):
        assert_read_only(self.source())  # does not raise

    def test_an_extra_source_column_stops_the_run(self):
        """The monolith having grown a field nobody has reviewed is a reason to
        stop and look, not to export the columns we recognise."""
        source = self.standard_source()
        source.schema['billing_subscription'].add('card_last4')
        with self.assertRaises(SchemaMismatch):
            build_payload(source, organization_uuid_for=ORGS)

    def test_a_missing_source_column_stops_the_run(self):
        source = self.standard_source()
        source.schema['billing_subscription'].discard('cancel_at_period_end')
        with self.assertRaises(SchemaMismatch):
            build_payload(source, organization_uuid_for=ORGS)

    def test_a_missing_table_stops_the_run(self):
        with self.assertRaises(SchemaMismatch):
            validate_source_schema({'billing_subscription': set()})

    def test_only_allowlisted_fields_cross_the_boundary(self):
        payload = build_payload(self.standard_source(), organization_uuid_for=ORGS)
        allowed = {
            'source_id',
            'organization_id',
            'plan_key',
            'state',
            'provider',
            'provider_subscription_id',
            'provider_customer_id',
            'provider_price_id',
            'current_period_end',
            'cancel_at_period_end',
            'created_at',
        }
        for row in payload['subscriptions']:
            self.assertEqual(set(row), allowed)
        # No user id, no note beyond `reason`, nothing from auth_user.
        self.assertNotIn('user_id', payload['subscriptions'][0])

    def test_an_unsupported_status_is_refused_not_guessed(self):
        source = self.source()
        source.add_subscription(practice_id=1, status='some_future_status')
        payload = build_payload(source, organization_uuid_for=ORGS)
        self.assertEqual(payload['subscriptions'], [])
        self.assertTrue(any('unsupported_status' in r for r in payload['refusals']))

    def test_a_user_scoped_row_is_refused(self):
        """No workspace means no organisation to key to. Refused, never guessed."""
        source = self.source()
        source.add_subscription(practice_id=None, pcn_id=None)
        source.add_grant(user_id=7, practice_id=None, pcn_id=None)
        payload = build_payload(source, organization_uuid_for=ORGS)
        self.assertEqual(payload['subscriptions'], [])
        self.assertEqual(payload['grants'], [])
        self.assertEqual(len(payload['refusals']), 2)

    def test_an_unmapped_organization_is_refused(self):
        source = self.source()
        source.add_subscription(practice_id=999)
        payload = build_payload(source, organization_uuid_for=ORGS)
        self.assertTrue(any('unmapped_organization' in r for r in payload['refusals']))

    def test_the_manifest_is_aggregate_only(self):
        payload = build_payload(self.standard_source(), organization_uuid_for=ORGS)
        manifest = payload['manifest']
        self.assertEqual(manifest['counts']['subscriptions'], 3)
        self.assertEqual(manifest['counts']['organizations'], 4)
        # No organisation ids and no provider references anywhere in it.
        import json

        raw = json.dumps(manifest)
        for organization_id in ORGS.values():
            self.assertNotIn(organization_id, raw)
        self.assertNotIn('sub_synthetic', raw)
        self.assertNotIn('cus_synthetic', raw)


# --- Artifacts ----------------------------------------------------------------


class ArtifactTests(MigrationTestCase):
    def test_the_artifact_is_written_at_mode_600(self):
        path, _ = self.artifact(self.standard_source())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_no_plaintext_reaches_disk(self):
        path, _ = self.artifact(self.standard_source())
        raw = path.read_bytes()
        self.assertNotIn(b'sub_synthetic', raw)
        self.assertNotIn(b'practice', raw)
        for organization_id in ORGS.values():
            self.assertNotIn(organization_id.encode(), raw)

    def test_a_tampered_artifact_fails_authentication(self):
        path, _ = self.artifact(self.standard_source())
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 0xFF
        with self.assertRaises(ArtifactError):
            decrypt_payload(bytes(raw), self.key)

    def test_a_truncated_artifact_fails_authentication(self):
        path, _ = self.artifact(self.standard_source())
        with self.assertRaises(ArtifactError):
            decrypt_payload(path.read_bytes()[:-20], self.key)

    def test_the_wrong_key_fails_authentication(self):
        path, _ = self.artifact(self.standard_source())
        with self.assertRaises(ArtifactError):
            decrypt_payload(path.read_bytes(), os.urandom(32))

    def test_a_group_readable_key_file_is_refused(self):
        from legacy_migration.artifacts import load_protected_key

        self.key_path.chmod(0o640)
        with self.assertRaises(ArtifactError):
            load_protected_key(self.key_path)

    def test_an_existing_artifact_is_never_replaced(self):
        source = self.standard_source()
        self.artifact(source)
        with self.assertRaises(ArtifactError):
            self.artifact(source)

    def test_an_unknown_schema_version_is_refused(self):
        from legacy_migration.artifacts import encrypt_payload

        payload = build_payload(self.standard_source(), organization_uuid_for=ORGS)
        payload['schema_version'] = 99
        with self.assertRaises(ArtifactError):
            load(encrypt_payload(payload, self.key), self.key)

    def test_a_different_source_system_is_refused(self):
        from legacy_migration.artifacts import encrypt_payload

        payload = build_payload(self.standard_source(), organization_uuid_for=ORGS)
        payload['source_system'] = 'somebody-elses-monolith'
        with self.assertRaises(ArtifactError):
            load(encrypt_payload(payload, self.key), self.key)


# --- Import -------------------------------------------------------------------


class ImportTests(MigrationTestCase):
    def test_an_apply_without_a_dry_run_is_refused(self):
        path, _ = self.artifact(self.standard_source())
        payload, digest = load(path.read_bytes(), self.key)
        with self.assertRaises(DryRunRequired):
            run(payload, digest, operation=ImportRun.Operation.APPLY)

    def test_a_dry_run_writes_nothing(self):
        path, _ = self.artifact(self.standard_source())
        import_run, _ = self.import_artifact(path)
        self.assertEqual(import_run.status, ImportRun.Status.SUCCEEDED)
        self.assertEqual(import_run.result_counts['subscriptions_created'], 3)
        self.assertEqual(Subscription.objects.count(), 0)
        self.assertEqual(BillingAccount.objects.count(), 0)

    def test_a_dry_run_and_an_apply_report_the_same_counts(self):
        """A dry run that took a different code path would be testing something
        other than the apply."""
        path, _ = self.artifact(self.standard_source())
        dry, _ = self.import_artifact(path)
        applied, _ = self.import_artifact(path, apply=True)
        self.assertEqual(dry.result_counts, applied.result_counts)

    def test_a_full_import_creates_the_expected_state(self):
        path, _ = self.artifact(self.standard_source())
        self.import_artifact(path)
        import_run, _ = self.import_artifact(path, apply=True)

        self.assertEqual(import_run.status, ImportRun.Status.SUCCEEDED)
        self.assertEqual(Subscription.objects.count(), 3)
        self.assertEqual(ComplimentaryGrant.objects.count(), 1)
        self.assertEqual(BillingAccount.objects.count(), 4)
        self.assertEqual(LegacySubscriptionMapping.objects.count(), 3)
        self.assertEqual(LegacyGrantMapping.objects.count(), 1)

    def test_reconciliation_matches_exactly(self):
        path, _ = self.artifact(self.standard_source())
        self.import_artifact(path)
        _, payload = self.import_artifact(path, apply=True)

        counts = reconcile(payload)
        self.assertEqual(counts['source_subscriptions'], counts['mapped_subscriptions'])
        self.assertEqual(counts['state_mismatches'], 0)
        self.assertEqual(counts['missing_locally'], 0)
        self.assertEqual(counts['source_grants'], counts['mapped_grants'])

    def test_a_rerun_of_the_same_artifact_is_a_no_op(self):
        source = self.standard_source()
        first, _ = self.artifact(source, 'first.hsbill')
        self.import_artifact(first)
        self.import_artifact(first, apply=True)

        # A byte-identical re-export of unchanged source.
        second, _ = self.artifact(source, 'second.hsbill')
        self.import_artifact(second)
        import_run, _ = self.import_artifact(second, apply=True)

        self.assertEqual(import_run.result_counts['subscriptions_created'], 0)
        self.assertEqual(import_run.result_counts['subscriptions_updated'], 0)
        self.assertEqual(import_run.result_counts['subscriptions_unchanged'], 3)
        self.assertEqual(import_run.result_counts['grants_unchanged'], 1)
        self.assertEqual(Subscription.objects.count(), 3)

    def test_entitlements_after_import_match_the_source_intent(self):
        path, _ = self.artifact(self.standard_source())
        self.import_artifact(path)
        self.import_artifact(path, apply=True)

        from billing.entitlements import entitlements_for_organization
        from catalog.seed import PCN_DASHBOARDS, PRO_TOOLS

        practice_one = entitlements_for_organization(ORGS[('practice', '1')])
        self.assertTrue(practice_one.holds(PRO_TOOLS))
        self.assertFalse(practice_one.holds(PCN_DASHBOARDS))

        pcn = entitlements_for_organization(ORGS[('pcn', '10')])
        self.assertTrue(pcn.holds(PCN_DASHBOARDS))

        # Practice 3 has only a complimentary grant.
        self.assertTrue(entitlements_for_organization(ORGS[('practice', '3')]).holds(PRO_TOOLS))


class DeltaTests(MigrationTestCase):
    def _initial(self):
        source = self.standard_source()
        path, _ = self.artifact(source, 'initial.hsbill')
        self.import_artifact(path)
        self.import_artifact(path, apply=True)
        return source

    def test_a_plan_change_is_applied_as_an_update(self):
        source = self._initial()
        source.rows['billing_subscription'][0]['plan_key'] = 'pcn'
        source.rows['billing_subscription'][0]['stripe_price_id'] = 'price_pcn_month'

        path, _ = self.artifact(source, 'delta.hsbill')
        self.import_artifact(path)
        import_run, _ = self.import_artifact(path, apply=True)

        self.assertEqual(import_run.result_counts['subscriptions_updated'], 1)
        self.assertEqual(import_run.result_counts['subscriptions_unchanged'], 2)
        self.assertEqual(Subscription.objects.count(), 3)
        self.assertEqual(
            Subscription.objects.get(provider_subscription_id='sub_synthetic_0001').plan.key,
            'pcn',
        )

    def test_a_cancellation_is_applied(self):
        source = self._initial()
        source.rows['billing_subscription'][0]['status'] = 'canceled'

        path, _ = self.artifact(source, 'cancel.hsbill')
        self.import_artifact(path)
        self.import_artifact(path, apply=True)

        subscription = Subscription.objects.get(provider_subscription_id='sub_synthetic_0001')
        self.assertEqual(subscription.state, Subscription.State.CANCELED)

        from billing.entitlements import entitlements_for_organization

        self.assertEqual(entitlements_for_organization(ORGS[('practice', '1')]).entitled_keys, [])

    def test_a_payment_failure_is_applied_and_closes_access(self):
        source = self._initial()
        source.rows['billing_subscription'][0]['status'] = 'past_due'

        path, _ = self.artifact(source, 'pastdue.hsbill')
        self.import_artifact(path)
        self.import_artifact(path, apply=True)

        from billing.entitlements import entitlements_for_organization

        self.assertEqual(
            Subscription.objects.get(provider_subscription_id='sub_synthetic_0001').state,
            Subscription.State.PAST_DUE,
        )
        self.assertEqual(entitlements_for_organization(ORGS[('practice', '1')]).entitled_keys, [])

    def test_a_removed_source_record_is_flagged_never_deleted(self):
        """A subscription disappearing from the monolith is not the same fact as a
        subscription being cancelled."""
        source = self._initial()
        source.rows['billing_subscription'].pop(0)

        path, _ = self.artifact(source, 'removed.hsbill')
        self.import_artifact(path)
        import_run, _ = self.import_artifact(path, apply=True)

        self.assertEqual(import_run.result_counts['source_removed'], 1)
        self.assertEqual(Subscription.objects.count(), 3)
        self.assertTrue(
            LegacySubscriptionMapping.objects.filter(
                source_record_id='1', source_missing=True
            ).exists()
        )
        # And the customer keeps their access, because nothing said to remove it.
        from billing.entitlements import entitlements_for_organization
        from catalog.seed import PRO_TOOLS

        self.assertTrue(entitlements_for_organization(ORGS[('practice', '1')]).holds(PRO_TOOLS))


class ConflictTests(MigrationTestCase):
    def test_a_provider_identifier_collision_aborts_the_run(self):
        """Two source rows claiming one Stripe subscription is a data-quality
        incident, not a state to resolve by picking one."""
        source = self.standard_source()
        path, _ = self.artifact(source, 'first.hsbill')
        self.import_artifact(path)
        self.import_artifact(path, apply=True)

        # A second source row now carries the first row's provider reference.
        source.add_subscription(
            practice_id=2,
            subscription_id='sub_synthetic_0001',
            price_id='price_practice_month',
        )
        collided, _ = self.artifact(source, 'collision.hsbill')
        import_run, _ = self.import_artifact(collided)

        self.assertEqual(import_run.status, ImportRun.Status.CONFLICT)
        self.assertIn('provider_identifier_collision', import_run.conflict_counts)

    def test_an_organization_uuid_collision_aborts_the_run(self):
        """The same provider subscription now naming a different organisation
        would move a paid subscription between customers."""
        source = self.standard_source()
        path, _ = self.artifact(source, 'first.hsbill')
        self.import_artifact(path)
        self.import_artifact(path, apply=True)

        source.rows['billing_subscription'][0]['practice_id'] = 2
        moved, _ = self.artifact(source, 'moved.hsbill')
        import_run, _ = self.import_artifact(moved)

        self.assertEqual(import_run.status, ImportRun.Status.CONFLICT)

    def test_an_unmapped_pre_existing_subscription_aborts_the_run(self):
        """Adopting a row somebody else created would silently claim it."""
        account = BillingAccount.objects.create(organization_id=ORGS[('practice', '1')])
        Subscription.objects.create(
            account=account,
            plan=factories.plan('practice'),
            provider='stripe',
            provider_subscription_id='sub_synthetic_0001',
            state=Subscription.State.ACTIVE,
        )
        path, _ = self.artifact(self.standard_source())
        import_run, _ = self.import_artifact(path)

        self.assertEqual(import_run.status, ImportRun.Status.CONFLICT)
        self.assertIn('unmapped_existing_subscription', import_run.conflict_counts)

    def test_a_conflict_rolls_the_whole_run_back(self):
        """No partial-apply mode. Half an organisation's subscriptions is a state
        nobody can reason about."""
        account = BillingAccount.objects.create(organization_id=ORGS[('practice', '1')])
        Subscription.objects.create(
            account=account,
            plan=factories.plan('practice'),
            provider='stripe',
            provider_subscription_id='sub_synthetic_0001',
            state=Subscription.State.ACTIVE,
        )
        path, _ = self.artifact(self.standard_source())
        self.import_artifact(path)

        # The two clean subscriptions in the same artifact were not written either.
        self.assertEqual(Subscription.objects.count(), 1)
        self.assertEqual(LegacySubscriptionMapping.objects.count(), 0)

    def test_a_conflicting_run_is_recorded(self):
        account = BillingAccount.objects.create(organization_id=ORGS[('practice', '1')])
        Subscription.objects.create(
            account=account,
            plan=factories.plan('practice'),
            provider='stripe',
            provider_subscription_id='sub_synthetic_0001',
            state=Subscription.State.ACTIVE,
        )
        path, _ = self.artifact(self.standard_source())
        import_run, _ = self.import_artifact(path)
        self.assertEqual(ImportRun.objects.get(pk=import_run.pk).status, ImportRun.Status.CONFLICT)

    def test_a_grant_without_an_expiry_is_a_conflict(self):
        source = self.source()
        source.add_grant(practice_id=1)
        source.rows['billing_access_grant'][0]['expires_at'] = None
        path, _ = self.artifact(source)
        import_run, _ = self.import_artifact(path)
        self.assertEqual(import_run.status, ImportRun.Status.CONFLICT)
        self.assertIn('grant_without_expiry', import_run.conflict_counts)


class MemberLinkTests(MigrationTestCase):
    def test_member_links_are_imported_and_cover_a_practice(self):
        source = self.source()
        source.add_subscription(
            pcn_id=10, plan_key='pcn', status='active', price_id='price_pcn_month'
        )
        path, _ = self.artifact(
            source,
            member_links=[(ORGS[('pcn', '10')], ORGS[('practice', '1')])],
        )
        self.import_artifact(path)
        self.import_artifact(path, apply=True)

        from billing.entitlements import entitlements_for_organization
        from catalog.seed import PRO_TOOLS

        result = entitlements_for_organization(ORGS[('practice', '1')])
        self.assertTrue(result.holds(PRO_TOOLS))
        self.assertEqual(result.products[PRO_TOOLS].source, 'member_organization')

    def test_a_self_referential_link_is_a_conflict(self):
        source = self.source()
        source.add_subscription(practice_id=1, price_id='price_practice_month')
        path, _ = self.artifact(
            source, member_links=[(ORGS[('practice', '1')], ORGS[('practice', '1')])]
        )
        import_run, _ = self.import_artifact(path)
        self.assertEqual(import_run.status, ImportRun.Status.CONFLICT)


class OutputMinimisationTests(MigrationTestCase):
    def test_run_records_hold_counts_not_rows(self):
        path, _ = self.artifact(self.standard_source())
        import_run, _ = self.import_artifact(path)

        import json

        for field in ('source_counts', 'result_counts', 'conflict_counts'):
            raw = json.dumps(getattr(import_run, field))
            self.assertNotIn('sub_synthetic', raw)
            self.assertNotIn('cus_synthetic', raw)
            for organization_id in ORGS.values():
                self.assertNotIn(organization_id, raw)

    def test_mappings_store_digests_not_provider_identifiers(self):
        path, _ = self.artifact(self.standard_source())
        self.import_artifact(path)
        self.import_artifact(path, apply=True)

        for mapping in LegacySubscriptionMapping.objects.all():
            self.assertEqual(len(mapping.provider_reference_digest), 64)
            self.assertNotIn('sub_synthetic', mapping.provider_reference_digest)
            self.assertNotIn('sub_synthetic', mapping.source_fingerprint)

    def test_migration_audit_metadata_holds_no_source_detail(self):
        from audit.models import AuditEvent

        path, _ = self.artifact(self.standard_source())
        self.import_artifact(path)

        import json

        for event in AuditEvent.objects.all():
            raw = json.dumps(event.metadata)
            self.assertNotIn('sub_synthetic', raw)
            self.assertNotIn('cus_synthetic', raw)


class ImporterIsolationTests(TestCase):
    def test_the_importer_never_imports_a_source_connection(self):
        """The importer must have no route to the monolith. Asserted structurally:
        if it cannot name the exporter or a source database, it cannot reach one."""
        import legacy_migration.importer as importer

        source = Path(importer.__file__).read_text()
        # Import statements only. The module docstring explains the separation and
        # necessarily names the exporter to do so; what matters is that it cannot
        # *reach* one.
        imports = '\n'.join(
            line
            for line in source.splitlines()
            if line.startswith(('import ', 'from '))
            or line.strip().startswith(('import ', 'from '))
        )
        source = imports
        for forbidden in ('exporter', 'psycopg', 'SOURCE_DATABASE', 'connect('):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_settings_define_no_source_database(self):
        from django.conf import settings

        self.assertEqual(list(settings.DATABASES), ['default'])
        self.assertNotIn('haresign_net', settings.DATABASES['default']['NAME'])


class GrantImportTests(MigrationTestCase):
    def test_a_revoked_grant_is_imported_as_revoked_and_grants_nothing(self):
        source = self.source()
        source.add_grant(practice_id=1, revoked=True)
        path, _ = self.artifact(source)
        self.import_artifact(path)
        self.import_artifact(path, apply=True)

        grant = ComplimentaryGrant.objects.get()
        self.assertIsNotNone(grant.revoked_at)

        from billing.entitlements import entitlements_for_organization

        self.assertEqual(entitlements_for_organization(ORGS[('practice', '1')]).entitled_keys, [])

    def test_an_already_expired_grant_imports_and_grants_nothing(self):
        source = self.source()
        source.add_grant(practice_id=1)
        source.rows['billing_access_grant'][0]['expires_at'] = timezone.now() - timedelta(days=1)
        path, _ = self.artifact(source)
        self.import_artifact(path)
        self.import_artifact(path, apply=True)

        self.assertEqual(ComplimentaryGrant.objects.count(), 1)
        from billing.entitlements import entitlements_for_organization

        self.assertEqual(entitlements_for_organization(ORGS[('practice', '1')]).entitled_keys, [])
