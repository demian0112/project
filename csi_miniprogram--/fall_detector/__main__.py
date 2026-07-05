"""
跌倒检测器进程入口。

功能：
1. 连接 Mosquitto Broker
2. 订阅 esp32s3/test 接收 C 板上传的 CSI 批量数据
3. 解析 CSIB 二进制格式 → 提取每帧幅度
4. 逐帧喂入 FallDetector 滑动窗口
5. 检测到跌倒时 publish 到 esp32s3/fall_alert

启动：
    python -m fall_detector
"""

from __future__ import annotations

import base64
import json
import logging
import struct
import sys
import time
from typing import List, Optional

import numpy as np
import paho.mqtt.client as mqtt

from .config import (
    DEVICE_ID,
    MQTT_HOST,
    MQTT_PORT,
    MQTT_USERNAME,
    MQTT_PASSWORD,
    MQTT_CLIENT_ID,
    MQTT_TOPIC_CSI_DATA,
    MQTT_TOPIC_FALL_ALERT,
)
from .detector import FallDetector

# ── 日志 ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [fall_detector] %(levelname)s: %(message)s",
)
logger = logging.getLogger("fall_detector")


# ── CSIB 二进制解析（与 C 板 app_main_mosquitto_csi_2s_v3.c 一致）─

BATCH_HEADER_STRUCT = struct.Struct("<4sBBBBIqq")
FRAME_HEADER_STRUCT = struct.Struct("<IqbBH")


def raw_iq_to_amplitude(raw: np.ndarray) -> np.ndarray:
    """将 ESP32 CSI int8 I/Q 数据转换为幅度 sqrt(I²+Q²)。"""
    if raw.size < 2:
        return np.empty((0,), dtype=np.float32)
    if raw.size % 2 != 0:
        raw = raw[:-1]
    iq = raw.astype(np.float32).reshape(-1, 2)
    amp = np.sqrt(iq[:, 0] ** 2 + iq[:, 1] ** 2)
    return amp.astype(np.float32)


def parse_mqtt_payload(payload: bytes) -> Optional[List[np.ndarray]]:
    """
    解析 C 板 MQTT JSON + Base64 + CSIB 二进制包。

    返回该批次中每帧的幅度数组列表；若非 CSI 包或解析失败则返回 None。
    """
    try:
        obj = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None

    if obj.get("type") != "csi_batch":
        return None

    fmt = obj.get("payload_format")
    if fmt != "CSIB_b64_split_v1":
        return None

    chunk_count = int(obj.get("payload_chunk_count", 0))
    if chunk_count <= 0:
        return None

    b64_parts = []
    for i in range(chunk_count):
        part = obj.get(f"payload_{i}")
        if part is None:
            return None
        b64_parts.append(part)

    b64_text = "".join(b64_parts)

    try:
        bin_data = base64.b64decode(b64_text)
    except Exception:
        logger.warning("Base64 decode failed")
        return None

    if len(bin_data) < BATCH_HEADER_STRUCT.size:
        return None

    magic, version, frame_count, sample_hz, _reserved, batch_seq, start_ts_us, end_ts_us = (
        BATCH_HEADER_STRUCT.unpack_from(bin_data, 0)
    )

    if magic != b"CSIB" or version != 0x01:
        return None

    offset = BATCH_HEADER_STRUCT.size
    amplitudes_list: List[np.ndarray] = []

    for _ in range(frame_count):
        if offset + FRAME_HEADER_STRUCT.size > len(bin_data):
            break

        seq, timestamp_us, rssi, first_word_invalid, csi_len = (
            FRAME_HEADER_STRUCT.unpack_from(bin_data, offset)
        )
        offset += FRAME_HEADER_STRUCT.size

        if csi_len <= 0 or offset + csi_len > len(bin_data):
            break

        raw = np.frombuffer(bin_data[offset:offset + csi_len], dtype=np.int8).copy()
        offset += csi_len

        amp = raw_iq_to_amplitude(raw)
        if amp.size == 0:
            continue

        amplitudes_list.append(amp)

    return amplitudes_list if amplitudes_list else None


# ── MQTT 回调 ────────────────────────────────────────

def _on_connect(client, userdata, flags, reason_code, properties):
    from paho.mqtt.client import connack_string
    rc = reason_code.value if hasattr(reason_code, "value") else reason_code
    if rc == 0:
        client.subscribe(MQTT_TOPIC_CSI_DATA, qos=0)
        logger.info("MQTT connected, subscribed to %s", MQTT_TOPIC_CSI_DATA)
    else:
        logger.error("MQTT connect failed: %s", connack_string(reason_code))


def _on_disconnect(client, userdata, flags, reason_code, properties):
    logger.info("MQTT disconnected: rc=%s", reason_code)


def _on_message(client, userdata, msg):
    detector: FallDetector = userdata["detector"]
    mqtt_client: mqtt.Client = userdata["mqtt_client"]

    amplitudes_list = parse_mqtt_payload(msg.payload)
    if amplitudes_list is None:
        return

    now = time.time()

    for amp in amplitudes_list:
        alert = detector.ingest_sample(now, amp)
        if alert is not None:
            payload = json.dumps(alert, ensure_ascii=False)
            mqtt_client.publish(MQTT_TOPIC_FALL_ALERT, payload, qos=1)
            logger.info("Fall alert published → %s: %s", MQTT_TOPIC_FALL_ALERT, payload)


# ── 入口 ─────────────────────────────────────────────

def main() -> int:
    logger.info("Fall detector starting...")
    logger.info("MQTT broker: %s:%d", MQTT_HOST, MQTT_PORT)
    logger.info("Input topic: %s, Output topic: %s", MQTT_TOPIC_CSI_DATA, MQTT_TOPIC_FALL_ALERT)

    detector = FallDetector()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=MQTT_CLIENT_ID,
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.user_data_set({"detector": detector, "mqtt_client": client})
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.on_disconnect = _on_disconnect

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except Exception as exc:
        logger.error("MQTT connection failed: %s", exc)
        return 1

    client.loop_start()

    try:
        logger.info("Running. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        client.loop_stop()
        client.disconnect()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
