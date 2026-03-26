"""p2 S3 URLs — loaded only when an AWS request is detected."""
from django.conf import settings
from django.urls import include, path, register_converter

from p2.s3.urls import EverythingConverter, S3BucketConverter
from p2.s3.views import buckets, get, objects

try:
    register_converter(S3BucketConverter, 's3')
except ValueError:
    pass
try:
    register_converter(EverythingConverter, 'everything')
except ValueError:
    pass

app_name = 'p2_s3'

urlpatterns = [
    path('<s3:bucket>', buckets.BucketView.as_view(), name='bucket'),
    path('<s3:bucket>/', buckets.BucketView.as_view(), name='bucket'),
    path('<s3:bucket>/<everything:path>', objects.ObjectView.as_view(), name='bucket-object'),
    path('', get.ListView.as_view(), name='list'),
]

if settings.DEBUG:
    try:
        import debug_toolbar
        urlpatterns = [path('_/debug/', include(debug_toolbar.urls))] + urlpatterns
    except ImportError:
        pass
