from django.urls import path

from . import views

app_name = 'billing'

urlpatterns = [
    path('', views.home, name='home'),
    path('<uuid:organization_id>/', views.organization_billing, name='organization'),
    # The shape the subscription card in Haresign Identity reads. Identity shows
    # it and links to the page above; it never stores it.
    path('<uuid:organization_id>/summary.json', views.organization_summary, name='summary'),
    path('<uuid:organization_id>/checkout/', views.start_checkout, name='checkout'),
    path('<uuid:organization_id>/portal/', views.open_portal, name='portal'),
]
