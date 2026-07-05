from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import current_app


@dataclass(frozen=True, slots=True)
class WeChatIdentity:
    openid: str
    session_key: str
    unionid: str | None = None


class WeChatLoginError(RuntimeError):
    def __init__(self, message: str, *, unavailable: bool = False) -> None:
        super().__init__(message)
        self.unavailable = unavailable


class WeChatPhoneError(RuntimeError):
    def __init__(self, message: str, *, unavailable: bool = False) -> None:
        super().__init__(message)
        self.unavailable = unavailable


def _identity_from_payload(payload: dict[str, Any]) -> WeChatIdentity:
    errcode = payload.get("errcode")
    if errcode not in {None, 0}:
        message = str(payload.get("errmsg") or f"WeChat error {errcode}")
        raise WeChatLoginError(message)

    openid = str(payload.get("openid") or "").strip()
    session_key = str(payload.get("session_key") or "").strip()
    if not openid or not session_key:
        raise WeChatLoginError("WeChat response is missing openid or session_key")

    unionid = str(payload.get("unionid") or "").strip() or None
    return WeChatIdentity(
        openid=openid,
        session_key=session_key,
        unionid=unionid,
    )


def exchange_wechat_code(code: str) -> WeChatIdentity:
    """Exchange a one-time wx.login code on the trusted server side."""
    override = current_app.config.get("WECHAT_CODE_EXCHANGE")
    if override is not None:
        result = override(code)
        if isinstance(result, WeChatIdentity):
            return result
        return _identity_from_payload(dict(result))

    appid = current_app.config.get("WECHAT_APPID")
    secret = current_app.config.get("WECHAT_SECRET")
    if not appid or not secret:
        raise WeChatLoginError(
            "WECHAT_APPID and WECHAT_SECRET are not configured",
            unavailable=True,
        )

    query = urlencode(
        {
            "appid": appid,
            "secret": secret,
            "js_code": code,
            "grant_type": "authorization_code",
        }
    )
    url = f"{current_app.config['WECHAT_CODE2SESSION_URL']}?{query}"

    try:
        with urlopen(url, timeout=5) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeChatLoginError(
            "WeChat code2Session is temporarily unavailable",
            unavailable=True,
        ) from exc

    if not isinstance(payload, dict):
        raise WeChatLoginError("invalid WeChat response", unavailable=True)
    return _identity_from_payload(payload)


def exchange_phone_code(code: str) -> str:
    """Exchange a getPhoneNumber one-time code for a verified phone number."""
    override = current_app.config.get("WECHAT_PHONE_NUMBER_EXCHANGE")
    if override is not None:
        result = override(code)
        if isinstance(result, str):
            return result
        return _phone_from_payload(dict(result))

    access_token = _get_wechat_access_token()
    payload = _post_json(
        f"{current_app.config['WECHAT_PHONE_NUMBER_URL']}?access_token={access_token}",
        {"code": code},
    )
    return _phone_from_payload(payload)


def _get_wechat_access_token() -> str:
    cached = current_app.extensions.get("wechat_access_token")
    now = time.time()
    if cached and cached["expires_at"] > now + 60:
        return str(cached["access_token"])

    appid = current_app.config.get("WECHAT_APPID")
    secret = current_app.config.get("WECHAT_SECRET")
    if not appid or not secret:
        raise WeChatPhoneError(
            "WECHAT_APPID and WECHAT_SECRET are not configured",
            unavailable=True,
        )

    query = urlencode(
        {
            "grant_type": "client_credential",
            "appid": appid,
            "secret": secret,
        }
    )
    payload = _get_json(f"{current_app.config['WECHAT_ACCESS_TOKEN_URL']}?{query}")
    errcode = payload.get("errcode")
    if errcode not in {None, 0}:
        raise WeChatPhoneError(
            str(payload.get("errmsg") or f"WeChat error {errcode}"),
            unavailable=False,
        )

    access_token = str(payload.get("access_token") or "").strip()
    expires_in = int(payload.get("expires_in") or 0)
    if not access_token or expires_in <= 0:
        raise WeChatPhoneError("invalid WeChat access token response", unavailable=True)

    current_app.extensions["wechat_access_token"] = {
        "access_token": access_token,
        "expires_at": now + max(60, expires_in - 300),
    }
    return access_token


def _phone_from_payload(payload: dict[str, Any]) -> str:
    errcode = payload.get("errcode")
    if errcode not in {None, 0}:
        raise WeChatPhoneError(
            str(payload.get("errmsg") or f"WeChat error {errcode}"),
            unavailable=False,
        )

    phone_info = payload.get("phone_info")
    if not isinstance(phone_info, dict):
        raise WeChatPhoneError("WeChat response is missing phone_info")

    phone = str(
        phone_info.get("purePhoneNumber")
        or phone_info.get("phoneNumber")
        or ""
    ).strip()
    if not phone:
        raise WeChatPhoneError("WeChat response is missing phone number")
    return phone[:32]


def _get_json(url: str) -> dict[str, Any]:
    try:
        with urlopen(url, timeout=5) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeChatPhoneError(
            "WeChat service is temporarily unavailable",
            unavailable=True,
        ) from exc

    if not isinstance(payload, dict):
        raise WeChatPhoneError("invalid WeChat response", unavailable=True)
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
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeChatPhoneError(
            "WeChat service is temporarily unavailable",
            unavailable=True,
        ) from exc

    if not isinstance(payload, dict):
        raise WeChatPhoneError("invalid WeChat response", unavailable=True)
    return payload

