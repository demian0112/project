import { TOKEN_STORAGE_KEY } from './config/env'
import { getCurrentUser, getDevices, getFallEvents, loginWithWechat } from './utils/api'
import { realtimeClient } from './services/realtime'
import { markAuthReady, resetAuth } from './services/auth'

App<IAppOption>({
  globalData: {
    token: '',
    user: null,
    devices: [],
    fallEvents: [],
    authReady: false,
    bootstrapReady: false,
    bootstrapError: '',
    isNewUser: false,
  },

  onLaunch() {
    this.clearBusinessData()
    this.restoreSession()
  },

  onShow() {
    if (this.globalData.authReady) {
      this.bootstrapBusinessData()
      realtimeClient.connect()
    }
  },

  async restoreSession() {
    if ((this as any)._restoreInProgress) return
    ;(this as any)._restoreInProgress = true
    const token = wx.getStorageSync(TOKEN_STORAGE_KEY) as string
    try {
      if (token) {
        this.globalData.token = token
        this.globalData.authReady = true
        markAuthReady(true)
        const restored = await this.bootstrapBusinessData()
        if (restored) {
          realtimeClient.connect()
          return
        }
      }
      // code2Session is idempotent by openid: first use creates the user,
      // later launches update last_login_at without creating duplicates.
      if (!this.globalData.authReady) await this.login(true)
    } finally {
      ;(this as any)._restoreInProgress = false
    }
  },

  async login(createIfMissing = true): Promise<boolean> {
    if ((this as any)._loginInProgress) return false
    ;(this as any)._loginInProgress = true
    realtimeClient.close()
    resetAuth()
    try {
      const code = await this.getWechatLoginCode()
      const response = await loginWithWechat(code, createIfMissing)
      wx.setStorageSync(TOKEN_STORAGE_KEY, response.access_token)
      this.globalData.token = response.access_token
      this.globalData.isNewUser = response.user.is_new_user
      this.globalData.authReady = true
      markAuthReady(true)
      await this.bootstrapBusinessData()
      realtimeClient.connect()
      return true
    } catch (error: any) {
      const isUnregistered = error && error.code === 'USER_NOT_REGISTERED'
      this.finishAuth(false, isUnregistered ? '' : (error && error.message) || '微信登录失败')
      return false
    } finally {
      ;(this as any)._loginInProgress = false
    }
  },

  getWechatLoginCode(): Promise<string> {
    return new Promise((resolve, reject) => {
      wx.login({
        success: ({ code }) => {
          if (code) resolve(code)
          else reject(new Error('微信登录未返回有效凭证'))
        },
        fail: () => reject(new Error('无法调用微信登录')),
      })
    })
  },

  async bootstrapBusinessData(): Promise<boolean> {
    if (!this.globalData.authReady || (this as any)._bootstrapInProgress) return false
    ;(this as any)._bootstrapInProgress = true
    this.globalData.bootstrapReady = false
    this.globalData.bootstrapError = ''
    try {
      const [user, devices, fallEvents] = await Promise.all([
        getCurrentUser(),
        getDevices(),
        getFallEvents(20),
      ])
      this.globalData.user = user
      this.globalData.devices = devices
      this.globalData.fallEvents = fallEvents
      this.globalData.bootstrapReady = true
      return true
    } catch (error: any) {
      this.clearBusinessData()
      this.globalData.bootstrapError = (error && error.message) || '业务数据加载失败'
      return false
    } finally {
      ;(this as any)._bootstrapInProgress = false
    }
  },

  finishAuth(success: boolean, message = '') {
    this.globalData.authReady = success
    markAuthReady(success)
    if (!success) {
      wx.removeStorageSync(TOKEN_STORAGE_KEY)
      this.globalData.token = ''
      this.globalData.bootstrapError = message
      this.clearBusinessData()
    }
  },

  clearBusinessData() {
    this.globalData.user = null
    this.globalData.devices = []
    this.globalData.fallEvents = []
    this.globalData.bootstrapReady = false
  },
} as IAppOption & Record<string, any>)
