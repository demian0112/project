from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from flask import current_app
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import (
    Device,
    FallEvent,
    User,
    WxNotifyLog,
    WxSubscription,
    fault_template_data,
    utc_now,
)


ALLOWED_SUBSCRIPTION_STATUSES = {"accept", "reject", "ban", "filter"}
FALL_ALERT_SCENE = "fall_alert"
DEVICE_FAULT_SCENE = "device_fault"
DEVICE_FAULT_NOTICE_CODES = {
    "NO_CSI_FRAME",
    "NO_CSI_FRAME_TIMEOUT",
    "UART_TIMEOUT",
}
NO_REMAINING_ERRCODE = 43101


class WeChatNotifyError(RuntimeError):
    def __init__(self, message: str, *, errcode: int | None = None) -> None:
        super().__init__(message)
        self.errcode = errcode


def get_access_token(*, force_refresh: bool = False) -> str:
    """Return a cached WeChat access_token for subscribe-message sending."""
    override = current_app.config.get("WECHAT_ACCESS_TOKEN_FETCHER")
    if override is not None:
        result = override(force_refresh=force_refresh)
        if isinstance(result, str):
            return result
        token = str(dict(result).get("access_token") or "").strip()
        if token:
            return token
        raise WeChatNotifyError("invalid access token override response")

    cached = current_app.extensions.get("wechat_notify_access_token")
    now = time.time()
    if (
        not force_refresh
        and cached
        and cached.get("expires_at", 0) > now + 300
    ):
        return str(cached["access_token"])

    appid = current_app.config.get("WECHAT_APPID")
    secret = current_app.config.get("WECHAT_SECRET")
    if not appid or not secret:
        raise WeChatNotifyError(
            "WECHAT_APPID and WECHAT_SECRET are not configured"
        )

    query = urlencode(
        {
            "grant_type": "client_credential",
            "appid": appid,
            "secret": secret,
        }
    )
    url = f"{current_app.config['WECHAT_ACCESS_TOKEN_URL']}?{query}"
    try:
        payload = _get_json(url)
    except WeChatNotifyError:
        current_app.extensions.pop("wechat_notify_access_token", None)
        raise

    errcode = payload.get("errcode")
    if errcode not in {None, 0}:
        raise WeChatNotifyError(
            str(payload.get("errmsg") or f"WeChat error {errcode}"),
            errcode=int(errcode),
        )

    access_token = str(payload.get("access_token") or "").strip()
    expires_in = int(payload.get("expires_in") or 0)
    if not access_token or expires_in <= 0:
        raise WeChatNotifyError("invalid WeChat access token response")

    cache_seconds = int(current_app.config["WECHAT_ACCESS_TOKEN_CACHE_SECONDS"])
    current_app.extensions["wechat_notify_access_token"] = {
        "access_token": access_token,
        "expires_at": now + min(max(60, expires_in - 300), cache_seconds),
    }
    return access_token


def record_subscription(
    user: User,
    scene: str,
    template_id: str,
    status: str,
) -> WxSubscription:
    """Upsert one user's fall-alert subscription state."""
    scene = (scene or FALL_ALERT_SCENE).strip() or FALL_ALERT_SCENE
    template_id = template_id.strip()
    status = status.strip().lower()
    if status not in ALLOWED_SUBSCRIPTION_STATUSES:
        raise ValueError("invalid subscription status")

    subscription = db.session.scalar(
        db.select(WxSubscription).where(
            WxSubscription.user_id == user.id,
            WxSubscription.scene == scene,
            WxSubscription.template_id == template_id,
        )
    )
    if subscription is None:
        subscription = WxSubscription(
            user_id=user.id,
            scene=scene,
            template_id=template_id,
            status=status,
            remaining_count=0,
        )
        db.session.add(subscription)

    subscription.status = status
    subscription.last_subscribed_at = utc_now()
    if status == "accept":
        subscription.remaining_count = 1
    else:
        subscription.remaining_count = 0

    db.session.commit()
    return subscription


