# 安守 CSI 跌倒检测 Python Flask 后端开发文档（更新详细版）

> 适用对象：Python Flask 后端开发同学  
> 技术栈建议：Python Flask + SQLAlchemy + SQLite + paho-mqtt + WebSocket  
> 硬件通信：Mosquitto MQTT + ESP 设备  
> 版本：v3.0，根据最新业务需求调整  

---

## 1. 后端项目目标

本后端负责连接微信小程序、数据库、MQTT Broker 和 ESP 硬件设备，是整个系统的业务核心。

整体链路：

```text
微信小程序
  ↓ HTTPS / WebSocket
Python Flask 后端
  ↓ SQLite / SQLAlchemy
数据库

Python Flask 后端
  ↓ MQTT / Mosquitto
ESP 硬件设备
```

后端核心职责：

```text
1. 接收小程序 wx.login() 返回的 code。
2. 调用微信 code2Session 获取 openid / unionid / session_key。
3. 将微信身份信息保存到用户表。
4. 生成后端 access_token，返回给小程序。
5. 查询当前用户绑定的设备。
6. 订阅设备 /up/online，判断设备在线。
7. 订阅设备 /up/fault，判断设备出错。
8. 结合在线、离线、错误信息，向小程序返回设备状态。
9. 接收小程序 start / stop 请求。
10. 根据设备在线状态和 /up/status 判断是否可以启动。
11. 发布 /down/control 到 MQTT 控制硬件。
12. 接收硬件每 2 秒上传的 /up/csi。
13. 解析 CSI 时间戳和序号，判断是否丢帧，得到网络质量。
14. 将有效 CSI 送入跌倒检测算法。
15. 算法返回 1 时，保存跌倒记录并推送给小程序。
16. 用户停止设备后，停止 CSI 分析和跌倒检测。
```

---

## 2. 本版本数据库设计原则

根据最新需求，后端数据库只保留三张核心业务表：

```text
1. 用户表 users
2. 设备表 devices
3. 跌倒发生表 fall_events
```

暂时不单独建立以下表：

```text
- wechat_accounts
- control_commands
- detection_sessions
- emergency_contacts
- device_bindings
- mqtt_messages
```

原因：

```text
1. 当前项目是课程/阶段性项目，表结构不宜过度复杂。
2. 一个设备当前只绑定一个用户，可以直接在 devices 表中保存 owner_user_id。
3. 微信身份字段可以先合并到 users 表。
4. 当前运行 session 可以临时保存在 devices 表和 Python 内存中。
5. 控制命令和 ACK 可以先放在 Python 运行时管理，不单独建表。
6. 跌倒事件是需要长期保存的核心业务数据，所以单独建 fall_events 表。
```

---

## 3. 推荐 Flask 项目结构

```text
backend/
  app.py
  config.py
  requirements.txt
  .env

  models/
    __init__.py
    user.py
    device.py
    fall_event.py

  routes/
    __init__.py
    auth.py
    me.py
    devices.py
    fall_events.py

  services/
    wechat_service.py
    token_service.py
    mqtt_service.py
    device_state_service.py
    csi_quality_service.py
    fall_detect_service.py
    websocket_service.py

  utils/
    response.py
    decorators.py
    time_utils.py

  migrations/

  instance/
    app.db
```

如果项目规模较小，也可以先写成：

```text
backend/
  app.py
  models.py
  mqtt_client.py
  services.py
  requirements.txt
```

但是建议至少把路由、模型和 MQTT 逻辑分开，后续更容易维护。

---

## 4. 环境变量配置

`.env` 示例：

```env
FLASK_ENV=development
SECRET_KEY=change-this-secret
DATABASE_URL=sqlite:///instance/app.db

WECHAT_APPID=你的微信小程序appid
WECHAT_SECRET=你的微信小程序secret

MQTT_HOST=127.0.0.1
MQTT_PORT=1883
MQTT_USERNAME=
MQTT_PASSWORD=

TOKEN_EXPIRE_SECONDS=7200
```

说明：

```text
WECHAT_SECRET 不要写死在前端。
session_key 不要返回给小程序。
MQTT 账号密码不要暴露给小程序。
```

---

## 5. 三张核心数据库表

---

# 5.1 用户表 users

## 5.1.1 表作用

用户表保存微信小程序用户身份和用户资料。

用户首次进入小程序时：

```text
小程序 wx.login() 获取 code
  ↓
后端 code2Session 获取 openid / unionid / session_key
  ↓
后端根据 wx_openid 查询 users 表
  ↓
如果不存在，创建新用户
  ↓
如果存在，更新 session_key 和 last_login_at
  ↓
返回 access_token 给小程序
```

