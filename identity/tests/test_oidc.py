"""OIDC relying-party validation and session security.

Every check in `identity/client.py` gets a test that supplies a token failing
exactly that check and asserts no session was created. A relying party is only as
good as its least-tested validation, and the ones people forget — audience, `azp`,
nonce, issuer-as-identifier — are the ones that matter most.

The provider here is synthetic: a real RSA key generated per test class, real
RS256 signing, and a discovery document and JWKS served from memory. Nothing
reaches Haresign Identity, and no production client exists.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import TestCase, override_settings
from django.urls import reverse

from audit import events
from audit.models import AuditEvent
from identity.client import OIDCError, validate_id_token
from identity.models import IdentityUser, SessionMembership
from identity.views import PENDING_KEY

ISSUER = 'https://identity.test.invalid'
CLIENT_ID = 'haresign-billing-synthetic'

SETTINGS = {
    'OIDC_ENABLED': True,
    'OIDC_ISSUER': ISSUER,
    'OIDC_CLIENT_ID': CLIENT_ID,
    'OIDC_CLIENT_SECRET': 'synthetic-disposable-client-secret',
    'OIDC_REDIRECT_URI': 'http://testserver/auth/callback/',
}


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class OIDCTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private_key = _key()
        cls.public_key = cls.private_key.public_key()

    def discovery(self):
        return {
            'issuer': ISSUER,
            'authorization_endpoint': f'{ISSUER}/oauth/authorize/',
            'token_endpoint': f'{ISSUER}/oauth/token/',
            'userinfo_endpoint': f'{ISSUER}/oauth/userinfo/',
            'jwks_uri': f'{ISSUER}/oauth/jwks/',
            'end_session_endpoint': f'{ISSUER}/oauth/logout/',
            'code_challenge_methods_supported': ['S256'],
        }

    def token(self, **overrides):
        now = int(time.time())
        claims = {
            'iss': ISSUER,
            'sub': str(uuid.uuid4()),
            'aud': CLIENT_ID,
            'exp': now + 600,
            'iat': now,
            'nonce': 'the-nonce',
            'name': 'Synthetic Person',
            'email': 'person@identity-test.invalid',
        }
        claims.update(overrides)
        for key, value in list(claims.items()):
            if value is None:
                del claims[key]
        return jwt.encode(claims, self.private_key, algorithm='RS256')

    def pending(self, **overrides):
        from identity.client import AuthorizationRequest

        values = {
            'state': 'the-state',
            'nonce': 'the-nonce',
            'code_verifier': 'the-verifier',
            'created_at': time.time(),
            'next_url': '/',
        }
        values.update(overrides)
        return AuthorizationRequest(**values)


@override_settings(**SETTINGS)
class IdTokenValidationTests(OIDCTestCase):
    def setUp(self):
        self.discovery_patch = patch(
            'identity.client.discovery_document', return_value=self.discovery()
        )
        self.discovery_patch.start()
        self.addCleanup(self.discovery_patch.stop)

        signing = type('K', (), {'key': self.public_key})()
        self.jwks_patch = patch(
            'identity.client.PyJWKClient.get_signing_key_from_jwt', return_value=signing
        )
        self.jwks_patch.start()
        self.addCleanup(self.jwks_patch.stop)

    def test_a_valid_token_validates(self):
        claims = validate_id_token(self.token(), self.pending())
        self.assertEqual(claims['name'], 'Synthetic Person')

    def test_a_token_from_a_different_issuer_is_refused(self):
        """The issuer is an identifier, compared exactly. A prefix comparison is
        how a service ends up trusting `…haresign.net.attacker.example`."""
        with self.assertRaises(OIDCError):
            validate_id_token(self.token(iss=f'{ISSUER}.attacker.invalid'), self.pending())

    def test_a_token_for_a_different_audience_is_refused(self):
        """A token minted for another relying party of the same provider is
        perfectly valid and says nothing about our session."""
        with self.assertRaises(OIDCError):
            validate_id_token(self.token(aud='some-other-client'), self.pending())

    def test_a_token_whose_azp_is_another_client_is_refused(self):
        with self.assertRaises(OIDCError):
            validate_id_token(self.token(aud=[CLIENT_ID, 'other'], azp='other'), self.pending())

    def test_an_expired_token_is_refused(self):
        with self.assertRaises(OIDCError):
            validate_id_token(self.token(exp=int(time.time()) - 3600), self.pending())

    def test_a_token_with_the_wrong_nonce_is_refused(self):
        """Without this, a token minted for another session of this same client is
        replayable here."""
        with self.assertRaises(OIDCError):
            validate_id_token(self.token(nonce='somebody-elses-nonce'), self.pending())

    def test_a_token_with_no_nonce_is_refused(self):
        with self.assertRaises(OIDCError):
            validate_id_token(self.token(nonce=None), self.pending())

    def test_a_token_missing_a_required_claim_is_refused(self):
        for claim in ('sub', 'exp', 'iat'):
            with self.subTest(claim=claim), self.assertRaises(OIDCError):
                validate_id_token(self.token(**{claim: None}), self.pending())

    def test_an_unsigned_token_is_refused(self):
        """`alg: none`. Refused by allow-listing the asymmetric algorithms rather
        than by blocking the bad ones."""
        unsigned = jwt.encode(
            {
                'iss': ISSUER,
                'sub': str(uuid.uuid4()),
                'aud': CLIENT_ID,
                'exp': int(time.time()) + 600,
                'iat': int(time.time()),
                'nonce': 'the-nonce',
            },
            key='',
            algorithm='none',
        )
        with self.assertRaises(OIDCError):
            validate_id_token(unsigned, self.pending())

    def test_a_token_signed_with_the_wrong_key_is_refused(self):
        other = _key()
        forged = jwt.encode(
            {
                'iss': ISSUER,
                'sub': str(uuid.uuid4()),
                'aud': CLIENT_ID,
                'exp': int(time.time()) + 600,
                'iat': int(time.time()),
                'nonce': 'the-nonce',
            },
            other,
            algorithm='RS256',
        )
        with self.assertRaises(OIDCError):
            validate_id_token(forged, self.pending())

    def test_the_error_never_quotes_the_token(self):
        try:
            validate_id_token(self.token(aud='other'), self.pending())
        except OIDCError as exc:
            self.assertNotIn('eyJ', str(exc))
        else:
            self.fail('expected a refusal')


@override_settings(**SETTINGS)
class DiscoveryTests(OIDCTestCase):
    def test_a_discovery_document_naming_another_issuer_is_refused(self):
        """RFC 8414 §3.3. This is the check that makes discovery safe to trust."""
        from django.core.cache import cache

        from identity.client import discovery_document

        cache.clear()
        document = {**self.discovery(), 'issuer': 'https://somebody-else.invalid'}
        response = type(
            'R', (), {'raise_for_status': lambda self: None, 'json': lambda self: document}
        )()
        with patch('identity.client.requests.get', return_value=response):
            with self.assertRaises(OIDCError):
                discovery_document()

    def test_a_provider_without_pkce_s256_is_refused(self):
        from django.core.cache import cache

        from identity.client import discovery_document

        cache.clear()
        document = {**self.discovery(), 'code_challenge_methods_supported': ['plain']}
        response = type(
            'R', (), {'raise_for_status': lambda self: None, 'json': lambda self: document}
        )()
        with patch('identity.client.requests.get', return_value=response):
            with self.assertRaises(OIDCError):
                discovery_document()

    def test_a_plain_http_issuer_is_refused(self):
        from django.core.cache import cache

        from identity.client import discovery_document

        cache.clear()
        with override_settings(OIDC_ISSUER='http://identity.test.invalid'):
            with self.assertRaises(OIDCError):
                discovery_document()


@override_settings(**SETTINGS)
class AuthorizationRequestTests(OIDCTestCase):
    def test_the_request_carries_pkce_state_and_nonce(self):
        from urllib.parse import parse_qs, urlparse

        from identity.client import build_authorization_request

        with patch('identity.client.discovery_document', return_value=self.discovery()):
            url, pending = build_authorization_request()

        query = parse_qs(urlparse(url).query)
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertTrue(query['code_challenge'][0])
        self.assertEqual(query['state'], [pending.state])
        self.assertEqual(query['nonce'], [pending.nonce])
        self.assertEqual(query['response_type'], ['code'])
        self.assertEqual(query['redirect_uri'], [SETTINGS['OIDC_REDIRECT_URI']])

    def test_the_challenge_is_the_sha256_of_the_verifier(self):
        import base64
        import hashlib
        from urllib.parse import parse_qs, urlparse

        from identity.client import build_authorization_request

        with patch('identity.client.discovery_document', return_value=self.discovery()):
            url, pending = build_authorization_request()

        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(pending.code_verifier.encode()).digest())
            .rstrip(b'=')
            .decode()
        )
        self.assertEqual(parse_qs(urlparse(url).query)['code_challenge'], [expected])

    def test_the_verifier_never_reaches_the_browser(self):
        """With it, a leaked authorization code becomes redeemable."""
        with patch('identity.client.discovery_document', return_value=self.discovery()):
            response = self.client.get(reverse('identity:login'))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('code_verifier', response['Location'])
        self.assertIn(PENDING_KEY, self.client.session)


@override_settings(**SETTINGS)
class CallbackTests(OIDCTestCase):
    def setUp(self):
        patcher = patch('identity.client.discovery_document', return_value=self.discovery())
        patcher.start()
        self.addCleanup(patcher.stop)

        signing = type('K', (), {'key': self.public_key})()
        jwks = patch('identity.client.PyJWKClient.get_signing_key_from_jwt', return_value=signing)
        jwks.start()
        self.addCleanup(jwks.stop)

        self.url = reverse('identity:callback')

    def _begin(self):
        """Put a pending authorization request in the session, as login does."""
        session = self.client.session
        session[PENDING_KEY] = {
            'state': 'the-state',
            'nonce': 'the-nonce',
            'code_verifier': 'the-verifier',
            'created_at': time.time(),
            'next_url': '/',
        }
        session.save()

    def _complete(self, token=None, state='the-state', code='the-code', memberships=None):
        tokens = {'id_token': token or self.token(), 'access_token': 'synthetic-access-token'}
        userinfo = {'haresign_memberships': memberships} if memberships is not None else {}
        with (
            patch('identity.views.exchange_code', return_value=tokens),
            patch('identity.views.fetch_userinfo', return_value=userinfo),
        ):
            return self.client.get(self.url, {'state': state, 'code': code})

    def test_a_valid_callback_signs_the_person_in(self):
        self._begin()
        response = self._complete()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(IdentityUser.objects.count(), 1)
        self.assertIn('_auth_user_id', self.client.session)

    def test_a_replayed_state_is_refused(self):
        """The pending request is single-use: popped from the session before the
        code is redeemed."""
        self._begin()
        self._complete()
        self.client.logout()
        response = self._complete()
        self.assertEqual(response.status_code, 400)

    def test_a_mismatched_state_is_refused(self):
        self._begin()
        response = self._complete(state='an-attackers-state')
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_a_callback_with_no_pending_request_is_refused(self):
        response = self._complete()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(IdentityUser.objects.count(), 0)

    def test_an_expired_authorization_request_is_refused(self):
        session = self.client.session
        session[PENDING_KEY] = {
            'state': 'the-state',
            'nonce': 'the-nonce',
            'code_verifier': 'v',
            'created_at': time.time() - 99999,
            'next_url': '/',
        }
        session.save()
        self.assertEqual(self._complete().status_code, 400)

    def test_a_provider_error_is_refused(self):
        self._begin()
        response = self.client.get(self.url, {'error': 'access_denied', 'state': 'the-state'})
        self.assertEqual(response.status_code, 400)

    def test_a_bad_token_creates_no_user(self):
        self._begin()
        response = self._complete(token=self.token(aud='another-client'))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(IdentityUser.objects.count(), 0)

    def test_every_refusal_is_audited(self):
        self._begin()
        self._complete(state='wrong')
        self.assertTrue(AuditEvent.objects.filter(event=events.OIDC_VALIDATION_FAILED).exists())

    def test_the_refusal_page_names_no_specific_check(self):
        """Telling an attacker which check failed is free reconnaissance."""
        self._begin()
        body = self._complete(state='wrong').content.decode()
        for leak in ('state', 'nonce', 'audience', 'issuer', 'signature'):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, body.lower().split('<footer')[0])

    def test_the_session_key_is_cycled_on_sign_in(self):
        """A session fixated before sign-in must not survive it."""
        self.client.get('/')
        self._begin()
        before = self.client.session.session_key
        self._complete()
        self.assertNotEqual(before, self.client.session.session_key)

    def test_memberships_are_stored_for_this_session_only(self):
        self._begin()
        organization_id = str(uuid.uuid4())
        self._complete(
            memberships=[
                {
                    'organization_id': organization_id,
                    'name': 'A Practice',
                    'type': 'practice',
                    'role': 'organization_admin',
                },
                {'organization_id': str(uuid.uuid4()), 'role': 'member'},
            ]
        )
        rows = SessionMembership.objects.all()
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.filter(is_administrator=True).count(), 1)
        self.assertEqual(
            set(rows.values_list('session_key', flat=True)), {self.client.session.session_key}
        )

    def test_an_unrecognised_role_is_stored_but_not_administrative(self):
        self._begin()
        self._complete(
            memberships=[{'organization_id': str(uuid.uuid4()), 'role': 'something_new'}]
        )
        self.assertFalse(SessionMembership.objects.get().is_administrator)

    def test_a_malformed_membership_does_not_cost_the_whole_sign_in(self):
        self._begin()
        response = self._complete(
            memberships=[
                'not a dict',
                {'no_organization': True},
                {'organization_id': str(uuid.uuid4()), 'role': 'organization_admin'},
            ]
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SessionMembership.objects.count(), 1)

    def test_platform_admin_status_is_refreshed_in_both_directions(self):
        self._begin()
        subject = str(uuid.uuid4())
        self._complete(token=self.token(sub=subject, haresign_platform_admin=True))
        self.assertTrue(IdentityUser.objects.get(identity_user_id=subject).is_platform_admin)

        self.client.logout()
        self._begin()
        self._complete(token=self.token(sub=subject))
        self.assertFalse(IdentityUser.objects.get(identity_user_id=subject).is_platform_admin)

    def test_an_open_redirect_is_not_followed(self):
        session = self.client.session
        session[PENDING_KEY] = {
            'state': 'the-state',
            'nonce': 'the-nonce',
            'code_verifier': 'v',
            'created_at': time.time(),
            'next_url': 'https://attacker.invalid/phish',
        }
        session.save()
        response = self._complete()
        self.assertEqual(response['Location'], '/')

    def test_a_protocol_relative_next_is_not_followed(self):
        session = self.client.session
        session[PENDING_KEY] = {
            'state': 'the-state',
            'nonce': 'the-nonce',
            'code_verifier': 'v',
            'created_at': time.time(),
            'next_url': '//attacker.invalid/phish',
        }
        session.save()
        self.assertEqual(self._complete()['Location'], '/')


@override_settings(**SETTINGS)
class LogoutTests(OIDCTestCase):
    def test_logout_refuses_a_get(self):
        """A GET sign-out is fired by any prefetch or link scanner."""
        self.assertEqual(self.client.get(reverse('identity:logout')).status_code, 405)

    def test_logout_ends_the_session_and_its_memberships(self):
        from billing.tests import factories

        user = factories.identity_user()
        factories.sign_in(self.client, user, [{'organization_id': uuid.uuid4()}])
        self.assertEqual(SessionMembership.objects.count(), 1)

        with patch('identity.views.end_session_url', return_value=''):
            self.client.post(reverse('identity:logout'))

        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(SessionMembership.objects.count(), 0)

    def test_logout_is_audited(self):
        from billing.tests import factories

        user = factories.identity_user()
        factories.sign_in(self.client, user, [])
        with patch('identity.views.end_session_url', return_value=''):
            self.client.post(reverse('identity:logout'))
        self.assertTrue(AuditEvent.objects.filter(event=events.SESSION_ENDED).exists())

    def test_logout_redirects_to_the_provider_when_it_advertises_one(self):
        from billing.tests import factories

        user = factories.identity_user()
        factories.sign_in(self.client, user, [])
        session = self.client.session
        session['oidc_id_token'] = 'a-synthetic-id-token'
        session.save()

        with patch('identity.client.discovery_document', return_value=self.discovery()):
            response = self.client.post(reverse('identity:logout'))
        self.assertIn(f'{ISSUER}/oauth/logout/', response['Location'])


class DisabledTests(TestCase):
    @override_settings(OIDC_ENABLED=False)
    def test_sign_in_is_unavailable_rather_than_broken(self):
        response = self.client.get(reverse('identity:login'))
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, 'unavailable', status_code=503)


class SessionSecurityTests(TestCase):
    def test_the_session_cookie_is_named_for_this_service_only(self):
        from django.conf import settings

        self.assertEqual(settings.SESSION_COOKIE_NAME, 'hs_billing_sessionid')
        self.assertNotEqual(settings.SESSION_COOKIE_NAME, 'sessionid')

    def test_sessions_are_stored_server_side(self):
        from django.conf import settings

        self.assertEqual(settings.SESSION_ENGINE, 'django.contrib.sessions.backends.db')

    def test_the_id_token_is_never_rendered(self):
        from billing.tests import factories

        user = factories.identity_user()
        factories.sign_in(self.client, user, [])
        session = self.client.session
        session['oidc_id_token'] = 'a-synthetic-id-token-value'
        session.save()
        self.assertNotContains(self.client.get('/'), 'a-synthetic-id-token-value')

    def test_the_id_token_is_never_audited(self):
        import json as json_module

        from audit.services import record

        record('test.event', metadata={'id_token': 'a-synthetic-id-token-value'})
        stored = AuditEvent.objects.get(event='test.event').metadata
        self.assertNotIn('a-synthetic-id-token-value', json_module.dumps(stored))
