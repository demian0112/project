import { runtimeConfig, TOKEN_STORAGE_KEY } from '../config/env'
import { RealtimeEvent } from '../models/domain'

type Listener = (event: RealtimeEvent) => void
type ConnectionListener = () => void

let socket: WechatMiniprogram.SocketTask | null = null
let listeners: Listener[] = []
let connectionListeners: ConnectionListener[] = []
let reconnectTimer: number | null = null
let retryCount = 0
let manuallyClosed = false

function subscribe(listener: Listener): () => void {
  listeners.push(listener)
  return () => { listeners = listeners.filter((item) => item !== listener) }
}

function subscribeConnection(listener: ConnectionListener): () => void {
  connectionListeners.push(listener)
  return () => { connectionListeners = connectionListeners.filter((item) => item !== listener) }
}

function connect(): void {
  if (!runtimeConfig.wsBaseUrl || socket) return
  const token = wx.getStorageSync(TOKEN_STORAGE_KEY) as string
  if (!token) return
  manuallyClosed = false
  const separator = runtimeConfig.wsBaseUrl.indexOf('?') >= 0 ? '&' : '?'
  const url = token ? `${runtimeConfig.wsBaseUrl}${separator}token=${encodeURIComponent(token)}` : runtimeConfig.wsBaseUrl
  socket = wx.connectSocket({ url })
  socket.onOpen(() => {
    retryCount = 0
    connectionListeners.forEach((listener) => listener())
  })
  socket.onMessage((message) => {
    try {
      const event = JSON.parse(String(message.data)) as RealtimeEvent
      listeners.forEach((listener) => listener(event))
    } catch (error) {
      console.error('无法解析实时事件', error)
    }
  })
  socket.onClose(() => {
    socket = null
    if (!manuallyClosed) scheduleReconnect()
  })
  socket.onError(() => {
    socket = null
    if (!manuallyClosed) scheduleReconnect()
  })
}

function close(): void {
  manuallyClosed = true
  if (reconnectTimer !== null) clearTimeout(reconnectTimer)
  reconnectTimer = null
  if (socket) socket.close({})
  socket = null
}

function scheduleReconnect(): void {
  if (reconnectTimer !== null) return
  const delay = Math.min(30000, 1000 * Math.pow(2, retryCount))
  retryCount += 1
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    connect()
  }, delay) as unknown as number
}

export const realtimeClient = { subscribe, subscribeConnection, connect, close }
