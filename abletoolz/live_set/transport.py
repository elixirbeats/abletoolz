"""The set's transport: its own settings, tempo and arrangement length."""

from __future__ import annotations

import dataclasses
import logging
import math
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from abletoolz.elements import get_element
from abletoolz.errors import SetError
from abletoolz.live_set.domain import Domain
from abletoolz.live_set.sections import track_envelopes
from abletoolz.live_set.settings import Setting, find, resolve
from abletoolz.schema import Attr, Tag, objects, path
from abletoolz.schema.objects import Mixer
from abletoolz.schema.versioning import versioned

if TYPE_CHECKING:
    from abletoolz.live_set.tracks import AbletonTrack

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True, slots=True)
class Meter:
    """A time signature; Live stores it as ``numerator - 1 + 99 * log2(denominator)``, so 4/4 is 201."""

    numerator: int
    denominator: int

    @classmethod
    def decode(cls, value: int) -> Meter:
        return cls(value % 99 + 1, 2 ** (value // 99))

    @property
    def beats(self) -> float:
        """Beats in one bar; Live's beat is a quarter note in every meter."""
        return self.numerator * 4 / self.denominator

    def __str__(self) -> str:
        return f"{self.numerator}/{self.denominator}"


@dataclasses.dataclass(frozen=True)
class SetLength:
    """Estimated set length at the set's tempo, its bars counted in its own meters."""

    bars: int
    bpm: float
    seconds: float

    def __str__(self) -> str:
        return f"{int(self.seconds // 60)}:{round(self.seconds % 60):02d}"


class Transport(Domain):
    """Tempo and timeline facts of one set."""

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
        bpm_value = self._tempo(self._set.tracks.main())
        if bpm_value is None:
            raise SetError("Couldn't find BPM in set XML")
        self._set.bpm = round(float(bpm_value), 6)
        return self._set.bpm

    @versioned
    def _tempo(self, main: AbletonTrack) -> str | None:
        """Before 9.7 the tempo a set plays at is its first tempo automation event, not the Manual value."""
        return get_element(
            main.track_root,
            path(Tag.DEVICE_CHAIN, Tag.MIXER, Tag.TEMPO, Tag.ARRANGER_AUTOMATION, Tag.EVENTS, Tag.FLOAT_EVENT),
            attribute=Attr.VALUE,
        )

    @_tempo.since((9, 7, 0))  # type: ignore[no-redef]  # @versioned pattern: same name registers the 9.7+ override
    def _tempo(self, main: AbletonTrack) -> str | None:
        """From 9.7 the tempo is the main mixer's Tempo setting."""
        tempo = main.find_setting(Mixer.TEMPO)
        return None if tempo is None else tempo.raw

    def meters(self) -> list[tuple[float, Meter]]:
        """The arrangement's time signatures, each with the beat it starts on; the first starts on 0."""
        main = self._set.tracks.main()
        signature = get_element(main.track_root, path(Tag.DEVICE_CHAIN, Tag.MIXER, Mixer.TIME_SIGNATURE))
        events = list(signature.iter(Tag.ENUM_EVENT))  # before Live 10 the envelope sits inside the parameter
        if not events:
            target = get_element(signature, Tag.AUTOMATION_TARGET, attribute=Attr.ID)
            for envelope in track_envelopes(main.track_root):
                if get_element(envelope, path(Tag.ENVELOPE_TARGET, Tag.POINTEE_ID), attribute=Attr.VALUE) == target:
                    events = list(envelope.iter(Tag.ENUM_EVENT))
        if not events:  # an unautomated signature plays its Manual value
            return [(0.0, Meter.decode(int(main.setting(Mixer.TIME_SIGNATURE).value)))]
        changes = sorted((float(event.get(Attr.TIME, 0)), int(event.get(Attr.VALUE, 0))) for event in events)
        return [(max(0.0, beat), Meter.decode(value)) for beat, value in changes]

    def bars(self, beat: float) -> float:
        """How many bars the arrangement holds up to ``beat``, each counted in the meter it plays in."""
        meters = self.meters()
        ends = [start for start, _ in meters[1:]] + [math.inf]
        return sum(
            max(0.0, min(beat, end) - start) / meter.beats for (start, meter), end in zip(meters, ends, strict=True)
        )

    def _end(self) -> float:
        """The longest clip or furthest arrangement position, in beats."""
        return max((float(el.get(Attr.VALUE, 0)) for el in self._root.iter(Tag.CURRENT_END)), default=0.0)

    def furthest_bar(self, end: float | None = None) -> int:
        """Max of the longest clip or furthest arrangement position, in whole bars."""
        self._set.furthest_bar = int(self.bars(self._end() if end is None else end))
        return self._set.furthest_bar

    def length(self) -> SetLength:
        """Estimated length from bpm and the furthest position."""
        bpm, end = self.bpm(), self._end()
        return SetLength(bars=self.furthest_bar(end), bpm=bpm, seconds=end / bpm * 60)
