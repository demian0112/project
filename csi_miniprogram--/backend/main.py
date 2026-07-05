"""
FastAPI 后端入口。

启动：
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

模块依赖链：
    config → models → device_state → mqtt_client → control → main.py
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import config
from models import (
    DeviceCommand,
    DeviceStateResponse,
    FallAlert,
    FallAlertResponse,
    HealthResponse,
    SwitchRequest,
    SwitchResponse,
)
from device_state import get_state, update_state

# ── 日志 ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("main")

# ── FastAPI 应用 ────────────────────────────────────

app = FastAPI(title="CSI MiniProgram Control Backend")

# 开发阶段允许跨域（小程序开发工具和真机调试）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 启动事件 ─────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    """启动 MQTT 连接（若 config.MQTT_ENABLED=True）"""
    from mqtt_client import start
    start()
    logger.info("Backend started, MQTT_ENABLED=%s", config.MQTT_ENABLED)


@app.on_event("shutdown")
def on_shutdown():
    from mqtt_client import stop
    stop()


# ── API 路由 ─────────────────────────────────────────
# 接口说明（与小程序的数据契约）：
#   GET  /api/health         → HealthResponse
#   GET  /api/device/state   → DeviceStateResponse
#   POST /api/device/switch  ← SwitchRequest → SwitchResponse

@app.get("/api/device/fall", response_model=FallAlertResponse)
def get_fall_alert():
    """获取最新跌倒告警（无告警时 alert 为 null）"""
    from mqtt_client import get_latest_fall_alert
    raw = get_latest_fall_alert()
    if raw is None:
        return FallAlertResponse(ok=True, alert=None)
    return FallAlertResponse(ok=True, alert=FallAlert(**raw))


@app.get("/api/health", response_model=HealthResponse)
def health_check():
    """健康检查：确认后端正在运行"""
    return HealthResponse(
        ok=True,
        message="backend is running",
        time=datetime.now().isoformat(timespec="seconds"),
    )


@app.get("/api/device/state", response_model=DeviceStateResponse)
def get_device_state():
    """获取当前设备状态"""
    return DeviceStateResponse(ok=True, device_state=get_state())


@app.post("/api/device/switch", response_model=SwitchResponse)
def set_device_switch(req: SwitchRequest):
    """
    接收小程序开关信号。

    处理流程：
    1. 生成 request_id 用于全链路追踪
    2. 更新内存中的设备状态
    3. 生成控制命令（预留，后续发往 C 板）
    4. 发布到 Mosquitto esp32s3/control（若 MQTT 已启用）
    """
    request_id = str(uuid.uuid4())

    # 更新内存状态
    state = update_state(
        device_id=req.device_id,
        enable=req.enable,
        request_id=request_id,
        source=req.source,
    )

    logger.info(
        "Switch signal received: enable=%s, device_id=%s, source=%s, request_id=%s",
        req.enable, req.device_id, req.source, request_id,
    )

    # 生成控制命令并发布到 MQTT
    from control import generate_and_publish
    publish_result = generate_and_publish(
        device_id=req.device_id,
        enable=req.enable,
        request_id=request_id,
    )

    return SwitchResponse(
        ok=True,
        recognized=True,
        request_id=request_id,
        monitor_enabled=state.monitor_enabled,
        state_text=state.state_text,
        device_state=state,
        reserved_device_command=DeviceCommand(**publish_result["payload"]),
    )
