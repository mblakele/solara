"""QH-boundary forecast regression: label must survive a complete QH1.

At the end of a QH period (see bugs/2026-10-14-index-html-glitch.json)
all four NBC quarters are complete, so QH1.predicted_wh is None and the
load manager reports status ``no_incomplete_qh``. The index template
must still render the forecast-period label (``waiting for data``) next
to the placeholder instead of leaving a blank space.
"""

from load_models import CycleResult
import unittest
from unittest.mock import MagicMock

from app import app
from load_models import CycleResult

from tests.test_app import mock_config


class TestQHBoundaryForecastLabel(unittest.TestCase):
    """Forecast period label is visible when QH1 is complete."""

    def setUp(self) -> None:
        self.app = app.test_client()

    def test_boundary_complete_qh_still_shows_waiting_label(self) -> None:
        """Complete QH1 (instant_minute>=60) shows its Wh as the headline."""
        import app as app_mod

        mock_lm = MagicMock()
        mock_lm.enabled = True
        mock_lm.target_wh = -500
        mock_lm.nbc_device = "test_nbc"
        mock_lm.state = MagicMock()
        mock_lm.state.devices = {}
        mock_lm.state.pending_effects = []
        mock_lm.run_cycle.return_value = CycleResult(status="no_incomplete_qh")
        mock_lm.sentinel_names = []

        with mock_config(overrides={"MOCK": "True", "DEBUG": "True"}):
            from config import Config

            Config().set("LOAD_MANAGE_ENABLED", "True")
            app_mod._state.load_manager = mock_lm
            app_mod._state.last_cycle_result = mock_lm.run_cycle.return_value
            response = self.app.get(
                "/?instant_minute=65", headers={"Accept": "text/html"}
            )
        self.assertEqual(response.status_code, 200)
        data = response.data.decode("utf-8")
        self.assertIn("waiting for data", data)
        # The completed QH's Wh is surfaced as the headline number (instead
        # of a placeholder) — the most recent period's value is now visible
        # in the forecast card.
        self.assertNotIn("forecast__placeholder", data)
        self.assertIn("forecast__value--gen", data)
