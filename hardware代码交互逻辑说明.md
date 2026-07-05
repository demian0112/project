# ESP32 CSI 硬件组交互逻辑与 MQTT Topic 说明

本文档基于当前最新版代码整理，系统采用 **A板 + B板 + C板** 的三板结构：

- **A板：ESP32-C5 csi_send**，持续发送 ESP-NOW 无线帧，用于触发 CSI。
- **B板：ESP32-C5 csi_recv**，接收 A 板无线帧产生 CSI，并通过 UART 二进制帧发送给 C 板。
- **C板：ESP32-S3 SoftAP + Wi-Fi + MQTT**，负责配网、MQTT连接、订阅控制 topic、控制 B 板启动/停止 CSI、接收 UART CSI、按 1 秒整包上传 CSI 数据。

当前通信策略为：

```text
Python / MQTTFX
   ↓ down/control
C板 ESP32-S3
   ↓ UART 控制命令 CSI_START / CSI_STOP
B板 ESP32-C5
   ↓ UART 二进制 CSI 数据
C板 ESP32-S3
   ↓ up/csi / up/status / up/fault / up/ack
MQTT Broker
   ↓
Python 后端订阅处理
```

---

## 1. 设备名与 Topic 命名规则

C板根据设备名 `device_name` 生成 MQTT Client ID 和全部 topic。

### 1.1 MQTT Client ID

```text
esp_{device_name}_001
```

示例：

```text
esp_esp01_001
```

### 1.2 Topic 总表

| 方向 | Topic | 用途 | QoS | retain |
|---|---|---|---:|---:|
| C板 → MQTT | `csi/v1/devices/{device_name}/up/online` | 上报设备在线 / 离线状态 | 1 | true |
| C板 → MQTT | `csi/v1/devices/{device_name}/up/wifi` | 上报 Wi-Fi 连接信息 | 1 | true |
| C板 → MQTT | `csi/v1/devices/{device_name}/up/status` | 周期上报硬件状态 | 1 | false |
| C板 → MQTT | `csi/v1/devices/{device_name}/up/csi` | 上传 CSI 数据包 | 0 | false |
| C板 → MQTT | `csi/v1/devices/{device_name}/up/ack` | 回复 Python 控制命令 | 1 | false |
| C板 → MQTT | `csi/v1/devices/{device_name}/up/fault` | 上报故障信息 | 1 | false |
| MQTT → C板 | `csi/v1/devices/{device_name}/down/control` | Python 下发启动 / 停止检测命令 | 1 | false |

---

## 2. B板与C板的 UART 交互逻辑

B板与C板之间使用 UART 通信，波特率固定为：

```c
#define B2C_UART_BAUD 921600
#define LINK_UART_BAUD 921600
```

接线关系：

```text
B板 GPIO4  TX  →  C板 GPIO18 RX
B板 GPIO5  RX  ←  C板 GPIO17 TX
B板 GND        ↔  C板 GND
```

### 2.1 C板控制B板

C板收到 MQTT `down/control` 后，会通过 UART 给 B 板发送 ASCII 控制命令：

| C板动作 | 发送给B板的UART命令 | B板动作 |
|---|---|---|
| 启动检测 | `CSI_START\n` | B板开启 CSI 回调并开始通过 UART 发送 CSI 数据 |
| 停止检测 | `CSI_STOP\n` | B板关闭 CSI 上传 |

### 2.2 B板向C板发送CSI

B板接收到 A 板无线帧并产生 CSI 后，会把每一帧 CSI 封装为二进制 UART 帧发送给 C 板。

当前 UART 二进制帧头长度为 20 字节：

```text
magic0        1 byte   固定 0xA5
magic1        1 byte   固定 0x5A
version       1 byte   当前为 0x02
msg_type      1 byte   当前 CSI 类型为 0x01
payload_len   2 bytes  CSI payload 长度
seq           4 bytes  B板本地递增序号
timestamp_us  8 bytes  CSI 时间戳，单位 us
rssi          1 byte   Wi-Fi RSSI
flags         1 byte   bit0 表示 first_word_invalid
payload       N bytes  原始 CSI 数据
crc16         2 bytes  CRC16 校验
```

C板接收时会校验：

```text
magic0 / magic1
version
msg_type
payload_len
crc16
```

只有校验通过的 CSI 帧才会进入 C 板的 1 秒批量缓存。

---

## 3. C板状态机

C板内部主要状态如下：

