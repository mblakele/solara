"""
Data classes and abstract interfaces for load management.

Pure data structures and ABCs — no business logic, no I/O.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Literal


logger = logging.getLogger(__name__)


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

    Note: REST-based status polling (get_charging_state, is_at_home,
    is_plugged_in, start_charging) is deprecated.  The load manager now relies
    exclusively on Tesla fleet-telemetry MQTT for vehicle state.
    Only ``stop_charging`` and ``set_charge_amps`` remain active
    command methods.
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
    """Configuration for a smart plug load.

    Attributes:
        name: Unique plug identifier.
        accessory_id: Hardware accessory ID for the plug.
        power_watts: Expected power draw in watts when on (None for sentinel
            plugs, which never need a power value).
        priority: Sort priority for load management decisions.
        controller_type: Controller protocol — "homekit" or "vocolinc".
        time_range: Optional (start, end) time window for device eligibility.
        sentinel: When True, the plug is a privileged "sentinel" device —
            tracked for diagnostics but never acted upon. If on, load
            management disables entirely.
    """

    name: str
    accessory_id: str
    power_watts: float | None = None
    priority: int = 0
    controller_type: Literal["homekit", "vocolinc"] = "homekit"
    time_range: tuple[time, time] | None = None
    sentinel: bool = False


@dataclass
class TeslaConfig:
    """Configuration for Tesla vehicle integration.

    Attributes:
        client_id: Tesla Fleet API client ID.
        client_secret: Tesla Fleet API client secret.
        redirect_uri: OAuth redirect URI.
        vehicle_id: Tesla vehicle ID (required for any Tesla integration).
        home_radius_m: Radius in metres around home for at_home detection.
        home_lat: Home latitude (may be None if coords are not configured).
        home_lon: Home longitude (may be None if coords are not configured).
        charge_amps_min: Minimum charge amps before stopping.
        charge_amps_max: Maximum charge amps.
        private_key_path: Path to private key for signing requests.
        time_range: Optional (start, end) time window for device eligibility.
        vehicle_command_proxy_url: Optional proxy URL for vehicle commands.
    """

    client_id: str
    client_secret: str
    redirect_uri: str
    vehicle_id: str
    home_radius_m: float = 500.0
    home_lat: float | None = None
    home_lon: float | None = None
    charge_amps_min: int = 5
    charge_amps_max: int = 48
    private_key_path: str | None = None
    time_range: tuple[time, time] | None = None
    vehicle_command_proxy_url: str | None = None


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
        target_amps: Target amps for "set_amps" actions; None otherwise.
        power_watts: Expected power impact of the action in Watts.
    """

    device_name: str
    action: Literal["turn_on", "turn_off", "set_amps"]
    timestamp: datetime
    data_point_at: datetime
    power_watts: float
    target_amps: int | None = None


@dataclass
class TeslaState:
    """Runtime state of Tesla vehicle."""

    is_charging: bool
    current_amps: int | None
    plugged_in: bool
    at_home: bool


@dataclass(frozen=True)
class TeslaVehicleTelemetry:
    """State received via fleet-telemetry push callbacks.

    Attributes:
        timestamp: When the telemetry data was generated (epoch ms or ISO).
        vehicle_id: Tesla vehicle ID for multi-vehicle safety.
        is_charging: Current charging state.
        current_amps: Current charge current (A).
        plugged_in: Whether vehicle is plugged in.
        at_home: Whether vehicle is at configured home location.
    """

    timestamp: datetime
    vehicle_id: int | str
    is_charging: bool | None = None
    current_amps: int | None = None
    plugged_in: bool | None = None
    at_home: bool | None = None


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
        "plugged_in": state.plugged_in,
        "at_home": state.at_home,
    }


@dataclass
class PlugAction:
    """Record of a plug action taken by the controller."""

    name: str
    on: bool
    timestamp: datetime


