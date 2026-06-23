"""Clock abstraction for testable time-dependent code.

Provides a ``Clock`` protocol (structural subtyping) with two concrete
implementations:

* ``RealClock`` — returns ``datetime.now(timezone.utc)`` in production.
* ``FakeClock`` — returns a fixed datetime for deterministic tests.

Usage::

    from clock import Clock, RealClock

    def do_something(clock: Clock | None = None) -> None:
        clock = clock or RealClock()
        now = clock.now()
        ...
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """Abstract interface for getting the current time.

    Any object with a ``now()`` method returning a timezone-aware
    :class:`~datetime.datetime` satisfies this protocol — no inheritance
    required.
    """

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""


class RealClock:
    """Production clock — returns the real wall-clock time in UTC."""

    def now(self) -> datetime:
        """Return the current UTC time via :func:`datetime.now`.

        Returns:
            A timezone-aware :class:`~datetime.datetime` with ``tzinfo=timezone.utc``.
        """
        return datetime.now(timezone.utc)


class FakeClock:
    """Test clock — returns a fixed datetime for deterministic tests.

    Supports :meth:`advance` to shift the fixed time forward or backward,
    enabling time-progression scenarios without sleeping.
    """

    def __init__(self, fixed: datetime | None = None) -> None:
        """Initialize the clock with an optional fixed time.

        Args:
            fixed: The datetime to return from :meth:`now`. If ``None``,
                defaults to ``2026-01-01T00:00:00Z``.
        """
        if fixed is None:
            fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        if fixed.tzinfo is None:
            fixed = fixed.replace(tzinfo=timezone.utc)
        self._fixed = fixed

    def now(self) -> datetime:
        """Return the fixed datetime.

        Returns:
            The same datetime on every call (until :meth:`advance` is called).
        """
        return self._fixed

    def advance(self, seconds: int) -> None:
        """Advance the fixed time by *seconds*.

        Positive values move time forward; negative values move it backward.

        Args:
            seconds: Number of seconds to advance (or retreat, if negative).
        """
        self._fixed += timedelta(seconds=seconds)
