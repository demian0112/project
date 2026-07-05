"""
MQTT 客户端封装。

职责：
- 连接 Mosquitto Broker
- 向 esp32s3/control 发布控制命令
- 订阅 esp32s3/status 接收设备 ACK（预留）

当 config.MQTT_ENABLED = False 时，所有公开方法安全降级为空操作，
不会抛出异常，确保后端不因 MQTT 未就绪而崩溃。
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import config
from models import DeviceCommand

logger = logging.getLogger("mqtt_client")

_client: Optional[object] = None   # paho.mqtt.client.Client
_connected: bool = False
_latest_fall_alert: Optional[dict] = None  # 缓存最新跌倒告警


# ── 公开接口 ─────────────────────────────────────────

def start() -> None:
    """启动 MQTT 连接。若 MQTT_ENABLED=False 则跳过。"""
    global _client, _connected

    if not config.MQTT_ENABLED:
        logger.info("MQTT is disabled by config.MQTT_ENABLED, skipping connection")
        return

    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        logger.warning("paho-mqtt not installed, MQTT disabled. Run: pip install paho-mqtt")
        return

    _client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=config.MQTT_CLIENT_ID,
        protocol=mqtt.MQTTv311,
    )
    _client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)
    _client.on_connect = _on_connect
    _client.on_message = _on_message
    _client.on_disconnect = _on_disconnect

    try:
        _client.connect(config.MQTT_HOST, config.MQTT_PORT, keepalive=60)
        _client.loop_start()
        logger.info(
            "MQTT connecting to %s:%d as %s",
            config.MQTT_HOST, config.MQTT_PORT, config.MQTT_CLIENT_ID,
        )
    except Exception as exc:
        logger.error("MQTT connection failed: %s", exc)
        _client = None


def stop() -> None:
    """停止 MQTT 连接"""
    global _client, _connected
    if _client is not None:
        try:
            _client.loop_stop()
            _client.disconnect()
        except Exception:
            pass
        _client = None
    _connected = False


def publish_command(cmd: DeviceCommand) -> dict:
    """
    向 esp32s3/control 发布控制命令。

    返回：
        {"topic": ..., "payload": ..., "mqtt_publish_rc": ...}
    若 MQTT 未启用或未连接，rc 为 None。
    """
    payload = json.dumps(cmd.model_dump(), ensure_ascii=False)
    result = {"topic": config.MQTT_TOPIC_CONTROL, "payload": cmd.model_dump()}

    if not config.MQTT_ENABLED or _client is None:
        logger.info("MQTT skipped (enabled=%s, connected=%s): %s", config.MQTT_ENABLED, _connected, payload)
        result["mqtt_publish_rc"] = None
        return result

    logger.info("MQTT publish → %s: %s", config.MQTT_TOPIC_CONTROL, payload)

    info = _client.publish(config.MQTT_TOPIC_CONTROL, payload, qos=1, retain=False)
    result["mqtt_publish_rc"] = info.rc if hasattr(info, "rc") else None
    return result


def get_latest_fall_alert() -> Optional[dict]:
    """返回最新缓存的跌倒告警，无告警时返回 None"""
    return _latest_fall_alert


def is_connected() -> bool:
    return _connected


# ── 内部回调 ─────────────────────────────────────────

def _on_connect(client, userdata, flags, reason_code, properties):
    global _connected
    from paho.mqtt.client import connack_string
    _connected = reason_code == 0
    logger.info("MQTT %s: %s", "connected" if _connected else "connect failed", connack_string(reason_code))
    if _connected:
        client.subscribe(config.MQTT_TOPIC_STATUS, qos=0)
        client.subscribe(config.MQTT_TOPIC_FALL_ALERT, qos=0)
        logger.info("Subscribed to %s, %s", config.MQTT_TOPIC_STATUS, config.MQTT_TOPIC_FALL_ALERT)


def _on_message(client, userdata, msg):
    global _latest_fall_alert
    payload = msg.payload.decode("utf-8", errors="replace")
    logger.info("MQTT received ← %s: %s", msg.topic, payload)

    if msg.topic == config.MQTT_TOPIC_FALL_ALERT:
        try:
            _latest_fall_alert = json.loads(payload)
            logger.info("Fall alert cached: %s", _latest_fall_alert)
        except json.JSONDecodeError:
            logger.warning("Invalid fall alert JSON: %s", payload)


def _on_disconnect(client, userdata, flags, reason_code, properties):
    global _connected
    _connected = False
    logger.info("MQTT disconnected: rc=%s", reason_code)
