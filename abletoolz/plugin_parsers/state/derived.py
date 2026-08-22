"""A transfer table the derivation rig measured, and the state it writes.

MODEL.md's "The derivation rig". A product whose VST2 exposes no chunk is saved
as a :class:`~abletoolz.plugin_parsers.state.fxbk.LegacyBank` of normalized
floats and its VST3 as a block of floats in the plugin's own units, and nobody
outside the vendor has written down the curve between the two. Rather than
guess one, the rig asks both binaries: which state slot each parameter owns,
and what the plugin writes there across a sweep of that parameter. What comes
back is a table, one JSON file per product in ``abletoolz/data/``.

Three things live here, in that order: the curves a table may name, the table
itself, and the ``FabF`` container the tables measured so far write. The
container sits with the tables rather than with
:mod:`abletoolz.plugin_parsers.state.fabfilter` because a derived table is the
only thing in the library that writes one -- FabFilter's other generation, the
``FFBS`` chunk, is a codec on its own.

Nothing in a table is reasoned out, which is why the shapes are so plain: a
line is the two numbers the plugin wrote at 0 and at 1, a step is the values it
snapped to, and a curve that is neither is the measured points themselves.
"""

from __future__ import annotations

import bisect
import dataclasses
import json
import math
import pathlib
import struct
from collections.abc import Mapping
from typing import cast

from abletoolz.plugin_parsers.state import StateTransformError
from abletoolz.plugin_parsers.state.fxbk import LegacyBank

# Length-prefixed fields run through every FabFilter format: a little-endian
# word, then that many bytes.
_U32 = struct.Struct("<I")


# -- what a plugin does between a knob and its saved state ------------------


@dataclasses.dataclass(frozen=True, slots=True)
class LinearTransfer:
    """``native = intercept + slope * normalized``.

    Covers dB, log2 Hz, ratios and every identity mapping, which between them is
    most of a FabFilter. Both coefficients are what the plugin wrote at 0 and at
    1 rather than a regression, so a state built from them is byte-identical to
    one the plugin would have written.
    """

    intercept: float
    slope: float

    def __call__(self, normalized: float) -> float:
        return self.intercept + self.slope * normalized


@dataclasses.dataclass(frozen=True, slots=True)
class StepTransfer:
    """A lookup over the parameter's own step count, for enums and switches.

    The index rule is round-half-up and not Python's ``round``: a two-way switch
    at exactly 0.5 reads as on, where banker's rounding would call it off.
    """

    values: tuple[float, ...]

    def __call__(self, normalized: float) -> float:
        last = len(self.values) - 1
        return self.values[min(math.floor(min(max(normalized, 0.0), 1.0) * last + 0.5), last)]


@dataclasses.dataclass(frozen=True, slots=True)
class TableTransfer:
    """Piecewise linear through a measured sweep, clamped at both ends.

    What a curve gets when neither of the other two reproduces it. That is the
    honest answer rather than a fallback: it says the shape was measured and not
    recognised.
    """

    positions: tuple[float, ...]
    values: tuple[float, ...]

    def __call__(self, normalized: float) -> float:
        if normalized <= self.positions[0]:
            return self.values[0]
        if normalized >= self.positions[-1]:
            return self.values[-1]
        above = bisect.bisect_right(self.positions, normalized)
        low, high = self.positions[above - 1], self.positions[above]
        span = (normalized - low) / (high - low)
        return self.values[above - 1] + span * (self.values[above] - self.values[above - 1])


# What a derived table may say a parameter's curve is.
type ParameterTransfer = LinearTransfer | StepTransfer | TableTransfer


def _read_transfer(payload: Mapping[str, object]) -> ParameterTransfer:
    """One transfer model off disk, refusing a kind nothing here evaluates."""
    kind = payload["kind"]
    if kind == "linear":
        intercept, slope = cast(float, payload["intercept"]), cast(float, payload["slope"])
        return LinearTransfer(intercept=float(intercept), slope=float(slope))
    if kind == "step":
        return StepTransfer(values=tuple(float(value) for value in cast(list[float], payload["values"])))
    if kind == "table":
        return TableTransfer(
            positions=tuple(float(value) for value in cast(list[float], payload["positions"])),
            values=tuple(float(value) for value in cast(list[float], payload["values"])),
        )
    raise StateTransformError(f"{kind!r} is not a transfer model this module evaluates")


# -- the container a derived table writes -----------------------------------

FABF_MAGIC = b"FabF"


