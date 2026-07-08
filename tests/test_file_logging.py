"""Tests for file-based logging with rotation.

Verifies that _setup_file_logging creates a RotatingFileHandler when
LOG_FILE is configured, returns None when it isn't, and that the handler
uses the correct rotation parameters and formatter.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

import pytest

from config import Config


class TestSetupFileLogging:
    """_setup_file_logging() helper behavior."""

    def test_returns_none_when_log_file_unset(self):
        """No LOG_FILE configured → returns None."""
        from app import _setup_file_logging

        cfg = Config(overrides={})
        assert _setup_file_logging(cfg) is None

    def test_returns_none_when_log_file_empty(self):
        """LOG_FILE='' → returns None (empty string is falsy)."""
        from app import _setup_file_logging

        cfg = Config(overrides={"LOG_FILE": ""})
        assert _setup_file_logging(cfg) is None

    def test_returns_rotating_handler_when_log_file_set(self, tmp_path: Path):
        """LOG_FILE set → returns RotatingFileHandler."""
        from app import _setup_file_logging

        log_file = str(tmp_path / "test.log")
        cfg = Config(overrides={"LOG_FILE": log_file})
        handler = _setup_file_logging(cfg)

        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        handler.close()

    def test_handler_uses_config_max_bytes(self, tmp_path: Path):
        """Handler maxBytes matches LOG_MAX_BYTES config."""
        from app import _setup_file_logging

        log_file = str(tmp_path / "test.log")
        cfg = Config(overrides={
            "LOG_FILE": log_file,
            "LOG_MAX_BYTES": "5000",
        })
        handler = _setup_file_logging(cfg)

        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert handler.maxBytes == 5000
        handler.close()

    def test_handler_uses_config_backup_count(self, tmp_path: Path):
        """Handler backupCount matches LOG_BACKUP_COUNT config."""
        from app import _setup_file_logging

        log_file = str(tmp_path / "test.log")
        cfg = Config(overrides={
            "LOG_FILE": log_file,
            "LOG_BACKUP_COUNT": "3",
        })
        handler = _setup_file_logging(cfg)

        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert handler.backupCount == 3
        handler.close()

    def test_handler_defaults(self, tmp_path: Path):
        """Default maxBytes=10MB and backupCount=5 when not configured."""
        from app import _setup_file_logging

        log_file = str(tmp_path / "test.log")
        cfg = Config(overrides={"LOG_FILE": log_file})
        handler = _setup_file_logging(cfg)

        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert handler.maxBytes == 10_485_760
        assert handler.backupCount == 5
        handler.close()

    def test_formatter_includes_module_name(self, tmp_path: Path):
        """Formatter format string includes %(name)s for module identification."""
        from app import _setup_file_logging

        log_file = str(tmp_path / "test.log")
        cfg = Config(overrides={"LOG_FILE": log_file})
        handler = _setup_file_logging(cfg)

        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        formatter = handler.formatter
        assert formatter is not None
        assert "%(name)s" in formatter._fmt
        handler.close()

    def test_log_writes_to_file(self, tmp_path: Path):
        """Log messages appear in the configured file."""
        from app import _setup_file_logging

        log_file = str(tmp_path / "test.log")
        cfg = Config(overrides={"LOG_FILE": log_file})
        handler = _setup_file_logging(cfg)

        test_logger = logging.getLogger("test_file_logging")
        test_logger.addHandler(handler)
        try:
            test_logger.warning("test message")
            handler.flush()
            content = Path(log_file).read_text()
            assert "test message" in content
        finally:
            test_logger.removeHandler(handler)
            handler.close()

    def test_rotation_triggers(self, tmp_path: Path):
        """Log file rotates when maxBytes is exceeded."""
        from app import _setup_file_logging

        log_file = str(tmp_path / "test.log")
        cfg = Config(overrides={
            "LOG_FILE": log_file,
            "LOG_MAX_BYTES": "100",
            "LOG_BACKUP_COUNT": "2",
        })
        handler = _setup_file_logging(cfg)

        test_logger = logging.getLogger("test_rotation")
        test_logger.addHandler(handler)
        try:
            # Write enough to exceed 100 bytes and trigger rotation
            for i in range(20):
                test_logger.info("line %d padding to fill bytes", i)
            handler.flush()

            log_path = Path(log_file)
            backup = Path(log_file + ".1")
            assert log_path.exists(), "current log file should exist"
            assert backup.exists(), "backup file should exist after rotation"
        finally:
            test_logger.removeHandler(handler)
            handler.close()
