from abletoolz.common import G, B, C
from abletoolz.common import get_element


class AbletonTrack(object):
    """Single track object."""
    name = None
    id = None  # Internal track number.
    group_id = None  # group id is -1 when track isn't grouped.
    type = None  # Group, MidiTrack, AudioTrack, ReturnTrack
    unfolded = None
    width = None  # In Clip view.
    height = None  # In Arrangement view.
    color = None

    def __init__(self, track_root):
        self.track_root = track_root
        self.type = track_root.tag
        self.name = get_element(track_root, 'Name.EffectiveName', attribute='Value')
        self.id = track_root.get('Id')
        self.group_id = get_element(track_root, 'TrackGroupId', attribute='Value')

        # Guessing 'Sesstion' was a typo early on and got stuck to not break backwards compatibility
        self.width = get_element(track_root, 'DeviceChain.Mixer.ViewStateSesstionTrackWidth', attribute='Value')
        # Lane height in arrangement view will be automation lane 0
        self.height = get_element(track_root, 'DeviceChain.AutomationLanes.AutomationLanes.AutomationLane.LaneHeight',
                                  attribute='Value')
        self.color = get_element(track_root, 'ColorIndex', attribute='Value')
        self.unfolded = get_element(track_root, 'TrackUnfolded', attribute='Value')

    def __str__(self):
        return (f'{B}Track type {self.type:>12}, {G}Name {self.name:>50}, {C} Group id {self.group_id:>4}, '
                f'Id {self.id:>4}, Color {self.color}, Width {self.width}, Height {self.height}, '
                f'Unfolded: {self.unfolded}')
