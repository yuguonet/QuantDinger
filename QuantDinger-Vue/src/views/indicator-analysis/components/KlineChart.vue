<template>
  <div class="chart-left" :class="{ 'theme-dark': chartTheme === 'dark' }">
    <div class="chart-wrapper">
      <!-- 画线工具工具栏 -->
      <div class="drawing-toolbar">
        <a-tooltip
          v-for="tool in drawingTools"
          :key="tool.name"
          :title="tool.title"
          placement="right"
        >
          <div
            class="drawing-tool-btn"
            :class="{ active: activeDrawingTool === tool.name }"
            @click="selectDrawingTool(tool.name)"
          >
            <a-icon :type="tool.icon" />
          </div>
        </a-tooltip>
        <a-divider type="vertical" />
        <a-tooltip :title="$t('dashboard.indicator.drawing.clearAll')" placement="right">
          <div class="drawing-tool-btn" @click="clearAllDrawings">
            <a-icon type="delete" />
          </div>
        </a-tooltip>
      </div>
      <!-- 图表内容区域 -->
      <div class="chart-content-area">
        <!-- 指标工具栏（外部管理时可通过 showIndicatorBar 隐藏） -->
        <div v-if="showIndicatorBar" class="indicator-toolbar">
          <div
            v-for="indicator in indicatorButtons"
            :key="indicator.id"
            class="indicator-btn"
            :class="{ active: isIndicatorActive(indicator.id) }"
            @click="handleIndicatorButtonClick(indicator)"
            :title="indicator.name"
          >
            {{ indicator.shortName }}
          </div>
        </div>
        <div v-if="showIndicatorBar && activePresetIndicators.length" class="indicator-active-bar">
          <div
            v-for="indicator in activePresetIndicators"
            :key="indicator.instanceId || indicator.id"
            class="indicator-active-chip"
            :class="{ 'indicator-active-chip--hidden': indicator.visible === false }"
          >
            <span class="indicator-active-chip__label" @click="openIndicatorEditor(indicator)">
              {{ formatIndicatorInstanceLabel(indicator) }}
            </span>
            <a-tooltip :title="indicator.visible === false ? $t('indicatorIde.editor.showIndicator') : $t('indicatorIde.editor.hideIndicator')">
              <a-icon
                :type="indicator.visible === false ? 'eye-invisible' : 'eye'"
                class="indicator-active-chip__action"
                @click.stop="toggleIndicatorVisibility(indicator)"
              />
            </a-tooltip>
            <a-tooltip :title="$t('indicatorIde.editor.settings')">
              <a-icon
                type="setting"
                class="indicator-active-chip__action"
                @click.stop="openIndicatorEditor(indicator)"
              />
            </a-tooltip>
            <a-tooltip :title="$t('indicatorIde.editor.deleteIndicator')">
              <a-icon
                type="close"
                class="indicator-active-chip__action"
                @click.stop="removeIndicatorInstance(indicator)"
              />
            </a-tooltip>
          </div>
        </div>
        <div class="kline-chart-with-pct" :class="{ 'kline-chart-with-pct--chip': showChip }">
          <!-- 左侧百分比坐标轴：padding 预留 + 绝对定位覆盖层（位置在左侧）。
               布局/观感对齐右侧金额轴：透明底、无分隔线，仅刻度短线与文字（运行时取自 yAxis 样式）；
               0%=昨收(分时)/最新收盘(其它周期)，与右侧金额轴同范围同步 -->
          <div v-if="pctAxisVisible" ref="pctAxisOverlayRef" class="pct-axis-overlay">
            <canvas ref="pctAxisCanvasRef" class="pct-axis-overlay__canvas"></canvas>
          </div>
          <div
            id="kline-chart-container"
            ref="klineContainerRef"
            class="kline-chart-container"
          ></div>
          <!-- 筹码分布覆盖层 -->
          <div v-if="showChip" ref="chipOverlayRef" class="chip-overlay" :class="{ 'chip-overlay--dark': chartTheme === 'dark' }">
            <div class="chip-overlay__header">
              <span class="chip-overlay__title">筹码分布</span>
              <span class="chip-overlay__avg" v-if="chipDataForTemplate">AVG: {{ chipDataForTemplate.avgCost ? chipDataForTemplate.avgCost.toFixed(2) : '--' }}</span>
            </div>
            <canvas
              ref="chipCanvasRef"
              class="chip-overlay__canvas"
            ></canvas>
          </div>
        </div>
        <canvas
          ref="wmCanvasRef"
          class="qd-wm-layer"
          :class="{ 'qd-wm-layer--dark': chartTheme === 'dark' }"
        ></canvas>
      </div>

      <div v-if="loading" class="chart-overlay">
        <a-spin size="large">
          <a-icon slot="indicator" type="loading" style="font-size: 24px; color: #13c2c2" spin />
        </a-spin>
      </div>

      <div v-if="error" class="chart-overlay">
        <div class="error-box">
          <a-icon type="warning" style="font-size: 24px; color: #ef5350; margin-bottom: 10px" />
          <span>{{ error }}</span>
          <a-button type="primary" size="small" ghost @click="handleRetry" style="margin-top: 12px">
            {{ $t('dashboard.indicator.retry') }}
          </a-button>
        </div>
      </div>

      <!-- Pyodide 加载失败提示 -->
      <div v-if="pyodideLoadFailed" class="chart-overlay pyodide-warning">
        <div class="warning-box">
          <a-icon type="warning" style="font-size: 32px; color: #faad14; margin-bottom: 12px" />
          <div class="warning-title">{{ $t('dashboard.indicator.warning.pyodideLoadFailed') }}</div>
          <div class="warning-desc">{{ $t('dashboard.indicator.warning.pyodideLoadFailedDesc') }}</div>
        </div>
      </div>

      <!-- 初始提示蒙版 -->
      <div v-if="!symbol && !loading && !error && !pyodideLoadFailed" class="chart-overlay initial-hint">
        <div class="hint-box">
          <a-icon type="line-chart" style="font-size: 48px; color: #1890ff; margin-bottom: 16px" />
          <div class="hint-title">{{ $t('dashboard.indicator.hint.selectSymbol') }}</div>
          <div class="hint-desc">{{ $t('dashboard.indicator.hint.selectSymbolDesc') }}</div>
        </div>
      </div>
    </div>
    <a-modal
      :visible="indicatorEditorVisible"
      :title="indicatorEditorTitle"
      :confirmLoading="indicatorEditorSaving"
      :okText="$t('common.confirm')"
      :cancelText="$t('common.cancel')"
      :wrap-class-name="indicatorEditorModalWrapClass"
      @ok="applyIndicatorEditor"
      @cancel="closeIndicatorEditor"
    >
      <div v-if="indicatorEditorSchema.length" class="indicator-editor-form">
        <div
          v-for="field in indicatorEditorSchema"
          :key="field.key"
          class="indicator-editor-field"
        >
          <div class="indicator-editor-field__label">{{ field.label }}</div>
          <a-input-number
            v-model="indicatorEditorForm[field.key]"
            :min="field.min"
            :max="field.max"
            :step="field.step || 1"
            :precision="field.precision != null ? field.precision : 0"
            style="width: 100%"
          />
          <div v-if="field.hint" class="indicator-editor-field__hint">{{ field.hint }}</div>
        </div>
        <div class="indicator-editor-field">
          <div class="indicator-editor-field__label">{{ $t('indicatorIde.editor.color') }}</div>
          <input v-model="indicatorEditorForm._styleColor" type="color" class="indicator-editor-color" />
        </div>
        <div class="indicator-editor-field">
          <div class="indicator-editor-field__label">{{ $t('indicatorIde.editor.lineWidth') }}</div>
          <a-input-number
            v-model="indicatorEditorForm._styleLineWidth"
            :min="1"
            :max="3"
            :step="1"
            :precision="0"
            style="width: 100%"
          />
        </div>
      </div>
      <div v-else class="indicator-editor-empty">{{ $t('indicatorIde.editor.noEditableParams') }}</div>
    </a-modal>
  </div>
</template>

<script>
import { ref, reactive, computed, onMounted, onBeforeUnmount, nextTick, watch, shallowRef, getCurrentInstance } from 'vue'
import { init, registerIndicator, registerOverlay } from 'klinecharts'
import request from '@/utils/request'
import { decryptCodeAuto, needsDecrypt } from '@/utils/codeDecrypt'
import ExchangeKlineWs from '@/utils/exchangeWs'
import { INDICATOR_REGISTRY } from './indicatorCalculations'

