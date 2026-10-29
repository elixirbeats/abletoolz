"""Explicit model-to-model conversions that consume versioned plugin maps.

A converter tells two things apart. A patch it cannot read -- a bank of the
wrong size, a value that is no number -- raises, since there is nothing to
convert. A setting the target has no place for is a limitation: called
directly, the conversion functions refuse it, so a caller can tell; the
registered converters a plan runs carry everything else and say what they left
out, through :func:`limitation`.
"""

from abletoolz.plugins.state.errors import StateTransformError


def limitation(notices: list[str] | None, message: str) -> None:
    """A setting the conversion cannot carry: refused without ``notices``, or said there and left out."""
    if notices is None:
        raise StateTransformError(message)
    notices.append(message)
