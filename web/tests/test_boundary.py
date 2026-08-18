"""The production boundary and security posture, as configuration assertions.

Every one of these would pass silently as prose in a document. Here they fail a
build.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from providers.fake import FakeProvider, sign

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

    def test_hosted_checkout_and_portal_cannot_reach_stripe(self):
        """Phase 4B implemented both methods against the pinned SDK. They are
        still unreachable, and for the *stronger* reason: constructing the client
        at all requires a secret key no environment sets, so the implementation
        cannot run rather than merely declining to."""
        from providers.base import ProviderError
        from providers.stripe_provider import StripeProvider

        with self.assertRaises(ProviderError):
            StripeProvider().create_checkout_session(
                provider_price_id='price_x',
                success_url='https://example.invalid/ok',
                cancel_url='https://example.invalid/no',
                idempotency_key='k',
            )
        with self.assertRaises(ProviderError):
            StripeProvider().create_portal_session(
                customer_id='cus_x', return_url='https://example.invalid/'
            )

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


def _without_comments(document: str) -> str:
    """Drop comment lines from a compose file.

    The same convention the rest of this module follows: comments and docs may
    *name* a forbidden host, a middleware or a credential, because explaining a
    boundary requires naming it. Only values are asserted against. Without this,
    the comment saying "deliberately not chain-no-auth@file, because it allows
    robots" would fail the test asserting chain-no-auth is not used.
    """
    return '\n'.join(line for line in document.splitlines() if not line.lstrip().startswith('#'))


def _service_blocks(document: str) -> dict[str, str]:
    """Split a compose file into `{service_name: raw_block}`.

    Deliberately crude, and adequate: service names sit at exactly one indent
    level under `services:`, so the block for each runs to the next name at that
    level. It exists so these assertions need no YAML dependency in an image that
    handles payments.
    """
    lines = document.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == 'services:')
    except StopIteration:  # pragma: no cover - a compose file without services
        return {}

    blocks: dict[str, list[str]] = {}
    current = None
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith(' '):
            break  # a new top-level key: networks, volumes, secrets
        match = re.match(r'^  (\w[\w-]*):\s*$', line)
        if match:
            current = match.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return {name: '\n'.join(body) for name, body in blocks.items()}


class TraefikRouterTests(TestCase):
    """The production overlay's routing, read as text.

    A compose file is configuration, not code, so nothing else would notice it
    drifting. These assertions are what stop `billing.haresign.net` being served
    by an edit nobody reviewed as a deployment.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.overlay = _without_comments(
            (Path(settings.BASE_DIR) / 'docker-compose.production.yml').read_text()
        )

    def test_the_router_ships_disabled(self):
        """The single line that keeps the hostname unserved."""
        self.assertIn("traefik.enable: '${BILLING_TRAEFIK_ENABLED:-false}'", self.overlay)

    def test_exactly_one_router_serves_the_hostname(self):
        rules = re.findall(r'traefik\.http\.routers\.([a-z0-9-]+)\.rule', self.overlay)
        self.assertEqual(len(set(rules)), 1, f'expected one router, found {sorted(set(rules))}')

    def test_the_router_is_https_only(self):
        self.assertIn(
            "traefik.http.routers.haresign-billing-rtr.entrypoints: 'websecure'", self.overlay
        )
        self.assertNotIn("entrypoints: 'web'", self.overlay)

    def test_tls_is_on_and_uses_the_entrypoints_default_resolver(self):
        """No per-router certresolver: the existing dns-cloudflare resolver is the
        default on this entrypoint and already holds the wildcard, so this
        hostname needs no new certificate and issues no ACME request."""
        self.assertIn("haresign-billing-rtr.tls: 'true'", self.overlay)
        self.assertNotIn('certresolver', self.overlay)

    def test_there_is_no_basic_auth(self):
        """The preview host has one because it is a preview. This is production,
        and its gate is Haresign Identity."""
        for forbidden in ('basicauth', 'basicAuth', 'users=', 'htpasswd'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.overlay)

    def test_the_shared_robots_allowing_chain_is_not_used(self):
        """`chain-no-auth@file` sets `X-Robots-Tag: all`, the exact opposite of
        this service's noindex rule, and would override what Django sends."""
        self.assertNotIn('chain-no-auth', self.overlay)
        self.assertNotIn('robots-allow', self.overlay)

    def test_the_router_carries_rate_limiting(self):
        self.assertIn('middlewares-rate-limit@file', self.overlay)

    def test_the_headers_middleware_denies_framing_rather_than_sameorigin(self):
        """The shared secure-headers middleware sets SAMEORIGIN, which would
        *weaken* the DENY Django sends. A proxy quietly downgrading the
        application's own header is worse than no proxy header at all."""
        self.assertIn('haresign-billing-headers.headers.frameDeny', self.overlay)
        self.assertNotIn('SAMEORIGIN', self.overlay)

    def test_the_headers_middleware_sets_noindex(self):
        self.assertIn('customResponseHeaders.X-Robots-Tag', self.overlay)
        self.assertIn('noindex', self.overlay)

    def test_the_headers_middleware_sets_hsts_and_nosniff(self):
        for expected in (
            'stsSeconds',
            'stsIncludeSubdomains',
            'contentTypeNosniff',
            'referrerPolicy',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, self.overlay)

    def test_an_explicit_application_port_is_declared(self):
        self.assertIn('haresign-billing-svc.loadbalancer.server.port', self.overlay)

    def test_every_file_backed_secret_is_resolved_before_privileges_drop(self):
        """Each `<NAME>_FILE` on the application must be in the entrypoint allowlist.

        `secret_entrypoint.py` reads these while it is still root and then drops
        to uid 10001, after which a mode-600 root-owned secret is unreadable. A
        `_FILE` variable the entrypoint does not know about does not degrade to
        the direct form or to the default — `env_secret` raises and the container
        will not boot. Nothing short of a real deployment reaches that failure,
        which is precisely why a configuration file must not be able to cause it
        on its own.

        Scoped to the application: `billing_backup` runs as root and reads its
        own recipient file directly.
        """
        from config.secret_entrypoint import SECRET_NAMES

        application = _service_blocks(self.overlay)['haresign_billing']
        wired = set(re.findall(r'(\w+)_FILE: /run/secrets/', application))
        self.assertTrue(wired, 'no file-backed secrets found on the application')
        self.assertEqual(sorted(wired - set(SECRET_NAMES)), [])

    def test_secrets_live_in_one_protected_directory(self):
        """A mode-700 parent means a non-root account cannot even enumerate which
        secrets this service holds, which sibling files at the top of
        `/opt/docker/secrets/` do not give us."""
        files = re.findall(r'^\s+file: (\S+)$', self.overlay, re.MULTILINE)
        self.assertTrue(files)
        for path in files:
            with self.subTest(path=path):
                self.assertTrue(path.startswith('/opt/docker/secrets/haresign-billing/'), path)

    def test_no_stripe_credential_is_declared(self):
        """The provider boundary, asserted at the deployment rather than trusted.

        This stack must not be able to reach Stripe even by misconfiguration, so
        neither the variable nor a secret carrying it appears here at all.
        """
        for forbidden in ('STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET', 'stripe'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.overlay)

    def test_no_monolith_source_dsn_reaches_the_application(self):
        """The importer's source connection is an operator process, never a
        runtime dependency of the service that holds billing state."""
        application = _service_blocks(self.overlay)['haresign_billing']
        for forbidden in ('SOURCE_DSN', 'MONOLITH', 'haresigndb', 'HaresignDB'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, application)

    def test_only_the_web_service_joins_the_proxy_network(self):
        """Putting the database on the shared proxy network would put the billing
        database on the same L2 as every other container on the host.

        Parsed by hand rather than with a YAML library: the test suite runs inside
        the runtime image, which carries runtime dependencies only, and adding one
        to the payment service so a test can read a file it could read anyway is a
        bad trade.
        """
        base = _without_comments((Path(settings.BASE_DIR) / 'docker-compose.yml').read_text())
        for service, block in _service_blocks(base).items():
            with self.subTest(service=service):
                if service == 'haresign_billing':
                    self.assertIn('t3_proxy', block)
                else:
                    self.assertNotIn('t3_proxy', block)

    def test_no_database_or_redis_port_is_published(self):
        for name in ('docker-compose.yml', 'docker-compose.production.yml'):
            blocks = _service_blocks(
                _without_comments((Path(settings.BASE_DIR) / name).read_text())
            )
            for service in ('billing_db', 'billing_redis'):
                if service in blocks:
                    with self.subTest(file=name, service=service):
                        self.assertNotIn('ports:', blocks[service])

    def test_no_secret_appears_in_a_label(self):
        """Labels are readable by anything that can talk to the Docker socket and
        show up in `docker inspect`. Every value here is a header constant."""
        labels = [line for line in self.overlay.splitlines() if line.strip().startswith('traefik.')]
        self.assertTrue(labels)
        for line in labels:
            with self.subTest(label=line.strip()[:60]):
                for forbidden in ('sk_live', 'sk_test', 'whsec_', 'password', 'secret'):
                    self.assertNotIn(forbidden, line.lower())

    def test_no_stripe_credential_is_declared_in_the_overlay(self):
        self.assertNotIn('STRIPE_SECRET_KEY', self.overlay)
        self.assertNotIn('stripe_secret', self.overlay)


