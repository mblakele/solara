"""Tests for structured log formatting (plan subtask 1.2).

The codebase logs rich context via ``extra={"event": ...}`` dicts (see
tests/test_structured_logging.py), but standard ``logging`` formatters
ignore those attributes — production output loses them. These tests pin
the contract of ``logfmt.StructuredFormatter`` / ``logfmt.JsonFormatter``
and their wiring into app logging handlers.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

from logfmt import JsonFormatter, StructuredFormatter, create_formatter


def _record(msg: str, **extra) -> logging.LogRecord:
    """Build a LogRecord with caller-supplied extra attributes."""
    record = logging.LogRecord(
        name="load_manager",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestStructuredFormatter:
    """Default text mode appends extras as [key=value ...]."""

    def test_extras_appended_as_key_value_pairs(self):
        fmt = StructuredFormatter("%(levelname)s %(message)s")
        out = fmt.format(_record("action decided", event="action", gap_wh=12.5))
        assert "action decided" in out
        assert "event=action" in out
        assert "gap_wh=12.5" in out

    def test_plain_record_has_no_suffix(self):
        """Records without extras render exactly like a plain formatter."""
        plain = logging.Formatter("%(levelname)s %(message)s")
        structured = StructuredFormatter("%(levelname)s %(message)s")
        out = structured.format(_record("hello world"))
        assert out == plain.format(_record("hello world"))
        assert "[" not in out

    def test_reserved_attributes_not_rendered(self):
        """Standard LogRecord attributes must not leak into the suffix."""
        fmt = StructuredFormatter("%(message)s")
        out = fmt.format(_record("hello"))
        assert "name=" not in out
        assert "thread=" not in out

    def test_exception_traceback_preserved(self):
        """exc_info tracebacks survive alongside the extras suffix."""
        fmt = StructuredFormatter("%(levelname)s %(message)s")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            record = _record("failed", event="cycle_error")
            import sys

            record.exc_info = sys.exc_info()
        out = fmt.format(record)
        assert "Traceback" in out
        assert "RuntimeError: boom" in out
        assert "event=cycle_error" in out

    def test_dict_values_rendered_as_json(self):
        """Dict/list extras (e.g. timings) render readably."""
        fmt = StructuredFormatter("%(message)s")
        out = fmt.format(
            _record("done", timings={"nbc_fetch": 0.5})
        )
        assert '"nbc_fetch"' in out or "nbc_fetch" in out
        assert "0.5" in out


class TestJsonFormatter:
    """LOG_FORMAT=json emits one JSON object per record."""

    def test_emits_valid_json_with_core_fields(self):
        fmt = JsonFormatter()
        payload = json.loads(fmt.format(_record("hello", event="action")))
        assert payload["message"] == "hello"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "load_manager"
        assert payload["event"] == "action"

    def test_json_includes_exc_info_string(self):
        fmt = JsonFormatter()
        try:
            raise ValueError("bad")
        except ValueError:
            record = _record("failed")
            import sys

            record.exc_info = sys.exc_info()
        payload = json.loads(fmt.format(record))
        assert "ValueError: bad" in payload["exc_info"]


class TestCreateFormatter:
    """create_formatter() selects text vs JSON mode."""

    def test_default_is_structured_text(self):
        assert isinstance(create_formatter(False), StructuredFormatter)

    def test_json_mode_returns_json_formatter(self):
        assert isinstance(create_formatter(True), JsonFormatter)


class TestAppLoggingWiring:
    """app handlers use the structured formatters."""

    def test_file_logging_uses_structured_formatter(self, tmp_path, monkeypatch):
        """_setup_file_logging attaches StructuredFormatter by default."""
        import app as app_mod
        from config import _config

        log_path = tmp_path / "solara.log"
        monkeypatch.setenv("LOG_FILE", str(log_path))
        monkeypatch.delenv("LOG_FORMAT", raising=False)

        handler = app_mod._setup_file_logging(_config)
        try:
            assert handler is not None
            assert isinstance(handler.formatter, StructuredFormatter)
        finally:
            handler.close()

    def test_file_logging_json_mode(self, tmp_path, monkeypatch):
        """LOG_FORMAT=json selects JsonFormatter for the file handler."""
        import app as app_mod
        from config import _config

        log_path = tmp_path / "solara.jsonl"
        monkeypatch.setenv("LOG_FILE", str(log_path))
        monkeypatch.setenv("LOG_FORMAT", "json")

        handler = app_mod._setup_file_logging(_config)
        try:
            assert handler is not None
            assert isinstance(handler.formatter, JsonFormatter)
        finally:
            handler.close()

    def test_end_to_end_extras_reach_handler_output(self, tmp_path):
        """A real logger wired with StructuredFormatter renders extras."""
        logger = logging.getLogger("test_logfmt_e2e")
        logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler()
        handler.setFormatter(StructuredFormatter("%(levelname)s %(message)s"))
        logger.addHandler(handler)
        try:
            with patch.object(handler, "stream") as mock_stream:
                logger.info(
                    "cycle_complete",
                    extra={"event": "cycle_complete", "gap_wh": -3.2},
                )
                written = "".join(
                    call.args[0]
                    for call in mock_stream.write.call_args_list
                )
        finally:
            logger.removeHandler(handler)
        assert "gap_wh=-3.2" in written
        assert "event=cycle_complete" in written


class TestJsonFormatterKeyCollisions:
    """Caller extras must never clobber the JSON envelope keys (review #5).

    Note: extras named after RESERVED LogRecord attrs (``message``,
    ``exc_info``, ...) never reach the payload at all — ``_extra_fields``
    drops them. Only unreserved envelope keys (``ts``, ``level``,
    ``logger``) can actually collide, so those get the ``x_`` prefix.
    """

    COLLIDERS = {
        "ts": "FAKE-ts",
        "level": "FAKE-level",
        "logger": "FAKE-logger",
        "message": "FAKE-message",
    }

    def test_envelope_keys_win_and_colliders_are_prefixed(self) -> None:
        record = _record("hello", **self.COLLIDERS)
        payload = json.loads(JsonFormatter().format(record))
        assert payload["message"] == "hello"  # reserved: extra dropped
        assert payload["level"] == "INFO"
        assert payload["logger"] == "load_manager"
        assert payload["ts"] != "FAKE-ts"  # real ISO timestamp
        for key in ("ts", "level", "logger"):
            assert payload[f"x_{key}"] == self.COLLIDERS[key], (
                f"colliding extra {key!r} must be preserved as x_{key}"
            )
        assert "x_message" not in payload  # reserved attr, filtered upstream

    def test_non_colliding_extras_unchanged(self) -> None:
        record = _record("hello", event="cycle", gap_wh=-12.5)
        payload = json.loads(JsonFormatter().format(record))
        assert payload["event"] == "cycle"
        assert payload["gap_wh"] == -12.5
