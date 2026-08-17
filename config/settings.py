"""Settings for haresign-billing — the Haresign Billing service.

Repository/service name: haresign-billing. **User-facing product name: Haresign
Billing.** The repository name must never appear in anything a person sees.

Ownership boundary, and it does not bend: this application owns billing accounts
keyed to Identity organisation UUIDs, the product catalogue, plans and prices,
the subscription lifecycle, billing contacts, provider references, derived
entitlements and the webhook ledger. It has its **own** PostgreSQL database. The
Django runtime never connects to the monolith's or to Haresign Identity's
database, and no other application connects to this one's. Integration is by OIDC
(inbound, as a relying party) and a versioned internal entitlement API (outbound,
to Haresign Intelligence).

This is a pre-cutover build. `billing.haresign.net` is not served, no production
OIDC client exists, and no code path here may reach Stripe — see AGENTS.md.

Anything environment-specific is read from the environment. Nothing in this file
is a secret, and no secret has a usable default.
"""

import sys
from pathlib import Path

from .env import env_bool, env_int, env_list, env_secret, env_str

BASE_DIR = Path(__file__).resolve().parent.parent

# The test runner is the one caller that must not depend on Redis being up or on
# a real provider being configured, so a few settings below branch on it.
TESTING = 'test' in sys.argv


# --- Core -------------------------------------------------------------------

DEBUG = env_bool('DEBUG', False)
SECRET_KEY = env_secret('SECRET_KEY', '')

if not SECRET_KEY:
    if DEBUG:
        # Development only, and named so that it can never be mistaken for a real
        # key in a log line or a process listing.
        SECRET_KEY = 'django-insecure-development-only-haresign-billing'  # noqa: S105
    else:
        raise RuntimeError(
            'SECRET_KEY must be set when DEBUG is off. Generate one with: '
            'python -c "import secrets; print(secrets.token_urlsafe(64))"'
        )

# The value every security setting below was derived from. Django's test runner
# forces `settings.DEBUG = False` while tests run, so a test that reads DEBUG
# cannot tell which branch the cookies and headers were built from. This can.
STARTUP_DEBUG = DEBUG

ALLOWED_HOSTS = env_list('ALLOWED_HOSTS', 'localhost,127.0.0.1')

# A wildcard host on a service that mints redirects back from an OIDC provider
# defeats Host-header validation. Refusing to start is the correct response.
if not DEBUG and ('*' in ALLOWED_HOSTS or any(h.startswith('*') for h in ALLOWED_HOSTS)):
    raise RuntimeError('ALLOWED_HOSTS must not contain a wildcard when DEBUG is off.')

CSRF_TRUSTED_ORIGINS = [
    f'https://{host}' for host in ALLOWED_HOSTS if host not in ('localhost', '127.0.0.1', '[::1]')
]

# The canonical public address of this service. It is *not* read from the
# request: the OIDC redirect URI must name the service, not whichever Host header
# happened to arrive.
SITE_BASE_URL = env_str('SITE_BASE_URL', 'http://localhost:8000').rstrip('/')

SITE_NAME = 'Haresign Billing'

# Shown as a badge in the header. Any non-empty value marks the environment as
# non-production. Configuration, not a DEBUG check: staging runs with DEBUG off.
ENVIRONMENT_LABEL = env_str('ENVIRONMENT_LABEL', 'Phase 4A')


# --- Applications -----------------------------------------------------------

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Domain apps, in dependency order.
    'catalog',
    'billing',
    'providers',
    'api',
    # The OIDC relying party. Imports no domain app.
    'identity',
    # Written to by all of the above; imports none of their business logic.
    'audit',
    # Depends on billing, catalog and providers; no domain app imports it.
    'legacy_migration',
    'web',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Sets the enforced Content-Security-Policy on every response, including
    # error responses.
    'web.middleware.ContentSecurityPolicyMiddleware',
    # Stamps a request id used to correlate audit events with logs. Last, so it
    # only runs for requests that survived the security middleware above.
    'audit.middleware.RequestCorrelationMiddleware',
]

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'web.context_processors.site',
                'identity.context_processors.identity_session',
            ],
        },
    },
]


