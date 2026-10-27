"""The one error every state codec, map and converter raises."""


class StateTransformError(ValueError):
    """An invalid or unsupported saved state or state operation."""
