"""
NBC reader, device state tracking, and the bin-packing decision engine.

NBCReader reads current quarter-hour predictions from a shared EnergyCache
instance instead of maintaining its own NBCCache layer. NBC quarters are
computed on demand from raw per-second samples via util.compute_nbc_quarters().
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from load_models import DeviceState, PendingEffect, TeslaState, TeslaVehicleTelemetry

from constants import DEFAULT_PREDICTION_WINDOW_SECS

# Deferred import to avoid circular dependency with metrics module.
_energy_cache_type: Any = None


def _get_energy_cache_type() -> Any:
    """Return the EnergyCache class, importing lazily to avoid circular deps."""
    global _energy_cache_type
    if _energy_cache_type is None:
        from metrics import EnergyCache  # type: ignore[assignment]
        _energy_cache_type = EnergyCache
    return _energy_cache_type


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedMetricsQH:
    """Parsed quarter-hour prediction from _parse_metrics.

    Replaces the dict return from _parse_metrics().
    """
    qh_name: str
    predicted_wh: float
    seconds_remaining: int
    data_lag_secs: float

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for backward compat."""
        return {
            "qh_name": self.qh_name,
            "predicted_wh": self.predicted_wh,
            "seconds_remaining": self.seconds_remaining,
            "_data_lag_secs": self.data_lag_secs,
        }


class NBCPeriod:
    """A fixed-length quarter-hour period within an NBC cycle.

    Each period is 900 seconds (15 minutes) long and is used to align
    NBC predictions into hourly windows for load management decisions.

    Attributes:
        PERIOD_SECS: Duration of each period in seconds (always 900).
    """

    PERIOD_SECS = 900

    @staticmethod
    def current_qh_window(now: datetime) -> tuple[datetime, datetime]:
        """Return (start, end) of the QH window containing ``now``.

        Quarter-hour periods are aligned to hour boundaries:

          - QH1: seconds 0-899   (minutes 0-14)
          - QH2: seconds 900-1799 (minutes 15-29)
          - QH3: seconds 1800-2699 (minutes 30-44)
          - QH4: seconds 2700-3599 (minutes 45-59)

        Args:
            now: A timezone-aware or naive datetime. Naive datetimes are
                treated as UTC.

        Returns:
            Tuple of (window_start, window_end) datetimes in the same
            timezone as ``now``.  If ``now`` is naive, both boundaries are
                also naive (UTC).
        """
        utc_now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
        seconds_into_hour = utc_now.hour * 3600 + utc_now.minute * 60 + utc_now.second
        qh_index = seconds_into_hour // NBCPeriod.PERIOD_SECS  # 0-3
        qh_start_seconds = qh_index * NBCPeriod.PERIOD_SECS

        # Build the start of this QH window.
        qh_start = utc_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(seconds=qh_start_seconds)
        window_end = qh_start + timedelta(seconds=NBCPeriod.PERIOD_SECS)

        # Return in the same "style" as input (naive vs aware).
        if now.tzinfo is None:
            return qh_start.replace(tzinfo=None), window_end.replace(tzinfo=None)  # type: ignore[return-value]
        return qh_start, window_end


