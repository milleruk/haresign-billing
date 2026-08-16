from django.urls import path

from . import views

app_name = 'identity'

urlpatterns = [
    path('login/', views.login_view, name='login'),
    # The exact redirect URI registered with Haresign Identity. Changing this path
    # is a coordinated change on both sides, never a unilateral one.
    path('callback/', views.callback_view, name='callback'),
    path('logout/', views.logout_view, name='logout'),
]
