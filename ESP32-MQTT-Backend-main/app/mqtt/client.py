from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from secrets import token_hex
from typing import Any, Callable

import paho.mqtt.client as mqtt

from .config import MqttConfig
from .topics import (
    DeviceTopics,
    build_client_id,
    build_device_topics,
    topic_name_for_device,
    validate_device_name,
)


logger = logging.getLogger(__name__)
MessageHandler = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class PublishedControl:
    topic: str
    payload: dict[str, Any]
    message_id: int
    result_code: int


def generate_message_id(prefix: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{timestamp}-{token_hex(3)}"


class DeviceMqttClient:
    """One MQTT connection for one database device_name."""

    def __init__(
        self,
        device_name: str,
        *,
        config: MqttConfig | None = None,
        instance: int = 1,
        on_message: MessageHandler | None = None,
    ) -> None:
        self.device_name = validate_device_name(device_name)
        self.config = config or MqttConfig.from_env()
        self.client_id = build_client_id(device_name, instance)
        self.topics: DeviceTopics = build_device_topics(device_name)
        self.on_payload = on_message
        self.connected = threading.Event()

        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
            protocol=mqtt.MQTTv311,
        )
        self.client.username_pw_set(
            self.config.username,
            self.config.password,
        )
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def connect(self) -> None:
        """Connect to Mosquitto and start Paho's background network loop."""
        self.client.connect(
            self.config.host,
            self.config.port,
            self.config.keepalive,
        )
        self.client.loop_start()

    def connect_async(self) -> None:
        """Start a non-blocking connection with automatic reconnect."""
        self.client.connect_async(
            self.config.host,
            self.config.port,
            self.config.keepalive,
        )
        self.client.loop_start()

    def disconnect(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()
        self.connected.clear()

    def wait_until_connected(self, timeout: float = 10) -> bool:
        return self.connected.wait(timeout)

    def subscribe_up_topics(self) -> tuple[tuple[str, int], ...]:
        """Subscribe to the six required exact topics without wildcards."""
        subscriptions = self.topics.subscriptions()
        for topic, qos in subscriptions:
            result, _message_id = self.client.subscribe(topic, qos=qos)
            if result != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(
                    f"failed to subscribe to {topic}: MQTT code {result}"
                )
        return subscriptions

    def publish_control(
        self,
        action: str,
        *,
        session: str | None = None,
        command_id: str | None = None,
        reason: str | None = None,
        source: str | None = None,
    ) -> PublishedControl:
        """Publish control actions to down/control using QoS 1 and retain=false."""
        if action not in {"start", "stop", "reset", "clear_fault"}:
            raise ValueError("action must be start, stop, reset or clear_fault")
        if action == "start" and not session:
            session = generate_message_id("sess")
        if action == "stop" and not session:
            raise ValueError("stop requires the current session")

        payload = {
            "cmd": "control",
            "action": action,
            "session": session or "",
        }
        if action in {"reset", "clear_fault"}:
            if command_id:
                payload["command_id"] = command_id
            if reason:
                payload["reason"] = reason
            if source:
                payload["source"] = source
        info = self.client.publish(
            self.topics.control,
            json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=False,
        )
        return PublishedControl(
            topic=self.topics.control,
            payload=payload,
            message_id=info.mid,
            result_code=info.rc,
        )

    def _on_connect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _connect_flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        if reason_code.is_failure:
            logger.error(
                "Mosquitto rejected client %s: %s",
                self.client_id,
                reason_code,
            )
            self.connected.clear()
            return

        self.connected.set()
        self.subscribe_up_topics()
        logger.info(
            "MQTT client %s connected to %s:%s",
            self.client_id,
            self.config.host,
            self.config.port,
        )

    def _on_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _disconnect_flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        self.connected.clear()
        logger.warning(
            "MQTT client %s disconnected: %s",
            self.client_id,
            reason_code,
        )

    def _on_message(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        topic_name = topic_name_for_device(
            self.device_name,
            message.topic,
        )
        if topic_name is None:
            logger.warning("Ignored unexpected topic: %s", message.topic)
            return

        try:
            payload = json.loads(message.payload.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload is not a JSON object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            logger.warning("Ignored invalid JSON on topic %s", message.topic)
            return

        if self.on_payload is not None:
            self.on_payload(topic_name, payload)
