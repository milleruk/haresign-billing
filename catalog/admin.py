from django.contrib import admin

from .models import Plan, PlanPrice, Product


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    """Product keys are the contract with Haresign Intelligence.

    `key` is read-only after creation: changing one silently revokes every
    consumer's configured entitlement check, and there is no way to notice except
    by a customer reporting a tool that stopped opening.
    """

    list_display = ('key', 'name', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('key', 'name')

    def get_readonly_fields(self, request, obj=None):
        return ('key', 'id') if obj else ('id',)


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ('key', 'name', 'scope', 'covers_member_organizations', 'is_active')
    list_filter = ('scope', 'is_active', 'covers_member_organizations')
    search_fields = ('key', 'name')
    filter_horizontal = ('products',)

    def get_readonly_fields(self, request, obj=None):
        return ('key', 'id') if obj else ('id',)


@admin.register(PlanPrice)
class PlanPriceAdmin(admin.ModelAdmin):
    list_display = (
        'plan',
        'interval',
        'display_amount',
        'currency',
        'provider_price_id',
        'is_active',
    )
    list_filter = ('interval', 'currency', 'provider', 'is_active')
    search_fields = ('plan__key', 'provider_price_id')