class NBCReader:
    """Reads current QH predicted_wh from cached energy samples.

    Reads directly from EnergyCache instead of wrapping a fetch callable
    and using NBCCache. NBC quarters are computed on demand from raw samples.

    Attributes:
        energy_cache: Shared EnergyCache instance for reading cached per-second data.
        device_name: Name of the VUE device to query (for future multi-device support).
    """

    def __init__(
        self, energy_cache: Any | None = None, device_name: str = ""
    ) -> None:
        """Initialize NBCReader with an optional EnergyCache and device name.

        Args:
            energy_cache: Shared EnergyCache instance for reading cached per-second data.
                When None, creates a default EnergyCache(ttl_seconds=30).
            device_name: Name of the VUE device to query. Defaults to empty string.
        """
        EnergyCacheType = _get_energy_cache_type()
        self.energy_cache: Any = energy_cache or EnergyCacheType(ttl_seconds=30)
        self.device_name = device_name
        # Callable injected by LoadManager to fetch raw metrics data.
        self._metrics_fetch: Any | None = None

    def get_data_lag_secs(self) -> float:
        """Return the data lag in seconds from the underlying energy cache.

        Returns:
            The data lag in seconds (0.0 when unavailable).
        """
        return getattr(
            self.energy_cache, "_data_lag_secs", 0.0
        )

    def get_current_qh(
        self, now: datetime, force: bool = False
    ) -> tuple[str, float, int, datetime] | None:
        """Return (qh_name, predicted_wh, seconds_remaining, data_point_at).

        Uses EnergyCache.get_current_qh() to extract QH prediction from cached
        per-second samples. When force=True, bypasses cache and triggers a fresh
        fetch via ``_metrics_fetch`` if available.

        Args:
            force: When True, bypass cache and always fetch fresh data from the API.
            now: Current time for TTL check. Required.

        Returns:
            Tuple of (qh_name, predicted_wh in Wh, seconds remaining in QH,
            data_point_at), or None if no incomplete QH available.
        """
        # Fast path: cache is valid — read directly from it.
        if not force and self.energy_cache.is_valid(now=now):
            qh_data = self.energy_cache.get_current_qh(now=now)
            if qh_data is not None:
                # Cache hit with incomplete QH — return it.
                fetched_at = self.energy_cache.last_fetch_at
                if fetched_at is None:
                    fetched_at = now
                lag_secs = getattr(
                    self.energy_cache, "_data_lag_secs", 0.0
                )
                data_point_at = fetched_at - timedelta(seconds=lag_secs)
                return (  # type: ignore[return-value]
                    qh_data["qh_name"],
                    qh_data.get("predicted_wh", 0),
                    qh_data.get("seconds_remaining", 0),
                    data_point_at,
                )
            # Cache is valid but no incomplete QH (QH1 is complete) — fall
            # through to the fetch path below so we can check for a newer
            # incomplete quarter that may have started since the cache was
            # populated.

        # Try to fetch fresh data via _metrics_fetch when the cache is not
        # valid, or when the cache is valid but has no incomplete QH.
        if hasattr(self, "_metrics_fetch") and self._metrics_fetch is not None:
            metrics_data = self._metrics_fetch()
            if metrics_data is None:
                return None
            # Tag with wall-clock time so _parse_metrics can compute
            # seconds_remaining from the clock rather than sample counts.
            if metrics_data is not None:
                metrics_data["_now"] = now
            parsed = self._parse_metrics(self.device_name, metrics_data)
            if parsed is None:
                return None
            fetched_at = metrics_data.get("_fetched_at", now)
            data_point_at = fetched_at - timedelta(seconds=parsed.data_lag_secs)
            return (  # type: ignore[return-value]
                parsed.qh_name,
                parsed.predicted_wh,
                parsed.seconds_remaining,
                data_point_at,
            )

        # force=True but no fetch callable: fall back to reading from cache.
        if force:
            qh_data = self.energy_cache.get_current_qh(now=now)
            if qh_data is None:
                return None
            fetched_at = self.energy_cache.last_fetch_at
            if fetched_at is None:
                fetched_at = now
            lag_secs = getattr(
                self.energy_cache, "_data_lag_secs", 0.0
            )
            data_point_at = fetched_at - timedelta(seconds=lag_secs)
            return (  # type: ignore[return-value]
                qh_data["qh_name"],
                qh_data.get("predicted_wh", 0),
                qh_data.get("seconds_remaining", 0),
                data_point_at,
            )

        return None

    def get_current_qh_direct(
        self, metrics_data: dict[str, Any] | None
    ) -> tuple[str, float, int] | None:
        """Parse metrics data directly without cache.

        Useful for testing with injected mock data. Unchanged from current
        implementation except device_name is no longer needed (first device found).

        Args:
            metrics_data: The raw metrics dict from HourlyProjection, or None.

        Returns:
            Tuple of (qh_name, predicted_wh in Wh, seconds remaining in QH),
            or None if no incomplete QH available.
        """
        result = self._parse_metrics(self.device_name, metrics_data)
        if result is None:
            return None
        return result.qh_name, result.predicted_wh, result.seconds_remaining

    def _parse_metrics(
        self, device_name: str, metrics_data: dict[str, Any] | None,
    ) -> ParsedMetricsQH | None:
        """Parse metrics data and extract incomplete QH info.

        Maps hour-relative NBC quarters to clock-boundary semantics:
        QH1 = most recent 15-min window (per _clock_boundary_windows).

        Args:
            device_name: Name of the VUE device to query (reserved for future use).
            metrics_data: The raw metrics dict from HourlyProjection, or None.

        Returns:
            Dict with qh_name, predicted_wh, seconds_remaining, _data_lag_secs, or None.
        """
        if metrics_data is None:
            return None

        devices = metrics_data.get("devices", [])
        target_device = None
        for dev in devices:
            if dev.get("name") == device_name:
                target_device = dev
                break

        if target_device is None and devices:
            # No device name match — use the first device with NBC data.
            target_device = devices[0] if devices else None

        if target_device is None:
            return None

        nbc = target_device.get("nbc")
        if nbc is None:
            return None

        qh_order = ["QH1", "QH2", "QH3", "QH4"]
        incomplete_result: ParsedMetricsQH | None = None

        for qh_name in qh_order:
            qh_data = nbc.get(qh_name)
            if qh_data is None:
                continue
            if not qh_data.get("complete", True):
                predicted_wh = qh_data.get("predicted_wh", 0)
                # Derive seconds_remaining from wall-clock time so it stays
                # monotonic across cache refreshes even when sample counts
                # fluctuate due to API delivery latency.
                now = metrics_data.get("_now")
                if now is not None:
                    offset_in_hour = now.second + (now.minute % 15) * 60
                    remaining_seconds = 900 - offset_in_hour
                    remaining_seconds = max(0, remaining_seconds)
                else:
                    remaining_seconds = qh_data.get(
                        "remaining_seconds", NBCPeriod.PERIOD_SECS
                    )
                # Clock-boundary: the incomplete QH is always QH1 (most recent).
                incomplete_result = ParsedMetricsQH(
                    qh_name="QH1",
                    predicted_wh=predicted_wh,
                    seconds_remaining=remaining_seconds,
                    data_lag_secs=metrics_data.get("_data_lag_secs", 0.0),
                )
                # Don't break — keep scanning for the last complete QH fallback.
            else:
                continue

        # Return incomplete QH if found; otherwise return None.
        # Never return a complete quarter as a fallback — its data is stale
        # and using it for load management causes incorrect decisions
        # (e.g., turning off loads based on a completed quarter's 0 Wh).
        if incomplete_result is not None:
            return incomplete_result
        return None


