import { DeviceSummary } from '../../models/domain'
import { getDevices } from '../../utils/api'

interface DeviceInfo {
  device_name: string
  display_name: string | null
  location: string | null
}

Page({
  data: {
    statusBarHeight: 44,
    loading: true,
    loadError: '',
    devices: [] as DeviceInfo[],
  },

  onLoad() {
    const system = wx.getSystemInfoSync()
    this.setData({ statusBarHeight: system.statusBarHeight || 44 })
    this.loadDevices()
  },

  onPullDownRefresh() {
    this.loadDevices().finally(() => wx.stopPullDownRefresh())
  },

  async loadDevices() {
    try {
      const devices = await getDevices()
      const displayDevices = devices.map((device: DeviceSummary) => ({
        device_name: device.device_name,
        display_name: device.display_name,
        location: device.location,
      }))
      this.setData({ loading: false, loadError: '', devices: displayDevices })
    } catch (error: any) {
      this.setData({
        loading: false,
        loadError: (error && error.message) || '设备信息加载失败',
      })
    }
  },

  onBack() { wx.navigateBack() },
  onRetry() { this.setData({ loading: true }); this.loadDevices() },
})
