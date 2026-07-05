# 安守 CSI 跌倒检测微信小程序前端开发文档（更新详细版）

> 适用对象：微信小程序前端开发同学  
> 技术栈建议：微信原生小程序 / Vant Weapp 可选 / HTTPS API / WebSocket  
> 后端：Python Flask + SQLite/SQLAlchemy + MQTT/Mosquitto + ESP 硬件  
> 版本：v3.0，根据最新业务需求调整  

---

## 1. 项目目标

本小程序是整个“安守 CSI 跌倒检测系统”的前端界面。小程序不直接连接 MQTT，也不直接连接 ESP 硬件设备，而是通过 Python Flask 后端完成登录、设备查询、设备控制、状态展示、跌倒告警展示等功能。

整体链路如下：

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

小程序需要实现的核心功能：

```text
1. 用户首次进入小程序，完成微信登录。
2. 登录后显示当前用户绑定的设备。
3. 显示设备在线、离线、出错三种状态。
4. 在线设备可以进入详情页并点击启动检测。
5. 检测过程中显示 CSI 网络质量和检测状态。
6. 如果后端算法判断发生跌倒，小程序立即显示跌倒告警。
7. 用户可以点击停止检测，后端通知硬件停止上传 CSI。
8. 个人中心显示用户信息、设备数量和基础统计。
```

---

## 2. 前后端职责边界

### 2.1 小程序负责什么

小程序只负责界面和请求，不负责硬件通信。

```text
小程序负责：
- 微信登录入口
- 用户信息展示
- 设备列表展示
- 设备状态展示
- start / stop 按钮交互
- 网络质量展示
- 跌倒告警页面展示
- WebSocket 实时事件接收
- 页面刷新后的状态恢复
```

### 2.2 小程序不负责什么

```text
小程序不负责：
- 不直接连接 MQTT
- 不保存 MQTT 地址、账号、密码
- 不生成 MQTT Topic
- 不生成 cmd
- 不生成 session
- 不判断硬件是否真正启动
- 不解析原始 CSI
- 不运行跌倒检测算法
- 不直接保存 openid / session_key 作为可信凭证
```

### 2.3 后端负责什么

```text
Python Flask 后端负责：
- 接收 wx.login() 的 code
- 调用微信 code2Session 换取 openid / unionid / session_key
- 将微信身份信息保存到用户表
- 生成后端 access_token
- 判断用户和设备绑定关系
- 根据设备名订阅 /up/online、/up/status、/up/fault、/up/csi 等 Topic
- 判断设备 online / offline / error
- 接收 start / stop 请求
- 发布 /down/control 到 MQTT
- 接收硬件 CSI 数据
- 判断网络质量
- 调用跌倒检测算法
- 发生跌倒时写入跌倒发生表并推送给小程序
```

---

## 3. UI 页面规划

根据当前 UI 定稿，本项目包含 5 个主要页面。

```text
pages/
  index/index              监测首页
  device-detail/index      设备监测详情页
  device-management/index  设备资料管理页（只读）
  fall-alert/index         跌倒告警页
  mine/mine                个人中心页
```

也可以后续扩展：

```text
pages/device-list/index    设备列表页
pages/fall-records/index   跌倒记录页
pages/settings/index       设置页
```

---

## 4. 页面一：监测首页

### 4.1 页面作用

监测首页是用户打开小程序后最主要的页面，用于展示当前用户绑定设备的整体状态。首页不直接执行 start / stop，用户点击设备卡片后进入设备监测详情页操作。

首页需要展示：

```text
- 用户问候语
- 系统当前状态
- 设备在线 / 离线 / 出错
- 当前是否正在检测
- CSI 网络质量
- 今日守护时长
- 设备列表卡片
- 点击设备进入设备监测详情
```

### 4.2 首页推荐数据结构

```javascript
Page({
  data: {
    user: {
      id: null,
      nickname: '',
      avatar_url: ''
    },

    devices: [],

    currentDevice: null,

    summary: {
      online_count: 0,
      offline_count: 0,
      error_count: 0,
      running_count: 0,
      today_guard_minutes: 0
    },

    wsConnected: false,
    loading: false
  }
})
```

### 4.3 设备卡片字段

每台设备在前端至少需要这些字段：

