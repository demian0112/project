# 小程序 UI 架构与发布配置

> 业务接口以《微信小程序前端与后端API及数据库集成说明_最新版.md》为唯一依据。

## 1. 数据原则

- 小程序冷启动首先执行 `wx.login()`，由 Flask 后端根据 `openid` 查询或新增 `users`。
- 用户、设备和跌倒事件只通过 `/api/v1` 读取数据库，不保留旧 FastAPI、Mock 或本地业务回退。
- WebSocket 只提示变化；首次连接、重连和小程序回到前台后都重新读取 HTTP 权威快照。
- 小程序不保存 `openid`、`session_key`、AppSecret 或 MQTT 密码。

## 2. 前端分层

```text
pages
  ↓
utils/api.ts          Flask /api/v1 契约和 Bearer Token
  ↓
services/auth.ts      冷启动登录与 Token 状态
services/realtime.ts  WebSocket 重连与变化通知
  ↓
config/env.ts         唯一服务地址配置
```

## 3. 联调配置

在 `miniprogram/config/env.ts` 填写：

```text
development.apiBaseUrl = http://<后端IP>:<端口>
development.wsBaseUrl  = ws://<后端IP>:<端口>/ws/v1/events
```

当前地址故意保持为空；未配置时前端显示明确错误，不生成演示数据。

## 4. 后端接口

```text
POST  /api/v1/auth/wechat-login
GET   /api/v1/me
PATCH /api/v1/me/profile
GET   /api/v1/devices
GET   /api/v1/devices/{device_name}
POST  /api/v1/devices/{device_name}/control
GET   /api/v1/fall-events
PATCH /api/v1/fall-events/{id}
WS    /ws/v1/events?token=<access_token>
```

控制接口返回 `202 accepted` 只代表命令已发布。前端保持 `starting/stopping` 并等待 WebSocket 或设备详情接口返回最终状态。

## 5. 正式发布

1. 云端 API 使用备案域名、有效 HTTPS 证书和标准 `443` 端口。
2. WebSocket 使用 `wss://`。
3. 在微信公众平台配置 request 与 socket 合法域名。
4. 将 `ACTIVE_ENV` 切换为 `production` 并填写真实域名。
5. 后端保管微信密钥和 MQTT 凭据，关闭不必要的公网端口。
6. 上传前运行 `npm run typecheck`，完成弱网、Token 过期、WebSocket 重连和控制幂等测试。
