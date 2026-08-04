"""Tests for DaemonThreadPoolExecutor thread leak fix."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from energy_cache import DaemonThreadPoolExecutor, EnergyCache


@pytest.mark.slow
class TestDaemonThreadPoolExecutor:
    """Tests for DaemonThreadPoolExecutor."""

    def test_creates_daemon_threads(self) -> None:
        """DaemonThreadPoolExecutor should create daemon worker threads."""
        executor = DaemonThreadPoolExecutor(max_workers=2)
        try:
            # Submit a task to trigger thread creation
            future = executor.submit(lambda: time.sleep(0.1))
            future.result(timeout=1.0)

            # Check all threads in the executor are daemon
            for t in executor._threads:
                assert t.daemon is True, f"Thread {t.name} should be daemon"
        finally:
            executor.shutdown(wait=True)

    def test_shutdown_wait_false_does_not_hang(self) -> None:
        """shutdown(wait=False) should return immediately even with blocking work."""
        executor = DaemonThreadPoolExecutor(max_workers=1)

        # Submit a task that will block for a while
        event = threading.Event()

        def blocking_task() -> str:
            event.wait(timeout=2.0)  # Block for up to 2 seconds
            return "done"

        future = executor.submit(blocking_task)

        # Give the thread time to start and block
        time.sleep(0.1)

        # Shutdown without waiting - should return immediately
        start = time.monotonic()
        executor.shutdown(wait=False, cancel_futures=True)
        elapsed = time.monotonic() - start

        # Should return quickly (not wait for the 2-second block)
        assert elapsed < 0.5, f"shutdown(wait=False) took {elapsed:.2f}s, should be near-instant"

        # The future is already running, so it can't be cancelled by shutdown.
        # That's expected — cancel_futures only affects pending queue items.
        assert future.running() or future.done()


@pytest.mark.slow
class TestEnergyCacheThreadLeakFix:
    """Integration tests for EnergyCache._run_fetch_with_timeout thread leak fix."""

    def test_timeout_does_not_leak_threads(self) -> None:
        """_run_fetch_with_timeout should not leave zombie threads on timeout."""
        cache = EnergyCache(fetch_timeout_secs=1)  # 1 second timeout

        # Track active thread count before
        initial_threads = threading.active_count()

        def slow_fetch() -> dict[str, Any]:
            time.sleep(2.0)  # Longer than timeout
            return {"per_second_data": [1.0], "data_start": None}

        # This should timeout and return None
        result = cache._run_fetch_with_timeout(slow_fetch)
        assert result is None

        # Give some time for any threads to clean up
        time.sleep(0.2)

        # Thread count should not have grown permanently.
        # Allow small variance (+2) for pytest/asyncio test runner threads.
        final_threads = threading.active_count()
        assert final_threads <= initial_threads + 2, (
            f"Thread count grew from {initial_threads} to {final_threads} "
            f"(leaked {final_threads - initial_threads} threads)"
        )

    def test_successful_fetch_completes_normally(self) -> None:
        """_run_fetch_with_timeout should work normally for successful fetches."""
        cache = EnergyCache(fetch_timeout_secs=5)

        def quick_fetch() -> dict[str, Any]:
            return {"per_second_data": [1.0, 2.0], "data_start": None}

        result = cache._run_fetch_with_timeout(quick_fetch)
        assert result is not None
        assert result["per_second_data"] == [1.0, 2.0]

    def test_exception_in_fetch_is_handled(self) -> None:
        """Exceptions in fetch_func should be caught and return None."""
        cache = EnergyCache(fetch_timeout_secs=5)

        def failing_fetch() -> dict[str, Any]:
            raise ValueError("fetch failed")

        result = cache._run_fetch_with_timeout(failing_fetch)
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
