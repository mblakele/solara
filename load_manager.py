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
import sys
import threading
import time as _time_mod

# Third-party imports.
from typing import Any, Callable

import pytz

# First-party local imports — LoadManagerConfig references their types, so they must
# appear before the dataclass definition below.
import device_config

from clock import Clock, RealClock
from config import Config, _config
from constants import (
    DEFAULT_SLEEP_HINT_SECS,
    STALE_DATA_THRESHOLD_SECS,
)

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
from mqtt_telemetry import (
    get_field_update_at,
    get_telemetry_snapshot,
    has_telemetry,
    tesla_state_from_snapshot,
)


from load_models import (  # noqa: F401
    AbstractPlugController,
    AbstractTeslaController,
    CandidateDetail,
    CycleContext,
    CycleDiagnostics,
    CycleResult,
    DeviceState,
    FleetTelemetryProvisionConfig,
    PendingEffect,
    PlugAction,
    PlugConfig,
    TeslaAuthError,
    TeslaConfig,
    TeslaState,
    TeslaVehicleTelemetry,
    _tesla_state_to_dict,
)

from load_nbc import NBCPeriod, NBCReader, StateTracker, GapMinder, DecideContext  # noqa: F401

from metrics import EnergyCache

from telegram import (  # noqa: F401
    TelegramSender,
    build_error_notification,
    build_notification,
)

from vocolinc import VOCOlinc  # pylint: disable=W0611

# Re-exported for test patching — pylint complains about unused import but it's
# intentionally exposed so tests can monkey-patch vocolinc.VOCOlinc.


