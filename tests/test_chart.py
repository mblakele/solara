"""Tests for the per-second SVG sparkline generator (chart.py).

The sparkline renders each per-second energy sample as a vertical bar whose
height encodes magnitude (abs value) and whose color encodes sign: green for
generation (negative values) and blue for consumption (positive values).
"""

import re

import pytest

from chart import per_second_sparkline


def _bar_colors(svg: str) -> list[str]:
    """Extract the fill/presentation color of each rendered bar in order."""
    import re

    # A bar is a <rect> (or <path>) with an explicit fill. Grab fills in order.
    return re.findall(r'fill="([^"]+)"', svg)


def _bar_count(svg: str) -> int:
    import re

    return len(re.findall(r"<(rect|path|line)\b", svg))


def _bar_xs(svg: str) -> list[float]:
    """Parse each bar's left-edge x position in render order."""
    import re

    return [float(x) for x in re.findall(r'<rect[^>]*\bx="([0-9.-]+)"', svg)]


class TestPerSecondSparkline:
    def test_empty_samples_returns_svg_no_bars(self):
        svg = per_second_sparkline([])
        assert "<svg" in svg
        assert svg.strip().endswith("</svg>")
        assert _bar_count(svg) == 0

    def test_single_positive_value_blue_bar(self):
        svg = per_second_sparkline([0.001])
        assert _bar_count(svg) == 1
        assert _bar_colors(svg) == ["#1f77b4"]  # blue

    def test_single_negative_value_green_bar(self):
        svg = per_second_sparkline([-0.001])
        assert _bar_count(svg) == 1
        assert _bar_colors(svg) == ["#2ca02c"]  # green

    def test_mixed_signs_colored_by_sign(self):
        svg = per_second_sparkline([-0.001, 0.001, -0.002, 0.0005])
        # green, blue, green, blue
        assert _bar_colors(svg) == ["#2ca02c", "#1f77b4", "#2ca02c", "#1f77b4"]

    def test_all_zero_no_division_by_zero(self):
        svg = per_second_sparkline([0.0, 0.0, 0.0])
        assert "<svg" in svg
        assert svg.strip().endswith("</svg>")
        # zero-magnitude values produce no meaningful bars, but must not crash
        assert _bar_count(svg) == 0

    def test_relative_scaling_heights_proportional(self):
        # 0.002 is twice 0.001, so its bar should render twice as tall.
        svg = per_second_sparkline([0.002, 0.001])
        heights = _bar_heights(svg)
        assert len(heights) == 2
        assert heights[0] > heights[1]
        # The taller (first) bar is exactly twice the shorter one.
        assert heights[0] == pytest.approx(2 * heights[1])

    def test_bar_count_equals_nonzero_sample_count(self):
        svg = per_second_sparkline([-0.001, 0.0, 0.002, 0.003])
        # zero sample contributes no bar
        assert _bar_count(svg) == 3

    def test_partial_window_left_aligned_with_blank_right(self):
        # A short window (less than the full 5 minutes) must not stretch the
        # bars across the view: each second keeps its fixed 1-unit slot, so a
        # 30-second window renders left-aligned with blank space on the right.
        svg = per_second_sparkline([0.001] * 30)
        xs = _bar_xs(svg)
        assert len(xs) == 30
        assert xs[0] == pytest.approx(0.0)
        # DEFAULT_WIDTH (300) over the 300-second window = one bar per slot.
        assert xs[-1] == pytest.approx(29.0)
        # Data ends well before the right margin.
        assert xs[-1] < 100.0

    def test_full_window_fills_view_width(self):
        # A complete 300-second window spans the full view with no blank edge.
        svg = per_second_sparkline([0.001] * 300)
        xs = _bar_xs(svg)
        assert len(xs) == 300
        assert xs[0] == pytest.approx(0.0)
        assert xs[-1] == pytest.approx(299.0)

    def test_bars_render_flush_with_no_interbar_gap(self):
        # Each bar fills its 1-unit slot (no sub-pixel 0.2-gap grating). The
        # zero-gap layout avoids the moire banding caused by aliasing a fine
        # transparent grating against the device pixel grid when the CSS
        # scales the SVG to a non-integer width.
        svg = per_second_sparkline([0.001] * 300)
        widths = [float(w) for w in re.findall(r'<rect[^>]*\bwidth="([0-9.]+)"', svg)]
        assert len(widths) == 300
        assert all(w == pytest.approx(1.0) for w in widths)
        # Bar slots tile the full width exactly: right edges at 1, 2, ... 300.
        xs = _bar_xs(svg)
        right_edges = [x + w for x, w in zip(xs, widths)]
        assert right_edges[-1] == pytest.approx(300.0)

    def test_bucket_seconds_default_keeps_one_bar_per_second(self):
        # Without an explicit bucket, behavior is unchanged: 300 bars, one
        # per second, each exactly one viewBox unit wide.
        svg = per_second_sparkline([0.001] * 300)
        widths = [float(w) for w in re.findall(r'<rect[^>]*\bwidth="([0-9.]+)"', svg)]
        assert len(widths) == 300
        assert all(w == pytest.approx(1.0) for w in widths)

    def test_bucket_seconds_groups_samples_into_quantized_bars(self):
        # Downsampling to a 30-second quantization window for a full 300s
        # sample window yields 10 bars, each spanning its 30-unit slot tiled
        # edge-to-edge across the view — wide bars rather than a sub-pixel
        # grating, so the CSS-scaled SVG cannot alias into moire bands.
        svg = per_second_sparkline([0.001] * 300, bucket_secs=30)
        widths = [float(w) for w in re.findall(r'<rect[^>]*\bwidth="([0-9.]+)"', svg)]
        xs = _bar_xs(svg)
        assert len(widths) == 10
        # 30 s of data + 1 s seam overlap onto the following bar.
        assert all(w == pytest.approx(31.0) for w in widths)
        assert xs[0] == pytest.approx(0.0)
        assert xs[-1] == pytest.approx(270.0)
        # Each bar overhangs its right neighbor so the anti-aliased seam at
        # the shared edge is repainted solid (no hairline gaps).
        for x, w, nx in zip(xs, widths, xs[1:]):
            assert x + w > nx
        # The final bar's overhang extends to 301, clipped by the 300-unit view.
        right_edges = [x + w for x, w in zip(xs, widths)]
        assert right_edges[-1] == pytest.approx(301.0)

    def test_bucket_seconds_keeps_left_aligned_time_positions(self):
        # A 60-second window bucketed at 30s keeps real-time slots: bars at
        # x=0 and x=30, blank to the right.
        svg = per_second_sparkline([0.001] * 60, bucket_secs=30)
        xs = _bar_xs(svg)
        assert xs == [pytest.approx(0.0), pytest.approx(30.0)]

    def test_bucket_seconds_averages_mixed_signs_per_window(self):
        # Two 2-second buckets: (+1,+1) -> +1 blue bar; (+1,-1) -> 0 mean,
        # which contributes no bar (signed-mean aggregation).
        svg = per_second_sparkline([1.0, 1.0, 1.0, -1.0], bucket_secs=2)
        assert _bar_count(svg) == 1
        assert _bar_colors(svg) == ["#1f77b4"]

    def test_bucket_partial_chunk_includes_seam_overlap(self):
        # A trailing chunk shorter than the bucket spans its real seconds
        # plus the 1 s seam overlap (25 data seconds -> 26-unit bar).
        svg = per_second_sparkline([0.001] * 25, bucket_secs=30)
        widths = [float(w) for w in re.findall(r'<rect[^>]*\bwidth="([0-9.]+)"', svg)]
        assert len(widths) == 1
        assert widths[0] == pytest.approx(26.0)

    def test_zero_samples_keep_their_time_slot(self):
        # Zero seconds produce no bar but still occupy their slot, so bars
        # render at their true positions within the 5-minute window.
        svg = per_second_sparkline([0.0, 0.001, 0.0, 0.002, 0.0])
        assert _bar_xs(svg) == [pytest.approx(1.0), pytest.approx(3.0)]

    def test_svg_well_formed(self):
        svg = per_second_sparkline([-0.001, 0.002, 0.003])
        assert svg.startswith("<svg")
        assert "</svg>" in svg
        assert "width" in svg and "height" in svg

    def test_svg_snaps_edges_to_pixel_grid(self):
        # shape-rendering="crispEdges" forces the browser to snap bar edges
        # onto whole device pixels, so anti-aliased seams of chart background
        # cannot render between adjacent columns at fractional CSS scales.
        svg = per_second_sparkline([0.001] * 300, bucket_secs=30)
        root = svg.split(">")[0]
        assert 'shape-rendering="crispEdges"' in root
        # The attribute belongs on the svg root only, not on the bars.
        assert svg.count('shape-rendering="crispEdges"') == 1


def _bar_heights(svg: str) -> list[float]:
    """Parse each bar's rendered height from its height attribute."""
    import re

    # Bars are <rect> elements; match only the rects' height attribute so the
    # svg element's own height="N" attribute is not counted.
    return [float(h) for h in re.findall(r"<rect[^>]*\bheight=\"([0-9.]+)\"", svg)]
