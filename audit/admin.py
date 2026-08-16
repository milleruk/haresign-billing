from django.contrib import admin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    """Read-only in admin as well as in the model.

    The model already refuses updates and deletes, so this is not the control —
    it is the affordance. An admin that offers a delete button which then raises
    teaches operators that the tool is broken rather than that the record is
    permanent.
    """

    list_display = ('created_at', 'event', 'organization_id', 'support_access', 'request_id')
    list_filter = ('event', 'support_access', 'created_at')
    search_fields = ('event', 'request_id', 'provider_event_id')
    date_hierarchy = 'created_at'
    readonly_fields = tuple(field.name for field in AuditEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
