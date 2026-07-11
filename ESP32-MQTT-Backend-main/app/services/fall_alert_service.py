from __future__ import annotations

import logging
import threading
from collections import defaultdict
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any

from flask import has_app_context
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import Device, FallEvent, isoformat, utc_now
from .fall_algorithm_client import AlgorithmAlert
from .wechat_notify_service import send_fall_alert
from .websocket_service import websocket_hub


logger = logging.getLogger(__name__)


class FallAlertService:
    """Persist and notify valid Docker fall alerts.

    Docker can emit multiple alerts while the user has not confirmed or ignored
    the first one. The database pending event is the source of truth; the
    per-device lock only serializes in-process alert workers around that query
    and commit.
    """

    def __init__(self, app) -> None:
        self.app = app
        self._locks: dict[str, threading.RLock] = defaultdict(threading.RLock)

    def handle_algorithm_alert(
        self,
        *,
        device_name: str,
        session: str | None,
        alert: AlgorithmAlert,
        network_quality: str | None = None,
    ) -> dict[str, Any]:
        owns_context = not has_app_context()
        context = self.app.app_context() if owns_context else nullcontext()
        with context:
            try:
                return self._handle_algorithm_alert(
                    device_name=device_name,
                    session=session,
                    alert=alert,
                    network_quality=network_quality,
                )
            except (IntegrityError, SQLAlchemyError):
                db.session.rollback()
                logger.exception(
                    "FALL_ALERT_DB_ERROR device=%s session=%s operation=alert",
                    device_name,
                    session,
                )
                return {"ok": False, "created": False, "error": "db_error"}
            finally:
                if owns_context:
                    db.session.remove()

    def _handle_algorithm_alert(
        self,
        *,
        device_name: str,
        session: str | None,
        alert: AlgorithmAlert,
        network_quality: str | None,
    ) -> dict[str, Any]:
        now = utc_now()
        algorithm_timestamp = _parse_algorithm_timestamp(alert.timestamp)
        with self._locks[device_name]:
            device = db.session.scalar(
                db.select(Device)
                .options(joinedload(Device.owner))
                .where(Device.device_name == device_name)
            )
            if device is None:
                logger.warning(
                    "FALL_ALERT_DEVICE_MISSING device=%s session=%s "
                    "operation=alert",
                    device_name,
                    session,
                )
                return {"ok": False, "created": False, "error": "device_missing"}

            pending = db.session.scalar(
                db.select(FallEvent)
                .where(
                    FallEvent.device_id == device.id,
                    FallEvent.status == "pending",
                )
                .order_by(FallEvent.occurred_at.asc())
                .limit(1)
            )
            if pending is not None:
                pending.alert_count = int(pending.alert_count or 1) + 1
                pending.last_detected_at = now
                pending.max_confidence = max(
                    pending.max_confidence or 0.0,
                    alert.confidence,
                )
                pending.algorithm_source = "docker"
                pending.algorithm_class = alert.algorithm_class
                pending.algorithm_confidence = alert.confidence
                pending.algorithm_timestamp = algorithm_timestamp
                pending.network_quality = network_quality or device.network_quality
                db.session.commit()
                logger.info(
                    "FALL_ALERT_AGGREGATED device=%s session=%s "
                    "operation=alert event_id=%s alert_count=%s",
                    device.device_name,
                    session,
                    pending.id,
                    pending.alert_count,
                )
                return {
                    "ok": True,
                    "created": False,
                    "fall_event_id": pending.id,
                    "alert_count": pending.alert_count,
                }

            event = FallEvent(
                user_id=device.owner_user_id,
                device_id=device.id,
                device_name=device.device_name,
                session=session,
                result=1,
                network_quality=network_quality or device.network_quality,
                occurred_at=now,
                status="pending",
                notified=True,
                notified_at=now,
                alert_count=1,
                last_detected_at=now,
                max_confidence=alert.confidence,
                algorithm_source="docker",
                algorithm_class=alert.algorithm_class,
                algorithm_confidence=alert.confidence,
                algorithm_timestamp=algorithm_timestamp,
            )
            db.session.add(event)
            db.session.commit()
            event_id = event.id
            payload = {
                "fall_event_id": event_id,
                "device_id": device.id,
                "device_name": device.device_name,
                "session": session,
                "result": 1,
                "fall_detected": True,
                "status": event.status,
                "network_quality": event.network_quality,
                "occurred_at": isoformat(event.occurred_at),
            }

        delivered = websocket_hub.push_to_user(
            device.owner_user_id,
            "detection.fall-result",
            device.device_name,
            payload,
        )
        try:
            wechat = send_fall_alert(event_id, triggered_by="docker")
        except Exception:
            db.session.rollback()
            logger.exception(
                "FALL_ALERT_WECHAT_ERROR device=%s session=%s operation=notify "
                "event_id=%s",
                device.device_name,
                session,
                event_id,
            )
            wechat = {"ok": False, "sent": False, "reason": "exception"}

        logger.info(
            "FALL_ALERT_CREATED device=%s session=%s operation=alert "
            "event_id=%s websocket_delivered=%s wechat_sent=%s",
            device.device_name,
            session,
            event_id,
            delivered,
            wechat.get("sent"),
        )
        return {
            "ok": True,
            "created": True,
            "fall_event_id": event_id,
            "websocket_delivered": delivered,
            "wechat": wechat,
        }


def _parse_algorithm_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    for parser in (_parse_iso_timestamp, _parse_common_timestamp):
        parsed = parser(text)
        if parsed is not None:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    return None


def _parse_iso_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_common_timestamp(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None
