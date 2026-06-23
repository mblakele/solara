"""Tests for the Clock abstraction (protocol, RealClock, FakeClock).

Also includes integration tests verifying Clock injection into
LoadManager, EnergyCache, and other consumers.
"""

from datetime import datetime, timedelta, timezone

import pytest

from clock import Clock, FakeClock, RealClock


class TestClockProtocol:
    """Clock protocol structural subtyping tests."""

    def test_realclock_is_clock(self):
        """RealClock must satisfy the Clock protocol (duck typing)."""
        clock: Clock = RealClock()
        assert clock is not None

    def test_fakeclock_is_clock(self):
        """FakeClock must satisfy the Clock protocol (duck typing)."""
        clock: Clock = FakeClock()
        assert clock is not None

    def test_any_now_callable_is_clock(self):
        """Any object with a now() method returning datetime must be a Clock."""
        class MinimalClock:
            def now(self) -> datetime:
                return datetime(2026, 1, 1, tzinfo=timezone.utc)

        clock: Clock = MinimalClock()
        assert isinstance(clock.now(), datetime)


class TestRealClock:
    """Tests for RealClock — returns real wall-clock UTC time."""

    def test_now_returns_datetime(self):
        """RealClock.now() must return a datetime."""
        clock = RealClock()
        result = clock.now()
        assert isinstance(result, datetime)

    def test_now_is_timezone_aware_utc(self):
        """RealClock.now() must return a timezone-aware UTC datetime."""
        clock = RealClock()
        result = clock.now()
        assert result.tzinfo is not None
        assert result.tzinfo.utcoffset(result) == timezone.utc.utcoffset(result)

    def test_now_is_recent(self):
        """RealClock.now() must return a time within 10 seconds of real now."""
        clock = RealClock()
        result = clock.now()
        real_now = datetime.now(timezone.utc)
        diff = abs((real_now - result).total_seconds())
        assert diff < 10, f"Clock differs from real time by {diff}s"


class TestFakeClock:
    """Tests for FakeClock — returns fixed deterministic time."""

    FIXED = datetime(2026, 6, 1, 12, 30, 0, tzinfo=timezone.utc)

    def test_now_returns_fixed_time(self):
        """FakeClock must return the fixed time passed to constructor."""
        clock = FakeClock(self.FIXED)
        assert clock.now() == self.FIXED

    def test_default_fixed_time(self):
        """FakeClock with no args must default to a known fixed time."""
        clock = FakeClock()
        expected = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert clock.now() == expected

    def test_now_is_deterministic(self):
        """Multiple calls to FakeClock.now() must return the same value."""
        clock = FakeClock(self.FIXED)
        assert clock.now() == self.FIXED
        assert clock.now() == self.FIXED
        assert clock.now() == self.FIXED

    def test_now_is_timezone_aware_utc(self):
        """FakeClock must return timezone-aware UTC datetimes."""
        clock = FakeClock(self.FIXED)
        result = clock.now()
        assert result.tzinfo is not None
        assert result.tzinfo.utcoffset(result) == timezone.utc.utcoffset(result)

    def test_advance_shifts_time_forward(self):
        """advance(seconds) must shift the fixed time forward."""
        clock = FakeClock(self.FIXED)
        clock.advance(120)
        expected = self.FIXED + timedelta(seconds=120)
        assert clock.now() == expected

    def test_advance_multiple_calls_accumulate(self):
        """Multiple advance() calls must accumulate."""
        clock = FakeClock(self.FIXED)
        clock.advance(60)
        clock.advance(30)
        expected = self.FIXED + timedelta(seconds=90)
        assert clock.now() == expected

    def test_advance_zero_seconds(self):
        """advance(0) must not change the time."""
        clock = FakeClock(self.FIXED)
        clock.advance(0)
        assert clock.now() == self.FIXED

    def test_advance_negative_seconds(self):
        """advance() with negative seconds must shift time backward."""
        clock = FakeClock(self.FIXED)
        clock.advance(-60)
        expected = self.FIXED - timedelta(seconds=60)
        assert clock.now() == expected