class StateTracker:
    """In-memory state of managed devices and pending effects."""

    # Asymmetric debounce: turn-on is conservative (prevents chatter), turn-off
    # is fast (enables rapid recovery from an over-commit without waiting out the
    # full on-guard period, which would leave a large deficit in place).
    MIN_TOGGLE_ON_SECS = 60
    MIN_TOGGLE_OFF_SECS = 20
    STALE_THRESHOLD_SECS = 61
    VOLTAGE = 240

    # The settle window duration is now derived dynamically from the
    # quantization-based prediction window via effective_settle_secs.
    # The old TESLA_SETTLE_SECS=60 constant is replaced by
    # prediction_window_seconds * 2 at init time.

    @property
    def effective_settle_secs(self) -> int:
        """Settle window duration in seconds: twice the prediction window.

        Derived from the quantization-based prediction window so that the
        settle window scales proportionally to how quickly the NBC prediction
        absorbs load changes.  With the default 60 s prediction window the
        settle is 120 s; with a 30 s quantization window it is 60 s.
        """
        return self._pending_effect_min_secs

    @staticmethod
    def watts_to_wh(power_watts: float, seconds: int) -> float:
        """Convert power in watts over a duration to watt-hours."""
        return power_watts * seconds / 3600

    @staticmethod
    def wh_to_watts(energy_wh: float, seconds: int) -> float:
        """Convert energy in watt-hours to average power in watts over a duration."""
        return energy_wh * 3600.0 / seconds

    @staticmethod
    def amps_to_watts(current_amps: float | None) -> float:
        """Convert current in amps to watts at nominal voltage.

        Args:
            current_amps: Current in amps, or None to indicate unknown.

        Returns:
            Wattage, or 0.0 if current_amps is None.
        """
        if current_amps is None:
            return 0.0
        return current_amps * StateTracker.VOLTAGE

    @staticmethod
    def watts_to_amps(power_watts: float) -> int:
        """Convert power in watts to integer amps at nominal voltage."""
        return int(power_watts / StateTracker.VOLTAGE)

    @staticmethod
    def delta_amps_to_wh(amp_delta: float, seconds: int) -> float:
        """Convert an amp change over a duration to watt-hours.

        Args:
            amp_delta: Change in amps (positive = more draw).
            seconds: Duration the change applies for.

        Returns:
            Watt-hours consumed or saved by the delta.
        """
        return amp_delta * StateTracker.VOLTAGE * seconds / 3600.0

    @staticmethod
    def wh_to_amps(energy_wh: float, seconds: int) -> float:
        """Convert watt-hours to amp change needed over a duration.

        Args:
            energy_wh: Energy in watt-hours to absorb or shed.
            seconds: Duration in seconds the amp change applies for.

        Returns:
            Float amp change — callers apply floor or ceil as appropriate.
        """
        return energy_wh * 3600.0 / (StateTracker.VOLTAGE * seconds)

    def __init__(self, prediction_window_seconds: int = DEFAULT_PREDICTION_WINDOW_SECS) -> None:
        self._pending_effect_min_secs = prediction_window_seconds
        self.devices: dict[str, DeviceState] = {}
        self.pending_effects: list[PendingEffect] = []
        self.last_data_point_at: datetime | None = None
        self.last_nbc_predicted_wh: float | None = None
        self.last_commanded_amps: int | None = None
        # Fleet-telemetry push callbacks replace REST reads of Tesla state.
        self.tesla_telemetry_state: TeslaVehicleTelemetry | None = None
        self.has_fresh_telemetry: bool = False
        # Whether fleet-telemetry push has been registered via the callback API.
        self.registered: bool = False

    def estimated_current_wh(self, nbc_predicted_wh: float, seconds_remaining:
  int) -> float:
        """Estimate actual current Wh by adding pending effects to NBC prediction.

        Tesla set_amps effects are excluded — their contribution is recomputed live
        each cycle in _cycle_async_phase using the vehicle API's reported
        current_amps via tesla_inflight_wh().

        Other effects (plug turn_on/turn_off) use power_watts to compute Wh
        dynamically from seconds_remaining, so the estimate adjusts as the
        quarter-hour progresses.

        Args:
            nbc_predicted_wh: Raw predicted Wh from NBC cache/API.
            seconds_remaining: Seconds left in the current quarter-hour.

        Returns:
            Adjusted Wh estimate including all pending effect deltas.
        """
        adjusted = nbc_predicted_wh
        for effect in self.pending_effects:
            if effect.device_name == "tesla" and effect.action == "set_amps":
                continue
            adjusted += StateTracker.watts_to_wh(effect.power_watts, seconds_remaining)
        return adjusted


    def tesla_inflight_wh(
        self, reported_amps: int | None, seconds_remaining: int,
        now: datetime | None = None,
        data_point_at: datetime | None = None,
    ) -> float:
        """Compute the still-unconfirmed Tesla amp-change contribution.

        Recomputed fresh every cycle using the vehicle API's reported
        current_amps, so the adjustment decays correctly as seconds_remaining
        shrinks rather than being frozen at command time.

        Returns zero when no amp command is in flight or the car has already
        reached the commanded level (confirmed by vehicle API).

        If the car reports 1 A during the settle window after a recent command,
        it is considered to be ramping up (not stale) and ``last_commanded_amps``
        is preserved.  The settle window considers both wall-clock age and
        data-point-at age — the ramp-up status is preserved if **either**
        measure is still within ``effective_settle_secs``.

        Args:
            reported_amps: current_amps from the vehicle API this cycle.
            seconds_remaining: Seconds left in the current quarter-hour.
            now: Current wall-clock time.  Defaults to ``datetime.now(timezone.utc)``
                when ``None``.
            data_point_at: Current NBC data-point-at timestamp.  When provided,
                the data-point-at age is checked alongside wall-clock age for
                the 1A stale-clearing gate.  ``None`` falls back to
                wall-clock-only.

        Returns:
            Wh still expected from the in-flight amp delta.
        """
        if self.last_commanded_amps is None or reported_amps is None:
            return 0.0
        resolve_now = now if now is not None else datetime.now(timezone.utc)
        # Car has stopped drawing power — clear the stale command state.
        # reported_amps == 0 means the car is idle or disconnected.
        if reported_amps == 0:
            self.last_commanded_amps = None
            return 0.0
        latest_cmd = self._latest_tesla_command()
        # Car is at 1 A: gate the stale-clearing behind a recency check.
        # During ramp-up the car briefly reports 1A — treat as stale only
        # when the command was issued long enough ago that the car should
        # have reached a higher level by now.
        if reported_amps == 1:
            if latest_cmd is not None:
                elapsed_wall = (resolve_now - latest_cmd.timestamp).total_seconds()
                if elapsed_wall < self.effective_settle_secs:
                    # Recent command — car is ramping up, don't clear.
                    return 0.0
                # Wall clock expired — check data-point-at age.
                if (data_point_at is not None
                        and latest_cmd.data_point_at is not None):
                    elapsed_data = (
                        data_point_at - latest_cmd.data_point_at
                    ).total_seconds()
                    if elapsed_data < self.effective_settle_secs:
                        return 0.0
            # No recent command (or command age exceeds settle window) —
            # treat as stale and clear.
            self.last_commanded_amps = None
            return 0.0
        delta = self.last_commanded_amps - reported_amps
        if delta == 0:
            return 0.0
        # After both settle windows expire, if the car still hasn't reached
        # the commanded level, treat the command as unsuccessful and clear
        # the stale state so it doesn't distort the gap forever.
        # Only fires when at least one settle effect was ever recorded — tests
        # that set last_commanded_amps directly without pending effects
        # still compute the delta as expected.
        has_settle = any(
            eff.device_name == "tesla" and eff.action == "set_amps"
            and eff.direction is not None
            for eff in self.pending_effects
        )
        if (has_settle
                and not self.is_settling(resolve_now, data_point_at=data_point_at, direction="increase")
                and not self.is_settling(resolve_now, data_point_at=data_point_at, direction="decrease")):
            logger.debug(
                "[tesla_inflight_wh] settle expired — clearing stale last_commanded_amps=%d "
                "(reported=%d)",
                self.last_commanded_amps, reported_amps,
            )
            self.last_commanded_amps = None
            return 0.0
        return StateTracker.delta_amps_to_wh(delta, seconds_remaining)

    def sync_tesla_device_state(
        self, tesla_state: TeslaState | None,
    ) -> None:
        """Update or remove the Tesla entry in devices from live vehicle state.

        When ``tesla_state`` is provided, creates or updates a "tesla" entry in
        ``self.devices`` reflecting actual charging state and commanded amps.
        When ``tesla_state`` is None, removes the entry if present.

        Args:
            tesla_state: Current Tesla vehicle state, or None if unavailable.
        """
        if tesla_state is not None:
            latest_cmd = self._latest_tesla_command()
            self.devices["tesla"] = DeviceState(
                name="tesla",
                actual_state=tesla_state.is_charging,
                current_amps=tesla_state.current_amps,
                desired_state=self.last_commanded_amps is not None,
                last_toggle=latest_cmd.timestamp if latest_cmd else None,
            )
        else:
            self.devices.pop("tesla", None)

    def _latest_tesla_command(self) -> PendingEffect | None:
        """Return the most recent Tesla effect (set_amps, turn_on, or turn_off).

        Searches ``pending_effects`` in reverse order to find the newest
        effect targeting the Tesla device. Used by ``tesla_inflight_wh()``
        for 1A ramp-up detection and stale-command clearing.

        Returns:
            The most recent Tesla effect, or None if none exist.
        """
        for eff in reversed(self.pending_effects):
            if eff.device_name == "tesla":
                return eff
        return None

    def get_active_tesla_settle(
        self, now: datetime, current_qh: str | None = None,
        data_point_at: datetime | None = None,
        direction: str = "increase",
    ) -> PendingEffect | None:
        """Return the active settle effect for the given direction, or None.

        Like ``is_settling()`` but returns the effect itself so callers can
        access its timestamp for logging or diagnostics.

        Args:
            now: Current wall-clock timestamp.
            current_qh: Current QH name for QH-boundary expiry.
            data_point_at: Current NBC data-point-at timestamp.
            direction: "increase" or "decrease".

        Returns:
            The active settle effect, or None if no active settle exists.
        """
        for eff in reversed(self.pending_effects):
            if (eff.device_name == "tesla" and eff.action == "set_amps"
                    and eff.direction == direction):
                if current_qh is not None and eff.qh_name != current_qh:
                    return None
                elapsed_wall = (now - eff.timestamp).total_seconds()
                if elapsed_wall < self.effective_settle_secs:
                    return eff
                if (data_point_at is not None
                        and eff.data_point_at is not None):
                    elapsed_data = (
                        data_point_at - eff.data_point_at
                    ).total_seconds()
                    if elapsed_data < self.effective_settle_secs:
                        return eff
                return None
        return None

    def is_settling(
        self, now: datetime, current_qh: str | None = None,
        data_point_at: datetime | None = None,
        direction: str = "increase",
    ) -> bool:
        """Return True if we are in the settle window for the given direction.

        Unified settle-window check that queries ``pending_effects`` instead
        of dedicated attributes. Finds the most recent Tesla "set_amps" effect
        with the matching ``direction`` and checks whether it is still active
        using dual-age expiry (wall clock + data-point-at) and QH-boundary
        expiry.

        Args:
            now: Current wall-clock timestamp.
            current_qh: Current QH name; if different from the QH in which the
                effect was recorded, the window is treated as expired.
            data_point_at: Current NBC data-point-at timestamp. When provided,
                the data-point-at age is checked alongside wall-clock age.
            direction: "increase" or "decrease" — which settle window to check.

        Returns:
            True if turn-off (increase) or turn-on (decrease) decisions should
            be suppressed.
        """
        for eff in reversed(self.pending_effects):
            if (eff.device_name == "tesla" and eff.action == "set_amps"
                    and eff.direction == direction):
                if current_qh is not None and eff.qh_name != current_qh:
                    return False
                elapsed_wall = (now - eff.timestamp).total_seconds()
                if elapsed_wall < self.effective_settle_secs:
                    return True
                if (data_point_at is not None
                        and eff.data_point_at is not None):
                    elapsed_data = (
                        data_point_at - eff.data_point_at
                    ).total_seconds()
                    if elapsed_data < self.effective_settle_secs:
                        return True
                return False
        return False

    def clear_tesla_settle_effects(self) -> None:
        """Remove Tesla set_amps effects from pending_effects.

        Called when Tesla charging is stopped or started via turn_on/turn_off,
        which supersedes any prior amp-change effects. Only removes "set_amps"
        effects — turn_on/turn_off effects are preserved.
        """
        self.pending_effects = [
            eff for eff in self.pending_effects
            if not (eff.device_name == "tesla" and eff.action == "set_amps")
        ]

    def has_pending_effect_since(self, nbc_timestamp: datetime) -> bool:
        """Return True if we took an action after the NBC timestamp by either measure.

        Checks both the wall clock timestamp and the data-point-at timestamp so
        that effects recorded with a future data-point-at are still detected.
        Uses a ``prediction_window_seconds`` buffer on both measures to catch
        effects taken just before the NBC data point that have not yet been
        reflected in API data.

        Args:
            nbc_timestamp: The NBC data-point-at timestamp to compare against.

        Returns:
            True if any effect has either timestamp within ``prediction_window_seconds``
            after ``nbc_timestamp``.
        """
        buffer = timedelta(seconds=self._pending_effect_min_secs)
        for effect in self.pending_effects:
            if effect.timestamp > nbc_timestamp - buffer:
                return True
            if effect.data_point_at > nbc_timestamp - buffer:
                return True

        return False

    def pending_since_count(self, nbc_timestamp: datetime) -> int:
        """Return the number of effects taken after the given timestamp.

        Uses the same ``prediction_window_seconds`` buffer as
        ``has_pending_effect_since`` so the count reflects the same set of
        effects that triggers the waiting path.

        Args:
            nbc_timestamp: The NBC data-point-at timestamp to compare against.

        Returns:
            Count of effects whose wall clock or data-point-at timestamp is
            within ``prediction_window_seconds`` after ``nbc_timestamp``.
        """
        buffer = timedelta(seconds=self._pending_effect_min_secs)
        return sum(
            1 for eff in self.pending_effects
            if eff.timestamp > nbc_timestamp - buffer
            or eff.data_point_at > nbc_timestamp - buffer
        )

    def prune_old_effects(
        self, data_point_at: datetime, now: datetime
    ) -> int:
        """Remove pending effects eligible for pruning based on dual age checks.

        An effect is pruned when it is over ``prediction_window_seconds`` old
        by **both** measures:
          1. wall clock age:  now - effect.timestamp >= prediction_window_seconds
          2. data-point age:  effect.data_point_at <= data_point_at - prediction_window_seconds
        (If ``data_point_at`` is unknown, only the wall-clock check applies.)

        This dual-criteria pruning ensures effects are not pruned prematurely
        when the Emporia API reports old/stale data (small ``data_lag_secs``)
        while wall-clock time has advanced, and prevents effects from lingering
        when data is fresh but the wall clock has not moved forward enough.

        Args:
            data_point_at: Timestamp of the most recent per-second data point.
            now: Current wall clock time. Required.

        Returns:
            Number of effects removed.
        """
        wall_cutoff = now - timedelta(seconds=self._pending_effect_min_secs)
        dp_cutoff = data_point_at - timedelta(seconds=self._pending_effect_min_secs)
        before = len(self.pending_effects)
        self.pending_effects = [
            eff for eff in self.pending_effects
            if eff.timestamp >= wall_cutoff or eff.data_point_at >= dp_cutoff
        ]
        return before - len(self.pending_effects)

    def can_toggle(
        self, device_name: str, now: datetime, turning_on: bool = True
    ) -> bool:
        """Check asymmetric debounce: has enough time elapsed since last toggle?

        Turn-on uses MIN_TOGGLE_ON_SECS (60 s) to prevent rapid cycling.
        Turn-off uses MIN_TOGGLE_OFF_SECS (10 s) to allow fast recovery from
        an over-commit without waiting out the full on-guard period.

        Args:
            device_name: The device to check.
            now: Current timestamp.
            turning_on: True if we are considering turning the device ON,
                False if we are considering turning it OFF. Defaults to True
                so that call-sites that only need the conservative (turn-on)
                threshold keep working without a keyword argument.

        Returns:
            True if the debounce period has elapsed and toggling is permitted.
        """
        dev_state = self.devices.get(device_name)
        if dev_state is None or dev_state.last_toggle is None:
            return True
        elapsed = (now - dev_state.last_toggle).total_seconds()
        threshold = self.MIN_TOGGLE_ON_SECS if turning_on else self.MIN_TOGGLE_OFF_SECS
        return elapsed >= threshold

    def to_dict(self) -> dict[str, Any]:
        """Serialize state for API/template consumption.

        Returns:
            Dict with devices, pending_effects, and last_data_point_at
            suitable for JSON serialization.
        """
        ts_dict = None
        if self.tesla_telemetry_state is not None:
            ts_dict = {
                "timestamp": self.tesla_telemetry_state.timestamp.isoformat(),
                "vehicle_id": self.tesla_telemetry_state.vehicle_id,
                "is_charging": self.tesla_telemetry_state.is_charging,
                "current_amps": self.tesla_telemetry_state.current_amps,
                "plugged_in": self.tesla_telemetry_state.plugged_in,
                "at_home": self.tesla_telemetry_state.at_home,
            }
        return {
            "devices": {name: {
                "desired_state": ds.desired_state,
                "actual_state": ds.actual_state,
                "current_amps": ds.current_amps,
                "last_toggle": ds.last_toggle.isoformat()
                if ds.last_toggle else None,
            } for name, ds in self.devices.items()},
            "pending_effects": [{
                "device_name": eff.device_name,
                "action": eff.action,
                "timestamp": eff.timestamp.isoformat(),
                "data_point_at": eff.data_point_at.isoformat(),
                "power_watts": eff.power_watts,
            } for eff in self.pending_effects],
            "last_data_point_at": (self.last_data_point_at.isoformat()
                                   if self.last_data_point_at else None),
            "last_commanded_amps": self.last_commanded_amps,
            "has_fresh_telemetry": self.has_fresh_telemetry,
            "tesla_telemetry_state": ts_dict,
        }