export default {
  name: 'KlineChart',
  props: {
    symbol: {
      type: String,
      default: ''
    },
    market: {
      type: String,
      default: ''
    },
    timeframe: {
      type: String,
      default: '1H'
    },
    theme: {
      type: String,
      default: 'light'
    },
    activeIndicators: {
      type: Array,
      default: () => []
    },
    realtimeEnabled: {
      type: Boolean,
      default: false
    },
    userId: {
      type: Number,
      default: null
    },
    /** 是否显示内部指标按钮栏和激活指标条（外部管理时可关闭） */
    showIndicatorBar: {
      type: Boolean,
      default: true
    },
    /** 是否显示筹码分布叠加层（置于主图右侧，Y轴右边） */
    showChip: {
      type: Boolean,
      default: true
    },
    /** 龙回头Pro 买卖点标记 [{time, side: signal/buy/sell, price, label}]；为空时组件自动按 symbol 拉取 */
    dragonMarkers: {
      type: Array,
      default: () => []
    },
    /** 昨日首板价（昨收），分时图 Y 轴中心与 0 轴线基准；缺省时自动回退获取 */
    prevClose: {
      type: Number,
      default: null
    }
  },
  emits: ['retry', 'price-change', 'load', 'indicator-toggle'],
  setup (props, { emit }) {
    // K线数据
    const klineData = shallowRef([])
    const loading = ref(false)
    const error = ref(null)
    const loadingHistory = ref(false)
    const hasMoreHistory = ref(true)
    // 用于追踪正在进行的加载请求，防止重复请求
    let loadingHistoryPromise = null
    // 标记图表是否已初始化完成，避免初始化时触发加载
    const chartInitialized = ref(false)

    // 图表实例
    const chartRef = shallowRef(null)
    const chartTheme = ref(props.theme || 'light')
    /** 分时图 Y 轴中心 / 0 轴线基准：昨日首板价（昨收） */
    const minutePrevClose = ref(null)
    /** 分时昨收解析来源标记（props / 1m推导 / 今开近似 / 最新价近似），用于诊断日志去重 */
    let _minutePcSource = ''
    /** 分时昨收诊断日志去重键（昨收值 + 来源 + 锁定范围） */
    let _minutePcLogKey = ''
    /** 分时昨收缓存归属的标的（换标的才清空，同标的刷新保留，避免锁定短暂消失造成闪烁） */
    let _minutePcSymbolKey = ''
    /** 分时加载时保存的跨日 1m 原始数据（klineData 只保留当日，昨收推导需要跨日数据） */
    let _minuteRawData = []
    /** 父容器高度变化（如指标 IDE 拖拽分割条）不会触发 window.resize，需 ResizeObserver 调 chart.resize */
    let chartResizeObserver = null
    let chartResizeRafId = null
    /** handleResize 的 rAF 句柄（P1-4：替代固定 100ms 延时做防抖，与项目内其他 resize 一致） */
    let _resizeRafId = null

    // ═══════════════════════════════════════════════════════
    // 龙回头Pro 买卖点标记 (B/S 标注在日线/分时图上)
    // ═══════════════════════════════════════════════════════
    const dragonMarkerSource = shallowRef([])

    /** 应用标记: 清除旧 overlay → 按日期映射到 bar → simpleAnnotation 画 B/S */
    const applyDragonMarkers = () => {
      const chart = chartRef.value
      if (!chart || typeof chart.createOverlay !== 'function' || typeof chart.removeOverlay !== 'function') return
      try { chart.removeOverlay(o => o && o.dragonTag) } catch (_) { /* 低版本不支持过滤回调则忽略 */ }
      const list = dragonMarkerSource.value || []
      const data = klineData.value || []
      if (!list.length || !data.length) return
      // 日号 → bar 索引 (同时兼容 UTC 与 UTC+8 两种时间戳约定)
      const dayMap = new Map()
      data.forEach((d, i) => {
        const ts = Number(d && d.timestamp)
        if (!Number.isFinite(ts) || ts <= 0) return
        const k1 = Math.floor(ts / 86400000)
        const k2 = Math.floor((ts + 8 * 3600000) / 86400000)
        if (!dayMap.has(k1)) dayMap.set(k1, i)
        if (!dayMap.has(k2)) dayMap.set(k2, i)
      })
      const colors = { buy: '#15803d', sell: '#dc2626', signal: '#d97706' }
      const texts = { buy: 'B', sell: 'S', signal: '信' }
      const dayNumOf = (s) => {
        const m = String(s || '').match(/^(\d{4})-(\d{2})-(\d{2})/)
        if (!m) return null
        return Date.UTC(+m[1], +m[2] - 1, +m[3]) / 86400000
      }
      let applied = 0
      list.forEach(m => {
        const dayNum = dayNumOf(m.time)
        if (dayNum === null) return
        const idx = dayMap.get(dayNum)
        if (idx === undefined) return
        const color = colors[m.side] || '#d97706'
        try {
          chart.createOverlay({
            name: 'simpleAnnotation',
            dragonTag: true,
            points: [{ timestamp: Number(data[idx].timestamp), value: Number(m.price) }],
            text: texts[m.side] || '信',
            styles: {
              symbol: { size: 13, color: color, activeColor: color },
              text: { size: 10, color: color, weight: 700 }
            }
          })
          applied += 1
        } catch (_) { /* 单点失败不影响其它标记 */ }
      })
      if (applied > 0 && console) console.log('[KlineChart] 龙回头Pro 标记 %d 个', applied)
    }

    /** 拉取龙回头Pro 标记 (props 显式传入时跳过自取) */
    const loadDragonMarkers = async () => {
      const hasProp = Array.isArray(props.dragonMarkers) && props.dragonMarkers.length > 0
      if (hasProp) return
      const sym = (props.symbol || '').trim()
      if (!sym) { dragonMarkerSource.value = []; return }
      try {
        const res = await request({
          url: '/api/market/dragon/markers',
          method: 'get',
          params: { symbol: sym, days: 120 }
        })
        const data = (res && typeof res === 'object' && res.data !== undefined) ? res.data : []
        dragonMarkerSource.value = Array.isArray(data) ? data : []
      } catch (_) {
        dragonMarkerSource.value = []
      }
      nextTick(applyDragonMarkers)
    }

    watch(() => props.symbol, () => loadDragonMarkers(), { immediate: true })
    watch(() => props.dragonMarkers, v => {
      if (Array.isArray(v) && v.length) dragonMarkerSource.value = v
    })
    // 主图数据就绪/换代码后重挂标记 (分时实时 tick 不重挂, 标记不随分钟内变化)
    watch(klineData, () => nextTick(applyDragonMarkers))

    const wmCanvasRef = ref(null)
    const chipCanvasRef = ref(null)
    const chipOverlayRef = ref(null)
    let _chipPaneObserver = null
    let _wmTimer = null
    let _wmObserver = null
    let _chipData = null // { prices, density, avg_cost, current_price }
    let _chipRafId = null

    // ---- 左侧百分比坐标轴状态 ----
    const pctAxisOverlayRef = ref(null)
    const pctAxisCanvasRef = ref(null)
    const klineContainerRef = ref(null) // 主图容器模板引用（优先于全局 getElementById，避免同页异源 DOM 干扰）
    let _pctPaneObserver = null // ResizeObserver(candle_pane root)
    let _pctRafId = null // rAF 合并重绘
    let _pctWrappedAxis = null // 已包装 buildTicks 的 axis 实例
    let _pctOrigBuildTicks = null // 被包装前的 axis.buildTicks
    let _pctRulerSig = '' // 幂等签名（范围+基准+尺寸+主题+光标）
    let _pctCrosshairY = null // 最近一次十字光标 y（像素，相对 pane 顶）
    let _pctSubscribed = false // 是否已订阅 onDataReady / onCrosshairChange
    let _pctWatchdog = null // 兜底重绘定时器：即便事件订阅全部失效，也能周期性补绘左轴
    let _pctEverPainted = false // 是否成功绘制过一次
    let _pctFailCount = 0 // 有数据期间连续绘制失败计数
    let _pctFailTag = '' // 最近一次失败原因
    let _pctDiagShown = false // 一次性诊断提示是否已输出

    /** 组件是否已卸载：用于阻断延迟回调在卸载后继续操作/重建图表（P0-1 修复） */
    let _isUnmounted = false
    /** 统一收纳所有 setTimeout，供 onBeforeUnmount 一次性清理（P0-1 修复） */
    const _timers = new Set()
    /** 统一收纳等待容器尺寸的 ResizeObserver，供 onBeforeUnmount 一次性断开（冗余修复） */
    const _observers = new Set()
    /** 受管 setTimeout：回调执行后自动移出集合；组件卸载时统一清除，
     *  避免卸载后回调仍操作已销毁的 chartRef，或在已卸载组件上重建图表（孤儿实例） */
    const safeTimeout = (fn, delay) => {
      const id = setTimeout(() => {
        _timers.delete(id)
        fn()
      }, delay)
      _timers.add(id)
      return id
    }

    // 实时更新设置
    const realtimeTimer = ref(null)
    const realtimeInterval = ref(5000)
    /** 避免实时请求堆叠（上一轮未完成又触发下一轮会加重闪烁） */
    const realtimeFetchInFlight = ref(false)
    let realtimeChartRafId = null
    /** WebSocket 实时推送实例（加密市场直连交易所 WS） */
    let wsClient = null
    const wsActive = ref(false)
    let _cachedExchangeId = null
    let _exchangeIdTs = 0
    let _realtimeGeneration = 0
    /** 价格条节流：避免父组件因 price-change 频繁重绘 */
    const lastPriceEmitSig = ref('')

    /** 当前标的的价格精度（小数位数），根据K线数据自动推算 */
    const pricePrecision = ref(2)

    /**
     * 根据一组K线数据自动推算合理的价格精度。
     * 策略：取 close 价格中有效小数位数最多的那个，再额外 +1 保留余量，
     * 同时确保小范围价差（如 0.15678 vs 0.15701）不被抹平。
     */
    const calcPricePrecision = (data) => {
      if (!data || data.length === 0) return 2

      let maxDecimals = 0
      const sample = data.length > 50 ? data.slice(-50) : data
      for (let i = 0; i < sample.length; i++) {
        const vals = [sample[i].close, sample[i].open, sample[i].high, sample[i].low]
        for (let j = 0; j < vals.length; j++) {
          const s = String(vals[j])
          const dot = s.indexOf('.')
          if (dot >= 0) {
            const dec = s.length - dot - 1
            if (dec > maxDecimals) maxDecimals = dec
          }
        }
      }

      // 另一个视角：最小价差。如果 high-low 相对于价格非常小，需要更多小数位
      let minSpread = Infinity
      for (let i = 0; i < sample.length; i++) {
        const spread = sample[i].high - sample[i].low
        if (spread > 0 && spread < minSpread) minSpread = spread
      }
      let spreadDecimals = 2
      if (minSpread < Infinity && minSpread > 0) {
        // 需要多少位才能区分这个最小价差？至少让它显示为非零
        spreadDecimals = Math.ceil(-Math.log10(minSpread)) + 2
      }

      const result = Math.max(maxDecimals, spreadDecimals, 2)
      return Math.min(result, 10) // 上限 10 位
    }

    /** 用当前精度格式化价格 */
    const formatPrice = (v) => {
      return (Number(v) || 0).toFixed(pricePrecision.value)
    }

    // 指标刷新锁：避免实时定时器触发时 updateIndicators 重入（Python 指标可能较慢）
    const indicatorsUpdating = ref(false)
    // 指标刷新节流：K线/价格可高频刷新，但指标计算可以低频刷新（默认 10s）
    const indicatorRefreshInterval = ref(10000)
    const lastIndicatorRefreshTs = ref(0)

    // K线刷新很频繁时，指标计算不必同步频率；这里做节流（并且有重入锁）。
    const maybeUpdateIndicators = (force = false) => {
      if (!chartRef.value) return
      const now = Date.now()
      const iv = Number(indicatorRefreshInterval.value || 10000)
      if (force || !lastIndicatorRefreshTs.value || (now - lastIndicatorRefreshTs.value) >= iv) {
        lastIndicatorRefreshTs.value = now
        updateIndicators()
      }
    }

    // 筹码分布数据（暴露给模板显示 AVG）
    const chipDataForTemplate = ref(null)

    // 已添加的指标 ID 列表（用于清理）
    const addedIndicatorIds = ref([])
    const volPaneId = ref(null)  // 追踪 VOL 副图 paneId，避免重复创建
    // 已添加的信号 overlay ID 列表（用于清理）
    const addedSignalOverlayIds = ref([])
    // 已添加的画线 overlay ID 列表（用于清理和管理）
    const addedDrawingOverlayIds = ref([])
    // 副图关闭按钮（key=paneId, value=DOM element）
    const paneCloseButtons = new Map()
    // 当前激活的画线工具
    const activeDrawingTool = ref(null)

    // 画线工具定义（使用 computed 实现多语言支持）
    const { proxy } = getCurrentInstance()

    const drawingTools = computed(() => [
      { name: 'line', title: proxy.$t('dashboard.indicator.drawing.line'), icon: 'line' },
      { name: 'horizontalLine', title: proxy.$t('dashboard.indicator.drawing.horizontalLine'), icon: 'minus' },
      { name: 'verticalLine', title: proxy.$t('dashboard.indicator.drawing.verticalLine'), icon: 'column-width' },
      { name: 'ray', title: proxy.$t('dashboard.indicator.drawing.ray'), icon: 'arrow-right' },
      { name: 'straightLine', title: proxy.$t('dashboard.indicator.drawing.straightLine'), icon: 'menu' },
      { name: 'parallelStraightLine', title: proxy.$t('dashboard.indicator.drawing.parallelLine'), icon: 'menu' },
      { name: 'priceLine', title: proxy.$t('dashboard.indicator.drawing.priceLine'), icon: 'dollar' },
      { name: 'priceChannelLine', title: proxy.$t('dashboard.indicator.drawing.priceChannel'), icon: 'border' },
      { name: 'fibonacciLine', title: proxy.$t('dashboard.indicator.drawing.fibonacciLine'), icon: 'rise' }
    ])

    // 指标按钮定义
    const indicatorButtons = ref([
      {
        id: 'sma',
        name: 'SMA (简单移动平均)',
        shortName: 'SMA',
        type: 'line',
        defaultParams: { length: 20 },
        paramSchema: [{ key: 'length', labelKey: 'indicatorIde.editor.period', type: 'number', min: 1, max: 300, step: 1 }]
      },
      {
        id: 'ema',
        name: 'EMA (指数移动平均)',
        shortName: 'EMA',
        type: 'line',
        defaultParams: { length: 20 },
        paramSchema: [{ key: 'length', labelKey: 'indicatorIde.editor.period', type: 'number', min: 1, max: 300, step: 1 }]
      },
      {
        id: 'rsi',
        name: 'RSI (相对强弱)',
        shortName: 'RSI',
        type: 'line',
        defaultParams: { length: 14 },
        paramSchema: [{ key: 'length', labelKey: 'indicatorIde.editor.period', type: 'number', min: 1, max: 200, step: 1 }]
      },
      {
        id: 'macd',
        name: 'MACD',
        shortName: 'MACD',
        type: 'macd',
        defaultParams: { fast: 12, slow: 26, signal: 9 },
        paramSchema: [
          { key: 'fast', labelKey: 'indicatorIde.editor.fastLine', type: 'number', min: 1, max: 100, step: 1 },
          { key: 'slow', labelKey: 'indicatorIde.editor.slowLine', type: 'number', min: 2, max: 200, step: 1 },
          { key: 'signal', labelKey: 'indicatorIde.editor.signalLine', type: 'number', min: 1, max: 100, step: 1 }
        ]
      },
      {
        id: 'bb',
        name: '布林带 (Bollinger Bands)',
        shortName: 'BB',
        type: 'band',
        defaultParams: { length: 20, mult: 2 },
        paramSchema: [
          { key: 'length', labelKey: 'indicatorIde.editor.period', type: 'number', min: 1, max: 300, step: 1 },
          { key: 'mult', labelKey: 'indicatorIde.editor.multiplier', type: 'number', min: 0.1, max: 10, step: 0.1, precision: 1 }
        ]
      },
      {
        id: 'atr',
        name: 'ATR (平均真实波幅)',
        shortName: 'ATR',
        type: 'line',
        defaultParams: { period: 14 },
        paramSchema: [{ key: 'period', labelKey: 'indicatorIde.editor.period', type: 'number', min: 1, max: 200, step: 1 }]
      },
      {
        id: 'cci',
        name: 'CCI (商品通道指数)',
        shortName: 'CCI',
        type: 'line',
        defaultParams: { length: 20 },
        paramSchema: [{ key: 'length', labelKey: 'indicatorIde.editor.period', type: 'number', min: 1, max: 200, step: 1 }]
      },
      {
        id: 'williams',
        name: 'Williams %R (威廉指标)',
        shortName: 'W%R',
        type: 'line',
        defaultParams: { length: 14 },
        paramSchema: [{ key: 'length', labelKey: 'indicatorIde.editor.period', type: 'number', min: 1, max: 200, step: 1 }]
      },
      {
        id: 'mfi',
        name: 'MFI (资金流量指标)',
        shortName: 'MFI',
        type: 'line',
        defaultParams: { length: 14 },
        paramSchema: [{ key: 'length', labelKey: 'indicatorIde.editor.period', type: 'number', min: 1, max: 200, step: 1 }]
      },
      {
        id: 'adx',
        name: 'ADX (平均趋向指数)',
        shortName: 'ADX',
        type: 'adx',
        defaultParams: { length: 14 },
        paramSchema: [{ key: 'length', labelKey: 'indicatorIde.editor.period', type: 'number', min: 1, max: 200, step: 1 }]
      },
      { id: 'obv', name: 'OBV (能量潮)', shortName: 'OBV', type: 'line', defaultParams: {}, paramSchema: [] },
      {
        id: 'adosc',
        name: 'ADOSC (积累/派发振荡器)',
        shortName: 'ADOSC',
        type: 'line',
        defaultParams: { fast: 3, slow: 10 },
        paramSchema: [
          { key: 'fast', labelKey: 'indicatorIde.editor.fastLine', type: 'number', min: 1, max: 100, step: 1 },
          { key: 'slow', labelKey: 'indicatorIde.editor.slowLine', type: 'number', min: 2, max: 200, step: 1 }
        ]
      },
      { id: 'ad', name: 'AD (积累/派发线)', shortName: 'AD', type: 'line', defaultParams: {}, paramSchema: [] },
      {
        id: 'kdj',
        name: 'KDJ (随机指标)',
        shortName: 'KDJ',
        type: 'line',
        defaultParams: { period: 9, k: 3, d: 3 },
        paramSchema: [
          { key: 'period', labelKey: 'indicatorIde.editor.period', type: 'number', min: 1, max: 100, step: 1 },
          { key: 'k', labelKey: 'indicatorIde.editor.kSmoothing', type: 'number', min: 1, max: 20, step: 1 },
          { key: 'd', labelKey: 'indicatorIde.editor.dSmoothing', type: 'number', min: 1, max: 20, step: 1 }
        ]
      }
    ])

    const getIndicatorTemplate = (indicatorId) => {
      return indicatorButtons.value.find(item => item.id === indicatorId) || null
    }

    const normalizeIndicatorParams = (template, rawParams = {}) => {
      const params = { ...(template?.defaultParams || {}) }
      const schema = (template && Array.isArray(template.paramSchema)) ? template.paramSchema : []
      schema.forEach(field => {
        const rawValue = rawParams[field.key]
        const fallback = params[field.key]
        let nextValue = rawValue != null && rawValue !== '' ? Number(rawValue) : fallback
        if (Number.isNaN(nextValue)) nextValue = fallback
        if (field.min != null && nextValue < field.min) nextValue = field.min
        if (field.max != null && nextValue > field.max) nextValue = field.max
        if (field.precision != null && typeof nextValue === 'number') {
          nextValue = Number(nextValue.toFixed(field.precision))
        } else if (typeof nextValue === 'number' && Number.isInteger(field.step || 1)) {
          nextValue = Math.round(nextValue)
        }
        params[field.key] = nextValue
      })
      return params
    }

    const createIndicatorInstanceId = (indicatorId) => {
      return `${indicatorId}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
    }

    const normalizeIndicatorStyle = (style = {}, fallbackColor = '') => {
      const lineWidth = Math.max(1, Math.min(3, parseInt(style.lineWidth, 10) || 1))
      return {
        color: String(style.color || fallbackColor || '').trim() || fallbackColor || '#1890ff',
        lineWidth
      }
    }

    const pickNextDefaultParams = (template, existingIndicators = []) => {
      const baseParams = normalizeIndicatorParams(template, template?.defaultParams || {})
      if (!template || !template.id) return baseParams
      const sameType = (existingIndicators || []).filter(item => item && item.id === template.id)
      if (!sameType.length) return baseParams

      if (template.id === 'ema' || template.id === 'sma') {
        const preferred = [10, 20, 60, 120, 250]
        const used = new Set(sameType.map(item => Number(item?.params?.length || item?.params?.period || 0)).filter(Boolean))
        const candidate = preferred.find(value => !used.has(value))
        if (candidate) {
          return {
            ...baseParams,
            length: candidate
          }
        }
        const maxUsed = Math.max(...Array.from(used))
        return {
          ...baseParams,
          length: maxUsed > 0 ? maxUsed + 10 : (baseParams.length || 20)
        }
      }

      return baseParams
    }

    const formatIndicatorInstanceLabel = (indicator) => {
      if (!indicator) return ''
      const template = getIndicatorTemplate(indicator.id)
      const params = normalizeIndicatorParams(template, indicator.params || {})
      switch (indicator.id) {
        case 'sma':
        case 'ema':
        case 'rsi':
        case 'cci':
        case 'mfi':
        case 'adx':
        case 'williams':
          return `${template ? template.shortName : indicator.id.toUpperCase()}(${params.length})`
        case 'atr':
          return `ATR(${params.period})`
        case 'macd':
          return `MACD(${params.fast}, ${params.slow}, ${params.signal})`
        case 'bb':
          return `BB(${params.length}, ${params.mult})`
        case 'adosc':
          return `ADOSC(${params.fast}, ${params.slow})`
        case 'kdj':
          return `KDJ(${params.period}, ${params.k}, ${params.d})`
        default:
          return template ? template.shortName : indicator.id.toUpperCase()
      }
    }

    const activePresetIndicators = computed(() => {
      // 分时模式下隐藏外置 Python 指标，保留内置指标
      return (props.activeIndicators || []).filter(item => {
        if (!item || !item.id) return false
        if (item.id === 'selected-python-indicator') return false
        if (isMinuteLine.value && item.type === 'python') return false
        return true
      })
    })

    const indicatorEditorVisible = ref(false)
    const indicatorEditorSaving = ref(false)
    const indicatorEditorTargetId = ref('')
    const indicatorEditorForm = reactive({})

    const indicatorEditorTarget = computed(() => {
      return activePresetIndicators.value.find(item => (item.instanceId || item.id) === indicatorEditorTargetId.value) || null
    })

    const indicatorEditorTemplate = computed(() => {
      return indicatorEditorTarget.value ? getIndicatorTemplate(indicatorEditorTarget.value.id) : null
    })

    const indicatorEditorSchema = computed(() => {
      return indicatorEditorTemplate.value && Array.isArray(indicatorEditorTemplate.value.paramSchema)
        ? indicatorEditorTemplate.value.paramSchema.map(field => ({
            ...field,
            label: field.labelKey ? proxy.$t(field.labelKey) : field.label
          }))
        : []
    })

    const indicatorEditorModalWrapClass = computed(() => {
      return chartTheme.value === 'dark' ? 'indicator-editor-modal indicator-editor-modal--dark' : 'indicator-editor-modal'
    })

    const indicatorEditorTitle = computed(() => {
      return indicatorEditorTarget.value
        ? `${proxy.$t('indicatorIde.editor.edit')} ${formatIndicatorInstanceLabel(indicatorEditorTarget.value)}`
        : proxy.$t('indicatorIde.editor.editParams')
    })

    // 检查指标是否激活
    const isIndicatorActive = (indicatorId) => {
      return props.activeIndicators.some(ind => ind.id === indicatorId)
    }

    // 选择画线工具
    const selectDrawingTool = (toolName) => {
      if (!chartRef.value) {
        return
      }

      // 工具名称映射（UI 工具名 -> klinecharts 内部覆盖物名称）
      const toolMap = {
        line: 'segment',
        horizontalLine: 'horizontalStraightLine',
        verticalLine: 'verticalStraightLine',
        ray: 'rayLine',
        straightLine: 'straightLine',
        parallelStraightLine: 'parallelStraightLine',
        priceLine: 'priceLine',
        priceChannelLine: 'priceChannelLine',
        fibonacciLine: 'fibonacciLine',
        measure: 'priceRangeMeasure'
      }

      const overlayName = toolMap[toolName] || toolName

      // 如果点击的是当前激活的工具，则取消激活
      if (activeDrawingTool.value === toolName) {
        activeDrawingTool.value = null
        // 取消当前的绘制模式
        // KLineChart 没有直接的 "cancelDrawing" API，通常移除最后一个未完成的覆盖物
        // 或者通过 overrideOverlay(null) 来取消正在进行的动作（如果支持）
        try {
          if (typeof chartRef.value.overrideOverlay === 'function') {
            chartRef.value.overrideOverlay(null)
          }
        } catch (e) {
        }
        return
      }

      // 激活新的画线工具
      activeDrawingTool.value = toolName

      try {
        // klinecharts v9：overrideOverlay 只更新已存在的覆盖物，不会进入绘制模式。
        // 自定义 priceRangeMeasure 与内置工具一样，用 createOverlay（无 points）即可进入逐步取点绘制。
        const overlayConfig = {
          name: overlayName,
          lock: false,
          extendData: {
            isDrawing: true
          }
        }
        const overlayId = chartRef.value.createOverlay(overlayConfig)
        if (overlayId) {
          addedDrawingOverlayIds.value.push(overlayId)
        } else {
          console.warn(`Failed to create overlay: ${overlayName}. Make sure the overlay is registered.`)
          activeDrawingTool.value = null
        }
      } catch (err) {
        console.error(`Error selecting drawing tool ${toolName} (${overlayName}):`, err)
        activeDrawingTool.value = null
      }
    }

    // 清除所有画线
    const clearAllDrawings = () => {
      if (!chartRef.value) return

      try {
        // 移除所有已添加的画线覆盖物
        addedDrawingOverlayIds.value.forEach(overlayId => {
          try {
            if (typeof chartRef.value.removeOverlay === 'function') {
              chartRef.value.removeOverlay(overlayId)
            } else if (typeof chartRef.value.removeOverlayById === 'function') {
              chartRef.value.removeOverlayById(overlayId)
            }
          } catch (err) {
          }
        })
        addedDrawingOverlayIds.value = []
        activeDrawingTool.value = null

        // 取消当前的绘制模式
        if (typeof chartRef.value.overrideOverlay === 'function') {
          chartRef.value.overrideOverlay(null)
        }
      } catch (err) {
      }
    }

    // 切换指标显示/隐藏
    const toggleIndicator = (indicator) => {
      const isActive = isIndicatorActive(indicator.id)

      if (isActive) {
        // 移除指标
        emit('indicator-toggle', {
          action: 'remove',
          indicator: { id: indicator.id }
        })
      } else {
        // 添加指标
        const indicatorToAdd = {
          ...indicator,
          params: { ...indicator.defaultParams },
          calculate: null // calculate 函数在 updateIndicators 中通过 id 判断
        }
        emit('indicator-toggle', {
          action: 'add',
          indicator: indicatorToAdd
        })
      }
    }

    const handleIndicatorButtonClick = (indicator) => {
      if (!indicator || !indicator.id) return
      const fallbackColor = getIndicatorColor(activePresetIndicators.value.length)
      const nextParams = pickNextDefaultParams(indicator, activePresetIndicators.value)
      emit('indicator-toggle', {
        action: 'add',
        indicator: {
          ...indicator,
          instanceId: createIndicatorInstanceId(indicator.id),
          params: nextParams,
          style: normalizeIndicatorStyle({}, fallbackColor),
          visible: true,
          calculate: null
        }
      })
    }

    const openIndicatorEditor = (indicator) => {
      if (!indicator || !indicator.id) return
      const template = getIndicatorTemplate(indicator.id)
      const indicatorIndex = activePresetIndicators.value.findIndex(item => (item.instanceId || item.id) === (indicator.instanceId || indicator.id))
      const fallbackColor = indicator.style?.color || getIndicatorColor(indicatorIndex >= 0 ? indicatorIndex : 0)
      indicatorEditorTargetId.value = indicator.instanceId || indicator.id
      const nextParams = normalizeIndicatorParams(template, indicator.params || {})
      Object.keys(indicatorEditorForm).forEach(key => {
        delete indicatorEditorForm[key]
      })
      Object.keys(nextParams).forEach(key => {
        indicatorEditorForm[key] = nextParams[key]
      })
      indicatorEditorForm._styleColor = normalizeIndicatorStyle(indicator.style || {}, fallbackColor).color
      indicatorEditorForm._styleLineWidth = normalizeIndicatorStyle(indicator.style || {}, fallbackColor).lineWidth
      indicatorEditorVisible.value = true
    }

    const closeIndicatorEditor = () => {
      indicatorEditorVisible.value = false
      indicatorEditorTargetId.value = ''
      indicatorEditorSaving.value = false
      Object.keys(indicatorEditorForm).forEach(key => {
        delete indicatorEditorForm[key]
      })
    }

    const removeIndicatorInstance = (indicator) => {
      if (!indicator || !indicator.id) return
      emit('indicator-toggle', {
        action: 'remove',
        indicator: { id: indicator.id, instanceId: indicator.instanceId || indicator.id }
      })
      if (indicatorEditorTargetId.value === (indicator.instanceId || indicator.id)) {
        closeIndicatorEditor()
      }
    }

    /**
     * 给副图 pane 左上角添加关闭按钮。
     * klinecharts v9 的 getDom(paneId, 'root') 返回 pane 的根容器。
     */
    const addPaneCloseButton = (paneId, indicatorId, instanceId) => {
      if (!chartRef.value || !paneId) return
      // 清理旧按钮（如有）
      removePaneCloseButton(paneId)
      try {
        const paneContainer = chartRef.value.getDom(paneId, 'root')
        if (!paneContainer) return
        paneContainer.style.position = 'relative'
        const btn = document.createElement('div')
        btn.className = 'pane-close-btn'
        btn.innerHTML = '×'
        btn.title = proxy.$t('indicatorIde.editor.deleteIndicator') || '移除指标'
        // 内联样式（动态创建的元素无法命中 scoped CSS）
        Object.assign(btn.style, {
          position: 'absolute',
          top: '2px',
          left: '2px',
          width: '16px',
          height: '16px',
          lineHeight: '14px',
          textAlign: 'center',
          fontSize: '13px',
          fontWeight: 'bold',
          color: '#999',
          backgroundColor: 'rgba(255,255,255,0.75)',
          borderRadius: '3px',
          cursor: 'pointer',
          zIndex: '10',
          userSelect: 'none',
          opacity: '0.6',
          transition: 'opacity 0.15s, color 0.15s'
        })
        // 冗余修复：用 AbortController 统一解绑 3 个监听（原先无 remove，只能等元素移除后被动回收）
        const ac = new AbortController()
        const opts = { signal: ac.signal }
        btn.addEventListener('mouseenter', () => { btn.style.opacity = '1'; btn.style.color = '#f5222d' }, opts)
        btn.addEventListener('mouseleave', () => { btn.style.opacity = '0.6'; btn.style.color = '#999' }, opts)
        btn.addEventListener('click', (e) => {
          e.stopPropagation()
          removeIndicatorInstance({ id: indicatorId, instanceId: instanceId || indicatorId })
        }, opts)
        paneContainer.appendChild(btn)
        // 同时保存按钮与其 controller，移除时一并 abort 解绑
        paneCloseButtons.set(paneId, { el: btn, ac })
      } catch (e) { /* 预期内失败：pane 容器可能已被销毁 */ }
    }

    const removePaneCloseButton = (paneId) => {
      const item = paneCloseButtons.get(paneId)
      if (item) {
        // 冗余修复：先显式解绑监听，再移除 DOM
        item.ac.abort()
        const btn = item.el
        try { btn.parentNode && btn.parentNode.removeChild(btn) } catch (e) { /* 预期内：节点可能已不存在 */ }
        paneCloseButtons.delete(paneId)
      }
    }

    const removeAllPaneCloseButtons = () => {
      paneCloseButtons.forEach((item) => {
        item.ac.abort()
        const btn = item.el
        try { btn.parentNode && btn.parentNode.removeChild(btn) } catch (e) { /* 预期内：节点可能已不存在 */ }
      })
      paneCloseButtons.clear()
    }

    const toggleIndicatorVisibility = (indicator) => {
      if (!indicator || !indicator.id) return
      emit('indicator-toggle', {
        action: 'update',
        indicator: {
          ...indicator,
          instanceId: indicator.instanceId || indicator.id,
          visible: indicator.visible === false
        }
      })
    }

    const applyIndicatorEditor = () => {
      const indicator = indicatorEditorTarget.value
      const template = indicatorEditorTemplate.value
      if (!indicator || !template) {
        closeIndicatorEditor()
        return
      }
      const nextParams = normalizeIndicatorParams(template, indicatorEditorForm)
      if (Object.prototype.hasOwnProperty.call(nextParams, 'fast') &&
          Object.prototype.hasOwnProperty.call(nextParams, 'slow') &&
          Number(nextParams.fast) >= Number(nextParams.slow)) {
        proxy.$message.warning(proxy.$t('indicatorIde.editor.fastLessThanSlow'))
        return
      }
      const nextStyle = normalizeIndicatorStyle({
        color: indicatorEditorForm._styleColor,
        lineWidth: indicatorEditorForm._styleLineWidth
      }, indicator.style?.color || getIndicatorColor(0))
      indicatorEditorSaving.value = true
      emit('indicator-toggle', {
        action: 'update',
        indicator: {
          ...indicator,
          instanceId: indicator.instanceId || indicator.id,
          params: nextParams,
          style: nextStyle,
          visible: indicator.visible !== false
        }
      })
      closeIndicatorEditor()
    }

    // Pyodide 相关
    const pyodide = ref(null)
    const loadingPython = ref(false)
    const pythonReady = ref(false)
    const pyodideLoadFailed = ref(false)

    // 主题配置
    const themeConfig = computed(() => {
      if (chartTheme.value === 'dark') {
        return {
          backgroundColor: '#141414',
          textColor: '#d1d4dc',
          textColorSecondary: '#787b86',
          borderColor: '#2a2a2a',
          gridLineColor: '#252525',
          gridLineColorDashed: '#363c4e',
          tooltipBg: 'rgba(25, 27, 32, 0.95)',
          tooltipBorder: '#333',
          tooltipText: '#ccc',
          tooltipTextSecondary: '#888',
          axisLabelColor: '#787b86',
          splitAreaColor: ['rgba(250,250,250,0.05)', 'rgba(200,200,200,0.02)'],
          dataZoomBorder: '#2a2a2a',
          dataZoomFiller: 'rgba(41, 98, 255, 0.15)',
          dataZoomHandle: '#13c2c2',
          dataZoomText: 'transparent',
          dataZoomBg: '#252525',
          separatorColor: '#2f2f2f',
          separatorActive: 'rgba(88, 166, 255, 0.06)'
        }
      } else {
        return {
          backgroundColor: '#fff',
          textColor: '#333',
          textColorSecondary: '#666',
          borderColor: '#e8e8e8',
          gridLineColor: '#e8e8e8',
          gridLineColorDashed: '#e8e8e8',
          tooltipBg: 'rgba(255, 255, 255, 0.95)',
          tooltipBorder: '#e8e8e8',
          tooltipText: '#333',
          tooltipTextSecondary: '#666',
          axisLabelColor: '#666',
          splitAreaColor: ['rgba(250,250,250,0.05)', 'rgba(200,200,200,0.02)'],
          dataZoomBorder: '#e8e8e8',
          dataZoomFiller: 'rgba(24, 144, 255, 0.15)',
          dataZoomHandle: '#1890ff',
          dataZoomText: '#999',
          dataZoomBg: '#f0f2f5',
          separatorColor: '#f0f0f0',
          separatorActive: 'rgba(24, 144, 255, 0.05)'
        }
      }
    })

    // 根据主题获取指标颜色
    const getIndicatorColor = (idx) => {
      if (chartTheme.value === 'dark') {
        return ['#13c2c2', '#e040fb', '#ffeb3b', '#00e676', '#ff6d00', '#9c27b0'][idx % 6]
      } else {
        return ['#13c2c2', '#9c27b0', '#f57c00', '#1976d2', '#c2185b', '#7b1fa2'][idx % 6]
      }
    }

    // ========== Pyodide 初始化 ==========
    const loadPyodide = () => {
      return new Promise((resolve, reject) => {
        // 检查是否已经加载
        if (window.pyodide) {
          pyodide.value = window.pyodide
          pythonReady.value = true
          resolve(window.pyodide)
          return
        }

        loadingPython.value = true

        // 动态加载 Pyodide（生产环境默认 CDN 优先，避免本地静态资源缺失导致 404 卡住/报错）
        // 可通过环境变量自定义：
        // - VUE_APP_PYODIDE_CDN_BASE: 覆盖 CDN 基础路径（需以 / 结尾或会自动补齐）
        // - VUE_APP_PYODIDE_LOCAL_BASE: 覆盖本地基础路径（需以 / 结尾或会自动补齐）
        // - VUE_APP_PYODIDE_PREFER_CDN: 'true'/'false' 强制优先级
        const PYODIDE_VERSION = '0.25.0'
        const _ensureTrailingSlash = (s) => (s && s.endsWith('/')) ? s : (s ? (s + '/') : s)
        const defaultLocalBase = `/assets/pyodide/v${PYODIDE_VERSION}/full/`
        const defaultCdnBase = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`
        const localBase = _ensureTrailingSlash(process.env.VUE_APP_PYODIDE_LOCAL_BASE || defaultLocalBase)
        const cdnBase = _ensureTrailingSlash(process.env.VUE_APP_PYODIDE_CDN_BASE || defaultCdnBase)
        const preferCdnEnv = (process.env.VUE_APP_PYODIDE_PREFER_CDN || '').toString().toLowerCase()
        const preferCdn = preferCdnEnv
          ? (preferCdnEnv === 'true' || preferCdnEnv === '1' || preferCdnEnv === 'yes')
          : (process.env.NODE_ENV === 'production')

        const loadScript = (src) => new Promise((resolve, reject) => {
          // If script already inserted, reuse it
          const existing = document.querySelector(`script[data-pyodide-src="${src}"]`)
          if (existing) {
            // If already loaded, resolve immediately.
            if (typeof window.loadPyodide === 'function') return resolve()
            // Otherwise wait for load/error.
            existing.addEventListener('load', () => resolve(), { once: true })
            existing.addEventListener('error', () => reject(new Error('Pyodide 脚本加载失败')), { once: true })
            return
          }

          const s = document.createElement('script')
          s.dataset.pyodideSrc = src
          s.src = src
          s.onload = () => resolve()
          s.onerror = () => reject(new Error('Pyodide 脚本加载失败'))
          document.head.appendChild(s)
        })

        const initFromBase = async (baseUrl) => {
          if (typeof window.loadPyodide !== 'function') {
            throw new Error('loadPyodide 函数未找到')
          }
          window.pyodide = await window.loadPyodide({ indexURL: baseUrl })

              // 预加载 pandas 和 numpy
              await window.pyodide.loadPackage(['pandas', 'numpy'])

              pyodide.value = window.pyodide
              pythonReady.value = true
              loadingPython.value = false
              resolve(window.pyodide)
        }

        (async () => {
          const tryLoad = async (base) => {
            await loadScript(base + 'pyodide.js')
            await initFromBase(base)
          }

          try {
            if (preferCdn) {
              // 1) CDN-first (production default)
              await tryLoad(cdnBase)
            } else {
              // 1) Local-first (dev convenience)
              await tryLoad(localBase)
            }
          } catch (firstErr) {
            try {
              // 2) Fallback
              await tryLoad(preferCdn ? localBase : cdnBase)
            } catch (secondErr) {
              throw secondErr || firstErr
            }
          }
        })().catch((err) => {
          loadingPython.value = false
          pyodideLoadFailed.value = true
          reject(err)
        })
      })
    }

    // ========== Python 代码解析 ==========
    // 解析 Python 代码，提取参数信息
    const parsePythonStrategy = (code) => {
      if (!code || typeof code !== 'string') {
        return null
      }

      try {
        // 简单的参数提取：查找类似 @param 或 #param 的注释，或者函数参数
        // 提取可能的参数
        const params = {}

        // 尝试从代码中提取参数（如果有的话）
        // 例如：查找类似 span=144 这样的参数
        const paramMatches = code.match(/(\w+)\s*=\s*(\d+\.?\d*)/g)
        if (paramMatches) {
          paramMatches.forEach(match => {
            const parts = match.split('=')
            if (parts.length === 2) {
              const key = parts[0].trim()
              const value = parseFloat(parts[1].trim())
              if (!isNaN(value)) {
                params[key] = value
              }
            }
          })
        }

        // 返回解析结果
        return {
          params: params,
          plots: [], // 从代码中无法直接提取 plots，需要在执行时确定
          success: true
        }
      } catch (err) {
        // 即使解析失败，也返回一个基本对象，允许执行
        return {
          params: {},
          plots: [],
          success: false
        }
      }
    }

    // ========== Python 执行引擎 ==========
    /**
     * 等待 Pyodide 就绪（P1-5 修复）
     * 原实现为 while + 500ms sleep 轮询空转（最多 30 次 = 15 秒），每 500ms 才检查一次状态，
     * 加载完成时最多要空等 500ms，且整条调用链被阻塞、无法取消。
     * 改为事件驱动：就绪/失败信号一到即刻返回，只在超时候底时依赖定时器。
     */
    const waitForPyodide = (timeoutMs = 15000) => {
      // 已就绪 → 立即返回，不进入等待
      if (pythonReady.value && pyodide.value) return Promise.resolve(true)
      // 已明确失败、或根本没在加载 → 无需等待（保持原逻辑：直接进入失败分支）
      if (pyodideLoadFailed.value || !loadingPython.value) return Promise.resolve(false)
      return new Promise((resolve) => {
        let settled = false
        const finish = (ok) => {
          if (settled) return
          settled = true
          clearTimeout(timer)
          stopWatch()
          resolve(ok)
        }
        // 事件驱动：就绪信号到达即结束；加载失败或中止同样立即结束，不再空转
        const stopWatch = watch(
          () => [pythonReady.value, !!pyodide.value, pyodideLoadFailed.value, loadingPython.value],
          ([ready, py, failed, loading]) => {
            if (ready && py) finish(true)
            else if (failed || !loading) finish(false)
          }
        )
        const timer = safeTimeout(() => finish(false), timeoutMs)
      })
    }

    const executePythonStrategy = async (userCode, klineData, params = {}, indicatorInfo = {}) => {
      if (!pythonReady.value || !pyodide.value) {
        // P1-5: 事件驱动等待（替代 while + 500ms 轮询空转），最多 15 秒
        const ready = await waitForPyodide(15000)

        // 如果仍然未就绪，检查是否加载失败
        if (!ready) {
          // 如果不在加载中，说明加载失败或超时
          if (!loadingPython.value) {
            pyodideLoadFailed.value = true
          } else {
            // 如果还在加载中但超时了，也标记为失败
            loadingPython.value = false
            pyodideLoadFailed.value = true
          }
          throw new Error('Python 引擎未就绪，请等待加载完成')
        }
      }

      try {
        // 检查代码是否需要解密（购买的指标）
        let finalCode = userCode
        const isEncrypted = indicatorInfo.is_encrypted || indicatorInfo.isEncrypted || 0
        if (isEncrypted || needsDecrypt(userCode, isEncrypted)) {
          // 获取用户ID（优先级：indicatorInfo > props > params）
          const userId = indicatorInfo.user_id || indicatorInfo.userId || props.userId || params.userId
          // 使用原始数据库ID（originalId），如果没有则使用id
          const indicatorId = indicatorInfo.originalId || indicatorInfo.id || params.indicatorId

          if (userId && indicatorId) {
            try {
              finalCode = await decryptCodeAuto(finalCode, userId, indicatorId)
            } catch (decryptError) {
              throw new Error('代码解密失败，无法执行指标: ' + (decryptError.message || '未知错误'))
            }
          } else {
            throw new Error('缺少必要的解密参数（用户ID或指标ID），无法执行加密指标')
          }
        }
        // 1. 数据转换：将 JS 的 klineData / params 转换为 JSON 字符串
        // klineData 可能是内部格式（time）或 KLineChart 格式（timestamp）
        const rawData = klineData.map(item => {
          // 兼容两种格式
          let timeValue = item.timestamp || item.time
          // 如果是秒级时间戳，转换为毫秒
          if (timeValue < 1e10) {
            timeValue = timeValue * 1000
          }
          return {
            time: Math.floor(timeValue / 1000), // Python 端使用秒级时间戳
            open: parseFloat(item.open) || 0,
            high: parseFloat(item.high) || 0,
            low: parseFloat(item.low) || 0,
            close: parseFloat(item.close) || 0,
            volume: parseFloat(item.volume) || 0
          }
        })
        const rawDataJson = JSON.stringify(rawData)
        const paramsJson = JSON.stringify(params || {})

        // 2. 构建 Python 执行代码
        // 转义 JSON 字符串中的特殊字符
        const escapedJson = rawDataJson.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r')
        const escapedParams = paramsJson.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r')

        const pythonCode = `
import json
import pandas as pd
import numpy as np

# 递归清理 NaN 值的函数
def clean_nan(obj):
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan(item) for item in obj]
    elif isinstance(obj, (pd.Series, np.ndarray)):
        return [None if (isinstance(x, float) and (np.isnan(x) or np.isinf(x))) else x for x in obj]
    elif isinstance(obj, (float, np.floating)):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return float(obj)
    elif pd.isna(obj):
        return None
    else:
        return obj

# 接收 JSON 数据
raw_data = json.loads('${escapedJson}')
params = json.loads('${escapedParams}')

# 将前端参数注入为指标代码可直接使用的变量（对齐回测/实盘执行环境）
# 兼容多种命名（snake_case / camelCase）
def _get_param(key, default=None):
    if key in params:
        return params.get(key, default)
    # camelCase fallback
    camel = ''.join([key.split('_')[0]] + [p.capitalize() for p in key.split('_')[1:]])
    return params.get(camel, default)

try:
    leverage = float(_get_param('leverage', 1) or 1)
except Exception:
    leverage = 1

trade_direction = _get_param('trade_direction', _get_param('tradeDirection', 'both')) or 'both'

try:
    initial_position = int(_get_param('initial_position', 0) or 0)
except Exception:
    initial_position = 0

try:
    initial_avg_entry_price = float(_get_param('initial_avg_entry_price', 0.0) or 0.0)
except Exception:
    initial_avg_entry_price = 0.0

try:
    initial_position_count = int(_get_param('initial_position_count', 0) or 0)
except Exception:
    initial_position_count = 0

try:
    initial_last_add_price = float(_get_param('initial_last_add_price', 0.0) or 0.0)
except Exception:
    initial_last_add_price = 0.0

try:
    initial_highest_price = float(_get_param('initial_highest_price', 0.0) or 0.0)
except Exception:
    initial_highest_price = 0.0

# 转换为 DataFrame
df = pd.DataFrame(raw_data)

# 转换数据类型
df['open'] = df['open'].astype(float)
df['high'] = df['high'].astype(float)
df['low'] = df['low'].astype(float)
df['close'] = df['close'].astype(float)
df['volume'] = df['volume'].astype(float)

# 用户代码（已解密）
${finalCode}

# 构造输出（如果用户没有定义 output，则尝试从 result_json 获取）
if 'output' not in locals():
    if 'result_json' in locals():
        output = json.loads(result_json)
    else:
        output = {"plots": []}
else:
    # 确保 output 是字典格式
    if isinstance(output, str):
        output = json.loads(output)

# 清理 output 中的所有 NaN 值
output = clean_nan(output)

# 返回 JSON 字符串
json.dumps(output)
`

        // 3. 执行 Python 代码
        const resultJson = await pyodide.value.runPythonAsync(pythonCode)

        // 检查返回结果
        if (!resultJson || typeof resultJson !== 'string') {
          throw new Error(`Python 代码执行后未返回有效的 JSON 字符串，返回类型: ${typeof resultJson}`)
        }

        let result
        try {
          result = JSON.parse(resultJson)
        } catch (parseError) {
          throw new Error(`JSON 解析失败: ${parseError.message}。可能是数据中包含 NaN 或其他无效值。`)
        }

        // 4. 验证和格式化输出
        if (!result) {
          return { plots: [], signals: [], calculatedVars: {} }
        }

        // 确保 plots 存在且为数组
        if (!result.plots || !Array.isArray(result.plots)) {
          result.plots = []
        }

        // 5. 处理每个 plot 的数据，将 NaN 转换为 null
        result.plots = result.plots.map(plot => {
          if (plot.data && Array.isArray(plot.data)) {
            plot.data = plot.data.map(val => {
              if (val === null || val === undefined || (typeof val === 'number' && isNaN(val))) {
                return null
              }
              return val
            })
          }
          return plot
        })

        // 6. 处理 signals（如果有）
        if (result.signals && Array.isArray(result.signals)) {
          result.signals = result.signals.map(signal => {
            if (signal.data && Array.isArray(signal.data)) {
              signal.data = signal.data.map(val => {
                if (val === null || val === undefined || (typeof val === 'number' && isNaN(val))) {
                  return null
                }
                return val
              })
            }
            return signal
          })
        }

        // 7. 确保 calculatedVars 存在
        if (!result.calculatedVars) {
          result.calculatedVars = {}
        }

        return result
      } catch (err) {
        throw new Error(`Python 执行失败: ${err.message}`)
      }
    }

    // --- 指标计算函数 ---
    // 这些函数可能通过 indicator.calculate 间接调用，所以 ESLint 可能无法识别

    // eslint-disable-next-line no-unused-vars
    function calculateSMA (data, length) {
      const result = []
      for (let i = 0; i < data.length; i++) {
        if (i < length - 1) {
          result.push(null)
        } else {
          let sum = 0
          for (let j = i - length + 1; j <= i; j++) {
            sum += data[j].close
          }
          result.push(sum / length)
        }
      }
      return result
    }

    function calculateEMA (data, length) {
      const result = []
      const multiplier = 2 / (length + 1)
      let ema = null
      for (let i = 0; i < data.length; i++) {
        if (i === 0) {
          ema = data[i].close
        } else {
          ema = (data[i].close - ema) * multiplier + ema
        }
        result.push(ema)
      }
      return result
    }

    // eslint-disable-next-line no-unused-vars
    function calculateBollingerBands (data, length, mult) {
      // 内部计算SMA
      const sma = []
      for (let i = 0; i < data.length; i++) {
        if (i < length - 1) {
          sma.push(null)
        } else {
          let sum = 0
          for (let j = i - length + 1; j <= i; j++) {
            sum += data[j].close
          }
          sma.push(sum / length)
        }
      }

      const result = []
      for (let i = 0; i < data.length; i++) {
        if (i < length - 1) {
          result.push({ upper: null, middle: null, lower: null })
          continue
        }
        let sum = 0
        for (let j = i - length + 1; j <= i; j++) {
          sum += Math.pow(data[j].close - sma[i], 2)
        }
        const std = Math.sqrt(sum / length)
        result.push({
          upper: sma[i] + mult * std,
          middle: sma[i],
          lower: sma[i] - mult * std
        })
      }
      return result
    }

    // eslint-disable-next-line no-unused-vars
    function calculateRSI (data, length) {
      const result = []
      let avgGain = 0
      let avgLoss = 0

      for (let i = 0; i < data.length; i++) {
        if (i === 0) {
          result.push(null)
          continue
        }

        const change = data[i].close - data[i - 1].close
        const gain = change > 0 ? change : 0
        const loss = change < 0 ? Math.abs(change) : 0

        if (i < length) {
          // 前length-1个值，累积但不计算RSI
          result.push(null)
        } else if (i === length) {
          // 第length个值，计算初始平均值
          let sumGain = 0
          let sumLoss = 0
          for (let j = 1; j <= length; j++) {
            const chg = data[j].close - data[j - 1].close
            if (chg > 0) sumGain += chg
            else sumLoss += Math.abs(chg)
          }
          avgGain = sumGain / length
          avgLoss = sumLoss / length
          const rs = avgLoss === 0 ? 100 : avgGain / avgLoss
          result.push(100 - (100 / (1 + rs)))
        } else {
          // 后续值，使用平滑移动平均
          avgGain = (avgGain * (length - 1) + gain) / length
          avgLoss = (avgLoss * (length - 1) + loss) / length
          const rs = avgLoss === 0 ? 100 : avgGain / avgLoss
          result.push(100 - (100 / (1 + rs)))
        }
      }
      return result
    }

    // eslint-disable-next-line no-unused-vars
    function calculateMACD (data, fast, slow, signal) {
      const fastEMA = calculateEMA(data, fast)
      const slowEMA = calculateEMA(data, slow)
      const macdLine = []

      // 计算MACD线
      for (let i = 0; i < data.length; i++) {
        if (fastEMA[i] == null || slowEMA[i] == null) {
          macdLine.push(null)
        } else {
          macdLine.push(fastEMA[i] - slowEMA[i])
        }
      }

      // 计算Signal线 (MACD的EMA)
      // 需要保持原始数组长度，对null值进行特殊处理
      const signalLine = []
      const histogram = []
      let signalEMA = null
      let signalStartIdx = -1

      // 找到第一个非null的MACD值作为signal计算的起点
      for (let i = 0; i < macdLine.length; i++) {
        if (macdLine[i] !== null && signalStartIdx === -1) {
          signalStartIdx = i
          signalEMA = macdLine[i]
          break
        }
      }

      // 如果找到了起点，继续计算signal
      if (signalStartIdx >= 0) {
        const multiplier = 2 / (signal + 1)
        for (let i = 0; i < macdLine.length; i++) {
          if (i < signalStartIdx + signal - 1) {
            // signal需要等待足够的MACD值
            signalLine.push(null)
            histogram.push(null)
          } else if (macdLine[i] === null) {
            signalLine.push(null)
            histogram.push(null)
          } else {
            if (i === signalStartIdx + signal - 1) {
              // 第一个signal值：计算前signal个MACD值的平均值
              let sum = 0
              let count = 0
              for (let j = signalStartIdx; j <= i; j++) {
                if (macdLine[j] !== null) {
                  sum += macdLine[j]
                  count++
                }
              }
              signalEMA = sum / count
            } else {
              // 后续值：使用EMA公式
              signalEMA = (macdLine[i] - signalEMA) * multiplier + signalEMA
            }
            signalLine.push(signalEMA)
            histogram.push(macdLine[i] - signalEMA)
          }
        }
      } else {
        // 如果没有有效的MACD值，全部设为null
        for (let i = 0; i < macdLine.length; i++) {
          signalLine.push(null)
          histogram.push(null)
        }
      }

      return { macd: macdLine, signal: signalLine, histogram }
    }

    // 计算ATR（平均真实波幅）
    function calculateATR (data, period) {
      const tr = [] // 真实波幅
      for (let i = 0; i < data.length; i++) {
        if (i === 0) {
          tr.push(data[i].high - data[i].low)
        } else {
          const hl = data[i].high - data[i].low
          const hc = Math.abs(data[i].high - data[i - 1].close)
          const lc = Math.abs(data[i].low - data[i - 1].close)
          tr.push(Math.max(hl, hc, lc))
        }
      }

      // 计算ATR（TR的SMA）
      const atr = []
      for (let i = 0; i < data.length; i++) {
        if (i < period - 1) {
          atr.push(null)
        } else {
          let sum = 0
          for (let j = i - period + 1; j <= i; j++) {
            sum += tr[j]
          }
          atr.push(sum / period)
        }
      }
      return atr
    }

    // 计算CCI (商品通道指数)
    function calculateCCI (data, length) {
      const cci = []
      for (let i = 0; i < data.length; i++) {
        if (i < length - 1) {
          cci.push(null)
        } else {
          // 计算典型价格 (TP)
          const tp = []
          for (let j = i - length + 1; j <= i; j++) {
            tp.push((data[j].high + data[j].low + data[j].close) / 3)
          }
          // 计算TP的SMA
          const sma = tp.reduce((sum, val) => sum + val, 0) / length
          // 计算平均偏差
          const meanDev = tp.reduce((sum, val) => sum + Math.abs(val - sma), 0) / length
          // 计算CCI
          const currentTP = (data[i].high + data[i].low + data[i].close) / 3
          const cciValue = meanDev === 0 ? 0 : (currentTP - sma) / (0.015 * meanDev)
          cci.push(cciValue)
        }
      }
      return cci
    }

    // 计算Williams %R (威廉指标)
    function calculateWilliamsR (data, length) {
      const williamsR = []
      for (let i = 0; i < data.length; i++) {
        if (i < length - 1) {
          williamsR.push(null)
        } else {
          let highest = -Infinity
          let lowest = Infinity
          for (let j = i - length + 1; j <= i; j++) {
            highest = Math.max(highest, data[j].high)
            lowest = Math.min(lowest, data[j].low)
          }
          const wr = (highest - lowest) === 0 ? -50 : ((highest - data[i].close) / (highest - lowest)) * -100
          williamsR.push(wr)
        }
      }
      return williamsR
    }

    // 计算MFI (资金流量指标)
    function calculateMFI (data, length) {
      const mfi = []
      for (let i = 0; i < data.length; i++) {
        if (i < length) {
          mfi.push(null)
        } else {
          let positiveFlow = 0
          let negativeFlow = 0
          for (let j = i - length + 1; j <= i; j++) {
            const typicalPrice = (data[j].high + data[j].low + data[j].close) / 3
            const rawMoneyFlow = typicalPrice * data[j].volume
            if (j > i - length + 1) {
              const prevTypicalPrice = (data[j - 1].high + data[j - 1].low + data[j - 1].close) / 3
              if (typicalPrice > prevTypicalPrice) {
                positiveFlow += rawMoneyFlow
              } else if (typicalPrice < prevTypicalPrice) {
                negativeFlow += rawMoneyFlow
              }
            }
          }
          const moneyFlowRatio = negativeFlow === 0 ? 100 : positiveFlow / negativeFlow
          const mfiValue = 100 - (100 / (1 + moneyFlowRatio))
          mfi.push(mfiValue)
        }
      }
      return mfi
    }

    // 计算ADX (平均趋向指数) 和 DMI (+DI, -DI)
    function calculateADX (data, length) {
      const plusDI = []
      const minusDI = []
      const adx = []

      // 计算真实波幅(TR)和方向移动(+DM, -DM)
      const tr = []
      const plusDM = []
      const minusDM = []

      for (let i = 0; i < data.length; i++) {
        if (i === 0) {
          tr.push(data[i].high - data[i].low)
          plusDM.push(0)
          minusDM.push(0)
        } else {
          const hl = data[i].high - data[i].low
          const hc = Math.abs(data[i].high - data[i - 1].close)
          const lc = Math.abs(data[i].low - data[i - 1].close)
          tr.push(Math.max(hl, hc, lc))

          const upMove = data[i].high - data[i - 1].high
          const downMove = data[i - 1].low - data[i].low

          if (upMove > downMove && upMove > 0) {
            plusDM.push(upMove)
          } else {
            plusDM.push(0)
          }

          if (downMove > upMove && downMove > 0) {
            minusDM.push(downMove)
          } else {
            minusDM.push(0)
          }
        }
      }

      // 计算平滑的TR, +DM, -DM
      const smoothTR = []
      const smoothPlusDM = []
      const smoothMinusDM = []

      for (let i = 0; i < data.length; i++) {
        if (i < length - 1) {
          smoothTR.push(null)
          smoothPlusDM.push(null)
          smoothMinusDM.push(null)
          plusDI.push(null)
          minusDI.push(null)
          adx.push(null)
        } else if (i === length - 1) {
          // 初始值：简单求和
          let sumTR = 0
          let sumPlusDM = 0
          let sumMinusDM = 0
          for (let j = 0; j <= i; j++) {
            sumTR += tr[j]
            sumPlusDM += plusDM[j]
            sumMinusDM += minusDM[j]
          }
          smoothTR.push(sumTR)
          smoothPlusDM.push(sumPlusDM)
          smoothMinusDM.push(sumMinusDM)
        } else {
          // 平滑计算：Wilder's smoothing
          smoothTR.push(smoothTR[i - 1] - (smoothTR[i - 1] / length) + tr[i])
          smoothPlusDM.push(smoothPlusDM[i - 1] - (smoothPlusDM[i - 1] / length) + plusDM[i])
          smoothMinusDM.push(smoothMinusDM[i - 1] - (smoothMinusDM[i - 1] / length) + minusDM[i])
        }

        if (i >= length - 1) {
          const trVal = smoothTR[i]
          const plusDMVal = smoothPlusDM[i]
          const minusDMVal = smoothMinusDM[i]

          if (trVal === 0) {
            plusDI.push(0)
            minusDI.push(0)
          } else {
            plusDI.push((plusDMVal / trVal) * 100)
            minusDI.push((minusDMVal / trVal) * 100)
          }

          // 计算DX
          if (i >= length - 1) {
            const diSum = plusDI[i] + minusDI[i]
            const dx = diSum === 0 ? 0 : Math.abs(plusDI[i] - minusDI[i]) / diSum * 100

            // 计算ADX (DX的平滑)
            if (i === length - 1) {
              adx.push(dx)
            } else if (i === length) {
              // 第二个ADX值：前两个DX的平均值
              const prevDX = Math.abs(plusDI[i - 1] - minusDI[i - 1]) / (plusDI[i - 1] + minusDI[i - 1]) * 100
              adx.push((prevDX + dx) / 2)
            } else {
              // ADX平滑：Wilder's smoothing
              adx.push((adx[i - 1] * (length - 1) + dx) / length)
            }
          }
        }
      }

      return { adx, plusDI, minusDI }
    }

    // 计算OBV (能量潮指标)
    function calculateOBV (data) {
      const obv = []
      let obvValue = 0

      for (let i = 0; i < data.length; i++) {
        if (i === 0) {
          obvValue = data[i].volume
        } else {
          if (data[i].close > data[i - 1].close) {
            obvValue += data[i].volume
          } else if (data[i].close < data[i - 1].close) {
            obvValue -= data[i].volume
          }
          // 如果收盘价相同，OBV不变
        }
        obv.push(obvValue)
      }
      return obv
    }

    // 计算AD (积累/派发线)
    function calculateAD (data) {
      const ad = []
      let adValue = 0

      for (let i = 0; i < data.length; i++) {
        const high = data[i].high
        const low = data[i].low
        const close = data[i].close
        const volume = data[i].volume

        if (high !== low) {
          const clv = ((close - low) - (high - close)) / (high - low)
          adValue += clv * volume
        }
        ad.push(adValue)
      }
      return ad
    }

    // 计算ADOSC (积累/派发振荡器) = AD的快速EMA - AD的慢速EMA
    function calculateADOSC (data, fast, slow) {
      const ad = calculateAD(data)
      const fastEMA = []
      const slowEMA = []
      const adosc = []

      const fastMultiplier = 2 / (fast + 1)
      const slowMultiplier = 2 / (slow + 1)

      let fastEMAValue = ad[0]
      let slowEMAValue = ad[0]

      for (let i = 0; i < ad.length; i++) {
        if (i === 0) {
          fastEMA.push(ad[0])
          slowEMA.push(ad[0])
          adosc.push(0)
        } else {
          fastEMAValue = (ad[i] - fastEMAValue) * fastMultiplier + fastEMAValue
          slowEMAValue = (ad[i] - slowEMAValue) * slowMultiplier + slowEMAValue

          fastEMA.push(fastEMAValue)
          slowEMA.push(slowEMAValue)
          adosc.push(fastEMAValue - slowEMAValue)
        }
      }

      return adosc
    }

    // 计算KDJ (随机指标)
    function calculateKDJ (data, period, kPeriod, dPeriod) {
      const kValues = []
      const dValues = []
      const jValues = []

      for (let i = 0; i < data.length; i++) {
        if (i < period - 1) {
          kValues.push(null)
          dValues.push(null)
          jValues.push(null)
        } else {
          // 找到period内的最高价和最低价
          let highest = -Infinity
          let lowest = Infinity
          for (let j = i - period + 1; j <= i; j++) {
            highest = Math.max(highest, data[j].high)
            lowest = Math.min(lowest, data[j].low)
          }

          // 计算RSV
          const rsv = (highest - lowest) === 0 ? 50 : ((data[i].close - lowest) / (highest - lowest)) * 100

          // 计算K值 (RSV的移动平均)
          if (kValues[i - 1] === null) {
            kValues.push(rsv)
          } else {
            kValues.push((rsv * 2 + kValues[i - 1] * (kPeriod - 2)) / kPeriod)
          }

          // 计算D值 (K值的移动平均)
          if (dValues[i - 1] === null) {
            dValues.push(kValues[i])
          } else {
            dValues.push((kValues[i] * 2 + dValues[i - 1] * (dPeriod - 2)) / dPeriod)
          }

          // 计算J值
          jValues.push(3 * kValues[i] - 2 * dValues[i])
        }
      }

      return { k: kValues, d: dValues, j: jValues }
    }

    // ========== 注册自定义信号 Overlay (Signal Tag) ==========
    // 这是一个能够绘制 "圆点 + 带背景色文字框" 的自定义覆盖物
