"""Quantization detection for per-second float data.

Scans per-second float arrays for repeating constant-value windows
(N-second chunks) and reports the sample size, the offset where the
first complete sample begins, and a confidence score.
"""

from __future__ import annotations

import math
from collections import Counter


def _equal(a: float, b: float) -> bool:
    """Compare two floats, treating NaN == NaN as equal."""
    a_nan = math.isnan(a)
    b_nan = math.isnan(b)
    if a_nan:
        return b_nan
    return a == b


def detect_quantization(data: list[float]) -> tuple[int, int, float] | None:
    """Detect quantization in per-second float data.

    Scans the data for repeating constant-value windows of N seconds.
    Returns (N, offset, confidence) where:

      - **N** is the sample size in seconds (always >= 2, capped at 60)
      - **offset** is the number of seconds from index 0 where the first
        complete sample begins
      - **confidence** is the fraction of data points that conform to the
        observed pattern (points in pure windows / total points), in [0, 1]

    Returns ``None`` if no quantization is detected.

    The algorithm works in three steps:

    1. **Find runs** — consecutive sequences of identical values.
    2. **Determine N** — the most common run length (the mode; smallest
       in case of a tie).  This is the fundamental sample size.
    3. **Find the best offset** — iterate over offsets ``0 .. N-1`` and
       pick the one that maximises the number of "pure" windows (windows
       whose every element is identical).  Confidence is the fraction of
       points inside pure windows.

    This approach naturally handles real-world clock skew: some runs
    will be slightly shorter or longer than N, but the mode still gives
    the correct sample size, and most windows will be pure.

    Args:
        data: List of per-second float values (2700-3600 typical).

    Returns:
        ``(sample_size, offset, confidence)`` tuple, or ``None`` if no
        quantization found.

    Examples:
        Three 20-second samples after a 7-second preamble::

            >>> data = [0.0] * 7 + [1.0] * 20 + [2.0] * 20 + [3.0] * 20
            >>> detect_quantization(data)
            (20, 7, 0.8955...)

        Full hour of exact 30-second samples::

            >>> data = []
            >>> for i in range(120):
            ...     data.extend([float(i)] * 30)
            >>> detect_quantization(data)
            (30, 0, 1.0)

        Random noise — no quantization::

            >>> import random
            >>> data = [random.random() for _ in range(3600)]
            >>> detect_quantization(data) is None
            True
    """
    data_len = len(data)
    if data_len < 4:
        return None

    # Step 1: Find runs of consecutive identical values.
    # Each run is (value, start_index, length).
    runs: list[tuple[float, int, int]] = []
    run_start = 0
    for i in range(1, data_len):
        if not _equal(data[i], data[i - 1]):
            runs.append((data[i - 1], run_start, i - run_start))
            run_start = i
    runs.append((data[-1], run_start, data_len - run_start))

    if len(runs) < 2:
        # All values are the same — every N-sized window is pure.
        # Smallest valid sample size is 2.
        return (2, 0, 1.0)

    # Step 2: N = mode of run lengths (smallest in case of tie).
    length_counts: Counter = Counter(r[2] for r in runs)
    max_count = max(length_counts.values())
    candidates = sorted(n for n, c in length_counts.items() if c == max_count)
    n = candidates[0]

    # The run-length mode can be wrong in two ways:
    #   1. Adjacent windows with identical values merge, inflating the mode
    #      (e.g. real period 30, mode becomes 60).  Check N/2.
    #   2. Frequent value changes within a true window shrink the mode
    #      (e.g. real period 30, mode becomes 15).  Check multiples of N.
    # For each candidate, score window-purity and prefer the one that
    # scores strictly higher (≥ 2 % improvement) over the mode.
    def _score(d: int) -> int:
        best = 0
        for off in range(d):
            s = 0
            nw = (data_len - off) // d
            for k in range(nw):
                ws = off + k * d
                v = data[ws]
                if all(_equal(data[j], v) for j in range(ws + 1, ws + d)):
                    s += d
            if s > best:
                best = s
        return best

    best_n = n
    best_s = _score(n)

    # Check N/2 (handles inflated mode).
    # Only switch if N/2 has some representation in the run distribution —
    # otherwise its higher purity is just an artifact of smaller windows
    # dodging value changes rather than a genuine quantization period.
    if n > 2 and n % 2 == 0:
        n_half = n // 2
        n_half_runs = sum(
            1 for _, _, rl in runs
            if abs(rl - n_half) <= 1
        )
        if n_half_runs >= 1:
            s_half = _score(n_half)
            if best_s > 0 and s_half > best_s * 1.02:
                best_n, best_s = n_half, s_half

    # Check multiples 2N, 3N, … up to 60 (handles shrunk mode).
    mult = 2
    while mult * n <= 60:
        n_mult = mult * n
        s_mult = _score(n_mult)
        if best_s > 0 and s_mult > best_s * 1.02:
            best_n, best_s = n_mult, s_mult
        mult += 1

    n = best_n

    # Cap at 60; must be at least 2.
    if n > 60 or n < 2:
        return None

    # Step 3: For each offset 0 .. N-1, score by number of pure windows.
    best_offset = 0
    best_score = 0

    for offset in range(n):
        score = 0
        num_windows = (data_len - offset) // n
        for k in range(num_windows):
            window_start = offset + k * n
            window_end = window_start + n
            # Check if the window is pure (all values equal, NaN==NaN).
            window_value = data[window_start]
            is_pure = True
            for j in range(window_start + 1, window_end):
                if not _equal(data[j], window_value):
                    is_pure = False
                    break
            if is_pure:
                score += n
        if score > best_score:
            best_score = score
            best_offset = offset

    confidence = best_score / data_len
    return (n, best_offset, confidence)