class WebhookHardeningTests(TestCase):
    """The one route the public internet reaches without a session."""

    def setUp(self):
        FakeProvider.reset()
        self.url = reverse('providers:webhook')

    def test_the_webhook_needs_no_interactive_session(self):
        """It must answer an unauthenticated POST — with a refusal, but an
        answer. A login redirect here would break every delivery."""
        response = self.client.post(
            self.url,
            data=b'{}',
            content_type='application/json',
            headers={'stripe-signature': 'nonsense'},
        )
        self.assertEqual(response.status_code, 400)

    def test_an_unsigned_delivery_is_refused(self):
        response = self.client.post(self.url, data=b'{}', content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_a_get_is_refused(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_an_oversized_payload_is_refused_before_it_is_parsed(self):
        body = b'{"padding": "' + b'x' * (settings.WEBHOOK_MAX_BODY_BYTES + 1024) + b'"}'
        response = self.client.post(
            self.url,
            data=body,
            content_type='application/json',
            headers={'stripe-signature': sign(body)},
        )
        # 413, and *not* the 400 a bad signature would give — the size check runs
        # first, before the body is verified or parsed.
        self.assertEqual(response.status_code, 413)

    def test_a_payload_within_the_limit_is_processed_normally(self):
        body = b'{"id": "evt_size_ok", "type": "ping", "created": 0}'
        response = self.client.post(
            self.url,
            data=body,
            content_type='application/json',
            headers={'stripe-signature': sign(body)},
        )
        self.assertEqual(response.status_code, 200)

    def test_a_stale_signature_timestamp_is_refused(self):
        """Replay protection at the transport layer, before the event ledger's."""
        body = b'{"id": "evt_stale", "type": "ping", "created": 0}'
        response = self.client.post(
            self.url,
            data=body,
            content_type='application/json',
            headers={'stripe-signature': sign(body, timestamp=int(time.time()) - 86400)},
        )
        self.assertEqual(response.status_code, 400)

    def test_the_webhook_has_a_throttle_scope(self):
        self.assertIn('webhook', settings.THROTTLE_SCOPES)


class SignOutRedirectPolicyTests(TestCase):
    """`form-action` must permit the redirect the sign-out form exists to cause.

    Found on a phone, alongside the same bug in the other direction at Identity.

    Sign-out is POST only — a GET sign-out is fired by any prefetch or link
    scanner — and it ends by redirecting to Identity's RP-initiated logout
    endpoint. WebKit enforces `form-action` against the *redirect target* of a
    form submission, not only against the form's own action, so under a bare
    `form-action 'self'` every browser on iOS refuses that redirect and sign-out
    appears to do nothing. Chrome and Firefox check the immediate action alone.

    The control lives in the base template, so this is not scopeable to one
    route: every page carrying it needs the destination allowed.
    """

    ISSUER = 'https://identity.invalid'

    def _form_action(self, response):
        policy = response['Content-Security-Policy']
        return next(part for part in policy.split(';') if part.strip().startswith('form-action'))

    @override_settings(OIDC_ENABLED=True, OIDC_ISSUER=ISSUER)
    def test_the_issuers_origin_is_permitted_when_the_relying_party_is_configured(self):
        # The middleware builds its policy once at process start, so the
        # directives are asserted directly rather than through a live response.
        from web.middleware import ContentSecurityPolicyMiddleware

        form_action = ContentSecurityPolicyMiddleware._directives()['form-action']
        self.assertIn(self.ISSUER, form_action)
        self.assertIn("'self'", form_action)

    @override_settings(OIDC_ENABLED=False, OIDC_ISSUER=ISSUER)
    def test_nothing_is_permitted_when_the_relying_party_is_off(self):
        from web.middleware import ContentSecurityPolicyMiddleware

        self.assertEqual(ContentSecurityPolicyMiddleware._directives()['form-action'], "'self'")

    @override_settings(OIDC_ENABLED=True, OIDC_ISSUER=ISSUER)
    def test_only_the_origin_is_permitted_never_a_wildcard_or_a_path(self):
        from web.middleware import ContentSecurityPolicyMiddleware

        form_action = ContentSecurityPolicyMiddleware._directives()['form-action']
        self.assertNotIn('*', form_action)
        self.assertEqual(form_action, f"'self' {self.ISSUER}")

    @override_settings(OIDC_ENABLED=True, OIDC_ISSUER='not-a-url')
    def test_an_unparseable_issuer_widens_nothing(self):
        from web.middleware import ContentSecurityPolicyMiddleware

        self.assertEqual(ContentSecurityPolicyMiddleware._directives()['form-action'], "'self'")

    def test_the_rest_of_the_policy_is_untouched(self):
        from web.middleware import ContentSecurityPolicyMiddleware

        directives = ContentSecurityPolicyMiddleware._directives()
        for name in ('default-src', 'base-uri', 'object-src', 'frame-ancestors', 'script-src'):
            with self.subTest(name=name):
                self.assertEqual(directives[name], settings.CSP_DIRECTIVES[name])