// ========== 注册自定义信号 Overlay (Signal Tag) ==========
registerOverlay({
      name: 'signalTag',
      // 【关键修改1】必须改为 1。告诉图表这个图形只需要一个点就画完了。
      // 只要这里是 1，图表就不会画那个蓝色的"编辑中"手柄。
      totalStep: 1,

      // 【关键修改2】彻底禁止该 Overlay 响应任何鼠标事件
      // 这样鼠标放上去也不会有蓝色的选中框
      lock: true,
      needDefaultPointFigure: true,
      needDefaultXAxisFigure: false,
      needDefaultYAxisFigure: false,

      // 【建议保留】进一步确保不拦截事件
      checkEventOn: () => false,

      createPointFigures: ({ coordinates, overlay }) => {
        const { text } = overlay.extendData || {}
        const color = overlay.extendData?.color || '#555555'

        // 1. 获取信号点坐标
        if (!coordinates[0]) return []
        const x = coordinates[0].x
        const signalY = coordinates[0].y // Point 0: Python中计算的标签位置（已包含垂直间距）

        // 2. 获取K线极值坐标（用于画圆点）
        const anchorY = coordinates[1] ? coordinates[1].y : signalY // Point 1: K线的high/low

        const boxPaddingX = 8
        const boxPaddingY = 4
        const fontSize = 12
        const textStr = String(text || '')
        // 简单的字符宽度估算
        const textWidth = textStr.split('').reduce((acc, char) => acc + (char.charCodeAt(0) > 255 ? 12 : 7), 0)
        const boxWidth = textWidth + boxPaddingX * 2
        const boxHeight = fontSize + boxPaddingY * 2

        // Compatibility: old overlays used extendData.type='buy'/'sell', new overlays use extendData.side='buy'/'sell'
        const side = overlay.extendData?.side || overlay.extendData?.type || 'buy'
        const isBuy = side === 'buy'

        // 3. 计算 Box 的 Y 轴位置
        // 【关键修改】直接使用 signalY（Python中已经调整好的位置），不再使用固定margin
        // signalY 已经包含了反转信号的垂直间距调整
        const boxY = isBuy ? signalY : (signalY - boxHeight)

        // 计算线段连接点
        // 圆点画在K线极值位置（anchorY），紧挨着K线
        // 连线从圆点连到标签框
        const circleY = anchorY // 圆点位置：K线的High或Low
        const lineStartY = circleY // 连线起点：圆点位置
        const lineEndY = isBuy ? boxY : (boxY + boxHeight) // 连线终点：标签框边缘

        return [
          // 1. 虚线 (从圆点连到标签框)
          {
            type: 'line',
            attrs: {
              coordinates: [
                { x, y: lineStartY }, // 从圆点（K线极值位置）
                { x, y: lineEndY } // 连到标签框边缘
              ]
            },
            styles: { style: 'stroke', color: color, dashedValue: [2, 2] },
            ignoreEvent: true
          },
          // 2. 圆点 (画在K线极值位置，紧挨着K线)
          {
            type: 'circle',
            attrs: { x, y: circleY, r: 4 },
            styles: { style: 'fill', color: color },
            ignoreEvent: true
          },
          // 3. 背景框 (基于 boxY)
          {
            type: 'rect',
            attrs: {
              x: x - boxWidth / 2,
              y: boxY,
              width: boxWidth,
              height: boxHeight,
              r: 4
            },
            styles: { style: 'fill', color: color, borderSize: 0 },
            ignoreEvent: true
          },
          // 4. 文字
          {
            type: 'text',
            attrs: {
              x: x,
              y: boxY + boxHeight / 2,
              text: textStr,
              align: 'center',
              baseline: 'middle'
            },
            styles: { color: '#ffffff', size: fontSize, weight: 'bold', backgroundColor: color, borderRadius: 5 },
            ignoreEvent: true
          }
        ]
      }
    })

    // ========== 注册价格测量工具 Overlay (Price Range Measure) ==========
    // 类似 TradingView 的测量工具，显示两点之间的价格变化和涨跌幅
    registerOverlay({
      name: 'priceRangeMeasure',
      totalStep: 2, // 需要两个点：起点和终点
      lock: false, // 允许编辑
      needDefaultPointFigure: false,
      needDefaultXAxisFigure: false,
      needDefaultYAxisFigure: false,

      createPointFigures: ({ coordinates, overlay, ctx }) => {
        if (!coordinates[0] || !coordinates[1]) return []

        const startPoint = overlay.points[0]
        const endPoint = overlay.points[1]

        if (!startPoint || !endPoint) return []

        // 获取起点和终点的价格
        const startPrice = startPoint.value
        const endPrice = endPoint.value
        const priceChange = endPrice - startPrice
        const percentChange = (priceChange / startPrice) * 100

        // 计算时间跨度（通过时间戳差值）
        const startTimestamp = startPoint.timestamp
        const endTimestamp = endPoint.timestamp
        const timeDiff = Math.abs(endTimestamp - startTimestamp)

        // 格式化时间跨度
        let timeSpan = ''
        const days = Math.floor(timeDiff / (1000 * 60 * 60 * 24))
        const hours = Math.floor((timeDiff % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60))
        const minutes = Math.floor((timeDiff % (1000 * 60 * 60)) / (1000 * 60))
        const seconds = Math.floor((timeDiff % (1000 * 60)) / 1000)

        if (days > 0) {
          timeSpan = `${days}天${hours > 0 ? hours + '小时' : ''}`
        } else if (hours > 0) {
          timeSpan = `${hours}小时${minutes > 0 ? minutes + '分钟' : ''}`
        } else if (minutes > 0) {
          timeSpan = `${minutes}分钟`
        } else {
          timeSpan = `${seconds}秒`
        }

        // 尝试从图表实例获取数据来计算K线数量
        let barCount = 0
        try {
          if (ctx && ctx.chart) {
            const chartData = ctx.chart.getData()
            if (chartData && Array.isArray(chartData) && chartData.length > 0) {
              const startIndex = chartData.findIndex(item => Math.abs(item.timestamp - startTimestamp) < 1000)
              const endIndex = chartData.findIndex(item => Math.abs(item.timestamp - endTimestamp) < 1000)
              if (startIndex >= 0 && endIndex >= 0) {
                barCount = Math.abs(endIndex - startIndex)
              }
            }
          }
        } catch (e) {
          // 如果无法获取数据，忽略错误
        }

        // 格式化显示文本
        const percentStr = percentChange >= 0
          ? `+${percentChange.toFixed(2)}%`
          : `${percentChange.toFixed(2)}%`
        const pp = pricePrecision.value
        const priceChangeStr = priceChange >= 0
          ? `+${priceChange.toFixed(pp)}`
          : `${priceChange.toFixed(pp)}`

        // 构建显示文本
        let displayText = `${percentStr}  ${priceChangeStr}`
        if (barCount > 0) {
          displayText += `  (${barCount}根`
          if (timeSpan) {
            displayText += ` / ${timeSpan}`
          }
          displayText += ')'
        } else if (timeSpan) {
          displayText += `  (${timeSpan})`
        }

        // 根据涨跌设置颜色（中国市场：红涨绿跌）
        const isUp = priceChange >= 0
        const lineColor = isUp ? '#f5222d' : '#52c41a'
        const textColor = isUp ? '#f5222d' : '#52c41a'
        const bgColor = isUp ? 'rgba(245, 34, 45, 0.1)' : 'rgba(82, 196, 26, 0.1)'

        const x1 = coordinates[0].x
        const y1 = coordinates[0].y
        const x2 = coordinates[1].x
        const y2 = coordinates[1].y

        // 计算文本位置（在线的中点上方）
        const midX = (x1 + x2) / 2
        const midY = (y1 + y2) / 2
        const textOffsetY = -20 // 文本在线上方

        // 估算文本宽度
        const fontSize = 12
        const textWidth = displayText.length * 7 + 16 // 简单估算
        const textHeight = fontSize + 8

        return [
          // 1. 连接线（带箭头）
          {
            type: 'line',
            attrs: {
              coordinates: [
                { x: x1, y: y1 },
                { x: x2, y: y2 }
              ]
            },
            styles: {
              style: 'stroke',
              color: lineColor,
              size: 2,
              dashedValue: [4, 4] // 虚线样式
            },
            ignoreEvent: false
          },
          // 2. 起点标记（小圆点）
          {
            type: 'circle',
            attrs: { x: x1, y: y1, r: 4 },
            styles: { style: 'fill', color: lineColor },
            ignoreEvent: false
          },
          // 3. 终点标记（小圆点）
          {
            type: 'circle',
            attrs: { x: x2, y: y2, r: 4 },
            styles: { style: 'fill', color: lineColor },
            ignoreEvent: false
          },
          // 4. 文本背景框
          {
            type: 'rect',
            attrs: {
              x: midX - textWidth / 2,
              y: midY + textOffsetY - textHeight / 2,
              width: textWidth,
              height: textHeight,
              r: 4
            },
            styles: {
              style: 'fill',
              color: bgColor,
              borderSize: 1,
              borderColor: lineColor
            },
            ignoreEvent: false
          },
          // 5. 文本
          {
            type: 'text',
            attrs: {
              x: midX,
              y: midY + textOffsetY,
              text: displayText,
              align: 'center',
              baseline: 'middle'
            },
            styles: {
              color: textColor,
              size: fontSize,
              weight: 'bold'
            },
            ignoreEvent: false
          }
        ]
      }
    })

    // ========== 分时均价线：使用 klinecharts 9.8 官方内置 AVP（Average Price Line 均价线） ==========
    // 内置算法：累计成交额 / 累计成交量（逐柱累加 turnover / volume），无需自研 calc。
    // 这里仅覆盖线条样式（金黄，浅色底上比原 #ffeb3b 更醒目）与 tooltip 文案；
    // 数据柱的 turnover 字段由 processMinuteLineData / mergeMinuteLineRealtime 负责补充
    // （后端未提供成交额时按「典型价×成交量」合成，与旧版口径一致）。
    const MINUTE_AVP_OPTIONS = () => ({
      name: 'AVP',
      shortName: '均价',
      figures: [{ key: 'avp', title: '均价: ', type: 'line' }],
      // 注意：9.8.12 的指标线绘制合并逻辑会直接读取 styles.lines[i].dashedValue[0]，
      // 覆盖对象必须带全字段（缺 dashedValue 会在 IndicatorView.drawImp 中抛 TypeError，
      // 并中断同 pane 后续指标的绘制——0 轴线因此消失）
      styles: { lines: [{ color: '#f0b90b', size: 1, style: 'stroke', dashedValue: [4, 4], smooth: false }] }
    })

    // ========== 分时图：0 轴线（原生指标线，与日K收盘线同机制） + Y 轴对称锚定 ==========
    // 0 轴线不再用自定义 overlay 实现：overlay 的坐标按「创建/绘制时刻」的轴范围换算，
    // 在锁轴/重排/实时刷新的时序竞争中容易错位甚至不渲染。
    // 指标线与库原生画线（含日K的收盘价线）走同一条逐帧重绘管线：每次绘制都
    // 用当前轴范围现算坐标，天然跟随锁定范围，永不脱节。

    // --- 分时图交互控制相关变量 ---
    /** 分时图模式下禁用滚轮/拖拽的事件处理器引用，用于恢复时移除 */
    let _minuteWheelHandler = null
    let _minuteTouchStartHandler = null
    let _minuteTouchMoveHandler = null
    let _minuteMouseDownHandler = null
    /** 分时图模式下锁定的可见范围 */
    let _minuteLockedRange = null
    /** 分时图模式下的 onVisibleRangeChange 回调引用，用于恢复时移除 */
    let _minuteRangeChangeCallback = null
    /** 分时图模式下添加的均价线指标（内置 AVP）的 paneId */
    let _avpPaneId = null
    /** 铺满视图计算中的重入保护（铺满过程会连续触发多次可见范围事件） */
    let _minuteFitting = false
    /** 分时：最近一次数据真正发生变化的时刻（用于停滞看门狗） */
    let _minuteLastChangeTs = 0
    /** 分时：看门狗上次全量重载的时刻（限流，避免频繁重载） */
    let _minuteWatchdogTs = 0
    /** 分时：整分钟强制刷新定时器，保证每一分钟的新柱在过界后立刻补上 */
    let _minuteBoundaryTimer = null
    /** 分时：图表全量写入的合并节流定时器 / 待写入数据 / 上次写入时刻 */
    let _minuteApplyTimer = null
    let _minuteApplyPending = null
    let _minuteApplyTs = 0
    /** 分时：图表全量写入的最小间隔（WS tick 高频到达时避免每帧 applyNewData） */
    const MINUTE_APPLY_THROTTLE = 800

    /**
     * 分时图模式：把可见视口固定锁死为「整个交易时段」。
     * 数据多长就多长（不再用 null 占位柱补齐未来时段），改为纯视口方案：
     *   1. barSpace = 绘图区宽度 / 全时段总柱数（A股=240，港股=330，见 minuteSessionBarCount）；
     *   2. scrollToDataIndex(count-1) 先让最后一根真实柱贴到右缘（diff=0 基准）；
     *   3. setOffsetRightDistance((总柱数-数据数)×barSpace) 把数据锚定到最左侧（from=0），
     *      右侧留白即为尚未走完的未来时段；X 轴刻度按库原生行为只画到最后一根真实柱；
     *   4. finally 中 setScrollEnabled(false) 锁死视口（不再随滚动/缩放变化）。
     * 由于 barSpace 恒定（= 宽度/总柱数），实时追加新柱时视图零抖动，只需重设右侧空位。
     * 注意：setBarSpace / scrollToDataIndex 依赖滚动能力，必须在 setScrollEnabled(false) 之前执行。
     */
    const fitMinuteLineView = () => {
      const chart = chartRef.value
      if (!chart || _minuteFitting) return
      _minuteFitting = true
      try {
        const dataList = typeof chart.getDataList === 'function' ? chart.getDataList() : (klineData.value || [])
        const count = dataList.length
        if (!count) return
        // 铺满与滚动需要滚动能力，先临时恢复（finally 中统一关闭）
        if (typeof chart.setScrollEnabled === 'function') chart.setScrollEnabled(true)
        const totalBars = minuteSessionBarCount(getMinuteLineSession())
        // 绘图区宽度：优先取主图 drawing 区精确值（不含 Y 轴，与库内部 _totalBarSpace 同源），
        // 失败时退化为 DOM clientWidth，再失败则放弃换算（沿用当前 barSpace 只做锚定）
        let plotWidth = 0
        try {
          if (typeof chart.getSize === 'function') {
            const size = chart.getSize('candle_pane', 'main')
            if (size && size.width > 0) plotWidth = size.width
          }
        } catch (_) { /* 预期内 */ }
        if (!(plotWidth > 0)) {
          try {
            const dom = typeof chart.getDom === 'function' ? chart.getDom('candle_pane', 'main') : null
            if (dom && dom.clientWidth > 0) plotWidth = dom.clientWidth
          } catch (_) { /* 预期内 */ }
        }
        // barSpace 合法域与库一致（MIN=1 / MAX=50），越界会被 setBarSpace 静默拒绝
        let barSpace = plotWidth > 0 ? Math.max(1, Math.min(50, plotWidth / totalBars)) : 0
        if (barSpace > 0 && typeof chart.setBarSpace === 'function') {
          chart.setBarSpace(barSpace)
        }
        if (typeof chart.getBarSpace === 'function') {
          // 公开 API getBarSpace() 返回数值；部分版本/内部实现返回对象 {bar,...}，两种都兼容
          const bs = chart.getBarSpace()
          const bsVal = typeof bs === 'number' ? bs : (bs && bs.bar)
          if (bsVal > 0) barSpace = bsVal
        }
        if (!(barSpace > 0)) return
        // 数据锚定最左侧：先让最后一根贴右缘（diff=0），再设置右侧空位 = 未走完的未来时段
        if (typeof chart.scrollToDataIndex === 'function') chart.scrollToDataIndex(count - 1, 0)
        if (typeof chart.setOffsetRightDistance === 'function') {
          chart.setOffsetRightDistance(Math.max(0, totalBars - count) * barSpace, true)
        }
        // 记录锁定后的实际范围（to 被库钳到数据数），供范围反弹回调判断是否被外力扰动
        const range = typeof chart.getVisibleRange === 'function' ? chart.getVisibleRange() : null
        _minuteLockedRange = range
          ? { from: Math.round(range.from), to: Math.round(range.to) }
          : { from: 0, to: count - 1 }
      } catch (_) {
        // 预期内：图表未就绪时无法铺满，静默降级
      } finally {
        _minuteFitting = false
        const chart2 = chartRef.value
        if (chart2 && typeof chart2.setScrollEnabled === 'function') {
          try { chart2.setScrollEnabled(false) } catch (_) { /* 预期内：版本不支持时依赖事件拦截 */ }
        }
      }
    }

    /**
     * 分时图模式：锁定主图 Y 轴手势（拖拽/滚轮缩放右轴），保持自动跟随数据。
     * klinecharts 的 Y 轴手动缩放由 pane 级 axisOptions.scrollZoomEnabled 控制，
     * 与 setZoomEnabled（X 向缩放）是两套开关。
     */
    const lockMinutePaneAxes = () => {
      const chart = chartRef.value
      if (!chart || typeof chart.setPaneOptions !== 'function') return
      try {
        chart.setPaneOptions({ id: 'candle_pane', axisOptions: { scrollZoomEnabled: false } })
      } catch (_) { /* 预期内：版本不支持时忽略 */ }
    }

    /** 恢复主图 Y 轴手势（退出分时时调用） */
    const unlockMinutePaneAxes = () => {
      const chart = chartRef.value
      if (!chart || typeof chart.setPaneOptions !== 'function') return
      try {
        chart.setPaneOptions({ id: 'candle_pane', axisOptions: { scrollZoomEnabled: true } })
      } catch (_) { /* 预期内 */ }
    }

    /** 分时图模式：禁用图表区域的滚轮、触摸和拖拽交互，并锁死可见范围 */
    const disableMinuteInteractions = () => {
      // klinecharts v9 原生开关：禁拖拽滚动 + 禁缩放（滚轮/触摸捏合）
      const chart = chartRef.value
      if (chart) {
        try {
          if (typeof chart.setScrollEnabled === 'function') chart.setScrollEnabled(false)
          if (typeof chart.setZoomEnabled === 'function') chart.setZoomEnabled(false)
        } catch (_) { /* 预期内：版本不支持时依赖下方事件拦截 */ }
      }

      const container = document.getElementById('kline-chart-container')
      if (!container) return

      // 禁用滚轮（zoom + scroll）
      if (_minuteWheelHandler) {
        container.removeEventListener('wheel', _minuteWheelHandler)
      }
      _minuteWheelHandler = (e) => {
        e.preventDefault()
        e.stopPropagation()
      }
      container.addEventListener('wheel', _minuteWheelHandler, { passive: false })

      // 禁用鼠标拖拽
      if (_minuteMouseDownHandler) {
        container.removeEventListener('mousedown', _minuteMouseDownHandler)
      }
      _minuteMouseDownHandler = (e) => {
        if (e.button === 0) {
          e.preventDefault()
        }
      }
      container.addEventListener('mousedown', _minuteMouseDownHandler)

      // 禁用触摸板缩放（pinch zoom）
      let lastTouchDist = 0
      _minuteTouchStartHandler = (e) => {
        if (e.touches.length >= 2) {
          const dx = e.touches[0].clientX - e.touches[1].clientX
          const dy = e.touches[0].clientY - e.touches[1].clientY
          lastTouchDist = Math.sqrt(dx * dx + dy * dy)
        }
      }
      _minuteTouchMoveHandler = (e) => {
        if (e.touches.length >= 2) {
          const dx = e.touches[0].clientX - e.touches[1].clientX
          const dy = e.touches[0].clientY - e.touches[1].clientY
          const dist = Math.sqrt(dx * dx + dy * dy)
          if (Math.abs(dist - lastTouchDist) > 10) {
            e.preventDefault()
            e.stopPropagation()
          }
          lastTouchDist = dist
        } else if (e.touches.length === 1) {
          // 单指滑动也阻止（防止拖拽）
          e.preventDefault()
          e.stopPropagation()
        }
      }
      container.addEventListener('touchstart', _minuteTouchStartHandler, { passive: true })
      container.addEventListener('touchmove', _minuteTouchMoveHandler, { passive: false })

      // 兜底锁死可见范围：任何范围变化（applyNewData / resize 等）都立即重新铺满
      if (chartRef.value && typeof chartRef.value.subscribeAction === 'function') {
        _minuteRangeChangeCallback = () => {
          if (_minuteLockedRange && !_minuteFitting && chartRef.value && isMinuteLine.value) {
            const current = chartRef.value.getVisibleRange()
            if (current && (Math.round(current.from) !== _minuteLockedRange.from || Math.round(current.to) !== _minuteLockedRange.to)) {
              fitMinuteLineView()
            }
          }
        }
        chartRef.value.subscribeAction('onVisibleRangeChange', _minuteRangeChangeCallback)
      }
    }

    /** 分时图模式：恢复图表区域的滚轮和触摸交互 */
    const enableMinuteInteractions = () => {
      // 恢复 klinecharts v9 原生滚动与缩放能力
      const chart = chartRef.value
      if (chart) {
        try {
          if (typeof chart.setScrollEnabled === 'function') chart.setScrollEnabled(true)
          if (typeof chart.setZoomEnabled === 'function') chart.setZoomEnabled(true)
        } catch (_) { /* 预期内：图表可能已销毁 */ }
      }
      // 移除范围反弹回调（v9 支持按引用取消订阅）
      if (chart && _minuteRangeChangeCallback && typeof chart.unsubscribeAction === 'function') {
        try { chart.unsubscribeAction('onVisibleRangeChange', _minuteRangeChangeCallback) } catch (_) { /* 预期内 */ }
      }
      _minuteRangeChangeCallback = null
      _minuteLockedRange = null

      const container = document.getElementById('kline-chart-container')
      if (container) {
        if (_minuteWheelHandler) {
          container.removeEventListener('wheel', _minuteWheelHandler)
          _minuteWheelHandler = null
        }
        if (_minuteMouseDownHandler) {
          container.removeEventListener('mousedown', _minuteMouseDownHandler)
          _minuteMouseDownHandler = null
        }
        if (_minuteTouchStartHandler) {
          container.removeEventListener('touchstart', _minuteTouchStartHandler)
          _minuteTouchStartHandler = null
        }
        if (_minuteTouchMoveHandler) {
          container.removeEventListener('touchmove', _minuteTouchMoveHandler)
          _minuteTouchMoveHandler = null
        }
      }
    }

    // --- 数据加载相关函数 ---
    // 格式化数据为 KLineChart 格式（timestamp 需要是毫秒）
    const formatKlineData = (data) => {
      return data.map(item => {
        let timeValue = item.time || item.timestamp
        if (typeof timeValue === 'string') {
          timeValue = parseInt(timeValue)
        }
        // KLineChart 需要毫秒时间戳，如果当前是秒级，转换为毫秒
        if (timeValue < 1e10) {
          timeValue = timeValue * 1000
        }
        return {
          timestamp: timeValue,
          open: parseFloat(item.open),
          high: parseFloat(item.high),
          low: parseFloat(item.low),
          close: parseFloat(item.close),
          volume: parseFloat(item.volume || 0),
          // 成交额透传（后端若提供 turnover/amount）：内置 AVP 均价线优先用真实成交额，
          // 缺失时由分时管线（attachMinuteTurnover）按典型价×成交量合成
          turnover: Number.isFinite(parseFloat(item.turnover))
            ? parseFloat(item.turnover)
            : (Number.isFinite(parseFloat(item.amount)) ? parseFloat(item.amount) : undefined)
        }
      }).filter(item => item.timestamp && !isNaN(item.open) && !isNaN(item.high) && !isNaN(item.low) && !isNaN(item.close))
        .sort((a, b) => a.timestamp - b.timestamp)
    }

    /** 用于判断合并后的 K 线与合并前是否一致，避免无意义的 updateData */
    const klineBarSnapshotKey = (b) => {
      if (!b) return ''
      const p = pricePrecision.value + 2
      const q = (x) => (Number(x) || 0).toFixed(p)
      return [q(b.open), q(b.high), q(b.low), q(b.close), q(b.volume)].join('|')
    }

    const flushRealtimeChartBar = (bar) => {
      if (!chartRef.value || typeof chartRef.value.updateData !== 'function') return
      try {
        chartRef.value.updateData(bar)
      } catch (e) {
        try {
          chartRef.value.applyNewData(klineData.value)
        } catch (_) {
          // P1-3: applyNewData 失败会导致图表空白，必须留痕
          console.warn('[KlineChart] applyNewData 失败，图表可能无法渲染:', _)
        }
      }
    }

    const scheduleRealtimeChartBarUpdate = (bar) => {
      if (realtimeChartRafId != null) {
        cancelAnimationFrame(realtimeChartRafId)
      }
      realtimeChartRafId = requestAnimationFrame(() => {
        realtimeChartRafId = null
        flushRealtimeChartBar(bar)
      })
    }

    /**
     * @param {Array} data 内部格式 K 线
     * @param {{ force?: boolean }} options force=true 时总是向父组件发价格（换标的/全量加载）
     */
    const updatePricePanel = (data, options = {}) => {
      const force = !!(options && options.force)
      if (!data || data.length === 0) return
      const last = data[data.length - 1]
      let sig
      let payload
      if (data.length > 1) {
        const prev = data[data.length - 2]
        const price = formatPrice(last.close)
        const change = ((last.close - prev.close) / prev.close) * 100
        sig = `${price}|${change.toFixed(3)}`
        payload = { price, change }
      } else {
        const price = formatPrice(last.close)
        sig = `${price}|0`
        payload = { price, change: 0 }
      }
      if (!force && sig === lastPriceEmitSig.value) return
      lastPriceEmitSig.value = sig
      emit('price-change', payload)
    }

    // 将 KLineChart 格式的数据转换为内部格式（用于 isSameTimeframe 等函数）
    const convertToInternalFormat = (data) => {
      return data.map(item => ({
        time: Math.floor(item.timestamp / 1000), // 转回秒级时间戳用于比较
        open: item.open,
        high: item.high,
        low: item.low,
        close: item.close,
        volume: item.volume
      }))
    }

    const isSameTimeframe = (time1, time2, tf) => {
      const date1 = new Date(time1 * 1000)
      const date2 = new Date(time2 * 1000)

      switch (tf) {
        case '1m':
          return date1.getFullYear() === date2.getFullYear() &&
                 date1.getMonth() === date2.getMonth() &&
                 date1.getDate() === date2.getDate() &&
                 date1.getHours() === date2.getHours() &&
                 date1.getMinutes() === date2.getMinutes()
        case '5m':
          return date1.getFullYear() === date2.getFullYear() &&
                 date1.getMonth() === date2.getMonth() &&
                 date1.getDate() === date2.getDate() &&
                 date1.getHours() === date2.getHours() &&
                 Math.floor(date1.getMinutes() / 5) === Math.floor(date2.getMinutes() / 5)
        case '15m':
          return date1.getFullYear() === date2.getFullYear() &&
                 date1.getMonth() === date2.getMonth() &&
                 date1.getDate() === date2.getDate() &&
                 date1.getHours() === date2.getHours() &&
                 Math.floor(date1.getMinutes() / 15) === Math.floor(date2.getMinutes() / 15)
        case '30m':
          return date1.getFullYear() === date2.getFullYear() &&
                 date1.getMonth() === date2.getMonth() &&
                 date1.getDate() === date2.getDate() &&
                 date1.getHours() === date2.getHours() &&
                 Math.floor(date1.getMinutes() / 30) === Math.floor(date2.getMinutes() / 30)
        case '1H':
          return date1.getFullYear() === date2.getFullYear() &&
                 date1.getMonth() === date2.getMonth() &&
                 date1.getDate() === date2.getDate() &&
                 date1.getHours() === date2.getHours()
        // [MODIFIED] 2H/4H K线已移除
        case '1D':
          return date1.getFullYear() === date2.getFullYear() &&
                 date1.getMonth() === date2.getMonth() &&
                 date1.getDate() === date2.getDate()
        case '1W':
          const week1 = Math.floor((date1.getTime() - new Date(date1.getFullYear(), 0, 1).getTime()) / (7 * 24 * 60 * 60 * 1000))
          const week2 = Math.floor((date2.getTime() - new Date(date2.getFullYear(), 0, 1).getTime()) / (7 * 24 * 60 * 60 * 1000))
          return date1.getFullYear() === date2.getFullYear() && week1 === week2
        default:
          return time1 === time2
      }
    }

    /** 是否为分时图模式 */
    const isMinuteLine = computed(() => props.timeframe === '分时')
    /** 是否显示左侧百分比坐标轴（本轮常显，保留开关余地） */
    const pctAxisVisible = computed(() => true)

    // --- 分时线交易时段定义（X 轴固定覆盖整个交易时段）---
    /** 各市场分时时段（小时分钟用 hour*100+minute 表示）；缺省按 A 股处理 */
    const MINUTE_LINE_SESSIONS = {
      cnstock: { start: 930, morningEnd: 1130, afternoonStart: 1300, end: 1500 },
      hkstock: { start: 930, morningEnd: 1200, afternoonStart: 1300, end: 1600 }
    }

    const getMinuteLineSession = () => {
      const m = String(props.market || '').toLowerCase()
      return MINUTE_LINE_SESSIONS[m] || MINUTE_LINE_SESSIONS.cnstock
    }

    /**
     * 分时全时段总柱数（X 轴固定覆盖整个交易时段所需的 bar 总数）。
     * 后端 1m 柱时间戳为结束时间（首柱 09:31，上午末柱 11:30，下午 13:01-15:00），
     * 因此 上午 = morningEnd - start 根、下午 = end - afternoonStart 根：
     * A股 = 120 + 120 = 240；港股 = 150 + 180 = 330。
     */
    const minuteSessionBarCount = (session) => {
      const toMin = (n) => Math.floor(n / 100) * 60 + (n % 100)
      const morningBars = Math.max(0, toMin(session.morningEnd) - toMin(session.start))
      const afternoonBars = Math.max(0, toMin(session.end) - toMin(session.afternoonStart))
      return Math.max(1, morningBars + afternoonBars)
    }

    const minuteDateStr = (ts) => {
      const d = new Date(ts)
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
    }

    /** 为分时真实柱补充 turnover（成交额）：内置 AVP 均价线的必需字段。
     *  后端 1m 数据无成交额时按「典型价×成交量」合成 */
    const attachMinuteTurnover = (bars) => {
      bars.forEach(b => {
        if (b == null || Number.isFinite(b.turnover)) return
        const typicalPrice = (b.high + b.low + b.close) / 3
        b.turnover = typicalPrice * (b.volume || 0)
      })
      return bars
    }

    /**
     * 从已加载的 1m 原始数据中推导「被展示交易日的前一交易日」收盘价。
     * 分时加载时 limit=1000 根 1m，通常覆盖多个交易日，
     * 因此无需额外的日线接口即可拿到昨收，可靠性最高。
     * @param {Array} rawData 1m 原始数据
     * @param {string} skipDayStr 需要跳过的日期（分时当前展示的那一天：今天，或回退显示的最近交易日）
     */
    const derivePrevCloseFromMinuteBars = (rawData, skipDayStr) => {
      if (!Array.isArray(rawData) || rawData.length === 0) return null
      // date -> 该日最后一根有效柱
      const lastBarByDate = new Map()
      for (let i = 0; i < rawData.length; i++) {
        const b = rawData[i]
        if (!b || !b.timestamp) continue
        const c = Number(b.close)
        if (!Number.isFinite(c) || c <= 0) continue
        const d = minuteDateStr(b.timestamp)
        if (d === skipDayStr) continue
        const prev = lastBarByDate.get(d)
        if (!prev || b.timestamp > prev.timestamp) lastBarByDate.set(d, b)
      }
      if (lastBarByDate.size === 0) return null
      // 取日期最大的（紧邻被展示交易日的前一交易日）
      let bestDate = null
      lastBarByDate.forEach((_, d) => { if (!bestDate || d > bestDate) bestDate = d })
      const bar = bestDate ? lastBarByDate.get(bestDate) : null
      const c = bar ? Number(bar.close) : NaN
      return Number.isFinite(c) && c > 0 ? c : null
    }

    /** 分时图：获取昨日收盘价（昨收）作为 Y 轴中心与 0 轴线基准 */
    const fetchMinutePrevClose = async (rawData) => {
      // 1. 父组件显式传入
      if (props.prevClose != null && Number(props.prevClose) > 0) {
        minutePrevClose.value = Number(props.prevClose)
        _minutePcSource = 'props'
        return
      }
      // 2. 从已加载的 1m 原始数据推导（无需额外请求，最可靠）
      try {
        // 「今日」可能没有数据（休市/盘前），分时会回退显示最近一个有数据的交易日。
        // 此时 0 轴线的基准应为「被展示的那一天」的前一交易日收盘，
        // 因此要从 klineData 最后一根真实柱反推正在展示的日期，而不是硬编码 Date.now()。
        let displayDay = minuteDateStr(Date.now())
        try {
          const realBars = (klineData.value || []).filter(b => b && b.timestamp)
          if (realBars.length > 0) {
            displayDay = minuteDateStr(realBars[realBars.length - 1].timestamp)
          }
        } catch (_) { /* 预期内 */ }
        const fromBars = derivePrevCloseFromMinuteBars(rawData, displayDay)
        if (fromBars != null) {
          minutePrevClose.value = fromBars
          _minutePcSource = '1m推导'
          return
        }
      } catch (_) { /* 预期内 */ }
      // 3. 日线接口兜底
      try {
        const res = await request({
          url: '/api/indicator/kline',
          method: 'get',
          params: { market: props.market, symbol: props.symbol, timeframe: '1D', limit: 2 }
        })
        const arr = (res && res.code === 1 && Array.isArray(res.data)) ? formatKlineData(res.data) : []
        // 先按时间升序，再取倒数第二根（上一交易日）收盘
        const sorted = arr.slice().sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0))
        if (sorted.length >= 2) {
          const c = Number(sorted[sorted.length - 2].close)
          if (Number.isFinite(c) && c > 0) {
            minutePrevClose.value = c
            _minutePcSource = '日线接口'
            return
          }
        }
      } catch (_) { /* 预期内：接口缺失时回退 */ }
      // 4. 最后回退：用当日首根真实柱的开盘价近似（开盘价缺失时用收盘价）
      const firstReal = (klineData.value || []).find(b => b && b.timestamp)
      const openVal = Number(firstReal && firstReal.open)
      const closeVal = Number(firstReal && firstReal.close)
      const approx = (Number.isFinite(openVal) && openVal > 0)
        ? openVal
        : ((Number.isFinite(closeVal) && closeVal > 0) ? closeVal : null)
      if (approx != null) {
        minutePrevClose.value = approx
        if (!_minutePcSource) _minutePcSource = '今开近似'
        return
      }
      minutePrevClose.value = null
      console.warn('[KlineChart] 分时：未能获取昨收，0 轴线与 Y 轴居中将被跳过（图表上无任何有效价格）')
    }

    /**
     * 分时：把最新分时数据整体写入图表（applyNewData + 重新锁死视口 + 锁定 Y 轴）。
     * 带合并节流：WS tick 高频到达时，最多每 MINUTE_APPLY_THROTTLE ms 全量刷一次，
     * 期间只保留最新一份数据，避免每帧都触发 applyNewData 造成卡顿。
     */
    const scheduleMinuteChartApply = (bars) => {
      _minuteApplyPending = bars
      if (_minuteApplyTimer != null) return
      const wait = _minuteApplyTs ? Math.max(0, MINUTE_APPLY_THROTTLE - (Date.now() - _minuteApplyTs)) : 0
      _minuteApplyTimer = safeTimeout(() => {
        _minuteApplyTimer = null
        const data = _minuteApplyPending
        _minuteApplyPending = null
        if (data && isMinuteLine.value && chartRef.value) {
          _minuteApplyTs = Date.now()
          try {
            if (typeof chartRef.value.applyNewData === 'function') {
              chartRef.value.applyNewData(data)
              // 实时新增/替换分钟柱后重新铺满，避免 applyNewData 重置 barSpace 导致时段不再占满宽度
              fitMinuteLineView()
            }
            // 【注意】这里不再调 maybeUpdateIndicators()：
            // 1) applyNewData 内部会异步重算全部图表指标实例（AVP 均价线/MA/VOL 均自动跟随新数据）；
            // 2) updateIndicators 是「全删重建」，每次都会重挂 VOL 副图与主图指标，
            //    分时下每 10s 重建一次会表现为周期性的窗口闪烁/抖动。
            // 盘中创新高/新低会扩大波动区间 → 重新锁定 Y 轴（锚点未变则内部跳过）
            applyMinutePrevCloseAxis()
          } catch (_) { /* 预期内：图表未就绪时等待下一轮全量加载 */ }
        }
      }, wait)
    }

    /**
     * 分时模式实时合并：真实柱按时间戳精确替换/追加，并补充成交额（turnover）字段。
     * 数据多长就多长（不再补齐未来占位柱），视口由 fitMinuteLineView 统一锁死为全时段。
     * 不能走通用时间段合并：通用逻辑按「周期边界」判重，不适配 1m 柱的时间戳口径。
     */
    const mergeMinuteLineRealtime = (newData) => {
      if (!newData || newData.length === 0) return
      const todayStr = minuteDateStr(Date.now())
      // 与 processMinuteLineData 口径一致：只接受当日「开盘之后」的柱
      const session = getMinuteLineSession()
      const toNumForFilter = (ts) => {
        const d = new Date(ts)
        return d.getHours() * 100 + d.getMinutes()
      }
      const incoming = newData.filter(b =>
        b && b.timestamp &&
        minuteDateStr(b.timestamp) === todayStr &&
        toNumForFilter(b.timestamp) >= session.start)
      const existingReal = (klineData.value || []).filter(b => b && b.timestamp && minuteDateStr(b.timestamp) === todayStr)
      if (incoming.length === 0 && existingReal.length === 0) return

      const realMap = new Map()
      existingReal.forEach(b => realMap.set(b.timestamp, b))
      incoming.forEach(b => realMap.set(b.timestamp, { ...b }))
      const realBars = attachMinuteTurnover(Array.from(realMap.values()).sort((a, b) => a.timestamp - b.timestamp))
      if (realBars.length === 0) return

      const prev = klineData.value || []
      // 与上一帧完全一致则直接跳过（避免无意义的重绘）
      const sameAsPrev = prev.length === realBars.length &&
        prev.every((b, i) => b && b.timestamp === realBars[i].timestamp && klineBarSnapshotKey(b) === klineBarSnapshotKey(realBars[i]))
      if (sameAsPrev) return

      klineData.value = realBars
      // 数据确实发生了变化 → 记录时间戳，供停滞看门狗判断
      _minuteLastChangeTs = Date.now()
      updatePricePanel(convertToInternalFormat(realBars), { force: true })

      const structureChanged = prev.length !== realBars.length ||
        prev.some((b, i) => !b || b.timestamp !== realBars[i].timestamp)

      if (isMinuteLine.value) {
        // 走全量 applyNewData（内部带合并节流）：视口锁死与指标重算由 fitMinuteLineView /
        // applyMinutePrevCloseAxis 统一处理。数据末尾就是最新真实柱（已无占位柱），
        // updateData 也可用，但全量刷新能同步重设右侧空位并保持指标/视口状态一致，更稳。
        scheduleMinuteChartApply(realBars)
      } else if (structureChanged || !chartRef.value || typeof chartRef.value.updateData !== 'function') {
        // 时间轴变化（跨日 / 首次构建）：全量刷新
        if (chartRef.value && typeof chartRef.value.applyNewData === 'function') {
          try {
            chartRef.value.applyNewData(realBars)
            maybeUpdateIndicators(true)
          } catch (_) { /* 预期内：图表未就绪时等待下一轮全量加载 */ }
        }
      } else {
        // 时间轴未变：只替换内容有变化的柱
        if (chartRef.value && typeof chartRef.value.updateData === 'function') {
          for (let i = 0; i < realBars.length; i++) {
            const p = realBars[i]
            const q = prev[i]
            if (!q || q.timestamp !== p.timestamp || klineBarSnapshotKey(q) !== klineBarSnapshotKey(p)) {
              flushRealtimeChartBar(p)
            }
          }
        }
      }
      // 分时：盘中创新高/新低会扩大波动区间 → 重新锚定 Y 轴（锚点未变则内部跳过）
      if (isMinuteLine.value) applyMinutePrevCloseAxis()
    }

    /**
     * 周期切换的图表现场保存 / 恢复 ---
     * 每个周期的图表现场：barSpace + 右缘可见柱索引（二者完全决定视图位置）。
     * klinecharts 的可见范围 to = round(diff + count + 0.5)（右侧空位时鍴到 count），
     * 因此恢复时先归零到右缘基准，再按公式反解 diff 精确滚动。
     */
    let _tfSceneMap = {}

    /** 读取当前图表现场（分时为锁定视图，不保存） */
    const captureChartScene = () => {
      const chart = chartRef.value
      if (!chart || isMinuteLine.value) return null
      if (typeof chart.getVisibleRange !== 'function') return null
      try {
        const range = chart.getVisibleRange()
        let barSpace = typeof chart.getBarSpace === 'function' ? chart.getBarSpace() : 0
        if (barSpace && typeof barSpace === 'object') barSpace = barSpace.bar
        const list = typeof chart.getDataList === 'function' ? chart.getDataList() : (klineData.value || [])
        if (!range || !(barSpace > 0) || !list.length) return null
        return {
          barSpace,
          savedTo: range.to,
          savedCount: list.length
        }
      } catch (_) {
        return null
      }
    }

    /** 恢复指定周期的图表现场；无现场时保持默认视图（最新居右） */
    const restoreChartScene = (tf) => {
      const chart = chartRef.value
      const scene = tf ? _tfSceneMap[tf] : null
      if (!chart || !scene || !(scene.barSpace > 0) || scene.savedTo == null) return
      try {
        const list = typeof chart.getDataList === 'function' ? chart.getDataList() : (klineData.value || [])
        const count = list.length
        if (!count) return
        if (typeof chart.setBarSpace === 'function') chart.setBarSpace(scene.barSpace)
        if (typeof chart.scrollToDataIndex !== 'function' || typeof chart.scrollByDistance !== 'function') return
        // 先把最后一根贴右缘（diff=0 基准），再按保存的 to 反解 diff 精确滚动
        chart.scrollToDataIndex(count - 1, 0)
        const savedTo = Math.min(Math.max(scene.savedTo, 1), count)
        const targetDiff = savedTo - count - 0.5
        const distance = -targetDiff * scene.barSpace
        if (distance) chart.scrollByDistance(distance, 0)
      } catch (_) { /* 预期内：数据尚未就绪时放弃本次恢复 */ }
    }

    /**
     * 把主图 Y 轴恢复为自动计算刻度。
     * klinecharts 一旦手动缩放过 Y 轴，autoCalcTickFlag 永久为 false，
     * 换股/换周期后新数据会被塞进旧的手动范围，无法最大化填充窗口；
     * 官方交互是双击右轴复位，这里走内部实例等价复位（版本锁定 9.8.x，带降级保护）。
     */
    const resetYAxisToAuto = () => {
      const chart = chartRef.value
      if (!chart) return
      try {
        const pane = chart._candlePane
        const axis = pane && typeof pane.getAxisComponent === 'function' ? pane.getAxisComponent() : null
        if (axis && typeof axis.setAutoCalcTickFlag === 'function' && axis.getAutoCalcTickFlag() === false) {
          axis.setAutoCalcTickFlag(true)
          if (typeof chart.adjustPaneViewport === 'function') {
            chart.adjustPaneViewport(false, true, true, true)
          }
        }
      } catch (_) { /* 预期内：内部结构变化时静默跳过 */ }
    }

    /** 换股后下一次数据加载：视口回归默认（barSpace 复位），保证数据最大化填充窗口 */
    let _resetViewportOnNextLoad = false

    /**
     * 分时图数据处理：
     * 1. 使用1m数据
     * 2. 只保留当天从9:30开始的数据
     * 3. 补充成交额 turnover（供内置 AVP 均价线使用；数据多长就多长，不补齐未来时段，
     *    显示区域由 fitMinuteLineView 锁死为整个交易时段）
     */
    const processMinuteLineData = (rawData) => {
      if (!rawData || rawData.length === 0) return []

      // 获取今天的日期（使用本地时间）
      const now = new Date()
      const year = now.getFullYear()
      const month = String(now.getMonth() + 1).padStart(2, '0')
      const day = String(now.getDate()).padStart(2, '0')
      const todayStr = `${year}-${month}-${day}`

      // 过滤只保留今天的数据，并且时间 >= 9:30
      const todayData = rawData.filter(item => {
        const d = new Date(item.timestamp)
        const itemYear = d.getFullYear()
        const itemMonth = String(d.getMonth() + 1).padStart(2, '0')
        const itemDay = String(d.getDate()).padStart(2, '0')
        const dateStr = `${itemYear}-${itemMonth}-${itemDay}`
        const hours = d.getHours()
        const minutes = d.getMinutes()
        const timeNum = hours * 100 + minutes

        // 只保留今天 9:30 之后的数据
        return dateStr === todayStr && timeNum >= 930
      })

      if (todayData.length === 0) {
        // 如果没有今天的数据，返回最近一天的数据
        const lastItem = rawData[rawData.length - 1]
        if (lastItem) {
          const lastDate = new Date(lastItem.timestamp)
          const lastDateStr = `${lastDate.getFullYear()}-${String(lastDate.getMonth() + 1).padStart(2, '0')}-${String(lastDate.getDate()).padStart(2, '0')}`
          const fallbackData = rawData.filter(item => {
            const d = new Date(item.timestamp)
            const dateStr = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
            return dateStr === lastDateStr
          })
          if (fallbackData.length > 0) {
            return attachMinuteTurnover(fallbackData.map(item => ({ ...item })))
          }
        }
        return []
      }

      // 浅拷贝（避免污染 _minuteRawData 原始数据）并补充成交额
      const result = todayData.map(item => ({ ...item }))

      return attachMinuteTurnover(result)
    }

    /**
     * 分时 0 轴线指标名：一条画在昨收价上的原生指标横线（与日K收盘价线同机制），
     * 同时兼任 Y 轴的兜底锚定（minValue / maxValue 参与 calcRange）。
     */
    const MINUTE_ZERO_LINE_IND = 'MINUTE_PREV_CLOSE_LINE'
    /**
     * 0 轴线的自定义绘制：横贯整个绘图区（含未来时段留白区）。
     * 库默认的逐柱 figure 连线只覆盖「有数据的柱」，占位柱移除后数据只占左侧一段，
     * 0 轴线会中途截断；参考线语义上应横贯全宽（与真实分时软件一致）。
     * yAxis.convertToPixel 在 percentage 轴下自动完成 价格→涨跌幅 换算，传绝对价格即可。
     * 返回 true 表示接管该指标的全部绘制（默认逐柱连线不再执行）。
     */
    const MINUTE_ZERO_LINE_DRAW = ({ ctx, bounding, yAxis, indicator }) => {
      try {
        const v = minutePrevClose.value
        if (v == null || !(v > 0) || !ctx || !bounding) return true
        const y = yAxis && typeof yAxis.convertToPixel === 'function' ? yAxis.convertToPixel(v) : null
        if (y == null || !Number.isFinite(y)) return true
        // 允许越界 ±2px（范围被外力改变时仍可见），完全出界则跳过
        if (y < -2 || y > bounding.height + 2) return true
        const lineStyle = (indicator && indicator.styles && indicator.styles.lines && indicator.styles.lines[0]) || {}
        ctx.save()
        ctx.strokeStyle = lineStyle.color || '#999999'
        ctx.lineWidth = lineStyle.size || 1
        ctx.setLineDash(lineStyle.dashedValue || [4, 4])
        ctx.beginPath()
        ctx.moveTo(0, Math.round(y) + 0.5)
        ctx.lineTo(bounding.width, Math.round(y) + 0.5)
        ctx.stroke()
        ctx.restore()
      } catch (_) { /* 预期内：轴未就绪时由下一帧重绘 */ }
      return true
    }
    /** 已应用的锚点区间，避免实时刷新时无谓重建 */
    let _minuteAxisRange = null
    /** 是否已锁定主图 Y 轴范围（退出分时时用于精确还原，避免影响其它周期） */
    let _minuteAxisLocked = false
    /** 分时 Y 轴上下各留的视觉余量比例（相对最大偏离） */
    const MINUTE_AXIS_PADDING = 0.1
    /**
     * 分时极坐标的涨跌停幅度（%）：按标的代码所在板块自动识别。
     * 沪深主板（600/601/603/605/000/001/002/003）±10%；
     * 创业板（300/301）与科创板（688/689）±20%；北交所（8xx/4xx/920）±30%。
     * symbol 可能带交易所前后缀（SH600000 / 600000.SH / bj430047），统一剥离。
     */
    const _minutePolarLimit = (market, symbol) => {
      let code = String(symbol || '').trim()
      code = code.replace(/^[a-zA-Z]+/, '').replace(/\..*$/, '').replace(/^\d{1,2}_/, '')
      if (code.startsWith('300') || code.startsWith('301') || code.startsWith('688') || code.startsWith('689')) return 20
      if (code.startsWith('920') || code.startsWith('8') || code.startsWith('4')) return 30
      return 10
    }
    /** 涨跌幅轴「0%=昨收」重定基刻度：已安装的轴实例与库原型实现（退出分时时还原） */
    let _minutePcTickAxis = null
    let _minutePcOrigCreateTicks = null
    /** 分时最新价标签是否已隐藏（涨跌幅轴下库标签基于今开，与昨收定基刻度矛盾） */
    let _minutePriceTagHidden = false
    /** 分时十字光标水平标签的昨收定基补丁：已补丁的视图实例与库原型实现 */
    let _minuteCrosshairView = null
    let _minuteCrosshairOrigGetText = null
    /** 分时极坐标模式：开=Y 轴固定 昨收±涨跌停%（按板块识别），关=按当日最大涨跌幅自适应 */
    let _minutePolarEnabled = false

    /** 取主图 Y 轴实例（klinecharts 9.8.x 内部结构，带降级保护） */
    const getCandleYAxis = () => {
      const chart = chartRef.value
      if (!chart) return null
      try {
        const pane = chart._candlePane
        const axis = pane && typeof pane.getAxisComponent === 'function' ? pane.getAxisComponent() : null
        return axis || null
      } catch (_) {
        return null
      }
    }

    // ---- 涨跌幅轴刻度的「0%=昨收」重定基（A股分时惯例：中轴 0.00%，上下 ±对称）----
    // 库的 percentage 轴 0% 基准是「首个可见柱收盘」（≈今开），昨收不在 0% 时
    // 刻度呈 [-2%, +10%] 形态（几何上昨收已居中，但标签不以 0 为中心）。
    // 这里在锁定的对称区间上按 rebased 值生成刻度：rebased 0 = 昨收位置，
    // 文本 ±X.XX%，像素坐标用库自己的 _innerConvertToPixel 现算。
    const minuteRound = (value, precision) => {
      const p = Math.pow(10, precision)
      return Math.round(value * p) / p
    }
    // 复刻库的 nice（1/2/3/4/5/6/8 × 10^n），保证刻度间隔手感与库一致
    const minuteNice = (value) => {
      if (!(value > 0) || !Number.isFinite(value)) return 0
      const exponent = Math.floor(Math.log10(value))
      const exp10 = Math.pow(10, exponent)
      const f = value / exp10
      let nf
      if (f < 1.5) nf = 1
      else if (f < 2.5) nf = 2
      else if (f < 3.5) nf = 3
      else if (f < 4.5) nf = 4
      else if (f < 5.5) nf = 5
      else if (f < 6.5) nf = 6
      else nf = 8
      return +(nf * exp10).toFixed(exponent < 0 ? -exponent : 0)
    }
    const minuteGetPrecision = (value) => {
      const str = String(value)
      const eIndex = str.indexOf('e')
      if (eIndex > 0) {
        const precision = Number(str.slice(eIndex + 1))
        return precision < 0 ? -precision : 0
      }
      const dotIndex = str.indexOf('.')
      return dotIndex < 0 ? 0 : str.length - 1 - dotIndex
    }
    /** 在锁定的对称区间上生成以昨收为 0% 的对称刻度（乘法重定基） */
    const buildMinuteRebasedTicks = (axis, range, bounding) => {
      if (!range || !(range.realRange > 0)) return []
      const height = bounding && bounding.height > 0 ? bounding.height : 0
      if (!(height > 0)) return []
      // 沿用库 optimalTicks 的标签避让基准（取 xAxis.tickText.size）
      let textHeight = 12
      try {
        textHeight = axis.getParent().getChart().getChartStore().getStyles().xAxis.tickText.size || 12
      } catch (_) { /* 预期内：取不到样式时用默认值 */ }
      const center = (Number(range.realFrom) + Number(range.realTo)) / 2 // 昨收的内部值（相对基准价的涨跌幅%）
      // 【乘法重定基·关键】「昨收 + r%」的真实价格 = 昨收×(1+r/100)，换算到内部
      // 百分比空间 = center + r×k，其中 k = 昨收/基准价 = 1 + center/100。
      // 若用加法（center + r），标签值会偏离真实昨收涨跌幅达 r×缺口%，今开≠昨收时
      // 实时线与坐标明显错位（偏离随涨幅放大）。
      const k = 1 + center / 100
      if (!(k > 0)) return []
      const R = range.realRange / 2 / k // rebased（昨收 %）半区间
      // 刻度间隔下限 0.1：保证 1 位小数的标签可分辨（需求：百分比坐标 1 位小数）
      const interval = Math.max(minuteNice((2 * R) / 8.0), 0.1)
      if (!(interval > 0)) return []
      const precision = minuteGetPrecision(interval)
      // 内部值 → 像素（优先用库实现；缺失时按库公式纯数学计算，兼容旧版本）
      const toPx = (internal) => {
        if (typeof axis._innerConvertToPixel === 'function') return axis._innerConvertToPixel(internal)
        try {
          const rg = axis.getRange()
          const rate = (internal - rg.from) / rg.range
          const reverse = typeof axis.isReverse === 'function' ? axis.isReverse() : false
          return Math.round(reverse ? rate * height : (1 - rate) * height)
        } catch (_) { return NaN }
      }
      const ticks = []
      let validY = null
      let r = minuteRound(Math.ceil(-R / interval) * interval, precision)
      let guard = 0
      while (r <= R + interval * 1e-6 && guard < 64) {
        guard++
        const y = toPx(center + r * k)
        if (Number.isFinite(y) && y > textHeight && y < height - textHeight &&
            (!Number.isFinite(validY) || Math.abs(validY - y) > textHeight * 2)) {
          validY = y
          const v = Number(r.toFixed(precision))
          ticks.push({ text: `${v > 0 ? '+' : ''}${v.toFixed(1)}%`, coord: y, value: String(center + r * k) })
        }
        r = minuteRound(r + interval, precision)
      }
      return ticks
    }
    /** 文本小数位截断（保留前缀/千分位/符号，仅限制小数点后位数） */
    const capTextDecimals = (text, maxDec) => {
      const s = String(text)
      const i = s.indexOf('.')
      if (i < 0 || s.length - i - 1 <= maxDec) return s
      return s.slice(0, i + maxDec + 1)
    }
    /**
     * 给分时主图 Y 轴安装自定义刻度：
     *  - percentage（涨跌幅轴）：昨收定基重定基刻度，1 位小数（buildMinuteRebasedTicks）
     *  - normal（金额轴）：Y 轴刻度最多 3 位小数（图表价格精度 >3 时仅截断显示文本，
     *    不改数据与精度本身；<3 时透传库默认刻度，与其它周期表现一致）
     * 仅分时锁定态生效，退出分时/切普通周期时透传并最终还原。
     */
    const installMinuteAxisTicks = (axis) => {
      if (!axis || typeof axis.createTicks !== 'function') return
      if (_minutePcTickAxis === axis) return
      try {
        _minutePcOrigCreateTicks = axis.createTicks
        axis.createTicks = function ({ range, bounding, defaultTicks }) {
          try {
            if (_minuteAxisLocked && typeof this.getType === 'function') {
              const type = this.getType()
              if (type === 'percentage' && range) {
                const rebased = buildMinuteRebasedTicks(this, range, bounding)
                if (rebased.length) return rebased
              } else if (type === 'normal') {
                const chartStore = this.getParent().getChart().getChartStore()
                if (chartStore && chartStore.getPrecision().price > 3) {
                  return defaultTicks.map(t => ({ ...t, text: capTextDecimals(t.text, 3) }))
                }
              }
            }
          } catch (_) { /* 预期内：退化到库默认刻度 */ }
          return typeof _minutePcOrigCreateTicks === 'function'
            ? _minutePcOrigCreateTicks.call(this, { range, bounding, defaultTicks })
            : defaultTicks
        }
        _minutePcTickAxis = axis
      } catch (_) { /* 预期内 */ }
    }
    /** 还原分时 Y 轴自定义刻度（退出分时/清理时调用） */
    const restoreMinuteAxisTicks = () => {
      if (_minutePcTickAxis) {
        try { delete _minutePcTickAxis.createTicks } catch (_) { /* 预期内 */ }
        try { _minutePcTickAxis.buildTicks(true) } catch (_) { /* 预期内 */ }
        _minutePcTickAxis = null
      }
      _minutePcOrigCreateTicks = null
    }
    /**
     * 分时最新价标签的显隐（带状态记忆，避免每次刷新重复 setStyles）。
     * 涨跌幅轴下库标签固定显示「相对今开」的涨跌幅，与昨收定基的刻度/实时线
     * 基准不一致（同一像素两套读数）→ 隐藏；金额轴下显示的是价格本身 → 保留。
     */
    const setMinutePriceTagHidden = (hidden) => {
      if (_minutePriceTagHidden === hidden) return
      _minutePriceTagHidden = hidden
      try {
        chartRef.value && chartRef.value.setStyles({ candle: { priceMark: { last: { show: !hidden } } } })
      } catch (_) { /* 预期内 */ }
    }

    /**
     * 安装分时十字光标水平标签的补丁：
     *  - percentage：读数改为「昨收定基」涨跌幅，1 位小数（库默认按今开基准 → 光标
     *    移动时百分比与昨收定基的坐标刻度不一致，即“光标百分比比例不对”）
     *  - normal：价格读数小数位 >3 时截断为 3 位（与 Y 轴刻度规则一致）
     * 仅分时主图生效，退出分时还原库原型实现。
     */
    const installMinuteCrosshairRebase = () => {
      const chart = chartRef.value
      if (!chart || !isMinuteLine.value) return
      try {
        const pane = chart._candlePane
        const widget = pane && typeof pane.getYAxisWidget === 'function' ? pane.getYAxisWidget() : null
        const view = widget && widget._crosshairHorizontalLabelView
        if (!view || typeof view.getText !== 'function') return
        if (_minuteCrosshairView === view) return
        _minuteCrosshairOrigGetText = view.getText
        view.getText = function (crosshair, chartStore, axis) {
          try {
            const type = axis && typeof axis.getType === 'function' ? axis.getType() : ''
            if (type === 'percentage' && crosshair && typeof axis.convertFromPixel === 'function') {
              const pc = minutePrevClose.value
              if (pc != null && pc > 0) {
                const price = axis.convertFromPixel(crosshair.y)
                if (Number.isFinite(price) && price > 0) {
                  // 光标处价格 → 昨收涨跌幅（昨收定基，1 位小数，与坐标刻度一致）
                  const pct = (price - pc) / pc * 100
                  const r = Number(pct.toFixed(1))
                  return `${r > 0 ? '+' : ''}${r.toFixed(1)}%`
                }
              }
            } else if (type === 'normal' && crosshair && chartStore &&
                       typeof chartStore.getPrecision === 'function' &&
                       chartStore.getPrecision().price > 3 &&
                       typeof axis.convertFromPixel === 'function') {
              // 价格读数最多 3 位小数（保留库的完整格式化后仅截断小数位）
              const t = _minuteCrosshairOrigGetText.call(this, crosshair, chartStore, axis)
              return capTextDecimals(t, 3)
            }
          } catch (_) { /* 预期内：退化到库默认读数 */ }
          return typeof _minuteCrosshairOrigGetText === 'function'
            ? _minuteCrosshairOrigGetText.call(this, crosshair, chartStore, axis)
            : ''
        }
        _minuteCrosshairView = view
      } catch (_) { /* 预期内 */ }
    }
    /** 还原分时十字光标水平标签的库实现（退出分时/清理时调用） */
    const restoreMinuteCrosshairRebase = () => {
      if (_minuteCrosshairView) {
        try { delete _minuteCrosshairView.getText } catch (_) { /* 预期内 */ }
        _minuteCrosshairView = null
      }
      _minuteCrosshairOrigGetText = null
    }

    /**
     * 确保「0 轴线指标」已创建（不存在才创建，已存在直接返回）。
     *
     * 与日K收盘线同理：指标线由库的逐帧重绘管线画出来，每次绘制都用
     * 当前轴范围现算坐标，天然跟随 Y 轴锁定范围，不存在 overlay 那种
     * 「创建时刻坐标过期 → 错位 / 不渲染」的问题。
     * calc 闭包在每次重算（applyNewData / 实时刷新触发）时读取最新昨收。
     */
    const ensureMinuteZeroLineIndicator = () => {
      const chart = chartRef.value
      if (!chart || !isMinuteLine.value) return
      try {
        let names = []
        try {
          const instances = chart.getIndicatorByPaneId('candle_pane')
          if (instances && typeof instances.keys === 'function') names = Array.from(instances.keys())
        } catch (_) { /* 预期内 */ }
        if (names.includes(MINUTE_ZERO_LINE_IND)) return
        registerIndicator({
          name: MINUTE_ZERO_LINE_IND,
          shortName: '',
          series: 'price',
          precision: 2,
          figures: [{ key: 'zeroLine', title: '', type: 'line' }],
          styles: {
            lines: [{ color: '#999999', style: 'dashed', dashedValue: [4, 4], size: 1 }]
          },
          calc: (list) => {
            const v = minutePrevClose.value
            return (list || []).map(() => ({ zeroLine: (v != null && v > 0) ? v : null }))
          },
          draw: MINUTE_ZERO_LINE_DRAW
        })
        // isStack 必须为 true：klinecharts 的 IndicatorStore.addInstance 在 isStack=false
        // 时会执行 `paneInstances = []`，把该 pane 上已有的指标（均价线等）全部清空
        chart.createIndicator(MINUTE_ZERO_LINE_IND, true, { id: 'candle_pane' })
      } catch (_) { /* 预期内：指标未就绪时由自愈重试补上 */ }
    }

    /**
     * 分时图：把主图 Y 轴范围锁定为「以昨收为中心」的对称区间。
     *
     * 首选方案：直接调用 YAxisImp.setRange() 锁定范围。
     * - AxisImp.buildTicks() 只在 `_autoCalcTickFlag === true` 时才重算范围，
     *   而 setRange() 会把它置为 false → 范围被完全固定，不再受数据/指标/重排影响。
     * - 这样同时绕开了 klinecharts 默认的不对称 gap（top 0.2 / bottom 0.1），
     *   该 gap 会把中心整体上移，是「看不出居中」的根因之一。
     * - 该方式与指标实例无关，因此不会被 updateIndicators 的指标增删（尤其是
     *   isStack=false 时 `paneInstances = []` 清空主图指标）连带清掉。
     *
     * 兜底方案：0 轴线指标（MINUTE_ZERO_LINE_IND）的 minValue / maxValue 会计入
     * YAxisImp.calcRange 的范围计算（min = min(specifyMin, ...)、max = max(specifyMax, ...)，
     * percentage 轴下库会先把这些绝对价格换算成涨跌幅再叠加 gap），
     * 配合上下相等的 gap 也能撑出对称区间。仅在 setRange 不可用时启用。
     *
     * 涨跌幅轴（percentage）：锁定范围用百分比单位，以 pct(昨收) 为中心对称展开；
     * pct 基准 = 首个可见柱收盘（与库 convertToPixel 的换算基准一致），
     * 最大偏离按 |涨跌幅| 的绝对值取对称 → 跌0%涨10% 时范围 ±10%+余量，0 轴线恒居中。
     */
    /**
     * 分时昨收的「取值即得」多级兜底解析（自愈）：
     * fetchMinutePrevClose 是异步链路（props / 1m 推导 / 日线接口），任一环节失败
     * 都可能让 minutePrevClose 停留在 null —— 表现为 0 轴线消失、Y 轴不居中，
     * 且每次实时刷新 Y 轴随数据自动重算（视觉上整窗抖动）。
     * 这里在每次应用锁定时就地解析：只要图表上有任何真实数据就一定拿得到昨收。
     * @returns {number|null}
     */
    const resolveMinutePrevClose = () => {
      if (minutePrevClose.value != null && minutePrevClose.value > 0) {
        return minutePrevClose.value
      }
      // 父组件显式传入
      if (props.prevClose != null && Number(props.prevClose) > 0) {
        minutePrevClose.value = Number(props.prevClose)
        _minutePcSource = 'props'
        return minutePrevClose.value
      }
      const realBars = (klineData.value || []).filter(b => b && b.timestamp)
      if (realBars.length === 0) return null
      // 【关键】klineData 的真实柱只有「正在展示的交易日」，
      // 推导上一交易日收盘必须用加载时保存的跨日 1m 原始数据（_minuteRawData）
      const displayDay = minuteDateStr(realBars[realBars.length - 1].timestamp)
      const srcData = (_minuteRawData && _minuteRawData.length > 0) ? _minuteRawData : realBars
      try {
        const fromBars = derivePrevCloseFromMinuteBars(srcData, displayDay)
        if (fromBars != null && fromBars > 0) {
          minutePrevClose.value = fromBars
          _minutePcSource = '1m推导(自愈)'
          return fromBars
        }
      } catch (_) { /* 预期内 */ }
      // 今日首柱 开/收 近似
      const first = realBars[0]
      const openVal = Number(first.open)
      const closeVal = Number(first.close)
      const approx = (Number.isFinite(openVal) && openVal > 0)
        ? openVal
        : ((Number.isFinite(closeVal) && closeVal > 0) ? closeVal : null)
      if (approx != null) {
        minutePrevClose.value = approx
        _minutePcSource = '今开近似'
        return approx
      }
      // 最后真实柱收盘
      const lastClose = Number(realBars[realBars.length - 1].close)
      if (Number.isFinite(lastClose) && lastClose > 0) {
        minutePrevClose.value = lastClose
        _minutePcSource = '最新价近似'
        return lastClose
      }
      return null
    }

    const applyMinutePrevCloseAxis = () => {
      const chart = chartRef.value
      if (!chart || !isMinuteLine.value) return
      const pc = resolveMinutePrevClose()
      if (pc == null || !(pc > 0)) return
      const realBars = (klineData.value || []).filter(b => b && b.timestamp)
      if (realBars.length === 0) return

      const axis = getCandleYAxis()
      // 轴型分支：normal=价格单位；percentage=涨跌幅单位（库以「首个可见柱收盘」
      // 为基准换算 convertToPixel/calcRange，锁定范围必须用同一基准换算成百分比，
      // 否则只能放弃锁定 → 轴退回「按数据 min~max 铺满」，0 轴线无法居中）
      const axisType = axis && typeof axis.getType === 'function' ? axis.getType() : 'normal'
      const percentMode = axisType === 'percentage'
      let baseClose = null
      let pcPct = 0
      if (percentMode) {
        try {
          const dl = chart.getDataList ? chart.getDataList() : (klineData.value || [])
          const vr = typeof chart.getVisibleRange === 'function' ? chart.getVisibleRange() : null
          const fd = (vr && Number.isInteger(vr.from) && dl[vr.from]) ? dl[vr.from] : dl[0]
          const c = Number(fd && fd.close)
          if (Number.isFinite(c) && c > 0) {
            baseClose = c
            // 昨收的涨跌幅（相对首个可见柱收盘，与库的百分比换算一致）
            pcPct = (pc - baseClose) / baseClose * 100
          }
        } catch (_) { /* 预期内 */ }
        // 基准缺失时放弃锁定，避免把价格值当百分比值用导致显示错乱
        if (baseClose == null) return
      }
      // 涨跌幅轴下隐藏库的最新价标签（基准=今开，与昨收定基刻度矛盾）；金额轴保留
      setMinutePriceTagHidden(percentMode)
      // 分时 Y 轴自定义刻度（涨跌幅轴：昨收定基 1 位小数；金额轴：刻度 ≤3 位小数）
      installMinuteAxisTicks(axis)
      // 十字光标读数昨收定基（涨跌幅轴）+ 价格读数 ≤3 位小数
      installMinuteCrosshairRebase()

      // 取所有参与 Y 轴极值计算的价格相对昨收的最大【绝对值】偏离
      // （涨跌不对称时按大的那一侧对称展开：跌0%涨10% → 范围 ±10%+余量）
      // 注：均价线（AVP）是各柱典型价的加权平均，恒在 [min(low), max(high)] 区间内，
      // 不参与极值计算也不会越界（均价线取值恒在当日最低/最高之间）
      let maxDev = 0
      realBars.forEach(b => {
        [b.close, b.high, b.low].forEach(v => {
          if (v == null || !Number.isFinite(v)) return
          const d = Math.abs(v - pc)
          if (d > maxDev) maxDev = d
        })
      })
      if (!(maxDev > 0)) maxDev = Math.abs(pc) * 0.001

      // 分时极坐标模式：范围精确锁定为 昨收×(1∓涨跌停%)，顶部=+limit%、底部=-limit%
      // （0 轴线居中；不叠加 padding，保证涨/跌停刻度贴边）。关闭时为自适应对称范围。
      const polarLimit = _minutePolarEnabled ? _minutePolarLimit(props.market, props.symbol) : 0
      const halfPrice = polarLimit > 0
        ? pc * (polarLimit / 100)
        : maxDev * (1 + MINUTE_AXIS_PADDING)
      const fromPrice = pc - halfPrice
      const toPrice = pc + halfPrice
      // 锁定范围单位跟随轴型：percentage 轴用百分比单位，且以 pct(昨收) 为中心
      // （昨收未必等于基准价，如今开≠昨收时中心点不是 0%，而是昨收的涨跌幅）
      let from
      let to
      if (percentMode) {
        const halfPct = maxDev / baseClose * 100 * (1 + MINUTE_AXIS_PADDING)
        from = pcPct - halfPct
        to = pcPct + halfPct
      } else {
        from = fromPrice
        to = toPrice
      }

      // 锚点未变化且仍处于锁定态则跳过，避免实时刷新时反复重排
      if (axis && _minuteAxisRange &&
          Math.abs(_minuteAxisRange.from - from) < 1e-10 &&
          Math.abs(_minuteAxisRange.to - to) < 1e-10 &&
          typeof axis.getAutoCalcTickFlag === 'function' &&
          axis.getAutoCalcTickFlag() === false) return

      // 无条件输出一行诊断日志（按 昨收+来源+范围 去重），便于现场确认昨收取值与来源
      const logKey = `${pc}|${_minutePcSource}|${axisType}|${polarLimit}|${from}|${to}`
      if (logKey !== _minutePcLogKey) {
        _minutePcLogKey = logKey
        console.info(`[KlineChart] 分时0轴：昨收=${pc}（来源=${_minutePcSource || '已缓存'}） 轴=${axisType}${polarLimit > 0 ? ` 极坐标=±${polarLimit}%` : ''} 范围=[${from.toFixed(4)}, ${to.toFixed(4)}]`)
      }

      // ---- 0 轴线指标（原生逐帧重绘，画在昨收价上）----
      ensureMinuteZeroLineIndicator()

      // ---- 首选：直接锁定 Y 轴范围 ----
      if (axis && typeof axis.setRange === 'function') {
        try {
          axis.setRange({
            from, to,
            range: to - from,
            realFrom: from,
            realTo: to,
            realRange: to - from
          })
          _minuteAxisRange = { from, to }
          _minuteAxisLocked = true
          try { axis.buildTicks(true) } catch (_) { /* 预期内：布局时也会重建 */ }
          if (typeof chart.adjustPaneViewport === 'function') {
            chart.adjustPaneViewport(true, true, true, true, true)
          }
          return
        } catch (e) {
          console.warn('[KlineChart] 分时 Y 轴锁定失败，回退到指标锚定:', e)
        }
      }

      // ---- 兜底：用 0 轴线指标本身锚定（minValue/maxValue 参与 calcRange） + 上下相等的 gap ----
      // 注意单位必须是【绝对价格】：库的 calcRange 会按轴型自行换算
      // （percentage 轴先把 min/max 换算成涨跌幅再叠加 gap），传百分比单位反而会错乱
      try {
        registerIndicator({
          name: MINUTE_ZERO_LINE_IND,
          shortName: '',
          series: 'price',
          precision: 2,
          figures: [{ key: 'zeroLine', title: '', type: 'line' }],
          styles: {
            lines: [{ color: '#999999', style: 'dashed', dashedValue: [4, 4], size: 1 }]
          },
          minValue: fromPrice,
          maxValue: toPrice,
          calc: (list) => {
            const v = minutePrevClose.value
            return (list || []).map(() => ({ zeroLine: (v != null && v > 0) ? v : null }))
          },
          draw: MINUTE_ZERO_LINE_DRAW
        })
        // 先移除旧实例，确保新锚点生效
        try { chart.removeIndicator('candle_pane', MINUTE_ZERO_LINE_IND) } catch (_) { /* 预期内：首次尚未创建 */ }
        // 注意：isStack 必须为 true。klinecharts 的 IndicatorStore.addInstance 在
        // isStack=false 时会执行 `paneInstances = []`，把该 pane 上已有的指标
        // （如分时主图的均价线）全部清空，导致均价线消失。
        chart.createIndicator(MINUTE_ZERO_LINE_IND, true, { id: 'candle_pane' })
        _minuteAxisRange = { from, to }
        _minuteAxisLocked = true
      } catch (e) {
        console.warn('[KlineChart] 分时 Y 轴对称锚定失败:', e)
      }

      try {
        // 上下 gap 相等 → 昨收恰好落在垂直中心（库默认 0.2/0.1 不对称会导致偏移）
        chart.setPaneOptions({ id: 'candle_pane', gap: { top: 0.15, bottom: 0.15 } })
      } catch (_) { /* 预期内 */ }
      if (percentMode) {
        try { axis && axis.buildTicks(true) } catch (_) { /* 预期内 */ }
      }
    }

    /** 分时图：解除 Y 轴范围锁定并还原默认 gap（切换回普通周期时调用） */
    const clearMinutePrevCloseAxis = () => {
      const chart = chartRef.value
      // 还原分时 Y 轴自定义刻度（重定基刻度仅分时涨跌幅轴使用）
      restoreMinuteAxisTicks()
      // 还原十字光标水平标签的库实现
      restoreMinuteCrosshairRebase()
      // 还原库的最新价标签
      setMinutePriceTagHidden(false)
      try {
        if (chart && typeof chart.removeIndicator === 'function') {
          // 移除 0 轴线指标；同时清理旧版锚定指标名，防止历史实例残留
          chart.removeIndicator('candle_pane', MINUTE_ZERO_LINE_IND)
          try { chart.removeIndicator('candle_pane', 'MINUTE_PREV_CLOSE_AXIS') } catch (_) { /* 预期内：旧版无实例 */ }
        }
      } catch (_) { /* 预期内 */ }
      _minuteAxisRange = null
      const axis = getCandleYAxis()
      if (_minuteAxisLocked && axis) {
        try {
          // 还原为自动计算刻度（等价于双击右轴复位）
          if (typeof axis.setAutoCalcTickFlag === 'function') axis.setAutoCalcTickFlag(true)
          if (chart && typeof chart.adjustPaneViewport === 'function') {
            chart.adjustPaneViewport(true, true, true, true, true)
          }
        } catch (_) { /* 预期内 */ }
      }
      if (_minuteAxisLocked && chart && typeof chart.setPaneOptions === 'function') {
        try {
          // 还原为 klinecharts 默认 gap
          chart.setPaneOptions({ id: 'candle_pane', gap: { top: 0.2, bottom: 0.1 } })
        } catch (_) { /* 预期内 */ }
      }
      _minuteAxisLocked = false
    }

    /**
     * 分时模式：确保主图的均价线（AVP）与 Y 轴锚定指标仍然存在。
     *
     * 背景：klinecharts 的 IndicatorStore.addInstance 在 isStack=false 时会执行
     * `paneInstances = []`，把该 pane 上已有的指标全部清空。updateIndicators 里
     * 往 candle_pane 创建主图指标时若传了 false，就会连带清掉 AVP 与锚定指标，
     * 表现为「均价线消失 / Y 轴不再以昨收为中心」。这里在指标刷新后补挂恢复。
     */
    const ensureMinuteIndicators = () => {
      const chart = chartRef.value
      if (!chart || !isMinuteLine.value) return
      let names = []
      try {
        const instances = chart.getIndicatorByPaneId('candle_pane')
        if (instances && typeof instances.keys === 'function') {
          names = Array.from(instances.keys())
        }
      } catch (_) { /* 预期内 */ }
      try {
        if (!names.includes('AVP')) {
          // isStack=true 追加，避免把别人的指标清掉
          const paneId = chart.createIndicator(MINUTE_AVP_OPTIONS(), true, { id: 'candle_pane' })
          if (paneId) _avpPaneId = paneId
        }
        // 0 轴线/锚定指标被清掉时，强制重建（清掉缓存锚点，跳过「未变化」短路）
        if (!names.includes(MINUTE_ZERO_LINE_IND)) _minuteAxisRange = null
        applyMinutePrevCloseAxis()
      } catch (_) { /* 预期内 */ }
    }

    /**
     * 分时图：以昨收为中心绘制 0 轴线（原生指标线），并锁定 Y 轴为对称区间。
     *
     * 首次进入分时图表时容器可能尚未完成布局，此时 Y 轴高度为 0，
     * 锁定会被后续的 resize / 铺满动作冲掉，因此这里排几次延时重试做自愈。
     */
    const setupMinutePrevCloseReference = () => {
      const chart = chartRef.value
      if (!chart || !isMinuteLine.value) return
      // 取值即得：即使异步获取链路（props/1m推导/日线接口）未完成，
      // 也会就地从已加载的真实柱解析出昨收，保证 0 轴线与居中不缺席
      const pcReady = resolveMinutePrevClose()
      if (pcReady == null || !(pcReady > 0)) return

      const drawZeroLine = () => {
        // 0 轴线指标（原生逐帧重绘，与日K收盘线同机制）+ 锁定 Y 轴（对称 → 昨收恰在垂直中心）
        ensureMinuteZeroLineIndicator()
        applyMinutePrevCloseAxis()
      }

      drawZeroLine()
      // 布局/铺满/指标刷新都可能把 Y 轴重置，延时自愈重试
      ;[150, 400, 1000].forEach(delay => {
        safeTimeout(() => {
          if (!isMinuteLine.value || !chartRef.value) return
          if (resolveMinutePrevClose() == null) return
          drawZeroLine()
        }, delay)
      })
    }

    /** 分时图：清除 0 轴线并解除 Y 轴锚定 */
    const clearMinutePrevCloseReference = () => {
      // 取消尚未执行的分时全量写入节流定时器，避免切走之后旧数据覆盖新周期
      if (_minuteApplyTimer != null) {
        clearTimeout(_minuteApplyTimer)
        _timers.delete(_minuteApplyTimer)
        _minuteApplyTimer = null
        _minuteApplyPending = null
      }
      // 0 轴线指标在 clearMinutePrevCloseAxis 中统一移除
      clearMinutePrevCloseAxis()
    }

    /** 设置图表为分时图模式 */
    const applyMinuteLineChartStyle = () => {
      if (!chartRef.value) return

      try {
        // 1. 设置面积图样式：使用 close 值画面积线（蓝色），K 线柱体全部透明
        //    注意：klinecharts 的 area.value 不支持自定义字段名，
        //    因此面积线使用 close 价格，均价线通过内置 AVP 指标叠加显示
        chartRef.value.setStyles({
          candle: {
            type: 'area',
            area: {
              lineColor: '#1890ff',
              lineSize: 2,
              style: 'stroke',
              backgroundColor: 'rgba(24, 144, 255, 0.08)'
            },
            bar: {
              upColor: 'transparent',
              downColor: 'transparent',
              noChangeColor: 'transparent',
              upBorderColor: 'transparent',
              downBorderColor: 'transparent',
              noChangeBorderColor: 'transparent',
              upWickColor: 'transparent',
              downWickColor: 'transparent',
              noChangeWickColor: 'transparent'
            },
            priceMark: {
              show: true,
              // 最新价标记保持关闭：当前价格由常驻 tooltip 呈现，避免与 0 轴线/左轴刻度视觉冲突
              last: {
                show: false
              }
            },
            tooltip: {
              showRule: 'always',
              showType: 'standard',
              // klinecharts 9.8 的 candle.tooltip 自定义行已改为 custom（旧 labels/values 不再被消费）；
              // 均价一行由主图上的内置 AVP 指标自动附带（figures.title = '均价: '）
              custom: (data) => {
                const cur = data && data.current ? data.current : {}
                const d = new Date(cur.timestamp)
                const p = pricePrecision.value
                const timeStr = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
                const close = Number(cur.close)
                const valid = cur.close != null && Number.isFinite(close)
                return [
                  { title: '时间', value: timeStr },
                  { title: '价格', value: valid ? close.toFixed(p) : '--' },
                  { title: '成交量', value: String(cur.volume || 0) }
                ]
              }
            }
          },
          // 禁用滚动条视觉
          scroll: {
            horizontal: { bar: { style: 'none' } },
            vertical: { bar: { style: 'none' } }
          },
          // 分时下隐藏指标 tooltip 的 name 行：库会先画 shortName（"均价"）再画
          // figures.title（"均价: 34.13"），两行叠加显示成「均价: 均价: 34.13」；
          // 关闭 name 行后只剩黄色单行「均价: 34.13」，与真实分时软件一致。
          // （恢复日K时在 restoreNormalChartStyle 中显式还原为 true）
          indicator: {
            tooltip: { showName: false }
          }
        })

        // 2. 添加内置均价线指标 AVP（klinecharts 9.8 官方自带）
        try {
          if (_avpPaneId) {
            // 重进分时先移除上一轮 AVP，避免重复叠加（指标不存在时为静默 no-op）
            try { chartRef.value.removeIndicator(_avpPaneId, 'AVP') } catch (_) { /* 预期内：尚未创建 */ }
            _avpPaneId = null
          }
          // isStack=true 追加。传 false 时 klinecharts 会执行 `paneInstances = []`，
          // 把主图已有的指标（0 轴线等）清空；此处已先移除旧 AVP，用 true 更安全
          const paneId = chartRef.value.createIndicator(MINUTE_AVP_OPTIONS(), true, { id: 'candle_pane' })
          if (paneId) {
            _avpPaneId = paneId
            // 注意：不加入 addedIndicatorIds，避免被 updateIndicators 清除
          }
        } catch (avpErr) {
          console.warn('添加均价线指标(AVP)失败:', avpErr)
        }

        // 2.5 分时专用：昨日首板价 0 轴线 + Y 轴以昨收为中心
        setupMinutePrevCloseReference()

        // 3. 铺满全部数据并锁定交互：X 轴固定覆盖整个交易时段（9:30 - 15:00）
        //    铺满需要在滚动能力未被禁用时执行，故先 fit 再 disable
        fitMinuteLineView()
        // 铺满会重排绘图区并重算刻度，之后必须再锁定一次 Y 轴范围
        applyMinutePrevCloseAxis()
        disableMinuteInteractions()
        // 锁定主图 Y 轴手势（右轴拖拽/滚轮缩放），Y 轴自动跟随数据
        lockMinutePaneAxes()
        // 布局/指标副图就绪后再校准一次（首次铺满时绘图区宽度可能尚未稳定）
        safeTimeout(() => {
          if (!isMinuteLine.value) return
          fitMinuteLineView()
          applyMinutePrevCloseAxis()
        }, 150)
      } catch (e) {
        console.warn('设置分时图样式失败:', e)
      }
    }

    /** 恢复普通K线图样式 */
    const restoreNormalChartStyle = () => {
      if (!chartRef.value) return

      try {
        // 1. 移除分时图添加的均价线指标（内置 AVP）
        if (_avpPaneId) {
          try {
            chartRef.value.removeIndicator(_avpPaneId, 'AVP')
          } catch (_) { /* 预期内：指标可能已被移除 */ }
          _avpPaneId = null
        }

        // 1.5 清除分时 0 轴线并恢复默认 Y 轴
        clearMinutePrevCloseReference()

        // 2. 恢复滚轮和触摸交互（含主图 Y 轴手势）
        enableMinuteInteractions()
        unlockMinutePaneAxes()

        // 3. 恢复蜡烛图样式和滚动配置
        //    必须显式清除 area 配置并恢复 bar 颜色，
        //    因为 klinecharts 的 setStyles 是深度合并，不会自动重置旧字段
        const isDark = chartTheme.value === 'dark'
        chartRef.value.setStyles({
          candle: {
            type: 'candle_solid',
            // 清除分时图的 area 配置
            area: {
              lineColor: 'transparent',
              lineSize: 0,
              style: 'stroke',
              backgroundColor: 'transparent'
            },
            // 恢复 K 线柱体颜色
            bar: {
              upColor: isDark ? '#ef5350' : '#f5222d',
              downColor: isDark ? '#0ecb81' : '#52c41a',
              noChangeColor: isDark ? '#888' : '#999',
              upBorderColor: isDark ? '#ef5350' : '#f5222d',
              downBorderColor: isDark ? '#0ecb81' : '#52c41a',
              noChangeBorderColor: isDark ? '#888' : '#999',
              upWickColor: isDark ? '#ef5350' : '#f5222d',
              downWickColor: isDark ? '#0ecb81' : '#52c41a',
              noChangeWickColor: isDark ? '#888' : '#999'
            },
            // 恢复价格标记
            priceMark: {
              show: true,
              last: {
                show: true,
                color: isDark ? '#d1d4dc' : '#333',
                lineStyle: 'dashed'
              }
            },
            // 恢复日K tooltip：9.8 起 candle.tooltip.labels/values 已不再被消费（死配置），
            // 这里只需把分时的 custom 回调清掉——setStyles 是深度合并，不清会残留分时的三行
            tooltip: {
              showRule: 'always',
              showType: 'standard',
              custom: null
            }
          },
          // 滚动/缩放能力的恢复由 enableMinuteInteractions 通过 klinecharts API 完成
          scroll: {
            horizontal: { bar: { style: 'normal' } },
            vertical: { bar: { style: 'normal' } }
          },
          // 还原分时模式关闭的指标 tooltip name 行（MA/BOLL 等指标名行恢复显示）
          indicator: {
            tooltip: { showName: true }
          }
        })
      } catch (e) {
        console.warn('恢复K线图样式失败:', e)
      }
    }

    const loadKlineData = async (silent = false) => {
      if (!props.symbol) return
      if (loading.value && !silent) return

      // 立即停止旧的实时数据源（WS / REST），防止旧标的数据污染新数据
      stopRealtime()

      loading.value = true
      error.value = null
      // 换标的时才清空昨收缓存；同标的的常规刷新（轮询/边界/看门狗重载）保留缓存，
      // 避免重载到异步重取昨收之间 Y 轴短暂失去锚定，表现为每轮刷新闪烁一下
      const _pcKey = `${props.market || ''}|${props.symbol || ''}`
      if (_minutePcSymbolKey !== _pcKey) {
        _minutePcSymbolKey = _pcKey
        minutePrevClose.value = null
        _minutePcSource = ''
      }

      // 分时图模式：使用1m数据
      const loadTimeframe = isMinuteLine.value ? '1m' : props.timeframe

      try {
        let formattedData = []
        try {
          const response = await request({
            url: '/api/indicator/kline',
            method: 'get',
            params: {
              market: props.market,
              symbol: props.symbol,
              timeframe: loadTimeframe,
              limit: 1000
            }
          })

          if (response.code === 1 && response.data && Array.isArray(response.data)) {
            formattedData = formatKlineData(response.data)
          } else {
            // 特殊处理 Tiingo 订阅限制提示
            let errMsg = response.msg || '获取K线数据失败'
            if (response.hint === 'tiingo_subscription') {
              errMsg = proxy.$t('dashboard.indicator.error.tiingoSubscription') || 'Forex 1-minute data requires Tiingo paid subscription'
            }
            throw new Error(errMsg)
          }
        } catch (apiErr) {
          throw apiErr
        }

        // 检查数据是否为空
        if (!formattedData || formattedData.length === 0) {
          throw new Error('未获取到K线数据')
        }

        // 分时图模式：处理数据
        if (isMinuteLine.value) {
          const minuteData = processMinuteLineData(formattedData)
          if (minuteData.length === 0) {
            throw new Error('今日暂无分时数据')
          }
          klineData.value = minuteData
          // 保存跨日 1m 原始数据：昨收自愈推导需要（klineData 仅含展示当日）
          _minuteRawData = formattedData
          // 获取昨日收盘价（昨收），用于 Y 轴居中与 0 轴线
          // 传入 1m 原始数据：昨收优先从中推导，避免依赖额外的日线接口
          await fetchMinutePrevClose(formattedData)
        } else {
          klineData.value = formattedData
        }

        // 分时模式只需当日数据，不参与历史加载
        hasMoreHistory.value = !isMinuteLine.value

        // 根据数据自动推算价格精度并设置到图表
        const realBars = klineData.value.filter(item => item && item.timestamp)
        pricePrecision.value = calcPricePrecision(realBars)

        const internalData = convertToInternalFormat(realBars)
        updatePricePanel(internalData, { force: true })

        nextTick(() => {
          if (!chartRef.value) {
            initChart()
          } else {
            // 设置图表精度（必须在 applyNewData 之前）
            if (typeof chartRef.value.setPriceVolumePrecision === 'function') {
              chartRef.value.setPriceVolumePrecision(pricePrecision.value, 0)
            }

            // 确保数据格式正确
            const validData = klineData.value.filter(item =>
              item.timestamp &&
              !isNaN(item.open) &&
              !isNaN(item.high) &&
              !isNaN(item.low) &&
              !isNaN(item.close)
            )

            if (validData.length > 0 && chartRef.value) {
              // 使用 applyNewData 初始化
              try {
                chartRef.value.applyNewData(validData)
              } catch (e) {
                chartRef.value.applyNewData(validData)
              }

              // Y 轴复位为自动贴合：手动缩放状态不清除会让新数据卡在旧范围里
              resetYAxisToAuto()
              // 换股场景：X 视口回归默认缩放（滚动到最新由外部 600ms 兼容逻辑负责）
              if (_resetViewportOnNextLoad) {
                _resetViewportOnNextLoad = false
                if (!isMinuteLine.value && typeof chartRef.value.setBarSpace === 'function') {
                  try { chartRef.value.setBarSpace(8) } catch (_) { /* 预期内 */ }
                }
              }

              // 分时图模式：应用面积图样式
              if (isMinuteLine.value) {
                applyMinuteLineChartStyle()
              } else {
                restoreNormalChartStyle()
                // 恢复该周期上次的图表现场（分时为锁定视图，无需恢复）
                restoreChartScene(props.timeframe)
              }

              // 确保 VOL 副图指标存在（applyNewData 可能导致 VOL pane 数据绑定丢失）
              // 先移除旧 VOL pane，避免重复创建
              // VOL 现在作为可选内置指标，通过 updateIndicators 管理
              // 延迟更新指标（P0-1 受管 timer + P0-2 symbol 一致性校验）
              const _indSymbol = props.symbol
              safeTimeout(() => {
                // 已切换到其他标的 → 丢弃本次回调，防止旧标的指标写入新图表
                if (props.symbol !== _indSymbol) return
                if (chartRef.value) {
                  updateIndicators()
                }
              }, 100)
              // K 线加载完成后计算筹码分布
              fetchChipData()
              // 左轴百分比列：数据就绪后重建同步（换标的/换周期均可能换轴实例或改最新收盘基准）
              _syncPctRuler()
            }
          }

          if (props.realtimeEnabled) {
            startRealtime()
          }

          // 如果初始数据明显不足（如美股小时线），自动补充加载历史（分时模式跳过）
          if (!isMinuteLine.value && formattedData.length < 200 && hasMoreHistory.value) {
            // P0-2: 闭包捕获发起时的 symbol，避免切换股票后旧回调把历史数据写入新图表
            const _histSymbol = props.symbol
            safeTimeout(() => {
              // 已切换到其他标的 → 丢弃本次回调（此时 klineData 已属于新标的）
              if (props.symbol !== _histSymbol) return
              if (klineData.value.length > 0 && klineData.value.length < 200 && hasMoreHistory.value) {
                loadMoreHistoryDataForScroll(klineData.value[0].timestamp)
              }
            }, 1500)
          }
        })
      } catch (err) {
        error.value = proxy.$t('dashboard.indicator.error.loadDataFailed') + ': ' + (err.message || proxy.$t('dashboard.indicator.error.loadDataFailedDesc'))
        // 清空K线数据，不显示图表
        klineData.value = []
        // 如果有图表实例，清空数据
        if (chartRef.value) {
          try {
            chartRef.value.applyNewData([])
          } catch (e) {
          }
        }
      } finally {
        loading.value = false
      }
    }

    // 加载更多历史数据（用于滚动加载，保持滚动位置）
    const loadMoreHistoryDataForScroll = async (timestamp) => {
      if (!props.symbol || !klineData.value || klineData.value.length === 0) {
        return
      }

      // 分时模式下不加载历史数据（只需当日数据）
      if (isMinuteLine.value) {
        return
      }

      // 【核心修复】防止重复请求：如果已经有正在进行的请求，直接返回
      if (loadingHistory.value || loadingHistoryPromise) {
        // 如果有正在进行的请求，等待它完成
        if (loadingHistoryPromise) {
          try {
            await loadingHistoryPromise
          } catch (e) {
          }
        }
        return
      }

      if (!hasMoreHistory.value) {
        // 如果没有更多数据，通知图表
        if (chartRef.value && typeof chartRef.value.noMoreData === 'function') {
          chartRef.value.noMoreData()
        }
        return
      }

      // 立即设置加载状态和创建 Promise，防止并发请求
      loadingHistory.value = true
      loadingHistoryPromise = (async () => {
        // 强制触发更新
        await nextTick()

        try {
        // timestamp 是毫秒时间戳，转换为秒级用于 API
        const beforeTime = Math.floor(timestamp / 1000)

        const response = await request({
          url: '/api/indicator/kline',
          method: 'get',
          params: {
            market: props.market,
            symbol: props.symbol,
            timeframe: isMinuteLine.value ? '1m' : props.timeframe,
            limit: 1000,
            before_time: beforeTime // 获取此时间之前的数据
          }
        })

        if (response.code === 1 && response.data && Array.isArray(response.data)) {
          const newData = formatKlineData(response.data)

          if (newData.length === 0) {
            // 没有更多数据了
            hasMoreHistory.value = false
            if (chartRef.value && typeof chartRef.value.noMoreData === 'function') {
              chartRef.value.noMoreData()
            }
            return
          }

          // 确保新数据的时间早于传入的时间戳
          const filteredNewData = newData.filter(item => item.timestamp < timestamp)

          if (filteredNewData.length === 0) {
            // 没有更早的数据了
            hasMoreHistory.value = false
            if (chartRef.value && typeof chartRef.value.noMoreData === 'function') {
              chartRef.value.noMoreData()
            }
            return
          }

          // 保存当前可见范围，用于恢复滚动位置
          // klinecharts 9.x 的 getVisibleRange() 返回的 from/to 是数据索引（整数），不是百分比
          let savedVisibleRange = null
          try {
            if (chartRef.value && typeof chartRef.value.getVisibleRange === 'function') {
              savedVisibleRange = chartRef.value.getVisibleRange()
            }
          } catch (e) {
          }

          // 记录新数据的数量，用于后续计算偏移
          const newDataCount = filteredNewData.length

          // 将新数据插入到现有数据的前面
          klineData.value = [...filteredNewData, ...klineData.value]

          // 使用 applyNewData 添加历史数据（applyMoreData 在 v9.8.0 已废弃）
          nextTick(() => {
            if (chartRef.value) {
              // 应用新数据
              chartRef.value.applyNewData(klineData.value)

              // 恢复滚动位置
              // 由于新数据插入到了前面，原来的索引需要偏移 newDataCount
              if (savedVisibleRange && typeof savedVisibleRange.from === 'number') {
                // 计算新的可见范围索引
                // 原来看的是索引 from 到 to 的数据，现在这些数据的索引变成了 from + newDataCount 到 to + newDataCount
                const newFrom = savedVisibleRange.from + newDataCount

                // P1-4: 用 nextTick 替代「赌 50ms 渲染完成」——渲染完成即执行，不再依赖固定延时
                const _scrollSymbol = props.symbol
                nextTick(() => {
                  // 已切换标的 → 旧的滚动位置偏移量无意义，丢弃
                  if (props.symbol !== _scrollSymbol) return
                  if (!chartRef.value) return
                  try {
                    if (typeof chartRef.value.scrollToDataIndex === 'function') {
                      chartRef.value.scrollToDataIndex(newFrom)
                    }
                  } catch (e) {
                    // P1-3: 恢复滚动位置属增强逻辑，失败不影响主流程，但需留痕
                    console.warn('[KlineChart] 恢复滚动位置失败:', e)
                  }
                })
              }

              // 更新指标
              updateIndicators()
            }
          })
        } else {
          // API返回错误，通知图表加载失败
          if (chartRef.value && typeof chartRef.value.noMoreData === 'function') {
            chartRef.value.noMoreData()
          }
        }
        } catch (err) {
          // 加载失败，通知图表
          if (chartRef.value && typeof chartRef.value.noMoreData === 'function') {
            chartRef.value.noMoreData()
          }
        } finally {
          loadingHistory.value = false
          loadingHistoryPromise = null // 清除请求追踪
        }
      })() // 立即执行 Promise

      // 等待请求完成
      try {
        await loadingHistoryPromise
      } catch (err) {
        // 错误已经在内部的 catch 中处理，这里只是确保 Promise 完成
      }
    }

    // 增量更新K线数据（实时更新）
    const updateKlineRealtime = async () => {
      if (!props.symbol || !klineData.value || klineData.value.length === 0) {
        return // 如果没有现有数据，不进行增量更新
      }
      if (realtimeFetchInFlight.value) {
        return
      }
      realtimeFetchInFlight.value = true

      try {
        // 只获取最新的5根K线用于更新
        const response = await request({
          url: '/api/indicator/kline',
          method: 'get',
          params: {
            market: props.market,
            symbol: props.symbol,
            timeframe: isMinuteLine.value ? '1m' : props.timeframe,
            limit: 5 // 只获取最新5根
          }
        })

        if (response.code === 1 && response.data && Array.isArray(response.data) && response.data.length > 0) {
          const newData = formatKlineData(response.data)
          const existingData = [...klineData.value]

          if (newData.length > 0) {
            // 分时模式：走专用合并（通用逻辑按周期边界判重，不适配 1m 柱口径）
            if (isMinuteLine.value) {
              mergeMinuteLineRealtime(newData)
              return
            }
            const lastNewTime = Math.floor(newData[newData.length - 1].timestamp / 1000) // 转回秒级用于比较
            const lastExistingTime = Math.floor(existingData[existingData.length - 1].timestamp / 1000)

            // 判断是否属于同一时间段
            if (isSameTimeframe(lastNewTime, lastExistingTime, props.timeframe)) {
              // 同一时间段，合并更新最后一根K线的数据
              // K线合并规则：
              // - open: 保持不变（时间段开始时的价格）
              // - high: 取最大值（时间段内的最高价）
              // - low: 取最小值（时间段内的最低价）
              // - close: 更新为最新价格（当前价格）
              // - volume: 使用API返回的最新值（API返回的已是该周期的总成交量，无需累加）
              const existingLast = existingData[existingData.length - 1]
              const newLast = newData[newData.length - 1]

              const mergedLast = {
                timestamp: existingLast.timestamp, // 保持原有时间戳（毫秒）
                open: existingLast.open, // 开盘价保持不变
                high: Math.max(existingLast.high, newLast.high), // 最高价取最大值
                low: Math.min(existingLast.low, newLast.low), // 最低价取最小值
                close: newLast.close, // 收盘价更新为最新价格
                volume: newLast.volume // 成交量使用API返回的最新值（已是该周期的总成交量）
              }
              // 与当前最后一根在显示精度下无变化则跳过（减少无意义重绘与父组件刷新）
              if (klineBarSnapshotKey(mergedLast) === klineBarSnapshotKey(existingLast)) {
                return
              }
              existingData[existingData.length - 1] = mergedLast
              klineData.value = existingData

              // 更新价格面板（使用内部格式；实时路径节流 emit）
              const internalData = convertToInternalFormat(klineData.value)
              updatePricePanel(internalData)

              // 合并到下一帧再 updateData，避免同一宏任务内多次改动与库内部重入
              const last = existingData[existingData.length - 1]
              const bar = {
                timestamp: last.timestamp,
                open: last.open,
                high: last.high,
                low: last.low,
                close: last.close,
                volume: last.volume != null ? last.volume : 0
              }
              if (chartRef.value && typeof chartRef.value.updateData === 'function') {
                scheduleRealtimeChartBarUpdate(bar)
              } else if (chartRef.value) {
                try {
                  chartRef.value.applyNewData(klineData.value)
                } catch (_) {
                  // P1-3: applyNewData 失败会导致图表空白，必须留痕
                  console.warn('[KlineChart] applyNewData 失败，图表可能无法渲染:', _)
                }
              }
            } else if (lastNewTime > lastExistingTime) {
              // 新的时间段，追加新数据
              // 先移除可能重复的K线（基于时间段，而不是精确时间戳）
              const uniqueNewData = newData.filter(newItem => {
                const newItemTime = Math.floor(newItem.timestamp / 1000)
                // 检查是否与现有数据中的任何一条属于同一时间段
                return !existingData.some(existingItem => {
                  const existingItemTime = Math.floor(existingItem.timestamp / 1000)
                  return isSameTimeframe(newItemTime, existingItemTime, props.timeframe)
                })
              })

              // 分时模式：只保留当日数据，避免均价线（AVP）基于多日数据计算
              let dataToAppend = uniqueNewData
              if (isMinuteLine.value && uniqueNewData.length > 0) {
                const now = new Date()
                const todayStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
                dataToAppend = uniqueNewData.filter(item => {
                  const d = new Date(item.timestamp)
                  const dateStr = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
                  return dateStr === todayStr
                })
              }

              if (dataToAppend.length > 0) {
                klineData.value = [...existingData, ...dataToAppend]
                // 如果数据超过限制，保留最新的数据
                if (klineData.value.length > 500) {
                  klineData.value = klineData.value.slice(-500)
                }

                // 更新价格面板（使用内部格式）
                const internalData = convertToInternalFormat(klineData.value)
                updatePricePanel(internalData, { force: true })

                // 更新 KLineChart - v9.8.0+ 已移除 applyMoreData，改用 applyNewData
                if (chartRef.value && typeof chartRef.value.applyNewData === 'function') {
                  // 追加新K线，使用 applyNewData（合并后整体刷新）
                  chartRef.value.applyNewData(klineData.value)
                  // 新K线出现时强制刷新一次指标
                  maybeUpdateIndicators(true)
                } else if (chartRef.value) {
                  // 降级方案：使用 applyNewData（会重置滚动位置）
                  chartRef.value.applyNewData(klineData.value)
                  maybeUpdateIndicators(true)
                }
              }
            }
            // 如果新数据的时间更早，说明没有更新，保持原数据不变
          }
        }
      } catch (err) {
        // 增量更新失败时静默处理，不影响现有数据
      } finally {
        realtimeFetchInFlight.value = false
      }
    }

    // ── REST 轮询（非加密市场 / WS 断连临时回退） ──

    /**
     * 分时：把下一次刷新对齐到整分钟过界后 ~1.5s。
     * 分时的最小粒度就是 1 分钟，常规 5s 轮询已经能覆盖，但对齐过界可以确保
     * 「新一分钟的柱子」在出现后第一时间被拉取，不会滞后近一个轮询周期。
     */
    const scheduleMinuteBoundaryRefresh = () => {
      if (_minuteBoundaryTimer) {
        clearTimeout(_minuteBoundaryTimer)
        _timers.delete(_minuteBoundaryTimer)
        _minuteBoundaryTimer = null
      }
      if (!isMinuteLine.value) return
      const now = Date.now()
      // 距下一个整分钟的毫秒数 + 1.5s 后端聚合缓冲
      const delay = (60 - Math.floor(now / 1000) % 60) * 1000 - (now % 1000) + 1500
      _minuteBoundaryTimer = safeTimeout(() => {
        _minuteBoundaryTimer = null
        if (!isMinuteLine.value || !chartRef.value) return
        if (!props.realtimeEnabled || !props.symbol) return
        if (loading.value || loadingHistory.value) return
        // 常规增量刷新
        if (!realtimeFetchInFlight.value) updateKlineRealtime()
        runMinuteStallWatchdog()
        scheduleMinuteBoundaryRefresh()
      }, Math.max(delay, 500))
    }

    /**
     * 分时：数据停滞看门狗。
     * 增量链路任何一环失效（接口无增量、WS 假连）都会表现为「图不动」，
     * 这里在盘中发现长时间没有变化就静默全量重载一次，保证最终一定能追上。
     */
    const runMinuteStallWatchdog = () => {
      try {
        if (!isMinuteLine.value || !props.realtimeEnabled || !props.symbol) return
        if (loading.value || loadingHistory.value) return
        if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
        // 只对「正在显示当日盘面」生效：休市/看历史日期时不重载，避免无谓刷新
        const nowStr = minuteDateStr(Date.now())
        const hasToday = (klineData.value || []).some(b => b && b.timestamp && minuteDateStr(b.timestamp) === nowStr)
        if (!hasToday) return
        const now = Date.now()
        const since = _minuteLastChangeTs || now
        // 超过 2.5 分钟没变化 → 静默全量重载；两次重载至少间隔 3 分钟
        if (now - since < 150000) return
        if (_minuteWatchdogTs && now - _minuteWatchdogTs < 180000) return
        _minuteWatchdogTs = now
        _minuteLastChangeTs = now
        loadKlineData(true)
      } catch (_) { /* 预期内：重载失败等下一轮 */ }
    }

    const startRestPolling = () => {
      if (realtimeTimer.value) {
        clearInterval(realtimeTimer.value)
      }
      const intervalMap = {
        '1m': 5000,
        '5m': 10000,
        '15m': 15000,
        '30m': 30000,
        '1H': 60000,
        // [MODIFIED] 2H/4H K线已移除
        '1D': 600000,
        '1W': 1800000
      }
      const base = intervalMap[props.timeframe] || 10000
      // 分时模式实时性要求更高，固定 5s 轮询（intervalMap 无 '分时' 键）
      realtimeInterval.value = Math.min(Math.max(isMinuteLine.value ? 5000 : base, 2000), 15000)

      if (props.realtimeEnabled && props.symbol && klineData.value.length > 0) {
        realtimeTimer.value = setInterval(() => {
          if (!loading.value && props.symbol && klineData.value && klineData.value.length > 0) {
            updateKlineRealtime()
          }
        }, realtimeInterval.value)
      }

      // 分时：额外挂一个整分钟对齐的强制刷新（新柱出现后 1.5s 内必定补上）
      if (isMinuteLine.value) {
        _minuteLastChangeTs = Date.now()
        scheduleMinuteBoundaryRefresh()
      }
    }

    const stopRestPolling = () => {
      if (realtimeTimer.value) {
        clearInterval(realtimeTimer.value)
        realtimeTimer.value = null
      }
      if (_minuteBoundaryTimer) {
        clearTimeout(_minuteBoundaryTimer)
        _timers.delete(_minuteBoundaryTimer)
        _minuteBoundaryTimer = null
      }
    }

    // ── WebSocket 实时推送处理（高性能路径） ──

    // 待刷新的最新 bar 缓存：WS tick 高频到达时只保留最新值，由 rAF 合并刷新
    let pendingWsBar = null
    let wsTickRafId = null

    const flushWsTick = () => {
      wsTickRafId = null
      if (!wsActive.value) { pendingWsBar = null; return }
      const bar = pendingWsBar
      if (!bar || !chartRef.value) return
      pendingWsBar = null
      scheduleRealtimeChartBarUpdate(bar)
    }

    const handleWsTick = (bar) => {
      // WS 关闭前可能还有残留消息，确认 wsActive 才处理
      if (!wsActive.value) return
      const arr = klineData.value
      if (!arr || arr.length === 0) return

      // 分时模式：WS tick 走专用合并（通用同柱/新柱判断不适配 1m 柱口径）
      if (isMinuteLine.value) {
        mergeMinuteLineRealtime([bar])
        return
      }

      const lastBar = arr[arr.length - 1]

      if (bar.timestamp === lastBar.timestamp) {
        // 同一根K线内更新：原地修改最后一个元素，避免整个数组拷贝
        const newHigh = Math.max(lastBar.high, bar.high)
        const newLow = Math.min(lastBar.low, bar.low)
        if (lastBar.close === bar.close &&
            lastBar.high === newHigh &&
            lastBar.low === newLow &&
            lastBar.volume === bar.volume) {
          return // 数值无变化，跳过
        }
        const merged = {
          timestamp: lastBar.timestamp,
          open: lastBar.open,
          high: newHigh,
          low: newLow,
          close: bar.close,
          volume: bar.volume
        }
        arr[arr.length - 1] = merged
        // shallowRef 需要赋值新引用触发响应式；slice 只创建浅拷贝引用数组，不拷贝对象
        klineData.value = arr.slice()

        // 直接用最后两根算价格，避免 convertToInternalFormat 遍历全部 500 根
        updatePricePanelFromLastBars(arr)

        // 如果价格超出当前可见范围，刷新筹码覆盖层
        if (bar.high > lastBar.high || bar.low < lastBar.low) {
          renderChip()
        }

        // 合并到 rAF 再刷新图表（如果 WS tick 1秒来多次，只刷最后一次）
        pendingWsBar = merged
        if (wsTickRafId == null) {
          wsTickRafId = requestAnimationFrame(flushWsTick)
        }
      } else if (bar.timestamp > lastBar.timestamp) {
        // 新K线诞生
        arr.push(bar)
        if (arr.length > 500) {
          arr.splice(0, arr.length - 500)
        }
        klineData.value = arr.slice()

        updatePricePanelFromLastBars(arr, true)

        if (chartRef.value && typeof chartRef.value.applyNewData === 'function') {
          chartRef.value.applyNewData(klineData.value)
        } else if (chartRef.value) {
          chartRef.value.applyNewData(klineData.value)
        }
        // 新K线产生时立即刷新指标
        maybeUpdateIndicators(true)
        renderChip()
      }
    }

    const handleWsNewBar = (_bar) => {
      // newBar 信号在 handleWsTick 的 timestamp 分支中已触发 maybeUpdateIndicators
      // 此回调保留作为语义钩子，不再重复触发
    }

    /** 精简版价格面板更新：只用最后两根 bar，不遍历全量数据 */
    const updatePricePanelFromLastBars = (arr, force) => {
      if (!arr || arr.length === 0) return
      const last = arr[arr.length - 1]
      let payload, sig
      if (arr.length > 1) {
        const prev = arr[arr.length - 2]
        const price = formatPrice(last.close)
        const change = ((last.close - prev.close) / prev.close) * 100
        sig = `${price}|${change.toFixed(3)}`
        payload = { price, change }
      } else {
        const price = formatPrice(last.close)
        sig = `${price}|0`
        payload = { price, change: 0 }
      }
      if (!force && sig === lastPriceEmitSig.value) return
      lastPriceEmitSig.value = sig
      emit('price-change', payload)
    }

    // ── WS 断连/重连回调 ──
    const handleWsReconnecting = () => {
      // WS 断开但正在重连 → 临时启动 REST 轮询保持数据流
      startRestPolling()
    }

    const handleWsReconnected = () => {
      // WS 恢复 → 立即停止 REST 轮询，避免冗余 HTTP 请求
      stopRestPolling()
    }

    const handleWsError = () => {
      wsActive.value = false
      startRestPolling()
    }

    const isCryptoMarket = () => {
      const m = (props.market || '').toLowerCase()
      return m === 'crypto' || m === '' || m === 'cryptocurrency'
    }

    const _fetchExchangeId = async () => {
      const now = Date.now()
      if (_cachedExchangeId && (now - _exchangeIdTs) < 300000) return _cachedExchangeId
      try {
        const res = await request({ url: '/api/settings/public-config', method: 'get' })
        if (res && res.data && res.data.ccxt_default_exchange) {
          _cachedExchangeId = res.data.ccxt_default_exchange
          _exchangeIdTs = now
        }
      } catch (_) { /* keep cached or null */ }
      return _cachedExchangeId || 'binance'
    }

    // 启动实时更新
    const startRealtime = async () => {
      stopRealtime()
      const gen = ++_realtimeGeneration

      if (!props.realtimeEnabled || !props.symbol || klineData.value.length === 0) return

      if (isCryptoMarket()) {
        try {
          const exchangeId = await _fetchExchangeId()
          if (gen !== _realtimeGeneration) return
          if (!wsClient) {
            wsClient = new ExchangeKlineWs()
          }
          wsClient.connect(props.symbol, isMinuteLine.value ? '1m' : props.timeframe, {
            onTick: handleWsTick,
            onNewBar: handleWsNewBar,
            onError: handleWsError,
            onReconnecting: handleWsReconnecting,
            onReconnected: handleWsReconnected
          }, exchangeId)
          wsActive.value = true
        } catch (_) {
          if (gen !== _realtimeGeneration) return
          wsActive.value = false
          startRestPolling()
        }
      } else {
        startRestPolling()
      }

      // 分时：无论走 WS 还是 REST 轮询，都额外挂一个整分钟对齐的刷新，
      // 保证整分钟的新柱一定会被补上（并顺带跑停滞看门狗做兜底）
      if (gen === _realtimeGeneration && isMinuteLine.value) {
        _minuteLastChangeTs = Date.now()
        scheduleMinuteBoundaryRefresh()
      }
    }

    // 停止实时更新
    const stopRealtime = () => {
      stopRestPolling()
      if (wsTickRafId != null) {
        cancelAnimationFrame(wsTickRafId)
        wsTickRafId = null
      }
      pendingWsBar = null
      if (wsClient) {
        wsClient.disconnect()
      }
      wsActive.value = false
    }

    // --- 图表初始化函数 ---
    const initChart = () => {
      const container = document.getElementById('kline-chart-container')
      if (!container) return

      if (container.clientWidth === 0 || container.clientHeight === 0) {
        // 冗余修复：用 ResizeObserver 等待容器获得尺寸，替代原先 200ms × 10 次的定时轮询
        // （项目内 chartResizeObserver / _chipPaneObserver 已在用此机制，此处属于重复造轮子）
        // ResizeObserver 在尺寸变化时被回调，无需空转轮询；2.5 秒兜底后放弃并断开观察
        let settled = false
        const finish = (shouldInit) => {
          if (settled) return
          settled = true
          if (ro) {
            ro.disconnect()
            _observers.delete(ro)
          }
          // P0-1: 已卸载则绝不重建图表（孤儿实例）
          if (shouldInit && !_isUnmounted) initChart()
        }
        const ro = new ResizeObserver(() => {
          const el = document.getElementById('kline-chart-container')
          if (el && el.clientWidth > 0 && el.clientHeight > 0) finish(true)
        })
        _observers.add(ro)
        ro.observe(container)
        // 兜底：超时仍未获得尺寸则放弃（原轮询上限约 2 秒，此处略放宽）
        safeTimeout(() => finish(false), 2500)
        return
      }

      // 如果图表已存在，先销毁
      if (chartRef.value) {
        try {
          chartRef.value.destroy()
        } catch (e) {
          // P1-3: destroy 失败意味着图表实例可能泄漏，必须留痕
          console.warn('[KlineChart] 图表销毁失败，实例可能泄漏:', e)
        }
        chartRef.value = null
        volPaneId.value = null
      }

      try {
        // 初始化 KLineChart
        const container = document.getElementById('kline-chart-container')
        if (!container) {
          throw new Error('容器元素不存在')
        }

        // 设置中文语言

        // 尝试使用配置选项初始化
        try {
          chartRef.value = init(container, {
            locale: 'zh-CN',
            drawingBarVisible: true,
            overlay: { visible: true }
          })
        } catch (e) {
          try {
            chartRef.value = init(container, { locale: 'zh-CN' })
          } catch (_) {
            chartRef.value = init(container)
          }
        }

        // 如果配置选项方式不支持，尝试调用方法启用画线工具栏
        if (chartRef.value && typeof chartRef.value.setDrawingBarVisible === 'function') {
          chartRef.value.setDrawingBarVisible(true)
        } else if (chartRef.value && typeof chartRef.value.setDrawingBar === 'function') {
          chartRef.value.setDrawingBar(true)
        } else if (chartRef.value && typeof chartRef.value.enableDrawing === 'function') {
          chartRef.value.enableDrawing(true)
        }

        if (!chartRef.value) {
          throw new Error('图表初始化失败：无法创建图表实例')
        }
        // 换实例后废弃左轴旧绑定（包装的 buildTicks/订阅），_syncPctRuler 会按新实例重建
        _resetPctAxisBindings()

        // 调试：输出图表实例的所有方法，检查是否有画线工具栏相关的方法
        if (chartRef.value) {
          // 检查是否有内置画线工具栏的方法
          if (typeof chartRef.value.setDrawingBarVisible === 'function') {
            chartRef.value.setDrawingBarVisible(true)
          }
          if (typeof chartRef.value.setDrawingBar === 'function') {
            chartRef.value.setDrawingBar(true)
          }
          if (typeof chartRef.value.enableDrawing === 'function') {
            chartRef.value.enableDrawing(true)
          }
        }

        // 设置价格精度（在 applyNewData 之前）
        if (typeof chartRef.value.setPriceVolumePrecision === 'function') {
          chartRef.value.setPriceVolumePrecision(pricePrecision.value, 0)
        }

        // 设置主题样式
        updateChartTheme()
        nextTick(() => _ensureWmLayer())

        // 监听覆盖物创建完成事件，自动退出绘制模式
        if (chartRef.value && typeof chartRef.value.subscribeAction === 'function') {
          // 监听覆盖物创建完成事件
          chartRef.value.subscribeAction('onOverlayCreated', (overlay) => {
            // 如果是通过画线工具创建的覆盖物，记录ID并退出绘制模式
            if (activeDrawingTool.value && overlay && overlay.id) {
              // 检查覆盖物名称是否匹配当前激活的工具
              const toolMap = {
                line: 'segment',
                horizontalLine: 'horizontalStraightLine',
                verticalLine: 'verticalStraightLine',
                ray: 'rayLine',
                straightLine: 'straightLine',
                parallelStraightLine: 'parallelStraightLine',
                priceLine: 'priceLine',
                priceChannelLine: 'priceChannelLine',
                fibonacciLine: 'fibonacciLine',
                measure: 'priceRangeMeasure'
              }
              const expectedOverlayName = toolMap[activeDrawingTool.value]

              // 测量工具需要等待第二个点完成，不能在 created 阶段就退出绘制模式
              if (expectedOverlayName === 'priceRangeMeasure') {
                return
              }
              // 如果覆盖物名称匹配，或者是通过 overrideOverlay 创建的自定义覆盖物
              if (!overlay.name || overlay.name === expectedOverlayName) {
                addedDrawingOverlayIds.value.push(overlay.id)
                // 重置激活状态
                activeDrawingTool.value = null
                // 退出绘制模式
                try {
                  if (typeof chartRef.value.overrideOverlay === 'function') {
                    chartRef.value.overrideOverlay(null)
                  }
                } catch (e) {
                }
              }
            }
          })

          // 监听覆盖物绘制完成事件（某些版本可能使用此事件）
          if (typeof chartRef.value.subscribeAction === 'function') {
            try {
              chartRef.value.subscribeAction('onOverlayComplete', (overlay) => {
                if (activeDrawingTool.value && overlay && overlay.id) {
                  if (activeDrawingTool.value === 'measure') {
                    const points = overlay.points || []
                    if (points.length < 2 || !points[0] || !points[1]) {
                      return
                    }
                  }
                  addedDrawingOverlayIds.value.push(overlay.id)
                  activeDrawingTool.value = null
                  // 退出绘制模式 - 不调用 overrideOverlay(null)，因为会导致错误
                }
              })
            } catch (e) {
              // 如果 onOverlayComplete 不存在，忽略错误
            }
          }

          // 监听覆盖物移除事件
          chartRef.value.subscribeAction('onOverlayRemoved', (overlayId) => {
            // 从列表中移除
            const index = addedDrawingOverlayIds.value.indexOf(overlayId)
            if (index > -1) {
              addedDrawingOverlayIds.value.splice(index, 1)
            }
          })
        }

        // 使用 subscribeAction 监听可见范围变化，手动触发加载更多
        // 替代 setLoadMoreDataCallback，因为它在某些版本可能不触发
        if (chartRef.value && typeof chartRef.value.subscribeAction === 'function') {
          // 保存上一次的可见范围，用于检测是否滚动到最左侧
          let lastVisibleFrom = null
          // 标记是否已经处理过初始化时的可见范围变化
          let initialRangeProcessed = false

          chartRef.value.subscribeAction('onVisibleRangeChange', async (data) => {
            if (data && typeof data.from === 'number') {
              // 如果是初始化时的第一次可见范围变化，只记录，不触发加载
              if (!initialRangeProcessed) {
                lastVisibleFrom = data.from
                initialRangeProcessed = true
                // 延迟标记图表初始化完成，确保初始化完成后再允许触发加载
                safeTimeout(() => {
                  chartInitialized.value = true
                }, 1000)
                return
              }

              // 如果图表还未初始化完成，不触发加载
              if (!chartInitialized.value) {
                lastVisibleFrom = data.from
                return
              }

              // 如果正在加载历史数据，且用户尝试继续向左滚动，阻止滚动
              if (loadingHistory.value && data.from <= 0) {
                // 尝试将可见范围保持在第一个数据点之后，防止继续向左
                  try {
                    // 换股/重载后 Y 轴已复位为自动，无需再干预可见范围
                  } catch (e) {
                  }
                  return
              }

              // 当滚动到最左侧（索引接近0或小于等于5）时触发加载
              // 【关键】同时检查 loadingHistory.value 和 loadingHistoryPromise，确保没有正在进行的请求
              if (data.from <= 5 && !loadingHistory.value && !loadingHistoryPromise && hasMoreHistory.value && chartInitialized.value) {
                // 两种情况都应触发：
                // 1. 用户主动向左滚动（lastVisibleFrom > data.from）
                // 2. from 已经在 0 附近但还有更多历史数据（数据量太少导致初始就在最左侧）
                const isScrollingLeft = lastVisibleFrom !== null && lastVisibleFrom > data.from
                const isAlreadyAtEdge = data.from <= 0
                if (isScrollingLeft || isAlreadyAtEdge) {
                  if (klineData.value.length > 0) {
                    const earliestTimestamp = klineData.value[0].timestamp
                    await loadMoreHistoryDataForScroll(earliestTimestamp)
                  }
                }
              }

              // 更新上一次的可见范围
              lastVisibleFrom = data.from
              renderChip()
            }
          })
        }

        // 如果有数据，应用数据
        if (klineData.value && klineData.value.length > 0) {
          // 确保数据格式正确
          const validData = klineData.value.filter(item =>
            item.timestamp &&
            !isNaN(item.open) &&
            !isNaN(item.high) &&
            !isNaN(item.low) &&
            !isNaN(item.close)
          )

          if (validData.length > 0) {
            // 使用 applyNewData 初始化
            try {
              chartRef.value.applyNewData(validData)
            } catch (e) {
              // 尝试降级处理
              try {
                chartRef.value.applyNewData(validData)
              } catch (e2) {
              }
            }

            // 分时模式：首次经 initChart 建图时也必须应用面积图样式 / 0 轴线 / Y 轴居中 /
            // 均价线。原先只在 loadKlineData 的「图表已存在」分支里调用 applyMinuteLineChartStyle，
            // 若首个标的在 initChart 定时器(300ms)之前就绪，图表由这里创建 → 分时样式整体缺失。
            if (isMinuteLine.value) {
              nextTick(() => {
                if (chartRef.value && isMinuteLine.value) applyMinuteLineChartStyle()
              })
            } else {
              nextTick(() => {
                if (chartRef.value) {
                  restoreNormalChartStyle()
                  restoreChartScene(props.timeframe)
                }
              })
            }

            // VOL 现在作为可选内置指标，通过 updateIndicators 管理
            // 延迟更新指标，确保K线先渲染
            nextTick(() => {
              updateIndicators()
              renderChip()
              // 左侧百分比坐标轴：图就绪后建立同步（observer/包装 buildTicks/订阅事件）
              _syncPctRuler()
            })
          }
        }

        window.addEventListener('resize', handleResize)
      } catch (error) {
        error.value = proxy.$t('dashboard.indicator.error.chartInitFailed') + ': ' + (error.message || '未知错误')
      }
    }

    const handleResize = () => {
      if (chartRef.value) {
        // P1-4: 用 rAF 防抖替代固定 100ms 延时。同一帧内多次 resize 只执行一次，
        // 且卸载时统一 cancel，不会对已销毁的图表调用 resize
        if (_resizeRafId != null) cancelAnimationFrame(_resizeRafId)
        _resizeRafId = requestAnimationFrame(() => {
          _resizeRafId = null
          if (chartRef.value) {
            try {
              chartRef.value.resize()
              renderChip()
              // 分时模式：绘图区宽度变化后重新铺满（X 轴保持覆盖整个交易时段）
              if (isMinuteLine.value) {
                fitMinuteLineView()
                // resize 会触发刻度重建，重排后再次锁定 Y 轴居中范围
                applyMinutePrevCloseAxis()
              }
              _schedulePctRulerPaint()
            } catch (e) {
              // P1-3: resize 失败需留痕，否则图表错位难以定位
              console.warn('[KlineChart] resize 重绘失败:', e)
            }
          }
        })
      } else {
        const container = document.getElementById('kline-chart-container')
        if (container && container.clientWidth > 0 && container.clientHeight > 0) {
          initChart()
        }
      }
    }

    // 更新图表主题
    const updateChartTheme = () => {
      if (!chartRef.value) return

      const theme = themeConfig.value
      const isDark = chartTheme.value === 'dark'

      chartRef.value.setStyles({
        grid: {
          show: true,
          horizontal: {
            show: true,
            color: theme.gridLineColor,
            style: 'dashed',
            size: 1
          },
          vertical: {
            show: false
          }
        },
        separator: {
          size: 1,
          color: theme.separatorColor,
          fill: false,
          activeBackgroundColor: theme.separatorActive
        },
        candle: {
          priceMark: {
            show: true,
            high: {
              show: true,
              color: theme.axisLabelColor
            },
            low: {
              show: true,
              color: theme.axisLabelColor
            }
          },
          tooltip: {
            showRule: 'always',
            showType: 'standard'
          },
          bar: {
            upColor: isDark ? '#ef5350' : '#f5222d',
            downColor: isDark ? '#0ecb81' : '#52c41a',
            noChangeColor: theme.borderColor,
            upBorderColor: isDark ? '#ef5350' : '#f5222d',
            downBorderColor: isDark ? '#0ecb81' : '#52c41a',
            noChangeBorderColor: theme.borderColor,
            upWickColor: isDark ? '#ef5350' : '#f5222d',
            downWickColor: isDark ? '#0ecb81' : '#52c41a',
            noChangeWickColor: theme.borderColor
          },
          // 若使用面积图类型，关闭末端点动画可减少实时跳动观感
          area: {
            point: { animation: false, animationDuration: 0 }
          }
        },
        indicator: {
          tooltip: {
            showRule: 'always',
            showType: 'standard'
          },
          // indicator.bars 控制副图指标（VOL等）的柱状图颜色
          bars: [{
            style: 'fill',
            upColor: isDark ? '#ef5350' : '#f5222d',
            downColor: isDark ? '#0ecb81' : '#52c41a',
            noChangeColor: theme.borderColor,
            upBorderColor: isDark ? '#ef5350' : '#f5222d',
            downBorderColor: isDark ? '#0ecb81' : '#52c41a',
            noChangeBorderColor: theme.borderColor
          }]
        },
        xAxis: {
          show: true,
          axisLine: {
            show: true,
            color: theme.borderColor
          }
        },
        yAxis: {
          show: true,
          axisLine: {
            show: false
          }
        },
        crosshair: {
          show: true,
          horizontal: {
            show: true,
            line: {
              show: true,
              style: 'dashed',
              color: theme.gridLineColor,
              size: 1
            },
            text: {
              show: true,
              style: 'fill',
              color: '#fff',
              size: 11,
              backgroundColor: theme.axisLabelColor || '#485368'
            }
          },
          vertical: {
            show: true,
            line: {
              show: true,
              style: 'dashed',
              color: theme.gridLineColor,
              size: 1
            },
            text: {
              show: true,
              style: 'fill',
              color: '#fff',
              size: 11,
              backgroundColor: theme.axisLabelColor || '#485368'
            }
          }
        },
        watermark: {
          show: false
        }
      })

      // 主题配色变化 → 重绘左轴百分比列（文字颜色跟随主题）
      _schedulePctRulerPaint()
    }

    // --- 注册自定义指标辅助函数 ---
    const registerCustomIndicator = (nameOrObj, calcFunc, figures, calcParams = [], precision = -1, shouldOverlay = false) => {
      let indicatorConfig
      if (typeof nameOrObj === 'object' && nameOrObj !== null) {
        // 对象参数形式（支持 draw 等高级配置）
        indicatorConfig = { ...nameOrObj }
        if (!indicatorConfig.precision) indicatorConfig.precision = pricePrecision.value
        if (!indicatorConfig.series) indicatorConfig.series = 'normal'
      } else {
        // 传统参数形式
        if (precision < 0) precision = pricePrecision.value
        indicatorConfig = {
          name: nameOrObj,
          shortName: nameOrObj,
          calc: calcFunc,
          figures,
          calcParams,
          precision,
          series: shouldOverlay ? 'price' : 'normal'
        }
      }
      try {
        registerIndicator(indicatorConfig)
        return true
      } catch (err) {
        // 如果已注册，忽略错误
        if (err.message && err.message.includes('already registered')) {
          return true
        }
        return false
      }
    }

    // --- 更新指标（KLineChart 版本） ---
    const updateIndicators = async () => {
      if (indicatorsUpdating.value) {
        return
      }
      // 使用 JSON 序列化/反序列化去除 Vue 2 Observer 的干扰
      if (!chartRef.value || klineData.value.length === 0) {
        return
      }

      indicatorsUpdating.value = true
      try {
      // 1. 移除所有已添加的信号 overlays
      try {
        if (addedSignalOverlayIds.value.length > 0 && chartRef.value) {
          addedSignalOverlayIds.value.forEach(overlayId => {
            try {
              if (typeof chartRef.value.removeOverlay === 'function') {
                chartRef.value.removeOverlay(overlayId)
              } else if (typeof chartRef.value.removeOverlayById === 'function') {
                chartRef.value.removeOverlayById(overlayId)
              }
            } catch (err) {
            }
          })
          // 清空列表
          addedSignalOverlayIds.value = []
        }
      } catch (e) {
      }

      // 2. 移除所有已添加的指标
      try {
        if (addedIndicatorIds.value.length > 0) {
          addedIndicatorIds.value.forEach(info => {
            // info 可以是 { paneId, name } 对象或仅 name 字符串
            const name = typeof info === 'string' ? info : info.name
            const paneId = typeof info === 'string' ? undefined : info.paneId

            // 尝试移除指标
            // KLineChart v9: removeIndicator(paneId, name)
            if (paneId) {
              chartRef.value.removeIndicator(paneId, name)
            } else {
              // 如果没有 paneId，尝试从主图移除
              chartRef.value.removeIndicator('candle_pane', name)
              // 也可以尝试不传 paneId
              chartRef.value.removeIndicator(name)
            }
          })
          // 清空列表
          addedIndicatorIds.value = []
        }
      } catch (e) {
      }
      // 清理副图关闭按钮
      removeAllPaneCloseButtons()

      // 转换数据格式（KLineChart 需要内部格式用于计算）
      const internalData = convertToInternalFormat(klineData.value)
      const mainPaneOverlayFigures = []
      const mainPaneOverlayCalcEntries = []
      const mainPaneOverlaySignatureParts = []
      const addMainPaneOverlayEntry = ({ signature, figures, calc }) => {
        if (signature) {
          mainPaneOverlaySignatureParts.push(String(signature))
        }
        if (Array.isArray(figures) && figures.length) {
          mainPaneOverlayFigures.push(...figures)
        }
        if (typeof calc === 'function') {
          mainPaneOverlayCalcEntries.push(calc)
        }
      }

      // 遍历所有激活的指标
      for (let idx = 0; idx < props.activeIndicators.length; idx++) {
        const indicator = props.activeIndicators[idx]
        try {
          if (indicator && indicator.visible === false) {
            continue
          }
          // 处理 Python 指标
          if (indicator.type === 'python') {
            // 分时模式下跳过外置 Python 指标
            if (isMinuteLine.value) continue
            if (!indicator.code) continue

            try {
              // 如果有 calculate 函数，使用它（用于 Python 指标）
              if (indicator.calculate && typeof indicator.calculate === 'function') {
                const result = await indicator.calculate(internalData, indicator.params || {})

                // 处理结果中的 plots - 将所有 plots 合并到一个指标中
                // 注意：signals 不添加到指标中，而是单独处理，避免显示 "n/a"
                let allPlots = []
                if (result && result.plots && Array.isArray(result.plots)) {
                  allPlots = [...result.plots]
                }

                // 处理 signals - 使用 KLineChart 的 createOverlay 显示（不添加到指标中）
                if (result && result.signals && Array.isArray(result.signals)) {
                  for (const signal of result.signals) {
                    if (signal.data && Array.isArray(signal.data) && signal.data.length > 0) {
                      // 统计非空值的数量
                      const sampleValues = []
                      for (let i = 0; i < Math.min(signal.data.length, 20); i++) {
                        const val = signal.data[i]
                        if (val !== null && val !== undefined && !isNaN(val)) {
                          if (sampleValues.length < 5) {
                            sampleValues.push({ index: i, value: val })
                          }
                        }
                      }

                      // 找到所有非空的信号点
                      const signalPoints = []
                      for (let i = 0; i < signal.data.length && i < internalData.length; i++) {
                        const signalValue = signal.data[i]
                        if (signalValue !== null && signalValue !== undefined && !isNaN(signalValue)) {
                          const klineItem = internalData[i]
                          const timestamp = klineItem.timestamp || klineItem.time

                          // 【核心修改】获取当前 K 线的 High 和 Low
                          // 注意：internalData 已经是你转换过的格式，直接取即可
                          const highPrice = klineItem.high
                          const lowPrice = klineItem.low

                          // Signal type: chart only displays indicator signals (buy/sell).
                          const signalTypeRaw = (signal.type || 'buy')
                          const signalType = String(signalTypeRaw).toLowerCase()
                          // Chart only displays indicator signals (no position mgmt / TP/SL / trailing etc).
                          const allowedSignalTypes = ['buy', 'sell']
                          if (!allowedSignalTypes.includes(signalType)) {
                            continue
                          }
                          // Buy-side labels are shown below candles; sell-side labels above candles.
                          const isBuySignal = signalType === 'buy'

                          // Text: prefer per-point textData, otherwise use signal.text, otherwise fallback to B/S.
                          let pointText = signal.text || (isBuySignal ? 'B' : 'S')
                          if (signal.textData && signal.textData[i] != null) {
                            pointText = signal.textData[i]
                          }

                          signalPoints.push({
                            timestamp,
                            price: signalValue,
                            // 确定锚点价格：买入看 Low，卖出看 High
                            anchorPrice: isBuySignal ? lowPrice : highPrice,
                            // side is used for layout/styling; action preserves the original type (buy/sell).
                            side: isBuySignal ? 'buy' : 'sell',
                            action: signalType,
                            color: signal.color || (isBuySignal ? '#00E676' : '#FF5252'),
                            text: pointText
                          })
                        }
                      }

                      // 使用 KLineChart 的 createOverlay 添加标记
                      if (signalPoints.length > 0 && chartRef.value) {
                        for (const point of signalPoints) {
                          try {
                            // 确保时间戳是毫秒级
                            let timestamp = point.timestamp
                            if (timestamp < 1e10) {
                              timestamp = timestamp * 1000
                            }

                            // 只显示 buy 或 sell，不显示金额
                            const displaySimpleText = point.text

                            // === 使用自定义 signalTag ===
                            if (typeof chartRef.value.createOverlay === 'function') {
                              const overlayId = chartRef.value.createOverlay({
                                name: 'signalTag',
                                // 【核心修改】传入两个点：
                                // Point 0: 信号触发价格 (用于画圆点)
                                // Point 1: K线极值价格 (用于定位标签)
                                points: [
                                  { timestamp: timestamp, value: point.price },
                                  { timestamp: timestamp, value: point.anchorPrice }
                                ],
                                extendData: {
                                  text: displaySimpleText,
                                  color: point.color,
                                  side: point.side,
                                  action: point.action,
                                  price: point.price
                                },
                                lock: true // 锁定防止拖动
                              }, 'candle_pane') // 绘制在主图

                              if (overlayId) {
                                addedSignalOverlayIds.value.push(overlayId)
                              }
                            }
                            // === 修改结束 ===
                          } catch (overlayErr) {
                          }
                        }
                      } else {
                      }
                    }
                  }
                }

                // 只处理 plots（不包括 signals）
                if (allPlots.length > 0) {
                  // 过滤出有效的 plots
                  const validPlots = allPlots.filter(plot => plot.data && Array.isArray(plot.data) && plot.data.length > 0)

                  if (validPlots.length > 0) {
                    // 构建 figures 数组，包含所有 plots
                    const figures = []
                    const plotDataMap = {}

                    for (let plotIdx = 0; plotIdx < validPlots.length; plotIdx++) {
                      const plot = validPlots[plotIdx]
                      const plotName = plot.name || `PLOT_${plotIdx}_${idx}`
                      const figureKey = plotName.toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, '_')
                      const plotColor = plot.color || getIndicatorColor(plotIdx)

                      // 对于普通 plot，使用原类型或 'line'
                      const figureType = plot.type || 'line'

                      figures.push({
                        key: figureKey,
                        title: plot.name || plotName,
                        type: figureType,
                        color: plotColor
                      })

                      plotDataMap[figureKey] = plot.data
                    }

                    // 确定是否叠加在主图上（如果所有 plots 都是 overlay，则叠加）
                    const allOverlay = validPlots.every(plot => plot.overlay !== false)
                    // const customIndicatorName = `${indicator.id}_combined`
                    let customIndicatorName = `${indicator.id}_combined`
                    if (result && result.name) {
                      customIndicatorName = result.name
                    }
                    try {
                      // 注册合并的自定义指标
                      const registered = registerCustomIndicator(
                        customIndicatorName,
                        (kLineDataList) => {
                          const result = []
                          for (let i = 0; i < kLineDataList.length; i++) {
                            const dataPoint = {}
                            for (const figureKey in plotDataMap) {
                              const plotData = plotDataMap[figureKey]
                              dataPoint[figureKey] = i < plotData.length ? plotData[i] : null
                            }
                            result.push(dataPoint)
                          }
                          return result
                        },
                        figures,
                        [],
                        2,
                        allOverlay
                      )

                      if (registered) {
                        if (allOverlay) {
                          // 主图指标
                          const paneId = chartRef.value.createIndicator(
                            customIndicatorName,
                            true, // isStack=true 追加；传 false 会清空主图已有的均价线 / 锚定指标
                            { id: 'candle_pane' }
                          )
                          if (paneId) {
                            addedIndicatorIds.value.push({ paneId, name: customIndicatorName })
                          } else {
                            addedIndicatorIds.value.push({ paneId: 'candle_pane', name: customIndicatorName })
                          }
                        } else {
                          // 副图指标
                          const indicatorId = chartRef.value.createIndicator(
                            customIndicatorName,
                            false,
                            { height: 100, dragEnabled: true }
                          )
                          if (indicatorId) {
                            addedIndicatorIds.value.push({ paneId: indicatorId, name: customIndicatorName })
                addPaneCloseButton(indicatorId, indicator.id, indicator.instanceId)
                          }
                        }
                      }
                    } catch (plotErr) {
                    }
                  }
                }
              } else {
                // 如果没有 calculate 函数，直接使用 executePythonStrategy
                // 构建解密所需的信息
                const decryptInfo = {
                  id: indicator.originalId || indicator.id, // 优先使用原始数据库ID
                  user_id: indicator.user_id || indicator.userId,
                  is_encrypted: indicator.is_encrypted || indicator.isEncrypted || 0
                }
                const pythonResult = await executePythonStrategy(
                  indicator.code,
                  internalData,
                  indicator.params || {},
                  decryptInfo // 传递解密信息
                )

                // 处理 plots - 将所有 plots 合并到一个指标中
                // 注意：signals 不添加到指标中，而是单独处理，避免显示 "n/a"
                let allPlots = []
                if (pythonResult && pythonResult.plots && Array.isArray(pythonResult.plots)) {
                  allPlots = [...pythonResult.plots]
                }

                // 处理 signals - 使用 KLineChart 的 createOverlay 显示（不添加到指标中）
                if (pythonResult && pythonResult.signals && Array.isArray(pythonResult.signals)) {
                  for (const signal of pythonResult.signals) {
                    if (signal.data && Array.isArray(signal.data) && signal.data.length > 0) {
                      // 统计非空值的数量
                      const sampleValues = []
                      for (let i = 0; i < Math.min(signal.data.length, 20); i++) {
                        const val = signal.data[i]
                        if (val !== null && val !== undefined && !isNaN(val)) {
                          if (sampleValues.length < 5) {
                            sampleValues.push({ index: i, value: val })
                          }
                        }
                      }

                      // 找到所有非空的信号点
                      const signalPoints = []
                      for (let i = 0; i < signal.data.length && i < internalData.length; i++) {
                        const signalValue = signal.data[i]
                        if (signalValue !== null && signalValue !== undefined && !isNaN(signalValue)) {
                          const klineItem = internalData[i]
                          const timestamp = klineItem.timestamp || klineItem.time

                          // 【核心修改】获取当前 K 线的 High 和 Low
                          // 注意：internalData 已经是你转换过的格式，直接取即可
                          const highPrice = klineItem.high
                          const lowPrice = klineItem.low

                          // Signal type: chart only displays indicator signals (buy/sell).
                          const signalTypeRaw = (signal.type || 'buy')
                          const signalType = String(signalTypeRaw).toLowerCase()
                          // Chart only displays indicator signals (no position mgmt / TP/SL / trailing etc).
                          const allowedSignalTypes = ['buy', 'sell']
                          if (!allowedSignalTypes.includes(signalType)) {
                            continue
                          }
                          const isBuySignal = signalType === 'buy'

                          // Text: prefer per-point textData, otherwise use signal.text, otherwise fallback to B/S.
                          let pointText = signal.text || (isBuySignal ? 'B' : 'S')
                          if (signal.textData && signal.textData[i] != null) {
                            pointText = signal.textData[i]
                          }

                          signalPoints.push({
                            timestamp,
                            price: signalValue,
                            // 确定锚点价格：买入看 Low，卖出看 High
                            anchorPrice: isBuySignal ? lowPrice : highPrice,
                            side: isBuySignal ? 'buy' : 'sell',
                            action: signalType,
                            color: signal.color || (isBuySignal ? '#00E676' : '#FF5252'),
                            text: pointText
                          })
                        }
                      }

                      // 使用 KLineChart 的 createOverlay 添加标记
                      if (signalPoints.length > 0 && chartRef.value) {
                        for (const point of signalPoints) {
                          try {
                            // 确保时间戳是毫秒级
                            let timestamp = point.timestamp
                            if (timestamp < 1e10) {
                              timestamp = timestamp * 1000
                            }

                            // 只显示 buy 或 sell，不显示金额
                            const displaySimpleText = point.text

                            // === 使用自定义 signalTag ===
                            if (typeof chartRef.value.createOverlay === 'function') {
                              const overlayId = chartRef.value.createOverlay({
                                name: 'signalTag',
                                // 【核心修改】传入两个点：
                                // Point 0: 信号触发价格 (用于画圆点)
                                // Point 1: K线极值价格 (用于定位标签)
                                points: [
                                  { timestamp: timestamp, value: point.price },
                                  { timestamp: timestamp, value: point.anchorPrice }
                                ],
                                extendData: {
                                  text: displaySimpleText,
                                  color: point.color,
                                  side: point.side,
                                  action: point.action,
                                  price: point.price
                                },
                                lock: true // 锁定防止拖动
                              }, 'candle_pane') // 绘制在主图

                              if (overlayId) {
                                addedSignalOverlayIds.value.push(overlayId)
                              }
                            }
                            // === 修改结束 ===
                          } catch (overlayErr) {
                          }
                        }
                      } else {
                      }
                    }
                  }
                }

                // 只处理 plots（不包括 signals）
                if (allPlots.length > 0) {
                  // 过滤出有效的 plots
                  const validPlots = allPlots.filter(plot => plot.data && Array.isArray(plot.data) && plot.data.length > 0)

                  if (validPlots.length > 0) {
                    // 构建 figures 数组，包含所有 plots
                    const figures = []
                    const plotDataMap = {}

                    for (let plotIdx = 0; plotIdx < validPlots.length; plotIdx++) {
                      const plot = validPlots[plotIdx]
                      const plotName = plot.name || `PLOT_${plotIdx}`
                      const figureKey = plotName.toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, '_')
                      const plotColor = plot.color || getIndicatorColor(plotIdx)

                      // 对于普通 plot，使用原类型或 'line'
                      const figureType = plot.type || 'line'

                      figures.push({
                        key: figureKey,
                        title: plot.name || plotName,
                        type: figureType,
                        color: plotColor
                      })

                      plotDataMap[figureKey] = plot.data
                    }

                    // 确定是否叠加在主图上（如果所有 plots 都是 overlay，则叠加）
                    const allOverlay = validPlots.every(plot => plot.overlay !== false)
                    // const customIndicatorName = `${indicator.id}_combined`
                    let customIndicatorName = `${indicator.id}_combined`
                    if (pythonResult && pythonResult.name) {
                      customIndicatorName = pythonResult.name
                    }

                    try {
                      if (allOverlay) {
                        addMainPaneOverlayEntry({
                          signature: `${customIndicatorName}_${idx}`,
                          figures,
                          calc: () => {
                            const result = []
                            for (let i = 0; i < internalData.length; i++) {
                              const dataPoint = {}
                              for (const figureKey in plotDataMap) {
                                const plotData = plotDataMap[figureKey]
                                dataPoint[figureKey] = i < plotData.length ? plotData[i] : null
                              }
                              result.push(dataPoint)
                            }
                            return result
                          }
                        })
                      } else {
                        // 注册合并的自定义指标
                        const registered = registerCustomIndicator(
                          customIndicatorName,
                          (kLineDataList) => {
                            const result = []
                            for (let i = 0; i < kLineDataList.length; i++) {
                              const dataPoint = {}
                              for (const figureKey in plotDataMap) {
                                const plotData = plotDataMap[figureKey]
                                dataPoint[figureKey] = i < plotData.length ? plotData[i] : null
                              }
                              result.push(dataPoint)
                            }
                            return result
                          },
                          figures,
                          [],
                          2,
                          false
                        )

                        if (registered) {
                          // 副图指标
                          const indicatorId = chartRef.value.createIndicator(
                            customIndicatorName,
                            false,
                            { height: 100, dragEnabled: true }
                          )
                          if (indicatorId) {
                            addedIndicatorIds.value.push({ paneId: indicatorId, name: customIndicatorName })
                addPaneCloseButton(indicatorId, indicator.id, indicator.instanceId)
                          }
                        }
                      }
                    } catch (plotErr) {
                    }
                  }
                }
              }
            } catch (err) {
              // 如果是 Python 引擎未就绪的错误，设置加载失败状态
              if (err.message && err.message.includes('Python 引擎未就绪')) {
                if (!loadingPython.value) {
                  pyodideLoadFailed.value = true
                }
              }
            }
            continue
          }

          // 注意：calculate 函数可能为 null，因为指标的计算逻辑在 updateIndicators 中通过 id 判断
          // 所以这里不检查 calculate，而是直接根据 indicator.id 处理

          const indicatorStyle = normalizeIndicatorStyle(indicator.style || {}, getIndicatorColor(idx))
          const color = indicatorStyle.color
          const lineWidth = indicatorStyle.lineWidth
          const indicatorInstanceKey = String(indicator.instanceId || `${indicator.id}_${idx}`).replace(/[^a-zA-Z0-9_]/g, '_')
          const buildUniqueIndicatorName = (baseName) => `${baseName}_${indicatorInstanceKey}`
          const buildLineFigure = (key, title, figureColor = color, width = lineWidth) => ({
            key,
            title,
            type: 'line',
            styles: () => ({
              color: figureColor,
              size: width,
              style: 'solid'
            })
          })
          const buildCircleFigure = (key, title, figureColor = color) => ({
            key,
            title,
            type: 'circle',
            styles: () => ({
              style: 'fill',
              color: figureColor
            })
          })
          const buildFigure = (key, title, figureColor = color, width = lineWidth, figType = 'line') => {
            if (figType === 'circle') return buildCircleFigure(key, title, figureColor)
            return buildLineFigure(key, title, figureColor, width)
          }
          // 根据指标类型创建 KLineChart 指标
          if (indicator.id === 'sma' || indicator.id === 'sma2' || indicator.id === 'ema') {
            const maType = (indicator.id === 'ema') ? 'EMA' : 'SMA'
            const figureKey = maType.toLowerCase()
            // 默认显示 5,10,20,60 四条线；用户指定了单个 period 则只显示那一条
            const singlePeriod = indicator.params?.length || indicator.params?.period
            const periods = singlePeriod ? [singlePeriod] : [5, 10, 20, 60]

            try {
              for (const p of periods) {
                const calcPeriod = p
                const lineColor = getIndicatorColor(periods.indexOf(p))
                addMainPaneOverlayEntry({
                  signature: buildUniqueIndicatorName(`${maType}_${p}`),
                  figures: [buildLineFigure(`${figureKey}_${p}_${indicatorInstanceKey}`, `${maType}(${p})`, lineColor, lineWidth)],
                  calc: (kLineDataList) => {
                    const values = maType === 'SMA'
                      ? calculateSMA(kLineDataList, calcPeriod)
                      : calculateEMA(kLineDataList, calcPeriod)
                    return values.map(v => ({ [`${figureKey}_${calcPeriod}_${indicatorInstanceKey}`]: v }))
                  }
                })
              }
            } catch (err) {
            }
          } else if (indicator.id === 'macd') {
            const fast = indicator.params?.fast || 12
            const slow = indicator.params?.slow || 26
            const signal = indicator.params?.signal || 9
            const customIndicatorName = buildUniqueIndicatorName(`MACD_${fast}_${slow}_${signal}`)
            try {
              const registered = registerCustomIndicator({
                name: customIndicatorName,
                shortName: customIndicatorName,
                calcParams: [fast, slow, signal],
                figures: [
                  buildLineFigure('macd', `MACD(${fast},${slow})`, color, lineWidth),
                  buildLineFigure('signal', `SIGNAL(${signal})`, '#fa8c16', lineWidth),
                  {
                    key: 'histogram',
                    title: 'HIST',
                    type: 'bar',
                    baseValue: 0,
                    styles: (data, indicator, defaultStyles) => {
                      const prev = data.prev
                      const current = data.current
                      const prevHist = prev?.indicatorData?.histogram ?? 0
                      const currentHist = current?.indicatorData?.histogram ?? 0
                      const isDark = props.theme === 'dark'
                      const bars = defaultStyles.bars || []
                      const barStyle = bars[0] || {}
                      if (currentHist >= prevHist) {
                        return { color: barStyle.upColor || (isDark ? '#ef5350' : '#f5222d'), style: 'fill' }
                      } else {
                        return { color: barStyle.downColor || (isDark ? '#0ecb81' : '#52c41a'), style: 'fill' }
                      }
                    }
                  }
                ],
                calc: (kLineDataList, indicator) => {
                  const f = indicator.calcParams[0] || 12
                  const s = indicator.calcParams[1] || 26
                  const sig = indicator.calcParams[2] || 9
                  const macdValues = calculateMACD(kLineDataList, f, s, sig)
                  return macdValues.macd.map((value, i) => ({
                    macd: value,
                    signal: macdValues.signal[i],
                    histogram: macdValues.histogram[i]
                  }))
                }
              })
              if (registered) {
                const indicatorId = chartRef.value.createIndicator(customIndicatorName, false, { height: 100, dragEnabled: true })
                if (indicatorId) {
                  addedIndicatorIds.value.push({ paneId: indicatorId, name: customIndicatorName })
                addPaneCloseButton(indicatorId, indicator.id, indicator.instanceId)
                }
              }
            } catch (err) {
            }
          } else if (indicator.id === 'rsi') {
            const length = indicator.params?.length || 14
            const customIndicatorName = buildUniqueIndicatorName(`RSI_${length}`)
            try {
              const registered = registerCustomIndicator(
                customIndicatorName,
                (kLineDataList, indicator) => {
                  const length = indicator.calcParams[0] || 14
                  const rsiValues = calculateRSI(kLineDataList, length)
                  return rsiValues.map(value => ({ rsi: value }))
                },
                [buildLineFigure('rsi', `RSI(${length})`, color, lineWidth)],
                [length]
              )
              if (registered) {
                const indicatorId = chartRef.value.createIndicator(customIndicatorName, false, { height: 100, dragEnabled: true })
                if (indicatorId) {
                  addedIndicatorIds.value.push({ paneId: indicatorId, name: customIndicatorName })
                addPaneCloseButton(indicatorId, indicator.id, indicator.instanceId)
                }
              }
            } catch (err) {
            }
          } else if (indicator.id === 'bollinger_bands' || indicator.id === 'bb') {
            // 布林带需要注册自定义指标
            const length = indicator.params?.length || 20
            const mult = indicator.params?.mult || 2

            try {
              addMainPaneOverlayEntry({
                signature: buildUniqueIndicatorName(`BOLL_${length}_${mult}`),
                figures: [
                  buildLineFigure(`upper_${indicatorInstanceKey}`, `上轨(${length},${mult})`, color, lineWidth),
                  buildLineFigure(`middle_${indicatorInstanceKey}`, `中轨(${length})`, '#8c8c8c', lineWidth),
                  buildLineFigure(`lower_${indicatorInstanceKey}`, `下轨(${length},${mult})`, color, lineWidth)
                ],
                calc: (kLineDataList) => {
                  const currentLength = length
                  const currentMult = mult
                  // calculateBollingerBands 需要传入包含 close 属性的对象数组
                  const bbResult = calculateBollingerBands(kLineDataList, currentLength, currentMult)
                  // KLineChart 需要返回对象数组，每个对象的键对应 figures 的 key
                  const result = []
                  for (let i = 0; i < bbResult.length; i++) {
                    result.push({
                      [`upper_${indicatorInstanceKey}`]: bbResult[i]?.upper ?? null,
                      [`middle_${indicatorInstanceKey}`]: bbResult[i]?.middle ?? null,
                      [`lower_${indicatorInstanceKey}`]: bbResult[i]?.lower ?? null
                    })
                  }
                  return result
                }
              })
            } catch (err) {
            }
          } else if (indicator.id === 'atr') {
            // ATR 需要注册自定义指标
            const period = indicator.params?.period || indicator.params?.length || 14
            const customIndicatorName = buildUniqueIndicatorName(`ATR_${period}`)

            try {
              const registered = registerCustomIndicator(
                customIndicatorName,
                (kLineDataList, indicator) => {
                  const period = indicator.calcParams[0] || 14
                  const data = kLineDataList.map(d => ({
                    high: d.high,
                    low: d.low,
                    close: d.close
                  }))
                  const atrValues = calculateATR(data, period)
                  // 转换为 KLineChart 需要的格式：返回对象数组
                  return atrValues.map(value => ({ atr: value }))
                },
                [buildLineFigure('atr', `ATR(${period})`, color, lineWidth)],
                [period]
              )

              if (registered) {
                const indicatorId = chartRef.value.createIndicator(customIndicatorName, false, { height: 100, dragEnabled: true })
                if (indicatorId) {
                  addedIndicatorIds.value.push({ paneId: indicatorId, name: customIndicatorName })
                addPaneCloseButton(indicatorId, indicator.id, indicator.instanceId)
                }
              }
            } catch (err) {
            }
          } else if (indicator.id === 'williams' || indicator.id === 'williams_r') {
            // Williams %R 需要注册自定义指标
            const length = indicator.params?.length || 14
            const customIndicatorName = buildUniqueIndicatorName(`WPR_${length}`)

            try {
              const registered = registerCustomIndicator(
                customIndicatorName,
                (kLineDataList, indicator) => {
                  const length = indicator.calcParams[0] || 14
                  const data = kLineDataList.map(d => ({
                    high: d.high,
                    low: d.low,
                    close: d.close
                  }))
                  const wrValues = calculateWilliamsR(data, length)
                  // 转换为 KLineChart 需要的格式：返回对象数组
                  return wrValues.map(value => ({ wr: value }))
                },
                [buildLineFigure('wr', `W%R(${length})`, color, lineWidth)],
                [length]
              )

              if (registered) {
                const indicatorId = chartRef.value.createIndicator(customIndicatorName, false, { height: 100, dragEnabled: true })
                if (indicatorId) {
                  addedIndicatorIds.value.push({ paneId: indicatorId, name: customIndicatorName })
                addPaneCloseButton(indicatorId, indicator.id, indicator.instanceId)
                }
              }
            } catch (err) {
            }
          } else if (indicator.id === 'mfi') {
            // MFI 需要注册自定义指标
            const length = indicator.params?.length || 14
            const customIndicatorName = buildUniqueIndicatorName(`MFI_${length}`)

            try {
              const registered = registerCustomIndicator(
                customIndicatorName,
                (kLineDataList, indicator) => {
                  const length = indicator.calcParams[0] || 14
                  const data = kLineDataList.map(d => ({
                    high: d.high,
                    low: d.low,
                    close: d.close,
                    volume: d.volume
                  }))
                  const mfiValues = calculateMFI(data, length)
                  // 转换为 KLineChart 需要的格式：返回对象数组
                  return mfiValues.map(value => ({ mfi: value }))
                },
                [buildLineFigure('mfi', `MFI(${length})`, color, lineWidth)],
                [length]
              )

              if (registered) {
                const indicatorId = chartRef.value.createIndicator(customIndicatorName, false, { height: 100, dragEnabled: true })
                if (indicatorId) {
                  addedIndicatorIds.value.push({ paneId: indicatorId, name: customIndicatorName })
                addPaneCloseButton(indicatorId, indicator.id, indicator.instanceId)
                }
              }
            } catch (err) {
            }
          } else if (indicator.id === 'cci') {
            // CCI 需要注册自定义指标
            const length = indicator.params?.length || 20
            const customIndicatorName = buildUniqueIndicatorName(`CCI_${length}`)

            try {
              const registered = registerCustomIndicator(
                customIndicatorName,
                (kLineDataList, indicator) => {
                  const length = indicator.calcParams[0] || 20
                  const data = kLineDataList.map(d => ({
                    high: d.high,
                    low: d.low,
                    close: d.close
                  }))
                  const cciValues = calculateCCI(data, length)
                  // 转换为 KLineChart 需要的格式：返回对象数组
                  return cciValues.map(value => ({ cci: value }))
                },
                [buildLineFigure('cci', `CCI(${length})`, color, lineWidth)],
                [length]
              )

              if (registered) {
                const indicatorId = chartRef.value.createIndicator(customIndicatorName, false, { height: 100, dragEnabled: true })
                if (indicatorId) {
                  addedIndicatorIds.value.push({ paneId: indicatorId, name: customIndicatorName })
                addPaneCloseButton(indicatorId, indicator.id, indicator.instanceId)
                }
              }
            } catch (err) {
            }
          } else if (indicator.id === 'adx') {
            // ADX 需要注册自定义指标
            const length = indicator.params?.length || 14
            const customIndicatorName = buildUniqueIndicatorName(`ADX_${length}`)

            try {
              const registered = registerCustomIndicator(
                customIndicatorName,
                (kLineDataList, indicator) => {
                  const length = indicator.calcParams[0] || 14
                  const data = kLineDataList.map(d => ({
                    high: d.high,
                    low: d.low,
                    close: d.close
                  }))
                  const result = calculateADX(data, length)
                  // 转换为 KLineChart 需要的格式：返回对象数组
                  return result.adx.map(value => ({ adx: value }))
                },
                [buildLineFigure('adx', `ADX(${length})`, color, lineWidth)],
                [length]
              )

              if (registered) {
                const indicatorId = chartRef.value.createIndicator(customIndicatorName, false, { height: 100, dragEnabled: true })
                if (indicatorId) {
                  addedIndicatorIds.value.push({ paneId: indicatorId, name: customIndicatorName })
                addPaneCloseButton(indicatorId, indicator.id, indicator.instanceId)
                }
              }
            } catch (err) {
            }
          } else if (indicator.id === 'obv') {
            // OBV 需要注册自定义指标
            const customIndicatorName = buildUniqueIndicatorName('OBV')

            try {
              const registered = registerCustomIndicator(
                customIndicatorName,
                (kLineDataList, indicator) => {
                  const data = kLineDataList.map(d => ({
                    close: d.close,
                    volume: d.volume || 0
                  }))
                  const obvValues = calculateOBV(data)
                  return obvValues.map(value => ({ obv: value }))
                },
                [buildLineFigure('obv', 'OBV', color, lineWidth)],
                []
              )

              if (registered) {
                const indicatorId = chartRef.value.createIndicator(customIndicatorName, false, { height: 100, dragEnabled: true })
                if (indicatorId) {
                  addedIndicatorIds.value.push({ paneId: indicatorId, name: customIndicatorName })
                addPaneCloseButton(indicatorId, indicator.id, indicator.instanceId)
                }
              }
            } catch (err) {
            }
          } else if (indicator.id === 'adosc') {
            // ADOSC 需要注册自定义指标
            const fast = indicator.params?.fast || 3
            const slow = indicator.params?.slow || 10
            const customIndicatorName = buildUniqueIndicatorName(`ADOSC_${fast}_${slow}`)

            try {
              const registered = registerCustomIndicator(
                customIndicatorName,
                (kLineDataList, indicator) => {
                  const fast = indicator.calcParams[0] || 3
                  const slow = indicator.calcParams[1] || 10
                  const data = kLineDataList.map(d => ({
                    high: d.high,
                    low: d.low,
                    close: d.close,
                    volume: d.volume || 0
                  }))
                  const adoscValues = calculateADOSC(data, fast, slow)
                  return adoscValues.map(value => ({ adosc: value }))
                },
                [buildLineFigure('adosc', `ADOSC(${fast},${slow})`, color, lineWidth)],
                [fast, slow]
              )

              if (registered) {
                const indicatorId = chartRef.value.createIndicator(customIndicatorName, false, { height: 100, dragEnabled: true })
                if (indicatorId) {
                  addedIndicatorIds.value.push({ paneId: indicatorId, name: customIndicatorName })
                addPaneCloseButton(indicatorId, indicator.id, indicator.instanceId)
                }
              }
            } catch (err) {
            }
          } else if (indicator.id === 'ad') {
            // AD 需要注册自定义指标
            const customIndicatorName = buildUniqueIndicatorName('AD')

            try {
              const registered = registerCustomIndicator(
                customIndicatorName,
                (kLineDataList, indicator) => {
                  const data = kLineDataList.map(d => ({
                    high: d.high,
                    low: d.low,
                    close: d.close,
                    volume: d.volume || 0
                  }))
                  const adValues = calculateAD(data)
                  return adValues.map(value => ({ ad: value }))
                },
                [buildLineFigure('ad', 'AD', color, lineWidth)],
                []
              )

              if (registered) {
                const indicatorId = chartRef.value.createIndicator(customIndicatorName, false, { height: 100, dragEnabled: true })
                if (indicatorId) {
                  addedIndicatorIds.value.push({ paneId: indicatorId, name: customIndicatorName })
                addPaneCloseButton(indicatorId, indicator.id, indicator.instanceId)
                }
              }
            } catch (err) {
            }
          } else if (indicator.id === 'kdj') {
            // KDJ 需要注册自定义指标
            const period = indicator.params?.period || 9
            const kPeriod = indicator.params?.k || 3
            const dPeriod = indicator.params?.d || 3
            const customIndicatorName = buildUniqueIndicatorName(`KDJ_${period}_${kPeriod}_${dPeriod}`)

            try {
              const registered = registerCustomIndicator(
                customIndicatorName,
                (kLineDataList, indicator) => {
                  const period = indicator.calcParams[0] || 9
                  const kPeriod = indicator.calcParams[1] || 3
                  const dPeriod = indicator.calcParams[2] || 3
                  const data = kLineDataList.map(d => ({
                    high: d.high,
                    low: d.low,
                    close: d.close
                  }))
                  const result = calculateKDJ(data, period, kPeriod, dPeriod)
                  return result.k.map((k, i) => ({
                    k: k,
                    d: result.d[i],
                    j: result.j[i]
                  }))
                },
                [
                  buildLineFigure('k', `K(${period},${kPeriod})`, color, lineWidth),
                  buildLineFigure('d', `D(${dPeriod})`, '#4ECDC4', lineWidth),
                  buildLineFigure('j', 'J', '#95E1D3', lineWidth)
                ],
                [period, kPeriod, dPeriod]
              )

              if (registered) {
                const indicatorId = chartRef.value.createIndicator(customIndicatorName, false, { height: 100, dragEnabled: true })
                if (indicatorId) {
                  addedIndicatorIds.value.push({ paneId: indicatorId, name: customIndicatorName })
                addPaneCloseButton(indicatorId, indicator.id, indicator.instanceId)
                }
              }
            } catch (err) {
            }
          } else if (indicator.id === 'vol') {
            // VOL - 使用 klinecharts 内置 VOL 指标
            try {
              const indicatorId = chartRef.value.createIndicator('VOL', false, { height: 100, dragEnabled: true })
              if (indicatorId) {
                addedIndicatorIds.value.push({ paneId: indicatorId, name: 'VOL' })
                addPaneCloseButton(indicatorId, indicator.id, indicator.instanceId)
              }
            } catch (err) { /* 预期内：pane 容器可能已销毁，关闭按钮添加失败 */ }
          } else if (INDICATOR_REGISTRY[indicator.id]) {
            // 通用指标注册（来自 indicatorCalculations.js 注册表）
            const def = INDICATOR_REGISTRY[indicator.id]
            const params = { ...def.defaultParams, ...(indicator.params || {}) }
            const paramKeys = Object.keys(def.defaultParams)
            const paramValues = paramKeys.map(k => params[k])
            const customIndicatorName = buildUniqueIndicatorName(`${indicator.id.toUpperCase()}_${paramValues.join('_')}`)
            try {
              const registered = registerCustomIndicator({
                name: customIndicatorName,
                shortName: customIndicatorName,
                calcParams: paramValues,
                figures: def.figures.map(f => {
                  if (f.type === 'line') { return buildLineFigure(f.key, f.title, color, lineWidth) }
                  if (f.type === 'bar') {
                    return {
                      key: f.key, title: f.title, type: 'bar', baseValue: 0,
                      styles: (data, ind, defaultStyles) => {
                        const prevVal = data.prev?.indicatorData?.[f.key] ?? 0
                        const curVal = data.current?.indicatorData?.[f.key] ?? 0
                        const bars = defaultStyles.bars || []
                        const barStyle = bars[0] || {}
                        return curVal >= prevVal
                          ? { color: barStyle.upColor || '#f5222d', style: 'fill' }
                          : { color: barStyle.downColor || '#52c41a', style: 'fill' }
                      }
                    }
                  }
                  if (f.type === 'circle') { return { key: f.key, title: f.title, type: 'circle' } }
                  return buildLineFigure(f.key, f.title, color, lineWidth)
                }),
                calc: (kLineDataList, ind) => {
                  const p = {}
                  paramKeys.forEach((k, idx) => { p[k] = ind.calcParams[idx] })
                  const raw = def.calc(kLineDataList, p)
                  // 标准化输出：数组 of 对象
                  if (Array.isArray(raw) && raw.length > 0 && typeof raw[0] === 'object' && raw[0] !== null && !Array.isArray(raw[0])) {
                    return raw
                  }
                  // 如果返回的是简单数值数组，包装成对象
                  const figKey = def.figures[0].key
                  return raw.map(v => ({ [figKey]: v }))
                },
                precision: pricePrecision.value,
                series: 'normal'
              })
              if (registered) {
                const isMainPane = def.figures.some(f => f.overlay)
                if (isMainPane) {
                  // 主图指标：叠加到蜡烛图上（和 MA 一样）
                  const ik = indicatorInstanceKey
                  addMainPaneOverlayEntry({
                    signature: buildUniqueIndicatorName(`${indicator.id.toUpperCase()}_${paramValues.join('_')}`),
                    figures: def.figures.map(f => {
                      return buildFigure(`${f.key}_${ik}`, f.title, color, lineWidth, f.type || 'line')
                    }),
                    calc: (kLineDataList) => {
                      const p = {}
                      paramKeys.forEach((k, idx) => { p[k] = paramValues[idx] })
                      const raw = def.calc(kLineDataList, p)
                      if (Array.isArray(raw) && raw.length > 0 && typeof raw[0] === 'object' && raw[0] !== null && !Array.isArray(raw[0])) {
                        return raw.map(item => {
                          const mapped = {}
                          def.figures.forEach(f => { mapped[`${f.key}_${ik}`] = item[f.key] })
                          return mapped
                        })
                      }
                      const figKey = def.figures[0].key
                      return raw.map(v => ({ [`${figKey}_${ik}`]: v }))
                    }
                  })
                } else {
                  // 副图指标：创建独立 pane
                  const paneId = chartRef.value.createIndicator(customIndicatorName, false, { height: 100, dragEnabled: true })
                  if (paneId) {
                    addedIndicatorIds.value.push({ paneId, name: customIndicatorName })
                    addPaneCloseButton(paneId, indicator.id, indicator.instanceId)
                  }
                }
              }
            } catch (err) { /* 预期内：pane 容器可能已销毁，指标重建失败 */ }
          } else {
            // 尝试直接用 indicator.id 创建（假设是内置指标名）
            try {
              const indicatorName = indicator.id.toUpperCase()
              const indicatorId = chartRef.value.createIndicator(indicatorName, false, { height: 100, dragEnabled: true })
              if (indicatorId) {
                addedIndicatorIds.value.push({ paneId: indicatorId, name: indicatorName })
                addPaneCloseButton(indicatorId, indicator.id, indicator.instanceId)
              }
            } catch (err) { /* 预期内：pane 容器可能已销毁，关闭按钮添加失败 */ }
          }
          // ... 其他指标 ...
        } catch (e) {
        }
      }
      if (mainPaneOverlayFigures.length > 0) {
        try {
          const combinedName = `QD_MAIN_OVERLAY_${mainPaneOverlaySignatureParts.join('_').replace(/[^a-zA-Z0-9_]/g, '_').slice(0, 120)}`
          const registered = registerCustomIndicator(
            combinedName,
            (kLineDataList) => {
              const mergedResults = Array.from({ length: kLineDataList.length }, () => ({}))
              mainPaneOverlayCalcEntries.forEach(calc => {
                const partial = calc(kLineDataList) || []
                for (let i = 0; i < mergedResults.length; i++) {
                  if (partial[i] && typeof partial[i] === 'object') {
                    Object.assign(mergedResults[i], partial[i])
                  }
                }
              })
              return mergedResults
            },
            mainPaneOverlayFigures,
            [],
            -1,
            true
          )
          if (registered) {
            const paneId = chartRef.value.createIndicator(combinedName, true, { id: 'candle_pane' })
            if (paneId) {
              addedIndicatorIds.value.push({ paneId, name: combinedName })
            } else {
              addedIndicatorIds.value.push({ paneId: 'candle_pane', name: combinedName })
            }
          }
        } catch (e) {
        }
      }
      } finally {
        // 分时：指标刷新可能清空主图指标 → 补挂均价线与 Y 轴锚定
        try { ensureMinuteIndicators() } catch (_) { /* 预期内 */ }
        indicatorsUpdating.value = false
      }
    }

    const handleRetry = () => {
      loadKlineData()
    }

    // 生命周期
    /** debounce 包装，防止 symbol/market/timeframe 同时变化时重复加载 */
    let _loadDebounceTimer = null
    const debouncedLoad = () => {
      clearTimeout(_loadDebounceTimer)
      _loadDebounceTimer = setTimeout(() => {
        // P1-1: 筹码数据由 loadKlineData() 内部统一触发（K 线加载完成后），此处不再重复请求
        if (props.symbol) { loadKlineData() }
      }, 80)
    }

    /** 切换股票时自动适配：加载完成后滚动到最新并适配Y轴，仅执行一次 */
    watch(() => props.symbol, (newVal, oldVal) => {
      if (newVal && newVal !== oldVal) {
        // 标的变化后旧现场无意义，全部作废；视口回归默认让新数据最大化填充窗口
        _tfSceneMap = {}
        _resetViewportOnNextLoad = true
        debouncedLoad()
        // P1-1: 原先此处 1500ms 后再拉一次筹码，与 loadKlineData() 内部调用重复（一次切换请求 3 次），已移除
        // P0-2: 捕获切换后的目标 symbol，供下方自动适配回调比对
        const _targetSymbol = newVal
        // 延迟执行一次自动适配
        safeTimeout(() => {
          // 已再次切换标的 → 丢弃本次自动适配
          if (props.symbol !== _targetSymbol) return
          if (chartRef.value) {
            try {
              if (typeof chartRef.value.scrollToRealTime === 'function') {
                chartRef.value.scrollToRealTime()
              }
            } catch (_) { /* 预期内：图表未就绪时无法滚动到最新，静默忽略 */ }
          }
        }, 600)
      }
    })
    watch(() => props.theme, (newTheme) => {
      chartTheme.value = newTheme
      if (chartRef.value) {
        updateChartTheme()
        updateIndicators()
      }
      nextTick(() => _ensureWmLayer())
    })

    // P1-1: 筹码由 loadKlineData() 内部触发，market 变化走 debouncedLoad 即可，不再重复请求
    watch(() => props.market, () => {
      // 市场变化后交易时段/旧现场均无意义，全部作废
      _tfSceneMap = {}
      debouncedLoad()
    })
    watch(() => props.timeframe, (newTf, oldTf) => {
      // 切走前归档旧周期的图表现场（分时为锁定视图，无现场可存）
      if (oldTf && oldTf !== newTf) {
        const scene = captureChartScene()
        if (scene) _tfSceneMap[oldTf] = scene
      }
      debouncedLoad()
    })

    watch(() => props.activeIndicators, (newVal, oldVal) => {
      // 当指标列表变化时，重新渲染图表
      if (chartRef.value && klineData.value.length > 0) {
        // 使用 nextTick 确保 DOM 更新完成后再更新图表
        nextTick(() => {
          if (chartRef.value) {
            updateIndicators()
          }
        })
      }
      if (indicatorEditorVisible.value && indicatorEditorTargetId.value) {
        const current = (newVal || []).find(item => item && (item.instanceId || item.id) === indicatorEditorTargetId.value)
        if (!current) {
          closeIndicatorEditor()
        }
      }
    }, { deep: true })

    // 分时：昨收可能异步到位（父组件传入 / 从 1m 数据推导 / 日线接口兜底），
    // 到位后立即补画 0 轴线并锁定 Y 轴，避免「数据先渲染、昨收后到」导致两者都缺失
    watch(() => minutePrevClose.value, (pc) => {
      if (!isMinuteLine.value || !chartRef.value) return
      if (pc == null || !(pc > 0)) return
      nextTick(() => {
        if (!isMinuteLine.value || !chartRef.value) return
        try { setupMinutePrevCloseReference() } catch (_) { /* 预期内 */ }
      })
    })

    watch(() => props.realtimeEnabled, (newVal) => {
      if (newVal) {
        startRealtime()
      } else {
        stopRealtime()
      }
    })

    watch(() => props.showChip, () => {
      // 筹码开关切换后，图表宽度/布局变化，需重绘并让芯片重新对齐
      nextTick(() => {
        if (chartRef.value && typeof chartRef.value.resize === 'function') {
          chartRef.value.resize()
        }
        renderChip()
      })
    })

    onMounted(async () => {
      // 优先使用 props.theme（从 Vuex store 获取），确保与系统主题同步
      // 使用 nextTick 确保 props 已经正确传递
      await nextTick()
      if (props.theme && (props.theme === 'dark' || props.theme === 'light')) {
        chartTheme.value = props.theme
      }

      // 加载 Pyodide
      try {
        await loadPyodide()
      } catch (err) {
        pyodideLoadFailed.value = true
      }

      nextTick(() => {
        // P0-1: 受管 timer + 卸载守卫，避免卸载后重建图表
        safeTimeout(() => {
          if (_isUnmounted) return
          if (!chartRef.value && props.symbol) {
            initChart()
          }
        }, 300)
      })

      nextTick(() => {
        const el = document.getElementById('kline-chart-container')
        if (!el || typeof ResizeObserver === 'undefined') return
        chartResizeObserver = new ResizeObserver(() => {
          if (chartResizeRafId != null) cancelAnimationFrame(chartResizeRafId)
          chartResizeRafId = requestAnimationFrame(() => {
            chartResizeRafId = null
            if (chartRef.value && typeof chartRef.value.resize === 'function') {
              chartRef.value.resize()
            } else {
              const c = document.getElementById('kline-chart-container')
              if (c && c.clientWidth > 0 && c.clientHeight > 0) {
                initChart()
              }
            }
            _ensureWmLayer()
            renderChip()
          })
        })
        chartResizeObserver.observe(el)
      })

      nextTick(() => {
        _ensureWmLayer()
        _startWmGuard()
      })
    })

    // ── Watermark (multi-layer, tamper-resistant) ──
    const _wmText = [81, 117, 97, 110, 116, 68, 105, 110, 103, 101, 114].map(c => String.fromCharCode(c)).join('')
    const _wmSub = [113, 117, 97, 110, 116, 100, 105, 110, 103, 101, 114, 46, 99, 111, 109].map(c => String.fromCharCode(c)).join('')

    const _paintWmCanvas = () => {
      const cvs = wmCanvasRef.value
      if (!cvs) return
      const parent = cvs.parentElement
      if (!parent) return
      const w = parent.clientWidth
      const h = parent.clientHeight
      if (w === 0 || h === 0) return
      const dpr = window.devicePixelRatio || 1
      cvs.width = w * dpr
      cvs.height = h * dpr
      cvs.style.width = w + 'px'
      cvs.style.height = h + 'px'
      const ctx = cvs.getContext('2d')
      if (!ctx) return
      ctx.clearRect(0, 0, cvs.width, cvs.height)
      ctx.save()
      ctx.scale(dpr, dpr)
      const isDark = chartTheme.value === 'dark'
      // main brand
      ctx.font = 'bold 18px "Segoe UI", Helvetica, Arial, sans-serif'
      ctx.fillStyle = isDark ? 'rgba(255,255,255,0.07)' : 'rgba(0,0,0,0.06)'
      ctx.textBaseline = 'bottom'
      ctx.fillText(_wmText, 12, h - 24)
      // sub domain
      ctx.font = '11px "Segoe UI", Helvetica, Arial, sans-serif'
      ctx.fillStyle = isDark ? 'rgba(255,255,255,0.05)' : 'rgba(0,0,0,0.045)'
      ctx.fillText(_wmSub, 12, h - 10)
      // tiled repeat across chart
      ctx.font = '13px "Segoe UI", Helvetica, Arial, sans-serif'
      ctx.fillStyle = isDark ? 'rgba(255,255,255,0.025)' : 'rgba(0,0,0,0.022)'
      ctx.save()
      ctx.rotate(-0.35)
      for (let y = 0; y < h + 200; y += 140) {
        for (let x = -200; x < w + 200; x += 260) {
          ctx.fillText(_wmText, x, y)
        }
      }
      ctx.restore()
      ctx.restore()
    }

    // ═══════════════════════════════════════════
    // 筹码分布图
    // ═══════════════════════════════════════════
    /** 用前端 K 线数据直接计算筹码分布（不依赖后端 API） */
    const fetchChipData = () => {
      const data = klineData.value
      if (!data || data.length < 10) { _chipData = null; return }

      // 取最近 120 根 K 线
      const lookback = Math.min(data.length, 120)
      const bars = data.slice(-lookback)
      const numBuckets = 60

      // 价格区间
      let priceMin = Infinity, priceMax = -Infinity
      for (const b of bars) {
        if (b.low < priceMin) priceMin = b.low
        if (b.high > priceMax) priceMax = b.high
      }
      if (priceMax <= priceMin) { _chipData = null; return }

      const bucketWidth = Math.max((priceMax - priceMin) / numBuckets, 0.0001)
      const buckets = Math.ceil((priceMax - priceMin) / bucketWidth) + 1
      const chipDensity = new Float64Array(buckets)
      const n = bars.length

      for (let i = 0; i < n; i++) {
        const lo = bars[i].low, hi = bars[i].high, cl = bars[i].close, vol = bars[i].volume || 0
        if (hi <= lo || vol <= 0) continue
        const decay = Math.pow(0.98, n - 1 - i)
        const leftHalf = Math.max(cl - lo, 0.0001)
        const rightHalf = Math.max(hi - cl, 0.0001)
        const steps = Math.max(Math.floor((hi - lo) / bucketWidth) + 1, 8)
        let totalW = 0
        const weights = []
        for (let j = 0; j <= steps; j++) {
          const p = lo + (hi - lo) * j / steps
          const dist = p <= cl ? (cl - p) / leftHalf : (p - cl) / rightHalf
          const w = Math.max(1 - dist, 0)
          weights.push({ p, w })
          totalW += w
        }
        if (totalW <= 0) continue
        for (const { p, w } of weights) {
          let idx = Math.floor((p - priceMin) / bucketWidth)
          if (idx < 0) idx = 0
          if (idx >= buckets) idx = buckets - 1
          chipDensity[idx] += vol * decay * w / totalW
        }
      }

      let maxD = 0
      for (let i = 0; i < buckets; i++) { if (chipDensity[i] > maxD) maxD = chipDensity[i] }
      if (maxD <= 0) { _chipData = null; return }

      const prices = []
      const density = []
      for (let i = 0; i < buckets; i++) {
        prices.push(priceMin + i * bucketWidth)
        density.push(chipDensity[i] / maxD)
      }

      const currentPrice = bars[n - 1].close
      let totalChips = 0
      for (let i = 0; i < buckets; i++) totalChips += chipDensity[i]
      let costSum = 0
      for (let i = 0; i < buckets; i++) costSum += prices[i] * chipDensity[i]
      const avgCost = costSum / totalChips

      _chipData = { prices, density, avgCost, currentPrice }
      chipDataForTemplate.value = { avgCost }
      renderChip()
    }

    const renderChip = () => {
      _syncChipPaneObserver()
      if (_chipRafId != null) return
      _chipRafId = requestAnimationFrame(() => {
        _chipRafId = null
        _doRenderChip()
      })
    }

    // 监听主图 pane 的尺寸变化（副图新增/删除/拖拽改高都会改变主图高度），
    // 触发筹码覆盖层自动重新定位与重绘，保证始终与蜡烛图区域对齐。
    const _syncChipPaneObserver = () => {
      const chart = chartRef.value
      if (!chart) return
      let paneEl = null
      try { paneEl = chart.getDom('candle_pane', 'root') } catch (_) { /* 预期内：candle pane 尚未渲染 */ }
      if (!paneEl) return
      if (_chipPaneObserver) {
        try { _chipPaneObserver.unobserve(paneEl); _chipPaneObserver.disconnect() } catch (_) { /* 预期内：Observer 可能已断开 */ }
        _chipPaneObserver = null
      }
      if (typeof ResizeObserver === 'undefined') return
      _chipPaneObserver = new ResizeObserver(() => {
        renderChip()
      })
      _chipPaneObserver.observe(paneEl)
    }

    const _doRenderChip = () => {
      const canvas = chipCanvasRef.value
      const overlay = chipOverlayRef.value
      const chart = chartRef.value
      if (!canvas || !chart) return

      // 获取主图 pane 的高度，确保筹码图与主图Y轴对齐
      const container = document.getElementById('kline-chart-container')
      if (!container) return
      const containerH = container.clientHeight

      // 通过主图 pane 的真实包围盒定位筹码覆盖层，使其严格与蜡烛图区域重合
      // （不占用副图区域；副图 add/remove/拖拽改高度时由 ResizeObserver 重新测量）
      let overlayTop = 0
      let paneH = containerH
      try {
        const containerRect = container.getBoundingClientRect()
        const mainPane = chart.getDom('candle_pane', 'root')
        if (mainPane && mainPane.clientHeight > 0) {
          const paneRect = mainPane.getBoundingClientRect()
          overlayTop = Math.max(0, paneRect.top - containerRect.top)
          paneH = paneRect.height
        }
      } catch (_) { /* 预期内：pane 尺寸读取失败，回退默认值 */ }

      // 覆盖层紧跟主图：top/height 与主图重合，不计入副图
      if (overlay) {
        overlay.style.top = overlayTop + 'px'
        overlay.style.height = paneH + 'px'
      }

      // 筹码图使用完整主图高度绘制，使价格刻度与蜡烛图完全对应
      const h = paneH

      // 使用 canvas 自身的宽度，高度使用主图高度
      const w = canvas.clientWidth || 140
      if (w <= 0 || h <= 0) return

      const dpr = window.devicePixelRatio || 1
      canvas.width = w * dpr
      canvas.height = h * dpr
      canvas.style.width = w + 'px'
      canvas.style.height = h + 'px'

      const ctx = canvas.getContext('2d')
      ctx.scale(dpr, dpr)
      ctx.clearRect(0, 0, w, h)

      if (!_chipData || !_chipData.prices || !_chipData.density) return
      if (!_chipData.prices.length) return

      const { prices, density, avgCost, currentPrice } = _chipData

      // 获取可见价格范围（与 K 线图对齐）
      const data = klineData.value
      if (!data || data.length === 0) return
      const range = chart.getVisibleRange()
      if (!range) return
      const dataLen = data.length
      // klinecharts v9.8.0 的 getVisibleRange() 返回的是数据索引（整数），不是百分比
      const fromIdx = Math.max(0, range.from)
      const toIdx = Math.min(dataLen - 1, range.to)
      if (fromIdx >= toIdx) return

      let visHigh = -Infinity
      let visLow = Infinity
      for (let i = fromIdx; i <= toIdx; i++) {
        if (data[i].high > visHigh) visHigh = data[i].high
        if (data[i].low < visLow) visLow = data[i].low
      }
      if (visHigh <= visLow) return

      // 价格 → 主图像素 Y：走 klinecharts 的 Y 轴换算，
      // 天然对齐右轴（含上下留白、百分比/价格模式、缩放状态），不再用自算线性映射
      const priceToY = (price) => {
        try {
          const c = chart.convertToPixel([{ value: price }], { paneId: 'candle_pane' })
          const coord = Array.isArray(c) ? c[0] : c
          return coord && coord.y != null && isFinite(coord.y) ? coord.y : null
        } catch (_) { return null }
      }

      // 筹码图显示区域：占满整个副图宽度
      const padding = 4
      const maxBarWidth = w - padding * 2

      // 颜色
      const isDark = chartTheme.value === 'dark'
      const profitColor = isDark ? 'rgba(239,83,80,0.6)' : 'rgba(245,34,45,0.5)'
      const lossColor = isDark ? 'rgba(14,203,129,0.6)' : 'rgba(82,196,26,0.5)'
      const avgCostColor = isDark ? '#faad14' : '#fa8c16'
      const gridColor = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)'

      // 画网格线（价格刻度）
      ctx.strokeStyle = gridColor
      ctx.lineWidth = 0.5
      const priceStep = (visHigh - visLow) / 5
      for (let i = 0; i <= 5; i++) {
        const price = visLow + priceStep * i
        const y = priceToY(price)
        if (y == null) continue
        ctx.beginPath()
        ctx.moveTo(0, y)
        ctx.lineTo(w, y)
        ctx.stroke()
      }

      // 画筹码条
      const barHeight = Math.max(2, h / prices.length)
      for (let i = 0; i < prices.length; i++) {
        const price = prices[i]
        const d = density[i]
        if (d <= 0) continue

        const y = priceToY(price)
        if (y == null || y < -barHeight || y > h + barHeight) continue
        const barWidth = d * maxBarWidth
        const color = price <= currentPrice ? profitColor : lossColor

        ctx.fillStyle = color
        ctx.fillRect(padding, y - barHeight / 2, barWidth, barHeight)
      }

      // 平均成本线
      {
        const avgY = priceToY(avgCost)
        if (avgY != null && avgY >= 0 && avgY <= h) {
          ctx.strokeStyle = avgCostColor
          ctx.lineWidth = 1.5
          ctx.setLineDash([4, 3])
          ctx.beginPath()
          ctx.moveTo(0, avgY)
          ctx.lineTo(w, avgY)
          ctx.stroke()
          ctx.setLineDash([])

          // AVG 标签
          ctx.fillStyle = avgCostColor
          ctx.font = 'bold 10px sans-serif'
          ctx.textAlign = 'left'
          ctx.fillText(`AVG ${avgCost.toFixed(2)}`, padding, avgY - 4)
        }
      }

      // 当前价格线
      {
        const curY = priceToY(currentPrice)
        if (curY != null && curY >= 0 && curY <= h) {
          ctx.strokeStyle = isDark ? '#1890ff' : '#1890ff'
          ctx.lineWidth = 1
          ctx.setLineDash([2, 2])
          ctx.beginPath()
          ctx.moveTo(0, curY)
          ctx.lineTo(w, curY)
          ctx.stroke()
          ctx.setLineDash([])
        }
      }
    }

    // ==================== 左侧百分比坐标轴（自绘，与右侧金额轴同范围同步） ====================
    // klinecharts 每个 pane 只支持单根 Y 轴（右=金额）。左轴列由 CSS padding-left 预留，
    // 本引擎在每个范围/数据变化后用与右轴完全相同的 convertToPixel 换算百分比刻度像素，
    // 因此两轴天然同步。0% 基准：分时=昨收；其它周期=数据最新收盘价。

    /** 左轴百分比基准：分时→昨收；其它周期→最新一根真实 bar 的收盘价 */
    const _pctRulerBase = () => {
      const chart = chartRef.value
      if (!chart) return null
      try {
        if (isMinuteLine.value) {
          const pc = minutePrevClose.value
          if (pc != null && pc > 0) return pc
        }
        const list = typeof chart.getDataList === 'function' ? chart.getDataList() : []
        for (let i = list.length - 1; i >= 0; i--) {
          const b = list[i]
          const c = b ? Number(b.close) : NaN
          if (Number.isFinite(c) && c > 0) return c
        }
      } catch (_) { /* 预期内 */ }
      return null
    }

    /** 绘制失败埋点：仅在组件已有行情数据时计数；连续失败达阈值后输出一次性控制台诊断。
     *  设计目的：左轴任何单点失效都不再"静默空白"，用户控制台可直接看到原因 */
    const _pctFail = (tag) => {
      if (_pctEverPainted || !(klineData.value && klineData.value.length > 0)) return undefined
      _pctFailCount += 1
      _pctFailTag = tag
      if (_pctFailCount >= 8 && !_pctDiagShown) {
        _pctDiagShown = true
        console.info('[KlineChart] 左侧百分比轴未绘制，原因: ' + _pctFailTag + '（本提示仅出现一次）')
      }
      return undefined
    }

    /** rAF 合并：安排一次左轴重绘 */
    const _schedulePctRulerPaint = () => {
      if (_pctRafId != null) return
      _pctRafId = requestAnimationFrame(() => {
        _pctRafId = null
        _pctRulerPaint()
      })
    }

    const _pctRulerText = (r, decimals) => {
      const v = Number(r.toFixed(decimals))
      return `${v > 0 ? '+' : ''}${v.toFixed(decimals)}%`
    }

    /** 绘制左侧百分比坐标（幂等：范围/基准/尺寸/主题/光标未变则跳过） */
    const _pctRulerPaint = () => {
      const canvas = pctAxisCanvasRef.value
      const overlay = pctAxisOverlayRef.value
      const chart = chartRef.value
      if (!canvas || !chart) return _pctFail('canvas/chart 引用未就绪（模板未挂载？）')
      // 价格→像素换算：优先走轴实例（与右轴完全一致的内部换算）；
      // 私有属性不可用时降级为公开 API convertToPixel/convertFromPixel 适配
      let axis = null
      try {
        axis = chart._candlePane && typeof chart._candlePane.getAxisComponent === 'function'
          ? chart._candlePane.getAxisComponent()
          : null
      } catch (_) { /* 预期内 */ }
      let priceToY = null
      let yToPrice = null
      if (axis && typeof axis.convertToPixel === 'function' && typeof axis.convertFromPixel === 'function') {
        priceToY = (v) => axis.convertToPixel(v)
        yToPrice = (y) => axis.convertFromPixel(y)
      } else if (typeof chart.convertToPixel === 'function' && typeof chart.convertFromPixel === 'function') {
        priceToY = (v) => {
          const r = chart.convertToPixel([{ dataIndex: 0, value: v }], { paneId: 'candle_pane' })
          return r && r[0] ? r[0].y : null
        }
        yToPrice = (y) => {
          const r = chart.convertFromPixel([{ y }], { paneId: 'candle_pane' })
          return r && r[0] ? r[0].value : null
        }
      }
      if (!priceToY || !yToPrice) return _pctFail('无法获取价格→像素换算（axis/公开API 均不可用）')
      // 右轴恒为金额轴：若被其它路径切到 percentage/log，这里强制回 normal 并等下一轮重画
      if (axis && typeof axis.getType === 'function' && axis.getType() !== 'normal') {
        try { chart.setStyles({ yAxis: { type: 'normal' } }) } catch (_) { /* 预期内 */ }
        _schedulePctRulerPaint()
        return
      }
      const base = _pctRulerBase()
      if (base == null || !(base > 0)) return _pctFail('无有效基准价（昨收/最新收盘）')

      // 1) 定位：overlay top/height = candle pane root 相对覆盖层定位父级（.kline-chart-with-pct）的真实区域。
      //    容器优先用模板 ref（组件内自引用，免疫同页重复 id）；pane 取不到时降级为容器整高，宁可偏、不可无
      const container = klineContainerRef.value || document.getElementById('kline-chart-container')
      if (!container) return _pctFail('找不到主图容器')
      const host = (overlay && overlay.offsetParent) || container.parentElement || container
      let overlayTop = 0
      let paneH = 0
      try {
        const hostRect = host.getBoundingClientRect()
        let mainPane = null
        try { mainPane = chart.getDom('candle_pane', 'root') } catch (_) { /* 预期内：旧版本无此 API */ }
        if (!mainPane) {
          try { mainPane = chart._candlePane && chart._candlePane.getContainer ? chart._candlePane.getContainer() : null } catch (_) { /* 预期内 */ }
        }
        if (mainPane && mainPane.clientHeight > 0) {
          const paneRect = mainPane.getBoundingClientRect()
          overlayTop = Math.max(0, paneRect.top - hostRect.top)
          paneH = paneRect.height
        } else {
          // 降级：拿不到主图 pane 时按容器整高绘制（可能略越过副图/时间轴，但保证轴可见）
          overlayTop = 0
          paneH = container.clientHeight
        }
      } catch (_) { /* 预期内 */ }
      if (!(paneH > 10)) return _pctFail('主图高度不足（容器未展开？）')
      if (overlay) {
        // 覆盖层为绝对定位：top 对齐主图 pane 顶，高度只覆盖主图带，避免覆盖副图/时间轴区域
        overlay.style.top = overlayTop + 'px'
        overlay.style.height = paneH + 'px'
      }
      const w = canvas.clientWidth || 64
      const h = paneH
      if (w <= 0 || h <= 0) return

      const rg = axis && typeof axis.getRange === 'function' ? axis.getRange() : null
      let pLow = Number(rg && rg.from)
      let pHigh = Number(rg && rg.to)
      if (!Number.isFinite(pLow) || !Number.isFinite(pHigh) || !(pHigh > pLow)) {
        // 降级：从数据自身 hi/lo 推算范围（极坐标锁定场景下数据极值≈锁定边界，误差可忽略）
        try {
          const list = chart.getDataList()
          let lo = Infinity
          let hi = -Infinity
          for (const b of list) {
            if (!b) continue
            const l = Number(b.low)
            const h = Number(b.high)
            if (Number.isFinite(l)) lo = Math.min(lo, l)
            if (Number.isFinite(h)) hi = Math.max(hi, h)
          }
          if (Number.isFinite(lo) && Number.isFinite(hi) && hi > lo) {
            const pad = (hi - lo) * 0.05
            pLow = lo - pad
            pHigh = hi + pad
          }
        } catch (_) { /* 预期内 */ }
      }
      if (!Number.isFinite(pLow) || !Number.isFinite(pHigh) || !(pHigh > pLow)) return _pctFail('无法确定价格范围')

      const isDark = chartTheme.value === 'dark'
      const crosshairY = _pctCrosshairY
      // 幂等签名：范围/基准/高度/主题/光标任一变化才重绘
      const sig = `${pLow}|${pHigh}|${base}|${h}|${isDark}|${crosshairY == null ? 'n' : crosshairY.toFixed(1)}`
      if (sig === _pctRulerSig) return
      _pctRulerSig = sig

      const dpr = window.devicePixelRatio || 1
      if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
        canvas.width = Math.round(w * dpr)
        canvas.height = Math.round(h * dpr)
        canvas.style.width = w + 'px'
        canvas.style.height = h + 'px'
      }
      const ctx = canvas.getContext && canvas.getContext('2d')
      if (!ctx) return
      ctx.save()
      ctx.scale(dpr, dpr)
      ctx.clearRect(0, 0, w, h)

      // 2) 百分比刻度网格：span=(pHigh/base-1)-(pLow/base-1)；step=nice(span/8)，下限 0.05
      const pctOf = (price) => (price / base - 1) * 100
      const pMin = pctOf(pLow)
      const pMax = pctOf(pHigh)
      if (!(pMax > pMin)) { ctx.restore(); return }
      let step = minuteNice((pMax - pMin) / 8.0)
      if (!(step > 0)) step = 0.05
      if (step < 0.05) step = 0.05
      const decimals = step >= 1 ? 0 : (step >= 0.1 ? 1 : 2)
      const rPrecision = Math.max(minuteGetPrecision(step), decimals)
      // 观感对齐右侧金额轴：字号/颜色/字族/刻度线全部取自 yAxis 样式，取不到再用兜底值
      let tickSize = 10
      let labelColor = isDark ? '#9b9b9b' : '#6b6b6b'
      let tickFamily = '-apple-system, "PingFang SC", "Microsoft YaHei", sans-serif'
      let tickLen = 3
      let tickLineColor = 'rgba(128,128,128,0.4)'
      try {
        const ys = chart.getStyles() && chart.getStyles().yAxis
        if (ys && ys.tickText) {
          tickSize = ys.tickText.size || tickSize
          labelColor = ys.tickText.color || labelColor
          tickFamily = ys.tickText.family || tickFamily
        }
        if (ys && ys.tickLine) {
          tickLen = ys.tickLine.length != null ? ys.tickLine.length : tickLen
          tickLineColor = ys.tickLine.color || tickLineColor
        }
      } catch (_) { /* 预期内：样式读取失败时用兜底值 */ }
      const zeroColor = isDark ? '#d7d7d7' : '#262626'
      const font = `${tickSize}px ${tickFamily}`

      ctx.textAlign = 'right'
      ctx.textBaseline = 'middle'
      const labelX = w - 8 - tickLen
      for (let r = Math.ceil(pMin / step) * step; r <= pMax + step * 1e-6; r = minuteRound(r + step, rPrecision)) {
        const price = base * (1 + r / 100)
        let y = null
        try { y = priceToY(price) } catch (_) { /* 预期内 */ }
        if (y == null || !Number.isFinite(y) || y < 2 || y > h - 2) continue
        const isZero = Math.abs(r) < step * 0.5
        // 刻度短线（镜像右轴布局：文字 | 刻度线 | 图表）
        ctx.strokeStyle = isZero ? zeroColor : tickLineColor
        ctx.lineWidth = isZero ? 1.5 : 1
        ctx.beginPath()
        ctx.moveTo(w - 3 - tickLen, y)
        ctx.lineTo(w - 3, y)
        ctx.stroke()
        // 标签
        ctx.fillStyle = isZero ? zeroColor : labelColor
        ctx.font = isZero ? `bold ${font}` : font
        ctx.fillText(_pctRulerText(r, decimals), labelX, y)
      }

      // 3) 十字光标处的百分比读数（与右轴价格标签对应：昨收/最新收盘基准）
      if (crosshairY != null && crosshairY >= 0 && crosshairY <= h) {
        let priceAt = null
        try { priceAt = yToPrice(crosshairY) } catch (_) { /* 预期内 */ }
        if (priceAt != null && Number.isFinite(priceAt) && priceAt > 0) {
          const rr = Number(pctOf(priceAt).toFixed(2))
          const txt = `${rr > 0 ? '+' : ''}${rr.toFixed(2)}%`
          ctx.font = font
          const tw = Math.ceil(ctx.measureText(txt).width)
          const chipW = tw + 8
          const chipH = 15
          const cx = Math.max(1, w - 2 - chipW)
          const cyy = Math.max(0, Math.min(h - chipH, crosshairY - chipH / 2))
          ctx.fillStyle = isDark ? 'rgba(24,144,255,0.95)' : 'rgba(24,144,255,0.92)'
          ctx.fillRect(cx, cyy, chipW, chipH)
          ctx.fillStyle = '#fff'
          ctx.textAlign = 'left'
          ctx.fillText(txt, cx + 4, cyy + chipH / 2 + 0.5)
          ctx.textAlign = 'right'
        }
      }
      _pctEverPainted = true
      _pctFailCount = 0
      ctx.restore()
    }

    /** 建立左轴同步：ResizeObserver + 包装 buildTicks + 订阅数据/光标事件（幂等） */
    const _syncPctRuler = () => {
      const chart = chartRef.value
      if (!chart || !pctAxisVisible.value) return
      // 1) candle pane 尺寸变化（副图增删/拖拽/左右预留变化都改变主图高度或宽度）
      let paneEl = null
      try { paneEl = chart.getDom('candle_pane', 'root') } catch (_) { /* 预期内 */ }
      if (paneEl && typeof ResizeObserver !== 'undefined') {
        if (_pctPaneObserver) {
          try { _pctPaneObserver.disconnect() } catch (_) { /* 预期内 */ }
          _pctPaneObserver = null
        }
        _pctPaneObserver = new ResizeObserver(() => _schedulePctRulerPaint())
        try {
          _pctPaneObserver.observe(paneEl)
          _observers.add(_pctPaneObserver)
        } catch (_) { /* 预期内 */ }
      }
      // 2) 包装 axis.buildTicks：所有范围变化（Y 拖拽/双击复位/指标增删/applyNewData/锁轴）
      //    都经 adjustPaneViewport → 每 pane buildTicks，包装后即可获得可靠的重绘时机
      try {
        const axis = chart._candlePane && typeof chart._candlePane.getAxisComponent === 'function'
          ? chart._candlePane.getAxisComponent()
          : null
        if (axis && typeof axis.buildTicks === 'function' && _pctWrappedAxis !== axis) {
          _pctOrigBuildTicks = axis.buildTicks
          const origBuildTicks = axis.buildTicks
          axis.buildTicks = function (...args) {
            const res = origBuildTicks.apply(this, args)
            try { _schedulePctRulerPaint() } catch (_) { /* 预期内 */ }
            return res
          }
          _pctWrappedAxis = axis
        }
      } catch (_) { /* 预期内 */ }
      // 3) 数据就绪（换周期/实时追加 → 最新收盘基准可能变化）与十字光标
      if (!_pctSubscribed) {
        _pctSubscribed = true
        try {
          if (typeof chart.subscribeAction === 'function') {
            chart.subscribeAction('onDataReady', () => _schedulePctRulerPaint())
            chart.subscribeAction('onCrosshairChange', (crosshair) => {
              _pctCrosshairY = (crosshair && typeof crosshair.y === 'number') ? crosshair.y : null
              _schedulePctRulerPaint()
            })
          }
        } catch (_) { /* 预期内 */ }
      }
      // 兜底看门狗：即便 ResizeObserver/包装/订阅全部失效，也每 800ms 尝试补绘一次
      // （rAF 合并 + 幂等签名，绘制被跳过时开销可忽略）
      if (_pctWatchdog == null && typeof setInterval === 'function') {
        _pctWatchdog = setInterval(() => _schedulePctRulerPaint(), 800)
      }
      _schedulePctRulerPaint()
    }

    /** 换图表实例（重新 init）后：废弃旧绑定，等待下一次 _syncPctRuler 重建 */
    const _resetPctAxisBindings = () => {
      if (_pctWrappedAxis && _pctOrigBuildTicks) {
        try { _pctWrappedAxis.buildTicks = _pctOrigBuildTicks } catch (_) { /* 预期内 */ }
      }
      _pctWrappedAxis = null
      _pctOrigBuildTicks = null
      _pctSubscribed = false
      _pctCrosshairY = null
      _pctRulerSig = ''
    }

    /** 卸载清理：取消 rAF、断开 observer、还原被包装的 buildTicks */
    const _disposePctRuler = () => {
      if (_pctRafId != null) {
        try { cancelAnimationFrame(_pctRafId) } catch (_) { /* 预期内 */ }
        _pctRafId = null
      }
      if (_pctPaneObserver) {
        try { _pctPaneObserver.disconnect() } catch (_) { /* 预期内 */ }
        _pctPaneObserver = null
      }
      if (_pctWatchdog != null) {
        try { clearInterval(_pctWatchdog) } catch (_) { /* 预期内 */ }
        _pctWatchdog = null
      }
      _resetPctAxisBindings()
    }

    const _ensureWmLayer = () => {
      const cvs = wmCanvasRef.value
      if (!cvs) return
      // force visibility
      cvs.style.display = 'block'
      cvs.style.opacity = '1'
      cvs.style.visibility = 'visible'
      cvs.style.pointerEvents = 'none'
      _paintWmCanvas()
    }

    const _startWmGuard = () => {
      if (_wmTimer) clearInterval(_wmTimer)
      _wmTimer = setInterval(_ensureWmLayer, 3000)

      if (typeof MutationObserver !== 'undefined' && wmCanvasRef.value) {
        if (_wmObserver) _wmObserver.disconnect()
        _wmObserver = new MutationObserver(() => { _ensureWmLayer() })
        _wmObserver.observe(wmCanvasRef.value, { attributes: true, attributeFilter: ['style', 'class'] })
        const parent = wmCanvasRef.value.parentElement
        if (parent) {
          _wmObserver.observe(parent, { childList: true })
        }
      }
    }

    onBeforeUnmount(() => {
      // P0-1: 先标记已卸载，阻断所有延迟回调继续创建图表
      _isUnmounted = true
      // 清理全部受管 setTimeout
      _timers.forEach(clearTimeout)
      _timers.clear()
      // 清理 debounce 加载定时器
      if (_loadDebounceTimer) { clearTimeout(_loadDebounceTimer); _loadDebounceTimer = null }
      // 清理等待容器尺寸的 ResizeObserver（冗余修复引入）
      _observers.forEach(ro => ro.disconnect())
      _observers.clear()
      // 清理 handleResize 的 rAF（P1-4 引入）
      if (_resizeRafId != null) { cancelAnimationFrame(_resizeRafId); _resizeRafId = null }

      stopRealtime()
      wsClient = null
      if (realtimeChartRafId != null) {
        cancelAnimationFrame(realtimeChartRafId)
        realtimeChartRafId = null
      }
      if (chartResizeRafId != null) {
        cancelAnimationFrame(chartResizeRafId)
        chartResizeRafId = null
      }
      if (chartResizeObserver) {
        chartResizeObserver.disconnect()
        chartResizeObserver = null
      }
      if (_chipRafId != null) { cancelAnimationFrame(_chipRafId); _chipRafId = null }
      if (_chipPaneObserver) { _chipPaneObserver.disconnect(); _chipPaneObserver = null }
      _chipData = null
      if (_wmTimer) { clearInterval(_wmTimer); _wmTimer = null }
      if (_wmObserver) { _wmObserver.disconnect(); _wmObserver = null }
      // 清理左侧百分比坐标轴（取消 rAF/observer，并在销毁前还原被包装的 buildTicks）
      _disposePctRuler()
      // 清理分时图交互禁用事件处理器
      enableMinuteInteractions()
      if (chartRef.value) {
        chartRef.value.destroy()
        chartRef.value = null
        volPaneId.value = null
      }
      window.removeEventListener('resize', handleResize)
    })

    return {
      klineData,
      loading,
      error,
      loadingHistory,
      chartRef,
      // 左侧百分比轴三件套必须导出给模板：缺失时 v-if="pctAxisVisible" 取到 undefined，
      // 轴 DOM 根本不会挂载（这正是此前"左轴始终不显示"的根因），refs 也拿不到元素
      pctAxisVisible,
      pctAxisOverlayRef,
      pctAxisCanvasRef,
      klineContainerRef,
      chartTheme,
      themeConfig,
      isMinuteLine,
      wmCanvasRef,
      chipCanvasRef,
      chipOverlayRef,
      chipDataForTemplate,
      getIndicatorColor,
      handleRetry,
      loadingPython,
      pythonReady,
      pyodideLoadFailed,
      formatKlineData,
      updatePricePanel,
      isSameTimeframe,
      loadKlineData,
      updateKlineRealtime,
      startRealtime,
      stopRealtime,
      initChart,
      handleResize,
      updateChartTheme,
      updateIndicators,
      renderChip,
      fetchChipData,
      executePythonStrategy,
      parsePythonStrategy,
      indicatorButtons,
      activePresetIndicators,
      handleIndicatorButtonClick,
      isIndicatorActive,
      toggleIndicator,
      indicatorEditorVisible,
      indicatorEditorSaving,
      indicatorEditorForm,
      indicatorEditorSchema,
      indicatorEditorTitle,
      indicatorEditorModalWrapClass,
      formatIndicatorInstanceLabel,
      openIndicatorEditor,
      closeIndicatorEditor,
      applyIndicatorEditor,
      removeIndicatorInstance,
      toggleIndicatorVisibility,
      drawingTools,
      activeDrawingTool,
      selectDrawingTool,
      clearAllDrawings,
      addedSignalOverlayIds,
      hideIndicatorSignals () {
        if (addedSignalOverlayIds.value.length > 0 && chartRef.value) {
          addedSignalOverlayIds.value.forEach(id => {
            try {
              if (typeof chartRef.value.removeOverlay === 'function') chartRef.value.removeOverlay(id)
              else if (typeof chartRef.value.removeOverlayById === 'function') chartRef.value.removeOverlayById(id)
            } catch (_) { /* 预期内：overlay 可能已被移除 */ }
          })
        }
      },
      showIndicatorSignals () {
        // P1-2 修复：原为空实现，父组件调用后信号不显示且无任何提示。
        // 重新应用指标以重绘买卖信号标记（与 hideIndicatorSignals 对称）
        if (!chartRef.value) return
        try {
          updateIndicators()
        } catch (e) {
          console.warn('[KlineChart] 重绘指标信号失败:', e)
        }
      },
      /** 切换K线配色方案: cn=红涨绿跌, intl=绿涨红跌 */
      setChartColorScheme (scheme) {
        if (!chartRef.value) return
        const isIntl = scheme === 'intl'
        const isDark = props.theme === 'dark'
        try {
          chartRef.value.setStyles({
            candle: {
              bar: {
                upColor: isIntl ? (isDark ? '#0ecb81' : '#52c41a') : (isDark ? '#ef5350' : '#f5222d'),
                downColor: isIntl ? (isDark ? '#ef5350' : '#f5222d') : (isDark ? '#0ecb81' : '#52c41a'),
                upBorderColor: isIntl ? (isDark ? '#0ecb81' : '#52c41a') : (isDark ? '#ef5350' : '#f5222d'),
                downBorderColor: isIntl ? (isDark ? '#ef5350' : '#f5222d') : (isDark ? '#0ecb81' : '#52c41a'),
                upWickColor: isIntl ? (isDark ? '#0ecb81' : '#52c41a') : (isDark ? '#ef5350' : '#f5222d'),
                downWickColor: isIntl ? (isDark ? '#ef5350' : '#f5222d') : (isDark ? '#0ecb81' : '#52c41a')
              }
            },
            indicator: {
              bars: [{
                upColor: isIntl ? (isDark ? '#0ecb81' : '#52c41a') : (isDark ? '#ef5350' : '#f5222d'),
                downColor: isIntl ? (isDark ? '#ef5350' : '#f5222d') : (isDark ? '#0ecb81' : '#52c41a')
              }]
            }
          })
        } catch (e) { console.warn('setChartColorScheme failed:', e) }
        _schedulePctRulerPaint()
      },
      /** 显示/隐藏画线工具栏 */
      setDrawingBarVisible (visible) {
        const el = document.querySelector('.drawing-toolbar')
        if (el) el.style.display = visible ? 'flex' : 'none'
      },
      /**
       * 分时极坐标开关（设置弹窗「右侧Y轴 金额/比例」已废弃，右轴恒为金额轴）。
       * 开：分时 Y 轴固定 昨收±涨跌停%（主板±10/创业·科创±20/北交所±30），
       *     顶部=+limit%、底部=-limit%，0 轴线（昨收）居中；
       * 关：按当日最大涨跌幅自适应对称锁定。
       * 左轴百分比列与右轴金额列始终同范围同步显示。
       */
      setMinutePolarMode (enabled) {
        _minutePolarEnabled = !!enabled
        if (!chartRef.value) return
        // 右轴恒为金额轴；setStyles 指定 type 会重置自动刻度标志（解除锁定），
        // 随后由重试补锁与下一布局自愈
        try {
          chartRef.value.setStyles({ yAxis: { type: 'normal' } })
        } catch (_) { /* 预期内 */ }
        if (isMinuteLine.value) {
          ;[60, 300].forEach(delay => {
            safeTimeout(() => {
              if (!isMinuteLine.value || !chartRef.value) return
              try {
                applyMinutePrevCloseAxis()
                _schedulePctRulerPaint()
              } catch (_) { /* 预期内 */ }
            }, delay)
          })
        }
        _schedulePctRulerPaint()
      }
    }
  }
}
</script>