## 5.1.2 推荐字段

```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    wx_openid VARCHAR(64) NOT NULL UNIQUE,
    wx_unionid VARCHAR(64),
    wx_session_key_enc TEXT,

    nickname VARCHAR(64),
    avatar_url VARCHAR(255),
    phone VARCHAR(32),

    role VARCHAR(20) NOT NULL DEFAULT 'user',
    status VARCHAR(20) NOT NULL DEFAULT 'active',

    last_login_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);
```

## 5.1.3 字段说明

| 字段 | 说明 |
|---|---|
| `id` | 系统内部用户 ID，后端业务主要使用这个字段 |
| `wx_openid` | 当前微信小程序下的用户唯一身份 |
| `wx_unionid` | 微信开放平台统一身份，可能为空 |
| `wx_session_key_enc` | 加密保存的 session_key，不返回前端 |
| `nickname` | 用户昵称，可为空 |
| `avatar_url` | 用户头像，可为空 |
| `phone` | 手机号，可后续扩展 |
| `role` | 用户角色，普通用户为 user，管理员为 admin |
| `status` | active / disabled |
| `last_login_at` | 最近登录时间 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

## 5.1.4 为什么用户表要保存微信字段

本项目当前只保留三张表，不单独设计微信账号表，所以需要把微信登录相关字段放到 users 表中。

```text
wx_openid 用于识别当前小程序用户。
wx_unionid 用于以后多应用统一用户身份，可为空。
wx_session_key_enc 用于后端需要解密微信加密数据时使用。
```

## 5.1.5 注意事项

```text
1. 前端不能直接传 openid 作为可信身份。
2. 前端不能直接传 session_key。
3. 小程序只传 code。
4. 后端通过 code2Session 获取 openid / unionid / session_key。
5. session_key 应加密保存或只保存在服务端安全位置。
6. 后端返回给小程序的是 access_token，不是 session_key。
```

---

# 5.2 设备表 devices

## 5.2.1 表作用

设备表保存硬件设备信息、设备归属关系和设备最近一次状态快照。

由于当前不单独建立用户-设备绑定表，因此一台设备直接通过 `owner_user_id` 绑定一个用户。

关系：

```text
users.id  1 ───── N  devices.owner_user_id
```

## 5.2.2 推荐字段

```sql
CREATE TABLE devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    device_name VARCHAR(32) NOT NULL UNIQUE,
    display_name VARCHAR(64),
    owner_user_id INTEGER NOT NULL,

    location VARCHAR(128),
    remark TEXT,

    enabled BOOLEAN NOT NULL DEFAULT 1,
    state VARCHAR(20) NOT NULL DEFAULT 'offline',

    runtime_state VARCHAR(20) NOT NULL DEFAULT 'idle',
    detection_state VARCHAR(20) NOT NULL DEFAULT 'idle',
    current_session VARCHAR(64),

    network_quality VARCHAR(20) NOT NULL DEFAULT 'unknown',

    fault_code VARCHAR(64),
    fault_message VARCHAR(255),

    last_seen_at DATETIME,
    last_online_at DATETIME,
    last_status_at DATETIME,
    last_csi_at DATETIME,

    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,

    FOREIGN KEY(owner_user_id) REFERENCES users(id)
);
```

## 5.2.3 字段说明

| 字段 | 说明 |
|---|---|
| `id` | 设备表主键 |
| `device_name` | 硬件唯一名称，用于生成 MQTT Topic |
| `display_name` | 小程序显示名称 |
| `owner_user_id` | 设备所属用户 ID |
| `location` | 安装位置，例如客厅、卧室 |
| `remark` | 备注 |
| `enabled` | 是否启用 |
| `state` | 小程序主状态：online / offline / error |
| `runtime_state` | ESP 运行状态：idle / running / stopped |
| `detection_state` | 检测状态：idle / starting / running / stopping |
| `current_session` | 当前检测 session，停止后可清空 |
| `network_quality` | good / fair / poor / unknown |
| `fault_code` | 故障码 |
| `fault_message` | 故障描述 |
| `last_seen_at` | 最近一次有效通信时间 |
| `last_online_at` | 最近一次 online 时间 |
| `last_status_at` | 最近一次 status 时间 |
| `last_csi_at` | 最近一次 CSI 时间 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

## 5.2.4 设备状态规则

小程序只展示三种主状态：

```text
online   在线
offline  离线
error    出错
```

后端更新规则：

