from django.contrib import admin

from .models import IdentityUser, SessionMembership


@admin.register(IdentityUser)
class IdentityUserAdmin(admin.ModelAdmin):
    """Shells for people Identity owns. Read-mostly on purpose.

    Every field that means anything is refreshed from the ID token at each
    sign-in, so editing one here is a change that survives until the person next
    signs in and is then silently reverted. `is_staff` is the exception — Django
    admin access here is a local operational decision.
    """

    list_display = (
        'identity_user_id',
        'display_name',
        'is_platform_admin',
        'is_active',
        'last_login',
    )
    list_filter = ('is_platform_admin', 'is_active', 'is_staff')
    search_fields = ('identity_user_id', 'display_name')
    readonly_fields = (
        'id',
        'identity_user_id',
        'display_name',
        'email',
        'is_platform_admin',
        'first_seen_at',
        'last_login',
        'password',
    )

    def has_add_permission(self, request):
        return False


@admin.register(SessionMembership)
class SessionMembershipAdmin(admin.ModelAdmin):
    """Per-session claims, not a membership table. They expire with the session."""

    list_display = (
        'user',
        'organization_id',
        'organization_name',
        'role',
        'is_administrator',
        'captured_at',
    )
    list_filter = ('is_administrator', 'role')
    search_fields = ('organization_id', 'organization_name')
    readonly_fields = tuple(field.name for field in SessionMembership._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
