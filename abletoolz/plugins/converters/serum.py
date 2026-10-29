"""Serum patches into Serum 2, rebuilt without either plugin the way Serum 2's own Serum 1 importer builds them.

Serum 2 imports a Serum 1 preset itself, but only from a preset file its editor
loads; a Serum 1 patch handed over as Serum 2's state is refused. So the patch is
rebuilt here into what that importer writes: its import of Serum 1's init patch
is the starting point, and each field lands as the value the importer stores for
it. Measured in Serum 1.3 and Serum 2 2.1.4, hosted headless, with the importer
itself as the reference:

* Every control's value is the one the importer stores, read at 513 knob
  positions per control: oscillators, sub, noise, filter, envelopes, glide,
  macros, the main volume (a cubic gain in Serum, stored as its importer scales
  it), warp modes (Serum's FM is Serum 2's PD; the sync windows are Serum 2's
  sync with its variant) and the chaos LFOs.
* Oscillator A's switch, and where each source goes -- the filter, past it, or
  straight out -- as Serum's per-source filter and direct switches set it.
* Serum's settings block -- mono, legato, portamento, poly count, filter and
  noise key tracking, detune modes, unison range, tuning, oversampling, note
  latch, the reverb's mode -- whose place in the patch moved between Serum
  versions: the patch's own version decides where it is read, and a version
  not measured is refused.
* Each LFO's shape, rate, rise, delay, smoothing, tempo sync, dotted and
  triplet switches, and its trigger or envelope mode; a free-running LFO crosses
  in Hz and seconds.
* All 32 mod slots from their own records: source, destination, amount, output,
  polarity, bypass, aux inversion and curves. Each source and destination is the
  one the importer resolves it to, and where a destination's range differs, the
  output is scaled as the importer scales it.
* Wavetables and the noise sample by their library path, and the wavetable
  frames a patch embeds.
* The FX rack: each module Serum has loaded or a mod slot points into, switched
  on or off as Serum has it, in Serum's order (a position past the rack, or one
  a later module also holds, goes to the first place nobody holds), with its
  controls, output levels, delay times and the reverb's decay and spin.

Not carried, and named on every device that may use it: the drawn curves of
the warp modes and the distortion, the velocity and note scaling curves, a
custom tuning, an oscillator octave or semitone between steps (the importer
rounds it and routes the rest through a mod slot), the few mod sources and
destinations the importer does not resolve, and, in patches from Serum versions
that keep them elsewhere, LFO rise and delay, LFOs 5-8, mod slots 17-32 and FX
output levels. Serum's earliest patch layout (20,704 bytes) is refused.
"""

from __future__ import annotations

import copy
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
_INT = struct.Struct("<i")
_SLOT = struct.Struct("<IHH")
# A mod slot's record: amount and output (floats), polarity (a byte), source and destination, switches
# (bit 0 inverts the aux source, bit 1 bypasses the slot), then the curves (bytes).
_SLOT_RECORD = 40
_SLOT_AMOUNT = 4
_SLOT_BIPOLAR = 12
_SLOT_SOURCE = 20
_SLOT_SWITCHES = 28
_SLOT_CURVES = 32
_SLOT_COUNT = 32
# Fewer distinct values than this over 513 positions is a stepped control: read the nearest step, never between two.
_STEPPED = 64
# Free, Serum shows LFO rate 100 x^4 Hz and rise and delay 4 x seconds at knob position x (every one of 513
# positions), and Serum 2 stores Hz and seconds as they are.
_FREE_RATE, _FREE_SECONDS = 100.0, 4.0
_ORDER = struct.Struct("<10I")
# Bytes the embedded frames of oscillators A and B take in the chunk's tables stream, then each one's interpolation.
_EMBEDDED_SIZES, _EMBEDDED_INTERPOLATION = 18792, 18800
_FRAME = 2048
# The first patch version with mod slots 17-32, LFO rise and delay and LFOs 5-8 where the importer reads them, and FX
# output levels past Serum's settings block (which earlier versions keep there).
_FULL_LAYOUT = 0.1601
# A knob this far from where Serum's init or 0 dB leaves it has been moved.
_MOVED = 1 / 1024

UNFINISHED_NOTE = "Serum to Serum 2 is not finished: audition this device before trusting it."
CARRIED_NOTE = (
    "Carried from Serum as Serum 2's own Serum 1 importer writes it: oscillators, sub, noise, filter, envelopes, "
    "voicing, routing, LFO rates and modes, glide, macros, the mod matrix and the FX rack; wavetables by their library "
    "path or embedded frames."
)


