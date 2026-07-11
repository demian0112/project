from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from ..models import Device
from .csi_algorithm_formatter import (
    CsiAlgorithmFormatError,
    CsiAlgorithmFormatter,
    CsiAlgorithmFormatterConfig,
)
from .csi_payload_service import CsiBatch
from .fall_alert_service import FallAlertService
from .fall_algorithm_client import (
    AlgorithmAlert,
    FallAlgorithmClient,
    FallAlgorithmClientError,
    WebSocketTransport,
)
from .fall_algorithm_config import device_algorithm_config


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class QueuedFrame:
    line: str
    interval_seconds: float


class _StreamState:
    def __init__(
        self,
        *,
        device_name: str,
        session: str,
        algorithm_config: dict[str, Any],
        queue_max_frames: int,
        network_quality: str | None,
    ) -> None:
        self.device_name = device_name
        self.session = session
        self.algorithm_config = algorithm_config
        self.network_quality = network_quality
        self.queue_max_frames = queue_max_frames
        self.queue: deque[QueuedFrame] = deque()
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.websocket: WebSocketTransport | None = None
        self.sender_thread: threading.Thread | None = None
        self.receiver_thread: threading.Thread | None = None
        self.next_send_at: float | None = None
        self.sent_frames = 0

    def enqueue(self, frames: list[QueuedFrame]) -> int:
        if not frames:
            return 0
        dropped = 0
        with self.condition:
            if len(frames) >= self.queue_max_frames:
                dropped += len(frames) - self.queue_max_frames
                frames = frames[-self.queue_max_frames :]
                self.queue.clear()
            overflow = len(self.queue) + len(frames) - self.queue_max_frames
            for _ in range(max(0, overflow)):
                self.queue.popleft()
                dropped += 1
            self.queue.extend(frames)
            self.condition.notify_all()
        return dropped

    def get_frame(self, timeout: float) -> QueuedFrame | None:
        with self.condition:
            while not self.stop_event.is_set() and not self.queue:
                self.condition.wait(timeout=timeout)
                if not self.queue:
                    return None
            if self.stop_event.is_set() or not self.queue:
                return None
            return self.queue.popleft()

    def clear_queue(self) -> int:
        with self.condition:
            count = len(self.queue)
            self.queue.clear()
            self.next_send_at = None
            self.condition.notify_all()
            return count

    def set_websocket(self, websocket: WebSocketTransport) -> None:
        with self.condition:
            self.websocket = websocket
            self.condition.notify_all()

    def wait_for_websocket(self, timeout: float) -> WebSocketTransport | None:
        with self.condition:
            while not self.stop_event.is_set() and self.websocket is None:
                self.condition.wait(timeout=timeout)
                if self.websocket is None:
                    return None
            return self.websocket

    def detach_websocket(
        self,
        websocket: WebSocketTransport | None = None,
    ) -> WebSocketTransport | None:
        with self.condition:
            if websocket is not None and self.websocket is not websocket:
                return None
            current = self.websocket
            self.websocket = None
            self.condition.notify_all()
            return current

    def stop(self) -> WebSocketTransport | None:
        self.stop_event.set()
        self.clear_queue()
        websocket = self.detach_websocket()
        with self.condition:
            self.condition.notify_all()
        return websocket


