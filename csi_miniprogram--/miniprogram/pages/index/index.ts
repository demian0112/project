import { runtimeConfig } from '../../config/env'
import { DeviceSummary, FallEvent, RealtimeEvent } from '../../models/domain'
import { getDevices, getFallEvents, qualityText } from '../../utils/api'
import { realtimeClient } from '../../services/realtime'

const CSI_FAULT_CODES = ['NO_CSI_FRAME', 'NO_CSI_FRAME_TIMEOUT', 'UART_TIMEOUT']

interface DisplayDevice extends DeviceSummary {
  stateText: string
  detectionText: string
  qualityLabel: string
  qualityClass: string
}

Page({
  data: {
    statusBarHeight: 44,
    loading: true,
    loadError: '',
    devices: [] as DisplayDevice[],
    totalCount: 0,
    onlineCount: 0,
    runningCount: 0,
    activeAlert: null as FallEvent | null,
    alertTimeText: '',
    _pollingTimer: null as any,
    _unsubscribe: null as null | (() => void),
    _unsubscribeConnection: null as null | (() => void),
  },

  onLoad() {
    const system = wx.getSystemInfoSync()
    this.setData({ statusBarHeight: system.statusBarHeight || 44 })
    const unsubscribe = realtimeClient.subscribe((event) => this.handleRealtimeEvent(event))
    const unsubscribeConnection = realtimeClient.subscribeConnection(() => this.loadHome())
    ;(this as any).data._unsubscribe = unsubscribe
    ;(this as any).data._unsubscribeConnection = unsubscribeConnection
  },

  onShow() {
    ;(this as any)._active = true
    this.selectTab()
    this.loadHome()
    if (!runtimeConfig.wsBaseUrl) this.startPolling()
  },

  onHide() {
    ;(this as any)._active = false
    this.stopPolling()
  },

  onUnload() {
    this.stopPolling()
    const unsubscribe = (this as any).data._unsubscribe
    if (unsubscribe) unsubscribe()
    const unsubscribeConnection = (this as any).data._unsubscribeConnection
    if (unsubscribeConnection) unsubscribeConnection()
  },

  onPullDownRefresh() {
    this.loadHome().finally(() => wx.stopPullDownRefresh())
  },

  async loadHome() {
    try {
      const devices = await getDevices()
      const displayDevices = devices.map((device) => ({
        ...device,
        stateText: { online: '在线', offline: '离线', error: '采集异常' }[device.state],
        detectionText: {
          idle: '等待检测',
          starting: '正在启动',
          running: '检测中',
          stopping: '正在停止',
        }[device.detection_state],
        qualityLabel: qualityText(device.network_quality),
        qualityClass: `quality-${device.network_quality}`,
      }))
      this.setData({
        loading: false,
        loadError: '',
        devices: displayDevices,
        totalCount: devices.length,
        onlineCount: devices.filter((device) => device.state === 'online').length,
        runningCount: devices.filter((device) => device.detection_state === 'running').length,
      })
      await this.loadLatestAlert()
    } catch (error: any) {
      const app = getApp<IAppOption>()
      this.setData({
        loading: false,
        loadError: app.globalData.bootstrapError || (error && error.message) || '设备信息加载失败',
      })
    }
  },

  async loadLatestAlert() {
    try {
      const alerts = await getFallEvents(1)
      const alert = alerts.find((item) => item.status === 'pending') || null
      this.setData({
        activeAlert: alert,
        alertTimeText: alert ? this.formatDate(alert.occurred_at) : '',
      })
    } catch (error) {
      console.error('首页告警同步失败', error)
    }
  },

  onDeviceTap(e: WechatMiniprogram.TouchEvent) {
    const deviceName = String(e.currentTarget.dataset.name || '')
    if (!deviceName) return
    wx.navigateTo({ url: `/pages/device-detail/index?deviceName=${encodeURIComponent(deviceName)}` })
  },

  onAlertTap() {
    const alert = this.data.activeAlert
    if (!alert) return
    wx.navigateTo({ url: `/pages/fall-alert/index?id=${encodeURIComponent(String(alert.id))}` })
  },

  onRetry() {
    const app = getApp<IAppOption & { login: () => void }>()
    if (!app.globalData.authReady && typeof app.login === 'function') app.login()
    this.setData({ loading: true })
    this.loadHome()
  },

  startPolling() {
    this.stopPolling()
    ;(this as any).data._pollingTimer = setInterval(() => this.loadHome(), 5000)
  },

  stopPolling() {
    const timer = (this as any).data._pollingTimer
    if (timer) clearInterval(timer)
    ;(this as any).data._pollingTimer = null
  },

  handleRealtimeEvent(event: RealtimeEvent) {
    if (event.event === 'device.fault') {
      this.showDeviceFaultModal(event)
    }
    if (event.event === 'detection.fall-result' && event.data && (event.data as any).fall_detected) {
      const id = (event.data as any).fall_event_id
      if (id !== undefined) {
        wx.navigateTo({ url: `/pages/fall-alert/index?id=${encodeURIComponent(String(id))}` })
      }
    }
    this.loadHome()
  },

  showDeviceFaultModal(event: RealtimeEvent) {
    if (!(this as any)._active) return
    const data = (event.data || {}) as any
    const code = String(data.code || '').toUpperCase()
    if (CSI_FAULT_CODES.indexOf(code) < 0) return
    const faultKey = `${event.device_name || ''}:${code}`
    if ((this as any)._shownFaultKey === faultKey) return
    ;(this as any)._shownFaultKey = faultKey
    const templateData = data.template_data || {}
    const content = (templateData.thing5 && templateData.thing5.value)
      || data.message
      || '未接收到 CSI 数据，请检查设备电源、A/B 板连接和设备摆放后恢复设备。'
    wx.showModal({
      title: '设备采集异常',
      content,
      confirmText: '去处理',
      showCancel: false,
      success: () => {
        if (event.device_name) {
          wx.navigateTo({ url: `/pages/device-detail/index?deviceName=${encodeURIComponent(event.device_name)}` })
        }
      },
    })
  },

  formatDate(value: string): string {
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return value
    const pad = (number: number) => String(number).padStart(2, '0')
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
  },

  selectTab() {
    if (typeof this.getTabBar === 'function') {
      const tabBar = this.getTabBar()
      if (tabBar) tabBar.setData({ selected: 0 })
    }
  },
})
