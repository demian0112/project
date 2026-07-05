# 微信小程序开关控制与 Mosquitto 验收层操作指南

## 0. 文档目标

本文档用于指导当前阶段的系统开发与验证。

当前阶段目标不是修改 A/B/C 板，也不是重写 CSI 采集流程，而是先打通：

```text
微信小程序开关
    ↓
后端接口识别开 / 关信号
    ↓
后端生成预留控制命令
    ↓
可选：后端把控制命令发布到 Mosquitto
    ↓
可选：用 mosquitto_sub 验收控制信号是否进入 MQTT 层
```

当前阶段的核心验收点是：

```text
小程序能发出 enable=true / false
后端能收到并识别 enable=true / false
后端能生成后续要发给设备的控制命令
Mosquitto 可以作为后端到设备之间的信号传输验收层
```

---

## 1. 结论先说

### 1.1 Mosquitto 能不能和后端直接连接？

可以。

后端可以直接作为一个 MQTT Client 连接 Mosquitto Broker。

后端连接 Mosquitto 后，可以做两件事：

```text
1. publish：向 esp32s3/control 发布控制命令
2. subscribe：订阅 esp32s3/status，接收设备状态回执
```

也就是说，后端完全可以变成：

```text
微信小程序
    ↓ HTTP
FastAPI 后端
    ↓ MQTT publish
Mosquitto Broker
    ↓ MQTT message
C 板 / 设备端
```

---

### 1.2 Python 为什么不能直接和 ABC 板互联？

不是绝对不能，而是不推荐直接互联。

你的系统里：

```text
A 板：负责 AP
B 板：负责 CSI 采集
C 板：负责联网和 MQTT 上传
```

其中 B 板不直接连接路由器，也不直接连接 Mosquitto。C 板才是联网网关。所以 Python 或后端不应该直接找 A/B 板通信，而应该通过 C 板和 MQTT 进行数据/指令交换。

更合理的结构是：

```text
Python / 后端
    ↓
Mosquitto
    ↓
C 板
    ↓ UART
B 板
```

如果未来要让 B 板真正停止采集，可以由 C 板收到 MQTT 控制命令后，再通过 UART 给 B 板发送 `START_CSI` 或 `STOP_CSI`。

---

### 1.3 Mosquitto 能不能作为最后信号传输的验收层？

可以，但要注意它验收的是“信号进入 MQTT 层”，不是“设备一定已经执行”。

更准确地说：

```text
Mosquitto 可以作为控制信号传输链路的中间验收层。
```

它能证明：

```text
后端已经把控制命令发到了指定 topic
订阅者可以从指定 topic 收到这条命令
MQTT 层的 topic、账号、密码、端口、网络是通的
```

但它不能单独证明：

```text
C 板已经收到命令
C 板已经解析成功
C 板已经执行成功
B 板已经停止采集
系统真的进入休眠
```

如果要证明设备真的执行了，需要让 C 板执行后再发布状态回执：

```text
C 板执行成功
    ↓
发布到 esp32s3/status
    ↓
后端订阅 esp32s3/status
    ↓
小程序显示“设备已进入休眠”或“设备已开启监测”
```

所以最终推荐验收层分成三层：

```text
第一层：后端识别层
小程序 → 后端，后端收到 enable=true / false

第二层：MQTT 传输层
后端 → Mosquitto，mosquitto_sub 能收到 esp32s3/control 命令

第三层：设备执行层
C 板 → esp32s3/status，后端收到设备 ACK / 状态回执
```

---

## 2. 推荐最终架构

当前阶段先做前两段：

```text
微信小程序
    ↓ wx.request
FastAPI 后端
    ↓ 可选 MQTT publish
Mosquitto Broker
```

后续再接设备：

```text
微信小程序
    ↓
FastAPI 后端
    ↓ publish esp32s3/control
Mosquitto Broker
    ↓
C 板订阅 esp32s3/control
    ↓
C 板控制上传 / 或通过 UART 控制 B 板
    ↓
C 板发布 esp32s3/status 状态回执
    ↓
后端订阅状态
    ↓
小程序显示真实设备状态
```