@dataclass(frozen=True)
class LoadManagerConfig:
    """Configuration for LoadManager initialization.

    Groups related parameters to reduce __init__ argument count from 10+
    down to a single config object. All fields have sensible defaults so
    production code can pass an empty LoadManagerConfig() and rely on env
    vars / devices.json for the rest.

    Attributes:
        config: Optional injected ``Config`` instance. When provided, used
            for all env-var lookups instead of the module-level ``_config``
            singleton. Tests should create ``Config(overrides={...})`` and
            pass it here — no patching needed.
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

    config: Any | None = None  # Config | None — forward ref, resolved at runtime
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
    telegram_sender: TelegramSender | None = None
    clock: Any | None = None  # clock.Clock | None — forward ref, resolved at runtime

# Re-exported for backward compatibility.


logger = logging.getLogger(__name__)



_no_telemetry_warn_cycle: int = 0
_NO_TELEMETRY_WARN_INTERVAL = 10


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
            telegram_sender = config.telegram_sender
            clock = config.clock  # type: ignore[assignment]
            # Resolve injected Config — use the provided one, or fall back to singleton
            config_obj: Config | None = config.config  # type: ignore[assignment]
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
            telegram_sender = kwargs.get("telegram_sender")
            clock = kwargs.get("clock")  # type: ignore[assignment]
            config_obj = kwargs.get("_config")  # type: ignore[assignment]

        self._cfg = config_obj if config_obj is not None else _config
        self._clock: Clock = clock if clock is not None else RealClock()

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
            enabled = self._resolve_enabled(self._cfg)
        self.enabled: bool | tuple[time, time] = enabled
        logger.info("LoadManager %s", self.enabled)

        if dry_run is None:
            dry_run = self._cfg.dry_run
        self.dry_run = dry_run

        self.telegram_sender = telegram_sender  # type: ignore[assignment]
        if self.telegram_sender is not None:
            try:
                chat_id = self.telegram_sender.config.chat_id
            except AttributeError:
                chat_id = "<mock>"
            logger.info("Telegram sender configured for chat %s", chat_id)
        else:
            logger.info("Telegram sender not configured — notifications disabled")

        self.config_interval_secs = interval_secs  # type: ignore[assignment]

        hysteresis_wh = int(abs(target_wh) / 3)
        self.tesla_config = load_tesla_config(config=self._cfg)
        tesla_config = self.tesla_config
        if engine is not None:
            self.engine = engine
        else:
            self.engine = GapMinder(
                hysteresis_wh=hysteresis_wh,
                charge_amps_min=tesla_config.charge_amps_min if tesla_config else 5,
                charge_amps_max=tesla_config.charge_amps_max if tesla_config else 48,
            )
        self.state = StateTracker(
            prediction_window_seconds=self._resolve_prediction_window(),
        )
        # Tracks the last known at_home value from Location telemetry snapshots.
        # Preserved when Location is absent so the requires_home_check gate in
        # GapMinder doesn't incorrectly block Tesla decisions.
        self._last_tesla_at_home: bool | None = None
        # Anti-spam dedup: tracks the last auth error text sent to Telegram so we
        # only send one alert per unique error message (not one per 30 s cycle).
        self._last_auth_error_msg: str | None = None

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
                controller_type = self._cfg.load_plug_controller
                hk_ctrl = (
                    RealPlugController(plugs_from_file)
                    if controller_type == "real"
                    else PlugController(plugs_from_file)
                )
                vc_creds = load_vocolinc_credentials(config=self._cfg)
                vc_ctrl = VocolincPlugController(
                    vocolinc_plugs,
                    username=vc_creds[0] if vc_creds else None,
                    password=vc_creds[1] if vc_creds else None,
                    config=self._cfg,
                )
                self.plug_ctrl = CompositePlugController(hk_ctrl, vc_ctrl)
            elif has_vocolinc_plugs:
                vc_creds = load_vocolinc_credentials(config=self._cfg)
                self.plug_ctrl = VocolincPlugController(
                    vocolinc_plugs,
                    username=vc_creds[0] if vc_creds else None,
                    password=vc_creds[1] if vc_creds else None,
                    config=self._cfg,
                )
            else:
                controller_type = self._cfg.load_plug_controller
                if controller_type == "real":
                    self.plug_ctrl = RealPlugController(plugs_from_file)
                else:
                    self.plug_ctrl = PlugController(plugs_from_file)

        self.plugs = self.plug_ctrl.plugs  # type: ignore[attr-defined]

        # Collect sentinel plug names for fast lookup during cycles.
        self.sentinel_names: frozenset[str] = frozenset(
            name for name, plug in self.plugs.items() if plug.sentinel
        )

        # Load telegram config: auth error alerts default to enabled.
        # alert_on_auth_error controls whether auth errors trigger a notification.
        self._telegram_alert_on_auth_error: bool = True

        # Load the telegram.devices whitelist for notification gating.
        # Dict maps device name (lowercased) → set of allowed action types.
        # None means no whitelist configured (backward-compatible: no gating).
        tg_config = device_config.get_telegram_config()
        if tg_config:
            self._telegram_alert_on_auth_error = tg_config.get("alert_on_auth_error", True)
            devices = tg_config.get("devices")
            if devices:
                self._telegram_devices: dict[str, set[str]] | None = {
                    name.lower(): set(actions) for name, actions in devices.items()
                }
            else:
                self._telegram_devices = {}
        else:
            self._telegram_devices = None

        if tesla_ctrl is not None:
            self.tesla_ctrl = tesla_ctrl
        elif tesla_config is not None:
            controller_type = self._cfg.load_tesla_controller
            if controller_type == "real":
                self.tesla_ctrl = RealTeslaController(tesla_config, config=self._cfg)
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
    def _resolve_enabled(cfg: Config | None = None) -> bool | tuple[time, time]:
        """Resolve LOAD_MANAGE_ENABLED from config.

        Reads the raw env var value and parses it using
        _parse_load_manage_enabled. On parse error, logs the issue
        and returns False (disabled).

        Args:
            cfg: Optional Config instance. Falls back to module-level
                ``_config`` singleton when None (backward compatible).

        Returns:
            Parsed enabled value: True, False, or a time range tuple.
        """
        resolved = cfg if cfg is not None else _config
        raw_value = resolved.load_manage_enabled  # type: ignore[assignment]
        try:
            return _parse_load_manage_enabled(raw_value)
        except ValueError as e:
            logger.error("%s. Disabling load management.", e)
            return False

    def _resolve_prediction_window(self) -> int:
        """Return the prediction window in seconds, derived from EnergyCache quantization.

        When the energy cache has high-confidence quantization data (>= 0.9),
        returns ``quantization_seconds``. Otherwise falls back to 60 seconds
        (the classic default pre-quantization prediction window).

        Safe to call before ``nbc_reader`` is initialized (returns 60), and
        when ``nbc_reader`` is replaced with a mock that doesn't expose
        ``energy_cache``.

        Returns:
            Prediction window in seconds.
        """
        nbc = getattr(self, 'nbc_reader', None)
        if nbc is None:
            return 60
        ec = getattr(nbc, 'energy_cache', None)
        if ec is not None and ec.data is not None:
            qs = ec.quantization_seconds
            qc = ec.quantization_confidence
            if qs is not None and qc is not None and qc >= 0.9:
                return qs
        return 60

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

    def _stage_enabled_check(
        self, ctx: CycleContext
    ) -> CycleResult | None:
        """Stage 1: Check if load management is enabled at ctx.now.

        Returns None to continue, or CycleResult(status='disabled') if
        load management is inactive or outside the configured time window.
        """
        if self.is_enabled_at(ctx.now):
            return None
        return CycleResult(
            status="disabled",
            diagnostics=CycleDiagnostics(
                gap_wh=None,
                hysteresis_wh=self.engine.HYSTERESIS_WH,
                seconds_remaining=None,
                data_point_at=None,
                reason=self._disabled_reason("run_cycle"),
                tesla_configured=self.tesla_ctrl is not None,
                tesla_state=None,
                tesla_error=None,
                plugs_configured=list(self.plugs.keys()),
            ),
            sleep_hint=self.config_interval_secs,
            sleep_hint_at=ctx.now.isoformat(),
        )

    def _stage_nbc_fetch(
        self, ctx: CycleContext
    ) -> CycleResult | None:
        """Stage 2: Fetch NBC data for the current quarter-hour.

        Calls get_current_qh on the NBC reader. If the fetch returns None
        (no incomplete quarter-hour), returns CycleResult(status='no_incomplete_qh').
        Otherwise populates ctx.qh_name, ctx.predicted_wh, ctx.seconds_remaining,
        ctx.data_point_at, and ctx.now_postfetch, then returns None.
        """
        fetch_start = _time_mod.perf_counter()
        qh_result = self.nbc_reader.get_current_qh(force=ctx.force, now=ctx.now)
        if qh_result is None:
            return CycleResult(
                status="no_incomplete_qh",
                diagnostics=CycleDiagnostics(
                    gap_wh=None,
                    hysteresis_wh=self.engine.HYSTERESIS_WH,
                    seconds_remaining=None,
                    data_point_at=None,
                    reason="no_incomplete_qh",
                    tesla_configured=self.tesla_ctrl is not None,
                    tesla_state=None,
                    tesla_error=None,
                    plugs_configured=list(self.plugs.keys()),
                ),
                sleep_hint=DEFAULT_SLEEP_HINT_SECS,
                sleep_hint_at=ctx.now.isoformat(),
            )
        (
            ctx.qh_name,
            ctx.predicted_wh,
            ctx.seconds_remaining,
            ctx.data_point_at,
        ) = qh_result
        fetch_end = _time_mod.perf_counter()
        ctx.now_postfetch = ctx.now + timedelta(seconds=fetch_end - fetch_start)
        return None

    def _stage_compute_gap(self, ctx: CycleContext) -> None:
        """Stage 4: Accept fresh data and compute the Wh gap.

        Prunes old effects, records the data point timestamp, computes
        adjusted_wh (prediction adjusted by still-pending effects), and
        gap_wh (target - adjusted). Mutates ctx in place. Always returns
        None (continue pipeline).
        """
        data_point_at = ctx.data_point_at
        now_postfetch = ctx.now_postfetch
        predicted_wh = ctx.predicted_wh
        seconds_remaining = ctx.seconds_remaining
        assert data_point_at is not None
        assert now_postfetch is not None
        assert predicted_wh is not None
        assert seconds_remaining is not None

        self.state.prune_old_effects(data_point_at, now=now_postfetch)
        self.state.last_data_point_at = data_point_at
        adjusted_wh = self.state.estimated_current_wh(predicted_wh, seconds_remaining)
        gap_wh = self.target_wh - adjusted_wh
        ctx.adjusted_wh = adjusted_wh
        ctx.gap_wh = gap_wh

    def _stage_commit(self, ctx: CycleContext) -> CycleResult | None:
        """Stage 6: Sentinel check, commit effects, Tesla tracking, hysteresis.

        Returns a CycleResult for early-exit conditions (sentinel, hysteresis)
        or None to continue to _stage_build_result.
        """
        if ctx.sentinel_on:
            return CycleResult(
                status="disabled",
                qh=ctx.qh_name,
                predicted_wh=ctx.predicted_wh,
                adjusted_wh=ctx.adjusted_wh,
                target_wh=self.target_wh,
                actions=[],
                diagnostics=CycleDiagnostics(
                    gap_wh=None,
                    hysteresis_wh=self.engine.HYSTERESIS_WH,
                    seconds_remaining=ctx.seconds_remaining,
                    data_point_at=ctx.data_point_at,
                    reason="sentinel_on",
                    pending_effects_count=0,
                    tesla_configured=self.tesla_ctrl is not None,
                    tesla_state=None,
                    tesla_error=None,
                    plugs_configured=list(self.plugs.keys()),
                    sentinel_names=list(self.sentinel_names),
                    sentinel_on=True,
                ),
                sleep_hint=self.config_interval_secs,
                sleep_hint_at=ctx.now_postfetch.isoformat() if ctx.now_postfetch else "",
            )

        # Commit succeeded effects to state
        for effect in ctx.succeeded_effects:
            self.state.pending_effects.append(effect)

        # Track Tesla amp state and settle-window
        now_postfetch = ctx.now_postfetch
        assert now_postfetch is not None
        for effect in ctx.succeeded_effects:
            if effect.device_name == "tesla":
                if effect.action == "set_amps":
                    prev_amps = self.state.last_commanded_amps
                    new_amps = effect.target_amps
                    self.state.last_commanded_amps = new_amps
                    if new_amps is not None and (
                        prev_amps is None or new_amps > prev_amps
                    ):
                        self.state.record_tesla_amp_increase(
                            effect.timestamp, qh_name=ctx.qh_name,
                            data_point_at=effect.data_point_at,
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
                        self.state.record_tesla_amp_decrease(
                            effect.timestamp, qh_name=ctx.qh_name,
                            data_point_at=effect.data_point_at,
                        )
                        logger.debug(
                            "Tesla amp decrease recorded: %s → %d A "
                            "(post-decrease settle window starts)",
                            prev_amps,
                            new_amps,
                        )
                elif effect.action in ("turn_off", "turn_on"):
                    self.state.last_commanded_amps = None
                    self.state.clear_tesla_settle()

        # Hysteresis check
        gap_wh = ctx.gap_wh
        assert gap_wh is not None
        if abs(gap_wh) <= self.engine.HYSTERESIS_WH:
            sentinel_on = any(
                self.state.devices.get(
                    name, DeviceState(name=name)
                ).actual_state is True
                for name in self.sentinel_names
            )
            return CycleResult(
                status="dry-run" if self.dry_run else "ok",
                qh=ctx.qh_name,
                predicted_wh=ctx.predicted_wh,
                adjusted_wh=ctx.adjusted_wh,
                target_wh=self.target_wh,
                actions=[],
                diagnostics=CycleDiagnostics(
                    gap_wh=gap_wh,
                    hysteresis_wh=self.engine.HYSTERESIS_WH,
                    seconds_remaining=ctx.seconds_remaining,
                    data_point_at=ctx.data_point_at,
                    reason="hysteresis",
                    pending_effects_count=len(self.state.pending_effects),
                    tesla_configured=self.tesla_ctrl is not None,
                    tesla_state=_tesla_state_to_dict(ctx.tesla_state),
                    tesla_error=ctx.tesla_error,
                    tesla_login_url=ctx.tesla_login_url,
                    plugs_configured=list(self.plugs.keys()),
                    sentinel_names=list(self.sentinel_names),
                    sentinel_on=sentinel_on,
                ),
                sleep_hint=self.config_interval_secs,
                sleep_hint_at=now_postfetch.isoformat(),
            )

        return None

    def _stage_build_result(self, ctx: CycleContext) -> CycleResult:
        """Stage 7 (final): Build candidate details and construct the result.

        Calls _build_candidate_details and _determine_no_action_reason,
        then constructs and returns the final CycleResult. Always returns
        a CycleResult (never None).
        """
        gap_wh = ctx.gap_wh
        seconds_remaining = ctx.seconds_remaining
        assert gap_wh is not None
        assert seconds_remaining is not None

        tesla_configured = self.tesla_ctrl is not None
        active_telemetry = (
            get_telemetry_snapshot() if has_telemetry() else None
        )
        candidate_details = self._build_candidate_details(
            ctx.now, seconds_remaining, ctx.tesla_state,
            ctx.tesla_error, tesla_configured,
        )
        reason = self._determine_no_action_reason(
            ctx.actions,
            gap_wh,
            ctx.now,
            seconds_remaining,
            ctx.tesla_state,
            tesla_configured,
            ctx.tesla_error,
        )
        return CycleResult(
            status="dry-run" if self.dry_run else "ok",
            qh=ctx.qh_name,
            predicted_wh=ctx.predicted_wh,
            adjusted_wh=ctx.adjusted_wh,
            target_wh=self.target_wh,
            actions=ctx.actions,
            diagnostics=CycleDiagnostics(
                gap_wh=gap_wh,
                hysteresis_wh=self.engine.HYSTERESIS_WH,
                seconds_remaining=seconds_remaining,
                data_point_at=ctx.data_point_at,
                reason=reason,
                pending_effects_count=len(self.state.pending_effects),
                candidates=candidate_details,
                tesla_configured=tesla_configured,
                tesla_state=_tesla_state_to_dict(ctx.tesla_state),
                tesla_error=ctx.tesla_error,
                tesla_login_url=ctx.tesla_login_url,
                plugs_configured=list(self.plugs.keys()),
                sentinel_names=list(self.sentinel_names),
                sentinel_on=any(
                    self.state.devices.get(
                        name, DeviceState(name=name)
                    ).actual_state is True
                    for name in self.sentinel_names
                ),
                telemetry_registered=has_telemetry(),
                active_tesla_telemetry=active_telemetry,
            ),
            sleep_hint=self.config_interval_secs,
            sleep_hint_at=(
                ctx.now_postfetch.isoformat()
                if ctx.now_postfetch else ""
            ),
        )

    def _stage_async_phase(self, ctx: CycleContext) -> None:
        """Stage 5: Run the async portion of the cycle.

        Resets the Tesla controller session, then runs _cycle_async_phase
        via asyncio.run(). Unpacks the 8-tuple result into ctx fields,
        overwriting ctx.gap_wh and ctx.adjusted_wh with corrected values
        from the async phase. Always returns None.
        """
        gap_wh = ctx.gap_wh
        adjusted_wh = ctx.adjusted_wh
        now_postfetch = ctx.now_postfetch
        seconds_remaining = ctx.seconds_remaining
        qh_name = ctx.qh_name
        data_point_at = ctx.data_point_at
        assert gap_wh is not None
        assert adjusted_wh is not None
        assert now_postfetch is not None
        assert seconds_remaining is not None

        if self.tesla_ctrl is not None:
            self.tesla_ctrl.reset_session()

        if self.telegram_sender is not None:
            self.telegram_sender.reset_session()
        (
            ctx.tesla_state,
            ctx.tesla_error,
            ctx.tesla_login_url,
            ctx.succeeded_effects,
            ctx.actions,
            ctx.gap_wh,
            ctx.adjusted_wh,
            ctx.sentinel_on,
        ) = asyncio.run(
            self._cycle_async_phase(
                gap_wh, adjusted_wh, now_postfetch, seconds_remaining,
                self.dry_run, qh_name, data_point_at=data_point_at,
            )
        )

    def _stage_pending_check(
        self, ctx: CycleContext
    ) -> CycleResult | None:
        """Stage 3: Check whether NBC data is stale or pending effects
        are not yet reflected in the prediction.

        When force=True, bypasses all checks and returns None immediately.
        Otherwise returns a CycleResult for early-exit conditions or None
        to continue the pipeline.
        """
        if ctx.force:
            return None

        now_postfetch = ctx.now_postfetch
        data_point_at = ctx.data_point_at
        seconds_remaining = ctx.seconds_remaining
        assert now_postfetch is not None  # guaranteed by Stage 2

        tesla_configured = self.tesla_ctrl is not None
        plugs_configured = list(self.plugs.keys())

        if data_point_at is None:
            candidate_details = self._build_candidate_details(
                now_postfetch, seconds_remaining or 0, None, None, tesla_configured
            )
            return CycleResult(
                status="no_incomplete_qh",
                diagnostics=CycleDiagnostics(
                    gap_wh=None,
                    hysteresis_wh=self.engine.HYSTERESIS_WH,
                    seconds_remaining=None,
                    data_point_at=None,
                    reason="no_incomplete_qh",
                    tesla_configured=tesla_configured,
                    tesla_state=None,
                    tesla_error=None,
                    plugs_configured=plugs_configured,
                ),
                sleep_hint=DEFAULT_SLEEP_HINT_SECS,
                sleep_hint_at=now_postfetch.isoformat(),
            )

        # Stale-data gate: data_point_age > 120 s (accounting for Emporia lag).
        data_lag = self.nbc_reader.get_data_lag_secs()
        base_diag: dict[str, Any] = {
            "gap_wh": None,
            "hysteresis_wh": self.engine.HYSTERESIS_WH,
            "seconds_remaining": seconds_remaining,
            "data_point_at": data_point_at,
            "tesla_configured": tesla_configured,
            "tesla_state": None,
            "tesla_error": None,
            "plugs_configured": plugs_configured,
        }
        if now_postfetch - data_point_at > timedelta(seconds=STALE_DATA_THRESHOLD_SECS):
            pruned = self.state.prune_old_effects(data_point_at, now_postfetch)
            if pruned > 0:
                logger.debug("Pruned %d old pending effects (stale check)", pruned)
            pending_count = self.state.pending_since_count(data_point_at)
            candidate_details = self._build_candidate_details(
                now_postfetch, seconds_remaining or 0, None, None, tesla_configured
            )
            logger.warning(
                "cycle_stale_data data_lag=%ds pending_effects=%d data_point_at=%s",
                data_lag, pending_count, data_point_at.isoformat(),
                extra={"event": "cycle_stale_data", "data_lag_secs": data_lag,
                       "pending_effects_count": pending_count, "reason": "stale_data",
                       "data_point_at": data_point_at.isoformat()},
            )
            return CycleResult(
                status="stale_data",
                diagnostics=CycleDiagnostics(
                    **base_diag,  # type: ignore[arg-type]
                    reason="stale_data",
                    pending_effects_count=pending_count,
                    candidates=candidate_details,
                ),
                sleep_hint=DEFAULT_SLEEP_HINT_SECS,
                sleep_hint_at=now_postfetch.isoformat(),
                candidates=candidate_details,
            )

        # QH boundary check: data_point_at must fall within current quarter-hour.
        current_qh_start, _ = NBCPeriod.current_qh_window(now_postfetch)
        if data_point_at < current_qh_start:
            pruned = self.state.prune_old_effects(data_point_at, now_postfetch)
            if pruned > 0:
                logger.debug("Pruned %d old pending effects (previous QH)", pruned)
            candidate_details = self._build_candidate_details(
                now_postfetch, seconds_remaining or 0, None, None, tesla_configured
            )
            logger.warning(
                "cycle_previous_qh data_point_at=%s pending_effects=%d",
                data_point_at.isoformat(), len(self.state.pending_effects),
                extra={"event": "cycle_previous_qh", "data_point_at": data_point_at.isoformat(),
                       "pending_effects_count": len(self.state.pending_effects)},
            )
            return CycleResult(
                status="stale_data",
                diagnostics=CycleDiagnostics(
                    **base_diag,  # type: ignore[arg-type]
                    reason="previous_qh",
                    pending_effects_count=len(self.state.pending_effects),
                    candidates=candidate_details,
                ),
                sleep_hint=DEFAULT_SLEEP_HINT_SECS,
                sleep_hint_at=now_postfetch.isoformat(),
                candidates=candidate_details,
            )

        # ── Tesla charge-state divergence check ────────────────────────────
        # When the Tesla is drawing significant amps and we didn't command it,
        # the charging may have started externally after the last NBC data
        # point.  In that case the prediction doesn't include this load, so
        # wait for fresh data before making any decisions.
        if tesla_configured and data_point_at is not None:
            charge_last_update = get_field_update_at("ChargeAmps")
            if charge_last_update is not None and charge_last_update > data_point_at:
                snapshot = get_telemetry_snapshot()
                charge_amps = snapshot.get("ChargeAmps")
                if (
                    charge_amps is not None
                    and charge_amps > 0
                    and self.state.last_commanded_amps is None
                ):
                    candidate_details = self._build_candidate_details(
                        now_postfetch, seconds_remaining or 0, None, None,
                        tesla_configured,
                    )
                    logger.warning(
                        "cycle_tesla_external_charge charge_amps=%s "
                        "charge_seen_at=%s data_point_at=%s",
                        charge_amps, charge_last_update.isoformat(),
                        data_point_at.isoformat(),
                        extra={"event": "cycle_tesla_external_charge",
                               "charge_amps": charge_amps,
                               "charge_seen_at": charge_last_update.isoformat(),
                               "data_point_at": data_point_at.isoformat()},
                    )
                    return CycleResult(
                        status="waiting_for_fresh_data",
                        diagnostics=CycleDiagnostics(
                            **base_diag,
                            reason="external_tesla_charge",
                            pending_effects_count=len(self.state.pending_effects),
                            candidates=candidate_details,
                        ),
                        sleep_hint=min(
                            seconds_remaining or 0,
                            self._resolve_prediction_window(),
                        ),
                        sleep_hint_at=(now_postfetch + timedelta(seconds=min(
                            seconds_remaining or 0,
                            self._resolve_prediction_window(),
                        ))).isoformat(),
                        candidates=candidate_details,
                    )

        nbc_timestamp = data_point_at
        if nbc_timestamp is not None and self.state.has_pending_effect_since(
            nbc_timestamp
        ):
            self.state.prune_old_effects(data_point_at, now_postfetch)
            pending_count = len(self.state.pending_effects)
            candidate_details = self._build_candidate_details(
                now_postfetch, seconds_remaining or 0, None, None, tesla_configured
            )
            logger.info(
                "cycle_waiting_for_fresh_data pending_effects=%d seconds_remaining=%s",
                pending_count, seconds_remaining,
                extra={"event": "cycle_waiting_for_fresh_data",
                       "pending_effects_count": pending_count,
                       "seconds_remaining": seconds_remaining},
            )
            return CycleResult(
                status="waiting_for_fresh_data",
                diagnostics=CycleDiagnostics(
                    **base_diag,  # type: ignore[arg-type]
                    reason="waiting_for_fresh_data",
                    pending_effects_count=pending_count,
                    candidates=candidate_details,
                ),
                sleep_hint=min(
                    seconds_remaining or 0, self._resolve_prediction_window()
                ),
                sleep_hint_at=(now_postfetch + timedelta(seconds=min(
                    seconds_remaining or 0, self._resolve_prediction_window()
                ))).isoformat(),
                candidates=candidate_details,
            )

        return None

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
    ) -> list[CandidateDetail]:
        """Build per-device diagnostics for visibility into decisions.

        Args:
            now: current datetime.
            seconds_remaining: Seconds left in current quarter-hour.
            tesla_state: Tesla state if available.
            tesla_error: Error message if Tesla state fetch failed.
            tesla_configured: Whether a Tesla controller is configured.

        Returns:
            List of CandidateDetail objects for all plugs and optionally Tesla.
        """
        candidate_details: list[CandidateDetail] = []
        for name, plug in self.plugs.items():
            dev_state = self.state.devices.get(name)
            # For diagnostics, show whether the device can toggle in the
            # relevant direction: turn-off debounce for devices currently on,
            # turn-on debounce for devices currently off or unknown.
            currently_on = dev_state is not None and dev_state.desired_state is True
            can_toggle = self.state.can_toggle(name, now, turning_on=not currently_on)
            power = plug.power_watts if plug.power_watts is not None else 0.0
            capacity_wh = StateTracker.watts_to_wh(power, seconds_remaining)

            fields: dict[str, Any] = {
                "device_type": "plug",
                "name": name,
                "power_watts": plug.power_watts,
                "capacity_wh": round(capacity_wh, 1),
                "can_toggle": can_toggle,
            }
            if not self._is_device_in_time_range(now, plug.time_range):
                fields["reason"] = "outside_time_range"
            if dev_state:
                fields["desired_state"] = dev_state.desired_state
                fields["actual_state"] = dev_state.actual_state
            candidate_details.append(CandidateDetail(**fields))  # type: ignore[arg-type]

        # Add Tesla to candidate details when configured
        if tesla_configured:
            tesla_fields: dict[str, Any] = {
                "device_type": "tesla",
                "name": "tesla",
                "power_watts": None,
                "capacity_wh": 0.0,
                "can_toggle": False,
            }
            if tesla_state is not None:
                tesla_fields["state_available"] = True
                tesla_fields["is_charging"] = tesla_state.is_charging
                tesla_fields["current_amps"] = tesla_state.current_amps
                tesla_fields["plugged_in"] = tesla_state.plugged_in
                tesla_fields["at_home"] = tesla_state.at_home
            if self.tesla_config and not self._is_device_in_time_range(
                now, self.tesla_config.time_range
            ):
                tesla_fields["reason"] = "outside_time_range"
            candidate_details.append(CandidateDetail(**tesla_fields))  # type: ignore[arg-type]

        return candidate_details

    def _determine_no_action_reason(
        self,
        results: list[PendingEffect],
        gap_wh: float,
        now: datetime,
        seconds_remaining: int,
        tesla_state: TeslaState | None,
        tesla_configured: bool,
        tesla_error: str | None,
    ) -> str:
        """Determine specific reason when no actions were taken.

        Args:
            results: List of executed action PendingEffect objects.
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

    def _calculate_adaptive_sleep(self, cycle_result: CycleResult) -> float:
        """Calculate an adaptive sleep duration based on the current cycle state.

        The returned value is always clamped to [5, config_interval * 2].

        Args:
            cycle_result: The CycleResult from run_cycle().

        Returns:
            Suggested sleep duration in seconds.
        """
        status = cycle_result.status
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
        seconds_remaining = self._seconds_remaining(cycle_result)

        # --- Waiting for fresh data: wait until next NBC point ---
        if status == "waiting_for_fresh_data":
            return min(seconds_remaining, self._resolve_prediction_window())

        # --- Shared lookups ---
        predicted_wh = cycle_result.predicted_wh

        # --- No deficit (predicted >= target): check how much QH remains ---
        # When predicted_wh is unknown (None), fall through to deficit handling
        if predicted_wh is not None and predicted_wh >= self.target_wh:
            # No deficit. Early in the quarter -> sleep longer; late -> wake sooner.
            if seconds_remaining > 300:  # more than 5 min left in QH
                return min(config_interval * 1.5, config_interval * 2)

            return min(config_interval * 1.25, config_interval * 2)

        # --- Deficit with no actions possible: proportional sleep ---
        # Guard against None predicted_wh; if unknown, use a sensible default gap
        gap_wh = predicted_wh if predicted_wh is not None else self.target_wh or -500
        gap = abs(gap_wh - self.target_wh) if self.target_wh is not None else 0.0

        # Calculate total eligible capacity from candidates
        max_load_capacity = 0.0
        for candidate in cycle_result.candidates or []:
            if candidate.power_watts is not None:
                max_load_capacity += candidate.power_watts

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
    def _seconds_remaining(cycle_result: CycleResult) -> float:
        """Extract seconds_remaining from a CycleResult diagnostics.

        Args:
            cycle_result: The CycleResult from run_cycle().

        Returns:
            The seconds remaining in the current quarter-hour.
        """
        diagnostics = cycle_result.diagnostics
        if diagnostics is not None:
            return float(diagnostics.seconds_remaining or 0)
        return 0.0

    async def _fetch_tesla_state_async(
        self,
    ) -> tuple[TeslaState | None, str | None, str | None]:
        """Fetch Tesla charging state from MQTT telemetry, with REST fallback.

        Tries MQTT telemetry first. If telemetry is not yet available, delegates
        to the controller's ``init_tesla_state(timeout=0)`` which skips the
        telemetry wait (the fast path already checked) and jumps directly to
        the REST API fallback.
        The result is cached on the controller so subsequent calls are fast.

        Returns:
            Tuple of (tesla_state, tesla_error, tesla_login_url).
        """
        if self.tesla_ctrl is None:
            return None, None, None

        # Fast path: use live telemetry state whenever available.
        telemetry_state: TeslaState | None = None
        telemetry_snapshot: dict[str, Any] | None = None
        if has_telemetry():
            telemetry_snapshot = get_telemetry_snapshot()
            telemetry_state = tesla_state_from_snapshot(telemetry_snapshot)
            if telemetry_state is not None:
                # Track at_home from Location snapshots; preserve the last
                # known at_home when Location is absent so the
                # requires_home_check gate doesn't block Tesla decisions.
                if "Location" in telemetry_snapshot:
                    self._last_tesla_at_home = telemetry_state.at_home
                    return telemetry_state, None, None
                if self._last_tesla_at_home is not None:
                    if not telemetry_state.at_home:
                        telemetry_state = TeslaState(
                            is_charging=telemetry_state.is_charging,
                            current_amps=telemetry_state.current_amps,
                            plugged_in=telemetry_state.plugged_in,
                            at_home=self._last_tesla_at_home,
                        )
                    return telemetry_state, None, None
                # _last_tesla_at_home is None (never seeded) and Location is
                # absent — fall through to REST fallback to seed it.
                # This handles the bootstrapping race where ChargeAmps arrives
                # (15s interval) before Location (120s interval) so the
                # telemetry fast path would otherwise return at_home=False
                # without ever reaching the REST fallback below.
            # Telemetry exists but no parseable state yet (e.g. only
            # DetailedChargeState="Disconnected" without ChargeAmps).
            # Fall through to the controller's cached state.

        # REST fallback to seed _last_tesla_at_home and/or obtain location.
        if not isinstance(self.tesla_ctrl, RealTeslaController):
            return telemetry_state, None, None

        try:
            rest_state = await self.tesla_ctrl.init_tesla_state(timeout=0)
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            # TeslaFleetError (incl. VehicleOffline) inherits from BaseException
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            logger.warning(
                "_fetch_tesla_state_async: init_tesla_state failed: %s", exc,
            )
            error = str(exc)
            login_url = self.tesla_ctrl.get_login_url()
            # Return telemetry state if available, not None.
            return telemetry_state, error, login_url

        # Seed _last_tesla_at_home from the controller's REST init state so
        # that when telemetry later arrives without Location, the preservation
        # logic can keep at_home=True rather than flipping to False.
        if rest_state is not None:
            self._last_tesla_at_home = rest_state.at_home
            # Merge REST at_home into telemetry state — telemetry has fresher
            # ChargeAmps from MQTT, REST has accurate location data.
            if telemetry_state is not None and "Location" not in (
                telemetry_snapshot or {}
            ):
                return TeslaState(
                    is_charging=telemetry_state.is_charging,
                    current_amps=telemetry_state.current_amps,
                    plugged_in=telemetry_state.plugged_in,
                    at_home=rest_state.at_home,
                ), None, None
            return rest_state, None, None

        # REST failed — fall back to telemetry state if available.
        if telemetry_state is not None:
            return telemetry_state, None, None
        return None, None, None

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

    async def _fire_telegram_notification(
        self,
        actions: list[PendingEffect],
        predicted_wh: float,
        target_wh: float,
        dry_run: bool,
        now: datetime | None = None,
    ) -> bool:
        """Send a Telegram notification for successful plug actions.

        Builds a surplus notification from the actions and sends it via the
        configured TelegramSender. Skipped when the sender is not configured,
        dry-run mode is active, or there are no actions.

        Args:
            actions: List of PendingEffect actions describing what was done.
            predicted_wh: The adjusted predicted Wh for the current quarter-hour.
            target_wh: The target Wh for the current quarter-hour.
            dry_run: Whether load management is in dry-run mode.
            now: Current time, or the current time in UTC if None.

        Returns:
            True if a notification was sent successfully, False otherwise.
        """
        # Guard: no sender, not configured, dry-run, or no actions
        if self.telegram_sender is None:
            logger.info(
                "Telegram notification skipped: sender not configured (LoadManager.telegram_sender is None)",
            )
            return False
        if not self.telegram_sender.is_configured:
            logger.info(
                "Telegram notification skipped: sender not configured (is_configured=False)",
            )
            return False
        if dry_run:
            logger.info(
                "Telegram notification skipped: dry-run mode",
            )
            return False
        if not actions:
            gap_wh = predicted_wh - target_wh
            logger.info(
                "Telegram notification skipped: no actions (gap=%+.1f Wh)",
                gap_wh,
            )
            return False

        if now is None:
            now = self._clock.now()

        event = build_notification(
            actions=actions,
            predicted_wh=predicted_wh,
            target_wh=target_wh,
            now=now,
        )
        logger.debug("Telegram send event=%s", event)

        # Whitelist gate: notifications are only sent when a telegram.devices
        # whitelist is explicitly configured AND at least one action matches it.
        # When no whitelist is configured (_telegram_devices is None),
        # notifications are blocked — users must list devices to enable them.
        if self._telegram_devices is None:
            logger.info(
                "Telegram notification skipped: telegram.devices whitelist not configured",
            )
            return False
        matches = any(
            a.device_name.lower() in self._telegram_devices
            and a.action in self._telegram_devices[a.device_name.lower()]
            for a in actions
        )
        if not matches:
            logger.info(
                "Telegram notification skipped: no actions match telegram.devices whitelist",
            )
            return False

        sent = False
        try:
            sent = await self.telegram_sender.send_notification(event)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("Telegram send failed: %s", e)

        if sent:
            device_names = ", ".join(a.device_name for a in actions)
            logger.info(
                "Telegram notification sent: devices=%s gap=%+.1f Wh",
                device_names,
                predicted_wh - target_wh,
            )
        else:
            logger.info(
                "Telegram notification not sent: devices=%s gap=%+.1f Wh",
                ", ".join(a.device_name for a in actions),
                predicted_wh - target_wh,
            )

        return sent

    async def _fire_auth_error_notification(
        self, error_msg: str, login_url: str | None = None,
    ) -> bool:
        """Send a Telegram alert for a vehicle auth error.

        Unlike the surplus notification, this bypasses the devices whitelist
        and dry-run guard — auth errors should always alert when enabled.

        Args:
            error_msg: The auth error message text.
            login_url: Optional Tesla OAuth login URL to include in the alert.

        Returns:
            True if a notification was sent, False otherwise.
        """
        if self.telegram_sender is None:
            logger.info(
                "Auth error notification skipped: sender not configured",
            )
            return False
        if not self.telegram_sender.is_configured:
            logger.info(
                "Auth error notification skipped: sender not configured "
                "(is_configured=False)",
            )
            return False
        if not self._telegram_alert_on_auth_error:
            logger.info(
                "Auth error notification skipped: alert_on_auth_error is False",
            )
            return False

        now = self._clock.now()
        event = build_error_notification(error_msg, now=now, login_url=login_url)
        sent = False
        try:
            sent = await self.telegram_sender.send_notification(event)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("Auth error notification send failed: %s", e)

        if sent:
            logger.info(
                "Auth error notification sent: error=%s",
                error_msg,
            )
        else:
            logger.info(
                "Auth error notification not sent: error=%s",
                error_msg,
            )
        return sent

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
        list[PendingEffect],
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

        # Alert on Tesla auth errors (dedup by message text to avoid spam).
        if tesla_error is not None and tesla_error != self._last_auth_error_msg:
            self._last_auth_error_msg = tesla_error
            await self._fire_auth_error_notification(tesla_error, tesla_login_url)
        elif tesla_error is None:
            self._last_auth_error_msg = None  # reset dedup on success

        # Fold live Tesla in-flight contribution into the gap.  Tesla set_amps
        # effects are excluded from estimated_current_wh() so this live
        # recalculation prevents the adjustment from drifting as
        # seconds_remaining shrinks.
        tesla_reported = tesla_state.current_amps if tesla_state is not None else None
        inflight_wh = self.state.tesla_inflight_wh(
            tesla_reported, seconds_remaining, now=now, data_point_at=data_point_at,
        )
        corrected_adjusted_wh = adjusted_wh + inflight_wh
        corrected_gap_wh = self.target_wh - corrected_adjusted_wh
        logger.info(
            "tesla_inflight_correction tesla_reported=%s inflight_wh=%.1f "
            "corrected_gap=%.1f corrected_adjusted=%.1f",
            tesla_reported, inflight_wh, corrected_gap_wh, corrected_adjusted_wh,
            extra={"event": "tesla_inflight_correction", "reported_amps": tesla_reported,
                   "inflight_wh": inflight_wh, "corrected_gap_wh": corrected_gap_wh,
                   "corrected_adjusted_wh": corrected_adjusted_wh,
                   "last_commanded_amps": self.state.last_commanded_amps},
        )

        # Hysteresis guard uses corrected gap so in-flight Tesla draw is counted.
        if abs(corrected_gap_wh) <= self.engine.HYSTERESIS_WH:
            return tesla_state, tesla_error, tesla_login_url, [], [], corrected_gap_wh, corrected_adjusted_wh, False

        # ── Settle-window suppression ──────────────────────────────────────────
        # After a Tesla amp increase the NBC prediction needs a few cycles to
        # absorb the new load — the Emporia API data lags behind wall-clock time
        # by ~60 s, so some of the fetch history is still at the old amp level.
        # Combined with solar variability this can produce a large apparent
        # deficit on the very first post-confirmation cycle, triggering an
        # unwarranted cascade of plug shutoffs and Tesla stop.
        #
        # Similarly, after an amp decrease the prediction still includes the
        # now-removed load, producing a large apparent surplus that would
        # trigger re-enabling loads that were just turned off (bounce-back).
        #
        # Suppress decisions while the relevant settle window is active AND
        # the apparent gap is below ``TESLA_SETTLE_SUPPRESS_WH``.  Large
        # genuine gaps (e.g. new QH, clouds) still fire immediately; the
        # QH-name check inside each ``is_settling_*`` method auto-expires
        # the window on a QH transition.
        settle_now = now
        # ── Post-increase: suppress turn-off ──
        if (
            corrected_gap_wh < 0
            and abs(corrected_gap_wh) < self.engine.TESLA_SETTLE_SUPPRESS_WH
            and self.state.is_settling_after_amp_increase(settle_now, current_qh=qh_name, data_point_at=data_point_at)
        ):
            settle_remaining = (
                self.state.effective_settle_secs
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
        # ── Post-decrease: suppress turn-on ──
        if (
            corrected_gap_wh > 0
            and corrected_gap_wh < self.engine.TESLA_SETTLE_SUPPRESS_WH
            and self.state.is_settling_after_amp_decrease(settle_now, current_qh=qh_name, data_point_at=data_point_at)
        ):
            settle_remaining = (
                self.state.effective_settle_secs
                - (settle_now - self.state.last_tesla_decrease_at).total_seconds()  # type: ignore[operator]
            )
            logger.info(
                "[_cycle_async_phase] suppressing turn-on: settling after Tesla "
                "amp decrease (gap=%.1f Wh, %.0f s left in settle window)",
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
                requires_home_check=(
                    self.tesla_config is not None
                    and self.tesla_config.home_lat is not None
                    and self.tesla_config.home_lon is not None
                ),
            ),
            predicted_wh=corrected_adjusted_wh,
            target_wh=self.target_wh,
        )

        succeeded_effects: list[PendingEffect] = []
        results: list[PendingEffect] = []
        for action in actions:
            # Suppress Tesla turn_on when active telemetry confirms charging.
            # The callback updates telemetry with a ~10 s delay; if the car is
            # confirmed charging, dispatching a turn_on action is wasteful and
            # defeats the purpose of the telemetry feedback loop.
            if dry_run:
                logger.info(
                    "[DRY-RUN] Would execute: %s on %s",
                    action.action,
                    action.device_name,
                )
                results.append(action)
            else:
                success = await self._execute_action(action)
                if success:
                    succeeded_effects.append(action)
                    results.append(action)

        # Fire Telegram notification for successful plug actions (non-dry-run).
        logger.info(
            "Telegram notification: actions=%d dry_run=%s sender=%s",
            len(results), dry_run, self.telegram_sender is not None,
        )
        await self._fire_telegram_notification(
            actions=results,
            predicted_wh=corrected_adjusted_wh,
            target_wh=self.target_wh,
            dry_run=dry_run,
            now=now,
        )

        return tesla_state, tesla_error, tesla_login_url, succeeded_effects, results, corrected_gap_wh, corrected_adjusted_wh, False

    @staticmethod
    def _plug_states_from_candidates(
        candidate_details: list[CandidateDetail],
    ) -> dict[str, Any]:
        """Extract a compact per-plug state summary for log messages.

        Args:
            candidate_details: Output of _build_candidate_details.

        Returns:
            Dict keyed by plug name with desired/actual/can_toggle fields,
            excluding the tesla entry.
        """
        return {
            c.name: {
                "desired": c.desired_state,
                "actual": c.actual_state,
                "can_toggle": c.can_toggle,
            }
            for c in candidate_details
            if c.name != "tesla"
        }

    def run_cycle(self, force: bool = False) -> CycleResult:
        """Execute one load management cycle. Returns a CycleResult.

        Thread-safe: acquires lock for entire cycle to prevent race conditions
        with concurrent endpoint calls.

        The cycle runs as a seven-stage pipeline:
            1. Enabled check — bail early if disabled or outside time window.
            2. NBC fetch — obtain the current quarter-hour prediction.
            3. Pending-state check — skip if data is stale or pending effects
               are not yet reflected in the NBC data.
            4. Compute gap — prune old effects, compute adjusted Wh and gap.
            5. Async phase (single event loop) — fetch Tesla state, call
               decide(), execute all actions.
            6. Commit effects — sentinel check, persist succeeded effects,
               track Tesla amp state, hysteresis early exit.
            7. Build result — candidate details, no-action reason, final
               CycleResult.

        Args:
            force: If True, bypass stale-data check (debug only).

        Returns:
            CycleResult with status, diagnostics, and sleep_hint.
        """
        with self._lock:
            ctx = CycleContext(now=self._clock.now(), force=force)
            logger.info("cycle_start force=%s",
                        force,
                        extra={"event": "cycle_start", "force": force})

            _t0 = _time_mod.perf_counter()
            logger.debug("cycle_stage=enabled_check force=%s", force)
            if (result := self._stage_enabled_check(ctx)):
                ctx.timings["enabled_check"] = _time_mod.perf_counter() - _t0
                logger.info("cycle_early_exit stage=enabled_check status=%s reason=%s",
                            result.status, result.diagnostics.reason if result.diagnostics else "none",
                            extra={"event": "cycle_early_exit", "stage": "enabled_check",
                                   "status": result.status,
                                   "reason": result.diagnostics.reason if result.diagnostics else "none"})
                return result
            ctx.timings["enabled_check"] = _time_mod.perf_counter() - _t0

            _t0 = _time_mod.perf_counter()
            logger.debug("cycle_stage=nbc_fetch")
            if (result := self._stage_nbc_fetch(ctx)):
                ctx.timings["nbc_fetch"] = _time_mod.perf_counter() - _t0
                logger.info("cycle_early_exit stage=nbc_fetch status=%s reason=%s",
                            result.status, result.diagnostics.reason if result.diagnostics else "none",
                            extra={"event": "cycle_early_exit", "stage": "nbc_fetch",
                                   "status": result.status,
                                   "reason": result.diagnostics.reason if result.diagnostics else "none"})
                return result
            ctx.timings["nbc_fetch"] = _time_mod.perf_counter() - _t0

            _t0 = _time_mod.perf_counter()
            logger.debug("cycle_stage=pending_check")
            if (result := self._stage_pending_check(ctx)):
                ctx.timings["pending_check"] = _time_mod.perf_counter() - _t0
                logger.info("cycle_early_exit stage=pending_check status=%s reason=%s",
                            result.status, result.diagnostics.reason if result.diagnostics else "none",
                            extra={"event": "cycle_early_exit", "stage": "pending_check",
                                   "status": result.status,
                                   "reason": result.diagnostics.reason if result.diagnostics else "none"})
                return result
            ctx.timings["pending_check"] = _time_mod.perf_counter() - _t0

            _t0 = _time_mod.perf_counter()
            logger.debug("cycle_stage=compute_gap predicted=%.1f",
                         ctx.predicted_wh)
            self._stage_compute_gap(ctx)
            ctx.timings["compute_gap"] = _time_mod.perf_counter() - _t0

            _t0 = _time_mod.perf_counter()
            logger.debug("cycle_stage=async_phase")
            self._stage_async_phase(ctx)
            ctx.timings["async_phase"] = _time_mod.perf_counter() - _t0

            _t0 = _time_mod.perf_counter()
            logger.debug("cycle_stage=commit sentinel=%s effects=%d",
                         ctx.sentinel_on, len(ctx.succeeded_effects))
            if (result := self._stage_commit(ctx)):
                ctx.timings["commit"] = _time_mod.perf_counter() - _t0
                logger.info("cycle_early_exit stage=commit status=%s reason=%s",
                            result.status, result.diagnostics.reason if result.diagnostics else "none",
                            extra={"event": "cycle_early_exit", "stage": "commit",
                                   "status": result.status,
                                   "reason": result.diagnostics.reason if result.diagnostics else "none"})
                return result
            ctx.timings["commit"] = _time_mod.perf_counter() - _t0

            _t0 = _time_mod.perf_counter()
            logger.debug("cycle_stage=build_result")
            result = self._stage_build_result(ctx)
            ctx.timings["build_result"] = _time_mod.perf_counter() - _t0
            reason = result.diagnostics.reason if result.diagnostics else "none"
            logger.info("cycle_complete status=%s reason=%s actions=%d sleep_hint=%.1f timings=%s",
                        result.status, reason, len(result.actions), result.sleep_hint, ctx.timings,
                        extra={"event": "cycle_complete", "status": result.status,
                               "reason": reason, "actions_count": len(result.actions),
                               "sleep_hint": result.sleep_hint, "timings": ctx.timings,
                               "gap_wh": ctx.gap_wh, "adjusted_wh": ctx.adjusted_wh,
                               "predicted_wh": ctx.predicted_wh, "qh_name": ctx.qh_name})
            return result

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
        """Execute a Tesla charging action.

        ``turn_on`` is disabled — the load manager must not start charging.
        The car must be started manually or via a separate automation.
        This is a no-op that logs a warning and returns False.

        ``turn_off`` maps to ``stop_charging`` and ``set_amps`` maps to
        ``set_charge_amps`` (or ``stop_charging`` when amps < 5).

        Returns:
            True on success, False on failure or when the action is suppressed.
        """
        assert self.tesla_config is not None
        assert self.tesla_ctrl is not None

        # turn_on is disabled: the load manager must not start charging.
        if action.action == "turn_on":
            logger.warning(
                "Tesla turn_on is disabled: the load manager must not start "
                "charging. Please start the car manually or via separate automation.",
                extra={"event": "tesla_turn_on_disabled"},
            )
            return False

        if action.action == "turn_off":
            try:
                return await self.tesla_ctrl.stop_charging()
            except TeslaAuthError as e:
                await self._fire_auth_error_notification(str(e))
                return False
            except Exception as e:
                logger.error("Failed to stop Tesla charging: %s", e)
                return False
        if action.action == "set_amps":
            if action.target_amps is None or action.target_amps < 5:
                try:
                    return await self.tesla_ctrl.stop_charging()
                except TeslaAuthError as e:
                    await self._fire_auth_error_notification(str(e))
                    return False
                except Exception as e:
                    logger.error("Failed to stop Tesla charging: %s", e)
                    return False
            clamped_amps = min(action.target_amps, self.tesla_config.charge_amps_max)
            clamped_amps = min(clamped_amps, GapMinder.HARD_MAX_AMPS)
            try:
                return await self.tesla_ctrl.set_charge_amps(clamped_amps)
            except TeslaAuthError as e:
                await self._fire_auth_error_notification(str(e))
                return False
            except Exception as e:
                logger.error("Failed to set Tesla charge amps: %s", e)
                return False
        logger.warning("Unknown Tesla action: %s", action.action)
        return False

    def close(self) -> None:
        """Close the LoadManager and release resources.

        Calls close on the Tesla controller to clean up any open aiohttp
        sessions. Also closes the TelegramSender if one is configured.
        Safe to call multiple times.
        """
        if self.tesla_ctrl is not None:
            try:
                asyncio.run(self.tesla_ctrl.close())
            except Exception as e:
                logger.warning("Failed to close Tesla controller: %s", e)

        if self.telegram_sender is not None:
            try:
                asyncio.run(self.telegram_sender.close())
            except Exception as e:
                logger.warning("Failed to close TelegramSender: %s", e)

    def provision_fleet_telemetry(
        self, config: FleetTelemetryProvisionConfig
    ) -> bool:
        """Provision the fleet-telemetry server via the Tesla Fleet API.

        Calls ``fleet_telemetry_config_create`` on the API and logs the result.

        Args:
            config: Provisioning parameters (hostname, CA cert, intervals).

        Returns:
            True on success, False on failure.
        """
        from load_controllers import fleet_telemetry_config_create  # avoid circular

        if self.tesla_ctrl is None:
            logger.error("provision_fleet_telemetry: No Tesla controller configured.")
            return False
        if not isinstance(self.tesla_ctrl, RealTeslaController):
            logger.error("provision_fleet_telemetry: Stub controller — cannot provision.")
            return False

        # Validate that a vehicle-command proxy URL is configured.
        proxy_url = self.tesla_ctrl.config.vehicle_command_proxy_url
        if not proxy_url:
            logger.error(
                "provision_fleet_telemetry: TESLA_VEHICLE_COMMAND_PROXY_URL is not configured. "
                "Set it to your vehicle-command proxy URL (e.g., https://localhost:4444)."
            )
            print(
                "ERROR: TESLA_VEHICLE_COMMAND_PROXY_URL is not configured.  "
                "Set it to your vehicle-command proxy URL (e.g., https://localhost:4444).",
                file=sys.stderr,
            )
            return False

        # Pre-flight: verify Tesla OAuth tokens exist before attempting API calls.
        # This gives a clear, actionable message instead of a cryptic "Refresh token
        # is missing" from deep inside the API client.
        tokens = load_tesla_tokens()
        if tokens is None:
            logger.error(
                "provision_fleet_telemetry: No Tesla OAuth tokens found. "
                "Run 'python app.py --tesla-auth' on a machine with a browser to "
                "authenticate, then copy .tesla-tokens.json to this machine."
            )
            return False

        try:
            asyncio.run(fleet_telemetry_config_create(self.tesla_ctrl, config))
            logger.info("provision_fleet_telemetry: Provisioning succeeded.")
            from mqtt_telemetry import _FLEET_TELEMETRY_DOTFILE
            _FLEET_TELEMETRY_DOTFILE.write_text(
                datetime.now(tz=timezone.utc).isoformat() + "\n"
            )
            logger.info(
                "provision_fleet_telemetry: wrote dotfile %s",
                _FLEET_TELEMETRY_DOTFILE,
            )
            return True
        except Exception as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            logger.error("provision_fleet_telemetry: Provisioning failed: %s", e)
            return False


def provision_fleet_telemetry(config: FleetTelemetryProvisionConfig) -> bool:
    """CLI entrypoint for provisioning the fleet-telemetry server.

    Constructs a minimal LoadManager from env vars and calls
    ``provision_fleet_telemetry(config)``.

    Args:
        config: Provisioning parameters.

    Returns:
        True on success, False on failure.
    """
    try:
        lm = LoadManager()
        return lm.provision_fleet_telemetry(config)
    except Exception as e:
        logger.error("provision_fleet_telemetry CLI failed: %s", e)
        return False


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
    "FleetTelemetryProvisionConfig",
    "TeslaVehicleTelemetry",
    "VocolincPlugController",
    "_parse_load_manage_enabled",
    "_tesla_state_to_dict",
    "load_tesla_tokens",
    "pair_homekit_accessory",
    "provision_fleet_telemetry",
    "remove_tesla_tokens",
    "save_tesla_tokens",
    "tesla_auth_cli",
]
