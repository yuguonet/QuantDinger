<template>
  <div ref="chartContainer" class="klinecharts-pro-wrapper"></div>
</template>

<script>
import request from '@/utils/request'
import * as klinecharts from 'klinecharts'

// klinecharts-pro UMD 需要 window.klinecharts 全局变量
if (typeof window !== 'undefined' && !window.klinecharts) {
  window.klinecharts = klinecharts
}
import './klinecharts-pro.umd.js'
import './klinecharts-pro.css'

const klinechartspro = (typeof window !== 'undefined' && window.klinechartspro) || {}

/**
 * KLineChartPro Vue 包装组件
 * 直接使用 @klinecharts/pro 的 UMD 版本，无需 npm install
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
      realtimeTimer: null
    }
  },
  computed: {
    periodObj () {
      // 将 timeframe 转换为 Pro 的 period 格式
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
    this.initChart()
  },
  beforeDestroy () {
    this.destroyChart()
  },
  methods: {
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
            console.warn('searchSymbols error:', e)
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
            console.warn('getHistoryKLineData error:', e)
            return []
          }
        },
        subscribe (symbol, period, callback) {
          // 简单轮询实现，每 5 秒获取最新数据
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
                callback({
                  timestamp: (latest.time || latest.timestamp) * (latest.time < 1e10 ? 1000 : 1),
                  open: parseFloat(latest.open),
                  high: parseFloat(latest.high),
                  low: parseFloat(latest.low),
                  close: parseFloat(latest.close),
                  volume: parseFloat(latest.volume || 0)
                })
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
      if (!this.$refs.chartContainer || !klinechartspro.KLineChartPro) return
      try {
        this.chartPro = new klinechartspro.KLineChartPro({
          container: this.$refs.chartContainer,
          theme: this.theme,
          locale: this.locale,
          symbol: this.symbolObj,
          period: this.periodObj,
          drawingBarVisible: true,
          mainIndicators: ['MA'],
          subIndicators: ['VOL'],
          datafeed: this.createDatafeed()
        })
      } catch (e) {
        console.error('KLineChartPro init error:', e)
      }
    },
    destroyChart () {
      this.clearRealtimeTimer()
      // Pro 组件没有 destroy 方法，清理容器即可
      if (this.$refs.chartContainer) {
        this.$refs.chartContainer.innerHTML = ''
      }
      this.chartPro = null
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