<style lang="less" scoped>
/* 左侧图表容器 */
.chart-left {
  width: 70% !important;
  flex: 0 0 70% !important;
  position: relative;
  border-right: 1px solid #e8e8e8;
  background: #fff;
  transition: background-color 0.3s;
  touch-action: pan-x pan-y;
  -webkit-overflow-scrolling: touch;

  &.theme-dark {
    background: #141414;
    border-right-color: #2a2a2a;
  }
}

.chart-wrapper {
  width: 100%;
  height: 100%;
  position: relative;
  background: #fff;
  transition: background-color 0.3s;
  touch-action: pan-x pan-y;
  -webkit-overflow-scrolling: touch;
  display: flex;

  .theme-dark & {
    background: #141414;
  }
}

/* 画线工具工具栏 */
.drawing-toolbar {
  flex-shrink: 0;
  width: 40px;
  background: #fff;
  border-right: 1px solid #e8e8e8;
  display: none;
  flex-direction: column;
  align-items: center;
  padding: 8px 4px;
  gap: 4px;
  z-index: 10;
  overflow-y: auto;
  overflow-x: hidden;
}

.chart-left.theme-dark .drawing-toolbar {
  background: #141414;
  border-right-color: #2a2a2a;
}

.drawing-tool-btn {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  border-radius: 4px;
  transition: all 0.2s;
  color: #666;
  font-size: 16px;
  user-select: none;
}

