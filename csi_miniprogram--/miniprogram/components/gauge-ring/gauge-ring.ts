/**
 * 环形仪表盘组件。
 *
 * 用 Canvas 2D 绘制弧形圆环，替代 WXSS 不支持的 conic-gradient。
 * 中心区域通过 slot 插入内容（图标、文字等）。
 */

const START_ANGLE = (220 - 90) * (Math.PI / 180)   // CSS 220° → Canvas 2.268 rad (~7:20)
const END_ANGLE = (500 - 90) * (Math.PI / 180)      // CSS 500° → Canvas 7.155 rad (~4:40)
const ARC_SPAN = END_ANGLE - START_ANGLE             // ~4.887 rad = 280°

Component({
  properties: {
    /** 0-1，弧形填充比例 */
    progress: {
      type: Number,
      value: 0.85,
    },
    /** 前景弧颜色 */
    color: {
      type: String,
      value: '#1aa879',
    },
    /** 背景弧颜色 */
    bgColor: {
      type: String,
      value: '#edf2ef',
    },
    /** 弧线宽度 (px) */
    lineWidth: {
      type: Number,
      value: 9,
    },
    /** Canvas 逻辑尺寸 (px) */
    size: {
      type: Number,
      value: 194,
    },
  },

  data: {
    _ctx: null as any,
    _canvas: null as any,
    _dpr: 2,
  },

  lifetimes: {
    ready() {
      this._initCanvas()
    },
  },

  observers: {
    'progress, color, bgColor, lineWidth'() {
      this._draw()
    },
  },

  methods: {
    _initCanvas() {
      const query = this.createSelectorQuery()
      query.select('#gauge-canvas')
        .fields({ node: true, size: true })
        .exec((res: any[]) => {
          if (!res || !res[0] || !res[0].node) return
          const canvas = res[0].node
          const ctx = canvas.getContext('2d')
          const dpr = (wx.getSystemInfoSync().pixelRatio) || 2
          const size = this.properties.size
          canvas.width = size * dpr
          canvas.height = size * dpr
          ctx.scale(dpr, dpr)
          this.data._ctx = ctx
          this.data._canvas = canvas
          this.data._dpr = dpr
          this._draw()
        })
    },

    _draw() {
      const ctx = this.data._ctx
      if (!ctx) return

      const { size, progress, color, bgColor, lineWidth } = this.properties
      const cx = size / 2
      const cy = size / 2
      const radius = (size - lineWidth) / 2

      ctx.clearRect(0, 0, size, size)

      // 背景弧（灰色，完整跨度）
      ctx.beginPath()
      ctx.arc(cx, cy, radius, START_ANGLE, END_ANGLE, false)
      ctx.strokeStyle = bgColor
      ctx.lineWidth = lineWidth
      ctx.lineCap = 'round'
      ctx.stroke()

      // 前景弧（彩色，按 progress 比例）
      const fgEnd = START_ANGLE + ARC_SPAN * Math.min(1, Math.max(0, progress))
      ctx.beginPath()
      ctx.arc(cx, cy, radius, START_ANGLE, fgEnd, false)
      ctx.strokeStyle = color
      ctx.lineWidth = lineWidth
      ctx.lineCap = 'round'
      ctx.stroke()
    },
  },
})
