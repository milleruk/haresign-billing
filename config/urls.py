"""URL configuration.

Route order is deliberate. The provider webhook and the internal API are matched
before anything session-shaped: neither carries a Django session, and both must be
reachable without the OIDC middleware chain ever looking at them.
"""

from django.contrib import admin
from django.urls import include, path

from web import views as web_views

urlpatterns = [
    # Container probes. Loopback HTTP, exempt from the HTTPS redirect.
    path('health/', web_views.health, name='health'),
    path('ready/', web_views.ready, name='ready'),
    # Machine-to-machine. Signature-authenticated, no session.
    path('api/', include('api.urls')),
    # Provider deliveries. Signature-verified, CSRF-exempt, no session.
    path('providers/', include('providers.urls')),
    # Browser-facing.
    path('auth/', include('identity.urls')),
    path('organizations/', include('billing.urls')),
    path('admin/', admin.site.urls),
    path('', include('web.urls')),
]
