# Python 连接 MQTT 指南

> 注意：本文包含早期协议示例。当前联调必须以工作区根目录
> `hardware代码交互逻辑说明.md` 和 `交互逻辑优化说明.md` 为准；
> 当前控制包的 `cmd` 固定为 `control`，CSI 格式为
> `csib64-v2-1s`（1 秒一包）。

## 1. 文档目标

本文档用于说明 Python 后端连接 Mosquitto 的配置规则、Client ID 规则、Topic 规则、发布与订阅关系，以及各 Topic 的 JSON 数据格式。

当前系统中，Python 后端作为 MQTT 业务中枢，负责订阅硬件 C板发布的上行 Topic，并向指定设备发布控制 Topic。

Python 与微信小程序之间不通过 MQTT 通信，不在本文档中展开。

## 2. Mosquitto 连接参数

当前 Mosquitto 配置文件建议命名为：

```text
C:\Mosquitto\mosquitto_online.conf
```

Python 连接 Mosquitto 时使用统一账号密码：

```text
Broker Host: 192.168.101.48
Port: 1883
Username: csi_user
Password: 通过 MQTT_PASSWORD 环境变量配置
```

如果后续 Mosquitto 部署到服务器，只需要将 `192.168.101.48` 替换为服务器公网 IP 或域名。

## 3. 设备名来源

Python 端的设备名来自数据库 `device` 表。

每条设备记录至少需要包含：

```text
device_name
```

设备名必须与硬件初始化时写入的设备名完全一致。

设备名规则：

```text
1-32 位，只允许英文字母、数字、下划线 _ 和短横线 -
```

示例：

```text
csi-gw-001
room1_csi
fall-detector-01
```

Python 根据数据库中的 `device_name` 生成对应的 MQTT Client ID 和 Topic。

## 4. Python 端 Client ID 规则

Python 端 Client ID 在代码中生成，规则为：

```text
python_{device_name}_001
```

例如：

| 设备名 | Python Client ID |
|---|---|
| `csi-gw-001` | `python_csi-gw-001_001` |
| `room1_csi` | `python_room1_csi_001` |
| `fall-detector-01` | `python_fall-detector-01_001` |

如果后续同一设备存在第二个 Python MQTT 客户端实例，可以继续使用：

```text
python_{device_name}_002
python_{device_name}_003
```

第一阶段默认只使用：

```text
python_{device_name}_001
```

注意：一个 MQTT 连接只能使用一个 Client ID。如果 Python 后端按该规则为每个设备生成 `python_{device_name}_001`，则建议按“一个设备一个 MQTT Client 实例”的方式实现。否则，如果一个 Python 进程只创建一个 MQTT 连接，就只能使用一个 Client ID，不能同时对多个设备使用多个 Client ID。

## 5. Client ID 与设备名的关系

设备名用于 Topic 和业务识别：

```text
csi/v1/devices/{device_name}/up/status
```

Client ID 用于 MQTT Broker 区分连接：

```text
python_{device_name}_001
```

用户名密码用于认证：

```text
Username: csi_user
Password: 通过 MQTT_PASSWORD 环境变量配置
```

三者不要混淆。

## 6. Topic 总规则

统一 Topic 格式：

```text
csi/v1/devices/{device_name}/{direction}/{name}
```

字段说明：

```text
csi          项目前缀
v1           Topic 协议版本
devices      设备集合
device_name  硬件设备名
direction    消息方向
name         消息类型
```

方向字段固定为：

```text
up      ESP C板 → Python
down    Python → ESP C板
```

以设备名 `csi-gw-001` 为例：

```text
csi/v1/devices/csi-gw-001/up/status
csi/v1/devices/csi-gw-001/down/control
```

## 7. Python 侧订阅规则

Python 侧不使用 `+` 或 `#` 通配符订阅。

Python 应根据数据库 `device` 表中的设备名，逐个生成精确 Topic 并订阅。

以设备名 `csi-gw-001` 为例，Python 需要订阅：

```text
csi/v1/devices/csi-gw-001/up/online
csi/v1/devices/csi-gw-001/up/wifi
csi/v1/devices/csi-gw-001/up/status
csi/v1/devices/csi-gw-001/up/csi
csi/v1/devices/csi-gw-001/up/ack
csi/v1/devices/csi-gw-001/up/fault
```

如果数据库中有多台设备，则对每个设备名分别生成以上 6 个精确订阅 Topic。

## 8. Python 侧发布规则

Python 只向指定设备发布控制命令。

