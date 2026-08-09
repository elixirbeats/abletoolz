"""Track operations: listing, view state, fold state, colors, routing."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from abletoolz import color_tools, schema
from abletoolz.misc import RST, STEREO_OUTPUTS, G, R, get_element
from abletoolz.versioning import Version

if TYPE_CHECKING:
    from abletoolz.live_set.document import AbletonSet

logger = logging.getLogger(__name__)


class AbletonTrack:
    """Single track object."""

    def __init__(self, track_root: ET.Element, version: Version) -> None:
        """Parse one track element of a set saved by ``version``."""
        self.track_root = track_root
        self.type = track_root.tag
        self.name = get_element(track_root, "Name.UserName", attribute="Value")
        if not self.name:
            self.name = get_element(track_root, "Name.EffectiveName", attribute="Value")
        self.id = track_root.get("Id")
        self.group_id = get_element(track_root, "TrackGroupId", attribute="Value")
        self.width = get_element(
            track_root,
            f"DeviceChain.Mixer.{schema.tag('track_width', version)}",
            attribute="Value",
        )
        # Lane height in arrangement view will be automation lane 0
        self.height = get_element(
            track_root,
            "DeviceChain.AutomationLanes.AutomationLanes.AutomationLane.LaneHeight",
            attribute="Value",
        )
        self.color_element = schema.tag("color", version)
        self.unfolded = get_element(track_root, "TrackUnfolded", attribute="Value", silent_error=True)  # Ableton 10
        if not self.unfolded:
            folded = get_element(track_root, "DeviceChain.Mixer.IsFolded", attribute="Value")  # Ableton 9/8
            self.unfolded = "false" if folded == "true" else "true"

    def __str__(self) -> str:
        """Plain track summary; colorized rendering lives in console."""
        return (
            f"Track type {self.type:>12}, Name {self.name:>50}, Id {self.id:>4}, "
            f"Group id {self.group_id:>4}, Color {self.color:>3}, Width {self.width:>3}, "
            f"Height {self.height:>3}, Unfolded: {self.unfolded}"
        )

    @property
    def color(self) -> int:
        """Return color index, -1 when the element is missing."""
        if (clr := self.track_root.find(self.color_element)) is not None:
            return int(clr.get("Value", 0))
        return -1

    @color.setter
    def color(self, value: int) -> None:
        """Set color for track."""
        if not 0 <= value <= 69:
            raise ValueError("Color index must be within 0 - 69")
        if (clr_element := self.track_root.find(self.color_element)) is not None:
            clr_element.set("Value", str(value))

    def clips_clipview(self) -> Iterator[ET.Element]:
        """Iterate through all the clips in the clip view."""
        yield from self.track_root.iter("ClipSlot")

    def clips_arrangement(self) -> Iterator[ET.Element]:
        """Iterate through all the clips in the arangement view."""
        if self.type == "MidiTrack":
            tree_element = "ClipTimeable"
            clip_element = "MidiClip"
        elif self.type == "AudioTrack":
            tree_element = "Sample"
            clip_element = "AudioClip"
        else:
            return
        clip_timeample = self.track_root.find(f".//{tree_element}")
        if clip_timeample is None:
            return
        yield from clip_timeample.iter(clip_element)

    def clip_clipview_colors(self) -> Iterator[ET.Element]:
        """Yield clipview color elements from current track."""
        for clip in self.clips_clipview():
            if (color_element := clip.find(f".//{self.color_element}")) is not None:
                yield color_element

    def clip_arangement_colors(self) -> Iterator[ET.Element]:
        """Yield arrangement color elements from current track."""
        for clip in self.clips_arrangement():
            if (color_element := clip.find(f".//{self.color_element}")) is not None:
                yield color_element


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
        """Parse all tracks, cached."""
        if self._loaded is None:
            tracks_element = get_element(self._root, "LiveSet.Tracks", silent_error=False)
            self._loaded = [AbletonTrack(track, self.version) for track in tracks_element]
        return self._loaded

    def set_heights(self, height: int) -> int:
        """Set every arrangement lane height; returns elements changed."""
        height = min(425, (max(17, height)))  # Clamp to valid range.
        elements = list(self._root.iter("LaneHeight"))
        for el in elements:
            el.set("Value", str(height))
        logger.info("%sSet track heights to %s.", G, height)
        return len(elements)

    def set_widths(self, width: int) -> int:
        """Set every clip-view track width; returns elements changed."""
        width = min(264, (max(17, width)))  # Clamp to valid range.
        elements = list(self._root.iter(schema.tag("track_width", self.version)))
        for el in elements:
            el.set("Value", str(width))
        logger.info("%sSet track widths to %s.", G, width)
        return len(elements)

    def _group_fold_elements(self) -> list[ET.Element]:
        """Group-track collapse flags. Distinct from TrackUnfolded: IsFolded on the
        group's own mixer is what collapses the group, and it reads inverted."""
        return [
            is_folded
            for group in self._root.findall("LiveSet/Tracks/GroupTrack")
            if (is_folded := group.find("DeviceChain/Mixer/IsFolded")) is not None
        ]

    def fold(self) -> int:
        """Fold all tracks and collapse group tracks; returns elements changed."""
        elements = list(self._root.iter("TrackUnfolded"))
        for el in elements:
            el.set("Value", "false")
        group_folds = self._group_fold_elements()
        for el in group_folds:
            el.set("Value", "true")
        logger.info("%sFolded all tracks.", G)
        return len(elements) + len(group_folds)

    def unfold(self) -> int:
        """Unfold all tracks and expand group tracks; returns elements changed."""
        elements = list(self._root.iter("TrackUnfolded"))
        for el in elements:
            el.set("Value", "true")
        group_folds = self._group_fold_elements()
        for el in group_folds:
            el.set("Value", "false")
        logger.info("%sUnfolded all tracks.", G)
        return len(elements) + len(group_folds)

    def set_audio_output(self, output_number: int, element_string: str) -> None:
        """Route the master or cue track to a stereo output pair."""
        if output_number not in STEREO_OUTPUTS:
            raise ValueError(f"{R}Output number invalid!. Available options: \n{STEREO_OUTPUTS}{RST}")
        if element_string == "MasterTrack":
            element_string = schema.tag("master_track", self.version)
        # Some sets carry no cue track at all (seen in real 10.1 sets); nothing to route.
        if self._root.find(f"LiveSet/{element_string}") is None:
            logger.warning("%sSet has no %s, skipping audio output routing.", R, element_string)
            return
        output_obj = STEREO_OUTPUTS[output_number]
        out_target_element = get_element(
            self._root,
            f"LiveSet.{element_string}.DeviceChain.AudioOutputRouting.Target",
            silent_error=True,
        )
        if not isinstance(out_target_element, ET.Element):
            out_target_element = get_element(  # ableton 8 sets use "MasterChain" for master track.
                self._root,
                f"LiveSet.{element_string}.MasterChain.AudioOutputRouting.Target",
            )
            lower_display_string_element = get_element(
                self._root,
                f"LiveSet.{element_string}.MasterChain.AudioOutputRouting.LowerDisplayString",
            )
        else:
            lower_display_string_element = get_element(
                self._root,
                f"LiveSet.{element_string}.DeviceChain.AudioOutputRouting.LowerDisplayString",
            )
        out_target_element.set("Value", output_obj["target"])
        lower_display_string_element.set("Value", output_obj["lower_display_string"])
        logger.info("%sSet %s to %s", G, element_string, output_obj["lower_display_string"])

    def gradient(self) -> None:
        """Make a rough gradient across tracks and their clips using built in colors."""
        tracks = self.load()
        for clr_ind, track in zip(color_tools.create_gradient_ableton(len(tracks)), tracks, strict=False):
            track.color = clr_ind

            clipview_clr_elements = list(track.clip_clipview_colors())
            clip_view_gradient = color_tools.create_gradient_ableton(len(clipview_clr_elements), starting_index=clr_ind)
            for sub_ind, clip_clr_ele in zip(clip_view_gradient, clipview_clr_elements, strict=False):
                clip_clr_ele.set("Value", str(sub_ind))

            arangement_clr_elements = list(track.clip_arangement_colors())
            clip_view_gradient = color_tools.create_gradient_ableton(
                len(arangement_clr_elements), starting_index=clr_ind
            )
            for sub_ind, clip_clr_ele in zip(clip_view_gradient, arangement_clr_elements, strict=False):
                clip_clr_ele.set("Value", str(sub_ind))
