# Fleet Telemetry Docker Setup

This documents the setup and troubleshooting of the fleet-telemetry Docker stack for Solara, which streams Tesla vehicle telemetry via MQTT to the Solara load manager.

## Architecture

```
Tesla Vehicle
     │  (TLS WebSocket, port 4443)
     ▼
fleet-telemetry container
     │  (MQTT publish, host:port)
     ▼
mosquitto container  (port 1883)
     │  (paho subscribe)
     ▼
Solara (mqtt_telemetry.py)
```

## Prerequisites

- A publicly reachable hostname with a valid TLS certificate (Let's Encrypt works)
- Port 4443 forwarded through your router to the host running Docker
- Tesla Fleet API credentials and a registered fleet private key


## Directory Layout

```
~/solara-prod/fleet-telemetry/
├── compose.yaml
├── config.json          # fleet-telemetry server config
└── mosquitto.conf       # mosquitto broker config
```

TLS certificates are expected at `~/tesla-certs/`:

```
~/tesla-certs/
├── fullchain.pem
└── privkey.pem
```

The fleet private key used by `tesla-http-proxy` is expected at `/etc/tesla/private-key.pem`.


## Configuration Files

### `compose.yaml`

```yaml
services:
  mosquitto:
    image: eclipse-mosquitto:2
    ports:
      - 127.0.0.1:1883:1883
    volumes:
      - ./mosquitto.conf:/mosquitto/config/mosquitto.conf:ro
    restart: unless-stopped

  fleet-telemetry:
    image: tesla/fleet-telemetry:v0.9.0
    command:
      - /fleet-telemetry
      - -config=/etc/fleet-telemetry/config.json
    ports:
      - 4443:4443
    volumes:
      - ./config.json:/etc/fleet-telemetry/config.json:ro
      - ~/tesla-certs/fullchain.pem:/certs/fullchain.pem:ro
      - ~/tesla-certs/privkey.pem:/certs/privkey.pem:ro
    restart: unless-stopped

  tesla_http_proxy:
    image: tesla/vehicle-command:0.4.1
    command:
      - /tesla-http-proxy
    ports:
      - 4444:4444
    environment:
      - TESLA_HTTP_PROXY_TLS_CERT=/certs/fullchain.pem
      - TESLA_HTTP_PROXY_TLS_KEY=/certs/privkey.pem
      - TESLA_HTTP_PROXY_HOST=0.0.0.0
      - TESLA_HTTP_PROXY_PORT=4444
      - TESLA_HTTP_PROXY_TIMEOUT=10s
      - TESLA_KEY_FILE=/config/fleet-key.pem
      - TESLA_VERBOSE=true
    volumes:
      - ~/tesla-certs/fullchain.pem:/certs/fullchain.pem:ro
      - ~/tesla-certs/privkey.pem:/certs/privkey.pem:ro
      - /etc/tesla/private-key.pem:/config/fleet-key.pem:ro
    restart: unless-stopped
```

All three services include `restart: unless-stopped` so they recover automatically after crashes or host reboots.

### `config.json`

```json
{
    "host": "0.0.0.0",
    "port": 4443,
    "log_level": "info",
    "json_log_enable": true,
    "namespace": "Solara",
    "reliable_ack": true,
    "transmit_decoded_records": true,
    "records": {
        "V": ["mqtt"],
        "alerts": ["mqtt"],
        "errors": ["mqtt"],
        "connectivity": ["mqtt"]
    },
    "mqtt": {
        "broker": "mosquitto:1883",
        "client_id": "fleet-telemetry",
        "topic_base": "telemetry"
    },
    "tls": {
        "server_cert": "/certs/fullchain.pem",
        "server_key": "/certs/privkey.pem"
    }
}
```

**Critical:** the `broker` field must be `"host:port"` with no URL scheme. Using `"tcp://mosquitto:1883"` causes the MQTT publisher to silently fail to initialise — fleet-telemetry will receive vehicle data but publish nothing to mosquitto, with no error logged at startup.

The `records` section routes all four record types to MQTT. Omitting `alerts`, `errors`, and `connectivity` causes fleet-telemetry to count those records as dispatch errors, producing a fixed `"errors": 33` in every `socket_disconnected` log entry regardless of session length.

### `mosquitto.conf`

```
listener 1883
allow_anonymous true
```

mosquitto 2.x defaults to denying anonymous connections, so `allow_anonymous true` is required. Since port 1883 is bound to `127.0.0.1` in `compose.yaml`, this is not exposed to the public internet.


## Provisioning

Before the vehicle will stream telemetry, you must register the fleet-telemetry server with Tesla. Solara handles this via its `--provision-fleet-telemetry` command, which calls the Tesla Fleet API with the server hostname and the fields to stream:

```json
{
    "hostname": "<your-public-hostname>",
    "fields": {
        "DetailedChargeState": {"interval_seconds": 15},
        "ChargeAmps":          {"interval_seconds": 15},
        "ChargeState":         {"interval_seconds": 15},
        "Location":            {"interval_seconds": 120}
    }
}
```

On success, Solara writes a timestamp to `~/.solara-fleet-telemetry`. At startup, Solara checks for this file and warns if it is absent:

```
[WARNING] mqtt_telemetry: fleet-telemetry dotfile not found (~/.solara-fleet-telemetry) —
          vehicle may not be provisioned; run --provision-fleet-telemetry
```

Provisioning is async on Tesla's backend — allow a few minutes after running it before expecting the vehicle to connect.


## Startup Sequence

A healthy startup produces these log lines in order:

```
[INFO] mqtt_telemetry: fleet-telemetry provisioned at 2026-06-04T16:36:26+00:00 (~/.solara-fleet-telemetry)
[INFO] mqtt_telemetry: subscriber thread started host=localhost port=1883 topic=telemetry/#
[INFO] mqtt_telemetry: connected to localhost:1883, subscribing to telemetry/#
```

After the vehicle wakes and connects (may take seconds to minutes):

```
[INFO] mqtt_telemetry: first value for field ChargeState = 'Disconnected'
[INFO] mqtt_telemetry: first value for field Location = {'latitude': ..., 'longitude': ...}
```

Until those field lines appear, `tesla_state=None` in cycle diagnostics is expected. If they never appear, see Troubleshooting below.


## Verifying the Pipeline

```bash
# Is the vehicle connecting to fleet-telemetry?
sudo docker logs fleet-telemetry_fleet-telemetry_1 2>&1 | grep socket_connected | tail -5

# Is fleet-telemetry publishing to mosquitto?
mosquitto_sub -h localhost -p 1883 -t 'telemetry/#' -v -C 10

# Is port 4443 reachable from the internet?
# Run from outside your network (phone on LTE, VPS, etc):
curl -v https://<your-public-hostname>:4443/
```

A healthy `socket_disconnected` entry looks like:

```json
{"V":"106", "duration_sec":623, "errors":"0", "total":"139", "msg":"socket_disconnected"}
```


## Troubleshooting

### `telemetry_registered=False` in cycle diagnostics

Solara has not yet received any MQTT messages. Check in order:

1. Does `~/.solara-fleet-telemetry` exist? If not, run `--provision-fleet-telemetry`.
2. Is fleet-telemetry running? `docker ps | grep fleet-telemetry`
3. Is the vehicle connecting? Check `docker logs` for `socket_connected`.
4. Is mosquitto receiving publishes? Run `mosquitto_sub -h localhost -p 1883 -t '#' -v -C 5`.

### Vehicle connects but no MQTT messages arrive

Symptoms: `socket_connected` appears in fleet-telemetry logs, mosquitto receives nothing.

Most likely cause: the `broker` field in `config.json` uses a `tcp://` URL scheme instead of bare `host:port`. Fleet-telemetry silently fails to initialise the MQTT producer.

Fix:
```json
"broker": "mosquitto:1883"   ✓
"broker": "tcp://mosquitto:1883"   ✗
```

### Fixed `"errors": 33` in every `socket_disconnected`

The `records` section in `config.json` is missing `alerts`, `errors`, and/or `connectivity`. Fleet-telemetry receives those record types from the vehicle but has no dispatcher configured for them, and counts each as an error.

Fix: route all record types to `mqtt` (or `logger` if you don't need them in MQTT).

### `socket_disconnected` with `duration_sec` ~600, no errors

Normal — the vehicle went to sleep after ~10 minutes idle. Fleet-telemetry will reconnect when the vehicle wakes.

### fleet-telemetry crashes and does not restart

Ensure `restart: unless-stopped` is set in `compose.yaml` for the `fleet-telemetry` service. Apply without restarting other services:

```bash
docker compose up -d fleet-telemetry
```

### Provisioning succeeded but no data after several minutes

- Verify the provisioned hostname matches your TLS certificate's CN/SAN: `openssl x509 -in ~/tesla-certs/fullchain.pem -noout -subject -ext subjectAltName`
- Verify port 4443 is reachable from outside your network
- Re-run provisioning — Tesla's backend may need a fresh registration after infrastructure changes
