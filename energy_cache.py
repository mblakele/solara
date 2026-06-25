"""
Data caching and management.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from clock import Clock, RealClock
from constants import MIN_SLEEP_SECS
from util import NBCQuarter, compute_nbc_quarter, qh_seconds_remaining

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class FrozenQH:
    """A completed quarter-hour with pre-computed NBC statistics.

    Once a QH completes (900 samples), the per-second data is no longer
    needed.  Only the final ``NBCQuarter`` result is retained, saving
    ~7 KB per frozen QH.

    Attributes:
        data_start: Timestamp of the QH boundary (start of the 15-min window).
        nbc_result: Pre-computed NBC metrics with ``complete=True``.
    """

    data_start: datetime
    nbc_result: NBCQuarter


@dataclass(slots=True, kw_only=True)
class CurrentQH:
    """The current in-progress quarter-hour with raw per-second samples.

    Raw samples are retained because ``compute_nbc_quarter()`` needs them
    for trailing-window rate extrapolation while the QH is still active.

    Attributes:
        data_start: Timestamp of the QH boundary (start of the 15-min window).
        samples: Per-second energy values (Wh), 0–899 elements.
    """

    data_start: datetime
    samples: list[float]


@dataclass(frozen=True, slots=True, kw_only=True)
class EnergyCacheData:
    """Immutable snapshot of cached per-second energy data.

    This dataclass encapsulates all state that the ``EnergyCache`` wrapper
    tracks.  Being frozen and using ``slots`` make instances lightweight
    and safe to share between threads without deep-copying.

    The QH-block fields (``frozen_qhs``, ``current_qh``) are the primary
    storage model.  The legacy flat fields (``samples``, ``data_start``,
    etc.) are kept for backward compatibility and will be removed once all
    callers migrate to the QH-block API.

    Attributes:
        frozen_qhs: Completed QH blocks with pre-computed NBC results.
            Max 3, oldest first.
        current_qh: The in-progress QH accumulating raw samples.
        last_fetch_at: When data was last fetched from the API.
        full_metrics_dict: Optional full metrics dict (e.g. with "devices" key)
            returned on cache hits, preserved from the original fetch.
        quantization_seconds: Detected quantization interval in seconds.
        quantization_offset: Offset within the quantization period (seconds).
        quantization_confidence: Confidence in the quantization detection (0–1).
        samples: Per-second energy values (Wh). Derived from QH blocks.
        data_start: Timestamp of the first sample. Derived from QH blocks.
        last_sample_at: Timestamp of the last sample. Derived from QH blocks.
        sample_count: Number of samples. Derived from QH blocks.
    """

    frozen_qhs: list[FrozenQH] | None = None
    current_qh: CurrentQH | None = None
    last_fetch_at: datetime | None = None
    full_metrics_dict: dict[str, Any] | None = None
    quantization_seconds: int | None = None
    quantization_offset: int | None = None
    quantization_confidence: float | None = None
    samples: list[float] | None = None
    data_start: datetime | None = None
    last_sample_at: datetime | None = None
    sample_count: int | None = None


class EnergyCache:
    """Unified cache for per-second energy samples with sliding-window semantics.

    Stores raw Wh-per-second data points in a time-ordered list keyed by
    device name (currently only one device is used).  Supports incremental
    partial-range fetches: when new data arrives, old points older than
    3600 s are pruned and only the delta is fetched from pyemvue.

    Thread-safe via internal lock for concurrent access between Flask and
    LoadManager background threads.

    The public interface is a thin wrapper around the frozen ``EnergyCacheData``
    dataclass.  All mutating ``get_or_fetch`` logic is encapsulated inside the
    wrapper; callers receive immutable snapshots.

    Attributes:
        _data: Frozen ``EnergyCacheData`` snapshot or ``None`` when empty.
        _ttl_seconds: Maximum age of cached data before forcing a refresh.
        _lock: Thread-safety lock.
    """

    def __init__(self, ttl_seconds: int = 30, clock: Clock | None = None) -> None:
        self._data: EnergyCacheData | None = None
        self._ttl_seconds: int = ttl_seconds
        self._clock: Clock = clock if clock is not None else RealClock()
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public properties (mimic the old direct-attribute interface)
    # ------------------------------------------------------------------

    @property
    def data(self) -> EnergyCacheData | None:
        """The current ``EnergyCacheData`` snapshot, or ``None`` if empty."""
        return self._data

    @property
    def lock(self) -> threading.Lock:
        """Thread-safety lock."""
        return self._lock

    @property
    def ttl_seconds(self) -> int:
        """TTL in seconds after which cached data is considered stale."""
        return self._ttl_seconds

    @property
    def samples(self) -> list[float] | None:
        """Per-second energy samples, or ``None`` if empty.

        Returns:
            List of float Wh values, or ``None``.
        """
        if self._data is None:
            return None
        return self._data.samples

    @property
    def data_start(self) -> datetime | None:
        """Timestamp of the first sample, or ``None`` if empty."""
        if self._data is None:
            return None
        return self._data.data_start

    @property
    def last_sample_at(self) -> datetime | None:
        """Timestamp of the most recent sample, or ``None`` if empty."""
        if self._data is None:
            return None
        return self._data.last_sample_at

    @property
    def last_fetch_at(self) -> datetime | None:
        """Timestamp of the last API fetch, or ``None`` if no fetch yet."""
        if self._data is None:
            return None
        return self._data.last_fetch_at

    @property
    def sample_count(self) -> int | None:
        """Number of samples, or ``None`` if empty."""
        if self._data is None:
            return None
        return self._data.sample_count

    @property
    def quantization_seconds(self) -> int | None:
        """Detected quantization interval in seconds, or ``None``."""
        if self._data is None:
            return None
        return self._data.quantization_seconds

    @property
    def quantization_offset(self) -> int | None:
        """Offset within the quantization period, or ``None``."""
        if self._data is None:
            return None
        return self._data.quantization_offset

    @property
    def quantization_confidence(self) -> float | None:
        """Confidence in quantization detection (0–1), or ``None``."""
        if self._data is None:
            return None
        return self._data.quantization_confidence

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def is_valid(self, now: datetime) -> bool:
        """Check if cache has non-expired data.

        Args:
            now: Current time for TTL check.

        Returns:
            ``True`` if cache has data and it hasn't expired.
        """
        with self._lock:
            return self._is_valid_unlocked(now)

    def _is_valid_unlocked(self, now: datetime) -> bool:
        """Check if cache has non-expired data (caller must hold lock).

        Args:
            now: Current time for TTL check.

        Returns:
            ``True`` if cache has data and it hasn't expired.
        """
        if self._data is None:
            return False
        if self._data.last_fetch_at is None:
            return False
        elapsed = now - self._data.last_fetch_at
        return elapsed.total_seconds() < self._ttl_seconds

    # ------------------------------------------------------------------
    # QH-block update logic
    # ------------------------------------------------------------------

    @staticmethod
    def _qh_boundaries(dt: datetime) -> tuple[datetime, datetime]:
        """Return (start, end) of the QH window containing *dt*.

        QH periods are aligned to hour boundaries:
        QH1: 0-899s, QH2: 900-1799s, QH3: 1800-2699s, QH4: 2700-3599s.
        """
        utc = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        seconds_into_hour = utc.hour * 3600 + utc.minute * 60 + utc.second
        qh_index = seconds_into_hour // 900
        qh_start = utc.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(seconds=qh_index * 900)
        qh_end = qh_start + timedelta(seconds=900)
        if dt.tzinfo is None:
            return qh_start.replace(tzinfo=None), qh_end.replace(tzinfo=None)  # type: ignore[return-value]
        return qh_start, qh_end

    def _update_qh(
        self,
        new_samples: list[float],
        result_data_start: datetime | None,
        now: datetime,
    ) -> None:
        """Update QH-block storage with new samples.

        Determines which QH window the samples belong to, splits at
        boundaries if needed, and freezes completed QHs.

        Args:
            new_samples: Per-second Wh values from the API.
            result_data_start: Start timestamp of the new samples.
            now: Current time for QH boundary computation.
        """
        if not new_samples:
            return

        data_start = result_data_start or now

        # Initialize empty data if needed.
        if self._data is None:
            self._data = EnergyCacheData(last_fetch_at=now)

        frozen = list(self._data.frozen_qhs) if self._data.frozen_qhs else []
        current = self._data.current_qh

        # If current QH is from a different window, finalize it first.
        # Freeze whatever samples we have — partial QHs get complete=False
        # so callers know the data is incomplete.  The next fetch starts
        # from the NEW QH boundary, so discarding would lose this data.
        if current is not None and current.data_start != self._qh_boundaries(data_start)[0]:
            if len(current.samples) >= 900:
                nbc = compute_nbc_quarter(current.samples[:900])
            else:
                nbc = compute_nbc_quarter(current.samples)
            if nbc is not None:
                frozen.append(FrozenQH(data_start=current.data_start, nbc_result=nbc))
            current = None

        # Process all new samples, splitting at QH boundaries.
        remaining = list(new_samples)
        remaining_start = (data_start or now).replace(microsecond=0)

        while remaining:
            qh_start, qh_end = self._qh_boundaries(remaining_start)
            secs_to_boundary = int((qh_end - remaining_start).total_seconds())
            take = min(secs_to_boundary, len(remaining))
            if take <= 0:
                take = 1

            chunk = remaining[:take]
            remaining = remaining[take:]
            remaining_start = remaining_start + timedelta(seconds=take)

            if current is None:
                current = CurrentQH(data_start=qh_start, samples=list(chunk))
            elif current.data_start == qh_start:
                # Skip samples that overlap with existing data.
                # This happens when the API re-fetches from the same QH boundary.
                # remaining_start was already advanced by `take`, so chunk start is -take.
                skip = max(0, len(current.samples) - int(((remaining_start - timedelta(seconds=take)) - current.data_start).total_seconds()))
                if skip > 0:
                    chunk = chunk[skip:]
                if chunk:
                    current.samples.extend(chunk)
            else:
                current = CurrentQH(data_start=qh_start, samples=list(chunk))

            # Freeze if current QH is complete.
            if len(current.samples) >= 900:
                nbc = compute_nbc_quarter(current.samples[:900])
                if nbc is not None:
                    frozen.append(FrozenQH(data_start=current.data_start, nbc_result=nbc))
                overflow = current.samples[900:]
                if overflow:
                    next_qh_start = current.data_start + timedelta(seconds=900)
                    current = CurrentQH(data_start=next_qh_start, samples=overflow)
                else:
                    current = None

        # Prune frozen QHs to max 3.
        while len(frozen) > 3:
            frozen.pop(0)

        # Compute derived fields.
        all_samples = current.samples if current is not None else []

        data_start_derived = current.data_start if current else None
        last_sample_at = (
            (current.data_start + timedelta(seconds=len(current.samples) - 1))
            if current and current.samples
            else None
        )

        self._data = EnergyCacheData(
            frozen_qhs=frozen,
            current_qh=current,
            last_fetch_at=now,
            full_metrics_dict=self._data.full_metrics_dict,
            quantization_seconds=self._data.quantization_seconds,
            quantization_offset=self._data.quantization_offset,
            quantization_confidence=self._data.quantization_confidence,
            samples=all_samples if all_samples else None,
            data_start=data_start_derived,
            last_sample_at=last_sample_at,
            sample_count=len(all_samples) if all_samples else None,
        )

    def _prune_frozen_qhs(self) -> None:
        """Drop frozen QHs beyond the most recent 3."""
        if self._data is None or self._data.frozen_qhs is None:
            return
        while len(self._data.frozen_qhs) > 3:
            self._data.frozen_qhs.pop(0)

    # ------------------------------------------------------------------
    # Build result dict (for non-incremental callers)
    # ------------------------------------------------------------------

    def _build_result(self) -> dict[str, Any] | None:
        """Build the result dict from cached data (caller must hold lock).

        Returns the full result dict if available (with "devices", "nbc", etc.),
        or builds a minimal dict from raw samples.

        Returns:
            Cached metrics dict or ``None`` if no cached data exists.
        """
        if self._data is None:
            return None

        # Return the full metrics dict if it was stored during the original
        # fetch.  This preserves keys like "devices", "nbc", "instant" that
        # callers (e.g. the index endpoint) need but which _build_result
        # previously discarded on cache hits.
        if self._data.full_metrics_dict is not None:
            return self._data.full_metrics_dict

        # Fallback: build minimal dict from raw samples.
        result: dict[str, Any] = {}
        if self._data.samples is not None:
            result["per_second_data"] = self._data.samples
        if self._data.data_start is not None:
            result["data_start"] = self._data.data_start

        return result

    # ------------------------------------------------------------------
    # Main API
    # ------------------------------------------------------------------

    def get_or_fetch(
        self,
        fetch_func: Callable[[], dict[str, Any] | None],
        now: datetime,
        force: bool = False,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Return *(metrics_dict_or_none, was_fresh)*.

        If cache is valid and *force* is ``False``, return cached data with
        ``was_fresh=False``.  Otherwise calls *fetch_func* (which should do
        an incremental or full API call), stores the result, and returns
        ``was_fresh=True``.

        The *fetch_func* may return either:

        * A full metrics dict (e.g. ``HourlyProjection.metrics``) — stored
          as a nested ``_data`` fallback and returned directly to the
          caller.
        * An incremental dict with ``"per_second_data"`` and ``"data_start"``
          keys — the per-second samples are merged into the internal data
          for use by callers that need raw data (e.g. ``NBCReader``).

        Args:
            fetch_func: Callable that returns fresh data dict.
            now: Current datetime.
            force: When ``True``, bypass cache and always fetch.

        Returns:
            Tuple of *(metrics_dict_or_none, was_fresh)*.
        """
        with self._lock:
            # Check if cache is valid (non-expired data exists).
            if not force and self._is_valid_unlocked(now):
                result = self._build_result()
                logger.debug(
                    "EnergyCache cache_hit: keys=%s, "
                    "sample_count=%d, data_start=%s",
                    list(result.keys()) if result else [],
                    len(result.get("per_second_data", [])) if result else 0,
                    result.get("data_start") if result else None,
                )
                return result, False

            # Fetch fresh data.
            result = fetch_func()

            if result is not None:
                new_samples: list[float] = []

                # Extract per-second data from the result dict.
                if "per_second_data" in result:
                    new_samples = list(result["per_second_data"])
                elif "devices" in result:
                    # Extract from nested devices (full metrics dict path).
                    new_samples = [
                        point
                        for device in result["devices"]
                        for point in device.get("per_second_data", [])
                    ]

                result_data_start: datetime | None = result.get("data_start")

                if new_samples:
                    self._update_qh(new_samples, result_data_start, now)

                # Store the full metrics dict so cache hits return it.
                # Always update on fetch — ensures cache hits serve fresh
                # predictions (NBC, device metrics, etc.) rather than stale
                # values from the initial fetch.
                if self._data is not None:
                    self._data = replace(
                        self._data, full_metrics_dict=result
                    )

                data = self._data
                if data and data.samples:
                    logger.debug(
                        "EnergyCache: len %d start %s now %s",
                        len(data.samples),
                        data.data_start,
                        now,
                    )

                return (result, True) if result is not None else (None, True)

            # fetch_func returned None — keep existing cache data.
            return (None, True)

    # ------------------------------------------------------------------
    # Quarter-hour extraction (caller holds lock when called from
    # get_current_qh, but we acquire it here too for standalone safety.)
    # ------------------------------------------------------------------

    def get_current_qh(self, now: datetime) -> dict[str, Any] | None:
        """Extract current incomplete QH prediction from QH-block storage.

        Reads samples from ``current_qh`` and computes NBC for just that
        quarter.  Frozen QHs are never recomputed — their results are
        cached at completion time.

        ``seconds_remaining`` is derived from wall-clock time so it stays
        monotonic across cache refreshes even when the sample count
        fluctuates.

        Args:
            now: Current time for QH boundary computation.

        Returns:
            Dict with QH prediction info or ``None`` if no cached data.
        """
        with self._lock:
            if self._data is None:
                return None

            current = self._data.current_qh
            if current is None or not current.samples:
                return None

            samples = list(current.samples)
            data_start = current.data_start

            # Use quantization-aware prediction window when available.
            prediction_window_seconds: int | None = None
            qs = self._data.quantization_seconds
            qc = self._data.quantization_confidence
            if qs is not None and qc is not None and qc >= 0.9:
                prediction_window_seconds = qs

        # Compute NBC only for the current (incomplete) QH.
        qh = compute_nbc_quarter(samples, prediction_window_seconds)
        if qh is None or qh.complete:
            return None

        seconds_remaining = qh_seconds_remaining(now)
        predicted_wh = qh.predicted_wh if qh.predicted_wh is not None else qh.wh
        return {
            "qh_name": "QH1",
            "predicted_wh": predicted_wh,
            "seconds_remaining": seconds_remaining,
            "data_start": data_start,
        }

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def invalidate(self) -> None:
        """Clear the cache."""
        with self._lock:
            self._data = None

    def sleep_interval_adjust(
        self, interval_seconds: float, now: datetime
    ) -> float:
        """Given a sleep interval, adjust it to the nearest quantization step.

        Args:
            interval_seconds: Seconds to sleep.
            now: Current datetime.

        Returns:
            Adjusted sleep seconds. These may be the same or shorter than
            input, but not longer.
        """
        if self._data is None:
            return interval_seconds
        if self._data.quantization_confidence is None or self._data.quantization_confidence < 0.9:
            return interval_seconds

        # Early-exit: data older than 2× quantum → sleep minimum.
        if (
            self._data.last_sample_at is not None
            and self._data.quantization_seconds is not None
        ):
            data_age = (now - self._data.last_sample_at).total_seconds()
            if data_age > 2 * self._data.quantization_seconds:
                return MIN_SLEEP_SECS

        # At this point quantization fields are guaranteed to be set.
        assert self._data.data_start is not None
        assert self._data.quantization_seconds is not None
        assert self._data.quantization_offset is not None

        # quantization offset is relative to data_start.
        offset_start = self._data.data_start + timedelta(seconds=self._data.quantization_offset)
        seconds_from_start = (now - offset_start).total_seconds()
        seconds_in_period = seconds_from_start % self._data.quantization_seconds
        seconds_remaining = (
            self._data.quantization_seconds - seconds_in_period
        ) % self._data.quantization_seconds
        logger.debug(
            "EnergyCache.sleep_interval_adjust: %.1f > %.1f",
            interval_seconds,
            seconds_remaining,
        )
        return max(5.0, min(interval_seconds, seconds_remaining))
