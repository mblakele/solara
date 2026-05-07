"""
NBC cache/reader, device state tracking, and the bin-packing decision engine.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from load_models import DeviceState, PendingEffect, TeslaState


logger = logging.getLogger(__name__)


class NBCPeriod:
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


class NBCCache:
    """Cache for NBC (Non-Bypassable Charges) quarter-hour predictions.

    Historical quarters don't change, so we only refetch when the current
    incomplete quarter changes or the cache TTL expires.
    """

    def __init__(self, ttl_seconds: int = 60) -> None:
        self._cache: dict[str, Any] | None = None
        self._cached_at: datetime | None = None
        self._cached_qh: str | None = None
        self._ttl = timedelta(seconds=ttl_seconds)
        self._seconds_remaining_at_fetch: int | None = None

    def get_or_fetch(
        self,
        _device_name: str,
        current_qh: str | None,
        fetch_func: Callable[[], dict[str, Any] | None],
        pre_fetched: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Return (data, was_fresh).

        Returns cached data if: not expired AND same incomplete QH as before.
        Otherwise uses pre_fetched (if available) or calls fetch_func and
        returns fresh data.

        Args:
            _device_name: Name of the device (reserved for future use).
            current_qh: Current incomplete quarter-hour name, or None.
            fetch_func: Callable that returns fresh parsed NBC data.
            pre_fetched: Already-fetched parsed data to reuse when cache
                is invalid, avoiding a redundant API call.

        Returns:
            Tuple of (parsed_nbc_data, was_fresh). was_fresh is True when
            fetch_func was actually called.
        """
        now = datetime.now(timezone.utc)

        if (
            self._cache is not None
            and self._cached_at is not None
            and self._cached_qh == current_qh
            and now - self._cached_at < self._ttl
        ):
            return self._cache, False

        fresh_data: dict[str, Any] | None
        if pre_fetched is not None:
            fresh_data = pre_fetched
        else:
            fresh_data = fetch_func()

        self._cache = fresh_data
        self._cached_at = now
        self._cached_qh = current_qh
        self._seconds_remaining_at_fetch = (
            fresh_data.get("seconds_remaining") if fresh_data is not None else None
        )

        return fresh_data, True

    def is_valid(self, now: datetime) -> tuple[bool, str | None]:
        """Check if cache has a valid (non-expired) entry.

        Args:
            now: Current timestamp to check against TTL.

        Returns:
            Tuple of (is_valid, cached_qh_name). If not valid, qh_name is None.
        """
        if (
            self._cache is not None
            and self._cached_at is not None
            and (now - self._cached_at) < self._ttl
        ):
            return True, self._cached_qh
        return False, None

    def has_qh_likely_ended(self, now: datetime) -> bool:
        """Return True if the cached QH has likely ended by wall-clock time.

        Compares elapsed seconds since the last fetch against the
        seconds_remaining that was recorded at fetch time. When elapsed >=
        seconds_remaining the quarter has almost certainly rolled over, so the
        caller should treat the cache as invalid and probe for a fresh QH name
        even if the TTL has not yet expired.

        Args:
            now: Current timestamp to compare against the fetch time.

        Returns:
            True when the cached QH is likely complete; False when any
            required attribute is missing or the QH is still in progress.
        """
        if (
            self._cached_at is None
            or self._seconds_remaining_at_fetch is None
        ):
            return False
        elapsed = (now - self._cached_at).total_seconds()
        return elapsed >= self._seconds_remaining_at_fetch

    def invalidate(self) -> None:
        """Clear the cache."""
        self._cache = None
        self._cached_at = None
        self._cached_qh = None
        self._seconds_remaining_at_fetch = None


