# esp02 CSI 到 Docker 跌倒算法接入总结与服务器部署说明

本文档记录本次本地联调已经完成的改动、验证结论，以及后续迁移到服务器时需要调整的配置。文档不包含 `.env` 中的真实密钥。

## 1. 当前链路

当前已经跑通的真实数据链路是：

```text
esp02
  -> MQTT Broker 39.107.112.214:1883
  -> Flask 后端 MQTT 订阅与 CSI 解码
  -> Docker 跌倒算法 WebSocket ws://127.0.0.1:18080/stream
  -> Docker 模型推理
  -> 后端落库/事件推送/告警处理
```

本地端口约定：

| 服务 | 本地地址 | 说明 |
| --- | --- | --- |
| Flask 后端 | `http://172.20.10.14:5051` | 小程序开发环境访问后端 |
| Docker 算法服务 | `http://127.0.0.1:18080` | 宿主机端口 |
| Docker 容器内部 | `5000` | 只作为容器内部端口，不给小程序或宿主机后端直接使用 |
| MQTT Broker | `39.107.112.214:1883` | esp02 与后端都使用这个 Broker |

特别注意：`18080:5000` 的含义是宿主机 `18080` 映射到容器内部 `5000`。后端在宿主机运行时应访问 `127.0.0.1:18080`，不要改成 `127.0.0.1:5000`。

## 2. 本次主要修改

### 2.1 Docker 算法接入

新增/更新了后端到 Docker 算法的接入层：

- `app/services/fall_algorithm_client.py`
  - 封装 Docker `/health`、`/config`、`/reset`、`/stream`。
  - WebSocket 发送格式为 `{"type":"data","line":"..."}`。
  - 修复了 Docker 平时不返回消息时 WebSocket read timeout 被误判为异常断线的问题。

- `app/services/csi_algorithm_formatter.py`
  - 将后端解码出的 `CsiFrame` 转成 Docker 期望的 CSV 行。
  - 已按 Docker 容器真实字段顺序输出：
    `type,id,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel,local_timestamp,sig_len,rx_state,len,first_word,data`
  - 使用 `csv.writer` 输出，确保最后的 CSI 数组字段带引号，否则 Docker parser 会把数组里的逗号误拆成多个字段。
  - 当前 `MAC` 字段使用稳定设备名 `esp02`，因为现有 `csib64-v2` CSI payload 不包含真实 ESP 物理 MAC。

- `app/services/csi_algorithm_stream_service.py`
  - 管理每个设备 session 到 Docker 的 WebSocket 转发。
  - CSI 到达后按 batch 间隔拆帧发送给 Docker。
  - 增加首帧发送日志 `FALL_ALGORITHM_FIRST_FRAME_SENT`，便于确认真实 CSI 已送入 Docker。
  - 修复了 Docker WS 断开/重连时，发送线程与清队列并发导致 `next_send_at` 变成 `None` 后崩溃的问题。

- `app/services/fall_alert_service.py`
  - 接收 Docker 算法 alert 后创建/更新后端跌倒事件，并进入现有前端推送/微信通知流程。

- `app/services/fall_algorithm_config.py`
  - 将设备上的算法参数整理成 Docker `/config` 可接收的配置。

### 2.2 后端配置与模型

相关配置已进入 `app/config.py` 和 `.env.example`：

```dotenv
MQTT_ENABLED=1
MQTT_HOST=39.107.112.214
MQTT_PORT=1883
MQTT_AUTOSTART_DEVICES=1

FALL_ALGORITHM_ENABLED=1
FALL_ALGORITHM_HTTP_BASE_URL=http://127.0.0.1:18080
FALL_ALGORITHM_WS_URL=ws://127.0.0.1:18080/stream
FALL_ALGORITHM_SINGLE_ACTIVE_STREAM=1

FALL_ALGORITHM_RATE_SIG_MODE=11
FALL_ALGORITHM_CHANNEL=100
FALL_ALGORITHM_FFT_GAIN=0
FALL_ALGORITHM_AGC_GAIN=0
FALL_ALGORITHM_RX_STATE=0
FALL_ALGORITHM_NOISE_FLOOR=0
```

