"""Tests for Config dependency injection — no module-level _cfg patching needed."""

from config import Config
from load_manager import LoadManager, LoadManagerConfig


class TestConfigOverrides:
    """Config(overrides={...}) takes precedence over env/decouple."""

    def test_timezone_override(self):
        """Overrides TIMEZONE via dict."""
        cfg = Config(overrides={"TIMEZONE": "Europe/London"})
        assert cfg.timezone == "Europe/London"

    def test_dry_run_override(self):
        """Overrides LOAD_MANAGE_DRY_RUN via dict."""
        cfg = Config(overrides={"LOAD_MANAGE_DRY_RUN": "True"})
        assert cfg.dry_run is True

        cfg = Config(overrides={"LOAD_MANAGE_DRY_RUN": "False"})
        assert cfg.dry_run is False

    def test_load_manage_enabled_true(self):
        """Overrides LOAD_MANAGE_ENABLED to True."""
        cfg = Config(overrides={"LOAD_MANAGE_ENABLED": "True"})
        assert cfg.load_manage_enabled is True

    def test_load_manage_enabled_time_range(self):
        """Overrides LOAD_MANAGE_ENABLED with time range string."""
        cfg = Config(overrides={"LOAD_MANAGE_ENABLED": "06:45-15:00"})
        assert cfg.load_manage_enabled == "06:45-15:00"

    def test_load_target_wh_override(self):
        """Overrides LOAD_TARGET_WH via dict."""
        cfg = Config(overrides={"LOAD_TARGET_WH": "-1000"})
        assert cfg.load_target_wh == -1000

    def test_vue_credentials_override(self):
        """Overrides VUE_USERNAME and VUE_PASSWORD."""
        cfg = Config(overrides={
            "VUE_USERNAME": "test_user",
            "VUE_PASSWORD": "test_pass",
        })
        assert cfg.vue_username == "test_user"
        assert cfg.vue_password == "test_pass"

    def test_vue_credentials_empty_override_returns_none(self):
        """Empty string override returns None (not configured)."""
        cfg = Config(overrides={
            "VUE_USERNAME": "",
            "VUE_PASSWORD": "",
        })
        assert cfg.vue_username is None
        assert cfg.vue_password is None

    def test_plugin_controller_override(self):
        """Overrides LOAD_PLUG_CONTROLLER and LOAD_TESLA_CONTROLLER."""
        cfg = Config(overrides={
            "LOAD_PLUG_CONTROLLER": "real",
            "LOAD_TESLA_CONTROLLER": "real",
        })
        assert cfg.load_plug_controller == "real"
        assert cfg.load_tesla_controller == "real"

    def test_plugin_controller_override_case_insensitive(self):
        """Overrides are lowercased."""
        cfg = Config(overrides={
            "LOAD_PLUG_CONTROLLER": "REAL",
            "LOAD_TESLA_CONTROLLER": "REAL",
        })
        assert cfg.load_plug_controller == "real"
        assert cfg.load_tesla_controller == "real"

    def test_tesla_region_override(self):
        """Overrides TESLA_REGION."""
        cfg = Config(overrides={"TESLA_REGION": "eu"})
        assert cfg.tesla_region == "eu"

    def test_bool_default_false(self):
        """Unset bool properties return False by default."""
        cfg = Config()
        assert cfg.is_mock_mode is False
        assert cfg.is_mock_error is False
        assert cfg.debug is False

    def test_bool_override_true(self):
        """Setting a bool override to a truthy string returns True."""
        cfg = Config(overrides={"MOCK": "True"})
        assert cfg.is_mock_mode is True

    def test_bool_override_false(self):
        """Setting a bool override to 'False' returns False."""
        cfg = Config(overrides={"MOCK": "False"})
        assert cfg.is_mock_mode is False

    def test_int_override(self):
        """Integer config values are parsed correctly from override."""
        cfg = Config(overrides={"LOAD_MANAGE_INTERVAL_SECS": "60"})
        assert cfg.load_manage_interval_secs == 60

    def test_vocolinc_override(self):
        """Overrides VOCOLINC_USERNAME and VOCOLINC_PASSWORD."""
        cfg = Config(overrides={
            "VOCOLINC_USERNAME": "voco_user",
            "VOCOLINC_PASSWORD": "voco_pass",
        })
        assert cfg.vocolinc_username == "voco_user"
        assert cfg.vocolinc_password == "voco_pass"

    def test_debug_override(self):
        """Overrides DEBUG."""
        cfg = Config(overrides={"DEBUG": "True"})
        assert cfg.debug is True


class TestLoadManagerDI:
    """LoadManager accepts injected Config via LoadManagerConfig."""

    def test_dry_run_from_injected_config(self):
        """LoadManager.dry_run comes from injected Config."""
        cfg = Config(overrides={"LOAD_MANAGE_DRY_RUN": "True"})
        mgr = LoadManager(LoadManagerConfig(config=cfg, dry_run=None))
        assert mgr.dry_run is True

    def test_dry_run_false_from_injected_config(self):
        """LoadManager.dry_run=False from injected Config."""
        cfg = Config(overrides={"LOAD_MANAGE_DRY_RUN": "False"})
        mgr = LoadManager(LoadManagerConfig(config=cfg, dry_run=None))
        assert mgr.dry_run is False

    def test_target_wh_from_config_kwarg(self):
        """LoadManager.target_wh comes from explicit kwarg, not Config."""
        cfg = Config(overrides={"LOAD_TARGET_WH": "-999"})
        mgr = LoadManager(LoadManagerConfig(config=cfg, target_wh=-500))
        assert mgr.target_wh == -500

    def test_target_wh_fallback_to_device_config(self):
        """When target_wh is not set via kwarg and no override, falls back."""
        mgr = LoadManager(LoadManagerConfig())
        assert isinstance(mgr.target_wh, int)

    def test_enabled_from_injected_config(self):
        """LoadManager.enabled picks up True from injected Config."""
        cfg = Config(overrides={"LOAD_MANAGE_ENABLED": "True"})
        mgr = LoadManager(LoadManagerConfig(config=cfg, enabled=None))
        assert mgr.enabled is True

    def test_enabled_false_from_injected_config(self):
        """LoadManager.enabled picks up False from injected Config."""
        cfg = Config(overrides={"LOAD_MANAGE_ENABLED": "False"})
        mgr = LoadManager(LoadManagerConfig(config=cfg, enabled=None))
        assert mgr.enabled is False

    def test_nbc_device_from_config_kwarg(self):
        """LoadManager.nbc_device comes from explicit kwarg."""
        mgr = LoadManager(LoadManagerConfig(nbc_device="test_device"))
        assert mgr.nbc_device == "test_device"

    def test_backward_compat_no_config(self):
        """LoadManager() without LoadManagerConfig still works."""
        mgr = LoadManager(dry_run=True, target_wh=-500, enabled=True)
        assert mgr.dry_run is True
        assert mgr.target_wh == -500
        assert mgr.enabled is True

    def test_load_manager_with_empty_config(self):
        """LoadManager(LoadManagerConfig()) with no overrides uses defaults."""
        mgr = LoadManager(LoadManagerConfig())
        assert mgr.dry_run is True  # conftest sets LOAD_MANAGE_DRY_RUN=True
        assert isinstance(mgr.target_wh, int)
