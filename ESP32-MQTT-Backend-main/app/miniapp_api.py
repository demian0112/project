from __future__ import annotations

from functools import wraps
from typing import Any, Callable, TypeVar

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy.orm import joinedload

from .extensions import db
from .models import Device, FallEvent, User, utc_now
from .services.device_state_service import ControlError
from .services.token_service import (
    AccessTokenError,
    decode_access_token,
    issue_access_token,
)
from .services.wechat_service import (
    WeChatLoginError,
    WeChatPhoneError,
    exchange_phone_code,
    exchange_wechat_code,
)


miniapp_bp = Blueprint("miniapp_api", __name__, url_prefix="/api/v1")
F = TypeVar("F", bound=Callable[..., Any])


def api_error(code: str, message: str, status_code: int):
    return jsonify({"error": code, "message": message}), status_code


def token_required(view: F) -> F:
    @wraps(view)
    def wrapped(*args, **kwargs):
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return api_error("AUTH_REQUIRED", "请先登录", 401)

        try:
            user_id = decode_access_token(token)
        except AccessTokenError:
            return api_error(
                "AUTH_REQUIRED",
                "登录状态无效或已过期",
                401,
            )

        user = db.session.get(User, user_id)
        if user is None:
            return api_error("AUTH_REQUIRED", "用户不存在", 401)
        if user.status != "active":
            return api_error("USER_DISABLED", "用户已被禁用", 403)

        g.current_miniapp_user = user
        return view(*args, **kwargs)

    return wrapped  # type: ignore[return-value]


def _owned_device(device_name: str):
    device = db.session.scalar(
        db.select(Device).where(Device.device_name == device_name)
    )
    if device is None:
        return None, api_error(
            "DEVICE_NOT_FOUND",
            "设备不存在",
            404,
        )
    if device.owner_user_id != g.current_miniapp_user.id:
        return None, api_error(
            "DEVICE_NOT_OWNED",
            "设备不属于当前用户",
            403,
        )
    return device, None


@miniapp_bp.get("")
def api_index():
    return jsonify(
        {
            "name": "Anshou CSI Mini Program API",
            "version": "v1",
            "resources": [
                "/api/v1/auth/wechat-login",
                "/api/v1/me",
                "/api/v1/devices",
                "/api/v1/fall-events",
                "/ws/v1/events",
            ],
        }
    )


@miniapp_bp.post("/auth/wechat-login")
def wechat_login():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code") or "").strip()
    create_if_missing = bool(data.get("create_if_missing", True))
    if not code:
        return api_error(
            "INVALID_WECHAT_CODE",
            "code 不能为空",
            400,
        )

    try:
        identity = exchange_wechat_code(code)
    except WeChatLoginError as exc:
        return api_error(
            "WECHAT_LOGIN_FAILED",
            str(exc),
            503 if exc.unavailable else 401,
        )

    user = db.session.scalar(
        db.select(User).where(User.wx_openid == identity.openid)
    )
    is_new_user = user is None
    if user is None and not create_if_missing:
        return api_error(
            "USER_NOT_REGISTERED",
            "用户尚未登录过",
            404,
        )
    if user is None:
        user = User(wx_openid=identity.openid)
        db.session.add(user)

    user.wx_unionid = identity.unionid or user.wx_unionid
    # session_key must never be stored as plaintext. This project currently
    # does not need to decrypt WeChat encrypted data, so it is deliberately
    # kept in memory only and wx_session_key_enc remains nullable.
    user.last_login_at = utc_now()
    db.session.commit()

    if user.status != "active":
        return api_error("USER_DISABLED", "用户已被禁用", 403)

    return jsonify(
        {
            "access_token": issue_access_token(user.id),
            "expires_in": current_app.config["TOKEN_EXPIRE_SECONDS"],
            "user": {
                "id": user.id,
                "nickname": user.nickname,
                "avatar_url": user.avatar_url,
                "is_new_user": is_new_user,
            },
        }
    )


@miniapp_bp.post("/me/phone")
@token_required
def update_phone():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code") or "").strip()
    if not code:
        return api_error(
            "INVALID_PHONE_CODE",
            "手机号授权 code 不能为空",
            400,
        )

    try:
        phone = exchange_phone_code(code)
    except WeChatPhoneError as exc:
        return api_error(
            "PHONE_AUTH_FAILED",
            str(exc),
            503 if exc.unavailable else 400,
        )

    user = g.current_miniapp_user
    user.phone = phone
    db.session.commit()
    return jsonify(user.to_public_dict())


@miniapp_bp.get("/me")
@token_required
def current_user():
    return jsonify(g.current_miniapp_user.to_public_dict())


