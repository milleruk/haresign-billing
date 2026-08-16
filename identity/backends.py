"""The only authentication backend, and it authenticates nothing by itself.

Sign-in happens in `identity/views.py` after the OIDC flow has been fully
validated; `login()` is then called with this backend named explicitly. There is
deliberately no `authenticate()` that takes a password, because there is no
password in this service to take.
"""

from __future__ import annotations

from .models import IdentityUser


class IdentityOIDCBackend:
    """Session plumbing for users established by the OIDC callback."""

    def authenticate(self, request, **credentials):
        # Never authenticates. A backend that accepted credentials here would be a
        # second sign-in path that had skipped every check in identity/client.py.
        return None

    def get_user(self, user_id):
        return IdentityUser.objects.filter(pk=user_id, is_active=True).first()

    def has_perm(self, user_obj, perm, obj=None):
        return False

    def has_module_perms(self, user_obj, app_label):
        return False
