"""
Data caching and management.
"""

from __future__ import annotations

import concurrent.futures
import concurrent.futures.thread
import logging
import threading
import time as _time_mod
import weakref
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from clock import Clock, RealClock
from constants import MIN_SLEEP_SECS, QUANTIZATION_CONFIDENCE_THRESHOLD
from quantization import detect_quantization, usable_window
from util import (
    CompletedNBCPeriod,
    QH_PERIOD_SECONDS,
    RetryableError,
    ceil_to_qh,
    floor_to_qh,
    compute_nbc_quarters,
    qh_seconds_remaining,
)

logger = logging.getLogger(__name__)


class DaemonThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """ThreadPoolExecutor that spawns daemon worker threads.

    Ensures worker threads don't block process exit when shutdown(wait=False)
    is called while a worker is blocked in a system call.

    Worker threads are deliberately NOT registered in
    ``concurrent.futures.thread._threads_queues``: at interpreter exit,
    ``_python_exit`` joins every registered thread (daemon or not), so a
    worker blocked in an in-flight fetch (bounded by the fetch timeout) would
    stall gunicorn worker shutdown for that whole duration. Unregistered
    daemon threads are simply abandoned when the process exits, which matches
    this class's contract of never blocking process exit.
    """

    def _adjust_thread_count(self) -> None:  # type: ignore[override]
        # Create daemon thread instead of default non-daemon.
        # Based on CPython's own _adjust_thread_count in
        # concurrent.futures.thread. The stdlib's private internals differ
        # across Python versions:
        #   * 3.12/3.13 store initializer/initargs as instance attributes
        #     and _worker takes (executor_reference, work_queue,
        #     initializer, initargs).
        #   * 3.14+ builds a per-executor worker context via
        #     prepare_context() and _worker takes (executor_reference,
        #     ctx, work_queue).
        # Both shapes are supported so the daemon-thread fix keeps working
        # on every supported Python.
        #
        # If idle threads are available, don't spin new threads. Mirrors the
        # stdlib implementation. The semaphore is intentionally NOT released
        # here: a spawned worker releases it when it goes idle, so a `with`
        # block would be wrong.
        if self._idle_semaphore.acquire(timeout=0):  # pylint: disable=consider-using-with
            return

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            # When the executor gets lost, the weakref callback will wake up
            # the worker threads.
            def weakref_cb(_, q=self._work_queue):
                q.put(None)

            create_worker_context = getattr(self, "_create_worker_context", None)
            if create_worker_context is not None:
                # Python 3.14+: worker receives a per-executor context.
                worker_args: tuple[Any, ...] = (
                    weakref.ref(self, weakref_cb),
                    create_worker_context(),
                    self._work_queue,
                )
            else:
                # Python 3.12/3.13: worker receives initializer/initargs.
                worker_args = (
                    weakref.ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                )
            t = threading.Thread(
                name=f"{self._thread_name_prefix}_{num_threads}",
                target=concurrent.futures.thread._worker,
                args=worker_args,
                daemon=True,  # Key: daemon=True
            )
            t.start()
            self._threads.add(t)  # type: ignore[attr-defined]
            # Deliberately NOT registering in
            # concurrent.futures.thread._threads_queues: registration would
            # make _python_exit join this thread at interpreter exit,
            # blocking process shutdown on any in-flight fetch (see class
            # docstring). Idle workers block on the work queue and die with
            # the daemon thread at exit; the weakref above still wakes them
            # when the executor is garbage-collected.


def _root_cause(exc: BaseException) -> BaseException:
    """Walk exception chain to find the most informative cause.

    Follows ``__cause__`` then ``__context__`` links, but stops before
    raw OS-level errors (``OSError`` subclasses like ``gaierror``) that
    lack contextual details such as hostnames.  Returns the outermost
    exception if no deeper cause exists.
    """
    seen: set[int] = set()
    current = exc
    while True:
        seen.add(id(current))
        cause = current.__cause__ or current.__context__
        if cause is None or id(cause) in seen:
            break
        if isinstance(cause, OSError) and not isinstance(cause, ConnectionError):
            break
        current = cause
    return current