`run.py` 支持通过环境变量指定 Flask 端口：

```dotenv
FLASK_RUN_PORT=5051
```

数据库方面已增加算法配置与算法告警相关字段。初始化/升级命令仍然是：

```bash
flask --app run.py init-db
```

本地数据库中已确认只保留小写设备名：

```text
esp02
```

### 2.3 小程序配置

本地开发环境的小程序后端地址已改为：

```ts
apiBaseUrl: 'http://172.20.10.14:5051'
wsBaseUrl: 'ws://172.20.10.14:5051/ws/v1/events'
```

文件位置：

```text
/Users/niki/Desktop/project/csi_miniprogram--/miniprogram/config/env.ts
```

后续上服务器时，这里需要替换成服务器 HTTPS/WSS 域名，不能继续使用本机局域网 IP。

## 3. 本地实测结论

本次已经完成的实测：

- `39.107.112.214:1883` TCP 连接成功。
- 使用 `.env` 中 MQTT 认证信息连接 Broker 成功，返回码 `0`。
- 后端向 `esp02` 发布 `start` 控制命令成功。
- `esp02` 进入 `running` 状态。
- `last_csi_at` 持续刷新，说明真实 ESP CSI 数据已进后端。
- 后端到 Docker `127.0.0.1:18080` 存在稳定 WebSocket TCP 连接。
- Docker 日志持续输出模型推理：

```text
Predict Cost ..., Result: other, fall_conf=...
Predict Cost ..., Result: fall, fall_conf=...
```

这说明真实 `esp02` CSI 数据已经进入 Docker 算法容器并触发模型推理。

代码验证：

```bash
pytest -q
ruff check .
```

当前结果：

```text
56 passed, 1 skipped
All checks passed
```

## 4. 服务器部署时需要修改什么

### 4.1 MQTT 配置

服务器 `.env` 中应确认：

```dotenv
MQTT_ENABLED=1
MQTT_HOST=39.107.112.214
MQTT_PORT=1883
MQTT_USERNAME=...
MQTT_PASSWORD=...
MQTT_AUTOSTART_DEVICES=1
```

服务器需要能出站访问 `39.107.112.214:1883`：

```bash
nc -vz 39.107.112.214 1883
```

### 4.2 Docker 算法地址

如果 Flask 后端直接运行在服务器宿主机上，Docker 算法容器仍然映射为 `18080:5000`，则使用：

```dotenv
FALL_ALGORITHM_HTTP_BASE_URL=http://127.0.0.1:18080
FALL_ALGORITHM_WS_URL=ws://127.0.0.1:18080/stream
```

如果 Flask 后端也放进 Docker Compose，并且和算法容器在同一个 Docker network，建议不要绕宿主机端口，改用容器服务名和内部端口：

```dotenv
FALL_ALGORITHM_HTTP_BASE_URL=http://fall-detection:5000
FALL_ALGORITHM_WS_URL=ws://fall-detection:5000/stream
```

如果 Flask 后端在容器里，但算法只通过宿主机 `18080` 暴露，可以使用：

```dotenv
FALL_ALGORITHM_HTTP_BASE_URL=http://host.docker.internal:18080
FALL_ALGORITHM_WS_URL=ws://host.docker.internal:18080/stream
```

Linux 服务器上 `host.docker.internal` 可能需要额外配置，优先推荐同一个 Compose network 的 `fall-detection:5000` 方案。

### 4.3 小程序生产地址

小程序正式环境必须改成 HTTPS/WSS：

```ts
apiBaseUrl: 'https://your-domain.example.com'
wsBaseUrl: 'wss://your-domain.example.com/ws/v1/events'
```

服务器需要配置 Nginx 或其他反向代理，并支持 WebSocket upgrade。示例：

