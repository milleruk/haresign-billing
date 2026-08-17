"""Fetching, applying and relying on the organisation-graph projection.

This module is the whole of Billing's relationship with Identity's organisation
graph, and it has exactly one way in: an HTTPS GET to Identity's service
endpoint, signed with a narrowly scoped rotating credential. There is no database
connection to Identity here and there is no code path that could become one.

The three questions everything else asks:

* `current_graph()` — what projection do we hold?
* `require_fresh_graph()` — may we rely on it *right now*? Raises if not.
* `member_organizations()` / `sponsorship_is_valid()` — what does it say?

**Fail closed is the default and it is not negotiable.** A missing projection, a
stale one, an unreachable Identity, a refused credential and an unparseable
document are all the same answer: sponsored entitlements do not apply and new
purchases are refused. A practice's own subscription is untouched by every one of
those — direct entitlement never consults this module — so failing closed costs
nobody something they bought themselves.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from audit import events
from audit.services import record

from .graph_models import GraphOrganization, GraphRelationship, OrganizationGraph
from .service_auth import sign_request

logger = logging.getLogger('haresign.billing')

# The document shapes this consumer knows how to read. An unrecognised schema
# version is refused rather than parsed optimistically — that is the entire
# reason Identity puts a version in the document.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

_HTTP_TIMEOUT = 5


def graph_version(document: dict) -> str:
    """The content digest for a graph document, computed exactly as Identity does.

    A deliberate duplication of `organizations/graph.py` in `haresign-core`, and
    the two must stay byte-identical. It exists because the migration importer
    builds a projection locally and needs a version for it, and there is no
    sensible way to ask Identity to compute a digest over a document Identity has
    never seen.

    `generated_at` is excluded, as it is over there: the version answers "has the
    graph changed", and folding the clock in would make every rebuild look like a
    change.
    """
    material = json.dumps(
        {
            'schema_version': document['schema_version'],
            'organizations': document['organizations'],
            'relationships': document['relationships'],
        },
        sort_keys=True,
        separators=(',', ':'),
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


class GraphError(RuntimeError):
    """The projection could not be fetched or applied. Never carries a credential."""


class GraphUnavailable(RuntimeError):
    """No projection may be relied on right now.

    Raised by `require_fresh_graph`, and the callers that catch it are the ones
    that must fail closed: sponsored entitlement derivation, PCN allocation and
    checkout. Deliberately a different exception from `GraphError` — one means
    "the fetch went wrong", the other means "whatever the reason, do not proceed".
    """


# --- Reading what we hold -----------------------------------------------------


def current_graph() -> OrganizationGraph | None:
    """The projection in force, or None if we have never successfully held one."""
    return OrganizationGraph.objects.filter(is_current=True).first()


def require_fresh_graph() -> OrganizationGraph:
    """The current projection, if it may be relied on. Raises `GraphUnavailable`.

    Callers must not catch this and continue. It exists so that "we could not
    confirm the relationship" and "the relationship does not exist" produce the
    same outcome at the point of decision, which is the only safe conflation of
    the two.
    """
    graph = current_graph()
    if graph is None:
        raise GraphUnavailable('No organisation graph has been fetched.')
    if not graph.is_fresh:
        raise GraphUnavailable(
            f'The organisation graph is {int(graph.age_seconds)}s old, '
            f'past the {settings.IDENTITY_GRAPH_MAX_AGE}s maximum.'
        )
    return graph


def member_organizations(parent_organization_id, *, graph: OrganizationGraph) -> set[str]:
    """The organisations `parent` currently contains, per this projection."""
    return {
        str(child)
        for child in GraphRelationship.objects.filter(
            graph=graph, parent_organization_id=parent_organization_id
        ).values_list('child_organization_id', flat=True)
    }


def organization_is_active(organization_id, *, graph: OrganizationGraph) -> bool:
    """Does the projection report this organisation, and report it as active?

    An organisation the projection does not mention at all is **not** active for
    this purpose. Absence is not evidence of existence, and a purchase for an
    organisation Identity has never heard of is a purchase attributed to nobody.
    """
    row = GraphOrganization.objects.filter(graph=graph, organization_id=organization_id).first()
    return bool(row and row.is_active)


def sponsorship_is_valid(payer_organization_id, beneficiary_organization_id) -> bool:
    """May `payer` currently sponsor `beneficiary`?

    Fails closed on a missing or stale projection. The payer sponsoring *itself*
    is not sponsorship and does not consult the graph — a practice paying for its
    own subscription must never depend on Identity being reachable.
    """
    if str(payer_organization_id) == str(beneficiary_organization_id):
        return True
    try:
        graph = require_fresh_graph()
    except GraphUnavailable:
        return False
    if not organization_is_active(beneficiary_organization_id, graph=graph):
        return False
    return str(beneficiary_organization_id) in member_organizations(
        payer_organization_id, graph=graph
    )


# --- Fetching -----------------------------------------------------------------


@dataclass(frozen=True)
class RefreshResult:
    """What one refresh did. Aggregate only — no organisation is named here."""

    graph: OrganizationGraph | None
    changed: bool
    unchanged_version: bool = False
    relationships_added: int = 0
    relationships_removed: int = 0


def fetch_document() -> dict | None:
    """Read the projection from Identity. `None` means "unchanged, keep yours".

    Sends `If-None-Match` with the version we already hold, so an unchanged graph
    costs one conditional request rather than a full transfer.
    """
    # Deliberately *not* rstrip'd. The signature is path-bound, so the path that
    # is signed has to be the path that is finally served. Identity's route is
    # `/organizations/graph/v1/`; requesting it without the trailing slash earns
    # an APPEND_SLASH redirect, and the redirected request then carries a
    # signature computed for the unslashed path — which is a signature for a
    # different path, and is refused. Keeping the configured URL intact is what
    # makes the credential work at all.
    base = (settings.IDENTITY_GRAPH_URL or '').strip()
    if not base:
        raise GraphError('No IDENTITY_GRAPH_URL is configured.')

    key_id = settings.IDENTITY_GRAPH_KEY_ID
    secret = settings.IDENTITY_GRAPH_SECRET
    if not key_id or not secret:
        raise GraphError('No organisation-graph service credential is configured.')

    # The signature covers the path, so it must be signed with exactly the path
    # that will be requested — not the full URL.
    from urllib.parse import urlparse

    parsed = urlparse(base)
    if parsed.scheme != 'https' and not settings.OIDC_ALLOW_INSECURE_LOOPBACK:
        # The same opt-in the OIDC client uses for the isolated rehearsal, and for
        # the same reason: a setting that allowed plain HTTP to an arbitrary host
        # would put a service credential on the wire in clear.
        raise GraphError('The organisation-graph endpoint must be served over HTTPS.')

    headers = {'Authorization': sign_request(key_id, secret, 'GET', parsed.path)}
    held = current_graph()
    if held is not None:
        headers['If-None-Match'] = f'"{held.graph_version}"'

    try:
        response = requests.get(base, headers=headers, timeout=_HTTP_TIMEOUT)
    except Exception as exc:
        raise GraphError('Unable to reach the Identity organisation-graph endpoint.') from exc

    if response.status_code == 304:
        return None
    if response.status_code != 200:
        # The body can echo request detail. Only the status is recorded.
        raise GraphError(f'The organisation-graph endpoint refused (HTTP {response.status_code}).')

    try:
        document = response.json()
    except Exception as exc:
        raise GraphError(
            'The organisation-graph endpoint returned a body that was not JSON.'
        ) from exc

    if not isinstance(document, dict):
        raise GraphError('The organisation-graph document is not an object.')
    return document


def refresh(*, force: bool = False, request=None) -> RefreshResult:
    """Fetch and apply the projection. The scheduled and pre-decision entry point.

    `force` only bypasses the "we already hold a fresh graph" shortcut; it never
    bypasses validation.
    """
    held = current_graph()
    if not force and held is not None and held.is_fresh:
        return RefreshResult(graph=held, changed=False)

    document = fetch_document()
    if document is None:
        # Identity says our version is still current. Re-stamp the generation time
        # from the document we were told is unchanged? No — deliberately not: the
        # document was generated when it was generated, and pretending a 304 makes
        # it younger would defeat expiry entirely. A 304 means the *content* has
        # not changed; the graph is still as old as it is.
        return RefreshResult(graph=held, changed=False, unchanged_version=True)

    return apply_document(document, source=OrganizationGraph.Source.IDENTITY_API, request=request)


@transaction.atomic
def apply_document(
    document: dict,
    *,
    source: str = OrganizationGraph.Source.IDENTITY_API,
    request=None,
) -> RefreshResult:
    """Validate and store one projection document, and notice what changed.

    Validation is strict and refuses rather than repairs: an unknown schema
    version, a missing generation time, an edge naming an organisation the
    document does not describe. A projection this service half-understood would be
    worse than none, because it would look fresh.
    """
    schema_version = document.get('schema_version')
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise GraphError(f'Unsupported organisation-graph schema version {schema_version!r}.')

    graph_version = str(document.get('graph_version') or '')
    if not graph_version:
        raise GraphError('The organisation-graph document carries no version.')

    generated_at = parse_datetime(str(document.get('generated_at') or ''))
    if generated_at is None:
        raise GraphError('The organisation-graph document carries no generation time.')
    if timezone.is_naive(generated_at):
        generated_at = timezone.make_aware(generated_at, timezone.utc)

    organizations = document.get('organizations')
    relationships = document.get('relationships')
    if not isinstance(organizations, list) or not isinstance(relationships, list):
        raise GraphError('The organisation-graph document is malformed.')

    known = {
        str(entry.get('organization_id')) for entry in organizations if isinstance(entry, dict)
    }
    for edge in relationships:
        if not isinstance(edge, dict):
            raise GraphError('The organisation-graph document has a malformed relationship.')
        parent = str(edge.get('parent_organization_id') or '')
        child = str(edge.get('child_organization_id') or '')
        if parent not in known or child not in known:
            raise GraphError('A relationship names an organisation the document does not describe.')

    previous = current_graph()
    previous_edges = _edge_set(previous) if previous else set()

    existing = OrganizationGraph.objects.filter(graph_version=graph_version).first()
    if existing is not None:
        # We have seen this exact content before. Make it current again if it is
        # not — which happens when a change is reverted — but do not duplicate it.
        graph = existing
    else:
        graph = OrganizationGraph(
            graph_version=graph_version,
            schema_version=schema_version,
            source=source,
            generated_at=generated_at,
            organization_count=len(organizations),
            relationship_count=len(relationships),
        )
        graph.save()
        GraphOrganization.objects.bulk_create(
            GraphOrganization(
                graph=graph,
                organization_id=entry['organization_id'],
                organization_type=str(entry.get('organization_type') or '')[:20],
                is_active=bool(entry.get('is_active')),
            )
            for entry in organizations
            if isinstance(entry, dict) and entry.get('organization_id')
        )
        GraphRelationship.objects.bulk_create(
            GraphRelationship(
                graph=graph,
                parent_organization_id=edge['parent_organization_id'],
                child_organization_id=edge['child_organization_id'],
            )
            for edge in relationships
        )

    graph.fetched_at = timezone.now()
    graph.save(update_fields=['fetched_at'])

    # One current row, and the constraint enforces it. Clear before setting.
    OrganizationGraph.objects.filter(is_current=True).exclude(pk=graph.pk).update(is_current=False)
    if not graph.is_current:
        graph.is_current = True
        graph.save(update_fields=['is_current'])

    new_edges = _edge_set(graph)
    added = new_edges - previous_edges
    removed = previous_edges - new_edges

    if previous is not None and (added or removed):
        record(
            events.GRAPH_RELATIONSHIPS_CHANGED,
            request=request,
            metadata={
                'graph_version': graph_version,
                'previous_version': previous.graph_version,
                # Counts only. Naming the organisations here would put the estate's
                # structure into the audit trail on every reorganisation.
                'added': len(added),
                'removed': len(removed),
            },
        )

    record(
        events.GRAPH_REFRESHED,
        request=request,
        metadata={
            'graph_version': graph_version,
            'source': source,
            'organizations': graph.organization_count,
            'relationships': graph.relationship_count,
            'reused_existing_version': existing is not None,
        },
    )

    if removed:
        # Imported here rather than at module scope: `identity` imports no domain
        # app, and this is the one place the rule bends — with a local import, so
        # the dependency is visible at the point it is taken rather than hidden in
        # the header. See AGENTS.md.
        from billing.sponsorship import invalidate_lapsed_allocations

        invalidate_lapsed_allocations(removed_edges=removed, graph=graph, request=request)

    return RefreshResult(
        graph=graph,
        changed=bool(added or removed) or existing is None,
        relationships_added=len(added),
        relationships_removed=len(removed),
    )


def _edge_set(graph: OrganizationGraph) -> set[tuple[str, str]]:
    return {
        (str(parent), str(child))
        for parent, child in GraphRelationship.objects.filter(graph=graph).values_list(
            'parent_organization_id', 'child_organization_id'
        )
    }
