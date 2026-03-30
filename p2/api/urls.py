"""p2 API Urls"""
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView, TokenVerifyView

from p2.api.viewsets import APIKeyViewSet, UserViewSet
from p2.core.api.viewsets import StorageViewSet, VolumeViewSet
from p2.s3.views.presign import PresignedURLView
from p2.serve.api.viewsets import ServeRuleViewSet

ROUTER = DefaultRouter()
ROUTER.register('core/volume', VolumeViewSet)
ROUTER.register('core/storage', StorageViewSet)
ROUTER.register('system/user', UserViewSet)
ROUTER.register('system/key', APIKeyViewSet)
ROUTER.register('tier0/policy', ServeRuleViewSet)

app_name = 'p2_api'
urlpatterns = [
    path('v1/', include(ROUTER.urls)),
    path('v1/s3/presign/', PresignedURLView.as_view(), name='s3-presign'),
    # JWT authentication endpoints (djangorestframework-simplejwt)
    path('auth/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/token/verify/', TokenRefreshView.as_view(), name='token_verify'),
    # OpenAPI schema endpoints (drf-spectacular)
    path('schema/', SpectacularAPIView.as_view(), name='schema'),
    path('schema/swagger-ui/', SpectacularSwaggerView.as_view(url_name='p2_api:schema'), name='swagger-ui'),
    path('schema/redoc/', SpectacularRedocView.as_view(url_name='p2_api:schema'), name='redoc'),
]