@dataclass(frozen=True)
class DecideContext:
    """Shared context for GapMinder decision methods.

    Replaces the previous 8+ individual arguments on
    ``GapMinder.decide()`` and its private helpers, reducing
    positional-argument count and making call-sites self-documenting.

    Attributes:
        now: Current wall-clock time.
        seconds_remaining: Seconds left in the current quarter-hour.
        state: Current state tracker with device states and pending effects.
        plugs: Dictionary of plug configurations.
        tesla: Current Tesla state, if available.
        dry_run: When True, skip mutating ``state.devices`` so subsequent
            dry-run cycles re-evaluate instead of seeing stale state.
        data_point_at: The NBC data-point-at timestamp. Used by created
            ``PendingEffect`` objects for dual-pruning checks.
        requires_home_check: When True and a TeslaConfig was loaded with
            home_lat/home_lon, the engine checks ``tesla.at_home`` before
            issuing charging actions. When False (missing config), Tesla
            charging is allowed regardless of location.
    """

    now: datetime
    seconds_remaining: int
    state: StateTracker
    plugs: dict[str, Any]
    tesla: TeslaState | None
    dry_run: bool = False
    data_point_at: datetime | None = None
    requires_home_check: bool = True


class GapMinder:
    """Bin-pack eligible loads to fill (or reduce) the NBC surplus/deficit gap."""

    TESLA_AMP_CHANGE_THRESHOLD = 1
    MIN_SECONDS_TO_ACT = 31
    CAR_POWER_WATTS_5A = 5 * 240  # 1200W at Tesla's minimum charge rate
    MAX_DEFER_SECS = 120          # cap on the safe defer window
    HARD_MAX_AMPS = 48            # absolute max — never exceed, regardless of config
    # During the post-amp-increase settle window, suppress turn-off decisions
    # only if the apparent deficit is below this threshold.  Deficits larger
    # than this are treated as genuine even during the settle period (e.g. a
    # new QH starting with Tesla at a high amp level that immediately overwhelms
    # solar production).
    TESLA_SETTLE_SUPPRESS_WH = 240

    def __init__(
        self,
        hysteresis_wh: int | None = None,
        charge_amps_min: int = 5,
        charge_amps_max: int = 48,
    ) -> None:
        """Initialize the GapMinder.

        Args:
            hysteresis_wh: Hysteresis threshold in Wh. When None, defaults to
                1000 for backward compatibility.
            charge_amps_min: Minimum Tesla charge amps before turning off
                instead of reducing further. Defaults to 5.
            charge_amps_max: Maximum Tesla charge amps to command. Defaults
                to 48.
        """
        self.HYSTERESIS_WH = hysteresis_wh if hysteresis_wh is not None else 1000
        self.charge_amps_min = charge_amps_min
        self.charge_amps_max = min(charge_amps_max, self.HARD_MAX_AMPS)
        logger.info(
            "GapMinder: charge_amps_min=%d charge_amps_max=%d (config provided %d)",
            self.charge_amps_min, self.charge_amps_max, charge_amps_max,
            extra={"event": "gapminder_init", "charge_amps_min": self.charge_amps_min,
                   "charge_amps_max": self.charge_amps_max, "config_charge_amps_max": charge_amps_max},
        )

    def _safe_defer_secs(self, remaining_reduction: float) -> int:
        """Calculate the safe defer window in seconds.

        The defer window represents how long the Tesla can safely draw at 5A
        before its energy consumption exceeds the gap.  If the quarter-hour
        has more time remaining than this window, we defer stopping (we have
        buffer to stop later).  If less time remains, we stop immediately.

        Args:
            remaining_reduction: The gap in Wh (always positive — deficit to reduce).

        Returns:
            Maximum defer window in seconds, capped at MAX_DEFER_SECS.
        """
        return int(
            min(self.MAX_DEFER_SECS, remaining_reduction / (self.CAR_POWER_WATTS_5A / 3600))
        )

    def decide(
        self,
        ctx: DecideContext,
        predicted_wh: float,
        target_wh: float,
    ) -> list[PendingEffect]:
        """Decide what actions to take based on the predicted Wh and target Wh.

        Args:
            ctx: Decision context containing time, state, devices, and Tesla info.
            predicted_wh: The predicted Wh for the current quarter-hour.
            target_wh: The target Wh to achieve (negative = surplus).

        Returns:
            List of PendingEffect objects representing actions to take.
        """
        gap = target_wh - predicted_wh
        abs_gap = abs(gap)

        if abs_gap <= self.HYSTERESIS_WH:
            logger.info(
                "gapminder_hysteresis gap=%.1f hysteresis=%d no_action_needed",
                gap, self.HYSTERESIS_WH,
                extra={"event": "gapminder_hysteresis", "gap_wh": gap, "hysteresis_wh": self.HYSTERESIS_WH},
            )
            return []

        if gap > 0:
            edge_gap = gap - self.HYSTERESIS_WH  # aim for lower edge of deadband
            logger.info(
                "gapminder_decide direction=turn_on gap=%.1f edge_gap=%.1f hysteresis=%d",
                gap, edge_gap, self.HYSTERESIS_WH,
                extra={"event": "gapminder_decide", "direction": "turn_on", "gap_wh": gap, "edge_gap_wh": edge_gap, "hysteresis_wh": self.HYSTERESIS_WH},
            )
            return self._decide_turn_on(ctx, edge_gap)

        edge_gap = abs_gap - self.HYSTERESIS_WH  # aim for upper edge of deadband
        logger.info(
            "gapminder_decide direction=turn_off gap=%.1f edge_gap=%.1f hysteresis=%d",
            abs(gap), edge_gap, self.HYSTERESIS_WH,
            extra={"event": "gapminder_decide", "direction": "turn_off", "gap_wh": abs(gap), "edge_gap_wh": edge_gap, "hysteresis_wh": self.HYSTERESIS_WH},
        )
        return self._decide_turn_off(
            ctx, edge_gap,
        )

    def _decide_turn_on(self, ctx: DecideContext, gap: float) -> list[PendingEffect]:
        """Turn on eligible loads to absorb excess solar.

        Args:
            ctx: Decision context.
            gap: The Wh surplus to absorb.

        Returns:
            List of PendingEffect objects.
        """
        actions: list[PendingEffect] = []
        if ctx.seconds_remaining < self.MIN_SECONDS_TO_ACT:
            logger.debug(
                "[_decide_turn_on] skipped (too little time: %d sec)",
                ctx.seconds_remaining,
            )
            return actions

        remaining_gap = gap

        # Collect eligible loads that are currently off
        candidates: list[tuple[int, str, Any]] = []

        for name, plug in ctx.plugs.items():
            if not ctx.state.can_toggle(name, ctx.now, turning_on=True):
                logger.debug(
                    "[_decide_turn_on] %s: skipped (debounce)",
                    name,
                )
                continue
            dev_state = ctx.state.devices.get(name)
            if dev_state and dev_state.desired_state is True:
                continue  # already on
            candidates.append((plug.priority, name, plug))

        # Higher priority number = more important; sort descending so most
        # important eligible plugs are turned on first.
        candidates.sort(key=lambda x: x[0], reverse=True)

        for _, name, plug in candidates:
            capacity = StateTracker.watts_to_wh(plug.power_watts, ctx.seconds_remaining)
            if capacity <= remaining_gap:
                logger.debug(
                    "[_decide_turn_on] %s: turning on "
                    "(capacity=%.1f Wh fits in gap %.1f Wh)",
                    name,
                    capacity,
                    remaining_gap,
                )
                logger.info(
                    "action=turn_on device=%s capacity=%.1f gap=%.1f priority=%d",
                    name, capacity, remaining_gap, plug.priority,
                    extra={"event": "action", "device": name, "action_type": "turn_on",
                           "capacity_wh": capacity, "remaining_gap_wh": remaining_gap, "priority": plug.priority},
                )
                actions.append(
                    PendingEffect(
                        device_name=name,
                        action="turn_on",
                        timestamp=ctx.now,
                        data_point_at=ctx.data_point_at or ctx.now,
                        power_watts=plug.power_watts,
                    )
                )
                remaining_gap -= capacity
                if not ctx.dry_run:
                    ctx.state.devices[name] = DeviceState(
                        name=name, last_toggle=ctx.now, desired_state=True
                    )

            else:
                logger.debug(
                    "[_decide_turn_on] %s: too large "
                    "(capacity=%.1f Wh > gap %.1f Wh)",
                    name,
                    capacity,
                    remaining_gap,
                )

        if remaining_gap > 0 and ctx.tesla is not None and ctx.tesla.is_charging:
            logger.debug(
                "[_decide_turn_on] trying Tesla amps increase "
                "for remaining %.1f Wh",
                remaining_gap,
            )
            tesla_action = self._decide_tesla_amps(ctx, remaining_gap)
            if tesla_action:
                actions.append(tesla_action)

        return actions

    def _decide_turn_off(
        self,
        ctx: DecideContext,
        gap_wh: float,
    ) -> list[PendingEffect]:
        """Turn off loads to reduce consumption.

        Priority order:
          1. Reduce Tesla charge amps (partial, no stop).
          2. Disable plugs in priority order (lowest-priority first).
          3. Stop Tesla charging if a deficit still remains.

        Args:
            ctx: Decision context.
            gap_wh: Wh reduction needed.

        Returns:
            List of PendingEffect objects.
        """
        actions: list[PendingEffect] = []
        remaining_reduction = gap_wh
        dp = ctx.data_point_at or ctx.now

        logger.debug(
            "[_decide_turn_off] gap=%.1f Wh, seconds_remaining=%d",
            gap_wh,
            ctx.seconds_remaining,
        )

        # ── Step 1: reduce Tesla charge amps first (no stop) ──────────────────
        if ctx.tesla and ctx.tesla.is_charging:
            logger.debug(
                "[_decide_turn_off] trying Tesla amps-only reduce "
                "for %.1f Wh remaining",
                remaining_reduction,
            )
            tesla_action = self._decide_tesla_reduce(
                ctx,
                remaining_reduction,
                stop_allowed=False,
            )
            if tesla_action:
                actions.append(tesla_action)
                current_amps = ctx.tesla.current_amps or 0
                target_amps = tesla_action.target_amps or 0
                savings = StateTracker.delta_amps_to_wh(
                    current_amps - target_amps, ctx.seconds_remaining
                )
                logger.debug(
                    "[_decide_turn_off] Tesla amps %d → %d, "
                    "savings=%.1f Wh, remaining %.1f → %.1f Wh",
                    current_amps,
                    target_amps,
                    savings,
                    remaining_reduction,
                    remaining_reduction - savings,
                )
                remaining_reduction -= savings

        # ── Step 2: disable plugs in priority order ────────────────────────────
        candidates: list[tuple[int, str, Any]] = []

        for name, plug in ctx.plugs.items():
            if not ctx.state.can_toggle(name, ctx.now, turning_on=False):
                logger.debug(
                    "[_decide_turn_off] %s: skipped (debounce)",
                    name,
                )
                continue

            dev_state = ctx.state.devices.get(name)
            if dev_state and dev_state.desired_state is True:
                candidates.append((plug.priority, name, plug))
                logger.debug(
                    "[_decide_turn_off] %s: eligible (on)",
                    name,
                )
            else:
                logger.debug(
                    "[_decide_turn_off] %s: skipped (not on)",
                    name,
                )

        # Lower priority number = less important; sort ascending so least
        # important eligible plugs are turned off first.
        candidates.sort(key=lambda x: x[0])

        for _, name, plug in candidates:
            if remaining_reduction <= 0:
                break
            savings = StateTracker.watts_to_wh(plug.power_watts, ctx.seconds_remaining)

            # omit any "too large to turn off" logic:
            # not relevant when shedding load.
            logger.debug(
                "[_decide_turn_off] %s: turning off "
                "(savings=%.1f Wh fits in gap %.1f Wh)",
                name,
                savings,
                remaining_reduction,
            )
            logger.info(
                "action=turn_off device=%s savings=%.1f gap=%.1f priority=%d",
                name, savings, remaining_reduction, plug.priority,
                extra={"event": "action", "device": name, "action_type": "turn_off",
                       "savings_wh": savings, "remaining_gap_wh": remaining_reduction, "priority": plug.priority},
            )
            actions.append(
                PendingEffect(
                    device_name=name,
                    action="turn_off",
                    timestamp=ctx.now,
                    data_point_at=dp,
                    power_watts=plug.power_watts,
                )
            )
            remaining_reduction -= savings
            if not ctx.dry_run:
                ctx.state.devices[name] = DeviceState(
                    name=name, last_toggle=ctx.now, desired_state=False
                )
            if remaining_reduction <= 0:
                break

        # ── Step 3: if deficit remains, stop Tesla charging ────────────
        # Delegates to _decide_tesla_reduce with stop_allowed=True so
        # the gap-aware deferral logic (safe_defer_secs) decides whether
        # to stop now or keep the car on.  This path fires when Step 1
        # returned no action (e.g. car at min amps with stop_allowed=False).
        tesla_already_acted = any(a.device_name == "tesla" for a in actions)
        if ctx.tesla and ctx.tesla.is_charging and remaining_reduction > 0 and not tesla_already_acted:
            tesla_action = self._decide_tesla_reduce(ctx, remaining_reduction)
            if tesla_action:
                logger.info(
                    "action=turn_off device=tesla reason=deficit remaining=%.1f",
                    remaining_reduction,
                    extra={"event": "action", "device": "tesla", "action_type": "turn_off",
                           "reason": "deficit", "remaining_gap_wh": remaining_reduction},
                )
                actions.append(tesla_action)

        return actions

    def _tesla_supports_amps(
        self, _plug: Any, tesla: TeslaState | None, requires_home_check: bool
    ) -> bool:
        """Check if Tesla can handle partial amp adjustment.

        When ``requires_home_check`` is False (no home coords configured),
        only ``plugged_in`` is checked.
        When True, the vehicle must also be ``at_home``.

        Args:
            _plug: Plug config that triggered the fallback (unused).
            tesla: Current Tesla state.
            requires_home_check: Whether to check the vehicle's home location.

        Returns:
            True if Tesla is eligible for amp-adjustment actions.
        """
        if tesla is None:
            return False
        if not tesla.plugged_in:
            return False
        if requires_home_check and not tesla.at_home:
            return False
        return True

    def _decide_tesla_amps(
        self,
        ctx: DecideContext,
        gap_wh: float,
    ) -> PendingEffect | None:
        """Adjust Tesla charge amps to fill residual gap.

        Args:
            ctx: Decision context.
            gap_wh: Wh surplus to absorb.

        Returns:
            PendingEffect for set_amps, or None if no action needed.
        """
        tesla = ctx.tesla
        if tesla is None:
            return None
        if not tesla.plugged_in:
            logger.debug(
                "[_decide_tesla_amps] skipped: plugged_in=%s",
                tesla.plugged_in,
            )
            return None
        if ctx.requires_home_check and not tesla.at_home:
            logger.debug(
                "[_decide_tesla_amps] skipped: at_home=%s (requires_home_check=%s)",
                tesla.at_home,
                ctx.requires_home_check,
            )
            return None
        if ctx.seconds_remaining < self.MIN_SECONDS_TO_ACT:
            logger.debug(
                "[_decide_tesla_amps] skipped: too little time (%d s < %d s)",
                ctx.seconds_remaining, self.MIN_SECONDS_TO_ACT,
            )
            return None

        current_amps = tesla.current_amps or 0
        if current_amps < self.charge_amps_min:
            logger.debug(
                "[_decide_tesla_amps] skipped: current_amps=%d < charge_amps_min=%d",
                current_amps, self.charge_amps_min,
            )
            return None
        additional_amps = int(StateTracker.wh_to_amps(gap_wh, ctx.seconds_remaining))
        target_amps = current_amps + additional_amps
        # Clamp to the configured range; callers enforce controller-specific limits.
        target_amps = max(self.charge_amps_min, min(self.charge_amps_max, target_amps))

        logger.debug(
            "[_decide_tesla_amps] gap=%.1f Wh, seconds=%d, "
            "current_amps=%d, additional_amps=%d, target_amps=%d",
            gap_wh,
            ctx.seconds_remaining,
            current_amps,
            additional_amps,
            target_amps,
        )

        if abs(target_amps - current_amps) < self.TESLA_AMP_CHANGE_THRESHOLD:
            logger.debug(
                "[_decide_tesla_amps] skipped: change too small (%d → %d, threshold=%d)",
                current_amps, target_amps, self.TESLA_AMP_CHANGE_THRESHOLD,
            )
            return None

        logger.info(
            "action=set_amps device=tesla target=%d previous=%d gap=%.1f",
            target_amps, current_amps, gap_wh,
            extra={"event": "action", "device": "tesla", "action_type": "set_amps",
                   "target_amps": target_amps, "previous_amps": current_amps, "gap_wh": gap_wh},
        )
        return PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=ctx.now,
            data_point_at=ctx.data_point_at or ctx.now,
            power_watts=StateTracker.amps_to_watts(additional_amps),
            target_amps=target_amps,
        )

    def _decide_tesla_reduce(
        self,
        ctx: DecideContext,
        reduce_wh: float,
        stop_allowed: bool = True,
    ) -> PendingEffect | None:
        """Reduce Tesla charge amps, or stop charging if amps can't be reduced further.

        Args:
            ctx: Decision context.
            reduce_wh: Wh reduction needed.
            stop_allowed: When False, return None instead of issuing a turn_off
                command. Used when the caller wants amps-only reduction and will
                handle stopping as a separate last-resort step.
        """
        tesla = ctx.tesla
        if tesla is None:
            return None
        if not tesla.plugged_in:
            logger.debug(
                "[_decide_tesla_reduce] skipped: plugged_in=%s",
                tesla.plugged_in,
            )
            return None
        if ctx.requires_home_check and not tesla.at_home:
            logger.debug(
                "[_decide_tesla_reduce] skipped: at_home=%s (requires_home_check=%s)",
                tesla.at_home,
                ctx.requires_home_check,
            )
            return None

        current_amps = tesla.current_amps
        if current_amps is None or current_amps <= 5:
            if not stop_allowed:
                logger.debug(
                    "[_decide_tesla_reduce] skipped stop: stop_allowed=False, "
                    "current_amps=%s",
                    current_amps,
                )
                return None
            # Defer stopping when at minimum amps: calculate a safe defer window
            # based on the gap.  At 5A the car draws useful energy — stopping it
            # removes all that draw.  Defer if the quarter-hour has more time
            # remaining than the safe window (i.e., we have buffer to stop later).
            safe_defer_secs = self._safe_defer_secs(reduce_wh)
            if ctx.seconds_remaining > safe_defer_secs:
                logger.debug(
                    "[_decide_tesla_reduce] deferring stop: current_amps=%d, "
                    "seconds_remaining=%d, safe_defer=%ds, gap=%.1f Wh",
                    current_amps, ctx.seconds_remaining, safe_defer_secs,
                    reduce_wh,
                )
                return None
            logger.info(
                "action=turn_off device=tesla reason=amps_min_reached current_amps=%d",
                current_amps,
                extra={"event": "action", "device": "tesla", "action_type": "turn_off",
                       "reason": "amps_min_reached", "current_amps": current_amps},
            )
            return PendingEffect(
                device_name="tesla",
                action="turn_off",
                timestamp=ctx.now,
                data_point_at=ctx.data_point_at or ctx.now,
                power_watts=-StateTracker.amps_to_watts(current_amps),
            )

        # direct amp delta from energy over remaining window
        reduce_amps = math.ceil(StateTracker.wh_to_amps(reduce_wh, ctx.seconds_remaining))
        new_amps = max(0, min(self.charge_amps_max, current_amps - reduce_amps))
        # When stop is not allowed, clamp to minimum amps instead of
        # returning None — a reduction to charge_amps_min is still useful.
        if not stop_allowed:
            new_amps = max(new_amps, self.charge_amps_min)

        if new_amps < self.charge_amps_min:
            # guard against premature turn-off
            turn_off_hysteresis = int(3 * self.charge_amps_min / 5)
            if new_amps < (self.charge_amps_min - turn_off_hysteresis):
                logger.debug(
                    "[_decide_tesla_reduce] skipped stop: new_amps=%d ~= min=%d",
                    new_amps,
                    self.charge_amps_min,
                )
                return None

            if not stop_allowed:
                logger.debug(
                    "[_decide_tesla_reduce] skipped stop: stop_allowed=False, "
                    "new_amps=%d < min=%d",
                    new_amps,
                    self.charge_amps_min,
                )
                return None
            logger.info(
                "action=turn_off device=tesla reason=below_min_amps "
                "current_amps=%d new_amps=%d min_amps=%d",
                current_amps, new_amps, self.charge_amps_min,
                extra={"event": "action", "device": "tesla", "action_type": "turn_off",
                       "reason": "below_min_amps", "current_amps": current_amps,
                       "new_amps": new_amps, "min_amps": self.charge_amps_min},
            )
            return PendingEffect(
                device_name="tesla",
                action="turn_off",
                timestamp=ctx.now,
                data_point_at=ctx.data_point_at or ctx.now,
                power_watts=-StateTracker.amps_to_watts(current_amps),
            )

        if abs(new_amps - current_amps) < self.TESLA_AMP_CHANGE_THRESHOLD:
            logger.debug(
                "[_decide_tesla_reduce] skipped: change too small (%d - %d < %d)",
                new_amps, current_amps, self.TESLA_AMP_CHANGE_THRESHOLD,
            )
            return None

        logger.info(
            "action=set_amps device=tesla target=%d previous=%d reason=reduce",
            new_amps, current_amps,
            extra={"event": "action", "device": "tesla", "action_type": "set_amps",
                   "target_amps": new_amps, "previous_amps": current_amps, "reason": "reduce"},
        )
        return PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=ctx.now,
            data_point_at=ctx.data_point_at or ctx.now,
            power_watts=StateTracker.amps_to_watts(new_amps - current_amps),
            target_amps=new_amps,
        )