class EnergyCacheAlignmentError(Exception):
    """Raised when cached per-second data is not aligned to a QH boundary.

    The Emporia API reports the actual start of its data via
    ``firstUsageInstant``, which can drift off a quarter-hour boundary when
    data is missing at the head of the requested window.  Fetch-site checks
    (``_fetch_channel_data`` in metrics.py) reject such responses before
    they are stored, and ``compact()`` can only realign windows with
    >=900 samples — so this guard is a safety net for any path that stores
    misaligned data.

    A plain ``Exception`` (not ``AssertionError``) is used so the failure
    survives ``python -O`` and degrades loudly via the load-management
    loop's generic-except branch (ERROR + invalidate + alert) instead of
    being stripped or swallowed silently.
    """


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
        data_lag_secs: Seconds of lag between the fetched data and wall clock,
            captured from the metrics dict (``_data_lag_secs``) on every fetch.
            Stale-data detection uses it to compute ``data_point_at``.
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
    data_lag_secs: float = 0.0
    full_metrics_dict: dict[str, Any] | None = None
    completed_periods: list["CompletedNBCPeriod"] | None = None



class EnergyCache:
    """Unified cache for per-second energy samples with sliding-window semantics.

    Stores raw Wh-per-second data points in a time-ordered list keyed by
    device name (currently only one device is used).  Fetches use
    QH-window semantics (see ``_chart_start_for`` in metrics.py): the first
    fetch covers a full hour; steady-state fetches start at the current QH
    boundary.  Every fetch replaces the stored samples (``_merge_samples_replace``),
    completed QH periods are compacted into ``CompletedNBCPeriod`` records
    after every fetch, and quantization is re-detected on each replace.

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

    def __init__(
        self,
        ttl_seconds: int = 30,
        clock: Clock | None = None,
        fetch_timeout_secs: int = 30,
    ) -> None:
        self._data: EnergyCacheData | None = None
        self._ttl_seconds: int = ttl_seconds
        self._clock: Clock = clock if clock is not None else RealClock()
        self._lock: threading.Lock = threading.Lock()
        # Single-flight gate: serializes fetches WITHOUT holding _lock,
        # so readers never block behind network I/O (plan 2.2).
        self._fetch_lock: threading.Lock = threading.Lock()
        self._fetch_timeout_secs: int = fetch_timeout_secs

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

    @samples.setter
    def samples(self, value: list[float] | None) -> None:
        """Set the per-second samples."""
        self._set_data_field(samples=value)

    @property
    def data_start(self) -> datetime | None:
        """Timestamp of the first sample, or ``None`` if empty."""
        if self._data is None:
            return None
        return self._data.data_start

    @data_start.setter
    def data_start(self, value: datetime | None) -> None:
        """Set the timestamp of the first sample."""
        self._set_data_field(data_start=value)

    @property
    def last_sample_at(self) -> datetime | None:
        """Timestamp of the most recent sample, or ``None`` if empty."""
        if self._data is None:
            return None
        return self._data.last_sample_at

    @last_sample_at.setter
    def last_sample_at(self, value: datetime | None) -> None:
        """Set the timestamp of the most recent sample."""
        self._set_data_field(last_sample_at=value)

    @property
    def last_fetch_at(self) -> datetime | None:
        """Timestamp of the last API fetch, or ``None`` if no fetch yet."""
        if self._data is None:
            return None
        return self._data.last_fetch_at

    @last_fetch_at.setter
    def last_fetch_at(self, value: datetime | None) -> None:
        """Set the timestamp of the last API fetch."""
        self._set_data_field(last_fetch_at=value)

    @property
    def sample_count(self) -> int | None:
        """Number of samples, or ``None`` if empty."""
        if self._data is None:
            return None
        return self._data.sample_count

    @sample_count.setter
    def sample_count(self, value: int | None) -> None:
        """Set the cached sample count."""
        self._set_data_field(sample_count=value)

    @property
    def quantization_seconds(self) -> int | None:
        """Detected quantization interval in seconds, or ``None``."""
        if self._data is None:
            return None
        return self._data.quantization_seconds

    @quantization_seconds.setter
    def quantization_seconds(self, value: int | None) -> None:
        """Set the detected quantization interval in seconds."""
        self._set_data_field(quantization_seconds=value)

    @property
    def quantization_offset(self) -> int | None:
        """Offset within the quantization period, or ``None``."""
        if self._data is None:
            return None
        return self._data.quantization_offset

    @quantization_offset.setter
    def quantization_offset(self, value: int | None) -> None:
        """Set the offset within the quantization period."""
        self._set_data_field(quantization_offset=value)

    @property
    def quantization_confidence(self) -> float | None:
        """Confidence in quantization detection (0–1), or ``None``."""
        if self._data is None:
            return None
        return self._data.quantization_confidence

    @quantization_confidence.setter
    def quantization_confidence(self, value: float | None) -> None:
        """Set the quantization detection confidence (0–1)."""
        self._set_data_field(quantization_confidence=value)

    # ------------------------------------------------------------------
    # Field setters & extra getters
    # ------------------------------------------------------------------

    def _set_data_field(self, **updates: Any) -> None:
        """Create a fresh backing snapshot, or replace fields on the current one.

        Args:
            **updates: EnergyCacheData fields to set.
        """
        if self._data is None:
            defaults: dict[str, Any] = {
                "samples": None,
                "data_start": None,
                "last_sample_at": None,
                "last_fetch_at": None,
                "sample_count": None,
                "quantization_seconds": None,
                "quantization_offset": None,
                "quantization_confidence": None,
            }
            defaults.update(updates)
            self._data = EnergyCacheData(**defaults)
        else:
            self._data = replace(self._data, **updates)

    @property
    def full_metrics_dict(self) -> dict[str, Any] | None:
        """Full metrics dict from the last fetch, or ``None`` if empty."""
        if self._data is None:
            return None
        return self._data.full_metrics_dict

    @property
    def completed_periods(self) -> list[CompletedNBCPeriod] | None:
        """Completed NBC periods preserved by compaction, or ``None``."""
        if self._data is None:
            return None
        return self._data.completed_periods

    @property
    def data_lag_secs(self) -> float:
        """Seconds of API data lag captured from the last fetch, or ``0.0``."""
        if self._data is None:
            return 0.0
        return self._data.data_lag_secs

    @data_lag_secs.setter
    def data_lag_secs(self, value: float) -> None:
        """Set the API data lag in seconds."""
        self._set_data_field(data_lag_secs=value)

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
    # Prune helper (called inside get_or_fetch under lock)
    # ------------------------------------------------------------------

    def _prune_old_samples(
        self, data: EnergyCacheData, now: datetime
    ) -> EnergyCacheData:
        """Remove samples older than 3600 s from *now*.

        Runs only on the no-new-samples fallback path in ``get_or_fetch``
        — with always-replace semantics the sliding window is otherwise
        maintained by compaction plus QH-anchored fetches.  Kept as a
        safety net.

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

        result = data

        if old_count > 0:
            trimmed = data.samples[old_count:]
            new_data_start = data.data_start + timedelta(seconds=old_count)
            new_last_sample_at = (
                new_data_start + timedelta(seconds=len(trimmed) - 1)
            ) if trimmed else new_data_start
            result = EnergyCacheData(
                samples=trimmed,
                data_start=new_data_start,
                last_sample_at=new_last_sample_at,
                last_fetch_at=data.last_fetch_at,
                sample_count=len(trimmed),
                quantization_seconds=data.quantization_seconds,
                quantization_offset=data.quantization_offset,
                quantization_confidence=data.quantization_confidence,
                completed_periods=data.completed_periods,
                full_metrics_dict=data.full_metrics_dict,
            )

        # Completed-period pruning is owned exclusively by compact(), which
        # runs after every fetch and applies the same 1-hour cutoff.
        return result

    def compact(self, now: datetime) -> None:  # pylint: disable=too-many-locals
        """Compact completed QH periods into CompletedNBCPeriod objects.

        Must be called under ``self._lock`` (caller holds it via
        ``get_or_fetch``).

        1. Identify completed QH periods from per-second data.
        2. Compute raw_wh for each completed period.
        3. Store as CompletedNBCPeriod objects (up to 3).
        4. Purge completed QH per-second data.
        5. Purge CompletedNBCPeriod objects older than 1 hour.

        Args:
            now: Current time for QH boundary computation.
        """
        # Caller must hold self._lock (get_or_fetch holds it).
        assert self._lock.locked(), "compact() must be called under self._lock"
        if self._data is None:
            return

        # Always prune old completed periods, even when len(samples) < 900.
        existing_completed = list(self._data.completed_periods or [])
        if existing_completed:
            cutoff = now - timedelta(seconds=3600)
            pruned = [p for p in existing_completed if p.start >= cutoff]
            if len(pruned) != len(existing_completed):
                self._data = replace(self._data, completed_periods=pruned or None)

        # Fast path: not enough data for a complete QH.
        if self._data.samples is None or len(self._data.samples) < 900:
            return

        samples = self._data.samples
        data_start = self._data.data_start
        assert data_start is not None, "data_start is None in compact"

        current_qh_start = ceil_to_qh(now)

        # Build list of completed periods from per-second data.
        existing_completed = list(self._data.completed_periods or [])
        new_completed: list[CompletedNBCPeriod] = []

        # If data_start is not QH-aligned (API drift), trim the leading
        # partial chunk so 900-sample chunks start on real QH boundaries.
        # This also keeps remaining_data_start aligned by construction —
        # the old ceil-based snap could jump it into the future.
        aligned_start = ceil_to_qh(data_start)
        offset = int((aligned_start - data_start).total_seconds())
        if offset >= len(samples):
            # Entire window is one partial chunk — nothing to compact.
            return

        while offset + 900 <= len(samples):
            qh_start_time = data_start + timedelta(seconds=offset)
            qh_end_time = qh_start_time + timedelta(seconds=899)
            if qh_end_time >= current_qh_start:
                # This is the current (possibly incomplete) QH — don't compact.
                break
            qh_values = samples[offset:offset + 900]
            raw_wh = 1000.0 * sum(qh_values)
            new_completed.append(CompletedNBCPeriod(
                start=qh_start_time,
                raw_wh=raw_wh,
            ))
            offset += 900

        if offset == 0:
            # data_start is QH-aligned and no complete periods found — nothing to do.
            return

        if not new_completed:
            # The leading partial chunk is trimmed but no completed period
            # was materialized: up to 899 seconds of data vanish.  The trim
            # itself is correct (the partial chunk predates the first QH
            # boundary and can never form a period; keeping it would leave
            # a misaligned data_start), but the loss should be loud, not
            # silent — warn instead of dropping data quietly.
            logger.warning(
                "compact: trimming %d leading samples of a partial chunk "
                "starting %s with no completed period materialized",
                offset,
                data_start,
            )

        # Merge new completed periods with existing, deduplicate by start time.
        all_completed = existing_completed + new_completed
        seen_starts: set[datetime] = set()
        deduped: list[CompletedNBCPeriod] = []
        for p in reversed(all_completed):
            if p.start not in seen_starts:
                seen_starts.add(p.start)
                deduped.append(p)
        deduped.reverse()

        # Purge completed periods older than 1 hour.
        cutoff = now - timedelta(seconds=3600)
        deduped = [p for p in deduped if p.start >= cutoff]

        # Keep at most 3.
        deduped = deduped[-3:]

        # Trim per-second samples: remove compacted chunks.
        remaining_samples = samples[offset:]
        # QH-aligned by construction: offset starts at the aligned boundary
        # (see lead-trim above) and advances in whole QH steps, so the next
        # fetch always starts from a clean quarter-hour edge.
        remaining_data_start = data_start + timedelta(seconds=offset)

        # Re-detect quantization on remaining samples.
        quant_tuple = detect_quantization(remaining_samples) if remaining_samples else None
        qs: int | None = None
        qo: int | None = None
        qc: float | None = None
        if quant_tuple is not None:
            qs, qo, qc = quant_tuple
        else:
            qs = self._data.quantization_seconds
            qo = self._data.quantization_offset
            qc = self._data.quantization_confidence

        self._data = replace(
            self._data,
            samples=remaining_samples,
            data_start=remaining_data_start,
            last_sample_at=(
                remaining_data_start + timedelta(seconds=len(remaining_samples) - 1)
            ) if remaining_samples else remaining_data_start,
            sample_count=len(remaining_samples),
            completed_periods=deduped,
            quantization_seconds=qs,
            quantization_offset=qo,
            quantization_confidence=qc,
        )

        period_details = " ".join(
            f"{p.start.isoformat()}={p.raw_wh:.2f}Wh" for p in deduped
        )
        logger.debug(
            "EnergyCache compact: %d samples → %d samples, "
            "%d completed periods [%s]",
            len(samples),
            len(remaining_samples),
            len(deduped),
            period_details,
        )

    def _merge_samples_replace(
        self,
        new_samples: list[float],
        data_start: datetime,
        now: datetime,
    ) -> EnergyCacheData:
        """Replace per-second data entirely (always-replace model).

        Discards existing samples and stores only the new ones; completed
        QH periods are preserved from ``_data``.  Callers are expected to
        reject misaligned API responses (see the drift check in
        ``_fetch_channel_data`` in metrics.py),
        so the stored ``data_start`` is QH-aligned by construction.

        Args:
            new_samples: New per-second samples.
            data_start: Start time of the new samples.
            now: Current time for ``last_fetch_at``.

        Returns:
            A new ``EnergyCacheData`` with replaced samples.
        """
        quant_tuple = detect_quantization(new_samples)
        if quant_tuple is not None:
            qs, qo, qc = quant_tuple
            if qc < QUANTIZATION_CONFIDENCE_THRESHOLD:
                logger.warning(
                    "Quantization detected (N=%d, offset=%d) with low confidence %.3f",
                    qs, qo, qc,
                )
        else:
            qs, qo, qc = None, None, None

        last_sample_at = (
            data_start + timedelta(seconds=len(new_samples) - 1)
        ) if new_samples else data_start

        return EnergyCacheData(
            samples=list(new_samples),
            data_start=data_start,
            last_sample_at=last_sample_at,
            last_fetch_at=now,
            sample_count=len(new_samples),
            quantization_seconds=qs,
            quantization_offset=qo,
            quantization_confidence=qc,
            completed_periods=self._data.completed_periods if self._data else None,
        )

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

    def _run_fetch_with_timeout(
        self,
        fetch_func: Callable[[], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        """Run *fetch_func* with a timeout, returning ``None`` on expiry.

        Runs the fetch in a worker thread so the caller is never blocked
        indefinitely even if the fetch hangs on network I/O.

        On timeout the pool is shut down without waiting for the stuck
        worker — the caller returns immediately.  The worker's exception
        (if any) is logged immediately inside the thread so the error
        details appear in logs even when the thread is still blocked in
        a system call (e.g. DNS resolution) at timeout time.

        Note: fixed thread leak — the executor now uses DaemonThreadPoolExecutor
        which spawns daemon workers, so a worker blocked in a system call
        does not survive ``shutdown(wait=False)``.
        """
        timed_out = threading.Event()

        def _wrapped() -> dict[str, Any] | None:
            try:
                return fetch_func()
            except BaseException as exc:
                if timed_out.is_set():
                    root = _root_cause(exc)
                    logger.warning(
                        "EnergyCache fetch raised after timeout: "
                        "%s: %s",
                        type(root).__name__,
                        root,
                    )
                raise

        pool = DaemonThreadPoolExecutor(max_workers=1)
        future = pool.submit(_wrapped)
        try:
            result = future.result(timeout=self._fetch_timeout_secs)
        except concurrent.futures.TimeoutError:
            timed_out.set()
            logger.warning(
                "EnergyCache fetch timed out after %ds",
                self._fetch_timeout_secs,
            )
            pool.shutdown(wait=False, cancel_futures=True)
            return None
        except Exception as exc:  # noqa: BLE001
            # RetryableError subclasses (e.g. RetryableMetricsException) are
            # known transient conditions (e.g. the Emporia API returned no
            # data for the hour) that get_or_fetch handles by serving stale
            # cache and retrying next cycle — log as a warning, not an error.
            # Unexpected exceptions keep the ERROR log.
            if isinstance(exc, RetryableError):
                logger.warning(
                    "EnergyCache fetch_func raised %s: %s (transient, will retry)",
                    type(exc).__name__,
                    exc,
                )
            else:
                logger.exception("EnergyCache fetch_func raised")
            pool.shutdown(wait=False, cancel_futures=True)
            return None
        pool.shutdown(wait=False)
        return result

    def get_or_fetch(
        self,
        fetch_func: Callable[[], dict[str, Any] | None],
        now: datetime,
        force: bool = False,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Return *(metrics_dict_or_none, was_fresh)*.

        If cache is valid and *force* is ``False``, return cached data with
        ``was_fresh=False``.  Otherwise calls *fetch_func* (which should do
        a QH-window API call — full hour on the first fetch, current-QH
        boundary afterwards), stores the result, and returns
        ``was_fresh=True``.

        The *fetch_func* may return either:

        * A full metrics dict (e.g. ``HourlyProjection.metrics``) — stored
          as a nested ``_data`` fallback and returned directly to the
          caller.
        * A per-second dict with ``"per_second_data"`` and ``"data_start"``
          keys — the per-second samples are stored via always-replace
          semantics (``_merge_samples_replace``); ``compact()`` runs after
          every fetch.

        Args:
            fetch_func: Callable that returns fresh data dict.
            now: Current datetime.
            force: When ``True``, bypass cache and always fetch.

        Returns:
            Tuple of *(metrics_dict_or_none, was_fresh)*.

        Locking contract (plan 2.2): the state lock ``_lock`` guards only
        snapshot reads and store operations — never network I/O. Fetches
        run under a separate single-flight gate (``_fetch_lock``) so
        concurrent callers produce at most one fetch per refresh while
        readers proceed uninterrupted.
        """
        with self._lock:
            # Fast path: serve a valid snapshot without any I/O.
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

        with self._fetch_lock:
            # Double-check: another fetcher may have refreshed the cache
            # while we waited on the single-flight gate.
            with self._lock:
                if not force and self._is_valid_unlocked(now):
                    result = self._build_result()
                    logger.debug("EnergyCache cache_hit after fetch gate")
                    return result, False

            # Fetch fresh data with timeout protection — outside _lock.
            fetch_start = _time_mod.monotonic()
            result = self._run_fetch_with_timeout(fetch_func)
            fetch_elapsed = _time_mod.monotonic() - fetch_start
            logger.debug(
                "EnergyCache fetch_func completed in %.2fs, result=%s",
                fetch_elapsed,
                "ok" if result is not None else "None",
            )

            with self._lock:
                if result is None:
                    # Timed out or raised — serve stale cache if available,
                    # so callers get stale-but-valid data instead of
                    # crashing on None.
                    if self._data is not None:
                        return self._build_result(), False
                    logger.warning(
                        "EnergyCache: fetch failed and no stale cache available"
                    )
                    return (None, True)

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

                effective_data_start = result_data_start if result_data_start is not None else now
                if new_samples:
                    logger.debug(
                        "EnergyCache replace: %d old → %d new samples, "
                        "data_start=%s",
                        len(self._data.samples) if self._data and self._data.samples else 0,
                        len(new_samples),
                        result_data_start,
                    )
                    self._data = self._merge_samples_replace(
                        new_samples, effective_data_start, now,
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
                        self._data,
                        full_metrics_dict=result,
                        data_lag_secs=float(result.get("_data_lag_secs", 0.0)),
                    )

                # Always compact after fetch — O(1) no-op when
                # len(samples) < 900.
                self.compact(now)

                data = self._data
                if data and data.samples:
                    logger.debug(
                        "EnergyCache: len %d start %s now %s",
                        len(data.samples),
                        data.data_start,
                        now,
                    )

                return (result, True) if result is not None else (None, True)

    # ------------------------------------------------------------------
    # Quarter-hour extraction (caller holds lock when called from
    # get_current_qh, but we acquire it here too for standalone safety.)
    # ------------------------------------------------------------------

    def get_current_qh(self, now: datetime) -> dict[str, Any] | None:
        """Extract current incomplete QH prediction from cached samples.

        Computes NBC quarters using clock-boundary alignment (QH1 = most
        recent 15-min window) and returns a dict with keys
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
            data_start = self._data.data_start
            quantization_seconds = self._data.quantization_seconds
            quantization_confidence = self._data.quantization_confidence

        if samples_len == 0:
            return None

        # Required: data_start present and aligned to a QH boundary.  A
        # misaligned value means a fetch stored raw API data that drifted
        # off the quarter-hour (see EnergyCacheAlignmentError); compact()
        # can only realign windows with >=900 samples, so raise explicitly
        # (an assert would be stripped under `python -O`) to force a cache
        # refresh on the caller side.
        if data_start is None or data_start != ceil_to_qh(data_start):
            logger.warning(
                "EnergyCache get_current_qh: data_start %s not QH-aligned; "
                "raising to force cache refresh",
                data_start,
            )
            raise EnergyCacheAlignmentError(
                f"data_start {data_start} not aligned to QH boundary"
            )

        # Stale-QH guard: if the trailing (incomplete) chunk's wall window
        # has already ended, the cache predates a quarter boundary — e.g. an
        # API outage across :00/:15/:30/:45, exactly when stale serving
        # matters. Extrapolating it with the wall-clock remainder would
        # credit a full quarter of *future* energy to a window that is
        # already over; report no incomplete QH instead (same contract as
        # the qh1-complete branch below).
        chunk_start = data_start + timedelta(
            seconds=(samples_len // QH_PERIOD_SECONDS) * QH_PERIOD_SECONDS
        )
        if floor_to_qh(now) >= chunk_start + timedelta(seconds=QH_PERIOD_SECONDS):
            logger.info(
                "EnergyCache get_current_qh: cached window starting %s ended "
                "before %s; reporting no incomplete QH",
                chunk_start,
                now,
            )
            return None

        # Use quantization-aware prediction window when available. The
        # shared guard rejects the flat-data N=2 artifact (plan 3.2).
        prediction_window_seconds: int | None = usable_window(
            quantization_seconds,
            quantization_confidence,
        )

        # One time base (plan 3.3): extrapolate with wall-clock remaining
        # so prediction and downstream decision math agree even when data
        # lag makes sample count diverge from elapsed time.
        wall_remaining = qh_seconds_remaining(now)
        nbc = compute_nbc_quarters(
            samples,
            prediction_window_seconds,
            seconds_remaining_override=wall_remaining,
        )

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
                        "data_start": data_start,
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
            "data_start": data_start,
            "samples_used": qh1_data.samples_used,
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
        data = self._data
        # Single source of truth (plan 3.2): the shared guard rejects
        # sub-MIN-window artifacts (flat-data N=2 at confidence 1.0) that
        # the raw inline check used to let through.
        quantum = usable_window(data.quantization_seconds, data.quantization_confidence)
        if quantum is None:
            return interval_seconds

        # Early-exit: data older than 2× quantum → sleep minimum.
        if data.last_sample_at is not None:
            data_age = (now - data.last_sample_at).total_seconds()
            if data_age > 2 * quantum:
                return MIN_SLEEP_SECS

        # At this point quantization fields are guaranteed to be set.
        assert data.data_start is not None
        assert data.quantization_offset is not None

        # quantization offset is relative to data_start.
        offset_start = data.data_start + timedelta(seconds=data.quantization_offset)
        seconds_from_start = (now - offset_start).total_seconds()
        seconds_in_period = seconds_from_start % quantum
        seconds_remaining = (quantum - seconds_in_period) % quantum
        logger.debug(
            "EnergyCache.sleep_interval_adjust: %.1f > %.1f",
            interval_seconds,
            seconds_remaining,
        )
        return max(5.0, min(interval_seconds, seconds_remaining))