.chart-left.theme-dark .drawing-tool-btn {
  color: #d1d4dc;
}

.drawing-tool-btn:hover {
  background: #f0f2f5;
  color: #1890ff;
}

.chart-left.theme-dark .drawing-tool-btn:hover {
  background: #252525;
  color: #13c2c2;
}

.drawing-tool-btn.active {
  background: #e6f7ff;
  color: #1890ff;
  border: 1px solid #1890ff;
}

.chart-left.theme-dark .drawing-tool-btn.active {
  background: #252525;
  color: #13c2c2;
  border-color: #13c2c2;
}

.drawing-toolbar .ant-divider-vertical {
  margin: 8px 0;
  height: 20px;
}

/* 指标工具栏 */
.indicator-toolbar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: #fff;
  border-bottom: 1px solid #e8e8e8;
  flex-wrap: wrap;
  z-index: 1;
  position: relative;
  width: 100%;
  overflow-x: auto;
  overflow-y: hidden;
  scrollbar-width: none; /* Firefox */
  -ms-overflow-style: none; /* IE 10+ */
}

.indicator-toolbar::-webkit-scrollbar {
  display: none; /* Chrome Safari */
  width: 0;
  height: 0;
}

.indicator-active-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 0 12px 10px;
  background: #fff;
  border-bottom: 1px solid #f0f0f0;
}

