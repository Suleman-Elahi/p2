"""UI URLS"""
from django.urls import path
from p2.ui.views import general

app_name = 'p2_ui'
urlpatterns = [
    # General (fallback redirect / fallback to spa-index)
    path('', general.SpaIndexView.as_view(), name='index'),
]
