import { runtimeConfig } from '../../config/env'
import { DeviceDetail, FallEvent, RealtimeEvent } from '../../models/domain'
import type {
  WechatSubscriptionScene,
  WechatSubscriptionStatusValue,
} from '../../utils/api'
import {
  controlDevice,
  getDeviceDetail,
  getFallEvents,
  qualityText,
  registerWechatSubscription,
  updateFallEvent,
} from '../../utils/api'
import { realtimeClient } from '../../services/realtime'

const CONTROL_TIMEOUT_MS = 15000
const CONTROL_POLL_MS = 1500
type LightState = 'normal' | 'fall' | 'error' | 'offline'
type SubscribeTemplateTarget = {
  scene: WechatSubscriptionScene
  templateId: string
}
type SubscriptionSetting = {
  mainSwitch?: boolean
  itemSettings?: Record<string, string>
}

Page({
  data: {
    statusBarHeight: 44,
    deviceName: '',
    device: null as DeviceDetail | null,
    loading: true,
    loadError: '',
    controlLoading: false,
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
    if (!runtimeConfig.wsBaseUrl) this.startPolling()
    if (this.data.pendingAction) this.scheduleControlCheck()
  },

  onHide() { this.stopPolling(); this.stopControlTimer() },

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
      try {
        await this.requestDetectionSubscriptionsBeforeStart()
      } catch (error) {
        console.warn('[wechat-subscribe] ignored error before start:', error)
      }
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

  async requestDetectionSubscriptionsBeforeStart() {
    const templateTargets = this.getSubscribeTemplateTargets()
    const tmplIds = templateTargets.map((target) => target.templateId)
    console.log('[wechat-subscribe] request tmplIds:', tmplIds)
    if (!tmplIds.length) {
      console.warn('[wechat-subscribe] no template ids configured')
      return
    }
    if (typeof wx.requestSubscribeMessage !== 'function') {
      console.warn('[wechat-subscribe] requestSubscribeMessage is not supported')
      return
    }

    try {
      const settingResult = await this.getSubscriptionSetting()
      const setting = (settingResult as any).subscriptionsSetting as SubscriptionSetting | undefined
      console.log('[wechat-subscribe] setting:', setting)
      await this.showSubscriptionSettingHint(setting, templateTargets)
    } catch (error) {
      console.warn('[wechat-subscribe] get setting failed:', error)
    }

    try {
      const result = await this.requestSubscribeMessage(tmplIds)
      console.log('[wechat-subscribe] result:', result)
      await this.saveDetectionSubscribeResult(result, templateTargets)
      const accepted = templateTargets.some((target) => this.readSubscribeStatus(result[target.templateId]) === 'accept')
      wx.showToast({
        title: accepted ? '提醒已开启' : '未开启提醒',
        icon: accepted ? 'success' : 'none',
      })
    } catch (error) {
      console.warn('[wechat-subscribe] failed:', error)
      wx.showToast({ title: '提醒开启失败，将继续启动', icon: 'none' })
    }
  },

  getSubscribeTemplateTargets(): SubscribeTemplateTarget[] {
    const subscribeTemplateIds = runtimeConfig.subscribeTemplateIds || {
      fallAlert: '',
      deviceFault: '',
    }
    const fallAlertTemplateId = subscribeTemplateIds.fallAlert || runtimeConfig.subscribeTemplateId
    const deviceFaultTemplateId = subscribeTemplateIds.deviceFault
    const targets: Array<{ scene: WechatSubscriptionScene; templateId?: string }> = [
      { scene: 'fall_alert', templateId: fallAlertTemplateId },
      { scene: 'device_fault', templateId: deviceFaultTemplateId },
    ]
    return targets.filter(
      (target): target is SubscribeTemplateTarget => Boolean(target.templateId),
    )
  },

  getSubscriptionSetting(): Promise<WechatMiniprogram.GetSettingSuccessCallbackResult> {
    return new Promise((resolve, reject) => {
      wx.getSetting({
        withSubscriptions: true,
        success: resolve,
        fail: reject,
      } as any)
    })
  },

  async showSubscriptionSettingHint(
    setting: SubscriptionSetting | undefined,
    targets: SubscribeTemplateTarget[],
  ) {
    if (!setting) return
    if (setting.mainSwitch === false) {
      await this.showSubscribeHint('消息订阅总开关未开启，开启后才能收到跌倒提醒和设备异常提醒。')
      return
    }

    const itemSettings = setting.itemSettings || {}
    const rejectedScenes = targets
      .filter((target) => itemSettings[target.templateId] === 'reject')
      .map((target) => target.scene)
    if (!rejectedScenes.length) return

    const fallRejected = rejectedScenes.includes('fall_alert')
    const deviceRejected = rejectedScenes.includes('device_fault')
    const content = fallRejected && deviceRejected
      ? '你已关闭人员跌倒提醒和设备状态提醒，可能无法收到跌倒和设备异常服务通知，可在小程序设置中重新开启。'
      : deviceRejected
      ? '你已关闭设备状态提醒，设备异常时可能无法收到服务通知，可在小程序设置中重新开启。'
      : '你已关闭人员跌倒提醒，检测到跌倒时可能无法收到服务通知，可在小程序设置中重新开启。'
    await this.showSubscribeHint(content)
  },

  showSubscribeHint(content: string): Promise<void> {
    return new Promise((resolve) => {
      wx.showModal({
        title: '订阅提醒',
        content,
        showCancel: true,
        confirmText: '知道了',
        cancelText: '继续启动',
        success: () => resolve(),
        fail: () => resolve(),
      })
    })
  },

  requestSubscribeMessage(
    tmplIds: string[],
  ): Promise<Record<string, unknown>> {
    return new Promise((resolve, reject) => {
      wx.requestSubscribeMessage({
        tmplIds,
        success: (response) => resolve(response as Record<string, unknown>),
        fail: reject,
      })
    })
  },

  async saveDetectionSubscribeResult(
    result: Record<string, unknown>,
    targets: SubscribeTemplateTarget[],
  ) {
    const tasks = targets.reduce((items, target) => {
      const status = this.readSubscribeStatus(result[target.templateId])
      if (!status) {
        console.warn('[wechat-subscribe] unknown result skipped:', target.scene, result[target.templateId])
        return items
      }
      items.push(registerWechatSubscription({
        scene: target.scene,
        template_id: target.templateId,
        status,
      }))
      return items
    }, [] as Array<Promise<unknown>>)
    const settled: Array<{ status: 'fulfilled' | 'rejected'; value?: unknown; reason?: unknown }> = []
    for (let index = 0; index < tasks.length; index += 1) {
      try {
        const value = await tasks[index]
        settled.push({ status: 'fulfilled', value })
      } catch (reason) {
        settled.push({ status: 'rejected', reason })
      }
    }
    console.log('[wechat-subscribe] saved results:', settled)
  },

  readSubscribeStatus(value: unknown): WechatSubscriptionStatusValue | '' {
    const status = String(value || '')
    if (status === 'accept' || status === 'reject' || status === 'ban' || status === 'filter') {
      return status
    }
    return ''
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
      lightTitle = '设备异常'
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

  onRealtimeEvent(event: RealtimeEvent) {
    if (event.device_name !== this.data.deviceName) return
    const eventData = event.data as any
    if (
      event.event === 'device.runtime.changed' &&
      eventData &&
      eventData.control_ok === false &&
      eventData.action === this.data.pendingAction
    ) {
      this.clearPendingControl()
      wx.showToast({ title: eventData.message || '硬件拒绝了控制命令', icon: 'none' })
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
