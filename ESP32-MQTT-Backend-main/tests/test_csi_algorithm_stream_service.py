import threading

from app.extensions import db
from app.models import Device, User
from app.services.csi_algorithm_stream_service import (
    CsiAlgorithmStreamService,
    QueuedFrame,
    _StreamState,
)


class QuietWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, payload):
        self.sent.append(payload)

    def recv(self):
        threading.Event().wait(0.05)
        return None

    def close(self):
        self.closed = True


class FakeAlgorithmClient:
    def __init__(self):
        self.health_calls = 0
        self.configs = []
        self.resets = 0
        self.websockets = []

    def health_check(self):
        self.health_calls += 1
        return {"status": "ok"}

    def update_config(self, config):
        self.configs.append(dict(config))
        return {"ok": True}

    def connect_stream(self):
        websocket = QuietWebSocket()
        self.websockets.append(websocket)
        return websocket

    def send_ping(self, websocket):
        websocket.send('{"type":"ping"}')

    def send_data(self, websocket, line):
        websocket.send(line)

    def receive(self, websocket):
        websocket.recv()
        return None

    def reset(self):
        self.resets += 1
        return {"ok": True}


def make_device(name, user, session):
    return Device(
        device_name=name,
        display_name=name,
        owner=user,
        state="online",
        runtime_state="uploading",
        detection_state="running",
        current_session=session,
        network_quality="good",
    )


def test_frame_interval_uses_configured_batch_interval(app):
    app.config["FALL_ALGORITHM_BATCH_INTERVAL_SECONDS"] = 1.5
    service = CsiAlgorithmStreamService(app, client=FakeAlgorithmClient())

    assert service.frame_interval_seconds(3) == 0.5
    assert service.frame_interval_seconds(0) == 0.0


def test_stream_queue_drops_oldest_frames_when_full():
    state = _StreamState(
        device_name="queue-device",
        session="sess",
        algorithm_config={},
        queue_max_frames=3,
        network_quality="good",
    )

    dropped = state.enqueue(
        [
            QueuedFrame("a", 0.1),
            QueuedFrame("b", 0.1),
            QueuedFrame("c", 0.1),
            QueuedFrame("d", 0.1),
        ]
    )
    assert dropped == 1
    assert [item.line for item in state.queue] == ["b", "c", "d"]

    dropped = state.enqueue([QueuedFrame("e", 0.1), QueuedFrame("f", 0.1)])
    assert dropped == 2
    assert [item.line for item in state.queue] == ["d", "e", "f"]


def test_single_active_stream_blocks_second_device(app):
    app.config["FALL_ALGORITHM_ENABLED"] = True
    app.config["FALL_ALGORITHM_SINGLE_ACTIVE_STREAM"] = True
    fake = FakeAlgorithmClient()
    service = CsiAlgorithmStreamService(app, client=fake)
    with app.app_context():
        user = User(wx_openid="wx-stream")
        first = make_device("stream-one", user, "sess-one")
        second = make_device("stream-two", user, "sess-two")
        db.session.add_all([user, first, second])
        db.session.commit()

        assert service.start_stream(first, "sess-one") is True
        assert service.start_stream(first, "sess-one") is True
        assert service.start_stream(second, "sess-two") is False
        assert len(service._streams) == 1

    service.close_all()


def test_send_deadline_tolerates_concurrent_disconnect(app):
    clock_value = 0.0

    def clock():
        return clock_value

    state = _StreamState(
        device_name="race-device",
        session="sess",
        algorithm_config={},
        queue_max_frames=3,
        network_quality="good",
    )
    state.next_send_at = 1.0
    service = CsiAlgorithmStreamService(
        app,
        client=FakeAlgorithmClient(),
        clock=clock,
        sleeper=lambda _seconds: state.clear_queue(),
    )

    assert service._wait_for_send_deadline(state, 0.1) is True
    assert state.next_send_at is None