# --- Database ---------------------------------------------------------------
# This service's own PostgreSQL database, on its own container and its own
# volume. It is not the monolith's and not Identity's, and must never be pointed
# at one. Discrete variables rather than a DATABASE_URL, so a password containing
# "@" or "/" needs no URL-encoding and cannot silently truncate a connection
# string.

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': env_str('POSTGRES_DB', 'haresign_billing'),
        'USER': env_str('POSTGRES_USER', 'haresign_billing'),
        'PASSWORD': env_secret('POSTGRES_PASSWORD', ''),
        'HOST': env_str('POSTGRES_HOST', 'localhost'),
        'PORT': env_str('POSTGRES_PORT', '5432'),
        'CONN_MAX_AGE': env_int('DB_CONN_MAX_AGE', 60),
        'OPTIONS': {'connect_timeout': 5},
    }
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# --- Cache ------------------------------------------------------------------
# Holds the entitlement-API response cache and request throttle counters.
# Production requires a real Redis: a silent fall back to per-process memory
# would leave throttling technically "on" while counting each worker separately.

REDIS_URL = env_str('REDIS_URL', '')

if TESTING or (DEBUG and not REDIS_URL):
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'haresign-billing',
        }
    }
elif REDIS_URL:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL,
            'KEY_PREFIX': 'hsbill',
        }
    }
else:
    raise RuntimeError('REDIS_URL must be set when DEBUG is off.')


# --- Authentication ---------------------------------------------------------
# There is no local password anywhere in this service. People sign in at Haresign
# Identity over OIDC and this service holds a server-side session referencing
# their Identity user UUID. `django.contrib.auth` remains installed for the admin
# and for permissions plumbing only; `IdentityUser` rows are shells with an
# unusable password and are never a credential store.

AUTH_USER_MODEL = 'identity.IdentityUser'

AUTHENTICATION_BACKENDS = ['identity.backends.IdentityOIDCBackend']

# Deliberately hasher-free of anything importable as a login path: every account
# is created with an unusable password. The hasher list exists so Django's admin
# `createsuperuser` for a break-glass local operator still behaves sensibly.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.Argon2PasswordHasher']

LOGIN_URL = 'identity:login'
LOGIN_REDIRECT_URL = 'billing:home'
LOGOUT_REDIRECT_URL = 'web:home'


# --- Haresign Identity integration ------------------------------------------
# Billing is an OIDC **relying party**. Authorization Code with mandatory PKCE
# S256, state and nonce, exact redirect URIs, server-side sessions, and full
# issuer/signature/audience/expiry validation — see identity/client.py.
#
# No production client exists. The isolated rehearsal stack uses a synthetic,
# disposable client against a synthetic provider.

OIDC_ENABLED = env_bool('OIDC_ENABLED', TESTING)
OIDC_ISSUER = env_str('OIDC_ISSUER', '')
OIDC_CLIENT_ID = env_str('OIDC_CLIENT_ID', '')
OIDC_CLIENT_SECRET = env_secret('OIDC_CLIENT_SECRET', '')
# Exact, absolute, and never derived from the request Host header.
OIDC_REDIRECT_URI = env_str('OIDC_REDIRECT_URI', f'{SITE_BASE_URL}/auth/callback/')
OIDC_POST_LOGOUT_REDIRECT_URI = env_str('OIDC_POST_LOGOUT_REDIRECT_URI', f'{SITE_BASE_URL}/')
OIDC_SCOPES = env_list('OIDC_SCOPES', 'openid,profile,email,haresign:memberships')
# Seconds of clock skew tolerated when validating `iat`/`exp`/`nbf`.
OIDC_CLOCK_SKEW = env_int('OIDC_CLOCK_SKEW', 60)
# How long a completed authorization request may sit unredeemed.
OIDC_STATE_TTL = env_int('OIDC_STATE_TTL', 600)
# Discovery is fetched over HTTPS only unless a loopback rehearsal says otherwise.
OIDC_ALLOW_INSECURE_LOOPBACK = env_bool('OIDC_ALLOW_INSECURE_LOOPBACK', False)
# How long membership claims captured at sign-in may be relied on before the
# session must re-authorize. Stale membership is a real risk: someone removed
# from an organisation must lose billing access promptly, and an ID token is a
# snapshot. See docs/identity-integration.md.
IDENTITY_MEMBERSHIP_MAX_AGE = env_int('IDENTITY_MEMBERSHIP_MAX_AGE', 900)


