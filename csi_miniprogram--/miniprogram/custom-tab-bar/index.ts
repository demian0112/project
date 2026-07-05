Component({
  data: {
    selected: 0,
  },

  methods: {
    switchTab(e: WechatMiniprogram.TouchEvent) {
      const index = Number(e.currentTarget.dataset.index)
      const urls = ['/pages/index/index', '/pages/mine/mine']
      const url = urls[index]
      if (index !== this.data.selected) {
        wx.switchTab({ url })
      }
    },
  },
})
