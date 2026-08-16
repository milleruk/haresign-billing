"""Service-to-service authentication for the internal entitlement API.

**Why this is not OAuth.** Haresign Identity implements the authorization-code
grant and nothing else — no client-credentials grant is advertised in its
discovery document, and its token endpoint refuses any `grant_type` other than
`authorization_code`. Building an OAuth-shaped credential here would mean either
inventing a grant Identity does not have, or standing up a second authorization
server inside Billing. Both would pre-commit Phase 4B to a design nobody has
agreed.

So this is a **narrowly scoped internal credential**, documented as such, and
deliberately unambitious: a key id, a shared secret, an HMAC over the request, and
a timestamp. It is a stopgap with a stated replacement path (docs/entitlements.md,
decision D-6) rather than a pretend standard.

What it does provide:

* **Constant-time comparison**, so the secret is not recoverable by timing.
* **A signature over the request**, not a bearer token, so a captured
  `Authorization` header cannot be replayed against a different organisation.
* **A timestamp inside the signature**, bounded, so a captured request expires.
* **Overlap-capable rotation** — several `key_id:secret` pairs may be configured
  at once, so a rotation is: add the new pair, deploy consumers, remove the old
  pair. No window in which every consumer is broken.
* **Key ids in logs, secrets never.** An audit row says which credential was used;
  it never says what it was.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

from django.conf import settings

logger = logging.getLogger('haresign.billing')

# Header shape: `Haresign-Service <key_id>:<timestamp>:<signature>`.
SCHEME = 'Haresign-Service'
# Tolerated request age. Short: these are machine-to-machine calls on a private
# network, and a wide window is a replay window.
MAX_AGE_SECONDS = 60


class ServiceAuthError(RuntimeError):
    """Authentication failed. The message is for our logs, never for the response."""


def _configured_keys() -> dict[str, str]:
    """Parse `ENTITLEMENT_API_KEYS` into `{key_id: secret}`.

    Malformed entries are dropped with a warning that names the position, not the
    content — a log line quoting a malformed secret is still a log line containing
    a secret.
    """
    keys: dict[str, str] = {}
    raw = settings.ENTITLEMENT_API_KEYS or ''
    for index, pair in enumerate(part.strip() for part in raw.split(',')):
        if not pair:
            continue
        key_id, _, secret = pair.partition(':')
        if not key_id or not secret:
            logger.warning('entitlement api: credential %d is malformed and was ignored', index)
            continue
        keys[key_id.strip()] = secret.strip()
    return keys


def signing_string(method: str, path: str, timestamp: str) -> str:
    """What both sides sign.

    The path is included so a signature minted for one organisation cannot be
    replayed against another — which is the single reason this is a signature and
    not a bearer token.
    """
    return f'{method.upper()}\n{path}\n{timestamp}'


def sign_request(
    key_id: str, secret: str, method: str, path: str, timestamp: int | None = None
) -> str:
    """Build an Authorization header value. Used by consumers and by the tests."""
    stamp = str(timestamp or int(time.time()))
    digest = hmac.new(
        secret.encode(), signing_string(method, path, stamp).encode(), hashlib.sha256
    ).hexdigest()
    return f'{SCHEME} {key_id}:{stamp}:{digest}'


def authenticate(request) -> str:
    """Verify the request's credential. Returns the key id, or raises.

    Fails closed on every path: a missing header, a wrong scheme, an unknown key
    id, a stale timestamp and a bad signature are all refusals, and none of them
    reveals which one it was to the caller.
    """
    header = request.headers.get('Authorization', '')
    if not header.startswith(f'{SCHEME} '):
        raise ServiceAuthError('missing or wrong scheme')

    try:
        key_id, timestamp, supplied = header[len(SCHEME) + 1 :].split(':', 2)
    except ValueError as exc:
        raise ServiceAuthError('malformed credential') from exc

    keys = _configured_keys()
    if not keys:
        # No credentials configured means the API is closed, not open. The
        # opposite default would turn a forgotten deployment variable into an
        # unauthenticated entitlement oracle.
        raise ServiceAuthError('no credentials configured')

    secret = keys.get(key_id.strip())
    if secret is None:
        # Still do the work, so an unknown key id and a bad signature take the
        # same time and the header cannot be used to enumerate valid key ids.
        secret = 'unknown-key-placeholder'

    try:
        age = abs(time.time() - int(timestamp))
    except ValueError as exc:
        raise ServiceAuthError('malformed timestamp') from exc
    if age > MAX_AGE_SECONDS:
        raise ServiceAuthError('stale timestamp')

    expected = hmac.new(
        secret.encode(),
        signing_string(request.method, request.path, timestamp).encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, supplied) or key_id.strip() not in keys:
        raise ServiceAuthError('signature mismatch')

    return key_id.strip()
