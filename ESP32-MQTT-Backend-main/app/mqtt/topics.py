from __future__ import annotations

import re
from dataclasses import dataclass


UP_TOPIC_NAMES = ("online", "wifi", "status", "csi", "ack", "fault")
CONTROL_TOPIC_NAME = "control"
TOPIC_PREFIX = "csi/v1/devices"
DEVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

UP_TOPIC_QOS = {
    "online": 1,
    "wifi": 1,
    "status": 1,
    "csi": 0,
    "ack": 1,
    "fault": 1,
}


@dataclass(frozen=True, slots=True)
class DeviceTopics:
    online: str
    wifi: str
    status: str
    csi: str
    ack: str
    fault: str
    control: str

    def subscriptions(self) -> tuple[tuple[str, int], ...]:
        return (
            (self.online, UP_TOPIC_QOS["online"]),
            (self.wifi, UP_TOPIC_QOS["wifi"]),
            (self.status, UP_TOPIC_QOS["status"]),
            (self.csi, UP_TOPIC_QOS["csi"]),
            (self.ack, UP_TOPIC_QOS["ack"]),
            (self.fault, UP_TOPIC_QOS["fault"]),
        )


def validate_device_name(device_name: str) -> str:
    if not DEVICE_NAME_PATTERN.fullmatch(device_name or ""):
        raise ValueError(
            "device_name must contain 1-32 letters, numbers, _ or -"
        )
    return device_name


def build_client_id(device_uid: str, instance: int = 1) -> str:
    validate_device_name(device_uid)
    if instance < 1 or instance > 999:
        raise ValueError("instance must be between 1 and 999")
    return f"python_{device_uid}_{instance:03d}"


def build_topic(device_uid: str, direction: str, name: str) -> str:
    validate_device_name(device_uid)
    if direction not in {"up", "down"}:
        raise ValueError("direction must be 'up' or 'down'")
    return f"{TOPIC_PREFIX}/{device_uid}/{direction}/{name}"


def build_up_subscriptions(device_uid: str) -> tuple[tuple[str, int], ...]:
    return tuple(
        (build_topic(device_uid, "up", name), UP_TOPIC_QOS[name])
        for name in UP_TOPIC_NAMES
    )


def build_control_topic(device_uid: str) -> str:
    return build_topic(device_uid, "down", CONTROL_TOPIC_NAME)


def build_device_topics(device_uid: str) -> DeviceTopics:
    return DeviceTopics(
        online=build_topic(device_uid, "up", "online"),
        wifi=build_topic(device_uid, "up", "wifi"),
        status=build_topic(device_uid, "up", "status"),
        csi=build_topic(device_uid, "up", "csi"),
        ack=build_topic(device_uid, "up", "ack"),
        fault=build_topic(device_uid, "up", "fault"),
        control=build_control_topic(device_uid),
    )


def topic_name_for_device(device_uid: str, topic: str) -> str | None:
    validate_device_name(device_uid)
    prefix = f"{TOPIC_PREFIX}/{device_uid}/up/"
    if not topic.startswith(prefix):
        return None
    name = topic.removeprefix(prefix)
    return name if name in UP_TOPIC_NAMES else None
