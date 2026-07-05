from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_BROKER_HOST = "192.168.101.48"
DEFAULT_BROKER_PORT = 1883
DEFAULT_BROKER_USERNAME = "csi_user"
DEFAULT_BROKER_PASSWORD = ""


@dataclass(frozen=True, slots=True)
class MqttConfig:
    """Connection parameters shared by every per-device MQTT client."""

    host: str = DEFAULT_BROKER_HOST
    port: int = DEFAULT_BROKER_PORT
    username: str = DEFAULT_BROKER_USERNAME
    password: str = DEFAULT_BROKER_PASSWORD
    keepalive: int = 60

    @classmethod
    def from_env(cls) -> MqttConfig:
        """Allow deployment to override defaults without changing source code."""
        return cls(
            host=os.getenv(
                "MQTT_BROKER_HOST",
                os.getenv("MQTT_HOST", DEFAULT_BROKER_HOST),
            ),
            port=int(
                os.getenv(
                    "MQTT_BROKER_PORT",
                    os.getenv("MQTT_PORT", str(DEFAULT_BROKER_PORT)),
                )
            ),
            username=os.getenv(
                "MQTT_BROKER_USERNAME",
                os.getenv("MQTT_USERNAME", DEFAULT_BROKER_USERNAME),
            ),
            password=os.getenv(
                "MQTT_BROKER_PASSWORD",
                os.getenv("MQTT_PASSWORD", DEFAULT_BROKER_PASSWORD),
            ),
            keepalive=int(os.getenv("MQTT_KEEPALIVE", "60")),
        )
