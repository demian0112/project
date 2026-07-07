/** API 契约层：只对接最新版 Flask /api/v1，页面数据全部来自后端。 */
import { runtimeConfig, TOKEN_STORAGE_KEY } from '../config/env'
import {
  DeviceControlResult,
  DeviceDetail,
  DeviceSummary,
  FallEvent,
  FallEventStatus,
  UserProfile,
} from '../models/domain'
import { resetAuth, waitForAuth } from '../services/auth'

interface ListResponse<T> { items: T[] }

export interface LoginResponse {
  access_token: string
  expires_in: number
  user: {
    id: number
    nickname: string | null
    avatar_url: string | null
    is_new_user: boolean
  }
}

interface RawDeviceDetail {
  device_name: string
  display_name: string | null
  location: string | null
  remark: string | null
  state: DeviceDetail['state']
  enabled: boolean
  last_seen_at: string | null
  runtime: DeviceDetail['runtime']
  detection: DeviceDetail['detection']
  fault: DeviceDetail['fault']
}

export interface DeviceProfileUpdate {
  display_name?: string
  location?: string | null
}

export type WechatSubscriptionStatusValue = 'accept' | 'reject' | 'ban' | 'filter'

export interface WechatSubscriptionPayload {
  scene: 'fall_alert'
  template_id: string
  status: WechatSubscriptionStatusValue
}

export interface WechatSubscriptionStatus {
  ok: boolean
  enabled: boolean
  scene: 'fall_alert'
  template_id: string
  status: WechatSubscriptionStatusValue | ''
  remaining_count: number
  last_subscribed_at?: string | null
}

export interface ApiError extends Error {
  code: string
  statusCode: number
}

function createApiError(message: string, code = 'REQUEST_FAILED', statusCode = 0): ApiError {
  const error = new Error(message) as ApiError
  error.name = 'ApiError'
  error.code = code
  error.statusCode = statusCode
  return error
}

async function request<T>(path: string, options: Partial<WechatMiniprogram.RequestOption> = {}): Promise<T> {
  if (!runtimeConfig.apiBaseUrl) {
    throw createApiError('尚未配置后端服务地址', 'BACKEND_NOT_CONFIGURED')
  }
  if (path !== '/api/v1/auth/wechat-login') {
    const authenticated = await waitForAuth()
    if (!authenticated) throw createApiError('请先在我的页面登录', 'AUTH_REQUIRED', 401)
  }
  const token = wx.getStorageSync(TOKEN_STORAGE_KEY) as string
  return new Promise((resolve, reject) => {
    wx.request({
      ...options,
      url: `${runtimeConfig.apiBaseUrl}${path}`,
      timeout: runtimeConfig.requestTimeout,
      header: {
        'content-type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.header || {}),
      },
      success: (response) => {
        const body = response.data as any
        if (response.statusCode >= 200 && response.statusCode < 300) {
          resolve(body as T)
          return
        }
        if (response.statusCode === 401) {
          wx.removeStorageSync(TOKEN_STORAGE_KEY)
          resetAuth()
          const app = getApp<IAppOption & { finishAuth?: (success: boolean, message?: string) => void }>()
          if (typeof app.finishAuth === 'function') app.finishAuth(false, '登录状态已过期，请重新登录')
        }
        reject(createApiError(
          (body && body.message) || `请求失败（${response.statusCode}）`,
          (body && body.error) || 'REQUEST_FAILED',
          response.statusCode,
        ))
      },
      fail: () => reject(createApiError('网络连接失败，请稍后重试', 'NETWORK_ERROR')),
    })
  })
}

export function loginWithWechat(code: string, createIfMissing = true): Promise<LoginResponse> {
  return request<LoginResponse>('/api/v1/auth/wechat-login', {
    method: 'POST',
    data: { code, create_if_missing: createIfMissing },
  })
}

export function getCurrentUser(): Promise<UserProfile> {
  return request<UserProfile>('/api/v1/me')
}

export function updateCurrentUserProfile(profile: { nickname?: string; avatar_url?: string }): Promise<UserProfile> {
  return request<UserProfile>('/api/v1/me/profile', {
    method: 'PATCH' as any,
    data: profile,
  })
}

export function updateCurrentUserPhone(code: string): Promise<UserProfile> {
  return request<UserProfile>('/api/v1/me/phone', {
    method: 'POST',
    data: { code },
  })
}

export function registerWechatSubscription(
  payload: WechatSubscriptionPayload,
): Promise<WechatSubscriptionStatus> {
  return request<WechatSubscriptionStatus>('/api/v1/wechat/subscriptions', {
    method: 'POST',
    data: payload,
  })
}

export function getWechatSubscriptionStatus(): Promise<WechatSubscriptionStatus> {
  return request<WechatSubscriptionStatus>('/api/v1/wechat/subscriptions')
}

export async function getDevices(): Promise<DeviceSummary[]> {
  const response = await request<ListResponse<DeviceSummary>>('/api/v1/devices')
  return response.items
}

export async function getDeviceDetail(deviceName: string): Promise<DeviceDetail> {
  const response = await request<RawDeviceDetail>(`/api/v1/devices/${encodeURIComponent(deviceName)}`)
  return normalizeDeviceDetail(response)
}

export async function updateDeviceInfo(deviceName: string, profile: DeviceProfileUpdate): Promise<DeviceDetail> {
  const response = await request<RawDeviceDetail>(`/api/v1/devices/${encodeURIComponent(deviceName)}`, {
    method: 'PATCH' as any,
    data: profile,
  })
  return normalizeDeviceDetail(response)
}

function normalizeDeviceDetail(response: RawDeviceDetail): DeviceDetail {
  return {
    device_name: response.device_name,
    display_name: response.display_name,
    location: response.location,
    remark: response.remark,
    state: response.state,
    enabled: response.enabled,
    detection_state: response.detection.state,
    network_quality: response.detection.network_quality,
    last_seen_at: response.last_seen_at,
    fault_message: response.fault.message,
    runtime: response.runtime,
    detection: response.detection,
    fault: response.fault,
  }
}

export function controlDevice(deviceName: string, action: 'start' | 'stop'): Promise<DeviceControlResult> {
  return request<DeviceControlResult>(`/api/v1/devices/${encodeURIComponent(deviceName)}/control`, {
    method: 'POST',
    header: { 'Idempotency-Key': createRequestId() },
    data: { action },
  })
}

export async function getFallEvents(limit = 20): Promise<FallEvent[]> {
  const response = await request<ListResponse<FallEvent>>(`/api/v1/fall-events?limit=${limit}`)
  return response.items
}

export function updateFallEvent(id: number | string, status: FallEventStatus): Promise<{ success: boolean }> {
  return request<{ success: boolean }>(`/api/v1/fall-events/${encodeURIComponent(String(id))}`, {
    method: 'PATCH' as any,
    data: { status },
  })
}

export function qualityText(quality: DeviceSummary['network_quality']): string {
  return { good: '信号良好', fair: '信号一般', poor: '信号较差', unknown: '暂无数据' }[quality]
}

function createRequestId(): string {
  return `${Date.now()}-${Math.random().toString(16).slice(2)}-${Math.random().toString(16).slice(2)}`
}
