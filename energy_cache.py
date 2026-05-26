"""
Data caching and management.
"""

import threading

from datetime import datetime, timedelta, timezone
import logging

from quantization import detect_quantization
from util import ceil_to_qh, compute_nbc_quarters, qh_seconds_remaining

logger = logging.getLogger(__name__)

class EnergyCache:
    """Unified cache for per-second energy samples with sliding-window semantics.

    Stores raw kWh/second data points in a time-ordered list keyed by device name
    (currently only one device is used). Supports incremental partial-range fetches:
    when new data arrives, old points older than 3600s are pruned and only the
    delta is fetched from pyemvue.

    Thread-safe via internal lock for concurrent access between Flask and
    LoadManager background threads.

    Attributes:
        _samples: Per-second data points, one float per second (kWh). Ordered by time.
        _data_start: Timestamp of the first sample in _samples.
        _last_fetch_at: When data was last fetched from API (for TTL/stale checks).
        _ttl_seconds: Maximum age of cached data before forcing a refresh.
        _sample_count: Number of samples in _samples (cached for incremental fetch).
        _last_sample_at: Timestamp of the last sample in _samples.
    """

    def __init__(self, ttl_seconds: int = 30) -> None:
        self._samples: list[float] | None = None
        self._data_start: datetime | None = None  # start time of first sample
        self._last_fetch_at: datetime | None = None
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        # Sample metadata for incremental fetch tracking.
        self._sample_count: int | None = None  # number of samples in _samples
        self._last_sample_at: datetime | None = None  # time of last sample in _samples
        # Full result dict from the most recent fetch (for non-incremental callers).
        self._data: dict[str, Any] | None = None
        self._quantization_seconds: int = None
        self._quantization_offset: int = None
        self._quantization_confidence: float = None

    @property
    def last_sample_at(self) -> datetime | None:
        """Timestamp of the most recent sample in the cache, or None if empty.

        Returns:
            datetime of the last sample (data_start + len(samples) - 1), or None.
        """
        return self._last_sample_at

    @property
    def samples(self) -> list[float] | None:
        """The list of per-second Wh samples, or None if empty.

        Returns:
            List of float Wh values, or None.
        """
        return self._samples

    @property
    def data_start(self) -> datetime | None:
        """Timestamp of the first sample in the cache, or None if empty.

        Returns:
            datetime of the first sample, or None.
        """
        return self._data_start

    @property
    def last_fetch_at(self) -> datetime | None:
        """Timestamp of the last API fetch, or None if no fetch has occurred.

        Returns:
            datetime of the last fetch, or None.
        """
        return self._last_fetch_at

    def is_valid(self, now: datetime) -> bool:
        """Check if cache has non-expired data.

        Args:
            now: Current time for TTL check. Required.

        Returns:
            True if cache has data and it hasn't expired.
        """
        with self._lock:
            if self._samples is None or len(self._samples) == 0:
                return False
            if self._last_fetch_at is None:
                return False
            elapsed = now - self._last_fetch_at
            return elapsed.total_seconds() < self._ttl_seconds

    @staticmethod
    def merge_incremental(data_start: datetime,
                          result_data_start: datetime,
                          old_samples: list[float],
                          new_samples: list[float]
                          ) -> list[float] | None:
        """Merge new samples into existing cache samples based on time overlap.

        Computes the cache's effective time range from `data_start`, detects
        overlap with `result_data_start`, and returns the union of both
        sample lists in chronological order.

        New samples arriving before the cache start will raise AssertionError.

        Args:
            data_start: Start time of the existing cache samples.
            result_data_start: Start time of the new samples from the API.
            old_samples: Existing samples stored in the cache.
            new_samples: New samples returned from the API.

        Returns:
            Merged sample list in chronological order, or None if merge fails.

        Raises:
            AssertionError: If result_data_start is before data_start.
        """
        # Compute the cache's effective time range from its
        # own _data_start so overlap detection is anchored
        # to real sample positions.
        cache_start_time = data_start
        cache_end_time = data_start + timedelta(seconds=len(old_samples) - 1)

        # Number of new samples strictly before / after the cache.
        # Initialized here so they're always defined for the
        # data_start update below.
        after_count = 0

        # Standard overlap detection:
        # New samples should never arrive before the cache start.
        assert(result_data_start >= cache_start_time)

        # New samples after the cache end.
        # Where the new samples end (from API's data_start).
        new_end_time = result_data_start + timedelta(seconds=len(new_samples) - 1)
        if new_end_time > cache_end_time:
            after_count = int((new_end_time - cache_end_time).total_seconds())
            # Don't double-count: after_count can't exceed remaining samples.
            # TODO assert that these counts match exactly?
            after_count = min(after_count, len(new_samples))
            # TODO assert that any overlap has identical values?

        # Build merged samples in time order:
        #   before_samples + existing + after_samples
        after_samples = list(new_samples[len(new_samples) - after_count:])

        merged_samples = list(old_samples) + after_samples

        if len(merged_samples) > 3600:
            merged_samples = merged_samples[-3600:]

        return merged_samples


    def get_or_fetch(
        self, fetch_func: Callable[[], dict[str, Any] | None], now: datetime, force: bool = False
    ) -> tuple[dict[str, Any] | None, bool]:
        """Return (metrics_dict_or_none, was_fresh).

        If cache is valid and force=False: return cached data with was_fresh=False.
        Otherwise calls fetch_func() (which should do an incremental or full API call),
        stores the result, and returns was_fresh=True.

        The fetch_func may return either:
          - A full metrics dict (e.g. HourlyProjection.metrics) — stored as-is in
            ``_data`` and returned directly to the caller.
          - An incremental dict with "per_second_data" and "data_start" keys — the
            per-second samples are merged into ``_samples`` for use by callers that
            need raw data (e.g. NBCReader).

        Args:
            fetch_func: Callable that returns fresh data dict.
            now: current datetime.
            force: When True, bypass cache and always fetch.

        Returns:
            Tuple of (metrics_dict_or_none, was_fresh).
        """
        with self._lock:
            # Check if cache is valid (non-expired data exists).
            if not force and self._is_valid_unlocked(now):
                return self._build_result(), False

            # Fetch fresh data.
            result = fetch_func()

            if result is not None:
                # Store the full result dict for non-incremental callers.
                self._data = result

                new_samples = result.get("per_second_data", [])

                # When data comes from nested devices (full metrics dict path,
                # e.g. HourlyProjection.metrics), extract per_second_data from
                # devices so the merge logic below populates self._samples.
                if not new_samples and "devices" in result:
                    new_samples = [
                        point
                        for device in result["devices"]
                        for point in device.get("per_second_data", [])
                    ]

                # Merge with existing samples (incremental fetch path).
                if self._samples is not None and len(self._samples) > 0:
                    result_data_start = result.get("data_start")
                    if (self._last_sample_at is not None
                        and self._data_start is not None
                        and result_data_start is not None):
                        old_count = len(self._samples)
                        merged_samples = self.merge_incremental(
                            self._data_start,
                            result_data_start,
                            self._samples,
                            new_samples)
                        assert merged_samples is not None
                        self._samples = merged_samples
                        # Truncation in merge_incremental drops leading samples.
                        # Update _data_start to reflect the new start time so
                        # the pruning loop below computes sample timestamps
                        # correctly.
                        dropped = old_count - len(merged_samples)
                        if dropped > 0:
                            self._data_start = (
                                self._data_start + timedelta(seconds=dropped)
                            )
                elif new_samples:
                    self._samples = list(new_samples)

                # Update data_start and metadata if this is an initial fetch.
                if self._samples and "data_start" in result:
                    if self._data_start is None:
                        self._data_start = result["data_start"]
                        quant_tuple = detect_quantization(self._samples)
                        logger.debug("EnergyCache quantization %s", quant_tuple)
                        if quant_tuple is not None:
                            self._quantization_seconds = quant_tuple[0]
                            self._quantization_offset = quant_tuple[1]
                            self._quantization_confidence = quant_tuple[2]

                if self._samples:
                    cutoff = ceil_to_qh(now - timedelta(seconds=3600))
                    # Determine the time of each sample to know which are old.
                    if self._data_start is not None:
                        # Compute how many samples are before the cutoff.
                        old_count = 0
                        for i, _ in enumerate(self._samples):
                            sample_time = self._data_start + timedelta(seconds=i)
                            if sample_time < cutoff:
                                old_count += 1
                            else:
                                break
                        if old_count > 0:
                            self._samples = self._samples[old_count:]
                            if self._data_start is not None:
                                self._data_start = (
                                    self._data_start + timedelta(seconds=old_count)
                                )

                # Update sample metadata.
                if self._samples:
                    self._sample_count = len(self._samples)
                    if self._data_start is not None:
                        self._last_sample_at = (
                            self._data_start + timedelta(seconds=len(self._samples) - 1)
                        )

                psd = result.get("per_second_data")
                if not psd:
                    # Fall back to counting per_second_data from devices
                    # (full metrics dict path, e.g. HourlyProjection.metrics).
                    devices = result.get("devices", [])
                    if devices:
                        psd = [
                            point
                            for device in devices
                            for point in device.get("per_second_data", [])
                        ]

                if psd:
                    logger.debug(
                        "EnergyCache fetched %d data points (%s: %d samples) at %s",
                        len(psd),
                        self._data_start,
                        len(self._samples or []),
                        now,
                    )

                self._last_fetch_at = now
            else:
                result = None  # Ensure we store None on failure.

            return (result, True) if result is not None else (None, True)

    def _is_valid_unlocked(self, now: datetime) -> bool:
        """Check if cache has non-expired data (caller must hold lock).

        Args:
            now: Current time for TTL check. Required.

        Returns:
            True if cache has data and it hasn't expired.
        """
        # Cache is valid if we have either raw samples or a full result dict.
        has_data = (
            self._samples is not None and len(self._samples) > 0
        ) or (self._data is not None)
        if not has_data:
            return False
        if self._last_fetch_at is None:
            return False
        elapsed = now - self._last_fetch_at
        return elapsed.total_seconds() < self._ttl_seconds

    def _build_result(self) -> dict[str, Any] | None:
        """Build the result dict from cached data (caller must hold lock).

        Returns the full result dict stored by the most recent fetch.
        Falls back to building a dict from raw samples when no full result is cached.

        Returns:
            Cached metrics dict, or a dict with per_second_data and data_start
            built from raw samples, or None if no cached data exists.
        """
        # Return the full result dict if available (non-incremental path).
        if self._data is not None:
            return self._data

        # Fall back to building from raw samples (incremental path).
        if self._samples is None:
            return None

        result: dict[str, Any] = {
            "per_second_data": self._samples,
        }

        if self._data_start is not None:
            result["data_start"] = self._data_start

        return result

    def get_current_qh(self, now: datetime) -> dict[str, Any] | None:
        """Extract current incomplete QH prediction from cached samples.

        Computes NBC quarters using clock-boundary alignment (QH1 = most recent
        15-min window) and returns the same structure that
        NBCCache.get_or_fetch() would return: {qh_name, predicted_wh,
        seconds_remaining}.

        ``seconds_remaining`` is derived from wall-clock time so it stays
        monotonic across cache refreshes even when the sample count fluctuates.

        Args:
            now: Current time for QH boundary computation. Required.

        Returns:
            Dict with QH prediction info or None if no cached data.
        """
        with self._lock:
            samples = self._samples

        if samples is None:
            return None

        samples_len = len(samples)
        if samples_len == 0:
            return None

        # Required: data_start present and aligned to a QH boundary
        assert self._data_start is not None
        assert self._data_start == ceil_to_qh(self._data_start)

        #self.logger.debug("get_current_qh len %d", len(samples))
        nbc = compute_nbc_quarters(samples)

        # Find QH1 (most recent window).
        qh1_data = nbc.get("QH1")

        if qh1_data is None:
            # No data at all — return the first non-None QH as fallback.
            for qh in ("QH2", "QH3", "QH4"):
                if nbc.get(qh) is not None:
                    qh_data = nbc[qh]
                    return {
                        "qh_name": qh,
                        "predicted_wh": qh_data.get("predicted_wh", 0),
                        "seconds_remaining": qh_data.get("remaining_seconds", 0),
                        "data_start": self._data_start,
                    }
            return None

        # If QH1 is already complete, its data is stale — don't use it for
        # load management decisions. Return None so the caller knows to wait
        # for fresh incomplete data (this triggers the "no_incomplete_qh" path
        # in run_cycle with a short sleep hint instead of making decisions on
        # a completed quarter's Wh value).
        if qh1_data.get("complete"):
            return None

        seconds_remaining = qh_seconds_remaining(now)
        predicted_wh = qh1_data.get("predicted_wh", qh1_data.get("wh", 0))
        return {
            "qh_name": "QH1",
            "predicted_wh": predicted_wh,
            "seconds_remaining": seconds_remaining,
            "data_start": self._data_start,
        }


    def invalidate(self) -> None:
        """Clear the cache."""
        with self._lock:
            self._samples = None
            self._data_start = None
            self._last_fetch_at = None
            self._sample_count = None
            self._last_sample_at = None
            self._data = None


    def sleep_interval_adjust(self, interval_seconds: float, now: datetime) -> float:
        """Given a sleep interval, adjust it to the nearest quantization step.

        Args:
            interval_seconds: seconds to sleep.
            now: Current datetime. Timezone not needed nor used.

        Returns:
            Adjusted sleep seconds. These may be the same or shorter than input, but not longer.
        """
        if self._quantization_confidence is None or self._quantization_confidence < 0.9:
            return interval_seconds

        # quantization offset is relative to data_start
        offset_start = self.data_start + timedelta(seconds=self._quantization_offset)
        seconds_from_start = (now - offset_start).total_seconds()
        seconds_in_period = seconds_from_start % self._quantization_seconds
        # Seems to take pyemvue API ca 20-sec to settle.
        # 0-15.67 is unreliable: 16-sec may be optimal.
        seconds_remaining = (self._quantization_seconds - seconds_in_period + 15.75) % self._quantization_seconds
        logger.debug("EnergyCache.sleep_interval_adjust: %.1f > %.1f", interval_seconds, seconds_remaining)
        return max(5.0, min(interval_seconds, seconds_remaining))