```text
收到 /up/online：state = online
收到 /up/status：更新 last_status_at，必要时 state = online
超过在线超时时间没有收到 online/status：state = offline
收到 /up/fault：state = error，并保存 fault_code / fault_message
```

优先级：

```text
error > online > offline
```

也就是说，如果设备已经收到 fault，则即使之前 online，也要显示 error。

## 5.2.5 device_name 规则

```text
device_name 必须唯一。
device_name 建议只包含字母、数字、下划线和短横线。
推荐正则：^[A-Za-z0-9_-]{1,32}$
```

---

# 5.3 跌倒发生表 fall_events

## 5.3.1 表作用

跌倒发生表用于保存算法判断发生跌倒的事件。

只有当算法返回：

```text
result = 1
```

才需要写入 fall_events 表。

## 5.3.2 推荐字段

```sql
CREATE TABLE fall_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    user_id INTEGER NOT NULL,
    device_id INTEGER NOT NULL,
    device_name VARCHAR(32) NOT NULL,

    session VARCHAR(64),
    result INTEGER NOT NULL DEFAULT 1,

    network_quality VARCHAR(20),
    occurred_at DATETIME NOT NULL,

    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    notified BOOLEAN NOT NULL DEFAULT 0,
    notified_at DATETIME,
    handled_at DATETIME,

    remark TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,

    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(device_id) REFERENCES devices(id)
);
```

## 5.3.3 字段说明

| 字段 | 说明 |
|---|---|
| `id` | 跌倒事件 ID |
| `user_id` | 设备所属用户 ID |
| `device_id` | 设备 ID |
| `device_name` | 冗余保存设备名，方便查询 |
| `session` | 当前检测 session |
| `result` | 算法结果，跌倒为 1 |
| `network_quality` | 发生跌倒时的网络质量 |
| `occurred_at` | 跌倒发生时间 |
| `status` | pending / confirmed / ignored |
| `notified` | 是否已通知小程序或家属 |
| `notified_at` | 通知时间 |
| `handled_at` | 用户确认处理时间 |
| `remark` | 备注 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

## 5.3.4 状态说明

```text
pending    待处理
confirmed  用户确认安全或已处理
ignored    用户标记为误报或忽略
```

---

## 6. SQLAlchemy 模型示例

### 6.1 User 模型

```python
class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    wx_openid = db.Column(db.String(64), unique=True, nullable=False, index=True)
    wx_unionid = db.Column(db.String(64), nullable=True, index=True)
    wx_session_key_enc = db.Column(db.Text, nullable=True)

    nickname = db.Column(db.String(64), nullable=True)
    avatar_url = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(32), nullable=True)

    role = db.Column(db.String(20), default='user', nullable=False)
    status = db.Column(db.String(20), default='active', nullable=False)

    last_login_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    devices = db.relationship('Device', backref='owner', lazy=True)
```

### 6.2 Device 模型

```python
class Device(db.Model):
    __tablename__ = 'devices'

    id = db.Column(db.Integer, primary_key=True)
    device_name = db.Column(db.String(32), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(64), nullable=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    location = db.Column(db.String(128), nullable=True)
    remark = db.Column(db.Text, nullable=True)

    enabled = db.Column(db.Boolean, default=True, nullable=False)
    state = db.Column(db.String(20), default='offline', nullable=False)

    runtime_state = db.Column(db.String(20), default='idle', nullable=False)
    detection_state = db.Column(db.String(20), default='idle', nullable=False)
    current_session = db.Column(db.String(64), nullable=True)

    network_quality = db.Column(db.String(20), default='unknown', nullable=False)

    fault_code = db.Column(db.String(64), nullable=True)
    fault_message = db.Column(db.String(255), nullable=True)

    last_seen_at = db.Column(db.DateTime, nullable=True)
    last_online_at = db.Column(db.DateTime, nullable=True)
    last_status_at = db.Column(db.DateTime, nullable=True)
    last_csi_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
```

### 6.3 FallEvent 模型

```python
class FallEvent(db.Model):
    __tablename__ = 'fall_events'

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    device_id = db.Column(db.Integer, db.ForeignKey('devices.id'), nullable=False, index=True)
    device_name = db.Column(db.String(32), nullable=False, index=True)

    session = db.Column(db.String(64), nullable=True, index=True)
    result = db.Column(db.Integer, default=1, nullable=False)

    network_quality = db.Column(db.String(20), nullable=True)
    occurred_at = db.Column(db.DateTime, nullable=False)

    status = db.Column(db.String(20), default='pending', nullable=False)
    notified = db.Column(db.Boolean, default=False, nullable=False)
    notified_at = db.Column(db.DateTime, nullable=True)
    handled_at = db.Column(db.DateTime, nullable=True)

    remark = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
```

