"""Every vendor module, imported in one place so the plugin catalog is complete.

Reach the catalog through here. A vendor module registers its plugins, and the
converters between them, when it is imported, and this is the one list of
them: a vendor missing from it is a set of plugins abletoolz reports as
unmapped, never one it quietly handles differently.
"""

from abletoolz.plugins.converters import absynth as _absynth_conversion  # noqa: F401
from abletoolz.plugins.converters import analog_lab as _analog_lab_conversion  # noqa: F401
from abletoolz.plugins.converters import arturia_v2 as _arturia_v2_conversions  # noqa: F401
from abletoolz.plugins.converters import cs_80_v as _cs_80_v_conversion  # noqa: F401
from abletoolz.plugins.converters import fabfilter as _fabfilter_conversions  # noqa: F401
from abletoolz.plugins.converters import izotope_ozone as _ozone_conversion  # noqa: F401
from abletoolz.plugins.converters import izotope_trash as _trash_conversion  # noqa: F401
from abletoolz.plugins.converters import korg_ms20 as _korg_ms20_conversion  # noqa: F401
from abletoolz.plugins.converters import lounge_lizard as _lounge_lizard_conversion  # noqa: F401
from abletoolz.plugins.converters import minimoog_v as _minimoog_v_conversion  # noqa: F401
from abletoolz.plugins.converters import ohmicide as _ohmicide_conversion  # noqa: F401
from abletoolz.plugins.converters import pro53 as _pro53_conversion  # noqa: F401
from abletoolz.plugins.converters import serum as _serum_conversion  # noqa: F401
from abletoolz.plugins.converters import surge as _surge_conversion  # noqa: F401
from abletoolz.plugins.state import (  # noqa: F401
    akai,
    analog_lab,
    arturia,
    arturia_v2,
    audio_damage,
    camel_audio,
    cs_80_v,
    cs_80_v3,
    devious_machines,
    fabfilter,
    goodhertz,
    izotope,
    izotope_ozone,
    izotope_trash,
    izotope_trash2,
    keyscape,
    kilohearts,
    korg_legacy_cell,
    korg_legacy_plugins,
    korg_ms20,
    korg_ms20_v,
    lounge_lizard,
    mini_v3,
    minimoog_v,
    native_instruments,
    ohmicide,
    ohmicide_s,
    pro53,
    prophet_5_v,
    serato,
    sonimus,
    subsonic_labs,
    surge_synth_team,
    twin_delay,
    u_he,
    valhalla,
    xfadelooper,
    xfer,
)
from abletoolz.plugins.state.catalog import CATALOG

__all__ = ["CATALOG"]
