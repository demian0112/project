from __future__ import annotations

import base64
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable


MIN_SNAPSHOT_FRAMES = 30
DEFAULT_SNAPSHOT_FRAMES = 180
MAX_SNAPSHOT_FRAMES = 300
UINT32_SIZE = 2**32
SEQ_RESET_THRESHOLD = 2**31


@dataclass(frozen=True, slots=True)
class CsiLiveFrame:
    sequence: int | None
    timestamp_us: int | None
    rssi: int | None
    amplitude: tuple[float, ...]
    received_at: float


def clamp_snapshot_frames(value: int | None) -> int:
    if value is None:
        return DEFAULT_SNAPSHOT_FRAMES
    return min(max(value, MIN_SNAPSHOT_FRAMES), MAX_SNAPSHOT_FRAMES)


def _coerce_int(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]

    rank = (len(sorted_values) - 1) * percentile
    lower_index = math.floor(rank)
    upper_index = math.ceil(rank)
    if lower_index == upper_index:
        return sorted_values[int(rank)]

    lower = sorted_values[lower_index]
    upper = sorted_values[upper_index]
    return lower + (upper - lower) * (rank - lower_index)


def _scale_bounds(values: Iterable[float]) -> tuple[float, float]:
    finite_values = sorted(value for value in values if math.isfinite(value))
    if not finite_values:
        return 0.0, 1.0

    low = _percentile(finite_values, 0.05)
    high = _percentile(finite_values, 0.95)
    if high <= low:
        low = finite_values[0]
        high = finite_values[-1]
    if high <= low:
        high = low + 1.0
    return low, high


def _normalize_to_byte(value: float, low: float, high: float) -> int:
    if not math.isfinite(value):
        return 0
    normalized = (value - low) / (high - low)
    normalized = min(max(normalized, 0.0), 1.0)
    return int(round(normalized * 255))


def _sequence_delta(previous: int, current: int) -> int:
    if current >= previous:
        return current - previous
    return UINT32_SIZE - previous + current


