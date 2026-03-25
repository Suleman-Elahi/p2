"""p2 S3 URLs"""
from django.urls import path, register_converter
from django.urls.converters import StringConverter

from p2.s3.views import buckets, objects


class S3BucketConverter(StringConverter):
    """Match S3 bucket regex — excludes '_' which is reserved for internal p2 routes"""

    regex = r'(?!_(?:/|$))([a-zA-Z0-9\.\-\_]+)'


class EverythingConverter(StringConverter):
    """Match Everything. Just used for debugging"""

    regex = '.*'


register_converter(S3BucketConverter, 's3')
register_converter(EverythingConverter, 'everything')

app_name = 'p2_s3'

# These patterns are always available and don't require an AWS Header
urlpatterns = [
    path('<s3:bucket>/', buckets.BucketView.as_view(), name='bucket'),
    path('<s3:bucket>/<everything:path>', objects.ObjectView.as_view(), name='bucket-object'),
]
