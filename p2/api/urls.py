"""p2 API Urls"""
from django.urls import path
from p2.api.ninja_api import api
from p2.s3.views.presign import PresignedURLView

app_name = 'p2_api'
urlpatterns = [
    # Main Ninja Control Plane API (includes /api/v1/docs automatically)
    path('v1/', api.urls),
    
    # S3 Presigned URL View (standard Django Class Based View)
    path('v1/s3/presign/', PresignedURLView.as_view(), name='s3-presign'),
]
