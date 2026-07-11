# Docker Fall Algorithm Integration

This backend now treats the Docker fall-detection service as the production
source of automatic fall events. The old `predict_fall(...) -> 0` placeholder is
not called from the CSI ingestion path.

## Runtime Contract

- HTTP default: `http://127.0.0.1:18080`
- WebSocket default: `ws://127.0.0.1:18080/stream`
- CSI upload stays unchanged: ESP32 still sends the current MQTT `csib64-v2`
  batch payload.
- The backend decodes `CsiFrame`, formats each frame as Docker CSV, and sends:
  `{"type":"data","line":"..."}`
- `ID` maps to the decoded CSIB frame sequence.
- `MAC` maps to stable `devices.device_name` because the current `csib64-v2`
  payload does not contain the physical ESP32 MAC.
- CSV fields not present in `csib64-v2` are centralized in Flask config under
  `FALL_ALGORITHM_*`; they are not hard-coded in the coordinator.

## Database Upgrade

The project still uses the existing SQLite additive migration path:

```bash
flask --app run.py init-db
```

On startup or `init-db`, `ensure_database_schema()` runs `db.create_all()` for
new databases and adds missing columns for existing SQLite databases. It does
not delete or rebuild existing user, device, or event data.

New `devices` columns:

- `step_size`
- `buffer_size`
- `fall_confidence_threshold`
- `enable_sobel`
- `consecutive_required`
- `confirmation_window`
- `cooldown_seconds`
- `max_time_interval`

New `fall_events` columns:

- `alert_count`
- `last_detected_at`
- `max_confidence`
- `algorithm_source`
- `algorithm_class`
- `algorithm_confidence`
- `algorithm_timestamp`

## Environment

Copy `.env.example` and set the Docker values as needed. If Flask also runs in a
container, `127.0.0.1:18080` points to the Flask container itself, not the
algorithm container. Use the service name and internal port instead, for example:

```dotenv
FALL_ALGORITHM_HTTP_BASE_URL=http://fall-detection:5000
FALL_ALGORITHM_WS_URL=ws://fall-detection:5000/stream
```

Default safety policy:

```dotenv
FALL_ALGORITHM_SINGLE_ACTIVE_STREAM=1
```

The Docker `/config` and `/reset` APIs are global and do not carry a device ID,
so one Docker instance is treated as safe for only one active device stream by
default. A second active device is rejected/logged instead of mixing CSI streams.
Only disable this after confirming the Docker service fully isolates concurrent
connections.

## Running

```bash
cd ESP32-MQTT-Backend-main
python -m pip install -r requirements.txt
flask --app run.py init-db
flask --app run.py run
```

Run the algorithm container separately with host port `18080` mapped to its
internal port `5000`.

## Tests

Default tests do not require Docker, MQTT, or WeChat:

```bash
pytest -q
ruff check .
```

Optional Docker integration test:

```bash
RUN_FALL_ALGORITHM_INTEGRATION=1 pytest -q tests/test_fall_algorithm_integration.py
```

It verifies `/health`, `/config`, WebSocket `ping -> pong`, and `/reset`; it does
not require triggering a real fall alert.