@dataclass
class FleetTelemetryProvisionConfig:
    """Parameters for provisioning the fleet-telemetry server via Tesla Fleet API.

    Attributes:
        server_hostname: Public hostname of the self-hosted fleet-telemetry
            server that the Tesla API will push data to.
        ca_file_path: Path to the PEM-encoded CA certificate file.  The file
            contents are read and sent to the Tesla API (not the path itself).
        server_port: TCP port the Tesla API should push telemetry to
            (default: ``4443``).
        chargestate_interval_sec: Minimum publish interval for ChargeState
            telemetry in seconds.
        detailedchargestate_interval_sec: Minimum publish interval for
            DetailedChargeState telemetry in seconds.
        location_interval_sec: Minimum publish interval for Location
            telemetry in seconds.
        chargeamps_interval_sec: Minimum publish interval for ChargeAmps
            telemetry in seconds.
    """

    server_hostname: str
    ca_file_path: Path
    server_port: int = 4443
    chargestate_interval_sec: int = 15
    detailedchargestate_interval_sec: int = 15
    location_interval_sec: int = 120
    chargeamps_interval_sec: int = 15


# === Phase 2 Dataclasses ===

CycleStatus = Literal[
    "ok",
    "dry-run",
    "disabled",
    "no_incomplete_qh",
    "stale_data",
    "previous_qh",
    "waiting_for_fresh_data",
]


