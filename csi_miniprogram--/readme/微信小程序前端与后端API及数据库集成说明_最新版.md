# 微信小程序前端与后端 API 及数据库集成说明（最新版）

> 对接对象：微信小程序前端开发人员  
> 后端技术：Flask + SQLAlchemy + SQLite + MQTT + WebSocket  
> API 版本：`v1`  
> 更新时间：2026-07-04  

---

## 1. 文档目的

本文用于指导微信小程序开发人员优化现有前端逻辑，主要目标是：

1. 小程序首次进入时必须通过后端完成微信用户建档。
2. 用户、设备状态和跌倒事件必须读取真实数据库数据。
3. 删除前端现有的虚拟用户、虚拟设备、随机状态和模拟告警。
4. 明确当前可以调用的 API 及其数据库操作。
5. 形成登录、设备查询、启动、停止和跌倒告警的完整业务闭环。

本文以当前后端代码为准，文中标记为“必须”的内容属于本次前端改造的
验收要求。

---

## 2. 系统边界

```text
微信小程序
  │
  ├── HTTPS API：登录、查询、控制、更新跌倒事件
  │
  └── WebSocket：接收设备状态、网络质量和跌倒告警
          │
          ▼
Python Flask 后端
  │
  ├── SQLAlchemy / SQLite：保存用户、设备快照和跌倒事件
  │
  └── MQTT：订阅硬件上报并向硬件发布控制命令
          │
          ▼
ESP 硬件设备
```

前端需要遵守以下边界：

- 小程序不直接连接 MQTT。
- 小程序不能直接访问 SQLite。
- 小程序不能提交或信任自己提供的 `openid`。
- 小程序不能获得微信 `session_key`、`WECHAT_SECRET` 或 MQTT 密码。
- 设备实时状态以 Python 后端返回的数据库快照为准。

---

## 3. 前端必须完成的改造

### 3.1 首次进入必须完成用户建档

小程序不能直接连接 SQLite。这里所说的“访问数据库”，是指前端调用 Flask
API，由后端查询和更新数据库。

小程序每次冷启动都必须先执行：

```text
小程序启动
→ 调用 wx.login() 获取一次性 code
→ POST /api/v1/auth/wechat-login
→ 后端调用微信 code2Session
→ 后端根据 openid 查询 users 表
→ users 中不存在：自动新增用户
→ users 中已存在：更新 last_login_at
→ 后端返回 access_token 和 is_new_user
```

前端不能先展示写死的默认用户，也不能先生成一个本地用户 ID。

`wx.login()` 只负责提供登录 code，不会自动提供可直接保存的昵称和头像。
如果 `is_new_user = true`，前端应在用户授权或填写资料后调用：

```http
PATCH /api/v1/me/profile
```

把真实的 `nickname`、`avatar_url` 保存到 `users` 表。如果用户暂时不授权，
数据库中的用户身份仍然已经创建，前端应显示默认头像和“微信用户”等空状态，
不能使用虚拟昵称冒充真实资料。

### 3.2 首屏必须读取真实业务数据

登录成功并保存 Token 后，前端必须立即请求：

```text
GET /api/v1/me
GET /api/v1/devices
GET /api/v1/fall-events?limit=20
```

然后再建立 WebSocket：

```text
/ws/v1/events?token=<access_token>
```

页面数据来源必须满足：

| 页面内容 | 唯一可信来源 |
|---|---|
| 当前用户 | `GET /api/v1/me` |
| 用户昵称、头像 | `users` 表，通过用户 API 返回 |
| 设备列表 | `GET /api/v1/devices` |
| 设备在线、离线、故障状态 | `devices.state`，通过设备 API 返回 |
| 检测状态 | `devices.detection_state` |
| 网络质量 | `devices.network_quality` |
| 跌倒记录 | `GET /api/v1/fall-events` |
| 实时变化 | WebSocket 通知后刷新或合并后端数据 |

### 3.3 必须删除全部虚拟数据

前端项目中以下内容必须删除：