以设备名 `csi-gw-001` 为例，Python 发布：

```text
csi/v1/devices/csi-gw-001/down/control
```

如果控制设备 `room1_csi`，则发布：

```text
csi/v1/devices/room1_csi/down/control
```

## 9. Python 侧 Topic 总表

| 方向 | Topic | Python 动作 | 频率 | QoS | retain |
|---|---|---|---|---:|---:|
| ESP → Python | `csi/v1/devices/{device_name}/up/online` | 订阅 | 设备上线立即收到；异常离线由遗嘱触发 | 1 | true |
| ESP → Python | `csi/v1/devices/{device_name}/up/wifi` | 订阅 | 配网完成、WiFi 重连、IP 变化时收到 | 1 | true |
| ESP → Python | `csi/v1/devices/{device_name}/up/status` | 订阅 | 默认 5 秒一次，状态变化立即收到 | 1 | false |
| ESP → Python | `csi/v1/devices/{device_name}/up/csi` | 订阅 | 检测运行时固定 2 秒一次 | 0 | false |
| ESP → Python | `csi/v1/devices/{device_name}/up/ack` | 订阅 | 每条控制命令对应一次回复 | 1 | false |
| ESP → Python | `csi/v1/devices/{device_name}/up/fault` | 订阅 | 故障立即收到，同类故障建议 5 秒限流 | 1 | false |
| Python → ESP | `csi/v1/devices/{device_name}/down/control` | 发布 | 用户开始/停止检测时发布 | 1 | false |

## 10. 订阅：up/online

Topic：

```text
csi/v1/devices/{device_name}/up/online
```

用途：

```text
判断设备 MQTT 在线或离线。
```

设备上线 Payload：

```json
{
  "status": "online",
  "ip": "192.168.101.66",
  "fw": "1.0.0",
  "ts": 1782916658
}
```

设备异常离线 Payload：

```json
{
  "status": "offline",
  "reason": "lwt"
}
```

Python 处理要求：

```text
1. status=online 时，将设备标记为 MQTT 在线。
2. status=offline 时，将设备标记为 MQTT 离线。
3. 该 Topic 使用 retain=true，Python 重启后可以收到设备最近一次在线状态。
4. 不依赖 up/online 判断 CSI 是否正常。
```

## 11. 订阅：up/wifi

Topic：

```text
csi/v1/devices/{device_name}/up/wifi
```

用途：

```text
接收设备 WiFi 连接状态。
```

Payload：

```json
{
  "ok": true,
  "ip": "192.168.101.66",
  "rssi": -48,
  "ssid": "TP-LINK-****",
  "ts": 1782916658
}
```

Python 处理要求：

```text
1. ok=true 表示 ESP 已经连接目标 WiFi。
2. ok=false 表示 WiFi 当前异常。
3. ssid 必须是脱敏后的名称，不应包含 WiFi 密码。
4. 该 Topic 使用 retain=true，Python 重启后可以读取最近一次 WiFi 状态。
```

## 12. 订阅：up/status

Topic：

```text
csi/v1/devices/{device_name}/up/status
```

用途：

```text
接收 ESP 运行状态。
```

Payload：

```json
{
  "state": "idle",
  "session": "",
  "uart": true,
  "upload": false,
  "rssi": -48,
  "heap": 185432,
  "uptime": 350000,
  "ts": 1782916658
}
```

`state` 建议枚举：

```text
booting
provisioning
idle
starting
uploading
stopping
fault
```

Python 处理要求：

```text
1. 正常情况下每 5 秒收到一次。
2. 超过 15 秒没有收到 up/status，判定设备状态超时。
3. state=uploading 但长时间没有 up/csi，判定 CSI 数据异常。
4. state=fault 时，优先进入故障处理逻辑。
```

## 13. 订阅：up/csi

Topic：

```text
csi/v1/devices/{device_name}/up/csi
```

用途：

```text
接收 ESP 上传的 CSI 数据包。
```

发布频率：

```text
检测运行时固定 2 秒一次。
```

Payload：

```json
{
  "session": "sess-20260702-000001",
  "batch": 128,
  "frames": 61,
  "seq0": 6021,
  "seq1": 6081,
  "ts0": 1782916658000000,
  "ts1": 1782916660000000,
  "fmt": "csib64-v1",
  "data": [
    "base64_chunk_0",
    "base64_chunk_1",
    "base64_chunk_2"
  ],
  "crc": "A1B2C3D4"
}
```

Python 处理要求：

