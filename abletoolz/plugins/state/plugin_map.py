"""Plugin/version knowledge composed from codecs and format-specific maps."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from abletoolz.plugins.state import StateTransformError
from abletoolz.plugins.state.controls import ParameterCurves, ParameterMap
from abletoolz.plugins.state.model import ByteMap, EncodableState


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise StateTransformError(f"{label} must be explicit")


@dataclass(frozen=True, slots=True)
class StateFormat[State: EncodableState]:
    """One codec and its measured layouts and parameter knowledge.

    Byte maps can describe nested records rather than the complete codec input.
    Their scopes and vendor models establish those record boundaries.
    Providers must keep referenced map identities and fields stable; freezing
    this descriptor does not freeze a custom map or decoder's internal state.
    """

    id: str
    decoder: Callable[[bytes], State] | None
    scope: str
    byte_maps: tuple[ByteMap, ...] = ()
    parameter_maps: tuple[ParameterMap, ...] = ()
    parameter_curves: tuple[ParameterCurves, ...] = ()

    def __post_init__(self) -> None:
        _text(self.id, "State-format ID")
        _text(self.scope, "State-format scope")
        if self.decoder is not None and not callable(self.decoder):
            raise StateTransformError("State format requires a decoder")
        if any(type(items) is not tuple for items in (self.byte_maps, self.parameter_maps, self.parameter_curves)):
            raise StateTransformError("State-format maps must be immutable tuples")
        if len({layout.format_id for layout in self.byte_maps}) != len(self.byte_maps):
            raise StateTransformError("Duplicate byte-map ID in state format")
        for layout in self.byte_maps:
            _text(layout.format_id, "Byte-map ID")
        parameter_layouts = tuple(parameters.byte_map for parameters in self.parameter_maps) + tuple(
            curves.byte_map for curves in self.parameter_curves
        )
        for parameter_layout in parameter_layouts:
            if not any(parameter_layout is layout for layout in self.byte_maps):
                raise StateTransformError("Parameter knowledge must reference a byte map owned by this state format")

    def byte_map(self, format_id: str) -> ByteMap:
        for layout in self.byte_maps:
            if layout.format_id == format_id:
                return layout
        raise StateTransformError(f"Unknown byte map {format_id!r} in {self.id}")

    def decode(self, raw: bytes) -> State:
        if type(raw) is not bytes:
            raise StateTransformError("State-format decoding requires immutable bytes")
        if self.decoder is None:
            raise StateTransformError(f"No codec is available for state format {self.id!r}")
        return self.decoder(raw)


@dataclass(frozen=True, slots=True)
class PluginMap[State: EncodableState]:
    """Knowledge about one plugin/version, independent of converters and hosts.

    A missing version means the product release is unknown; formats and scope
    still bound the measurements. Format selection is always explicit.
    Fingerprints describe the evidence; callers enforce binary compatibility.
    """

    id: str
    name: str
    version: str | None
    scope: str
    formats: tuple[StateFormat[State], ...]
    binary_sha256: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.id, "Plugin-map ID")
        _text(self.name, "Plugin name")
        _text(self.scope, "Plugin-map scope")
        if self.version is not None:
            _text(self.version, "Plugin version")
        if type(self.formats) is not tuple or not self.formats:
            raise StateTransformError("Plugin map requires a nonempty immutable tuple of state formats")
        if len({format.id for format in self.formats}) != len(self.formats):
            raise StateTransformError("Duplicate state-format ID in plugin map")
        if type(self.binary_sha256) is not tuple or len(set(self.binary_sha256)) != len(self.binary_sha256):
            raise StateTransformError("Plugin-map binary hashes must be unique and immutable")
        for digest in self.binary_sha256:
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise StateTransformError("Plugin-map binary identity requires lowercase SHA256")

    def format(self, format_id: str) -> StateFormat[State]:
        for format in self.formats:
            if format.id == format_id:
                return format
        raise StateTransformError(f"Unknown state format {format_id!r} in {self.id}")

    def decode(self, format_id: str, raw: bytes) -> State:
        return self.format(format_id).decode(raw)