---

## 7. 微信登录流程

### 7.1 前端请求

小程序调用：

```javascript
wx.login()
```

获得：

```text
code
```

然后请求后端：

```http
POST /api/v1/auth/wechat-login
```

请求体：

```json
{
  "code": "微信临时登录凭证"
}
```

### 7.2 后端处理流程

```text
1. 接收 code。
2. 校验 code 是否为空。
3. 后端携带 appid、secret、code 调用微信 code2Session。
4. 微信返回 openid、session_key，满足条件时返回 unionid。
5. 根据 openid 查询 users 表。
6. 如果用户不存在，创建新用户。
7. 如果用户已存在，更新 session_key 和 last_login_at。
8. 生成后端 access_token。
9. 返回 access_token 和用户基础信息。
```

### 7.3 后端返回

```json
{
  "access_token": "backend-token",
  "expires_in": 7200,
  "user": {
    "id": 12,
    "nickname": null,
    "avatar_url": null,
    "is_new_user": true
  }
}
```

### 7.4 注意事项

```text
session_key 只用于后端。
不要把 session_key 返回给小程序。
不要让前端传 openid 控制设备。
后续所有请求都根据 access_token 识别用户。
```

---

## 8. Token 认证设计

后端返回自己的 access_token，小程序后续请求带：

```http
Authorization: Bearer <token>
```

后端认证流程：

```text
1. 读取 Authorization 请求头。
2. 解析 Bearer token。
3. 校验 token 签名和过期时间。
4. 从 token 中解析 user_id。
5. 查询 users 表。
6. 判断用户 status 是否为 active。
7. 将 current_user 注入到请求上下文。
```

---

## 9. API 设计

---

# 9.1 微信登录

```http
POST /api/v1/auth/wechat-login
```

请求：

```json
{
  "code": "wx.login 返回的 code"
}
```

响应：

```json
{
  "access_token": "backend-token",
  "expires_in": 7200,
  "user": {
    "id": 12,
    "nickname": null,
    "avatar_url": null,
    "is_new_user": true
  }
}
```

---

# 9.2 更新用户资料

```http
PATCH /api/v1/me/profile
Authorization: Bearer <token>
```

请求：

```json
{
  "nickname": "微信用户",
  "avatar_url": "https://..."
}
```

后端逻辑：

```text
1. 根据 token 获取 current_user。
2. 更新 users.nickname。
3. 更新 users.avatar_url。
4. 更新 updated_at。
5. 返回最新用户信息。
```

---

