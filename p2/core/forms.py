"""p2 core forms"""
from django import forms
from django.core.validators import RegexValidator

from p2.core.constants import ATTR_BLOB_SIZE_BYTES
from p2.core.models import Storage, Volume
from p2.lib.forms import TagModelForm, TagModelFormMeta
from p2.lib.reflection import path_to_class


# pylint: disable=too-few-public-methods
class VolumeValidator(RegexValidator):
    """Validate volume name (s3-compatible)"""

    regex = (r'(?=^.{3,63}$)(?!^(\d+\.)+\d+$)(^(([a-z0-9]|[a-z0-9][a-z0-9\-]'
             r'*[a-z0-9])\.)*([a-z0-9]|[a-z0-9][a-z0-9\-]*[a-z0-9])$)')
    message = """Volume names must be at least 3 and no more than 63 characters long.
        Volume names must be a series of one or more labels.
        Volume names can contain lowercase letters, numbers, and hyphens.
        Each label must start and end with a lowercase letter or a number.
        Adjacent labels are separated by a single period (.)
        Volume names must not be formatted as an IP address (for example, 192.168.5.4)
        """


class StorageForm(TagModelForm):
    """storage form"""

    def clean_tags(self):
        controller_class = path_to_class(self.cleaned_data.get('controller_path'))
        controller = controller_class(self.instance)
        tags = self.cleaned_data.get('tags') or {}
        for key in controller.get_required_tags():
            if key not in tags:
                raise forms.ValidationError("Tag '%s' missing." % key)
        return tags

    class Meta(TagModelFormMeta):

        model = Storage
        fields = ['name', 'controller_path', 'tags']
        widgets = {
            'name': forms.TextInput
        }


class VolumeForm(TagModelForm):
    """volume form"""

    name = forms.CharField(validators=[VolumeValidator()])

    class Meta(TagModelFormMeta):

        model = Volume
        fields = ['name', 'storage', 'tags']
