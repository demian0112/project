# Python MQTT 模块

该目录实现《Python 连接 MQTT 指南》要求的连接配置、精确 Topic 生成、
上行订阅和下行控制发布。数据库状态协调位于
`app/services/device_state_service.py`，小程序路由位于
`app/miniapp_api.py`。

## 默认 Broker

```text
Host:     192.168.101.48
Port:     1883
Username: csi_user
Password: 通过 MQTT_PASSWORD 环境变量配置
```

默认值位于 `config.py`。部署时也可以使用以下环境变量覆盖：

```text
MQTT_BROKER_HOST
MQTT_BROKER_PORT
MQTT_BROKER_USERNAME
MQTT_BROKER_PASSWORD
MQTT_KEEPALIVE
```

同时兼容修订版开发文档中的 `MQTT_HOST`、`MQTT_PORT`、
`MQTT_USERNAME` 和 `MQTT_PASSWORD`。

## 使用示例

```python
from app.mqtt import DeviceMqttClient


def handle_message(topic_name: str, payload: dict) -> None:
    print(topic_name, payload)


client = DeviceMqttClient("csi-gw-001", on_message=handle_message)
client.connect_async()

if client.wait_until_connected(timeout=10):
    start_result = client.publish_control("start")
    active_session = start_result.payload["session"]

    # 检测结束时携带同一个 session。
    client.publish_control("stop", session=active_session)
```

每个 `DeviceMqttClient` 只对应一个设备，Client ID 为：

```text
python_{device_name}_001
```

连接成功后会逐个订阅六个精确上行 Topic，不使用 `+` 或 `#`：

```text
csi/v1/devices/{device_name}/up/online  QoS 1
csi/v1/devices/{device_name}/up/wifi    QoS 1
csi/v1/devices/{device_name}/up/status  QoS 1
csi/v1/devices/{device_name}/up/csi     QoS 0
csi/v1/devices/{device_name}/up/ack     QoS 1
csi/v1/devices/{device_name}/up/fault   QoS 1
```

控制命令发布到：

```text
csi/v1/devices/{device_name}/down/control
```

发布参数固定为 QoS 1、`retain=false`。当前硬件协议要求 `cmd` 固定为
`control`；`start` 会生成新的 `session`，`stop` 必须复用当前
`session`。后端请求 ID 仅用于服务端幂等和日志，不写入硬件 payload：

```json
{"cmd":"control","action":"start","session":"sess-20260702-000001"}
```

## 真实 Broker 集成测试

普通测试不会连接外部 Broker。需要验证控制消息确实经过 Mosquitto 时运行：

```bash
RUN_MQTT_INTEGRATION=1 \
pytest -q app/mqtt/tests/test_publish_integration.py -s
```

测试会创建两个临时客户端：观察端先精确订阅测试设备的 `down/control`，
发布端再调用 `publish_control("start")`。只有观察端从 Broker 收到
`cmd=control`、相同的 `action` 和 `session`，测试才会通过。

默认测试设备名是 `pytest-device-001`，可通过环境变量修改：

```bash
RUN_MQTT_INTEGRATION=1 \
MQTT_TEST_DEVICE_NAME=csi-gw-001 \
pytest -q app/mqtt/tests/test_publish_integration.py -s
```
