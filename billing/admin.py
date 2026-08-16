from django.contrib import admin

from .models import (
    BillingAccount,
    BillingContact,
    ComplimentaryGrant,
    InvoiceReference,
    MemberOrganizationLink,
    Subscription,
    SubscriptionItem,
)


class BillingContactInline(admin.TabularInline):
    model = BillingContact
    extra = 0


@admin.register(BillingAccount)
class BillingAccountAdmin(admin.ModelAdmin):
    list_display = ('organization_name', 'organization_id', 'status', 'provider_customer_id')
    list_filter = ('status', 'organization_type', 'provider')
    search_fields = ('organization_id', 'organization_name', 'provider_customer_id')
    readonly_fields = ('id', 'organization_id', 'created_at', 'updated_at')
    inlines = [BillingContactInline]


class SubscriptionItemInline(admin.TabularInline):
    model = SubscriptionItem
    extra = 0
    readonly_fields = ('id', 'created_at', 'updated_at')


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    """The provider is the source of truth. This is a read-only local view of it.

    Editing a subscription here would drift from the provider and, worse, would
    grant or revoke a customer's access without a payment event behind it — which
    is precisely the shape of change this service exists to make traceable.
    """

    list_display = ('account', 'plan', 'state', 'current_period_end', 'cancel_at_period_end')
    list_filter = ('state', 'plan', 'cancel_at_period_end', 'provider')
    search_fields = (
        'account__organization_id',
        'account__organization_name',
        'provider_subscription_id',
        'provider_customer_id',
    )
    readonly_fields = tuple(field.name for field in Subscription._meta.fields)
    inlines = [SubscriptionItemInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ComplimentaryGrant)
class ComplimentaryGrantAdmin(admin.ModelAdmin):
    """Kept for auditing. Granting goes through billing.services so the audit
    events cannot be missed."""

    list_display = ('account', 'plan', 'expires_at', 'revoked_at', 'reason')
    list_filter = ('plan', 'revoked_at')
    search_fields = ('account__organization_id', 'account__organization_name', 'reason')
    readonly_fields = ('id', 'created_at', 'updated_at')


@admin.register(InvoiceReference)
class InvoiceReferenceAdmin(admin.ModelAdmin):
    list_display = ('number', 'account', 'status', 'total_minor', 'currency', 'issued_at')
    list_filter = ('status', 'currency', 'provider')
    search_fields = ('number', 'provider_invoice_id', 'account__organization_id')
    readonly_fields = tuple(field.name for field in InvoiceReference._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(MemberOrganizationLink)
class MemberOrganizationLinkAdmin(admin.ModelAdmin):
    """A cache of Identity's organisation graph, not an authority over it."""

    list_display = ('parent_organization_id', 'child_organization_id', 'source', 'observed_at')
    list_filter = ('source',)
    search_fields = ('parent_organization_id', 'child_organization_id')
    readonly_fields = ('id', 'created_at', 'updated_at')
