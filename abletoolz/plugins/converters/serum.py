"""Serum patches into Serum 2. Not finished: LFO shapes and some FX settings are not carried yet.

Serum 2 imports a Serum 1 preset itself, but only from a file loaded in its own
browser; a Serum 1 patch handed over as Serum 2's state is refused. So the patch
is rebuilt control by control into Serum 2's own init patch, the way the 22
Serum 2 patches in the owner's sets that Serum 2 imported from Serum 1 are
built: the same keys, and ``kParamS1Compatibility`` set.

Measured 2026-10-26 in the installed Serum 1.3 and Serum 2 2.1.4, hosted
headless (``serum1_to_serum2_20261026`` in the research):

* 189 controls -- oscillators, sub, noise, filter, envelopes and their curves,
  LFO rates, glide, macros, mod amounts and outputs -- are carried by display:
  each Serum 1 knob position becomes the value Serum 2 stores where its own
  display reads the same, measured at 513 positions per control. All 96 of
  Serum 1's filter types are Serum 2's first 96, by name; two warp modes,
  Sync Window and Sync 1/2 Win., have no counterpart.
* All 32 mod slots, with each source Serum 1 can name matched to Serum 2's by
  what drives it (each routed to a silenced oscillator and rendered under every
  input), and each destination resolved to the Serum 2 control its own control
  joined to. Serum 2 routes by the destination's name.
* Wavetables and the noise sample by their path in each library, as Serum 2's
  importer writes them.
* The FX rack: each module Serum switches on, in Serum's order, with 65 of its
  controls carried by display the same way, and the mod routings into them.
  Serum's order array holds each module's position; rendering a distortion and
  a lowpass under a permutation the two possible readings disagree on settled
  which.
* LFOs running free in Hz, which Serum switches for all eight at once.

Not carried, and named on every device that uses it: the delay's times, the
reverb's decay and spin, FX output levels moved off 0 dB, LFO shapes,
wavetables embedded in the patch rather than referenced by path, and the few
mod sources one rendered note cannot tell apart (Serum's ids 17-19). Serum's
earliest patch layout (20,704 bytes) is refused.

Accepted 2026-10-26 on 40 of the owner's patches picked at random: Serum 2
loaded every conversion and kept every FX module and value it was handed, and
of 7,474 control readings, 99.4% display the same in Serum 2 as the original
does in Serum 1.
"""

from __future__ import annotations

import datetime
import functools
import gzip
import json
import pathlib
import struct
from typing import Any

import cbor2

from abletoolz.plugins.converters import limitation
from abletoolz.plugins.state.catalog import Converted, converter
from abletoolz.plugins.state.errors import StateTransformError
from abletoolz.plugins.state.measured import MeasuredState, StateEvidence, StateRung
from abletoolz.plugins.state.xfer import SerumChunk, XferJson

DATA = pathlib.Path(__file__).with_name("data")
_FLOAT = struct.Struct("<f")
_SLOT = struct.Struct("<IHH")
_SLOT_SOURCE = 20
_SLOT_COUNT = 32
# Fewer distinct values than this over 513 positions is a stepped control: read the nearest step, never between two.
_STEPPED = 64
# 1 when all of the patch's LFOs run free in Hz rather than to the tempo; older layouts end before it. Free, Serum shows
# rate 100 x^4 Hz and rise and delay 4 x seconds at knob position x (every one of 513 positions), and Serum 2 stores
# Hz and seconds as they are.
_LFOS_FREE = 45537
_LFO_COUNT = 8
_ORDER = struct.Struct("<10I")
# A knob this far from where Serum's init or 0 dB leaves it has been moved.
_MOVED = 1 / 1024

UNFINISHED_NOTE = "Serum to Serum 2 is not finished: audition this device before trusting it."
CARRIED_NOTE = (
    "Carried from Serum: oscillators, sub, noise, filter, envelopes, LFO rates, glide, macros, the mod matrix and the "
    "FX rack, matched by what each plugin displays; wavetables and noise by their library path."
)


@functools.cache
def _data() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(gzip.decompress((DATA / "serum1-to-serum2.json.gz").read_bytes()))
    return loaded


