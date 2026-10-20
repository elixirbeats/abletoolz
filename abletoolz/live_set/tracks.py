"""Track operations: listing, view state, fold state, colors, routing."""

from __future__ import annotations

import enum
import logging
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from abletoolz.ansi import RST, G, R
from abletoolz.elements import get_element
from abletoolz.errors import ElementNotFound
from abletoolz.live_set import colors, sections
from abletoolz.live_set.settings import Setting, every, find, resolve
from abletoolz.schema import Attr, Tag, path
from abletoolz.schema.objects import OWNER_SPELLINGS, Clip, Mixer, Track
from abletoolz.schema.versioning import Version

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

logger = logging.getLogger(__name__)

# The main track's element, newest name first.
MAIN_TRACK_TAGS: tuple[str, ...] = (
    Tag.MAIN_TRACK,
    *(old for old, new in OWNER_SPELLINGS.items() if new == Tag.MAIN_TRACK),
)

# fmt: off
STEREO_OUTPUTS: dict[int, dict[str, str]] = {
    1: {"target": "AudioOut/External/S0",  "lower_display_string": "1/2"},
    2: {"target": "AudioOut/External/S1", "lower_display_string": "3/4"},
    3: {"target": "AudioOut/External/S2", "lower_display_string": "5/6"},
    4: {"target": "AudioOut/External/S3", "lower_display_string": "7/8"},
    5: {"target": "AudioOut/External/S4", "lower_display_string": "9/10"},
    6: {"target": "AudioOut/External/S5", "lower_display_string": "11/12"},
    7: {"target": "AudioOut/External/S6", "lower_display_string": "13/14"},
    8: {"target": "AudioOut/External/S7", "lower_display_string": "15/16"},
    9: {"target": "AudioOut/External/S8", "lower_display_string": "17/18"},
    10: {"target": "AudioOut/External/S9","lower_display_string": "19/20"},
}
# fmt: on


