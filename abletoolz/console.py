"""Colorized rendering of domain results. The only place ANSI colors meet data."""

from __future__ import annotations

import logging

from abletoolz.live_set.plugins import PluginKind, PluginRef
from abletoolz.live_set.sample_ref import SampleRef
from abletoolz.live_set.tracks import AbletonTrack
from abletoolz.live_set.transport import SetLength
from abletoolz.misc import CB, B, C, G, M, R, Y

logger = logging.getLogger(__name__)


def render_tracks(tracks: list[AbletonTrack]) -> None:
    """One colorized summary line per track."""
    lines = [
        f"{B}Track type {track.type:>12}, {G}Name {track.name:>50}, {C}Id {track.id:>4}, "
        f"Group id {track.group_id:>4}, {M}Color {track.color:>3}, Width {track.width:>3}, "
        f"Height {track.height:>3}, Unfolded: {track.unfolded}"
        for track in tracks
    ]
    logger.info("Tracks:\n%s", "\n".join(lines))


def render_length(length: SetLength) -> None:
    logger.info(
        "%sLongest clip or furthest arrangement position: %s bars. %sEstimated length(Only valid for 4/4): %s",
        M,
        length.bars,
        C,
        length,
    )


def render_missing_samples(missing: list[SampleRef]) -> None:
    for ref in missing:
        logger.warning(
            "%sSample %s missing: \n\tAbsolute[%s], Relative [%s]",
            R,
            ref.name,
            ref.absolute,
            ref.relative,
        )
    color = G if not missing else Y
    logger.info("%sMissing sample references: %s%s", color, M, len(missing))


def render_plugins(refs: list[PluginRef]) -> None:
    for ref in refs:
        color = G if ref.exists else (Y if ref.alternative else R)
        name = ref.name if ref.name is not None else "<unknown>"
        if ref.kind == PluginKind.AU and ref.manufacturer:
            name = f"{ref.manufacturer}: {name}"
        logger.info(
            "%s[%s%s%s] Plugin: %s, %sPath: %s, %sExists: %s",
            color,
            C,
            ref.track_location,
            color,
            name,
            M,
            ref.path,
            color,
            ref.exists,
        )
        if ref.alternative:
            logger.info(
                "%s\tPotential alternative path for %s found: %s%s",
                CB,
                ref.name,
                M,
                ref.alternative,
            )
