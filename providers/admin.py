from django.contrib import admin

from .models import ReconciliationRun, WebhookEvent


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    """The event ledger. Read-only: editing an entry would break idempotency."""

    list_display = (
        'received_at',
        'event_type',
        'outcome',
        'delivery_count',
        'organization_id',
        'provider_event_id',
    )
    list_filter = ('outcome', 'event_type', 'provider')
    search_fields = ('provider_event_id', 'organization_id')
    date_hierarchy = 'received_at'
    readonly_fields = tuple(field.name for field in WebhookEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ReconciliationRun)
class ReconciliationRunAdmin(admin.ModelAdmin):
    list_display = ('started_at', 'provider', 'status', 'applied', 'completed_at')
    list_filter = ('status', 'applied', 'provider')
    readonly_fields = tuple(field.name for field in ReconciliationRun._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
