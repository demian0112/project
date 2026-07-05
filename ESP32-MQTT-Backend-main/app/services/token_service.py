from __future__ import annotations

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


class AccessTokenError(ValueError):
    pass


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"],
        salt=current_app.config["TOKEN_SALT"],
    )


def issue_access_token(user_id: int) -> str:
    return _serializer().dumps({"user_id": user_id})


def decode_access_token(token: str) -> int:
    try:
        payload = _serializer().loads(
            token,
            max_age=current_app.config["TOKEN_EXPIRE_SECONDS"],
        )
        return int(payload["user_id"])
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError) as exc:
        raise AccessTokenError("invalid or expired access token") from exc

