import { DeviceSummary } from '../../models/domain'
import { getDevices, updateDeviceInfo } from '../../utils/api'

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
    editing: false,
    saving: false,
    editDeviceName: '',
    editDisplayName: '',
    editLocation: '',
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

  onEditTap(event: any) {
    const deviceName = String(event.currentTarget.dataset.name || '')
    const device = this.data.devices.find((item) => item.device_name === deviceName)
    if (!device) return
    this.setData({
      editing: true,
      editDeviceName: device.device_name,
      editDisplayName: device.display_name || '',
      editLocation: device.location || '',
    })
  },

  onDisplayNameInput(event: any) {
    this.setData({ editDisplayName: String(event.detail.value || '') })
  },

  onLocationInput(event: any) {
    this.setData({ editLocation: String(event.detail.value || '') })
  },

  onCancelEdit() {
    if (this.data.saving) return
    this.setData({
      editing: false,
      editDeviceName: '',
      editDisplayName: '',
      editLocation: '',
    })
  },

  noop() {},

  async onSaveEdit() {
    if (this.data.saving) return
    const deviceName = this.data.editDeviceName
    const displayName = this.data.editDisplayName.trim()
    const location = this.data.editLocation.trim()
    if (!displayName) {
      wx.showToast({ title: '设备显示名不能为空', icon: 'none' })
      return
    }
    if (displayName.length > 64) {
      wx.showToast({ title: '设备显示名不能超过 64 个字符', icon: 'none' })
      return
    }
    if (location.length > 128) {
      wx.showToast({ title: '安装位置不能超过 128 个字符', icon: 'none' })
      return
    }

    this.setData({ saving: true })
    try {
      const updated = await updateDeviceInfo(deviceName, {
        display_name: displayName,
        location: location || null,
      })
      const devices = this.data.devices.map((item) => (
        item.device_name === deviceName
          ? {
            ...item,
            display_name: updated.display_name,
            location: updated.location,
          }
          : item
      ))
      this.setData({
        devices,
        editing: false,
        saving: false,
        editDeviceName: '',
        editDisplayName: '',
        editLocation: '',
      })
      wx.showToast({ title: '设备信息已保存', icon: 'success' })
    } catch (error: any) {
      this.setData({ saving: false })
      wx.showToast({ title: (error && error.message) || '保存失败', icon: 'none' })
    }
  },
})
