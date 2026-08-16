"""Endpoint rate limiting.

Two independent counters per attempt: the client IP and a scope-specific
identifier. Either alone is trivially evaded — an IP-only counter is beaten by a
botnet, an identifier-only counter is beaten by spreading across identifiers.

Cache keys are **keyed digests**, never raw addresses or credentials. A Redis
instance that can be read should not be a directory of who has been calling.

Production fails **closed**. A cache outage that silently removed rate limiting
would be a security control quietly reporting healthy while doing nothing.
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.utils.crypto import salted_hmac

from audit.services import client_ip


class Throttled(RuntimeError):
    """The caller has exceeded a configured limit."""


def _key(scope: str, kind: str, value: str) -> str:
    digest = salted_hmac(f'billing.throttle.{scope}.{kind}', value, algorithm='sha256').hexdigest()
    return f'throttle:{scope}:{kind}:{digest}'


def _consume(key: str, limit: int, window: int) -> bool:
    """Increment and report whether the limit is now exceeded."""
    try:
        added = cache.add(key, 1, timeout=window)
        count = 1 if added else cache.incr(key)
    except Exception:
        # Redis is unreachable. In production this is a refusal; in development
        # and tests it is a pass, so nobody needs Redis to edit a template.
        if settings.THROTTLE_FAIL_OPEN:
            return True
        raise Throttled('Rate limiting state is unavailable.') from None
    return count <= limit


def throttle(request, scope: str, identifier: str = '') -> None:
    """Consume one unit of `scope`'s allowance. Raises `Throttled` when exhausted.

    Consumed *before* validation and before any lookup, so a rejected request
    still costs the caller their allowance — a limiter that only counts successful
    requests limits nothing.
    """
    config = settings.THROTTLE_SCOPES.get(scope)
    if not config:
        return

    address = client_ip(request) or 'unknown'
    if not _consume(_key(scope, 'ip', address), config['ip_limit'], config['ip_window']):
        raise Throttled(f'{scope}: too many requests from this address.')

    if identifier:
        if not _consume(_key(scope, 'id', identifier), config['id_limit'], config['id_window']):
            raise Throttled(f'{scope}: too many requests for this identifier.')
