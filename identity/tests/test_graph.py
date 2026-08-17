"""The organisation-graph projection: fetching it, trusting it, and refusing it.

Decision D-4, made executable. The property under test throughout is that a
projection is only ever relied on when it is *current and understood*, and that
every other case fails closed — with the single, deliberate exception that a
practice's own subscription is never affected.

Nothing here opens a socket. `fetch_document` is patched; what is exercised is
the validation, the versioning and the consequences, which is where the bugs
would be.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlparse

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from audit import events
from audit.models import AuditEvent
from billing.models import EntitlementAllocation, OperationalAlert
from billing.tests import factories
from identity.graph import (
    GraphError,
    GraphUnavailable,
    apply_document,
    current_graph,
    graph_version,
    member_organizations,
    organization_is_active,
    refresh,
    require_fresh_graph,
    sponsorship_is_valid,
)
from identity.graph_models import OrganizationGraph
from identity.service_auth import sign_request


def document(*, organizations=(), relationships=(), generated_at=None, schema_version=1):
    """Build a projection document exactly as Identity serves one."""
    body = {
        'schema_version': schema_version,
        'organizations': [
            {
                'organization_id': str(org_id),
                'organization_type': org_type,
                'is_active': is_active,
            }
            for org_id, org_type, is_active in organizations
        ],
        'relationships': [
            {'parent_organization_id': str(parent), 'child_organization_id': str(child)}
            for parent, child in relationships
        ],
    }
    body['graph_version'] = graph_version(body)
    body['generated_at'] = (generated_at or timezone.now()).isoformat()
    return body


class ApplyDocumentTests(TestCase):
    def setUp(self):
        self.pcn = factories.organization_id()
        self.practice = factories.organization_id()

    def _valid(self, **overrides):
        return document(
            organizations=[(self.pcn, 'pcn', True), (self.practice, 'practice', True)],
            relationships=[(self.pcn, self.practice)],
            **overrides,
        )

    def test_a_valid_document_becomes_the_current_projection(self):
        apply_document(self._valid())
        graph = current_graph()
        self.assertIsNotNone(graph)
        self.assertEqual(graph.organization_count, 2)
        self.assertEqual(graph.relationship_count, 1)
        self.assertTrue(graph.is_fresh)

    def test_the_edges_are_readable(self):
        apply_document(self._valid())
        graph = current_graph()
        self.assertEqual(member_organizations(self.pcn, graph=graph), {str(self.practice)})
        self.assertEqual(member_organizations(self.practice, graph=graph), set())

    def test_an_unknown_schema_version_is_refused(self):
        """Refused, not parsed optimistically. That is the entire reason Identity
        puts a version in the document."""
        with self.assertRaises(GraphError):
            apply_document(self._valid(schema_version=99))
        self.assertIsNone(current_graph())

    def test_a_document_with_no_version_is_refused(self):
        body = self._valid()
        del body['graph_version']
        with self.assertRaises(GraphError):
            apply_document(body)

    def test_a_document_with_no_generation_time_is_refused(self):
        body = self._valid()
        del body['generated_at']
        with self.assertRaises(GraphError):
            apply_document(body)

    def test_an_edge_naming_an_undescribed_organization_is_refused(self):
        """Otherwise the consumer holds an edge to a UUID it knows nothing about
        and has to guess what it meant."""
        body = document(
            organizations=[(self.pcn, 'pcn', True)],
            relationships=[(self.pcn, self.practice)],
        )
        with self.assertRaises(GraphError):
            apply_document(body)

    def test_a_malformed_document_is_refused(self):
        with self.assertRaises(GraphError):
            apply_document({'schema_version': 1, 'graph_version': 'x', 'generated_at': 'nope'})

    def test_refusal_leaves_the_previous_projection_in_place(self):
        """A bad fetch must not cost us the good projection we already hold."""
        apply_document(self._valid())
        held = current_graph().graph_version
        with self.assertRaises(GraphError):
            apply_document(self._valid(schema_version=99))
        self.assertEqual(current_graph().graph_version, held)

    def test_applying_the_same_content_twice_does_not_duplicate_it(self):
        apply_document(self._valid())
        apply_document(self._valid())
        self.assertEqual(OrganizationGraph.objects.count(), 1)

    def test_exactly_one_projection_is_ever_current(self):
        apply_document(self._valid())
        apply_document(document(organizations=[(self.pcn, 'pcn', True)], relationships=[]))
        self.assertEqual(OrganizationGraph.objects.filter(is_current=True).count(), 1)

    def test_a_new_version_replaces_the_current_one(self):
        apply_document(self._valid())
        first = current_graph().graph_version
        apply_document(document(organizations=[(self.pcn, 'pcn', True)], relationships=[]))
        self.assertNotEqual(current_graph().graph_version, first)
        self.assertEqual(OrganizationGraph.objects.count(), 2)

    def test_the_version_matches_the_one_identity_computes(self):
        """The digest is duplicated in two repositories and must stay identical,
        or every fetch would look like a change."""
        body = self._valid()
        recomputed = graph_version(
            {
                'schema_version': body['schema_version'],
                'organizations': body['organizations'],
                'relationships': body['relationships'],
            }
        )
        self.assertEqual(body['graph_version'], recomputed)

    def test_an_inactive_organization_is_recorded_as_inactive(self):
        apply_document(
            document(
                organizations=[(self.pcn, 'pcn', True), (self.practice, 'practice', False)],
                relationships=[(self.pcn, self.practice)],
            )
        )
        graph = current_graph()
        self.assertFalse(organization_is_active(self.practice, graph=graph))
        self.assertTrue(organization_is_active(self.pcn, graph=graph))

    def test_an_organization_absent_from_the_graph_is_not_active(self):
        """Absence is not evidence of existence."""
        apply_document(self._valid())
        self.assertFalse(organization_is_active(factories.organization_id(), graph=current_graph()))

    def test_a_refresh_is_audited(self):
        apply_document(self._valid())
        self.assertTrue(AuditEvent.objects.filter(event=events.GRAPH_REFRESHED).exists())


class FreshnessTests(TestCase):
    def setUp(self):
        self.pcn = factories.organization_id()
        self.practice = factories.organization_id()

    def test_no_projection_at_all_raises(self):
        with self.assertRaises(GraphUnavailable):
            require_fresh_graph()

    def test_a_fresh_projection_is_returned(self):
        apply_document(
            document(
                organizations=[(self.pcn, 'pcn', True)],
                relationships=[],
            )
        )
        self.assertIsNotNone(require_fresh_graph())

    def test_a_stale_projection_raises(self):
        apply_document(
            document(
                organizations=[(self.pcn, 'pcn', True)],
                relationships=[],
                generated_at=timezone.now() - timedelta(days=2),
            )
        )
        with self.assertRaises(GraphUnavailable):
            require_fresh_graph()

    @override_settings(IDENTITY_GRAPH_MAX_AGE=60)
    def test_the_maximum_age_is_configurable(self):
        apply_document(
            document(
                organizations=[(self.pcn, 'pcn', True)],
                relationships=[],
                generated_at=timezone.now() - timedelta(minutes=5),
            )
        )
        with self.assertRaises(GraphUnavailable):
            require_fresh_graph()

    def test_freshness_is_measured_from_generation_not_fetch(self):
        """A document generated an hour ago and fetched a second ago is an hour
        old. Measuring from the fetch would let a stale document look permanently
        fresh to anything that polls."""
        apply_document(
            document(
                organizations=[(self.pcn, 'pcn', True)],
                relationships=[],
                generated_at=timezone.now() - timedelta(days=2),
            )
        )
        graph = current_graph()
        # It was fetched just now...
        self.assertLess((timezone.now() - graph.fetched_at).total_seconds(), 60)
        # ...and it is still stale.
        self.assertFalse(graph.is_fresh)

    def test_sponsorship_of_self_needs_no_graph(self):
        """A practice paying for its own subscription must never depend on
        Identity being reachable."""
        organization = factories.organization_id()
        self.assertTrue(sponsorship_is_valid(organization, organization))

    def test_sponsorship_fails_closed_with_no_graph(self):
        self.assertFalse(sponsorship_is_valid(self.pcn, self.practice))

    def test_sponsorship_of_an_inactive_beneficiary_is_refused(self):
        apply_document(
            document(
                organizations=[(self.pcn, 'pcn', True), (self.practice, 'practice', False)],
                relationships=[(self.pcn, self.practice)],
            )
        )
        self.assertFalse(sponsorship_is_valid(self.pcn, self.practice))


class RelationshipChangeTests(TestCase):
    """Changes are noticed, audited, and cost nobody a subscription."""

    def setUp(self):
        self.pcn = factories.account(name='PCN', org_type='pcn')
        self.practice = factories.account(name='Practice')
        self.parent = self.pcn.organization_id
        self.child = self.practice.organization_id

        apply_document(
            document(
                organizations=[(self.parent, 'pcn', True), (self.child, 'practice', True)],
                relationships=[(self.parent, self.child)],
            )
        )
        self.subscription = factories.subscription(account_obj=self.pcn, plan_key='pcn')
        self.allocation = factories.allocate(self.subscription, self.child)

    def _remove(self):
        apply_document(
            document(
                organizations=[(self.parent, 'pcn', True), (self.child, 'practice', True)],
                relationships=[],
            )
        )

    def test_a_removed_relationship_is_audited(self):
        self._remove()
        row = AuditEvent.objects.filter(event=events.GRAPH_RELATIONSHIPS_CHANGED).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.metadata['removed'], 1)
        self.assertEqual(row.metadata['added'], 0)

    def test_the_change_audit_names_no_organization(self):
        """Counts only. Naming them would put the estate's structure into the
        audit trail on every reorganisation."""
        self._remove()
        row = AuditEvent.objects.get(event=events.GRAPH_RELATIONSHIPS_CHANGED)
        serialised = json.dumps(row.metadata)
        self.assertNotIn(str(self.parent), serialised)
        self.assertNotIn(str(self.child), serialised)

    def test_the_sponsored_allocation_becomes_ineligible(self):
        self._remove()
        self.allocation.refresh_from_db()
        self.assertEqual(self.allocation.status, EntitlementAllocation.Status.INELIGIBLE)

    def test_an_operational_alert_is_raised(self):
        self._remove()
        self.assertTrue(
            OperationalAlert.objects.filter(kind=OperationalAlert.Kind.SPONSORSHIP_LAPSED).exists()
        )

    def test_the_subscription_is_untouched(self):
        self._remove()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.state, 'active')
        self.assertIsNone(self.subscription.canceled_at)

    def test_an_added_relationship_is_audited_without_lapsing_anything(self):
        third = factories.organization_id()
        apply_document(
            document(
                organizations=[
                    (self.parent, 'pcn', True),
                    (self.child, 'practice', True),
                    (third, 'practice', True),
                ],
                relationships=[(self.parent, self.child), (self.parent, third)],
            )
        )
        row = AuditEvent.objects.filter(event=events.GRAPH_RELATIONSHIPS_CHANGED).first()
        self.assertEqual(row.metadata['added'], 1)
        self.assertEqual(row.metadata['removed'], 0)
        self.allocation.refresh_from_db()
        self.assertEqual(self.allocation.status, EntitlementAllocation.Status.ACTIVE)

    def test_the_first_projection_is_not_reported_as_a_change(self):
        """Everything is 'new' against nothing. Reporting the first fetch as a
        wholesale change would raise an alert per relationship on day one."""
        AuditEvent.objects.all().delete()
        OrganizationGraph.objects.all().delete()
        apply_document(
            document(
                organizations=[(self.parent, 'pcn', True), (self.child, 'practice', True)],
                relationships=[(self.parent, self.child)],
            )
        )
        self.assertFalse(
            AuditEvent.objects.filter(event=events.GRAPH_RELATIONSHIPS_CHANGED).exists()
        )


@override_settings(
    IDENTITY_GRAPH_URL='https://identity.invalid/organizations/graph/v1/',
    IDENTITY_GRAPH_KEY_ID='billing',
    IDENTITY_GRAPH_SECRET='a-test-secret',
)
class FetchTests(TestCase):
    def setUp(self):
        self.pcn = factories.organization_id()

    def test_a_fetched_document_is_applied(self):
        body = document(organizations=[(self.pcn, 'pcn', True)], relationships=[])
        with patch('identity.graph.fetch_document', return_value=body):
            result = refresh(force=True)
        self.assertTrue(result.changed)
        self.assertEqual(current_graph().graph_version, body['graph_version'])

    def test_a_304_keeps_the_held_projection_and_does_not_rejuvenate_it(self):
        """A 304 means the *content* has not changed. It does not make the
        document younger, and pretending it did would defeat expiry entirely."""
        stale = document(
            organizations=[(self.pcn, 'pcn', True)],
            relationships=[],
            generated_at=timezone.now() - timedelta(days=2),
        )
        apply_document(stale)
        with patch('identity.graph.fetch_document', return_value=None):
            result = refresh(force=True)
        self.assertTrue(result.unchanged_version)
        self.assertFalse(current_graph().is_fresh)

    def test_a_fresh_projection_short_circuits_the_fetch(self):
        apply_document(document(organizations=[(self.pcn, 'pcn', True)], relationships=[]))
        with patch('identity.graph.fetch_document') as fetch:
            refresh()
        fetch.assert_not_called()

    def test_force_bypasses_the_short_circuit(self):
        apply_document(document(organizations=[(self.pcn, 'pcn', True)], relationships=[]))
        with patch('identity.graph.fetch_document', return_value=None) as fetch:
            refresh(force=True)
        fetch.assert_called_once()

    @override_settings(IDENTITY_GRAPH_URL='')
    def test_an_unconfigured_endpoint_raises_rather_than_guessing(self):
        from identity.graph import fetch_document

        with self.assertRaises(GraphError):
            fetch_document()

    @override_settings(IDENTITY_GRAPH_SECRET='')
    def test_a_missing_credential_raises(self):
        from identity.graph import fetch_document

        with self.assertRaises(GraphError):
            fetch_document()

    @override_settings(IDENTITY_GRAPH_URL='http://identity.invalid/organizations/graph/v1/')
    def test_plain_http_is_refused(self):
        """A service credential on the wire in clear is a service credential
        somebody else has."""
        from identity.graph import fetch_document

        with self.assertRaises(GraphError):
            fetch_document()

    def test_the_signed_path_is_the_path_that_is_requested(self):
        """The trailing slash has to survive from configuration to signature.

        Identity's route is `/organizations/graph/v1/`. Asking for it without the
        trailing slash earns an APPEND_SLASH redirect, and the redirected request
        carries a signature computed for the *unslashed* path — a signature for a
        different path, which the path-bound scheme correctly refuses. The result
        is a credential that looks configured, and a graph that never refreshes.
        """
        from identity import graph as graph_module

        captured = {}

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {'schema_version': 1, 'organizations': [], 'relationships': []}

        def _capture(url, headers=None, timeout=None):
            captured['url'] = url
            captured['authorization'] = headers['Authorization']
            return _Response()

        with patch.object(graph_module.requests, 'get', _capture):
            graph_module.fetch_document()

        self.assertTrue(captured['url'].endswith('/organizations/graph/v1/'), captured['url'])
        requested_path = urlparse(captured['url']).path
        signed_for_requested = sign_request(
            settings.IDENTITY_GRAPH_KEY_ID,
            settings.IDENTITY_GRAPH_SECRET,
            'GET',
            requested_path,
            int(captured['authorization'].split(':')[1]),
        )
        self.assertEqual(captured['authorization'], signed_for_requested)

    def test_the_signature_is_path_bound_and_matches_identitys_scheme(self):
        """The two repositories implement this scheme independently and must stay
        byte-compatible, so the header is asserted rather than assumed."""
        header = sign_request('billing', 'a-test-secret', 'GET', '/organizations/graph/v1/', 1000)
        other = sign_request('billing', 'a-test-secret', 'GET', '/somewhere-else/', 1000)
        self.assertTrue(header.startswith('Haresign-Service billing:1000:'))
        self.assertNotEqual(header, other)
