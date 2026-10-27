"""What an installed plugin bundle declares, and whether macOS has an Audio Unit registered.

A set names a VST3 by display name and an Audio Unit by its exact identity; a
macOS bundle's Info.plist is where both are written down. Reading the plugin
folders and asking Live's database is :mod:`abletoolz.plugins.loadable`'s job;
this module answers for one bundle at a time, and reads no set.
"""

from __future__ import annotations

import ctypes
import functools
import pathlib
import sys
from typing import cast

from abletoolz.machine import read_plist
from abletoolz.plugins.identity.ids import AuIdentifier, fourcc

_AUDIO_TOOLBOX_PATH = "/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox"


class _AudioComponentDescription(ctypes.Structure):
    _fields_ = [
        ("component_type", ctypes.c_uint32),
        ("component_subtype", ctypes.c_uint32),
        ("component_manufacturer", ctypes.c_uint32),
        ("component_flags", ctypes.c_uint32),
        ("component_flags_mask", ctypes.c_uint32),
    ]


def plist_au_identifiers(plist_path: pathlib.Path) -> set[AuIdentifier]:
    """Exact component identities declared by one Audio Unit bundle."""
    plist = read_plist(plist_path)
    identifiers: set[AuIdentifier] = set()
    components = plist.get("AudioComponents")
    if not isinstance(components, list):
        return identifiers
    for component in cast(list[object], components):
        if not isinstance(component, dict):
            continue
        component_data = cast(dict[str, object], component)
        values = [component_data.get(key) for key in ("type", "subtype", "manufacturer")]
        codes = [fourcc(value) for value in values if isinstance(value, str) and len(value) == 4]
        if len(codes) == 3:
            identifiers.add((codes[0], codes[1], codes[2]))
    return identifiers


@functools.cache
def audio_component_registered(identifier: AuIdentifier) -> bool:
    """Ask macOS whether an exact Audio Unit identity is registered."""
    if sys.platform != "darwin":
        return False
    audio_toolbox = ctypes.CDLL(_AUDIO_TOOLBOX_PATH)
    find_next = audio_toolbox.AudioComponentFindNext
    find_next.argtypes = [ctypes.c_void_p, ctypes.POINTER(_AudioComponentDescription)]
    find_next.restype = ctypes.c_void_p
    description = _AudioComponentDescription(*identifier, 0, 0)
    return find_next(None, ctypes.byref(description)) is not None


def plist_declared_names(plist_path: pathlib.Path) -> set[str]:
    """Names a bundle's Info.plist declares. Empty for a malformed plist."""
    plist = read_plist(plist_path)
    names: set[str] = set()
    for key in ("CFBundleDisplayName", "CFBundleName"):
        value = plist.get(key)
        if isinstance(value, str):
            names.add(value)
    components = plist.get("AudioComponents")
    if isinstance(components, list):
        for component in cast(list[object], components):
            if isinstance(component, dict):
                name = cast(dict[str, object], component).get("name")
                if isinstance(name, str):
                    names.add(name)
    return names
