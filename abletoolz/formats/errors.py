"""The one error a format raises when the bytes are not what it reads."""


class FormatError(ValueError):
    """Bytes that are not the format asked for.

    A ``ValueError``, so a caller that caught the old per-format errors that
    way keeps working.
    """
