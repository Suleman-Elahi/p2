"""URL patterns for authlib OIDC authentication."""
from django.urls import path

from p2.auth.views import oidc_callback, oidc_login

app_name = 'p2_auth'

urlpatterns = [
    path('login/', oidc_login, name='oidc_login'),
    path('callback/', oidc_callback, name='oidc_callback'),
]
