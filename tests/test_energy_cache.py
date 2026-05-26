import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from energy_cache import EnergyCache
from mockdata import MetricsMock
from test_app import mock_config
from util import ceil_to_qh, compute_nbc_quarters

class TestEnergyCache(unittest.TestCase):
    """Tests for EnergyCache — unified per-second sample cache with sliding-window semantics."""

    def test_import_exists(self):
        """EnergyCache class must be importable from metrics."""
        # This test fails until EnergyCache is implemented.
        from metrics import EnergyCache  # noqa: F401

    def test_initial_state_empty(self):
        """Fresh EnergyCache has no samples, no start time, no fetch timestamp."""
        from metrics import EnergyCache

        cache = EnergyCache()
        self.assertIsNone(cache._samples)
        self.assertIsNone(cache._data_start)
        self.assertIsNone(cache._last_fetch_at)

    def test_last_fetch_at_property(self):
        """last_fetch_at property exposes the internal _last_fetch_at field."""
        from metrics import EnergyCache
        from datetime import datetime, timezone

        cache = EnergyCache()
        self.assertIsNone(cache.last_fetch_at)

        now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)
        cache._last_fetch_at = now
        self.assertEqual(cache.last_fetch_at, now)

    def test_is_valid_false_when_empty(self):
        """is_valid returns False when cache has no data."""
        from metrics import EnergyCache

        cache = EnergyCache()
        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)
        self.assertFalse(cache.is_valid(fixed_now))

    def test_is_valid_true_after_fetch(self):
        """is_valid returns True after a successful fetch within TTL."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)

        def fetch_func():
            return {
                "per_second_data": [0.001] * 10,
                "data_start": datetime.now(timezone.utc),
            }

        cache.get_or_fetch(fetch_func, fixed_now)
        self.assertTrue(cache.is_valid(fixed_now))

    def test_is_valid_false_after_ttl_expiry(self):
        """is_valid returns False after TTL expires."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=0)  # TTL of 0 means always expired
        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)

        def fetch_func():
            return {
                "per_second_data": [0.001] * 10,
                "data_start": fixed_now
            }

        cache.get_or_fetch(fetch_func, fixed_now)
        self.assertFalse(cache.is_valid(fixed_now))

    def test_get_or_fetch_miss_on_first_call(self):
        """First call to get_or_fetch should invoke fetch_func and return was_fresh=True."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fetch_count = 0

        now = datetime.now(timezone.utc)

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.002] * 5,
                "data_start": now,
            }

        result, was_fresh = cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))

        self.assertEqual(fetch_count, 1)
        self.assertTrue(was_fresh)
        self.assertIsNotNone(result)

    def test_get_or_fetch_hit_within_ttl(self):
        """Second call within TTL should return cached data with was_fresh=False."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fetch_count = 0
        now = datetime.now(timezone.utc)

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.003] * 5,
                "data_start": now,
            }

        cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        result2, was_fresh = cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))

        self.assertEqual(fetch_count, 1)
        self.assertFalse(was_fresh)
        # Should return the same cached data object (identity check)

    def test_get_or_fetch_miss_after_ttl_expiry(self):
        """After TTL expires, get_or_fetch should call fetch_func again."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=0)  # Always expired
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.004] * fetch_count,  # Varying length
                "data_start": datetime.now(timezone.utc),
            }

        cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        self.assertEqual(fetch_count, 1)

        result2, was_fresh = cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        self.assertEqual(fetch_count, 2)
        self.assertTrue(was_fresh)

    def test_get_or_fetch_force_bypasses_cache(self):
        """force=True should always call fetch_func even if cache is valid."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fetch_count = 0
        fixed_now = datetime(2025, 6, 15, 15, 10, 0, tzinfo=timezone.utc)

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.005] * 3,
                "data_start": fixed_now,
            }

        cache.get_or_fetch(fetch_func, fixed_now)  # First call: fresh
        self.assertEqual(fetch_count, 1)

        _, was_fresh = cache.get_or_fetch(fetch_func, fixed_now, force=True)
        self.assertEqual(fetch_count, 2)
        self.assertTrue(was_fresh)

    def test_get_or_fetch_stores_last_fetch_at_only_on_api_call(self):
        """_last_fetch_at should only be set when data comes from the API, not on cache hit."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.006] * 5,
                "data_start": datetime.now(timezone.utc),
            }

        cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        first_fetch_at = cache._last_fetch_at

        # Second call should be a cache hit — _last_fetch_at unchanged
        time.sleep(0.01)  # Small delay to ensure different timestamp if updated
        cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))

        self.assertEqual(cache._last_fetch_at, first_fetch_at)
        self.assertEqual(fetch_count, 1)

    def test_get_or_fetch_none_result(self):
        """When fetch_func returns None, cache stores None and is_valid returns False."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)

        def fetch_func():
            return None  # Simulate API failure

        result, was_fresh = cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        self.assertIsNone(result)

    def test_invalidate_clears_cache(self):
        """After invalidate, next call fetches fresh data."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fetch_count = 0

        def fetch_func():
            nonlocal fetch_count
            fetch_count += 1
            return {
                "per_second_data": [0.007] * 5,
                "data_start": datetime.now(timezone.utc),
            }

        cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        self.assertEqual(fetch_count, 1)

        cache.invalidate()

        result2, was_fresh = cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))
        self.assertEqual(fetch_count, 2)
        self.assertTrue(was_fresh)

    def test_get_current_qh_returns_none_when_empty(self):
        """get_current_qh returns None when cache has no data."""
        from metrics import EnergyCache

        cache = EnergyCache()
        self.assertIsNone(cache.get_current_qh(datetime.now(timezone.utc)))

    def test_get_or_fetch_merges_samples(self):
        """New samples from fetch_func should be appended to existing _samples."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        base_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = base_time

            def fetch_func():
                # Simulate incremental data: first call returns 5 samples, second adds more
                if not cache._samples or len(cache._samples) == 0:
                    return {
                        "per_second_data": [0.1] * 5,
                        "data_start": base_time,
                    }
                else:
                    # Second fetch starts right after the first 5 samples end
                    return {
                        "per_second_data": [0.2] * 3,  # New samples appended
                        "data_start": base_time + timedelta(seconds=5),
                    }

            cache.get_or_fetch(fetch_func, base_time)  # First fetch: [0.1, 0.1, 0.1, 0.1, 0.1]
            self.assertEqual(len(cache._samples), 5)

            # Use force=True to simulate an incremental fetch that appends new samples.
            cache.get_or_fetch(fetch_func, base_time, force=True)  # Second fetch: append [0.2, 0.2, 0.2]
            self.assertEqual(len(cache._samples), 8)

    def test_get_or_fetch_skips_overlapping_samples(self):
        """Overlapping samples from full-hour fetches should be deduplicated."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2026, 5, 13, 0, 30, 29, tzinfo=timezone.utc)

        # First fetch: 1830 samples (full hour so far, lag=70)
        first_samples = [0.1] * 1830
        first_start = fixed_now - timedelta(seconds=1900)

        # Second fetch: 31 samples, 1 overlap, lag=40
        second_samples = [0.1] * 31
        second_start = fixed_now - timedelta(seconds=71)

        def fetch_func():
            # Simulates HourlyProjection always fetching from top of hour.
            # On the second call, _samples already exists, so return the
            # larger full-hour window (overlapping with what's cached).
            if not cache._samples or len(cache._samples) == 0:
                return {
                    "per_second_data": first_samples,
                    "data_start": first_start,
                }
            return {
                "per_second_data": second_samples,
                "data_start": second_start,
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now

            cache.get_or_fetch(fetch_func, fixed_now)
            self.assertEqual(len(cache._samples), 1830)

            # Second fetch should deduplicate: 1830 existing + 30 new = 1860
            cache.get_or_fetch(fetch_func, fixed_now, force=True)
            self.assertEqual(len(cache._samples), 1860)

    def test_get_current_qh_computes_from_samples(self):
        """get_current_qh should compute QH prediction from raw samples."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        # data_start at QH boundary 12:00 + 1200 = 12:20:00, now at 12:20:00
        data_start = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        fixed_now = datetime(2025, 6, 1, 12, 20, 0, tzinfo=timezone.utc)

        # 1200 samples = 20 min (QH1 complete 900 + first 5 min of QH2 300)
        samples = [0.001] * 1200

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": data_start,
            }

        cache.get_or_fetch(fetch_func, fixed_now)
        result = cache.get_current_qh(fixed_now)

        self.assertIsNotNone(result)
        # Should return a dict with QH prediction info
        self.assertIn("qh_name", result)

    def test_thread_safety_concurrent_access(self):
        """Concurrent reads and writes should not corrupt the sample list."""
        from metrics import EnergyCache

        fixed_now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        cache = EnergyCache(ttl_seconds=60)
        errors: list[str] = []

        def writer():
            try:
                for _ in range(10):
                    cache.get_or_fetch(lambda: {
                        "per_second_data": [0.1] * 5,
                        "data_start": fixed_now,
                    }, fixed_now)
            except Exception as ex:  # noqa: BLE001
                errors.append(str(ex))

        def reader():
            try:
                for _ in range(10):
                    cache.get_current_qh(fixed_now)
            except Exception as ex:  # noqa: BLE001
                errors.append(str(ex))

        import threading

        threads = [threading.Thread(target=writer) for _ in range(3)]
        threads += [threading.Thread(target=reader) for _ in range(3)]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"Thread errors occurred: {errors}")
        # Samples should be a list (not corrupted) and have some length
        self.assertIsInstance(cache._samples, list | type(None))

    def test_pruning_removes_samples_older_than_3600s(self):
        """Samples older than 3600 seconds from now are pruned after get_or_fetch."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)

        # Pre-populate with 3601 samples (lag=20)
        old_start = fixed_now - timedelta(seconds=3621)
        cache._samples = [0.1] * 3601
        cache._data_start = old_start

        # Patch datetime.now so pruning uses fixed_now
        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now

            # Fetch adds 10 samples
            def fetch_func():
                return {
                    "per_second_data": [0.2] * 10,
                    "data_start": fixed_now - timedelta(seconds=10),
                }

            cache.get_or_fetch(fetch_func, now=fixed_now, force=True)

        # After merge: 3600 samples. After pruning (keep last 3600s): ~3600
        self.assertLessEqual(len(cache._samples), 3600)

    def test_pruning_does_not_remove_recent_samples(self):
        """Samples within the last 3600s are preserved after pruning."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 1, 12, 30, 0, tzinfo=timezone.utc)

        # Pre-populate with exactly 3600 samples (1 hour of data)
        old_start = fixed_now - timedelta(seconds=3600)
        cache._samples = [0.1] * 3600
        cache._data_start = old_start

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now

            def fetch_func():
                return {
                    "per_second_data": [0.2] * 5,
                    "data_start": fixed_now - timedelta(seconds=5),
                }

            cache.get_or_fetch(fetch_func, now=fixed_now, force=True)

        self.assertEqual(len(cache._samples), 3600)

    def test_get_current_qh_returns_incomplete_qh(self):
        """get_current_qh returns the first incomplete quarter with extrapolated prediction."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 1, 12, 7, 30, tzinfo=timezone.utc)

        # 450 samples = halfway through QH1 (0-899 seconds)
        # Each sample is 0.5 Wh (stored as kWh in per_second_data, so 0.5 * 1000 = 500 Wh per second...
        # actually per_second_data is in kWh, so 0.5 kWh/sec = 500 Wh/sec)
        # Let's use small values: 0.001 kWh = 1 Wh per second
        samples = [0.001] * 450

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": fixed_now - timedelta(seconds=450),
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            cache.get_or_fetch(fetch_func, fixed_now)

        result = cache.get_current_qh(fixed_now)

        self.assertIsNotNone(result)
        self.assertEqual(result["qh_name"], "QH1")
        self.assertFalse(result.get("seconds_remaining", 0) == 0)

    def test_get_current_qh_returns_none_when_all_done(self):
        """When all 4 quarters are complete, get_current_qh returns None.

        Stale complete-quarter data must not be used for load management
        decisions. This is the anti-regression test for the ecoflow chatter.
        """
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
        data_start = ceil_to_qh(fixed_now - timedelta(seconds=3600))
        self.assertEqual(data_start, datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc))

        # 3600 samples = exactly one hour (all 4 quarters complete)
        samples = [0.01] * 3600

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": data_start,
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            cache.get_or_fetch(fetch_func, fixed_now)
            result = cache.get_current_qh(fixed_now)

        # All quarters complete → must return None
        self.assertIsNone(result)

    def test_get_current_qh_returns_none_when_qh1_is_complete(self):
        """When QH1 is complete with zero Wh (solar), return None instead of stale data.

        This is the anti-regression test for the ecoflow chatter bug.
        When all 4 quarters are complete and QH1 has 0 Wh (e.g., solar
        generation during night), get_current_qh must return None so that
        run_cycle correctly treats it as "no_incomplete_qh" and waits for
        fresh data rather than making load management decisions on stale
        quarter-end data.
        """
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
        data_start = ceil_to_qh(fixed_now - timedelta(seconds=3600))

        # 3600 samples of 0 = exactly one hour of zero energy (nighttime solar)
        samples = [0.0] * 3600

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": data_start,
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            cache.get_or_fetch(fetch_func, fixed_now)
            result = cache.get_current_qh(fixed_now)

        # Must return None — stale complete-quarter data must not be used
        self.assertIsNone(result)

    def test_get_current_qh_returns_none_when_all_quarters_complete_with_solar(self):
        """When all quarters are complete with actual solar Wh, return None.

        Even when QH1 has non-zero Wh from a completed quarter, returning
        that stale value would cause incorrect load decisions. The system
        should always wait for fresh incomplete data.
        """
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
        data_start = ceil_to_qh(fixed_now - timedelta(seconds=3600))

        # Solar generation: 0.5 Wh per second = 450 Wh per quarter
        samples = [0.0005] * 3600

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": data_start,
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            cache.get_or_fetch(fetch_func, fixed_now)
            result = cache.get_current_qh(fixed_now)

        # Must return None — complete quarters are stale
        self.assertIsNone(result)

    def test_get_current_qh_returns_most_recent_qh(self):
        """get_current_qh returns QH1 (most recent window), not the last incomplete one."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        fixed_now = datetime(2025, 6, 1, 12, 37, 30, tzinfo=timezone.utc)

        # 2250 samples = 12:00:00 to 12:37:29
        samples = [0.002] * 2250

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": fixed_now - timedelta(seconds=2250),
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            cache.get_or_fetch(fetch_func, fixed_now)

        result = cache.get_current_qh(fixed_now)

        self.assertIsNotNone(result)
        self.assertEqual(result["qh_name"], "QH1")

    def test_get_current_qh_seconds_remaining_from_wall_clock(self):
        """seconds_remaining must derive from wall-clock time, not sample count.

        When the cache has accumulated many samples (e.g. from incremental
        fetches), n = len(samples) can be much larger than 900.  The
        seconds_remaining value must come from wall-clock time to stay
        monotonic and correct across cache refreshes.
        """
        from metrics import EnergyCache

        # Start at :07:30 — in QH1 (0-899).  Seconds into hour = 7*60+30 = 450.
        # Expected: QH1, remaining = 900 - 450 = 450
        now = datetime(2025, 6, 1, 12, 7, 30, tzinfo=timezone.utc)
        cache = EnergyCache(ttl_seconds=60)

        # Populate with 450 samples covering just QH1 (data_start aligned to
        # QH boundary so the cache is valid).  The key assertion is that
        # seconds_remaining = 450 comes from wall-clock, not from sample count.
        samples = [0.01] * 450
        data_start = now - timedelta(seconds=450)  # 12:0:0 — QH-aligned

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": data_start,
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = now
            cache.get_or_fetch(fetch_func, now)

        result = cache.get_current_qh(now=now)

        self.assertIsNotNone(result)
        self.assertEqual(result["qh_name"], "QH1")
        self.assertEqual(result["seconds_remaining"], 450)

    def test_get_current_qh_seconds_remaining_decreases_monotonically(self):
        """seconds_remaining must decrease as wall-clock time advances."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)

        # data_start at QH boundary 12:00 + samples cover 10 min into QH1
        data_start = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        now1 = datetime(2025, 6, 1, 12, 10, 0, tzinfo=timezone.utc)
        # 600 seconds into QH1 → remaining = 900 - 600 = 300
        samples = [0.01] * 600

        def fetch_func():
            return {
                "per_second_data": samples,
                "data_start": data_start,
            }

        with patch("metrics.datetime") as mock_dt:
            mock_dt.now.return_value = now1
            cache.get_or_fetch(fetch_func, now1)
            result1 = cache.get_current_qh(now=now1)

        self.assertEqual(result1["seconds_remaining"], 300)

        # Advance 15 seconds → remaining should be 285
        now2 = now1 + timedelta(seconds=15)
        result2 = cache.get_current_qh(now=now2)
        self.assertEqual(result2["seconds_remaining"], 285)

        # Advance to cross quarter boundary → clock-boundary QH1 = 12:15-12:30
        # now1 + 301s = 12:15:01 → QH1 (most recent), remaining = 899
        now3 = now1 + timedelta(seconds=301)
        result3 = cache.get_current_qh(now=now3)
        self.assertEqual(result3["qh_name"], "QH1")
        self.assertEqual(result3["seconds_remaining"], 899)

    def test_get_or_fetch_populates_samples_from_nested_device_data(self):
        """get_or_fetch populates self._samples when per_second_data is nested in devices."""
        from metrics import EnergyCache

        cache = EnergyCache(ttl_seconds=60)
        now = datetime.now(timezone.utc)

        # Simulate a full metrics dict like HourlyProjection.metrics returns.
        # per_second_data is nested inside devices, not at the top level.
        def fetch_func():
            return {
                "api_response": {},
                "devices": [
                    {
                        "gid": 123,
                        "name": "VUE Device",
                        "per_second_data": [0.01] * 150,
                    }
                ],
            }

        cache.get_or_fetch(fetch_func, datetime.now(timezone.utc))

        # Verify self._samples is populated from nested devices data.
        self.assertIsNotNone(cache._samples)
        self.assertEqual(len(cache._samples), 150)
        self.assertEqual(all(v > 0 for v in cache._samples), True)


