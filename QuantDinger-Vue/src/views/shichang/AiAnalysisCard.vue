<template>
  <div class="module-card">
    <header class="module-header">
      <h3>AI市场分析</h3>
      <div class="ai-label">AI分析</div>
      <button @click="refresh" :disabled="loading" class="btn-refresh">
        {{ loading ? '加载中...' : '刷新' }}
      </button>
    </header>
    <div class="module-content ai-analysis">
      <div class="confidence-score">
        温和置信度 {{ data.confidence }}%
        <span>建议: {{ data.advice }}</span>
      </div>
      <p class="market-phase">{{ data.phase }}</p>
      <div class="metrics-row">
        <div class="metric-item">
          <div class="metric-value">{{ data.temperature }}</div>
          <div class="metric-label">市场温度<br /><small>{{ getLevelText(data.temperature, ['低温', '适中', '高温']) }}</small></div>
        </div>
        <div class="metric-item">
          <div class="metric-value">{{ data.profitEffect }}</div>
          <div class="metric-label">赚钱效应<br /><small>{{ getLevelText(data.profitEffect, ['差', '一般', '好']) }}</small></div>
        </div>
        <div class="metric-item">
          <div class="metric-value">{{ data.riskScore }}</div>
          <div class="metric-label">风险等级<br /><small>{{ data.riskLevel }}</small></div>
        </div>
      </div>
      <div class="hot-sectors">
        <h4>● 热门板块</h4>
        <div class="sector-list">
          <div v-for="s in data.hotSectors" :key="s.name" class="sector-item">
            <strong>{{ s.name }}</strong><br />{{ s.driver }}<br /><span class="score">{{ s.score }}</span>
          </div>
        </div>
      </div>
      <div class="operation-advice">
        <h4>● 操作建议</h4>
        <ul>
          <li v-for="(a, i) in data.operationAdvice" :key="i">{{ a }}</li>
        </ul>
      </div>
    </div>
  </div>
</template>

<script>
import request from '@/utils/request'

export default {
  name: 'AiAnalysisCard',
  data () {
    return {
      loading: false,
      data: {
        confidence: 0,
        phase: '等待数据...',
        temperature: 50,
        profitEffect: 50,
        riskLevel: '中',
        riskScore: 50,
        advice: '等待',
        hotSectors: [],
        operationAdvice: []
      }
    }
  },
  methods: {
    async refresh () {
      this.loading = true
      try {
        const d = await request({ url: '/api/shichang/cards/ai-analysis', method: 'GET' })
        if (d) Object.assign(this.data, d)
      } catch (e) {
        console.error('AI分析刷新失败:', e)
      } finally {
        this.loading = false
      }
    },
    getLevelText (v, labels) {
      return v < 40 ? labels[0] : v < 70 ? labels[1] : labels[2]
    }
  },
  mounted () { this.refresh() }
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
.ai-label { font-size: 12px; background: #409eff; color: white; padding: 2px 6px; border-radius: 3px; }
.btn-refresh {
  padding: 4px 8px; background: #ecf5ff; color: #409eff; border: 1px solid #b3d8ff;
  border-radius: 4px; cursor: pointer; font-size: 12px;
}
.btn-refresh:disabled { opacity: 0.5; cursor: not-allowed; }
.ai-analysis { padding: 16px; }
.confidence-score { font-size: 14px; margin-bottom: 8px; }
.confidence-score span { color: #999; font-size: 12px; }
.market-phase { font-size: 13px; color: #666; margin: 8px 0; }
.metrics-row { display: flex; justify-content: space-between; margin: 16px 0; }
.metric-item { text-align: center; flex: 1; padding: 0 8px; }
.metric-value { font-size: 18px; font-weight: 600; }
.metric-label { font-size: 12px; color: #999; }
.hot-sectors h4, .operation-advice h4 { margin: 16px 0 8px; font-size: 14px; color: #333; }
.sector-list { display: flex; flex-wrap: wrap; gap: 10px; }
.sector-item { flex: 1; min-width: 90px; background: #f0f2f5; padding: 8px; border-radius: 4px; font-size: 12px; }
.sector-item .score { color: #409eff; font-weight: 600; }
.operation-advice ul { margin: 0; padding-left: 16px; font-size: 13px; color: #666; }
</style>