class AbletonTrack:
    """Single track object: a track in the set, the main track or the cue track."""

    def __init__(self, track_root: ET.Element, version: Version) -> None:
        """Parse one track element of a set saved by ``version``."""
        self.track_root = track_root
        self.type = track_root.tag
        self.version = version
        self.name = self.setting(Track.USER_NAME).raw or self.setting(Track.EFFECTIVE_NAME).raw
        self.id = track_root.get(Attr.ID)
        # Some group and return tracks leave out three things Live writes on
        # every track, and each one used to take the whole set down. Measured
        # over 811 sets: 44 return tracks have no TrackGroupId (they belong to
        # no group) and no automation lane, and 44 group tracks have no
        # automation lane and no session width. All 88 sit in the 22 sets a
        # third-party generator wrote, and no Live-written set in
        # the library omits any of them -- but absent is unset, not malformed,
        # whoever wrote the file.
        self.group_id = self._raw(Track.TRACK_GROUP_ID)
        self.width = self._raw(Mixer.VIEW_STATE_SESSION_TRACK_WIDTH)
        # Lane height in arrangement view is the first automation lane's.
        lanes = self.every(Track.LANE_HEIGHT)
        self.height = lanes[0].raw if lanes else None
        self.unfolded = self._raw(Track.TRACK_UNFOLDED)
        if not self.unfolded:
            # Live 9 and 8 kept the inverse on the mixer, which is a change of
            # meaning rather than of spelling, so it is read here and not
            # resolved as one setting. A track with neither has no fold state to
            # read -- the same 44 return tracks, which have no automation lanes
            # to fold in the first place -- and an unfoldable track is unfolded.
            self.unfolded = "false" if self._raw(Mixer.IS_FOLDED) == "true" else "true"

    def __str__(self) -> str:
        """Plain track summary; colorized rendering lives in console."""
        return (
            f"Track type {self.type:>12}, Name {self.name:>50}, Id {self.id:>4}, "
            f"Group id {self.group_id or '-':>4}, Color {self.color:>3}, Width {self.width or '-':>3}, "
            f"Height {self.height or '-':>3}, Unfolded: {self.unfolded}"
        )

    def _owner(self, member: enum.StrEnum) -> ET.Element | None:
        """The element ``member`` is relative to: the track, or its mixer."""
        if isinstance(member, Track):
            return self.track_root
        if isinstance(member, Mixer):
            return self.track_root.find(path(Tag.DEVICE_CHAIN, Tag.MIXER))
        raise TypeError(f"{type(member).__name__}.{member.name} is not a Track or Mixer setting")

    def setting(self, member: enum.StrEnum, **indices: int) -> Setting:
        """A setting of this track or of its mixer: ``setting(Track.COLOR)``, ``setting(Mixer.VOLUME)``.

        One in a list (an automation lane, a send) takes its position:
        ``setting(Mixer.SEND, index=0)``.
        """
        owner = self._owner(member)
        if owner is None:
            raise ElementNotFound(f"{self.type} {self.id} has no mixer")
        return resolve(owner, member, self.version, indices, owner_tag=self.type, label=self.type)

    def find_setting(self, member: enum.StrEnum, **indices: int) -> Setting | None:
        """:meth:`setting`, or None where this track has no such setting."""
        owner = self._owner(member)
        if owner is None:
            return None
        return find(owner, member, self.version, indices, owner_tag=self.type, label=self.type)

    def every(self, member: enum.StrEnum) -> list[Setting]:
        """``member`` at every position of its list: every automation lane, every send."""
        owner = self._owner(member)
        return [] if owner is None else every(owner, member, self.version, owner_tag=self.type)

    def _raw(self, member: enum.StrEnum) -> str | None:
        setting = self.find_setting(member)
        return None if setting is None else setting.raw

    @property
    def color(self) -> int:
        """Return color index, -1 when the track has none."""
        color = self._raw(Track.COLOR)
        return -1 if color is None else int(color)

    @color.setter
    def color(self, value: int) -> None:
        """Set color for track."""
        if not 0 <= value <= 69:
            raise ValueError("Color index must be within 0 - 69")
        if (setting := self.find_setting(Track.COLOR)) is not None:
            setting.set(value)

    def clips_clipview(self) -> Iterator[ET.Element]:
        """Iterate through all the clips in the clip view."""
        yield from self.track_root.iter(Tag.CLIP_SLOT)

    def clips_arrangement(self) -> Iterator[ET.Element]:
        """Iterate through all the clips in the arrangement view.

        The same list the section writers edit. This used to search the track
        for a ``ClipTimeable``/``Sample`` tag anywhere and then recurse into
        it, which also reached clips inside racks and take lanes -- clips the
        writers refuse to touch, reported to callers as if they were placeable.
        """
        events = sections.arrangement_events(self.track_root, self.type)
        if events is None:
            return
        yield from events.clips()

    def _session_clips(self) -> Iterator[ET.Element]:
        for slot in sections.session_slots(self.track_root):
            value = slot.find(path(Tag.CLIP_SLOT, Tag.VALUE))
            if value is not None:
                yield from value

    def _clip_colors(self, clips: Iterable[ET.Element]) -> Iterator[Setting]:
        for clip in clips:
            if (color := find(clip, Clip.COLOR, self.version)) is not None:
                yield color

    def clip_clipview_colors(self) -> Iterator[Setting]:
        """The color of every session clip on this track."""
        return self._clip_colors(self._session_clips())

    def clip_arangement_colors(self) -> Iterator[Setting]:
        """The color of every arrangement clip on this track."""
        return self._clip_colors(self.clips_arrangement())


