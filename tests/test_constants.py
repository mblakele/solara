"""Tests for shared constants in constants.py.

These pin the values of Tesla-related magic numbers so that any future
change is a deliberate, reviewed act rather than an accidental edit at a
call site.
"""

from __future__ import annotations

from constants import (
    TESLA_CHARGE_AMPS_MAX_DEFAULT,
    TESLA_CHARGE_AMPS_MIN_DEFAULT,
    TESLA_HARD_MAX_AMPS,
    TESLA_HOME_RADIUS_M_DEFAULT,
    TESLA_NOMINAL_VOLTAGE,
    TESLA_TOKEN_REFRESH_INTERVAL_SECS,
)


class TestTeslaConstants:
    """The shared Tesla constants carry the documented values."""

    def test_hard_max_amps(self) -> None:
        """Hard absolute amp ceiling regardless of configuration."""
        assert TESLA_HARD_MAX_AMPS == 48

    def test_charge_amps_defaults(self) -> None:
        """Default min/max charge amps."""
        assert TESLA_CHARGE_AMPS_MIN_DEFAULT == 5
        assert TESLA_CHARGE_AMPS_MAX_DEFAULT == 48

    def test_max_is_at_most_hard_max(self) -> None:
        """The configurable max default must never exceed the hard ceiling."""
        assert TESLA_CHARGE_AMPS_MAX_DEFAULT <= TESLA_HARD_MAX_AMPS

    def test_nominal_voltage(self) -> None:
        """Nominal mains voltage for W/A conversions."""
        assert TESLA_NOMINAL_VOLTAGE == 240

    def test_token_refresh_interval(self) -> None:
        """Proactive refresh window is 7 hours (below the 8 h token life)."""
        assert TESLA_TOKEN_REFRESH_INTERVAL_SECS == 7 * 3600

    def test_home_radius_default(self) -> None:
        """Default home-radius for at-home detection in metres."""
        assert TESLA_HOME_RADIUS_M_DEFAULT == 500.0
