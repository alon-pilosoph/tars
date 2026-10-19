"""A cloud service failing, whichever it is: the voice or the speech to text. One error for all of them, raised from
the one that says why, so a turn lost to a service that's down is a line in the log, not a traceback like a bug."""


class ServiceError(Exception):
    """A cloud service failed: unreachable, too slow, or refusing the request."""
