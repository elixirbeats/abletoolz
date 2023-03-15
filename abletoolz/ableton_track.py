"""Ableton track parser."""
import logging
from typing import Tuple
from xml.etree import ElementTree

from abletoolz.misc import B, C, G, M, get_element

logger = logging.getLogger(__name__)


class AbletonTrack(object):
    """Single track object."""

    # TODO: make private vars
    name = None
    id = None  # Internal track number.
    group_id = None  # group id is -1 when track isn't grouped.
    type = None  # Group, MidiTrack, AudioTrack, ReturnTrack
    unfolded = None
    width = None  # In Clip view.
    height = None  # In Arrangement view.
    _color = None

    def __init__(self, track_root: ElementTree.Element, version: Tuple[int, int, int]) -> None:
        """Construct AbletonTrack."""
        self.track_root = track_root
        self.type = track_root.tag
        self.name = get_element(track_root, "Name.UserName", attribute="Value")
        if not self.name:
            self.name = get_element(track_root, "Name.EffectiveName", attribute="Value")
        self.id = track_root.get("Id")
        self.group_id = get_element(track_root, "TrackGroupId", attribute="Value")

        # Guessing 'Sesstion' was a typo early on and got stuck to not break backwards compatibility
        self.width = get_element(
            track_root,
            "DeviceChain.Mixer.ViewStateSesstionTrackWidth",
            attribute="Value",
        )
        # Lane height in arrangement view will be automation lane 0
        self.height = get_element(
            track_root,
            "DeviceChain.AutomationLanes.AutomationLanes.AutomationLane.LaneHeight",
            attribute="Value",
        )

        # Ableton 11 changes.
        self.color_element = "Color" if version > (11, 0, 0) else "ColorIndex"

        self.unfolded = get_element(track_root, "TrackUnfolded", attribute="Value", silent_error=True)  # Ableton 10
        if not self.unfolded:
            folded = get_element(track_root, "DeviceChain.Mixer.IsFolded", attribute="Value")  # Ableton 9/8
            self.unfolded = "false" if folded == "true" else "true"

    def __str__(self) -> str:
        """Create string representation of parsed track."""
        return (
            f"{B}Track type {self.type:>12}, {G}Name {self.name:>50}, {C}Id {self.id:>4}, "
            f"Group id {self.group_id:>4}, {M}Color {self.color:>3}, Width {self.width:>3}, "
            f"Height {self.height:>3}, Unfolded: {self.unfolded}"
        )

    @property
    def color(self) -> int:
        """Return color.

        Returns:
            -1 on failure to get element value, color index otherwise.
        """
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
