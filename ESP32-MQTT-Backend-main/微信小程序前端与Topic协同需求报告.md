# 微信小程序前端与 Topic 协同需求报告

> 版本：v2.2  
> 日期：2026-07-03  
> 受众：微信小程序前端、Python 后端

## 文档目标

本文说明：

1. 当前项目已具备的基础能力。
2. 用户、设备、检测和跌倒告警的业务逻辑。
3. 微信小程序与 Python 后端的 API 协议。

```text
微信小程序
   ↕ HTTPS / WebSocket
Python 后端
   ↕ SQLite        ↕ MQTT
数据库           Mosquitto ↔ ESP
```

小程序不直接连接 MQTT。小程序通过 HTTP 发起操作，Python 通过 WebSocket
推送设备状态、控制结果、网络质量和跌倒结果。

---

# 第一部分：当前项目基础

当前已经完成：

| 能力 | 状态 |
|---|---|
| Flask 后端 | 已建立 |
| SQLite/SQLAlchemy | 已接入 |
| 管理员、用户、设备 | 已建立基础模型 |
| 用户与设备关系 | 已支持一名用户拥有多台设备 |
| 用户和设备管理 | 已支持增删改查 |
| Mosquitto 配置 | 已完成 |
| Topic 与 Client ID | 已完成生成规则 |
| 六个上行 Topic | 已支持精确订阅 |
| start/stop | 已支持控制消息发布 |
| cmd/session | 已支持生成 |

下一阶段需要完成：

- 微信小程序登录和访问令牌。
- 用户信息入库与更新。
- 当前用户设备查询。
- 设备在线、离线、出错状态聚合。
- 启动和停止 API。
- ACK 匹配、重试与超时。
- CSI 网络质量判断。
- 跌倒检测结果推送。
- WebSocket 实时事件。

---

# 第二部分：业务逻辑与数据库

## 2.1 用户首次进入小程序

流程：

```text
小程序获取微信临时登录凭证
  → 发送给 Python
  → Python 确定微信用户身份
  → 查询或创建 user
  → 更新最近登录时间
  → 返回后端 access_token
```

微信身份与昵称、头像资料应分开处理：

- 首次进入可以先完成身份登录。
- 昵称和头像允许为空。
- 小程序后续获得昵称和头像后再提交更新。
- 后端不能假设首次登录一定能取得完整用户资料。

## 2.2 user 表

建议字段：

| 字段 | 说明 |
|---|---|
| `id` | 用户主键 |
| `wx_openid` | 当前小程序用户唯一身份，唯一索引 |
| `wx_unionid` | 跨应用身份，预留，可为空 |
| `nickname` | 昵称，可为空 |
| `avatar_url` | 头像，可为空 |
| `status` | `active/disabled` |
| `last_login_at` | 最近登录时间 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

现有 username/password 可以在迁移期保留用于本地测试，正式小程序使用
`wx_openid` 识别用户。

## 2.3 device 表

建议字段：

| 字段 | 说明 |
|---|---|
| `id` | 设备主键 |
| `device_name` | 硬件唯一名称，用于生成 MQTT Topic |
| `display_name` | 小程序显示名称 |
| `owner_id` | 绑定用户，外键指向 user.id |
| `status` | 管理状态 `enabled/disabled` |
| `location` | 安装位置 |
| `remark` | 管理备注 |
| `last_seen_at` | 最近有效通信时间 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

关系：

```text
user.id  1 ───── N  device.owner_id
```

- 一个用户可以有多台设备。
- 一台设备当前只属于一个用户。
- 小程序只能访问当前 user.id 对应的设备。
- `device_name` 必须符合 `^[A-Za-z0-9_-]{1,32}$`。
- 完整 Topic 由 Python 根据 device_name 动态生成。

## 2.4 用户登录后的设备状态

用户登录后：

1. Python 根据 user.id 查询其设备。
2. Python 确保这些设备已经建立 MQTT 订阅。
3. 已订阅设备直接复用，不能因重复登录创建重复连接。
4. Python 结合数据库记录与 MQTT 状态生成设备快照。
5. 小程序展示设备状态。

前端主状态统一为：

```text
online     在线
offline    离线
error      出错
```

状态规则：

- `up/online=online`：在线。
- 收到 offline 或无法确认在线：离线。
- 收到 `up/fault`：出错。
- 出错优先级高于在线和离线。

## 2.5 启动检测

启动前，Python 必须确认：

- 用户 active。
- 设备属于当前用户。
- 设备 enabled。
- 设备 MQTT 在线。
- `up/status` 未超时。
- 当前没有运行中的检测。

流程：

```text
小程序请求 start
  → Python 校验用户、设备和实时状态
  → Python 生成 cmd 和 session
  → 发布 down/control(start)
  → ESP 回复 up/ack
  → ESP 每 2 秒发布 up/csi
  → Python 启动网络质量分析和跌倒检测
```

