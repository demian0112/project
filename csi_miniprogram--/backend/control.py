"""
控制命令生成模块。

职责：
- 根据小程序开关信号生成结构化控制命令
- 调用 mqtt_client 发布命令（若 MQTT 已启用）
- 独立于 HTTP 层，可被任何调用方使用（API / 脚本 / 测试）

后续 C 板对接时，只需修改此模块的命令格式或新增状态解析逻辑。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from models import DeviceCommand

logger = logging.getLogger("control")


# ── 公开接口 ─────────────────────────────────────────

def generate_and_publish(
    device_id: str,
    enable: bool,
    request_id: str | None = None,
    source: str = "backend",
) -> dict:
    """
    生成控制命令并发布到 MQTT。

    参数：
        device_id: 目标设备 ID
        enable: True=开启实时监测, False=关闭/休眠
        request_id: 请求追踪 ID，不传则自动生成
        source: 命令来源标识

    返回：
        {"topic": ..., "payload": ..., "mqtt_publish_rc": ...}
    始终返回结果字典，不抛异常。
    """
    if request_id is None:
        request_id = str(uuid.uuid4())

    cmd = DeviceCommand(
        cmd="set_monitor",
        enable=enable,
        target=device_id,
        request_id=request_id,
        source=source,
        time=datetime.now().isoformat(timespec="seconds"),
    )

    logger.info(
        "Control command generated: enable=%s, target=%s, request_id=%s",
        enable, device_id, request_id,
    )

    # 发布到 Mosquitto（若 MQTT 未启用，mqtt_client 会安全降级）
    from mqtt_client import publish_command
    return publish_command(cmd)
