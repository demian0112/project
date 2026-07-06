from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import selectinload

from ..extensions import db
from ..models import Device, FallEvent, User, isoformat, utc_now
from ..mqtt.client import generate_message_id
from .csi_payload_service import CsiPayloadError, decode_csi_payload
from .csi_quality_service import CsiQualityTracker
from .fall_detect_service import predict_fall
from .mqtt_service import MqttManager, MqttUnavailable
from .websocket_service import websocket_hub


logger = logging.getLogger(__name__)
HARDWARE_RUNTIME_STATES = {"booting", "idle", "uploading", "fault"}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _payload_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "online", "ok"}:
            return True
        if normalized in {"false", "0", "offline"}:
            return False
    return None


class ControlError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class DeviceCoordinator:
    """Coordinate database snapshots, MQTT traffic and frontend events."""

    def __init__(self, app) -> None:
        self.app = app
        self.quality = CsiQualityTracker()
        self.mqtt = MqttManager(app, self.handle_mqtt_payload)
        self._csi_windows: dict[
            tuple[str, str], deque[dict[str, Any]]
        ] = defaultdict(
            lambda: deque(
                maxlen=max(20, int(app.config["CSI_WINDOW_SIZE"]))
            )
        )
        self._fall_emitted: set[tuple[str, str]] = set()
        self._fault_stop_requested: set[tuple[str, str]] = set()
        self._last_fault_event: dict[tuple[str, str], float] = {}
        self._idempotency: dict[
            tuple[int, str],
            tuple[float, str, str, dict[str, Any]],
        ] = {}
        self._lock = threading.RLock()
        self._monitor_started = False

    def ensure_all_devices(self) -> None:
        device_names = db.session.scalars(
            db.select(Device.device_name).where(Device.enabled.is_(True))
        ).all()
        for device_name in device_names:
            self.mqtt.ensure_device(device_name)

    def ensure_for_user(self, user_id: int) -> None:
        device_names = db.session.scalars(
            db.select(Device.device_name).where(
                Device.owner_user_id == user_id,
                Device.enabled.is_(True),
            )
        ).all()
        for device_name in device_names:
            self.mqtt.ensure_device(device_name)

    def control_device(
        self,
        user: User,
        device: Device,
        action: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        if action not in {"start", "stop"}:
            raise ControlError(
                "INVALID_ACTION",
                "action 只能是 start 或 stop",
                400,
            )

        cache_key = (
            (user.id, idempotency_key) if idempotency_key else None
        )
        with self._lock:
            if cache_key is not None:
                cached = self._idempotency.get(cache_key)
                if cached is not None and time.monotonic() - cached[0] < 300:
                    _, cached_device, cached_action, cached_response = cached
                    if (
                        cached_device != device.device_name
                        or cached_action != action
                    ):
                        raise ControlError(
                            "IDEMPOTENCY_CONFLICT",
                            "该 Idempotency-Key 已用于其他控制请求",
                            409,
                        )
                    return cached_response

            if action == "start":
                response = self._start_device(device, idempotency_key)
            else:
                response = self._stop_device(device, idempotency_key)

            if cache_key is not None:
                self._idempotency[cache_key] = (
                    time.monotonic(),
                    device.device_name,
                    action,
                    response,
                )
            return response

    def _start_device(
        self,
        device: Device,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        if not device.enabled:
            raise ControlError("DEVICE_DISABLED", "设备已被禁用", 409)
        if device.state == "error" or device.fault_code:
            raise ControlError(
                "DEVICE_ERROR",
                device.fault_message or "设备存在故障",
                409,
            )
        if device.state != "online":
            raise ControlError("DEVICE_OFFLINE", "设备离线，无法启动检测", 409)

        last_status_at = _aware(device.last_status_at)
        status_timeout = self.app.config["STATUS_TIMEOUT_SECONDS"]
        if (
            last_status_at is None
            or (utc_now() - last_status_at).total_seconds() > status_timeout
        ):
            raise ControlError(
                "STATUS_TIMEOUT",
                "设备运行状态已超时，请稍后重试",
                409,
            )
        if device.detection_state != "idle":
            raise ControlError(
                "CONTROL_BUSY",
                "设备正在检测或控制处理中",
                409,
            )

        session = generate_message_id("sess")
        command_id = idempotency_key or generate_message_id("cmd")
        try:
            self.mqtt.publish_control(
                device.device_name,
                "start",
                session,
                command_id,
            )
        except MqttUnavailable as exc:
            raise ControlError(
                "MQTT_UNAVAILABLE",
                "设备控制服务暂时不可用",
                503,
            ) from exc

        device.current_session = session
        device.detection_state = "starting"
        device.network_quality = "unknown"
        db.session.commit()

        return {
            "accepted": True,
            "device_name": device.device_name,
            "action": "start",
            "control_state": "published",
            "session": session,
            "message": "启动命令已发送",
        }

    def _stop_device(
        self,
        device: Device,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        if device.detection_state not in {"starting", "running"}:
            raise ControlError(
                "CONTROL_BUSY",
                "设备当前没有正在运行的检测任务",
                409,
            )
        if not device.current_session:
            raise ControlError(
                "CONTROL_BUSY",
                "设备当前检测 session 无效",
                409,
            )

        session = device.current_session
        command_id = idempotency_key or generate_message_id("cmd")
        try:
            self.mqtt.publish_control(
                device.device_name,
                "stop",
                session,
                command_id,
            )
        except MqttUnavailable as exc:
            raise ControlError(
                "MQTT_UNAVAILABLE",
                "设备控制服务暂时不可用",
                503,
            ) from exc

        device.detection_state = "stopping"
        device.network_quality = "unknown"
        self._clear_session(device.device_name, session)
        db.session.commit()

        return {
            "accepted": True,
            "device_name": device.device_name,
            "action": "stop",
            "control_state": "published",
            "session": session,
            "message": "停止命令已发送",
        }

    def handle_mqtt_payload(
        self,
        device_name: str,
        topic_name: str,
        payload: dict[str, Any],
    ) -> None:
        with self._lock:
            device = db.session.scalar(
                db.select(Device)
                .options(selectinload(Device.owner))
                .where(Device.device_name == device_name)
            )
            if device is None:
                return

            handlers = {
                "online": self._handle_online,
                "wifi": self._handle_wifi,
                "status": self._handle_status,
                "csi": self._handle_csi,
                "ack": self._handle_ack,
                "fault": self._handle_fault,
            }
            handler = handlers.get(topic_name)
            if handler is not None:
                handler(device, payload)

    def _handle_online(
        self,
        device: Device,
        payload: dict[str, Any],
    ) -> None:
        now = utc_now()
        value = payload.get(
            "status",
            payload.get("online", payload.get("state")),
        )
        online = _payload_bool(value)
        if online is None:
            logger.warning(
                "Ignored invalid online payload for %s: %r",
                device.device_name,
                value,
            )
            return
        device.last_seen_at = now

        if online:
            device.state = "online"
            device.last_online_at = now
            device.fault_code = None
            device.fault_message = None
            self._fault_stop_requested = {
                key
                for key in self._fault_stop_requested
                if key[0] != device.device_name
            }
        else:
            old_session = device.current_session
            device.state = "offline"
            device.detection_state = "idle"
            device.runtime_state = "idle"
            device.current_session = None
            device.network_quality = "unknown"
            self._clear_session(device.device_name, old_session)
            if old_session:
                self._fault_stop_requested.discard(
                    (device.device_name, old_session)
                )

        db.session.commit()
        websocket_hub.push_to_user(
            device.owner_user_id,
            "device.state.changed",
            device.device_name,
            {
                "state": device.state,
                "last_seen_at": isoformat(device.last_seen_at),
            },
        )

    def _handle_wifi(
        self,
        device: Device,
        payload: dict[str, Any],
    ) -> None:
        ok = _payload_bool(payload.get("ok"))
        if ok is not True:
            return
        device.last_seen_at = utc_now()
        if device.state != "error":
            device.state = "online"
            device.last_online_at = device.last_seen_at
        db.session.commit()

    def _handle_status(
        self,
        device: Device,
        payload: dict[str, Any],
    ) -> None:
        now = utc_now()
        runtime_state = str(payload.get("state") or "").strip().lower()
        if runtime_state not in HARDWARE_RUNTIME_STATES:
            logger.warning(
                "Ignored invalid status state for %s: %r",
                device.device_name,
                runtime_state,
            )
            return
        if runtime_state == "fault":
            device.last_status_at = now
            self._record_fault(
                device,
                code=str(payload.get("code") or "DEVICE_FAULT_STATE"),
                message=str(
                    payload.get("msg")
                    or payload.get("message")
                    or "设备状态进入 fault"
                ),
                now=now,
            )
            return

        old_session = device.current_session
        reported_session = str(payload.get("session") or "").strip()
        device.last_seen_at = now
        device.last_status_at = now
        device.runtime_state = runtime_state
        if device.state != "error":
            device.state = "online"
            device.last_online_at = now

        if runtime_state == "uploading":
            if reported_session and (
                not device.current_session
                or device.detection_state not in {"starting", "stopping"}
            ):
                device.current_session = reported_session
            if device.detection_state != "stopping":
                device.detection_state = (
                    "starting"
                    if (
                        reported_session
                        and device.current_session
                        and reported_session != device.current_session
                    )
                    else "running"
                )
        elif runtime_state == "booting":
            device.detection_state = "idle"
            device.current_session = None
            device.network_quality = "unknown"
            self._clear_session(device.device_name, old_session)
        elif (
            runtime_state == "idle"
            and device.detection_state == "stopping"
        ):
            device.detection_state = "idle"
            device.current_session = None
            device.network_quality = "unknown"
            self._clear_session(device.device_name, old_session)
        elif device.detection_state not in {"starting", "stopping"}:
            device.detection_state = "idle"
            device.current_session = None
            device.network_quality = "unknown"
            self._clear_session(device.device_name, old_session)

        db.session.commit()
        self._push_runtime_event(device)

    def _handle_fault(
        self,
        device: Device,
        payload: dict[str, Any],
    ) -> None:
        self._record_fault(
            device,
            code=str(
                payload.get("code", payload.get("fault_code", "UNKNOWN"))
            ),
            message=str(
                payload.get(
                    "msg",
                    payload.get(
                        "message",
                        payload.get("fault_message", "设备上报故障"),
                    ),
                )
            ),
            now=utc_now(),
        )

    def _handle_ack(
        self,
        device: Device,
        payload: dict[str, Any],
    ) -> None:
        if str(payload.get("cmd") or "control").lower() != "control":
            return
        action = str(payload.get("action") or "").strip().lower()
        ok = _payload_bool(payload.get("ok"))
        if action not in {"start", "stop"} or ok is None:
            return

        pending_state = "starting" if action == "start" else "stopping"
        final_state = "running" if action == "start" else "idle"
        if device.detection_state not in {pending_state, final_state}:
            logger.info(
                "Ignored stale %s ACK for %s in detection state %s",
                action,
                device.device_name,
                device.detection_state,
            )
            return

        ack_session = str(payload.get("session") or "").strip()
        if (
            ack_session
            and device.current_session
            and ack_session != device.current_session
        ):
            logger.info(
                "Ignored ACK with stale session for %s",
                device.device_name,
            )
            return

        now = utc_now()
        old_session = device.current_session
        ack_state = str(payload.get("state") or "").strip().lower()
        device.last_seen_at = now
        if action == "start":
            if ok:
                device.runtime_state = (
                    ack_state
                    if ack_state in HARDWARE_RUNTIME_STATES
                    else "uploading"
                )
                device.detection_state = "running"
                if device.state != "error":
                    device.state = "online"
                    device.last_online_at = now
            else:
                device.runtime_state = (
                    ack_state
                    if ack_state in HARDWARE_RUNTIME_STATES
                    else "idle"
                )
                device.detection_state = "idle"
                device.current_session = None
                device.network_quality = "unknown"
                self._clear_session(device.device_name, old_session)
        elif ok:
            device.runtime_state = (
                ack_state
                if ack_state in HARDWARE_RUNTIME_STATES
                else "idle"
            )
            device.detection_state = "idle"
            device.current_session = None
            device.network_quality = "unknown"
            self._clear_session(device.device_name, old_session)
            if old_session:
                self._fault_stop_requested.discard(
                    (device.device_name, old_session)
                )
        else:
            device.runtime_state = (
                ack_state
                if ack_state in HARDWARE_RUNTIME_STATES
                else "uploading"
            )
            device.detection_state = "running"
            if old_session:
                self._fault_stop_requested.discard(
                    (device.device_name, old_session)
                )

        db.session.commit()
        self._push_runtime_event(
            device,
            {
                "control_ok": ok,
                "action": action,
                "err": payload.get("err", 0),
                "message": str(
                    payload.get("msg") or payload.get("message") or ""
                ),
            },
        )

    def _handle_csi(
        self,
        device: Device,
        payload: dict[str, Any],
    ) -> None:
        session = str(payload.get("session") or "")
        if (
            device.state != "online"
            or device.detection_state not in {"starting", "running"}
            or not device.current_session
            or session != device.current_session
        ):
            return

        try:
            batch = decode_csi_payload(payload)
        except CsiPayloadError as exc:
            logger.warning(
                "Ignored invalid CSI batch for %s: %s",
                device.device_name,
                exc,
            )
            return

        now = utc_now()
        previous_quality = device.network_quality
        quality = self.quality.add(
            device.device_name,
            session,
            batch,
            now,
        )
        device.last_seen_at = now
        device.last_csi_at = now
        device.network_quality = quality
        if device.state != "error":
            device.state = "online"

        key = (device.device_name, session)
        window = self._csi_windows[key]
        window.append(batch.to_algorithm_input())
        fall_event = None
        window_size = int(self.app.config["CSI_WINDOW_SIZE"])
        if (
            len(window) >= window_size
            and quality != "unknown"
            and key not in self._fall_emitted
        ):
            predictor = self.app.config.get("FALL_PREDICTOR", predict_fall)
            if int(predictor(device.device_name, session, list(window))) == 1:
                fall_event = FallEvent(
                    user_id=device.owner_user_id,
                    device_id=device.id,
                    device_name=device.device_name,
                    session=session,
                    result=1,
                    network_quality=quality,
                    occurred_at=now,
                    status="pending",
                    notified=True,
                    notified_at=now,
                )
                db.session.add(fall_event)
                self._fall_emitted.add(key)

        db.session.commit()

        if quality != previous_quality:
            websocket_hub.push_to_user(
                device.owner_user_id,
                "detection.network-quality",
                device.device_name,
                {
                    "session": session,
                    "network_quality": quality,
                    "last_csi_at": isoformat(now),
                },
            )

        if fall_event is not None:
            websocket_hub.push_to_user(
                device.owner_user_id,
                "detection.fall-result",
                device.device_name,
                {
                    "fall_event_id": fall_event.id,
                    "session": session,
                    "result": 1,
                    "fall_detected": True,
                    "network_quality": quality,
                    "occurred_at": isoformat(now),
                },
            )

    def _record_fault(
        self,
        device: Device,
        *,
        code: str,
        message: str,
        now: datetime,
    ) -> None:
        code = (code.strip() or "UNKNOWN")[:64]
        message = (message.strip() or "设备上报故障")[:255]
        session = device.current_session
        stop_key = (
            (device.device_name, session) if session is not None else None
        )
        should_stop = (
            stop_key is not None
            and device.detection_state in {"starting", "running", "stopping"}
            and stop_key not in self._fault_stop_requested
        )

        device.last_seen_at = now
        device.state = "error"
        device.runtime_state = "fault"
        device.fault_code = code
        device.fault_message = message
        if should_stop:
            device.detection_state = "stopping"
            device.network_quality = "unknown"
            self._clear_session(device.device_name, session)
        elif session is None:
            device.detection_state = "idle"
            device.network_quality = "unknown"
        db.session.commit()

        event_key = (device.device_name, code)
        limit = float(self.app.config["FAULT_EVENT_LIMIT_SECONDS"])
        last_event = self._last_fault_event.get(event_key)
        if last_event is None or time.monotonic() - last_event >= limit:
            self._last_fault_event[event_key] = time.monotonic()
            websocket_hub.push_to_user(
                device.owner_user_id,
                "device.fault",
                device.device_name,
                {
                    "state": "error",
                    "code": code,
                    "message": message,
                    "auto_stop_requested": should_stop,
                },
            )

        if not should_stop or stop_key is None or session is None:
            return
        self._fault_stop_requested.add(stop_key)
        try:
            self.mqtt.publish_control(
                device.device_name,
                "stop",
                session,
                generate_message_id("fault-stop"),
            )
        except MqttUnavailable:
            self._fault_stop_requested.discard(stop_key)
            logger.exception(
                "Failed to publish automatic stop for faulted device %s",
                device.device_name,
            )

    def _push_runtime_event(
        self,
        device: Device,
        extra: dict[str, Any] | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "runtime_state": device.runtime_state,
            "detection_state": device.detection_state,
            "session": device.current_session,
        }
        if extra:
            data.update(extra)
        websocket_hub.push_to_user(
            device.owner_user_id,
            "device.runtime.changed",
            device.device_name,
            data,
        )

    def scan_offline_devices(self) -> int:
        now = utc_now()
        offline_timeout = self.app.config["OFFLINE_TIMEOUT_SECONDS"]
        csi_timeout = self.app.config["CSI_TIMEOUT_SECONDS"]
        changed: list[Device] = []
        quality_changed: list[Device] = []

        with self._lock:
            devices = db.session.scalars(
                db.select(Device).where(Device.state != "offline")
            ).all()
            for device in devices:
                last_seen_at = _aware(device.last_seen_at)
                if (
                    last_seen_at is not None
                    and (now - last_seen_at).total_seconds() > offline_timeout
                ):
                    old_session = device.current_session
                    device.state = "offline"
                    device.runtime_state = "idle"
                    device.detection_state = "idle"
                    device.current_session = None
                    device.network_quality = "unknown"
                    self._clear_session(device.device_name, old_session)
                    if old_session:
                        self._fault_stop_requested.discard(
                            (device.device_name, old_session)
                        )
                    changed.append(device)
                    continue

                last_csi_at = _aware(device.last_csi_at)
                if (
                    device.detection_state == "running"
                    and last_csi_at is not None
                    and (now - last_csi_at).total_seconds() > csi_timeout
                    and device.network_quality != "poor"
                ):
                    device.network_quality = "poor"
                    quality_changed.append(device)

            if changed or quality_changed:
                db.session.commit()

        for device in changed:
            websocket_hub.push_to_user(
                device.owner_user_id,
                "device.state.changed",
                device.device_name,
                {"state": "offline", "last_seen_at": isoformat(device.last_seen_at)},
            )
        for device in quality_changed:
            websocket_hub.push_to_user(
                device.owner_user_id,
                "detection.network-quality",
                device.device_name,
                {
                    "session": device.current_session,
                    "network_quality": "poor",
                    "last_csi_at": isoformat(device.last_csi_at),
                },
            )
        return len(changed)

    def start_monitor(self) -> None:
        if (
            self._monitor_started
            or self.app.config["TESTING"]
            or not self.app.config["OFFLINE_MONITOR_ENABLED"]
        ):
            return
        self._monitor_started = True

        def monitor() -> None:
            while True:
                time.sleep(2)
                try:
                    with self.app.app_context():
                        self.scan_offline_devices()
                except Exception:
                    self.app.logger.exception("Device offline scan failed")

        threading.Thread(
            target=monitor,
            name="device-offline-monitor",
            daemon=True,
        ).start()

    def _clear_session(
        self,
        device_name: str,
        session: str | None,
    ) -> None:
        self.quality.clear(device_name, session)
        if session is None:
            for key in [
                key for key in self._csi_windows if key[0] == device_name
            ]:
                self._csi_windows.pop(key, None)
                self._fall_emitted.discard(key)
            return
        key = (device_name, session)
        self._csi_windows.pop(key, None)
        self._fall_emitted.discard(key)