```nginx
location / {
    proxy_pass http://127.0.0.1:5051;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location /ws/ {
    proxy_pass http://127.0.0.1:5051;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;
}
```

### 4.4 后端运行方式

本地当前使用 Flask development server。服务器不要直接用 debug server，建议先用单 worker：

```bash
gunicorn -w 1 -b 0.0.0.0:5051 run:app
```

这里先用 `-w 1` 是因为当前进程内会启动 MQTT client。如果直接开多个 worker，每个 worker 都可能重复订阅 MQTT、重复处理同一份 CSI。后续如果要多 worker，需要把 MQTT 消费拆成独立单例进程，或引入队列。

### 4.5 数据库与持久化

当前本地是 SQLite，适合开发和小规模联调。上服务器时有两种选择：

- 继续 SQLite：需要持久化 `instance/app.db`，并做好备份。
- 切换 MySQL/PostgreSQL：需要设置 `DATABASE_URL`，并重新验证 `init-db` 和现有 additive schema upgrade。

初始化命令：

```bash
flask --app run.py init-db
```

### 4.6 Docker 算法容器

算法容器必须先健康：

```bash
curl http://127.0.0.1:18080/health
docker logs --tail 100 fall-detection
```

如果使用服务器 Compose，建议只在内网/本机暴露 Docker 算法服务，不要把 `18080` 直接暴露到公网。

## 5. 上服务器后的验收清单

按顺序验收：

1. MQTT Broker 连通：

```bash
nc -vz 39.107.112.214 1883
```

2. Docker 算法健康：

```bash
curl http://127.0.0.1:18080/health
```

3. 后端健康：

```bash
curl http://127.0.0.1:5051/health
```

4. 数据库存在 `esp02` 且启用。

5. 启动 `esp02` 检测后，确认后端设备状态：

```text
state=online
detection_state=running
runtime_state=uploading
last_csi_at 持续刷新
network_quality=good
```

6. 确认后端与 Docker 建立连接：

```bash
lsof -nP -iTCP:18080
```

应看到 Python 后端到 Docker 端口的 `ESTABLISHED` 连接。

7. 确认 Docker 正在推理：

```bash
docker logs --tail 100 fall-detection
```

应看到：

```text
Predict Cost ..., Result: ..., fall_conf=...
```

不要只依赖 Docker `/stats` 判断 WebSocket 数据是否进入算法。当前镜像中 `/stats` 更像是 HTTP 共享检测器状态，不一定能准确反映 WebSocket stream 的实时处理情况。以 Docker 日志、TCP 连接、后端 `last_csi_at` 作为主要判断依据。

## 6. 后续风险与建议

- 当前默认 `FALL_ALGORITHM_SINGLE_ACTIVE_STREAM=1`，因为 Docker `/config` 和 `/reset` 是全局接口，不带 device/session。一个 Docker 实例默认只安全支持一个活跃设备流。如果后续多设备同时检测，建议每个设备一个算法容器，或先确认算法服务能隔离不同 WebSocket 连接。
- Docker 推理速度如果低于 CSI 到达速度，会出现队列丢旧帧。可从三方面优化：提升服务器 CPU/GPU、调整 Docker 模型参数、调整 `FALL_ALGORITHM_BATCH_INTERVAL_SECONDS` 和 `FALL_ALGORITHM_QUEUE_MAX_FRAMES`。
- 服务器上不要把 Docker 算法端口直接暴露公网。对外只暴露 HTTPS 后端域名。
- 小程序正式版需要可信 HTTPS/WSS 域名，并在微信后台配置合法请求域名和 socket 域名。
- 如果后续需要更清楚地观察首帧日志，建议给后端增加统一 logging 配置，让 `INFO` 级别日志进入文件，例如记录 `FALL_ALGORITHM_FIRST_FRAME_SENT`、`FALL_ALGORITHM_WS_CONNECTED` 等关键事件。

