"""Append-only billing audit events.

The point of an audit log is that it says what happened, not what somebody would
prefer to have happened. So the record is append-only at the *model* layer, not
merely by convention or by admin configuration: ``save()`` refuses to update an
existing row and ``delete()`` refuses outright. Both raise rather than silently
no-op, because a caller that tries to rewrite history has a bug worth seeing.

This is not tamper-*proof* — anything with the database password can still
rewrite rows. It is tamper-*evident against the application*, which is the layer
where mistakes and misuse actually happen.

Everything here is keyed by **UUID reference**, never by foreign key into
Identity. Billing does not own users or organisations and must not grow a table
that looks like it does.
"""

import uuid

from django.db import models


class AuditEventImmutableError(RuntimeError):
    """Raised on any attempt to modify or delete a recorded audit event."""


class AuditEvent(models.Model):
    """One billing-relevant thing that happened.

    Written through ``audit.services.record()``, never constructed directly by a
    view — the service is where actor resolution, request context and metadata
    scrubbing are applied, and a second construction path would be a second set
    of rules.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # A stable dotted key from audit.events, e.g. "subscription.state.changed".
    # Free text rather than choices on purpose: an event key added by a later
    # phase must not require a migration on the largest table in the schema, and
    # the constants module is the real contract.
    event = models.CharField(max_length=100, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Who did it, as an Identity user UUID. Null for provider-initiated events
    # (a webhook has no human actor) and for system actions. A plain UUID field
    # rather than a foreign key: Identity owns people, and a CASCADE from a
    # deleted account must never be able to remove the record of what happened.
    actor_user_id = models.UUIDField(null=True, blank=True, db_index=True)

    # Which organisation it happened to, as an Identity organisation UUID.
    organization_id = models.UUIDField(null=True, blank=True, db_index=True)

    # Whether the actor was exercising platform-administrator support access
    # rather than their own organisation membership. Its own column and not a
    # metadata key, because "how many times did staff read a customer's billing
    # page this quarter" must be answerable by a query, not by a text search.
    support_access = models.BooleanField(default=False, db_index=True)

    # The provider's own event id where one caused this, so a Stripe dashboard
    # entry and a Billing audit row can be lined up during an incident. Never
    # the payload, never a signature.
    provider_event_id = models.CharField(max_length=255, blank=True, default='', db_index=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True, default='')

    # Ties an event to the request that caused it, and to the log lines from that
    # request. Set by audit.middleware.RequestCorrelationMiddleware.
    request_id = models.CharField(max_length=64, blank=True, default='', db_index=True)

    # Structured detail. Scrubbed by the service before it reaches here: see
    # audit.services.scrub_metadata. Never a secret, a card detail, a full
    # provider payload or a personal email address.
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        verbose_name = 'audit event'
        verbose_name_plural = 'audit events'
        indexes = [
            models.Index(fields=['event', '-created_at']),
            models.Index(fields=['organization_id', '-created_at']),
            models.Index(fields=['support_access', '-created_at']),
        ]

    def __str__(self) -> str:
        return f'{self.created_at:%Y-%m-%d %H:%M:%S} {self.event}'

    def save(self, *args, **kwargs):
        # `_state.adding` distinguishes the first insert from every later write.
        # A UUID primary key is assigned in Python, so "has a pk" would call every
        # insert an update and nothing could ever be written.
        if not self._state.adding:
            raise AuditEventImmutableError(
                'Audit events are append-only; an existing event cannot be modified.'
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditEventImmutableError('Audit events are append-only; they cannot be deleted.')
