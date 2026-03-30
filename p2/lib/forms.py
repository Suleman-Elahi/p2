"""p2 form helpers"""
from django import forms

from p2.lib.fields import YAMLField


class TagModelForm(forms.ModelForm):
    """Base form for models that inherit p2.lib.models.TagModel"""

    def __init__(self, *args, **kwargs):
        # Check if we have an instance, load tags otherwise use an empty dict
        instance = kwargs.get('instance', None)
        tags = instance.tags if instance else {}
        # Make sure all predefined tags exist in tags, and set default if they don't
        predefined_tags = self._meta.model().get_predefined_tags()  # pylint: disable=no-member
        for key, value in predefined_tags.items():
            if key not in tags:
                tags[key] = value
        # Format JSON
        if 'initial' not in kwargs:
            kwargs['initial'] = {}
        kwargs['initial']['tags'] = tags
        super().__init__(*args, **kwargs)

    def clean_tags(self):
        """Make sure all required tags are set"""
        tags = self.cleaned_data.get('tags') or {}
        if hasattr(self.instance, 'get_required_keys') and hasattr(self.instance, 'tags'):
            for key in self.instance.get_required_keys():
                if key not in tags:
                    raise forms.ValidationError("Tag %s missing." % key)
        return tags

# pylint: disable=too-few-public-methods
class TagModelFormMeta:
    """Base Meta class that uses the YAMLField"""

    field_classes = {
        'tags': YAMLField
    }
