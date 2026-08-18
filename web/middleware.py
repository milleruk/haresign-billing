"""Response security headers.

A small explicit middleware rather than a dependency. `django-csp` is well
maintained and would be a reasonable choice, but the whole policy here is nine
static directives with no nonces, no per-view overrides and no report-only
mode — the entire feature is the twenty lines below, and a dependency would add
a version to track and a settings vocabulary to learn for no correctness gain.
That trade would flip the moment nonces or per-view policies are needed, which
is what the ``_policy_for`` hook is for.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from django.conf import settings


class ContentSecurityPolicyMiddleware:
    """Set an enforced Content-Security-Policy on every response.

    Enforced, not report-only: a report-only policy on a service this small is a
    way of deferring the decision indefinitely. Everything is same-origin
    already, so there is nothing to break.

    The header is applied to *every* response, including 404s, 429s and
    ``/health/``. Error responses are exactly where an injected payload would
    like to land — they are often the pages that echo user input — and skipping
    them would leave the most reflective responses in the application unprotected.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.policy = self._build(self._directives())

    @staticmethod
    def _directives() -> dict:
        """The configured policy, plus the one destination a form here targets.

        The sign-out control is a form — POST only, because a GET sign-out is
        fired by any prefetch or link scanner — and it ends by redirecting to
        Identity's RP-initiated logout endpoint. WebKit enforces `form-action`
        against the *redirect target* of a form submission, not only against the
        form's own action, so under a bare `form-action 'self'` Safari and every
        browser on iOS refuse that redirect and sign-out appears to do nothing.
        Chrome and Firefox check the immediate action alone and never showed it.

        The sign-out form is in the base template, so this cannot be scoped to
        one route the way Identity scopes its consent screen — every page
        carrying the control needs the destination allowed. It is taken from
        `OIDC_ISSUER`, so no hostname is written here, and it is added only when
        the relying party is configured at all.
        """
        directives = dict(settings.CSP_DIRECTIVES)
        issuer = (settings.OIDC_ISSUER or '').strip() if settings.OIDC_ENABLED else ''
        if not issuer:
            return directives
        origin = urlsplit(issuer)
        if not (origin.scheme and origin.netloc):
            return directives
        form_action = directives.get('form-action', "'self'")
        directives['form-action'] = f'{form_action} {origin.scheme}://{origin.netloc}'
        return directives

    @staticmethod
    def _build(directives: dict) -> str:
        policy = '; '.join(f'{name} {value}' for name, value in directives.items())
        if getattr(settings, 'CSP_UPGRADE_INSECURE_REQUESTS', False):
            # Off in development: it would rewrite the loopback HTTP smoke
            # environment to https and break it for no benefit.
            policy += '; upgrade-insecure-requests'
        return policy

    def _policy_for(self, request) -> str:
        """Hook for a future route-specific policy.

        There is no exception today, including for Django admin. That was
        verified rather than assumed: Django 5.2's admin templates contain no
        inline ``<script>`` and no ``style`` attributes, and its widgets set
        styles through the CSSOM (``element.style.x = …``), which CSP does not
        restrict. See docs/security.md.
        """
        return self.policy

    def __call__(self, request):
        response = self.get_response(request)
        # setdefault, so a view that has deliberately set its own policy keeps it.
        response.headers.setdefault('Content-Security-Policy', self._policy_for(request))
        response.headers.setdefault('X-Robots-Tag', 'noindex, nofollow')
        return response
