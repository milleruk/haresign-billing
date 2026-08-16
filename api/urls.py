"""Versioned internal API routes.

The version is in the **path**, not a header. A consumer's configuration then
names the contract it was written against, and a breaking change is a new path
that old consumers simply never call rather than a header they forgot to send.
"""

from django.urls import path

from . import views

app_name = 'api'

urlpatterns = [
    path(
        'v1/organizations/<uuid:organization_id>/entitlements/',
        views.organization_entitlements,
        name='organization_entitlements',
    ),
    path('v1/products/', views.catalogue, name='products'),
]
