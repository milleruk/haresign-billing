"""Template context shared by every page."""

from django.conf import settings


def site(request):
    """Brand and environment values.

    ``SITE_NAME`` is always "Haresign Billing". The repository is called
    haresign-billing; that name is internal and must never reach a template — see
    AGENTS.md and the test that enforces it.
    """
    return {
        'SITE_NAME': settings.SITE_NAME,
        'SITE_BASE_URL': settings.SITE_BASE_URL,
        # Non-empty in every environment that is not production. Rendered beside
        # the wordmark, because somebody looking at a subscription state needs to
        # know whether it is the real one before they act on it.
        'ENVIRONMENT_LABEL': settings.ENVIRONMENT_LABEL,
        'BILLING_CHECKOUT_ENABLED': settings.BILLING_CHECKOUT_ENABLED,
        'legal': settings.LEGAL,
        'HARESIGN_WEB_URL': settings.HARESIGN_WEB_URL,
    }
