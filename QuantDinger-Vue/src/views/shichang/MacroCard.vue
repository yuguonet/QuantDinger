<template>
  <div class="module-card macro-card">
    <header class="module-header">
      <h3>📊 国内宏观数据</h3>
      <div class="header-right">
        <span v-if="lastUpdate" class="update-time">{{ lastUpdate }}</span>
        <button @click="doRefresh" :disabled="loading" class="btn-refresh">
          {{ loading ? '加载中...' : '刷新' }}
        </button>
      </div>
    </header>
    <div class="macro-content">
      <div class="macro-grid">
        <div
          v-for="item in macroItems"
          :key="item.key"
          class="macro-item"
          :class="item.trendClass"
        >
          <div class="macro-icon">{{ item.icon }}</div>
          <div class="macro-body">
            <div class="macro-name">{{ item.name }}</div>
            <div class="macro-value">
              <span class="value-text">{{ item.value }}</span>
              <span v-if="item.unit" class="value-unit">{{ item.unit }}</span>
            </div>
            <div v-if="item.change !== null" class="macro-change" :class="item.changeClass">
              {{ item.changePrefix }}{{ item.changeText }}
              <span class="change-label">环比</span>
            </div>
            <div v-else-if="item.period" class="macro-period">{{ item.period }}</div>
          </div>
          <div class="macro-trend-badge" :class="item.trendClass">
            {{ item.trendText }}
          </div>
        </div>
      </div>

      <div v-if="!loading && macroItems.length === 0" class="empty-state">
        <span>暂无宏观数据</span>
      </div>
    </div>
  </div>
</template>

<script>
import request from '@/utils/request'

/**
 * 每个指标的配置：尝试多种列名，兼容 Tushare / AKShare / 国家统计局
 */
const INDICATORS = [
  {
    key: 'gdp',
    name: 'GDP 同比',
    icon: '🏛️',
    unit: '%',
    // Tushare: gdp_yoy  AKShare: GDP同比增长(%)/同比  官方: value
    valueCols: [
      'gdp_yoy', 'GDP同比增长(%)', '同比', 'yoy',
      'gdp_yoy_yoy', 'value', 'GDP增速'
    ],
    periodCols: ['quarter', '季度', 'period', 'date'],
    trend: 'higher_better'
  },
  {
    key: 'cpi',
    name: 'CPI 同比',
    icon: '🛒',
    unit: '%',
    valueCols: [
      'cpi_yoy', '全国', '同比', 'yoy',
      'cpi', 'value', '全国-同比'
    ],
    periodCols: ['month', '月份', 'period', 'date'],
    trend: 'lower_better_2_3'
  },
  {
    key: 'ppi',
    name: 'PPI 同比',
    icon: '🏭',
    unit: '%',
    valueCols: [
      'ppi_yoy', '当月同比', '当月', '同比', 'yoy',
      'ppi', 'value'
    ],
    periodCols: ['month', '月份', 'period', 'date'],
    trend: 'higher_better'
  },
  {
    key: 'pmi',
    name: '制造业PMI',
    icon: '⚙️',
    unit: '',
    valueCols: [
      'pmi', '制造业-指数', '制造业', '指数', 'value',
      '制造业PMI'
    ],
    periodCols: ['month', '月份', 'period', 'date'],
    trend: 'higher_better_50'
  },
  {
    key: 'm2',
    name: 'M2 同比',
    icon: '💰',
    unit: '%',
    valueCols: [
      'm2_yoy', '货币和准货币(M2)-同比增长(%)', 'm2',
      'yoy', 'value', '同比增长(%)'
    ],
    periodCols: ['month', '月份', 'period', 'date'],
    trend: 'neutral'
  },
  {
    key: 'lpr',
    name: 'LPR 1Y',
    icon: '🏦',
    unit: '%',
    valueCols: [
      'lpr_1y', '1Y', '1年', 'lpr1y', 'lpr', 'value'
    ],
    periodCols: ['date', '日期', 'month', 'period'],
    trend: 'lower_better'
  },
  {
    key: 'social_financing',
    name: '社融增量',
    icon: '📈',
    unit: '万亿',
    valueCols: [
      'increment', '社会融资规模增量(亿元)', '社会融资规模增量',
      'value', 'shrzgm', 'total', '当月值(亿元)'
    ],
    periodCols: ['month', '月份', 'period', 'date'],
    trend: 'higher_better',
    divisor: 10000
  },
  {
    key: 'trade',
    name: '贸易差额',
    icon: '🚢',
    unit: '亿美元',
    valueCols: [
      'balance', '差额(亿元)', '贸易差额', '贸易差额(亿元)',
      'value', '当月差额'
    ],
    periodCols: ['month', '月份', 'period', 'date'],
    trend: 'higher_better'
  }
]