class Tracks:
    """Track-level operations of one set."""

    def __init__(self, live_set: AbletonSet) -> None:
        self._set = live_set
        self._loaded: list[AbletonTrack] | None = None

    @property
    def version(self) -> Version:
        return self._set.version_tuple

    @property
    def _root(self) -> ET.Element:
        return self._set.root

    def load(self) -> list[AbletonTrack]:
        """Parse all tracks, cached: the set's own, returns included, main and cue left out."""
        if self._loaded is None:
            tracks_element = get_element(self._root, path(Tag.LIVE_SET, Tag.TRACKS), silent_error=False)
            self._loaded = [AbletonTrack(track, self.version) for track in tracks_element]
        return self._loaded

    def main(self) -> AbletonTrack:
        """The main track, ``MainTrack`` or, before Live 12, ``MasterTrack``."""
        for tag in MAIN_TRACK_TAGS:
            if (element := self._root.find(path(Tag.LIVE_SET, tag))) is not None:
                return AbletonTrack(element, self.version)
        raise ElementNotFound("the set has no main track")

    def cue(self) -> AbletonTrack | None:
        """The cue (pre-hear) track; some sets carry none."""
        element = self._root.find(path(Tag.LIVE_SET, Tag.PRE_HEAR_TRACK))
        return None if element is None else AbletonTrack(element, self.version)

    def walk(self, *, cue: bool = False) -> list[AbletonTrack]:
        """Every track, then the main track, then the cue track when ``cue`` asks for it."""
        tracks = [*self.load(), self.main()]
        if cue and (cue_track := self.cue()) is not None:
            tracks.append(cue_track)
        return tracks

    def set_heights(self, height: int) -> int:
        """Set every arrangement lane height; returns elements changed."""
        height = min(425, (max(17, height)))  # Clamp to valid range.
        lanes = [lane for track in self.walk(cue=True) for lane in track.every(Track.LANE_HEIGHT)]
        for lane in lanes:
            lane.set(height)
        logger.info("%sSet track heights to %s.", G, height)
        return len(lanes)

    def set_widths(self, width: int) -> int:
        """Set every clip-view track width; returns elements changed."""
        width = min(264, (max(17, width)))  # Clamp to valid range.
        widths = [
            setting
            for track in self.walk(cue=True)
            if (setting := track.find_setting(Mixer.VIEW_STATE_SESSION_TRACK_WIDTH)) is not None
        ]
        for setting in widths:
            setting.set(width)
        logger.info("%sSet track widths to %s.", G, width)
        return len(widths)

    def _fold_settings(self) -> tuple[list[Setting], list[Setting]]:
        """Every track's ``TrackUnfolded``, and every group's collapse flag.

        A group collapses through ``IsFolded`` on its own mixer, which reads
        inverted and is distinct from ``TrackUnfolded``.
        """
        unfolded = [
            setting
            for track in self.walk(cue=True)
            if (setting := track.find_setting(Track.TRACK_UNFOLDED)) is not None
        ]
        group_folds = [
            setting
            for track in self.load()
            if track.type == Tag.GROUP_TRACK and (setting := track.find_setting(Mixer.IS_FOLDED)) is not None
        ]
        return unfolded, group_folds

    def fold(self) -> int:
        """Fold all tracks and collapse group tracks; returns elements changed."""
        unfolded, group_folds = self._fold_settings()
        for setting in unfolded:
            setting.set(False)
        for setting in group_folds:
            setting.set(True)
        logger.info("%sFolded all tracks.", G)
        return len(unfolded) + len(group_folds)

    def unfold(self) -> int:
        """Unfold all tracks and expand group tracks; returns elements changed."""
        unfolded, group_folds = self._fold_settings()
        for setting in unfolded:
            setting.set(True)
        for setting in group_folds:
            setting.set(False)
        logger.info("%sUnfolded all tracks.", G)
        return len(unfolded) + len(group_folds)

    def set_audio_output(self, output_number: int, element_string: str) -> None:
        """Route the main or cue track to a stereo output pair."""
        if output_number not in STEREO_OUTPUTS:
            raise ValueError(f"{R}Output number invalid!. Available options: \n{STEREO_OUTPUTS}{RST}")
        track = self.main() if element_string in MAIN_TRACK_TAGS else self.cue()
        # Some sets carry no cue track at all (seen in real 10.1 sets); nothing to route.
        if track is None:
            logger.warning("%sSet has no %s, skipping audio output routing.", R, element_string)
            return
        output_obj = STEREO_OUTPUTS[output_number]
        target = track.find_setting(Track.AUDIO_OUTPUT_ROUTING_TARGET)
        label = track.find_setting(Track.AUDIO_OUTPUT_ROUTING_LOWER_DISPLAY_STRING)
        if target is not None and label is not None:
            target.set(output_obj["target"])
            label.set(output_obj["lower_display_string"])
        else:
            # Live 8 kept the main track's routing on MasterChain, older than any
            # set the census read, so it is addressed by hand.
            routing = get_element(track.track_root, path(Tag.MASTER_CHAIN, Tag.AUDIO_OUTPUT_ROUTING))
            get_element(routing, Tag.TARGET).set(Attr.VALUE, output_obj["target"])
            get_element(routing, Tag.LOWER_DISPLAY_STRING).set(Attr.VALUE, output_obj["lower_display_string"])
        logger.info("%sSet %s to %s", G, track.type, output_obj["lower_display_string"])

    def gradient(self) -> None:
        """Make a rough gradient across tracks and their clips using built in colors."""
        tracks = self.load()
        for clr_ind, track in zip(colors.create_gradient_ableton(len(tracks)), tracks, strict=False):
            track.color = clr_ind

            clipview_clr_elements = list(track.clip_clipview_colors())
            clip_view_gradient = colors.create_gradient_ableton(len(clipview_clr_elements), starting_index=clr_ind)
            for sub_ind, clip_color in zip(clip_view_gradient, clipview_clr_elements, strict=False):
                clip_color.set(sub_ind)

            arangement_clr_elements = list(track.clip_arangement_colors())
            clip_view_gradient = colors.create_gradient_ableton(len(arangement_clr_elements), starting_index=clr_ind)
            for sub_ind, clip_color in zip(clip_view_gradient, arangement_clr_elements, strict=False):
                clip_color.set(sub_ind)
