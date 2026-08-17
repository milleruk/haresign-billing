"""The OpenID Connect relying party.

Billing is a *client* of Haresign Identity, using the one flow Identity supports:
Authorization Code with mandatory PKCE S256. Everything protocol-shaped lives
here so there is one place where a validation step could be missed, rather than
several.

What is validated on the way back in, and why each one matters:

* **state** — CSRF for the callback. Single-use, held server-side in the session,
  compared in constant time. Without it, an attacker completes their own
  authorization and hands the victim a callback URL that logs the victim into the
  attacker's account.
* **PKCE verifier** — the code cannot be redeemed by anyone who did not start the
  flow, even if it leaks from a redirect log or a referrer header.
* **nonce** — binds the ID token to this authorization request. Without it a
  token minted for a different session of the same client is replayable here.
* **issuer** — exactly the configured issuer string. Not "starts with", not
  "same host": the issuer is an identifier, and a prefix comparison is how a
  service ends up trusting `https://identity.haresign.net.attacker.example`.
* **audience** — must contain our client id, and `azp` must be us where present.
  An ID token issued to a *different* relying party of the same provider is a
  perfectly valid token that says nothing about our session.
* **signature** — RS256 against the provider's JWKS, fetched over HTTPS and
  cached briefly. `alg: none` and HMAC algorithms are rejected by allow-listing
  the asymmetric algorithms rather than by blocking the bad ones.
* **expiry / issued-at** — with a bounded, configured clock skew.

Discovery is fetched from the issuer rather than configured endpoint by endpoint,
so a provider that moves an endpoint does not need a Billing deployment — but the
issuer *in* the discovery document must equal the issuer we asked, which is the
check that makes discovery safe to trust (RFC 8414 §3.3).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import jwt
import requests
from django.conf import settings
from django.core.cache import cache
from jwt import PyJWKClient

logger = logging.getLogger('haresign.billing')

# Asymmetric only. Allow-listed rather than deny-listed: a deny list has to
# anticipate every dangerous algorithm, and it only takes one it did not know
# about ("none", or an HMAC verified against the public key) to accept a forgery.
ALLOWED_ALGORITHMS = ('RS256', 'RS384', 'RS512', 'ES256', 'ES384')

_DISCOVERY_CACHE_SECONDS = 300
_HTTP_TIMEOUT = 5


class OIDCError(RuntimeError):
    """Any failure of the relying-party flow. Never carries a token or a secret."""


@dataclass(frozen=True)
class AuthorizationRequest:
    """The values a pending authorization needs, held server-side in the session."""

    state: str
    nonce: str
    code_verifier: str
    created_at: float
    next_url: str = '/'


@dataclass(frozen=True)
class IdentityClaims:
    """The validated claims this service is prepared to act on."""

    subject: str
    display_name: str
    email: str
    memberships: list[dict]
    id_token_expires_at: int
    # Kept only to send to the provider's end-session endpoint at sign-out, so
    # logging out of Billing can end the Identity session too. Never logged,
    # never audited, never rendered.
    id_token: str = ''


# --- Discovery ----------------------------------------------------------------


def _require_https(url: str, what: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme == 'https':
        return
    if settings.OIDC_ALLOW_INSECURE_LOOPBACK and parsed.hostname in (
        'localhost',
        '127.0.0.1',
        '::1',
    ):
        # The isolated rehearsal stack speaks plain HTTP on a private network with
        # no route out. Allowed only by explicit configuration, and only for
        # loopback: a setting that permitted plain HTTP to an arbitrary host would
        # put every token on the wire in clear.
        return
    if settings.OIDC_ALLOW_INSECURE_LOOPBACK and parsed.scheme == 'http':
        # Compose service names are not loopback but are equally unroutable.
        # Still gated behind the same explicit opt-in.
        return
    raise OIDCError(f'{what} must be served over HTTPS.')


def discovery_document() -> dict:
    """The provider's metadata, cached briefly and issuer-checked."""
    issuer = settings.OIDC_ISSUER.rstrip('/')
    if not issuer:
        raise OIDCError('No OIDC issuer is configured.')

    cache_key = f'oidc:discovery:{hashlib.sha256(issuer.encode()).hexdigest()}'
    cached = cache.get(cache_key)
    if cached:
        return cached

    url = f'{issuer}/.well-known/openid-configuration'
    _require_https(url, 'The OIDC discovery endpoint')
    try:
        response = requests.get(url, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        document = response.json()
    except Exception as exc:
        raise OIDCError('Unable to read the Identity discovery document.') from exc

    # RFC 8414 §3.3. A document that names a different issuer is either
    # misconfigured or an attempt to point us at somebody else's provider.
    if document.get('issuer', '').rstrip('/') != issuer:
        raise OIDCError('The discovery document names a different issuer.')

    for required in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
        if not document.get(required):
            raise OIDCError(f'The discovery document is missing {required}.')
        _require_https(document[required], f'The {required}')

    # Identity advertises S256 and nothing else. Asserting it here means a
    # provider that silently dropped PKCE support would stop us rather than
    # letting us fall back to a flow without it.
    methods = document.get('code_challenge_methods_supported') or []
    if methods and 'S256' not in methods:
        raise OIDCError('The provider does not support PKCE S256.')

    cache.set(cache_key, document, _DISCOVERY_CACHE_SECONDS)
    return document


# --- Outbound: building the authorization request -----------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def build_authorization_request(next_url: str = '/') -> tuple[str, AuthorizationRequest]:
    """Return `(redirect_url, pending)`. `pending` must be stored server-side."""
    document = discovery_document()

    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    pending = AuthorizationRequest(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        code_verifier=verifier,
        created_at=time.time(),
        next_url=next_url,
    )

    query = urlencode(
        {
            'response_type': 'code',
            'client_id': settings.OIDC_CLIENT_ID,
            # Exact, absolute, and from settings — never built from the request
            # Host header, which an attacker controls.
            'redirect_uri': settings.OIDC_REDIRECT_URI,
            'scope': ' '.join(settings.OIDC_SCOPES),
            'state': pending.state,
            'nonce': pending.nonce,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        }
    )
    return f'{document["authorization_endpoint"]}?{query}', pending


# --- Inbound: redeeming and validating ----------------------------------------


def exchange_code(code: str, pending: AuthorizationRequest) -> dict:
    """Redeem an authorization code. Returns the raw token response."""
    document = discovery_document()

    data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': settings.OIDC_REDIRECT_URI,
        'client_id': settings.OIDC_CLIENT_ID,
        'code_verifier': pending.code_verifier,
    }
    auth = None
    if settings.OIDC_CLIENT_SECRET:
        # client_secret_basic. The secret never appears in a query string or a
        # log line; `requests` puts it in an Authorization header.
        auth = (settings.OIDC_CLIENT_ID, settings.OIDC_CLIENT_SECRET)

    try:
        response = requests.post(
            document['token_endpoint'], data=data, auth=auth, timeout=_HTTP_TIMEOUT
        )
    except Exception as exc:
        raise OIDCError('Unable to reach the Identity token endpoint.') from exc

    if response.status_code != 200:
        # The provider's error body can contain the code and the client id.
        # Neither belongs in our logs, so only the status is recorded.
        raise OIDCError(f'The token endpoint refused the code (HTTP {response.status_code}).')

    try:
        payload = response.json()
    except Exception as exc:
        raise OIDCError('The token endpoint returned a body that was not JSON.') from exc

    if not payload.get('id_token'):
        raise OIDCError('The token response carried no ID token.')
    return payload