@functools.cache
def _data() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(gzip.decompress((DATA / "serum1-to-serum2.json.gz").read_bytes()))
    return loaded


@functools.cache
def _baseline() -> XferJson:
    """Serum 2's own import of Serum 1's init patch."""
    return XferJson.parse((DATA / "serum1-init-imported.state").read_bytes())


def _lookup(table: list[Any], menu: bool, x: float, between: dict[str, float] | None = None) -> Any:
    """What Serum 2 stores for Serum knob position ``x``; None is Serum 2's default, which it leaves out.

    ``between`` holds the value under a measured default, for a knob just off it to interpolate through.
    """
    position = min(max(x, 0.0), 1.0) * (len(table) - 1)
    low = int(position)
    high = min(low + 1, len(table) - 1)
    nearest = table[round(position)]
    if menu or position == low:
        return table[low] if position == low else nearest
    lo, hi = table[low], table[high]
    if between:
        lo, hi = between.get(str(low), lo), between.get(str(high), hi)
    if lo is None or hi is None or len({v for v in table if v is not None}) < _STEPPED:
        return nearest
    fraction = position - low
    return lo + (hi - lo) * fraction


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


def _curve(byte: int) -> float | None:
    """A mod slot's curve, a byte with 128 straight; Serum 2 stores -100..100."""
    return None if byte == 128 else (byte - 128) / (128 if byte < 128 else 127.5) * 100


def _slot_params(source: bytes, record: int, out_scale: float) -> dict[str, Any]:
    """A mod slot's amount, output, polarity, switches and curves, from its own record.

    Where a destination's range differs between the two, the importer scales the slot's output by the ratio
    (``out_scale``, measured per destination).
    """
    amount, out = struct.unpack_from("<2f", source, record + _SLOT_AMOUNT)
    switches = source[record + _SLOT_SWITCHES]
    params: dict[str, Any] = {}
    values = {
        "kParamAmount": 100.0 * amount or None,
        "kParamOut": None if abs(out * out_scale - 1.0) < 1e-9 else 100.0 * out * out_scale,
        "kParamBipolar": 1.0 if source[record + _SLOT_BIPOLAR] else None,
        "kParamAuxInverted": 1.0 if switches & 1 else None,
        "kParamBypass": 1.0 if switches & 2 else None,
        "kParamCurveIn": _curve(source[record + _SLOT_CURVES]),
        "kParamAuxCurve": _curve(source[record + _SLOT_CURVES + 1]),
    }
    for key, value in values.items():
        _place(params, key, value)
    return params


def _layout(source: bytes) -> tuple[float, dict[str, Any]]:
    """The patch's Serum version, and where that version keeps its settings and LFO switches."""
    versions = _data()["versions"]
    version = round(_value(source, versions["offset"]), 4)
    layout = versions["layouts"].get(f"{version:.4f}")
    if layout is None:
        raise StateTransformError(
            f"A Serum patch from patch version {version:.4f} is a layout abletoolz has not measured"
        )
    if layout["lfo_switches"] == "blocks":
        switches = _data()["lfo_switches"]
        end = switches["blocks"]["first"] + switches["count"] * switches["blocks"]["stride"]
        if len(source) < end:
            raise StateTransformError(
                f"A {len(source)} byte Serum patch is too short for patch version {version:.4f}, "
                f"whose LFOs run to byte {end}"
            )
    return version, layout


def _settings(source: bytes, doc: dict[str, Any], start: int) -> None:
    for field in _data()["settings"]["fields"]:
        x = _value(source, start + field["at"])
        _place(doc, field["key"], _lookup(field["table"], field["menu"], x, field.get("between")))


def _routing(source: bytes, doc: dict[str, Any], settings: int) -> None:
    """Where each source goes: Serum's filter switch per source, and the noise's and sub's direct-out switches."""
    for rule in _data()["routing"]:
        filtered = _value(source, rule["filter"]) >= 0.5
        if "direct" in rule and _value(source, settings + rule["direct"]) >= 0.5:
            destination = "kRoutingDestDirect"
        elif filtered:
            destination = rule.get("when_on")
        else:
            destination = rule.get("when_off")
        _place(doc, f"RoutingSlot{rule['slot']}/plainParams/kParamRoutingDest", destination)


