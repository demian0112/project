import pytest

from app.services.fall_algorithm_client import (
    AlgorithmAlert,
    AlgorithmPong,
    FallAlgorithmClient,
    FallAlgorithmClientError,
)


class FakeWebSocket:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.sent = []
        self.closed = False

    def send(self, payload):
        self.sent.append(payload)

    def recv(self):
        return self.messages.pop(0)

    def close(self):
        self.closed = True


class TimeoutWebSocket(FakeWebSocket):
    def recv(self):
        raise TimeoutError("timed out")


def make_client(http_transport=None, ws=None):
    return FallAlgorithmClient(
        http_base_url="http://algo.local",
        ws_url="ws://algo.local/stream",
        http_transport=http_transport or (lambda *_args: {"status": "ok"}),
        ws_connect=lambda *_args, **_kwargs: ws or FakeWebSocket(),
    )


def test_http_methods_use_configured_paths_and_validate_health():
    calls = []

    def http(method, url, payload, timeout):
        calls.append((method, url, payload, timeout))
        if url.endswith("/config") and method == "GET":
            return {"step_size": 30}
        return {"status": "ok", "updated": payload}

    client = make_client(http_transport=http)

    assert client.health_check()["status"] == "ok"
    assert client.get_config() == {"step_size": 30}
    assert client.update_config({"step_size": 31})["updated"] == {
        "step_size": 31
    }
    assert client.reset()["updated"] == {}
    assert calls[0][:3] == ("GET", "http://algo.local/health", None)
    assert calls[-1][:3] == ("POST", "http://algo.local/reset", {})


def test_health_failure_raises_protocol_error():
    client = make_client(http_transport=lambda *_args: {"status": "bad"})

    with pytest.raises(FallAlgorithmClientError):
        client.health_check()


def test_websocket_ping_data_pong_and_alert_parsing():
    websocket = FakeWebSocket(
        [
            '{"type":"pong"}',
            '{"type":"alert","is_fall":true,"confidence":0.9231,'
            '"class":"fall","timestamp":"2026-07-07 15:30:00"}',
        ]
    )
    client = make_client(ws=websocket)

    client.send_ping(websocket)
    client.send_data(websocket, "CSI_DATA,...")
    pong = client.receive(websocket)
    alert = client.receive(websocket)

    assert websocket.sent == [
        '{"type":"ping"}',
        '{"type":"data","line":"CSI_DATA,..."}',
    ]
    assert isinstance(pong, AlgorithmPong)
    assert isinstance(alert, AlgorithmAlert)
    assert alert.confidence == 0.9231
    assert alert.algorithm_class == "fall"


@pytest.mark.parametrize(
    "message",
    [
        "not-json",
        '{"type":"alert","is_fall":false,"confidence":0.9}',
        '{"type":"alert","is_fall":true,"confidence":2}',
        '{"type":"alert","is_fall":true,"confidence":0.9,"class":"walk"}',
        '{"type":"stats"}',
    ],
)
def test_websocket_ignores_invalid_or_non_fall_messages(message):
    websocket = FakeWebSocket([message])
    client = make_client(ws=websocket)

    assert client.receive(websocket) is None


def test_websocket_receive_timeout_is_empty_message():
    websocket = TimeoutWebSocket()
    client = make_client(ws=websocket)

    assert client.receive(websocket) is None
