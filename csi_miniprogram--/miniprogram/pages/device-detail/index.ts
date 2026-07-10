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
  resetDeviceFault,
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
    resetting: false,
    pendingAction: '' as '' | 'start' | 'stop' | 'reset',
    pendingDeadline: 0,
    resetPendingUntil: 0,
    stateText: '未知',
    detectionText: '等待启动',
    qualityLabel: '暂无数据',
    lastSeenText: '暂无记录',
    lightState: 'offline' as LightState,
    lightTitle: '等待启动',
    lightDesc: '设备准备就绪',
    hasActiveFault: false,
    faultReasonText: '',
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
      let resetting = this.data.resetting
      let resetPendingUntil = this.data.resetPendingUntil
      if (pendingAction) {
        const activeFault = this.hasActiveFault(actualDevice)
        if (pendingAction === 'reset') {
          if (!activeFault) {
            this.stopControlTimer()
            wx.showToast({ title: '设备已恢复', icon: 'success' })
            pendingAction = ''
            resetting = false
            resetPendingUntil = 0
          } else if (Date.now() >= resetPendingUntil) {
            this.stopControlTimer()
            wx.showToast({
              title: '设备暂未返回复位确认，请检查连接',
              icon: 'none',
              duration: 3000,
            })
            pendingAction = ''
            resetting = false
            resetPendingUntil = 0
          } else {
            this.scheduleControlCheck()
          }
        } else {
          const reachedTarget = pendingAction === 'start'
            ? actualDevice.detection_state === 'running'
            : actualDevice.detection_state === 'idle'
          if (actualDevice.state !== 'online' || activeFault) {
            this.stopControlTimer()
            wx.showToast({
              title: this.getFaultReason(actualDevice) || (actualDevice.state === 'offline' ? '设备已离线' : '设备发生故障'),
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
      }
      this.setData({
        device,
        pendingAction,
        pendingDeadline: pendingAction && pendingAction !== 'reset' ? this.data.pendingDeadline : 0,
        resetPendingUntil,
        controlLoading,
        resetting,
        loading: false,
        loadError: '',
        ...this.getDeviceViewState(device, this.data.activeAlert),
      })
      await this.loadLatestAlert()
    } catch (error: any) {
      if (this.data.pendingAction) {
        const deadline = this.data.pendingAction === 'reset'
          ? this.data.resetPendingUntil
          : this.data.pendingDeadline
        if (Date.now() >= deadline) this.clearPendingControl()
        else this.scheduleControlCheck()
      }
      this.setData({ loading: false, loadError: (error && error.message) || '设备信息加载失败' })
    }
  },

  async onControlTap() {
    const device = this.data.device
    if (!device || this.data.controlLoading || this.data.resetting) return
    if (device.state !== 'online') {
      wx.showToast({ title: device.state === 'error' ? '设备异常，无法操作' : '设备离线，无法操作', icon: 'none' })
      return
    }
    if (!device.enabled) {
      wx.showToast({ title: '设备已被管理员停用', icon: 'none' })
      return
    }
    if (this.hasActiveFault(device)) {
      wx.showToast({ title: this.getFaultReason(device) || '设备存在故障，无法操作', icon: 'none' })
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
    const { fallAlert, deviceFault } = runtimeConfig.subscribeTemplateIds
    this.logSubscriptionConfig(fallAlert, deviceFault, tmplIds)
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
      if (setting && setting.mainSwitch === false) {
        wx.showToast({
          title: '订阅消息总开关未开启，本次仍可启动检测',
          icon: 'none',
          duration: 2500,
        })
        return
      }
    } catch (error) {
      console.warn('[wechat-subscribe] get setting failed:', error)
    }

    try {
      const result = await this.requestSubscribeMessage(tmplIds)
      if (runtimeConfig.environment === 'development') {
        console.info('[wechat-subscribe] result statuses:', this.summarizeSubscribeResult(result, templateTargets))
      }
      await this.saveDetectionSubscribeResult(result, templateTargets)
      const accepted = templateTargets.some((target) => this.normalizeSubscribeStatus(result[target.templateId]) === 'accept')
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
    const { fallAlert: fallAlertTemplateId, deviceFault: deviceFaultTemplateId } = runtimeConfig.subscribeTemplateIds
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
      const status = this.normalizeSubscribeStatus(result[target.templateId])
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
    const settled = await Promise.allSettled(tasks)
    if (runtimeConfig.environment === 'development') {
      const rejectedCount = settled.filter((item) => item.status === 'rejected').length
      console.info('[wechat-subscribe] saved results:', {
        requested: targets.length,
        saved: settled.length - rejectedCount,
        failed: rejectedCount,
      })
    }
  },

  normalizeSubscribeStatus(value: unknown): WechatSubscriptionStatusValue | '' {
    const status = String(value || '')
    if (status === 'accept' || status === 'reject' || status === 'ban' || status === 'filter') {
      return status
    }
    return ''
  },

  logSubscriptionConfig(
    fallAlertTemplateId: string,
    deviceFaultTemplateId: string,
    tmplIds: string[],
  ) {
    if (runtimeConfig.environment !== 'development') return
    if (!fallAlertTemplateId) console.warn('[wechat-subscribe] fall alert template id is empty')
    if (!deviceFaultTemplateId) console.warn('[wechat-subscribe] device fault template id is empty')
    if (fallAlertTemplateId && deviceFaultTemplateId && fallAlertTemplateId === deviceFaultTemplateId) {
      console.warn('[wechat-subscribe] fall alert and device fault template ids are identical')
    }
    if (tmplIds.length !== 2) {
      console.warn('[wechat-subscribe] request template count is not 2:', tmplIds.length)
    }
    console.info('[wechat-subscribe] request templates', {
      fallAlertConfigured: Boolean(fallAlertTemplateId),
      deviceFaultConfigured: Boolean(deviceFaultTemplateId),
      templateCount: tmplIds.length,
    })
  },

  summarizeSubscribeResult(
    result: Record<string, unknown>,
    targets: SubscribeTemplateTarget[],
  ): Record<WechatSubscriptionScene, WechatSubscriptionStatusValue | ''> {
    return targets.reduce((items, target) => ({
      ...items,
      [target.scene]: this.normalizeSubscribeStatus(result[target.templateId]),
    }), {
      fall_alert: '',
      device_fault: '',
    } as Record<WechatSubscriptionScene, WechatSubscriptionStatusValue | ''>)
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
    if (!device || this.data.resetting || !this.hasActiveFault(device)) return

    const confirmed = await this.confirmResetFault()
    if (!confirmed) return

    const resetPendingUntil = Date.now() + CONTROL_TIMEOUT_MS
    this.setData({
      resetting: true,
      pendingAction: 'reset',
      pendingDeadline: 0,
      resetPendingUntil,
    })

    try {
      const result = await resetDeviceFault(device.device_name)
      if (!result.accepted) throw new Error(result.message)
      wx.showToast({ title: '复位指令已发送，等待设备确认', icon: 'none' })
      this.scheduleControlCheck()
    } catch (error: any) {
      this.clearPendingControl()
      wx.showToast({ title: (error && error.message) || '复位指令发送失败', icon: 'none' })
      await this.loadDevice()
    }
  },

  confirmResetFault(): Promise<boolean> {
    return new Promise((resolve) => {
      wx.showModal({
        title: '确认复位设备',
        content: '请确认已经检查设备供电和硬件连接。复位后设备将尝试退出故障状态。',
        confirmText: '确认复位',
        cancelText: '取消',
        success: (result) => resolve(Boolean(result.confirm)),
        fail: () => resolve(false),
      })
    })
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
    this.setData({
      controlLoading: false,
      resetting: false,
      pendingAction: '',
      pendingDeadline: 0,
      resetPendingUntil: 0,
    })
  },

  onBack() { wx.navigateBack() },
  onRetry() { this.loadDevice() },

  getDeviceViewState(device: DeviceDetail, alert: FallEvent | null) {
    const stateText = { online: '在线', offline: '离线', error: '异常' }[device.state]
    const detectionText = { idle: '等待启动', starting: '正在启动', running: '检测运行中', stopping: '正在停止' }[device.detection_state]
    const hasActiveFault = this.hasActiveFault(device)
    const faultReasonText = this.getFaultReason(device)
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
    } else if (hasActiveFault) {
      lightState = 'error'
      lightTitle = '设备异常'
      lightDesc = faultReasonText || '请检查设备连接和供电状态'
    }

    return {
      stateText,
      detectionText,
      qualityLabel: qualityText(device.network_quality),
      lastSeenText: this.formatDate(device.last_seen_at),
      lightState,
      lightTitle,
      lightDesc,
      hasActiveFault,
      faultReasonText,
    }
  },

  hasActiveFault(device: DeviceDetail): boolean {
    return (
      device.state === 'error' ||
      device.runtime.state === 'fault' ||
      Boolean(device.fault.code) ||
      Boolean(device.fault.message) ||
      Boolean(device.fault_message)
    )
  },

  getFaultReason(device: DeviceDetail): string {
    return device.fault.message || device.fault_message || '未收到CSI数据，请检查供电'
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