@dataclasses.dataclass(frozen=True, slots=True)
class FabfState:
    """The state the FabF-generation products keep, taken apart.

    The other half of the same story as
    :class:`~abletoolz.plugin_parsers.state.fabfilter.FfbsState`. Pro-C 2,
    Pro-L 2, Pro-R, Pro-MB, Pro-DS, Pro-G and Micro expose no VST2 chunk either
    -- so their ``.als`` Buffer is a
    :class:`~abletoolz.plugin_parsers.state.fxbk.LegacyBank` too -- but their
    VST3 writes this: ``FabF``, a version, a length-prefixed preset name, a word
    that is always zero, a parameter count, that many float32 in the plugin's
    own units, and two trailing words that are always one.

    ``leading`` and ``trailing`` are carried rather than understood. They are
    read off the plugin's own default state and written back unchanged, so a
    state built here differs from one the plugin wrote only where a parameter
    genuinely differs.
    """

    version: int
    preset_name: str
    leading: int
    parameters: tuple[float, ...]
    trailing: tuple[int, ...]

    @classmethod
    def parse(cls, payload: bytes) -> FabfState:
        """Read a FabF state, checking the declared count against what is there.

        A count that disagrees with the byte length means this is not the layout
        above, and every number after it would be read out of the wrong place.
        """
        if len(payload) < 20 or payload[:4] != FABF_MAGIC:
            raise StateTransformError(f"a chunk opening {payload[:8].hex()} is not FabF")
        (version,) = _U32.unpack_from(payload, 4)
        (name_length,) = _U32.unpack_from(payload, 8)
        if 12 + name_length + 8 > len(payload):
            raise StateTransformError(f"a name length of {name_length} runs past {len(payload)} bytes")
        cursor = 12 + name_length
        (leading,) = _U32.unpack_from(payload, cursor)
        (count,) = _U32.unpack_from(payload, cursor + 4)
        cursor += 8
        if count * 4 > len(payload) - cursor:
            raise StateTransformError(f"a declared {count} floats overruns the {len(payload) - cursor} bytes left")
        rest = payload[cursor + count * 4 :]
        if len(rest) % 4:
            raise StateTransformError(f"{len(rest)} trailing bytes is not a whole number of words")
        return cls(
            version=version,
            preset_name=payload[12 : 12 + name_length].decode("latin1"),
            leading=leading,
            parameters=struct.unpack_from(f"<{count}f", payload, cursor),
            trailing=struct.unpack(f"<{len(rest) // 4}I", rest),
        )

    def encode(self) -> bytes:
        """The bytes an ``.als`` holds as this device's ``ProcessorState``."""
        name = self.preset_name.encode("latin1")
        return b"".join(
            (
                FABF_MAGIC,
                _U32.pack(self.version),
                _U32.pack(len(name)),
                name,
                _U32.pack(self.leading),
                _U32.pack(len(self.parameters)),
                struct.pack(f"<{len(self.parameters)}f", *self.parameters),
                struct.pack(f"<{len(self.trailing)}I", *self.trailing),
            )
        )


# -- a table derived by asking both plugins ---------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class DerivedParameter:
    """One parameter's route from the VST2 bank into the VST3 state."""

    name: str
    bank_index: int
    slot: int
    transfer: ParameterTransfer


@dataclasses.dataclass(frozen=True, slots=True)
class DerivedTable:
    """How one FabF-generation product's bank becomes its VST3 state.

    Nothing in here was reasoned out. The derivation rig loaded the product's
    VST2 in one host and its VST3 in another, proved the bank's float order is
    the VST2 parameter order, moved each VST3 parameter to find which state slot
    it owns, swept it to fit its curve, and joined the two sides by parameter
    name. See MODEL.md, "The derivation rig".
    """

    product: str
    state_version: int
    leading: int
    trailing: tuple[int, ...]
    defaults: tuple[float, ...]
    parameters: tuple[DerivedParameter, ...]

    def parameter(self, name: str) -> DerivedParameter:
        """The measured route for the parameter of this name.

        By name because that is the only handle a *version* migration has. The
        older product's bank has its own indices and its own count, and the one
        thing the two versions agree on is what a knob is called.
        """
        for parameter in self.parameters:
            if parameter.name == name:
                return parameter
        raise StateTransformError(f"{self.product} has no parameter named {name!r}")

    def build(self, preset_name: str, normalized: Mapping[str, float]) -> bytes:
        """A state where each named parameter takes a normalized value.

        The seam a version migration writes through: the caller decides which
        of this product's knobs a dead predecessor's bank has something to say
        about, and the measured curves turn each 0-to-1 value into what the
        plugin stores. Every other slot keeps the plugin's default, which is
        what the plugin itself would have there.
        """
        values = list(self.defaults)
        for name, value in normalized.items():
            parameter = self.parameter(name)
            values[parameter.slot] = parameter.transfer(value)
        return FabfState(
            version=self.state_version,
            preset_name=preset_name,
            leading=self.leading,
            parameters=tuple(values),
            trailing=self.trailing,
        ).encode()

    def convert(self, payload: bytes) -> bytes:
        """Re-encode this product's own VST2 bank as its VST3 ``ProcessorState``.

        Same product on both sides, so the join is the table itself: every
        parameter takes the bank float its own index names.
        """
        bank = LegacyBank.parse(payload)
        highest = max(parameter.bank_index for parameter in self.parameters)
        if len(bank.parameters) <= highest:
            raise StateTransformError(
                f"a {self.product} bank has more than {highest} parameters, not {len(bank.parameters)}. "
                "Another product's bank read as this one would land on a patch nobody chose."
            )
        return self.build(
            bank.preset_name,
            {parameter.name: bank.parameters[parameter.bank_index] for parameter in self.parameters},
        )


# Where the tables the rig produced are kept, one JSON file per product. Three
# levels up from here is the package root, because this module sits two
# packages deep.
DERIVED_TABLES: pathlib.Path = pathlib.Path(__file__).parents[2] / "data" / "fabfilter"


def read_derived_table(path: pathlib.Path) -> DerivedTable:
    """One product's derived table off disk."""
    document = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    parameters = cast(list[dict[str, object]], document["parameters"])
    return DerivedTable(
        product=cast(str, document["product"]),
        state_version=int(cast(int, document["state_version"])),
        leading=int(cast(int, document["leading"])),
        trailing=tuple(int(word) for word in cast(list[int], document["trailing"])),
        defaults=tuple(float(value) for value in cast(list[float], document["defaults"])),
        parameters=tuple(
            DerivedParameter(
                name=cast(str, entry["name"]),
                bank_index=int(cast(int, entry["bank_index"])),
                slot=int(cast(int, entry["slot"])),
                transfer=_read_transfer(cast(Mapping[str, object], entry["transfer"])),
            )
            for entry in parameters
        ),
    )
