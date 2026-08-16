"""The production boundary and security posture, as configuration assertions.

Every one of these would pass silently as prose in a document. Here they fail a
build.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase, override_settings

REPO_ROOT = Path(settings.BASE_DIR)

# Hosts this service must never name in a *value*. Comments and docs may name them,
# because explaining a boundary requires naming it.
FORBIDDEN_HOSTS = (
    'app.haresign.net',
    'auth.haresign.net',
    'identity.haresign.net',
)


class ProductionBoundaryTests(TestCase):
    def test_no_production_host_is_configured(self):
        values = [
            *settings.ALLOWED_HOSTS,
            settings.SITE_BASE_URL,
            settings.OIDC_ISSUER,
            settings.OIDC_REDIRECT_URI,
            settings.HARESIGN_IDENTITY_URL,
        ]
        for value in values:
            for host in FORBIDDEN_HOSTS:
                with self.subTest(value=value, host=host):
                    self.assertNotIn(host, value)

    def test_billing_host_is_not_served(self):
        self.assertNotIn('billing.haresign.net', settings.ALLOWED_HOSTS)

    def test_there_is_exactly_one_database_and_it_is_ours(self):
        self.assertEqual(list(settings.DATABASES), ['default'])
        self.assertIn('billing', settings.DATABASES['default']['NAME'])

    def test_no_stripe_credential_is_configured(self):
        self.assertEqual(settings.STRIPE_SECRET_KEY, '')
        self.assertEqual(settings.STRIPE_WEBHOOK_SECRET, '')

    def test_the_provider_backend_is_the_fake(self):
        """No configuration in this phase reaches Stripe."""
        self.assertEqual(settings.PROVIDER_BACKEND, 'fake')

    def test_checkout_is_disabled(self):
        self.assertFalse(settings.BILLING_CHECKOUT_ENABLED)

    def test_no_stripe_call_is_reachable_without_a_secret_key(self):
        from providers.base import ProviderError
        from providers.stripe_provider import StripeProvider

        with self.assertRaises(ProviderError):
            StripeProvider().fetch_subscription('sub_anything')

    def test_hosted_checkout_and_portal_are_refused_by_the_stripe_adapter(self):
        from providers.base import ProviderError
        from providers.stripe_provider import StripeProvider

        with self.assertRaises(ProviderError):
            StripeProvider().create_checkout_session()
        with self.assertRaises(ProviderError):
            StripeProvider().create_portal_session()

    def test_no_source_database_configuration_exists(self):
        for name in dir(settings):
            if 'SOURCE' in name and 'DATABASE' in name:
                self.fail(f'{name} would give the Billing runtime a route to the monolith.')


class SecurityPostureTests(TestCase):
    def test_session_cookie_is_host_only(self):
        """The monolith shares one cookie across *.haresign.net. Billing must not
        join that arrangement."""
        self.assertIsNone(getattr(settings, 'SESSION_COOKIE_DOMAIN', None))

    def test_cookies_are_httponly_and_samesite(self):
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, 'Lax')

    def test_frames_are_denied_in_every_environment(self):
        self.assertEqual(settings.X_FRAME_OPTIONS, 'DENY')

    def test_there_is_no_local_password_authentication(self):
        self.assertEqual(
            settings.AUTHENTICATION_BACKENDS, ['identity.backends.IdentityOIDCBackend']
        )

    def test_the_authentication_backend_never_authenticates(self):
        from identity.backends import IdentityOIDCBackend

        self.assertIsNone(IdentityOIDCBackend().authenticate(None, password='anything'))

    def test_email_cannot_leave_this_service(self):
        """Dunning and receipts are the provider's job and stay there."""
        self.assertIn(
            settings.EMAIL_BACKEND,
            (
                'django.core.mail.backends.locmem.EmailBackend',
                'django.core.mail.backends.dummy.EmailBackend',
            ),
        )

    def test_the_csp_has_no_unsafe_directive(self):
        policy = '; '.join(f'{k} {v}' for k, v in settings.CSP_DIRECTIVES.items())
        self.assertNotIn('unsafe-inline', policy)
        self.assertNotIn('unsafe-eval', policy)

    def test_the_csp_closes_the_directives_that_matter(self):
        self.assertEqual(settings.CSP_DIRECTIVES['frame-ancestors'], "'none'")
        self.assertEqual(settings.CSP_DIRECTIVES['object-src'], "'none'")
        self.assertEqual(settings.CSP_DIRECTIVES['form-action'], "'self'")
        self.assertEqual(settings.CSP_DIRECTIVES['base-uri'], "'self'")


