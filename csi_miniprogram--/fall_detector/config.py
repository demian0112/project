"""
跌倒检测模块配置。

复用 backend/config.py 的 MQTT 连接参数，仅定义检测特有配置。
"""

from pathlib import Path
import sys

# 复用 backend 的 MQTT 配置
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from config import (  # noqa: E402
    DEVICE_ID,
    MQTT_HOST,
    MQTT_PORT,
    MQTT_USERNAME,
    MQTT_PASSWORD,
)

# MQTT Topics
MQTT_TOPIC_CSI_DATA = "esp32s3/test"          # 输入：C 板上传的 CSI 数据
MQTT_TOPIC_FALL_ALERT = "esp32s3/fall_alert"  # 输出：跌倒告警

# 检测参数
SLIDING_WINDOW_SIZE = 200       # 滑动窗口帧数（约 6 秒 @33fps）
CONFIDENCE_THRESHOLD = 0.7      # 置信度阈值（0-1）
ALERT_COOLDOWN_SEC = 5.0        # 两次告警最小间隔（秒）

# MQTT Client ID
MQTT_CLIENT_ID = "fall_detector_001"
