"""Writing audit events.

One entry point, ``record()``, so that actor resolution, request context and
metadata scrubbing happen the same way every time. Views never construct an
``AuditEvent``.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

from .models import AuditEvent

logger = logging.getLogger('haresign.billing')

# Metadata is developer-supplied and ends up in a long-lived table that support
# staff read. Anything whose *name* suggests a credential is dropped rather than
# truncated or masked: a masked secret is still a statement about a secret, and
# the interesting failure here is the well-meaning `metadata={'secret': ...}`.
#
# The billing-specific additions are the second block. A billing audit trail that
# quietly accumulates card fingerprints, bank details, customer email addresses
# and full webhook bodies is a worse liability than no audit trail at all, and
# every one of those has a plausible-sounding reason to be passed in.
_FORBIDDEN_KEY_PARTS = (
    'password',
    'passwd',
    'secret',
    'token',
    'credential',
    'authorization',
    'cookie',
    'session',
    'api_key',
    'apikey',
    'private_key',
    'signature',
    # Billing-specific.
    'card',
    'pan',
    'cvc',
    'cvv',
    'iban',
    'sort_code',
    'account_number',
    'bank',
    'payment_method',
    'client_secret',
    'raw_payload',
    'payload',
    'email',
    'phone',
    'address',
)

_REDACTED = '[redacted]'
_MAX_VALUE_LENGTH = 500


def scrub_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return metadata safe to store: no secrets, no personal data, no unbounded values."""
    if not metadata:
        return {}

    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        name = str(key)
        if any(part in name.lower() for part in _FORBIDDEN_KEY_PARTS):
            cleaned[name] = _REDACTED
            continue
        if isinstance(value, dict):
            cleaned[name] = scrub_metadata(value)
        elif isinstance(value, (list, tuple)):
            cleaned[name] = [_scrub_scalar(item) for item in value]
        else:
            cleaned[name] = _scrub_scalar(value)
    return cleaned


def _scrub_scalar(value: Any) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = str(value)
    # A long opaque string in an audit record is almost always something that
    # should not have been passed in. Truncating bounds the damage and the
    # ellipsis makes the truncation visible rather than silent.
    return text if len(text) <= _MAX_VALUE_LENGTH else text[:_MAX_VALUE_LENGTH] + '…'


def client_ip(request) -> str | None:
    """The client address, trusting only as many proxy hops as are configured.

    ``X-Forwarded-For`` is client-writable on its left-hand side. Reading the
    leftmost entry — the common mistake — lets any caller choose their own
    apparent address, which would make both the audit trail and the IP throttle
    counter meaningless.
    """
    if request is None:
        return None

    hops = getattr(settings, 'TRUSTED_PROXY_COUNT', 0)
    if hops > 0:
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
        parts = [part.strip() for part in forwarded.split(',') if part.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return request.META.get('REMOTE_ADDR') or None


def record(
    event: str,
    *,
    request=None,
    actor_user_id=None,
    organization_id=None,
    support_access: bool = False,
    provider_event_id: str = '',
    metadata: dict[str, Any] | None = None,
) -> AuditEvent | None:
    """Append one audit event. Never raises into the caller.

    An audit write that fails must not take a subscription state change down with
    it: the provider has already moved, and a 500 would make Stripe retry an
    event we had in fact applied. The failure is logged loudly instead, which is
    the honest trade.
    """
    if actor_user_id is None and request is not None:
        user = getattr(request, 'user', None)
        if user is not None and getattr(user, 'is_authenticated', False):
            actor_user_id = getattr(user, 'identity_user_id', None)

    try:
        return AuditEvent.objects.create(
            event=event,
            actor_user_id=actor_user_id,
            organization_id=organization_id,
            support_access=support_access,
            provider_event_id=(provider_event_id or '')[:255],
            ip_address=client_ip(request),
            user_agent=(request.META.get('HTTP_USER_AGENT', '')[:400] if request else ''),
            request_id=getattr(request, 'request_id', '') if request else '',
            metadata=scrub_metadata(metadata),
        )
    except Exception:
        logger.exception('Failed to record audit event %s', event)
        return None