```javascript
{
  device_name: 'csi-gw-001',
  display_name: '客厅设备',
  location: '客厅',

  state: 'online',           // online / offline / error
  state_text: '在线',

  detection_state: 'idle',   // idle / starting / running / stopping
  network_quality: 'unknown',// good / fair / poor / unknown

  last_seen_at: '2026-07-03T14:30:00+08:00',
  fault_message: null
}
```

### 4.4 设备主状态映射

后端只返回三个主状态，前端不要自己发明更多主状态。

| 后端 state | 前端显示 | UI 建议 |
|---|---|---|
| `online` | 在线 | 绿色圆点 |
| `offline` | 离线 | 灰色圆点 |
| `error` | 出错 | 红色圆点 |

前端显示函数示例：

```javascript
function mapDeviceState(state) {
  const map = {
    online: { text: '在线', className: 'state-online' },
    offline: { text: '离线', className: 'state-offline' },
    error: { text: '出错', className: 'state-error' }
  }
  return map[state] || map.offline
}
```

---

## 5. 页面二：设备详情页

### 5.1 页面作用

用户点击某个设备卡片后进入设备监测详情页。该页面合并实时检测展示和设备详情功能；只有设备处于 `online` 状态时，才允许点击启动检测。

设备详情页需要展示：

```text
- 设备名称
- 安装位置
- 在线状态
- 运行状态
- Wi-Fi / RSSI 信息
- 当前是否正在检测
- CSI 网络质量
- 最近一次通信时间
- 启动检测按钮
- 停止检测按钮
- 故障提示
```

### 5.2 详情页数据结构

```javascript
Page({
  data: {
    deviceName: '',
    device: null,
    controlLoading: false,
    canStart: false,
    canStop: false
  }
})
```

### 5.3 启动按钮显示规则

```text
如果 state = online 且 detection_state = idle：显示“开始检测”
如果 detection_state = starting：显示“正在启动...”并禁用按钮
如果 detection_state = running：显示“停止检测”
如果 detection_state = stopping：显示“正在停止...”并禁用按钮
如果 state = offline：显示“设备离线，无法启动”
如果 state = error：显示“设备出错，请检查设备”
```

前端不要仅仅因为 HTTP 请求成功就认为设备已经启动成功。HTTP 成功只代表后端接收了请求，真正启动结果以后端 WebSocket 推送或状态接口返回为准。

---

## 6. 页面三：跌倒告警页

### 6.1 页面触发条件

当小程序收到后端推送的跌倒事件时跳转到告警页。

事件名：

```text
detection.fall-result
```

并且：

```text
fall_detected = true
或 result = 1
```

### 6.2 告警页展示内容

```text
- 检测到跌倒
- 设备名称
- 安装位置
- 检测时间
- 网络质量
- 是否已发送微信提醒
- 确认安全按钮
- 联系家人按钮
- 返回首页按钮
```

### 6.3 告警页数据结构

```javascript
Page({
  data: {
    alert: {
      id: null,
      device_name: '',
      display_name: '',
      location: '',
      occurred_at: '',
      network_quality: 'unknown',
      status: 'pending'
    }
  }
})
```

### 6.4 告警处理动作

第一阶段建议只做两个动作：

```text
1. 我已安全：把跌倒记录状态改为 confirmed。
2. 返回首页：返回首页继续展示设备状态。
```

后续可以扩展：

```text
- 联系家人
- 微信订阅消息提醒
- 电话提醒
- 告警误报标记
```

---

## 7. 页面四：个人中心页

### 7.1 页面作用

个人中心只显示用户基础信息和设备管理入口。设备管理页仅展示设备名称、设备编号和安装位置，不展示在线、检测、网络质量等运行状态，也不提供检测控制。

展示内容：

```text
- 用户头像
- 用户昵称
- 当前账号状态
- 最近一次登录时间
- 设备管理入口
```

### 7.2 用户信息字段

```javascript
{
  id: 12,
  nickname: '微信用户',
  avatar_url: 'https://...',
  phone: null,
  status: 'active',
  last_login_at: '2026-07-03T14:30:00+08:00'
}
```

---

## 8. 小程序启动流程

### 8.1 启动时要做什么

用户首次进入小程序时，前端需要完成以下步骤：

