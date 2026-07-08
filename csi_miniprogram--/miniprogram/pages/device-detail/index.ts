import { runtimeConfig } from '../../config/env'
import { DeviceDetail, FallEvent, RealtimeEvent } from '../../models/domain'
import type { WechatSubscriptionScene, WechatSubscriptionStatusValue } from '../../utils/api'
import {
  controlDevice,
  getDeviceDetail,
  getFallEvents,
  qualityText,
  registerWechatSubscription,
  resetDeviceFault,
  updateFallEvent,
} from '../../utils/api'
import { realtimeClient } from '../../services/realtime'

const CONTROL_TIMEOUT_MS = 15000
const CONTROL_POLL_MS = 1500
type LightState = 'normal' | 'fall' | 'error' | 'offline'
const CSI_FAULT_CODES = ['NO_CSI_FRAME', 'NO_CSI_FRAME_TIMEOUT', 'UART_TIMEOUT']

Page({
  data: {
    statusBarHeight: 44,
    deviceName: '',
    device: null as DeviceDetail | null,
    loading: true,
    loadError: '',
    controlLoading: false,
    resetLoading: false,
    pendingAction: '' as '' | 'start' | 'stop',
    pendingDeadline: 0,
    stateText: '未知',
    detectionText: '等待启动',
    qualityLabel: '暂无数据',
    lastSeenText: '暂无记录',
    lightState: 'offline' as LightState,
    lightTitle: '等待启动',
    lightDesc: '设备准备就绪',
    activeAlert: null as FallEvent | null,
    alertTimeText: '',
    _pollingTimer: null as any,
    _controlTimer: null as any,
    _unsubscribe: null as null | (() => void),
    _unsubscribeConnection: null as null | (() => void),
  },

  onLoad(options: Record<string, string>) {
    const system = wx.getSystemInfoSync()
    const deviceName = decodeURIComponent(options.deviceName || '')
    this.setData({ statusBarHeight: system.statusBarHeight || 44, deviceName })
    ;(this as any).data._unsubscribe = realtimeClient.subscribe((event) => this.onRealtimeEvent(event))
    ;(this as any).data._unsubscribeConnection = realtimeClient.subscribeConnection(() => this.loadDevice())
    this.loadDevice()
  },

  onShow() {
    ;(this as any)._active = true
    if (!runtimeConfig.wsBaseUrl) this.startPolling()
    if (this.data.pendingAction) this.scheduleControlCheck()
  },

  onHide() {
    ;(this as any)._active = false
    this.stopPolling()
    this.stopControlTimer()
  },

  onUnload() {
    this.stopPolling()
    this.stopControlTimer()
    const unsubscribe = (this as any).data._unsubscribe
    if (unsubscribe) unsubscribe()
    const unsubscribeConnection = (this as any).data._unsubscribeConnection
    if (unsubscribeConnection) unsubscribeConnection()
  },

  onPullDownRefresh() {
    this.loadDevice().finally(() => wx.stopPullDownRefresh())
  },

  async loadDevice() {
    if (!this.data.deviceName) {
      this.setData({ loading: false, loadError: '缺少设备标识' })
      return
    }
    try {
      const actualDevice = await getDeviceDetail(this.data.deviceName)
      let device = actualDevice
      let pendingAction = this.data.pendingAction
      let controlLoading = this.data.controlLoading
      if (pendingAction) {
        const reachedTarget = pendingAction === 'start'
          ? actualDevice.detection_state === 'running'
          : actualDevice.detection_state === 'idle'
        if (actualDevice.state !== 'online' || actualDevice.fault.code) {
          this.stopControlTimer()
          wx.showToast({
            title: actualDevice.fault.message || (actualDevice.state === 'offline' ? '设备已离线' : '设备发生故障'),
            icon: 'none',
          })
          pendingAction = ''
          controlLoading = false
        } else if (reachedTarget) {
          this.stopControlTimer()
          wx.showToast({ title: pendingAction === 'start' ? '设备已开始检测' : '设备已停止检测', icon: 'success' })
          pendingAction = ''
          controlLoading = false
        } else if (Date.now() >= this.data.pendingDeadline) {
          this.stopControlTimer()
          wx.showToast({ title: '设备状态确认超时，请稍后刷新', icon: 'none' })
          pendingAction = ''
          controlLoading = false
        } else {
          device = {
            ...actualDevice,
            detection_state: pendingAction === 'start' ? 'starting' : 'stopping',
          }
          this.scheduleControlCheck()
        }
      }
      this.setData({
        device,
        pendingAction,
        pendingDeadline: pendingAction ? this.data.pendingDeadline : 0,
        controlLoading,
        loading: false,
        loadError: '',
        ...this.getDeviceViewState(device, this.data.activeAlert),
      })
      await this.loadLatestAlert()
    } catch (error: any) {
      if (this.data.pendingAction) {
        if (Date.now() >= this.data.pendingDeadline) this.clearPendingControl()
        else this.scheduleControlCheck()
      }
      this.setData({ loading: false, loadError: (error && error.message) || '设备信息加载失败' })
    }
  },

  async onControlTap() {
    const device = this.data.device
    if (!device || this.data.controlLoading) return
    if (device.state !== 'online') {
      wx.showToast({ title: device.state === 'error' ? '设备异常，无法操作' : '设备离线，无法操作', icon: 'none' })
      return
    }
    if (!device.enabled) {
      wx.showToast({ title: '设备已被管理员停用', icon: 'none' })
      return
    }
    if (device.fault.code) {
      wx.showToast({ title: device.fault.message || '设备存在故障，无法操作', icon: 'none' })
      return
    }
    const action = device.detection_state === 'running' ? 'stop' : 'start'
    if (action === 'start') {
      await this.promptStartupSubscriptionsBeforeStart()
      if (this.data.controlLoading) return
    }
    const pendingState = action === 'start' ? 'starting' : 'stopping'
    const pendingDevice: DeviceDetail = { ...device, detection_state: pendingState }
    this.setData({
      controlLoading: true,
      pendingAction: action,
      pendingDeadline: Date.now() + CONTROL_TIMEOUT_MS,
      device: pendingDevice,
      ...this.getDeviceViewState(pendingDevice, this.data.activeAlert),
    })
    try {
      const result = await controlDevice(device.device_name, action)
      if (!result.accepted) throw new Error(result.message)
      wx.showToast({ title: '命令已发送，等待设备确认', icon: 'none' })
      this.scheduleControlCheck()
    } catch (error: any) {
      this.clearPendingControl()
      wx.showToast({ title: (error && error.message) || '控制失败', icon: 'none' })
      await this.loadDevice()
    }
  },

  async promptStartupSubscriptionsBeforeStart() {
    const entries = [
      {
        scene: 'fall_alert' as WechatSubscriptionScene,
        templateId: runtimeConfig.subscribeTemplateIds.fallAlert || runtimeConfig.subscribeTemplateId,
      },
      {
        scene: 'device_fault' as WechatSubscriptionScene,
        templateId: runtimeConfig.subscribeTemplateIds.deviceFault,
      },
    ].filter((item) => Boolean(item.templateId))
    if (!entries.length || typeof wx.requestSubscribeMessage !== 'function') return

    const shouldEnable = await new Promise<boolean>((resolve) => {
      wx.showModal({
        title: '开启检测提醒',
        content: '启动检测前建议开启微信提醒，检测到跌倒或设备采集异常时会通知你。',
        confirmText: '开启',
        cancelText: '跳过',
        success: (result) => resolve(Boolean(result.confirm)),
        fail: () => resolve(false),
      })
    })
    if (!shouldEnable) return

    try {
      const statuses = await this.requestStartupSubscriptions(entries)
      const accepted = Object.keys(statuses).some((key) => statuses[key] === 'accept')
      wx.showToast({
        title: accepted ? '提醒已开启' : '未开启提醒',
        icon: accepted ? 'success' : 'none',
      })
    } catch (error) {
      console.error('启动前订阅提醒失败', error)
      wx.showToast({ title: '提醒开启失败，将继续启动', icon: 'none' })
    }
  },

  async requestStartupSubscriptions(
    entries: Array<{ scene: WechatSubscriptionScene; templateId: string }>,
  ): Promise<Record<string, WechatSubscriptionStatusValue>> {
    const templateIds = Array.from(new Set(entries.map((item) => item.templateId)))
    const statuses = await new Promise<Record<string, WechatSubscriptionStatusValue>>((resolve, reject) => {
      wx.requestSubscribeMessage({
        tmplIds: templateIds,
        success: (result) => {
          const values: Record<string, WechatSubscriptionStatusValue> = {}
          templateIds.forEach((templateId) => {
            values[templateId] = String(
              (result as Record<string, unknown>)[templateId] || 'reject',
            ) as WechatSubscriptionStatusValue
          })
          resolve(values)
        },
        fail: reject,
      })
    })
    for (const entry of entries) {
      try {
        await registerWechatSubscription({
          scene: entry.scene,
          template_id: entry.templateId,
          status: statuses[entry.templateId] || 'reject',
        })
      } catch (error) {
        console.error('订阅状态保存失败', entry.scene, error)
      }
    }
    return statuses
  },

  async loadLatestAlert() {
    try {
      const alerts = await getFallEvents(20)
      const alert = alerts.find((item) => (
        item.device_name === this.data.deviceName &&
        item.status === 'pending'
      )) || null
      this.setData({
        activeAlert: alert,
        alertTimeText: alert ? this.formatDate(alert.occurred_at) : '',
        ...(this.data.device ? this.getDeviceViewState(this.data.device, alert) : {}),
      })
    } catch (error) {
      console.error('设备告警同步失败', error)
    }
  },

  onAlertTap() {
    const alert = this.data.activeAlert
    if (!alert) return
    wx.navigateTo({ url: `/pages/fall-alert/index?id=${encodeURIComponent(String(alert.id))}` })
  },

  async onConfirmSafe() {
    const alert = this.data.activeAlert
    if (!alert) return
    try {
      await updateFallEvent(alert.id, 'confirmed')
      this.setData({
        activeAlert: null,
        ...(this.data.device ? this.getDeviceViewState(this.data.device, null) : {}),
      })
      wx.showToast({ title: '已确认安全', icon: 'success' })
    } catch (error: any) {
      wx.showToast({ title: (error && error.message) || '提交失败', icon: 'none' })
    }
  },

  async onResetFaultTap() {
    const device = this.data.device
    if (!device || this.data.resetLoading) return
    if (!device.enabled) {
      wx.showToast({ title: '设备已被管理员停用', icon: 'none' })
      return
    }
    const confirmed = await new Promise<boolean>((resolve) => {
      wx.showModal({
        title: '恢复设备',
        content: '请确认已检查 A 板供电、A/B 板连接和设备摆放。确认后将复位设备到待检测状态。',
        confirmText: '我已检查',
        cancelText: '取消',
        success: (result) => resolve(Boolean(result.confirm)),
        fail: () => resolve(false),
      })
    })
    if (!confirmed) return

    this.setData({ resetLoading: true })
    try {
      const result = await resetDeviceFault(device.device_name)
      if (!result.accepted) throw new Error(result.message)
      wx.showToast({ title: '复位命令已发送', icon: 'none' })
      await this.loadDevice()
    } catch (error: any) {
      wx.showToast({ title: (error && error.message) || '复位失败', icon: 'none' })
    } finally {
      this.setData({ resetLoading: false })
    }
  },

  startPolling() {
    this.stopPolling()
    ;(this as any).data._pollingTimer = setInterval(() => this.loadDevice(), 5000)
  },

  stopPolling() {
    const timer = (this as any).data._pollingTimer
    if (timer) clearInterval(timer)
    ;(this as any).data._pollingTimer = null
  },

  scheduleControlCheck() {
    if ((this as any).data._controlTimer) return
    ;(this as any).data._controlTimer = setTimeout(() => {
      ;(this as any).data._controlTimer = null
      this.loadDevice()
    }, CONTROL_POLL_MS)
  },

  stopControlTimer() {
    const timer = (this as any).data._controlTimer
    if (timer) clearTimeout(timer)
    ;(this as any).data._controlTimer = null
  },

  clearPendingControl() {
    this.stopControlTimer()
    this.setData({ controlLoading: false, pendingAction: '', pendingDeadline: 0 })
  },

  onBack() { wx.navigateBack() },
  onRetry() { this.loadDevice() },

  getDeviceViewState(device: DeviceDetail, alert: FallEvent | null) {
    const stateText = { online: '在线', offline: '离线', error: '异常' }[device.state]
    const detectionText = { idle: '等待启动', starting: '正在启动', running: '检测运行中', stopping: '正在停止' }[device.detection_state]
    let lightState: LightState = 'normal'
    let lightTitle = detectionText
    let lightDesc = device.detection_state === 'running' ? '环境状态持续分析中' : '设备准备就绪'

    if (alert) {
      lightState = 'fall'
      lightTitle = '检测到跌倒'
      lightDesc = '请尽快确认安全'
    } else if (device.state === 'offline') {
      lightState = 'offline'
      lightTitle = '设备离线'
      lightDesc = '等待设备重新上线'
    } else if (device.state === 'error' || device.fault.code) {
      lightState = 'error'
      lightTitle = this.isCsiCollectionFault(device.fault.code) ? '设备采集异常' : '设备异常'
      lightDesc = device.fault.message || device.fault_message || '请检查设备连接和供电状态'
    }

    return {
      stateText,
      detectionText,
      qualityLabel: qualityText(device.network_quality),
      lastSeenText: this.formatDate(device.last_seen_at),
      lightState,
      lightTitle,
      lightDesc,
    }
  },

  isCsiCollectionFault(code: string | null): boolean {
    return Boolean(code && CSI_FAULT_CODES.indexOf(String(code).toUpperCase()) >= 0)
  },

  showDeviceFaultModal(event: RealtimeEvent) {
    if (!(this as any)._active) return
    const data = (event.data || {}) as any
    if (!this.isCsiCollectionFault(data.code)) return
    const faultKey = `${event.device_name || ''}:${String(data.code || '').toUpperCase()}`
    if ((this as any)._shownFaultKey === faultKey) return
    ;(this as any)._shownFaultKey = faultKey
    const templateData = data.template_data || {}
    const content = (templateData.thing5 && templateData.thing5.value)
      || data.message
      || '未接收到 CSI 数据，请检查设备电源、A/B 板连接和设备摆放后再恢复设备。'
    wx.showModal({
      title: '设备采集异常',
      content,
      confirmText: '去处理',
      showCancel: false,
    })
  },

  onRealtimeEvent(event: RealtimeEvent) {
    if (event.device_name !== this.data.deviceName) return
    const eventData = event.data as any
    if (event.event === 'device.fault') {
      this.clearPendingControl()
      this.showDeviceFaultModal(event)
    }
    if (
      event.event === 'device.runtime.changed' &&
      eventData &&
      eventData.control_ok === false &&
      eventData.action === this.data.pendingAction
    ) {
      this.clearPendingControl()
      wx.showToast({ title: eventData.message || '硬件拒绝了控制命令', icon: 'none' })
    }
    if (
      event.event === 'device.runtime.changed' &&
      eventData &&
      eventData.action === 'reset' &&
      eventData.fault_cleared
    ) {
      ;(this as any)._shownFaultKey = ''
      wx.showToast({ title: eventData.message || '设备已恢复', icon: 'success' })
    }
    if (event.event === 'detection.fall-result' && event.data && (event.data as any).fall_detected) {
      const id = (event.data as any).fall_event_id
      if (id !== undefined) {
        wx.navigateTo({ url: `/pages/fall-alert/index?id=${encodeURIComponent(String(id))}` })
      }
    }
    this.loadDevice()
  },

  formatDate(value: string | null): string {
    if (!value) return '暂无记录'
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return value
    const pad = (number: number) => String(number).padStart(2, '0')
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
  },
})