---

## 3. Topic 设计

建议统一使用下面三个 topic：

```text
esp32s3/control   后端向设备发送控制命令
esp32s3/status    设备向后端返回状态
esp32s3/test      设备上传 CSI 数据
```

含义如下：

| Topic | 方向 | 作用 |
|---|---|---|
| `esp32s3/control` | 后端 → Mosquitto → C 板 | 控制开关、模式切换 |
| `esp32s3/status` | C 板 → Mosquitto → 后端 | 设备上线、休眠、监测状态回执 |
| `esp32s3/test` | C 板 → Mosquitto → Python | CSI 数据上传 |

---

## 4. 控制命令格式

小程序发给后端：

```json
{
  "device_id": "esp32s3_csi_001",
  "enable": true,
  "source": "wechat_miniprogram"
}
```

后端转成准备发给 MQTT 的命令：

```json
{
  "cmd": "set_monitor",
  "target": "esp32s3_csi_001",
  "enable": true,
  "request_id": "xxxx-xxxx-xxxx",
  "source": "backend",
  "time": "2026-06-29T18:00:00"
}
```

关闭时：

```json
{
  "cmd": "set_monitor",
  "target": "esp32s3_csi_001",
  "enable": false,
  "request_id": "xxxx-xxxx-xxxx",
  "source": "backend",
  "time": "2026-06-29T18:00:00"
}
```

---

## 5. 后端第一版：只识别小程序开关，不连接 Mosquitto

### 5.1 安装依赖

```bat
pip install fastapi uvicorn
```

### 5.2 创建 `backend/main.py`

```python
from fastapi import FastAPI
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional
import uuid

app = FastAPI(title="CSI MiniProgram Control Backend")

device_state = {
    "device_id": "esp32s3_csi_001",
    "monitor_enabled": False,
    "state_text": "sleep",
    "last_request_id": None,
    "last_source": None,
    "last_update_time": None,
}


class SwitchRequest(BaseModel):
    device_id: str = Field(default="esp32s3_csi_001")
    enable: bool
    source: Optional[str] = Field(default="wechat_miniprogram")


def reserved_control_interface(device_id: str, enable: bool, request_id: str):
    """
    当前阶段只生成预留控制命令，不真正发给设备。
    后续可以在这里接 Mosquitto。
    """

    command = {
        "cmd": "set_monitor",
        "enable": enable,
        "target": device_id,
        "request_id": request_id,
        "source": "backend",
        "time": datetime.now().isoformat(timespec="seconds"),
    }

    print("\n========== 预留设备控制命令 ==========")
    print(command)
    print("=====================================\n")

    return command


@app.get("/api/health")
def health_check():
    return {
        "ok": True,
        "message": "backend is running",
        "time": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/device/state")
def get_device_state():
    return {
        "ok": True,
        "device_state": device_state,
    }


@app.post("/api/device/switch")
def set_device_switch(req: SwitchRequest):
    request_id = str(uuid.uuid4())

    monitor_enabled = req.enable
    state_text = "monitoring" if monitor_enabled else "sleep"

    device_state["device_id"] = req.device_id
    device_state["monitor_enabled"] = monitor_enabled
    device_state["state_text"] = state_text
    device_state["last_request_id"] = request_id
    device_state["last_source"] = req.source
    device_state["last_update_time"] = datetime.now().isoformat(timespec="seconds")

    print("\n========== 收到小程序开关信号 ==========")
    print(f"request_id: {request_id}")
    print(f"device_id : {req.device_id}")
    print(f"enable    : {req.enable}")
    print(f"source    : {req.source}")
    print("识别结果  :", "开启实时监测" if req.enable else "关闭实时监测 / 休眠")
    print("======================================\n")

    reserved_command = reserved_control_interface(
        device_id=req.device_id,
        enable=req.enable,
        request_id=request_id,
    )

    return {
        "ok": True,
        "recognized": True,
        "request_id": request_id,
        "monitor_enabled": monitor_enabled,
        "state_text": state_text,
        "device_state": device_state,
        "reserved_device_command": reserved_command,
    }
```

### 5.3 启动后端

