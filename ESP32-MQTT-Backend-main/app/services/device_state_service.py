from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import selectinload

from ..extensions import db
from ..models import (
    Device,
    FallEvent,
    User,
    isoformat,
    public_fault_payload,
    utc_now,
)
from ..mqtt.client import generate_message_id
from .csi_feature_service import raw_iq_to_amplitude
from .csi_live_buffer_service import csi_live_buffer_service
from .csi_payload_service import CsiPayloadError, decode_csi_payload
from .csi_quality_service import CsiQualityTracker
from .fall_detect_service import predict_fall
from .mqtt_service import MqttManager, MqttUnavailable
from .websocket_service import websocket_hub
from .wechat_notify_service import (
    DEVICE_FAULT_NOTICE_CODES,
    send_device_fault_notice,
)


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
        self.quality = CsiQualityTracker(
            expected_interval_seconds=float(
                app.config["CSI_EXPECTED_INTERVAL_SECONDS"]
            ),
            soft_timeout_seconds=float(app.config["CSI_SOFT_TIMEOUT_SECONDS"]),
            recovery_grace_seconds=float(
                app.config["CSI_RECOVERY_GRACE_SECONDS"]
            ),
        )
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
        self._fault_reset_pending: dict[str, tuple[float, str | None]] = {}
        self._last_fault_event: dict[tuple[str, str], float] = {}
        self._fault_notice_sent: set[tuple[str, str]] = set()
        self._parse_error_count: dict[tuple[str, str], int] = {}
        self._last_timeout_log: dict[tuple[str, str, str], float] = {}
        self._last_offline_scan_failure_log = 0.0
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
        if action not in {"start", "stop", "reset"}:
            raise ControlError(
                "INVALID_ACTION",
                "action 只能是 start、stop 或 reset",
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
            elif action == "stop":
                response = self._stop_device(device, idempotency_key)
            else:
                response = self._reset_device_fault(device, idempotency_key)

            if cache_key is not None:
                self._idempotency[cache_key] = (
                    time.monotonic(),
                    device.device_name,
                    action,
                    response,
                )
            return response

    def reset_device_fault(
        self,
        user: User,
        device: Device,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        return self.control_device(user, device, "reset", idempotency_key)

    def _start_device(
        self,
        device: Device,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        if not device.enabled:
            raise ControlError("DEVICE_DISABLED", "设备已被禁用", 409)
        if device.state == "error" or device.fault_code:
            fault = public_fault_payload(
                device_name=device.device_name,
                display_name=device.display_name,
                location=device.location,
                code=device.fault_code,
                raw_message=device.fault_message,
            )
            raise ControlError(
                "DEVICE_ERROR",
                fault["message"] or "设备存在故障",
                409,
            )
        if device.state != "online":
            raise ControlError("DEVICE_OFFLINE", "设备离线，无法启动检测", 409)

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
                reason="user_start",
                source="user",
            )
        except MqttUnavailable as exc:
            raise ControlError(
                "MQTT_UNAVAILABLE",
                "设备控制服务暂时不可用",
                503,
            ) from exc

        now = utc_now()

        device.current_session = session
        device.detection_state = "starting"
        device.runtime_state = "uploading"
        device.state = "online"
        device.last_seen_at = now
        device.last_online_at = now
        device.last_csi_at = None
        device.network_quality = "unknown"
        self._clear_session(device.device_name, None)

        logger.info(
            "START_MARK_ONLINE device=%s session=%s last_seen_at=%s",
            device.device_name,
            session,
            isoformat(now),
        )

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
                reason="user_stop",
                source="user",
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

    def _reset_device_fault(
        self,
        device: Device,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        if not device.enabled:
            raise ControlError("DEVICE_DISABLED", "设备已被禁用", 409)
        if not (
            device.fault_code
            or device.runtime_state == "fault"
            or device.state == "error"
        ):
            raise ControlError(
                "DEVICE_NOT_FAULTED",
                "设备当前没有待复位故障",
                409,
            )

        session = device.current_session or ""
        command_id = idempotency_key or generate_message_id("cmd")
        try:
            self.mqtt.publish_control(
                device.device_name,
                "reset",
                session,
                command_id,
                reason="user_fault_confirm",
                source="user",
            )
        except MqttUnavailable as exc:
            raise ControlError(
                "MQTT_UNAVAILABLE",
                "设备控制服务暂时不可用",
                503,
            ) from exc

        device.detection_state = "stopping"
        device.network_quality = "unknown"
        self._fault_reset_pending[device.device_name] = (
            time.monotonic(),
            session or None,
        )
        db.session.commit()

        logger.info(
            "RESET_FAULT_PUBLISHED device=%s session=%s",
            device.device_name,
            session or "",
        )

        return {
            "accepted": True,
            "device_name": device.device_name,
            "action": "reset",
            "control_state": "published",
            "session": session or None,
            "message": "复位命令已发送",
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
        if (
            not online
            and device.state == "offline"
            and device.runtime_state == "idle"
            and device.detection_state == "idle"
            and device.current_session is None
            and device.network_quality == "unknown"
        ):
            return
        device.last_seen_at = now

        if online:
            device.last_online_at = now
            if (
                device.state != "error"
                and device.runtime_state != "fault"
                and not device.fault_code
            ):
                device.state = "online"
        else:
            if (
                device.state == "error"
                or device.runtime_state == "fault"
                or device.fault_code
            ):
                device.last_seen_at = now
                db.session.commit()
                websocket_hub.push_to_user(
                    device.owner_user_id,
                    "device.state.changed",
                    device.device_name,
                    {
                        "state": device.state,
                        "runtime_state": device.runtime_state,
                        "fault_code": device.fault_code,
                        "last_seen_at": isoformat(device.last_seen_at),
                    },
                )
                return

            hard_timeout = float(self.app.config["CSI_HARD_TIMEOUT_SECONDS"])

            if self._is_running_like(device, now):
                last_signal = self._last_running_signal_at(device)
                gap = (
                    (now - last_signal).total_seconds()
                    if last_signal is not None
                    else None
                )
                if gap is not None and gap <= hard_timeout:
                    logger.warning(
                        "IGNORE_TRANSIENT_OFFLINE device=%s session=%s gap=%.1f "
                        "action=keep_running",
                        device.device_name,
                        device.current_session,
                        gap,
                    )
                    device.last_seen_at = now
                    db.session.commit()
                    return

            self._mark_device_offline(device)

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
        reported_session = str(payload.get("session") or "").strip()
        if runtime_state == "fault":
            device.last_status_at = now
            if reported_session and not device.current_session:
                device.current_session = reported_session
            self._record_fault(
                device,
                code=str(
                    payload.get("code")
                    or payload.get("fault_code")
                    or device.fault_code
                    or "DEVICE_FAULT_STATE"
                ),
                message=str(
                    payload.get("msg")
                    or payload.get("message")
                    or payload.get("fault_message")
                    or device.fault_message
                    or "设备状态进入 fault"
                ),
                now=now,
            )
            return

        if (
            runtime_state == "idle"
            and self._consume_fault_reset_pending(device, reported_session)
        ):
            old_session = device.current_session
            device.last_status_at = now
            self._clear_fault(device, old_session, now)
            db.session.commit()
            self._push_runtime_event(
                device,
                {
                    "control_ok": True,
                    "action": "reset",
                    "fault_cleared": True,
                    "message": "设备异常已确认，设备已恢复到待检测状态",
                },
            )
            return

        if (
            device.state == "error"
            or device.runtime_state == "fault"
            or device.fault_code
        ):
            device.last_seen_at = now
            device.last_status_at = now
            db.session.commit()
            logger.info(
                "Ignored non-fault status for %s while fault is pending",
                device.device_name,
            )
            return

        old_session = device.current_session
        device.last_seen_at = now
        device.last_status_at = now
        device.runtime_state = runtime_state
        if device.state != "error":
            device.state = "online"
            device.last_online_at = now

        if runtime_state == "uploading":
            if (
                reported_session
                and device.current_session
                and reported_session != device.current_session
            ):
                logger.info(
                    "Ignored uploading status with stale session for %s",
                    device.device_name,
                )
                db.session.commit()
                return
            if reported_session and not device.current_session:
                device.current_session = reported_session
                device.last_csi_at = None
                self._clear_session(device.device_name, None)
            if (
                device.detection_state in {"starting", "running"}
                or device.current_session
            ) and device.detection_state != "stopping":
                device.detection_state = "running"
        elif runtime_state == "booting":
            device.detection_state = "idle"
            device.current_session = None
            device.network_quality = "unknown"
            self._clear_session(device.device_name, old_session)
        elif (
            runtime_state == "idle"
            and device.detection_state in {"starting", "running", "stopping"}
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
        reported_session = str(payload.get("session") or "").strip()
        if (
            reported_session
            and device.current_session
            and reported_session != device.current_session
        ):
            logger.info(
                "Ignored fault with stale session for %s",
                device.device_name,
            )
            return
        if reported_session and not device.current_session:
            device.current_session = reported_session

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
        if (
            action not in {"start", "stop", "reset", "clear_fault"}
            or ok is None
        ):
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
        if action in {"reset", "clear_fault"}:
            if ok:
                self._clear_fault(device, old_session, now)
            else:
                self._fault_reset_pending.pop(device.device_name, None)
                device.state = "error"
                device.runtime_state = "fault"
                device.network_quality = "unknown"
            db.session.commit()
            self._push_runtime_event(
                device,
                {
                    "control_ok": ok,
                    "action": action,
                    "err": payload.get("err", 0),
                    "fault_cleared": ok,
                    "message": (
                        "设备异常已确认，设备已恢复到待检测状态"
                        if ok
                        else str(
                            payload.get("msg")
                            or payload.get("message")
                            or "设备复位失败"
                        )
                    ),
                },
            )
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
            if device.state == "error" or device.fault_code:
                device.runtime_state = "fault"
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
        session = str(payload.get("session") or "").strip()
        if (
            device.state == "error"
            or device.runtime_state == "fault"
            or device.detection_state not in {"starting", "running"}
        ):
            return
        if not device.current_session and session:
            device.current_session = session
        if not device.current_session or session != device.current_session:
            logger.info(
                "Ignored CSI with stale session for %s",
                device.device_name,
            )
            return

        try:
            batch = decode_csi_payload(payload)
        except CsiPayloadError as exc:
            self._record_csi_parse_error(device, session, str(exc))
            return

        self._push_live_csi_frames(device.device_name, session, batch)

        now = utc_now()
        previous_quality = device.network_quality
        old_runtime_state = device.runtime_state
        old_detection_state = device.detection_state
        quality_result = self.quality.add(
            device.device_name,
            session,
            batch,
            now,
        )
        quality = quality_result.quality
        device.last_seen_at = now
        device.last_csi_at = now
        device.runtime_state = "uploading"
        device.detection_state = "running"
        device.network_quality = quality
        if device.state != "error":
            device.state = "online"
            device.last_online_at = now
        self._parse_error_count.pop((device.device_name, session), None)

        key = (device.device_name, session)
        window = self._csi_windows[key]
        if quality_result.seq_reset:
            window.clear()
            logger.info(
                "CSI_SEQ_RESET device=%s session=%s prev_seq=%s new_seq0=%s "
                "gap=%s action=treat_as_hardware_restart",
                device.device_name,
                session,
                quality_result.previous_seq1,
                quality_result.new_seq0,
                quality_result.gap_seconds,
            )
        window.append(batch.to_algorithm_input())
        fall_event = None
        window_size = int(self.app.config["CSI_WINDOW_SIZE"])
        window_frame_count = sum(
            len(item.get("frames", [])) for item in window
        )
        if (
            window_frame_count >= window_size
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

        if (
            old_runtime_state != device.runtime_state
            or old_detection_state != device.detection_state
        ):
            self._push_runtime_event(device)

        if (
            previous_quality in {"poor", "fair"}
            and quality in {"fair", "good"}
            and quality != previous_quality
        ):
            logger.info(
                "CSI_RECOVERED device=%s session=%s network_quality=%s",
                device.device_name,
                session,
                quality,
            )

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

    def _push_live_csi_frames(
        self,
        device_name: str,
        session: str,
        batch: Any,
    ) -> None:
        for frame in batch.frames:
            try:
                amplitude = raw_iq_to_amplitude(frame.raw_csi)
                if not amplitude:
                    continue
                csi_live_buffer_service.push_frame(
                    device_name=device_name,
                    session=session,
                    sequence=frame.sequence,
                    timestamp_us=frame.timestamp_us,
                    rssi=frame.rssi,
                    amplitude=amplitude,
                )
            except Exception as exc:
                logger.debug(
                    "Ignored CSI live frame for %s session=%s seq=%s: %s",
                    device_name,
                    session,
                    getattr(frame, "sequence", None),
                    exc,
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
        csi_live_buffer_service.clear_device(device.device_name)
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
            fault_data = public_fault_payload(
                device_name=device.device_name,
                display_name=device.display_name,
                location=device.location,
                code=code,
                raw_message=message,
            )
            websocket_hub.push_to_user(
                device.owner_user_id,
                "device.fault",
                device.device_name,
                {
                    "state": "error",
                    **fault_data,
                    "auto_stop_requested": should_stop,
                },
            )

        notice_code = code.upper()
        notice_key = (device.device_name, notice_code)
        if (
            notice_code in DEVICE_FAULT_NOTICE_CODES
            and notice_key not in self._fault_notice_sent
        ):
            self._fault_notice_sent.add(notice_key)
            if device.owner is not None:
                try:
                    result = send_device_fault_notice(
                        device.owner,
                        device,
                        code=code,
                        message=message,
                    )
                    logger.info(
                        "DEVICE_FAULT_WECHAT_NOTICE device=%s code=%s sent=%s "
                        "reason=%s",
                        device.device_name,
                        code,
                        result.get("sent"),
                        result.get("reason"),
                    )
                except Exception:
                    logger.exception(
                        "Failed to send device fault notice for %s",
                        device.device_name,
                    )

        if not should_stop or stop_key is None or session is None:
            return
        self._fault_stop_requested.add(stop_key)
        self._log_auto_stop(device, session, f"fault:{code}")
        try:
            self.mqtt.publish_control(
                device.device_name,
                "stop",
                session,
                generate_message_id("fault-stop"),
                reason=f"fault:{code}",
                source="auto",
            )
        except MqttUnavailable:
            self._fault_stop_requested.discard(stop_key)
            logger.exception(
                "Failed to publish automatic stop for faulted device %s",
                device.device_name,
            )

    def _record_csi_parse_error(
        self,
        device: Device,
        session: str,
        message: str,
    ) -> None:
        key = (device.device_name, session or device.current_session or "")
        count = self._parse_error_count.get(key, 0) + 1
        self._parse_error_count[key] = count
        logger.warning(
            "CSI_PARSE_ERROR device=%s session=%s count=%s action=ignore_once "
            "error=%s",
            device.device_name,
            key[1],
            count,
            message,
        )
        if count < int(self.app.config["CSI_PARSE_ERROR_LIMIT"]):
            return

        if device.network_quality == "poor":
            return
        device.network_quality = "poor"
        db.session.commit()
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

    def _log_auto_stop(
        self,
        device: Device,
        session: str,
        reason: str,
    ) -> None:
        logger.warning(
            "AUTO_STOP device=%s session=%s reason=%s "
            "last_seen_at=%s last_status_at=%s last_csi_at=%s "
            "network_quality=%s fault_code=%s fault_message=%s "
            "detection_state=%s runtime_state=%s",
            device.device_name,
            session,
            reason,
            isoformat(device.last_seen_at),
            isoformat(device.last_status_at),
            isoformat(device.last_csi_at),
            device.network_quality,
            device.fault_code,
            device.fault_message,
            device.detection_state,
            device.runtime_state,
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

    def _clear_fault(
        self,
        device: Device,
        old_session: str | None,
        now: datetime,
    ) -> None:
        self._fault_reset_pending.pop(device.device_name, None)
        device.state = "online"
        device.runtime_state = "idle"
        device.detection_state = "idle"
        device.current_session = None
        device.network_quality = "unknown"
        device.fault_code = None
        device.fault_message = None
        device.last_seen_at = now
        device.last_online_at = now
        self._clear_session(device.device_name, old_session)
        if old_session:
            self._fault_stop_requested.discard(
                (device.device_name, old_session)
            )
        self._fault_stop_requested = {
            key
            for key in self._fault_stop_requested
            if key[0] != device.device_name
        }
        self._fault_notice_sent = {
            key
            for key in self._fault_notice_sent
            if key[0] != device.device_name
        }
        for key in [
            key for key in self._last_fault_event if key[0] == device.device_name
        ]:
            self._last_fault_event.pop(key, None)

    def _consume_fault_reset_pending(
        self,
        device: Device,
        reported_session: str,
    ) -> bool:
        pending = self._fault_reset_pending.get(device.device_name)
        if pending is None:
            return False
        created_at, pending_session = pending
        if time.monotonic() - created_at > 120:
            self._fault_reset_pending.pop(device.device_name, None)
            return False
        if (
            reported_session
            and pending_session
            and reported_session != pending_session
        ):
            return False
        return True

    def _mark_device_offline(self, device: Device) -> None:
        old_session = device.current_session
        self._fault_reset_pending.pop(device.device_name, None)
        device.state = "offline"
        device.runtime_state = "idle"
        device.detection_state = "idle"
        device.current_session = None
        device.network_quality = "unknown"
        device.fault_code = None
        device.fault_message = None
        self._clear_session(device.device_name, old_session)
        if old_session:
            self._fault_stop_requested.discard(
                (device.device_name, old_session)
            )
        self._fault_notice_sent = {
            key
            for key in self._fault_notice_sent
            if key[0] != device.device_name
        }
        for key in [
            key for key in self._last_fault_event if key[0] == device.device_name
        ]:
            self._last_fault_event.pop(key, None)

    def scan_offline_devices(self) -> int:
        try:
            return self._scan_offline_devices()
        except Exception:
            db.session.rollback()
            raise

    def _scan_offline_devices(self) -> int:
        now = utc_now()
        offline_timeout = self.app.config["OFFLINE_TIMEOUT_SECONDS"]
        expected_interval = float(
            self.app.config["CSI_EXPECTED_INTERVAL_SECONDS"]
        )
        normal_timeout = max(3.0, expected_interval * 2)
        soft_timeout = float(self.app.config["CSI_SOFT_TIMEOUT_SECONDS"])
        recovery_grace = float(self.app.config["CSI_RECOVERY_GRACE_SECONDS"])
        hard_timeout = float(self.app.config["CSI_HARD_TIMEOUT_SECONDS"])
        changed: list[dict[str, Any]] = []
        quality_changed: list[dict[str, Any]] = []

        def offline_event(device: Device) -> dict[str, Any]:
            return {
                "user_id": device.owner_user_id,
                "device_name": device.device_name,
                "data": {
                    "state": "offline",
                    "last_seen_at": isoformat(device.last_seen_at),
                },
            }

        def quality_event(
            device: Device,
            network_quality: str,
        ) -> dict[str, Any]:
            return {
                "user_id": device.owner_user_id,
                "device_name": device.device_name,
                "data": {
                    "session": device.current_session,
                    "network_quality": network_quality,
                    "last_csi_at": isoformat(device.last_csi_at),
                },
            }

        with self._lock:
            devices = db.session.scalars(
                db.select(Device).where(Device.state != "offline")
            ).all()
            for device in devices:
                if self._is_running_like(device, now):
                    last_signal = self._last_running_signal_at(device)
                    if last_signal is None:
                        continue
                    gap = (now - last_signal).total_seconds()

                    if gap > hard_timeout:
                        old_session = device.current_session
                        if (
                            old_session
                            and (
                                device.detection_state
                                in {"starting", "running"}
                                or device.runtime_state == "uploading"
                            )
                        ):
                            logger.warning(
                                "CSI_HARD_TIMEOUT_FAULT device=%s "
                                "session=%s gap=%.1f action=record_fault",
                                device.device_name,
                                old_session,
                                gap,
                            )
                            self._record_fault(
                                device,
                                code="NO_CSI_FRAME_TIMEOUT",
                                message=(
                                    "启动检测后未收到 CSI 数据，请检查 A 板供电、"
                                    "A/B 板链路或采集源"
                                ),
                                now=now,
                            )
                            continue

                        logger.warning(
                            "CSI_HARD_TIMEOUT device=%s session=%s gap=%.1f "
                            "action=mark_offline",
                            device.device_name,
                            old_session,
                            gap,
                        )
                        self._mark_device_offline(device)
                        changed.append(offline_event(device))
                        continue

                    if device.detection_state == "starting":
                        start_grace = float(
                            self.app.config["START_GRACE_SECONDS"]
                        )
                        if gap <= start_grace:
                            self._log_csi_timeout(
                                device,
                                gap,
                                "START_GRACE",
                                "keep_starting",
                            )
                            continue

                        if device.network_quality != "poor":
                            device.network_quality = "poor"
                            quality_changed.append(
                                quality_event(device, "poor")
                            )
                        self._log_csi_timeout(
                            device,
                            gap,
                            "START_GRACE_EXCEEDED",
                            "degrade_quality_keep_starting",
                        )
                        continue

                    target_quality = None
                    if gap > recovery_grace:
                        target_quality = "poor"
                        self._log_csi_timeout(
                            device,
                            gap,
                            "CSI_RECOVERY_GRACE",
                            "keep_running_wait_hardware_recovery",
                        )
                    elif gap > soft_timeout:
                        target_quality = "poor"
                        self._log_csi_timeout(
                            device,
                            gap,
                            "CSI_SOFT_TIMEOUT",
                            "degrade_quality_only",
                        )
                    elif gap > normal_timeout:
                        target_quality = "fair"

                    if (
                        target_quality is not None
                        and device.network_quality != target_quality
                    ):
                        device.network_quality = target_quality
                        quality_changed.append(
                            quality_event(device, target_quality)
                        )
                    continue

                last_seen_at = _aware(device.last_seen_at)
                if (
                    last_seen_at is not None
                    and (now - last_seen_at).total_seconds() > offline_timeout
                ):
                    self._mark_device_offline(device)
                    changed.append(offline_event(device))

            if changed or quality_changed:
                db.session.commit()

        for event in changed:
            websocket_hub.push_to_user(
                event["user_id"],
                "device.state.changed",
                event["device_name"],
                event["data"],
            )
        for event in quality_changed:
            websocket_hub.push_to_user(
                event["user_id"],
                "detection.network-quality",
                event["device_name"],
                event["data"],
            )
        return len(changed)

    def _is_running_like(self, device: Device, now: datetime) -> bool:
        if device.detection_state in {"starting", "running"}:
            return True
        if device.runtime_state == "uploading":
            return True
        last_csi_at = _aware(device.last_csi_at)
        return bool(
            device.current_session
            and last_csi_at is not None
            and (
                now - last_csi_at
            ).total_seconds() <= float(
                self.app.config["CSI_HARD_TIMEOUT_SECONDS"]
            )
        )

    def _last_running_signal_at(self, device: Device) -> datetime | None:
        last_csi_at = _aware(device.last_csi_at)
        if last_csi_at is not None:
            return last_csi_at

        candidates = [
            _aware(device.last_seen_at),
            _aware(device.last_status_at),
            _aware(device.updated_at),
        ]
        return max(
            (value for value in candidates if value is not None),
            default=None,
        )

    def _log_csi_timeout(
        self,
        device: Device,
        gap: float,
        event_name: str,
        action: str,
    ) -> None:
        session = device.current_session or ""
        key = (device.device_name, session, event_name)
        now = time.monotonic()
        if now - self._last_timeout_log.get(key, 0) < 10:
            return
        self._last_timeout_log[key] = now
        logger.info(
            "%s device=%s session=%s gap=%.1f action=%s",
            event_name,
            device.device_name,
            session,
            gap,
            action,
        )

    def _log_offline_scan_failure(self, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_offline_scan_failure_log >= 30:
            self._last_offline_scan_failure_log = now
            self.app.logger.exception("Device offline scan failed")
            return
        self.app.logger.warning("Device offline scan failed: %s", exc)

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
                with self.app.app_context():
                    try:
                        self.scan_offline_devices()
                    except Exception as exc:
                        self._log_offline_scan_failure(exc)
                    finally:
                        db.session.remove()

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
        csi_live_buffer_service.clear_session(device_name, session)
        if session is None:
            for key in [
                key for key in self._csi_windows if key[0] == device_name
            ]:
                self._csi_windows.pop(key, None)
                self._fall_emitted.discard(key)
            for key in [
                key for key in self._parse_error_count if key[0] == device_name
            ]:
                self._parse_error_count.pop(key, None)
            for key in [
                key for key in self._last_timeout_log if key[0] == device_name
            ]:
                self._last_timeout_log.pop(key, None)
            return
        key = (device_name, session)
        self._csi_windows.pop(key, None)
        self._fall_emitted.discard(key)
        self._parse_error_count.pop(key, None)
        for timeout_key in [
            timeout_key
            for timeout_key in self._last_timeout_log
            if timeout_key[0] == device_name and timeout_key[1] == session
        ]:
            self._last_timeout_log.pop(timeout_key, None)
