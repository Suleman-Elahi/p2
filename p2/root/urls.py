"""p2 Root URLs"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views
from django.urls import include, path, re_path
from p2.auth.views import P2LoginView
from django.views.generic import RedirectView

from p2.ui.views.errors import ServerErrorView
from p2.ui.views.general import SpaIndexView

admin.site.index_title = 'p2 Admin'
admin.site.site_title = 'p2'
admin.site.login = RedirectView.as_view(
    pattern_name='p2_ui:index', permanent=True, query_string=True)

# pylint: disable=invalid-name
handler500 = ServerErrorView.as_view()

# S3 URLs get routed via middleware
urlpatterns = [
    # Frappe UI static assets (served by Django when Nginx isn't available)
    re_path(r'^assets/(?P<asset_path>.+)$', SpaIndexView.as_view(), name='spa-asset'),
    # Frappe UI SPA routes — serves index.html for SPA frontend routing.
    re_path(r'^(?:$|(?:login|buckets|settings)(?:/.*)?$)', SpaIndexView.as_view(), name='spa-index'),
    path('_/admin/', admin.site.urls),
    path('_/api/', include('p2.api.urls', namespace='p2_api')),
    path('api/', include('p2.api.urls', namespace='p2_api_public')),
    path('_/ui/', include('p2.ui.urls', namespace='p2_ui')),
    path('_/oidc/', include('p2.auth.urls', namespace='p2_auth')),
    path('_/auth/password/', views.PasswordChangeView.as_view(), name='auth_password'),
    path('_/auth/login/', P2LoginView.as_view(), name='auth_login'),
    path('_/auth/logout/', views.LogoutView.as_view(), name='auth_logout'),
    re_path(r'^favicon\.ico/?$', RedirectView.as_view(url='/static/p2/img/icon.png', permanent=True)),
    path('', include('p2.s3.urls', namespace='p2_s3')),
] + static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

if settings.DEBUG:
    try:
        import debug_toolbar
        urlpatterns = [
            path('_/debug/', include(debug_toolbar.urls)),
        ] + urlpatterns
    except ImportError:
        pass
