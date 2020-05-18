from abletoolz import G, B, C, M
from abletoolz import get_element


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
        self.name = get_element(track_root, 'Name.UserName', attribute='Value')
        self.id = track_root.get('Id')
        self.group_id = get_element(track_root, 'TrackGroupId', attribute='Value')

        # Guessing 'Sesstion' was a typo early on and got stuck to not break backwards compatibility
        self.width = get_element(track_root, 'DeviceChain.Mixer.ViewStateSesstionTrackWidth', attribute='Value')
        # Lane height in arrangement view will be automation lane 0
        self.height = get_element(track_root, 'DeviceChain.AutomationLanes.AutomationLanes.AutomationLane.LaneHeight',
                                  attribute='Value')
        self.color = get_element(track_root, 'ColorIndex', attribute='Value')
        self.unfolded = get_element(track_root, 'TrackUnfolded', attribute='Value', silent_error=True)  # Ableton 10
        if not self.unfolded:
            folded = get_element(track_root, 'DeviceChain.Mixer.IsFolded', attribute='Value')  # Ableton 9/8
            self.unfolded = 'false' if folded == 'true' else 'true'

    def __str__(self):
        return (f'{B}Track type {self.type:>12}, {G}Name {self.name:>50}, {C}Id {self.id:>4}, '
                f'Group id {self.group_id:>4}, {M}Color {self.color:>3}, Width {self.width:>3}, Height {self.height:>3}, '
                f'Unfolded: {self.unfolded}')
