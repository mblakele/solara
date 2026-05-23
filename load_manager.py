"""
Load management for solar self-consumption optimization.

LoadManager orchestrator, config loading functions, time-range parsing,
and TeslaAuthError exception. Re-exports all public symbols from submodules
for backward compatibility.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone
import logging
import threading
import time as _time_mod

# Third-party imports.
from typing import Any, Callable

import pytz

# First-party local imports — LoadManagerConfig references their types, so they must
# appear before the dataclass definition below.
import device_config

from config import cfg as _cfg
from config_loader import (  # noqa: F401
    _parse_load_manage_enabled,
)

from config_loader import (  # runtime imports — also re-exported via __all__
    load_plugs_from_file,
    load_tesla_config,
    load_vocolinc_credentials,
    load_vocolinc_plugs_from_file,
)

from load_controllers import (  # noqa: F401
    CompositePlugController,
    PAIRINGS_FILE,
    PlugController,
    RealPlugController,
    RealTeslaController,
    TESLA_TOKENS_FILE,
    TeslaController,
    VocolincPlugController,
    load_tesla_tokens,
    pair_homekit_accessory,
    remove_tesla_tokens,
    save_tesla_tokens,
    tesla_auth_cli,
)

from load_models import (  # noqa: F401
    AbstractPlugController,
    AbstractTeslaController,
    DeviceState,
    PendingEffect,
    PlugAction,
    PlugConfig,
    TeslaAuthError,
    TeslaConfig,
    TeslaState,
    _tesla_state_to_dict,
)

from load_nbc import NBCPeriod, NBCReader, StateTracker, GapMinder, DecideContext  # noqa: F401

from metrics import EnergyCache

from vocolinc import VOCOlinc  # pylint: disable=W0611

# Re-exported for test patching — pylint complains about unused import but it's
# intentionally exposed so tests can monkey-patch vocolinc.VOCOlinc.


@dataclass(frozen=True)
class LoadManagerConfig:
    """Configuration for LoadManager initialization.

    Groups related parameters to reduce __init__ argument count from 10
    down to a single config object. All fields have sensible defaults so
    production code can pass an empty LoadManagerConfig() and rely on env
    vars / devices.json for the rest.

    Attributes:
        metrics_fetch: Callable that returns fresh metrics data, or None to load from env.
        energy_cache: Shared EnergyCache for per-second samples; creates default if None.
        plug_ctrl: Plug controller instance; auto-detected from env when None.
        tesla_ctrl: Tesla controller instance; loaded from config when None.
        engine: GapMinder instance; created with defaults if None.
        target_wh: Target Wh per QH (defaults to smartmeter.target_wh in devices.json).
        nbc_device: Device name for NBC readings (defaults to smartmeter.device in devices.json).
        enabled: Whether load management is active. Accepts True/False, or a time range tuple.
            Defaults to LOAD_MANAGE_ENABLED env var parsed via _parse_load_manage_enabled.
        dry_run: If True, log actions without executing them (defaults to LOAD_MANAGE_DRY_RUN).
        config_interval_secs: Target interval between cycles in seconds (default 30).
    """

    metrics_fetch: Callable[[], dict[str, Any] | None] | None = None
    energy_cache: EnergyCache | None = None
    plug_ctrl: AbstractPlugController | None = None
    tesla_ctrl: AbstractTeslaController | None = None
    engine: GapMinder | None = None
    target_wh: int | None = None
    nbc_device: str | None = None
    enabled: bool | tuple[Any, Any] | None = None  # type: ignore[type-arg]
    dry_run: bool | None = None
    config_interval_secs: int = 30

# Re-exported for backward compatibility.


logger = logging.getLogger(__name__)



# === LoadManager Orchestrator ===


class LoadManager:
    """Top-level orchestrator that runs the load management loop.

    Wires together NBCReader+Cache, StateTracker, controllers, and GapMinder
    to execute one load management cycle per call. Thread-safe via internal lock.

    Accepts either a single LoadManagerConfig object or individual keyword
    arguments for backward compatibility with existing callers.

    Usage::

        # New style — single config object
        mgr = LoadManager(LoadManagerConfig(dry_run=True))

        # Legacy style — individual kwargs (still supported)
        mgr = LoadManager(dry_run=True, config_interval_secs=60)

    """

    def __init__(  # noqa: C901
        self,
        config: LoadManagerConfig | None = None,
        **kwargs: Any,  # type: ignore[assignment]
    ) -> None:

        """Initialize LoadManager with optional dependency injection.

        Accepts a single `LoadManagerConfig` object containing all settings,
        or individual keyword arguments for backward compatibility with existing
        callers.

        Usage::

            # New style — single config object (recommended)
            mgr = LoadManager(LoadManagerConfig(dry_run=True))

            # Legacy style — individual kwargs (still supported)
            mgr = LoadManager(dry_run=True, config_interval_secs=60)

        Args:
            config: Optional LoadManagerConfig dataclass containing all settings.
                When provided, overrides individual kwargs entirely.

        **Backward-compatible keyword arguments** (deprecated in favour of
        ``LoadManagerConfig``):

            metrics_fetch: Callable that returns fresh metrics data.
            plug_ctrl: Plug controller instance. If None, selected via
                LOAD_PLUG_CONTROLLER env var (real or stub).
            tesla_ctrl: Tesla controller instance. If None, loaded from env.
            engine: GapMinder instance. If None, creates default.
            target_wh: Target Wh per QH. Defaults to smartmeter.target_wh in
                devices.json.
            nbc_device: Device name for NBC readings. Defaults to
                smartmeter.device in devices.json.
            enabled: Whether load management is active. Accepts True/False, or a
                time range string like "06:45-15:00" (24-hr clock). When a time
                range is given, load management is only active during that window
                (inclusive start, exclusive end) in the device timezone. Defaults to
                LOAD_MANAGE_ENABLED env var.
            dry_run: If True, log actions without executing them. Defaults to
                LOAD_MANAGE_DRY_RUN env var.
            config_interval_secs: Target interval between cycles in seconds.
                Used by adaptive sleep to clamp min/max sleep durations.
                Defaults to 30 seconds.
        """

        # ── Resolve config from either a LoadManagerConfig object or legacy kwargs
        if config is not None:
            # Primary path — use the LoadManagerConfig object directly.
            metrics_fetch = config.metrics_fetch  # type: ignore[assignment]
            energy_cache = config.energy_cache
            plug_ctrl = config.plug_ctrl  # type: ignore[assignment]
            tesla_ctrl = config.tesla_ctrl  # type: ignore[assignment]
            engine = config.engine  # type: ignore[assignment]
            target_wh = config.target_wh
            nbc_device = config.nbc_device  # type: ignore[assignment]
            enabled = config.enabled  # type: ignore[assignment]
            dry_run = config.dry_run
            interval_secs = config.config_interval_secs  # type: ignore[assignment]
        else:
            # Legacy path — accept individual kwargs for backward compatibility.
            metrics_fetch = kwargs.get("metrics_fetch")  # type: ignore[assignment]
            energy_cache = kwargs.get("energy_cache")  # type: ignore[assignment]
            plug_ctrl = kwargs.get("plug_ctrl")  # type: ignore[assignment]
            tesla_ctrl = kwargs.get("tesla_ctrl")  # type: ignore[assignment]
            engine = kwargs.get("engine")  # type: ignore[assignment]
            target_wh = kwargs.get("target_wh", None)  # type: ignore[assignment]
            nbc_device = kwargs.get("nbc_device", None)  # type: ignore[assignment]
            enabled = kwargs.get("enabled")  # type: ignore[assignment]
            dry_run = kwargs.get("dry_run", None)  # type: ignore[assignment]
            interval_secs = kwargs.get("config_interval_secs", 30)

        self._lock = threading.Lock()
        self.plug_ctrl: AbstractPlugController
        self.tesla_ctrl: AbstractTeslaController | None
        self.plugs: dict[str, PlugConfig]

        if target_wh is None:
            target_wh = device_config.get_target_wh()
        self.target_wh = target_wh

        if nbc_device is None:
            nbc_device = device_config.get_smartmeter_device()
        self.nbc_device = nbc_device

        if isinstance(enabled, str):
            try:
                enabled = _parse_load_manage_enabled(enabled)
            except ValueError as e:
                logger.error("%s. Disabling load management.", e)
                enabled = False
        elif enabled is None:
            enabled = LoadManager._resolve_enabled()
        self.enabled: bool | tuple[time, time] = enabled
        logger.info("LoadManager %s", self.enabled)

        if dry_run is None:
            dry_run = _cfg.dry_run
        self.dry_run = dry_run

        self.config_interval_secs = interval_secs  # type: ignore[assignment]

        hysteresis_wh = int(abs(target_wh) / 3)
        self.tesla_config = load_tesla_config()
        tesla_config = self.tesla_config
        if engine is not None:
            self.engine = engine
        else:
            self.engine = GapMinder(
                hysteresis_wh=hysteresis_wh,
                charge_amps_min=tesla_config.charge_amps_min if tesla_config else 5,
                charge_amps_max=tesla_config.charge_amps_max if tesla_config else 48,
            )
        self.state = StateTracker()

        logger.debug("LoadManager %s", plug_ctrl)
        if plug_ctrl is not None:
            self.plug_ctrl = plug_ctrl
        else:
            plugs_from_file = load_plugs_from_file()
            vocolinc_plugs = load_vocolinc_plugs_from_file()
            has_homekit_plugs = bool(plugs_from_file)
            has_vocolinc_plugs = bool(vocolinc_plugs)

            # Auto-detect: if both types exist, use composite controller
            if has_homekit_plugs and has_vocolinc_plugs:
                controller_type = _cfg.load_plug_controller
                hk_ctrl = (
                    RealPlugController(plugs_from_file)
                    if controller_type == "real"
                    else PlugController(plugs_from_file)
                )
                vc_creds = load_vocolinc_credentials()
                vc_ctrl = VocolincPlugController(
                    vocolinc_plugs,
                    username=vc_creds[0] if vc_creds else None,
                    password=vc_creds[1] if vc_creds else None,
                )
                self.plug_ctrl = CompositePlugController(hk_ctrl, vc_ctrl)
            elif has_vocolinc_plugs:
                vc_creds = load_vocolinc_credentials()
                self.plug_ctrl = VocolincPlugController(
                    vocolinc_plugs,
                    username=vc_creds[0] if vc_creds else None,
                    password=vc_creds[1] if vc_creds else None,
                )
            else:
                controller_type = _cfg.load_plug_controller
                if controller_type == "real":
                    self.plug_ctrl = RealPlugController(plugs_from_file)
                else:
                    self.plug_ctrl = PlugController(plugs_from_file)

        self.plugs = self.plug_ctrl.plugs  # type: ignore[attr-defined]

        # Collect sentinel plug names for fast lookup during cycles.
        self.sentinel_names: frozenset[str] = frozenset(
            name for name, plug in self.plugs.items() if plug.sentinel
        )

        if tesla_ctrl is not None:
            self.tesla_ctrl = tesla_ctrl
        elif tesla_config is not None:
            controller_type = _cfg.load_tesla_controller
            if controller_type == "real":
                self.tesla_ctrl = RealTeslaController(tesla_config)
            else:
                self.tesla_ctrl = TeslaController(tesla_config)
        else:
            self.tesla_ctrl = None

        self.nbc_reader = NBCReader(
            energy_cache=energy_cache,
        )

        # Wire the metrics fetch callable into the NBCReader so that
        # run_cycle(force=True) can trigger a fresh API fetch.
        if metrics_fetch is not None:
            self.nbc_reader._metrics_fetch = metrics_fetch  # noqa: SLF001

    @staticmethod
    def _resolve_enabled() -> bool | tuple[time, time]:
        """Resolve LOAD_MANAGE_ENABLED from config.

        Reads the raw env var value and parses it using
        _parse_load_manage_enabled. On parse error, logs the issue
        and returns False (disabled).

        Returns:
            Parsed enabled value: True, False, or a time range tuple.
        """
        raw_value = _cfg.load_manage_enabled  # type: ignore[assignment]
        try:
            return _parse_load_manage_enabled(raw_value)
        except ValueError as e:
            logger.error("%s. Disabling load management.", e)
            return False

    def is_enabled_at(self, now: datetime) -> bool:
        """Check if load management is enabled at the given moment.

        Evaluates the enabled setting against the current time in the device
        timezone. If enabled is True, always returns True. If False, always
        returns False. If a time range tuple, checks whether now falls within
        [start, end) — inclusive start, exclusive end.

        Args:
            now: The current moment (timezone-aware or naive).

        Returns:
            True if load management should be active at this time.
        """
        if isinstance(self.enabled, bool):
            return self.enabled

        start_time, end_time = self.enabled
        tz_name = device_config.get_timezone()
        try:
            local_tz = pytz.timezone(tz_name)
        except pytz.exceptions.UnknownTimeZoneError:
            local_tz = pytz.timezone("America/Los_Angeles")

        if now.tzinfo is None:
            now_local = local_tz.localize(now)
        else:
            now_local = now.astimezone(local_tz)

        current_time = now_local.time()
        return start_time <= current_time < end_time

    def _disabled_reason(self, source: str) -> str:
        """Return a human-readable reason string for disabled status.

        Returns:
            "disabled" when enabled is False, or
            "outside_time_range(HH:MM-HH:MM)" when outside the configured window.
        """
        if isinstance(self.enabled, tuple):
            start_str = self.enabled[0].strftime("%H:%M")
            end_str = self.enabled[1].strftime("%H:%M")
            return f"[{source}] outside_time_range({start_str}-{end_str})"
        return "disabled"

    def _is_device_in_time_range(
        self, now: datetime, time_range: tuple[time, time] | None
    ) -> bool:
        """Check if the current time is within a configured time range.

        Args:
            now: current datetime.
            time_range: (start, end) tuple from config, or None (always in range).

        Returns:
            True if the device has no time restriction or current time falls
            within [start, end). False if outside the configured window.
        """
        if time_range is None:
            return True
        start_time, end_time = time_range
        tz_name = device_config.get_timezone()
        try:
            local_tz = pytz.timezone(tz_name)
        except pytz.exceptions.UnknownTimeZoneError:
            local_tz = pytz.timezone("America/Los_Angeles")

        now_local = now.astimezone(local_tz)
        current_time = now_local.time()

        in_range = start_time <= current_time < end_time
        return in_range

    def _build_candidate_details(
        self,
        now: datetime,
        seconds_remaining: int,
        tesla_state: TeslaState | None,
        tesla_error: str | None,
        tesla_configured: bool,
    ) -> list[dict[str, Any]]:
        """Build per-device diagnostics for visibility into decisions.

        Args:
            now: current datetime.
            seconds_remaining: Seconds left in current quarter-hour.
            tesla_state: Tesla state if available.
            tesla_error: Error message if Tesla state fetch failed.
            tesla_configured: Whether a Tesla controller is configured.

        Returns:
            List of candidate detail dicts for all plugs and optionally Tesla.
        """
        candidate_details: list[dict[str, Any]] = []
        for name, plug in self.plugs.items():
            dev_state = self.state.devices.get(name)
            # For diagnostics, show whether the device can toggle in the
            # relevant direction: turn-off debounce for devices currently on,
            # turn-on debounce for devices currently off or unknown.
            currently_on = dev_state is not None and dev_state.desired_state is True
            can_toggle = self.state.can_toggle(name, now, turning_on=not currently_on)
            power = plug.power_watts if plug.power_watts is not None else 0.0
            capacity_wh = StateTracker.watts_to_wh(power, seconds_remaining)
            detail: dict[str, Any] = {
                "name": name,
                "power_watts": plug.power_watts,
                "capacity_wh": round(capacity_wh, 1),
                "can_toggle": can_toggle,
            }
            if not self._is_device_in_time_range(now, plug.time_range):
                detail["reason"] = "outside_time_range"
            if dev_state:
                detail["desired_state"] = dev_state.desired_state
                detail["actual_state"] = dev_state.actual_state
            candidate_details.append(detail)

        # Add Tesla to candidate details when configured
        if tesla_configured:
            tesla_detail: dict[str, Any] = {
                "name": "tesla",
                "state_available": tesla_state is not None,
                "error": tesla_error,
            }
            if tesla_state is not None:
                tesla_detail["is_charging"] = tesla_state.is_charging
                tesla_detail["current_amps"] = tesla_state.current_amps
                tesla_detail["soc_percent"] = tesla_state.soc_percent
                tesla_detail["plugged_in"] = tesla_state.plugged_in
                tesla_detail["at_home"] = tesla_state.at_home
                tesla_detail["at_charge_limit"] = tesla_state.at_charge_limit
            if self.tesla_config and not self._is_device_in_time_range(
                now, self.tesla_config.time_range
            ):
                tesla_detail["reason"] = "outside_time_range"
            candidate_details.append(tesla_detail)

        return candidate_details

    def _determine_no_action_reason(
        self,
        results: list[dict[str, str]],
        gap_wh: float,
        now: datetime,
        seconds_remaining: int,
        tesla_state: TeslaState | None,
        tesla_configured: bool,
        tesla_error: str | None,
    ) -> str:
        """Determine specific reason when no actions were taken.

        Args:
            results: List of executed action results.
            gap_wh: The Wh gap (positive = surplus, negative = deficit).
            now: current datetime.
            seconds_remaining: seconds remaining in NBC period.
            tesla_state: Tesla state if available.
            tesla_configured: Whether a Tesla controller is configured.
            tesla_error: Error message if Tesla state fetch failed.

        Returns:
            Reason string explaining why no actions were taken.
        """
        if results:
            return "ok"

        gap_positive = gap_wh > 0
        has_eligible = False
        has_too_large = False

        for name, plug in self.plugs.items():
            if name in self.sentinel_names:
                continue  # sentinels never participate in decisions
            if not self.state.can_toggle(name, now):
                continue
            if not self._is_device_in_time_range(now, plug.time_range):
                continue
            dev_state = self.state.devices.get(name)
            # All plugs are eligible: turn-on when off/unknown, turn-off when on
            if gap_positive:
                eligible = dev_state is None or dev_state.desired_state is False
            else:
                eligible = dev_state is not None and dev_state.desired_state is True

            if eligible:
                has_eligible = True
                power = plug.power_watts if plug.power_watts is not None else 0.0
                capacity_wh = StateTracker.watts_to_wh(
                    power, seconds_remaining
                )
                abs_gap = abs(gap_wh)
                if capacity_wh > abs_gap:
                    has_too_large = True

        # Check Tesla eligibility for turn-off scenarios
        if not gap_positive and tesla_state is not None:
            tesla_in_range = self._is_device_in_time_range(
                now,
                self.tesla_config.time_range if self.tesla_config else None,
            )
            if tesla_in_range and tesla_state.is_charging:
                has_eligible = True

        if not has_eligible:
            if tesla_configured and tesla_error is not None:
                return "no_eligible_tesla_unavailable"
            return "no_eligible"
        if has_too_large:
            return "loads_too_large"
        return "no_candidates"

    def _calculate_adaptive_sleep(self, cycle_result: dict) -> float:
        """Calculate an adaptive sleep duration based on the current cycle state.

        The returned value is always clamped to [5, config_interval * 2].

        Args:
            cycle_result: The dict returned by run_cycle() itself (before
                sleep_hint is added).

        Returns:
            Suggested sleep duration in seconds.
        """
        status = cycle_result.get("status", "")
        config_interval = self.config_interval_secs
        min_interval = 5.0

        # --- Disabled: normal interval ---
        if status == "disabled":
            return config_interval

        # --- Stale data: minimum sleep, refresh ASAP ---
        if status == "stale_data":
            return min_interval

        # --- No incomplete QH: minimum sleep ---
        if status == "no_incomplete_qh":
            return min_interval

        # --- Shared lookups ---
        seconds_remaining = self._seconds_remaining(cycle_result, config_interval)

        # --- Waiting for fresh data: wait until next NBC point ---
        if status == "waiting_for_fresh_data":
            return min(seconds_remaining, config_interval * 2)

        # --- Shared lookups ---
        predicted_wh = cycle_result.get("nbc_prediction_wh", 0)

        # --- No deficit (predicted >= target): check how much QH remains ---
        if predicted_wh >= self.target_wh:
            # No deficit. Early in the quarter -> sleep longer; late -> wake sooner.
            if seconds_remaining > 300:  # more than 5 min left in QH
                return min(config_interval * 1.5, config_interval * 2)

            return min(config_interval * 1.25, config_interval * 2)

        # --- Deficit with no actions possible: proportional sleep ---
        gap = abs(predicted_wh - self.target_wh)

        # Calculate total eligible capacity from candidates
        max_load_capacity = 0.0
        for candidate in (cycle_result.get("diagnostics") or {}).get(
            "candidates"
        ) or []:
            max_load_capacity += candidate.get("power_watts", 0)

        if max_load_capacity > 0 and seconds_remaining > 0:
            # Time to close the gap at full capacity (in seconds)
            time_to_close = (gap / max_load_capacity) * 3600.0
            # Proportion of config interval: if gap shrinks to fillable in half
            # the QH, sleep half as long relative to config.
            proportion = seconds_remaining / max(time_to_close, 1)
            sleep = config_interval * min(proportion, 2.0)
        else:
            # No capacity or unknown -> minimum sleep
            sleep = min_interval

        return max(min_interval, min(sleep, config_interval * 2))

    @staticmethod
    def _seconds_remaining(cycle_result: dict, default: float) -> float:
        """Extract seconds_remaining from a cycle result dict.

        Checks the top-level key first, then falls back to the diagnostics
        sub-dict.

        Args:
            cycle_result: The cycle result dict.
            default: Value to return if seconds_remaining is not found.

        Returns:
            The seconds remaining in the current quarter-hour.
        """
        value = cycle_result.get("seconds_remaining")
        if value is not None:
            return value
        diagnostics = cycle_result.get("diagnostics") or {}
        return diagnostics.get("seconds_remaining", default)

    async def _fetch_tesla_state_async(
        self,
    ) -> tuple[TeslaState | None, str | None, str | None]:
        """Fetch Tesla charging state and auth diagnostics (async).

        Returns:
            Tuple of (tesla_state, tesla_error, tesla_login_url).
            tesla_login_url is set on TeslaAuthError to provide a
            re-authentication link.
        """
        if self.tesla_ctrl is None:
            return None, None, None
        error: str | None = None
        state = None
        login_url: str | None = None
        try:
            state = await self.tesla_ctrl.get_charging_state()
        except TeslaAuthError as tae:
            error = str(tae)
            login_url = self.tesla_ctrl.get_login_url()
        return state, error, login_url

    async def _sync_plug_states(self) -> None:
        """Query actual plug states from controllers and reconcile with tracking.

        Detects external changes (e.g., user manually toggling a plug) by comparing
        the controller's reported state against our internal desired_state. When they
        diverge, updates both actual_state and desired_state to match reality so the
        GapMinder makes decisions based on current conditions.
        """
        for name in self.plugs:
            try:
                actual = await self.plug_ctrl.get_state(name)
            except Exception as e:
                logger.warning(
                    "Failed to sync state for plug %s: %s", name, e
                )
                continue

            if actual is None:
                continue

            dev_state = self.state.devices.get(name)
            if dev_state is None:
                # First time seeing this plug's state
                self.state.devices[name] = DeviceState(
                    name=name, actual_state=actual, desired_state=actual
                )
            else:
                dev_state.actual_state = actual
                # Reconcile: if external actor changed the state, match it
                if dev_state.desired_state != actual:
                    logger.info(
                        "Reconciling %s: desired=%s but actual=%s "
                        "(external change)",
                        name,
                        dev_state.desired_state,
                        actual,
                    )
                    dev_state.desired_state = actual

    async def _cycle_async_phase(
        self,
        gap_wh: float,
        adjusted_wh: float,
        now: datetime,
        seconds_remaining: int,
        dry_run: bool,
        qh_name: str | None = None,
        data_point_at: datetime | None = None,
    ) -> tuple[
        TeslaState | None,
        str | None,
        str | None,
        list[PendingEffect],
        list[dict[str, str]],
        float,
        float,
        bool,
    ]:
        """Run the async portion of a cycle in a single event loop.

        Syncs plug states from controllers, fetches Tesla state, calls decide()
        with that state, then executes all resulting actions. Consolidating into
        one coroutine means one event loop per cycle instead of one per action.

        Tesla amp-change effects have no power_watts so they're excluded from
        estimated_current_wh(). After fetching the vehicle state we recompute
        the in-flight contribution via tesla_inflight_wh() and fold it into
        corrected_adjusted_wh before calling decide(), so the gap never
        drifts as seconds_remaining shrinks.

        Returns:
            Tuple of (tesla_state, tesla_error, tesla_login_url,
            succeeded_effects, results, gap_wh, adjusted_wh, sentinel_on).
            The final bool is True when any sentinel device was detected on
            during sync, allowing the caller to disable the cycle.
        """
        # Sync actual plug states before making decisions so the engine sees
        # external changes (user toggles, other automations, etc.)
        await self._sync_plug_states()

        # If any sentinel device is on, disable load management entirely.
        # Placed after _sync_plug_states so device state is populated.
        sentinel_on: bool = any(
            self.state.devices.get(name, DeviceState(name=name)).actual_state
            is True
            for name in self.sentinel_names
        )
        if sentinel_on:
            logger.info(
                "[_cycle_async_phase] sentinel device is on, disabling load management"
            )
            return (
                None,
                None,
                None,
                [],
                [],
                0.0,
                0.0,
                True,
            )

        tesla_state, tesla_error, tesla_login_url = (
            await self._fetch_tesla_state_async()
        )

        # Fold live Tesla in-flight contribution into the gap.  Tesla set_amps
        # effects are excluded from estimated_current_wh() so this live
        # recalculation prevents the adjustment from drifting as
        # seconds_remaining shrinks.
        tesla_reported = tesla_state.current_amps if tesla_state is not None else None
        inflight_wh = self.state.tesla_inflight_wh(tesla_reported, seconds_remaining)
        corrected_adjusted_wh = adjusted_wh + inflight_wh
        corrected_gap_wh = self.target_wh - corrected_adjusted_wh
        if inflight_wh != 0.0:
            logger.debug(
                "[_cycle_async_phase] tesla inflight correction: "
                "commanded=%s reported=%s inflight=%.1f Wh "
                "gap %.1f → %.1f Wh",
                self.state.last_commanded_amps,
                tesla_reported,
                inflight_wh,
                gap_wh,
                corrected_gap_wh,
            )

        # Hysteresis guard uses corrected gap so in-flight Tesla draw is counted.
        if abs(corrected_gap_wh) <= self.engine.HYSTERESIS_WH:
            return tesla_state, tesla_error, tesla_login_url, [], [], corrected_gap_wh, corrected_adjusted_wh, False

        # ── Settle-window suppression ──────────────────────────────────────────
        # After a Tesla amp increase is confirmed the NBC prediction needs a few
        # cycles to absorb the new load — the Emporia window covers the last
        # 60 seconds, so some of that history is still at the old amp level.
        # Combined with solar variability this can produce a large apparent
        # deficit on the very first post-confirmation cycle, triggering an
        # unwarranted cascade of plug shutoffs and Tesla stop.
        #
        # Suppress turn-off decisions while the settle window is active AND the
        # apparent deficit is below TESLA_SETTLE_SUPPRESS_WH.  Large genuine
        # deficits (e.g. new QH, clouds) still fire immediately; the QH-name
        # check inside is_settling_after_amp_increase() also auto-expires the
        # window on a QH transition.
        settle_now = now
        if (
            corrected_gap_wh < 0
            and abs(corrected_gap_wh) < self.engine.TESLA_SETTLE_SUPPRESS_WH
            and self.state.is_settling_after_amp_increase(settle_now, current_qh=qh_name)
        ):
            settle_remaining = (
                StateTracker.TESLA_SETTLE_SECS
                - (settle_now - self.state.last_tesla_increase_at).total_seconds()  # type: ignore[operator]
            )
            logger.info(
                "[_cycle_async_phase] suppressing turn-off: settling after Tesla "
                "amp increase (gap=%.1f Wh, %.0f s left in settle window)",
                corrected_gap_wh,
                settle_remaining,
            )
            return (
                tesla_state,
                tesla_error,
                tesla_login_url,
                [],
                [],
                corrected_gap_wh,
                corrected_adjusted_wh,
                False,
            )

        # Filter plugs by per-device time range: only eligible plugs reach the engine.
        eligible_plugs: dict[str, PlugConfig] = {}
        outside_range: list[str] = []
        for name, plug in self.plugs.items():
            if name in self.sentinel_names:
                continue  # sentinels never participate in decisions
            if self._is_device_in_time_range(now, plug.time_range):
                eligible_plugs[name] = plug
            else:
                outside_range.append(name)

        # Log which devices were filtered out by time range (once per cycle).
        if outside_range:
            logger.debug("Outside time range: %s", ", ".join(outside_range))

        # Tesla is only a candidate if within its time range.
        eligible_tesla: TeslaState | None = tesla_state
        if (
            tesla_state is not None
            and self.tesla_config is not None
            and not self._is_device_in_time_range(
                now, self.tesla_config.time_range
            )
        ):
            eligible_tesla = None

        actions = self.engine.decide(
            ctx=DecideContext(
                now=now,
                seconds_remaining=seconds_remaining,
                state=self.state,
                plugs=eligible_plugs,
                tesla=eligible_tesla,
                dry_run=dry_run,
                data_point_at=data_point_at,
            ),
            predicted_wh=corrected_adjusted_wh,
            target_wh=self.target_wh,
        )

        succeeded_effects: list[PendingEffect] = []
        results: list[dict[str, str]] = []
        for action in actions:
            if dry_run:
                logger.info(
                    "[DRY-RUN] Would execute: %s on %s",
                    action.action,
                    action.device_name,
                )
                results.append(
                    {"device": action.device_name, "action": action.action}
                )
            else:
                success = await self._execute_action(action)
                if success:
                    succeeded_effects.append(action)
                    results.append(
                        {"device": action.device_name, "action": action.action}
                    )

        return tesla_state, tesla_error, tesla_login_url, succeeded_effects, results, corrected_gap_wh, corrected_adjusted_wh, False

    @staticmethod
    def _plug_states_from_candidates(
        candidate_details: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Extract a compact per-plug state summary for log messages.

        Args:
            candidate_details: Output of _build_candidate_details.

        Returns:
            Dict keyed by plug name with desired/actual/can_toggle fields,
            excluding the tesla entry.
        """
        return {
            c["name"]: {
                "desired": c.get("desired_state"),
                "actual": c.get("actual_state"),
                "can_toggle": c.get("can_toggle", False),
            }
            for c in candidate_details
            if c["name"] != "tesla"
        }

    def _check_pending_state(
        self,
        now_postfetch: datetime,
        data_point_at: datetime,
        seconds_remaining: int,
    ) -> dict[str, Any] | None:
        """Check for stale NBC data or unconfirmed pending effects.

        Only called when force=False. Returns an early-exit result dict if
        the cycle should be skipped, or None to continue.

        Stale path: NBC data is older than STALE_THRESHOLD_SECS. Unsafe to
        act — we don't know whether our last actions are reflected yet.
        Prunes effects old enough to be safe to discard and skips the cycle.

        Waiting path: Data is fresh enough, but we acted after the last NBC
        data point and the effect may not yet be visible. Skip to avoid
        double-counting the load.

        Args:
            now_postfetch: Timestamp taken after the NBC fetch completed.
            data_point_at: When the most recent per-second data point was
                recorded (fetched_at minus API lag). Used for accurate stale
                and waiting detection.
            seconds_remaining: Seconds left in the current QH (for diag).

        Returns:
            An early-exit status dict, or None to continue.
        """
        tesla_configured = self.tesla_ctrl is not None
        plugs_configured = list(self.plugs.keys())
        base_diag: dict[str, Any] = {
            "gap_wh": None,
            "hysteresis_wh": self.engine.HYSTERESIS_WH,
            "seconds_remaining": seconds_remaining,
            "tesla_configured": tesla_configured,
            "tesla_state": None,
            "tesla_error": None,
            "plugs_configured": plugs_configured,
        }

        # data_point_at is the timestamp of the most recent per-second data
        # point — what stale detection and waiting detection should use.
        nbc_data_age_secs = (now_postfetch - data_point_at).total_seconds()
        if (
            nbc_data_age_secs > StateTracker.STALE_THRESHOLD_SECS
            and len(self.state.pending_effects) > 0
        ):
            # Prune old effects even during stale cycles so the list doesn't
            # grow unbounded while we wait for fresh data.
            pruned = self.state.prune_old_effects(data_point_at, now_postfetch)
            if pruned > 0:
                logger.debug("Pruned %d old pending effects (stale)", pruned)
            pending_count = len(self.state.pending_effects)
            candidate_details = self._build_candidate_details(
                now_postfetch, seconds_remaining, None, None, tesla_configured
            )
            logger.warning(
                "NBC data stale (%d pending effects, %s), skipping cycle",
                pending_count,
                self._plug_states_from_candidates(candidate_details),
            )
            return {
                "status": "stale_data",
                "diagnostics": {
                    **base_diag,
                    "reason": "stale_data",
                    "pending_effects_count": pending_count,
                    "candidates": candidate_details,
                },
                "sleep_hint": 5.0,
            }

        # QH boundary check: data_point_at must fall within the current
        # quarter-hour window.  If it's from a previous QH, act on stale data
        # — the prediction describes conditions that no longer apply.  This
        # catches cases where data is <120s old but from a different QH.
        current_qh_start, _ = NBCPeriod.current_qh_window(now_postfetch)
        if data_point_at < current_qh_start:
            pruned = self.state.prune_old_effects(data_point_at, now_postfetch)
            if pruned > 0:
                logger.debug("Pruned %d old pending effects (previous QH)", pruned)
            candidate_details = self._build_candidate_details(
                now_postfetch, seconds_remaining, None, None, tesla_configured
            )
            logger.warning(
                "NBC data from previous QH (%s, %d pending effects), skipping cycle",
                self._plug_states_from_candidates(candidate_details),
                len(self.state.pending_effects),
            )
            return {
                "status": "stale_data",
                "diagnostics": {
                    **base_diag,
                    "reason": "previous_qh",
                    "pending_effects_count": len(self.state.pending_effects),
                    "candidates": candidate_details,
                },
                "sleep_hint": 5.0,
            }

        nbc_timestamp = data_point_at
        if nbc_timestamp is not None and self.state.has_pending_effect_since(
            nbc_timestamp
        ):
            pending_count = self.state.pending_since_count(nbc_timestamp)
            pruned = self.state.prune_old_effects(data_point_at, now_postfetch)
            if pruned > 0:
                logger.debug("Pruned %d old pending effects (waiting path)", pruned)
            candidate_details = self._build_candidate_details(
                now_postfetch, seconds_remaining, None, None, tesla_configured
            )
            logger.info(
                "Pending effects (%d) not yet reflected, %s; "
                "waiting for fresh data",
                pending_count,
                self._plug_states_from_candidates(candidate_details),
            )
            return {
                "status": "waiting_for_fresh_data",
                "diagnostics": {
                    **base_diag,
                    "reason": "waiting_for_fresh_data",
                    "pending_effects_count": pending_count,
                    "candidates": candidate_details,
                },
                "sleep_hint": min(
                    seconds_remaining, self.config_interval_secs * 2
                ),
            }

        return None

    def run_cycle(self, force: bool = False) -> dict[str, Any]:
        """Execute one load management cycle. Returns status dict.

        Thread-safe: acquires lock for entire cycle to prevent race conditions
        with concurrent endpoint calls.

        The cycle runs as a six-stage pipeline:
            1. Enabled check — bail early if disabled or outside time window.
            2. NBC fetch — obtain the current quarter-hour prediction.
            3. Pending-state check — skip if data is stale or pending effects
               are not yet reflected in the NBC data.
            4. Accept fresh data — prune old effects, record fetch timestamp,
               compute adjusted Wh and gap.
            5. Async phase (single event loop) — fetch Tesla state, call
               decide(), execute all actions.
            6. Commit and return — persist succeeded effects, build result.

        Args:
            force: If True, bypass stale-data check (debug only).

        Returns:
            Status dict with keys: status, qh, predicted_wh, target_wh,
            actions, and diagnostics.
        """
        tesla_configured = self.tesla_ctrl is not None
        plugs_configured = list(self.plugs.keys())

        with self._lock:
            # ── Stage 1: enabled check ─────────────────────────────────────
            now = datetime.now(timezone.utc) # ok because lock may take time

            if not self.is_enabled_at(now):
                return {
                    "status": "disabled",
                    "diagnostics": {
                        "gap_wh": None,
                        "hysteresis_wh": self.engine.HYSTERESIS_WH,
                        "seconds_remaining": None,
                        "data_point_at": None,
                        "reason": self._disabled_reason("run_cycle"),
                        "tesla_configured": tesla_configured,
                        "tesla_state": None,
                        "tesla_error": None,
                        "plugs_configured": plugs_configured,
                    },
                    "sleep_hint": self.config_interval_secs,
                    "sleep_hint_at": now.isoformat(),
                }

            # ── Stage 2: NBC fetch ─────────────────────────────────────────
            fetch_start = _time_mod.perf_counter()
            qh_result = self.nbc_reader.get_current_qh(force=force, now=now)
            if qh_result is None:
                return {
                    "status": "no_incomplete_qh",
                    "diagnostics": {
                        "gap_wh": None,
                        "hysteresis_wh": self.engine.HYSTERESIS_WH,
                        "seconds_remaining": None,
                        "data_point_at": None,
                        "reason": "no_incomplete_qh",
                        "tesla_configured": tesla_configured,
                        "tesla_state": None,
                        "tesla_error": None,
                        "plugs_configured": plugs_configured,
                    },
                    "sleep_hint": 5.0,
                    "sleep_hint_at": now.isoformat(),
                }
            (
                qh_name,
                predicted_wh,
                seconds_remaining,
                data_point_at,
            ) = qh_result
            fetch_end = _time_mod.perf_counter()
            # reduce calls to datetime.now, to reduce scope for test errors
            now_postfetch = now + timedelta(seconds=fetch_end - fetch_start)
            # TODO assert now_postfetch is same QH as local now? retryable exception?
            logger.debug("now %s postfetch %s", now, now_postfetch)

            # ── Stage 3: pending-state check ───────────────────────────────
            if not force:
                early = self._check_pending_state(
                    now_postfetch, data_point_at, seconds_remaining
                )
                if early is not None:
                    early["sleep_hint_at"] = now_postfetch.isoformat()
                    return early

            # ── Stage 4: accept fresh data, compute gap ────────────────────
            # ── Measure clock skew from pending effects BEFORE pruning ──────
            # We measure skew from effects that have been reflected in the NBC
            # data — these are exactly the effects that will be pruned below.
            # Measuring first ensures we capture the skew before cleanup.
            skew_diag = self._measure_clock_skew()

            pruned = self.state.prune_old_effects(data_point_at, now=now_postfetch)
            if pruned > 0:
                logger.debug("Pruned %d old pending effects", pruned)

            self.state.last_data_point_at = data_point_at
            # Adjust prediction with still-pending effects so decide() accounts
            # for actions already taken this quarter-hour.
            adjusted_wh = self.state.estimated_current_wh(predicted_wh, seconds_remaining)
            gap_wh = self.target_wh - adjusted_wh

            # ── Stage 5: async phase (one event loop for the whole cycle) ──
            # Single reset_session() call here instead of one per action.
            if self.tesla_ctrl is not None:
                self.tesla_ctrl.reset_session()
            (
                tesla_state,
                tesla_error,
                tesla_login_url,
                succeeded_effects,
                results,
                gap_wh,
                adjusted_wh,
                sentinel_on,
            ) = asyncio.run(
                self._cycle_async_phase(
                    gap_wh, adjusted_wh, now_postfetch, seconds_remaining, self.dry_run, qh_name,
                    data_point_at=data_point_at,
                )
            )

            # ── Sentinel-on: disable load management ───────────────────────
            # _cycle_async_phase returned sentinel_on=True, so disable the cycle.
            if sentinel_on:
                return {
                    "status": "disabled",
                    "qh": qh_name,
                    "predicted_wh": predicted_wh,
                    "adjusted_wh": adjusted_wh,
                    "target_wh": self.target_wh,
                    "actions": [],
                    "diagnostics": {
                        "gap_wh": None,
                        "hysteresis_wh": self.engine.HYSTERESIS_WH,
                        "seconds_remaining": seconds_remaining,
                        "data_point_at": data_point_at,
                        "reason": "sentinel_on",
                        "pending_effects_count": 0,
                        "tesla_configured": tesla_configured,
                        "tesla_state": None,
                        "tesla_error": None,
                        "plugs_configured": plugs_configured,
                        "sentinel_names": list(self.sentinel_names),
                        "sentinel_on": True,
                    },
                    "sleep_hint": self.config_interval_secs,
                    "sleep_hint_at": now_postfetch.isoformat(),
                }

            # ── Stage 6: commit succeeded effects, build result ────────────
            for effect in succeeded_effects:
                self.state.pending_effects.append(effect)

            # Track last commanded amps for tesla_inflight_wh() next cycle.
            # Also maintain the settle-window state for post-increase damping:
            # record amp increases so subsequent cycles can suppress premature
            # turn-off reactions; clear on decreases or charging stop.
            for effect in succeeded_effects:
                if effect.device_name == "tesla":
                    if effect.action == "set_amps":
                        prev_amps = self.state.last_commanded_amps
                        new_amps = effect.target_amps
                        self.state.last_commanded_amps = new_amps
                        if new_amps is not None and (
                            prev_amps is None or new_amps > prev_amps
                        ):
                            self.state.record_tesla_amp_increase(
                                effect.timestamp, qh_name=qh_name
                            )
                            logger.debug(
                                "Tesla amp increase recorded: %s → %d A "
                                "(settle window starts)",
                                prev_amps,
                                new_amps,
                            )
                        elif (
                            new_amps is not None
                            and prev_amps is not None
                            and new_amps < prev_amps
                        ):
                            self.state.clear_tesla_settle()
                    elif effect.action in ("turn_off", "turn_on"):
                        self.state.last_commanded_amps = None
                        self.state.clear_tesla_settle()

            if abs(gap_wh) <= self.engine.HYSTERESIS_WH:
                sentinel_on = any(
                    self.state.devices.get(name, DeviceState(name=name)).actual_state
                    is True
                    for name in self.sentinel_names
                )
                return {
                    "status": "dry-run" if self.dry_run else "ok",
                    "qh": qh_name,
                    "predicted_wh": predicted_wh,
                    "adjusted_wh": adjusted_wh,
                    "target_wh": self.target_wh,
                    "actions": [],
                    "diagnostics": {
                        "gap_wh": gap_wh,
                        "hysteresis_wh": self.engine.HYSTERESIS_WH,
                        "seconds_remaining": seconds_remaining,
                        "data_point_at": data_point_at,
                        "reason": "hysteresis",
                        "pending_effects_count": len(self.state.pending_effects),
                        **skew_diag,
                        "tesla_configured": tesla_configured,
                        "tesla_state": _tesla_state_to_dict(tesla_state),
                        "tesla_error": tesla_error,
                        "tesla_login_url": tesla_login_url,
                        "plugs_configured": plugs_configured,
                        "sentinel_names": list(self.sentinel_names),
                        "sentinel_on": sentinel_on,
                    },
                    "sleep_hint": self.config_interval_secs,
                    "sleep_hint_at": now_postfetch.isoformat(),
                }

            candidate_details = self._build_candidate_details(
                now, seconds_remaining, tesla_state, tesla_error, tesla_configured
            )
            reason = self._determine_no_action_reason(
                results,
                gap_wh,
                now,
                seconds_remaining,
                tesla_state,
                tesla_configured,
                tesla_error,
            )
            return {
                "status": "dry-run" if self.dry_run else "ok",
                "qh": qh_name,
                "predicted_wh": predicted_wh,
                "adjusted_wh": adjusted_wh,
                "target_wh": self.target_wh,
                "actions": results,
                "diagnostics": {
                    "gap_wh": gap_wh,
                    "hysteresis_wh": self.engine.HYSTERESIS_WH,
                    "seconds_remaining": seconds_remaining,
                    "data_point_at": data_point_at,
                    "reason": reason,
                    "pending_effects_count": len(self.state.pending_effects),
                    "candidates": candidate_details,
                    **skew_diag,
                    "tesla_configured": tesla_configured,
                    "tesla_state": _tesla_state_to_dict(tesla_state),
                    "tesla_error": tesla_error,
                    "tesla_login_url": tesla_login_url,
                    "plugs_configured": plugs_configured,
                    "sentinel_names": list(self.sentinel_names),
                    "sentinel_on": any(
                        self.state.devices.get(
                            name, DeviceState(name=name)
                        ).actual_state
                        is True
                        for name in self.sentinel_names
                    ),
                },
                "sleep_hint": self.config_interval_secs,
                "sleep_hint_at": now_postfetch.isoformat(),
            }

    async def _execute_action(self, action: PendingEffect) -> bool:
        """Execute a single pending action against the appropriate controller.

        Args:
            action: The action to execute.

        Returns:
            True on success, False on failure.
        """
        try:
            if action.device_name == "tesla":
                return await self._execute_tesla_action(action)
            return await self._execute_plug_action(action)
        except Exception as e:
            logger.error("Failed to execute action %s: %s", action, e)
            return False

    async def _execute_plug_action(self, action: PendingEffect) -> bool:
        """Execute a plug on/off action."""
        if action.action == "turn_on":
            return await self.plug_ctrl.set_state(action.device_name, True)
        if action.action == "turn_off":
            return await self.plug_ctrl.set_state(action.device_name, False)
        logger.warning("Unknown plug action: %s", action.action)
        return False

    async def _execute_tesla_action(self, action: PendingEffect) -> bool:
        """Execute a Tesla charging action."""
        if self.tesla_ctrl is None:
            return False

        if action.action == "turn_on":
            return await self.tesla_ctrl.start_charging()
        if action.action == "turn_off":
            return await self.tesla_ctrl.stop_charging()
        if action.action == "set_amps":
            if action.target_amps is None or action.target_amps < 5:
                return await self.tesla_ctrl.stop_charging()
            return await self.tesla_ctrl.set_charge_amps(action.target_amps)
        logger.warning("Unknown Tesla action: %s", action.action)
        return False

    def _measure_clock_skew(self) -> dict[str, Any]:
        """Scan per-second data for edges from pending plug effects.

        For each pending plug effect (turn_on/turn_off, not Tesla),
        searches the energy cache for a step matching the expected
        power change. Computes skew = meter_timestamp - local_timestamp
        for each edge found and feeds it to the skew estimator.

        The ``data_point_at`` is pre-adjusted (offset by ~20s) at effect
        creation time, so comparing against ``effect.timestamp`` directly
        gives the true clock skew between meter and local clock.

        Returns:
            Diagnostics dict with clock_skew info.
        """
        measurements: list[float] = []

        for effect in self.state.pending_effects:
            if effect.action not in ("turn_on", "turn_off"):
                continue
            if effect.device_name == "tesla":
                continue

            direction = +1 if effect.action == "turn_on" else -1
            edges = self.state._detect_skew_samples(
                energy_cache=self.nbc_reader.energy_cache,
                power_watts=effect.power_watts,
                action_direction=direction,
            )
            if edges is None:
                continue

            for edge_time in edges:
                skew = (edge_time - effect.timestamp).total_seconds()
                self.state._skew_estimator.record(skew)
                measurements.append(skew)

        return {
            "clock_skew": {
                "estimate_seconds": self.state._skew_estimator.estimate,
                "measurement_count": len(
                    self.state._skew_estimator.measurements
                ),
                "last_cycle_measurements": measurements,
            }
        }

    def close(self) -> None:
        """Close the LoadManager and release resources.

        Calls close on the Tesla controller to clean up any open aiohttp
        sessions. Safe to call multiple times.
        """
        if self.tesla_ctrl is not None:
            try:
                asyncio.run(self.tesla_ctrl.close())
            except Exception as e:
                logger.warning("Failed to close Tesla controller: %s", e)


# === Backward-compatible re-exports ===
# All public symbols are imported at the top of this module so that existing
# `from load_manager import X` statements in tests and app.py continue to work.

__all__ = [
    # Orchestrator
    "LoadManager",
    "TeslaAuthError",
    # Config loading
    "load_plugs_from_file",
    "load_tesla_config",
    "load_vocolinc_credentials",
    "load_vocolinc_plugs_from_file",
    # Re-exported from submodules (backward compat)
    "AbstractPlugController",
    "AbstractTeslaController",
    "CompositePlugController",
    "DeviceState",
    "NBCReader",
    "PAIRINGS_FILE",
    "PendingEffect",
    "PlugAction",
    "PlugConfig",
    "PlugController",
    "RealPlugController",
    "RealTeslaController",
    "TESLA_TOKENS_FILE",
    "GapMinder",
    "TeslaConfig",
    "TeslaController",
    "TeslaState",
    "VocolincPlugController",
    "_parse_load_manage_enabled",
    "_tesla_state_to_dict",
    "load_tesla_tokens",
    "pair_homekit_accessory",
    "remove_tesla_tokens",
    "save_tesla_tokens",
    "tesla_auth_cli",
]
