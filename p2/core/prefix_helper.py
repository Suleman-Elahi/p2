"""p2 core prefix helpers"""
import posixpath
import re
from typing import List

import logging

from p2.core.constants import ATTR_BLOB_IS_FOLDER

LOGGER = logging.getLogger(__name__)
SEPARATOR = posixpath.sep

def make_absolute_path(path):
    """Ensure prefix is absolute:
    Leading slash is prepended if not set already,
    if string is empty or None was given, / is returned."""
    if not path:
        path = ""
    if path == "" or path[0] != SEPARATOR:
        path = SEPARATOR + path
    # Call normpath to remove trailing slashes
    # Regex replaces duplicate slashes with singular slashes
    return posixpath.normpath(re.sub(r'\/+', SEPARATOR, path))

def make_absolute_prefix(prefix):
    """Same as make_absolute_path, except with a trailing slash"""
    prefix = make_absolute_path(prefix)
    if prefix[-1] != SEPARATOR:
        prefix = prefix + SEPARATOR
    return prefix


# pylint: disable=too-few-public-methods
class BreadCrumb:
    """Virtual Breadcrumb used for navigation"""

    title = ""
    prefix = ""

class VirtualPrefix:
    """Virtual Prefix"""

    volume = None
    absolute_path = ""
    relative_path = None
    blob = None

    def relative_to(self, _to):
        """Return absolute_path converted to a relative path from `_to`"""
        prefix = make_absolute_prefix(_to)
        return posixpath.relpath(self.absolute_path, prefix)

    def __eq__(self, other):
        return self.absolute_path == other.absolute_path \
                and self.volume == other.volume

    def __hash__(self):
        return hash(('absolute_path', self.absolute_path,
                     'volume', self.volume))

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        return f"<VirtualPrefix '{self.absolute_path}' for Blob {self.blob}>"

class PrefixHelper:
    """Get List of prefixes for a volume"""

    _volume = None
    _user = None
    _base = None

    _prefixes = None

    def __init__(self, user, volume, base):
        self._user = user
        self._volume = volume
        self._base = make_absolute_prefix(base)
        self._prefixes = []

    @property
    def prefixes(self) -> List[VirtualPrefix]:
        """Read-only access to prefixes"""
        return self._prefixes

    def add_up_prefix(self):
        """Add up prefix, useful for filebrowser"""
        up_prefix = VirtualPrefix()
        up_prefix.volume = self._volume
        up_prefix.absolute_path = posixpath.normpath(posixpath.join(self._base, '..'))
        up_prefix.relative_path = up_prefix.relative_to(self._base)
        self._prefixes.insert(0, up_prefix)

    def _prefix_for_blob(self, blob) -> VirtualPrefix:
        prefix_object = VirtualPrefix()
        prefix_object.blob = blob
        prefix_object.volume = self._volume
        if ATTR_BLOB_IS_FOLDER in blob.attributes:
            prefix_object.absolute_path = blob.path
        else:
            prefix_object.absolute_path = blob.prefix
        prefix_object.relative_path = prefix_object.relative_to(self._base)
        return prefix_object

    def _get_intermediate_prefix(self, blob) -> VirtualPrefix:
        """Ensure intermediate prefix objects exist, for example:
        Blob with prefix /test
        Blob with prefix /test/another/prefix;
        this function will create the prefixes [/test/another]"""
        next_part = posixpath.relpath(blob.prefix, self._base).split(SEPARATOR)[0]
        _sub_path = make_absolute_prefix(posixpath.join(self._base, next_part))
        intermedia_prefix = VirtualPrefix()
        intermedia_prefix.blob = blob
        intermedia_prefix.volume = self._volume
        intermedia_prefix.absolute_path = _sub_path
        intermedia_prefix.relative_path = next_part
        return intermedia_prefix

    def get_breadcrumbs(self):
        """Get list of breadcrumbs up until self.base"""
        until_here = []
        crumbs = []
        for part in self._base.split(SEPARATOR):
            if part == '':
                continue
            until_here.append(part)
            crumb = BreadCrumb()
            crumb.title = part
            crumb.prefix = SEPARATOR.join(until_here)
            crumbs.append(crumb)
        return crumbs

    def collect(self, max_levels=0):
        """Get Prefixes for user, optionally filtering out prefixes
        not starting with `base`"""
        from p2.core.models import Blob
        base_lookup = Blob.objects.filter(
            volume=self._volume,
            prefix__startswith=self._base)
        file_objects = base_lookup.distinct("prefix")
        folder_objects = base_lookup.filter(attributes__has_key=ATTR_BLOB_IS_FOLDER)
        objects = file_objects.union(folder_objects)

        # Make max_level relative to base
        _max_level = self._base.count(SEPARATOR) + max_levels
        LOGGER.debug("Finding prefixes with base: %s", self._base)
        for blob in objects:
            v_prefix = self._prefix_for_blob(blob)
            if ATTR_BLOB_IS_FOLDER in blob.attributes and blob.prefix == self._base:
                if v_prefix not in self._prefixes:
                    LOGGER.debug('Adding v_prefix: %s', v_prefix)
                    self._prefixes.append(v_prefix)
            else:
                if blob.prefix == self._base:
                    LOGGER.debug("Prefix equals base, ignoring it: %s", blob.prefix)
                    continue
                if max_levels and blob.prefix.count(SEPARATOR) > _max_level:
                    # Max levels is set, so ignore prefixes that have more separators than we want
                    LOGGER.debug("Making intermediate prefix: %s", v_prefix)
                    v_prefix = self._get_intermediate_prefix(blob)
                if v_prefix not in self._prefixes:
                    LOGGER.debug('Adding v_prefix: %s', v_prefix)
                    self._prefixes.append(v_prefix)