@functools.cache
def _baseline() -> XferJson:
    return XferJson.parse((DATA / "serum2-init.state").read_bytes())


def _lookup(table: list[Any], menu: bool, x: float) -> Any:
    """What Serum 2 stores for Serum knob position ``x``; None is Serum 2's default, which it leaves out."""
    position = min(max(x, 0.0), 1.0) * (len(table) - 1)
    low = int(position)
    high = min(low + 1, len(table) - 1)
    nearest = table[round(position)]
    if menu or nearest is None or table[low] is None or table[high] is None:
        return nearest
    if len({v for v in table if v is not None}) < _STEPPED:
        return nearest
    fraction = position - low
    return table[low] + (table[high] - table[low]) * fraction


def _place(doc: dict[str, Any], path: str, value: Any) -> None:
    *parents, name = path.split("/")
    node = doc
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    if value is None:
        node.pop(name, None)
    else:
        node[name] = value


def _path_at(patch: bytes, offset: int, field: int) -> str:
    raw = patch[offset : offset + field].split(b"\0", 1)[0]
    return raw.decode("latin1").replace("\\", "/")


def _slot_offset(slot: int) -> int:
    layout = _data()["mod_slots"]
    first = layout["first"]
    block = first if slot < first["count"] else layout["second"]
    return int(block["offset"]) + (slot % int(first["count"])) * int(layout["record"])


def _value(source: bytes, offset: int) -> float:
    (x,) = _FLOAT.unpack_from(source, offset)
    return float(x)


def _fx_rack(source: bytes, left_out: list[str]) -> tuple[list[dict[str, Any]], dict[int, int]]:
    """Serum's switched-on FX as Serum 2's main rack, and each module's place in it by Serum's module index."""
    fx = _data()["fx"]
    modules = fx["modules"]
    order = list(_ORDER.unpack_from(source, fx["order"]))
    if sorted(order) != list(range(len(modules))):
        order = list(range(len(modules)))
    used = [k for k, module in enumerate(modules) if _value(source, module["enable"]) >= 0.5]
    used.sort(key=lambda k: order[k])
    rack, places = [], {}
    moved_levels = []
    for place, k in enumerate(used):
        module = modules[k]
        params: dict[str, Any] = dict(module.get("fixed", {}))
        for control in module["controls"]:
            value = _lookup(control["table"], control["menu"], _value(source, control["offset"]))
            if value is not None:
                params[control["key"]] = value
        rack.append({module["serum2"]: {"plainParams": params}, "type": module["type"]})
        places[k] = place
        if any(abs(_value(source, level["offset"]) - level["unity"][0]) > _MOVED for level in module["levels"]):
            moved_levels.append(module["serum2"].removeprefix("FX"))
        for setting in module.get("not_carried", ()):
            if abs(_value(source, setting["offset"]) - setting["init"]) > _MOVED and setting["note"] not in left_out:
                left_out.append(setting["note"])
    if moved_levels:
        left_out.append("output levels of the " + ", ".join(moved_levels) + " FX")
    return rack, places


def _free_lfos(source: bytes, doc: dict[str, Any]) -> None:
    offsets = {control["serum1"]: control["offset"] for control in _data()["controls"]}
    for k in range(_LFO_COUNT):
        rate, rise, delay = (_value(source, offsets[f"LFO{k + 1} {name}"]) for name in ("Rate", "Rise", "Delay"))
        params = doc.setdefault(f"LFO{k}", {}).setdefault("plainParams", {})
        params.update(
            {"kParamBeatSync": 0.0, "kParamRate": 100.0 * rate**4, "kParamRise": 4.0 * rise, "kParamDelay": 4.0 * delay}
        )


