<template>
  <div ref="chartContainer" class="klinecharts-pro-wrapper"></div>
</template>

<script>
import request from '@/utils/request'
import * as klinecharts from 'klinecharts'

// 暴露到全局，Pro UMD 通过 window.klinecharts 使用同一个实例
if (typeof window !== 'undefined') {
  window.klinecharts = klinecharts
}

/**
 * KLineChartPro Vue 包装组件
 *
 * 核心思路：
 *   1. 在 Pro 加载前，通过 klinecharts.registerIndicator() 注册自定义指标
 *   2. Pro 创建时，将自定义指标名加入 mainIndicators / subIndicators
 *   3. Pro 内部调 _.createIndicator() 时，自动找到已注册的自定义指标
 *   4. Python 异步计算完成后，通过 overrideIndicator 更新 extendData 触发重绘
 */
export default {
  name: 'KlineChartProWrapper',
  props: {
    symbol: { type: String, default: '' },
    market: { type: String, default: '' },
    timeframe: { type: String, default: '1D' },
    theme: { type: String, default: 'light' },
    locale: { type: String, default: 'zh-CN' },
    userId: { type: [String, Number], default: null }
  },
  data () {
    return {
      chartPro: null,
      innerChart: null,
      realtimeTimer: null,
      proReady: false,
      // 已注册的自定义指标名（全局 klinecharts 实例）
      registeredCustomIndicators: [],
      // 当前 main/sub 指标列表（含内置 + 自定义）
      currentMainIndicators: ['MA'],
      currentSubIndicators: ['VOL'],
      // Pyodide
      pyodide: null,
      pythonReady: false,
      loadingPython: false,
      pyodideLoadFailed: false,
      // 缓存 K 线数据
      cachedKlineData: []
    }
  },
  computed: {
    periodObj () {
      const map = {
        '1m': { multiplier: 1, timespan: 'minute', text: '1m' },
        '5m': { multiplier: 5, timespan: 'minute', text: '5m' },
        '15m': { multiplier: 15, timespan: 'minute', text: '15m' },
        '30m': { multiplier: 30, timespan: 'minute', text: '30m' },
        '1H': { multiplier: 1, timespan: 'hour', text: '1H' },
        '4H': { multiplier: 4, timespan: 'hour', text: '4H' },
        '1D': { multiplier: 1, timespan: 'day', text: 'D' },
        '1W': { multiplier: 1, timespan: 'week', text: 'W' },
        '1M': { multiplier: 1, timespan: 'month', text: 'M' }
      }
      return map[this.timeframe] || map['1D']
    },
    symbolObj () {
      return { ticker: this.symbol, name: this.symbol, shortName: this.symbol }
    }
  },
  watch: {
    symbol (val) {
      if (this.chartPro && val) this.chartPro.setSymbol(this.symbolObj)
    },
    timeframe () {
      if (this.chartPro) this.chartPro.setPeriod(this.periodObj)
    },
    theme (val) {
      if (this.chartPro) this.chartPro.setTheme(val)
    }
  },
  mounted () {
    this.loadProScript()
    this.loadPyodide()
  },
  beforeDestroy () {
    this.clearRealtimeTimer()
  },
  methods: {
    // ========== Pro 加载 ==========
    loadProScript () {
      if (window.klinechartspro && window.klinechartspro.KLineChartPro) {
        this.proReady = true
        this.initChart()
        return
      }
      if (!document.querySelector('link[href*="klinecharts-pro.css"]')) {
        const link = document.createElement('link')
        link.rel = 'stylesheet'
        link.href = process.env.BASE_URL + 'static/klinecharts-pro.css'
        document.head.appendChild(link)
      }
      const script = document.createElement('script')
      script.src = process.env.BASE_URL + 'static/klinecharts-pro.umd.js'
      script.onload = () => {
        this.proReady = true
        this.initChart()
      }
      script.onerror = (e) => {
        console.error('Failed to load klinecharts-pro.umd.js:', e)
      }
      document.body.appendChild(script)
    },

    // ========== Pro 初始化 ==========
    initChart () {
      if (!this.$refs.chartContainer || !this.proReady) return
      const KLineChartPro = window.klinechartspro && window.klinechartspro.KLineChartPro
      if (!KLineChartPro) { console.error('klinecharts-pro not loaded'); return }
      try {
        const isDark = this.theme === 'dark'
        this.chartPro = new KLineChartPro({
          container: this.$refs.chartContainer,
          theme: this.theme, locale: this.locale,
          symbol: this.symbolObj, period: this.periodObj,
          drawingBarVisible: true,
          // 【关键】将已注册的自定义指标名加入列表
          mainIndicators: this.currentMainIndicators,
          subIndicators: this.currentSubIndicators,
          datafeed: this.createDatafeed(),
          styles: {
            candle: { bar: {
              upColor: isDark ? '#ef5350' : '#f5222d', downColor: isDark ? '#0ecb81' : '#52c41a',
              noChangeColor: '#888888',
              upBorderColor: isDark ? '#ef5350' : '#f5222d', downBorderColor: isDark ? '#0ecb81' : '#52c41a', noChangeBorderColor: '#888888',
              upWickColor: isDark ? '#ef5350' : '#f5222d', downWickColor: isDark ? '#0ecb81' : '#52c41a', noChangeWickColor: '#888888'
            } },
            indicator: { bars: [{ style: 'fill',
              upColor: isDark ? '#ef5350' : '#f5222d', downColor: isDark ? '#0ecb81' : '#52c41a', noChangeColor: '#888888'
            }] }
          }
        })
        // 捕获 Pro 内部 chart（用于 overlay 等操作）
        this._captureInnerChart()
      } catch (e) { console.error('KLineChartPro init error:', e) }
    },

    /** 尝试捕获 Pro 内部 chart 实例 */
    _captureInnerChart () {
      let retries = 0
      const maxRetries = 40
      const timer = setInterval(() => {
        retries++
        if (!this.innerChart && this.chartPro) {
          const api = this.chartPro._chartApi
          if (api && typeof api === 'object') {
            if (typeof api.createIndicator === 'function') {
              this.innerChart = api
            } else if (api.value && typeof api.value.createIndicator === 'function') {
              this.innerChart = api.value
            }
          }
        }
        if (this.innerChart || retries >= maxRetries) {
          clearInterval(timer)
          if (this.innerChart) {
            console.log('[KlineChartProWrapper] innerChart captured after', retries, 'retries')
          }
        }
      }, 250)
    },

    createDatafeed () {
      const self = this
      return {
        async searchSymbols (query) {
          try {
            const res = await request({
              url: '/api/market/search', method: 'get',
              params: { keyword: query, market: self.market }
            })
            if (res.code === 1 && Array.isArray(res.data)) {
              return res.data.map(item => ({
                ticker: item.symbol || item.ticker, name: item.name || item.symbol,
                shortName: item.symbol || item.ticker,
                exchange: item.exchange || '', market: item.market || self.market
              }))
            }
            return []
          } catch (e) { return [] }
        },
        async getHistoryKLineData (symbol, period, from, to) {
          try {
            const tfMap = {
              minute: period.multiplier === 1 ? '1m' : period.multiplier === 5 ? '5m' : period.multiplier === 15 ? '15m' : '30m',
              hour: period.multiplier === 1 ? '1H' : '4H',
              day: '1D', week: '1W', month: '1M'
            }
            const timeframe = tfMap[period.timespan] || '1D'
            const res = await request({
              url: '/api/indicator/kline', method: 'get',
              params: { market: self.market, symbol: symbol.ticker || symbol.shortName || symbol.name, timeframe, limit: 1000 }
            })
            if (res.code === 1 && Array.isArray(res.data)) {
              const data = res.data.map(item => ({
                timestamp: (item.time || item.timestamp) * (item.time < 1e10 ? 1000 : 1),
                open: parseFloat(item.open), high: parseFloat(item.high),
                low: parseFloat(item.low), close: parseFloat(item.close),
                volume: parseFloat(item.volume || 0)
              }))
              self.cachedKlineData = data
              return data
            }
            return []
          } catch (e) { return [] }
        },
        subscribe (symbol, period, callback) {
          self.clearRealtimeTimer()
          self.realtimeTimer = setInterval(async () => {
            try {
              const tfMap = {
                minute: period.multiplier === 1 ? '1m' : period.multiplier === 5 ? '5m' : period.multiplier === 15 ? '15m' : '30m',
                hour: period.multiplier === 1 ? '1H' : '4H',
                day: '1D', week: '1W', month: '1M'
              }
              const timeframe = tfMap[period.timespan] || '1D'
              const res = await request({
                url: '/api/indicator/kline', method: 'get',
                params: { market: self.market, symbol: symbol.ticker || symbol.shortName || symbol.name, timeframe, limit: 2 }
              })
              if (res.code === 1 && Array.isArray(res.data) && res.data.length > 0) {
                const latest = res.data[res.data.length - 1]
                const klineData = {
                  timestamp: (latest.time || latest.timestamp) * (latest.time < 1e10 ? 1000 : 1),
                  open: parseFloat(latest.open), high: parseFloat(latest.high),
                  low: parseFloat(latest.low), close: parseFloat(latest.close),
                  volume: parseFloat(latest.volume || 0)
                }
                callback(klineData)
              }
            } catch (e) { /* ignore */ }
          }, 5000)
        },
        unsubscribe () { self.clearRealtimeTimer() }
      }
    },

    clearRealtimeTimer () {
      if (this.realtimeTimer) { clearInterval(this.realtimeTimer); this.realtimeTimer = null }
    },

    // ========== Pyodide ==========
    loadPyodide () {
      return new Promise((resolve, reject) => {
        if (window.pyodide) {
          this.pyodide = window.pyodide
          this.pyodide.loadPackage(['pandas', 'numpy']).then(() => {
            this.pythonReady = true; resolve(window.pyodide)
          }).catch(() => {
            this.pythonReady = true; resolve(window.pyodide)
          })
          return
        }
        this.loadingPython = true
        const PYODIDE_VERSION = '0.25.0'
        const cdnBase = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`
        const loadScript = (src) => new Promise((resolve, reject) => {
          const existing = document.querySelector(`script[data-pyodide-src="${src}"]`)
          if (existing) {
            if (typeof window.loadPyodide === 'function') return resolve()
            existing.addEventListener('load', () => resolve(), { once: true })
            existing.addEventListener('error', () => reject(new Error('Pyodide 脚本加载失败')), { once: true })
            return
          }
          const s = document.createElement('script')
          s.dataset.pyodideSrc = src; s.src = src
          s.onload = () => resolve()
          s.onerror = () => reject(new Error('Pyodide 脚本加载失败'))
          document.head.appendChild(s)
        })
        const initPyodide = async () => {
          try {
            await loadScript(cdnBase + 'pyodide.js')
            const pyodideInstance = await window.loadPyodide({ indexURL: cdnBase })
            try { await pyodideInstance.loadPackage(['pandas', 'numpy']) }
            catch (e) { await pyodideInstance.runPythonAsync('import micropip; await micropip.install(["pandas", "numpy"])') }
            window.pyodide = pyodideInstance
            this.pyodide = pyodideInstance; this.pythonReady = true; this.loadingPython = false
            resolve(pyodideInstance)
          } catch (err) {
            this.loadingPython = false; this.pyodideLoadFailed = true; reject(err)
          }
        }
        initPyodide()
      })
    },

    // ========== Python 指标执行 ==========
    async executePythonStrategy (userCode, klineData, params, indicatorInfo) {
      if (!this.pythonReady || !this.pyodide) {
        if (this.loadingPython) {
          let waitCount = 0
          while (this.loadingPython && waitCount < 30) {
            await new Promise(resolve => setTimeout(resolve, 500)); waitCount++
            if (this.pythonReady && this.pyodide) break
          }
        }
        if (!this.pythonReady || !this.pyodide) {
          if (!this.loadingPython) this.pyodideLoadFailed = true
          throw new Error('Python 引擎未就绪，请等待加载完成')
        }
      }
      try {
        const rawData = klineData.map(item => {
          let timeValue = item.timestamp || item.time
          if (timeValue < 1e10) timeValue = timeValue * 1000
          return {
            time: Math.floor(timeValue / 1000),
            open: parseFloat(item.open) || 0, high: parseFloat(item.high) || 0,
            low: parseFloat(item.low) || 0, close: parseFloat(item.close) || 0,
            volume: parseFloat(item.volume) || 0
          }
        })
        const rawDataJson = JSON.stringify(rawData)
        const paramsJson = JSON.stringify(params || {})
        const escapedJson = rawDataJson.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r')
        const escapedParams = paramsJson.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r')
        const pythonCode = `
import json
import pandas as pd
import numpy as np

def clean_nan(obj):
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan(item) for item in obj]
    elif isinstance(obj, (pd.Series, np.ndarray)):
        return [None if (isinstance(x, float) and (np.isnan(x) or np.isinf(x))) else x for x in obj]
    elif isinstance(obj, (float, np.floating)):
        if np.isnan(obj) or np.isinf(obj): return None
        return float(obj)
    elif pd.isna(obj): return None
    else: return obj

raw_data = json.loads('${escapedJson}')
params = json.loads('${escapedParams}')

def _get_param(key, default=None):
    if key in params: return params.get(key, default)
    camel = ''.join([key.split('_')[0]] + [p.capitalize() for p in key.split('_')[1:]])
    return params.get(camel, default)

try: leverage = float(_get_param('leverage', 1) or 1)
except: leverage = 1
trade_direction = _get_param('trade_direction', _get_param('tradeDirection', 'both')) or 'both'

df = pd.DataFrame(raw_data)
for col in ['open','high','low','close','volume']: df[col] = df[col].astype(float)

${userCode}

if 'output' not in locals():
    if 'result_json' in locals(): output = json.loads(result_json)
    else: output = {"plots": []}
else:
    if isinstance(output, str): output = json.loads(output)

output = clean_nan(output)
json.dumps(output)
`
        const resultJson = await this.pyodide.runPythonAsync(pythonCode)
        if (!resultJson || typeof resultJson !== 'string') throw new Error(`Python 未返回 JSON，类型: ${typeof resultJson}`)
        let result
        try { result = JSON.parse(resultJson) } catch (e) { throw new Error(`JSON 解析失败: ${e.message}`) }
        if (!result) return { plots: [], signals: [], calculatedVars: {} }
        if (!result.plots || !Array.isArray(result.plots)) result.plots = []
        result.plots = result.plots.map(p => {
          if (p.data && Array.isArray(p.data)) p.data = p.data.map(v => (v === null || v === undefined || (typeof v === 'number' && isNaN(v))) ? null : v)
          return p
        })
        if (result.signals && Array.isArray(result.signals)) {
          result.signals = result.signals.map(s => {
            if (s.data && Array.isArray(s.data)) s.data = s.data.map(v => (v === null || v === undefined || (typeof v === 'number' && isNaN(v))) ? null : v)
            return s
          })
        }
        if (!result.calculatedVars) result.calculatedVars = {}
        return result
      } catch (err) { throw new Error(`Python 执行失败: ${err.message}`) }
    },

    // ========== 指标注册（全局 klinecharts 实例）==========
    /**
     * 注册自定义指标到全局 klinecharts。
     * 注册后 Pro 通过 mainIndicators/subIndicators 配置即可自动创建。
     */
    registerCustomIndicator (name, calcFunc, figures, calcParams, precision, shouldOverlay) {
      if (precision < 0) precision = 2
      try {
        klinecharts.registerIndicator({
          name, shortName: name,
          calc: calcFunc, figures,
          calcParams: calcParams || [], precision,
          series: shouldOverlay ? 'price' : 'normal'
        })
        if (!this.registeredCustomIndicators.includes(name)) {
          this.registeredCustomIndicators.push(name)
        }
        return true
      } catch (err) {
        if (err.message && err.message.includes('already registered')) return true
        console.error('[KlineChartProWrapper] registerCustomIndicator error:', err)
        return false
      }
    },

    /**
     * 将已注册的自定义指标添加到 Pro 图表。
     * 两种方式：
     *   1. 重建 Pro（更新 mainIndicators/subIndicators）— 可靠但会闪烁
     *   2. 直接调 innerChart.createIndicator — 需要 innerChart
     */
    injectIndicatorToPro (name, isMainPane, options) {
      if (this.innerChart) {
        try {
          const paneId = isMainPane ? 'candle_pane' : undefined
          // 先移除同名旧指标（klinecharts 按 name 移除）
          try { this.innerChart.removeIndicator(paneId || 'candle_pane', name) } catch (e) { /* ignore */ }
          const result = this.innerChart.createIndicator(
            { name, ...options },
            isMainPane,
            paneId ? { id: paneId } : undefined
          )
          return result
        } catch (e) {
          console.warn('[KlineChartProWrapper] innerChart.createIndicator failed, rebuilding Pro:', e)
        }
      }
      // 方式 2：更新指标列表并重建 Pro
      this._rebuildProWithIndicator(name, isMainPane)
      return null
    },

    /** 将指标加入列表并重建 Pro */
    _rebuildProWithIndicator (name, isMainPane) {
      const list = isMainPane ? this.currentMainIndicators : this.currentSubIndicators
      if (!list.includes(name)) list.push(name)
      this._rebuildPro()
    },

    /** 重建 Pro（销毁旧实例，用当前指标列表重新创建） */
    _rebuildPro () {
      if (this.chartPro) {
        try {
          const container = this.$refs.chartContainer
          if (container) container.innerHTML = ''
        } catch (e) { /* ignore */ }
      }
      this.innerChart = null
      this.initChart()
    },

    /** 从 Pro 移除自定义指标 */
    removeIndicatorFromPro (paneId, name, noRebuild) {
      this.currentMainIndicators = this.currentMainIndicators.filter(n => n !== name)
      this.currentSubIndicators = this.currentSubIndicators.filter(n => n !== name)
      // 尝试从 chart 上直接移除
      if (this.innerChart) {
        try { this.innerChart.removeIndicator(paneId, name) } catch (e) { /* ignore */ }
      }
      if (noRebuild) return
      this._rebuildPro()
    },

    updateIndicatorData (name, precomputed) {
      if (!this.innerChart) return
      try {
        this.innerChart.overrideIndicator({ name, extendData: { precomputed } })
      } catch (e) {
        console.error('[KlineChartProWrapper] updateIndicatorData error:', e)
      }
    },

    // ========== Overlay ==========
    createOverlay (overlayConfig) {
      if (!this.innerChart) return null
      try { return this.innerChart.createOverlay(overlayConfig) } catch (e) { return null }
    },
    removeOverlay (id) {
      if (!this.innerChart) return
      try { this.innerChart.removeOverlay(id) } catch (e) { /* ignore */ }
    },
    getOverlays () {
      if (!this.innerChart) return []
      try { return this.innerChart.getOverlays ? this.innerChart.getOverlays() : [] } catch (e) { return [] }
    },
    getDataList () {
      if (!this.innerChart) return this.cachedKlineData || []
      try { return this.innerChart.getDataList ? this.innerChart.getDataList() : [] } catch (e) { return [] }
    },

    // ========== 兼容旧接口 ==========
    getChart () { return this.innerChart },
    updateIndicators () { /* 由外部通过 injectIndicatorToPro() 操作 */ },
    resize () { /* Pro 自动管理 */ }
  }
}
</script>

<style scoped>
.klinecharts-pro-wrapper {
  width: 100%;
  height: 100%;
  min-height: 400px;
}
</style>
