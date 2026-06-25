"""Tests for quantization detection in per-second float data."""

from __future__ import annotations

import random

import pytest

from quantization import detect_quantization


class TestDetectQuantization:
    """Tests for detect_quantization."""

    def test_n20_offset7(self):
        """Basic case: N=20, offset=7 — example from the spec.

        Layout: 7 seconds of preamble, then three 20-second constant-value
        samples with different values.  Total = 67 samples, 60 conforming.
        """
        data = [0.0] * 7 + [1.0] * 20 + [2.0] * 20 + [3.0] * 20
        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 20
        assert offset == 7
        assert confidence == pytest.approx(60 / 67, abs=1e-9)

    def test_n10_offset0(self):
        """N=10, quantization starts at second 0."""
        data = [1.0] * 10 + [2.0] * 10 + [3.0] * 10
        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 10
        assert offset == 0
        assert confidence == 1.0

    def test_n60_offset30_prefers_n30(self):
        """Preamble of 30s then 60s windows — N/2=30 scores higher confidence."""
        data = [0.0] * 30 + [1.0] * 60 + [2.0] * 60 + [3.0] * 60
        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 30
        assert offset == 0
        assert confidence == pytest.approx(1.0, abs=1e-9)

    def test_n2_offset1(self):
        """N=2, offset=1 — each window has a constant pair, different pairs.

        Layout: preamble of 1, then pairs of identical values.
        """
        data = [0.0] + [1.0, 1.0] + [2.0, 2.0] + [3.0, 3.0]
        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 2
        assert offset == 1
        # 6 conforming / 7 total
        assert confidence == pytest.approx(6 / 7, abs=1e-9)

    def test_n2_offset0_minimal(self):
        """N=2, offset=0, exactly 4 samples (minimum valid length)."""
        data = [1.0, 1.0, 2.0, 2.0]
        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 2
        assert offset == 0
        assert confidence == 1.0

    def test_no_quantization_random(self):
        """Random floats should not be quantized."""
        rng = random.Random(42)
        data = [rng.random() for _ in range(3600)]
        result = detect_quantization(data)

        assert result is None

    def test_no_quantization_linear(self):
        """Strictly linear data should not be quantized."""
        data = [i * 0.1 for i in range(3600)]
        result = detect_quantization(data)

        assert result is None

    def test_n20_with_trailing_partial(self):
        """3600 samples, N=20, offset=7, partial trailing window ignored.

        7 + 179*20 + 13 = 3600.  The trailing 13 samples are a partial
        window that should be ignored.
        """
        data: list[float] = [0.0] * 7
        for i in range(179):
            data.extend([float(i)] * 20)
        data.extend([0.0] * 13)

        assert len(data) == 3600

        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 20
        assert offset == 7
        # 179 * 20 = 3580 conforming out of 3600
        assert confidence == pytest.approx(3580 / 3600, abs=1e-9)

    def test_offset_maximized(self):
        """For the smallest N, the largest valid offset should be returned.

        N=2 is the smallest.  Data with pairs after 1 second of preamble.
        """
        data = [0.0] + [1.0, 1.0] + [2.0, 2.0] + [3.0, 3.0] + [4.0, 4.0] + [5.0, 5.0]
        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 2
        assert offset == 1

    def test_partial_window_ignored(self):
        """Trailing partial window should not prevent detection.

        3 + 5 + 5 + 2 = 15 samples.  N=5, offset=3 gives 2 pure windows.
        """
        data = [0.0] * 3 + [1.0] * 5 + [2.0] * 5 + [3.0] * 2
        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 5
        assert offset == 3
        # 10 conforming / 15 total
        assert confidence == pytest.approx(10 / 15, abs=1e-9)

    def test_all_same_value(self):
        """All identical values — N=2, offset=0 (smallest N wins)."""
        data = [5.0] * 100
        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 2
        assert offset == 0
        assert confidence == 1.0

    def test_3600_samples_even_n(self):
        """Full hour with exact N=30, offset=0, 120 windows."""
        data: list[float] = []
        for i in range(120):
            data.extend([float(i)] * 30)

        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 30
        assert offset == 0
        assert confidence == 1.0

    def test_2700_samples_edge(self):
        """Minimum expected length (2700) with N=60, offset=0."""
        data: list[float] = []
        for i in range(45):  # 45 windows of 60 = 2700
            data.extend([float(i)] * 60)

        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 60
        assert offset == 0
        assert confidence == 1.0

    def test_short_data_too_small(self):
        """Data shorter than minimum valid length returns None.

        Minimum: need at least 2 runs, so at least 2 values.
        With 3 values all different → 3 runs of length 1, mode=1, N<2 → None.
        """
        data = [1.0, 2.0, 3.0]
        result = detect_quantization(data)

        assert result is None

    def test_confidence_with_noise(self):
        """Quantization with some non-conforming points yields < 1.0 confidence.

        50 windows of 10 = 500 samples.  Inject one noise point at index 55
        (inside window 5, offset 0) so that window 5 becomes impure.
        """
        data: list[float] = []
        for i in range(50):
            data.extend([float(i)] * 10)
        data[55] = 999.0  # noise in window 5 (indices 50-59)

        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 10
        assert offset == 0
        # 49/50 windows are pure (window 5 is broken)
        # 490 conforming / 500 total
        assert confidence == pytest.approx(490 / 500, abs=1e-9)

    def test_different_values_per_window(self):
        """Each window has the same value within it but different across
        windows — this IS quantization (constant within each N-second
        chunk).
        """
        data = [-1.0] * 5  # preamble different from first sample value
        for i in range(10):
            data.extend([float(i)] * 5)
        # Total: 5 + 50 = 55, 11 runs of length 5, mode=5

        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 5
        # offset=0 and offset=5 both give 55/55 pure points;
        # algorithm picks 0 (first best).
        assert offset == 0
        assert confidence == 1.0

    def test_n50_offset25_prefers_n25(self):
        """Preamble of 25s then 50s windows — N/2=25 scores higher confidence."""
        data = [0.0] * 25 + [1.0] * 50 + [2.0] * 50 + [3.0] * 50
        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 25
        assert offset == 0
        assert confidence == pytest.approx(1.0, abs=1e-9)

    def test_sample_size_capped_at_60(self):
        """If quantization only appears at N > 60, return None."""
        data: list[float] = []
        for i in range(3):  # 3 windows of 70 = 210
            data.extend([float(i)] * 70)

        result = detect_quantization(data)

        # Mode is 70, but > 60 → None
        assert result is None

    def test_data_len_4_n2_offset0(self):
        """4 samples: [1,1,2,2] → N=2, offset=0, confidence=1.0."""
        data = [1.0, 1.0, 2.0, 2.0]
        result = detect_quantization(data)
        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 2
        assert offset == 0
        assert confidence == 1.0

    def test_data_len_5_n2_offset1(self):
        """5 samples: [0,1,1,2,2] → N=2, offset=1, confidence=4/5."""
        data = [0.0, 1.0, 1.0, 2.0, 2.0]
        result = detect_quantization(data)
        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 2
        assert offset == 1
        assert confidence == pytest.approx(4 / 5, abs=1e-9)

    def test_data_len_6_n2_both_offsets(self):
        """11 samples: [0,0,1,1,1,2,2,2,3,3,3] → N=3, offset=2.

        Runs: [0]*2, [1]*3, [2]*3, [3]*3 → mode=3, N=3.
        offset=0: windows (0,0,1) impure, (1,1,2) impure, (2,2,2) pure → 3
        offset=1: windows (0,1,1) impure, (1,2,2) impure, (2,3,3) impure → 0
        offset=2: windows (1,1,1) pure, (2,2,2) pure, (3,3,3) pure → 9
        offset=2 wins with highest purity.
        """
        data = [0.0, 0.0, 1.0, 1.0, 1.0, 2.0, 2.0, 2.0, 3.0, 3.0, 3.0]
        result = detect_quantization(data)
        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 3
        assert offset == 2
        assert confidence == pytest.approx(9 / 11, abs=1e-9)

    def test_nan_windows(self):
        """Windows of all NaNs are valid quantization."""
        data = [0.0] * 3 + [float("nan")] * 10 + [1.0] * 10
        result = detect_quantization(data)
        # Runs: 3*0.0, 10*NaN, 10*1.0 → mode=10
        # offset=3: [3,13]=NaN*10 ✓, [13,23]=1.0*10 ✓
        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 10
        assert offset == 3
        assert confidence == pytest.approx(20 / 23, abs=1e-9)

    def test_alternating_pairs(self):
        """Data: [0,0,1,1,2,2,...,9,9] → N=2, offset=0.

        10 runs of length 2 (one per pair), mode=2.
        """
        data = []
        for i in range(10):
            data.extend([float(i)] * 2)
        # Total = 20

        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 2
        assert offset == 0
        assert confidence == 1.0

    def test_30s_quantization_with_60s_runs(self):
        """30s-quantized data where adjacent windows sometimes match.

        8 runs of 60s (adjacent 30s windows with identical values) and
        4 runs of 30s.  Mode picks 60, but divisor check should prefer 30
        because 30s runs appear in 4/12 = 33% of runs (≥30%).
        """
        data: list[float] = []
        # 8 runs of 60s (paired 30s windows with same value)
        for i in range(8):
            data.extend([float(i)] * 60)
        # 4 runs of 30s (single 30s windows)
        for i in range(8, 12):
            data.extend([float(i)] * 30)
        # Total: 480 + 120 = 600

        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        # Divisor check prefers 30 over 60
        assert sample_size == 30
        assert offset == 0

    def test_genuine_60s_quantization_preserved(self):
        """Genuine 60s quantization — N/2 confidence ties, so N stays at 60."""
        data: list[float] = []
        for i in range(10):
            data.extend([float(i)] * 60)
        # Total: 600, 10 runs of 60, 0 runs of 30

        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 60
        assert offset == 0
        assert confidence == 1.0

    def test_csv_real_data(self):
        """Real Emporia data — 30s quantization with adjacent-window merging."""
        import csv as csv_mod
        data: list[float] = []
        with open("tests/data/2026-06-25-quant.csv") as f:
            for row in csv_mod.DictReader(f):
                data.append(float(row["M1208.24-Mains (kWatts)"]))

        result = detect_quantization(data)

        assert result is not None
        sample_size, offset, confidence = result
        assert sample_size == 30
        assert offset == 1
        assert confidence >= 0.99
