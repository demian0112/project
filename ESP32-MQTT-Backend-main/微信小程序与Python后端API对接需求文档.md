# 微信小程序与 Python 后端 API 对接需求文档

> 版本：v1.0  
> 日期：2026-07-03  
> 文档用途：确定微信小程序与 Python 后端的业务边界、数据库字段和 API 协议

## 文档约定

本系统的通信关系为：

```text
微信小程序
   ↕ HTTPS / WebSocket
Python 后端
   ↕ SQLite        ↕ MQTT
数据库           Mosquitto ↔ ESP
```

- 小程序不直接连接 MQTT。
- 小程序使用 HTTPS 请求登录、设备和控制接口。
- Python 使用 WebSocket 向小程序推送设备状态、错误和跌倒结果。
- 如果 WebSocket 暂时不可用，小程序可以使用 GET 接口轮询降级。

---

# 第一部分：当前项目已实现功能

当前项目已经完成以下基础能力：

1. 使用 Flask 建立 Python 后端。
2. 使用 SQLite 和 SQLAlchemy 保存管理员、用户和设备。
3. 已实现用户与设备的一对多绑定关系。
4. 已实现管理员登录以及用户、设备的增删改查。
5. 已完成 Mosquitto 连接参数配置。
6. 已实现按设备生成 MQTT Client ID 和七类 Topic。
7. 已实现六个上行 Topic 的精确订阅。
8. 已实现 start/stop 控制命令发布。
9. 已实现 `cmd` 和 `session` 生成。
10. 已具备 MQTT 单元测试和可选真实 Broker 测试。

当前尚未完成：

- 微信小程序登录。
- 面向小程序的设备查询 API。
- 设备状态聚合。
- ACK 匹配和控制结果推送。
- CSI 丢帧与网络质量判断。
- 跌倒检测结果推送。
- WebSocket 实时事件。

---

# 第二部分：业务逻辑与数据库需求

## 2.1 用户首次进入小程序

业务流程：

1. 小程序获取微信临时登录凭证。
2. 小程序将临时凭证发送给 Python。
3. Python 获取该用户在当前小程序中的唯一身份。
4. Python 在 user 表中查询用户。
5. 用户不存在时创建记录，存在时更新最近登录时间。
6. Python 返回自己的登录 token。
7. 小程序使用该 token 请求设备和控制接口。

需要注意：

- 微信身份识别与昵称、头像资料是两件事。
- 首次登录可以先完成身份创建。
- 昵称和头像只有在小程序获得相应信息后才能更新。
- 后端不能假设首次进入一定能自动取得完整头像和昵称。
- 微信会话密钥等敏感信息不能返回小程序，也不应作为普通用户字段长期明文保存。

## 2.2 user 表优化

管理员表继续只服务内部管理网页。微信小程序业务主要使用 user 和 device。

建议 user 表字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Integer | 用户主键 |
| `wx_openid` | String(64) | 当前小程序内用户唯一标识，唯一索引 |
| `wx_unionid` | String(64), Nullable | 跨应用用户标识，预留 |
| `nickname` | String(64), Nullable | 用户昵称 |
| `avatar_url` | String(512), Nullable | 用户头像 |
| `status` | String(16) | `active/disabled` |
| `last_login_at` | DateTime, Nullable | 最近登录时间 |
| `created_at` | DateTime | 创建时间 |
| `updated_at` | DateTime | 更新时间 |

现有 username/password 字段可以在迁移期保留，用于本地测试；正式小程序用户
以 `wx_openid` 识别，不要求用户在小程序中再注册一套密码。

## 2.3 device 表优化

建议 device 表字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Integer | 设备主键 |
| `device_name` | String(32) | 硬件唯一名称，生成 MQTT Topic |
| `display_name` | String(64) | 小程序显示名称 |
| `owner_id` | Integer | 绑定用户，外键指向 user.id |
| `status` | String(16) | 管理状态 `enabled/disabled` |
| `location` | String(64), Nullable | 安装位置 |
| `remark` | String(255), Nullable | 管理备注 |
| `last_seen_at` | DateTime, Nullable | 最近有效通信时间 |
| `created_at` | DateTime | 创建时间 |
| `updated_at` | DateTime | 更新时间 |

关系：

```text
user.id  1 ───── N  device.owner_id
```

- 一个用户可以绑定多台设备。
- 一台设备当前只属于一个用户。
- 小程序只能查询当前用户绑定的设备。
- `device_name` 必须符合 `^[A-Za-z0-9_-]{1,32}$`。
- 完整 Topic 由 Python 根据 `device_name` 生成，不需要在数据库保存七个 Topic。

## 2.4 用户登录后的设备状态

用户登录后：

1. Python 根据 user.id 查询 device.owner_id。
2. Python 只加载该用户绑定的设备。
3. Python 确保这些设备已经建立 MQTT Client 并订阅上行 Topic。
4. 同一设备已经订阅时直接复用，不能因用户重复登录创建重复连接。
5. Python 先根据 `up/online` 判断在线状态。
6. Python 结合 MQTT 运行时状态生成设备快照。
7. 小程序显示设备状态。

