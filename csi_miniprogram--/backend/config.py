"""
全局配置常量。

修改此文件即可切换 Broker 地址、端口、Topic 等，无需改动业务代码。
"""

# ── 设备信息 ───────────────────────────────────────
DEVICE_ID = "esp32s3_c_csi_2s_001"

# ── MQTT Broker ─────────────────────────────────────
MQTT_HOST = "127.0.0.1"  # 后端与 Mosquitto 同机部署时用 127.0.0.1；跨机部署改为局域网 IP 如 192.168.101.48
MQTT_PORT = 1883
MQTT_USERNAME = "esp32"
MQTT_PASSWORD = "esp32pass"
MQTT_CLIENT_ID = "backend_control_001"

# ── MQTT Topics（与 C 板约定）────────────────────────
MQTT_TOPIC_CONTROL = "esp32s3/control"  # 后端 → C 板：控制命令
MQTT_TOPIC_STATUS = "esp32s3/status"    # C 板 → 后端：状态回执
MQTT_TOPIC_FALL_ALERT = "esp32s3/fall_alert"  # fall_detector → 后端：跌倒告警

# ── 功能开关 ───────────────────────────────────────
# 若 Mosquitto 暂未就绪，可设为 False，后端仍可接收小程序开关信号，
# 仅跳过 MQTT publish，不会因 MQTT 连接失败而崩溃。
MQTT_ENABLED = True
