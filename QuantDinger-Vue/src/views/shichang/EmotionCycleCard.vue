<template>
  <div class="module-card">
    <header class="module-header">
      <h3>情绪周期</h3>
      <button @click="refresh" :disabled="loading" class="btn-refresh">
        {{ loading ? '加载中...' : '刷新' }}
      </button>
    </header>
    <div class="module-content chart-container">
      <div ref="chart" class="echart-wrapper"></div>
    </div>
  </div>
</template>

<script>
import * as echarts from 'echarts'
import request from '@/utils/request'

export default {
  name: 'EmotionCycleCard',
  data () {
    return {
      loading: false,
      chartInstance: null,
      resizeHandler: null,
      history: []
    }
  },
  methods: {
    async refresh () {
      this.loading = true
      try {
        const d = await request({ url: '/api/shichang/cards/emotion-cycle', method: 'GET' })
        this.history = d?.history || []
        this.renderChart()
      } catch (e) {
        console.error('情绪周期刷新失败:', e)
      } finally {
        this.loading = false
      }
    },
    renderChart () {
      const el = this.$refs.chart
      if (!el) return
      if (this.chartInstance) this.chartInstance.dispose()
      this.chartInstance = echarts.init(el)

      const xData = this.history.map(d => d.time)
      const yData = this.history.map(d => d.value)

      this.chartInstance.setOption({
        tooltip: {
          trigger: 'axis',
          formatter: (params) => {
            const p = params[0]
            return `${p.name}<br/>情绪指数: ${p.value}`
          }
        },
        grid: { left: 40, right: 16, top: 16, bottom: 24 },
        xAxis: {
          type: 'category',
          data: xData,
          axisLabel: { interval: Math.max(0, Math.floor(xData.length / 8) - 1), fontSize: 11 },
          axisTick: { show: false }
        },
        yAxis: {
          type: 'value', min: 0, max: 100,
          splitLine: { lineStyle: { type: 'dashed', color: '#eee' } }
        },
        series: [{
          data: yData, type: 'line', smooth: true, symbol: 'none',
          lineStyle: { width: 2, color: '#409eff' },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(64,158,255,0.25)' },
              { offset: 1, color: 'rgba(64,158,255,0.02)' }
            ])
          },
          markLine: {
            silent: true, symbol: 'none',
            data: [
              { yAxis: 70, lineStyle: { color: '#f56c6c', type: 'dashed' }, label: { show: true, formatter: '高涨', fontSize: 10, color: '#f56c6c' } },
              { yAxis: 30, lineStyle: { color: '#67c23a', type: 'dashed' }, label: { show: true, formatter: '低迷', fontSize: 10, color: '#67c23a' } }
            ]
          }
        }]
      })

      if (this.resizeHandler) window.removeEventListener('resize', this.resizeHandler)
      this.resizeHandler = () => this.chartInstance?.resize()
      window.addEventListener('resize', this.resizeHandler)
    }
  },
  mounted () { this.refresh() },
  beforeDestroy () {
    if (this.resizeHandler) window.removeEventListener('resize', this.resizeHandler)
    if (this.chartInstance) { this.chartInstance.dispose(); this.chartInstance = null }
  }
}
</script>

<style scoped>
.module-card {
  background: var(--card-bg, #fff); border-radius: 8px; box-shadow: var(--shadow, 0 2px 10px rgba(0,0,0,0.1));
  border: 1px solid #dbdbdb; padding: 8px; overflow: hidden;
}
.module-header {
  padding: 12px 16px; background: #f8f9fc; border-bottom: 1px solid var(--border-color, #ebeef5);
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px;
}
.module-header h3 { margin: 0; font-size: 16px; font-weight: 600; color: #333; }
.btn-refresh {
  padding: 4px 8px; background: #ecf5ff; color: #409eff; border: 1px solid #b3d8ff;
  border-radius: 4px; cursor: pointer; font-size: 12px;
}
.btn-refresh:disabled { opacity: 0.5; cursor: not-allowed; }
.chart-container { height: 300px; position: relative; }
.echart-wrapper { width: 100%; height: 100%; }
</style>