- 写死的用户名称、头像、手机号和用户 ID。
- `mockUsers`、`mockDevices`、`demoDevice` 等模拟数组。
- 写死的设备在线、离线、运行中状态。
- 使用随机数或定时器模拟设备状态、网络质量和跌倒结果。
- 虚拟跌倒记录、虚拟告警时间和演示通知。
- API 请求失败后回退到 Mock 数据的逻辑。
- 本地写死的检测 session。

前端初始状态只能使用空值：

```javascript
data: {
  currentUser: null,
  devices: [],
  fallEvents: [],
  loading: true,
  errorMessage: ""
}
```

数据库没有数据时必须展示真实空状态：

```text
devices.items = []     → 显示“暂未绑定设备”
fall-events.items = [] → 显示“暂无跌倒记录”
请求失败               → 显示错误和重试按钮
请求中                 → 显示加载状态
```

禁止为了让页面“看起来有内容”而补充虚拟设备或告警。

### 3.4 页面恢复与数据重新同步

以下时机必须重新读取后端数据：

- 小程序冷启动。
- Token 重新登录成功。
- 小程序从后台切回前台。
- WebSocket 首次连接成功。
- WebSocket 断线重连成功。
- 用户完成启动或停止操作。
- 收到跌倒告警。

前端可以临时保存 Token，但不能把本地缓存的设备状态当成最终状态。

---

## 4. 服务地址与认证

### 4.1 后端地址（由联调人员填写）

本文不写死后端 IP。联调前由后端开发人员填写：

```text
后端 IP：____________________________
后端端口：__________________________

HTTP API：http://________________:____/api/v1
WebSocket：ws://________________:____/ws/v1/events
```

前端建议只在一个配置文件中维护地址：

```javascript
const BACKEND_IP = "";
const BACKEND_PORT = "";

const API_BASE_URL =
  `http://${BACKEND_IP}:${BACKEND_PORT}/api/v1`;
const WS_BASE_URL =
  `ws://${BACKEND_IP}:${BACKEND_PORT}/ws/v1/events`;
```

不得在不同页面分别写死 IP。正式环境再统一替换为经过备案和配置的
`https://`、`wss://` 域名。

### 4.2 Token 认证

除微信登录接口外，所有 API 请求都需要：

```http
Authorization: Bearer <access_token>
```

当前 Token 默认有效期：

```text
7200 秒
```

Token 过期或无效时返回：

```json
{
  "error": "AUTH_REQUIRED",
  "message": "登录状态无效或已过期"
}
```

前端收到 HTTP `401` 后，应重新执行 `wx.login()`，不要循环重试旧 Token。

---

## 5. 当前 API 总表

| 功能 | 方法 | 地址 | 认证 | 对应数据库 |
|---|---|---|---|---|
| 微信登录 | POST | `/api/v1/auth/wechat-login` | 否 | 查询/新增/更新 `users` |
| 获取当前用户 | GET | `/api/v1/me` | 是 | 查询 `users` |
| 更新用户资料 | PATCH | `/api/v1/me/profile` | 是 | 更新 `users` |
| 获取绑定设备 | GET | `/api/v1/devices` | 是 | 查询 `devices` |
| 获取设备详情 | GET | `/api/v1/devices/{device_name}` | 是 | 查询 `devices` |
| 启动/停止检测 | POST | `/api/v1/devices/{device_name}/control` | 是 | 校验并更新 `devices` |
| 查询跌倒事件 | GET | `/api/v1/fall-events` | 是 | 查询 `fall_events` |
| 处理跌倒事件 | PATCH | `/api/v1/fall-events/{id}` | 是 | 更新 `fall_events` |
| 实时事件 | WebSocket | `/ws/v1/events?token=...` | 是 | 推送最新业务变化 |

注意：

- 当前没有小程序侧“绑定设备”或“解绑设备”API。
- 设备通过管理员页面登记，并通过 `devices.owner_user_id` 绑定用户。
- 管理员内部 `/api/*` 接口不提供给小程序使用。

---

## 6. 数据库与 API 的对应关系

当前数据库只保留四张表：

