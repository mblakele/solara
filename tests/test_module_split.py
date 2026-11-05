"""E.1: metrics.py is split into focused modules with a re-export shim.

These import-compat tests pin the seam while the split lands: callers keep
importing from ``metrics`` and get the same objects from the new home.
"""

import metrics


def test_vue_session_owns_base_and_session_defaults():
    """Session/auth base, clock, and exceptions live in vue_session."""
    import vue_session

    assert metrics.MetricsBase is vue_session.MetricsBase
    assert metrics.VueAuthenticationError is vue_session.VueAuthenticationError
    assert metrics.RetryableMetricsException is vue_session.RetryableMetricsException
    assert metrics.set_clock is vue_session.set_clock


def test_tou_reporter_owns_tou_classes():
    """TOU result, reporter, and budget error live in tou_reporter."""
    import tou_reporter

    assert metrics.TOUResult is tou_reporter.TOUResult
    assert metrics.TOUReporter is tou_reporter.TOUReporter
    assert metrics.TOUBudgetExceeded is tou_reporter.TOUBudgetExceeded


def test_prediction_owns_models_and_window_helper():
    """Prediction models and the quantization helper live in prediction."""
    import prediction

    assert metrics.DeviceMetrics is prediction.DeviceMetrics
    assert metrics.DevicePrediction is prediction.DevicePrediction
    assert (
        metrics.prediction_window_from_cache
        is prediction.prediction_window_from_cache
    )


def test_prediction_owns_predict_and_nbc_methods():
    """Predict/NBC behavior lives in prediction, mixed into HourlyProjection."""
    import hourly_fetch
    import prediction

    for name in ("predict", "_compute_nbc", "_predict_device"):
        assert hasattr(prediction.PredictionMixin, name), name
    assert (
        hourly_fetch.HourlyProjection.predict
        is prediction.PredictionMixin.predict
    )


def test_split_modules_stay_under_500_lines():
    """E.1 acceptance: every module in the split stays under 500 lines."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    oversized = {
        name: len((root / f"{name}.py").read_text().splitlines())
        for name in (
            "metrics",
            "vue_session",
            "prediction",
            "hourly_fetch",
            "tou_reporter",
        )
        if len((root / f"{name}.py").read_text().splitlines()) >= 500
    }
    assert not oversized, f"modules over 500 lines: {oversized}"