def send_subscribe_message(
    openid: str,
    template_id: str,
    page: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Call WeChat subscribe/send and return the raw errcode/errmsg shape."""
    override = current_app.config.get("WECHAT_SUBSCRIBE_SENDER")
    if override is not None:
        return dict(
            override(
                openid=openid,
                template_id=template_id,
                page=page,
                data=data,
            )
        )

    payload = {
        "touser": openid,
        "template_id": template_id,
        "page": page,
        "data": data,
        "miniprogram_state": current_app.config["WECHAT_MINIPROGRAM_STATE"],
        "lang": current_app.config["WECHAT_LANG"],
    }
    return _post_subscribe_payload(payload)


def send_fall_alert(
    fall_event_id: int,
    *,
    triggered_by: str = "system",
) -> dict[str, Any]:
    """Send a WeChat fall alert for an existing FallEvent when allowed."""
    event = db.session.get(
        FallEvent,
        fall_event_id,
        options=[
            joinedload(FallEvent.user),
            joinedload(FallEvent.device),
        ],
    )
    if event is None:
        return _result(
            ok=False,
            sent=False,
            reason="fall_event_not_found",
            errmsg="fall event not found",
        )

    user = event.user
    device = event.device
    template_id = str(
        current_app.config.get("WECHAT_FALL_ALERT_TEMPLATE_ID") or ""
    ).strip()

    if event.wechat_notified:
        return _result(
            ok=True,
            sent=True,
            errcode=0,
            errmsg="already sent",
            reason="already_sent",
        )
    if not current_app.config.get("WECHAT_NOTIFY_ENABLED"):
        return _record_failure(
            event,
            user,
            device,
            template_id,
            triggered_by,
            reason="disabled",
            errmsg="WeChat notify is disabled",
        )
    if not template_id:
        return _record_failure(
            event,
            user,
            device,
            template_id,
            triggered_by,
            reason="template_not_configured",
            errmsg="WECHAT_FALL_ALERT_TEMPLATE_ID is not configured",
        )
    if not user.wx_openid:
        return _record_failure(
            event,
            user,
            device,
            template_id,
            triggered_by,
            reason="openid_missing",
            errmsg="user wx_openid is missing",
        )

    subscription = db.session.scalar(
        db.select(WxSubscription).where(
            WxSubscription.user_id == user.id,
            WxSubscription.scene == FALL_ALERT_SCENE,
            WxSubscription.template_id == template_id,
        )
    )
    if subscription is None:
        return _record_failure(
            event,
            user,
            device,
            template_id,
            triggered_by,
            reason="subscription_not_found",
            errcode=NO_REMAINING_ERRCODE,
            errmsg="user has not granted this subscription",
        )
    if subscription.status != "accept":
        return _record_failure(
            event,
            user,
            device,
            template_id,
            triggered_by,
            reason="subscription_not_accepted",
            errcode=NO_REMAINING_ERRCODE,
            errmsg=f"subscription status is {subscription.status}",
            remaining_count=subscription.remaining_count,
        )

    page = _fall_alert_page(event.id)
    template_data = _fall_alert_template_data(event, device)
    try:
        response = send_subscribe_message(
            user.wx_openid,
            template_id,
            page,
            template_data,
        )
    except WeChatNotifyError as exc:
        return _record_failure(
            event,
            user,
            device,
            template_id,
            triggered_by,
            reason="wechat_request_failed",
            errcode=exc.errcode,
            errmsg=str(exc),
            remaining_count=subscription.remaining_count,
        )

    errcode = _parse_errcode(response.get("errcode"))
    errmsg = str(response.get("errmsg") or "")
    if errcode == 0:
        subscription.remaining_count = 1
        now = utc_now()
        event.wechat_notified = True
        event.wechat_notified_at = now
        event.wechat_notify_errcode = 0
        event.wechat_notify_errmsg = errmsg or "ok"
        db.session.add(
            _notify_log(
                user=user,
                device=device,
                event=event,
                template_id=template_id,
                success=True,
                errcode=0,
                errmsg=errmsg or "ok",
                triggered_by=triggered_by,
                sent_at=now,
            )
        )
        db.session.commit()
        return _result(
            ok=True,
            sent=True,
            errcode=0,
            errmsg=errmsg or "ok",
            remaining_count=subscription.remaining_count,
        )

    if errcode == 40001:
        current_app.extensions.pop("wechat_notify_access_token", None)

    return _record_failure(
        event,
        user,
        device,
        template_id,
        triggered_by,
        reason="wechat_send_failed",
        errcode=errcode,
        errmsg=errmsg or "WeChat subscribe send failed",
        remaining_count=subscription.remaining_count,
    )


def send_device_fault_notice(
    user: User,
    device: Device,
    *,
    code: str,
    message: str,
    triggered_by: str = "device_fault",
) -> dict[str, Any]:
    """Send a WeChat notice for CSI collection faults when configured."""
    fault_code = (code or "").strip().upper()
    if fault_code not in DEVICE_FAULT_NOTICE_CODES:
        return _result(
            ok=True,
            sent=False,
            reason="fault_code_not_notifiable",
            errmsg="fault code is not configured for WeChat notice",
        )

    template_id = str(
        current_app.config.get("WECHAT_DEVICE_FAULT_TEMPLATE_ID") or ""
    ).strip()
    if not current_app.config.get("WECHAT_NOTIFY_ENABLED"):
        return _record_device_fault_notice_result(
            user,
            device,
            template_id,
            triggered_by,
            success=False,
            reason="disabled",
            errmsg="WeChat notify is disabled",
        )
    if not template_id:
        return _record_device_fault_notice_result(
            user,
            device,
            template_id,
            triggered_by,
            success=False,
            reason="template_not_configured",
            errmsg="WECHAT_DEVICE_FAULT_TEMPLATE_ID is not configured",
        )
    if not user.wx_openid:
        return _record_device_fault_notice_result(
            user,
            device,
            template_id,
            triggered_by,
            success=False,
            reason="openid_missing",
            errmsg="user wx_openid is missing",
        )

    subscription = db.session.scalar(
        db.select(WxSubscription).where(
            WxSubscription.user_id == user.id,
            WxSubscription.scene == DEVICE_FAULT_SCENE,
            WxSubscription.template_id == template_id,
        )
    )
    if subscription is None:
        return _record_device_fault_notice_result(
            user,
            device,
            template_id,
            triggered_by,
            success=False,
            reason="subscription_not_found",
            errcode=NO_REMAINING_ERRCODE,
            errmsg="user has not granted this subscription",
        )
    if subscription.status != "accept":
        return _record_device_fault_notice_result(
            user,
            device,
            template_id,
            triggered_by,
            success=False,
            reason="subscription_not_accepted",
            errcode=NO_REMAINING_ERRCODE,
            errmsg=f"subscription status is {subscription.status}",
            remaining_count=subscription.remaining_count,
        )

    try:
        response = send_subscribe_message(
            user.wx_openid,
            template_id,
            _device_fault_page(device),
            fault_template_data(
                device_name=device.device_name,
                display_name=device.display_name,
                location=device.location,
                code=fault_code,
                raw_message=message,
            )
            or {},
        )
    except WeChatNotifyError as exc:
        return _record_device_fault_notice_result(
            user,
            device,
            template_id,
            triggered_by,
            success=False,
            reason="wechat_request_failed",
            errcode=exc.errcode,
            errmsg=str(exc),
            remaining_count=subscription.remaining_count,
        )

    errcode = _parse_errcode(response.get("errcode"))
    errmsg = str(response.get("errmsg") or "")
    if errcode == 0:
        subscription.remaining_count = 1
        now = utc_now()
        db.session.add(
            _notify_log(
                user=user,
                device=device,
                event=None,
                scene=DEVICE_FAULT_SCENE,
                template_id=template_id,
                success=True,
                errcode=0,
                errmsg=errmsg or "ok",
                triggered_by=triggered_by,
                sent_at=now,
            )
        )
        db.session.commit()
        return _result(
            ok=True,
            sent=True,
            errcode=0,
            errmsg=errmsg or "ok",
            remaining_count=subscription.remaining_count,
        )

    if errcode == 40001:
        current_app.extensions.pop("wechat_notify_access_token", None)

    return _record_device_fault_notice_result(
        user,
        device,
        template_id,
        triggered_by,
        success=False,
        reason="wechat_send_failed",
        errcode=errcode,
        errmsg=errmsg or "WeChat subscribe send failed",
        remaining_count=subscription.remaining_count,
    )


def _post_subscribe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    token = get_access_token()
    response = _post_json(
        f"{current_app.config['WECHAT_SUBSCRIBE_SEND_URL']}?access_token={token}",
        payload,
    )
    if _parse_errcode(response.get("errcode")) != 40001:
        return response

    current_app.extensions.pop("wechat_notify_access_token", None)
    token = get_access_token(force_refresh=True)
    return _post_json(
        f"{current_app.config['WECHAT_SUBSCRIBE_SEND_URL']}?access_token={token}",
        payload,
    )


def _fall_alert_page(fall_event_id: int) -> str:
    base = str(current_app.config["WECHAT_FALL_ALERT_PAGE"] or "").strip()
    return _append_page_query(base, {"id": str(fall_event_id)})


def _device_fault_page(device: Device) -> str:
    base = str(current_app.config["WECHAT_DEVICE_FAULT_PAGE"] or "").strip()
    if not base:
        base = "pages/device-detail/index"
    if not device.device_name:
        return base
    return _append_page_query(base, {"deviceName": device.device_name})


def _append_page_query(base: str, params: dict[str, str]) -> str:
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: value for key, value in params.items() if value})
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )


def _fall_alert_template_data(
    event: FallEvent,
    device: Device,
) -> dict[str, dict[str, str]]:
    return {
        "thing1": {"value": _truncate(device.location or "未设置位置")},
        "time2": {"value": _format_wechat_time(event.occurred_at)},
        "thing3": {"value": _truncate("有人跌倒，请尽快确认")},
        "thing5": {
            "value": _truncate(device.display_name or device.device_name)
        },
    }


def _format_wechat_time(value: datetime) -> str:
    return value.strftime("%Y年%m月%d日 %H:%M:%S")


def _truncate(value: str, limit: int = 20) -> str:
    value = str(value or "").strip()
    return value[:limit]


def _record_failure(
    event: FallEvent,
    user: User,
    device: Device,
    template_id: str | None,
    triggered_by: str,
    *,
    reason: str,
    errmsg: str,
    errcode: int | None = None,
    remaining_count: int | None = None,
) -> dict[str, Any]:
    event.wechat_notify_errcode = errcode
    event.wechat_notify_errmsg = errmsg[:255]
    db.session.add(
        _notify_log(
            user=user,
            device=device,
            event=event,
            template_id=template_id,
            success=False,
            errcode=errcode,
            errmsg=errmsg,
            triggered_by=triggered_by,
            sent_at=utc_now(),
        )
    )
    db.session.commit()
    return _result(
        ok=False,
        sent=False,
        errcode=errcode,
        errmsg=errmsg,
        reason=reason,
        remaining_count=remaining_count,
    )


def _record_device_fault_notice_result(
    user: User,
    device: Device,
    template_id: str | None,
    triggered_by: str,
    *,
    success: bool,
    reason: str,
    errmsg: str,
    errcode: int | None = None,
    remaining_count: int | None = None,
) -> dict[str, Any]:
    db.session.add(
        _notify_log(
            user=user,
            device=device,
            event=None,
            scene=DEVICE_FAULT_SCENE,
            template_id=template_id,
            success=success,
            errcode=errcode,
            errmsg=errmsg,
            triggered_by=triggered_by,
            sent_at=utc_now(),
        )
    )
    db.session.commit()
    return _result(
        ok=success,
        sent=success,
        errcode=errcode,
        errmsg=errmsg,
        reason=reason,
        remaining_count=remaining_count,
    )


def _notify_log(
    *,
    user: User,
    device: Device,
    event: FallEvent | None,
    template_id: str | None,
    success: bool,
    errcode: int | None,
    errmsg: str,
    triggered_by: str,
    sent_at,
    scene: str = FALL_ALERT_SCENE,
) -> WxNotifyLog:
    return WxNotifyLog(
        user_id=user.id,
        device_id=device.id,
        fall_event_id=event.id if event is not None else None,
        scene=scene,
        template_id=template_id or None,
        openid_masked=_mask_openid(user.wx_openid),
        success=success,
        errcode=errcode,
        errmsg=(errmsg or "")[:255],
        triggered_by=triggered_by[:40],
        sent_at=sent_at,
    )


def _mask_openid(openid: str | None) -> str | None:
    if not openid:
        return None
    return f"{openid[:8]}***"


def _result(
    *,
    ok: bool,
    sent: bool,
    errcode: int | None = None,
    errmsg: str = "",
    reason: str = "",
    remaining_count: int | None = None,
) -> dict[str, Any]:
    return {
        "enabled": bool(current_app.config.get("WECHAT_NOTIFY_ENABLED")),
        "ok": ok,
        "sent": sent,
        "errcode": errcode,
        "errmsg": errmsg,
        "reason": reason,
        "remaining_count": remaining_count,
    }


def _parse_errcode(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _get_json(url: str) -> dict[str, Any]:
    try:
        with urlopen(url, timeout=5) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (
        HTTPError,
        URLError,
        TimeoutError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise WeChatNotifyError("WeChat service is temporarily unavailable") from exc

    if not isinstance(payload, dict):
        raise WeChatNotifyError("invalid WeChat response")
    return payload


def _post_json(url: str, data: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (
        HTTPError,
        URLError,
        TimeoutError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise WeChatNotifyError("WeChat service is temporarily unavailable") from exc

    if not isinstance(payload, dict):
        raise WeChatNotifyError("invalid WeChat response")
    return payload
