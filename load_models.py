"""
Data classes and abstract interfaces for load management.

Pure data structures and ABCs — no business logic, no I/O.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Any, Literal


# === Abstract Controller Interfaces ===


class AbstractPlugController(ABC):
    """Interface for smart plug controllers.

    Implementations may use aiohomekit, Home Assistant, or any other
    protocol to control physical plugs. The stub implementation provides
    in-memory state tracking for testing without real devices.
    """

    plugs: "dict[str, PlugConfig]"

    @abstractmethod
    async def get_state(self, name: str) -> bool | None:
        """Query plug on/off state.

        Args:
            name: Plug configuration name (e.g., "water_heater").

        Returns:
            True if on, False if off, None on error.
        """

    @abstractmethod
    async def set_state(self, name: str, on: bool) -> bool:
        """Turn plug on or off.

        Args:
            name: Plug configuration name.
            on: Desired state.

        Returns:
            True on success, False on failure.
        """


class AbstractTeslaController(ABC):
    """Interface for Tesla vehicle charging controllers.

    Implementations may use tesla-fleet-api or a mock for testing.
    All methods are async to allow non-blocking API calls.
    """

    @abstractmethod
    async def authenticate(self) -> None:
        """Perform OAuth flow or load cached token."""

    @abstractmethod
    async def is_at_home(self) -> bool:
        """Check vehicle GPS against home lat/lon using haversine distance."""

    @abstractmethod
    async def is_plugged_in(self) -> bool:
        """Check chargeState indicates plugged in (Charging or Connected)."""

    @abstractmethod
    async def get_charge_limit_pct(self) -> float | None:
        """Get target SOC percentage.

        Returns:
            Target SOC as 0-100, or None if at limit or error.
        """

    @abstractmethod
    async def is_at_charge_limit(self) -> bool:
        """Check if vehicle has reached its charge limit (saturated load)."""

    @abstractmethod
    async def start_charging(self) -> bool:
        """Send charge_start command.

        Returns:
            True on success, False on failure.
        """

    @abstractmethod
    async def stop_charging(self) -> bool:
        """Send charge_stop command.

        Returns:
            True on success, False on failure.
        """

    @abstractmethod
    async def set_charge_amps(self, amps: int) -> bool:
        """Set charging amps within [min, max] range.

        Args:
            amps: Desired charge amps.

        Returns:
            True on success, False on failure.
        """

    @abstractmethod
    async def get_charging_state(self) -> TeslaState | None:
        """Return full state: charging_bool, current_amps, soc_pct, plugged_in."""

    def reset_session(self) -> None:
        """Reset cached HTTP session before a new asyncio.run() call.

        asyncio.run() creates a fresh event loop each invocation. Cached
        aiohttp sessions are bound to the loop they were created in and
        become unusable once that loop closes. Override in subclasses that
        cache sessions.
        """

    def get_login_url(self, state: str = "solara-login") -> str:
        """Generate the Tesla OAuth authorization URL.

        Args:
            state: OAuth state parameter for CSRF protection.

        Returns:
            URL to open in browser for user authorization, or a placeholder
            if not implemented by subclass.
        """
        return (
            "https://auth.tesla.com/oauth2/v3/authorize"
            f"?response_type=code&prompt=consent&state={state}"
        )

    async def close(self) -> None:
        """Release resources held by this controller.

        Override in subclasses that hold resources (e.g., aiohttp sessions).
        """


@dataclass
class PlugConfig:
    """Configuration for a smart plug load."""

    name: str
    accessory_id: str
    power_watts: float
    priority: int = 0
    controller_type: Literal["homekit", "vocolinc"] = "homekit"
    time_range: tuple[time, time] | None = None


@dataclass
class TeslaConfig:
    """Configuration for Tesla vehicle integration."""

    client_id: str
    client_secret: str
    redirect_uri: str
    vehicle_id: str
    home_lat: float
    home_lon: float
    home_radius_m: float
    charge_amps_min: int = 5
    charge_amps_max: int = 48
    private_key_path: str | None = None
    time_range: tuple[time, time] | None = None


@dataclass
class DeviceState:
    """Runtime state of a managed device."""

    name: str
    last_toggle: datetime | None = None
    desired_state: bool | None = None
    actual_state: bool | None = None
    current_amps: int | None = None


@dataclass
class PendingEffect:
    """A pending action with expected power impact.

    Attributes:
        device_name: Name of the device the action targets.
        action: The action type — "turn_on", "turn_off", or "set_amps".
        timestamp: Wall clock time when the effect was created.
        data_point_at: The NBC data-point-at timestamp at creation time.
            Used alongside timestamp for dual-pruning checks.
        power_delta_wh: Expected watt-hour impact of the action.
        target_amps: Target amps for "set_amps" actions; None otherwise.
    """

    device_name: str
    action: Literal["turn_on", "turn_off", "set_amps"]
    timestamp: datetime
    data_point_at: datetime
    power_delta_wh: float
    target_amps: int | None = None  # Target amps for set_amps actions


@dataclass
class TeslaState:
    """Runtime state of Tesla vehicle."""

    is_charging: bool
    current_amps: int | None
    soc_percent: float | None
    plugged_in: bool
    at_home: bool
    at_charge_limit: bool


def _tesla_state_to_dict(
    state: TeslaState | None,
) -> dict[str, Any] | None:
    """Convert a TeslaState to a plain dict for diagnostics.

    Args:
        state: Tesla state instance, or None if unavailable.

    Returns:
        Dict with all Tesla state fields, or None if state is None.
    """
    if state is None:
        return None
    return {
        "is_charging": state.is_charging,
        "current_amps": state.current_amps,
        "soc_percent": state.soc_percent,
        "plugged_in": state.plugged_in,
        "at_home": state.at_home,
        "at_charge_limit": state.at_charge_limit,
    }


@dataclass
class PlugAction:
    """Record of a plug action taken by the controller."""

    name: str
    on: bool
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class TeslaAuthError(Exception):
    """Raised when Tesla credentials are invalid or expired."""