HTTP 请求被接受不代表 ESP 已启动。真正启动成功以匹配 `cmd` 的 ACK 为准。

## 2.6 CSI、网络质量与跌倒检测

Python 根据 CSI 的以下信息检查网络和数据质量：

```text
session
batch
frames
seq0 / seq1
ts0 / ts1
crc
```

对小程序输出简化质量：

```text
good       良好
fair       一般
poor       较差
unknown    暂无数据
```

原始 CSI 不发送给小程序。有效 CSI 进入跌倒检测算法，第一阶段结果为：

```text
0 = 未检测到跌倒
1 = 检测到跌倒
```

结果为 1 时，Python 立即通过 WebSocket 推送跌倒事件。算法细节和置信度后续
由 Python 算法模块补充。

## 2.7 停止检测

```text
小程序请求 stop
  → Python 读取当前 session
  → 发布 down/control(stop)
  → ESP 回复 up/ack
  → ESP 停止 up/csi
  → Python 停止网络质量分析和跌倒检测
```

停止结果以 ACK 为准，小程序不能自行生成 session。

## 2.8 数据库与实时状态边界

数据库保存：

```text
用户身份
设备信息
用户与设备关系
设备启用状态
最近通信时间
```

Python 运行时保存：

```text
在线、Wi-Fi 和设备运行状态
cmd 和 session
ACK 等待状态
CSI 网络质量
当前故障
最新算法结果
```

数据库不保存原始 CSI 或每一条 MQTT 消息。

---

# 第三部分：小程序 API

## 3.1 API 原则

1. 所有业务接口使用 HTTPS。
2. 登录后请求携带后端 access_token。
3. 小程序不保存 MQTT 配置。
4. 小程序不自行生成 Topic、cmd 或 session。
5. HTTP 用于请求，WebSocket 用于后端主动推送。
6. WebSocket 断开后通过 GET 接口恢复当前状态。

## 3.2 API 清单

| 方法 | 路径 | 功能 |
|---|---|---|
| POST | `/api/v1/auth/wechat-login` | 微信登录 |
| PATCH | `/api/v1/me/profile` | 更新昵称和头像 |
| GET | `/api/v1/me` | 当前用户 |
| GET | `/api/v1/devices` | 当前用户设备列表 |
| GET | `/api/v1/devices/{device_name}` | 设备详情 |
| POST | `/api/v1/devices/{device_name}/control` | 启动或停止 |
| GET | `/api/v1/detections/{session}` | 检测状态恢复 |
| WSS | `/ws/v1/events` | 实时事件 |

## 3.3 登录与用户信息

登录请求：

```http
POST /api/v1/auth/wechat-login
```

```json
{
  "code": "微信临时登录凭证"
}
```

响应：

```json
{
  "access_token": "python-backend-token",
  "expires_in": 7200,
  "user": {
    "id": 12,
    "nickname": null,
    "avatar_url": null,
    "is_new_user": true
  }
}
```

资料更新：

```http
PATCH /api/v1/me/profile
Authorization: Bearer <token>
```

```json
{
  "nickname": "微信用户",
  "avatar_url": "https://..."
}
```

## 3.4 设备列表

```http
GET /api/v1/devices
Authorization: Bearer <token>
```

```json
{
  "items": [
    {
      "device_name": "csi-gw-001",
      "display_name": "客厅设备",
      "location": "客厅",
      "state": "online",
      "detection_state": "idle",
      "last_seen_at": "2026-07-03T14:30:00+08:00"
    }
  ]
}
```

`state` 只返回 `online/offline/error`。

## 3.5 设备详情

```http
GET /api/v1/devices/{device_name}
Authorization: Bearer <token>
```

```json
{
  "device_name": "csi-gw-001",
  "display_name": "客厅设备",
  "state": "online",
  "fault": null,
  "wifi": {
    "ok": true,
    "rssi": -48
  },
  "runtime": {
    "state": "idle",
    "updated_at": "2026-07-03T14:30:00+08:00"
  },
  "detection": {
    "state": "idle",
    "session": null,
    "network_quality": "unknown",
    "last_csi_at": null
  }
}
```

后端必须确认 device.owner_id 等于当前 user.id。

## 3.6 控制接口

```http
POST /api/v1/devices/{device_name}/control
Authorization: Bearer <token>
Idempotency-Key: <uuid>
```

请求：

```json
{
  "action": "start"
}
```

或：

```json
{
  "action": "stop"
}
```

响应：

```json
{
  "accepted": true,
  "cmd": "cmd-20260703-000001",
  "session": "sess-20260703-000001",
  "control_state": "waiting_ack"
}
```