.chart-left.theme-dark .indicator-active-bar {
  background: #141414;
  border-bottom-color: #2a2a2a;
}

.indicator-active-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 5px 10px;
  border-radius: 999px;
  background: #f7faff;
  border: 1px solid #d6e4ff;
  color: #1f1f1f;
  font-size: 12px;
  line-height: 1;
}

.indicator-active-chip--hidden {
  opacity: 0.65;
  background: #fafafa;
  border-color: #d9d9d9;
}

.chart-left.theme-dark .indicator-active-chip {
  background: rgba(24, 144, 255, 0.12);
  border-color: rgba(24, 144, 255, 0.28);
  color: rgba(255, 255, 255, 0.88);
}

.chart-left.theme-dark .indicator-active-chip--hidden {
  background: #1f1f1f;
  border-color: #434343;
  color: rgba(255, 255, 255, 0.55);
}

.indicator-active-chip__label {
  cursor: pointer;
  font-weight: 600;
}

.indicator-active-chip__action {
  cursor: pointer;
  color: #8c8c8c;
  transition: color 0.2s ease;
}

.indicator-active-chip__action:hover {
  color: #1890ff;
}

.chart-left.theme-dark .indicator-active-chip__action {
  color: rgba(255, 255, 255, 0.55);
}

.chart-left.theme-dark .indicator-active-chip__action:hover {
  color: #13c2c2;
}

