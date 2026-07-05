# 安守 CSI 跌倒检测 Flask 后端

项目连接微信小程序、SQLite、Mosquitto MQTT 与 ESP32 设备。现有管理员网页
继续用于维护用户和设备；小程序通过独立的 `/api/v1` Bearer Token API
读取设备快照、下发启动/停止命令并处理跌倒记录。

## 已实现的业务链路

```text
微信 wx.login code → Flask code2Session → users → access_token
access_token → 当前用户 devices → 设备状态快照
小程序 start/stop → 状态和权限校验 → MQTT down/control
硬件 ack → 确认 starting/running/stopping/idle 控制闭环
MQTT online/status/fault/csi → 更新 devices → WebSocket 实时事件
CSI Base64/v2 二进制解包 → 丢帧率滚动窗口/算法接口 → fall_events
```

小程序不直接连接 MQTT，也不会获得 `openid`、`session_key`、微信
`secret` 或 MQTT 密码。原始 CSI、普通算法结果和 MQTT 消息不写数据库。

## 主要目录

```text
app/
├── api.py                         # 管理员用户、设备、跌倒事件接口
├── miniapp_api.py                 # 微信小程序 /api/v1
├── models.py                      # Admin、User、Device、FallEvent
├── mqtt/                          # Topic、连接、订阅与控制发布
└── services/
    ├── wechat_service.py          # code2Session
    ├── token_service.py           # 后端 access_token
    ├── mqtt_service.py            # 按设备维护 MQTT 客户端
    ├── device_state_service.py    # 状态机、控制、离线扫描
    ├── csi_quality_service.py     # CSI 序号/间隔质量判断
    ├── fall_detect_service.py     # 跌倒算法接入口
    └── websocket_service.py       # 按 user_id 推送事件
```

## 数据库

核心业务表为：

- `users`：微信身份、用户资料和账户状态。
- `devices`：所属用户、在线/运行/检测状态、session、网络质量和故障快照。
- `fall_events`：只记录算法返回 1 的跌倒事件及处理状态。

`admin` 是内部管理网页的独立认证表，不参与小程序登录。当前数据库只保留
`admin` 和上述三张业务表，不再包含旧版 `user`、`device` 表或业务用户密码。

设备名必须匹配 `^[A-Za-z0-9_-]{1,32}$`，并直接决定 MQTT Topic：

```text
csi/v1/devices/{device_name}/...
```

## 安装与初始化

```bash
conda activate csi_backend
pip install -r requirements.txt
cp .env.example .env
flask --app run init-db
flask --app run create-admin
```

`init-db` 可以重复执行，只负责创建当前版本的四张表。

`.env` 至少需要配置：

```env
SECRET_KEY=随机且足够长的服务端密钥
WECHAT_APPID=微信小程序AppID
WECHAT_SECRET=微信小程序AppSecret

MQTT_ENABLED=1
MQTT_HOST=192.168.101.48
MQTT_PORT=1883
MQTT_USERNAME=csi_user
MQTT_PASSWORD=实际Broker密码
```

`session_key` 目前不落明文数据库；现阶段没有微信加密数据解密需求，
`wx_session_key_enc` 保持可空。后续确有需要时应接入正式密钥管理和加密后
再保存，不能直接保存明文。

## 运行

```bash
conda activate csi_backend
flask --app run run --debug
```

管理员页面：<http://127.0.0.1:5000/admin>

管理员控制台按最新业务模型展示运行总览、设备权威快照、微信用户和跌倒事件。
设备表区分 `online/offline/error`、检测状态和网络质量；用户资料不展示
openid、unionid 或 session_key；待处理跌倒事件可以在页面中确认或忽略。

## 小程序 API

```text
POST  /api/v1/auth/wechat-login
GET   /api/v1/me
PATCH /api/v1/me/profile
GET   /api/v1/devices
GET   /api/v1/devices/<device_name>
POST  /api/v1/devices/<device_name>/control
GET   /api/v1/fall-events
PATCH /api/v1/fall-events/<id>
WS    /ws/v1/events?token=<access_token>
```

除登录外均使用：

```http
Authorization: Bearer <access_token>
```

控制接口请求体为 `{"action":"start"}` 或 `{"action":"stop"}`，建议同时发送
`Idempotency-Key`。后端会校验用户归属、设备启用状态、在线状态、故障、
状态新鲜度和当前检测状态；MQTT 发布失败时不会错误地推进数据库状态。
下发给硬件的 payload 严格使用
`{"cmd":"control","action":"start|stop","session":"..."}`；HTTP 返回 202
只代表发布成功，最终运行状态以 `up/ack` 为准。

WebSocket 事件包括：

```text
device.state.changed
device.runtime.changed
device.fault
detection.network-quality
detection.fall-result
```

## 管理员路由

```text
GET|POST          /admin/login
POST              /admin/logout
GET               /admin
GET               /api/users
GET|PUT|DELETE    /api/users/<id>
GET|POST          /api/devices
GET|PUT|DELETE    /api/devices/<id>
GET               /api/fall-events
PATCH             /api/fall-events/<id>
```

管理写接口继续使用管理员会话和 CSRF Token，与小程序 Token 认证互不混用。

## 离线扫描与算法接入

服务进程默认每 2 秒扫描一次：设备超过 15 秒没有有效上报时变为离线；
检测中超过 8 秒没有 CSI 时网络质量变为 `poor`。也可以手动执行：

```bash
flask --app run scan-offline
```

当前跌倒算法入口在
`app/services/fall_detect_service.py::predict_fall()`，安全默认值为 0。
`up/csi` 会先校验 `csib64-v2-1s`、Base64 长度、batch/frame header、
序号和时间戳，再将逐帧 `raw_csi` 送入该入口。接入正式算法时保持
`0/1` 返回约定即可。

## 测试

```bash
pytest -q
pytest -q app/mqtt/tests
ruff check app tests
node --check app/static/js/dashboard.js
```

真实 Broker 往返测试默认跳过，按 `app/mqtt/README.md` 中的说明显式开启。
