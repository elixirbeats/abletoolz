"""The set's transport: its own settings, tempo and arrangement length."""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from abletoolz.elements import get_element
from abletoolz.errors import SetError
from abletoolz.live_set.settings import Setting, find, resolve
from abletoolz.schema import Attr, Tag, objects, path
from abletoolz.schema.objects import Mixer
from abletoolz.schema.versioning import Version

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

    def setting(self, member: objects.Transport) -> Setting:
        """One of the set's transport settings: ``setting(Transport.LOOP_ON)``."""
        return resolve(self._element(), member, self.version, label="Transport")

    def find_setting(self, member: objects.Transport) -> Setting | None:
        """:meth:`setting`, or None where this set's transport has no such setting."""
        return find(self._element(), member, self.version, label="Transport")

    def _element(self) -> ET.Element:
        return get_element(self._root, path(Tag.LIVE_SET, Tag.TRANSPORT))

    def bpm(self) -> float:
        """The main track's tempo."""
        main = self._set.tracks.main()
        if self.version >= (9, 7, 0):
            tempo = main.find_setting(Mixer.TEMPO)
            bpm_value = None if tempo is None else tempo.raw
        else:
            # Before 9.7 the tempo a set plays at is its first tempo automation
            # event, not the Manual value: a difference of meaning, read by hand.
            bpm_value = get_element(
                main.track_root,
                path(Tag.DEVICE_CHAIN, Tag.MIXER, Tag.TEMPO, Tag.ARRANGER_AUTOMATION, Tag.EVENTS, Tag.FLOAT_EVENT),
                attribute=Attr.VALUE,
            )
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
