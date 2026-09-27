"""Tempo and arrangement-length queries."""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from abletoolz import schema
from abletoolz.misc import SetError, get_element
from abletoolz.schema import Attr, Tag, path
from abletoolz.versioning import Version

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class SetLength:
    """Estimated set length; 4/4 assumed."""

    bars: int
    bpm: float
    seconds: float

    def __str__(self) -> str:
        return f"{int(self.seconds // 60)}:{round(self.seconds % 60):02d}"


class Transport:
    """Tempo and timeline facts of one set."""

    def __init__(self, live_set: AbletonSet) -> None:
        self._set = live_set

    @property
    def version(self) -> Version:
        return self._set.version_tuple

    @property
    def _root(self) -> ET.Element:
        return self._set.root

    def bpm(self) -> float:
        """Master/main track tempo."""
        master = schema.tag("master_track", self.version)
        manual = path(Tag.LIVE_SET, master, Tag.DEVICE_CHAIN, Tag.MIXER, Tag.TEMPO, Tag.MANUAL)
        automation = path(
            Tag.LIVE_SET,
            master,
            Tag.DEVICE_CHAIN,
            Tag.MIXER,
            Tag.TEMPO,
            Tag.ARRANGER_AUTOMATION,
            Tag.EVENTS,
            Tag.FLOAT_EVENT,
        )
        if self.version >= (9, 7, 0):
            bpm_value = get_element(self._root, manual, attribute=Attr.VALUE, silent_error=True)
        else:
            bpm_value = get_element(self._root, automation, attribute=Attr.VALUE)
        if bpm_value is None:
            raise SetError("Couldn't find BPM in set XML")
        self._set.bpm = round(float(bpm_value), 6)
        return self._set.bpm

    def furthest_bar(self) -> int:
        """Max of the longest clip or furthest arrangement position, in bars."""
        current_end_times = [int(float(el.get(Attr.VALUE, 0))) for el in self._root.iter(Tag.CURRENT_END)]
        self._set.furthest_bar = int(max(current_end_times) / 4) if current_end_times else 0
        return self._set.furthest_bar

    def length(self) -> SetLength:
        """Estimated length from bpm and furthest bar."""
        # TODO use the set's time signature instead of assuming 4/4.
        bpm = self.bpm()
        bars = self.furthest_bar()
        return SetLength(bars=bars, bpm=bpm, seconds=((4 * bars) / bpm) * 60)
