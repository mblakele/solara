"""Tests for util.py functions.

Covers:
  - _haversine_distance (GPS distance calculation)
  - compute_nbc_quarter prediction window behavior
"""

import json
import unittest
from pathlib import Path

import pytest

from util import _haversine_distance, compute_nbc_quarter


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