```text
1. 只处理当前 active session 对应的 up/csi。
2. session 不匹配时丢弃该包。
3. batch 用于判断 MQTT 批次是否丢失。
4. seq0 和 seq1 用于判断 CSI 帧序号是否连续。
5. frames 用于判断 2 秒包内帧数是否异常。
6. ts0 和 ts1 用于恢复采样时间范围。
7. crc 校验失败时丢弃该包，并记录为坏包。
8. up/csi 使用 QoS0，不进行 retained。
```

超时规则：

```text
1. start 后 8 秒内没有收到第一包 up/csi，判定启动后无数据。
2. running 状态下超过 8 秒没有收到 up/csi，判定 CSI 数据超时。
```

## 14. 订阅：up/ack

Topic：

```text
csi/v1/devices/{device_name}/up/ack
```

用途：

```text
接收 ESP 对 down/control 的执行回复。
```

Payload：

```json
{
  "cmd": "cmd-20260702-000001",
  "action": "start",
  "ok": true,
  "state": "uploading",
  "err": 0,
  "ts": 1782916659
}
```

Python 处理要求：

```text
1. 每次发布 down/control 后，等待对应 cmd 的 up/ack。
2. 3 秒内未收到 ack，可以重发一次 control。
3. 重发后仍未收到 ack，判定控制失败。
4. ok=false 时，根据 err 进入异常处理。
```

## 15. 订阅：up/fault

Topic：

```text
csi/v1/devices/{device_name}/up/fault
```

用途：

```text
接收 ESP 主动故障上报。
```

Payload：

```json
{
  "code": "UART_TIMEOUT",
  "msg": "no csi frame from b board",
  "state": "fault",
  "ts": 1782916668
}
```

建议故障码：

```text
WIFI_LOST
MQTT_LOST
UART_TIMEOUT
UART_CRC_HIGH
B_NO_RESPONSE
CSI_EMPTY
HEAP_LOW
UNKNOWN
```

Python 处理要求：

```text
1. 收到 up/fault 后，立即标记设备异常。
2. 同类故障不要重复刷屏，Python 侧也应做限流。
3. fault 不替代 status，status 仍然需要按 5 秒周期上报。
```

## 16. 发布：down/control

Topic：

```text
csi/v1/devices/{device_name}/down/control
```

用途：

```text
Python 向指定 ESP 下发控制命令。
```

启动检测 Payload：

```json
{
  "cmd": "cmd-20260702-000001",
  "action": "start",
  "session": "sess-20260702-000001",
  "ts": 1782916658
}
```

停止检测 Payload：

```json
{
  "cmd": "cmd-20260702-000002",
  "action": "stop",
  "session": "sess-20260702-000001",
  "ts": 1782916688
}
```

发布要求：

```text
QoS: 1
retain: false
```

发布细节：

```text
1. 每条命令必须生成唯一 cmd。
2. start 命令必须生成新的 session。
3. stop 命令必须携带当前 session。
4. Python 发布 control 后，必须等待 up/ack。
5. 不建议将 start 和 stop 拆成两个 Topic，统一用 action 区分即可。
```

## 17. Python 端功能优先级

### P0：连接与控制闭环

```text
1. 从数据库 device 表读取 device_name。
2. 生成 Client ID：python_{device_name}_001。
3. 根据 device_name 拼接精确 Topic。
4. 使用统一账号密码连接 Mosquitto。
5. 订阅 up/online。
6. 订阅 up/status。
7. 发布 down/control。
8. 接收 up/ack。
9. 判断设备是否在线、是否状态超时、是否控制成功。
```

### P1：CSI 数据链路

```text
1. 订阅 up/csi。
2. 校验 session。
3. 校验 batch 连续性。
4. 校验 frames 是否合理。
5. 校验 crc。
6. 判断 CSI 数据超时。
```

### P2：状态与异常完善

```text
1. 订阅 up/wifi。
2. 订阅 up/fault。
3. 维护设备 WiFi 状态。
4. 维护设备故障状态。
5. 对重复故障进行限流。
```

## 18. 最终结论

Python 端固定使用以下规则：

```text
Client ID:
python_{device_name}_001

订阅：
csi/v1/devices/{device_name}/up/online
csi/v1/devices/{device_name}/up/wifi
csi/v1/devices/{device_name}/up/status
csi/v1/devices/{device_name}/up/csi
csi/v1/devices/{device_name}/up/ack
csi/v1/devices/{device_name}/up/fault

发布：
csi/v1/devices/{device_name}/down/control
```