# --- The organisation-graph projection ---------------------------------------
# Billing holds a dated, versioned copy of Identity's organisation graph — UUIDs,
# types, active status and active containment edges — fetched from Identity's
# service endpoint. It is the answer to docs/entitlements.md decision D-4, and it
# replaces the unversioned `MemberOrganizationLink` cache Phase 4A shipped.
#
# **There is no database connection to Identity, here or anywhere.** The only
# route to the graph is an HTTPS GET with a narrowly scoped rotating credential.

IDENTITY_GRAPH_URL = env_str('IDENTITY_GRAPH_URL', '')
IDENTITY_GRAPH_KEY_ID = env_str('IDENTITY_GRAPH_KEY_ID', '')
IDENTITY_GRAPH_SECRET = env_secret('IDENTITY_GRAPH_SECRET', '')

# How old the projection may be and still be relied on. Past this, **sponsored
# entitlements and new purchases fail closed** — see billing/entitlements.py. An
# organisation's own subscription is never affected, because direct entitlement
# does not consult the graph at all.
#
# An hour rather than a day: the thing being protected against is a practice
# keeping a PCN-funded tool after leaving the PCN, and a day of that is a day of
# somebody using something they are not entitled to.
IDENTITY_GRAPH_MAX_AGE = env_int('IDENTITY_GRAPH_MAX_AGE', 3600)
# How often the scheduled refresh runs. Comfortably inside the maximum age, so an
# occasional failed refresh does not immediately close sponsored entitlements.
IDENTITY_GRAPH_REFRESH_INTERVAL = env_int('IDENTITY_GRAPH_REFRESH_INTERVAL', 600)

if OIDC_ENABLED and not TESTING:
    if not OIDC_ISSUER:
        raise RuntimeError('OIDC_ISSUER must name the Haresign Identity issuer.')
    if not OIDC_CLIENT_ID:
        raise RuntimeError('OIDC_CLIENT_ID must be set when OIDC is enabled.')
    if not OIDC_REDIRECT_URI.startswith(SITE_BASE_URL):
        raise RuntimeError('OIDC_REDIRECT_URI must be an absolute URI under SITE_BASE_URL.')


# --- Payment provider --------------------------------------------------------
# The provider boundary. `fake` is a deterministic in-process implementation used
# by tests and the isolated rehearsal; `stripe` is the real adapter and is
# unreachable without a secret key, which no environment in this phase sets.
#
# There is no default that could reach Stripe by accident.

PROVIDER_BACKEND = env_str('PROVIDER_BACKEND', 'fake')

