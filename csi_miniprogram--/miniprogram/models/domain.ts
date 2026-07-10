export type DeviceState = 'online' | 'offline' | 'error'
export type DetectionState = 'idle' | 'starting' | 'running' | 'stopping'
export type HardwareRuntimeState = 'booting' | 'idle' | 'uploading' | 'fault'
export type NetworkQuality = 'good' | 'fair' | 'poor' | 'unknown'
export type FallEventStatus = 'pending' | 'confirmed' | 'ignored'

export interface UserProfile {
  id: number | null
  nickname: string | null
  avatar_url: string | null
  phone: string | null
  status: string
  last_login_at: string | null
}

export interface DeviceSummary {
  device_name: string
  display_name: string | null
  location: string | null
  state: DeviceState
  runtime_state?: HardwareRuntimeState
  detection_state: DetectionState
  network_quality: NetworkQuality
  last_seen_at: string | null
  fault_code?: string | null
  fault_message: string | null
  fault_template_data?: Record<string, { value: string }> | null
  fault?: {
    code: string | null
    message: string | null
    template_data?: Record<string, { value: string }> | null
  } | null
}

export interface DeviceDetail extends DeviceSummary {
  remark: string | null
  enabled: boolean
  runtime: {
    state: HardwareRuntimeState
    last_status_at: string | null
  }
  detection: {
    state: DetectionState
    session: string | null
    network_quality: NetworkQuality
    last_csi_at: string | null
  }
  fault: {
    code: string | null
    message: string | null
    template_data?: Record<string, { value: string }> | null
  }
}

export interface DeviceControlResult {
  accepted: boolean
  device_name: string
  action: 'start' | 'stop' | 'reset'
  control_state: string
  session: string | null
  message: string
}

export interface FallEvent {
  id: number | string
  device_name: string
  display_name: string | null
  location: string | null
  result: number
  occurred_at: string
  network_quality: NetworkQuality
  status: FallEventStatus
  handled_at: string | null
  remark: string | null
}

export interface RealtimeEvent<T = Record<string, unknown>> {
  event: 'connection.ready' | 'device.state.changed' | 'device.runtime.changed' | 'device.fault' | 'detection.network-quality' | 'detection.fall-result'
  device_name?: string
  server_time?: string
  data: T
}

export interface DashboardSummary {
  online_count: number
  offline_count: number
  error_count: number
  running_count: number
  today_guard_minutes: number
}
