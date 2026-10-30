"""Explicit model-to-model conversions that consume versioned plugin maps.

A converter tells two things apart. A patch it cannot read -- a bank of the
wrong size, a value that is no number -- raises, since there is nothing to
convert. A setting the target has no place for is a limitation: called
directly, the conversion functions refuse it, so a caller can tell; the
registered converters a plan runs carry everything else and say what they left
out, through :func:`limitation`.
"""

from collections.abc import Sequence

from abletoolz.plugins.state.errors import StateTransformError


def limitation(notices: list[str] | None, message: str) -> None:
    """A setting the conversion cannot carry: refused without ``notices``, or said there and left out."""
    if notices is None:
        raise StateTransformError(message)
    notices.append(message)


def interp(x: float, points: Sequence[Sequence[float]]) -> float:
    """Piecewise linear through ``points`` (sorted by x), held flat past either end."""
    if x <= points[0][0]:
        return float(points[0][1])
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if x <= x1:
            return float(y0 + (x - x0) / (x1 - x0) * (y1 - y0))
    return float(points[-1][1])