def _lfo_switches(source: bytes, doc: dict[str, Any], kind: str) -> None:
    """Each LFO's tempo sync and mode; a free-running one crosses its rate, rise and delay in Hz and seconds."""
    switches = _data()["lfo_switches"]
    offsets = {control["serum1"]: control["offset"] for control in _data()["controls"]}
    if kind == "blocks":
        blocks = switches["blocks"]
        count = switches["count"]
    else:
        flags = switches["flags"]
        count = flags["count"]
    if kind != "blocks":
        for k in range(count, switches["count"]):
            doc.setdefault(f"LFO{k}", {}).setdefault("curveData", {})["loopbackPointNum"] = flags["loopback"]
    for k in range(count):
        if kind == "blocks":
            at = blocks["first"] + k * blocks["stride"]
            free, trig, env, anchored = (source[at + blocks[name]] for name in ("free", "trig", "env", "anchored"))
            dotted, triplets = source[at + blocks["dotted"]], source[at + blocks["triplets"]]
        else:
            free, trig, env = (source[flags[name] + k] for name in ("free", "trig", "env"))
            anchored, dotted, triplets = 1, 0, 0
        lfo = doc.setdefault(f"LFO{k}", {})
        if kind == "blocks":
            count, loopback = (_INT.unpack_from(source, at + blocks[name])[0] for name in ("points", "loopback"))
            arrays = {name: at + blocks[name] for name in ("x", "y", "curve")}
            shape = _lfo_shape(count, min(loopback, blocks["loopback_max"]), arrays, source)
        else:
            arrays = {name: flags[name] + k * flags["stride"] for name in ("x", "y", "curve")}
            shape = _lfo_shape(source[flags["points"] + k], flags["loopback"], arrays, source)
        if shape is not None:
            lfo["curveData"] = shape
        params = lfo.setdefault("plainParams", {})
        _place(params, "kParamDotted", 1.0 if dotted else None)
        _place(params, "kParamTriplets", 1.0 if triplets else None)
        _place(params, "kParamMode", "Envelope" if env else None if trig else "Free")
        _place(params, "kParamDefaultMode", None if trig and not env else 0.0)
        _place(params, "kParamAnchored", 0.0 if not anchored and (trig or not free) else None)
        if not free:
            _place(params, "kParamBeatSync", None)
            continue
        params["kParamBeatSync"] = 0.0
        params["kParamRate"] = _FREE_RATE * _value(source, offsets[f"LFO{k + 1} Rate"]) ** 4
        if kind == "blocks":
            for name, key in (("Rise", "kParamRise"), ("Delay", "kParamDelay")):
                seconds = _FREE_SECONDS * _value(source, offsets[f"LFO{k + 1} {name}"])
                _place(params, key, seconds or None)


def _positions(order: list[int]) -> list[int]:
    """Each FX module's place in the rack, from Serum's position per module.

    A position past the rack, or one a later module also holds, is repaired the way Serum 2's importer does: such
    modules take the positions nobody holds, in module order.
    """
    seen: set[int] = set()
    again = []
    for k in reversed(range(len(order))):
        if order[k] < len(order) and order[k] not in seen:
            seen.add(order[k])
        else:
            again.append(k)
    places = list(order)
    for k, free in zip(sorted(again), sorted(set(range(len(order))) - seen), strict=False):
        places[k] = free
    return places


def _lfo_shape(count: int, loopback: int, arrays: dict[str, int], source: bytes) -> dict[str, Any] | None:
    """An LFO's shape as Serum 2's curve: its points' x, y and curve, one more than the point count (the wrap)."""
    if count < 1 or any(start < 0 or start + 8 * (count + 1) > len(source) for start in arrays.values()):
        return None
    values = {name: list(struct.unpack_from(f"<{count + 1}d", source, start)) for name, start in arrays.items()}
    return {
        "curveVals": values["curve"],
        "loopbackPointNum": loopback,
        "numPoints": count,
        "xVals": values["x"],
        "yVals": values["y"],
    }