前端收到 accepted 后显示“等待设备确认”，最终结果以 WebSocket
`control.result` 为准。

## 3.7 检测状态

```http
GET /api/v1/detections/{session}
Authorization: Bearer <token>
```

```json
{
  "session": "sess-20260703-000001",
  "device_name": "csi-gw-001",
  "state": "running",
  "network_quality": "good",
  "last_csi_at": "2026-07-03T14:30:04+08:00",
  "latest_result": 0
}
```

该接口用于页面刷新和 WebSocket 重连后的状态恢复。

## 3.8 WebSocket 事件

```text
WSS /ws/v1/events
```

统一格式：

```json
{
  "event": "device.state.changed",
  "device_name": "csi-gw-001",
  "server_time": "2026-07-03T14:30:05+08:00",
  "data": {}
}
```

| 事件 | 用途 |
|---|---|
| `device.state.changed` | 在线、离线、出错 |
| `device.wifi.changed` | Wi-Fi 与 RSSI |
| `device.runtime.changed` | ESP 运行状态 |
| `device.fault` | 故障内容 |
| `control.result` | start/stop 最终结果 |
| `detection.network-quality` | CSI 网络质量 |
| `detection.fall-result` | 跌倒检测结果 |

跌倒事件：

```json
{
  "event": "detection.fall-result",
  "device_name": "csi-gw-001",
  "data": {
    "session": "sess-20260703-000001",
    "result": 1,
    "fall_detected": true,
    "detected_at": "2026-07-03T14:30:06+08:00"
  }
}
```

## 3.9 七类 Topic 与业务映射

| Topic | 后端作用 | 小程序结果 |
|---|---|---|
| `up/online` | 判断在线 | `device.state.changed` |
| `up/wifi` | 获取 Wi-Fi/RSSI | `device.wifi.changed` |
| `up/status` | 获取运行状态 | `device.runtime.changed` |
| `down/control` | 发布 start/stop | 控制 API |
| `up/ack` | 确认控制结果 | `control.result` |
| `up/csi` | 质量判断和算法输入 | quality/fall 事件 |
| `up/fault` | 设备故障 | `device.fault` |

## 3.10 时间与超时

| 规则 | 时间 |
|---|---:|
| status 正常上报 | 每 5 秒 |
| status 超时 | 15 秒 |
| CSI 正常上报 | 每 2 秒 |
| CSI 超时 | 8 秒 |
| ACK 首次等待 | 3 秒 |
| ACK 重试 | 相同 cmd/session 重试一次 |
| 同类 fault 限流 | 5 秒 |

## 3.11 前端必须遵守

1. 登录后保存后端 token。
2. 所有业务请求携带 Authorization。
3. 不直接连接 MQTT，不保存 MQTT 密码。
4. 不允许用户修改 device_name。
5. 控制请求携带 Idempotency-Key。
6. 控制处理中禁止重复点击。
7. HTTP accepted 后显示等待确认。
8. 以 `control.result` 判断控制成功。
9. 检测中显示网络质量。
10. `fall-result=1` 时立即明显提示。
11. fault 到达时主状态显示 error。
12. WebSocket 重连后重新获取设备或检测状态。
13. 不展示或保存原始 CSI。

## 3.12 主要错误码

| 错误码 | 含义 |
|---|---|
| `AUTH_REQUIRED` | 未登录或 token 失效 |
| `USER_DISABLED` | 用户已禁用 |
| `DEVICE_NOT_FOUND` | 设备不存在 |
| `DEVICE_NOT_OWNED` | 设备不属于当前用户 |
| `DEVICE_DISABLED` | 设备已禁用 |
| `DEVICE_OFFLINE` | 设备离线 |
| `STATUS_TIMEOUT` | 状态超时 |
| `CONTROL_BUSY` | 已有控制正在执行 |
| `ACK_REJECTED` | 设备拒绝命令 |
| `ACK_TIMEOUT` | 设备未回复 |
| `CSI_TIMEOUT` | CSI 数据中断 |

## 3.13 最小验收标准

- 首次进入可以创建或更新 user。
- 登录后只返回当前用户设备。
- 设备正确显示在线、离线、出错。
- 用户不能访问其他用户设备。
- 在线设备可以启动和停止。
- 控制结果以 ACK 为准。
- 检测期间可以显示网络质量。
- 算法返回 1 时可以收到跌倒事件。
- 停止后网络质量分析和预测同时停止。
- WebSocket 重连后可以恢复当前状态。

---

# 结论

```text
数据库决定：用户可以使用哪些设备
MQTT 决定：设备当前发生了什么
Python API 决定：小程序显示什么、控制什么
```

小程序只需对接登录、设备列表、设备详情、控制、检测状态和 WebSocket，不需要
了解 Mosquitto 内部配置或原始 CSI 数据。