# 9.3 获取当前用户

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
  "last_login_at": "2026-07-03T14:30:00+08:00"
}
```

---

# 9.4 获取当前用户绑定设备

```http
GET /api/v1/devices
Authorization: Bearer <token>
```

后端逻辑：

```text
1. 根据 token 获取 current_user。
2. 查询 devices.owner_user_id = current_user.id 的设备。
3. 返回每台设备的最近状态快照。
4. 确保这些设备已经在 MQTT 中完成订阅。
```

响应：

```json
{
  "items": [
    {
      "device_name": "csi-gw-001",
      "display_name": "客厅设备",
      "location": "客厅",
      "state": "online",
      "detection_state": "idle",
      "network_quality": "unknown",
      "last_seen_at": "2026-07-03T14:30:00+08:00",
      "fault_message": null
    }
  ]
}
```

---

# 9.5 获取设备详情

```http
GET /api/v1/devices/{device_name}
Authorization: Bearer <token>
```

后端权限判断：

```text
1. 查询 device_name 对应设备。
2. 判断 device.owner_user_id 是否等于 current_user.id。
3. 如果不属于当前用户，返回 DEVICE_NOT_OWNED。
4. 返回设备详情。
```

响应：

```json
{
  "device_name": "csi-gw-001",
  "display_name": "客厅设备",
  "location": "客厅",
  "state": "online",
  "enabled": true,
  "runtime": {
    "state": "idle",
    "last_status_at": "2026-07-03T14:30:00+08:00"
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

---

# 9.6 启动 / 停止设备

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

## 9.6.1 start 后端判断流程

```text
1. 根据 token 获取 current_user。
2. 查询 device_name 对应设备。
3. 判断设备是否属于当前用户。
4. 判断用户 status 是否 active。
5. 判断设备 enabled 是否为 true。
6. 判断设备 state 是否 online。
7. 判断设备是否存在 fault。
8. 判断 /up/status 是否未超时。
9. 判断当前 detection_state 是否 idle。
10. 生成 session。
11. 将设备 detection_state 更新为 starting。
12. 发布 /down/control start 到 MQTT。
13. 返回 accepted=true。
```

响应：

```json
{
  "accepted": true,
  "device_name": "csi-gw-001",
  "action": "start",
  "control_state": "published",
  "session": "sess-20260703-000001",
  "message": "启动命令已发送"
}
```

## 9.6.2 stop 后端判断流程

```text
1. 根据 token 获取 current_user。
2. 查询 device_name 对应设备。
3. 判断设备是否属于当前用户。
4. 判断设备当前 detection_state 是否 running / starting。
5. 读取 current_session。
6. 将设备 detection_state 更新为 stopping。
7. 发布 /down/control stop 到 MQTT。
8. 停止当前 session 的 CSI 质量分析和算法预测。
9. 收到硬件状态后，将 detection_state 更新为 idle。
```

响应：

```json
{
  "accepted": true,
  "device_name": "csi-gw-001",
  "action": "stop",
  "control_state": "published",
  "session": "sess-20260703-000001",
  "message": "停止命令已发送"
}
```

---

# 9.7 查询跌倒记录

```http
GET /api/v1/fall-events?limit=20
Authorization: Bearer <token>
```

后端逻辑：

```text
1. 根据 current_user.id 查询 fall_events.user_id。
2. 按 occurred_at 倒序排列。
3. 返回最近记录。
```

响应：

```json
{
  "items": [
    {
      "id": 101,
      "device_name": "csi-gw-001",
      "display_name": "客厅设备",
      "location": "客厅",
      "occurred_at": "2026-07-03T14:30:06+08:00",
      "network_quality": "good",
      "status": "pending"
    }
  ]
}
```

---

# 9.8 更新跌倒记录状态

```http
PATCH /api/v1/fall-events/{id}
Authorization: Bearer <token>
```

请求：

```json
{
  "status": "confirmed"
}
```

后端逻辑：

```text
1. 根据 id 查询 fall_event。
2. 判断 fall_event.user_id 是否等于 current_user.id。
3. 更新 status。
4. 如果 status 为 confirmed 或 ignored，写入 handled_at。
5. 返回 success。
```

---

## 10. MQTT Topic 设计

为了和硬件通信，后端需要根据 `device_name` 生成 Topic。

本文档使用逻辑 Topic 表达：

| 逻辑 Topic | 方向 | 作用 |
|---|---|---|
| `{device_name}/up/online` | ESP → Python | 设备在线心跳 |
| `{device_name}/up/status` | ESP → Python | 设备运行状态 |
| `{device_name}/up/fault` | ESP → Python | 设备故障 |
| `{device_name}/up/csi` | ESP → Python | CSI 数据 |
| `{device_name}/down/control` | Python → ESP | 控制 start / stop |

如果你的已有项目已经定义了更完整的 Topic 生成规则，代码中应使用已有规则，本文档中的写法用于说明业务映射。

---

## 11. 用户登录后订阅设备 Topic

用户登录或获取设备列表时，后端需要确保该用户绑定的设备已经被订阅。

流程：

```text
1. 用户登录成功。
2. 后端得到 current_user.id。
3. 查询 devices.owner_user_id = current_user.id。
4. 对每台设备生成 Topic。
5. 订阅 /up/online。
6. 订阅 /up/status。
7. 订阅 /up/fault。
8. 如果设备开始检测，再处理 /up/csi。
9. 订阅要做去重，避免同一设备重复订阅。
```

示例伪代码：

```python
subscribed_devices = set()

def ensure_device_subscribed(device):
    if device.device_name in subscribed_devices:
        return

    mqtt_client.subscribe(f"{device.device_name}/up/online")
    mqtt_client.subscribe(f"{device.device_name}/up/status")
    mqtt_client.subscribe(f"{device.device_name}/up/fault")
    mqtt_client.subscribe(f"{device.device_name}/up/csi")

    subscribed_devices.add(device.device_name)
```

---

## 12. MQTT 消息处理逻辑

## 12.1 处理 /up/online

硬件发布：

```json
{
  "device_name": "csi-gw-001",
  "online": true,
  "ts": 1783060200
}
```

后端处理：

```text
1. 根据 device_name 查询设备。
2. 将 state 更新为 online。
3. 清空 fault_code / fault_message。
4. 更新 last_online_at。
5. 更新 last_seen_at。
6. 推送 device.state.changed 给小程序。
```

推送事件：

```json
{
  "event": "device.state.changed",
  "device_name": "csi-gw-001",
  "data": {
    "state": "online"
  }
}
```

---

## 12.2 处理 /up/status

硬件发布：

```json
{
  "device_name": "csi-gw-001",
  "runtime_state": "idle",
  "ts": 1783060200
}
```

后端处理：

```text
1. 更新 runtime_state。
2. 更新 last_status_at。
3. 更新 last_seen_at。
4. 如果没有 fault，保持或恢复 online。
5. 如果 runtime_state = running，则 detection_state 可更新为 running。
6. 推送 device.runtime.changed。
```

---

## 12.3 处理 /up/fault

硬件发布：

```json
{
  "device_name": "csi-gw-001",
  "fault_code": "SENSOR_ERROR",
  "fault_message": "传感器异常",
  "ts": 1783060200
}
```

后端处理：

```text
1. 根据 device_name 查询设备。
2. 将 state 更新为 error。
3. 保存 fault_code。
4. 保存 fault_message。
5. 更新 last_seen_at。
6. 推送 device.fault 给小程序。
```

---

## 12.4 处理 /up/csi

硬件每 2 秒上传一次 CSI。

消息建议包含：

```json
{
  "device_name": "csi-gw-001",
  "session": "sess-20260703-000001",
  "seq": 1024,
  "timestamp": 1783060200,
  "csi": "..."
}
```

后端处理：

```text
1. 根据 device_name 查询设备。
2. 判断设备 detection_state 是否 running。
3. 判断 session 是否等于 devices.current_session。
4. 解析 seq 和 timestamp。
5. 根据 seq 是否连续判断是否丢帧。
6. 根据 timestamp 间隔判断是否超时。
7. 得到网络质量 good / fair / poor。
8. 更新 devices.network_quality。
9. 更新 devices.last_csi_at。
10. 推送 detection.network-quality。
11. 将有效 CSI 送入跌倒检测算法。
12. 如果算法返回 1，写入 fall_events 并推送 detection.fall-result。
```

---

## 13. 设备离线判断

后端需要定时扫描设备是否离线。

推荐规则：

```text
如果当前时间 - last_seen_at > 15 秒，则认为设备 offline。
如果设备 state 不是 error，则更新 state = offline。
如果设备正在 running，则应同时将 detection_state 标记为 idle 或 interrupted。
```

伪代码：

```python
def check_offline_devices():
    now = datetime.utcnow()
    devices = Device.query.filter(Device.state != 'offline').all()

    for device in devices:
        if device.last_seen_at is None:
            continue

        delta = (now - device.last_seen_at).total_seconds()
        if delta > 15 and device.state != 'error':
            device.state = 'offline'
            device.detection_state = 'idle'
            device.network_quality = 'unknown'
            db.session.commit()
            websocket_service.push_to_user(
                device.owner_user_id,
                'device.state.changed',
                device.device_name,
                {'state': 'offline'}
            )
```

---

## 14. start 控制逻辑详细说明

### 14.1 小程序请求

```http
POST /api/v1/devices/csi-gw-001/control
Authorization: Bearer <token>
```

```json
{
  "action": "start"
}
```

### 14.2 后端完整流程

```text
1. 获取 current_user。
2. 查询设备。
3. 判断设备是否存在。
4. 判断设备是否属于 current_user。
5. 判断设备是否 enabled。
6. 判断设备 state 是否 online。
7. 判断设备是否 fault。
8. 判断 last_status_at 是否超时。
9. 判断 detection_state 是否 idle。
10. 生成 session，例如 sess-20260703-000001。
11. 更新 devices.current_session。
12. 更新 devices.detection_state = starting。
13. 更新 devices.network_quality = unknown。
14. 构造 MQTT payload。
15. 发布到 `{device_name}/down/control`。
16. 返回 accepted=true。
```

### 14.3 MQTT 控制消息

```json
{
  "action": "start",
  "session": "sess-20260703-000001",
  "ts": 1783060200
}
```

### 14.4 启动成功判断

如果硬件后续通过 `/up/status` 上报：

```json
{
  "runtime_state": "running"
}
```

后端更新：

```text
runtime_state = running
detection_state = running
```

然后推送：

```json
{
  "event": "device.runtime.changed",
  "device_name": "csi-gw-001",
  "data": {
    "runtime_state": "running",
    "detection_state": "running"
  }
}
```

---

## 15. stop 控制逻辑详细说明

### 15.1 小程序请求

```http
POST /api/v1/devices/csi-gw-001/control
Authorization: Bearer <token>
```

```json
{
  "action": "stop"
}
```

### 15.2 后端完整流程

```text
1. 获取 current_user。
2. 查询设备。
3. 判断设备是否属于 current_user。
4. 判断设备当前是否 starting / running。
5. 读取 current_session。
6. 更新 detection_state = stopping。
7. 发布 stop 到 `{device_name}/down/control`。
8. 停止该 session 的 CSI 缓存和算法输入。
9. 将 network_quality 置为 unknown。
10. 如果硬件状态返回 stopped / idle，则 detection_state = idle。
11. 清空 current_session。
```

### 15.3 MQTT 控制消息

```json
{
  "action": "stop",
  "session": "sess-20260703-000001",
  "ts": 1783060300
}
```

---

## 16. CSI 网络质量判断

### 16.1 输入信息

硬件每 2 秒上传一次 CSI，后端至少需要以下信息：

```text
session
seq
timestamp
csi 数据
```

如果硬件能提供更多字段，也可以加入：

```text
batch
frames
seq0 / seq1
ts0 / ts1
crc
```

### 16.2 判断依据

```text
1. 时间间隔是否接近 2 秒。
2. seq 是否连续。
3. 是否出现明显丢帧。
4. 是否超过 8 秒没有收到 CSI。
```

### 16.3 网络质量输出

后端只给小程序输出简化结果：

```text
good       良好
fair       一般
poor       较差
unknown    暂无数据
```

### 16.4 示例规则

```text
最近 10 个 CSI 包没有丢帧，时间间隔稳定：good
最近 10 个 CSI 包少量丢帧：fair
最近 10 个 CSI 包明显丢帧或时间间隔不稳定：poor
还没有收到 CSI 或检测已停止：unknown
```

### 16.5 推送给小程序

```json
{
  "event": "detection.network-quality",
  "device_name": "csi-gw-001",
  "data": {
    "session": "sess-20260703-000001",
    "network_quality": "good",
    "last_csi_at": "2026-07-03T14:30:04+08:00"
  }
}
```

---

## 17. 跌倒检测算法接入

### 17.1 算法输入

算法细节后续在 Python 端补充。当前后端只需要预留接口。

建议接口形式：

```python
def predict_fall(device_name: str, session: str, csi_window: list) -> int:
    """
    返回：
    0 = 未检测到跌倒
    1 = 检测到跌倒
    """
    return 0
```

### 17.2 算法调用流程

```text
1. 收到 /up/csi。
2. 判断 session 有效。
3. 判断网络质量不是 unknown。
4. 将 CSI 加入当前 session 缓冲区。
5. 当缓冲区满足算法输入长度时，调用 predict_fall。
6. 如果返回 0，不写 fall_events。
7. 如果返回 1，写 fall_events 并推送小程序。
```

### 17.3 跌倒发生处理

当 result = 1：

```text
1. 查询设备。
2. 查询设备所属用户。
3. 创建 fall_events 记录。
4. 设置 status = pending。
5. 设置 notified = true。
6. 推送 detection.fall-result 给该用户的小程序。
7. 小程序跳转跌倒告警页。
```

数据库写入示例：

```python
fall_event = FallEvent(
    user_id=device.owner_user_id,
    device_id=device.id,
    device_name=device.device_name,
    session=device.current_session,
    result=1,
    network_quality=device.network_quality,
    occurred_at=datetime.utcnow(),
    status='pending',
    notified=True,
    notified_at=datetime.utcnow()
)

db.session.add(fall_event)
db.session.commit()
```

推送事件：

```json
{
  "event": "detection.fall-result",
  "device_name": "csi-gw-001",
  "data": {
    "fall_event_id": 101,
    "session": "sess-20260703-000001",
    "result": 1,
    "fall_detected": true,
    "network_quality": "good",
    "occurred_at": "2026-07-03T14:30:06+08:00"
  }
}
```

---

## 18. WebSocket 推送设计

虽然业务上可以说“后端向小程序 post 在线信息”，但技术实现上，后端不能主动对一个正在运行的小程序页面发普通 HTTP POST。推荐用 WebSocket 实时推送。

### 18.1 连接地址

```text
wss://你的域名.com/ws/v1/events?token=<access_token>
```

### 18.2 推送对象

```text
后端根据 token 识别 user_id。
每个 user_id 可以维护一个或多个 WebSocket 连接。
设备状态变化后，根据 device.owner_user_id 找到对应用户连接并推送。
```

### 18.3 事件列表

| 事件 | 触发条件 |
|---|---|
| `device.state.changed` | 收到 online 或离线扫描触发 |
| `device.runtime.changed` | 收到 status |
| `device.fault` | 收到 fault |
| `detection.network-quality` | 收到 CSI 后质量变化 |
| `detection.fall-result` | 算法返回 1 |

---

## 19. 统一错误码

```json
{
  "error": "DEVICE_OFFLINE",
  "message": "设备离线，无法启动检测"
}
```

| 错误码 | 说明 |
|---|---|
| `AUTH_REQUIRED` | 未登录或 token 无效 |
| `USER_DISABLED` | 用户被禁用 |
| `DEVICE_NOT_FOUND` | 设备不存在 |
| `DEVICE_NOT_OWNED` | 设备不属于当前用户 |
| `DEVICE_DISABLED` | 设备被禁用 |
| `DEVICE_OFFLINE` | 设备离线 |
| `DEVICE_ERROR` | 设备出错 |
| `STATUS_TIMEOUT` | 状态超时 |
| `CONTROL_BUSY` | 设备正在控制中 |
| `INVALID_ACTION` | action 不合法 |
| `CSI_TIMEOUT` | CSI 数据中断 |
| `FALL_EVENT_NOT_FOUND` | 跌倒记录不存在 |
```

---

## 20. 后端开发顺序

### 第一阶段：数据库和登录

```text
1. 创建 Flask 项目。
2. 配置 SQLAlchemy。
3. 创建 users 表。
4. 创建 devices 表。
5. 创建 fall_events 表。
6. 完成 /api/v1/auth/wechat-login。
7. 完成 token 生成和认证装饰器。
8. 完成 /api/v1/me。
```

### 第二阶段：设备查询

```text
1. 完成 GET /api/v1/devices。
2. 完成 GET /api/v1/devices/{device_name}。
3. 完成用户和设备权限判断。
4. 准备几条测试设备数据。
```

### 第三阶段：MQTT 接入

```text
1. 连接 Mosquitto。
2. 根据设备名订阅 /up/online。
3. 处理 online 消息并更新 devices.state。
4. 订阅 /up/status。
5. 订阅 /up/fault。
6. 实现离线超时扫描。
```

### 第四阶段：设备控制

```text
1. 完成 POST /api/v1/devices/{device_name}/control。
2. 支持 start。
3. 支持 stop。
4. 发布 /down/control。
5. 根据 /up/status 更新 running / idle。
```

### 第五阶段：CSI 和网络质量

```text
1. 接收 /up/csi。
2. 按 session 过滤数据。
3. 解析 seq 和 timestamp。
4. 判断 good / fair / poor / unknown。
5. 更新 devices.network_quality。
```

### 第六阶段：算法和跌倒事件

```text
1. 预留 predict_fall 接口。
2. 将 CSI 窗口送入算法。
3. 算法返回 1 时写入 fall_events。
4. 完成 GET /api/v1/fall-events。
5. 完成 PATCH /api/v1/fall-events/{id}。
```

### 第七阶段：WebSocket

```text
1. 建立 WebSocket 服务。
2. 登录用户连接后绑定 user_id。
3. 推送 device.state.changed。
4. 推送 device.fault。
5. 推送 detection.network-quality。
6. 推送 detection.fall-result。
```

---

## 21. 后端验收标准

```text
1. 小程序传 code 后，后端可以换取 openid。
2. 后端可以创建或更新 users 表。
3. 后端不会把 session_key 返回给前端。
4. 后端可以生成 access_token。
5. 小程序带 token 可以访问当前用户信息。
6. 小程序只能看到 owner_user_id 属于自己的设备。
7. 后端可以根据 /up/online 将设备更新为 online。
8. 超时后设备可以变成 offline。
9. 收到 /up/fault 后设备可以变成 error。
10. 在线设备可以接收 start 请求。
11. 后端可以发布 /down/control start。
12. 硬件上传 /up/csi 后，后端可以判断网络质量。
13. 算法返回 1 后，后端可以写入 fall_events。
14. 算法返回 1 后，小程序可以收到跌倒告警。
15. stop 后可以停止 CSI 分析和算法预测。
16. 设备状态、网络质量、跌倒事件都可以通过 API 或 WebSocket 被小程序看到。
```

---

## 22. 本版本最终数据边界

本版本数据库只保存：

```text
users：用户身份、微信身份、用户资料

devices：设备归属、设备状态、运行状态、当前 session、网络质量

fall_events：跌倒发生记录、处理状态、发生时间
```

Python 运行时保存：

```text
MQTT 连接状态
已订阅设备集合
当前 CSI 缓冲区
当前 session 的算法窗口
WebSocket 用户连接
短期控制过程状态
```

不保存：

```text
原始 CSI 全量数据
每一条 MQTT 消息
每一次普通算法返回 0 的结果
前端无关的 MQTT 密码
```

这样可以保证当前项目结构简单、逻辑清楚，也方便后续扩展更多表。
