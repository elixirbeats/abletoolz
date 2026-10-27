"""xfadelooper, an old VST2 looper that plays one sample from disk.

Its state is a flat C struct rather than a serialized document. Every state
seen is exactly 652 bytes and lays out like this::

    offset  size  contents
    0       8     magic, ``lsxeslfx``
    8       4     layout version, ``00 02 03 00``
    12      32    preset name, NUL-terminated, NUL-padded
    44      256   sample path, NUL-terminated, NUL-padded
    300     352   88 little-endian float parameters

The path field is a fixed-width char array written into with a plain string
copy, so real states carry the tail of whatever longer path used to live there
after the terminator. That residue is dead to the plugin, which reads up to the
first NUL, and rewriting the whole field clears it.

Fixed width is what makes writing safe here: the buffer never changes length, so
no enclosing structure can go stale, and every parameter keeps its offset. The
one thing a writer must refuse is a replacement too long for the field, which
would otherwise be silently truncated into a path pointing nowhere.

The plugin ships under two names -- ``xfadelooper`` and ``xfadelooper.64`` --
and both store UniqueId 1163098214, so they are one plugin.
"""

from __future__ import annotations

import dataclasses
import pathlib

from abletoolz.plugins.identity.ids import Vst2Id
from abletoolz.plugins.state.catalog import register
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.framings import VERBATIM
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.plugin_map import AUDIO_SUFFIXES, FileRef, Layout, PluginFormat, PluginMap

MAGIC = b"lsxeslfx"
"""First eight bytes of every xfadelooper state."""

LAYOUT_VERSION = b"\x00\x02\x03\x00"
"""Version dword the measured layout belongs to. Anything else is a stranger."""

PATH_OFFSET = 44
"""Where the sample path field starts."""

PATH_FIELD_SIZE = 256
"""How wide that field is. The parameter block starts immediately after it."""

PATH_END = PATH_OFFSET + PATH_FIELD_SIZE

# The path is a Windows char array. latin-1 maps every byte to a character and
# back again, so a read never loses information and a write round-trips exactly.
PATH_ENCODING = "latin-1"


def _measured_layout(patch: bytes) -> bool:
    return len(patch) >= PATH_END and patch[: len(MAGIC)] == MAGIC and patch[8:12] == LAYOUT_VERSION


@dataclasses.dataclass(frozen=True, slots=True)
class XfadeLooperPaths:
    """The one sample path an xfadelooper state holds."""

    rewrites: bool = True
    when_empty: str | None = None
    suffixes: frozenset[str] = AUDIO_SUFFIXES

    def read(self, patch: bytes) -> tuple[FileRef, ...]:
        if not _measured_layout(patch):
            return ()
        text = patch[PATH_OFFSET:PATH_END].split(b"\x00", 1)[0].decode(PATH_ENCODING)
        return (FileRef(text),) if text else ()

    def rewrite(self, patch: bytes, ref: FileRef, path: pathlib.Path) -> bytes:
        if not _measured_layout(patch):
            raise StateTransformError("This xfadelooper state is not the measured layout")
        try:
            encoded = str(path).encode(PATH_ENCODING)
        except UnicodeEncodeError as error:
            raise StateTransformError(f"{path} is not representable in xfadelooper's path field") from error
        # One byte has to be left for the terminator.
        if len(encoded) >= PATH_FIELD_SIZE:
            raise StateTransformError(f"{path} is too long for xfadelooper's {PATH_FIELD_SIZE} byte path field")
        return patch[:PATH_OFFSET] + encoded.ljust(PATH_FIELD_SIZE, b"\x00") + patch[PATH_END:]


@register
def xfadelooper() -> PluginMap:
    return PluginMap(
        id="xfadelooper",
        name="xfadelooper",
        version=None,
        scope="VST2 state layout 00 02 03 00, read and rewritten in place",
        vst2=PluginFormat(
            Vst2Id(1163098214),
            "xfadelooper",
            VERBATIM,
            MeasuredState(StateRung.VERBATIM, (StateEvidence.STRUCTURAL,)),
        ),
        layouts=(Layout("layout 00 02 03 00", _measured_layout),),
        file_paths=XfadeLooperPaths(),
    )
