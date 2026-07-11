from __future__ import annotations

from typing import Any
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from .auth import admin_required, csrf_is_valid
from .extensions import db
from .models import Device, FallEvent, User, isoformat, utc_now
from .services.csi_live_buffer_service import (
    DEFAULT_SNAPSHOT_FRAMES,
    clamp_snapshot_frames,
    csi_live_buffer_service,
)
from .services.fall_algorithm_config import (
    AlgorithmConfigError,
    apply_algorithm_config,
    validate_algorithm_config,
)
from .services.wechat_notify_service import send_fall_alert
from .services.websocket_service import websocket_hub


api_bp = Blueprint("api", __name__, url_prefix="/api")
USER_STATUSES = {"active", "disabled"}
DEVICE_STATUSES = {"enabled", "disabled"}
USER_ROLES = {"user", "admin"}
FALL_EVENT_STATUSES = {"pending", "confirmed", "ignored"}


def error_response(message: str, status_code: int):
    return jsonify({"error": message}), status_code


def commit_or_conflict(message: str):
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return error_response(message, 409)
    return None


def parse_owner_id(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@api_bp.before_request
@admin_required
def protect_api():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if not csrf_is_valid():
            return error_response("invalid or missing CSRF token", 403)
    return None


@api_bp.get("")
def api_index():
    return jsonify(
        {
            "name": "ESP32 MQTT Admin API",
            "resources": [
                "/api/users",
                "/api/devices",
                "/api/fall-events",
            ],
        }
    )


@api_bp.get("/users")
def list_users():
    statement = (
        db.select(User)
        .options(selectinload(User.devices))
        .order_by(User.id)
    )
    users = db.session.scalars(statement).all()
    return jsonify([user.to_dict() for user in users])


@api_bp.route("/users/<int:user_id>", methods=["GET", "PUT", "DELETE"])
def user_detail(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        return error_response("user not found", 404)

    if request.method == "GET":
        return jsonify(user.to_dict())

    if request.method == "DELETE":
        deleted_devices = len(user.devices)
        db.session.delete(user)
        db.session.commit()
        return jsonify(
            {
                "deleted": True,
                "deleted_devices": deleted_devices,
            }
        )

    data = request.get_json(silent=True) or {}
    status = str(data.get("status", user.status)).strip().lower()
    role = str(data.get("role", user.role)).strip().lower()
    nickname = str(
        data.get("nickname", user.nickname or "") or ""
    ).strip()
    phone = str(data.get("phone", user.phone or "") or "").strip()

    if status not in USER_STATUSES:
        return error_response("status must be active or disabled", 400)
    if role not in USER_ROLES:
        return error_response("role must be user or admin", 400)
    if len(nickname) > 64:
        return error_response("nickname must not exceed 64 characters", 400)
    if len(phone) > 32:
        return error_response("phone must not exceed 32 characters", 400)

    user.nickname = nickname or None
    user.phone = phone or None
    user.status = status
    user.role = role
    db.session.commit()
    return jsonify(user.to_dict())


def validate_device_data(
    data: dict[str, Any],
    *,
    current: Device | None = None,
) -> tuple[dict[str, Any] | None, tuple[Any, int] | None]:
    device_uid = str(
        data.get("device_uid", current.device_uid if current else "")
    ).strip()
    name = str(data.get("name", current.name if current else "")).strip()
    mqtt_topic = str(data.get("mqtt_topic", "")).strip()
    owner_value = data.get("owner_id", current.owner_id if current else None)
    owner_id = parse_owner_id(owner_value)
    status = str(
        data.get("status", current.status if current else "enabled")
    ).lower()
    location = str(
        data.get("location", current.location if current else "") or ""
    ).strip()
    remark = str(
        data.get("remark", current.remark if current else "") or ""
    ).strip()

    if current is not None and device_uid != current.device_uid:
        return None, error_response("device_uid cannot be changed", 400)
    if not Device.is_valid_device_uid(device_uid):
        return None, error_response(
            "device_uid must contain 1-32 letters, numbers, _ or -",
            400,
        )
    if not name or len(name) > 64:
        return None, error_response("name must contain 1-64 characters", 400)
    expected_topic = f"csi/v1/devices/{device_uid}/up/csi"
    if mqtt_topic and mqtt_topic != expected_topic:
        return None, error_response(
            "mqtt_topic is generated from device_uid and cannot be changed",
            400,
        )
    if owner_id is None:
        return None, error_response("owner_id must be an integer", 400)
    if db.session.get(User, owner_id) is None:
        return None, error_response("owner user does not exist", 404)
    if status not in DEVICE_STATUSES:
        return None, error_response("status must be enabled or disabled", 400)
    if len(location) > 128 or len(remark) > 1000:
        return None, error_response("location or remark is too long", 400)

    return {
        "device_name": device_uid,
        "display_name": name,
        "owner_user_id": owner_id,
        "enabled": status == "enabled",
        "location": location or None,
        "remark": remark or None,
    }, None


def validate_device_algorithm_data(
    data: dict[str, Any],
    *,
    current: Device,
) -> tuple[dict[str, Any] | None, tuple[Any, int] | None]:
    try:
        return validate_algorithm_config(data, current=current), None
    except AlgorithmConfigError as exc:
        return None, error_response(str(exc), 400)


@api_bp.get("/devices")
def list_devices():
    statement = (
        db.select(Device)
        .options(selectinload(Device.owner))
        .order_by(Device.id)
    )
    devices = db.session.scalars(statement).all()
    return jsonify([device.to_dict() for device in devices])


@api_bp.post("/devices")
def create_device():
    data = request.get_json(silent=True) or {}
    values, validation_error = validate_device_data(data)
    if validation_error:
        return validation_error
    algorithm_values = None
    if "algorithm_config" in data:
        try:
            algorithm_values = validate_algorithm_config(
                dict(data.get("algorithm_config") or {})
            )
        except AlgorithmConfigError as exc:
            return error_response(str(exc), 400)

    device = Device(**values)
    if algorithm_values is not None:
        apply_algorithm_config(device, algorithm_values)
    db.session.add(device)
    conflict = commit_or_conflict("device_uid already exists")
    if conflict:
        return conflict
    if current_app.config["MQTT_AUTOSTART_DEVICES"]:
        current_app.extensions["device_coordinator"].mqtt.ensure_device(
            device.device_name
        )
    return jsonify(device.to_dict()), 201


@api_bp.route("/devices/<int:device_id>", methods=["GET", "PUT", "DELETE"])
def device_detail(device_id: int):
    device = db.session.get(Device, device_id)
    if device is None:
        return error_response("device not found", 404)

    if request.method == "GET":
        return jsonify(device.to_dict())
    if request.method == "DELETE":
        device_name = device.device_name
        current_app.extensions[
            "device_coordinator"
        ].algorithm_stream.stop_stream(device_name, None)
        db.session.delete(device)
        db.session.commit()
        current_app.extensions["device_coordinator"].mqtt.remove_device(
            device_name
        )
        return jsonify({"deleted": True})

    data = request.get_json(silent=True) or {}
    values, validation_error = validate_device_data(data, current=device)
    if validation_error:
        return validation_error
    algorithm_values = None
    if "algorithm_config" in data:
        algorithm_values, algorithm_error = validate_device_algorithm_data(
            dict(data.get("algorithm_config") or {}),
            current=device,
        )
        if algorithm_error:
            return algorithm_error

    for key, value in values.items():
        setattr(device, key, value)
    if algorithm_values is not None:
        apply_algorithm_config(device, algorithm_values)

    conflict = commit_or_conflict("device_uid already exists")
    if conflict:
        return conflict
    runtime_sync = {"ok": True, "active": False}
    if algorithm_values is not None and device.current_session:
        runtime_sync = current_app.extensions[
            "device_coordinator"
        ].algorithm_stream.sync_running_config(device)
    if device.enabled and current_app.config["MQTT_AUTOSTART_DEVICES"]:
        current_app.extensions["device_coordinator"].mqtt.ensure_device(
            device.device_name
        )
    elif not device.enabled:
        current_app.extensions["device_coordinator"].mqtt.remove_device(
            device.device_name
        )
    payload = device.to_dict()
    payload["runtime_sync"] = runtime_sync
    status_code = 200 if runtime_sync.get("ok") else 202
    return jsonify(payload), status_code


@api_bp.put("/devices/<int:device_id>/fall-algorithm-config")
def update_device_algorithm_config(device_id: int):
    device = db.session.get(Device, device_id)
    if device is None:
        return error_response("device not found", 404)
    data = request.get_json(silent=True) or {}
    values, validation_error = validate_device_algorithm_data(
        data,
        current=device,
    )
    if validation_error:
        return validation_error

    apply_algorithm_config(device, values)
    db.session.commit()
    runtime_sync = current_app.extensions[
        "device_coordinator"
    ].algorithm_stream.sync_running_config(device)
    payload = device.to_dict()
    payload["runtime_sync"] = runtime_sync
    status_code = 200 if runtime_sync.get("ok") else 202
    return jsonify(payload), status_code


@api_bp.get("/devices/<string:device_name>/csi-heatmap")
def device_csi_heatmap(device_name: str):
    device = db.session.scalar(
        db.select(Device).where(Device.device_name == device_name)
    )
    if device is None:
        return error_response("device not found", 404)

    try:
        requested_frames = int(
            request.args.get("frames", DEFAULT_SNAPSHOT_FRAMES)
        )
    except (TypeError, ValueError):
        return error_response("frames must be an integer", 400)

    frames = clamp_snapshot_frames(requested_frames)
    heatmap = csi_live_buffer_service.get_snapshot(device.device_name, frames)
    return jsonify(
        {
            "ok": bool(heatmap.get("ok")),
            "device": {
                "id": device.id,
                "device_name": device.device_name,
                "display_name": device.display_name,
                "state": device.state,
                "runtime_state": device.runtime_state,
                "detection_state": device.detection_state,
                "current_session": device.current_session,
                "network_quality": device.network_quality,
                "last_seen_at": isoformat(device.last_seen_at),
                "last_csi_at": isoformat(device.last_csi_at),
            },
            "heatmap": heatmap,
        }
    )


@api_bp.post("/devices/<int:device_id>/simulate-fall")
def simulate_device_fall(device_id: int):
    device = db.session.get(
        Device,
        device_id,
        options=[joinedload(Device.owner)],
    )
    if device is None:
        return error_response("device not found", 404)
    if not device.owner_user_id:
        return error_response(
            "device is not bound to a user; cannot simulate fall alert",
            400,
        )
    user = device.owner
    if user is None:
        return error_response(
            "device owner does not exist; cannot simulate fall alert",
            400,
        )
    if not user.wx_openid:
        return error_response(
            "device owner has no wx_openid; ask the user to login first",
            400,
        )

    data = request.get_json(silent=True) or {}
    remark = str(
        data.get("remark") or "admin simulated fall trigger"
    ).strip()
    if len(remark) > 1000:
        return error_response("remark is too long", 400)
    send_wechat = bool(data.get("send_wechat", True))
    now = utc_now()
    event = FallEvent(
        user_id=user.id,
        device_id=device.id,
        device_name=device.device_name,
        session=f"admin-simulated-{uuid4().hex}",
        result=1,
        network_quality=device.network_quality or "unknown",
        occurred_at=now,
        status="pending",
        notified=True,
        notified_at=now,
        alert_count=1,
        last_detected_at=now,
        algorithm_source="admin",
        remark=remark,
    )
    db.session.add(event)
    db.session.commit()

    delivered = websocket_hub.push_to_user(
        user.id,
        "detection.fall-result",
        device.device_name,
        {
            "fall_event_id": event.id,
            "device_id": device.id,
            "device_name": device.device_name,
            "session": event.session,
            "result": 1,
            "fall_detected": True,
            "status": event.status,
            "network_quality": event.network_quality,
            "occurred_at": isoformat(event.occurred_at),
            "triggered_by": "admin",
        },
    )

    if send_wechat:
        wechat = send_fall_alert(event.id, triggered_by="admin")
    else:
        wechat = {
            "enabled": bool(current_app.config["WECHAT_NOTIFY_ENABLED"]),
            "ok": True,
            "sent": False,
            "errcode": None,
            "errmsg": "",
            "reason": "skipped_by_request",
            "remaining_count": None,
        }

    message = (
        "simulated fall event created"
        if wechat.get("sent") or not send_wechat
        else "simulated fall event created, but WeChat notify was not sent"
    )
    return jsonify(
        {
            "ok": True,
            "message": message,
            "fall_event": {
                "id": event.id,
                "device_id": device.id,
                "device_name": device.device_name,
                "display_name": device.display_name,
                "location": device.location,
                "status": event.status,
                "occurred_at": isoformat(event.occurred_at),
                "network_quality": event.network_quality,
                "remark": event.remark,
            },
            "websocket": {
                "event": "detection.fall-result",
                "delivered": delivered,
            },
            "wechat": wechat,
        }
    ), 201


@api_bp.get("/fall-events")
def list_fall_events():
    status = str(request.args.get("status", "")).strip().lower()
    if status and status not in FALL_EVENT_STATUSES:
        return error_response("invalid fall event status", 400)
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        return error_response("limit must be an integer", 400)
    limit = min(max(limit, 1), 500)

    statement = (
        db.select(FallEvent)
        .options(
            joinedload(FallEvent.user),
            joinedload(FallEvent.device),
        )
        .order_by(FallEvent.occurred_at.desc())
        .limit(limit)
    )
    if status:
        statement = statement.where(FallEvent.status == status)
    events = db.session.scalars(statement).all()
    return jsonify([event.to_admin_dict() for event in events])


@api_bp.patch("/fall-events/<int:event_id>")
def update_fall_event(event_id: int):
    event = db.session.get(FallEvent, event_id)
    if event is None:
        return error_response("fall event not found", 404)

    data = request.get_json(silent=True) or {}
    status = str(data.get("status", event.status)).strip().lower()
    remark = str(data.get("remark", event.remark or "") or "").strip()
    if status not in FALL_EVENT_STATUSES:
        return error_response("invalid fall event status", 400)
    if len(remark) > 1000:
        return error_response("remark is too long", 400)

    event.status = status
    event.remark = remark or None
    event.handled_at = None if status == "pending" else utc_now()
    db.session.commit()
    return jsonify(event.to_admin_dict())
