"""What the plugin does to a number between the knob and the saved state.

A VST3 parameter is normalised: the host always hands the plugin a value in
0..1. What FabFilter then writes into its ``FabF`` state is the same parameter
in its own units, and the shape of that conversion is different for almost
every knob. Threshold is a straight line in decibels. A crossover frequency is a
straight line in *log2 hertz*, which is a line here and an exponential in hertz.
Style is an index into a list of names. Attack, unexpectedly, is stored still
normalised, so its curve lives entirely inside the plugin and the state never
sees milliseconds at all.

Guessing which is which is exactly the mistake this rig exists to avoid. So
nothing here proposes a curve: the caller sweeps the real parameter on the real
plugin, hands over the (normalised in, state float out) pairs, and this module
picks the simplest model that reproduces them and reports how far off it is.
Three models, in order of how much they claim:

* **linear** -- two coefficients. Covers dB, log2 Hz, ratios, and every
  identity mapping, which between them is most of the plugin.
* **step** -- a lookup table over the parameter's own step count, for enums and
  switches. The index rule is ``floor(x * (steps - 1) + 0.5)``, and it is
  *checked* against a denser sweep rather than assumed. That is round-half-up,
  not Python's round, and the difference is not pedantry: a two-step switch at
  exactly 0.5 reads as on, and Python's banker's rounding would call it off.
  The 65-point sweep lands on every one of these boundaries exactly, which is
  how the tie-break was measured rather than picked.
* **table** -- piecewise linear through the measured points, for anything the
  first two cannot reproduce. It is the honest answer, not a fallback: it says
  the curve was measured and not recognised.

Residual is always reported in the state float's own units, as a maximum
absolute error over the sweep, because a mean would hide the one parameter that
is wrong at one end.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np

# A linear fit is accepted when it reproduces every measured point this closely,
# relative to the span the parameter covers. float32 storage alone costs about
# 1e-7 relative, so this leaves three decimal digits of headroom for a genuine
# curve to show itself.
_LINEAR_TOLERANCE = 1e-4

# Above this many distinct output values a parameter is treated as continuous
# even if the host declares a step count, because a "stepped" parameter with
# hundreds of steps is a quantised continuum and a table of it is useless.
_MAX_STEPS = 256


@dataclasses.dataclass(frozen=True, slots=True)
class LinearTransfer:
    """``native = intercept + slope * normalised``."""

    intercept: float
    slope: float

    kind: str = "linear"

    def apply(self, normalised: float) -> float:
        return self.intercept + self.slope * normalised


@dataclasses.dataclass(frozen=True, slots=True)
class StepTransfer:
    """``native = values[floor(normalised * (len(values) - 1) + 0.5)]``."""

    values: tuple[float, ...]

    kind: str = "step"

    def apply(self, normalised: float) -> float:
        last = len(self.values) - 1
        index = math.floor(min(max(normalised, 0.0), 1.0) * last + 0.5)
        return self.values[min(index, last)]


@dataclasses.dataclass(frozen=True, slots=True)
class TableTransfer:
    """Piecewise linear through the measured sweep, clamped at both ends."""

    positions: tuple[float, ...]
    values: tuple[float, ...]

    kind: str = "table"

    def apply(self, normalised: float) -> float:
        return float(np.interp(normalised, self.positions, self.values))


Transfer = LinearTransfer | StepTransfer | TableTransfer


@dataclasses.dataclass(frozen=True, slots=True)
class Fit:
    """A chosen model and how badly it missed."""

    transfer: Transfer
    residual: float
    distinct_values: int
    span: float


def _residual(transfer: Transfer, positions: np.ndarray, values: np.ndarray) -> float:
    predicted = np.array([transfer.apply(float(x)) for x in positions])
    return float(np.max(np.abs(predicted - values)))


def fit(
    positions: tuple[float, ...],
    values: tuple[float, ...],
    declared_steps: int | None = None,
) -> Fit:
    """Pick the simplest model that reproduces this sweep.

    ``declared_steps`` is the host's own step count for the parameter. It is
    used to *propose* a step model, never to conclude one: the model still has
    to reproduce the dense sweep before it is accepted.
    """
    if len(positions) != len(values):
        raise ValueError(f"{len(positions)} positions against {len(values)} values")
    if len(positions) < 2:
        raise ValueError("a sweep of fewer than two points says nothing about a curve")

    grid = np.asarray(positions, dtype=np.float64)
    measured = np.asarray(values, dtype=np.float64)
    distinct = int(np.unique(np.round(measured, 6)).size)
    span = float(np.max(measured) - np.min(measured))
    scale = max(span, 1.0)

    steps = declared_steps if declared_steps is not None and 1 < declared_steps <= _MAX_STEPS else None
    if steps is not None:
        # Nearest measured sample, not an interpolation: a step's value is only
        # ever one of the values actually observed, and interpolating between
        # two steps would invent a level the plugin never produces.
        centres = [int(np.argmin(np.abs(grid - k / (steps - 1)))) for k in range(steps)]
        table = StepTransfer(values=tuple(float(measured[index]) for index in centres))
        error = _residual(table, grid, measured)
        if error <= _LINEAR_TOLERANCE * scale:
            return Fit(transfer=table, residual=error, distinct_values=distinct, span=span)

    # Anchored on the two endpoints rather than least-squares fitted. Both
    # coefficients are then measurements -- what the plugin wrote at 0 and what
    # it wrote at 1 -- instead of a regression, and the difference is not
    # cosmetic: least squares puts 2e-16 where the plugin puts a clean zero, and
    # a state built from that is no longer byte-identical to one the plugin
    # would have written. The residual over the other 63 points still decides
    # whether a line is the right shape at all.
    intercept = measured[0]
    slope = (measured[-1] - measured[0]) / (grid[-1] - grid[0])
    line = LinearTransfer(intercept=float(intercept), slope=float(slope))
    error = _residual(line, grid, measured)
    if error <= _LINEAR_TOLERANCE * scale:
        return Fit(transfer=line, residual=error, distinct_values=distinct, span=span)

    curve = TableTransfer(positions=tuple(float(x) for x in grid), values=tuple(float(v) for v in measured))
    return Fit(transfer=curve, residual=_residual(curve, grid, measured), distinct_values=distinct, span=span)


def to_json(transfer: Transfer) -> dict[str, object]:
    """A model as plain data, for the derived table on disk."""
    return dataclasses.asdict(transfer)


def from_json(payload: dict[str, object]) -> Transfer:
    """A model back off disk."""
    kind = payload["kind"]
    if kind == "linear":
        return LinearTransfer(intercept=float(payload["intercept"]), slope=float(payload["slope"]))  # type: ignore[arg-type]
    if kind == "step":
        return StepTransfer(values=tuple(float(v) for v in payload["values"]))  # type: ignore[union-attr]
    if kind == "table":
        return TableTransfer(
            positions=tuple(float(v) for v in payload["positions"]),  # type: ignore[union-attr]
            values=tuple(float(v) for v in payload["values"]),  # type: ignore[union-attr]
        )
    raise ValueError(f"{kind!r} is not a transfer model this module writes")
