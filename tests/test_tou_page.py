"""TOU page integration tests: look-and-feel, date pickers, details default."""

import json
import unittest
from datetime import datetime, timedelta, timezone

import pytest

from app import app
from tests.test_app import mock_config


@pytest.mark.parametrize(
    ("start", "end", "count"),
    [
        ("2026-09-16", "2026-09-16", 96),
        ("2026-09-16", "2026-09-17", 192),
        ("2026-03-08", "2026-03-08", 92),
        ("2026-11-01", "2026-11-01", 100),
        ("2026-09-16", "2026-09-16T04:00:00", 16),
    ],
)
def test_tou_inclusive_date_range(start: str, end: str, count: int) -> None:
    """Date-only ends include the local day, including DST-short/long days."""
    with mock_config(MOCK=True, TIMEZONE="America/Los_Angeles"):
        response = app.test_client().get(
            "/api/v1/tou",
            query_string={"start_date": start, "end_date": end, "details": "true"},
            headers={"Accept": "application/json"},
        )
    assert response.status_code == 200
    assert len(response.get_json()["periods"]) == count


def test_tou_same_date_defaults_and_picker() -> None:
    """The exclusive fetch boundary must not alter the picker or details default."""
    with mock_config(MOCK=True, TIMEZONE="America/Los_Angeles"):
        response = app.test_client().get(
            "/api/v1/tou?start_date=2026-09-16&end_date=2026-09-16",
            headers={"Accept": "text/html"},
        )
    html = response.get_data(as_text=True)
    assert '<table' in html
    assert 'name="end_date" value="2026-09-16"' in html


@pytest.mark.parametrize(("days", "status"), [(366, 200), (367, 400)])
def test_tou_inclusive_range_limit(days: int, status: int) -> None:
    """The range limit counts the selected end day as well."""
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=days - 1)
    with mock_config(MOCK=True, TIMEZONE="America/Los_Angeles"):
        response = app.test_client().get(
            "/api/v1/tou",
            query_string={"start_date": start.date(), "end_date": end.date()},
        )
    assert response.status_code == status


@pytest.mark.parametrize("details", ["true", "false"])
def test_tou_refresh_controls(details: str) -> None:
    """Refresh submits the date form; checkbox changes use validated submission."""
    with mock_config(MOCK=True, TIMEZONE="America/Los_Angeles"):
        response = app.test_client().get(
            "/api/v1/tou?start_date=2026-09-16&end_date=2026-09-16"
            f"&details={details}",
            headers={"Accept": "text/html"},
        )
    html = response.get_data(as_text=True)
    header = html.split('<header', 1)[1].split('</header>', 1)[0]
    nav = html.split('<nav', 1)[1].split('</nav>', 1)[0]
    assert '>Update</button>' not in html
    assert 'Dashboard</a>' in nav
    assert 'type="submit" form="tou-form">Refresh ⟳</button>' in header
    # Header order: Solara, refresh, spacer, dashboard link.
    title_idx = header.index('>Solara</h1>')
    refresh_idx = header.index('>Refresh ⟳</button>')
    spacer_idx = header.index('app-bar__spacer')
    dash_idx = header.index('Dashboard</a>')
    assert title_idx < refresh_idx < spacer_idx < dash_idx
    assert "onchange=\"this.form.requestSubmit()\"" in html
    # Date pickers also trigger an update: the change handler syncs the
    # details checkbox first, then submits via requestSubmit (script-wired,
    # so the synced checkbox value is included in the submission).
    script = html.split("<script>", 1)[1]
    assert "requestSubmit" in script
    # requestSubmit fires the submit listener, preserving explicit false
    # instead of letting the server restore the single-day default of true.
    assert "form.addEventListener('submit'" in html
    assert "h.value = 'false'" in html
    assert ('<table' in html) == (details == "true")


