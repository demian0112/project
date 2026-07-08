from __future__ import annotations

import logging
import threading
from typing import Any, Callable

import paho.mqtt.client as mqtt

from ..extensions import db
from ..mqtt import DeviceMqttClient


logger = logging.getLogger(__name__)
MqttPayloadHandler = Callable[[str, str, dict[str, Any]], None]


class MqttUnavailable(RuntimeError):
    pass


class MqttManager:
    """Own one reconnecting MQTT client for each active database device."""

    def __init__(self, app, on_payload: MqttPayloadHandler) -> None:
        self.app = app
        self.on_payload = on_payload
        self._clients: dict[str, DeviceMqttClient] = {}
        self._lock = threading.RLock()

    def ensure_device(self, device_name: str) -> DeviceMqttClient | None:
        if not self.app.config["MQTT_ENABLED"]:
            return None

        with self._lock:
            client = self._clients.get(device_name)
            if client is not None:
                return client

            client = DeviceMqttClient(
                device_name,
                on_message=lambda topic_name, payload: self._dispatch(
                    device_name,
                    topic_name,
                    payload,
                ),
            )
            self._clients[device_name] = client
            client.connect_async()
            return client

    def remove_device(self, device_name: str) -> None:
        with self._lock:
            client = self._clients.pop(device_name, None)
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:
            logger.exception("Failed to disconnect MQTT client for %s", device_name)

    def publish_control(
        self,
        device_name: str,
        action: str,
        session: str,
        command_id: str,
        *,
        reason: str = "manual_control",
        source: str = "user",
    ):
        logger.info(
            "CONTROL_PUBLISH device=%s action=%s session=%s reason=%s source=%s",
            device_name,
            action,
            session,
            reason,
            source,
        )
        override = self.app.config.get("MQTT_CONTROL_PUBLISHER")
        if override is not None:
            return override(
                device_name=device_name,
                action=action,
                session=session,
                command_id=command_id,
                reason=reason,
                source=source,
            )

        client = self.ensure_device(device_name)
        if client is None:
            raise MqttUnavailable(
                "MQTT is disabled; set MQTT_ENABLED=1 before controlling devices"
            )

        timeout = float(self.app.config.get("MQTT_CONNECT_TIMEOUT_SECONDS", 5))
        if not client.wait_until_connected(timeout):
            raise MqttUnavailable("MQTT broker is not connected")

        result = client.publish_control(
            action,
            session=session,
            command_id=command_id,
        )
        if result.result_code != mqtt.MQTT_ERR_SUCCESS:
            raise MqttUnavailable(
                f"MQTT publish failed with code {result.result_code}"
            )
        return result

    def _dispatch(
        self,
        device_name: str,
        topic_name: str,
        payload: dict[str, Any],
    ) -> None:
        with self.app.app_context():
            try:
                self.on_payload(device_name, topic_name, payload)
            except Exception:
                db.session.rollback()
                logger.exception(
                    "Failed to process MQTT %s for %s",
                    topic_name,
                    device_name,
                )
            finally:
                db.session.remove()

