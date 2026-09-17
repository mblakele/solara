"""TOUReporter must not write raw-data debug files to the working directory."""

from datetime import datetime, timezone
from unittest.mock import patch

from config import Config
from metrics import MetricsBase, TOUReporter


def test_tou_reporter_writes_no_files_in_debug_mode(tmp_path, monkeypatch):
    """Constructing a TOUReporter with DEBUG=True leaves the cwd empty."""
    monkeypatch.chdir(tmp_path)
    Config().set("DEBUG", "True")
    try:
        with (
                    patch.object(MetricsBase, "vue_init", return_value=None),
                    patch.object(MetricsBase, "get_device_info", return_value=None),
                    patch.object(MetricsBase, "device_info", {}),
                ):
            TOUReporter(
                datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
                datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            )
    finally:
        Config().set("DEBUG", "False")
    assert list(tmp_path.iterdir()) == []