def _fx_rack(
    source: bytes, version: float, settings: int, slots: int, left_out: list[str]
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    """Serum's loaded FX, and any a mod slot points into, as Serum 2's main rack; each module's place by its index."""
    fx = _data()["fx"]
    modules = fx["modules"]
    targeted = set()
    for slot in range(slots):
        record = _slot_offset(slot)
        if record + _SLOT_RECORD <= len(source):
            fx_target = fx["destinations"].get(str(_SLOT.unpack_from(source, record + _SLOT_SOURCE)[2]))
            if fx_target is not None:
                targeted.add(fx_target["module"])
    order = _positions(list(_ORDER.unpack_from(source, fx["order"])))
    used = [k for k, module in enumerate(modules) if source[module["present"]] or k in targeted]
    used.sort(key=lambda k: order[k])
    rack, places = [], {}
    moved_levels = []
    for place, k in enumerate(used):
        module = modules[k]
        entry: dict[str, Any] = copy.deepcopy(module["template"])
        params = entry[module["serum2"]]["plainParams"]
        switched_off = _value(source, module["enable"]) < 0.5 or source[module["off"]]
        _place(params, "kParamEnable", 0.0 if switched_off else None)
        if "phase" in module and module["phase"] + _FLOAT.size <= len(source):
            entry[module["serum2"]]["lfophasor"] = _value(source, module["phase"])
        mode = module.get("mode")
        plate = mode is not None and _value(source, settings + mode["settings_at"]) < 0.5
        if plate:
            for key, value in mode["plate"].items():
                _place(params, key, value)
        for control in module["controls"]:
            if control.get("from_version", 0.0) > version:
                continue
            x = _value(source, control["offset"])
            if "index_at" in control:
                x = struct.unpack_from("<H", source, control["index_at"])[0] / control["steps"]
            if plate and "plate" in control:
                for key, table in control["plate"].items():
                    _place(params, key, _lookup(table, control["menu"], x))
                continue
            table = control["table"]
            if "sync" in control and _value(source, control["sync"]) >= 0.5:
                table = control["synced_table"]
            when = control.get("when")
            if when is not None and params.get(when["key"]) == when["value"]:
                table = when["table"]
            _place(params, control["key"], _lookup(table, control["menu"], x, control.get("between")))
        entry["type"] = module["type"]
        rack.append(entry)
        places[k] = place
        moved = (abs(_value(source, level["offset"]) - level["unity"][0]) > _MOVED for level in module["levels"])
        if version < _FULL_LAYOUT and any(moved):
            moved_levels.append(module["serum2"].removeprefix("FX"))
        for setting in module.get("not_carried", ()):
            if abs(_value(source, setting["offset"]) - setting["init"]) > _MOVED and setting["note"] not in left_out:
                left_out.append(setting["note"])
    if moved_levels:
        left_out.append("output levels of the " + ", ".join(moved_levels) + " FX")
    return rack, places


def _wavetables(chunk: SerumChunk, doc: dict[str, Any]) -> None:
    """Each oscillator's table by its library path, and the frames the patch embeds, as the importer writes them."""
    source, tables = chunk.patch, _data()["wavetables"]
    sizes = struct.unpack_from("<2i", source, _EMBEDDED_SIZES)
    frames = memoryview(chunk.tables)
    start = 0
    for index, letter in enumerate("ab"):
        oscillator: dict[str, Any] = {"plainParams": doc[f"Oscillator{index}"][f"WTOsc{index}"].get("plainParams", {})}
        for keep in ("flex",):
            if keep in doc[f"Oscillator{index}"][f"WTOsc{index}"]:
                oscillator[keep] = doc[f"Oscillator{index}"][f"WTOsc{index}"][keep]
        path = _path_at(source, tables[letter], tables["field"])
        size = max(sizes[index], 0)
        if path:
            oscillator["relativePathToWT"] = path
        if size and (size % (4 * _FRAME) or start + size > len(frames)):
            raise StateTransformError(
                f"Serum's oscillator {letter.upper()} declares {size} bytes of embedded frames, "
                f"which the chunk's {len(frames)} bytes of tables cannot hold"
            )
        if size:
            oscillator["embeddedWTData"] = list(struct.unpack_from(f"<{size // 4}f", frames, start))
            oscillator["interpolateAfterLoad"] = source[_EMBEDDED_INTERPOLATION + index]
            if path:
                oscillator["tableDisplayName"] = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        start += size
        doc[f"Oscillator{index}"][f"WTOsc{index}"] = oscillator


def convert(patch: bytes, *, lossy: bool = False) -> Converted:
    """A Serum chunk as a Serum 2 processor state.

    What is not carried yet is refused, or with ``lossy`` named in the notices and left out.
    """
    chunk = SerumChunk.parse(patch)
    data = _data()
    source = chunk.patch
    version, layout = _layout(source)
    doc = cbor2.loads(_baseline().decoded())
    notices = [UNFINISHED_NOTE, CARRIED_NOTE]
    left_out: list[str] = []

    for control in data["controls"]:
        if control["offset"] + _FLOAT.size > len(source) or control.get("from_version", 0.0) > version:
            continue
        (x,) = _FLOAT.unpack_from(source, control["offset"])
        _place(doc, control["key"], _lookup(control["table"], control["menu"], x, control.get("between")))
    _settings(source, doc, layout["settings"])
    if doc["Global0"]["plainParams"].get("kParamLegato"):
        doc["Global0"]["plainParams"]["kParamVelocityLegato"] = 0.0  # as the importer writes legato
    _routing(source, doc, layout["settings"])
    _lfo_switches(source, doc, layout["lfo_switches"])
    if version < _FULL_LAYOUT:
        left_out.append(
            "LFO rise and delay, LFOs 5-8 and mod slots 17-32, if this earlier Serum version's patch uses them"
        )

    slots = _SLOT_COUNT if version >= _FULL_LAYOUT else _SLOT_COUNT // 2
    rack, places = _fx_rack(source, version, layout["settings"], slots, left_out)
    doc["FXRack0"]["FX"] = rack
    modules = data["fx"]["modules"]

    sources, destinations = data["sources"], data["destinations"]
    unmapped_sources = unmapped_destinations = 0
    for slot in range(slots):
        record = _slot_offset(slot)
        if record + _SLOT_RECORD > len(source):
            continue
        word, _, destination = _SLOT.unpack_from(source, record + _SLOT_SOURCE)
        main, aux = word & 0xFFFF, word >> 16
        if main == 0:
            continue
        target = destinations.get(str(destination))
        fx_target = data["fx"]["destinations"].get(str(destination))
        if fx_target is not None:
            if fx_target["module"] not in places:
                continue  # into a module Serum has not loaded, where it does nothing
            target = {**fx_target, "id": places[fx_target["module"]], "type": modules[fx_target["module"]]["serum2"]}
        if str(main) not in sources:
            unmapped_sources += 1
            continue
        if target is None:
            unmapped_destinations += 1
            continue
        doc[f"ModSlot{slot}"] = {
            "destModuleID": target["id"],
            "destModuleParamID": target["param_id"],
            "destModuleParamName": target["param"],
            "destModuleTypeString": target["type"],
            "source": [sources[str(main)], sources.get(str(aux), 0)],
            "plainParams": _slot_params(source, record, target.get("out", 1.0)),
        }
    if unmapped_sources:
        left_out.append(f"{unmapped_sources} mod routing(s) from a source not yet told apart")
    if unmapped_destinations:
        left_out.append(f"{unmapped_destinations} mod routing(s) into a control not carried")

    _wavetables(chunk, doc)
    tables = data["wavetables"]
    noise = _path_at(source, tables["noise"], tables["field"])
    noise_osc = doc["Oscillator3"]["NoiseOsc3"]
    if noise and noise != noise_osc.get("relativePathToNoiseSample"):
        noise_osc["relativePathToNoiseSample"] = noise
        for stale in ("numFrames", "numChannels", "sampleRate", "detuneFactor"):
            noise_osc.pop(stale, None)

    warps = [doc[f"Oscillator{k}"][f"WTOsc{k}"].get("plainParams", {}).get("kParamWarpMenu") for k in (0, 1)]
    if any(warps) or any(module["type"] == 0 for module in rack):
        left_out.append("the drawn curves of the warp modes and the distortion, which cross as Serum's init curves")
    scalars = data["scalars"]
    if source[scalars["start"] : scalars["end"]] != bytes.fromhex(scalars["init"]):
        left_out.append("the velocity and note scaling curves")
    if any(
        abs(_value(source, at) * steps - round(_value(source, at) * steps)) > _MOVED
        for at, steps in data["pitch_steps"]
    ):
        left_out.append("an oscillator octave or semitone set between steps, which Serum 2 rounds")
    if len(source) > data["tuning"] and source[data["tuning"]]:
        left_out.append("the custom tuning")

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
    MeasuredState(StateRung.RE_ENCODE, (StateEvidence.HOSTED,), datetime.date(2026, 10, 29)),
)
def serum_into_serum_2(patch: bytes, program: int | None) -> Converted:
    """A Serum chunk as Serum 2's patch. ``program`` is not used: the chunk is the patch."""
    return convert(patch, lossy=True)