```text
admin
users
devices
fall_events
```

其中 `admin` 只用于内部管理员网页，小程序业务只涉及后三张表。

### 6.1 users：微信用户

主要字段：

| 字段 | 用途 | 是否返回前端 |
|---|---|---|
| `id` | 后端内部用户 ID | 是 |
| `wx_openid` | 微信用户唯一身份 | 否 |
| `wx_unionid` | 微信开放平台统一身份 | 否 |
| `wx_session_key_enc` | 预留的加密 session_key 字段 | 否 |
| `nickname` | 小程序显示昵称 | 是 |
| `avatar_url` | 小程序头像地址 | 是 |
| `phone` | 手机号，当前由管理员维护 | 是 |
| `role` | 业务角色 | 小程序当前不使用 |
| `status` | `active / disabled` | 是 |
| `last_login_at` | 最近登录时间 | 是 |

API 集成方式：

```text
POST /auth/wechat-login
  → 根据微信返回的 openid 查询 users
  → 不存在则新增
  → 存在则更新 unionid 和 last_login_at

GET /me
  → 根据 Token 中的 user_id 查询 users

PATCH /me/profile
  → 更新 nickname、avatar_url
```

业务用户没有独立密码，身份完全来自微信登录。

### 6.2 devices：设备权威快照

主要字段：

| 字段 | 用途 |
|---|---|
| `device_name` | 硬件唯一名称，也是 MQTT Topic 的设备段 |
| `display_name` | 小程序显示名称 |
| `owner_user_id` | 当前绑定用户，对应 `users.id` |
| `enabled` | 管理端是否允许使用 |
| `state` | `online / offline / error` |
| `runtime_state` | `idle / running / stopped` |
| `detection_state` | `idle / starting / running / stopping` |
| `current_session` | 当前检测 session |
| `network_quality` | `good / fair / poor / unknown` |
| `fault_code` | 设备故障码 |
| `fault_message` | 设备故障说明 |
| `last_seen_at` | 最近有效通信时间 |
| `last_status_at` | 最近状态上报时间 |
| `last_csi_at` | 最近 CSI 上报时间 |

API 集成方式：

```text
GET /devices
  → 只查询 owner_user_id = 当前用户 ID 的设备

GET /devices/{device_name}
  → 查询设备详情并再次验证设备归属

POST /devices/{device_name}/control
  → 校验 devices 当前快照
  → 通过 MQTT 发布控制命令
  → 发布成功后更新 detection_state、current_session 等字段
```

设备状态不是由小程序写入，而是后端根据 MQTT 上报和离线扫描更新。

### 6.3 fall_events：跌倒事件

只有跌倒算法返回 `1` 时才创建记录。

主要字段：

| 字段 | 用途 |
|---|---|
| `id` | 跌倒事件 ID |
| `user_id` | 事件所属用户 |
| `device_id` | 事件对应设备 |
| `device_name` | 冗余设备名，便于查询 |
| `session` | 发生跌倒时的检测 session |
| `result` | 当前固定为 `1` |
| `network_quality` | 跌倒发生时的网络质量 |
| `occurred_at` | 发生时间 |
| `status` | `pending / confirmed / ignored` |
| `notified` | 是否已推送通知 |
| `handled_at` | 用户处理时间 |

API 集成方式：

```text
算法返回 1
  → 后端新增 fall_events
  → 提交数据库
  → WebSocket 推送 detection.fall-result

GET /fall-events
  → 只查询 user_id = 当前用户 ID 的记录

PATCH /fall-events/{id}
  → 校验记录归属
  → 更新 status 和 handled_at
```

---

## 7. API 详细约定

### 7.1 微信登录

```http
POST /api/v1/auth/wechat-login
Content-Type: application/json
```

请求：

```json
{
  "code": "wx.login 返回的一次性 code"
}
```

响应：

```json
{
  "access_token": "backend-access-token",
  "expires_in": 7200,
  "user": {
    "id": 12,
    "nickname": null,
    "avatar_url": null,
    "is_new_user": true
  }
}
```

数据库操作：