```bat
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

浏览器访问：

```text
http://127.0.0.1:8000/api/health
```

正常返回：

```json
{
  "ok": true,
  "message": "backend is running"
}
```

---

## 6. 小程序页面代码

### 6.1 `index.wxml`

```xml
<view class="page">
  <view class="card">
    <view class="title">CSI 实时监测控制</view>

    <view class="row">
      <text>当前状态：</text>
      <text class="{{monitoring ? 'on' : 'off'}}">{{statusText}}</text>
    </view>

    <view class="row">
      <text>实时监测开关：</text>
      <switch checked="{{monitoring}}" bindchange="onSwitchChange" />
    </view>

    <view class="info">
      <view>后端连接：{{backendStatus}}</view>
      <view>设备 ID：{{deviceId}}</view>
      <view>最近请求：{{lastRequestId}}</view>
      <view>提示：{{message}}</view>
    </view>
  </view>
</view>
```

### 6.2 `index.wxss`

```css
.page {
  padding: 40rpx;
  background: #f5f5f5;
  min-height: 100vh;
}

.card {
  background: #ffffff;
  border-radius: 20rpx;
  padding: 40rpx;
}

.title {
  font-size: 40rpx;
  font-weight: bold;
  margin-bottom: 40rpx;
}

.row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 30rpx;
  font-size: 32rpx;
}

.on {
  color: #07c160;
  font-weight: bold;
}

.off {
  color: #999999;
  font-weight: bold;
}

.info {
  margin-top: 40rpx;
  font-size: 26rpx;
  color: #666666;
  line-height: 1.8;
}
```

### 6.3 `index.js`

开发者工具模拟器先使用：

```js
const BASE_URL = 'http://127.0.0.1:8000'
```

如果用手机真机调试，需要改成电脑局域网 IP，例如：

```js
const BASE_URL = 'http://192.168.101.48:8000'
```

完整代码：

```js
const BASE_URL = 'http://127.0.0.1:8000'

Page({
  data: {
    deviceId: 'esp32s3_csi_001',
    monitoring: false,
    statusText: '未知',
    backendStatus: '未连接',
    lastRequestId: '-',
    message: '等待操作'
  },

  onLoad() {
    this.checkBackend()
    this.getDeviceState()
  },

  checkBackend() {
    wx.request({
      url: BASE_URL + '/api/health',
      method: 'GET',
      success: (res) => {
        console.log('后端健康检查成功:', res.data)
        this.setData({
          backendStatus: '已连接',
          message: '后端连接成功'
        })
      },
      fail: (err) => {
        console.error('后端健康检查失败:', err)
        this.setData({
          backendStatus: '连接失败',
          message: '请检查后端是否启动'
        })
      }
    })
  },

  getDeviceState() {
    wx.request({
      url: BASE_URL + '/api/device/state',
      method: 'GET',
      success: (res) => {
        console.log('当前设备状态:', res.data)

        const state = res.data.device_state
        const enabled = state.monitor_enabled

        this.setData({
          monitoring: enabled,
          statusText: enabled ? '实时监测中' : '休眠中',
          lastRequestId: state.last_request_id || '-',
          message: '状态读取成功'
        })
      },
      fail: (err) => {
        console.error('读取状态失败:', err)
        this.setData({
          message: '读取状态失败'
        })
      }
    })
  },

  onSwitchChange(e) {
    const enabled = e.detail.value

    console.log('小程序开关变化:', enabled)

    this.setData({
      monitoring: enabled,
      statusText: enabled ? '正在开启...' : '正在关闭...',
      message: '正在发送开关信号...'
    })

    wx.request({
      url: BASE_URL + '/api/device/switch',
      method: 'POST',
      header: {
        'content-type': 'application/json'
      },
      data: {
        device_id: this.data.deviceId,
        enable: enabled,
        source: 'wechat_miniprogram'
      },
      success: (res) => {
        console.log('后端识别结果:', res.data)

        if (res.data.ok && res.data.recognized) {
          this.setData({
            monitoring: res.data.monitor_enabled,
            statusText: res.data.monitor_enabled ? '实时监测中' : '休眠中',
            lastRequestId: res.data.request_id,
            message: '后端已识别开关信号'
          })
        } else {
          this.rollbackSwitch(enabled, '后端未识别开关信号')
        }
      },
      fail: (err) => {
        console.error('发送开关失败:', err)
        this.rollbackSwitch(enabled, '发送失败，请检查后端')
      }
    })
  },

  rollbackSwitch(enabled, message) {
    wx.showToast({
      title: '控制失败',
      icon: 'error'
    })

    this.setData({
      monitoring: !enabled,
      statusText: !enabled ? '实时监测中' : '休眠中',
      message: message
    })
  }
})
```

---

## 7. 开发工具设置

开发阶段需要在微信开发者工具中设置：

```text
详情
    ↓
