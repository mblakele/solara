"""
Utility functions and custom JSON provider for the application.
"""

from datetime import datetime, timedelta
from decouple import config
import isodate
from flask.json.provider import DefaultJSONProvider


TIMEZONE = config("TIMEZONE", default="America/Los_Angeles")


class CustomJSONProvider(DefaultJSONProvider):
    """Custom JSON provider handling datetime and timedelta serialization."""

    def __init__(self, app=None) -> None:
        pass

    def default(self, o: object) -> object:
        """Convert datetime and timedelta objects to ISO format strings."""
        try:
            if isinstance(o, datetime):
                return o.isoformat()
            if isinstance(o, timedelta):
                return isodate.duration_isoformat(o)
            iterable = iter(o)
        except TypeError:
            pass
        else:
            return list(iterable)
        return o
