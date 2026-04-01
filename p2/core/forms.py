"""p2 core forms"""
from django import forms
from django.core.validators import RegexValidator

from p2.core.constants import ATTR_BLOB_SIZE_BYTES
from p2.core.models import Storage, Volume
from p2.lib.forms import TagModelForm, TagModelFormMeta
from p2.lib.reflection import path_to_class


# Required tags documentation for each controller
CONTROLLER_REQUIRED_TAGS = {
    'p2.core.storages.null.NullStorageController': [],
    'p2.storage.local.controller.LocalStorageController': [
        ('storage.p2.io/local/root', 'Filesystem path for blob storage (e.g., /storage/data)'),
    ],
    'p2.storage.s3.controller.S3StorageController': [
        ('storage.p2.io/s3/access_key', 'AWS/S3 access key ID'),
        ('storage.p2.io/s3/secret_key', 'AWS/S3 secret access key'),
        ('storage.p2.io/s3/region', 'AWS region (e.g., us-east-1)'),
        ('storage.p2.io/s3/endpoint', '(Optional) Custom S3 endpoint URL for MinIO, etc.'),
        ('storage.p2.io/s3/ssl_verify', '(Optional) Set to false to disable SSL verification'),
    ],
}


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Build help text showing required tags for each controller
        help_lines = ['<strong>Required tags by controller type:</strong><br>']
        for controller_path, tags in CONTROLLER_REQUIRED_TAGS.items():
            controller_name = controller_path.split('.')[-1]
            if not tags:
                help_lines.append(f'<em>{controller_name}</em>: No tags required<br>')
            else:
                required = [t for t in tags if not t[1].startswith('(Optional)')]
                tag_list = ', '.join(f'<code>{t[0]}</code>' for t in required)
                help_lines.append(f'<em>{controller_name}</em>: {tag_list}<br>')
        
        self.fields['tags'].help_text = ''.join(help_lines)

    def clean_tags(self):
        controller_path = self.cleaned_data.get('controller_path')
        if not controller_path:
            return self.cleaned_data.get('tags') or {}
        
        controller_class = path_to_class(controller_path)
        controller = controller_class(self.instance)
        tags = self.cleaned_data.get('tags') or {}
        
        missing_tags = []
        for key in controller.get_required_tags():
            if key not in tags:
                missing_tags.append(key)
        
        if missing_tags:
            # Get friendly names from our documentation
            tag_info = CONTROLLER_REQUIRED_TAGS.get(controller_path, [])
            tag_descriptions = {t[0]: t[1] for t in tag_info}
            
            error_parts = []
            for tag in missing_tags:
                desc = tag_descriptions.get(tag, '')
                if desc:
                    error_parts.append(f"'{tag}' ({desc})")
                else:
                    error_parts.append(f"'{tag}'")
            
            raise forms.ValidationError(
                f"Missing required tags: {', '.join(error_parts)}. "
                f"Add them to the tags field as JSON, e.g.: {{\"{missing_tags[0]}\": \"value\"}}"
            )
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
