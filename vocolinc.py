"""
vocolinc.py — Unofficial Python library for controlling VOCOlinc smart plugs.

Reverse-engineered from the VOCOlinc iOS app. Uses AWS Cognito for auth,
AWS IoT Device Shadow for control, and VOCOlinc's own API for device discovery.

Dependencies:
    pip install boto3 requests pyjwt

Usage:
    client = VOCOlinc("you@example.com", "yourpassword")
    client.login()

    for device in client.devices.values():
        print(device)

    client.set_plug("floor lamp", on=True)
    print(client.get_plug("floor lamp"))   # True

    client.set_plug("floor lamp", on=False)
"""

from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass

import boto3
import jwt
import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants extracted from VOCOlinc app binary / mitmproxy capture
# ---------------------------------------------------------------------------

_REGION                 = "us-east-1"
_USER_POOL_ID           = "us-east-1_Keiu1peve"
_COGNITO_CLIENT_ID      = "2e23tbhavbpug3bqhr6o119ubv"
_IDENTITY_POOL_ID       = "us-east-1:a8d3ead4-4c38-cda5-b6f9-058a47b23979"
_IOT_ENDPOINT           = "https://a1i6tou42mzagf-ats.iot.us-east-1.amazonaws.com"
_VOCOLINC_API           = "https://cloud.awsiot.vocolinc.com"
_VOCOLINC_JWT_SECRET    = "7yIqIyl136RnId6oZOeuPA2cmR1dsPug"
_VOCOLINC_JWT_ISSUER    = "devicecloud.vocolinc.com/iot/registry"
_VOCOLINC_TOKEN_TTL     = 3600          # seconds; matches app behaviour
_AWS_CREDS_REFRESH_MARGIN = 300         # refresh AWS creds 5 min before expiry


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Device:
    """Represents a VOCOlinc smart plug device."""

    friendly_name: str
    thing_name: str          # e.g. "0d02d631_540e1e50"
    appliance_id: str        # e.g. "0d02d631_540e1e50_0"
    model: str               # e.g. "VP5", "SmartBar"
    serial: str
    device_id: str

    def __str__(self) -> str:
        return (
            f"Device(name={self.friendly_name!r}, "
            f"model={self.model}, thing={self.thing_name})"
        )


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class VOCOlinc:
    """
    Client for VOCOlinc smart plugs.

    Authentication is lazy: call login() explicitly, or let the first
    plug operation trigger it automatically.

    Tokens are refreshed automatically:
      - Cognito IdToken (1-hour TTL) via RefreshToken — no password re-use.
      - AWS temporary credentials (1-hour TTL) refreshed from IdToken.
      - VOCOlinc HS256 token generated locally from the hardcoded secret.
    """

    def __init__(self, username: str, password: str):
        self._username = username
        self._password = password

        # Cognito tokens
        self._id_token: str | None = None
        self._refresh_token: str | None = None
        self._id_token_exp: float = 0

        # AWS temporary credentials
        self._aws_creds: dict | None = None
        self._aws_creds_exp: float = 0

        # VOCOlinc HS256 token (generated locally)
        self._vocolinc_token: str | None = None
        self._vocolinc_token_exp: float = 0

        # Discovered devices, keyed by friendly_name (lowercase for lookup)
        self.devices: dict[str, Device] = {}

        # Lazily created IoT Data client
        self._iot_client = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def login(self) -> None:
        """
        Authenticate and populate self.devices.
        Safe to call multiple times — re-authenticates from scratch.
        """
        self._cognito_login()
        self._refresh_aws_creds()
        self._refresh_vocolinc_token()
        self._register_user()
        self._discover_devices()
        logger.info("Logged in. %d device(s) found.", len(self.devices))

    def set_plug(self, name: str, on: bool) -> None:
        """
        Turn a plug on (True) or off (False) by friendly name.
        Name matching is case-insensitive.
        """
        device = self._get_device(name)
        iot = self._get_iot_client()
        payload = json.dumps({"state": {"desired": {"switch0": on}}})
        iot.update_thing_shadow(thingName=device.thing_name, payload=payload)
        logger.debug("set_plug %r -> %s", name, on)

    def get_plug(self, name: str) -> bool:
        """
        Return the reported switch state of a plug (True = on).
        """
        device = self._get_device(name)
        iot = self._get_iot_client()
        response = iot.get_thing_shadow(thingName=device.thing_name)
        shadow = json.loads(response["payload"].read())
        return bool(shadow["state"]["reported"]["switch0"])

    def get_shadow(self, name: str) -> dict:
        """
        Return the full device shadow dict for advanced use
        (power monitoring, timers, firmware version, etc.).
        """
        device = self._get_device(name)
        iot = self._get_iot_client()
        response = iot.get_thing_shadow(thingName=device.thing_name)
        return json.loads(response["payload"].read())

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _cognito_login(self) -> None:
        """Full username/password auth. Stores IdToken and RefreshToken."""
        cognito = boto3.client("cognito-idp", region_name=_REGION)
        resp = cognito.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": self._username,
                "PASSWORD": self._password,
            },
            ClientId=_COGNITO_CLIENT_ID,
        )
        result = resp["AuthenticationResult"]
        self._store_cognito_tokens(result)

    def _cognito_refresh(self) -> None:
        """Refresh IdToken using the long-lived RefreshToken (no password needed)."""
        if not self._refresh_token:
            logger.debug("No refresh token — doing full login.")
            self._cognito_login()
            return
        cognito = boto3.client("cognito-idp", region_name=_REGION)
        resp = cognito.initiate_auth(
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": self._refresh_token},
            ClientId=_COGNITO_CLIENT_ID,
        )
        self._store_cognito_tokens(resp["AuthenticationResult"])

    def _store_cognito_tokens(self, result: dict) -> None:
        self._id_token = result["IdToken"]
        self._id_token_exp = time.time() + result.get("ExpiresIn", 3600) - 60
        if "RefreshToken" in result:
            self._refresh_token = result["RefreshToken"]

    def _ensure_id_token(self) -> str:
        if time.time() >= self._id_token_exp:
            logger.debug("IdToken expired — refreshing.")
            self._cognito_refresh()
        return self._id_token

    def _refresh_aws_creds(self) -> None:
        id_token = self._ensure_id_token()
        identity = boto3.client("cognito-identity", region_name=_REGION)
        resp = identity.get_credentials_for_identity(
            IdentityId=_IDENTITY_POOL_ID,
            Logins={
                f"cognito-idp.{_REGION}.amazonaws.com/{_USER_POOL_ID}": id_token
            },
        )
        creds = resp["Credentials"]
        self._aws_creds = creds
        # Expiration is a datetime; convert to epoch float
        self._aws_creds_exp = (
            creds["Expiration"].timestamp() - _AWS_CREDS_REFRESH_MARGIN
        )
        self._iot_client = None  # force rebuild with new creds

    def _ensure_aws_creds(self) -> dict:
        if not self._aws_creds or time.time() >= self._aws_creds_exp:
            logger.debug("AWS creds expired — refreshing.")
            self._refresh_aws_creds()
        return self._aws_creds

    def _refresh_vocolinc_token(self) -> None:
        now = int(time.time())
        self._vocolinc_token = jwt.encode(
            {
                "iss": _VOCOLINC_JWT_ISSUER,
                "sub": "userId",
                "jti": self._username,
                "iat": now,
                "exp": now + _VOCOLINC_TOKEN_TTL,
            },
            _VOCOLINC_JWT_SECRET,
            algorithm="HS256",
            headers={"kid": "none"},
        )
        self._vocolinc_token_exp = now + _VOCOLINC_TOKEN_TTL - 60

    def _ensure_vocolinc_token(self) -> str:
        if not self._vocolinc_token or time.time() >= self._vocolinc_token_exp:
            logger.debug("VOCOlinc token expired — regenerating.")
            self._refresh_vocolinc_token()
        return self._vocolinc_token

    # ------------------------------------------------------------------
    # VOCOlinc API calls
    # ------------------------------------------------------------------

    def _vocolinc_post(self, path: str, data: dict) -> dict:
        """POST to the VOCOlinc cloud API with the HS256 token."""
        data["idToken"] = self._ensure_vocolinc_token()
        resp = requests.post(
            f"{_VOCOLINC_API}{path}",
            data=data,
            headers={"User-Agent": "VOCOlinc/3.0 CFNetwork/3860.400.51 Darwin/25.3.0"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _register_user(self) -> str:
        """Register/validate the user session. Returns ownerId."""
        result = self._vocolinc_post("/iot/register_user", {})
        owner_id = result.get("ownerId")
        logger.debug("register_user ownerId=%s", owner_id)
        return owner_id

    def _discover_devices(self) -> None:
        """Populate self.devices from the VOCOlinc device list."""
        result = self._vocolinc_post("/iot/discover_device", {"region": "1"})
        self.devices = {}
        for d in result.get("devices", []):
            appliance_id = d["applianceId"]           # e.g. "0d02d631_540e1e50_0"
            thing_name = appliance_id.rsplit("_", 1)[0]  # strip trailing "_0"
            device = Device(
                friendly_name=d["friendlyName"],
                thing_name=thing_name,
                appliance_id=appliance_id,
                model=d.get("modelName", ""),
                serial=d.get("serial", ""),
                device_id=d.get("deviceId", ""),
            )
            self.devices[d["friendlyName"].lower()] = device
        logger.debug("Discovered %d device(s).", len(self.devices))

    # ------------------------------------------------------------------
    # IoT client
    # ------------------------------------------------------------------

    def _get_iot_client(self):
        creds = self._ensure_aws_creds()
        if self._iot_client is None:
            self._iot_client = boto3.client(
                "iot-data",
                region_name=_REGION,
                endpoint_url=_IOT_ENDPOINT,
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretKey"],
                aws_session_token=creds["SessionToken"],
            )
        return self._iot_client

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_device(self, name: str) -> Device:
        if not self.devices:
            logger.debug("No devices loaded — triggering login.")
            self.login()
        key = name.lower()
        if key not in self.devices:
            available = ", ".join(sorted(self.devices.keys()))
            raise KeyError(
                f"Device {name!r} not found. Available: {available}"
            )
        return self.devices[key]


# ---------------------------------------------------------------------------
# CLI for quick testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import os

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="VOCOlinc plug controller")
    parser.add_argument("--user", default=os.environ.get("VOCOLINC_USER"))
    parser.add_argument("--password", default=os.environ.get("VOCOLINC_PASSWORD"))
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="List all devices")

    p_on = sub.add_parser("on", help="Turn a plug on")
    p_on.add_argument("name")

    p_off = sub.add_parser("off", help="Turn a plug off")
    p_off.add_argument("name")

    p_status = sub.add_parser("status", help="Get plug state")
    p_status.add_argument("name")

    args = parser.parse_args()

    if not args.user or not args.password:
        parser.error(
            "Provide --user/--password or set VOCOLINC_USER/VOCOLINC_PASSWORD"
        )

    client = VOCOlinc(args.user, args.password)
    client.login()

    if args.cmd == "list":
        for device in sorted(client.devices.values(), key=lambda d: d.friendly_name):
            print(device)

    elif args.cmd == "on":
        client.set_plug(args.name, on=True)
        print(f"{args.name}: on")

    elif args.cmd == "off":
        client.set_plug(args.name, on=False)
        print(f"{args.name}: off")

    elif args.cmd == "status":
        state = client.get_plug(args.name)
        print(f"{args.name}: {'on' if state else 'off'}")

    else:
        parser.print_help()