def convert(patch: bytes, *, lossy: bool = False) -> Converted:
    """A Serum chunk as a Serum 2 processor state.

    What is not carried yet is refused, or with ``lossy`` named in the notices and left out.
    """
    chunk = SerumChunk.parse(patch)
    data = _data()
    source = chunk.patch
    doc = cbor2.loads(_baseline().decoded())
    notices = [UNFINISHED_NOTE, CARRIED_NOTE]
    left_out: list[str] = []

    for control in data["controls"]:
        if control["offset"] + _FLOAT.size > len(source):
            continue
        (x,) = _FLOAT.unpack_from(source, control["offset"])
        _place(doc, control["key"], _lookup(control["table"], control["menu"], x))

    rack, places = _fx_rack(source, left_out)
    doc["FXRack0"]["FX"] = rack
    modules = data["fx"]["modules"]

    sources, destinations = data["sources"], data["destinations"]
    unmapped_sources = unmapped_destinations = 0
    for slot in range(_SLOT_COUNT):
        record = _slot_offset(slot)
        if record + _SLOT_SOURCE + _SLOT.size > len(source):
            continue
        word, _, destination = _SLOT.unpack_from(source, record + _SLOT_SOURCE)
        main, aux = word & 0xFFFF, word >> 16
        if main == 0:
            continue
        target = destinations.get(str(destination))
        fx_target = data["fx"]["destinations"].get(str(destination))
        if fx_target is not None:
            if fx_target["module"] not in places:
                continue  # into a module Serum has switched off, where it does nothing
            module = modules[fx_target["module"]]
            target = {
                "id": places[fx_target["module"]],
                "param_id": fx_target["param_id"],
                "param": fx_target["param"],
                "type": module["serum2"],
            }
        if str(main) not in sources:
            unmapped_sources += 1
            continue
        if target is None:
            unmapped_destinations += 1
            continue
        mod_slot = doc.setdefault(f"ModSlot{slot}", {})
        mod_slot.update(
            {
                "destModuleID": target["id"],
                "destModuleParamID": target["param_id"],
                "destModuleParamName": target["param"],
                "destModuleTypeString": target["type"],
                "source": [sources[str(main)], sources.get(str(aux), 0)],
            }
        )
    if unmapped_sources:
        left_out.append(f"{unmapped_sources} mod routing(s) from a source not yet told apart")
    if unmapped_destinations:
        left_out.append(f"{unmapped_destinations} mod routing(s) into a control not carried")

    tables = data["wavetables"]
    for letter, index in (("a", 0), ("b", 1)):
        path = _path_at(source, tables[letter], tables["field"])
        oscillator = doc[f"Oscillator{index}"][f"WTOsc{index}"]
        if path:
            oscillator["relativePathToWT"] = path
            for stale in ("numFrames", "numChannels", "sampleRate", "embeddedWTData"):
                oscillator.pop(stale, None)
        else:
            left_out.append(f"oscillator {letter.upper()}'s wavetable, embedded in the patch")
    noise = _path_at(source, tables["noise"], tables["field"])
    noise_osc = doc["Oscillator3"]["NoiseOsc3"]
    if noise and noise != noise_osc.get("relativePathToNoiseSample"):
        noise_osc["relativePathToNoiseSample"] = noise
        for stale in ("numFrames", "numChannels", "sampleRate", "detuneFactor"):
            noise_osc.pop(stale, None)

    if len(source) > _LFOS_FREE and source[_LFOS_FREE]:
        _free_lfos(source, doc)
    if chunk.tables and not any("embedded" in item for item in left_out):
        left_out.append("wavetable frames the patch embeds; Serum 2 loads each table from its library path")

    _place(doc, "Global0/plainParams/kParamS1Compatibility", 1.0)
    if left_out:
        limitation(notices if lossy else None, "Not carried yet: " + "; ".join(left_out) + ".")
    try:
        state = _baseline().with_body(cbor2.dumps(doc)).encode()
    except (ValueError, TypeError) as error:
        raise StateTransformError(f"the Serum 2 patch could not be written: {error}") from None
    return Converted(state, tuple(notices))


@converter(
    "xfer.serum",
    "xfer.serum-2",
    MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 26)),
)
def serum_into_serum_2(patch: bytes, program: int | None) -> Converted:
    """A Serum chunk as Serum 2's patch. ``program`` is not used: the chunk is the patch."""
    return convert(patch, lossy=True)