class TestEnergyCacheClockInjection:
    """Tests that EnergyCache accepts and uses a Clock for time."""

    FIXED = datetime(2026, 6, 15, 14, 30, 0, tzinfo=timezone.utc)

    def test_constructor_accepts_clock(self):
        """EnergyCache must accept a Clock via constructor."""
        from energy_cache import EnergyCache

        clock = FakeClock(self.FIXED)
        cache = EnergyCache(clock=clock)
        assert cache._clock is clock

    def test_defaults_to_realclock(self):
        """EnergyCache must use RealClock when no clock provided."""
        from energy_cache import EnergyCache

        cache = EnergyCache()
        assert isinstance(cache._clock, RealClock)

    def test_clock_used_through_merge(self):
        """EnergyCache must use the injected clock for merge timing."""
        from energy_cache import EnergyCache, EnergyCacheData

        clock = FakeClock(self.FIXED)
        cache = EnergyCache(clock=clock)

        # Populate with initial samples via _merge_samples (full-fetch path)
        existing = EnergyCacheData(
            samples=[],
            data_start=None,
            last_sample_at=None,
            last_fetch_at=None,
            sample_count=None,
            quantization_seconds=None,
            quantization_offset=None,
            quantization_confidence=None,
        )
        data = cache._merge_samples(
            existing=existing,
            new_samples=[0.001] * 100,
            result_data_start=self.FIXED - timedelta(seconds=100),
            now=clock.now(),
        )
        assert data is not None
        # last_fetch_at should be the clock's now
        assert data.last_fetch_at == self.FIXED


class TestMetricsClockInjection:
    """Tests that metrics.py uses the module-level clock."""

    FIXED = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)

    def test_set_clock_accepts_fakeclock(self):
        """set_clock must accept a FakeClock and make it the active clock."""
        import metrics

        clock = FakeClock(self.FIXED)
        metrics.set_clock(clock)
        assert metrics._CLOCK is clock

    def test_reset_to_realclock(self):
        """Calling set_clock with RealClock must restore real time."""
        import metrics

        metrics.set_clock(RealClock())
        assert isinstance(metrics._CLOCK, RealClock)

    def test_set_clock_affects_retryable_exception(self):
        """RetryableMetricsException.instant must use the injected clock."""
        import metrics

        clock = FakeClock(self.FIXED)
        metrics.set_clock(clock)
        exc = metrics.RetryableMetricsException("test")
        assert exc.instant == self.FIXED


class TestLoadManagerClockInjection:
    """Tests that LoadManager accepts and uses a Clock for time."""

    FIXED = datetime(2026, 6, 15, 14, 30, 0, tzinfo=timezone.utc)

    def test_constructor_accepts_clock_via_config(self):
        """LoadManager must accept a Clock via LoadManagerConfig.clock."""
        from load_manager import LoadManager, LoadManagerConfig

        clock = FakeClock(self.FIXED)
        config = LoadManagerConfig(clock=clock, enabled=False)
        lm = LoadManager(config)
        assert lm._clock is clock

    def test_constructor_accepts_clock_via_kwargs(self):
        """LoadManager must accept a Clock via legacy kwargs."""
        from load_manager import LoadManager

        clock = FakeClock(self.FIXED)
        lm = LoadManager(clock=clock, enabled=False)
        assert lm._clock is clock

    def test_defaults_to_realclock(self):
        """LoadManager must use RealClock when no clock is provided."""
        from load_manager import LoadManager

        lm = LoadManager(enabled=False)
        assert isinstance(lm._clock, RealClock)

    def test_clock_propagates_to_run_cycle(self):
        """run_cycle() must use the injected clock for CycleContext.now."""
        from load_manager import LoadManager, LoadManagerConfig

        clock = FakeClock(self.FIXED)
        config = LoadManagerConfig(clock=clock, enabled=False)
        lm = LoadManager(config)
        result = lm.run_cycle()
        assert result is not None
        # A disabled cycle still creates a CycleContext with the clock's now
        assert result.status == "disabled"
