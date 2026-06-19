"""
Data caching and management.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from clock import Clock, RealClock
from constants import MIN_SLEEP_SECS
from quantization import detect_quantization
from util import ceil_to_qh, compute_nbc_quarters, qh_seconds_remaining

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class EnergyCacheData:
    """Immutable snapshot of cached per-second energy data.

    This dataclass encapsulates all state that the ``EnergyCache`` wrapper
    tracks.  Being frozen and using ``slots`` make instances lightweight
    and safe to share between threads without deep-copying.

    Attributes:
        samples: Per-second energy values (Wh). Ordered chronologically.
        data_start: Timestamp of the first sample in *samples*.
        last_sample_at: Timestamp of the last sample in *samples*.
        last_fetch_at: When data was last fetched from the API.
        sample_count: Number of samples in *samples* (cached length).
        quantization_seconds: Detected quantization interval in seconds.
        quantization_offset: Offset within the quantization period (seconds).
        quantization_confidence: Confidence in the quantization detection (0–1).
        full_metrics_dict: Optional full metrics dict (e.g. with "devices" key)
            returned on cache hits, preserved from the original fetch.
    """

    samples: list[float] | None
    data_start: datetime | None
    last_sample_at: datetime | None
    last_fetch_at: datetime | None
    sample_count: int | None
    quantization_seconds: int | None
    quantization_offset: int | None
    quantization_confidence: float | None
    full_metrics_dict: dict[str, Any] | None = None


class OverlapMismatchError(Exception):
    """Raised when incremental fetch overlap samples don't match cache.

    Attributes:
        mismatch_count: Number of samples that differed.
        overlap_count: Total number of overlapping samples checked.
        first_idx: Index within the overlap window of the first mismatch.
        cached_val: Cached value at the first mismatch.
        new_val: New value at the first mismatch.
    """

    def __init__(
        self,
        *,
        mismatch_count: int,
        overlap_count: int,
        first_idx: int,
        cached_val: float,
        new_val: float,
    ) -> None:
        self.mismatch_count = mismatch_count
        self.overlap_count = overlap_count
        self.first_idx = first_idx
        self.cached_val = cached_val
        self.new_val = new_val
        super().__init__(
            f"{mismatch_count}/{overlap_count} overlap samples differ "
            f"(first at index {first_idx}: cached={cached_val}, new={new_val})"
        )


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
    # Backward-compatible aliases for old private attributes
    # ------------------------------------------------------------------

    @property
    def _samples(self) -> list[float] | None:
        """Alias for samples (backward compatibility)."""
        return self.samples

    @_samples.setter
    def _samples(self, value: list[float] | None) -> None:
        """Set samples via the old private-name path (backward compatibility)."""
        if self._data is None:
            self._data = EnergyCacheData(
                samples=value,
                data_start=None,
                last_sample_at=None,
                last_fetch_at=None,
                sample_count=None,
                quantization_seconds=None,
                quantization_offset=None,
                quantization_confidence=None,
            )
        else:
            self._data = replace(self._data, samples=value)

    @property
    def _data_start(self) -> datetime | None:
        """Alias for data_start (backward compatibility)."""
        return self.data_start

    @_data_start.setter
    def _data_start(self, value: datetime | None) -> None:
        """Set data_start via the old private-name path."""
        if self._data is None:
            self._data = EnergyCacheData(
                samples=self._data.samples if self._data else None,
                data_start=value,
                last_sample_at=None,
                last_fetch_at=None,
                sample_count=None,
                quantization_seconds=None,
                quantization_offset=None,
                quantization_confidence=None,
            )
        else:
            self._data = replace(self._data, data_start=value)

    @property
    def _last_sample_at(self) -> datetime | None:
        """Alias for last_sample_at (backward compatibility)."""
        return self.last_sample_at

    @_last_sample_at.setter
    def _last_sample_at(self, value: datetime | None) -> None:
        """Set last_sample_at via the old private-name path."""
        if self._data is None:
            self._data = EnergyCacheData(
                samples=self._data.samples if self._data else None,
                data_start=self._data.data_start if self._data else None,
                last_sample_at=value,
                last_fetch_at=None,
                sample_count=None,
                quantization_seconds=None,
                quantization_offset=None,
                quantization_confidence=None,
            )
        else:
            self._data = replace(self._data, last_sample_at=value)

    @property
    def _sample_count(self) -> int | None:
        """Alias for sample_count (backward compatibility)."""
        return self.sample_count

    @_sample_count.setter
    def _sample_count(self, value: int | None) -> None:
        """Set sample_count via the old private-name path."""
        if self._data is None:
            self._data = EnergyCacheData(
                samples=self._data.samples if self._data else None,
                data_start=self._data.data_start if self._data else None,
                last_sample_at=self._data.last_sample_at if self._data else None,
                last_fetch_at=None,
                sample_count=value,
                quantization_seconds=None,
                quantization_offset=None,
                quantization_confidence=None,
            )
        else:
            self._data = replace(self._data, sample_count=value)

    @property
    def _last_fetch_at(self) -> datetime | None:
        """Alias for last_fetch_at (backward compatibility)."""
        return self.last_fetch_at

    @_last_fetch_at.setter
    def _last_fetch_at(self, value: datetime | None) -> None:
        """Set last_fetch_at via the old private-name path."""
        if self._data is None:
            self._data = EnergyCacheData(
                samples=self._data.samples if self._data else None,
                data_start=self._data.data_start if self._data else None,
                last_sample_at=self._data.last_sample_at if self._data else None,
                last_fetch_at=value,
                sample_count=None,
                quantization_seconds=None,
                quantization_offset=None,
                quantization_confidence=None,
            )
        else:
            self._data = replace(self._data, last_fetch_at=value)

    @property
    def _quantization_seconds(self) -> int | None:
        """Alias for quantization_seconds (backward compatibility)."""
        return self.quantization_seconds

    @_quantization_seconds.setter
    def _quantization_seconds(self, value: int | None) -> None:
        """Set quantization_seconds via the old private-name path."""
        if self._data is None:
            self._data = EnergyCacheData(
                samples=self._data.samples if self._data else None,
                data_start=self._data.data_start if self._data else None,
                last_sample_at=self._data.last_sample_at if self._data else None,
                last_fetch_at=self._data.last_fetch_at if self._data else None,
                sample_count=None,
                quantization_seconds=value,
                quantization_offset=None,
                quantization_confidence=None,
            )
        else:
            self._data = replace(self._data, quantization_seconds=value)

    @property
    def _quantization_offset(self) -> int | None:
        """Alias for quantization_offset (backward compatibility)."""
        return self.quantization_offset

    @_quantization_offset.setter
    def _quantization_offset(self, value: int | None) -> None:
        """Set quantization_offset via the old private-name path."""
        if self._data is None:
            self._data = EnergyCacheData(
                samples=self._data.samples if self._data else None,
                data_start=self._data.data_start if self._data else None,
                last_sample_at=self._data.last_sample_at if self._data else None,
                last_fetch_at=self._data.last_fetch_at if self._data else None,
                sample_count=None,
                quantization_seconds=self._quantization_seconds,
                quantization_offset=value,
                quantization_confidence=None,
            )
        else:
            self._data = replace(self._data, quantization_offset=value)

    @property
    def _quantization_confidence(self) -> float | None:
        """Alias for quantization_confidence (backward compatibility)."""
        return self.quantization_confidence

    @_quantization_confidence.setter
    def _quantization_confidence(self, value: float | None) -> None:
        """Set quantization_confidence via the old private-name path."""
        if self._data is None:
            self._data = EnergyCacheData(
                samples=self._data.samples if self._data else None,
                data_start=self._data.data_start if self._data else None,
                last_sample_at=self._data.last_sample_at if self._data else None,
                last_fetch_at=self._data.last_fetch_at if self._data else None,
                sample_count=None,
                quantization_seconds=self._quantization_seconds,
                quantization_offset=self._quantization_offset,
                quantization_confidence=value,
            )
        else:
            self._data = replace(self._data, quantization_confidence=value)

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
    # Merge utility (kept as a module-level helper for callers)
    # ------------------------------------------------------------------

    @staticmethod
    def merge_incremental(
        existing: EnergyCacheData,
        new_samples: list[float] | None,
        result_data_start: datetime,
        now: datetime | None = None,
        *,
        preserve_quantization: bool = True,
    ) -> EnergyCacheData | None:
        """Merge new samples into existing cache samples based on time overlap.

        Computes the cache's effective time range from *existing.data_start*,
        detects overlap with *result_data_start*, and returns a new
        ``EnergyCacheData`` with the union of both sample lists in
        chronological order.

        Quantization fields from *existing* are preserved by default; set
        ``preserve_quantization=False`` to clear them. Quantization is not
        re-detected after merges — it reflects the most recent full-hour
        fetch. This matches the design of :meth:`_merge_samples` which only
        calls ``detect_quantization`` on the initial fetch.

        New samples arriving before the cache start will raise
        ``AssertionError``.

        Args:
            existing: Current ``EnergyCacheData`` snapshot.
            new_samples: New samples returned from the API (may be ``None``).
            result_data_start: Start time of the new samples from the API.
            now: Current time for last_fetch_at. Falls back to ``datetime.now()``
                when ``None`` (backward compatible).
            preserve_quantization: If ``True``, carry forward quantization fields
                from *existing*. Defaults to ``True``.

        Returns:
            A new ``EnergyCacheData`` with merged samples, or ``None`` if
            *new_samples* is ``None``.

        Raises:
            AssertionError: If *result_data_start* is before *existing.data_start*.
        """
        if new_samples is None or len(new_samples) == 0:
            return None

        if existing.data_start is None or existing.samples is None:
            return None

        cache_start_time = existing.data_start
        cache_end_time = existing.data_start + timedelta(
            seconds=len(existing.samples) - 1
        )

        # New samples should never arrive before the cache start.
        assert result_data_start >= cache_start_time

        # Verify overlap samples match cached data.
        if result_data_start <= cache_end_time:
            overlap_count = int(
                (cache_end_time - result_data_start).total_seconds()
            ) + 1
            overlap_count = min(overlap_count, len(new_samples), len(existing.samples))

            cache_overlap_start = len(existing.samples) - overlap_count
            mismatch_count = 0
            first_mismatch: tuple[int, float, float] | None = None
            for i in range(overlap_count):
                cached_val = existing.samples[cache_overlap_start + i]
                new_val = new_samples[i]
                if cached_val != new_val:
                    mismatch_count += 1
                    if first_mismatch is None:
                        first_mismatch = (i, cached_val, new_val)

            if mismatch_count > 0:
                assert first_mismatch is not None
                logger.warning(
                    "Overlap mismatch %d/%d samples (first at index %d: "
                    "cached=%s, new=%s) — using new data",
                    mismatch_count, overlap_count,
                    first_mismatch[0], first_mismatch[1], first_mismatch[2],
                )

        # New samples after the cache end.
        new_end_time = result_data_start + timedelta(seconds=len(new_samples) - 1)
        after_count = 0
        if new_end_time > cache_end_time:
            after_count = int((new_end_time - cache_end_time).total_seconds())
            after_count = min(after_count, len(new_samples))

        # Build merged samples in time order.
        after_samples = list(new_samples[len(new_samples) - after_count:])
        merged_samples = list(existing.samples) + after_samples

        if len(merged_samples) > 3600:
            merged_samples = merged_samples[-3600:]

        merged_last_sample_at = (
            existing.data_start + timedelta(seconds=len(merged_samples) - 1)
        ) if merged_samples else None

        return EnergyCacheData(
            samples=merged_samples,
            data_start=existing.data_start,
            last_sample_at=merged_last_sample_at,
            last_fetch_at=now if now is not None else datetime.now(tz=existing.data_start.tzinfo if existing.data_start.tzinfo else None),
            sample_count=len(merged_samples),
            quantization_seconds=(
                existing.quantization_seconds if preserve_quantization else None
            ),
            quantization_offset=(
                existing.quantization_offset if preserve_quantization else None
            ),
            quantization_confidence=(
                existing.quantization_confidence if preserve_quantization else None
            ),
        )

    # ------------------------------------------------------------------
    # Merge / prune helpers (called inside get_or_fetch under lock)
    # ------------------------------------------------------------------

    def _merge_samples(
        self,
        existing: EnergyCacheData,
        new_samples: list[float],
        result_data_start: datetime | None,
        now: datetime,
    ) -> EnergyCacheData:
        """Merge *new_samples* into *existing*, returning a new snapshot.

        If there are existing samples the method calls
        :meth:`merge_incremental` to produce a merged list, then prunes
        samples older than 3600 s from *now*.  On the initial fetch (no
        existing samples) the new samples are stored directly.

        Quantization is detected **only on the initial fetch** and
        preserved through subsequent incremental merges by
        :meth:`merge_incremental` with ``preserve_quantization=True``.
        Quantization is not re-detected after merges by design — it
        reflects the characteristics of the most recent full-hour fetch.

        Args:
            existing: The current ``EnergyCacheData`` snapshot.
            new_samples: Samples from the latest API response.
            result_data_start: Start time of the new samples.
            now: Current time used for pruning.

        Returns:
            A new ``EnergyCacheData`` with merged (and possibly pruned)
            samples.
        """
        if existing.samples is None or len(existing.samples) == 0:
            # Initial fetch — store directly.
            quant_tuple = detect_quantization(new_samples)
            logger.debug("EnergyCache quantization %s", quant_tuple)
            if quant_tuple is not None:
                qs, qo, qc = quant_tuple
                if qc < 0.9:
                    logger.warning(
                        "Quantization detected (N=%d, offset=%d) with low confidence %.3f",
                        qs, qo, qc,
                    )
            else:
                qs, qo, qc = None, None, None

            last_sample_at = (
                (result_data_start or now) + timedelta(seconds=len(new_samples) - 1)
            ) if new_samples else None

            return EnergyCacheData(
                samples=list(new_samples),
                data_start=result_data_start,
                last_sample_at=last_sample_at,
                last_fetch_at=now,
                sample_count=len(new_samples),
                quantization_seconds=qs,
                quantization_offset=qo,
                quantization_confidence=qc,
            )

        # Incremental merge path.
        logger.debug(
            "EnergyCache incremental_merge: %d old + %d new samples, "
            "quantization_preserved (qs=%s, qo=%s, qc=%.3f)",
            len(existing.samples),
            len(new_samples),
            existing.quantization_seconds,
            existing.quantization_offset,
            existing.quantization_confidence if existing.quantization_confidence else 0,
        )
        merged_data = self.merge_incremental(
            existing,
            new_samples,
            result_data_start or now,
            now=now,
        )

        if merged_data is None:
            return existing

        # Prune old samples.
        merged_data = self._prune_old_samples(merged_data, now)

        return EnergyCacheData(
            samples=merged_data.samples,
            data_start=merged_data.data_start,
            last_sample_at=merged_data.last_sample_at,
            last_fetch_at=now,
            sample_count=merged_data.sample_count,
            quantization_seconds=merged_data.quantization_seconds,
            quantization_offset=merged_data.quantization_offset,
            quantization_confidence=merged_data.quantization_confidence,
            full_metrics_dict=existing.full_metrics_dict,
        )

    def _prune_old_samples(
        self, data: EnergyCacheData, now: datetime
    ) -> EnergyCacheData:
        """Remove samples older than 3600 s from *now*.

        Args:
            data: Current ``EnergyCacheData`` snapshot.
            now: Current time for the pruning window.

        Returns:
            A new ``EnergyCacheData`` with old samples removed.
        """
        if data.samples is None or data.data_start is None:
            return data

        cutoff = ceil_to_qh(now - timedelta(seconds=3600))
        old_count = 0
        for i, _ in enumerate(data.samples):
            sample_time = data.data_start + timedelta(seconds=i)
            if sample_time < cutoff:
                old_count += 1
            else:
                break

        if old_count > 0:
            trimmed = data.samples[old_count:]
            new_data_start = data.data_start + timedelta(seconds=old_count)
            return EnergyCacheData(
                samples=trimmed,
                data_start=new_data_start,
                last_sample_at=data.last_sample_at,
                last_fetch_at=data.last_fetch_at,
                sample_count=len(trimmed),
                quantization_seconds=data.quantization_seconds,
                quantization_offset=data.quantization_offset,
                quantization_confidence=data.quantization_confidence,
            )

        return data

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
            try:
                result = fetch_func()
            except OverlapMismatchError as exc:
                logger.warning(
                    "Overlap mismatch in fetch_func (%s) — "
                    "clearing cache and retrying",
                    exc,
                )
                self._data = EnergyCacheData(
                    samples=None,
                    data_start=None,
                    last_sample_at=None,
                    last_fetch_at=None,
                    sample_count=None,
                    quantization_seconds=None,
                    quantization_offset=None,
                    quantization_confidence=None,
                )
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

                logger.debug(
                    "EnergyCache merge_input: extracted %d samples from "
                    "result_keys=%s, existing_samples=%d",
                    len(new_samples),
                    list(result.keys()),
                    len(self._data.samples) if self._data and self._data.samples else 0,
                )

                result_data_start: datetime | None = result.get("data_start")

                if self._data is not None and new_samples:
                    # Incremental merge with overlap verification.
                    try:
                        self._data = self._merge_samples(
                            self._data,
                            new_samples,
                            result_data_start,
                            now,
                        )
                    except OverlapMismatchError as exc:
                        logger.warning(
                            "Incremental overlap mismatch (%s) — "
                            "re-fetching full hour",
                            exc,
                        )
                        self._data = EnergyCacheData(
                            samples=None,
                            data_start=None,
                            last_sample_at=None,
                            last_fetch_at=None,
                            sample_count=None,
                            quantization_seconds=None,
                            quantization_offset=None,
                            quantization_confidence=None,
                        )
                        result = fetch_func()
                        if result is not None:
                            new_samples = []
                            if "per_second_data" in result:
                                new_samples = list(result["per_second_data"])
                            elif "devices" in result:
                                new_samples = [
                                    point
                                    for device in result["devices"]
                                    for point in device.get("per_second_data", [])
                                ]
                            result_data_start = result.get("data_start")
                            if new_samples:
                                self._data = self._merge_samples(
                                    EnergyCacheData(
                                        samples=[],
                                        data_start=None,
                                        last_sample_at=None,
                                        last_fetch_at=None,
                                        sample_count=None,
                                        quantization_seconds=None,
                                        quantization_offset=None,
                                        quantization_confidence=None,
                                    ),
                                    new_samples,
                                    result_data_start,
                                    now,
                                )
                elif new_samples:
                    # Initial fetch.
                    self._data = self._merge_samples(
                        EnergyCacheData(
                            samples=[],
                            data_start=None,
                            last_sample_at=None,
                            last_fetch_at=None,
                            sample_count=None,
                            quantization_seconds=None,
                            quantization_offset=None,
                            quantization_confidence=None,
                        ),
                        new_samples,
                        result_data_start,
                        now,
                    )
                elif self._data is not None:
                    # No new samples — prune old data in place.
                    self._data = self._prune_old_samples(self._data, now)

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
        """Extract current incomplete QH prediction from cached samples.

        Computes NBC quarters using clock-boundary alignment (QH1 = most
        recent 15-min window) and returns the same structure that
        ``NBCCache.get_or_fetch()`` would return:
        ``{qh_name, predicted_wh, seconds_remaining}``.

        ``seconds_remaining`` is derived from wall-clock time so it stays
        monotonic across cache refreshes even when the sample count
        fluctuates.

        Args:
            now: Current time for QH boundary computation.

        Returns:
            Dict with QH prediction info or ``None`` if no cached data.
        """
        with self._lock:
            if self._data is None or self._data.samples is None:
                return None

            samples = self._data.samples
            samples_len = len(samples)

        if samples_len == 0:
            return None

        # Required: data_start present and aligned to a QH boundary.
        assert self._data.data_start is not None
        assert self._data.data_start == ceil_to_qh(self._data.data_start)

        # Use quantization-aware prediction window when available.
        prediction_window_seconds: int | None = None
        qs = self._data.quantization_seconds
        qc = self._data.quantization_confidence
        if qs is not None and qc is not None and qc >= 0.9:
            prediction_window_seconds = qs

        nbc = compute_nbc_quarters(samples, prediction_window_seconds)

        # Map from attribute names to QH labels for fallback lookup.
        _qh_attrs = [("qh1", "QH1"), ("qh2", "QH2"), ("qh3", "QH3"), ("qh4", "QH4")]

        # Find QH1 (most recent window).
        qh1_data = nbc.qh1

        if qh1_data is None:
            # No data at all — return the first non-None QH as fallback.
            for attr, label in _qh_attrs[1:]:
                qh_data = getattr(nbc, attr)
                if qh_data is not None:
                    return {
                        "qh_name": label,
                        "predicted_wh": qh_data.predicted_wh or 0,
                        "seconds_remaining": qh_data.remaining_seconds or 0,
                        "data_start": self._data.data_start,
                    }
            return None

        # If QH1 is already complete, its data is stale — don't use it for
        # load management decisions. Return None so the caller knows to wait
        # for fresh incomplete data (this triggers the "no_incomplete_qh"
        # path in run_cycle with a short sleep hint instead of making
        # decisions on a completed quarter's Wh value).
        if qh1_data.complete:
            return None

        seconds_remaining = qh_seconds_remaining(now)
        predicted_wh = qh1_data.predicted_wh if qh1_data.predicted_wh is not None else qh1_data.wh
        return {
            "qh_name": "QH1",
            "predicted_wh": predicted_wh,
            "seconds_remaining": seconds_remaining,
            "data_start": self._data.data_start,
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
