"""Ideas / demand section uses gap Wh (target-aware), not forecast Wh."""
from __future__ import annotations


def _render(
    predicted_wh: float,
    remaining_seconds: int = 900,
    gap_wh: float | None = None,
    target_wh: float | None = None,
    hysteresis_wh: float = 50.0,
) -> str:
    """Render _metrics.html with controlled QH1 + diagnostics."""
    from flask import render_template
    import app as app_mod

    from tests.test_app import realistic_metrics

    metrics = realistic_metrics()
    nbc = metrics["devices"][0]["nbc"]
    nbc = dict(
        nbc,
        QH1=dict(
            nbc["QH1"],
            predicted_wh=predicted_wh,
            remaining_seconds=remaining_seconds,
        ),
    )
    metrics = {"devices": [dict(metrics["devices"][0], nbc=nbc)]}
    diagnostics: dict = {"hysteresis_wh": hysteresis_wh}
    if gap_wh is not None:
        diagnostics["gap_wh"] = gap_wh
    load_management: dict = {"last_cycle_result": {"diagnostics": diagnostics}}
    if target_wh is not None:
        load_management["target_wh"] = target_wh
    with app_mod.app.test_request_context():
        return render_template(
            "_metrics.html",
            metrics=metrics,
            load_management=load_management,
            freshness=None,
        )


def test_ideas_use_gap_wh_not_forecast() -> None:
    """Surplus ideas derive from gap Wh (750), not predicted Wh (-800)."""
    html = _render(predicted_wh=-800.0, gap_wh=750.0, target_wh=-50.0)
    # 750/81 = 9.26 -> "9.3 min"; 750/88 = 8.52 -> "8.5 min";
    # 750/0.25/240 = 12.5 A.
    assert "9.3" in html
    assert "8.5" in html
    assert "+12.5 Amps 240V" in html
    # Old forecast-based values must be gone.
    assert "9.9 min" not in html
    assert "9.1 min" not in html
    assert "+13.3 Amps 240V" not in html


def test_reduce_branch_uses_gap_wh() -> None:
    """Deficit ideas derive from a negative gap."""
    html = _render(predicted_wh=100.0, gap_wh=-150.0, target_wh=-50.0)
    assert "reduce usage if possible" in html
    # -150/0.25/240 = -2.5 A.
    assert "-2.5 Amps 240V" in html


def test_hysteresis_gates_on_gap() -> None:
    """A gap inside hysteresis shows neither Ideas nor reduce."""
    html = _render(predicted_wh=-800.0, gap_wh=10.0, target_wh=-50.0)
    assert ">Ideas<" not in html
    assert "reduce usage if possible" not in html


def test_fallback_derives_gap_from_target() -> None:
    """Without diag gap, gap = target - predicted still accounts for target."""
    html = _render(predicted_wh=-800.0, gap_wh=None, target_wh=-50.0)
    assert "9.3" in html
    assert "+12.5 Amps 240V" in html