class CsiAlgorithmStreamService:
    """Own per-device CSI forwarding workers for the Docker fall algorithm."""

    def __init__(
        self,
        app,
        *,
        client: FallAlgorithmClient | None = None,
        alert_service: FallAlertService | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.app = app
        self.client = client or FallAlgorithmClient.from_app_config(app.config)
        self.alert_service = alert_service or FallAlertService(app)
        self.formatter = CsiAlgorithmFormatter(
            CsiAlgorithmFormatterConfig.from_app_config(app.config)
        )
        self.clock = clock
        self.sleeper = sleeper
        self._streams: dict[tuple[str, str], _StreamState] = {}
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return bool(self.app.config.get("FALL_ALGORITHM_ENABLED", True))

    def frame_interval_seconds(self, frame_count: int) -> float:
        if frame_count <= 0:
            return 0.0
        batch_interval = float(
            self.app.config.get("FALL_ALGORITHM_BATCH_INTERVAL_SECONDS", 1.5)
        )
        return batch_interval / frame_count

    def start_stream(self, device: Device, session: str) -> bool:
        if not self.enabled or not session:
            return False

        key = (device.device_name, session)
        with self._lock:
            existing = self._streams.get(key)
            if existing is not None:
                existing.network_quality = device.network_quality
                return True

            for old_key in [
                old_key
                for old_key in self._streams
                if old_key[0] == device.device_name and old_key != key
            ]:
                old_state = self._streams.pop(old_key)
                self._stop_state(old_state, reset=True)

            if (
                bool(self.app.config.get("FALL_ALGORITHM_SINGLE_ACTIVE_STREAM", True))
                and self._streams
            ):
                active_device, active_session = next(iter(self._streams))
                logger.warning(
                    "FALL_ALGORITHM_BUSY device=%s session=%s operation=start "
                    "active_device=%s active_session=%s",
                    device.device_name,
                    session,
                    active_device,
                    active_session,
                )
                return False

            state = _StreamState(
                device_name=device.device_name,
                session=session,
                algorithm_config=device_algorithm_config(device),
                queue_max_frames=max(
                    1,
                    int(self.app.config.get("FALL_ALGORITHM_QUEUE_MAX_FRAMES", 2000)),
                ),
                network_quality=device.network_quality,
            )
            self._streams[key] = state
            state.sender_thread = threading.Thread(
                target=self._send_worker,
                args=(state,),
                name=f"fall-algorithm-send-{device.device_name}-{session}",
                daemon=True,
            )
            state.sender_thread.start()
            logger.info(
                "FALL_ALGORITHM_STREAM_START device=%s session=%s operation=start",
                device.device_name,
                session,
            )
            return True

    def submit_batch(
        self,
        *,
        device: Device,
        batch: CsiBatch,
        network_quality: str | None,
    ) -> bool:
        if not self.enabled:
            return False
        if batch.frame_count <= 0:
            return False
        session = batch.session
        if not self.start_stream(device, session):
            return False

        interval = self.frame_interval_seconds(batch.frame_count)
        frames: list[QueuedFrame] = []
        for frame in batch.frames:
            try:
                line = self.formatter.to_csv_line(
                    frame,
                    device_identity=device.device_name,
                )
            except CsiAlgorithmFormatError:
                logger.exception(
                    "FALL_ALGORITHM_FORMAT_ERROR device=%s session=%s "
                    "operation=format seq=%s",
                    device.device_name,
                    session,
                    getattr(frame, "sequence", None),
                )
                continue
            frames.append(QueuedFrame(line=line, interval_seconds=interval))

        key = (device.device_name, session)
        with self._lock:
            state = self._streams.get(key)
        if state is None:
            return False
        state.network_quality = network_quality
        dropped = state.enqueue(frames)
        if dropped:
            logger.warning(
                "FALL_ALGORITHM_QUEUE_OVERFLOW device=%s session=%s "
                "operation=enqueue dropped_frames=%s",
                device.device_name,
                session,
                dropped,
            )
        return True

    def stop_stream(
        self,
        device_name: str,
        session: str | None = None,
        *,
        reset: bool = True,
    ) -> None:
        with self._lock:
            if session is None:
                keys = [key for key in self._streams if key[0] == device_name]
            else:
                keys = [(device_name, session)]
            states = [
                self._streams.pop(key)
                for key in keys
                if key in self._streams
            ]
        for state in states:
            self._stop_state(state, reset=reset)

    def close_all(self) -> None:
        with self._lock:
            states = list(self._streams.values())
            self._streams.clear()
        for state in states:
            self._stop_state(state, reset=False)

    def sync_running_config(self, device: Device) -> dict[str, Any]:
        session = device.current_session
        if not session:
            return {"ok": True, "active": False}
        key = (device.device_name, session)
        with self._lock:
            state = self._streams.get(key)
            other_active = any(active_key != key for active_key in self._streams)
        if state is None:
            return {"ok": True, "active": False}
        if other_active and bool(
            self.app.config.get("FALL_ALGORITHM_SINGLE_ACTIVE_STREAM", True)
        ):
            return {
                "ok": False,
                "active": True,
                "error": "algorithm instance is used by another stream",
            }
        config = device_algorithm_config(device)
        try:
            self.client.update_config(config)
        except Exception as exc:
            logger.exception(
                "FALL_ALGORITHM_CONFIG_SYNC_FAILED device=%s session=%s "
                "operation=config",
                device.device_name,
                session,
            )
            return {"ok": False, "active": True, "error": str(exc)}
        state.algorithm_config = config
        logger.info(
            "FALL_ALGORITHM_CONFIG_SYNCED device=%s session=%s operation=config",
            device.device_name,
            session,
        )
        return {"ok": True, "active": True}

    def _stop_state(self, state: _StreamState, *, reset: bool) -> None:
        websocket = state.stop()
        if websocket is not None:
            self._close_websocket(websocket)
        logger.info(
            "FALL_ALGORITHM_STREAM_STOP device=%s session=%s operation=stop",
            state.device_name,
            state.session,
        )
        if reset:
            threading.Thread(
                target=self._reset_worker,
                args=(state.device_name, state.session),
                name=f"fall-algorithm-reset-{state.device_name}-{state.session}",
                daemon=True,
            ).start()

    def _send_worker(self, state: _StreamState) -> None:
        backoff = float(
            self.app.config.get("FALL_ALGORITHM_RECONNECT_INITIAL_SECONDS", 1.0)
        )
        max_backoff = float(
            self.app.config.get("FALL_ALGORITHM_RECONNECT_MAX_SECONDS", 30.0)
        )
        ping_interval = float(
            self.app.config.get("FALL_ALGORITHM_PING_INTERVAL_SECONDS", 30.0)
        )
        last_ping = self.clock()

        while not state.stop_event.is_set():
            if state.websocket is None and not self._connect_state(state):
                state.clear_queue()
                self._sleep_until_stopped(state, backoff)
                backoff = min(max_backoff, backoff * 2)
                continue
            backoff = float(
                self.app.config.get("FALL_ALGORITHM_RECONNECT_INITIAL_SECONDS", 1.0)
            )

            frame = state.get_frame(timeout=0.5)
            if frame is None:
                if (
                    state.websocket is not None
                    and self.clock() - last_ping >= ping_interval
                ):
                    try:
                        self.client.send_ping(state.websocket)
                        last_ping = self.clock()
                    except Exception:
                        self._disconnect_state(state)
                continue

            if not self._wait_for_send_deadline(state, frame.interval_seconds):
                break
            websocket = state.websocket
            if websocket is None:
                continue
            try:
                self.client.send_data(websocket, frame.line)
                state.sent_frames += 1
                if state.sent_frames == 1:
                    logger.info(
                        "FALL_ALGORITHM_FIRST_FRAME_SENT device=%s session=%s "
                        "operation=send",
                        state.device_name,
                        state.session,
                    )
            except Exception:
                logger.exception(
                    "FALL_ALGORITHM_SEND_FAILED device=%s session=%s "
                    "operation=send",
                    state.device_name,
                    state.session,
                )
                self._disconnect_state(state)

        self._remove_state_if_current(state)

    def _connect_state(self, state: _StreamState) -> bool:
        try:
            self.client.health_check()
            logger.info(
                "FALL_ALGORITHM_HEALTH_OK device=%s session=%s operation=health",
                state.device_name,
                state.session,
            )
            if self._can_apply_global_config(state):
                self.client.update_config(state.algorithm_config)
                logger.info(
                    "FALL_ALGORITHM_CONFIG_SYNCED device=%s session=%s "
                    "operation=config",
                    state.device_name,
                    state.session,
                )
            websocket = self.client.connect_stream()
        except Exception:
            logger.exception(
                "FALL_ALGORITHM_CONNECT_FAILED device=%s session=%s "
                "operation=connect",
                state.device_name,
                state.session,
            )
            return False

        state.set_websocket(websocket)
        if state.receiver_thread is None or not state.receiver_thread.is_alive():
            state.receiver_thread = threading.Thread(
                target=self._receive_worker,
                args=(state,),
                name=f"fall-algorithm-recv-{state.device_name}-{state.session}",
                daemon=True,
            )
            state.receiver_thread.start()
        logger.info(
            "FALL_ALGORITHM_WS_CONNECTED device=%s session=%s operation=connect",
            state.device_name,
            state.session,
        )
        return True

    def _receive_worker(self, state: _StreamState) -> None:
        while not state.stop_event.is_set():
            websocket = state.wait_for_websocket(timeout=1.0)
            if websocket is None:
                continue
            try:
                message = self.client.receive(websocket)
            except Exception:
                if not state.stop_event.is_set():
                    logger.exception(
                        "FALL_ALGORITHM_RECEIVE_FAILED device=%s session=%s "
                        "operation=receive",
                        state.device_name,
                        state.session,
                    )
                    self._disconnect_state(state, websocket)
                continue
            if isinstance(message, AlgorithmAlert):
                self.alert_service.handle_algorithm_alert(
                    device_name=state.device_name,
                    session=state.session,
                    alert=message,
                    network_quality=state.network_quality,
                )

    def _wait_for_send_deadline(
        self,
        state: _StreamState,
        interval_seconds: float,
    ) -> bool:
        now = self.clock()
        if state.next_send_at is None or now > state.next_send_at:
            state.next_send_at = now
        while not state.stop_event.is_set():
            now = self.clock()
            next_send_at = state.next_send_at
            if next_send_at is None:
                return True
            remaining = next_send_at - now
            if remaining <= 0:
                break
            self.sleeper(min(remaining, 0.25))
        if state.stop_event.is_set():
            return False
        now = self.clock()
        next_send_at = state.next_send_at
        if next_send_at is None:
            state.next_send_at = now + interval_seconds
        else:
            state.next_send_at = max(next_send_at, now) + interval_seconds
        return True

    def _disconnect_state(
        self,
        state: _StreamState,
        websocket: WebSocketTransport | None = None,
    ) -> None:
        closed = state.detach_websocket(websocket)
        dropped = state.clear_queue()
        if closed is not None:
            self._close_websocket(closed)
        logger.warning(
            "FALL_ALGORITHM_WS_DISCONNECTED device=%s session=%s "
            "operation=disconnect dropped_frames=%s",
            state.device_name,
            state.session,
            dropped,
        )

    def _reset_worker(self, device_name: str, session: str) -> None:
        with self._lock:
            other_active = bool(self._streams)
        if other_active:
            logger.info(
                "FALL_ALGORITHM_RESET_SKIPPED device=%s session=%s "
                "operation=reset reason=other_active_stream",
                device_name,
                session,
            )
            return
        try:
            self.client.reset()
            logger.info(
                "FALL_ALGORITHM_RESET_OK device=%s session=%s operation=reset",
                device_name,
                session,
            )
        except FallAlgorithmClientError:
            logger.exception(
                "FALL_ALGORITHM_RESET_FAILED device=%s session=%s operation=reset",
                device_name,
                session,
            )

    def _can_apply_global_config(self, state: _StreamState) -> bool:
        with self._lock:
            return all(
                key == (state.device_name, state.session)
                for key in self._streams
            )

    def _remove_state_if_current(self, state: _StreamState) -> None:
        key = (state.device_name, state.session)
        with self._lock:
            if self._streams.get(key) is state:
                self._streams.pop(key, None)

    def _sleep_until_stopped(self, state: _StreamState, seconds: float) -> None:
        deadline = self.clock() + seconds
        while not state.stop_event.is_set() and self.clock() < deadline:
            self.sleeper(min(deadline - self.clock(), 0.25))

    @staticmethod
    def _close_websocket(websocket: WebSocketTransport) -> None:
        try:
            websocket.close()
        except Exception:
            logger.debug("Ignored error while closing algorithm WebSocket")