本地设置
    ↓
勾选：不校验合法域名、web-view、TLS 版本以及 HTTPS 证书
```

正式上线时需要使用 HTTPS 域名，不能使用 `127.0.0.1` 或普通 HTTP。

---

## 8. 第一阶段验收：小程序到后端

启动后端：

```bat
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

打开小程序，点击开关。

开启时，后端应打印：

```text
========== 收到小程序开关信号 ==========
request_id: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
device_id : esp32s3_csi_001
enable    : True
source    : wechat_miniprogram
识别结果  : 开启实时监测
======================================
```

关闭时，后端应打印：

```text
enable    : False
识别结果  : 关闭实时监测 / 休眠
```

如果这一步成功，说明：

```text
小程序开关信号已经能被后端识别。
```

---

## 9. 第二阶段：让后端直接连接 Mosquitto

这一阶段用于验证：

```text
小程序 → 后端 → Mosquitto
```

### 9.1 安装 MQTT 依赖

```bat
pip install paho-mqtt
```

### 9.2 修改后端代码

在 `main.py` 顶部新增：

```python
import json
import paho.mqtt.client as mqtt
```

添加 MQTT 配置：

```python
MQTT_HOST = "192.168.101.48"
MQTT_PORT = 1883
MQTT_USERNAME = "esp32"
MQTT_PASSWORD = "esp32pass"
MQTT_TOPIC_CONTROL = "esp32s3/control"
MQTT_TOPIC_STATUS = "esp32s3/status"

mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)


def on_connect(client, userdata, flags, rc):
    print("MQTT connected, rc=", rc)
    client.subscribe(MQTT_TOPIC_STATUS)


def on_message(client, userdata, msg):
    print("收到设备状态 topic:", msg.topic)
    print("payload:", msg.payload.decode("utf-8", errors="ignore"))


mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
mqtt_client.loop_start()
```

然后把原来的 `reserved_control_interface()` 改成：

```python
def reserved_control_interface(device_id: str, enable: bool, request_id: str):
    command = {
        "cmd": "set_monitor",
        "enable": enable,
        "target": device_id,
        "request_id": request_id,
        "source": "backend",
        "time": datetime.now().isoformat(timespec="seconds"),
    }

    payload = json.dumps(command, ensure_ascii=False)

    print("\n========== 发布 MQTT 控制命令 ==========")
    print("topic:", MQTT_TOPIC_CONTROL)
    print("payload:", payload)
    print("======================================\n")

    result = mqtt_client.publish(
        MQTT_TOPIC_CONTROL,
        payload,
        qos=1,
        retain=False,
    )

    return {
        "topic": MQTT_TOPIC_CONTROL,
        "payload": command,
        "mqtt_publish_rc": result.rc,
    }
```

---

## 10. Mosquitto 验收方法

### 10.1 启动 Mosquitto

```bat
cd /d C:\Mosquitto
mosquitto.exe -c C:\Mosquitto\mosquitto_esp32.conf -v
```

### 10.2 打开订阅窗口

另开一个 CMD：

```bat
cd /d C:\Mosquitto
mosquitto_sub.exe -h 192.168.101.48 -p 1883 -u esp32 -P esp32pass -t esp32s3/# -v
```

### 10.3 点击小程序开关

当你在小程序里打开开关时，订阅窗口应该看到类似：