def validate_id_token(id_token: str, pending: AuthorizationRequest) -> dict:
    """Verify signature, issuer, audience, expiry and nonce. Returns the claims."""
    document = discovery_document()

    try:
        signing_key = PyJWKClient(
            document['jwks_uri'], cache_keys=True, lifespan=_DISCOVERY_CACHE_SECONDS
        ).get_signing_key_from_jwt(id_token)
    except Exception as exc:
        raise OIDCError('Unable to resolve the signing key for the ID token.') from exc

    try:
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=list(ALLOWED_ALGORITHMS),
            audience=settings.OIDC_CLIENT_ID,
            issuer=settings.OIDC_ISSUER.rstrip('/'),
            leeway=settings.OIDC_CLOCK_SKEW,
            options={
                'require': ['iss', 'sub', 'aud', 'exp', 'iat'],
                'verify_signature': True,
                'verify_exp': True,
                'verify_iat': True,
                'verify_aud': True,
                'verify_iss': True,
            },
        )
    except jwt.PyJWTError as exc:
        # The exception text can quote token contents; the type alone is enough
        # to diagnose and carries nothing sensitive.
        raise OIDCError(f'ID token validation failed ({type(exc).__name__}).') from exc

    # `azp` is only meaningful when present, and when it is, it must be us. A
    # token with multiple audiences and somebody else's `azp` was minted for
    # another relying party.
    azp = claims.get('azp')
    if azp and azp != settings.OIDC_CLIENT_ID:
        raise OIDCError('The ID token was authorised for a different client.')

    if not secrets.compare_digest(str(claims.get('nonce', '')), pending.nonce):
        raise OIDCError('The ID token nonce does not match this authorization request.')

    return claims


def fetch_userinfo(access_token: str) -> dict:
    """Read UserInfo. Optional: absent or failing, the ID token claims stand.

    Called because Identity's `haresign:memberships` scope is more likely to be
    served here than crammed into an ID token, and because a membership read at
    the moment of sign-in is fresher than one minted with the code.
    """
    document = discovery_document()
    endpoint = document.get('userinfo_endpoint')
    if not endpoint:
        return {}
    _require_https(endpoint, 'The userinfo endpoint')
    try:
        response = requests.get(
            endpoint,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=_HTTP_TIMEOUT,
        )
        if response.status_code != 200:
            return {}
        payload = response.json()
    except Exception:
        logger.warning('oidc: userinfo request failed; falling back to ID token claims')
        return {}
    return payload if isinstance(payload, dict) else {}


def end_session_url(id_token: str) -> str:
    """The provider's RP-initiated logout URL, or '' if it advertises none."""
    try:
        document = discovery_document()
    except OIDCError:
        return ''
    endpoint = document.get('end_session_endpoint')
    if not endpoint:
        return ''
    query = urlencode(
        {
            'id_token_hint': id_token,
            'post_logout_redirect_uri': settings.OIDC_POST_LOGOUT_REDIRECT_URI,
            'client_id': settings.OIDC_CLIENT_ID,
        }
    )
    return f'{endpoint}?{query}'
