"""Every vendor module, imported in one place so the plugin catalog is complete.

Reach the catalog through here. A vendor module registers its plugins, and the
converters between them, when it is imported, and this is the one list of
them: a vendor missing from it is a set of plugins abletoolz reports as
unmapped, never one it quietly handles differently.
"""

from abletoolz.plugins.converters import fabfilter as _fabfilter_conversions  # noqa: F401
from abletoolz.plugins.converters import izotope_trash as _trash_conversion  # noqa: F401
from abletoolz.plugins.converters import surge as _surge_conversion  # noqa: F401
from abletoolz.plugins.state import (  # noqa: F401
    akai,
    arturia,
    devious_machines,
    fabfilter,
    goodhertz,
    izotope,
    izotope_trash,
    izotope_trash2,
    kilohearts,
    native_instruments,
    serato,
    surge_synth_team,
    valhalla,
    xfadelooper,
    xfer,
)
from abletoolz.plugins.state.catalog import CATALOG

__all__ = ["CATALOG"]