class ResponseHeaderTests(TestCase):
    def test_the_csp_is_enforced_on_every_response(self):
        for path in ('/', '/health/', '/no-such-page/'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertIn('Content-Security-Policy', response.headers)
                self.assertNotIn('Content-Security-Policy-Report-Only', response.headers)

    def test_every_response_is_noindex(self):
        for path in ('/', '/health/'):
            with self.subTest(path=path):
                self.assertIn('noindex', self.client.get(path)['X-Robots-Tag'])

    def test_the_admin_needs_no_csp_exception(self):
        response = self.client.get('/admin/login/')
        self.assertNotIn('unsafe-inline', response['Content-Security-Policy'])

    def test_health_and_readiness_answer(self):
        self.assertEqual(self.client.get('/health/').status_code, 200)
        self.assertEqual(self.client.get('/ready/').status_code, 200)

    def test_probes_are_never_cached(self):
        for path in ('/health/', '/ready/'):
            with self.subTest(path=path):
                self.assertIn('no-store', self.client.get(path)['Cache-Control'])


class TemplateHygieneTests(TestCase):
    """The CSP is only strict because no template needs it not to be."""

    def _templates(self):
        return [path for path in REPO_ROOT.rglob('templates/**/*.html') if '.git' not in str(path)]

    def test_no_template_contains_an_inline_style_or_script(self):
        offenders = []
        for path in self._templates():
            text = path.read_text()
            if '<style' in text or 'style="' in text:
                offenders.append(f'{path.name}: inline style')
            # `<script src=...>` would be fine; an inline body is not. No template
            # here has either, so the simpler assertion is the honest one.
            if '<script' in text:
                offenders.append(f'{path.name}: script tag')
        self.assertEqual(offenders, [])

    def test_no_template_loads_an_off_origin_asset(self):
        """Scans `src=`/`href=` values, not prose. The base template's comment
        explaining that there is no CDN is not a CDN."""
        offenders = []
        pattern = re.compile(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']')
        for path in self._templates():
            for value in pattern.findall(path.read_text()):
                if value.startswith('//') or re.match(r'https?://', value):
                    # Links out to configured Haresign hosts are template
                    # variables, not literals; a literal external URL is not.
                    offenders.append(f'{path.name}: {value}')
        self.assertEqual(offenders, [])


class NamingTests(TestCase):
    def test_the_repository_name_never_reaches_a_page(self):
        """A *page*. `/health/` deliberately names the service to an orchestrator,
        which is not something a person reads."""
        for path in ('/', '/auth/login/'):
            with self.subTest(path=path):
                response = self.client.get(path)
                if response.status_code in (301, 302, 503):
                    continue
                self.assertNotContains(response, 'haresign-billing')

    def test_the_health_endpoint_is_not_html(self):
        response = self.client.get('/health/')
        self.assertEqual(response['Content-Type'], 'application/json')

    def test_the_product_is_named_haresign_billing(self):
        self.assertEqual(settings.SITE_NAME, 'Haresign Billing')
        self.assertContains(self.client.get('/'), 'Haresign Billing')


class SecretHygieneTests(TestCase):
    """Static assertions that no secret has a usable default."""

    def test_no_secret_setting_carries_a_production_default(self):
        """Read from the settings source, not the runtime value.

        Every environment legitimately supplies its own; what must never exist is
        a *default* that would let a misconfigured deployment start with a working
        secret nobody chose.
        """
        source = (REPO_ROOT / 'config' / 'settings.py').read_text()
        for name in (
            'SECRET_KEY',
            'OIDC_CLIENT_SECRET',
            'ENTITLEMENT_API_KEYS',
            'STRIPE_SECRET_KEY',
            'STRIPE_WEBHOOK_SECRET',
            'POSTGRES_PASSWORD',
        ):
            with self.subTest(name=name):
                for line in source.splitlines():
                    if f"env_secret('{name}'" not in line:
                        continue
                    self.assertIn(
                        "''",
                        line,
                        f'{name} has a default. A secret must have no usable default.',
                    )

    def test_the_application_refuses_to_start_without_a_secret_key(self):
        self.assertIn(
            'SECRET_KEY must be set when DEBUG is off',
            (REPO_ROOT / 'config' / 'settings.py').read_text(),
        )

    def test_no_secret_is_committed_to_the_repository(self):
        """Scans for a marker *followed by a plausible key body*.

        A bare `sk_live_` is prose — `docs/security.md` names these markers in
        order to explain the control, exactly as `docs/` may name a production
        hostname. What must never appear is one with a value after it.
        """
        patterns = [
            re.compile(r'sk_(?:live|test)_[A-Za-z0-9]{8,}'),
            re.compile(r'rk_(?:live|test)_[A-Za-z0-9]{8,}'),
            re.compile(r'whsec_(?!fake_)[A-Za-z0-9]{16,}'),
            re.compile(r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----'),
        ]
        offenders = []
        for path in REPO_ROOT.rglob('*'):
            if not path.is_file() or '.git/' in str(path):
                continue
            if path.suffix not in {'.py', '.md', '.txt', '.yml', '.yaml', '.toml', '.html', '.sh'}:
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            if str(path).endswith('test_boundary.py'):
                # The patterns themselves live here.
                continue
            for pattern in patterns:
                if pattern.search(text):
                    offenders.append(f'{path.relative_to(REPO_ROOT)}: {pattern.pattern[:20]}')
        self.assertEqual(offenders, [])

    def test_the_scanner_would_catch_a_real_key(self):
        """A scanner nobody has seen fire is a scanner nobody should trust."""
        pattern = re.compile(r'sk_(?:live|test)_[A-Za-z0-9]{8,}')
        self.assertIsNone(pattern.search('the marker sk_live_ named in prose'))
        self.assertIsNotNone(pattern.search('sk_live_' + 'A1b2C3d4E5f6'))


class LoggingHygieneTests(TestCase):
    def test_no_module_logs_a_request_body_or_a_signature(self):
        offenders = []
        for path in REPO_ROOT.rglob('*.py'):
            if '.git/' in str(path) or '/tests/' in str(path):
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if not stripped.startswith(('logger.', 'logging.')):
                    continue
                # Only what is *interpolated* matters. Strip the format string
                # literal first: `logger.warning('signature verification failed')`
                # says the word and logs nothing.
                arguments = re.sub(r'"[^"]*"|\'[^\']*\'', '', stripped)
                for forbidden in ('request.body', 'payload', 'signature', 'secret', 'id_token'):
                    if forbidden in arguments:
                        offenders.append(f'{path.name}:{number} ({forbidden})')
        self.assertEqual(offenders, [])


@override_settings(THROTTLE_FAIL_OPEN=False)
class ThrottleFailClosedTests(TestCase):
    def test_throttling_refuses_when_the_cache_is_unreachable(self):
        from unittest.mock import patch

        from web.throttling import Throttled, throttle

        request = Client().get('/').wsgi_request
        with patch('web.throttling.cache.add', side_effect=RuntimeError('redis is gone')):
            with self.assertRaises(Throttled):
                throttle(request, 'oidc_login')
