# SSE Streaming Endpoint — Client Guide

Solara exposes a Server-Sent Events (SSE) endpoint at `/stream/status` that
streams real-time load management state and energy metrics to connected clients.

## Endpoint

```
GET /stream/status
```

### Headers

| Header | Value | Notes |
|---|---|---|
| `Cache-Control` | `no-cache` | Prevents intermediary caching |
| `Connection` | `keep-alive` | Required by SSE spec |
| `X-Accel-Buffering` | `no` | Critical for nginx proxying |

### Content Type

`text/event-stream`

## Event Types

Each frame follows the SSE wire format:

```
event: <event_name>
data: <JSON payload>

```

The blank line (double newline) terminates each frame.

### Events

| Event | Payload | When |
|---|---|---|
| `initial_load_state` | Load management state dict | First event on connect |
| `initial_metrics` | Metrics dict (if cached) | Second event on connect |
| `load_cycle` | Load management state dict | Every load cycle (~15–30s) |
| `metrics_update` | Metrics dict | Every load cycle (~15–30s) |
| `heartbeat` | `{}` | After 60s of no other events |

### Load Management Payload (`initial_load_state`, `load_cycle`)

```json
{
    "enabled": true,
    "dryRun": true,
    "targetWh": -50,
    "nbcDevice": "my_solar_meter",
    "state": {
        "devices": {
            "pool_pump": {
                "desiredState": true,
                "actualState": true,
                "currentAmps": null,
                "lastToggle": "2026-06-08T14:30:00+00:00"
            }
        },
        "pendingEffects": []
    },
    "lastCycleResult": {
        "status": "dry-run",
        "qh": "QH1",
        "predictedWh": -1230.0,
        "adjustedWh": -1150.0,
        "targetWh": -50,
        "gapWh": 650.0,
        "actions": [
            {
                "deviceName": "pool_pump",
                "action": "turn_on",
                "powerWatts": 1400.0,
                "targetAmps": null
            }
        ],
        "sleepHint": 12.5,
        "sleepHintAt": "2026-06-08T14:28:15+00:00"
    },
    "sleepHint": 12.5,
    "sleepHintAt": "2026-06-08T14:28:15+00:00"
}
```

### Metrics Payload (`initial_metrics`, `metrics_update`)

```json
{
    "devices": [
        {
            "name": "my_solar_meter",
            "gid": 123456,
            "lag": "P0DT0H0M15S",
            "timezone": "America/Los_Angeles",
            "prediction": -1850.0,
            "predictionMin": -2100.0,
            "predictionMax": -1600.0,
            "minutePredicted": -205.0,
            "minutesRemaining": 45.0,
            "nbc": {
                "QH1": {
                    "wh": -450.0,
                    "complete": true,
                    "rawWh": -360.0,
                    "predictedWh": -450.0,
                    "samplesUsed": 900,
                    "remainingSeconds": 0
                },
                "QH2": {
                    "wh": -320.0,
                    "complete": false,
                    "rawWh": -260.0,
                    "predictedWh": -400.0,
                    "samplesUsed": 320,
                    "remainingSeconds": 580
                },
                "QH3": null,
                "QH4": null
            },
            "perSecondData": [ ... ]
        }
    ],
    "apiResponse": {
        "total": "0:00:01.235000"
    },
    "instant": "2026-06-08T14:28:00+00:00"
}
```

## Client Examples

### Browser JavaScript

```javascript
const source = new EventSource("/stream/status");

// Listen for specific event types
source.addEventListener("initial_load_state", (event) => {
    const data = JSON.parse(event.data);
    console.log("Initial state:", data.enabled, data.dryRun);
});

source.addEventListener("load_cycle", (event) => {
    const data = JSON.parse(event.data);
    const cycle = data.lastCycleResult;
    document.getElementById("status").textContent = cycle.status;
    document.getElementById("gap").textContent = `${cycle.gapWh} Wh`;
});

source.addEventListener("metrics_update", (event) => {
    const data = JSON.parse(event.data);
    const device = data.devices[0];
    updateChart(device.perSecondData);
});

source.addEventListener("heartbeat", () => {
    // connection is alive, no action needed
});

// Error handling — EventSource auto-reconnects on connection loss
source.onerror = (err) => {
    console.warn("SSE connection lost, reconnecting...", err);
};
```

### Python (requests — polling fallback)

```python
import json
import requests

response = requests.get(
    "http://localhost:8000/stream/status",
    stream=True,
    headers={"Accept": "text/event-stream"},
)

for line in response.iter_lines(decode_unicode=True):
    if line.startswith("event: "):
        event_type = line[7:]
    elif line.startswith("data: "):
        payload = json.loads(line[6:])
        if event_type == "load_cycle":
            print(f"Cycle: {payload['lastCycleResult']['status']}")
        elif event_type == "metrics_update":
            print(f"Devices: {len(payload['devices'])}")
```