```text
esp32s3/control {"cmd":"set_monitor","enable":true,"target":"esp32s3_csi_001","request_id":"...","source":"backend","time":"..."}
```

关闭时应该看到：

```text
esp32s3/control {"cmd":"set_monitor","enable":false,"target":"esp32s3_csi_001","request_id":"...","source":"backend","time":"..."}
```

如果你能在 `mosquitto_sub` 里看到这两条消息，说明：

```text
小程序 → 后端 → Mosquitto 这条链路已经打通。
```

这时 Mosquitto 就可以作为“控制信号传输验收层”。

---

## 11. 第三阶段：设备执行层验收设计

后续接 C 板时，不应该只看 `esp32s3/control`，还应该让 C 板执行后回复 `esp32s3/status`。

例如 C 板收到开启命令后回复：

```json
{
  "device_id": "esp32s3_c_csi_2s_001",
  "request_id": "xxxx-xxxx-xxxx",
  "monitor_enabled": true,
  "state": "monitoring",
  "ack": true
}
```

C 板收到关闭命令后回复：

```json
{
  "device_id": "esp32s3_c_csi_2s_001",
  "request_id": "xxxx-xxxx-xxxx",
  "monitor_enabled": false,
  "state": "sleep",
  "ack": true
}
```

后端订阅 `esp32s3/status`，收到 ACK 后，小程序再显示：

```text
设备已开启实时监测
```

或：

```text
设备已进入休眠
```

这样才是完整闭环：

```text
小程序发出命令
    ↓
后端识别命令
    ↓
Mosquitto 转发命令
    ↓
C 板收到命令
    ↓
C 板执行命令
    ↓
C 板返回 ACK
    ↓
后端收到 ACK
    ↓
小程序显示真实状态
```

---

## 12. 各阶段验收标准

| 阶段 | 验收对象 | 验收方法 | 成功标准 |
|---|---|---|---|
| 第一阶段 | 小程序到后端 | 看后端控制台 | 后端打印 `enable=True/False` |
| 第二阶段 | 后端到 Mosquitto | 看 `mosquitto_sub` | 收到 `esp32s3/control` 消息 |
| 第三阶段 | Mosquitto 到 C 板 | 看 C 板串口 | C 板打印收到控制命令 |
| 第四阶段 | C 板执行结果 | 看 `esp32s3/status` | 收到 `ack=true` 状态回执 |
| 第五阶段 | 小程序显示真实状态 | 看小程序页面 | 页面显示设备真实状态 |

---

## 13. 当前阶段建议做到哪里

你现在建议先做到第二阶段：

```text
小程序 → 后端 → Mosquitto
```

也就是：

```text
1. 小程序点击开关
2. 后端收到 enable=true / false
3. 后端发布到 esp32s3/control
4. mosquitto_sub 能看到控制命令
```

暂时不用接 C 板。

这样你就能向老师或项目验收说明：

```text
小程序控制信号已经完成前端输入、后端识别和 MQTT 信号发布；
Mosquitto 已作为设备控制信号的中间传输与验收层；
后续只需要让 C 板订阅 esp32s3/control 并返回 esp32s3/status，即可完成完整设备闭环。
```

---

## 14. 最推荐的最终链路

最终系统建议采用：

```text
微信小程序
    ↓ HTTPS
FastAPI 后端
    ↓ MQTT publish
Mosquitto Broker
    ↓ MQTT subscribe
C 板
    ↓ UART，可选
B 板
```

数据方向：

```text
控制流：小程序 → 后端 → Mosquitto → C 板 → B 板
状态流：C 板 → Mosquitto → 后端 → 小程序
数据流：C 板 → Mosquitto → Python 可视化
```

这样结构清晰，每一层都能单独验收，也方便后续调试和扩展。

---

## 15. 一句话总结

Mosquitto 可以直接和后端连接，后端通过 MQTT 向 `esp32s3/control` 发布控制命令；Mosquitto 可以作为“控制信号是否成功进入 MQTT 传输层”的验收层，但最终是否真正执行，还需要 C 板通过 `esp32s3/status` 返回 ACK 状态回执来确认。
