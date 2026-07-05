"""
CSI MQTT Sliding Viewer (33 FPS)
=================================

功能：
1. 连接 Mosquitto Broker；
2. 订阅 C 板上传的 CSI 批量数据 Topic；
3. 每收到一个 MQTT 包，解析其中约 2 秒的 CSI 帧；
4. 将原始 int8 I/Q CSI 转换为幅度 sqrt(I^2 + Q^2)；
5. 不一次性刷新整包，而是放入播放队列，按 33 FPS 逐帧显示；
6. 热力图从右侧进入新帧，旧帧向左滑动，模拟真实实时滑动效果。

运行示例：
python csi_mqtt_viewer_sliding_33fps.py --broker 192.168.101.48 --topic esp32s3/test

依赖：
pip install numpy paho-mqtt PySide6 pyqtgraph
"""

from __future__ import annotations

import argparse
import base64
import json
import queue
import struct
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple

import numpy as np
import paho.mqtt.client as mqtt
from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg


# =========================
# 默认 MQTT 参数
# =========================
DEFAULT_BROKER_HOST = "192.168.101.48"
DEFAULT_BROKER_PORT = 1883
DEFAULT_USERNAME = "esp32"
DEFAULT_PASSWORD = "esp32pass"
DEFAULT_TOPIC = "esp32s3/test"
DEFAULT_CLIENT_ID = "python_csi_sliding_viewer_001"


# =========================
# C 板 CSIB 二进制包格式
# 需要与 C 板 app_main_mosquitto_csi_2s_v3.c 保持一致
# =========================
BATCH_HEADER_STRUCT = struct.Struct("<4sBBBBIqq")
FRAME_HEADER_STRUCT = struct.Struct("<IqbBH")


# =========================
# 工具函数
# =========================
def mqtt_reason_code_value(reason_code) -> int | None:
    """兼容 paho-mqtt 1.x/2.x 的连接返回码。"""
    if reason_code is None:
        return None

    if isinstance(reason_code, int):
        return reason_code

    value = getattr(reason_code, "value", None)
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass

    try:
        return int(reason_code)
    except (TypeError, ValueError):
        pass

    text = str(reason_code).strip().lower()
    if text in {"success", "0"}:
        return 0

    return None


@dataclass
class CsiFrame:
    seq: int
    timestamp_us: int
    rssi: int
    first_word_invalid: int
    raw_csi: np.ndarray
    amplitude: np.ndarray


@dataclass
class CsiBatch:
    batch_seq: int
    sample_hz: int
    start_ts_us: int
    end_ts_us: int
    frames: List[CsiFrame]
    json_meta: dict