class TestTOUPage(unittest.TestCase):
    """Integration of /api/v1/tou with the index dashboard look and feel."""

    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    def test_tou_html_defaults_to_today(self):
        """GET /api/v1/tou as HTML with no dates defaults to today from midnight."""
        with mock_config(MOCK=True):
            resp = self.app.get("/api/v1/tou", headers={"Accept": "text/html"})
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertIn('type="date"', html)
        self.assertIn('name="start_date"', html)
        self.assertIn('name="end_date"', html)

    def test_tou_json_still_requires_start_date(self):
        """JSON clients still get 400 when start_date is missing."""
        resp = self.app.get(
            "/api/v1/tou", headers={"Accept": "application/json"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_tou_html_has_nav_link_to_index(self):
        """TOU page links back to the index dashboard."""
        with mock_config(MOCK=True):
            resp = self.app.get(
                "/api/v1/tou?start_date=2026-01-01&end_date=2026-01-01T04:00:00",
                headers={"Accept": "text/html"},
            )
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertIn("../../", html)

    def test_index_html_links_to_tou(self):
        """Index dashboard links to the TOU report."""
        with mock_config():
            resp = self.app.get("/", headers={"Accept": "text/html"})
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertIn("api/v1/tou", html)

    def test_index_header_order(self):
        """Index header order: Solara, connection, spacer, TOU link."""
        with mock_config():
            resp = self.app.get("/", headers={"Accept": "text/html"})
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        header = html.split("<header", 1)[1].split("</header>", 1)[0]
        title_idx = header.index(">Solara</h1>")
        conn_idx = header.index('id="connection"')
        spacer_idx = header.index("app-bar__spacer")
        tou_idx = header.index(">TOU</a>")
        self.assertLess(title_idx, conn_idx)
        self.assertLess(conn_idx, spacer_idx)
        self.assertLess(spacer_idx, tou_idx)

    def test_tou_single_day_details_default_on_html(self):
        """Single-day HTML shows 15-min details by default."""
        with mock_config(MOCK=True):
            resp = self.app.get(
                "/api/v1/tou?start_date=2026-01-01&end_date=2026-01-01T04:00:00",
                headers={"Accept": "text/html"},
            )
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertIn("15-min", html)

    def test_tou_same_date_explicit_details(self) -> None:
        """Equal date-picker values return a day's data, not an empty report."""
        url = "/api/v1/tou?start_date=2026-09-16&end_date=2026-09-16&details=true"
        with mock_config(MOCK=True, TIMEZONE="America/Los_Angeles"):
            for accept in ("*/*", "application/json"):
                with self.subTest(accept=accept):
                    resp = self.app.get(url, headers={"Accept": accept})
                    self.assertEqual(resp.status_code, 200)
                    if accept == "application/json":
                        data = json.loads(resp.data)
                        self.assertEqual(len(data["periods"]), 96)
                    else:
                        self.assertIn("<table", resp.data.decode("utf-8"))

    def test_tou_multiday_details_default_off_html(self):
        """Multi-day HTML hides 15-min details by default."""
        with mock_config(MOCK=True):
            resp = self.app.get(
                "/api/v1/tou?start_date=2026-01-01&end_date=2026-01-05",
                headers={"Accept": "text/html"},
            )
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertNotIn("<table", html)
        self.assertNotIn("data-table", html)

    def test_tou_single_day_json_includes_periods_by_default(self):
        """Single-day JSON includes per-period details by default."""
        with mock_config(MOCK=True):
            resp = self.app.get(
                "/api/v1/tou?start_date=2026-01-01&end_date=2026-01-01T04:00:00",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("periods", data)
        self.assertTrue(len(data["periods"]) > 0)

    def test_tou_multiday_json_excludes_periods_by_default(self):
        """Multi-day JSON excludes per-period details by default."""
        with mock_config(MOCK=True):
            resp = self.app.get(
                "/api/v1/tou?start_date=2026-01-01&end_date=2026-01-05",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertNotIn("periods", data)

    def test_tou_explicit_details_overrides_default(self):
        """Explicit details param overrides the single/multi-day default."""
        with mock_config(MOCK=True):
            resp = self.app.get(
                "/api/v1/tou?start_date=2026-01-01&end_date=2026-01-05&details=true",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("periods", data)

        with mock_config(MOCK=True):
            resp = self.app.get(
                "/api/v1/tou?start_date=2026-01-01"
                "&end_date=2026-01-01T04:00:00&details=false",
                headers={"Accept": "application/json"},
            )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertNotIn("periods", data)