.indicator-editor-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.indicator-editor-field__label {
  margin-bottom: 8px;
  color: #262626;
  font-weight: 600;
}

.indicator-editor-field__hint {
  margin-top: 6px;
  font-size: 12px;
  color: #8c8c8c;
}

.indicator-editor-color {
  width: 100%;
  height: 36px;
  padding: 4px;
  border: 1px solid #d9d9d9;
  border-radius: 8px;
  background: #fff;
  cursor: pointer;
}

.chart-left.theme-dark .indicator-editor-color {
  border-color: #434343;
  background: #1f1f1f;
}

.indicator-editor-empty {
  color: #8c8c8c;
}

/deep/ .indicator-editor-modal--dark .ant-modal-content {
  background: #1f1f1f;
  box-shadow: 0 12px 36px rgba(0, 0, 0, 0.45);
}

/deep/ .indicator-editor-modal--dark .ant-modal-header {
  background: #1f1f1f;
  border-bottom: 1px solid #303030;
}

/deep/ .indicator-editor-modal--dark .ant-modal-title {
  color: rgba(255, 255, 255, 0.9);
}

/deep/ .indicator-editor-modal--dark .ant-modal-close {
  color: rgba(255, 255, 255, 0.45);
}

/deep/ .indicator-editor-modal--dark .ant-modal-close:hover {
  color: rgba(255, 255, 255, 0.85);
}