class CsiPacketParser:
    """解析 C 板 MQTT JSON + Base64 + CSIB 二进制包。"""

    @staticmethod
    def raw_iq_to_amplitude(raw: np.ndarray) -> np.ndarray:
        """
        将 ESP32 CSI int8 原始 I/Q 数据转换为幅度。

        当前 B 板 last_len=128 时，通常对应 64 个复数点。
        原始顺序即使是 [I,Q] 或 [Q,I]，幅度 sqrt(I^2 + Q^2) 不受影响。
        """
        if raw.size < 2:
            return np.empty((0,), dtype=np.float32)

        if raw.size % 2 != 0:
            raw = raw[:-1]

        iq = raw.astype(np.float32).reshape(-1, 2)
        amp = np.sqrt(iq[:, 0] * iq[:, 0] + iq[:, 1] * iq[:, 1])
        return amp.astype(np.float32)

    @staticmethod
    def parse_mqtt_payload(payload: bytes) -> Optional[CsiBatch]:
        obj = json.loads(payload.decode("utf-8", errors="replace"))

        # 允许订阅 esp32s3/#，自动忽略 status/control 等非 CSI 包。
        if obj.get("type") != "csi_batch":
            return None

        fmt = obj.get("payload_format")
        if fmt != "CSIB_b64_split_v1":
            raise ValueError(f"Unsupported payload_format: {fmt}")

        chunk_count = int(obj.get("payload_chunk_count", 0))
        if chunk_count <= 0:
            raise ValueError("payload_chunk_count missing or invalid")

        b64_parts = []
        for i in range(chunk_count):
            key = f"payload_{i}"
            part = obj.get(key)
            if part is None:
                raise ValueError(f"Missing {key}")
            b64_parts.append(part)

        b64_text = "".join(b64_parts)

        expected_b64_len = int(obj.get("payload_total_len", len(b64_text)))
        if len(b64_text) != expected_b64_len:
            raise ValueError(
                f"Base64 length mismatch: got={len(b64_text)}, expected={expected_b64_len}"
            )

        bin_data = base64.b64decode(b64_text)
        expected_payload_bytes = int(obj.get("payload_bytes", len(bin_data)))
        if len(bin_data) != expected_payload_bytes:
            raise ValueError(
                f"Binary length mismatch: got={len(bin_data)}, expected={expected_payload_bytes}"
            )

        return CsiPacketParser.parse_csib_binary(bin_data, obj)

    @staticmethod
    def parse_csib_binary(data: bytes, json_meta: dict) -> CsiBatch:
        if len(data) < BATCH_HEADER_STRUCT.size:
            raise ValueError("CSIB data too short")

        magic, version, frame_count, sample_hz, _reserved, batch_seq, start_ts_us, end_ts_us = (
            BATCH_HEADER_STRUCT.unpack_from(data, 0)
        )

        if magic != b"CSIB":
            raise ValueError(f"Invalid CSIB magic: {magic!r}")
        if version != 0x01:
            raise ValueError(f"Unsupported CSIB version: {version}")

        offset = BATCH_HEADER_STRUCT.size
        frames: List[CsiFrame] = []

        for _ in range(frame_count):
            if offset + FRAME_HEADER_STRUCT.size > len(data):
                raise ValueError("Truncated frame header")

            seq, timestamp_us, rssi, first_word_invalid, csi_len = (
                FRAME_HEADER_STRUCT.unpack_from(data, offset)
            )
            offset += FRAME_HEADER_STRUCT.size

            if csi_len <= 0:
                raise ValueError(f"Invalid csi_len: {csi_len}")
            if offset + csi_len > len(data):
                raise ValueError("Truncated CSI payload")

            raw = np.frombuffer(data[offset: offset + csi_len], dtype=np.int8).copy()
            offset += csi_len

            amp = CsiPacketParser.raw_iq_to_amplitude(raw)
            if amp.size == 0:
                continue

            frames.append(
                CsiFrame(
                    seq=int(seq),
                    timestamp_us=int(timestamp_us),
                    rssi=int(rssi),
                    first_word_invalid=int(first_word_invalid),
                    raw_csi=raw,
                    amplitude=amp,
                )
            )

        return CsiBatch(
            batch_seq=int(batch_seq),
            sample_hz=int(sample_hz),
            start_ts_us=int(start_ts_us),
            end_ts_us=int(end_ts_us),
            frames=frames,
            json_meta=json_meta,
        )


