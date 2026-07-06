import os
from pathlib import Path

from flask import Flask


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def configure_app(app: Flask) -> None:
    """Load administrator, mini-program, database and runtime settings."""
    database_path = Path(app.instance_path) / "app.db"

    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "dev-only-change-me"),
        SQLALCHEMY_DATABASE_URI=os.getenv(
            "DATABASE_URL",
            f"sqlite:///{database_path}",
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        INITIAL_ADMIN_USERNAME=os.getenv("ADMIN_USERNAME"),
        INITIAL_ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD"),
        WECHAT_APPID=os.getenv("WECHAT_APPID"),
        WECHAT_SECRET=os.getenv("WECHAT_SECRET"),
        WECHAT_CODE2SESSION_URL=os.getenv(
            "WECHAT_CODE2SESSION_URL",
            "https://api.weixin.qq.com/sns/jscode2session",
        ),
        WECHAT_ACCESS_TOKEN_URL=os.getenv(
            "WECHAT_ACCESS_TOKEN_URL",
            "https://api.weixin.qq.com/cgi-bin/token",
        ),
        WECHAT_PHONE_NUMBER_URL=os.getenv(
            "WECHAT_PHONE_NUMBER_URL",
            "https://api.weixin.qq.com/wxa/business/getuserphonenumber",
        ),
        TOKEN_EXPIRE_SECONDS=int(os.getenv("TOKEN_EXPIRE_SECONDS", "7200")),
        TOKEN_SALT=os.getenv("TOKEN_SALT", "anshou-miniapp-access-token"),
        MQTT_ENABLED=env_bool("MQTT_ENABLED", False),
        MQTT_AUTOSTART_DEVICES=env_bool("MQTT_AUTOSTART_DEVICES", False),
        OFFLINE_MONITOR_ENABLED=env_bool(
            "OFFLINE_MONITOR_ENABLED",
            True,
        ),
        OFFLINE_TIMEOUT_SECONDS=int(
            os.getenv("OFFLINE_TIMEOUT_SECONDS", "15")
        ),
        STATUS_TIMEOUT_SECONDS=int(
            os.getenv("STATUS_TIMEOUT_SECONDS", "15")
        ),
        CSI_TIMEOUT_SECONDS=int(os.getenv("CSI_TIMEOUT_SECONDS", "8")),
        CSI_WINDOW_SIZE=int(os.getenv("CSI_WINDOW_SIZE", "5")),
        FAULT_EVENT_LIMIT_SECONDS=int(
            os.getenv("FAULT_EVENT_LIMIT_SECONDS", "5")
        ),
    )
