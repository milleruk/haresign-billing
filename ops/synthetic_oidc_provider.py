"""A synthetic OpenID Connect provider for the isolated rehearsal.

**Not a product, not shipped, and never reachable from a deployment.** It exists
so the rehearsal can drive a real Authorization Code + PKCE flow — real RS256
signatures, real discovery, real JWKS — without touching Haresign Identity and
without a production relying-party client existing anywhere.

It deliberately implements *only* what Haresign Identity implements, so a Billing
relying party that works against this one has not learned to rely on anything
Identity does not offer:

* Authorization Code, and no other grant. No client credentials, no implicit.
* PKCE S256 required. A request without a challenge is refused.
* `state` and `nonce` echoed, exactly.
* RS256 ID tokens with `iss`, `sub`, `aud`, `exp`, `iat`, `nonce`.
* A `haresign:memberships` claim shaped as Identity's is.

The signing key is generated at start-up and lives only in memory, so the
rehearsal leaves no key material behind at all.

Fixture people are declared in `PEOPLE` below. Everything is invented: UUIDs are
fixed so the rehearsal is repeatable, and every address is `.invalid`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = os.environ.get('SYNTHETIC_ISSUER', 'http://oidc_provider:9000')
CLIENT_ID = os.environ.get('SYNTHETIC_CLIENT_ID', 'haresign-billing-rehearsal')
CLIENT_SECRET = os.environ.get('SYNTHETIC_CLIENT_SECRET', 'rehearsal-only-client-secret')
REDIRECT_URI = os.environ.get('SYNTHETIC_REDIRECT_URI', 'http://localhost:8000/auth/callback/')
PORT = int(os.environ.get('SYNTHETIC_PORT', '9000'))

# Fixed, so the rehearsal's assertions are repeatable across runs.
ORG_ALPHA = '11111111-1111-4111-8111-111111111111'
ORG_BETA = '22222222-2222-4222-8222-222222222222'
ORG_PCN = '33333333-3333-4333-8333-333333333333'

# Identity's real organisation-administrator role key, dotted and namespaced,
# exactly as haresign-core/organizations/roles.py defines it. A rehearsal that
# used a different one would rehearse a flow production never runs.
ADMIN_ROLE = 'organization.admin'

PEOPLE = {
    # An administrator of Alpha Practice. The ordinary case.
    'admin': {
        'sub': '44444444-4444-4444-8444-444444444444',
        'name': 'Alex Administrator',
        'email': 'alex@rehearsal.invalid',
        'platform_admin': False,
        'memberships': [
            {
                'organization_id': ORG_ALPHA,
                'organization_type': 'practice',
                'role': ADMIN_ROLE,
            }
        ],
    },
    # A *member* of Alpha Practice, not an administrator. Proves the refusal.
    'member': {
        'sub': '55555555-5555-4555-8555-555555555555',
        'name': 'Morgan Member',
        'email': 'morgan@rehearsal.invalid',
        'platform_admin': False,
        'memberships': [
            {
                'organization_id': ORG_ALPHA,
                'organization_type': 'practice',
                'role': 'organization.member',
            }
        ],
    },
    # An administrator of a *different* organisation. Proves cross-organisation
    # refusal against a real session rather than a contrived one.
    'stranger': {
        'sub': '66666666-6666-4666-8666-666666666666',
        'name': 'Sam Stranger',
        'email': 'sam@rehearsal.invalid',
        'platform_admin': False,
        'memberships': [
            {
                'organization_id': ORG_BETA,
                'organization_type': 'practice',
                'role': ADMIN_ROLE,
            }
        ],
    },
    # Somebody with no organisation membership at all. Phase 4A used this
    # persona to prove the platform-administrator support bypass worked; Phase 4B
    # removed that bypass, so it now proves the opposite — that holding no
    # qualifying membership means holding no billing access.
    'platform': {
        'sub': '77777777-7777-4777-8777-777777777777',
        'name': 'Pat Platform',
        'email': 'pat@rehearsal.invalid',
        'platform_admin': True,
        'memberships': [],
    },
}

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KID = uuid.uuid4().hex
# Authorization codes, held in memory and single-use.
_CODES: dict[str, dict] = {}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def _jwks() -> dict:
    numbers = _KEY.public_key().public_numbers()
    return {
        'keys': [
            {
                'kty': 'RSA',
                'use': 'sig',
                'alg': 'RS256',
                'kid': _KID,
                'n': _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, 'big')),
                'e': _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, 'big')),
            }
        ]
    }


def _discovery() -> dict:
    return {
        'issuer': ISSUER,
        'authorization_endpoint': f'{ISSUER}/oauth/authorize/',
        'token_endpoint': f'{ISSUER}/oauth/token/',
        'userinfo_endpoint': f'{ISSUER}/oauth/userinfo/',
        'jwks_uri': f'{ISSUER}/oauth/jwks/',
        'end_session_endpoint': f'{ISSUER}/oauth/logout/',
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code'],
        'subject_types_supported': ['public'],
        'id_token_signing_alg_values_supported': ['RS256'],
        'code_challenge_methods_supported': ['S256'],
        'scopes_supported': ['openid', 'profile', 'email', 'haresign:memberships'],
        'token_endpoint_auth_methods_supported': ['client_secret_basic'],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        print(f'[oidc] {fmt % args}')

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.end_headers()

    # --- GET -------------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == '/.well-known/openid-configuration':
            return self._json(_discovery())
        if parsed.path == '/oauth/jwks/':
            return self._json(_jwks())
        if parsed.path == '/oauth/logout/':
            target = query.get('post_logout_redirect_uri', ['/'])[0]
            return self._redirect(target)
        if parsed.path == '/oauth/authorize/':
            return self._authorize(query)
        if parsed.path == '/oauth/userinfo/':
            return self._userinfo()
        if parsed.path == '/health':
            return self._json({'status': 'ok'})
        return self._json({'error': 'not_found'}, status=404)

    def _authorize(self, query):
        """Auto-approve for the named fixture person. No consent screen.

        Which person is chosen comes from a `login_hint` — the rehearsal drives
        this by URL, because a consent screen would need a browser to click it and
        the point of the exercise is the protocol, not the chrome.
        """
        who = query.get('login_hint', ['admin'])[0]
        if who not in PEOPLE:
            return self._json({'error': 'unknown_fixture_person'}, status=400)

        if query.get('client_id', [''])[0] != CLIENT_ID:
            return self._json({'error': 'unauthorized_client'}, status=400)
        if query.get('redirect_uri', [''])[0] != REDIRECT_URI:
            # Exact match, as Identity requires.
            return self._json({'error': 'invalid_redirect_uri'}, status=400)
        if query.get('code_challenge_method', [''])[0] != 'S256':
            return self._json(
                {'error': 'invalid_request', 'detail': 'PKCE S256 required'}, status=400
            )
        challenge = query.get('code_challenge', [''])[0]
        if not challenge:
            return self._json({'error': 'invalid_request'}, status=400)

        code = secrets.token_urlsafe(32)
        _CODES[code] = {
            'who': who,
            'challenge': challenge,
            'nonce': query.get('nonce', [''])[0],
            'created': time.time(),
        }
        target = query.get('redirect_uri')[0]
        return self._redirect(
            f'{target}?{urlencode({"code": code, "state": query.get("state", [""])[0]})}'
        )

    def _userinfo(self):
        header = self.headers.get('Authorization', '')
        token = header.removeprefix('Bearer ').strip()
        who = _ACCESS_TOKENS.get(token)
        if not who:
            return self._json({'error': 'invalid_token'}, status=401)
        person = PEOPLE[who]
        return self._json(
            {
                'sub': person['sub'],
                'name': person['name'],
                'email': person['email'],
                # The real envelope Identity serves: a colon in the key, an
                # object with a version, and `memberships` inside it. No
                # platform-administrator claim, because Identity emits none.
                'haresign:memberships': {
                    'version': 1,
                    'memberships': person['memberships'],
                },
            }
        )

    # --- POST ------------------------------------------------------------------

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get('Content-Length', '0'))
        form = parse_qs(self.rfile.read(length).decode())

        if parsed.path != '/oauth/token/':
            return self._json({'error': 'not_found'}, status=404)

        if form.get('grant_type', [''])[0] != 'authorization_code':
            # The only grant Identity offers, so the only one offered here.
            return self._json({'error': 'unsupported_grant_type'}, status=400)

        code = form.get('code', [''])[0]
        record = _CODES.pop(code, None)  # single use
        if record is None:
            return self._json({'error': 'invalid_grant'}, status=400)
        if time.time() - record['created'] > 60:
            return self._json({'error': 'invalid_grant'}, status=400)

        verifier = form.get('code_verifier', [''])[0]
        expected = _b64url(hashlib.sha256(verifier.encode()).digest())
        if expected != record['challenge']:
            return self._json({'error': 'invalid_grant', 'detail': 'PKCE'}, status=400)

        if not self._client_authenticated(form):
            return self._json({'error': 'invalid_client'}, status=401)

        person = PEOPLE[record['who']]
        now = int(time.time())
        id_token = jwt.encode(
            {
                'iss': ISSUER,
                'sub': person['sub'],
                'aud': CLIENT_ID,
                'exp': now + 600,
                'iat': now,
                'nonce': record['nonce'],
                'name': person['name'],
                'email': person['email'],
            },
            _KEY.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            algorithm='RS256',
            headers={'kid': _KID},
        )
        access_token = secrets.token_urlsafe(32)
        _ACCESS_TOKENS[access_token] = record['who']

        return self._json(
            {
                'access_token': access_token,
                'token_type': 'Bearer',
                'expires_in': 600,
                'id_token': id_token,
            }
        )

    def _client_authenticated(self, form) -> bool:
        header = self.headers.get('Authorization', '')
        if header.startswith('Basic '):
            raw = base64.b64decode(header[6:]).decode()
            client_id, _, secret = raw.partition(':')
            return client_id == CLIENT_ID and secrets.compare_digest(secret, CLIENT_SECRET)
        return secrets.compare_digest(form.get('client_secret', [''])[0], CLIENT_SECRET)


_ACCESS_TOKENS: dict[str, str] = {}


if __name__ == '__main__':
    print(f'[oidc] synthetic provider on :{PORT} as {ISSUER}')
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()  # noqa: S104