class MqttReceiver(QtCore.QObject):
    """MQTT 接收线程桥接到 Qt 主线程。"""

    status_changed = QtCore.Signal(str)

    def __init__(self, args: argparse.Namespace):
        super().__init__()
        self.args = args
        self.queue: queue.Queue[bytes] = queue.Queue(maxsize=100)
        self.client: Optional[mqtt.Client] = None
        self.connected = False

    def start(self) -> None:
        try:
            self.client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=self.args.client_id,
                protocol=mqtt.MQTTv311,
            )
            self.client.on_connect = self._on_connect_v2
            self.client.on_disconnect = self._on_disconnect_v2
        except Exception:
            self.client = mqtt.Client(
                client_id=self.args.client_id,
                protocol=mqtt.MQTTv311,
            )
            self.client.on_connect = self._on_connect_v1
            self.client.on_disconnect = self._on_disconnect_v1

        self.client.username_pw_set(self.args.username, self.args.password)
        self.client.on_message = self._on_message

        self.status_changed.emit(
            f"Connecting to {self.args.broker}:{self.args.port}, topic={self.args.topic}"
        )

        self.client.connect(self.args.broker, self.args.port, keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass

    def _on_connect_v2(self, client, userdata, flags, reason_code, properties):
        rc = mqtt_reason_code_value(reason_code)
        if rc == 0:
            self.connected = True
            client.subscribe(self.args.topic, qos=0)
            self.status_changed.emit(f"MQTT connected. Subscribed: {self.args.topic}")
        else:
            self.status_changed.emit(f"MQTT connect failed: {reason_code}, rc={rc}")

    def _on_disconnect_v2(self, client, userdata, disconnect_flags, reason_code, properties):
        self.connected = False
        rc = mqtt_reason_code_value(reason_code)
        self.status_changed.emit(f"MQTT disconnected: {reason_code}, rc={rc}")

    def _on_connect_v1(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            client.subscribe(self.args.topic, qos=0)
            self.status_changed.emit(f"MQTT connected. Subscribed: {self.args.topic}")
        else:
            self.status_changed.emit(f"MQTT connect failed: rc={rc}")

    def _on_disconnect_v1(self, client, userdata, rc):
        self.connected = False
        self.status_changed.emit(f"MQTT disconnected: rc={rc}")

    def _on_message(self, client, userdata, msg):
        try:
            self.queue.put_nowait(msg.payload)
        except queue.Full:
            # 队列满时丢弃最旧消息，优先保持实时性。
            try:
                _ = self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(msg.payload)
            except queue.Full:
                pass


class SlidingDisplayBuffer:
    """固定宽度热力图缓冲区：每来一帧，整体左移，新帧插到最右侧。"""

    def __init__(self, display_frames: int):
        self.display_frames = display_frames
        self.subcarriers = 64
        self.matrix = np.full((self.subcarriers, self.display_frames), np.nan, dtype=np.float32)
        self.last_frame: Optional[CsiFrame] = None

    def push_frame(self, frame: CsiFrame) -> None:
        amp = frame.amplitude.astype(np.float32)
        if amp.size == 0:
            return

        if amp.size != self.subcarriers:
            self.subcarriers = int(amp.size)
            self.matrix = np.full((self.subcarriers, self.display_frames), np.nan, dtype=np.float32)

        self.matrix[:, :-1] = self.matrix[:, 1:]
        self.matrix[:, -1] = amp[:self.subcarriers]
        self.last_frame = frame

    def image_matrix(self) -> np.ndarray:
        # pyqtgraph 对 NaN 显示不总是稳定，这里用 0 填充初始空白。
        return np.nan_to_num(self.matrix, nan=0.0)

    def latest_amplitude(self) -> Optional[np.ndarray]:
        if self.last_frame is None:
            return None
        return self.last_frame.amplitude


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, args: argparse.Namespace):
        super().__init__()
        self.args = args

        self.setWindowTitle("ESP32 CSI MQTT Sliding Viewer - 33 FPS")
        self.resize(1280, 860)

        pg.setConfigOptions(antialias=True, imageAxisOrder="row-major")

        self.parser = CsiPacketParser()
        self.receiver = MqttReceiver(args)
        self.receiver.status_changed.connect(self._set_mqtt_status)

        self.play_queue: Deque[CsiFrame] = deque(maxlen=args.max_play_queue)
        self.display_frames = max(1, int(round(args.window_seconds * args.display_fps)))
        self.display_buffer = SlidingDisplayBuffer(display_frames=self.display_frames)

        self.batch_count = 0
        self.received_frame_count = 0
        self.displayed_frame_count = 0
        self.parse_error_count = 0
        self.dropped_frame_count = 0
        self.last_batch_seq: Optional[int] = None
        self.last_msg_time = 0.0
        self.last_error = ""
        self.mqtt_status = "starting"
        self.color_levels: Optional[Tuple[float, float]] = None

        self._build_ui()

        # MQTT 消息轮询：负责把大包解析成帧并放入播放队列。
        self.message_timer = QtCore.QTimer(self)
        self.message_timer.timeout.connect(self._poll_mqtt_messages)
        self.message_timer.start(args.message_poll_ms)

        # 播放定时器：严格按照指定 FPS 从播放队列取一帧显示。
        self.play_timer = QtCore.QTimer(self)
        self.play_timer.timeout.connect(self._play_one_frame)
        self.play_timer.start(max(1, int(round(1000.0 / args.display_fps))))

        # 状态刷新定时器。
        self.status_timer = QtCore.QTimer(self)
        self.status_timer.timeout.connect(self._refresh_status)
        self.status_timer.start(250)

        self.receiver.start()

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        main_layout = QtWidgets.QVBoxLayout(central)

        self.status_label = QtWidgets.QLabel("Starting...")
        self.status_label.setMinimumHeight(32)
        self.status_label.setStyleSheet(
            "font-size: 13px; padding: 6px; background-color: #f0f0f0; border-radius: 4px;"
        )
        main_layout.addWidget(self.status_label)

        # 热力图区域
        self.heatmap_plot = pg.PlotWidget(title="CSI amplitude sliding heatmap (old ← left | newest → right)")
        self.heatmap_plot.setLabel("bottom", "Sliding time window: old → new")
        self.heatmap_plot.setLabel("left", "Subcarrier index")
        self.heatmap_plot.showGrid(x=True, y=True, alpha=0.18)
        self.heatmap_plot.setMouseEnabled(x=False, y=False)
        self.heatmap_plot.setMenuEnabled(False)

        self.img_item = pg.ImageItem()
        self.heatmap_plot.addItem(self.img_item)
        self.img_item.setRect(QtCore.QRectF(0, 0, self.display_frames, 64))

        try:
            cmap = pg.colormap.get("viridis")
            self.img_item.setLookupTable(cmap.getLookupTable(0.0, 1.0, 256))
        except Exception:
            pass

        self.heatmap_plot.setXRange(0, self.display_frames, padding=0)
        self.heatmap_plot.setYRange(0, 64, padding=0)
        main_layout.addWidget(self.heatmap_plot, stretch=5)

        # 最新一帧折线图
        self.line_plot = pg.PlotWidget(title="Latest CSI amplitude frame")
        self.line_plot.setLabel("bottom", "Subcarrier index")
        self.line_plot.setLabel("left", "Amplitude")
        self.line_plot.showGrid(x=True, y=True, alpha=0.25)
        self.line_curve = self.line_plot.plot([], [], pen=pg.mkPen(width=2))
        main_layout.addWidget(self.line_plot, stretch=2)

        # 控件区
        controls = QtWidgets.QHBoxLayout()

        self.auto_level_checkbox = QtWidgets.QCheckBox("自动色阶")
        self.auto_level_checkbox.setChecked(True)
        controls.addWidget(self.auto_level_checkbox)

        controls.addWidget(QtWidgets.QLabel("下百分位"))
        self.lower_percentile = QtWidgets.QSpinBox()
        self.lower_percentile.setRange(0, 49)
        self.lower_percentile.setValue(5)
        self.lower_percentile.setSuffix("%")
        controls.addWidget(self.lower_percentile)

        controls.addWidget(QtWidgets.QLabel("上百分位"))
        self.upper_percentile = QtWidgets.QSpinBox()
        self.upper_percentile.setRange(50, 100)
        self.upper_percentile.setValue(95)
        self.upper_percentile.setSuffix("%")
        controls.addWidget(self.upper_percentile)

        controls.addWidget(QtWidgets.QLabel("播放FPS"))
        self.fps_label = QtWidgets.QLabel(f"{self.args.display_fps:.1f}")
        controls.addWidget(self.fps_label)

        controls.addStretch(1)
        main_layout.addLayout(controls)

        self.setCentralWidget(central)

    @QtCore.Slot(str)
    def _set_mqtt_status(self, text: str) -> None:
        self.mqtt_status = text
        self._refresh_status()

    def _poll_mqtt_messages(self) -> None:
        drained = 0
        while drained < self.args.max_messages_per_poll:
            try:
                payload = self.receiver.queue.get_nowait()
            except queue.Empty:
                break
            drained += 1
            self._handle_mqtt_payload(payload)

    def _handle_mqtt_payload(self, payload: bytes) -> None:
        try:
            batch = self.parser.parse_mqtt_payload(payload)
            if batch is None:
                return

            self.batch_count += 1
            self.last_batch_seq = batch.batch_seq
            self.last_msg_time = time.time()

            # 如果播放队列堆积过多，说明显示跟不上接收。为了保持实时性，丢弃一部分最旧帧。
            free_space = self.args.max_play_queue - len(self.play_queue)
            incoming = len(batch.frames)
            if incoming > free_space:
                drop_n = incoming - max(0, free_space)
                for _ in range(min(drop_n, len(self.play_queue))):
                    self.play_queue.popleft()
                    self.dropped_frame_count += 1

            for frame in batch.frames:
                self.play_queue.append(frame)

            self.received_frame_count += len(batch.frames)
            print(
                f"[BATCH] seq={batch.batch_seq}, frames={len(batch.frames)}, "
                f"queue={len(self.play_queue)}, sample_hz={batch.sample_hz}"
            )

        except Exception as exc:
            self.parse_error_count += 1
            self.last_error = str(exc)
            print(f"[ERROR] Failed to parse MQTT payload: {exc}")

    def _play_one_frame(self) -> None:
        if self.play_queue:
            frame = self.play_queue.popleft()
            self.display_buffer.push_frame(frame)
            self.displayed_frame_count += 1

            self._refresh_heatmap()
            self._refresh_latest_line()

    def _refresh_heatmap(self) -> None:
        img = self.display_buffer.image_matrix()

        # ImageItem 的显示尺寸根据当前子载波数动态更新。
        self.img_item.setRect(QtCore.QRectF(0, 0, self.display_frames, img.shape[0]))
        self.heatmap_plot.setYRange(0, img.shape[0], padding=0)

        if self.auto_level_checkbox.isChecked():
            finite = img[np.isfinite(img)]
            if finite.size > 0:
                low = float(np.percentile(finite, self.lower_percentile.value()))
                high = float(np.percentile(finite, self.upper_percentile.value()))
                if low >= high:
                    high = low + 1e-3
                self.color_levels = (low, high)
        else:
            # 固定使用上一次色阶，若没有则自动计算一次。
            if self.color_levels is None:
                finite = img[np.isfinite(img)]
                if finite.size > 0:
                    low = float(np.percentile(finite, self.lower_percentile.value()))
                    high = float(np.percentile(finite, self.upper_percentile.value()))
                    if low >= high:
                        high = low + 1e-3
                    self.color_levels = (low, high)

        if self.color_levels is not None:
            self.img_item.setImage(img, autoLevels=False, levels=self.color_levels)
        else:
            self.img_item.setImage(img, autoLevels=True)

    def _refresh_latest_line(self) -> None:
        amp = self.display_buffer.latest_amplitude()
        if amp is None or amp.size == 0:
            return

        x = np.arange(amp.size)
        self.line_curve.setData(x, amp)

    def _refresh_status(self) -> None:
        now = time.time()
        age_text = "none"
        if self.last_msg_time > 0:
            age_text = f"{now - self.last_msg_time:.1f}s ago"

        last_seq = "-"
        last_rssi = "-"
        if self.display_buffer.last_frame is not None:
            last_seq = str(self.display_buffer.last_frame.seq)
            last_rssi = str(self.display_buffer.last_frame.rssi)

        status = (
            f"{self.mqtt_status} | "
            f"batches={self.batch_count} | "
            f"received_frames={self.received_frame_count} | "
            f"displayed_frames={self.displayed_frame_count} | "
            f"queue={len(self.play_queue)} | "
            f"dropped={self.dropped_frame_count} | "
            f"last_batch={self.last_batch_seq} | "
            f"last_seq={last_seq} | "
            f"rssi={last_rssi} | "
            f"last_msg={age_text} | "
            f"errors={self.parse_error_count}"
        )

        if self.last_error:
            status += f" | last_error={self.last_error[:80]}"

        self.status_label.setText(status)

    def closeEvent(self, event):
        self.receiver.stop()
        event.accept()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ESP32 CSI MQTT sliding viewer")

    parser.add_argument("--broker", default=DEFAULT_BROKER_HOST, help="Mosquitto Broker IP")
    parser.add_argument("--port", type=int, default=DEFAULT_BROKER_PORT, help="MQTT port")
    parser.add_argument("--username", default=DEFAULT_USERNAME, help="MQTT username")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="MQTT password")
    parser.add_argument("--topic", default=DEFAULT_TOPIC, help="MQTT topic, e.g. esp32s3/test or esp32s3/#")
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID, help="MQTT client id")

    parser.add_argument("--display-fps", type=float, default=33.0, help="Playback FPS. Default: 33")
    parser.add_argument("--window-seconds", type=float, default=2.0, help="Visible sliding window length in seconds")
    parser.add_argument("--max-play-queue", type=int, default=300, help="Max queued frames waiting for playback")
    parser.add_argument("--message-poll-ms", type=int, default=20, help="MQTT message polling interval in ms")
    parser.add_argument("--max-messages-per-poll", type=int, default=5, help="Max MQTT packages parsed per GUI poll")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow(args)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