class NBCReader:
    """Reads current QH predicted_wh from metrics for a specific device.

    Uses NBCCache to avoid redundant API calls. Accepts a fetch callable
    that returns fresh metrics data when cache misses occur.
    """

    def __init__(
        self,
        cache: NBCCache | None = None,
        metrics_fetch: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self.cache = cache or NBCCache(ttl_seconds=50)
        self.metrics_fetch = metrics_fetch

    def get_current_qh(
        self, device_name: str
    ) -> tuple[str, float, int, datetime] | None:
        """Return (qh_name, predicted_wh, seconds_remaining, data_point_at) for QH.

        Uses cache to avoid redundant API calls. Returns None if all quarters
        are complete or no data available. The data_point_at timestamp is the
        time of the most recent per-second data point (fetched_at minus API
        lag), which is what stale detection and waiting detection use.

        Args:
            device_name: Name of the VUE device to query.

        Returns:
            Tuple of (qh_name, predicted_wh in Wh, seconds remaining in QH,
            data_point_at), or None if no incomplete QH available.
        """
        if self.metrics_fetch is None:
            return None
        fetch = self.metrics_fetch

        def _fetch_and_parse() -> dict[str, Any] | None:
            metrics_data = fetch()
            if metrics_data is None:
                return None
            parsed = self._parse_metrics(device_name, metrics_data)
            if parsed is not None:
                parsed["_fetched_at"] = metrics_data.get(
                    "_fetched_at", datetime.now(timezone.utc)
                )
            return parsed

        current_qh: str | None = None
        pre_fetched: dict[str, Any] | None = None
        now = datetime.now(timezone.utc)
        cache_valid, cached_qh = self.cache.is_valid(now)
        if cache_valid and not self.cache.has_qh_likely_ended(now):
            current_qh = cached_qh
        else:
            probe = fetch()
            if probe is not None:
                current_qh = self._find_incomplete_qh(probe, device_name)
                pre_fetched = self._parse_metrics(device_name, probe)
                if pre_fetched is not None:
                    pre_fetched["_fetched_at"] = probe.get(
                        "_fetched_at", datetime.now(timezone.utc)
                    )

        cached, _ = self.cache.get_or_fetch(
            device_name, current_qh, _fetch_and_parse, pre_fetched=pre_fetched
        )
        if cached is None:
            return None
        fetched_at = cached.get("_fetched_at", now)
        data_lag_secs: float = cached.get("_data_lag_secs", 0.0)
        # Derive the actual data-point timestamp so callers don't need to
        # recompute it from fetched_at + lag.
        data_point_at = fetched_at - timedelta(seconds=data_lag_secs)
        return (  # type: ignore[return-value]
            cached.get("qh_name"),
            cached.get("predicted_wh", 0),
            cached.get("seconds_remaining", 0),
            data_point_at,
        )

    def get_current_qh_direct(
        self, device_name: str, metrics_data: dict[str, Any] | None
    ) -> tuple[str, float, int] | None:
        """Parse metrics data directly without cache.

        Useful for testing with injected mock data.

        Args:
            device_name: Name of the VUE device to query.
            metrics_data: The raw metrics dict from HourlyProjection, or None.

        Returns:
            Tuple of (qh_name, predicted_wh in Wh, seconds remaining in QH),
            or None if no incomplete QH available.
        """
        result = self._parse_metrics(device_name, metrics_data)
        if result is None:
            return None
        return result["qh_name"], result["predicted_wh"], result["seconds_remaining"]

    def _find_incomplete_qh(
        self, metrics_data: dict[str, Any], device_name: str
    ) -> str | None:
        """Find the name of the first incomplete QH for a device.

        Args:
            metrics_data: The raw metrics dict from HourlyProjection.
            device_name: Name of the VUE device to query.

        Returns:
            QH name (e.g., "QH1") or None if all quarters are complete.
        """
        devices = metrics_data.get("devices", [])
        target_device = None
        for dev in devices:
            if dev.get("name") == device_name:
                target_device = dev
                break

        if target_device is None:
            return None

        nbc = target_device.get("nbc")
        if nbc is None:
            return None

        qh_order = ["QH1", "QH2", "QH3", "QH4"]
        for qh_name in qh_order:
            qh_data = nbc.get(qh_name)
            if qh_data is None:
                continue
            if not qh_data.get("complete", True):
                return qh_name

        return None

    def _parse_metrics(
        self, device_name: str, metrics_data: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Parse metrics data and extract incomplete QH info.

        Args:
            device_name: Name of the VUE device to query.
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

        if target_device is None:
            return None

        nbc = target_device.get("nbc")
        if nbc is None:
            return None

        qh_order = ["QH1", "QH2", "QH3", "QH4"]
        for qh_name in qh_order:
            qh_data = nbc.get(qh_name)
            if qh_data is None:
                continue
            if not qh_data.get("complete", True):
                predicted_wh = qh_data.get("predicted_wh", 0)
                seconds_remaining = qh_data.get("remaining_seconds", NBCPeriod.PERIOD_SECS)
                return {
                    "qh_name": qh_name,
                    "predicted_wh": predicted_wh,
                    "seconds_remaining": seconds_remaining,
                    "_data_lag_secs": metrics_data.get("_data_lag_secs", 0.0),
                }

        return None


class StateTracker:
    """In-memory state of managed devices and pending effects."""

    # Asymmetric debounce: turn-on is conservative (prevents chatter), turn-off
    # is fast (enables rapid recovery from an over-commit without waiting out the
    # full on-guard period, which would leave a large deficit in place).
    MIN_TOGGLE_ON_SECS = 60
    MIN_TOGGLE_OFF_SECS = 10
    STALE_THRESHOLD_SECS = 61
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
    def amps_to_watts(current_amps: float) -> float:
        """Convert current in amps to watts at nominal voltage."""
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

    def estimated_current_wh(self, nbc_predicted_wh: float) -> float:
        """Estimate actual current Wh by adding pending effects to NBC prediction.

        Used in run_cycle() to adjust predictions before calling decide(),
        so the engine accounts for actions already taken this quarter-hour
        without waiting for fresh API data.

        Tesla set_amps effects are excluded here — their contribution is
        recomputed live each cycle in _cycle_async_phase using the vehicle
        API's reported current_amps, so it never goes stale as seconds_remaining
        shrinks. See tesla_inflight_wh().

        Args:
            nbc_predicted_wh: Raw predicted Wh from NBC cache/API.

        Returns:
            Adjusted Wh estimate including all pending effect deltas.
        """
        adjusted = nbc_predicted_wh
        for effect in self.pending_effects:
            if effect.device_name == "tesla" and effect.action == "set_amps":
                continue
            adjusted += effect.power_delta_wh
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
        """Return True if we took an action AFTER the last NBC data point."""
        for effect in self.pending_effects:
            if effect.timestamp > nbc_timestamp:
                return True
        return False

    def pending_since_count(self, nbc_timestamp: datetime) -> int:
        """Return the number of effects taken after the given timestamp."""
        return sum(
            1 for eff in self.pending_effects
            if eff.timestamp > nbc_timestamp
        )

    def prune_old_effects(self, cutoff: datetime) -> int:
        """Remove pending effects older than the cutoff timestamp.

        Effects before a fresh NBC fetch are already reflected in that data,
        so they no longer serve a purpose and would cause unbounded list growth.

        Args:
            cutoff: Only keep effects with timestamp >= this value.

        Returns:
            Number of effects removed.
        """
        before = len(self.pending_effects)
        self.pending_effects = [
            eff for eff in self.pending_effects
            if eff.timestamp > cutoff
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
                "power_delta_wh": eff.power_delta_wh,
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


class TetrisEngine:
    """Fit flexible loads into the NBC surplus gap like tetris pieces."""

    TESLA_AMP_CHANGE_THRESHOLD = 1
    MIN_SECONDS_TO_ACT = 60
    # Capacity discount applied to turn-on decisions.  A load is only turned on
    # if its discounted capacity fits in the gap, reserving headroom for solar
    # variability and NBC prediction error.  Acts as damping to reduce overshoot.
    TURN_ON_MARGIN = 1.0 # was 0.85 TODO TESTING
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
        """Initialize the TetrisEngine.

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
        predicted_wh: float,
        target_wh: float,
        seconds_remaining: int,
        state: StateTracker,
        plugs: dict[str, Any],
        tesla: TeslaState | None,
        dry_run: bool = False,
    ) -> list[PendingEffect]:
        """Decide what actions to take based on the predicted Wh and target Wh.

        Args:
            predicted_wh: The predicted Wh for the current quarter-hour.
            target_wh: The target Wh to achieve (negative = surplus).
            seconds_remaining: Seconds left in the current quarter-hour.
            state: The current state tracker.
            plugs: Dictionary of plug configurations.
            tesla: Current Tesla state, if available.
            dry_run: When True, skip mutating state.devices so subsequent
                dry-run cycles re-evaluate instead of seeing stale desired_state.

        Returns:
            List of PendingEffect objects representing actions to take.
        """
        gap = target_wh - predicted_wh
        abs_gap = abs(gap)

        if abs_gap <= self.HYSTERESIS_WH:
            return []

        now = datetime.now(timezone.utc)

        if gap > 0:
            return self._decide_turn_on(
                gap, seconds_remaining, state, plugs, tesla, now, dry_run
            )

        return self._decide_turn_off(
            abs(gap), seconds_remaining, state, plugs, tesla, now, dry_run
        )

    def _decide_turn_on(
        self,
        gap_wh: float,
        seconds_remaining: int,
        state: StateTracker,
        plugs: dict[str, Any],
        tesla: TeslaState | None,
        now: datetime,
        dry_run: bool = False,
    ) -> list[PendingEffect]:
        """Turn on loads to fill the surplus gap.

        Returns:
            List of PendingEffect actions. Logs debug details for each
            plug evaluated so the caller can understand rejections.
        """
        actions: list[PendingEffect] = []
        remaining_gap = gap_wh

        logger.debug(
            "[_decide_turn_on] gap=%.1f Wh, seconds_remaining=%d",
            gap_wh,
            seconds_remaining,
        )

        candidates: list[tuple[int, str, Any]] = []
        for name, plug in plugs.items():
            if not state.can_toggle(name, now, turning_on=True):
                logger.debug(
                    "[_decide_turn_on] %s: skipped (debounce)",
                    name,
                )
                continue

            dev_state = state.devices.get(name)

            if plug.role == "fixed":
                if dev_state is None or dev_state.desired_state is False:
                    candidates.append((plug.priority, name, plug))
                    logger.debug(
                        "[_decide_turn_on] %s: eligible (fixed, off)",
                        name,
                    )
                else:
                    logger.debug(
                        "[_decide_turn_on] %s: skipped (already on)",
                        name,
                    )
            elif plug.role == "flexible":
                if dev_state is None or dev_state.desired_state is False:
                    candidates.append((plug.priority, name, plug))
                    logger.debug(
                        "[_decide_turn_on] %s: eligible (flexible, off)",
                        name,
                    )
                else:
                    logger.debug(
                        "[_decide_turn_on] %s: skipped (already on)",
                        name,
                    )

        # Higher priority number = more important; sort descending so most
        # important eligible plugs are turned on first.
        candidates.sort(key=lambda x: x[0], reverse=True)

        for _, name, plug in candidates:
            capacity = StateTracker.watts_to_wh(plug.power_watts, seconds_remaining)
            # Apply turn-on margin: only commit if the discounted capacity fits in
            # the gap.  This reserves headroom for solar variability and NBC
            # prediction error, acting as damping to reduce overshoot.
            discounted_capacity = capacity * self.TURN_ON_MARGIN

            if discounted_capacity <= remaining_gap:
                logger.debug(
                    "[_decide_turn_on] %s: turning on "
                    "(capacity=%.1f Wh, discounted=%.1f Wh fits in gap %.1f Wh)",
                    name,
                    capacity,
                    discounted_capacity,
                    remaining_gap,
                )
                actions.append(
                    PendingEffect(
                        device_name=name,
                        action="turn_on",
                        timestamp=now,
                        power_delta_wh=capacity,
                    )
                )
                remaining_gap -= capacity
                if not dry_run:
                    state.devices[name] = DeviceState(
                        name=name, last_toggle=now, desired_state=True
                    )

            elif self._tesla_supports_partial(plug, tesla) and tesla is not None and tesla.is_charging:
                logger.debug(
                    "[_decide_turn_on] %s: partial via Tesla "
                    "(capacity=%.1f Wh > gap %.1f Wh)",
                    "tesla",
                    capacity,
                    remaining_gap,
                )
                tesla_action = self._decide_tesla_amps(remaining_gap, seconds_remaining, tesla, now)
                if tesla_action:
                    actions.append(tesla_action)
                remaining_gap = 0
                break

            else:
                logger.debug(
                    "[_decide_turn_on] %s: too large "
                    "(capacity=%.1f Wh > gap %.1f Wh, no partial)",
                    name,
                    capacity,
                    remaining_gap,
                )

        if tesla and tesla.is_charging and remaining_gap > 0:
            logger.debug(
                "[_decide_turn_on] trying Tesla amps increase "
                "for remaining %.1f Wh",
                remaining_gap,
            )
            tesla_action = self._decide_tesla_amps(
                remaining_gap, seconds_remaining, tesla, now
            )
            if tesla_action:
                actions.append(tesla_action)

        return actions

    def _decide_turn_off(
        self,
        gap_wh: float,
        seconds_remaining: int,
        state: StateTracker,
        plugs: dict[str, Any],
        tesla: TeslaState | None,
        now: datetime,
        dry_run: bool = False,
    ) -> list[PendingEffect]:
        """Turn off loads to reduce consumption.

        Priority order:
          1. Reduce Tesla charge amps (partial, no stop).
          2. Disable plugs in priority order (lowest-priority first).
          3. Stop Tesla charging if a deficit still remains.
        """
        actions: list[PendingEffect] = []
        remaining_reduction = gap_wh

        logger.debug(
            "[_decide_turn_off] gap=%.1f Wh, seconds_remaining=%d",
            gap_wh,
            seconds_remaining,
        )

        # ── Step 1: reduce Tesla charge amps first (no stop) ──────────────────
        if tesla and tesla.is_charging:
            logger.debug(
                "[_decide_turn_off] trying Tesla amps-only reduce "
                "for %.1f Wh remaining",
                remaining_reduction,
            )
            tesla_action = self._decide_tesla_reduce(
                remaining_reduction,
                seconds_remaining,
                tesla,
                now,
                stop_allowed=False,
            )
            if tesla_action:
                actions.append(tesla_action)
                current_amps = tesla.current_amps or 0
                target_amps = tesla_action.target_amps or 0
                savings = StateTracker.delta_amps_to_wh(
                    current_amps - target_amps, seconds_remaining
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

        for name, plug in plugs.items():
            if plug.role != "flexible":
                logger.debug(
                    "[_decide_turn_off] %s: skipped (not flexible)",
                    name,
                )
                continue
            if not state.can_toggle(name, now, turning_on=False):
                logger.debug(
                    "[_decide_turn_off] %s: skipped (debounce)",
                    name,
                )
                continue

            dev_state = state.devices.get(name)
            if dev_state and dev_state.desired_state is True:
                candidates.append((plug.priority, name, plug))
                logger.debug(
                    "[_decide_turn_off] %s: eligible (flexible, on)",
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
            savings = StateTracker.watts_to_wh(plug.power_watts, seconds_remaining)

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
                    timestamp=now,
                    power_delta_wh=-savings,
                )
            )
            remaining_reduction -= savings
            if not dry_run:
                state.devices[name] = DeviceState(
                    name=name, last_toggle=now, desired_state=False
                )
            if remaining_reduction <= 0:
                break

        # ── Step 3: if deficit remains, stop Tesla charging ────────────
        if tesla and tesla.is_charging and remaining_reduction > 0:
            logger.debug(
                "[_decide_turn_off] stopping Tesla to cover "
                "%.1f Wh remaining deficit",
                remaining_reduction,
            )
            actions.append(
                PendingEffect(
                    device_name="tesla",
                    action="turn_off",
                    timestamp=now,
                    power_delta_wh=-remaining_reduction,
                )
            )

        return actions

    def _tesla_supports_partial(
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
        gap_wh: float,
        seconds_remaining: int,
        tesla: TeslaState,
        now: datetime,
    ) -> PendingEffect | None:
        """Adjust Tesla charge amps to fill residual gap."""
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
        if seconds_remaining < self.MIN_SECONDS_TO_ACT:
            logger.debug(
                "[_decide_tesla_amps] skipped: too little time (%d s < %d s)",
                seconds_remaining, self.MIN_SECONDS_TO_ACT,
            )
            return None

        current_amps = tesla.current_amps or 0
        needed_watts = StateTracker.wh_to_watts(gap_wh, seconds_remaining)
        additional_amps = StateTracker.watts_to_amps(needed_watts, seconds_remaining)
        target_amps = current_amps + additional_amps
        # Clamp to the configured range; callers enforce controller-specific limits.
        target_amps = max(self.charge_amps_min, min(self.charge_amps_max, target_amps))

        logger.debug(
            "[_decide_tesla_amps] gap=%.1f Wh, seconds=%d, needed_watts=%.1f, "
            "current_amps=%d, additional_amps=%d, target_amps=%d",
            gap_wh,
            seconds_remaining,
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

        power_delta = StateTracker.delta_amps_to_wh(
            target_amps - current_amps, seconds_remaining
        )
        return PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=now,
            power_delta_wh=0.0,  # recomputed live via tesla_inflight_wh each cycle
            target_amps=target_amps,
        )

    def _decide_tesla_reduce(
        self,
        reduce_wh: float,
        seconds_remaining: int,
        tesla: TeslaState,
        now: datetime,
        stop_allowed: bool = True,
    ) -> PendingEffect | None:
        """Reduce Tesla charge amps, or stop charging if amps can't be reduced further.

        Args:
            reduce_wh: Wh reduction needed.
            seconds_remaining: Seconds left in the current quarter-hour.
            tesla: Current Tesla state.
            now: Current timestamp.
            stop_allowed: When False, return None instead of issuing a turn_off
                command. Used when the caller wants amps-only reduction and will
                handle stopping as a separate last-resort step.
        """
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
                timestamp=now,
                power_delta_wh=-reduce_wh,
            )

        current_watts = StateTracker.amps_to_watts(current_amps)
        reduce_watts = StateTracker.wh_to_watts(reduce_wh, seconds_remaining)
        new_watts = max(0, current_watts - reduce_watts)
        new_amps = StateTracker.watts_to_amps(new_watts, seconds_remaining)

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
                timestamp=now,
                power_delta_wh=-reduce_wh,
            )

        if seconds_remaining < self.MIN_SECONDS_TO_ACT:
            logger.debug(
                "[_decide_tesla_reduce] skipped: too little time (%d s < %d s)",
                seconds_remaining, self.MIN_SECONDS_TO_ACT,
            )
            return None

        # TODO test for 3-4A threshold for stop?
        if abs(new_amps - current_amps) < self.TESLA_AMP_CHANGE_THRESHOLD:
            logger.debug(
                "[_decide_tesla_reduce] skipped: change too small (%d - %d < %d)",
                new_amps, current_amps, self.TESLA_AMP_CHANGE_THRESHOLD,
            )
            return None

        power_delta = StateTracker.delta_amps_to_wh(
            new_amps - current_amps, seconds_remaining
        )

        return PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=now,
            power_delta_wh=power_delta,
            target_amps=new_amps,
        )
