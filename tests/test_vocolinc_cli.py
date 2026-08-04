"""Tests for the standalone vocolinc.py command-line interface."""

import pytest

from vocolinc import _build_parser, main


def test_cli_parser_defaults_from_app_env_var_names(monkeypatch):
    """--user/--password default to VOCOLINC_USERNAME/VOCOLINC_PASSWORD.

    Regression guard: the app reads VOCOLINC_USERNAME via
    Config.vocolinc_username; the CLI must use the same names (not the
    legacy VOCOLINC_USER) or manual use misleads users.
    """
    monkeypatch.setenv("VOCOLINC_USERNAME", "cli-user")
    monkeypatch.setenv("VOCOLINC_PASSWORD", "cli-pass")

    args = _build_parser().parse_args([])

    assert args.user == "cli-user"
    assert args.password == "cli-pass"


def test_cli_parser_ignores_legacy_env_var_name(monkeypatch):
    """VOCOLINC_USER alone is not enough (wrong name)."""
    monkeypatch.setenv("VOCOLINC_USER", "legacy-user")
    monkeypatch.delenv("VOCOLINC_USERNAME", raising=False)
    monkeypatch.delenv("VOCOLINC_PASSWORD", raising=False)

    args = _build_parser().parse_args([])

    assert args.user is None
    assert args.password is None


def test_cli_errors_without_credentials(monkeypatch, capsys):
    """Missing credentials exit(2) and name the correct env vars."""
    monkeypatch.delenv("VOCOLINC_USERNAME", raising=False)
    monkeypatch.delenv("VOCOLINC_PASSWORD", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        main(["list"])

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "VOCOLINC_USERNAME" in err
    assert "VOCOLINC_PASSWORD" in err
