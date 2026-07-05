"""Opt-in test that publishes through a real Mosquitto broker."""

import json
import os
import threading
from uuid import uuid4

import paho.mqtt.client as mqtt
import pytest

from app.mqtt import DeviceMqttClient, MqttConfig, build_control_topic


RUN_INTEGRATION = os.getenv("RUN_MQTT_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason="set RUN_MQTT_INTEGRATION=1 to use the real MQTT broker",
)


def test_publish_control_round_trip_through_broker():
    """A second client proves that down/control reached the broker."""
    config = MqttConfig.from_env()
    device_name = os.getenv("MQTT_TEST_DEVICE_NAME", "esp01")
    control_topic = build_control_topic(device_name)
    session_id = f"sess-pytest-{uuid4().hex[:8]}"

    observer_ready = threading.Event()
    message_received = threading.Event()
    observer_errors = []
    received_payload = {}


    observer = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"pytest_observer_{uuid4().hex[:10]}",
        protocol=mqtt.MQTTv311,
    )
    observer.username_pw_set(config.username, config.password)

    def on_observer_connect(
        client,
        _userdata,
        _flags,
        reason_code,
        _properties,
    ):
        if reason_code.is_failure:
            observer_errors.append(f"observer login failed: {reason_code}")
            observer_ready.set()
            return
        client.subscribe(control_topic, qos=1)

    def on_observer_subscribe(
        _client,
        _userdata,
        _message_id,
        reason_codes,
        _properties,
    ):
        if any(code.is_failure for code in reason_codes):
            observer_errors.append(
                f"observer subscription rejected: {reason_codes}"
            )
        observer_ready.set()

    def on_observer_message(_client, _userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if (
            payload.get("cmd") != "control"
            or payload.get("session") != session_id
        ):
            return
        received_payload.update(payload)
        message_received.set()

    observer.on_connect = on_observer_connect
    observer.on_subscribe = on_observer_subscribe
    observer.on_message = on_observer_message

    publisher = None
    observer.connect_async(config.host, config.port, config.keepalive)
    observer.loop_start()

    try:
        assert observer_ready.wait(10), (
            f"observer could not connect to {config.host}:{config.port}"
        )
        assert not observer_errors, "; ".join(observer_errors)

        publisher = DeviceMqttClient(device_name, config=config)
        publisher.connect_async()
        assert publisher.wait_until_connected(10), (
            f"publisher could not connect to {config.host}:{config.port}"
        )

        result = publisher.publish_control(
            "start",
            session=session_id,
        )

        assert result.result_code == mqtt.MQTT_ERR_SUCCESS
        assert result.topic == control_topic
        assert message_received.wait(10), (
            f"broker did not deliver {control_topic} within 10 seconds"
        )
        assert received_payload["cmd"] == "control"
        assert received_payload["action"] == "start"
        assert received_payload["session"] == session_id
        assert set(received_payload) == {"cmd", "action", "session"}
    finally:
        if publisher is not None:
            publisher.disconnect()
        observer.disconnect()
        observer.loop_stop()