```text
1. 调用 wx.login() 获取 code。
2. 将 code 发送给后端 /api/v1/auth/wechat-login。
3. 后端通过 code2Session 获取 openid / unionid / session_key。
4. 后端将微信身份信息保存到用户表。
5. 后端返回 access_token 和用户基础信息。
6. 小程序保存 access_token。
7. 小程序请求当前用户设备列表。
8. 小程序建立 WebSocket 连接。
9. 小程序显示设备在线 / 离线 / 出错状态。
```

### 8.2 重要说明

前端不要直接传 openid、unionid、session_key 给后端。

正确方式是：

```text
前端传 code
后端用 code 换 openid / unionid / session_key
后端保存这些微信身份字段
后端返回自己的 access_token
```

原因：

```text
- openid 不能由前端伪造。
- session_key 属于敏感会话密钥，不应暴露在前端业务逻辑中。
- 后端需要基于 openid 建立自己的用户体系。
- 小程序后续请求只需要携带后端 access_token。
```

### 8.3 app.js 启动示例

```javascript
App({
  globalData: {
    baseUrl: 'https://你的域名.com',
    wsUrl: 'wss://你的域名.com/ws/v1/events',
    token: '',
    user: null
  },

  onLaunch() {
    this.login()
  },

  login() {
    wx.login({
      success: (res) => {
        if (!res.code) {
          wx.showToast({ title: '微信登录失败', icon: 'none' })
          return
        }

        wx.request({
          url: this.globalData.baseUrl + '/api/v1/auth/wechat-login',
          method: 'POST',
          data: {
            code: res.code
          },
          success: (resp) => {
            const data = resp.data
            if (data.access_token) {
              wx.setStorageSync('token', data.access_token)
              this.globalData.token = data.access_token
              this.globalData.user = data.user
            }
          }
        })
      }
    })
  }
})
```

---

## 9. 用户资料获取与保存

### 9.1 微信身份和用户资料要分开

后端通过 code2Session 获取的是微信身份：

```text
openid
unionid
session_key
```

用户头像、昵称不一定在登录时自动获得，通常需要用户主动授权、选择或填写后提交。

因此小程序建议采用两步：

```text
第一步：登录，后端保存 openid / unionid / session_key。
第二步：用户授权或填写昵称头像后，调用资料更新接口。
```

### 9.2 更新资料接口

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

响应：

```json
{
  "success": true,
  "user": {
    "id": 12,
    "nickname": "微信用户",
    "avatar_url": "https://..."
  }
}
```

---

## 10. 前端请求封装

建议统一封装请求，避免每个页面重复写 token。

```javascript
const BASE_URL = 'https://你的域名.com'

function request(options) {
  const token = wx.getStorageSync('token')

  return new Promise((resolve, reject) => {
    wx.request({
      url: BASE_URL + options.url,
      method: options.method || 'GET',
      data: options.data || {},
      header: {
        'Content-Type': 'application/json',
        'Authorization': token ? 'Bearer ' + token : '',
        ...(options.header || {})
      },
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data)
        } else {
          handleApiError(res.data)
          reject(res.data)
        }
      },
      fail(err) {
        wx.showToast({ title: '网络请求失败', icon: 'none' })
        reject(err)
      }
    })
  })
}

function handleApiError(err) {
  const msg = err && err.message ? err.message : '请求失败'
  wx.showToast({ title: msg, icon: 'none' })
}

module.exports = { request }
```

---

## 11. API 对接说明

## 11.1 微信登录

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

前端处理：

```javascript
wx.setStorageSync('token', res.access_token)
```

---

## 11.2 获取当前用户

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