```text
1. 后端调用微信 code2Session。
2. 根据 wx_openid 查询 users。
3. 首次登录新增 users；再次登录更新 last_login_at。
4. 数据库提交成功后签发 access_token。
5. 根据用户绑定设备启动相应 MQTT 订阅。
```

### 7.2 获取当前用户

```http
GET /api/v1/me
Authorization: Bearer <token>
```

响应：

```json
{
  "id": 12,
  "nickname": "微信用户",
  "avatar_url": "https://...",
  "phone": null,
  "status": "active",
  "last_login_at": "2026-07-04T10:00:00"
}
```

### 7.3 更新用户资料

```http
PATCH /api/v1/me/profile
Authorization: Bearer <token>
Content-Type: application/json
```

请求：

```json
{
  "nickname": "微信用户",
  "avatar_url": "https://..."
}
```

约束：

- `nickname` 最长 64 个字符。
- `avatar_url` 最长 255 个字符。
- 至少提交其中一个字段。
- 当前接口不允许小程序直接修改手机号和账户状态。

### 7.4 获取设备列表

```http
GET /api/v1/devices
Authorization: Bearer <token>
```

响应：

```json
{
  "items": [
    {
      "device_name": "csi-gw-001",
      "display_name": "客厅检测器",
      "location": "客厅",
      "state": "online",
      "detection_state": "idle",
      "network_quality": "unknown",
      "last_seen_at": "2026-07-04T10:00:00",
      "fault_message": null
    }
  ]
}
```

设备列表页建议直接使用该响应，不需要前端自行拼接 MQTT 状态。

### 7.5 获取设备详情

```http
GET /api/v1/devices/csi-gw-001
Authorization: Bearer <token>
```

响应：

```json
{
  "device_name": "csi-gw-001",
  "display_name": "客厅检测器",
  "location": "客厅",
  "remark": null,
  "state": "online",
  "enabled": true,
  "last_seen_at": "2026-07-04T10:00:00",
  "runtime": {
    "state": "idle",
    "last_status_at": "2026-07-04T10:00:00"
  },
  "detection": {
    "state": "idle",
    "session": null,
    "network_quality": "unknown",
    "last_csi_at": null
  },
  "fault": {
    "code": null,
    "message": null
  }
}
```

前端进入设备详情页后，应先获取该接口，再决定按钮展示：

```text
state != online            → 禁止启动
enabled == false           → 禁止启动
fault.code != null         → 显示故障
detection.state == idle    → 显示“启动”
detection.state == running → 显示“停止”
starting / stopping        → 按钮进入加载状态，防止重复点击
```

最终是否允许控制仍以后端校验为准。

### 7.6 启动检测

```http
POST /api/v1/devices/csi-gw-001/control
Authorization: Bearer <token>
Idempotency-Key: 由前端生成的唯一值
Content-Type: application/json
```

请求：

```json
{
  "action": "start"
}
```

成功响应，HTTP 状态码为 `202`：

```json
{
  "accepted": true,
  "device_name": "csi-gw-001",
  "action": "start",
  "control_state": "published",
  "session": "sess-20260704-xxxxxx",
  "message": "启动命令已发送"
}
```

后端启动校验：

```text
设备属于当前用户
→ enabled = true
→ state = online
→ 无 fault
→ last_status_at 未超时
→ detection_state = idle
→ 发布 MQTT start
→ 更新 current_session 和 detection_state = starting
```

收到 `202` 只表示命令已发送，不表示硬件已经进入运行状态。

前端应等待 WebSocket 的 `device.runtime.changed`，或重新查询设备详情，直到：

```text
detection.state = running
```

### 7.7 停止检测

请求：

```json
{
  "action": "stop"
}
```

后端流程：

```text
校验 detection_state 为 starting 或 running
→ 使用 current_session 发布 MQTT stop
→ detection_state = stopping
→ 停止当前 CSI 缓冲和算法输入
→ 收到硬件 stopped/idle 状态
→ detection_state = idle
→ current_session = null
```

### 7.8 查询跌倒事件

