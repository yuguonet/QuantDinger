/**
 * KlineChart 视口 / 刻度纯函数（无 DOM、无图表实例依赖）。
 *
 * 为什么单独成文件：本组件的视觉定位此前全部内联在 KlineChart.vue（8000+ 行）的
 * 副作用里，改一处要顺着三条路径读，且无法在浏览器外验证。而"进入态跨度、右缘锚定、
 * 极坐标冗余、分时网格刻度"本质都是纯算术 —— 抽到这里后可脱离浏览器直接把边界情形
 * 跑一遍（含 0 宽绘图区、单根数据、跨午休时段等）。
 *
 * 口径唯一（与 klinecharts 9.8 TimeScaleStore 一致）：
 *   - barSpace：单根柱占用的像素宽度，合法域 1 ~ 50（越界会被 setBarSpace 静默拒绝）
 *   - diff：右缘空位（bar 数）= 最后一根柱距绘图区右边缘的柱数；diff=0 表示最新柱贴右缘
 *   - to   = round(diff + count + 0.5)，超过 count 时截断为 count（realTo 不截断）
 *   - from = round(to - 绘图区宽度 / barSpace) - 1，下限 0
 */

/** 进入态默认可见柱数：初始进入展示「最新 120 根」 */
export const ENTRY_VISIBLE_BARS = 120
/** 进入态右缘呼吸位（bar 数）：最新柱不贴死右轴（与换股定位同口径，留出最后一根的高低点） */
export const ENTRY_BREATH_BARS = 2
/** barSpace 合法域（与库 BarSpaceLimitConstants 一致） */
export const BAR_SPACE_MIN = 1
export const BAR_SPACE_MAX = 50

/** 分时背景网格间隔（分钟）：30 分钟一根 */
export const MINUTE_GRID_INTERVAL_MIN = 30
/** 分时网格刻度最小像素间距：不足时隔位稀疏，避免时间标签互相压字 */
export const MINUTE_X_TICK_MIN_GAP_PX = 44

/**
 * 分时极坐标的上下冗余比例（相对涨跌停幅度）。
 *
 * 为什么必须留冗余：极坐标把 Y 轴锁成「恰好 昨收 ± 涨跌停%」，±limit 那一刻度正好落在
 * pane 上下边缘，被库 YAxisImp.optimalTicks 的 `y > textHeight && y < height - textHeight`
 * 过滤掉 —— 表现为「跌停/涨停线（刻度线 + 标签）看不到」。
 * 放宽 8% 后，±limit 刻度回到 pane 内部（约 1000px 高的 pane 上离边缘 ~37px）；
 * 昨收仍在正中（范围对昨收对称），只是上下各多出一点余量。
 */
export const MINUTE_POLAR_PADDING = 0.12

/** 收敛到库的 barSpace 合法域；非法入参返回 0，调用方应放弃本次定位 */
export const clampBarSpace = (value) => {
  const v = Number(value)
  if (!Number.isFinite(v) || !(v > 0)) return 0
  return Math.max(BAR_SPACE_MIN, Math.min(BAR_SPACE_MAX, v))
}

/**
 * 进入态 barSpace：让「最新 visibleBars 根」+ 右侧 breathBars 根呼吸位正好铺满绘图区。
 *
 * 数据不足 visibleBars 根时按实际根数铺满（否则左侧会留出大片空白，看起来像"没数据"）。
 * @param {{plotWidth:number, barCount:number, visibleBars?:number, breathBars?:number}} o
 * @returns {number} 0 = 入参不可用
 */
export const entryBarSpace = (o) => {
  const { plotWidth, barCount, visibleBars = ENTRY_VISIBLE_BARS, breathBars = ENTRY_BREATH_BARS } = o || {}
  const width = Number(plotWidth)
  const count = Math.floor(Number(barCount))
  if (!(width > 0) || !(count > 0)) return 0
  const wantBars = Math.max(1, Math.floor(Number(visibleBars) || 0))
  const shown = Math.min(count, wantBars)
  const breath = Math.max(0, Math.floor(Number(breathBars) || 0))
  return clampBarSpace(width / (shown + breath))
}

/**
 * 「想留 N 根右缘空位」换算成库需要的像素值。
 *
 * setOffsetRightDistance 收的是像素，内部会立即重算 diff = px / barSpace，
 * 因此必须在 setBarSpace 之后、用当前 barSpace 现算，否则空位会随跨度变化而漂移。
 */
export const rightOffsetPx = (barSpace, breathBars = ENTRY_BREATH_BARS) => {
  const bs = Number(barSpace)
  const bars = Math.max(0, Number(breathBars) || 0)
  if (!(bs > 0)) return 0
  return bars * bs
}