@miniapp_bp.patch("/me/profile")
@token_required
def update_profile():
    data = request.get_json(silent=True) or {}
    user = g.current_miniapp_user

    if "nickname" in data:
        nickname = str(data.get("nickname") or "").strip()
        if len(nickname) > 64:
            return api_error(
                "INVALID_PROFILE",
                "nickname 不能超过 64 个字符",
                400,
            )
        user.nickname = nickname or None

    if "avatar_url" in data:
        avatar_url = str(data.get("avatar_url") or "").strip()
        if len(avatar_url) > 255:
            return api_error(
                "INVALID_PROFILE",
                "avatar_url 不能超过 255 个字符",
                400,
            )
        user.avatar_url = avatar_url or None

    if not {"nickname", "avatar_url"}.intersection(data):
        return api_error(
            "INVALID_PROFILE",
            "至少需要提交 nickname 或 avatar_url",
            400,
        )

    db.session.commit()
    return jsonify(user.to_public_dict())


@miniapp_bp.get("/devices")
@token_required
def list_devices():
    devices = db.session.scalars(
        db.select(Device)
        .where(Device.owner_user_id == g.current_miniapp_user.id)
        .order_by(Device.id)
    ).all()
    current_app.extensions["device_coordinator"].ensure_for_user(
        g.current_miniapp_user.id
    )
    return jsonify({"items": [device.to_summary_dict() for device in devices]})


@miniapp_bp.get("/devices/<string:device_name>")
@token_required
def device_detail(device_name: str):
    device, error = _owned_device(device_name)
    if error is not None:
        return error
    if device.enabled:
        current_app.extensions["device_coordinator"].mqtt.ensure_device(
            device.device_name
        )
    return jsonify(device.to_detail_dict())


@miniapp_bp.patch("/devices/<string:device_name>")
@token_required
def update_device_profile(device_name: str):
    device, error = _owned_device(device_name)
    if error is not None:
        return error

    data = request.get_json(silent=True) or {}
    allowed_fields = {"display_name", "location"}
    if not allowed_fields.intersection(data):
        return api_error(
            "INVALID_DEVICE_PROFILE",
            "至少需要提交 display_name 或 location",
            400,
        )

    if "display_name" in data:
        display_name = str(data.get("display_name") or "").strip()
        if not display_name:
            return api_error(
                "INVALID_DEVICE_PROFILE",
                "display_name 不能为空",
                400,
            )
        if len(display_name) > 64:
            return api_error(
                "INVALID_DEVICE_PROFILE",
                "display_name 不能超过 64 个字符",
                400,
            )
        device.display_name = display_name

    if "location" in data:
        location = str(data.get("location") or "").strip()
        if len(location) > 128:
            return api_error(
                "INVALID_DEVICE_PROFILE",
                "location 不能超过 128 个字符",
                400,
            )
        device.location = location or None

    db.session.commit()
    return jsonify(device.to_detail_dict())


@miniapp_bp.post("/devices/<string:device_name>/control")
@token_required
def control_device(device_name: str):
    device, error = _owned_device(device_name)
    if error is not None:
        return error

    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "").strip().lower()
    idempotency_key = (
        request.headers.get("Idempotency-Key", "").strip() or None
    )
    if idempotency_key is not None and len(idempotency_key) > 128:
        return api_error(
            "INVALID_IDEMPOTENCY_KEY",
            "Idempotency-Key 不能超过 128 个字符",
            400,
        )

    try:
        response = current_app.extensions[
            "device_coordinator"
        ].control_device(
            g.current_miniapp_user,
            device,
            action,
            idempotency_key,
        )
    except ControlError as exc:
        return api_error(exc.code, exc.message, exc.status_code)
    return jsonify(response), 202


@miniapp_bp.get("/fall-events")
@token_required
def list_fall_events():
    try:
        limit = int(request.args.get("limit", "20"))
    except ValueError:
        return api_error("INVALID_LIMIT", "limit 必须是整数", 400)
    limit = min(max(limit, 1), 100)

    events = db.session.scalars(
        db.select(FallEvent)
        .options(joinedload(FallEvent.device))
        .where(FallEvent.user_id == g.current_miniapp_user.id)
        .order_by(FallEvent.occurred_at.desc())
        .limit(limit)
    ).all()
    return jsonify({"items": [event.to_public_dict() for event in events]})


@miniapp_bp.patch("/fall-events/<int:event_id>")
@token_required
def update_fall_event(event_id: int):
    event = db.session.get(FallEvent, event_id)
    if event is None or event.user_id != g.current_miniapp_user.id:
        return api_error(
            "FALL_EVENT_NOT_FOUND",
            "跌倒记录不存在",
            404,
        )

    data = request.get_json(silent=True) or {}
    status = str(data.get("status") or "").strip().lower()
    if status not in {"confirmed", "ignored"}:
        return api_error(
            "INVALID_FALL_EVENT_STATUS",
            "status 只能是 confirmed 或 ignored",
            400,
        )

    event.status = status
    event.handled_at = utc_now()
    db.session.commit()
    return jsonify({"success": True, "item": event.to_public_dict()})
