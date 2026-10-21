"""Every load of a real plugin the derivation needs, one job per process.

Nothing in here decides anything. It loads one plugin, does exactly the
measurement its job asks for, and prints the numbers. The reasoning lives in
:mod:`derive_rig`, where a crash cannot reach it -- and crashes are the reason
for the split. These are third-party binaries with licence checks, static
initialisers and teardown paths that hang; one of them taking a process down
has to cost one measurement rather than the run.

Four jobs, two hosts, and the two hosts are not interchangeable:

* ``vst2_describe`` and ``vst2_bank`` go through DawDreamer, which is the only
  one of the two that will load a VST2 at all. DawDreamer speaks normalised
  values and will render a parameter's value as text, which is the ground truth
  for what a saved patch actually means.
* ``vst3_sweep`` and ``vst3_inject`` go through pedalboard, because pedalboard
  is a JUCE host and JUCE is what puts the plugin's state inside the envelope
  :mod:`juce_container` decodes -- the same envelope whose ``IComponent`` half
  is what an ``.als`` stores.

One thing about ``vst3_sweep`` is not obvious and was measured the hard way:
setting a VST3 parameter does *not* change the processor's saved state. The host
sets it on the edit controller, and the processor only learns about it when it
is handed a block of audio. Reading ``raw_state`` straight after a set returns
the state from before the set -- which is silently wrong, not an error. So every
set is followed by a render of a short silent block before the state is read.

Results go to stdout between sentinels because plugins print to stdout
themselves while they load, and the process ends in ``os._exit`` because
several of them never return from their own teardown.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import pathlib
import sys

import numpy as np

import fabf_state
from juce_container import unwrap, with_component

BEGIN = "<<<DERIVE"
END = "DERIVE>>>"

SAMPLE_RATE = 44100
# One short silent block is all it takes to make the processor drain its
# parameter-change queue, and short keeps a 46-parameter sweep to seconds.
_BLOCK = 64


@dataclasses.dataclass(frozen=True, slots=True)
class ParameterSweep:
    """One parameter moved across its whole range, and what the state did."""

    name: str
    python_name: str
    index: int
    declared_steps: int | None
    declared_min: float | None
    declared_max: float | None
    units: str
    positions: tuple[float, ...]
    texts: tuple[str, ...]
    # Every state slot that moved anywhere in this sweep, and its values.
    moved: dict[str, tuple[float, ...]]
    restored: bool


@dataclasses.dataclass(frozen=True, slots=True)
class InjectionReading:
    """What the target plugin said after one state blob was pushed into it."""

    label: str
    injected_len: int
    echoed: bool
    echoed_len: int
    echoed_hex: str
    setter_error: str
    raw_values: dict[str, float]
    texts: dict[str, str]


def _emit(payload: object) -> None:
    sys.stdout.write(f"{BEGIN}\n{json.dumps(payload, default=str)}\n{END}\n")
    sys.stdout.flush()
    os._exit(0)


# --- VST2, through DawDreamer -----------------------------------------------


def _native(module: str) -> str:
    """A path spelled the way the loader wants it.

    DawDreamer will take a forward-slash path, fail to open it, and hand back a
    processor object anyway; the failure only surfaces later as "Please load the
    plugin first". Normalising the separators here is cheaper than diagnosing
    that twice.
    """
    return str(pathlib.Path(module))


def _vst2_plugin(module: str) -> tuple[object, object]:
    """A loaded VST2 and the engine it belongs to.

    The engine is handed back because it has to stay referenced. A processor
    whose engine has been collected is still a live Python object and still
    answers ``get_num_output_channels``, but every call that needs the plugin
    itself raises "Please load the plugin first" -- so a function that returned
    only the processor would fail one call later, somewhere unrelated.
    """
    import dawdreamer

    engine = dawdreamer.RenderEngine(SAMPLE_RATE, 512)
    plugin = engine.make_plugin_processor("fx", _native(module))
    if plugin.get_plugin_parameter_size() < 1:
        raise RuntimeError(f"DawDreamer opened no plugin at {module}")
    return engine, plugin


def job_vst2_describe(job: dict[str, object]) -> dict[str, object]:
    """Names, defaults, the FXB the host writes, and a text sweep of every knob.

    The text sweep is what makes the VST2 and VST3 sides comparable at all: two
    different hosts, two different plugin formats, but if both render 0.7 on
    "Threshold" as ``-18.00 dB`` then the two normalisations are the same
    normalisation and a value can be carried across by name.
    """
    engine, plugin = _vst2_plugin(str(job["module"]))
    assert engine is not None  # the engine must outlive every call below
    count = plugin.get_plugin_parameter_size()  # type: ignore[attr-defined]
    names = [plugin.get_parameter_name(i) for i in range(count)]  # type: ignore[attr-defined]
    defaults = [float(plugin.get_parameter(i)) for i in range(count)]  # type: ignore[attr-defined]
    positions = [float(x) for x in job["positions"]]  # type: ignore[arg-type]

    bank_path = pathlib.Path(str(job["scratch"])) / "vst2_default.fxb"
    plugin.save_state(str(bank_path))  # type: ignore[attr-defined]
    default_bank = bank_path.read_bytes()

    texts: dict[str, list[str]] = {}
    for index, name in enumerate(names):
        original = defaults[index]
        row: list[str] = []
        for position in positions:
            plugin.set_parameter(index, position)  # type: ignore[attr-defined]
            row.append(str(plugin.get_parameter_text(index)))  # type: ignore[attr-defined]
        plugin.set_parameter(index, original)  # type: ignore[attr-defined]
        texts[name] = row

    return {
        "count": count,
        "names": names,
        "defaults": defaults,
        "patch": [[int(i), float(v)] for i, v in plugin.get_patch()],  # type: ignore[attr-defined]
        "default_bank_hex": default_bank.hex(),
        "positions": positions,
        "texts": texts,
    }


def job_vst2_order_proof(job: dict[str, object]) -> dict[str, object]:
    """Give every parameter a value nothing else has, then read the bank back.

    Distinctive values are the whole trick. If parameter 12 is the only one set
    to 0.12, then finding 0.12 at float slot 12 of the saved bank is not a
    coincidence that a different ordering could also produce.
    """
    engine, plugin = _vst2_plugin(str(job["module"]))
    assert engine is not None  # the engine must outlive every call below
    count = plugin.get_plugin_parameter_size()  # type: ignore[attr-defined]
    stamps = [round(0.01 + index * (0.98 / max(count - 1, 1)), 6) for index in range(count)]
    for index, value in enumerate(stamps):
        plugin.set_parameter(index, value)  # type: ignore[attr-defined]
    bank_path = pathlib.Path(str(job["scratch"])) / "vst2_stamped.fxb"
    plugin.save_state(str(bank_path))  # type: ignore[attr-defined]
    return {
        "count": count,
        "stamps": stamps,
        "readback": [[int(i), float(v)] for i, v in plugin.get_patch()],  # type: ignore[attr-defined]
        "bank_hex": bank_path.read_bytes().hex(),
    }


def job_vst2_bank(job: dict[str, object]) -> dict[str, object]:
    """Play real ``.als`` buffers back through the VST2 and read what they mean.

    Each buffer is dropped into the host's own FXB -- same bank header, same
    program header, only the name field and the floats replaced -- because that
    is the one container DawDreamer will read, and the ``.als`` Buffer is
    exactly that program's payload with the floats byte-swapped.
    """
    engine, plugin = _vst2_plugin(str(job["module"]))
    assert engine is not None  # the engine must outlive every call below
    count = plugin.get_plugin_parameter_size()  # type: ignore[attr-defined]
    names = [plugin.get_parameter_name(i) for i in range(count)]  # type: ignore[attr-defined]
    scratch = pathlib.Path(str(job["scratch"]))
    template = bytes.fromhex(str(job["bank_template_hex"]))

    readings: list[dict[str, object]] = []
    for entry in job["buffers"]:  # type: ignore[union-attr]
        label = str(entry["label"])  # type: ignore[index]
        buffer = bytes.fromhex(str(entry["hex"]))  # type: ignore[index]
        path = scratch / f"vst2_case_{len(readings)}.fxb"
        path.write_bytes(fabf_state.fxb_with_buffer(template, buffer))
        try:
            plugin.load_state(str(path))  # type: ignore[attr-defined]
        except (RuntimeError, ValueError, OSError) as error:
            readings.append({"label": label, "error": f"{type(error).__name__}: {error}"})
            continue
        readings.append(
            {
                "label": label,
                "error": "",
                "raw_values": {names[i]: float(plugin.get_parameter(i)) for i in range(count)},  # type: ignore[attr-defined]
                "texts": {names[i]: str(plugin.get_parameter_text(i)) for i in range(count)},  # type: ignore[attr-defined]
            }
        )
    return {"names": names, "readings": readings}


# --- VST3, through pedalboard -----------------------------------------------


def _descriptor_field(descriptor: object, field: str) -> object:
    value = getattr(descriptor, field, None)
    if isinstance(value, (int, float, str)):
        return value
    return None


def job_vst3_sweep(job: dict[str, object]) -> dict[str, object]:
    """Move every parameter across its range and watch the processor state.

    The default state is captured first and restored after every parameter, so
    a slot that moves during one sweep moved because of that parameter and not
    because of the one before it. Whether the restore actually took is recorded
    rather than assumed -- a parameter that will not go back is a parameter
    whose sweep contaminated everything after it.
    """
    from pedalboard import load_plugin

    plugin = load_plugin(_native(str(job["module"])))
    silence = np.zeros((2, _BLOCK), dtype=np.float32)

    def settle() -> None:
        plugin(silence, SAMPLE_RATE, reset=False)

    def component() -> bytes:
        return unwrap(bytes(plugin.raw_state)).component

    default_component = component()
    default_state = fabf_state.parse_fabf(default_component)
    names = list(plugin.parameters.keys())
    defaults = {name: float(plugin.parameters[name].raw_value) for name in names}
    positions = [float(x) for x in job["positions"]]  # type: ignore[arg-type]

    sweeps: list[dict[str, object]] = []
    for index, python_name in enumerate(names):
        descriptor = plugin.parameters[python_name]
        steps = _descriptor_field(descriptor, "num_steps")
        rows: list[tuple[float, ...]] = []
        texts: list[str] = []
        for position in positions:
            descriptor.raw_value = position
            settle()
            texts.append(str(getattr(descriptor, "string_value", "")))
            rows.append(fabf_state.parse_fabf(component()).values)
        descriptor.raw_value = defaults[python_name]
        settle()
        restored = component() == default_component

        moved: dict[str, tuple[float, ...]] = {}
        for slot in range(len(default_state.values)):
            column = tuple(row[slot] for row in rows)
            if max(column) - min(column) > 0.0:
                moved[str(slot)] = column

        sweeps.append(
            dataclasses.asdict(
                ParameterSweep(
                    name=str(getattr(descriptor, "name", python_name)),
                    python_name=python_name,
                    index=index,
                    declared_steps=steps if isinstance(steps, int) else None,  # type: ignore[arg-type]
                    declared_min=_descriptor_field(descriptor, "min_value"),  # type: ignore[arg-type]
                    declared_max=_descriptor_field(descriptor, "max_value"),  # type: ignore[arg-type]
                    units=str(_descriptor_field(descriptor, "units") or ""),
                    positions=tuple(positions),
                    texts=tuple(texts),
                    moved=moved,
                    restored=restored,
                )
            )
        )

    return {
        "default_component_hex": default_component.hex(),
        "default_raw_values": defaults,
        "parameter_names": names,
        "sweeps": sweeps,
    }


def job_vst3_inject(job: dict[str, object]) -> dict[str, object]:
    """Push whole state blobs in and ask the plugin what it now thinks it is.

    The blob goes in through ``with_component``, which keeps the target's own
    edit-controller state, so a refusal is the plugin refusing the patch rather
    than the host feeding it a controller blob from somewhere else. A render
    follows the injection for the same reason it follows a parameter set: the
    processor is where the state landed and the controller is what gets read.
    """
    from pedalboard import load_plugin

    plugin = load_plugin(_native(str(job["module"])))
    silence = np.zeros((2, _BLOCK), dtype=np.float32)
    default_raw = bytes(plugin.raw_state)
    # Keyed by the plugin's own spelling, not pedalboard's python identifier,
    # because the display name is what the VST2 side answers to and the two
    # readings only line up if both tables use the same key.
    named = {
        str(getattr(plugin.parameters[key], "name", key)): plugin.parameters[key] for key in plugin.parameters
    }
    names = list(named)

    readings: list[dict[str, object]] = []
    for entry in job["states"]:  # type: ignore[union-attr]
        label = str(entry["label"])  # type: ignore[index]
        blob = bytes.fromhex(str(entry["hex"]))  # type: ignore[index]
        plugin.raw_state = default_raw
        plugin(silence, SAMPLE_RATE, reset=False)
        try:
            plugin.raw_state = with_component(default_raw, blob)
        except (RuntimeError, ValueError) as error:
            readings.append(
                dataclasses.asdict(
                    InjectionReading(
                        label=label,
                        injected_len=len(blob),
                        echoed=False,
                        echoed_len=0,
                        echoed_hex="",
                        setter_error=f"{type(error).__name__}: {error}",
                        raw_values={},
                        texts={},
                    )
                )
            )
            continue
        plugin(silence, SAMPLE_RATE, reset=False)
        back = unwrap(bytes(plugin.raw_state)).component
        readings.append(
            dataclasses.asdict(
                InjectionReading(
                    label=label,
                    injected_len=len(blob),
                    echoed=back == blob,
                    echoed_len=len(back),
                    echoed_hex=back.hex(),
                    setter_error="",
                    raw_values={name: float(named[name].raw_value) for name in names},
                    texts={name: str(getattr(named[name], "string_value", "")) for name in names},
                )
            )
        )
    return {
        "parameter_names": names,
        "default_component_hex": unwrap(default_raw).component.hex(),
        "readings": readings,
    }


JOBS = {
    "vst2_describe": job_vst2_describe,
    "vst2_order_proof": job_vst2_order_proof,
    "vst2_bank": job_vst2_bank,
    "vst3_sweep": job_vst3_sweep,
    "vst3_inject": job_vst3_inject,
}


def main() -> int:
    job = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    chatter = io.StringIO()
    with contextlib.redirect_stdout(chatter):
        try:
            payload: dict[str, object] = {"ok": True, "result": JOBS[str(job["kind"])](job)}
        except (RuntimeError, ValueError, OSError, KeyError, AttributeError, ImportError) as error:
            import traceback

            payload = {"ok": False, "error": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc()}
    _emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