@dataclass(frozen=True)
class CycleDiagnostics:
    """Diagnostic snapshot for one load management cycle.

    Attributes:
        gap_wh: Predicted surplus/deficit for the current quarter-hour,
            or None if not available.
        hysteresis_wh: Hysteresis threshold used in load decisions.
        seconds_remaining: Seconds left in the current QH, or None.
        data_point_at: Timestamp of the most recent NBC data point,
            or None if unavailable.
        reason: Human-readable explanation for the cycle status.
        pending_effects_count: Number of pending effects at cycle time.
        tesla_configured: Whether Tesla integration is enabled.
        tesla_state: Current Tesla state snapshot (dict from
            _tesla_state_to_dict), or None.
        tesla_error: Error message from Tesla API, or None.
        tesla_login_url: OAuth login URL if Tesla auth is required,
            or None.
        plugs_configured: List of configured plug names, or None.
        telemetry_registered: Whether the Tesla telemetry webhook is
            registered (True/False) or None when Tesla is not
            configured.
        active_tesla_telemetry: Latest MQTT telemetry snapshot dict, or None.
    """

    gap_wh: float | None = None
    hysteresis_wh: int = 0
    seconds_remaining: int | None = None
    data_point_at: datetime | None = None
    reason: str = ""
    # Optional fields (populated when relevant)
    pending_effects_count: int | None = None
    tesla_configured: bool | None = None
    tesla_state: dict[str, Any] | None = None
    tesla_error: str | None = None
    tesla_login_url: str | None = None
    plugs_configured: list[str] | None = None
    candidates: list[CandidateDetail] | None = None
    sentinel_names: list[str] | None = None
    sentinel_on: bool = False
    telemetry_registered: bool | None = None
    active_tesla_telemetry: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict.

        Returns:
            Dict with all fields. Datetime fields are ISO-8601 strings.
            None values are preserved (Flask's json.dumps handles them).
        """
        return {
            "gap_wh": self.gap_wh,
            "hysteresis_wh": self.hysteresis_wh,
            "seconds_remaining": self.seconds_remaining,
            "data_point_at": (
                self.data_point_at.isoformat()
                if self.data_point_at
                else None
            ),
            "reason": self.reason,
            "pending_effects_count": self.pending_effects_count,
            "tesla_configured": self.tesla_configured,
            "tesla_state": self.tesla_state,
            "tesla_error": self.tesla_error,
            "tesla_login_url": self.tesla_login_url,
            "plugs_configured": self.plugs_configured,
            "candidates": (
                [c.to_dict() for c in self.candidates]
                if self.candidates
                else None
            ),
            "sentinel_names": self.sentinel_names,
            "sentinel_on": self.sentinel_on,
            "telemetry_registered": self.telemetry_registered,
            "active_tesla_telemetry": self.active_tesla_telemetry,
        }


@dataclass(frozen=True)
class CandidateDetailPlug:
    """Candidate detail for a smart plug.

    Attributes:
        name: Plug configuration name.
        power_watts: Expected power draw in watts when on.
        capacity_wh: Total energy capacity in Wh.
        can_toggle: Whether the plug can be turned on/off.
        desired_state: Desired state from load decision, or None.
        actual_state: Current actual state, or None.
    """

    name: str
    power_watts: float
    capacity_wh: float
    can_toggle: bool
    desired_state: bool | None = None
    actual_state: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict.

        Returns:
            Dict with all candidate detail fields.
        """
        return {
            "name": self.name,
            "power_watts": self.power_watts,
            "capacity_wh": self.capacity_wh,
            "can_toggle": self.can_toggle,
            "desired_state": self.desired_state,
            "actual_state": self.actual_state,
        }


@dataclass(frozen=True)
class CandidateDetailTesla:
    """Candidate detail for a Tesla vehicle.

    Attributes:
        name: Tesla name identifier (default: "tesla").
        state_available: Whether state data was successfully retrieved.
        error: Error message from Tesla API, or None.
        is_charging: Current charging state, or None.
        current_amps: Current charging amps, or None.
        plugged_in: Whether vehicle is plugged in, or None.
        at_home: Whether vehicle is at home location, or None.
    """

    name: str = "tesla"
    state_available: bool = False
    error: str | None = None
    is_charging: bool | None = None
    current_amps: int | None = None
    plugged_in: bool | None = None
    at_home: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict.

        Returns:
            Dict with all candidate detail fields.
        """
        return {
            "name": self.name,
            "state_available": self.state_available,
            "error": self.error,
            "is_charging": self.is_charging,
            "current_amps": self.current_amps,
            "plugged_in": self.plugged_in,
            "at_home": self.at_home,
        }


@dataclass(frozen=True)
class CandidateDetail:
    """Candidate detail for a smart plug or Tesla vehicle.

    Attributes:
        device_type: Device type — "plug" or "tesla".
        name: Device or plug configuration name.
        power_watts: Expected power draw in watts when on; None for Tesla.
        capacity_wh: Total energy capacity in Wh.
        can_toggle: Whether the device can be turned on/off.
        desired_state: Desired state from load decision, or None.
        actual_state: Current actual state, or None.
        state_available: Whether state data was successfully retrieved, or None.
        is_charging: Current Tesla charging state, or None.
        current_amps: Current Tesla charging amps, or None.
        plugged_in: Whether Tesla is plugged in, or None.
        at_home: Whether Tesla is at home location, or None.
        reason: Reason the candidate was excluded, or None.
        error: Error message from device API, or None.
    """

    device_type: Literal["plug", "tesla"]
    name: str
    power_watts: float | None
    capacity_wh: float
    can_toggle: bool
    desired_state: bool | None = None
    actual_state: bool | None = None
    state_available: bool | None = None
    is_charging: bool | None = None
    current_amps: int | None = None
    plugged_in: bool | None = None
    at_home: bool | None = None
    reason: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict.

        Returns:
            Dict with all candidate detail fields.
        """
        return {
            "device_type": self.device_type,
            "name": self.name,
            "power_watts": self.power_watts,
            "capacity_wh": self.capacity_wh,
            "can_toggle": self.can_toggle,
            "desired_state": self.desired_state,
            "actual_state": self.actual_state,
            "state_available": self.state_available,
            "is_charging": self.is_charging,
            "current_amps": self.current_amps,
            "plugged_in": self.plugged_in,
            "at_home": self.at_home,
            "reason": self.reason,
            "error": self.error,
        }


@dataclass(frozen=True)
class CycleResult:
    """Result of one load management cycle.

    Attributes:
        status: Cycle status — "ok", "dry-run", "disabled", etc.
        qh: Current quarter-hour identifier (QH1–QH4), or None.
        predicted_wh: Raw NBC prediction for current QH, or None.
        adjusted_wh: Prediction adjusted by pending effects, or None.
        target_wh: Target Wh threshold for the cycle, or None.
        current_wh: Current actual Wh for the QH, or None.
        estimated_wh: Estimated Wh after applying actions, or None.
        actions: List of actions decided by this cycle.
        diagnostics: Diagnostic snapshot, or None for early-exit statuses.
        sleep_hint: Recommended seconds to wait before next cycle.
        sleep_hint_at: ISO-8601 timestamp for when to wake, or None.
        gap_wh: Predicted surplus/deficit for the current QH, or None.
        pending_effects_count: Number of pending effects at cycle time.
        candidates: List of candidate devices considered for action,
            or None if not computed.
    """

    status: CycleStatus
    qh: str | None = None
    predicted_wh: float | None = None
    adjusted_wh: float | None = None
    target_wh: int | None = None
    current_wh: float | None = None
    estimated_wh: float | None = None
    actions: list[PendingEffect] = field(default_factory=list)
    diagnostics: CycleDiagnostics | None = None
    sleep_hint: float = 0.0
    sleep_hint_at: str | None = None
    gap_wh: float | None = None
    pending_effects_count: int | None = None
    candidates: list[CandidateDetail] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict.

        Returns:
            Dict with all cycle result fields. PendingEffect actions
            are converted with datetime fields as ISO-8601 strings.
            Nested dataclasses are converted via their .to_dict() methods.
        """
        def _pe_to_dict(pe: PendingEffect) -> dict[str, Any]:
            """Convert PendingEffect with datetime serialization."""
            return {
                "device_name": pe.device_name,
                "action": pe.action,
                "timestamp": pe.timestamp.isoformat(),
                "data_point_at": pe.data_point_at.isoformat(),
                "power_watts": pe.power_watts,
                "target_amps": pe.target_amps,
            }

        return {
            "status": self.status,
            "qh": self.qh,
            "predicted_wh": self.predicted_wh,
            "adjusted_wh": self.adjusted_wh,
            "target_wh": self.target_wh,
            "current_wh": self.current_wh,
            "estimated_wh": self.estimated_wh,
            "actions": [_pe_to_dict(a) for a in self.actions],
            "diagnostics": (
                self.diagnostics.to_dict() if self.diagnostics else None
            ),
            "sleep_hint": self.sleep_hint,
            "sleep_hint_at": self.sleep_hint_at,
            "gap_wh": self.gap_wh,
            "pending_effects_count": self.pending_effects_count,
            "candidates": (
                [c.to_dict() for c in self.candidates]
                if self.candidates
                else None
            ),
        }


# === Pipeline Context (Direction A) ===


@dataclass
class CycleContext:
    """Intermediate state carried through the run_cycle() pipeline stages.

    Stages read fields and write back new values. Not frozen to allow
    in-place mutation as the pipeline progresses.

    Attributes:
        now: Current time at pipeline start.

        force: If True, bypass stale-data and pending-effects checks.

        # Stage 2 (NBC fetch) outputs
        qh_name: Current quarter-hour identifier (QH1–QH4), or None.
        predicted_wh: Raw NBC prediction for the current QH, or None.
        seconds_remaining: Seconds left in the current QH, or None.
        data_point_at: Timestamp of the most recent NBC data point, or None.

        # Stage 4 (compute gap) outputs
        adjusted_wh: Prediction adjusted by pending effects, or None.
        gap_wh: Predicted surplus (+) or deficit (-) in Wh, or None.

        # Stage 5 (async phase) outputs
        tesla_state: Current Tesla state, or None.
        tesla_error: Error message from Tesla API, or None.
        tesla_login_url: OAuth login URL if re-authentication needed, or None.
        succeeded_effects: Actions that were successfully executed.
        actions: All actions decided by this cycle (including dry-run).
        sentinel_on: True when a sentinel device was found on.
        timings: Wall-clock seconds per pipeline stage, populated during run_cycle().
    """

    # Input
    now: datetime
    force: bool = False

    # Stage 2 output
    qh_name: str | None = None
    predicted_wh: float | None = None
    seconds_remaining: int | None = None
    data_point_at: datetime | None = None
    now_postfetch: datetime | None = None

    # Stage 4 output
    adjusted_wh: float | None = None
    gap_wh: float | None = None

    # Stage 5 output
    tesla_state: TeslaState | None = None
    tesla_error: str | None = None
    tesla_login_url: str | None = None
    succeeded_effects: list[PendingEffect] = field(default_factory=list)
    actions: list[PendingEffect] = field(default_factory=list)
    sentinel_on: bool = False

    # Timing
    timings: dict[str, float] = field(default_factory=dict)


# === Exception Types ===


class TeslaAuthError(Exception):
    """Raised when Tesla credentials are invalid or expired."""
