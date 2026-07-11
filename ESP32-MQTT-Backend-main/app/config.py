import os
from pathlib import Path

from flask import Flask
from dotenv import load_dotenv


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


def env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def configure_app(app: Flask) -> None:
    """Load administrator, mini-program, database and runtime settings."""
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

    database_path = Path(app.instance_path) / "app.db"

    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "dev-only-change-me"),
        SQLALCHEMY_DATABASE_URI=os.getenv(
            "DATABASE_URL",
            f"sqlite:///{database_path}",
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={
            "pool_pre_ping": True,
            "pool_recycle": 1800,
        },
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
        WECHAT_NOTIFY_ENABLED=env_bool("WECHAT_NOTIFY_ENABLED", False),
        WECHAT_FALL_ALERT_TEMPLATE_ID=os.getenv(
            "WECHAT_FALL_ALERT_TEMPLATE_ID",
            "",
        ),
        WECHAT_DEVICE_FAULT_TEMPLATE_ID=os.getenv(
            "WECHAT_DEVICE_FAULT_TEMPLATE_ID",
            "",
        ),
        WECHAT_MINIPROGRAM_STATE=os.getenv(
            "WECHAT_MINIPROGRAM_STATE",
            "trial",
        ),
        WECHAT_LANG=os.getenv("WECHAT_LANG", "zh_CN"),
        WECHAT_FALL_ALERT_PAGE=os.getenv(
            "WECHAT_FALL_ALERT_PAGE",
            "pages/fall-alert/index",
        ),
        WECHAT_DEVICE_FAULT_PAGE=os.getenv(
            "WECHAT_DEVICE_FAULT_PAGE",
            "pages/device-detail/index",
        ),
        WECHAT_ACCESS_TOKEN_CACHE_SECONDS=int(
            os.getenv("WECHAT_ACCESS_TOKEN_CACHE_SECONDS", "6600")
        ),
        WECHAT_SUBSCRIBE_SEND_URL=os.getenv(
            "WECHAT_SUBSCRIBE_SEND_URL",
            "https://api.weixin.qq.com/cgi-bin/message/subscribe/send",
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
        CSI_EXPECTED_INTERVAL_SECONDS=float(
            os.getenv("CSI_EXPECTED_INTERVAL_SECONDS", "1.5")
        ),
        CSI_SOFT_TIMEOUT_SECONDS=float(
            os.getenv("CSI_SOFT_TIMEOUT_SECONDS", "10")
        ),
        CSI_RECOVERY_GRACE_SECONDS=float(
            os.getenv("CSI_RECOVERY_GRACE_SECONDS", "20")
        ),
        CSI_HARD_TIMEOUT_SECONDS=float(
            os.getenv("CSI_HARD_TIMEOUT_SECONDS", "35")
        ),
        RUNNING_IGNORE_STATUS_TIMEOUT=env_bool(
            "RUNNING_IGNORE_STATUS_TIMEOUT",
            True,
        ),
        START_GRACE_SECONDS=float(os.getenv("START_GRACE_SECONDS", "10")),
        CSI_PARSE_ERROR_LIMIT=int(os.getenv("CSI_PARSE_ERROR_LIMIT", "3")),
        CSI_WINDOW_SIZE=int(os.getenv("CSI_WINDOW_SIZE", "5")),
        FAULT_EVENT_LIMIT_SECONDS=int(
            os.getenv("FAULT_EVENT_LIMIT_SECONDS", "5")
        ),
        FALL_ALGORITHM_ENABLED=env_bool("FALL_ALGORITHM_ENABLED", True),
        FALL_ALGORITHM_HTTP_BASE_URL=env_str(
            "FALL_ALGORITHM_HTTP_BASE_URL",
            "http://127.0.0.1:18080",
        ).rstrip("/"),
        FALL_ALGORITHM_WS_URL=env_str(
            "FALL_ALGORITHM_WS_URL",
            "ws://127.0.0.1:18080/stream",
        ),
        FALL_ALGORITHM_HEALTH_PATH=env_str(
            "FALL_ALGORITHM_HEALTH_PATH",
            "/health",
        ),
        FALL_ALGORITHM_STATS_PATH=env_str(
            "FALL_ALGORITHM_STATS_PATH",
            "/stats",
        ),
        FALL_ALGORITHM_CONFIG_PATH=env_str(
            "FALL_ALGORITHM_CONFIG_PATH",
            "/config",
        ),
        FALL_ALGORITHM_RESET_PATH=env_str(
            "FALL_ALGORITHM_RESET_PATH",
            "/reset",
        ),
        FALL_ALGORITHM_HTTP_TIMEOUT_SECONDS=env_float(
            "FALL_ALGORITHM_HTTP_TIMEOUT_SECONDS",
            3.0,
        ),
        FALL_ALGORITHM_WS_CONNECT_TIMEOUT_SECONDS=env_float(
            "FALL_ALGORITHM_WS_CONNECT_TIMEOUT_SECONDS",
            5.0,
        ),
        FALL_ALGORITHM_WS_READ_TIMEOUT_SECONDS=env_float(
            "FALL_ALGORITHM_WS_READ_TIMEOUT_SECONDS",
            35.0,
        ),
        FALL_ALGORITHM_PING_INTERVAL_SECONDS=env_float(
            "FALL_ALGORITHM_PING_INTERVAL_SECONDS",
            30.0,
        ),
        FALL_ALGORITHM_RECONNECT_INITIAL_SECONDS=env_float(
            "FALL_ALGORITHM_RECONNECT_INITIAL_SECONDS",
            1.0,
        ),
        FALL_ALGORITHM_RECONNECT_MAX_SECONDS=env_float(
            "FALL_ALGORITHM_RECONNECT_MAX_SECONDS",
            30.0,
        ),
        FALL_ALGORITHM_BATCH_INTERVAL_SECONDS=env_float(
            "FALL_ALGORITHM_BATCH_INTERVAL_SECONDS",
            1.5,
        ),
        FALL_ALGORITHM_QUEUE_MAX_FRAMES=env_int(
            "FALL_ALGORITHM_QUEUE_MAX_FRAMES",
            2000,
        ),
        FALL_ALGORITHM_SINGLE_ACTIVE_STREAM=env_bool(
            "FALL_ALGORITHM_SINGLE_ACTIVE_STREAM",
            True,
        ),
        FALL_ALGORITHM_CSI_TYPE=env_str(
            "FALL_ALGORITHM_CSI_TYPE",
            "CSI_DATA",
        ),
        FALL_ALGORITHM_RATE_SIG_MODE=env_int(
            "FALL_ALGORITHM_RATE_SIG_MODE",
            11,
        ),
        FALL_ALGORITHM_CHANNEL=env_int("FALL_ALGORITHM_CHANNEL", 100),
        FALL_ALGORITHM_FFT_GAIN=env_int(
            "FALL_ALGORITHM_FFT_GAIN",
            0,
        ),
        FALL_ALGORITHM_AGC_GAIN=env_int("FALL_ALGORITHM_AGC_GAIN", 0),
        FALL_ALGORITHM_RX_STATE=env_int("FALL_ALGORITHM_RX_STATE", 0),
        FALL_ALGORITHM_SECONDARY_CHANNEL=env_int(
            "FALL_ALGORITHM_SECONDARY_CHANNEL",
            0,
        ),
        FALL_ALGORITHM_NOISE_FLOOR=env_int(
            "FALL_ALGORITHM_NOISE_FLOOR",
            0,
        ),
        FALL_ALGORITHM_PAYLOAD_TYPE=env_int(
            "FALL_ALGORITHM_PAYLOAD_TYPE",
            13,
        ),
    )