| 状态 | 含义 |
|---|---|
| `booting` | 设备启动中 |
| `idle` | 已连接 MQTT，但未开始上传 CSI |
| `uploading` | 正在接收 B板 CSI 并上传 MQTT |
| `fault` | 检测运行中发生故障，例如 B板 CSI 中断 |

状态变化逻辑：

```text
上电启动
  ↓
Wi-Fi连接成功
  ↓
MQTT连接成功，发布 online / wifi / status
  ↓
等待 down/control
  ↓ start
C板发送 CSI_START 给B板
  ↓
状态变为 uploading
  ↓
每1秒打包发布 up/csi
  ↓ stop
C板发送 CSI_STOP 给B板
  ↓
状态变为 idle
```

如果 `uploading` 状态下超过 `UART_TIMEOUT_MS` 没有收到 B 板 CSI，则进入：

```text
fault
```

并发布 `up/fault`。

---

## 4. down/control：Python 控制 C板

### 4.1 Topic

```text
csi/v1/devices/{device_name}/down/control
```

### 4.2 启动检测

Python 向该 topic 发布：

```json
{
  "cmd": "control",
  "action": "start",
  "session": "sess-20260702-000001"
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `cmd` | string | 命令类型，建议固定为 `control` |
| `action` | string | 控制动作，`start` 表示启动检测 |
| `session` | string | 本次检测会话ID，由 Python 生成；如果不传，C板会生成本地 session |

C板收到后执行：

1. 清空当前 CSI 缓存；
2. 重置 CSI batch 编号；
3. 设置 `upload=true`；
4. 通过 UART 发送 `CSI_START\n` 给 B 板；
5. 状态切换为 `uploading`；
6. 发布 `up/ack` 回复 Python；
7. 后续每 1 秒发布一次 `up/csi`。

### 4.3 停止检测

Python 向该 topic 发布：

```json
{
  "cmd": "control",
  "action": "stop",
  "session": "sess-20260702-000001"
}
```

C板收到后执行：

1. 通过 UART 发送 `CSI_STOP\n` 给 B 板；
2. 设置 `upload=false`；
3. 清空当前 CSI 缓存；
4. 状态切换为 `idle`；
5. 发布 `up/ack` 回复 Python。

---

## 5. up/ack：控制命令确认

### 5.1 Topic

```text
csi/v1/devices/{device_name}/up/ack
```

### 5.2 触发时机

C板每收到一条 `down/control` 命令后，都会回复一条 `up/ack`。

### 5.3 启动成功示例

```json
{
  "cmd": "control",
  "action": "start",
  "ok": true,
  "state": "uploading",
  "err": 0,
  "msg": "",
  "ts": 240
}
```

### 5.4 停止成功示例

```json
{
  "cmd": "control",
  "action": "stop",
  "ok": true,
  "state": "idle",
  "err": 0,
  "msg": "",
  "ts": 260
}
```

### 5.5 不支持的命令示例

```json
{
  "cmd": "control",
  "action": "pause",
  "ok": false,
  "state": "idle",
  "err": 1,
  "msg": "unsupported action",
  "ts": 270
}
```

---

## 6. up/online：设备在线状态

### 6.1 Topic

```text
csi/v1/devices/{device_name}/up/online
```

### 6.2 触发时机

C板 MQTT 连接成功后发布。

同时 C板配置了 MQTT Last Will。异常断开时，Broker 会代发 offline 信息。

### 6.3 在线 payload

```json
{
  "status": "online",
  "ip": "192.168.1.122",
  "fw": "csi-softap-mqtt-1.0.0",
  "ts": 240
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `status` | 在线状态，正常为 `online` |
| `ip` | C板 STA 获取到的局域网 IP |
| `fw` | 固件版本 |
| `ts` | C板本地运行时间戳，单位秒 |

### 6.4 离线 Last Will

```json
{
  "status": "offline",
  "reason": "lwt"
}
```

---

## 7. up/wifi：Wi-Fi 连接信息

### 7.1 Topic

```text
csi/v1/devices/{device_name}/up/wifi
```

### 7.2 触发时机

当前代码在 C板获取 IP 并启动 MQTT 后发布 Wi-Fi 信息；主要用于记录设备当前连接的 Wi-Fi 状态。

### 7.3 payload 示例

```json
{
  "ok": true,
  "ip": "192.168.1.122",
  "rssi": -45,
  "ssid": "your_wifi_ssid",
  "ts": 240
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `ok` | Wi-Fi 是否连接成功 |
| `ip` | C板 STA IP |
| `rssi` | 当前连接 AP 的 RSSI |
| `ssid` | 当前连接的 Wi-Fi 名称 |
| `ts` | C板本地运行时间戳，单位秒 |

---

## 8. up/status：设备周期状态

### 8.1 Topic

```text
csi/v1/devices/{device_name}/up/status
```

### 8.2 触发时机

C板每 5 秒发布一次。

```c
#define STATUS_PUBLISH_MS 5000
```

MQTT 连接成功、状态变化时也可能强制发布一次。

### 8.3 payload 示例

```json
{
  "state": "uploading",
  "session": "sess-20260702-000001",
  "uart": true,
  "upload": true,
  "rssi": -45,
  "heap": 158720,
  "uptime": 240000,
  "ts": 240
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `state` | 当前状态：`booting` / `idle` / `uploading` / `fault` |
| `session` | 当前检测会话ID |
| `uart` | 当前代码固定为 `true`，表示 UART 链路功能启用 |
| `upload` | 是否正在上传 CSI |
| `rssi` | C板连接路由器的 Wi-Fi RSSI |
| `heap` | C板当前剩余堆内存 |
| `uptime` | C板启动后的运行时间，单位 ms |
| `ts` | C板本地运行时间戳，单位秒 |

---

## 9. up/csi：CSI 数据上传

### 9.1 Topic

```text
csi/v1/devices/{device_name}/up/csi
```

### 9.2 触发时机

C板收到 `down/control` 的 `start` 后进入 `uploading` 状态。

之后 C板接收 B 板通过 UART 发来的 CSI 帧，并按 1 秒打包上传。

当前配置：

```c
#define CSI_BATCH_INTERVAL_MS 1000
#define MAX_BATCH_FRAMES      40
```

理论上 A/B 约 30Hz 时，每包约 29~30 帧。

### 9.3 payload 示例

```json
{
  "session": "sess-20260702-000001",
  "batch": 1,
  "frames": 30,
  "seq0": 1,
  "seq1": 30,
  "ts0": 123456,
  "ts1": 1123456,
  "fmt": "csib64-v2-1s",
  "bytes": 7524,
  "rssi": -45,
  "invalid": 0,
  "data": "base64_encoded_binary_csi_data",
  "ts": 240
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `session` | 当前检测会话ID |
| `batch` | C板本地 CSI 包编号，从 1 开始递增 |
| `frames` | 本包包含的 CSI 帧数 |
| `seq0` | 本包第一帧的 UART CSI 序号 |
| `seq1` | 本包最后一帧的 UART CSI 序号 |
| `ts0` | 本包第一帧的 CSI 时间戳，单位 us |
| `ts1` | 本包最后一帧的 CSI 时间戳，单位 us |
| `fmt` | 数据格式，当前为 `csib64-v2-1s` |
| `bytes` | Base64 解码后的二进制数据长度 |
| `rssi` | 本包 CSI 帧的平均 RSSI |
| `invalid` | 本包 `first_word_invalid` 的帧数 |
| `data` | Base64 编码后的二进制 CSI batch |
| `ts` | C板本地运行时间戳，单位秒 |

### 9.4 data 解码后的二进制结构

`data` 字段 Base64 解码后，是一个完整的二进制 CSI batch。

整体结构：

```text
batch_header
frame_header_0
csi_payload_0
frame_header_1
csi_payload_1
...
frame_header_N
csi_payload_N
```

batch header：

```text
magic0        1 byte
magic1        1 byte
version       1 byte
frame_count   1 byte
batch_no      4 bytes
```

每帧 frame header：

```text
seq                  4 bytes
timestamp_us         8 bytes
rssi                 1 byte
first_word_invalid   1 byte
csi_len              2 bytes
```

随后紧跟该帧的原始 CSI payload：

```text
csi_payload          csi_len bytes
```

Python 后端处理时，应先 JSON 解析，再 Base64 解码，最后按上述二进制格式拆帧。

---

## 10. up/fault：故障上报

### 10.1 Topic

```text
csi/v1/devices/{device_name}/up/fault
```

### 10.2 触发时机

C板检测到故障时发布。

当前主要故障包括：

| code | 触发原因 |
|---|---|
| `UART_TIMEOUT` | `uploading` 状态下超过 `UART_TIMEOUT_MS` 没有收到 B板 CSI |
| `HEAP_LOW` | C板打包 CSI 或构造 JSON 时内存不足 |

### 10.3 UART 中断故障示例

```json
{
  "code": "UART_TIMEOUT",
  "msg": "no csi frame from b board",
  "state": "fault",
  "ts": 300
}
```

### 10.4 堆内存不足故障示例

```json
{
  "code": "HEAP_LOW",
  "msg": "build csi json failed",
  "state": "uploading",
  "ts": 300
}
```

### 10.5 fault 限流

C板对 fault 做了限流，避免异常时高频刷屏。

```c
#define FAULT_LIMIT_MS 5000
```

也就是同类故障不会无限制高频发布。

---

## 11. 一次完整控制流程示例

### 11.1 设备上电

C板连接 Wi-Fi 和 MQTT 后发布：

```text
up/online
up/wifi
up/status
```

### 11.2 Python 启动检测

Python 发布：

```text
down/control
```

payload：

```json
{
  "cmd": "control",
  "action": "start",
  "session": "sess-20260702-000001"
}
```

C板执行：

```text
清空CSI缓存
发送 CSI_START 给B板
状态变为 uploading
发布 up/ack
开始接收B板UART CSI
每1秒发布 up/csi
每5秒发布 up/status
```

### 11.3 Python 停止检测

Python 发布：

```json
{
  "cmd": "control",
  "action": "stop",
  "session": "sess-20260702-000001"
}
```

C板执行：

```text
发送 CSI_STOP 给B板
停止上传
清空CSI缓存
状态变为 idle
发布 up/ack
继续每5秒发布 up/status
```

### 11.4 检测中 CSI 中断

如果 C板处于 `uploading` 状态，但长时间没有收到 B板 CSI，则执行：

```text
状态变为 fault
发布 up/fault
继续发布 up/status
```

---

## 12. Python 后端建议处理逻辑

Python 后端建议订阅：

```text
csi/v1/devices/+/up/online
csi/v1/devices/+/up/wifi
csi/v1/devices/+/up/status
csi/v1/devices/+/up/ack
csi/v1/devices/+/up/fault
csi/v1/devices/+/up/csi
```

控制某个设备时发布：

```text
csi/v1/devices/{device_name}/down/control
```

Python 后端建议维护以下状态：

| 数据 | 来源 |
|---|---|
| 设备在线 / 离线 | `up/online` |
| Wi-Fi 信息 | `up/wifi` |
| 当前运行状态 | `up/status` |
| 控制命令是否成功 | `up/ack` |
| 硬件故障 | `up/fault` |
| CSI 数据流 | `up/csi` |

`up/csi` 的 `data` 字段需要：

```text
JSON解析
  ↓
取 data 字段
  ↓
Base64解码
  ↓
解析 batch header
  ↓
逐帧解析 frame header + CSI payload
  ↓
送入算法或保存数据库
```

---

## 13. 当前版本关键参数汇总

| 参数 | 当前值 | 说明 |
|---|---:|---|
| UART 波特率 | `921600` | B板与C板通信 |
| CSI 打包周期 | `1000 ms` | C板每1秒发布一次 `up/csi` |
| 最大 batch 帧数 | `40` | 适配约30Hz CSI |
| status 周期 | `5000 ms` | C板每5秒发布一次状态 |
| UART 超时 | `5000 ms` | uploading 状态下超过该时间无 CSI 则 fault |
| fault 限流 | `5000 ms` | 避免故障 topic 高频发布 |
| CSI MQTT QoS | `0` | 数据流优先低延迟 |
| status / ack / fault QoS | `1` | 状态和控制回复要求可靠 |
| online retain | `true` | 保留在线状态 |
| wifi retain | `true` | 保留 Wi-Fi 信息 |

---

## 14. 当前设计结论

当前最新版代码的核心设计是：

```text
C板只负责联网、配网、控制和上传
B板只负责CSI接收和UART二进制输出
Python只通过MQTT控制C板，不直接接触硬件UART
```

数据流和控制流分离：

```text
控制流：Python -> MQTT down/control -> C板 -> UART命令 -> B板
数据流：A板无线帧 -> B板CSI -> UART二进制 -> C板 -> MQTT up/csi -> Python
状态流：C板 -> MQTT up/online / up/wifi / up/status / up/ack / up/fault -> Python
```

这种结构便于后续接入 Flask 后端、微信小程序和算法服务。