export default {
  name: 'MacroCard',
  data () {
    return {
      loading: false,
      lastUpdate: null,
      macroData: {},
      isDestroyed: false
    }
  },
  computed: {
    macroItems () {
      const items = []
      for (const ind of INDICATORS) {
        const data = this.macroData[ind.key]
        if (!data || !data.latest || data.latest.length === 0) continue

        const latest = data.latest[data.latest.length - 1]
        const value = this.pickValue(latest, ind.valueCols)
        if (value === null) continue

        // 环比变化
        let change = null
        let changeText = ''
        let changePrefix = ''
        let changeClass = ''
        if (data.latest.length >= 2) {
          const prev = data.latest[data.latest.length - 2]
          const prevVal = this.pickValue(prev, ind.valueCols)
          if (prevVal !== null) {
            change = value - prevVal
            changeText = Math.abs(change).toFixed(2)
            changePrefix = change >= 0 ? '+' : '-'
            changeClass = change >= 0 ? 'change-up' : 'change-down'
          }
        }

        const period = this.pickPeriod(latest, ind.periodCols)
        const { trendClass, trendText } = this.getTrend(ind.trend, value, change)
        const displayValue = ind.divisor ? (value / ind.divisor).toFixed(2) : this.formatDisplay(value, ind.key)

        items.push({
          key: ind.key,
          name: ind.name,
          icon: ind.icon,
          value: displayValue,
          unit: ind.unit,
          change,
          changeText,
          changePrefix,
          changeClass,
          trendClass,
          trendText,
          period
        })
      }
      return items
    }
  },
  mounted () {
    this.fetchMacro()
    this.pollTimer = setInterval(() => this.fetchMacro(), 600000)
  },
  beforeDestroy () {
    this.isDestroyed = true
    if (this.pollTimer) clearInterval(this.pollTimer)
  },
  methods: {
    async fetchMacro () {
      if (this.isDestroyed) return
      try {
        const res = await request({
          url: '/api/shichang/china-macro',
          method: 'GET',
          timeout: 30000
        })
        const body = res?.data || res
        if (this.isDestroyed) return
        if (body?.code === 1 && body.data) {
          this.macroData = body.data
        } else if (body && typeof body === 'object') {
          this.macroData = body
        }
        this.lastUpdate = new Date().toLocaleTimeString()
      } catch (e) {
        console.error('[宏观数据] 获取失败:', e)
      }
    },

    doRefresh () {
      this.loading = true
      this.fetchMacro().finally(() => {
        if (!this.isDestroyed) this.loading = false
      })
    },

    /** 从记录中按候选列名取数值，兼容中英文列名 */
    pickValue (record, candidates) {
      // 精确匹配候选列名
      for (const col of candidates) {
        if (record[col] !== undefined && record[col] !== null && record[col] !== '') {
          const v = parseFloat(record[col])
          if (!isNaN(v)) return v
        }
      }
      // 模糊匹配：遍历所有 key，找包含关键字的列
      const keywords = candidates.map(c => c.toLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]/g, ''))
      for (const [key, val] of Object.entries(record)) {
        if (key === 'name' || key === 'columns' || key === 'count') continue
        const clean = String(key).toLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]/g, '')
        for (const kw of keywords) {
          if (kw && clean.includes(kw)) {
            const v = parseFloat(val)
            if (!isNaN(v)) return v
          }
        }
      }
      // 最后兜底：取第一个看起来像数值的字段
      for (const [, val] of Object.entries(record)) {
        const v = parseFloat(val)
        if (!isNaN(v) && String(val).length < 20) return v
      }
      return null
    },

    pickPeriod (record, candidates) {
      for (const col of candidates) {
        if (record[col] !== undefined && record[col] !== null && record[col] !== '') {
          return String(record[col])
        }
      }
      return ''
    },

    formatDisplay (value, key) {
      if (key === 'trade') return value.toFixed(0)
      return value.toFixed(2)
    },

    getTrend (logic, value, change) {
      switch (logic) {
        case 'higher_better':
          if (change !== null && change > 0) return { trendClass: 'trend-up', trendText: '↑' }
          if (change !== null && change < 0) return { trendClass: 'trend-down', trendText: '↓' }
          return { trendClass: 'trend-neutral', trendText: '→' }
        case 'lower_better':
          if (change !== null && change < 0) return { trendClass: 'trend-up', trendText: '↑' }
          if (change !== null && change > 0) return { trendClass: 'trend-down', trendText: '↓' }
          return { trendClass: 'trend-neutral', trendText: '→' }
        case 'higher_better_50':
          if (value >= 50) return { trendClass: 'trend-up', trendText: '扩张' }
          return { trendClass: 'trend-down', trendText: '收缩' }
        case 'lower_better_2_3':
          if (value >= 2 && value <= 3) return { trendClass: 'trend-neutral', trendText: '温和' }
          if (value < 2) return { trendClass: 'trend-down', trendText: '偏低' }
          return { trendClass: 'trend-up', trendText: '偏高' }
        default:
          return { trendClass: 'trend-neutral', trendText: '' }
      }
    }
  }
}
</script>

