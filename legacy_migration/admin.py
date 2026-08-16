from django.contrib import admin

from .models import (
    ImportRun,
    LegacyAccountMapping,
    LegacyGrantMapping,
    LegacySubscriptionMapping,
)


@admin.register(ImportRun)
class ImportRunAdmin(admin.ModelAdmin):
    list_display = ('started_at', 'operation', 'status', 'exporter_version', 'completed_at')
    list_filter = ('operation', 'status')
    search_fields = ('artifact_sha256',)
    readonly_fields = tuple(field.name for field in ImportRun._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class _MappingAdmin(admin.ModelAdmin):
    """Mappings are written by the importer. Editing one desynchronises a delta."""

    list_display = ('source_record_id', 'last_seen_at', 'source_missing')
    list_filter = ('source_missing',)
    search_fields = ('source_record_id',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


admin.site.register(LegacyAccountMapping, _MappingAdmin)
admin.site.register(LegacySubscriptionMapping, _MappingAdmin)
admin.site.register(LegacyGrantMapping, _MappingAdmin)