STRIPE_SECRET_KEY = env_secret('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET = env_secret('STRIPE_WEBHOOK_SECRET', '')
# Pinned in requirements.txt as well; asserted at import time by the adapter so a
# silently upgraded SDK cannot change the wire behaviour of a payment integration.
STRIPE_API_VERSION = env_str('STRIPE_API_VERSION', '2025-10-29.clover')

if PROVIDER_BACKEND == 'stripe' and not STRIPE_SECRET_KEY:
    raise RuntimeError('PROVIDER_BACKEND=stripe requires STRIPE_SECRET_KEY.')

# Whether hosted checkout and portal links are offered in the UI. Off until
# cutover; the pages render the plan and state without a purchase route.
BILLING_CHECKOUT_ENABLED = env_bool('BILLING_CHECKOUT_ENABLED', False)


# --- Entitlement API ---------------------------------------------------------
# Service-to-service credentials for the internal entitlement API. Haresign
# Identity does not advertise an OAuth client-credentials grant, so this is a
# narrowly scoped, documented internal mechanism rather than a pretend OAuth
# flow — see docs/entitlements.md.
#
# Format: comma-separated `key_id:secret` pairs. Only digests are ever compared,
# only key ids are ever logged, and rotation is overlap-capable because more than
# one pair may be configured at once.

ENTITLEMENT_API_KEYS = env_secret('ENTITLEMENT_API_KEYS', '')
# How long a consumer may cache an entitlement answer. Also the ceiling this
# service advertises in its own response metadata.
ENTITLEMENT_CACHE_SECONDS = env_int('ENTITLEMENT_CACHE_SECONDS', 60)
ENTITLEMENT_API_THROTTLE = {
    'ip_limit': env_int('ENTITLEMENT_API_IP_LIMIT', 600),
    'ip_window': env_int('ENTITLEMENT_API_IP_WINDOW', 60),
    'id_limit': env_int('ENTITLEMENT_API_ID_LIMIT', 600),
    'id_window': env_int('ENTITLEMENT_API_ID_WINDOW', 60),
}


# --- Throttling --------------------------------------------------------------

TRUSTED_PROXY_COUNT = env_int('TRUSTED_PROXY_COUNT', 1 if not DEBUG else 0)


def _scope(prefix: str, ip_limit: int, ip_window: int, id_limit: int, id_window: int) -> dict:
    return {
        'ip_limit': env_int(f'{prefix}_IP_LIMIT', ip_limit),
        'ip_window': env_int(f'{prefix}_IP_WINDOW', ip_window),
        'id_limit': env_int(f'{prefix}_ID_LIMIT', id_limit),
        'id_window': env_int(f'{prefix}_ID_WINDOW', id_window),
    }


THROTTLE_SCOPES = {
    'oidc_login': _scope('OIDC_LOGIN_THROTTLE', 60, 900, 30, 900),
    'oidc_callback': _scope('OIDC_CALLBACK_THROTTLE', 60, 900, 30, 900),
    'entitlement_api': ENTITLEMENT_API_THROTTLE,
    'webhook': _scope('WEBHOOK_THROTTLE', 1200, 60, 1200, 60),
}

# Production fails **closed**: a cache outage must not silently remove the only
# rate protection the OIDC and webhook endpoints have.
THROTTLE_FAIL_OPEN = env_bool('THROTTLE_FAIL_OPEN', DEBUG)


# --- Sessions and cookies ---------------------------------------------------
# Host-only cookies. SESSION_COOKIE_DOMAIN is deliberately never set: the
# monolith shares one cookie across *.haresign.net and Billing must not join that
# arrangement. Sessions here are for this service's own pages only.

SESSION_COOKIE_NAME = 'hs_billing_sessionid'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = env_int('SESSION_COOKIE_AGE', 60 * 60 * 8)
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_ENGINE = 'django.contrib.sessions.backends.db'
# Secure by default outside development. Overridable *only* so the isolated
# loopback-HTTP rehearsal can exercise the real POST flows: a Secure cookie is
# never returned over plain HTTP, so with it forced on every CSRF-protected form
# in that environment 403s. Production never sets these and gets `not DEBUG`.
SESSION_COOKIE_SECURE = env_bool('SESSION_COOKIE_SECURE', not DEBUG)

CSRF_COOKIE_NAME = 'hs_billing_csrftoken'
CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SECURE = env_bool('CSRF_COOKIE_SECURE', not DEBUG)
CSRF_COOKIE_HTTPONLY = False


# --- Transport security -----------------------------------------------------

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

X_FRAME_OPTIONS = 'DENY'

if not DEBUG:
    # `and not TESTING`: the test client speaks plain HTTP, so with the redirect
    # on, every request 301s and the suite asserts against a redirect body. The
    # production default stays True.
    SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', True) and not TESTING
    # Container probes call loopback HTTP directly, and Stripe posts to the
    # webhook through the proxy which terminates TLS.
    SECURE_REDIRECT_EXEMPT = [r'^(health|ready)/$']
    SECURE_HSTS_SECONDS = env_int('SECURE_HSTS_SECONDS', 31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'
    SECURE_CROSS_ORIGIN_OPENER_POLICY = 'same-origin'


# --- Content Security Policy -------------------------------------------------
# Enforced (not report-only) on every response. Strict because it can be: every
# asset is same-origin, there is no CDN, no analytics and not one inline style or
# script. `form-action 'self'` is relaxed to nothing here — the hosted-checkout
# redirect is a 302, not a cross-origin form post, precisely so this directive
# can stay closed.

CSP_DIRECTIVES = {
    'default-src': "'self'",
    'base-uri': "'self'",
    'object-src': "'none'",
    'frame-ancestors': "'none'",
    'form-action': "'self'",
    'img-src': "'self' data:",
    'font-src': "'self'",
    'style-src': "'self'",
    'script-src': "'self'",
    'connect-src': "'self'",
}

CSP_UPGRADE_INSECURE_REQUESTS = env_bool('CSP_UPGRADE_INSECURE_REQUESTS', not DEBUG)


# --- Email ------------------------------------------------------------------
# This service sends no email. Dunning and receipts are the provider's job and
# stay there. The backend is pinned to dummy so that a stray `send_mail` cannot
# quietly deliver to a person, and there is no SMTP configuration to misdirect.

EMAIL_BACKEND = (
    'django.core.mail.backends.locmem.EmailBackend'
    if TESTING
    else 'django.core.mail.backends.dummy.EmailBackend'
)


# --- Company identity --------------------------------------------------------

LEGAL = {
    'company_name': env_str('LEGAL_COMPANY_NAME', 'Haresign'),
    'company_number': env_str('LEGAL_COMPANY_NUMBER', ''),
    'registered_address': env_str('LEGAL_REGISTERED_ADDRESS', ''),
    'ico_registration': env_str('LEGAL_ICO_REGISTRATION', ''),
    'contact_email': env_str('LEGAL_CONTACT_EMAIL', ''),
}

HARESIGN_WEB_URL = env_str('HARESIGN_WEB_URL', 'https://haresign.net').rstrip('/')
# Where to send someone to manage the organisation itself. Configurable so a
# rehearsal can point at a synthetic provider.
HARESIGN_IDENTITY_URL = env_str('HARESIGN_IDENTITY_URL', '').rstrip('/')


# --- Internationalisation ---------------------------------------------------

LANGUAGE_CODE = 'en-gb'
TIME_ZONE = 'Europe/London'
USE_I18N = True
USE_TZ = True


# --- Static files -----------------------------------------------------------

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = []

STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}

if TESTING:
    STORAGES['staticfiles'] = {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}


# --- Logging ----------------------------------------------------------------
# Deliberately plain. Nothing in this application logs a provider secret, a
# webhook signature, a card detail, an amount tied to a person, or a full
# provider payload; see audit/services.py for the metadata scrubbing that keeps
# the same promise for audit records.

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'standard': {'format': '[{asctime}] {levelname} {name}: {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'standard'},
    },
    'root': {'handlers': ['console'], 'level': env_str('LOG_LEVEL', 'INFO')},
    'loggers': {
        'django.security': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'haresign.billing': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
    },
}