<style scoped>
.macro-card {
  background: var(--card-bg, #ffffff);
  border-radius: 8px;
  box-shadow: var(--shadow, 0 2px 10px rgba(0,0,0,0.1));
  border: 1px solid var(--border-color, #dbdbdb);
  overflow: hidden;
}
.module-header {
  padding: 12px 16px;
  background: var(--header-bg, #f8f9fc);
  border-bottom: 1px solid var(--border-color, #ebeef5);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.module-header h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--text-color, #333);
}
.header-right { display: flex; align-items: center; gap: 10px; }
.update-time { font-size: 11px; color: #999; }
.btn-refresh {
  padding: 4px 8px;
  background: #ecf5ff;
  color: #409eff;
  border: 1px solid #b3d8ff;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}
.btn-refresh:disabled { opacity: 0.5; cursor: not-allowed; }
.macro-content { padding: 12px; }
.macro-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 8px;
}
.macro-item {
  background: var(--row-bg, #fafbfc);
  border-radius: 8px;
  padding: 12px;
  border: 1px solid var(--border-color, #ebeef5);
  transition: all 0.2s ease;
  position: relative;
  overflow: hidden;
}
.macro-item:hover {
  border-color: #409eff;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.1);
  transform: translateY(-1px);
}
.macro-icon { font-size: 18px; margin-bottom: 6px; }
.macro-body { position: relative; z-index: 1; }
.macro-name {
  font-size: 12px;
  color: #909399;
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.macro-value { display: flex; align-items: baseline; gap: 2px; margin-bottom: 4px; }
.value-text {
  font-size: 20px;
  font-weight: 700;
  color: var(--text-color, #303133);
  font-family: 'SF Mono', 'Menlo', 'Consolas', monospace;
}
.value-unit { font-size: 12px; color: #909399; }
.macro-change {
  font-size: 11px;
  font-weight: 600;
  font-family: 'SF Mono', 'Menlo', monospace;
}
.change-up { color: #f56c6c; }
.change-down { color: #67c23a; }
.change-label { color: #c0c4cc; font-weight: 400; margin-left: 2px; }
.macro-period { font-size: 11px; color: #c0c4cc; }
.macro-trend-badge {
  position: absolute;
  top: 8px;
  right: 8px;
  font-size: 10px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 3px;
}
.trend-up { background: #fef0f0; color: #f56c6c; }
.trend-down { background: #f0f9eb; color: #67c23a; }
.trend-neutral { background: #f4f4f5; color: #909399; }
.empty-state { text-align: center; padding: 24px; color: #c0c4cc; font-size: 14px; }
@media (max-width: 768px) {
  .macro-grid { grid-template-columns: repeat(2, 1fr); }
  .macro-item { padding: 10px; }
  .value-text { font-size: 18px; }
}
</style>
