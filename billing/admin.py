from django.contrib import admin

from .models import (
    BillingAccount,
    BillingContact,
    ComplimentaryGrant,
    EntitlementAllocation,
    InvoiceReference,
    OperationalAlert,
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


@admin.register(EntitlementAllocation)
class EntitlementAllocationAdmin(admin.ModelAdmin):
    """Who a subscription is for, as distinct from who pays for it.

    Read-only. An allocation is created by a purchase and withdrawn by an
    administrator or by the organisation graph moving; hand-editing one here would
    grant or remove paid access with no audit event and no reason attached.
    """

    list_display = (
        'subscription',
        'beneficiary_organization_id',
        'status',
        'status_changed_at',
        'created_at',
    )
    list_filter = ('status',)
    search_fields = ('beneficiary_organization_id', 'subscription__provider_subscription_id')
    readonly_fields = tuple(field.name for field in EntitlementAllocation._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(OperationalAlert)
class OperationalAlertAdmin(admin.ModelAdmin):
    """Decisions waiting for a person.

    Acknowledgement is the one field that may be edited here, because
    acknowledging is exactly the human act this model exists to capture.
    """

    list_display = (
        'created_at',
        'kind',
        'organization_id',
        'beneficiary_organization_id',
        'acknowledged_at',
    )
    list_filter = ('kind', 'acknowledged_at')
    search_fields = ('organization_id', 'beneficiary_organization_id')
    readonly_fields = (
        'id',
        'kind',
        'organization_id',
        'beneficiary_organization_id',
        'subscription',
        'detail',
        'created_at',
    )

    def has_add_permission(self, request):
        return False