```http
GET /api/v1/fall-events?limit=20
Authorization: Bearer <token>
```

`limit` 范围由后端限制为 `1–100`。

响应：

```json
{
  "items": [
    {
      "id": 101,
      "device_name": "csi-gw-001",
      "display_name": "客厅检测器",
      "location": "客厅",
      "result": 1,
      "occurred_at": "2026-07-04T10:30:06",
      "network_quality": "good",
      "status": "pending",
      "handled_at": null,
      "remark": null
    }
  ]
}
```

### 7.9 处理跌倒事件

```http
PATCH /api/v1/fall-events/101
Authorization: Bearer <token>
Content-Type: application/json
```

确认事件：

```json
{
  "status": "confirmed"
}
```

忽略误报：

```json
{
  "status": "ignored"
}
```

后端会同时写入 `handled_at`。

---

## 8. WebSocket 实时事件

连接方式：

```text
ws://<后端IP>:<后端端口>/ws/v1/events?token=<access_token>
```

正式环境使用 `wss://`。

连接成功：

```json
{
  "event": "connection.ready",
  "data": {
    "user_id": 12
  }
}
```

业务事件：

| 事件 | 前端用途 |
|---|---|
| `device.state.changed` | 更新在线或离线状态 |
| `device.runtime.changed` | 更新硬件运行状态和检测状态 |
| `device.fault` | 展示设备故障 |
| `detection.network-quality` | 更新 CSI 网络质量 |
| `detection.fall-result` | 弹出跌倒告警并进入告警页面 |

统一格式：

```json
{
  "event": "device.runtime.changed",
  "device_name": "csi-gw-001",
  "data": {
    "runtime_state": "running",
    "detection_state": "running",
    "session": "sess-20260704-xxxxxx"
  }
}
```

跌倒事件示例：

```json
{
  "event": "detection.fall-result",
  "device_name": "csi-gw-001",
  "data": {
    "fall_event_id": 101,
    "session": "sess-20260704-xxxxxx",
    "result": 1,
    "fall_detected": true,
    "network_quality": "good",
    "occurred_at": "2026-07-04T10:30:06"
  }
}
```

WebSocket 只负责提示“发生了变化”，不是永久数据源。

前端在以下情况应重新调用 HTTP API 获取完整快照：

- WebSocket 首次连接成功。
- WebSocket 断线重连成功。
- 小程序从后台回到前台。
- 收到无法识别的事件或发现本地状态不一致。

---

## 9. 推荐前端交互流程

### 9.1 小程序启动

```text
页面进入 loading 状态
→ 清空旧页面中的虚拟用户、设备和告警
wx.login()
→ POST /api/v1/auth/wechat-login
→ 保存 access_token
→ 如果 is_new_user = true，获取用户授权资料并 PATCH /api/v1/me/profile
→ 并行请求 GET /api/v1/me、GET /api/v1/devices、GET /api/v1/fall-events
→ 使用三个接口的真实响应更新页面
→ 建立 WebSocket
→ 页面结束 loading 状态
```

如果任何核心请求失败，前端应保留空数组并显示错误与重试按钮，不得加载
本地 Mock 数据顶替接口结果。

### 9.2 进入设备详情

```text
GET /devices/{device_name}
→ 展示数据库权威快照
→ 根据 state、enabled、fault、detection.state 设置按钮
```

### 9.3 用户点击启动

```text
生成 Idempotency-Key
→ POST control {action: start}
→ 收到 202 后显示“启动中”
→ 等待 device.runtime.changed
→ running 后显示“检测中”和“停止”按钮
```

### 9.4 检测运行中

```text
WebSocket 接收 detection.network-quality
→ 更新 good / fair / poor / unknown

WebSocket 接收 detection.fall-result
→ 展示跌倒告警
→ GET /fall-events 刷新事件列表
```

### 9.5 用户点击停止

```text
POST control {action: stop}
→ 显示“停止中”
→ 等待 device.runtime.changed
→ idle 后恢复“启动”按钮
```

---

## 10. 统一错误处理

错误响应格式：

