/// <reference path="./types/index.d.ts" />

interface IAppOption {
  globalData: {
    token: string
    user: import('../miniprogram/models/domain').UserProfile | null
    devices: import('../miniprogram/models/domain').DeviceSummary[]
    fallEvents: import('../miniprogram/models/domain').FallEvent[]
    authReady: boolean
    bootstrapReady: boolean
    bootstrapError: string
    isNewUser: boolean
  }
  userInfoReadyCallback?: WechatMiniprogram.GetUserInfoSuccessCallback
}
