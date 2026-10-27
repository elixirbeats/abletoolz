"""A transfer table the derivation rig measured, and the state it writes.

A product whose VST2 exposes no chunk is saved as a
:class:`~abletoolz.plugins.state.fxbk.LegacyBank` of normalized floats and its
VST3 as a block of floats in the plugin's own units, and nobody outside the
vendor has written down the curve between the two. Rather than
guess one, the rig asks both binaries: which state slot each parameter owns,
and what the plugin writes there across a sweep of that parameter. What comes
back is a table, one JSON file per product in ``state/data/``.

Three things live here, in that order: the curves a table may name, the table
itself, and the ``FabF`` container the tables measured so far write. The
container sits with the tables rather than with
:mod:`abletoolz.plugins.state.fabfilter` because a derived table is the
only thing in the library that writes one -- FabFilter's other generation, the
``FFBS`` chunk, is a codec on its own.

Nothing in a table is reasoned out, which is why the shapes are so plain: a
line is the two numbers the plugin wrote at 0 and at 1, and a step is the values
it snapped to. Every curve measured so far is one of the two.
"""

from __future__ import annotations

import dataclasses
import json
import math
import pathlib
import struct
from collections.abc import Mapping
from typing import cast

from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.fxbk import LegacyBank

# Length-prefixed fields run through every FabFilter format: a little-endian
# word, then that many bytes.
_U32 = struct.Struct("<I")
_F32 = struct.Struct("<f")


def _float32(value: float) -> float:
    """``value`` rounded to the nearest float32, the precision a plugin computes and saves in."""
    return cast(float, _F32.unpack(_F32.pack(value))[0])


# -- what a plugin does between a knob and its saved state ------------------


@dataclasses.dataclass(frozen=True, slots=True)
class LinearTransfer:
    """``native = intercept + slope * normalized``.

    Covers dB, log2 Hz, ratios and every identity mapping, which between them is
    most of a FabFilter. Both coefficients are what the plugin wrote at 0 and at
    1 rather than a regression, so a state built from them is byte-identical to
    one the plugin would have written.

    The arithmetic is the plugin's own: float32, the product rounded before the
    sum. Measured 2026-10-24 by sweeping Pro-C 2, Pro-L 2 and Pro-G's VST3 at 24
    positions: that matches all 1344 saved floats, where computing in double
    and rounding once missed 89 of them by one float32 step.
    """

    intercept: float
    slope: float

    def __call__(self, normalized: float) -> float:
        product = _float32(_float32(self.slope) * _float32(normalized))
        return _float32(_float32(self.intercept) + product)


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


# What a derived table may say a parameter's curve is.
type ParameterTransfer = LinearTransfer | StepTransfer


def _read_transfer(payload: Mapping[str, object]) -> ParameterTransfer:
    """One transfer model off disk, refusing a kind nothing here evaluates."""
    kind = payload["kind"]
    if kind == "linear":
        intercept, slope = cast(float, payload["intercept"]), cast(float, payload["slope"])
        return LinearTransfer(intercept=float(intercept), slope=float(slope))
    if kind == "step":
        return StepTransfer(values=tuple(float(value) for value in cast(list[float], payload["values"])))
    raise StateTransformError(f"{kind!r} is not a transfer model this module evaluates")


# -- the container a derived table writes -----------------------------------

FABF_MAGIC = b"FabF"


@dataclasses.dataclass(frozen=True, slots=True)
class FabfState:
    """The state the FabF-generation products keep, taken apart.

    The other half of the same story as
    :class:`~abletoolz.plugins.state.fabfilter.FfbsState`. Pro-C 2, Pro-L 2,
    Pro-R, Pro-MB, Pro-DS, Pro-G and Micro expose no VST2 chunk -- so their
    ``.als`` Buffer is a :class:`~abletoolz.plugins.state.fxbk.LegacyBank` --
    but their VST3 writes this: ``FabF``, a version, a length-prefixed preset name, a word
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
    """One parameter's route from the VST2 bank into the VST3 state.

    ``overlay`` marks an option that shares its slot with the parameter that
    owns it and takes the slot over only when set: Pro-G's "Ex Style" writes 5
    (Ducking) into the slot "Style" fills with 0 to 4, and writes nothing when
    off. An overlay is applied after every owner, and only when its value is
    not zero.
    """

    name: str
    bank_index: int
    slot: int
    transfer: ParameterTransfer
    overlay: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class DerivedTable:
    """How one FabF-generation product's bank becomes its VST3 state.

    The derivation rig loaded the product's VST2 in one host and its VST3 in another, proved the bank's float order is
    the VST2 parameter order, moved each VST3 parameter to find which state slot
    it owns, swept it to fit its curve, and joined the two sides by parameter
    name.
    """

    product: str
    state_version: int
    leading: int
    trailing: tuple[int, ...]
    defaults: tuple[float, ...]
    parameters: tuple[DerivedParameter, ...]

    def convert(self, payload: bytes) -> bytes:
        """Re-encode this product's own VST2 bank as its VST3 ``ProcessorState``.

        Same product on both sides, so the join is the table itself: every
        parameter takes the bank float its own index names, through its measured
        curve. Every other slot keeps the plugin's default, which is what the
        plugin itself would have there.
        """
        bank = LegacyBank.parse(payload)
        highest = max(parameter.bank_index for parameter in self.parameters)
        if len(bank.parameters) <= highest:
            raise StateTransformError(
                f"a {self.product} bank has more than {highest} parameters, not {len(bank.parameters)}. "
                "Another product's bank read as this one would land on a patch nobody chose."
            )
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in bank.parameters):
            raise StateTransformError(f"{self.product} parameters must be finite normalized values between 0 and 1")
        values = list(self.defaults)
        for parameter in sorted(self.parameters, key=lambda parameter: parameter.overlay):
            native = parameter.transfer(bank.parameters[parameter.bank_index])
            if not parameter.overlay or native != 0.0:
                values[parameter.slot] = native
        return FabfState(
            version=self.state_version,
            preset_name=bank.preset_name,
            leading=self.leading,
            parameters=tuple(values),
            trailing=self.trailing,
        ).encode()


# Where the tables the rig produced are kept, one JSON file per product.
DERIVED_TABLES: pathlib.Path = pathlib.Path(__file__).parent / "data"


def read_derived_table(path: pathlib.Path) -> DerivedTable:
    """One product's derived table off disk."""
    document = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    parameters = cast(list[dict[str, object]], document["parameters"])
    owners: dict[int, str] = {}
    for entry in parameters:
        if entry.get("overlay"):
            continue
        slot, name = int(cast(int, entry["slot"])), cast(str, entry["name"])
        if slot in owners:
            raise StateTransformError(
                f"{path.name}: {owners[slot]!r} and {name!r} both own slot {slot}; one of them has to be an overlay"
            )
        owners[slot] = name
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
                overlay=bool(entry.get("overlay", False)),
            )
            for entry in parameters
        ),
    )
