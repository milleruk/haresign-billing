"""Signing outbound service requests to Haresign Identity.

The mirror image of `api/auth.py`, which verifies inbound service credentials on
Billing's own entitlement API. Identity verifies these with the identical scheme
in `organizations/service_auth.py` over there, and the two must stay
byte-compatible — which is why the signing string is defined here as a function
rather than inlined at the call site.

Only the signing half lives here. Billing never verifies one of these; it only
produces them.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SCHEME = 'Haresign-Service'


def signing_string(method: str, path: str, timestamp: str) -> str:
    """What both sides sign. The path is included, so a signature is path-bound."""
    return f'{method.upper()}\n{path}\n{timestamp}'


def sign_request(
    key_id: str, secret: str, method: str, path: str, timestamp: int | None = None
) -> str:
    """Build an Authorization header value for one request.

    The timestamp is inside the signature and Identity bounds it, so a header
    captured from a log or a proxy expires rather than becoming a bearer token
    with no lifetime.
    """
    stamp = str(timestamp or int(time.time()))
    digest = hmac.new(
        secret.encode(), signing_string(method, path, stamp).encode(), hashlib.sha256
    ).hexdigest()
    return f'{SCHEME} {key_id}:{stamp}:{digest}'
