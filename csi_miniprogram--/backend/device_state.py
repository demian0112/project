"""
设备状态管理（内存存储）。

当前阶段用内存字典存储状态，单进程、无持久化。
后续如需持久化或分布式，只需替换此模块，不影响调用方。
"""

from __future__ import annotations

from datetime import datetime

from models import DeviceState


# ── 内部状态存储 ────────────────────────────────────

_store: dict = {
    "device_id": "esp32s3_csi_001",
    "monitor_enabled": False,
    "state_text": "sleep",
    "last_request_id": None,
    "last_source": None,
    "last_update_time": None,
}


# ── 对外接口 ─────────────────────────────────────────

def get_state() -> DeviceState:
    """返回当前设备状态快照"""
    return DeviceState(**_store)


def update_state(
    device_id: str,
    enable: bool,
    request_id: str,
    source: str | None = None,
) -> DeviceState:
    """根据小程序开关信号更新状态，返回更新后的快照"""
    state_text = "monitoring" if enable else "sleep"

    _store["device_id"] = device_id
    _store["monitor_enabled"] = enable
    _store["state_text"] = state_text
    _store["last_request_id"] = request_id
    _store["last_source"] = source
    _store["last_update_time"] = datetime.now().isoformat(timespec="seconds")

    return DeviceState(**_store)
