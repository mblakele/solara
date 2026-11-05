"""Call Emporia VUE API and marshal predicted usage.

Compatibility shim (architecture-v2 Phase E.1): the implementation now
lives in ``vue_session`` (auth/session), ``prediction`` (models and the
quantization window helper), ``hourly_fetch`` (hourly fetch and
prediction), and ``tou_reporter`` (Time-of-Use reporting). Every public
name stays importable from here so callers do not change.
"""

# This module is a re-export shim: every imported name is part of the
# public compatibility surface, not a local use.
# pylint: disable=unused-import

from constants import DRIFT_REJECTION_ALERT_AFTER
from energy_cache import EnergyCache
from hourly_fetch import (
    MAX_FETCH_WINDOW,
    DriftAlert,
    HourlyProjection,
    Metrics,
    _chart_start_for,
    _drift_alerts,
    _drift_rejections,
    cap_chart_start,
    create_metrics,
    drain_drift_alerts,
)
from prediction import (
    DeviceMetrics,
    DevicePrediction,
    _MinuteData,
    _PopulationResult,
    _PredictionData,
    prediction_window_from_cache,
)
from util import ceil_to_qh  # re-exported: tests and callers import it from here
from tou_reporter import (
    TOUBudgetExceeded,
    TOUReporter,
    TOUResult,
)
from vue_session import (
    MetricsBase,
    RetryableMetricsException,
    VueAuthenticationError,
    set_clock,
)
