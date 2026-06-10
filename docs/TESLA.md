TODO document telemetry setup, configuration, and provisioning

```
TESLA_VEHICLE_COMMAND_PROXY_URL=https://localhost:4444 \
  uv run python app.py --provision-fleet-telemetry \
  telemetry.yourdomain.com /home/username/tesla-certs/fullchain.pem
```

```
$ sudo docker exec -it fleet-telemetry_mosquitto_1 /bin/sh
```

# Is fleet-telemetry publishing anything at all?
mosquitto_sub -h localhost -p 1883 -t 'telemetry/#' -v

# Is the fleet-telemetry container running and connected?
docker logs fleet-telemetry --tail 50 2>&1 | grep -E "connect|error|vehicle"

# Any messages ever landed in mosquitto?
# grab first 5 msgs on any topic
mosquitto_sub -h localhost -p 1883 -t '#' -v -C 5

if mosquitto_sub on telemetry/# is also silent, the issue is upstream of Solara — likely the fleet-telemetry container isn't connected to your vehicle, hasn't been provisioned with the right server cert, or is publishing to a different topic base than telemetry/.

The _FLEET_TELEMETRY_DOTFILE at the bottom of mqtt_telemetry.py (~/.solara-fleet-telemetry) — does that file exist and have a recent timestamp? That may be how provisioning state is tracked.
