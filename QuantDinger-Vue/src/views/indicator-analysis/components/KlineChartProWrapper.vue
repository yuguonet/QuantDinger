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
 * 动态加载 public/static/klinecharts-pro.umd.js
 */
export default {
  name: 'KlineChartProWrapper',
  props: {
    symbol: { type: String, default: '' },
    market: { type: String, default: '' },
    timeframe: { type: String, default: '1D' },
    theme: { type: String, default: 'light' },
    locale: { type: String, default: 'zh-CN' }
  },
  data () {
    return {
      chartPro: null,
      innerChart: null,
      realtimeTimer: null,
      proReady: false
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
  },
  beforeDestroy () {
    this.clearRealtimeTimer()
  },
  methods: {
    loadProScript () {
      // 检查是否已加载
      if (window.klinechartspro && window.klinechartspro.KLineChartPro) {
        this.proReady = true
        this.initChart()
        return
      }

      // 动态加载 CSS
      if (!document.querySelector('link[href*="klinecharts-pro.css"]')) {
        const link = document.createElement('link')
        link.rel = 'stylesheet'
        link.href = process.env.BASE_URL + 'static/klinecharts-pro.css'
        document.head.appendChild(link)
      }

      // 动态加载 JS
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
          } catch (e) {
            return []
          }
        },
        async getHistoryKLineData (symbol, period, from, to) {
          try {
            const tfMap = {
              minute: period.multiplier === 1 ? '1m' : period.multiplier === 5 ? '5m' : period.multiplier === 15 ? '15m' : '30m',
              hour: period.multiplier === 1 ? '1H' : period.multiplier === 2 ? '2H' : '4H',
              day: '1D',
              week: '1W'
            }
            const timeframe = tfMap[period.timespan] || '1D'
            const res = await request({
              url: '/api/indicator/kline',
              method: 'get',
              params: {
                market: self.market,
                symbol: symbol.ticker || symbol.shortName || symbol.name,
                timeframe: timeframe,
                limit: 1000
              }
            })
            if (res.code === 1 && Array.isArray(res.data)) {
              return res.data.map(item => ({
                timestamp: (item.time || item.timestamp) * (item.time < 1e10 ? 1000 : 1),
                open: parseFloat(item.open),
                high: parseFloat(item.high),
                low: parseFloat(item.low),
                close: parseFloat(item.close),
                volume: parseFloat(item.volume || 0)
              }))
            }
            return []
          } catch (e) {
            return []
          }
        },
        subscribe (symbol, period, callback) {
          self.clearRealtimeTimer()
          self.realtimeTimer = setInterval(async () => {
            try {
              const tfMap = {
                minute: period.multiplier === 1 ? '1m' : period.multiplier === 5 ? '5m' : period.multiplier === 15 ? '15m' : '30m',
                hour: period.multiplier === 1 ? '1H' : period.multiplier === 2 ? '2H' : '4H',
                day: '1D',
                week: '1W'
              }
              const timeframe = tfMap[period.timespan] || '1D'
              const res = await request({
                url: '/api/indicator/kline',
                method: 'get',
                params: {
                  market: self.market,
                  symbol: symbol.ticker || symbol.shortName || symbol.name,
                  timeframe: timeframe,
                  limit: 2
                }
              })
              if (res.code === 1 && Array.isArray(res.data) && res.data.length > 0) {
                const latest = res.data[res.data.length - 1]
                const klineData = {
                  timestamp: (latest.time || latest.timestamp) * (latest.time < 1e10 ? 1000 : 1),
                  open: parseFloat(latest.open),
                  high: parseFloat(latest.high),
                  low: parseFloat(latest.low),
                  close: parseFloat(latest.close),
                  volume: parseFloat(latest.volume || 0)
                }
                callback(klineData)
              }
            } catch (e) { /* ignore */ }
          }, 5000)
        },
        unsubscribe () {
          self.clearRealtimeTimer()
        }
      }
    },
    clearRealtimeTimer () {
      if (this.realtimeTimer) {
        clearInterval(this.realtimeTimer)
        this.realtimeTimer = null
      }
    },
    initChart () {
      if (!this.$refs.chartContainer || !this.proReady) return

      const KLineChartPro = window.klinechartspro && window.klinechartspro.KLineChartPro
      if (!KLineChartPro) {
        console.error('klinecharts-pro not loaded')
        return
      }

      try {
        const isDark = this.theme === 'dark'
        this.chartPro = new KLineChartPro({
          container: this.$refs.chartContainer,
          theme: this.theme,
          locale: this.locale,
          symbol: this.symbolObj,
          period: this.periodObj,
          drawingBarVisible: true,
          mainIndicators: ['MA'],
          subIndicators: ['VOL'],
          datafeed: this.createDatafeed(),
          styles: {
            candle: {
              bar: {
                upColor: isDark ? '#ef5350' : '#f5222d',
                downColor: isDark ? '#0ecb81' : '#52c41a',
                noChangeColor: '#888888',
                upBorderColor: isDark ? '#ef5350' : '#f5222d',
                downBorderColor: isDark ? '#0ecb81' : '#52c41a',
                noChangeBorderColor: '#888888',
                upWickColor: isDark ? '#ef5350' : '#f5222d',
                downWickColor: isDark ? '#0ecb81' : '#52c41a',
                noChangeWickColor: '#888888'
              }
            },
            indicator: {
              bars: [{
                style: 'fill',
                upColor: isDark ? '#ef5350' : '#f5222d',
                downColor: isDark ? '#0ecb81' : '#52c41a',
                noChangeColor: '#888888'
              }]
            }
          }
        })

        // 获取内部klinecharts实例
        this.$nextTick(() => {
          this._bindInnerChart()
        })
      } catch (e) {
        console.error('KLineChartPro init error:', e)
      }
    },
    _bindInnerChart () {
      // Pro内部的chart容器是 .klinecharts-pro-widget
      const widgetEl = this.$refs.chartContainer.querySelector('.klinecharts-pro-widget')
      if (widgetEl && typeof klinecharts.init === 'function') {
        // klinecharts.init 会返回已有实例（如果容器已初始化）
        const chart = klinecharts.init(widgetEl)
        if (chart) {
          this.innerChart = chart
        }
      }
    },
    /** 获取内部klinecharts实例，供外部添加自定义指标 */
    getChart () {
      return this.innerChart
    },
    // 兼容原 KlineChart 组件的方法
    updateIndicators () {
      // 由外部调用，通过getChart()获取实例后自行操作
    },
    executePythonStrategy () {
      return null
    },
    resize () {
      if (this.innerChart) {
        this.innerChart.resize()
      }
    }
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
