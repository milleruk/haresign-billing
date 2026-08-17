"""Template context describing the signed-in person and what they may act for."""

from django.conf import settings


def identity_session(request):
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {'OIDC_ENABLED': settings.OIDC_ENABLED}

    from .authorization import administered_memberships

    memberships = administered_memberships(request)
    return {
        'OIDC_ENABLED': settings.OIDC_ENABLED,
        'IDENTITY_DISPLAY_NAME': user.display_name,
        # Only the organisations this person may actually administer. The header
        # switcher must never offer one the authorization check would refuse.
        'ADMIN_MEMBERSHIPS': memberships,
        'HARESIGN_IDENTITY_URL': settings.HARESIGN_IDENTITY_URL,
    }
