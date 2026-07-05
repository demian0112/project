import { DeviceSummary, UserProfile } from '../../models/domain'
import { getCurrentUser, getDevices, updateCurrentUserPhone } from '../../utils/api'

Page({
  data: {
    statusBarHeight: 44,
    loading: true,
    authenticated: false,
    loggingIn: false,
    phoneLoading: false,
    loadError: '',
    user: null as UserProfile | null,
    devices: [] as DeviceSummary[],
    avatarText: '',
    avatarUrl: '',
    userName: '',
    accountText: '',
    phoneText: '',
    lastLoginText: '',
    totalDevices: 0,
    showPhonePrompt: false,
  },

  onLoad() {
    const system = wx.getSystemInfoSync()
    this.setData({ statusBarHeight: system.statusBarHeight || 44 })
  },

  onShow() {
    this.selectTab()
    const app = getApp<IAppOption>()
    if (app.globalData.authReady) this.loadProfile()
    else this.showLoginEntry()
  },

  async loadProfile() {
    const app = getApp<IAppOption>()
    if (!app.globalData.authReady) {
      this.showLoginEntry()
      return
    }
    try {
      const [user, devices] = await Promise.all([getCurrentUser(), getDevices()])
      const nickname = user.nickname || ''
      app.globalData.user = user
      app.globalData.devices = devices
      this.setData({
        user,
        devices,
        authenticated: true,
        loading: false,
        loadError: '',
        avatarText: nickname ? nickname.slice(0, 1) : '',
        avatarUrl: user.avatar_url || '',
        userName: nickname,
        accountText: user.status === 'active' ? '账号正常' : '账号已停用',
        phoneText: user.phone ? `手机号 ${user.phone}` : '',
        lastLoginText: this.formatDate(user.last_login_at),
        totalDevices: devices.length,
        showPhonePrompt: !user.phone,
      })
    } catch (error: any) {
      console.error('个人中心加载失败', error)
      const app = getApp<IAppOption>()
      this.setData({
        authenticated: app.globalData.authReady,
        loading: false,
        loadError: app.globalData.bootstrapError || (error && error.message) || '用户资料加载失败',
        accountText: '',
        phoneText: '',
        userName: '',
        avatarUrl: '',
        avatarText: '',
        devices: [],
        totalDevices: 0,
        showPhonePrompt: false,
      })
    }
  },

  async onLogin() {
    const app = getApp<IAppOption & { login: (createIfMissing?: boolean) => Promise<boolean> }>()
    if (typeof app.login !== 'function' || this.data.loggingIn) return
    this.setData({ loggingIn: true, loadError: '' })
    const success = await app.login(true)
    this.setData({ loggingIn: false })
    if (success) {
      this.setData({ loading: true })
      await this.loadProfile()
    } else {
      this.showLoginEntry(app.globalData.bootstrapError || '微信登录失败，请重试')
    }
  },

  async onRetry() {
    const app = getApp<IAppOption & { login: (createIfMissing?: boolean) => Promise<boolean> }>()
    if (!app.globalData.authReady && typeof app.login === 'function') {
      await this.onLogin()
      return
    }
    this.setData({ loading: true, loadError: '' })
    this.loadProfile()
  },

  async onGetPhoneNumber(event: WechatMiniprogram.ButtonGetPhoneNumber) {
    const detail = event.detail as WechatMiniprogram.ButtonGetPhoneNumber['detail'] & { code?: string }
    if (!detail || detail.errMsg.indexOf('ok') < 0 || !detail.code) {
      wx.showToast({ title: '未获得手机号授权', icon: 'none' })
      return
    }
    if (this.data.phoneLoading) return
    this.setData({ phoneLoading: true })
    try {
      const user = await updateCurrentUserPhone(detail.code)
      const app = getApp<IAppOption>()
      app.globalData.user = user
      this.setData({
        user,
        phoneText: user.phone ? `手机号 ${user.phone}` : '',
        showPhonePrompt: !user.phone,
      })
      wx.showToast({ title: '手机号已绑定', icon: 'success' })
    } catch (error: any) {
      wx.showToast({ title: (error && error.message) || '手机号绑定失败', icon: 'none' })
    } finally {
      this.setData({ phoneLoading: false })
    }
  },

  onTapDeviceManagement() {
    wx.navigateTo({ url: '/pages/device-management/index' })
  },

  formatDate(value: string | null): string {
    if (!value) return '暂无记录'
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return value
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
  },

  showLoginEntry(message = '') {
    this.setData({
      authenticated: false,
      loading: false,
      loadError: message,
      user: null,
      devices: [],
      avatarText: '',
      avatarUrl: '',
      userName: '',
      accountText: '',
      phoneText: '',
      lastLoginText: '',
      totalDevices: 0,
      showPhonePrompt: false,
    })
  },

  selectTab() {
    if (typeof this.getTabBar === 'function') {
      const tabBar = this.getTabBar()
      if (tabBar) tabBar.setData({ selected: 1 })
    }
  },
})