前端主状态只保留三种：

```text
online     在线
offline    离线
error      出错
```

显示优先级：

```text
error > online > offline
```

解释：

- 收到有效 `up/online=online` 时为在线。
- 收到 offline 或无法确认在线时为离线。
- 在线设备收到 `up/fault` 时显示出错。
- 出错设备底层仍可能保持 MQTT 在线，但界面主状态优先显示 error。

## 2.5 启动检测业务

只有满足以下条件才允许启动：

- 用户状态为 active。
- 设备属于当前用户。
- 设备管理状态为 enabled。
- MQTT 在线。
- 最近 `up/status` 未超时。
- 当前没有正在运行的检测。

启动流程：

```text
用户点击开始
  → 小程序请求 Python
  → Python 校验用户、设备和实时状态
  → Python 生成 cmd 和 session
  → Python 发布 down/control(start)
  → ESP 回复 up/ack
  → ESP 开始每 2 秒发布 up/csi
  → Python 开始网络质量分析和跌倒检测
```

HTTP 请求成功只代表 Python 接受了请求。设备真正启动成功应以匹配 `cmd` 的
`up/ack` 为准。

## 2.6 CSI、网络质量与跌倒检测

检测运行期间，Python 接收 `up/csi`。

Python 根据以下字段判断数据质量：

- `session`：是否属于当前检测。
- `batch`：MQTT 批次是否连续。
- `frames`：当前数据包帧数。
- `seq0/seq1`：帧序号是否连续。
- `ts0/ts1`：采样时间范围。
- `crc`：数据是否损坏。

Python 对小程序输出简化网络质量：

```text
good       良好
fair       一般
poor       较差
unknown    暂无数据
```

原始 CSI 数据不发送到小程序。

有效 CSI 将进入跌倒检测算法。算法第一阶段输出：

```text
0 = 未检测到跌倒
1 = 检测到跌倒
```

检测到 1 时，Python 立即向小程序推送跌倒事件。

算法接口细节、模型版本和置信度后续由 Python 算法模块补充，本期 API 先保证
能够返回 `result=0/1`。

## 2.7 停止检测业务

停止流程：

```text
用户点击停止
  → 小程序请求 Python
  → Python读取当前 session
  → Python 发布 down/control(stop)
  → ESP 回复 up/ack
  → ESP 停止 up/csi
  → Python 停止网络质量分析和跌倒检测
```

stop 必须使用当前 session。最终停止结果仍以 ACK 为准。

## 2.8 七类 Topic 在业务中的作用

MQTT 细节由 Python 内部处理，小程序只关心对应业务结果。

| Topic | 后端业务作用 | 小程序表现 |
|---|---|---|
| `up/online` | 判断设备在线 | 在线/离线 |
| `up/wifi` | 获取网络连接和 RSSI | 网络信息 |
| `up/status` | 获取设备运行状态 | 空闲、启动、检测中、停止中 |
| `down/control` | 发布开始/停止 | 用户控制请求 |
| `up/ack` | 确认控制是否执行 | 启动/停止成功或失败 |
| `up/csi` | 网络质量分析和算法输入 | 网络质量、检测运行状态 |
| `up/fault` | 设备故障 | 出错状态和错误内容 |

---

# 第三部分：微信小程序与 Python API 需求

## 3.1 API 交互原则

1. 所有业务 API 使用 HTTPS。
2. 登录后请求携带后端 token。
3. 小程序不保存 MQTT 配置。
4. 小程序不直接订阅 Topic。
5. 小程序通过 HTTP 发起操作。
6. Python 通过 WebSocket 推送状态和结果。
7. WebSocket 断线时，小程序通过设备详情接口恢复状态。

## 3.2 API 清单

| 方法 | 路径 | 功能 |
|---|---|---|
| POST | `/api/v1/auth/wechat-login` | 微信登录 |
| PATCH | `/api/v1/me/profile` | 更新昵称和头像 |
| GET | `/api/v1/me` | 当前用户 |
| GET | `/api/v1/devices` | 用户绑定设备列表 |
| GET | `/api/v1/devices/{device_name}` | 设备详情和当前状态 |
| POST | `/api/v1/devices/{device_name}/control` | 启动或停止 |
| GET | `/api/v1/detections/{session}` | 查询当前检测状态 |
| WSS | `/ws/v1/events` | 实时状态、错误、质量和跌倒事件 |

## 3.3 微信登录 API

### 请求

```http
POST /api/v1/auth/wechat-login
Content-Type: application/json
```

```json
{
  "code": "微信临时登录凭证"
}
```

### 响应

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

若昵称和头像暂不可用，不影响登录和设备查询。

## 3.4 更新用户资料 API

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

前端只有在获得相关用户信息后才调用该接口。

