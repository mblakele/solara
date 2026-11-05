"""Emporia VUE session, authentication, and the metrics base class.

Split out of ``metrics.py`` (architecture-v2 Phase E.1). ``metrics.py``
re-exports these names so existing imports keep working.
"""

from __future__ import annotations

from datetime import timedelta
import json
import locale
import logging
import threading
from typing import Any, ClassVar, Optional

import requests
from pyemvue import PyEmVue

from clock import Clock, RealClock
from config import Config, _config
from util import CustomJSONProvider, RetryableError


logger = logging.getLogger(__name__)

_CLOCK: Clock = RealClock()

# Module-level shared VUE session (architecture-v2 Phase A.2): one
# authenticated PyEmVue client reused by all MetricsBase instances unless a
# caller injects a stub session per instance (tests / embedders).
_SHARED_VUE: PyEmVue = PyEmVue()

# Serializes MetricsBase auth/init (vue.login + get_devices) across the
# load-management fetch thread and TOU request threads (plan 2.4).
_vue_init_lock = threading.Lock()


def set_clock(clock: Clock) -> None:
    """Override the module-level clock for testing.

    Args:
        clock: A ``Clock`` instance (typically ``FakeClock`` in tests).
    """
    global _CLOCK  # noqa: PLW0603
    _CLOCK = clock


class VueAuthenticationError(Exception):
    """Raised when Vue authentication fails."""


class RetryableMetricsException(RetryableError):
    """Signal that the Emporia VUE API responded with a server error and the caller should retry."""

    def __init__(self, message, *args):
        self.message = message
        self.instant = _CLOCK.now()
        super().__init__(message, *args)


class MetricsBase:
    """
    Base class handling PyEmVue connection and authentication only.

    Each instance owns its PyEmVue session (``vue``), auth state (``vue_auth``)
    and discovered ``device_info``; production shares one session via the
    module-level default, tests inject stub sessions per instance (A.2).
    """

    device_info: dict[int, Any]
    json: ClassVar[type] = CustomJSONProvider
    vue_keys: ClassVar[str] = ".vue-keys.json"

    def __init__(
        self,
        logger_next: Optional[logging.Logger] = None,
        config: Config | None = None,
        vue: Optional[PyEmVue] = None,
        vue_auth: Optional[dict[str, Any]] = None,
        device_info: Optional[dict[int, Any]] = None,
    ) -> None:
        self._cfg = config if config is not None else _config
        self.logger = logger_next or logger
        self.vue = vue if vue is not None else _SHARED_VUE
        self.vue_auth = vue_auth if vue_auth is not None else {}
        self.device_info = device_info if device_info is not None else {}
        self.vue_init()
        self.get_device_info()

    def vue_init(self) -> None:
        """
        Initialize access to Emporia VUE API.
        Prefer stored authentication token,
        falling back on username and password.

        Serialized by ``_vue_init_lock`` so concurrent callers (fetch
        thread vs TOU requests) cannot race ``vue.login()`` or pyemvue's
        token-file writes.
        """

        if not self.vue:
            return

        cfg = getattr(self, '_cfg', _config)

        with _vue_init_lock:
            try:
                encoding = locale.getpreferredencoding()
                vkf = open(self.vue_keys, encoding=encoding)
                with vkf:
                    vkf_data = json.load(vkf)
                    login_ok = self.vue.login(
                        id_token=vkf_data["id_token"],
                        access_token=vkf_data["access_token"],
                        refresh_token=vkf_data["refresh_token"],
                        token_storage_file=self.vue_keys,
                    )
            except (
                requests.exceptions.RequestException,
                IOError,
                KeyError,
                ValueError,
            ):
                # ValueError covers json.JSONDecodeError from a corrupt
                # .vue-keys.json; KeyError covers missing token fields.
                # Both mean the stored tokens are unusable: fall back to
                # password auth instead of crashing the caller.
                self.logger.exception("keys failed: will use password")
                try:
                    login_ok = self.vue.login(
                        username=cfg.vue_username,
                        password=cfg.vue_password,
                        token_storage_file=self.vue_keys,
                    )
                except Exception as inner_ex:
                    raise VueAuthenticationError(
                        "Vue authentication failed: check credentials"
                    ) from inner_ex

            if not login_ok:
                # Token login returned False — fall back to password auth.
                self.logger.debug("token login failed, trying password")
                try:
                    login_ok = self.vue.login(
                        username=cfg.vue_username,
                        password=cfg.vue_password,
                        token_storage_file=self.vue_keys,
                    )
                except Exception as inner_ex:
                    raise VueAuthenticationError(
                        "Vue authentication failed: check credentials"
                    ) from inner_ex

            if not login_ok:
                self.logger.error("login failed")
                raise VueAuthenticationError(
                    "Vue authentication failed: check credentials"
                )

            self.vue_auth["last"] = _CLOCK.now()

    def get_device_info(self) -> None:
        """
        Wrapper for vue get_devices,
        filtering results for ZIG001 devices.

        Serialized by ``_vue_init_lock`` together with :meth:`vue_init`
        so device discovery cannot interleave with re-authentication.
        """
        with _vue_init_lock:
            rt_start = _CLOCK.now()
            age_limit = timedelta(hours=24)
            self.logger.debug(
                {"device_info_len": len(self.device_info), "vue_auth": self.vue_auth}
            )
            if len(self.device_info) > 0 and "last" in self.vue_auth:
                age = rt_start - self.vue_auth["last"]
                #self.logger.debug({"age": age})
                if age < age_limit:
                    #self.logger.debug({"device_info": self.device_info})
                    return

            try:
                devices = [self.vue.get_devices()[-1]]
            except requests.exceptions.HTTPError as ex:
                if ex.response is not None and ex.response.status_code == 401:
                    self.logger.exception("invalidating auth tokens")
                    self.vue.auth = None
                else:
                    self.logger.exception(ex)
                raise RetryableMetricsException("get_devices failed") from ex

            for vdi in devices:
                self.logger.debug(
                    "device %s, connected %s, model %s, channels %d",
                    vdi.device_gid,
                    vdi.connected,
                    vdi.model,
                    len(vdi.channels),
                )
                if not vdi.connected:
                    continue
                if not vdi.model == "ZIG001":
                    continue
                if not len(vdi.channels) > 0:
                    continue
                if not vdi.device_gid in self.device_info:
                    self.device_info[vdi.device_gid] = vdi
                    break
