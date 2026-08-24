<template>
  <div ref="chartContainer" class="klinecharts-pro-wrapper"></div>
</template>

<script>
import request from '@/utils/request'
import * as klinecharts from 'klinecharts'

// 设置全局变量供 Pro UMD 使用
if (typeof window !== 'undefined') {
  window.klinecharts = klinecharts
}

/**
 * KLineChartPro Vue 包装组件
 *
 * 架构：
 *   - Pro 实例：管理内置指标（MA, VOL 等）
 *   - 自定义 Python 指标通过 registerIndicator 全局注册后注入 Pro mainIndicators
 *   - Pro 内部调 createIndicator 时自动使用已注册的自定义指标
 *
 * 外置指标流程：
 *   1. registerCustomIndicator() 全局注册指标（Pro 加载前或后均可）
 *   2. injectIndicatorToPro() 将指标注入 Pro 的 mainIndicators 并创建
 *   3. updateIndicatorData() Python 计算完成后更新指标数据
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
      // Pyodide
      pyodide: null,
      pythonReady: false,
      loadingPython: false,
      pyodideLoadFailed: false,
      // 已注册的自定义指标名
      registeredCustomIndicators: [],
      // 已注入 Pro 的指标名
      injectedIndicators: []
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
        '2H': { multiplier: 2, timespan: 'hour', text: '2H' },
        '4H': { multiplier: 4, timespan: 'hour', text: '4H' },
        '1D': { multiplier: 1, timespan: 'day', text: 'D' },
        '1W': { multiplier: 1, timespan: 'week', text: 'W' }
      }
      return map[this.timeframe] || map['1D']
    },
    symbolObj () {
      return {
        ticker: this.symbol,
        name: this.symbol,
        shortName: this.symbol
      }
    }
  },
  watch: {
    symbol (val) {
      if (this.chartPro && val) {
        this.chartPro.setSymbol(this.symbolObj)
      }
    },
    timeframe () {
      if (this.chartPro) {
        this.chartPro.setPeriod(this.periodObj)
      }
    },
    theme (val) {
      if (this.chartPro) {
        this.chartPro.setTheme(val)
      }
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
    // ========== Pyodide 加载 ==========
    loadPyodide () {
      return new Promise((resolve, reject) => {
        if (window.pyodide) {
          this.pyodide = window.pyodide
          this.pyodide.loadPackage(['pandas', 'numpy']).then(() => {
            this.pythonReady = true
            resolve(window.pyodide)
          }).catch(() => {
            this.pythonReady = true
            resolve(window.pyodide)
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
          s.dataset.pyodideSrc = src
          s.src = src
          s.onload = () => resolve()
          s.onerror = () => reject(new Error('Pyodide 脚本加载失败'))
          document.head.appendChild(s)
        })
        const initPyodide = async () => {
          try {
            await loadScript(cdnBase + 'pyodide.js')
            const pyodideInstance = await window.loadPyodide({ indexURL: cdnBase })
            try {
              await pyodideInstance.loadPackage(['pandas', 'numpy'])
            } catch (e) {
              await pyodideInstance.runPythonAsync('import micropip; await micropip.install(["pandas", "numpy"])')
            }
            window.pyodide = pyodideInstance
            this.pyodide = pyodideInstance
            this.pythonReady = true
            this.loadingPython = false
            resolve(pyodideInstance)
          } catch (err) {
            this.loadingPython = false
            this.pyodideLoadFailed = true
            reject(err)
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
            await new Promise(resolve => setTimeout(resolve, 500))
            waitCount++
            if (this.pythonReady && this.pyodide) break
          }
        }
        if (!this.pythonReady || !this.pyodide) {
          if (!this.loadingPython) this.pyodideLoadFailed = true
          throw new Error('Python 引擎未就绪，请等待加载完成')
        }
      }
      try {
        const finalCode = userCode
        const rawData = klineData.map(item => {
          let timeValue = item.timestamp || item.time
          if (timeValue < 1e10) timeValue = timeValue * 1000
          return {
            time: Math.floor(timeValue / 1000),
            open: parseFloat(item.open) || 0,
            high: parseFloat(item.high) || 0,
            low: parseFloat(item.low) || 0,
            close: parseFloat(item.close) || 0,
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
        if np.isnan(obj) or np.isinf(obj):
            return None
        return float(obj)
    elif pd.isna(obj):
        return None
    else:
        return obj

raw_data = json.loads('${escapedJson}')
params = json.loads('${escapedParams}')

def _get_param(key, default=None):
    if key in params:
        return params.get(key, default)
    camel = ''.join([key.split('_')[0]] + [p.capitalize() for p in key.split('_')[1:]])
    return params.get(camel, default)

try:
    leverage = float(_get_param('leverage', 1) or 1)
except Exception:
    leverage = 1

trade_direction = _get_param('trade_direction', _get_param('tradeDirection', 'both')) or 'both'

df = pd.DataFrame(raw_data)
df['open'] = df['open'].astype(float)
df['high'] = df['high'].astype(float)
df['low'] = df['low'].astype(float)
df['close'] = df['close'].astype(float)
df['volume'] = df['volume'].astype(float)

${finalCode}

if 'output' not in locals():
    if 'result_json' in locals():
        output = json.loads(result_json)
    else:
        output = {"plots": []}
else:
    if isinstance(output, str):
        output = json.loads(output)

output = clean_nan(output)
json.dumps(output)
`
        const resultJson = await this.pyodide.runPythonAsync(pythonCode)
        if (!resultJson || typeof resultJson !== 'string') {
          throw new Error(`Python 代码执行后未返回有效的 JSON 字符串，返回类型: ${typeof resultJson}`)
        }
        let result
        try {
          result = JSON.parse(resultJson)
        } catch (parseError) {
          throw new Error(`JSON 解析失败: ${parseError.message}`)
        }
        if (!result) return { plots: [], signals: [], calculatedVars: {} }
        if (!result.plots || !Array.isArray(result.plots)) result.plots = []
        result.plots = result.plots.map(plot => {
          if (plot.data && Array.isArray(plot.data)) {
            plot.data = plot.data.map(val => {
              if (val === null || val === undefined || (typeof val === 'number' && isNaN(val))) return null
              return val
            })
          }
          return plot
        })
        if (result.signals && Array.isArray(result.signals)) {
          result.signals = result.signals.map(signal => {
            if (signal.data && Array.isArray(signal.data)) {
              signal.data = signal.data.map(val => {
                if (val === null || val === undefined || (typeof val === 'number' && isNaN(val))) return null
                return val
              })
            }
            return signal
          })
        }
        if (!result.calculatedVars) result.calculatedVars = {}
        return result
      } catch (err) {
        throw new Error(`Python 执行失败: ${err.message}`)
      }
    },

    // ========== 指标注册与注入 Pro ==========
    /**
     * 注册自定义指标到 klinecharts 全局。
     * 注册后可通过 injectIndicatorToPro() 将其添加到 Pro 主图。
     *
     * 对于 Python 异步指标，calc 函数应从 indicator.extendData 读取预计算结果：
     *   calc: (dataList, indicator) => indicator.extendData?.precomputed || []
     */
    registerCustomIndicator (name, calcFunc, figures, calcParams, precision, shouldOverlay) {
      if (precision < 0) precision = 2
      try {
        const indicatorConfig = {
          name,
          shortName: name,
          calc: calcFunc,
          figures,
          calcParams: calcParams || [],
          precision,
          series: shouldOverlay ? 'price' : 'normal'
        }
        klinecharts.registerIndicator(indicatorConfig)
        if (!this.registeredCustomIndicators.includes(name)) {
          this.registeredCustomIndicators.push(name)
        }
        return true
      } catch (err) {
        if (err.message && err.message.includes('already registered')) return true
        return false
      }
    },

    /**
     * 将已注册的自定义指标注入 Pro 主图。
     * 调用 Pro 的 _chartApi.createIndicator（内部 chart 实例）。
     */
    injectIndicatorToPro (name, isMainPane, options = {}) {
      if (!this.innerChart) {
        console.warn('[KlineChartProWrapper] No inner chart. Cannot inject indicator.')
        return null
      }
      try {
        const paneId = isMainPane ? 'candle_pane' : undefined
        const result = this.innerChart.createIndicator(
          { name, ...options },
          isMainPane,
          paneId ? { id: paneId } : undefined
        )
        if (!this.injectedIndicators.includes(name)) {
          this.injectedIndicators.push(name)
        }
        return result
      } catch (e) {
        console.error('[KlineChartProWrapper] injectIndicatorToPro error:', e)
        return null
      }
    },

    /**
     * 更新已注入 Pro 的指标数据（Python 计算完成后调用）
     */
    updateIndicatorData (name, precomputed) {
      if (!this.innerChart) return
      try {
        this.innerChart.overrideIndicator({
          name,
          extendData: { precomputed }
        })
      } catch (e) {
        console.error('[KlineChartProWrapper] updateIndicatorData error:', e)
      }
    },

    /**
     * 从 Pro 移除已注入的指标
     */
    removeIndicatorFromPro (paneId, name) {
      if (!this.innerChart) return
      try {
        this.innerChart.removeIndicator(paneId, name)
        this.injectedIndicators = this.injectedIndicators.filter(n => n !== name)
      } catch (e) { /* ignore */ }
    },

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
      // 【关键】在 Pro 加载前拦截 klinecharts.init
      // Pro 内部通过 `_` (klinecharts) 调用 `_.init()` 创建 chart
      // 我们包装 init，捕获 Pro 内部创建的 chart 实例
      const self = this
      const originalInit = klinecharts.init
      let captured = false
      klinecharts.init = function (...args) {
        console.log('[KlineChartProWrapper] klinecharts.init intercepted!')
        const chart = originalInit.apply(this, args)
        if (chart) {
          self.innerChart = chart
          captured = true
          console.log('[KlineChartProWrapper] ✓ Captured chart instance')
        }
        return chart
      }
      const script = document.createElement('script')
      script.src = process.env.BASE_URL + 'static/klinecharts-pro.umd.js'
      script.onload = () => {
        // 恢复原始 init
        klinecharts.init = originalInit
        if (!captured) {
          console.warn('[KlineChartProWrapper] ✗ init NOT intercepted during Pro load')
        }
        this.proReady = true
        this.initChart()
      }
      script.onerror = (e) => {
        klinecharts.init = originalInit
        console.error('Failed to load klinecharts-pro.umd.js:', e)
      }
      document.body.appendChild(script)
    },

    createDatafeed () {
      const self = this
      return {
        async searchSymbols (query) {
          try {
            const res = await request({
              url: '/api/market/search',
              method: 'get',
              params: { keyword: query, market: self.market }
            })
            if (res.code === 1 && Array.isArray(res.data)) {
              return res.data.map(item => ({
                ticker: item.symbol || item.ticker,
                name: item.name || item.symbol,
                shortName: item.symbol || item.ticker,
                exchange: item.exchange || '',
                market: item.market || self.market
              }))
            }
            return []
          } catch (e) { return [] }
        },
        async getHistoryKLineData (symbol, period, from, to) {
          try {
            const tfMap = {
              minute: period.multiplier === 1 ? '1m' : period.multiplier === 5 ? '5m' : period.multiplier === 15 ? '15m' : '30m',
              hour: period.multiplier === 1 ? '1H' : period.multiplier === 2 ? '2H' : '4H',
              day: '1D', week: '1W'
            }
            const timeframe = tfMap[period.timespan] || '1D'
            const res = await request({
              url: '/api/indicator/kline',
              method: 'get',
              params: { market: self.market, symbol: symbol.ticker || symbol.shortName || symbol.name, timeframe, limit: 1000 }
            })
            if (res.code === 1 && Array.isArray(res.data)) {
              const data = res.data.map(item => ({
                timestamp: (item.time || item.timestamp) * (item.time < 1e10 ? 1000 : 1),
                open: parseFloat(item.open), high: parseFloat(item.high),
                low: parseFloat(item.low), close: parseFloat(item.close),
                volume: parseFloat(item.volume || 0)
              }))
              // 缓存 K 线数据
              self._cachedKlineData = data
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
                hour: period.multiplier === 1 ? '1H' : period.multiplier === 2 ? '2H' : '4H',
                day: '1D', week: '1W'
              }
              const timeframe = tfMap[period.timespan] || '1D'
              const res = await request({
                url: '/api/indicator/kline',
                method: 'get',
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
          mainIndicators: ['MA'], subIndicators: ['VOL'],
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
        console.log('[KlineChartProWrapper] initChart: innerChart =', this.innerChart ? 'YES' : 'NO')
        // 如果拦截 init 未捕获到（Pro 可能异步创建 chart），轮询重试
        if (!this.innerChart) {
          let retries = 0
          const timer = setInterval(() => {
            retries++
            if (this.innerChart || retries >= 20) {
              clearInterval(timer)
              if (this.innerChart) {
                console.log('[KlineChartProWrapper] Chart captured after', retries, 'retries')
              } else {
                console.error('[KlineChartProWrapper] Failed to capture chart after 20 retries')
              }
            }
          }, 250)
        }
      } catch (e) { console.error('KLineChartPro init error:', e) }
    },

    // ========== Overlay（买卖信号等）==========
    createOverlay (overlayConfig) {
      if (!this.innerChart) return null
      try {
        return this.innerChart.createOverlay(overlayConfig)
      } catch (e) {
        console.error('[KlineChartProWrapper] createOverlay error:', e)
        return null
      }
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
      if (!this.innerChart) return this._cachedKlineData || []
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
