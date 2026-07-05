# 历史演示后端（不再用于当前小程序）

此目录是早期单设备 FastAPI/MQTT 演示代码，仍使用
`esp32s3/control`、内存状态和旧数据格式。

当前小程序 `miniprogram/` 已只对接工作区中的正式 Flask 后端：

```text
../../ESP32-MQTT-Backend-main
```

正式联调请启动 Flask 后端，不要同时启动本目录的 `main.py`，否则会出现
端口、Topic、设备状态和数据来源相互冲突。本目录仅保留作历史算法原型参考。