/deep/ .indicator-editor-modal--dark .ant-modal-body {
  background: #1f1f1f;
}

/deep/ .indicator-editor-modal--dark .ant-modal-footer {
  background: #1f1f1f;
  border-top: 1px solid #303030;
}

/deep/ .indicator-editor-modal--dark .ant-input-number {
  background: #141414;
  border-color: #434343;
}

/deep/ .indicator-editor-modal--dark .ant-input-number-input {
  background: transparent;
  color: rgba(255, 255, 255, 0.88);
}

/deep/ .indicator-editor-modal--dark .ant-input-number-handler-wrap {
  background: #141414;
  border-left-color: #303030;
}

/deep/ .indicator-editor-modal--dark .ant-input-number-handler {
  color: rgba(255, 255, 255, 0.45);
}

/deep/ .indicator-editor-modal--dark .ant-input-number:hover,
/deep/ .indicator-editor-modal--dark .ant-input-number-focused {
  border-color: #177ddc;
}

/deep/ .indicator-editor-modal--dark .indicator-editor-field__label {
  color: rgba(255, 255, 255, 0.88);
}

/deep/ .indicator-editor-modal--dark .indicator-editor-field__hint,
/deep/ .indicator-editor-modal--dark .indicator-editor-empty {
  color: rgba(255, 255, 255, 0.45);
}

/deep/ .indicator-editor-modal--dark .indicator-editor-color {
  background: #141414;
  border-color: #434343;
}

/* 图表内容区域 */
.chart-content-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
  position: relative;
}

.qd-wm-layer {
  position: absolute !important;
  left: 0 !important;
  top: 0 !important;
  width: 100% !important;
  height: 100% !important;
  z-index: 8 !important;
  pointer-events: none !important;
  display: block !important;
  opacity: 1 !important;
  visibility: visible !important;
}

.chart-left.theme-dark .indicator-toolbar {
  background: #141414;
  border-bottom-color: #2a2a2a;
}

.indicator-btn {
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 600;
  color: #666;
  background: #f0f2f5;
  border: 1px solid #e8e8e8;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
  min-width: 40px;
  text-align: center;
  user-select: none;
}

.chart-left.theme-dark .indicator-btn {
  color: #d1d4dc;
  background: #252525;
  border-color: #2a2a2a;
}

.indicator-btn:hover {
  color: #1890ff;
  border-color: #1890ff;
  background: #f0f8ff;
}

.chart-left.theme-dark .indicator-btn:hover {
  color: #13c2c2;
  border-color: #13c2c2;
  background: #252525;
}

.indicator-btn.active {
  color: #1890ff;
  background: #fff;
  border-color: #1890ff;
  border-width: 2px;
  box-shadow: 0 0 0 2px rgba(24, 144, 255, 0.1);
}

.chart-left.theme-dark .indicator-btn.active {
  color: #13c2c2;
  background: #252525;
  border-color: #13c2c2;
  box-shadow: 0 0 0 2px rgba(19, 194, 194, 0.2);
}

.kline-chart-container {
  flex: 1;
  width: 100%;
  min-width: 0; /* 防止 flex 子元素溢出 */
  background: #fff;
  transition: background-color 0.3s;
  touch-action: pan-x pan-y;
  -webkit-overflow-scrolling: touch;
  overflow: hidden;

  .theme-dark & {
    background: #141414;
  }
}

/* 百分比 Y 轴叠加层 */
.kline-chart-with-pct {
  flex: 1;
  position: relative;
  min-width: 0;
  overflow: hidden;
}

/* 筹码显示时，为右侧筹码窗口预留 140px，使蜡烛图及其Y轴保持在筹码窗口左侧 */
.kline-chart-with-pct--chip {
  padding-right: 140px;
}

/* 左侧百分比坐标轴：padding-left 预留 64px + 绝对定位覆盖层（回退方案，位置在左侧）。
   观感对齐右侧金额轴：透明底、无分隔线，仅刻度短线与文字（样式运行时取自 yAxis 配置） */
.kline-chart-with-pct {
  padding-left: 64px;
}

.pct-axis-overlay {
  position: absolute;
  left: 0;
  top: 0;
  width: 64px;
  height: 100%;
  z-index: 5;
  pointer-events: none;
  overflow: hidden;
}

.pct-axis-overlay__canvas {
  position: absolute;
  left: 0;
  top: 0;
  width: 100%;
  height: 100%;
  display: block;
}

/* 筹码分布覆盖层（绝对定位在K线图右侧、Y轴右边） */
.chip-overlay {
  position: absolute;
  right: 0;
  top: 0;
  width: 140px;
  height: 100%;
  background: rgba(250, 250, 250, 0.92);
  border-left: 1px solid #e8e8e8;
  z-index: 10;
  pointer-events: none;
}

.chip-overlay--dark {
  background: rgba(26, 26, 26, 0.92);
  border-left-color: #303030;
}

/* 标题栏绝对定位悬浮在 canvas 之上，不占用布局高度，
   保证 canvas 高度 = 主图高度，筹码价格刻度与蜡烛图完全对齐 */
.chip-overlay__header {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 6px;
  font-size: 10px;
  color: #888;
  border-bottom: 1px solid #e8e8e8;
  z-index: 1;
}

.chip-overlay--dark .chip-overlay__header {
  color: #999;
  border-bottom-color: #303030;
}

.chip-overlay__title {
  font-weight: 500;
}

.chip-overlay__avg {
  font-size: 9px;
  color: #fa8c16;
  font-weight: 500;
}

.chip-overlay--dark .chip-overlay__avg {
  color: #faad14;
}

.chip-overlay__canvas {
  width: 100%;
  height: 100%;
  display: block;
}

.chart-overlay {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(255, 255, 255, 0.95);
  display: flex;
  justify-content: center;
  align-items: center;
  z-index: 1;
  backdrop-filter: blur(2px);
}

.chart-left.theme-dark .chart-overlay {
  background: rgba(20, 20, 20, 0.95);
}

.error-box {
  display: flex;
  flex-direction: column;
  align-items: center;
  color: #333;
}

.initial-hint {
  background: rgba(255, 255, 255, 0.98);
}

.chart-left.theme-dark .initial-hint {
  background: rgba(20, 20, 20, 0.98);
}

.hint-box {
  text-align: center;
  color: #666;
  display: flex;
  flex-direction: column;
  align-items: center;
  max-width: 400px;
  padding: 20px;
}

.pyodide-warning {
  background: rgba(255, 255, 255, 0.98);
}

.chart-left.theme-dark .pyodide-warning {
  background: rgba(20, 20, 20, 0.98);
}

.warning-box {
  text-align: center;
  color: #666;
  display: flex;
  flex-direction: column;
  align-items: center;
  max-width: 500px;
  padding: 20px;
}

.warning-title {
  font-size: 16px;
  font-weight: 600;
  color: #faad14;
  margin-bottom: 8px;
}

.warning-desc {
  font-size: 14px;
  color: #666;
  line-height: 1.6;
}

.chart-left.theme-dark .warning-box {
  color: #d1d4dc;
}

.chart-left.theme-dark .warning-title {
  color: #faad14;
}

.chart-left.theme-dark .warning-desc {
  color: #868993;
}

.chart-left.theme-dark .hint-box {
  color: #d1d4dc;
}

.hint-title {
  font-size: 18px;
  font-weight: 600;
  color: #333;
  margin-bottom: 12px;
}

.chart-left.theme-dark .hint-title {
  color: #d1d4dc;
}

.hint-desc {
  font-size: 14px;
  color: #999;
  line-height: 1.6;
}

.chart-left.theme-dark .hint-desc {
  color: #787b86;
}

/* 历史数据加载提示 */
.history-loading-hint {
  position: absolute;
  left: 20px;
  top: 60px;
  z-index: 1000 !important;
  display: flex !important;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  background: rgba(255, 255, 255, 0.98) !important;
  border: 1px solid #e8e8e8;
  border-radius: 4px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
  font-size: 14px;
  color: #666 !important;
  backdrop-filter: blur(4px);
  pointer-events: none;
  visibility: visible !important;
  opacity: 1 !important;
}

.chart-left.theme-dark .history-loading-hint {
  background: rgba(20, 20, 20, 0.98) !important;
  border-color: #2a2a2a;
  color: #d1d4dc !important;
}

.loading-text {
  white-space: nowrap;
  margin-left: 4px;
}

/* 移动端适配 */
@media (max-width: 768px) {
  .drawing-toolbar {
    display: none; /* 移动端隐藏画线工具栏 */
  }

  .indicator-toolbar {
    padding-left: 12px; /* 移动端恢复原始padding */
    flex-wrap: nowrap; /* 手机端不换行，只显示一行 */
    overflow-x: auto; /* 允许横向滚动 */
    overflow-y: hidden; /* 禁止纵向滚动 */
    scrollbar-width: none; /* Firefox 隐藏滚动条 */
    -ms-overflow-style: none; /* IE 10+ 隐藏滚动条 */
    -webkit-overflow-scrolling: touch; /* iOS 平滑滚动 */
  }

  .indicator-toolbar::-webkit-scrollbar {
    display: none; /* Chrome Safari 隐藏滚动条 */
    width: 0;
    height: 0;
  }

  .indicator-btn {
    flex-shrink: 0; /* 按钮不收缩，保持原始大小 */
  }
}

@media (max-width: 1200px) {
  .drawing-toolbar {
    display: none; /* 移动端隐藏画线工具栏 */
  }

  .indicator-toolbar {
    padding-left: 12px; /* 移动端恢复原始padding */
  }

  .kline-chart-container {
    margin-left: 0; /* 移动端恢复原始margin */
  }

  .chart-left {
    width: 100% !important;
    min-width: 100% !important;
    border-right: none;
    border-bottom: 1px solid #e8e8e8;
    height: 600px !important;
    min-height: 600px !important;
  }

  .chart-wrapper {
    height: 100% !important;
    min-height: 600px !important;
  }

  .kline-chart-container {
    height: 100% !important;
    min-height: 600px !important;
  }
}

@media (max-width: 992px) {
  .chart-left {
    height: 650px !important;
    min-height: 650px !important;
  }

  .chart-wrapper {
    height: 100% !important;
    min-height: 650px !important;
  }

  .kline-chart-container {
    height: 100% !important;
    min-height: 650px !important;
  }
}

@media (max-width: 768px) {
  .chart-left {
    height: 60vh !important;
    min-height: 400px !important;
    max-height: 80vh !important;
  }

  .chart-wrapper {
    height: 100% !important;
    min-height: 400px !important;
    max-height: 100% !important;
  }

  .kline-chart-container {
    height: calc(100% - 45px) !important; /* 减去工具栏高度 */
    min-height: 350px !important;
    max-height: calc(100% - 45px) !important;
  }
}

@media (max-width: 576px) {
  .chart-left {
    height: 55vh !important;
    min-height: 350px !important;
    max-height: 75vh !important;
  }

  .chart-wrapper {
    height: 100% !important;
    min-height: 350px !important;
    max-height: 100% !important;
  }

  .kline-chart-container {
    height: calc(100% - 45px) !important; /* 减去工具栏高度 */
    min-height: 300px !important;
    max-height: calc(100% - 45px) !important;
  }
}
</style>