class DeviceCsiLiveBuffer:
    def __init__(self, *, max_frames: int = MAX_SNAPSHOT_FRAMES) -> None:
        self._lock = threading.RLock()
        self._max_frames = max_frames
        self._reset(None)

    def _reset(self, session: str | None) -> None:
        self.session = session
        self.subcarrier_count = 0
        self.frames: deque[CsiLiveFrame] = deque(maxlen=self._max_frames)
        self.received_frames = 0
        self.lost_frames = 0
        self.seq_gap_events = 0
        self.seq_reset_events = 0
        self.last_sequence: int | None = None
        self.last_timestamp_us: int | None = None
        self.last_rssi: int | None = None
        self.last_update_iso: str | None = None
        self.last_update_monotonic: float | None = None

    def push_frame(
        self,
        *,
        session: str | None,
        sequence: int | None,
        timestamp_us: int | None,
        rssi: int | None,
        amplitude: Iterable[float],
    ) -> None:
        values = tuple(float(value) for value in amplitude)
        if not values:
            return

        normalized_session = (session or "").strip() or None
        sequence = _coerce_int(sequence)
        timestamp_us = _coerce_int(timestamp_us)
        rssi = _coerce_int(rssi)
        received_at = time.monotonic()

        with self._lock:
            if self.session != normalized_session:
                self._reset(normalized_session)
            if self.subcarrier_count and len(values) != self.subcarrier_count:
                self._reset(normalized_session)
            if not self.subcarrier_count:
                self.subcarrier_count = len(values)

            if sequence is not None:
                self._record_sequence(sequence)

            self.frames.append(
                CsiLiveFrame(
                    sequence=sequence,
                    timestamp_us=timestamp_us,
                    rssi=rssi,
                    amplitude=values,
                    received_at=received_at,
                )
            )
            self.received_frames += 1
            self.last_sequence = sequence
            self.last_timestamp_us = timestamp_us
            self.last_rssi = rssi
            self.last_update_iso = _utc_now_iso()
            self.last_update_monotonic = received_at

    def _record_sequence(self, sequence: int) -> None:
        if self.last_sequence is None:
            return

        delta = _sequence_delta(self.last_sequence, sequence)
        if delta == 0:
            return
        if delta >= SEQ_RESET_THRESHOLD:
            self.seq_reset_events += 1
            return
        if delta > 1:
            self.lost_frames += delta - 1
            self.seq_gap_events += 1

    def clear_session(self, session: str | None) -> bool:
        normalized_session = (session or "").strip() or None
        with self._lock:
            if normalized_session is not None and self.session != normalized_session:
                return False
            self._reset(None)
            return True

    def is_empty(self) -> bool:
        with self._lock:
            return not self.frames

    def snapshot(self, requested_frames: int | None = None) -> dict:
        columns = clamp_snapshot_frames(requested_frames)
        with self._lock:
            frames = list(self.frames)[-columns:]
            session = self.session
            subcarrier_count = self.subcarrier_count
            received_frames = self.received_frames
            lost_frames = self.lost_frames
            seq_gap_events = self.seq_gap_events
            seq_reset_events = self.seq_reset_events
            last_sequence = self.last_sequence
            last_timestamp_us = self.last_timestamp_us
            last_rssi = self.last_rssi
            last_update_iso = self.last_update_iso
            last_update_monotonic = self.last_update_monotonic

        if not frames or subcarrier_count <= 0:
            return {
                "ok": False,
                "reason": "no_live_csi",
                "session": session,
                "frames": columns,
                "available_frames": 0,
                "subcarriers": 0,
                "matrix_b64": "",
            }

        low, high = _scale_bounds(
            value for frame in frames for value in frame.amplitude
        )
        left_padding = columns - len(frames)
        matrix = bytearray(columns * subcarrier_count)
        for column in range(columns):
            frame_index = column - left_padding
            if frame_index < 0:
                continue
            frame = frames[frame_index]
            for row in range(subcarrier_count):
                value = frame.amplitude[row] if row < len(frame.amplitude) else 0.0
                matrix[row * columns + column] = _normalize_to_byte(
                    value,
                    low,
                    high,
                )

        if len(frames) >= 2:
            elapsed = frames[-1].received_at - frames[0].received_at
            fps = (len(frames) - 1) / elapsed if elapsed > 0 else 0.0
        else:
            fps = 0.0
        total_sequences = received_frames + lost_frames
        loss_rate = lost_frames / total_sequences if total_sequences else 0.0
        last_update_age_ms = (
            int((time.monotonic() - last_update_monotonic) * 1000)
            if last_update_monotonic is not None
            else None
        )

        return {
            "ok": True,
            "session": session,
            "frames": columns,
            "available_frames": len(frames),
            "subcarriers": subcarrier_count,
            "matrix_b64": base64.b64encode(matrix).decode("ascii"),
            "matrix_encoding": "uint8_base64_row_major",
            "latest_column": columns - 1,
            "scale": {
                "mode": "p5_p95",
                "min": low,
                "max": high,
            },
            "stats": {
                "received_frames": received_frames,
                "lost_frames": lost_frames,
                "loss_rate": loss_rate,
                "seq_gap_events": seq_gap_events,
                "seq_reset_events": seq_reset_events,
                "fps": fps,
                "last_sequence": last_sequence,
                "last_timestamp_us": last_timestamp_us,
                "last_rssi": last_rssi,
                "last_update_at": last_update_iso,
                "last_update_age_ms": last_update_age_ms,
            },
        }


class CsiLiveBufferService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._buffers: dict[str, DeviceCsiLiveBuffer] = {}

    def push_frame(
        self,
        *,
        device_name: str,
        session: str | None,
        sequence: int | None,
        timestamp_us: int | None,
        rssi: int | None,
        amplitude: Iterable[float],
    ) -> None:
        device_key = (device_name or "").strip()
        if not device_key:
            return

        with self._lock:
            buffer = self._buffers.get(device_key)
            if buffer is None:
                buffer = DeviceCsiLiveBuffer()
                self._buffers[device_key] = buffer

        buffer.push_frame(
            session=session,
            sequence=sequence,
            timestamp_us=timestamp_us,
            rssi=rssi,
            amplitude=amplitude,
        )

    def get_snapshot(self, device_name: str, frames: int | None = None) -> dict:
        device_key = (device_name or "").strip()
        columns = clamp_snapshot_frames(frames)
        with self._lock:
            buffer = self._buffers.get(device_key)
        if buffer is None:
            return {
                "ok": False,
                "reason": "no_live_csi",
                "session": None,
                "frames": columns,
                "available_frames": 0,
                "subcarriers": 0,
                "matrix_b64": "",
            }
        return buffer.snapshot(columns)

    def clear_session(self, device_name: str, session: str | None) -> None:
        device_key = (device_name or "").strip()
        if not device_key:
            return
        if session is None:
            self.clear_device(device_key)
            return

        with self._lock:
            buffer = self._buffers.get(device_key)
        if buffer is None:
            return
        if buffer.clear_session(session) and buffer.is_empty():
            with self._lock:
                if self._buffers.get(device_key) is buffer:
                    self._buffers.pop(device_key, None)

    def clear_device(self, device_name: str) -> None:
        device_key = (device_name or "").strip()
        if not device_key:
            return
        with self._lock:
            self._buffers.pop(device_key, None)


csi_live_buffer_service = CsiLiveBufferService()
