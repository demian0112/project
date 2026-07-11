import os

import pytest

from app.services.fall_algorithm_client import AlgorithmPong, FallAlgorithmClient


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_FALL_ALGORITHM_INTEGRATION") != "1",
    reason="set RUN_FALL_ALGORITHM_INTEGRATION=1 to test the Docker service",
)


def test_docker_fall_algorithm_health_config_ping_and_reset():
    client = FallAlgorithmClient(
        http_base_url=os.getenv(
            "FALL_ALGORITHM_HTTP_BASE_URL",
            "http://127.0.0.1:18080",
        ),
        ws_url=os.getenv(
            "FALL_ALGORITHM_WS_URL",
            "ws://127.0.0.1:18080/stream",
        ),
        http_timeout=3,
        ws_connect_timeout=5,
        ws_read_timeout=5,
    )

    assert client.health_check()["status"] == "ok"
    config = client.get_config()
    for field in (
        "step_size",
        "buffer_size",
        "fall_confidence_threshold",
        "enable_sobel",
        "consecutive_required",
        "confirmation_window",
        "cooldown_seconds",
        "max_time_interval",
    ):
        assert field in config

    websocket = client.connect_stream()
    try:
        client.send_ping(websocket)
        assert isinstance(client.receive(websocket), AlgorithmPong)
    finally:
        websocket.close()

    assert isinstance(client.reset(), dict)
