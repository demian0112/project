"""
Pydantic 模型定义。

这是前端 ↔ 后端之间的数据契约，也是后端生成控制命令的结构定义。
修改模型会影响 API 响应格式，请同步更新小程序端的数据解析。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── 小程序 → 后端 ────────────────────────────────────

class SwitchRequest(BaseModel):
    """小程序发送的开关请求"""
    device_id: str = Field(default="esp32s3_csi_001")
    enable: bool
    source: Optional[str] = Field(default="wechat_miniprogram")


# ── 后端内部设备状态 ─────────────────────────────────

class DeviceState(BaseModel):
    """内存中的设备状态快照"""
    device_id: str = "esp32s3_csi_001"
    monitor_enabled: bool = False
    state_text: str = "sleep"              # "sleep" | "monitoring"
    last_request_id: Optional[str] = None
    last_source: Optional[str] = None
    last_update_time: Optional[str] = None


# ── 后端 → Mosquitto 控制命令（预留，C 板对接时使用）─────

class DeviceCommand(BaseModel):
    """后端生成、准备发往 esp32s3/control 的控制命令"""
    cmd: str = "set_monitor"
    enable: bool
    target: str
    request_id: str
    source: str = "backend"
    time: str


# ── API 响应 ────────────────────────────────────────

class HealthResponse(BaseModel):
    ok: bool = True
    message: str = "backend is running"
    time: Optional[str] = None


class DeviceStateResponse(BaseModel):
    ok: bool = True
    device_state: DeviceState


# ── 跌倒告警 ────────────────────────────────────────

class FallAlert(BaseModel):
    """跌倒检测告警（fall_detector → MQTT → 后端缓存 → 小程序轮询）"""
    detected: bool = False
    confidence: float = 0.0
    duration_seconds: float = 0.0
    timestamp: Optional[str] = None
    device_id: str = "esp32s3_c_csi_2s_001"


class FallAlertResponse(BaseModel):
    ok: bool = True
    alert: Optional[FallAlert] = None


class SwitchResponse(BaseModel):
    ok: bool = True
    recognized: bool = True
    request_id: str
    monitor_enabled: bool
    state_text: str
    device_state: DeviceState
    reserved_device_command: DeviceCommand
