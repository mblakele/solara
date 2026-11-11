"""Tests for util.py functions.

Covers:
  - _haversine_distance (GPS distance calculation)
  - compute_nbc_quarter prediction window behavior
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from util import (
    _haversine_distance,
    classify_trailing_chunk,
    complete_quarter,
    compute_nbc_quarter,
    is_stale_trailing_qh,
    qh_starts_for_window,
    TrailingChunkKind,
)


class TestHaversineDistance:

    def test_same_point_returns_zero(self):
        """Identical coordinates should yield 0 m distance."""
        assert _haversine_distance(37.0, -122.0, 37.0, -122.0) == pytest.approx(0, abs=0.01)

    def test_known_distance_nyc_to_la(self):
        """NYC to LA is approximately 3940 km — verify within +/-1%."""
        # NYC: (40.7128, -74.0060), LA: (34.0522, -118.2437)
        distance_m = _haversine_distance(40.7128, -74.0060, 34.0522, -118.2437)
        expected_m = 3_940_000.0
        assert distance_m == pytest.approx(expected_m, rel=0.01)

    def test_same_lat_different_lon(self):
        """Moving purely along longitude at equator should give known distance."""
        # At the equator, 1 degree of longitude ~ 111.3 km
        distance_m = _haversine_distance(0.0, 0.0, 0.0, 1.0)
        # Roughly 111 km; allow generous tolerance for spherical approximation
        assert distance_m == pytest.approx(111_000, rel=0.05)


class TestComputeNBCQuarterPredictionWindow(unittest.TestCase):
    """Tests that compute_nbc_quarter uses exactly the right number of samples for prediction."""

    def test_prediction_capped_at_60_samples_when_more_available(self):
        """With 360 samples available, prediction must use only the last 60 — not all 360.

        Uses constant values so we can compute expected prediction deterministically.

        Layout: 300 samples of 0.001 kWh/s (1 W) followed by 60 samples of 0.003 kWh/s (3 W).

        raw_wh = (300 * 0.001 + 60 * 0.003) * 1000 = 480 Wh
        remaining_seconds = 900 - 360 = 540

        Expected prediction_w when using exactly last 60:
            prediction_w = 1000 * (60 * 0.003) / 60 = 3.0 W

        Expected predicted_wh:
            predicted_wh = 480 + 540 * 3.0 = 2100 Wh

        If the code used more than 60 samples (e.g. last 90), prediction_w would be
        diluted by the earlier 1 W samples and predicted_wh would fall below 2100.
        """
        values = [0.001] * 300 + [0.003] * 60
        result = compute_nbc_quarter(values)

        self.assertIsNotNone(result)
        self.assertFalse(result.complete)
        self.assertEqual(result.samples_used, 360)

        expected_prediction_w = 3.0  # 1000 * 0.003
        expected_predicted_wh = 2100.0  # 480 + 540 * 3.0

        self.assertAlmostEqual(result.prediction_w, expected_prediction_w, places=6)
        self.assertAlmostEqual(result.predicted_wh, expected_predicted_wh, places=6)

    def test_prediction_uses_all_60_when_60_available(self):
        """With exactly 60 samples available, prediction must use all 60 — not fewer.

        Uses the last 30 samples to have a higher rate than the first 30 so that
        using fewer than 60 samples would inflate the prediction.

        Layout: 30 samples of 0.001 kWh/s (1 W) followed by 30 samples of 0.003 kWh/s (3 W).

        Expected prediction_w when using all 60:
            prediction_w = 1000 * (30 * 0.001 + 30 * 0.003) / 60 = 2.0 W

        Expected predicted_wh:
            raw_wh = 60 * 0.002 * 1000 = 120 Wh
            predicted_wh = 120 + 840 * 2.0 = 1800 Wh

        If the code used only the last 30 samples, prediction_w would be 3.0
        and predicted_wh would be 120 + 840 * 3.0 = 2640 Wh — clearly wrong.
        """
        values = [0.001] * 30 + [0.003] * 30
        result = compute_nbc_quarter(values, prediction_window_seconds=60)

        self.assertIsNotNone(result)
        self.assertFalse(result.complete)
        self.assertEqual(result.samples_used, 60)

        expected_prediction_w = 2.0  # 1000 * (30*0.001 + 30*0.003) / 60
        expected_predicted_wh = 1800.0  # 120 + 840 * 2.0

        self.assertAlmostEqual(result.prediction_w, expected_prediction_w, places=6)
        self.assertAlmostEqual(result.predicted_wh, expected_predicted_wh, places=6)

    def test_prediction_uses_all_samples_when_fewer_than_60(self):
        """With only 30 samples available, prediction must use all 30 — not try to use 60.

        Layout: 30 samples of 0.002 kWh/s (2 W).

        raw_wh = 30 * 0.002 * 1000 = 60 Wh
        remaining_seconds = 900 - 30 = 870

        Expected prediction_w = 1000 * 0.002 = 2.0 W
        Expected predicted_wh = 60 + 870 * 2.0 = 1800 Wh
        """
        values = [0.002] * 30
        result = compute_nbc_quarter(values)

        self.assertIsNotNone(result)
        self.assertFalse(result.complete)
        self.assertEqual(result.samples_used, 30)

        expected_prediction_w = 2.0
        expected_predicted_wh = 1800.0  # 60 + 870 * 2.0

        self.assertAlmostEqual(result.prediction_w, expected_prediction_w, places=6)
        self.assertAlmostEqual(result.predicted_wh, expected_predicted_wh, places=6)


    def test_custom_prediction_window_30(self):
        """With prediction_window_seconds=30, prediction must use only last 30 samples.

        Layout: 300 samples of 0.001 kWh/s (1 W) followed by 30 samples of 0.003 kWh/s (3 W).

        With window=30: values[-30:] = all 0.003
            prediction_w = 1000 * 0.003 = 3.0 W
            raw_wh = 1000 * (300*0.001 + 30*0.003) = 390 Wh
            remaining_seconds = 900 - 330 = 570
            predicted_wh = 390 + 570 * 3.0 = 2100 Wh

        With default 60, the window would dilute to 2.0 W and 1662 Wh.
        """
        values = [0.001] * 300 + [0.003] * 30
        result = compute_nbc_quarter(values, prediction_window_seconds=30)

        self.assertIsNotNone(result)
        self.assertFalse(result.complete)
        self.assertEqual(result.samples_used, 330)
        self.assertAlmostEqual(result.prediction_w, 3.0, places=6)
        self.assertAlmostEqual(result.predicted_wh, 2100.0, places=6)

    def test_prediction_window_capped_by_available_samples(self):
        """When prediction_window_seconds exceeds available samples, use all samples.

        Layout: 100 samples of 0.002 kWh/s. Request window=200.
        window = min(200, 100) = 100, uses all 100 samples.
        """
        values = [0.002] * 100
        result = compute_nbc_quarter(values, prediction_window_seconds=200)

        self.assertIsNotNone(result)
        self.assertFalse(result.complete)
        self.assertEqual(result.samples_used, 100)
        expected_prediction_w = 2.0  # 1000 * 0.002
        expected_predicted_wh = 1800.0  # 200 + 800 * 2.0
        self.assertAlmostEqual(result.prediction_w, expected_prediction_w, places=6)
        self.assertAlmostEqual(result.predicted_wh, expected_predicted_wh, places=6)

    def test_default_prediction_window_when_none(self):
        """With prediction_window_seconds=None, fall back to the default window.

        Same data as test_prediction_capped_at_60_samples_when_more_available,
        verifying the None default matches DEFAULT_PREDICTION_WINDOW_SECS.
        """
        values = [0.001] * 300 + [0.003] * 60
        result = compute_nbc_quarter(values, prediction_window_seconds=None)

        self.assertIsNotNone(result)
        self.assertFalse(result.complete)
        self.assertEqual(result.samples_used, 360)
        expected_prediction_w = 3.0
        expected_predicted_wh = 2100.0
        self.assertAlmostEqual(result.prediction_w, expected_prediction_w, places=6)
        self.assertAlmostEqual(result.predicted_wh, expected_predicted_wh, places=6)


class TestRetryableError:
    """RetryableError is the base class for transient, retryable failures.

    Subclasses (e.g. RetryableMetricsException) are handled as warnings and
    trigger stale-data serving; the base class relationship is what the
    EnergyCache fetch lock checks (energy_cache._with_fetch_lock), so it is
    locked in directly here.
    """

    def test_subclass_is_caught_by_base_class(self):
        """A RetryableError subclass is catchable as RetryableError."""
        from util import RetryableError

        class _Transient(RetryableError):
            """Test-only transient failure."""

        caught = False
        try:
            raise _Transient("transient failure")
        except RetryableError:
            caught = True
        assert caught

    def test_retryable_metrics_exception_is_subclass(self):
        """RetryableMetricsException derives from RetryableError."""
        from util import RetryableError
        from metrics import RetryableMetricsException

        assert issubclass(RetryableMetricsException, RetryableError)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# =============================================================================
# Atomic persistence (plan subtask 2.3, fixes R3)
# =============================================================================


class TestAtomicWriteJson:
    """atomic_write_json must never expose partial or corrupt files."""

    def test_roundtrip_writes_parseable_json(self, tmp_path):
        from util import atomic_write_json

        target = tmp_path / "out.json"
        atomic_write_json(target, {"refresh_token": "r", "expires": 5})
        assert json.loads(target.read_text(encoding="utf-8")) == {
            "refresh_token": "r",
            "expires": 5,
        }

    def test_failed_serialization_leaves_original_intact(self, tmp_path):
        """A mid-write failure preserves the previous file and cleans up."""
        from util import atomic_write_json

        target = tmp_path / "tokens.json"
        target.write_text('{"keep": true}', encoding="utf-8")

        with pytest.raises(TypeError):
            atomic_write_json(target, {"bad": object()})

        assert json.loads(target.read_text(encoding="utf-8")) == {"keep": True}
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == [], f"temp files leaked: {leftovers}"

    def test_concurrent_writers_converge_to_one_valid_file(self, tmp_path):
        """N racing writers leave exactly one parseable payload behind."""
        import threading

        from util import atomic_write_json

        target = tmp_path / "shared.json"
        barrier = threading.Barrier(8)

        def writer(i: int) -> None:
            barrier.wait()
            atomic_write_json(target, {"writer": i})

        threads = [
            threading.Thread(target=writer, args=(i,)) for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        payload = json.loads(target.read_text(encoding="utf-8"))
        assert isinstance(payload.get("writer"), int)

    def test_owner_only_permissions(self, tmp_path):
        """Credential files must not be world/group readable."""
        import stat

        from util import atomic_write_json

        target = tmp_path / "secret.json"
        atomic_write_json(target, {"access_token": "x"})
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode & 0o077 == 0, f"perms too open: {oct(mode)}"

    def test_accepts_str_paths(self, tmp_path):
        from util import atomic_write_json

        target = tmp_path / "s.json"
        atomic_write_json(str(target), {"ok": 1})
        assert json.loads(target.read_text(encoding="utf-8")) == {"ok": 1}


class TestAtomicWriteText:
    """Text twin for the fleet-telemetry timestamp dotfile."""

    def test_roundtrip_and_replace(self, tmp_path):
        from util import atomic_write_text

        target = tmp_path / "dotfile"
        atomic_write_text(target, "first\n")
        assert target.read_text(encoding="utf-8") == "first\n"
        atomic_write_text(target, "second\n")
        assert target.read_text(encoding="utf-8") == "second\n"


@pytest.mark.skipif(
    not Path("/proc/self/fd").exists(),
    reason="/proc is required to map fds to open paths (Linux)",
)
class TestAtomicWriteDirectoryFsync:
    """os.replace alone is not durable: until the *directory* entry is
    fsynced, a crash/power loss can lose the rename itself and revert the
    file to its previous contents even though the new data reached disk."""

    def test_directory_is_fsynced_after_replace(self, tmp_path, monkeypatch):
        """The final fsync in an atomic write must target the parent dir."""
        import os

        from util import atomic_write_text

        real_fsync = os.fsync
        synced_paths: list[str] = []

        def spying_fsync(fd: int) -> None:
            try:
                # /proc maps each open fd to its path while it is open.
                synced_paths.append(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:  # pragma: no cover - fd vanished mid-test
                synced_paths.append("<unmapped>")
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", spying_fsync)

        target = tmp_path / "tokens.json"
        atomic_write_text(target, "payload\n")

        # Two barriers total: file data first, then the rename via its dir.
        assert len(synced_paths) == 2, (
            f"expected 2 fsyncs (file, then dir), saw {synced_paths}"
        )
        tmp_file_path, dir_path = synced_paths
        assert tmp_file_path.startswith(str(tmp_path)), (
            f"first fsync must be the temp file, saw {tmp_file_path}"
        )
        assert dir_path == str(tmp_path), (
            f"last fsync must target the destination directory {tmp_path}, "
            f"saw {dir_path}"
        )

    @pytest.mark.skipif(
        __import__("sys").platform == "win32",
        reason="POSIX directory fds are unsupported on Windows",
    )
    def test_fsync_directory_helper_accepts_real_dir(self, tmp_path):
        """_fsync_directory runs cleanly against a live directory."""
        from util import _fsync_directory

        _fsync_directory(tmp_path)  # must not raise


class TestAtomicPersistenceWiring:
    """Production credential writes go through the atomic helpers."""

    def test_save_tesla_tokens_uses_atomic_write(self, tmp_path, monkeypatch):
        import load_controllers
        from load_controllers import save_tesla_tokens

        calls: list[tuple] = []

        def fake_atomic(path, data):
            calls.append((path, data))

        monkeypatch.setattr(
            load_controllers, "atomic_write_json", fake_atomic
        )
        tokens_path = tmp_path / ".tesla-tokens.json"
        save_tesla_tokens("rt", "at", 12345, tokens_path=tokens_path)
        assert calls, "save_tesla_tokens must use atomic_write_json"
        written_path, written_data = calls[0]
        assert Path(written_path) == tokens_path
        assert written_data == {
            "refresh_token": "rt",
            "access_token": "at",
            "expires": 12345,
        }

    def test_dotfile_write_uses_atomic_text(self, tmp_path, monkeypatch):
        import load_manager as lm_mod

        recorded: dict[str, str] = {}

        def fake_atomic(path, text):
            recorded["path"] = str(path)
            recorded["text"] = text

        monkeypatch.setattr(lm_mod, "atomic_write_text", fake_atomic)

        dotfile = tmp_path / ".fleet-telemetry-provisioned"
        assert hasattr(lm_mod, "_write_fleet_telemetry_dotfile")
        lm_mod._write_fleet_telemetry_dotfile(dotfile)
        assert Path(recorded["path"]) == dotfile
        assert recorded["text"].endswith("\n")


# =============================================================================
# Unified time base for QH extrapolation (plan subtask 3.3, fixes A3)
# =============================================================================


class TestSecondsRemainingOverride:
    """compute_nbc_quarter must accept a wall-clock remaining override."""

    def test_compute_nbc_quarter_honors_override(self):
        from util import compute_nbc_quarter

        values = [0.001] * 100
        default = compute_nbc_quarter(values)
        overridden = compute_nbc_quarter(
            values, seconds_remaining_override=300
        )

        assert default.remaining_seconds == 800
        assert overridden.remaining_seconds == 300
        expected = default.raw_wh + 300 * default.prediction_w
        assert overridden.predicted_wh == pytest.approx(expected)

    def test_negative_override_clamped_to_zero(self):
        from util import compute_nbc_quarter

        q = compute_nbc_quarter(
            [0.001] * 100, seconds_remaining_override=-5
        )
        assert q.remaining_seconds == 0
        assert q.predicted_wh == pytest.approx(q.raw_wh)

    def test_override_ignored_for_complete_quarter(self):
        from util import compute_nbc_quarter

        q = compute_nbc_quarter(
            [0.001] * 900, seconds_remaining_override=123
        )
        assert q.complete is True

    def test_quarters_passthrough_applies_to_qh1_only(self):
        from util import compute_nbc_quarters

        values = [0.001] * (900 + 100)
        qset = compute_nbc_quarters(
            values, seconds_remaining_override=250
        )
        assert qset.qh1.remaining_seconds == 250
        assert qset.qh2.complete is True


class TestTrailingChunkClassifier:
    """One classifier for a finished trailing quarter.

    ``data_start`` is 16:15. A finished 899-sample chunk is one second
    short of the full 16:15 window; a 839-sample chunk is 61 seconds short.
    A count that is a multiple of 900 means the whole trailing window is
    missing.
    """

    DATA_START = datetime(2026, 11, 5, 16, 15, 0, tzinfo=timezone.utc)

    def test_classifies_current_window(self) -> None:
        info = classify_trailing_chunk(
            self.DATA_START,
            899,
            datetime(2026, 11, 5, 16, 29, 59, tzinfo=timezone.utc),
        )
        assert info.kind is TrailingChunkKind.CURRENT
        assert info.shortfall_secs == 0
        assert info.start == self.DATA_START
        assert info.sample_count == 899

    def test_classifies_near_full_finished_window(self) -> None:
        info = classify_trailing_chunk(
            self.DATA_START,
            899,
            datetime(2026, 11, 5, 16, 30, 25, tzinfo=timezone.utc),
        )
        assert info.kind is TrailingChunkKind.FINISHED_NEAR_FULL
        assert info.shortfall_secs == 1
        assert info.start == self.DATA_START
        assert info.sample_count == 899

    def test_classifies_short_finished_window(self) -> None:
        info = classify_trailing_chunk(
            self.DATA_START,
            839,
            datetime(2026, 11, 5, 16, 30, 7, tzinfo=timezone.utc),
        )
        assert info.kind is TrailingChunkKind.FINISHED_SHORT
        assert info.shortfall_secs == 61
        assert info.start == self.DATA_START
        assert info.sample_count == 839

    def test_classifies_complete_gap(self) -> None:
        info = classify_trailing_chunk(
            self.DATA_START,
            900,
            datetime(2026, 11, 5, 16, 45, 0, tzinfo=timezone.utc),
        )
        assert info.kind is TrailingChunkKind.FINISHED_SHORT
        assert info.shortfall_secs == 900
        assert info.start == datetime(
            2026, 11, 5, 16, 30, tzinfo=timezone.utc
        )
        assert info.sample_count == 0

    @pytest.mark.parametrize(
        "sample_count,kind",
        [
            (895, TrailingChunkKind.FINISHED_NEAR_FULL),
            (894, TrailingChunkKind.FINISHED_SHORT),
        ],
    )
    def test_tolerance_boundary(
        self, sample_count: int, kind: TrailingChunkKind
    ) -> None:
        info = classify_trailing_chunk(
            self.DATA_START,
            sample_count,
            datetime(2026, 11, 5, 16, 30, 25, tzinfo=timezone.utc),
        )
        assert info.kind is kind

    def test_complete_quarter_uses_measured_sum(self) -> None:
        """899 samples finalize without padding to 900."""
        quarter = complete_quarter([0.001] * 899)
        assert quarter.complete is True
        assert quarter.raw_wh == pytest.approx(899.0)
        assert quarter.wh == pytest.approx(899.0)
        assert quarter.samples_used is None

    def test_complete_quarter_clamps_negative_wh(self) -> None:
        quarter = complete_quarter([-0.001] * 10)
        assert quarter.raw_wh == pytest.approx(-10.0)
        assert quarter.wh == 0.0

    @pytest.mark.parametrize(
        "sample_count,now",
        [
            (899, datetime(2026, 11, 5, 16, 29, 59, tzinfo=timezone.utc)),
            (899, datetime(2026, 11, 5, 16, 30, 25, tzinfo=timezone.utc)),
            (25, datetime(2026, 11, 5, 16, 15, 25, tzinfo=timezone.utc)),
            (900, datetime(2026, 11, 5, 16, 44, 59, tzinfo=timezone.utc)),
            (900, datetime(2026, 11, 5, 16, 45, 0, tzinfo=timezone.utc)),
        ],
    )
    def test_is_stale_trailing_qh_matches_classifier(
        self, sample_count: int, now: datetime
    ) -> None:
        info = classify_trailing_chunk(self.DATA_START, sample_count, now)
        assert is_stale_trailing_qh(self.DATA_START, sample_count, now) is (
            info.kind is not TrailingChunkKind.CURRENT
        )


class TestStaleTrailingQH:
    """The shared stale-slice predicate used by the cache and display paths.

    ``data_start`` is 16:15. A partial slice of 899 samples covers
    16:15:00-16:29:59 and its wall window ends at 16:30. A count that is a
    multiple of 900 means the trailing chunk is the next, unfetched window,
    so a complete-but-old data set also reports stale.
    """

    DATA_START = datetime(2026, 11, 5, 16, 15, 0, tzinfo=timezone.utc)

    @pytest.mark.parametrize(
        "sample_count,now,expected",
        [
            # Partial slice, still inside its own window.
            (899, datetime(2026, 11, 5, 16, 29, 59, tzinfo=timezone.utc), False),
            # Partial slice, the window has ended (inclusive boundary).
            (899, datetime(2026, 11, 5, 16, 30, 0, tzinfo=timezone.utc), True),
            (899, datetime(2026, 11, 5, 16, 30, 25, tzinfo=timezone.utc), True),
            # Fresh partial slice is inside its own window.
            (25, datetime(2026, 11, 5, 16, 15, 25, tzinfo=timezone.utc), False),
            # Complete chunk: the trailing chunk is the next, unfetched one.
            (900, datetime(2026, 11, 5, 16, 44, 59, tzinfo=timezone.utc), False),
            (900, datetime(2026, 11, 5, 16, 45, 0, tzinfo=timezone.utc), True),
            (1800, datetime(2026, 11, 5, 16, 59, 59, tzinfo=timezone.utc), False),
            (1800, datetime(2026, 11, 5, 17, 0, 0, tzinfo=timezone.utc), True),
        ],
    )
    def test_window_end_uses_floor_to_qh(
        self, sample_count: int, now: datetime, expected: bool
    ) -> None:
        """Only ``floor_to_qh(now)`` decides whether the window has ended."""
        assert (
            is_stale_trailing_qh(self.DATA_START, sample_count, now) is expected
        )


class TestQhStartsForWindow:
    """Wall-clock start of each QH slot for a fetch window.

    ``data_start`` is 16:15. The starts follow ``compute_nbc_quarters`` and
    ``PredictionMixin._compute_nbc``: the most recent slot is QH1 while the
    trailing chunk is current, QH2 after a near-full finalize, and QH3 after
    a short one. Older slots step back in 900-second increments, so the map
    also covers slots that ``inject_completed_qh`` fills from
    ``CompletedNBCPeriod`` records.
    """

    DATA_START = datetime(2026, 11, 5, 16, 15, 0, tzinfo=timezone.utc)
    UTC = timezone.utc

    def test_current_partial_window(self) -> None:
        """A partial trailing chunk is QH1; older slots step back 900 s."""
        starts = qh_starts_for_window(
            self.DATA_START,
            899,
            datetime(2026, 11, 5, 16, 29, 59, tzinfo=self.UTC),
        )
        assert starts == {
            "QH1": datetime(2026, 11, 5, 16, 15, tzinfo=self.UTC),
            "QH2": datetime(2026, 11, 5, 16, 0, tzinfo=self.UTC),
            "QH3": datetime(2026, 11, 5, 15, 45, tzinfo=self.UTC),
            "QH4": datetime(2026, 11, 5, 15, 30, tzinfo=self.UTC),
        }

    def test_current_window_ending_on_boundary(self) -> None:
        """A window that ends on a boundary makes the last full quarter QH1."""
        starts = qh_starts_for_window(
            self.DATA_START,
            1800,
            datetime(2026, 11, 5, 16, 32, tzinfo=self.UTC),
        )
        assert starts == {
            "QH1": datetime(2026, 11, 5, 16, 30, tzinfo=self.UTC),
            "QH2": datetime(2026, 11, 5, 16, 15, tzinfo=self.UTC),
            "QH3": datetime(2026, 11, 5, 16, 0, tzinfo=self.UTC),
            "QH4": datetime(2026, 11, 5, 15, 45, tzinfo=self.UTC),
        }

    def test_near_full_finalize_starts_at_qh2(self) -> None:
        """A near-full finished chunk becomes QH2; QH1 is absent."""
        starts = qh_starts_for_window(
            self.DATA_START,
            899,
            datetime(2026, 11, 5, 16, 30, 25, tzinfo=self.UTC),
        )
        assert starts == {
            "QH2": datetime(2026, 11, 5, 16, 15, tzinfo=self.UTC),
            "QH3": datetime(2026, 11, 5, 16, 0, tzinfo=self.UTC),
            "QH4": datetime(2026, 11, 5, 15, 45, tzinfo=self.UTC),
        }

    def test_short_finalize_starts_at_qh3(self) -> None:
        """A short finished chunk leaves QH2 empty; the fallback starts at QH3."""
        starts = qh_starts_for_window(
            self.DATA_START,
            1739,
            datetime(2026, 11, 5, 16, 45, 7, tzinfo=self.UTC),
        )
        assert starts == {
            "QH3": datetime(2026, 11, 5, 16, 15, tzinfo=self.UTC),
            "QH4": datetime(2026, 11, 5, 16, 0, tzinfo=self.UTC),
        }

    def test_short_finalize_without_complete_quarter_is_empty(self) -> None:
        """A dropped chunk that is the whole window names no quarter."""
        starts = qh_starts_for_window(
            self.DATA_START,
            839,
            datetime(2026, 11, 5, 16, 45, 7, tzinfo=self.UTC),
        )
        assert starts == {}

    def test_missing_window_starts_at_qh3(self) -> None:
        """A whole missing trailing window still starts the fallback at QH3."""
        starts = qh_starts_for_window(
            self.DATA_START,
            900,
            datetime(2026, 11, 5, 16, 45, tzinfo=self.UTC),
        )
        assert starts == {
            "QH3": datetime(2026, 11, 5, 16, 15, tzinfo=self.UTC),
            "QH4": datetime(2026, 11, 5, 16, 0, tzinfo=self.UTC),
        }

    def test_short_window_still_maps_qh2_to_qh4(self) -> None:
        """A 100-sample window maps every slot; injection fills QH2-QH4."""
        starts = qh_starts_for_window(
            self.DATA_START,
            100,
            datetime(2026, 11, 5, 16, 16, 40, tzinfo=self.UTC),
        )
        assert set(starts) == {"QH1", "QH2", "QH3", "QH4"}
        assert starts["QH2"] == datetime(2026, 11, 5, 16, 0, tzinfo=self.UTC)

    @pytest.mark.parametrize(
        "sample_count,now",
        [
            (100, datetime(2026, 11, 5, 16, 16, 40, tzinfo=timezone.utc)),
            (899, datetime(2026, 11, 5, 16, 29, 59, tzinfo=timezone.utc)),
            (1800, datetime(2026, 11, 5, 16, 32, 0, tzinfo=timezone.utc)),
            (2700, datetime(2026, 11, 5, 17, 2, 0, tzinfo=timezone.utc)),
        ],
    )
    def test_qh1_start_matches_sample_alignment(
        self, sample_count: int, now: datetime
    ) -> None:
        """QH1 starts at the first sample of the trailing chunk."""
        starts = qh_starts_for_window(self.DATA_START, sample_count, now)
        remainder = sample_count % 900
        first_sample = sample_count - (remainder or 900)
        assert starts["QH1"] == self.DATA_START + timedelta(seconds=first_sample)
