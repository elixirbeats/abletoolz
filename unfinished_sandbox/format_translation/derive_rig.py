"""Derive a plugin's VST2-to-VST3 state transform by asking the plugin, not a human.

The hand-reverse-engineering route to this answer is: stare at hex, guess that a
float is decibels, check one patch, guess again. It works and it does not
generalise -- FabFilter ships a dozen products and every one would cost the same
week.

This does it the other way round. Both formats of the same product are installed,
so both can be interrogated. The VST2 says what its parameters are called and
what a normalised value means as text. The VST3 says which float in its saved
state each parameter owns and what curve carries a normalised value into it.
Names join the two. Nothing is assumed about units, curves, or slot order --
each is measured, and where a measurement is ambiguous the ambiguity is written
into the output rather than resolved by taste.

The result is ``derived_<product>.json``: for every parameter, the state slot it
owns, the transfer model that reproduces its curve, and how far that model
missed. That file is enough to turn a set's VST2 buffer into a VST3
``ProcessorState`` without the plugin present, and :mod:`verify_translation`
then checks that claim against the plugin.

Nothing here loads a plugin in this process. Every load is a child with a
timeout, because these are third-party binaries and one of them hanging must
cost one measurement rather than the run.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import fabf_state
import state_mapper
import transfer as transfer_module

HERE = pathlib.Path(__file__).parent
CHILD = HERE / "derive_child.py"
PYTHON = pathlib.Path(
    os.environ.get(
        "DERIVE_PYTHON",
        r"C:\Users\llama\AppData\Local\Temp\claude\c--repo"
        r"\cff3bc44-ff22-4cb9-a93b-a8f2d1c959a7\scratchpad\hostrig\Scripts\python.exe",
    )
)

BEGIN = "<<<DERIVE"
END = "DERIVE>>>"
CHILD_TIMEOUT_SECONDS = 900

# 65 points, so every position is an exact binary fraction and the endpoints are
# hit dead on. Dense enough that a curve masquerading as a line has nowhere to
# hide, cheap enough that a 51-parameter sweep is seconds.
SWEEP_POINTS = 65


class ChildError(RuntimeError):
    """A measurement process that did not come back with numbers."""


@dataclasses.dataclass(frozen=True, slots=True)
class Product:
    """One plugin in both of its formats."""

    name: str
    vst2: pathlib.Path
    vst3: pathlib.Path


@dataclasses.dataclass(frozen=True, slots=True)
class DerivedParameter:
    """Everything measured about one knob."""

    name: str
    vst2_index: int | None
    vst3_index: int
    slot: int | None
    transfer: transfer_module.Transfer | None
    residual: float
    distinct_values: int
    span: float
    declared_steps: int | None
    units: str
    ambiguous_slots: tuple[int, ...]
    restored: bool
    vst2_texts: tuple[str, ...]
    vst3_texts: tuple[str, ...]
    text_agreement: float
    note: str


@dataclasses.dataclass(frozen=True, slots=True)
class BankOrderProof:
    """Deliverable 1: are the ``.als`` floats in VST2 parameter-index order?"""

    holds: bool
    form: str
    count: int
    offset_in_bank: int
    max_error: float
    mismatches: tuple[tuple[int, float, float], ...]
    note: str


def run_child(kind: str, payload: dict[str, object]) -> dict[str, object]:
    """One plugin load, in a process of its own, with a timeout."""
    job = dict(payload)
    job["kind"] = kind
    handle, name = tempfile.mkstemp(suffix=".job.json", dir=HERE)
    job_path = pathlib.Path(name)
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        json.dump(job, file)
    try:
        completed = subprocess.run(
            [str(PYTHON), str(CHILD), str(job_path)],
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT_SECONDS,
            check=False,
            cwd=str(HERE),
        )
    except subprocess.TimeoutExpired as error:
        raise ChildError(f"{kind}: no answer in {CHILD_TIMEOUT_SECONDS}s") from error
    finally:
        job_path.unlink(missing_ok=True)

    if BEGIN not in completed.stdout:
        tail = (completed.stderr or completed.stdout)[-600:].replace("\n", " ")
        raise ChildError(f"{kind}: child produced no report: {tail}")
    answer = json.loads(completed.stdout.split(BEGIN, 1)[1].split(END, 1)[0])
    if not answer["ok"]:
        raise ChildError(f"{kind}: {answer['error']}\n{answer.get('traceback', '')}")
    return answer["result"]  # type: ignore[no-any-return]


def _positions() -> list[float]:
    return [index / (SWEEP_POINTS - 1) for index in range(SWEEP_POINTS)]


def prove_bank_order(module: pathlib.Path, scratch: pathlib.Path) -> BankOrderProof:
    """Stamp every parameter with a unique value and find those values in the bank.

    The stamps are spread over 0.01..0.99 so no two parameters share one and
    none of them sits on a value the plugin might snap to anyway. What comes
    back is the host's own bank file; the floats inside it are big-endian by the
    VST2 spec and are the same floats, in the same order, that Ableton writes
    into a device's ``<Buffer>`` little-endian.
    """
    result = run_child("vst2_order_proof", {"module": str(module), "scratch": str(scratch)})
    bank = bytes.fromhex(str(result["bank_hex"]))
    stamps = [float(value) for value in result["stamps"]]  # type: ignore[union-attr]

    form = fabf_state.fxb_form(bank)
    if form != "FxBk":
        # A chunk bank has no per-parameter floats at all, so the question this
        # proof asks does not apply to this product -- and neither does the
        # value-by-value translation the rest of the rig builds.
        return BankOrderProof(
            holds=False,
            form=form,
            count=len(stamps),
            offset_in_bank=-1,
            max_error=float("nan"),
            mismatches=(),
            note=(
                f"this plugin saves an opaque {form} chunk rather than an FxBk parameter bank, so its "
                ".als Buffer is the plugin's own serialisation and holds no indexable float array"
            ),
        )

    program = fabf_state.fxb_program(bank)
    if len(program.values) != len(stamps):
        return BankOrderProof(
            holds=False,
            form=form,
            count=len(stamps),
            offset_in_bank=program.offset,
            max_error=float("inf"),
            mismatches=(),
            note=f"bank program holds {len(program.values)} floats, the plugin declares {len(stamps)}",
        )

    # A parameter the plugin quantises will not read back as the exact stamp, so
    # the honest question is not "is it equal" but "is the value at slot i
    # closer to stamp i than to any other stamp".
    mismatches: list[tuple[int, float, float]] = []
    worst = 0.0
    for index, stored in enumerate(program.values):
        nearest = min(range(len(stamps)), key=lambda other: abs(stamps[other] - stored))
        worst = max(worst, abs(stamps[index] - stored))
        if nearest != index:
            mismatches.append((index, stamps[index], float(stored)))

    return BankOrderProof(
        holds=not mismatches,
        form=form,
        count=len(stamps),
        offset_in_bank=program.offset + 28,
        max_error=worst,
        mismatches=tuple(mismatches),
        note=(
            "each stamped value sits at its own parameter index in the bank; the "
            "28 bytes before the floats are the preset-name field, which is what "
            "an .als Buffer also opens with"
        ),
    )


def derive(product: Product, scratch: pathlib.Path) -> dict[str, object]:
    """Measure both formats and join them by parameter name."""
    positions = _positions()

    print(f"  VST2 describe ({product.vst2.name}) ...", flush=True)
    vst2 = run_child(
        "vst2_describe", {"module": str(product.vst2), "scratch": str(scratch), "positions": positions}
    )
    vst2_names: list[str] = [str(name) for name in vst2["names"]]  # type: ignore[union-attr]
    vst2_texts: dict[str, list[str]] = {k: [str(t) for t in v] for k, v in vst2["texts"].items()}  # type: ignore[union-attr]

    print("  VST2 bank-order proof ...", flush=True)
    proof = prove_bank_order(product.vst2, scratch)
    print(f"    form={proof.form} holds={proof.holds} max_error={proof.max_error:.6f} mismatches={len(proof.mismatches)}", flush=True)

    print(f"  VST3 sweep ({product.vst3.name}) ...", flush=True)
    vst3 = run_child("vst3_sweep", {"module": str(product.vst3), "positions": positions})
    default_component = bytes.fromhex(str(vst3["default_component_hex"]))
    default_state = fabf_state.parse_fabf(default_component)

    claimed: dict[int, list[str]] = {}
    parameters: list[DerivedParameter] = []
    for sweep in vst3["sweeps"]:  # type: ignore[union-attr]
        name = str(sweep["name"])
        moved = {int(slot): tuple(float(v) for v in column) for slot, column in sweep["moved"].items()}
        slot: int | None = None
        fit: transfer_module.Fit | None = None
        note = ""
        if len(moved) == 1:
            slot = next(iter(moved))
        elif len(moved) > 1:
            # Several slots moved. The parameter's own slot is the one whose
            # values are a function of position with the widest reach; the rest
            # are knock-on effects of a control this one gates.
            slot = max(moved, key=lambda index: max(moved[index]) - min(moved[index]))
            note = f"{len(moved)} slots moved during this sweep"
        else:
            note = "no state slot moved anywhere in this sweep"

        if slot is not None:
            declared = sweep["declared_steps"]
            fit = transfer_module.fit(
                tuple(positions),
                moved[slot],
                declared_steps=int(declared) if isinstance(declared, int) else None,
            )
            claimed.setdefault(slot, []).append(name)

        mine = vst2_texts.get(name, [])
        agreement = (
            sum(1 for a, b in zip(mine, sweep["texts"]) if a == b) / len(positions) if mine else float("nan")
        )
        parameters.append(
            DerivedParameter(
                name=name,
                vst2_index=vst2_names.index(name) if name in vst2_names else None,
                vst3_index=int(sweep["index"]),
                slot=slot,
                transfer=fit.transfer if fit else None,
                residual=fit.residual if fit else float("nan"),
                distinct_values=fit.distinct_values if fit else 0,
                span=fit.span if fit else 0.0,
                declared_steps=sweep["declared_steps"],  # type: ignore[arg-type]
                units=str(sweep["units"]),
                ambiguous_slots=tuple(sorted(moved)) if len(moved) > 1 else (),
                restored=bool(sweep["restored"]),
                vst2_texts=tuple(mine),
                vst3_texts=tuple(str(t) for t in sweep["texts"]),
                text_agreement=agreement,
                note=note,
            )
        )

    contested = {slot: names for slot, names in claimed.items() if len(names) > 1}
    unattributed = [slot for slot in range(len(default_state.values)) if slot not in claimed]

    return {
        "product": product.name,
        "vst2_module": str(product.vst2),
        "vst3_module": str(product.vst3),
        "sweep_points": SWEEP_POINTS,
        "vst2": {
            "count": vst2["count"],
            "names": vst2_names,
            "defaults": vst2["defaults"],
            "default_bank_hex": vst2["default_bank_hex"],
        },
        "bank_order_proof": dataclasses.asdict(proof),
        "fabf": {
            "version": default_state.version,
            "name": default_state.name,
            "leading": default_state.leading,
            "count": len(default_state.values),
            "trailing": list(default_state.trailing),
            "default_values": list(default_state.values),
            "default_component_hex": default_component.hex(),
            "header_bytes": 12 + len(default_state.name.encode("latin-1")) + 8,
            "total_bytes": len(default_component),
        },
        "parameters": [
            {
                **dataclasses.asdict(parameter),
                "transfer": transfer_module.to_json(parameter.transfer) if parameter.transfer else None,
            }
            for parameter in parameters
        ],
        "contested_slots": {str(slot): names for slot, names in sorted(contested.items())},
        "unattributed_slots": unattributed,
    }


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "map":
        return state_mapper.main(sys.argv[2:])
    parser = argparse.ArgumentParser(description="Derive a plugin's VST2 -> VST3 state transform empirically",
                                     epilog="For a general single-plugin map: derive_rig.py map --help")
    parser.add_argument("--product", required=True, help="the name the derived table is filed under")
    parser.add_argument("--vst2", required=True, type=pathlib.Path)
    parser.add_argument("--vst3", required=True, type=pathlib.Path)
    parser.add_argument("--out", default="", help="where the table goes; defaults to derived_<product>.json")
    arguments = parser.parse_args()

    product = Product(name=arguments.product, vst2=arguments.vst2, vst3=arguments.vst3)
    for path in (product.vst2, product.vst3):
        if not path.exists():
            print(f"missing: {path}", file=sys.stderr)
            return 2

    scratch = HERE / "scratch"
    scratch.mkdir(exist_ok=True)
    print(f"deriving {product.name}", flush=True)
    table = derive(product, scratch)

    slug = product.name.lower().replace(" ", "_").replace("-", "")
    out = pathlib.Path(arguments.out) if arguments.out else HERE / f"derived_{slug}.json"
    out.write_text(json.dumps(table, indent=1), encoding="utf-8")

    mapped = sum(1 for row in table["parameters"] if row["slot"] is not None)  # type: ignore[index,union-attr]
    print(
        f"\n{mapped}/{len(table['parameters'])} parameters mapped, "  # type: ignore[arg-type]
        f"{len(table['unattributed_slots'])} slots unattributed, "  # type: ignore[arg-type]
        f"{len(table['contested_slots'])} contested"  # type: ignore[arg-type]
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
