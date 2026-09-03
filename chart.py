"""Server-side SVG generation for the per-second energy sparkline.

Generates an inline SVG column/bar sparkline from per-second energy samples.
Each sample is rendered as a vertical bar whose height encodes magnitude
(absolute value) and whose color encodes sign:

- Green for generation (negative values, i.e. solar exported to the grid).
- Blue for consumption (positive values, i.e. power drawn from the grid).

Consecutive samples may be aggregated into buckets (``bucket_secs``) so each
bar spans the detected quantization window (the actual Emporia sample size,
typically 30 s) instead of rendering one sub-pixel bar per second. Wide bars
cannot alias against the device pixel grid when the CSS scales the SVG to a
non-integer width, which is what caused the moire shimmer/banding. Bucketed
bars overhang the next bar by one second so the shared edge between two bars
is repainted solid, and the ``shape-rendering="crispEdges"`` presentation
attribute snaps every edge onto whole device pixels at rasterization time.
Together these prevent the ~1px anti-aliased seams at column boundaries
(including the taller-bar edge above a shorter neighbor) that would otherwise
show the light chart background through as hairline lines.

The output is intentionally label-free (no axes, ticks, or text) so it can be
embedded compactly in the dashboard. The function is pure and independently
testable (see tests/test_chart.py).
"""

from __future__ import annotations

from typing import Sequence

# Colors for sign encoding (green = generation/negative, blue = consumption/positive).
GEN_COLOR = "#2ca02c"  # green
LOAD_COLOR = "#1f77b4"  # blue

# Default rendered dimensions (logical units used by the viewBox).
DEFAULT_WIDTH = 300
DEFAULT_HEIGHT = 48
# Full 5-minute window in per-second samples. The sparkline is always laid
# out as if a complete window were present, so partial windows render
# left-aligned at their real time positions with blank space on the right
# margin instead of stretching the available data across the whole view.
# Bars are drawn flush (one full slot each), leaving no transparent grating
# to alias against the screen. Bucketed bars are intentionally wider than
# one unit (see the module docstring) so the rendered chart cannot produce
# sub-pixel moire patterns.
WINDOW_SECS = 300

# Bucketed bars extend one second past their nominal right edge. The next
# bar draws over that sliver (they are emitted left-to-right), so the shared
# boundary is repainted solid wherever both bars exist. This overhang alone
# cannot cover the seam region above a shorter neighbor (nothing is there to
# repaint), so the SVG root also carries shape-rendering="crispEdges", which
# snaps edges onto the device pixel grid and prevents anti-aliased seams
# entirely. Only applies to multi-second buckets; per-second bars stay
# exactly one unit wide.
SEAM_OVERLAP_SECS = 1


def _build_bar_path(
    x: float,
    y: float,
    width: float,
    height: float,
    color: str,
) -> str:
    """Return the SVG markup for a single vertical bar.

    Args:
        x: Left edge of the bar in viewBox units.
        y: Top edge of the bar in viewBox units (origin at top-left).
        width: Bar width in viewBox units.
        height: Bar height in viewBox units.
        color: Fill color for the bar.

    Returns:
        SVG <rect> element string.
    """
    return (
        f'<rect x="{x:.2f}" y="{y:.2f}" '
        f'width="{width:.2f}" height="{height:.2f}" fill="{color}"/>'
    )


def per_second_sparkline(
    samples: Sequence[float],
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    bucket_secs: int | None = None,
) -> str:
    """Generate an inline SVG column sparkline for per-second energy samples.

    Args:
        samples: Per-second energy values in kWh/second. Negative values are
            generation (green), positive values consumption (blue).
        width: Logical width of the chart (viewBox units).
        height: Logical height of the chart (viewBox units).
        bucket_secs: Optional aggregation window in seconds (> 1). Consecutive
            samples are averaged into one bar per window, so each bar spans a
            full ``bucket_secs``-unit slot (the quantized sample size) instead
            of a single sub-pixel second. Pass ``None`` (default) or 1 to keep
            one bar per second.

    Returns:
        A complete, self-contained inline SVG element string. Empty or all-zero
        samples yield a valid svg with no bars.
    """
    # Only the last 5 minutes are shown, matching the upstream 300-sample trim.
    samples = samples[-WINDOW_SECS:]

    # Each entry is (bucket_index, seconds_present, value). Zero-valued buckets
    # are kept out of the bar list but still occupy their slot, so bars land on
    # their true time positions within the fixed-width window.
    indexed: list[tuple[int, int, float]] = []
    if bucket_secs is not None and bucket_secs > 1:
        step = max(1, int(bucket_secs))
        for start in range(0, len(samples), step):
            chunk = samples[start : start + step]
            if not chunk:
                continue
            # Signed mean: preserves the waveform's sign (green vs blue) and
            # smooths per-second noise within each quantized window.
            mean = sum(chunk) / len(chunk)
            if mean != 0:
                indexed.append((start // step, len(chunk), mean))
    else:
        step = 1
        for i, value in enumerate(samples):
            if value != 0:
                indexed.append((i, 1, value))

    max_abs = max((abs(value) for _, _, value in indexed), default=0.0)

    bars: list[str] = []
    if indexed and max_abs > 0:
        # One fixed slot per second; the window never stretches to fit fewer.
        # Each bar fills its whole slot (flush) — one bar per bucket — so a
        # bucketed chart leaves no sub-pixel gap to alias against the device
        # pixel grid when the browser scales the SVG to a non-integer width.
        bar_step = width / WINDOW_SECS
        overlap = SEAM_OVERLAP_SECS if step > 1 else 0
        for i, span, value in indexed:
            magnitude = abs(value)
            bar_height = height * magnitude / max_abs
            y = height - bar_height
            x = i * step * bar_step
            # Bucketed bars overhang their right neighbor by one second; the
            # later bar repaints the shared edge so no hairline seam remains.
            bar_width = (span + overlap) * bar_step
            color = GEN_COLOR if value < 0 else LOAD_COLOR
            bars.append(_build_bar_path(x, y, bar_width, bar_height, color))

    body = "".join(bars)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'shape-rendering="crispEdges" '
        f'viewBox="0 0 {width} {height}" width="{width}" height="{height}">'
        f"{body}</svg>"
    )
