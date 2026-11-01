"""KORG's legacy effects and instruments are recognized by their unique ids."""

from __future__ import annotations

from abletoolz.plugins.identity.ids import Vst2Id
from abletoolz.plugins.state.all_plugins import CATALOG


def test_korg_legacy_plugins_are_recognized() -> None:
    for uid, map_id in ((0x4B4C4656, "korg.mde-x"), (0x4B4C5056, "korg.polysix"), (0x4B4C5756, "korg.wavestation")):
        found = CATALOG.find(Vst2Id(uid))
        assert found is not None and found.id == map_id
