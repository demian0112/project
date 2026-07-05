import json
from types import SimpleNamespace

import pytest

from app.mqtt import (
    DeviceMqttClient,
    MqttConfig,
    build_client_id,
    build_device_topics,
    build_up_subscriptions,
)


def test_default_broker_config_does_not_embed_a_password():
    config = MqttConfig()

    assert config.host == "192.168.101.48"
    assert config.port == 1883
    assert config.username == "csi_user"
    assert config.password == ""


def test_client_id_and_exact_topics():
    device_name = "csi-gw-001"
    topics = build_device_topics(device_name)
    subscriptions = build_up_subscriptions(device_name)

    assert build_client_id(device_name) == "python_csi-gw-001_001"
    assert topics.control == (
        "csi/v1/devices/csi-gw-001/down/control"
    )
    assert subscriptions == topics.subscriptions()
    assert len(subscriptions) == 6
    assert all("+" not in topic and "#" not in topic for topic, _ in subscriptions)
    assert dict(subscriptions)[topics.csi] == 0
    assert all(
        qos == 1
        for topic, qos in subscriptions
        if topic != topics.csi
    )


@pytest.mark.parametrize(
    "device_name",
    ["", "contains space", "a" * 33, "slash/name"],
)
def test_invalid_device_name_is_rejected(device_name):
    with pytest.raises(ValueError):
        build_client_id(device_name)


def test_client_subscribes_to_all_exact_topics(monkeypatch):
    client = DeviceMqttClient("room1_csi")
    calls = []

    def fake_subscribe(topic, qos):
        calls.append((topic, qos))
        return 0, len(calls)

    monkeypatch.setattr(client.client, "subscribe", fake_subscribe)

    subscriptions = client.subscribe_up_topics()

    assert tuple(calls) == subscriptions


def test_control_publish_uses_qos_one_and_no_retain(monkeypatch):
    client = DeviceMqttClient("fall-detector-01")
    published = {}

    def fake_publish(topic, payload, qos, retain):
        published.update(
            topic=topic,
            payload=json.loads(payload),
            qos=qos,
            retain=retain,
        )
        return SimpleNamespace(mid=42, rc=0)

    monkeypatch.setattr(client.client, "publish", fake_publish)

    result = client.publish_control(
        "start",
        command_id="cmd-test",
    )

    assert published["topic"] == (
        "csi/v1/devices/fall-detector-01/down/control"
    )
    assert published["qos"] == 1
    assert published["retain"] is False
    assert published["payload"]["cmd"] == "control"
    assert published["payload"]["action"] == "start"
    assert published["payload"]["session"].startswith("sess-")
    assert set(published["payload"]) == {"cmd", "action", "session"}
    assert result.message_id == 42


def test_stop_requires_active_session():
    client = DeviceMqttClient("csi-gw-001")

    with pytest.raises(ValueError, match="current session"):
        client.publish_control("stop")


def test_message_callback_receives_topic_name_and_json():
    received = []
    client = DeviceMqttClient(
        "csi-gw-001",
        on_message=lambda name, payload: received.append((name, payload)),
    )
    message = SimpleNamespace(
        topic="csi/v1/devices/csi-gw-001/up/status",
        payload=b'{"state":"idle","ts":123}',
    )

    client._on_message(client.client, None, message)

    assert received == [("status", {"state": "idle", "ts": 123})]