```json
{
  "error": "DEVICE_OFFLINE",
  "message": "设备离线，无法启动检测"
}
```

| 错误码 | 前端建议 |
|---|---|
| `AUTH_REQUIRED` | 清理旧 Token，重新微信登录 |
| `USER_DISABLED` | 显示账号已禁用，停止业务请求 |
| `INVALID_WECHAT_CODE` | 重新调用 `wx.login()` |
| `WECHAT_LOGIN_FAILED` | 提示登录服务不可用，允许用户重试 |
| `DEVICE_NOT_FOUND` | 返回设备列表并刷新 |
| `DEVICE_NOT_OWNED` | 禁止访问该设备 |
| `DEVICE_DISABLED` | 显示设备已停用 |
| `DEVICE_OFFLINE` | 禁止启动并刷新设备快照 |
| `DEVICE_ERROR` | 展示后端返回的故障信息 |
| `STATUS_TIMEOUT` | 提示状态过期，稍后刷新 |
| `CONTROL_BUSY` | 防止重复控制，等待状态变化 |
| `INVALID_ACTION` | 前端代码错误，只能传 start/stop |
| `IDEMPOTENCY_CONFLICT` | 为新控制操作生成新的幂等键 |
| `MQTT_UNAVAILABLE` | 提示控制服务暂时不可用 |
| `FALL_EVENT_NOT_FOUND` | 刷新跌倒记录列表 |
| `INVALID_FALL_EVENT_STATUS` | 只能提交 confirmed/ignored |

---

## 11. 前端实现注意事项

1. `access_token` 建议使用 `wx.setStorageSync()` 保存。
2. API 请求统一封装，在请求头自动加入 Bearer Token。
3. HTTP `401` 统一触发重新登录。
4. 每次 start/stop 使用新的 `Idempotency-Key`，同一次请求重试必须复用原键。
5. 不要在收到 `202` 后直接显示“运行中”，应先显示“启动中”。
6. 不要根据本地定时器自行判定设备离线，以后端 `state` 为准。
7. 不要自行生成或修改检测 session。
8. 不要向后端传递 openid 作为用户身份。
9. 所有时间字段均按后端 ISO 8601 字符串解析。
10. WebSocket 断线后需要重连，并通过 HTTP API 重新同步状态。

---

## 12. 当前版本暂未提供的功能

以下功能目前不在小程序 API 范围内：

- 小程序自行绑定或解绑设备。
- 小程序修改手机号。
- 小程序修改设备名称、位置或所属用户。
- 查询原始 CSI 数据。
- 查询每一条 MQTT 消息。
- 查看普通算法结果 `0`。
- 前端直接控制 MQTT Topic。

如小程序后续需要设备绑定功能，应新增经过身份校验的绑定 API，而不是让前端直接修改 `owner_user_id`。

---

## 13. 本次前端改造验收清单

交付前需要逐项确认：

- [ ] 项目中已不存在虚拟用户、虚拟设备和虚拟跌倒事件。
- [ ] 项目中已不存在随机生成在线状态、网络质量和跌倒结果的代码。
- [ ] 后端 IP 和端口集中在一个配置文件中，当前保持待填写状态。
- [ ] 小程序冷启动首先调用微信登录 API。
- [ ] 首次用户能够由后端自动写入 `users` 表。
- [ ] 新用户授权资料后会调用 `/me/profile` 保存昵称和头像。
- [ ] 首页用户信息来自 `/me`。
- [ ] 设备列表来自 `/devices`。
- [ ] 设备详情和控制按钮状态来自设备详情 API。
- [ ] 跌倒记录来自 `/fall-events`。
- [ ] 数据库无设备时显示“暂未绑定设备”，不生成演示设备。
- [ ] 数据库无跌倒记录时显示“暂无跌倒记录”，不生成演示告警。
- [ ] API 失败时展示错误和重试，不回退到 Mock 数据。
- [ ] WebSocket 重连或小程序回到前台后会重新同步 HTTP 快照。
- [ ] 收到 HTTP `401` 后会清理旧 Token 并重新登录。
