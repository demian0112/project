# 快速启动指南

当前工作区由三部分组成：

- `csi_miniprogram--`：微信小程序前端。
- `ESP32-MQTT-Backend-main`：当前正式 Flask 后端。
- `hardware代码交互逻辑说明.md`：ESP32 A/B/C 三板与 MQTT Topic 协议说明。

不要启动 `csi_miniprogram--/backend`，它是历史 FastAPI 演示后端，当前小程序不再使用。

## 1. 后端启动

先确认本机命令可用：

```powershell
python --version
node --version
npm --version
```

如果其中任意命令不存在，先安装 Python 3.11+、Node.js LTS 和微信开发者工具。Windows 上如果 `python --version` 没有输出版本，而是打开 Microsoft Store，说明当前只是系统占位入口，需要安装真正的 Python 后再继续。

```powershell
cd .\ESP32-MQTT-Backend-main
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

打开 `.env`，至少填写真实的 `WECHAT_SECRET`。`WECHAT_APPID` 已按小程序项目配置为 `wx6ecd7fbfbe1dfeec`。

第一次启动前初始化数据库：

```powershell
flask --app run init-db
```

启动 Flask：

```powershell
python run.py
```

验证：

- 后端健康检查：`http://127.0.0.1:5000/health`
- 管理员后台：`http://127.0.0.1:5000/admin`
- 默认管理员来自 `.env`：`admin / admin12345`

如果要用真机预览小程序，把 Flask 改成局域网监听：

```powershell
flask --app run run --host 0.0.0.0 --port 5000 --debug
```

同时把小程序配置里的 `127.0.0.1` 改成电脑局域网 IP。

## 2. 小程序启动

```powershell
cd .\csi_miniprogram--
npm install
npm run typecheck
```

用微信开发者工具导入 `csi_miniprogram--` 目录。当前服务地址在：

```text
csi_miniprogram--/miniprogram/config/env.ts
```

如果这个文件不存在，先从样板复制：

```powershell
Copy-Item .\miniprogram\config\env.example.ts .\miniprogram\config\env.ts
```

默认配置适合微信开发者工具模拟器：

```typescript
apiBaseUrl: 'http://127.0.0.1:5000'
wsBaseUrl: 'ws://127.0.0.1:5000/ws/v1/events'
```

本地 HTTP 联调时，在微信开发者工具里勾选“详情 -> 本地设置 -> 不校验合法域名、web-view、TLS 版本以及 HTTPS 证书”。正式发布时必须改成备案域名、HTTPS 和 WSS，并在微信公众平台配置合法域名。

## 3. 无硬件先跑通

`.env` 中保持：

```text
MQTT_ENABLED=0
```

这时可以验证：

- Flask 能启动。
- 管理员后台能登录。
- 小程序能通过微信登录创建用户。
- `/api/v1/me`、`/api/v1/devices`、`/api/v1/fall-events` 能返回真实数据库数据。

没有硬件和 MQTT 时，设备会是空列表或离线状态，开始/停止检测不能完整闭环。

## 4. 接硬件联调

先启动 Mosquitto，并确保 C 板固件连接同一个 Broker。然后修改后端 `.env`：

```text
MQTT_ENABLED=1
MQTT_HOST=你的Broker地址
MQTT_PORT=1883
MQTT_USERNAME=和Mosquitto/C板一致
MQTT_PASSWORD=和Mosquitto/C板一致
```

设备名必须三处一致：

- C 板固件里的 `device_name`。
- 管理员后台创建设备时的 `device_uid`。
- MQTT Topic：`csi/v1/devices/{device_name}/...`。

完整联调顺序：

1. 启动 Mosquitto。
2. 启动 Flask 后端。
3. 打开小程序并完成微信登录，让后端创建用户。
4. 登录管理员后台，为该用户创建设备，`device_uid` 填 C 板设备名，例如 `esp01`。
5. C 板上电连接 Wi-Fi 和 MQTT，后端收到 `up/online`、`up/status` 后设备变在线。
6. 小程序设备详情页点击开始/停止检测，后端发布 `down/control`，C 板回复 `up/ack` 并上传 `up/csi`。

硬件协议细节见 `hardware代码交互逻辑说明.md`。
