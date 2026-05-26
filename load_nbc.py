"""
NBC reader, device state tracking, and the bin-packing decision engine.

NBCReader reads current quarter-hour predictions from a shared EnergyCache
instance instead of maintaining its own NBCCache layer. NBC quarters are
computed on demand from raw per-second samples via util.compute_nbc_quarters().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from load_models import DeviceState, PendingEffect, TeslaState

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
            data_lag_secs = parsed.get("_data_lag_secs", 0.0)
            data_point_at = fetched_at - timedelta(seconds=data_lag_secs)
            return (  # type: ignore[return-value]
                parsed["qh_name"],
                parsed.get("predicted_wh", 0),
                parsed.get("seconds_remaining", 0),
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
        return result["qh_name"], result["predicted_wh"], result["seconds_remaining"]

    def _parse_metrics(
        self, device_name: str, metrics_data: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
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
        incomplete_result: dict[str, Any] | None = None

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
                incomplete_result = {
                    "qh_name": "QH1",
                    "predicted_wh": predicted_wh,
                    "seconds_remaining": remaining_seconds,
                    "_data_lag_secs": metrics_data.get("_data_lag_secs", 0.0),
                }
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
    PENDING_EFFECT_MIN_SECS = 60
    VOLTAGE = 240

    # After a Tesla amp *increase* is confirmed, suppress turn-off decisions for
    # this many seconds.  The NBC prediction integrates the new load gradually
    # (Emporia window covers the past 60 seconds), so the first post-confirmation
    # cycle can show a large apparent deficit that is mostly solar variability
    # rather than genuine overshoot.  The settle window damps this response.
    # The QH name is also tracked so that a new quarter-hour automatically expires
    # the settle state — a deficit at QH start is a fresh, genuine signal.
    TESLA_SETTLE_SECS = 60

    @staticmethod
    def watts_to_wh(power_watts: float, seconds: int) -> float:
        """Convert power in watts over a duration to watt-hours."""
        return power_watts * seconds / 3600

    @staticmethod
    def wh_to_watts(energy_wh: float, seconds: int) -> float:
        """Convert energy in watt-hours to equivalent average watts."""
        return energy_wh * NBCPeriod.PERIOD_SECS / seconds

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
    def watts_to_amps(power_watts: float, seconds: int) -> int:
        """Convert power in watts to integer amps at nominal voltage."""
        return int(
            power_watts * NBCPeriod.PERIOD_SECS
            / (StateTracker.VOLTAGE * seconds)
        )

    @staticmethod
    def delta_amps_to_wh(amp_delta: float, seconds: int) -> float:
        """Convert an amp change over a duration to watt-hours.

        Args:
            amp_delta: Change in amps (positive = more draw).
            seconds: Duration the change applies for.

        Returns:
            Watt-hours consumed or saved by the delta.
        """
        return amp_delta * StateTracker.VOLTAGE * seconds / NBCPeriod.PERIOD_SECS

    def __init__(self) -> None:
        self.devices: dict[str, DeviceState] = {}
        self.pending_effects: list[PendingEffect] = []
        self.last_data_point_at: datetime | None = None
        self.last_nbc_predicted_wh: float | None = None
        self.last_commanded_amps: int | None = None
        # Settle-window state: set when Tesla amps are increased so that the
        # next few cycles don't over-react before the NBC prediction absorbs
        # the new load.
        self.last_tesla_increase_at: datetime | None = None
        self.last_tesla_increase_qh: str | None = None

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
            adjusted += effect.power_watts * seconds_remaining / NBCPeriod.PERIOD_SECS
        return adjusted


    def tesla_inflight_wh(
        self, reported_amps: int | None, seconds_remaining: int
    ) -> float:
        """Compute the still-unconfirmed Tesla amp-change contribution.

        Recomputed fresh every cycle using the vehicle API's reported
        current_amps, so the adjustment decays correctly as seconds_remaining
        shrinks rather than being frozen at command time.

        Returns zero when no amp command is in flight or the car has already
        reached the commanded level (confirmed by vehicle API).

        Args:
            reported_amps: current_amps from the vehicle API this cycle.
            seconds_remaining: Seconds left in the current quarter-hour.

        Returns:
            Wh still expected from the in-flight amp delta.
        """
        if self.last_commanded_amps is None or reported_amps is None:
            return 0.0
        delta = self.last_commanded_amps - reported_amps
        if delta == 0:
            return 0.0
        return StateTracker.delta_amps_to_wh(delta, seconds_remaining)

    def record_tesla_amp_increase(
        self, now: datetime, qh_name: str | None = None
    ) -> None:
        """Record that Tesla charge amps were just increased.

        Called by LoadManager when a set_amps effect that raised amps succeeds.
        Starts the settle window that suppresses premature turn-off reactions.

        Args:
            now: Timestamp of the amp increase command.
            qh_name: Current QH name (e.g. "QH1").  Stored so that a QH
                transition automatically invalidates the settle state — a
                deficit at the start of a new quarter is a genuine signal.
        """
        self.last_tesla_increase_at = now
        self.last_tesla_increase_qh = qh_name

    def clear_tesla_settle(self) -> None:
        """Clear settle state.

        Called when amps are decreased or Tesla charging is stopped, which
        means the system is already correcting; the settle window is no longer
        relevant.
        """
        self.last_tesla_increase_at = None
        self.last_tesla_increase_qh = None

    def is_settling_after_amp_increase(
        self, now: datetime, current_qh: str | None = None
    ) -> bool:
        """Return True if we are still in the post-amp-increase settle window.

        The settle window suppresses turn-off decisions for TESLA_SETTLE_SECS
        after a Tesla amp increase so that the first few post-confirmation
        cycles don't react to apparent deficits that are mostly NBC prediction
        lag or solar variability rather than genuine overshoot.

        The window is automatically expired when the QH name changes, because
        a new quarter-hour represents a fresh accounting period where even a
        modest deficit is a real signal.

        Args:
            now: Current timestamp.
            current_qh: Current QH name; if different from the QH in which the
                increase was recorded, the window is treated as expired.

        Returns:
            True if turn-off decisions should be suppressed.
        """
        if self.last_tesla_increase_at is None:
            return False
        if current_qh is not None and self.last_tesla_increase_qh != current_qh:
            return False
        elapsed = (now - self.last_tesla_increase_at).total_seconds()
        return elapsed < self.TESLA_SETTLE_SECS

    def has_pending_effect_since(self, nbc_timestamp: datetime) -> bool:
        """Return True if we took an action after the NBC timestamp by either measure.

        Checks both the wall clock timestamp and the data-point-at timestamp so
        that effects recorded with a future data-point-at are still detected.
        Uses a 60-second buffer on both measures to catch effects taken just
        before the NBC data point that have not yet been reflected in API data.

        Args:
            nbc_timestamp: The NBC data-point-at timestamp to compare against.

        Returns:
            True if any effect has either timestamp within 60 seconds after
            ``nbc_timestamp``.
        """
        buffer = timedelta(seconds=self.PENDING_EFFECT_MIN_SECS)
        for effect in self.pending_effects:
            if effect.timestamp > nbc_timestamp - buffer:
                return True
            if effect.data_point_at > nbc_timestamp - buffer:
                return True

        return False

    def pending_since_count(self, nbc_timestamp: datetime) -> int:
        """Return the number of effects taken after the given timestamp.

        Uses a 60-second buffer on both the wall clock and data-point-at
        timestamps, matching the buffer used by ``has_pending_effect_since``
        so the count reflects the same set of effects that triggers the
        waiting path.

        Args:
            nbc_timestamp: The NBC data-point-at timestamp to compare against.

        Returns:
            Count of effects whose wall clock or data-point-at timestamp is
            within 60 seconds after ``nbc_timestamp``.
        """
        buffer = timedelta(seconds=self.PENDING_EFFECT_MIN_SECS)
        return sum(
            1 for eff in self.pending_effects
            if eff.timestamp > nbc_timestamp - buffer
            or eff.data_point_at > nbc_timestamp - buffer
        )

    def prune_old_effects(
        self, data_point_at: datetime, now: datetime
    ) -> int:
        """Remove pending effects eligible for pruning based on dual age checks.

        An effect is pruned when it is over 60 seconds old by **both** measures:
          1. wall clock age:  now - effect.timestamp >= 60s
          2. data-point age:  effect.data_point_at <= data_point_at - 60s
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
        wall_cutoff = now - timedelta(seconds=self.PENDING_EFFECT_MIN_SECS)
        dp_cutoff = data_point_at - timedelta(seconds=self.PENDING_EFFECT_MIN_SECS)
        before = len(self.pending_effects)
        self.pending_effects = [
            eff for eff in self.pending_effects
            if eff.timestamp > wall_cutoff or eff.data_point_at > dp_cutoff
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
            "last_tesla_increase_at": (
                self.last_tesla_increase_at.isoformat()
                if self.last_tesla_increase_at else None
            ),
            "last_tesla_increase_qh": self.last_tesla_increase_qh,
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
    """

    now: datetime
    seconds_remaining: int
    state: StateTracker
    plugs: dict[str, Any]
    tesla: TeslaState | None
    dry_run: bool = False
    data_point_at: datetime | None = None


class GapMinder:
    """Bin-pack eligible loads to fill (or reduce) the NBC surplus/deficit gap."""

    TESLA_AMP_CHANGE_THRESHOLD = 1
    MIN_SECONDS_TO_ACT = 45
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
        self.charge_amps_max = charge_amps_max

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
            return []

        if gap > 0:
            return self._decide_turn_on(
                ctx, gap,
            )

        return self._decide_turn_off(
            ctx, abs(gap),
        )

    def _decide_turn_on(
        self,
        ctx: DecideContext,
        gap: float,
    ) -> list[PendingEffect]:
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

            elif self._tesla_supports_amps(plug, ctx.tesla) and ctx.tesla is not None and ctx.tesla.is_charging:
                logger.debug(
                    "[_decide_turn_on] %s: partial via Tesla "
                    "(capacity=%.1f Wh > gap %.1f Wh)",
                    "tesla",
                    capacity,
                    remaining_gap,
                )
                tesla_action = self._decide_tesla_amps(
                    ctx, remaining_gap,
                )
                if tesla_action:
                    actions.append(tesla_action)
                remaining_gap = 0
                break

            else:
                logger.debug(
                    "[_decide_turn_on] %s: too large "
                    "(capacity=%.1f Wh > gap %.1f Wh)",
                    name,
                    capacity,
                    remaining_gap,
                )

        if ctx.tesla and ctx.tesla.is_charging and remaining_gap > 0:
            logger.debug(
                "[_decide_turn_on] trying Tesla amps increase "
                "for remaining %.1f Wh",
                remaining_gap,
            )
            tesla_action = self._decide_tesla_amps(
                ctx, remaining_gap,
            )
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
        if ctx.tesla and ctx.tesla.is_charging and remaining_reduction > 0:
            logger.debug(
                "[_decide_turn_off] stopping Tesla to cover "
                "%.1f Wh remaining deficit",
                remaining_reduction,
            )
            actions.append(
                PendingEffect(
                    device_name="tesla",
                    action="turn_off",
                    timestamp=ctx.now,
                    data_point_at=dp,
                    power_watts=-StateTracker.amps_to_watts(ctx.tesla.current_amps),
                )
            )

        return actions

    def _tesla_supports_amps(
        self, _plug: Any, tesla: TeslaState | None
    ) -> bool:
        """Check if Tesla can handle partial amp adjustment."""
        if tesla is None:
            return False
        return (
            tesla.plugged_in
            and tesla.at_home
            and not tesla.at_charge_limit
        )

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
        if not tesla.plugged_in or not tesla.at_home:
            logger.debug(
                "[_decide_tesla_amps] skipped: plugged_in=%s at_home=%s",
                tesla.plugged_in,
                tesla.at_home,
            )
            return None
        if tesla.at_charge_limit:
            logger.debug("[_decide_tesla_amps] skipped: at_charge_limit")
            return None
        if ctx.seconds_remaining < self.MIN_SECONDS_TO_ACT:
            logger.debug(
                "[_decide_tesla_amps] skipped: too little time (%d s < %d s)",
                ctx.seconds_remaining, self.MIN_SECONDS_TO_ACT,
            )
            return None

        current_amps = tesla.current_amps or 0
        needed_watts = StateTracker.wh_to_watts(gap_wh, ctx.seconds_remaining)
        additional_amps = StateTracker.watts_to_amps(needed_watts, ctx.seconds_remaining)
        target_amps = current_amps + additional_amps
        # Clamp to the configured range; callers enforce controller-specific limits.
        target_amps = max(self.charge_amps_min, min(self.charge_amps_max, target_amps))

        logger.debug(
            "[_decide_tesla_amps] gap=%.1f Wh, seconds=%d, needed_watts=%.1f, "
            "current_amps=%d, additional_amps=%d, target_amps=%d",
            gap_wh,
            ctx.seconds_remaining,
            needed_watts,
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
        current_amps = tesla.current_amps
        if current_amps is None or current_amps <= 5:
            if not stop_allowed:
                logger.debug(
                    "[_decide_tesla_reduce] skipped stop: stop_allowed=False, "
                    "current_amps=%s",
                    current_amps,
                )
                return None
            return PendingEffect(
                device_name="tesla",
                action="turn_off",
                timestamp=ctx.now,
                data_point_at=ctx.data_point_at or ctx.now,
                power_watts=-StateTracker.amps_to_watts(current_amps),
            )

        current_watts = StateTracker.amps_to_watts(current_amps)
        reduce_watts = StateTracker.wh_to_watts(reduce_wh, ctx.seconds_remaining)
        new_watts = max(0, current_watts - reduce_watts)
        new_amps = StateTracker.watts_to_amps(new_watts, ctx.seconds_remaining)

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
            return PendingEffect(
                device_name="tesla",
                action="turn_off",
                timestamp=ctx.now,
                data_point_at=ctx.data_point_at or ctx.now,
                power_watts=-StateTracker.amps_to_watts(current_amps),
            )

        if ctx.seconds_remaining < self.MIN_SECONDS_TO_ACT:
            logger.debug(
                "[_decide_tesla_reduce] skipped: too little time (%d s < %d s)",
                ctx.seconds_remaining, self.MIN_SECONDS_TO_ACT,
            )
            return None

        # TODO test for 3-4A threshold for stop?
        if abs(new_amps - current_amps) < self.TESLA_AMP_CHANGE_THRESHOLD:
            logger.debug(
                "[_decide_tesla_reduce] skipped: change too small (%d - %d < %d)",
                new_amps, current_amps, self.TESLA_AMP_CHANGE_THRESHOLD,
            )
            return None

        return PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=ctx.now,
            data_point_at=ctx.data_point_at or ctx.now,
            power_watts=StateTracker.amps_to_watts(new_amps - current_amps),
            target_amps=new_amps,
        )