## 3.5 设备列表 API

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
      "display_name": "客厅设备",
      "location": "客厅",
      "state": "online",
      "detection_state": "idle",
      "last_seen_at": "2026-07-03T14:30:00+08:00"
    }
  ]
}
```

`state` 只使用：

```text
online
offline
error
```

## 3.6 设备详情 API

```http
GET /api/v1/devices/{device_name}
Authorization: Bearer <token>
```

响应：

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

后端必须校验该设备的 owner_id 是当前登录用户。

## 3.7 控制 API

### 请求

```http
POST /api/v1/devices/{device_name}/control
Authorization: Bearer <token>
Idempotency-Key: <uuid>
```

开始：

```json
{
  "action": "start"
}
```

停止：

```json
{
  "action": "stop"
}
```

### 响应

```json
{
  "accepted": true,
  "cmd": "cmd-20260703-000001",
  "session": "sess-20260703-000001",
  "control_state": "waiting_ack"
}
```

前端收到 accepted 后显示：

```text
正在启动，等待设备确认
或
正在停止，等待设备确认
```

不能立即显示“启动成功”或“停止成功”。

## 3.8 检测状态查询 API

```http
GET /api/v1/detections/{session}
Authorization: Bearer <token>
```

响应：

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

该接口用于页面刷新或 WebSocket 重连后的状态恢复。

## 3.9 WebSocket 事件

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

### 设备状态事件

```json
{
  "event": "device.state.changed",
  "device_name": "csi-gw-001",
  "data": {
    "state": "online"
  }
}
```

### 设备错误事件

```json
{
  "event": "device.fault",
  "device_name": "csi-gw-001",
  "data": {
    "state": "error",
    "code": "UART_TIMEOUT",
    "message": "设备内部通信超时"
  }
}
```

### 控制结果事件

```json
{
  "event": "control.result",
  "device_name": "csi-gw-001",
  "data": {
    "cmd": "cmd-20260703-000001",
    "action": "start",
    "session": "sess-20260703-000001",
    "success": true
  }
}
```

### 网络质量事件

```json
{
  "event": "detection.network-quality",
  "device_name": "csi-gw-001",
  "data": {
    "session": "sess-20260703-000001",
    "quality": "good",
    "lost_frames": 0,
    "last_csi_at": "2026-07-03T14:30:04+08:00"
  }
}
```

### 跌倒检测事件

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

## 3.10 关键时间规则

| 规则 | 时间 |
|---|---:|
| 设备 status 正常上报 | 每 5 秒 |
| status 超时 | 15 秒 |
| CSI 正常上报 | 每 2 秒 |
| CSI 数据超时 | 8 秒 |
| 控制 ACK 首次等待 | 3 秒 |
| ACK 重试 | 使用相同 cmd/session 重试一次 |
| 相同故障限流 | 5 秒 |

## 3.11 小程序前端要求

1. 登录成功后保存后端 token。
2. 页面打开时先获取设备列表。
3. 设备主状态只显示在线、离线、出错。
4. 只有在线且可用的设备显示开始按钮。
5. 控制请求处理中禁止重复点击。
6. 使用 Idempotency-Key 防止重复命令。
7. 以 `control.result` 判断启动或停止是否成功。
8. 检测中展示网络质量。
9. 收到 `fall-result=1` 时立即进行明显提示。
10. 收到 fault 时将设备主状态更新为出错。
11. WebSocket 断开后重新连接，并调用 GET 接口恢复状态。
12. 不展示或保存原始 CSI 数据。

## 3.12 主要错误码

| 错误码 | 含义 |
|---|---|
| `AUTH_REQUIRED` | 未登录或 token 失效 |
| `USER_DISABLED` | 用户已禁用 |
| `DEVICE_NOT_FOUND` | 设备不存在 |
| `DEVICE_NOT_OWNED` | 设备不属于当前用户 |
| `DEVICE_DISABLED` | 设备已被禁用 |
| `DEVICE_OFFLINE` | 设备离线 |
| `STATUS_TIMEOUT` | 设备状态超时 |
| `CONTROL_BUSY` | 已有控制正在执行 |
| `ACK_REJECTED` | 设备拒绝命令 |
| `ACK_TIMEOUT` | 设备未回复 |
| `CSI_TIMEOUT` | CSI 数据中断 |

## 3.13 最小验收标准

- 首次进入可以创建或更新 user。
- 登录后只返回该用户绑定的设备。
- 设备能正确显示在线、离线、出错。
- 用户不能访问其他用户设备。
- 在线设备可以开始和停止检测。
- 启动和停止结果以 ACK 为准。
- CSI 运行期间可以显示网络质量。
- 算法返回 1 时小程序能收到跌倒事件。
- 停止后网络质量分析和预测同时停止。
- WebSocket 断线重连后可以恢复当前状态。

---

# 最终结论

本次前后端对接重点不是让小程序理解 MQTT，而是确定以下业务接口：

```text
登录用户
查询绑定设备
展示设备在线/离线/出错
启动检测
停止检测
展示网络质量
接收跌倒结果
```

数据库负责用户身份和设备归属，MQTT 负责设备通信，Python 负责把两者整理为
小程序可以直接使用的 HTTPS 与 WebSocket API。
