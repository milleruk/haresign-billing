"""Billing's held copy of Identity's organisation graph.

A **projection**, and the word is load-bearing. Identity owns the graph; this is
a dated, versioned copy of a small part of it, fetched over the service endpoint
at `GET /organizations/graph/v1/` and never by reading Identity's database.

What replaced what: Phase 4A had `billing.MemberOrganizationLink`, a flat table
of edges written only by the migration importer, with no version, no expiry, and
a documented decision to keep using stale rows because withdrawing access felt
worse than the staleness. That is `docs/entitlements.md` D-4, and the answer it
was waiting for is this module. Three things changed with it:

* **It is versioned.** Each fetch produces one `OrganizationGraph` row carrying
  the content digest Identity computed. Comparing versions is comparing content.
* **It expires.** A projection older than `IDENTITY_GRAPH_MAX_AGE` is stale, and
  stale means sponsored entitlements and new purchases **fail closed**. Not the
  old behaviour, deliberately: an entitlement inherited from a relationship we
  can no longer confirm is an entitlement we cannot justify, and the failure mode
  of continuing is that a practice keeps a paid tool because a sync stopped.
* **Changes are noticed.** Applying a new version diffs the edges against the
  current one, so a removed relationship becomes an audited event and an
  operational alert rather than a silent difference in a query result.

A practice's **own** subscription is never affected by any of this. Direct
entitlement does not consult the graph at all, so a projection that has gone
stale cannot cost anybody something they bought themselves.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class OrganizationGraph(models.Model):
    """One fetched version of the organisation graph.

    Rows accumulate rather than being overwritten. Keeping the previous version is
    what makes "what changed, and when did we first see it" answerable, and it is
    cheap: the whole document is a few thousand UUIDs at the size this estate will
    ever be.
    """

    class Source(models.TextChoices):
        # Fetched from Identity's service endpoint. The only source that is ever
        # correct in production.
        IDENTITY_API = 'identity_api', 'Haresign Identity API'
        # Built by the migration importer from allowlisted source data, so that a
        # migrated estate has a graph before the API is wired up. It is stamped
        # with its real generation time and therefore *goes stale*, which is the
        # mechanism that stops it being relied on indefinitely.
        LEGACY_MIGRATION = 'legacy_migration', 'Legacy migration import'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Identity's content digest over the graph. Unique: the same content is the
    # same version, and re-fetching an unchanged graph must not accumulate rows.
    graph_version = models.CharField(max_length=64, unique=True)
    # The document's own shape version. A projection whose schema we do not
    # recognise is refused rather than parsed optimistically.
    schema_version = models.PositiveIntegerField(default=1)

    source = models.CharField(
        max_length=32, choices=Source.choices, default=Source.IDENTITY_API, db_index=True
    )

    # When Identity built the document. **This, not `fetched_at`, is what
    # freshness is measured from**: a document generated an hour ago and fetched a
    # second ago is an hour old, and measuring from the fetch would let a
    # frequently-polled stale document look permanently fresh.
    generated_at = models.DateTimeField()
    fetched_at = models.DateTimeField(default=timezone.now)

    # Set on exactly one row. Not derived from `fetched_at` ordering, because the
    # current projection is a decision (a fetch that failed validation must not
    # become current by being the newest) rather than an observation.
    is_current = models.BooleanField(default=False, db_index=True)

    organization_count = models.PositiveIntegerField(default=0)
    relationship_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-generated_at', '-fetched_at']
        constraints = [
            # One current projection, enforced by the database rather than by
            # everybody remembering to clear the old flag.
            models.UniqueConstraint(
                fields=['is_current'],
                condition=models.Q(is_current=True),
                name='identity_graph_single_current',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.graph_version} ({self.source}, {self.generated_at:%Y-%m-%d %H:%M})'

    @property
    def age_seconds(self) -> float:
        return (timezone.now() - self.generated_at).total_seconds()

    @property
    def is_fresh(self) -> bool:
        """Within the configured maximum age.

        The single definition. Everything that needs to know whether the graph may
        be relied on asks this, so there is one place where the answer could be
        wrong rather than several that could disagree.
        """
        return self.age_seconds <= settings.IDENTITY_GRAPH_MAX_AGE


class GraphOrganization(models.Model):
    """One organisation as the projection reports it.

    Carries type and active status and nothing else. There is no name here on
    purpose: Identity does not send one, and a display name copied into a second
    service is a second thing to keep correct. The billing account holds the
    display copy, refreshed from the person's own session.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    graph = models.ForeignKey(
        OrganizationGraph, on_delete=models.CASCADE, related_name='organizations'
    )

    organization_id = models.UUIDField(db_index=True)
    organization_type = models.CharField(max_length=20, blank=True, default='')
    # An organisation Identity reports as suspended or archived. Reported rather
    # than omitted, because "withdrawn" and "never heard of it" are different
    # facts that call for different responses.
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['organization_id']
        constraints = [
            models.UniqueConstraint(
                fields=['graph', 'organization_id'], name='identity_graph_org_unique'
            ),
        ]

    def __str__(self) -> str:
        return f'{self.organization_id} ({self.organization_type})'


class GraphRelationship(models.Model):
    """One active containment edge: parent contains child.

    Only `member_of` edges reach here — Identity does not send any other kind, and
    if it started to, the schema version would move and this projection would
    refuse the document rather than quietly widen a PCN's reach.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    graph = models.ForeignKey(
        OrganizationGraph, on_delete=models.CASCADE, related_name='relationships'
    )

    parent_organization_id = models.UUIDField(db_index=True)
    child_organization_id = models.UUIDField(db_index=True)

    class Meta:
        ordering = ['parent_organization_id', 'child_organization_id']
        constraints = [
            models.UniqueConstraint(
                fields=['graph', 'parent_organization_id', 'child_organization_id'],
                name='identity_graph_edge_unique',
            ),
            models.CheckConstraint(
                condition=~models.Q(parent_organization_id=models.F('child_organization_id')),
                name='identity_graph_edge_no_self',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.child_organization_id} → {self.parent_organization_id}'