/**
 * 由可见范围反解当前右缘空位（bar 数）—— 用于校验"是否已锚定到最新"。
 * 库口径 realTo = round(diff + count + 0.5)（未被 count 截断），故 diff ≈ realTo - count - 0.5。
 * 缺 realTo 时退化为 to（to 被 count 截断，只能作为近似）。
 */
export const rightSideDiffBars = (visibleRange, barCount) => {
  const count = Number(barCount)
  if (!(count > 0) || !visibleRange) return 0
  const realTo = Number(visibleRange.realTo)
  if (Number.isFinite(realTo)) return realTo - count - 0.5
  const to = Number(visibleRange.to)
  if (Number.isFinite(to)) return to - count - 0.5
  return 0
}

/**
 * 分时极坐标半区间（价格单位）：昨收 ± 涨跌停% × (1 + padding)。
 * @returns {number} 0 = 入参不可用（调用方应放弃极坐标锁定）
 */
export const polarHalfPrice = (prevClose, limitPct, padding = MINUTE_POLAR_PADDING) => {
  const pc = Number(prevClose)
  const limit = Number(limitPct)
  if (!(pc > 0) || !(limit > 0)) return 0
  const pad = Number(padding) > 0 ? Number(padding) : 0
  return pc * (limit / 100) * (1 + pad)
}

/** 本地 HH:mm（与组件内其它时间处理同口径：一律用浏览器本地时区） */
export const hhmm = (timestamp) => {
  const d = new Date(Number(timestamp))
  if (!Number.isFinite(d.getTime())) return ''
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/**
 * 分时 X 轴刻度（30 分钟一根）：取时间戳落在 intervalMin 分钟边界上的真实柱。
 *
 * 为什么按时间戳而不是"索引步长"：1m 数据可能缺柱，索引步长会随时间漂移；
 * 而"整 30 分钟"是交易时段本身的性质（09:30 / 10:00 / … / 15:00），跨午休天然跳过。
 * 秒级噪声用「就近取整到分钟」吸收（后端时间戳可能是秒级或带毫秒）。
 *
 * @param {{bars:Array, session?:{start:number,end:number,morningEnd?:number,afternoonStart?:number},
 *          intervalMin?:number, barSpace?:number, minGapPx?:number}} o
 *        session 用 hhmm 数值口径（hour*100+minute）过滤；给了 morningEnd/afternoonStart
 *        时午休区间一并剔除（防住脏数据里冒出的 12:00 之类非交易时段柱）
 * @returns {Array<{index:number, timestamp:number, text:string}>}
 */
export const minuteGridMarks = (o) => {
  const { bars, session, intervalMin = MINUTE_GRID_INTERVAL_MIN, barSpace = 0, minGapPx = MINUTE_X_TICK_MIN_GAP_PX } = o || {}
  const list = Array.isArray(bars) ? bars : []
  const interval = Math.max(1, Math.floor(Number(intervalMin) || MINUTE_GRID_INTERVAL_MIN))
  if (list.length === 0) return []
  const start = session && Number.isFinite(Number(session.start)) ? Number(session.start) : -Infinity
  const end = session && Number.isFinite(Number(session.end)) ? Number(session.end) : Infinity
  const morningEnd = session && Number.isFinite(Number(session.morningEnd)) ? Number(session.morningEnd) : null
  const afternoonStart = session && Number.isFinite(Number(session.afternoonStart)) ? Number(session.afternoonStart) : null
  const marks = []
  for (let i = 0; i < list.length; i++) {
    const b = list[i]
    const ts = Number(b && b.timestamp)
    if (!Number.isFinite(ts) || ts <= 0) continue
    // 就近取整到分钟：吸收秒级/毫秒级噪声，再判断是否落在间隔边界
    const rounded = Math.round(ts / 60000) * 60000
    const d = new Date(rounded)
    if (d.getMinutes() % interval !== 0) continue
    const t = d.getHours() * 100 + d.getMinutes()
    if (t < start || t > end) continue
    // 午休区间剔除（仅当时段定义给出了上午收盘/下午开盘）
    if (morningEnd != null && afternoonStart != null && t > morningEnd && t < afternoonStart) continue
    marks.push({ index: i, timestamp: rounded, text: hhmm(rounded) })
  }
  // 窄屏稀掉：像素间距不足时按需隔位取（保留第一根），保证标签不叠字
  const bs = Number(barSpace)
  if (marks.length > 1 && bs > 0) {
    const gapBars = interval * bs
    const step = gapBars > 0 ? Math.ceil(minGapPx / gapBars) : 1
    if (step > 1) return marks.filter((_, i) => i % step === 0)
  }
  return marks
}
