"""The errors abletoolz raises about sets and their XML."""


class AbletoolzError(Exception):
    """Error running Abletoolz."""


class ElementNotFound(Exception):
    """Element doesnt exist within the xml hierarchy where expected."""


class SetError(Exception):
    """Ableton set parse error."""
