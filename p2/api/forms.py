"""API Key forms"""
from django import forms

from p2.api.models import APIKey


class APIKeyForm(forms.ModelForm):
    """API Key form"""

    class Meta:

        model = APIKey
        fields = ['name', 'user', 'access_key']
        widgets = {
            'name': forms.TextInput
        }