## 11.3 获取当前用户绑定设备

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
      "network_quality": "unknown",
      "last_seen_at": "2026-07-03T14:30:00+08:00",
      "fault_message": null
    }
  ]
}
```

前端逻辑：

```text
1. 首页 onShow 时请求设备列表。
2. 将 items 渲染为设备卡片。
3. 根据 state 显示在线、离线、出错。
4. 如果 WebSocket 未连接，依靠这个接口恢复状态。
```

---

## 11.4 获取设备详情

```http
GET /api/v1/devices/{device_name}
Authorization: Bearer <token>
```

响应：

```json
{
  "device_name": "csi-gw-001",
  "display_name": "客厅设备",
  "location": "客厅",
  "state": "online",
  "enabled": true,
  "network": {
    "rssi": -48
  },
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

## 11.5 启动或停止设备

```http
POST /api/v1/devices/{device_name}/control
Authorization: Bearer <token>
Idempotency-Key: <uuid>
```

启动请求：

```json
{
  "action": "start"
}
```

停止请求：

```json
{
  "action": "stop"
}
```

响应：

```json
{
  "accepted": true,
  "device_name": "csi-gw-001",
  "action": "start",
  "control_state": "published",
  "session": "sess-20260703-000001",
  "message": "启动命令已发送，等待设备执行"
}
```

前端注意：

```text
accepted = true 只表示后端已经接收并尝试发布 MQTT 控制命令。
是否真正启动成功，需要等 WebSocket 推送或重新请求设备详情。
前端在收到最终状态前保持 starting / stopping 并禁用控制按钮；15 秒仍未确认时显示状态确认超时。
```

按钮处理示例：

```javascript
async function controlDevice(deviceName, action) {
  const idempotencyKey = generateUUID()
  return request({
    url: `/api/v1/devices/${deviceName}/control`,
    method: 'POST',
    header: {
      'Idempotency-Key': idempotencyKey
    },
    data: { action }
  })
}
```

---

## 11.6 获取最近跌倒记录

首页或告警记录页可以调用：

```http
GET /api/v1/fall-events?limit=20
Authorization: Bearer <token>
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
      "result": 1,
      "network_quality": "good",
      "status": "pending",
      "handled_at": null,
      "remark": null
    }
  ]
}
```

---

## 11.7 确认跌倒告警已处理

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

响应：

```json
{
  "success": true
}
```

---

## 12. WebSocket 实时事件

### 12.1 为什么需要 WebSocket

后端无法像普通 HTTP 请求那样主动“POST 到正在运行的小程序页面”。如果需要实时更新在线状态、错误状态、网络质量和跌倒告警，推荐使用 WebSocket。

如果暂时不做 WebSocket，也可以用轮询：

```text
每 3~5 秒请求一次 GET /api/v1/devices
每 2~3 秒请求一次 GET /api/v1/devices/{device_name}
```

但最终建议使用 WebSocket。

WebSocket 每次首次连接或断线重连成功后，前端必须重新调用设备列表或设备详情接口，恢复权威状态，不能仅依赖后续增量事件。

### 12.2 连接地址

```text
wss://你的域名.com/ws/v1/events?token=<access_token>
```

### 12.3 WebSocket 连接示例

```javascript
function connectWS() {
  const token = wx.getStorageSync('token')
  const wsUrl = `wss://你的域名.com/ws/v1/events?token=${token}`

  const socket = wx.connectSocket({
    url: wsUrl
  })

  wx.onSocketOpen(() => {
    console.log('WebSocket connected')
  })

  wx.onSocketMessage((msg) => {
    const event = JSON.parse(msg.data)
    handleWsEvent(event)
  })

  wx.onSocketClose(() => {
    console.log('WebSocket closed')
    setTimeout(connectWS, 3000)
  })
}
```

### 12.4 WebSocket 事件格式

```json
{
  "event": "device.state.changed",
  "device_name": "csi-gw-001",
  "server_time": "2026-07-03T14:30:05+08:00",
  "data": {}
}
```

### 12.5 设备在线事件

后端订阅到 `/up/online` 后，会推送：

```json
{
  "event": "device.state.changed",
  "device_name": "csi-gw-001",
  "data": {
    "state": "online",
    "last_seen_at": "2026-07-03T14:30:05+08:00"
  }
}
```

前端处理：

```text
找到 devices 中 device_name 对应的设备，将 state 改为 online。
```

### 12.6 设备离线事件

如果后端一段时间没有收到 `/up/online` 或 `/up/status`，会推送：

```json
{
  "event": "device.state.changed",
  "device_name": "csi-gw-001",
  "data": {
    "state": "offline"
  }
}
```

### 12.7 设备错误事件

后端订阅到 `/up/fault` 后，会推送：

```json
{
  "event": "device.fault",
  "device_name": "csi-gw-001",
  "data": {
    "state": "error",
    "fault_code": "SENSOR_ERROR",
    "fault_message": "传感器异常"
  }
}
```

前端处理：

```text
设备主状态改为 error。
显示错误提示。
禁用启动检测按钮。
```

### 12.8 网络质量事件

后端解析 `/up/csi` 后，会根据丢帧率和时间戳判断网络质量：

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

前端展示：

| network_quality | 显示 |
|---|---|
| `good` | 信号良好 |
| `fair` | 信号一般 |
| `poor` | 信号较差 |
| `unknown` | 暂无数据 |

### 12.9 跌倒事件

算法返回 1 时，后端推送：

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

前端处理：

```javascript
function handleWsEvent(event) {
  if (event.event === 'detection.fall-result') {
    if (event.data && event.data.fall_detected) {
      wx.navigateTo({
        url: `/pages/fall-alert/index?id=${event.data.fall_event_id}`
      })
    }
  }
}
```

---

## 13. 前端错误码处理

后端统一错误格式：

```json
{
  "error": "DEVICE_OFFLINE",
  "message": "设备离线，无法启动检测"
}
```

前端需要重点处理：

| 错误码 | 前端提示 |
|---|---|
| `AUTH_REQUIRED` | 登录已失效，请重新登录 |
| `USER_DISABLED` | 当前账号已被禁用 |
| `DEVICE_NOT_FOUND` | 设备不存在 |
| `DEVICE_NOT_OWNED` | 无权访问该设备 |
| `DEVICE_DISABLED` | 设备已禁用 |
| `DEVICE_OFFLINE` | 设备离线，无法操作 |
| `DEVICE_ERROR` | 设备出错，请检查设备 |
| `STATUS_TIMEOUT` | 设备状态超时 |
| `CONTROL_BUSY` | 设备正在执行其他操作 |
| `CSI_TIMEOUT` | CSI 数据中断 |

---

## 14. 前端开发顺序

建议按下面顺序开发，不要一开始就做所有页面。

### 第一阶段：基础页面和登录

```text
1. 创建小程序项目。
2. 配置 app.json 页面路由。
3. 完成 app.js 的 wx.login。
4. 调用 /api/v1/auth/wechat-login。
5. 保存 access_token。
6. 完成 request 封装。
7. 完成首页静态 UI。
```

### 第二阶段：设备列表

```text
1. 调用 GET /api/v1/devices。
2. 渲染设备卡片。
3. 完成 online / offline / error 三种状态显示。
4. 点击设备进入详情页。
```

### 第三阶段：设备控制

```text
1. 设备详情页请求 GET /api/v1/devices/{device_name}。
2. 完成开始检测按钮。
3. 完成停止检测按钮。
4. 控制中禁用按钮，避免重复点击。
5. 根据后端返回和 WebSocket 事件更新状态。
```

### 第四阶段：WebSocket

```text
1. 登录成功后连接 WebSocket。
2. 处理 device.state.changed。
3. 处理 device.fault。
4. 处理 detection.network-quality。
5. 处理 detection.fall-result。
6. 断线后自动重连。
7. 重连后重新请求设备列表恢复状态。
```

### 第五阶段：跌倒告警

```text
1. 创建 fall-alert 页面。
2. 接收到 fall-result=1 后跳转。
3. 展示跌倒时间、设备、位置、网络质量。
4. 支持确认安全。
5. 支持查看历史跌倒记录。
```

---

## 15. 前端验收标准

前端完成后至少满足：

```text
1. 用户首次进入小程序可以完成登录。
2. 登录后 access_token 可以保存。
3. 首页可以显示当前用户绑定的设备。
4. 设备可以显示 online / offline / error。
5. 离线设备不能启动。
6. 出错设备不能启动。
7. 在线设备可以发送 start 请求。
8. 检测中可以显示网络质量 good / fair / poor / unknown。
9. 用户可以发送 stop 请求。
10. 收到 fall-result=1 可以进入跌倒告警页。
11. 告警记录可以被确认处理。
12. WebSocket 断开后可以重连并恢复状态。
```

---

## 16. 前端和后端对接时的注意事项

```text
1. 前端只传 code，不传 openid / session_key。
2. 小程序所有业务请求都要带 Authorization。
3. device_name 由后端返回，前端不要让用户修改。
4. 设备控制请求必须防止重复点击。
5. HTTP 请求成功不等于硬件执行成功。
6. 设备最终状态以后端推送或状态接口为准。
7. 前端不展示原始 CSI。
8. 跌倒告警要明显展示，不能只用普通 toast。
9. WebSocket 断开后必须重新 GET 当前设备状态。
10. 开发阶段可以用轮询，正式版本建议使用 WebSocket。
```
