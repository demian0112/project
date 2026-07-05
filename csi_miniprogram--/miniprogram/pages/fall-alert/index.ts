import { FallEvent } from '../../models/domain'
import { getFallEvents, qualityText, updateFallEvent } from '../../utils/api'

Page({
  data: {
    statusBarHeight: 44,
    alertId: '',
    alert: null as FallEvent | null,
    loading: true,
    loadError: '',
    confirming: false,
    qualityLabel: '暂无数据',
    occurredText: '',
    resultText: '',
  },

  onLoad(options: Record<string, string>) {
    const system = wx.getSystemInfoSync()
    this.setData({ statusBarHeight: system.statusBarHeight || 44, alertId: decodeURIComponent(options.id || '') })
    this.loadAlert()
  },

  async loadAlert() {
    try {
      if (!this.data.alertId) throw new Error('缺少告警标识')
      const alerts = await getFallEvents(100)
      const alert = alerts.find((item) => String(item.id) === this.data.alertId)
      if (!alert) throw new Error('未找到这条跌倒记录')
      this.setData({
        alert,
        loading: false,
        loadError: '',
        qualityLabel: qualityText(alert.network_quality),
        occurredText: this.formatDate(alert.occurred_at),
        resultText: alert.result === 1 ? '检测到跌倒' : '未知结果',
      })
    } catch (error: any) {
      this.setData({ loading: false, loadError: (error && error.message) || '告警加载失败' })
    }
  },

  async onConfirmSafe() {
    const alert = this.data.alert
    if (!alert || this.data.confirming) return
    this.setData({ confirming: true })
    try {
      await updateFallEvent(alert.id, 'confirmed')
      wx.showToast({ title: '已确认安全', icon: 'success' })
      setTimeout(() => wx.switchTab({ url: '/pages/index/index' }), 500)
    } catch (error: any) {
      wx.showToast({ title: (error && error.message) || '提交失败，请重试', icon: 'none' })
    } finally {
      this.setData({ confirming: false })
    }
  },

  onContactFamily() {
    wx.showModal({
      title: '联系家人',
      content: '紧急联系人能力将在联系人接口接入后启用。当前请使用微信或电话直接联系家人。',
      showCancel: false,
    })
  },

  onBackHome() { wx.switchTab({ url: '/pages/index/index' }) },
  onBack() { wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/index/index' }) }) },
  onRetry() { this.setData({ loading: true }); this.loadAlert() },

  formatDate(value: string): string {
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return value
    const pad = (number: number) => String(number).padStart(2, '0')
    return `${date.getFullYear()}年${pad(date.getMonth() + 1)}月${pad(date.getDate())}日 ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  },
})
