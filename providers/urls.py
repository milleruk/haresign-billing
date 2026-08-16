from django.urls import path

from . import webhooks

app_name = 'providers'

urlpatterns = [
    # The provider posts here. CSRF-exempt and signature-verified in the view;
    # this is the only route in the service a third party is expected to reach.
    path('webhook/', webhooks.webhook, name='webhook'),
]