### Python (aiohttp — async)

```python
import json
import aiohttp

async def stream_status(session, url="http://localhost:8000/stream/status"):
    async with session.get(url) as resp:
        async for line in resp.content:
            text = line.decode("utf-8").strip()
            if text.startswith("event: "):
                event_type = text[7:]
            elif text.startswith("data: "):
                payload = json.loads(text[6:])
                yield event_type, payload

async def main():
    async with aiohttp.ClientSession() as session:
        async for event_type, payload in stream_status(session):
            if event_type == "initial_load_state":
                print(f"Connected — dryRun={payload['dryRun']}")
            elif event_type == "load_cycle":
                print(f"Cycle: {payload['lastCycleResult']['status']}")
```

### iOS / macOS (Swift)

```swift
import Foundation

let url = URL(string: "https://solara.example.com/stream/status")!
var request = URLRequest(url: url)
request.setValue("text/event-stream", forHTTPHeaderField: "Accept")

let session = URLSession(configuration: .default)
let task = session.dataTask(with: request)
task.resume()

// Parse events using URLSession delegate or a library like
// https://github.com/getswift/event-source
```

## Gunicorn Configuration

SSE connections are long-lived and require a threaded or async worker model.
The production deployment uses:

```yaml
startCommand: gunicorn app:app --worker-class=gthread --threads=4 --timeout=0
```

Each concurrent SSE client occupies one thread. Adjust `--threads` to match
expected concurrency. The default `--timeout=0` disables the worker timeout,
which is required because SSE connections are open indefinitely.

## nginx Proxy Configuration

If behind nginx, ensure buffering is disabled:

```nginx
location /stream/status {
    proxy_pass http://localhost:8000;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    chunked_transfer_encoding on;
}
```

The endpoint already sets `X-Accel-Buffering: no` in its response headers,
which nginx respects when proxying.

## EventSource Auto-Reconnection

The browser `EventSource` API automatically reconnects on connection loss.
When reconnecting, a new `initial_load_state` event is emitted so the client
always receives the full current state without a separate API call.

## Debugging with curl

### Basic SSE stream

The `-N` (or `--no-buffer`) flag is essential — without it curl buffers
output and you see nothing until the connection closes (which never happens):

```bash
curl -N http://localhost:8000/stream/status \
  -H "Accept: text/event-stream"
```

Sample output (frames arrive as they are published):

```
event: initial_load_state
data: {"enabled":true,"dryRun":true,"targetWh":-50,...}

event: load_cycle
data: {"enabled":true,"lastCycleResult":{"status":"dry-run",...},...}

event: heartbeat
data: {}
```

### Inspect response headers

Add `-i` to see the response headers before the stream:

```bash
curl -N -i http://localhost:8000/stream/status \
  -H "Accept: text/event-stream"
```

Expected headers:

```
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

Confirm `Content-Type: text/event-stream` and `X-Accel-Buffering: no` are
present — missing headers cause proxies or browsers to buffer the stream.

### Filter to specific event types

Watch only `load_cycle` events by piping through `sed` and `grep`:

```bash
curl -N -s http://localhost:8000/stream/status \
  -H "Accept: text/event-stream" \
  | grep --line-buffered '^data: ' \
  | sed 's/^data: //'
```

To isolate a single event type and its payload:

```bash
curl -N -s http://localhost:8000/stream/status \
  -H "Accept: text/event-stream" \
  | sed -n '/^event: load_cycle$/{n;s/^data: //;p}'
```

### Limited-time connection

`--max-time` asks curl to stop after N seconds, but on some systems the
request may not be interrupted mid-frame. Use the `timeout` command for a
hard kill:

```bash
timeout 15 curl -N http://localhost:8000/stream/status \
  -H "Accept: text/event-stream"
```

### Common pitfalls

| Problem | Likely cause | Fix |
|---|---|---|
| No output at all | Missing `-N` (curl buffering) | Add `-N` or pipe through `stdbuf -oL` |
| Empty response / 404 | Wrong URL or path | Verify the path matches the route (`/stream/status`) |
| Stream freezes after first event | Proxy (nginx, HAProxy) buffering | Set `proxy_buffering off` on the proxy; confirm `X-Accel-Buffering: no` reaches the client |
| Connection drops after 60s | Proxy idle timeout | Increase proxy timeout or disable it for SSE routes |
| `curl: (55) Send failure` | Server restarted or connection reset | The stream is gone — just reconnect; EventSource clients do this automatically |
