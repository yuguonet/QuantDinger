<template>
  <div class="module-card macro-card">
    <header class="module-header">
      <h3>📊 国内宏观</h3>
      <div class="header-right">
        <span v-if="lastUpdate" class="update-time">{{ lastUpdate }}</span>
        <button @click="openDetail" class="btn-detail">详情</button>
        <button @click="doRefresh" :disabled="loading" class="btn-refresh">
          {{ loading ? '加载中...' : '刷新' }}
        </button>
      </div>
    </header>
    <div class="macro-content">
      <!-- 整体评分（与外围市场一致） -->
      <div class="overall-score" :class="getScoreClass(overallScore)">
        <div class="score-ring">
          <svg viewBox="0 0 120 120">
            <circle cx="60" cy="60" r="52" class="ring-bg" />
            <circle cx="60" cy="60" r="52"
                    class="ring-progress"
                    :stroke-dasharray="`${overallScore * 3.267}, 326.7`"
                    :class="getScoreClass(overallScore)"
            />
          </svg>
          <div class="score-inner">
            <span class="score-value">{{ overallScore }}</span>
            <span class="score-label">{{ getOverallLabel(overallScore) }}</span>
          </div>
        </div>
        <div class="score-desc">
          <div class="desc-title" :class="getScoreClass(overallScore)">
            宏观经济健康度
          </div>
          <div class="desc-detail">{{ getOverallDesc(overallScore) }}</div>
        </div>
      </div>

      <!-- 指标列表（最多3个，与外围市场一致） -->
      <div class="macro-list">
        <div
          v-for="item in displayItems"
          :key="item.key"
          class="macro-row"
        >
          <div class="macro-icon">{{ item.icon }}</div>
          <div class="macro-info">
            <div class="macro-name">{{ item.name }}</div>
            <div class="macro-detail">
              <span class="macro-value-text">{{ item.value }}{{ item.unit }}</span>
              <span v-if="item.change !== null" class="macro-change" :class="item.changeClass">
                {{ item.changePrefix }}{{ item.changeText }}
              </span>
              <span v-else-if="item.period" class="macro-period">{{ item.period }}</span>
            </div>
          </div>
          <div class="macro-score-col">
            <div class="sentiment-bar-wrap">
              <div class="sentiment-bar" :class="getSentimentClass(item.score)"
                   :style="{ width: item.score + '%' }">
              </div>
            </div>
            <div class="sentiment-value" :class="getSentimentClass(item.score)">
              {{ item.score }}
            </div>
          </div>
          <div class="macro-trend-badge" :class="item.trendClass">
            {{ item.trendText }}
          </div>
        </div>
      </div>

      <div v-if="!loading && displayItems.length === 0" class="empty-state">
        <span>暂无宏观数据</span>
      </div>
    </div>

    <!-- 详情弹窗 -->
    <div v-if="showDetail" class="modal-overlay" @click.self="showDetail = false">
      <div class="modal-container">
        <header class="modal-header">
          <h3>📊 国内宏观详情</h3>
          <div class="modal-tabs">
            <button
              v-for="tab in tabs"
              :key="tab.key"
              class="tab-btn"
              :class="{ active: activeTab === tab.key }"
              @click="switchTab(tab.key)"
            >{{ tab.label }}</button>
          </div>
          <button class="modal-close" @click="showDetail = false">&times;</button>
        </header>
        <div class="modal-body">
          <!-- 宏观数据完整展示 -->
          <div v-if="activeTab === 'macroData'" class="tab-content">
            <div v-if="loading && !hasMacroData" class="loading-state">加载中...</div>
            <div v-else class="macro-detail-wrap">
              <div
                v-for="ind in macroIndicators"
                :key="ind.key"
                class="macro-detail-section"
              >
                <div class="macro-detail-header">
                  <span class="macro-detail-icon">{{ ind.icon }}</span>
                  <span class="macro-detail-name">{{ ind.name }}</span>
                  <span v-if="getMacroLatestValue(ind) !== null" class="macro-detail-latest">
                    {{ formatMacroValue(getMacroLatestValue(ind), ind) }}{{ ind.unit }}
                  </span>
                  <span v-else class="macro-detail-latest macro-detail-na">--</span>
                </div>
                <div v-if="getMacroRecords(ind).length > 0" class="macro-detail-table-wrap">
                  <table class="data-table">
                    <thead>
                      <tr>
                        <th v-for="col in getMacroColumns(ind)" :key="col">{{ col }}</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="(row, ri) in getMacroRecords(ind)" :key="ri">
                        <td v-for="col in getMacroColumns(ind)" :key="col"
                            :class="isNumericCol(col, row) ? getNumericClass(row[col]) : ''">
                          {{ formatTableCell(row[col], col) }}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <div v-else class="macro-detail-empty">暂无数据</div>
              </div>
            </div>
          </div>

          <!-- 贪婪恐惧指数 -->
          <div v-if="activeTab === 'fearGreed'" class="tab-content">
            <div v-if="fgLoading" class="loading-state">加载中...</div>
            <div v-else-if="fgData && fgData.composite_score !== undefined" class="fg-content">
              <div class="fg-overall">
                <div class="fg-score-big" :class="getFgClass(fgData.composite_score)">
                  {{ fgData.composite_score }}
                </div>
                <div class="fg-label">{{ fgData.label }}</div>
                <div class="fg-bar-wrap">
                  <div class="fg-bar">
                    <div class="fg-bar-fill" :style="{ width: fgData.composite_score + '%' }"
                         :class="getFgClass(fgData.composite_score)"></div>
                  </div>
                  <div class="fg-bar-labels">
                    <span>极度恐惧</span><span>中性</span><span>极度贪婪</span>
                  </div>
                </div>
              </div>
              <div class="fg-indicators">
                <div v-for="ind in fgData.indicators" :key="ind.name" class="fg-indicator-row">
                  <div class="fg-ind-name">{{ ind.name }}</div>
                  <div class="fg-ind-bar-wrap">
                    <div class="fg-ind-bar">
                      <div class="fg-ind-bar-fill" :class="getFgClass(ind.score)"
                           :style="{ width: ind.score + '%' }"></div>
                    </div>
                  </div>
                  <div class="fg-ind-score" :class="getFgClass(ind.score)">{{ ind.score }}</div>
                  <div class="fg-ind-detail">{{ ind.detail }}</div>
                </div>
              </div>
            </div>
            <div v-else class="empty-state">暂无贪婪恐惧数据</div>
          </div>

          <!-- 政策解读 -->
          <div v-if="activeTab === 'policy'" class="tab-content">
            <div v-if="policyLoading" class="loading-state">加载中...</div>
            <div v-else-if="policyData && policyItems.length > 0" class="policy-content">
              <!-- AI 深度分析摘要 -->
              <div v-if="policyData.data && policyData.data.mode === 'ai'" class="policy-ai-summary">
                <div class="policy-sentiment">
                  <span class="sentiment-label">政策基调:</span>
                  <span class="sentiment-value">{{ policyData.data.overall_sentiment || '—' }}</span>
                </div>
                <p class="policy-summary-text">{{ policyData.data.summary || '' }}</p>
                <div v-if="policyData.data.market_outlook" class="policy-outlook">
                  <h4>🔮 市场展望</h4>
                  <p>{{ policyData.data.market_outlook }}</p>
                </div>
                <div v-if="policyData.data.actionable_advice" class="policy-advice">
                  <h4>💡 策略建议</h4>
                  <p>{{ policyData.data.actionable_advice }}</p>
                </div>
              </div>
              <!-- 政策影响列表 -->
              <div v-if="policyImpacts.length > 0" class="policy-impacts">
                <h4>🎯 政策影响分析</h4>
                <div v-for="(imp, i) in policyImpacts" :key="i" class="policy-impact-row">
                  <span class="impact-keyword">{{ imp.keyword }}</span>
                  <span class="impact-direction" :class="typeof imp.direction === 'number' ? (imp.direction > 0 ? 'up' : imp.direction < 0 ? 'down' : '') : ''">
                    {{ typeof imp.direction === 'number' ? (imp.direction > 0 ? '利好' : imp.direction < 0 ? '利空' : '中性') : imp.direction || '中性' }}
                  </span>
                  <span class="impact-sector">{{ imp.sector || '' }}</span>
                </div>
              </div>
              <!-- 政策详情列表 -->
              <div class="policy-list">
                <h4>📰 {{ policyData.data.mode === 'ai' ? '政策详情' : '政策关键词扫描' }}</h4>
                <div v-for="(item, i) in policyItems" :key="i" class="policy-item-row">
                  <span class="policy-time">{{ item.time || '' }}</span>
                  <span class="policy-title">{{ item.title || item.text || '' }}</span>
                  <span v-if="item.impact_detail" class="policy-impact-detail">{{ item.impact_detail }}</span>
                  <span v-if="item.keywords && item.keywords.length > 0" class="policy-keywords">
                    <span v-for="kw in item.keywords.slice(0, 3)" :key="kw" class="policy-kw-tag">{{ kw }}</span>
                  </span>
                </div>
              </div>
            </div>
            <div v-else class="empty-state">暂无政策数据</div>
          </div>

          <!-- 板块历史排名 -->
          <div v-if="activeTab === 'sectorHistory'" class="tab-content">
            <div class="sh-controls">
              <button
                v-for="bt in boardTypes"
                :key="bt.key"
                class="board-type-btn"
                :class="{ active: shBoardType === bt.key }"
                @click="shBoardType = bt.key; fetchSectorHistory()"
              >{{ bt.label }}</button>
              <select v-model="shDays" @change="fetchSectorHistory()" class="sh-days-select">
                <option :value="7">近7天</option>
                <option :value="14">近14天</option>
                <option :value="30">近30天</option>
                <option :value="60">近60天</option>
              </select>
            </div>
            <div v-if="shLoading" class="loading-state">加载中...</div>
            <div v-else-if="shData.length > 0" class="sh-table-wrap">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>日期</th>
                    <th>板块名称</th>
                    <th>排名</th>
                    <th>涨跌幅</th>
                    <th>领涨股</th>
                    <th>主力净流入</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, i) in shPagedData" :key="i">
                    <td>{{ row.date || '' }}</td>
                    <td>{{ row.name || '' }}</td>
                    <td>{{ row.rank || '' }}</td>
                    <td :class="parseFloat(row.change_pct || 0) >= 0 ? 'up' : 'down'">
                      {{ formatPct(row.change_pct) }}
                    </td>
                    <td>{{ row.leader || row.lead_stock || '' }}</td>
                    <td :class="parseFloat(row.main_net_inflow || 0) >= 0 ? 'up' : 'down'">
                      {{ formatAmount(row.main_net_inflow) }}
                    </td>
                  </tr>
                </tbody>
              </table>
              <div class="sh-pagination">
                <button :disabled="shPage <= 1" @click="shPage--">上一页</button>
                <span>{{ shPage }} / {{ shTotalPages }}</span>
                <button :disabled="shPage >= shTotalPages" @click="shPage++">下一页</button>
                <span class="sh-total">共 {{ shData.length }} 条</span>
              </div>
            </div>
            <div v-else class="empty-state">暂无板块历史数据</div>
          </div>
        </div>
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
    key: 'gdp', name: 'GDP 同比', icon: '🏛️', unit: '%',
    valueCols: ['gdp_yoy', 'GDP同比增长(%)', '同比', 'yoy', 'gdp_yoy_yoy', 'value', 'GDP增速'],
    periodCols: ['quarter', '季度', 'period', 'date'],
    trend: 'higher_better'
  },
  {
    key: 'cpi', name: 'CPI 同比', icon: '🛒', unit: '%',
    valueCols: ['cpi_yoy', '全国', '同比', 'yoy', 'cpi', 'value', '全国-同比'],
    periodCols: ['month', '月份', 'period', 'date'],
    trend: 'lower_better_2_3'
  },
  {
    key: 'ppi', name: 'PPI 同比', icon: '🏭', unit: '%',
    valueCols: ['ppi_yoy', '当月同比', '当月', '同比', 'yoy', 'ppi', 'value'],
    periodCols: ['month', '月份', 'period', 'date'],
    trend: 'higher_better'
  },
  {
    key: 'pmi', name: '制造业PMI', icon: '⚙️', unit: '',
    valueCols: ['pmi', '制造业-指数', '制造业', '指数', 'value', '制造业PMI'],
    periodCols: ['month', '月份', 'period', 'date'],
    trend: 'higher_better_50'
  },
  {
    key: 'm2', name: 'M2 同比', icon: '💰', unit: '%',
    valueCols: ['m2_yoy', '货币和准货币(M2)-同比增长(%)', 'm2', 'yoy', 'value', '同比增长(%)'],
    periodCols: ['month', '月份', 'period', 'date'],
    trend: 'neutral'
  },
  {
    key: 'lpr', name: 'LPR 1Y', icon: '🏦', unit: '%',
    valueCols: ['lpr_1y', '1Y', '1年', 'lpr1y', 'lpr', 'value'],
    periodCols: ['date', '日期', 'month', 'period'],
    trend: 'lower_better'
  },
  {
    key: 'social_financing', name: '社融增量', icon: '📈', unit: '万亿',
    valueCols: ['increment', '社会融资规模增量(亿元)', '社会融资规模增量', 'value', 'shrzgm', 'total', '当月值(亿元)'],
    periodCols: ['month', '月份', 'period', 'date'],
    trend: 'higher_better', divisor: 10000
  },
  {
    key: 'trade', name: '贸易差额', icon: '🚢', unit: '亿美元',
    valueCols: ['balance', '差额(亿元)', '贸易差额', '贸易差额(亿元)', 'value', '当月差额'],
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
      isDestroyed: false,

      // 详情弹窗
      showDetail: false,
      activeTab: 'macroData',
      tabs: [
        { key: 'macroData', label: '宏观数据' },
        { key: 'fearGreed', label: '贪婪恐惧' },
        { key: 'policy', label: '政策解读' },
        { key: 'sectorHistory', label: '板块历史' }
      ],

      // 贪婪恐惧
      fgLoading: false,
      fgData: {},
      fgCached: null,

      // 政策
      policyLoading: false,
      policyData: {},

      // 板块历史
      shLoading: false,
      shBoardType: 'industry',
      shDays: 30,
      shData: [],
      shPage: 1,
      boardTypes: [
        { key: 'industry', label: '行业板块' },
        { key: 'concept', label: '概念板块' }
      ]
    }
  },
  computed: {
    macroIndicators () {
      return INDICATORS
    },
    hasMacroData () {
      return this.macroData && Object.keys(this.macroData).length > 0
    },
    displayItems () {
      // 第一个固定为 A股贪恐指数，后两个为宏观指标 Top2
      const items = []

      // 贪恐指数
      if (this.fgCached && this.fgCached.composite_score !== undefined) {
        const score = Math.round(this.fgCached.composite_score)
        items.push({
          key: 'fearGreed',
          name: 'A股贪恐指数',
          icon: score > 60 ? '🔥' : score < 40 ? '😱' : '😐',
          value: String(score),
          unit: '',
          change: null,
          changeText: '',
          changePrefix: '',
          changeClass: '',
          trendClass: score >= 60 ? 'trend-up' : score <= 40 ? 'trend-down' : 'trend-neutral',
          trendText: this.fgCached.label || '',
          period: '',
          score: score
        })
      }

      // 宏观指标 Top2
      const macroTop = [...this.macroItems]
        .sort((a, b) => b.score - a.score)
        .slice(0, 3 - items.length)

      return [...items, ...macroTop]
    },
    macroItems () {
      const items = []
      for (const ind of INDICATORS) {
        const data = this.macroData[ind.key]
        if (!data || !data.latest || data.latest.length === 0) continue

        const latest = data.latest[data.latest.length - 1]
        const value = this.pickValue(latest, ind.valueCols)
        if (value === null) continue

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
        const score = this.calcIndicatorScore(ind.trend, value, change)

        items.push({
          key: ind.key, name: ind.name, icon: ind.icon,
          value: displayValue, unit: ind.unit,
          change, changeText, changePrefix, changeClass,
          trendClass, trendText, period, score
        })
      }
      return items
    },
    overallScore () {
      if (this.macroItems.length === 0) return 50
      const total = this.macroItems.reduce((sum, i) => sum + i.score, 0)
      return Math.round(total / this.macroItems.length)
    },
    policyItems () {
      const d = this.policyData
      if (!d || !d.data) return []
      return d.data.policy_items || d.data.items || []
    },
    policyImpacts () {
      const d = this.policyData
      if (!d || !d.data) return []
      return d.data.impacts || []
    },
    shTotalPages () {
      return Math.max(1, Math.ceil(this.shData.length / 20))
    },
    shPagedData () {
      const start = (this.shPage - 1) * 20
      return this.shData.slice(start, start + 20)
    }
  },
  mounted () {
    this.fetchMacro()
    this.fetchFearGreedCard()
    this.pollTimer = setInterval(() => {
      this.fetchMacro()
      this.fetchFearGreedCard()
    }, 600000)
  },
  beforeDestroy () {
    this.isDestroyed = true
    if (this.pollTimer) clearInterval(this.pollTimer)
  },
  methods: {
    async fetchMacro () {
      if (this.isDestroyed) return
      try {
        const res = await request({ url: '/api/shichang/china-macro', method: 'GET', timeout: 30000 })
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

    async doRefresh () {
      this.loading = true
      try {
        await Promise.all([this.fetchMacro(), this.fetchFearGreedCard()])
      } finally {
        if (!this.isDestroyed) this.loading = false
      }
    },

    // ====== 详情弹窗 ======

    openDetail () {
      this.showDetail = true
      this.switchTab(this.activeTab)
    },

    switchTab (key) {
      this.activeTab = key
      if (key === 'fearGreed' && this.fgData.composite_score === undefined) this.fetchFearGreed()
      if (key === 'policy' && !this.policyData.data) this.fetchPolicy()
      if (key === 'sectorHistory' && this.shData.length === 0) this.fetchSectorHistory()
      // macroData tab uses already-loaded data, no extra fetch needed
    },

    async fetchFearGreedCard () {
      if (this.isDestroyed) return
      try {
        const res = await request({ url: '/api/shichang/china-fear-greed', method: 'GET', timeout: 30000 })
        const body = res?.data || res
        const data = body?.data || body || {}
        this.fgCached = data
        // 同步到弹窗数据
        if (this.fgData.composite_score === undefined) this.fgData = data
      } catch (e) {
        console.error('[贪恐指数卡片] 获取失败:', e)
      }
    },

    async fetchFearGreed () {
      this.fgLoading = true
      try {
        const res = await request({ url: '/api/shichang/china-fear-greed', method: 'GET', timeout: 30000 })
        const body = res?.data || res
        this.fgData = body?.data || body || {}
        this.fgCached = this.fgData
      } catch (e) {
        console.error('[贪婪恐惧] 获取失败:', e)
      } finally {
        this.fgLoading = false
      }
    },

    async fetchPolicy () {
      this.policyLoading = true
      try {
        const res = await request({ url: '/api/shichang/china-policy', method: 'GET', timeout: 30000 })
        const body = res?.data || res
        this.policyData = body || {}
      } catch (e) {
        console.error('[政策] 获取失败:', e)
      } finally {
        this.policyLoading = false
      }
    },

    async fetchSectorHistory () {
      this.shLoading = true
      this.shPage = 1
      try {
        const res = await request({
          url: '/api/shichang/sector-history',
          method: 'GET',
          params: { type: this.shBoardType, days: this.shDays },
          timeout: 30000
        })
        const body = res?.data || res
        this.shData = body?.data || []
      } catch (e) {
        console.error('[板块历史] 获取失败:', e)
      } finally {
        this.shLoading = false
      }
    },

    // ====== 工具方法 ======

    pickValue (record, candidates) {
      for (const col of candidates) {
        if (record[col] !== undefined && record[col] !== null && record[col] !== '') {
          const v = parseFloat(record[col])
          if (!isNaN(v)) return v
        }
      }
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
    },

    calcIndicatorScore (logic, value, change) {
      switch (logic) {
        case 'higher_better':
          if (change !== null) return Math.round(Math.max(10, Math.min(90, 50 + change * 20)))
          return Math.round(Math.max(10, Math.min(90, value > 0 ? 60 : 40)))
        case 'lower_better':
          if (change !== null) return Math.round(Math.max(10, Math.min(90, 50 - change * 20)))
          return 50
        case 'higher_better_50':
          return Math.round(Math.max(10, Math.min(90, 50 + (value - 50) * 2)))
        case 'lower_better_2_3':
          if (value >= 2 && value <= 3) return 70
          if (value < 2) return Math.round(Math.max(20, 60 - (2 - value) * 20))
          return Math.round(Math.max(20, 60 - (value - 3) * 20))
        default:
          return 50
      }
    },

    getScoreClass (score) {
      if (score >= 70) return 'score-high'
      if (score >= 40) return 'score-neutral'
      return 'score-low'
    },
    getOverallLabel (score) {
      if (score >= 80) return '过热'
      if (score >= 70) return '偏强'
      if (score >= 55) return '中性偏强'
      if (score >= 45) return '中性'
      if (score >= 30) return '中性偏弱'
      return '偏弱'
    },
    getOverallDesc (score) {
      if (score >= 70) return '宏观经济指标整体向好，政策环境偏暖，有利于资本市场'
      if (score >= 55) return '宏观经济运行平稳，主要指标处于合理区间'
      if (score >= 45) return '宏观数据方向不明，关注后续政策发力'
      if (score >= 30) return '部分宏观指标走弱，关注逆周期调节政策'
      return '宏观经济面临压力，期待政策加码托底'
    },
    getSentimentClass (score) {
      if (score >= 70) return 'sentiment-bull'
      if (score >= 40) return 'sentiment-neutral'
      return 'sentiment-bear'
    },

    getFgClass (score) {
      if (score <= 25) return 'fg-extreme-fear'
      if (score <= 40) return 'fg-fear'
      if (score <= 60) return 'fg-neutral'
      if (score <= 75) return 'fg-greed'
      return 'fg-extreme-greed'
    },

    formatPct (val) {
      const v = parseFloat(val)
      if (isNaN(v)) return '--'
      return (v >= 0 ? '+' : '') + v.toFixed(2) + '%'
    },
    formatAmount (val) {
      const v = parseFloat(val)
      if (isNaN(v)) return '--'
      if (Math.abs(v) >= 1e8) return (v / 1e8).toFixed(2) + '亿'
      if (Math.abs(v) >= 1e4) return (v / 1e4).toFixed(2) + '万'
      return v.toFixed(2)
    },

    // ====== 宏观数据详情 Tab 工具方法 ======

    getMacroRecords (ind) {
      const data = this.macroData[ind.key]
      if (!data || !data.latest) return []
      return data.latest
    },

    getMacroColumns (ind) {
      const data = this.macroData[ind.key]
      if (!data || !data.columns || data.columns.length === 0) return []
      return data.columns
    },

    getMacroLatestValue (ind) {
      const records = this.getMacroRecords(ind)
      if (records.length === 0) return null
      const latest = records[records.length - 1]
      return this.pickValue(latest, ind.valueCols)
    },

    formatMacroValue (value, ind) {
      if (value === null || value === undefined) return '--'
      if (ind.divisor) return (value / ind.divisor).toFixed(2)
      return this.formatDisplay(value, ind.key)
    },

    isNumericCol (col, row) {
      const v = row[col]
      if (v === null || v === undefined || v === '') return false
      const num = parseFloat(v)
      return !isNaN(num) && String(v).length < 20
    },

    getNumericClass (val) {
      const v = parseFloat(val)
      if (isNaN(v)) return ''
      if (v > 0) return 'up'
      if (v < 0) return 'down'
      return ''
    },

    formatTableCell (val, col) {
      if (val === null || val === undefined) return ''
      const s = String(val)
      // 日期列直接返回
      if (/date|month|quarter|period|日期|月份|季度/i.test(col)) return s
      // 数值列格式化
      const v = parseFloat(s)
      if (!isNaN(v) && s.length < 20) {
        // 大额资金
        if (/amount|净流入|增量|融资|差额|balance|increment/i.test(col)) {
          if (Math.abs(v) >= 1e8) return (v / 1e8).toFixed(2) + '亿'
          if (Math.abs(v) >= 1e4) return (v / 1e4).toFixed(2) + '万'
        }
        return v.toFixed(2)
      }
      return s
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
.header-right { display: flex; align-items: center; gap: 8px; }
.update-time { font-size: 11px; color: #999; }
.btn-detail {
  padding: 4px 10px;
  background: #f0f9eb;
  color: #67c23a;
  border: 1px solid #c2e7b0;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
  font-weight: 500;
}
.btn-detail:hover { background: #e1f3d8; }
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
.macro-content { padding: 16px; }

/* === 整体评分（复用外围市场样式） === */
.overall-score {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 16px;
  margin-bottom: 16px;
  background: var(--header-bg, #f8fafc);
  border-radius: 10px;
  border: 1px solid var(--border-color, #e2e8f0);
}
.score-ring { position: relative; width: 100px; height: 100px; flex-shrink: 0; }
.score-ring svg { width: 100%; height: 100%; transform: rotate(-90deg); }
.ring-bg { fill: none; stroke: #e2e8f0; stroke-width: 8; }
.ring-progress { fill: none; stroke-width: 8; stroke-linecap: round; transition: stroke-dasharray 0.6s ease; }
.ring-progress.score-high { stroke: #10b981; }
.ring-progress.score-neutral { stroke: #f59e0b; }
.ring-progress.score-low { stroke: #ef4444; }
.score-inner {
  position: absolute; top: 50%; left: 50%;
  transform: translate(-50%, -50%); text-align: center;
}
.score-value {
  display: block; font-size: 28px; font-weight: 700; line-height: 1;
  color: var(--text-color, #1e293b);
}
.score-label { display: block; font-size: 11px; color: #64748b; margin-top: 2px; }
.score-desc { flex: 1; }
.desc-title { font-size: 15px; font-weight: 700; margin-bottom: 6px; }
.desc-title.score-high { color: #10b981; }
.desc-title.score-neutral { color: #f59e0b; }
.desc-title.score-low { color: #ef4444; }
.desc-detail { font-size: 13px; color: #64748b; line-height: 1.5; }

/* === 指标列表 === */
.macro-list { display: flex; flex-direction: column; gap: 6px; }
.macro-row {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px; background: var(--row-bg, #fafbfc);
  border-radius: 8px; border: 1px solid transparent; transition: all 0.2s ease;
}
.macro-row:hover { background: #f0f4f8; }
.macro-icon { font-size: 20px; width: 28px; text-align: center; flex-shrink: 0; }
.macro-info { flex: 1; min-width: 0; }
.macro-name {
  font-size: 13px; font-weight: 500; color: var(--text-color, #334155);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.macro-detail { display: flex; align-items: center; gap: 8px; margin-top: 2px; }
.macro-value-text {
  font-size: 14px; font-weight: 600; color: var(--text-color, #1e293b);
  font-family: 'SF Mono', 'Menlo', monospace;
}
.macro-change {
  font-size: 11px; font-weight: 600; font-family: 'SF Mono', 'Menlo', monospace;
}
.change-up { color: #f56c6c; }
.change-down { color: #67c23a; }
.macro-period { font-size: 11px; color: #c0c4cc; }

/* 情绪条 */
.macro-score-col {
  display: flex; align-items: center; gap: 8px; width: 120px; flex-shrink: 0;
}
.sentiment-bar-wrap {
  flex: 1; height: 6px; background: #e2e8f0; border-radius: 3px; overflow: hidden;
}
.sentiment-bar {
  height: 100%; border-radius: 3px; transition: width 0.4s ease;
}
.sentiment-bar.sentiment-bull { background: linear-gradient(90deg, #10b981, #34d399); }
.sentiment-bar.sentiment-neutral { background: linear-gradient(90deg, #f59e0b, #fbbf24); }
.sentiment-bar.sentiment-bear { background: linear-gradient(90deg, #ef4444, #f87171); }
.sentiment-value {
  font-size: 13px; font-weight: 700; width: 28px; text-align: right;
  font-family: 'SF Mono', 'Menlo', monospace;
}
.sentiment-value.sentiment-bull { color: #10b981; }
.sentiment-value.sentiment-neutral { color: #f59e0b; }
.sentiment-value.sentiment-bear { color: #ef4444; }

.macro-trend-badge {
  font-size: 10px; font-weight: 600; padding: 2px 6px; border-radius: 3px; flex-shrink: 0;
}
.trend-up { background: #fef0f0; color: #f56c6c; }
.trend-down { background: #f0f9eb; color: #67c23a; }
.trend-neutral { background: #f4f4f5; color: #909399; }

.empty-state { text-align: center; padding: 24px; color: #c0c4cc; font-size: 14px; }

/* ====== 宏观数据详情 Tab ====== */
.macro-detail-wrap { display: flex; flex-direction: column; gap: 16px; }
.macro-detail-section {
  background: #fafbfc; border-radius: 10px; border: 1px solid #ebeef5; overflow: hidden;
}
.macro-detail-header {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 16px; background: #f0f2f5; border-bottom: 1px solid #ebeef5;
}
.macro-detail-icon { font-size: 20px; }
.macro-detail-name { font-size: 14px; font-weight: 600; color: #333; flex: 1; }
.macro-detail-latest {
  font-size: 20px; font-weight: 700; color: #1e293b;
  font-family: 'SF Mono', 'Menlo', monospace;
}
.macro-detail-na { color: #c0c4cc; }
.macro-detail-table-wrap { overflow-x: auto; padding: 0; }
.macro-detail-section .data-table { margin: 0; }
.macro-detail-section .data-table th { background: #fff; }
.macro-detail-empty { text-align: center; padding: 16px; color: #c0c4cc; font-size: 13px; }

/* ====== 详情弹窗 ====== */
.modal-overlay {
  position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0, 0, 0, 0.5); display: flex;
  align-items: center; justify-content: center; z-index: 1000;
}
.modal-container {
  background: #fff; border-radius: 8px; width: 95vw; max-width: 1200px;
  max-height: 85vh; display: flex; flex-direction: column;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.2);
}
.modal-header {
  display: flex; align-items: center; gap: 16px;
  padding: 14px 20px; border-bottom: 1px solid #ebeef5; flex-shrink: 0;
}
.modal-header h3 { margin: 0; font-size: 16px; font-weight: 600; color: #333; white-space: nowrap; }
.modal-tabs { display: flex; gap: 4px; flex: 1; }
.tab-btn {
  padding: 6px 16px; background: #f4f4f5; color: #666; border: 1px solid #e4e7ed;
  border-radius: 4px; cursor: pointer; font-size: 13px; transition: all 0.2s;
}
.tab-btn.active { background: #ecf5ff; color: #409eff; border-color: #b3d8ff; font-weight: 600; }
.tab-btn:hover:not(.active) { background: #eaeaeb; }
.modal-close {
  background: none; border: none; font-size: 24px; cursor: pointer;
  color: #999; line-height: 1; padding: 0 4px;
}
.modal-close:hover { color: #333; }
.modal-body { flex: 1; overflow-y: auto; padding: 20px; }
.loading-state { text-align: center; padding: 40px; color: #999; font-size: 14px; }
.tab-content { min-height: 200px; }

/* 贪婪恐惧 */
.fg-content { }
.fg-overall {
  display: flex; align-items: center; gap: 24px;
  padding: 20px; background: #f8fafc; border-radius: 10px; margin-bottom: 20px;
}
.fg-score-big {
  font-size: 48px; font-weight: 800; width: 100px; text-align: center;
  font-family: 'SF Mono', 'Menlo', monospace;
}
.fg-extreme-fear { color: #dc2626; }
.fg-fear { color: #ea580c; }
.fg-neutral { color: #ca8a04; }
.fg-greed { color: #16a34a; }
.fg-extreme-greed { color: #059669; }
.fg-label { font-size: 18px; font-weight: 600; color: #333; }
.fg-bar-wrap { flex: 1; }
.fg-bar { height: 12px; background: #e2e8f0; border-radius: 6px; overflow: hidden; }
.fg-bar-fill { height: 100%; border-radius: 6px; transition: width 0.6s; }
.fg-bar-fill.fg-extreme-fear { background: linear-gradient(90deg, #dc2626, #ef4444); }
.fg-bar-fill.fg-fear { background: linear-gradient(90deg, #ea580c, #f97316); }
.fg-bar-fill.fg-neutral { background: linear-gradient(90deg, #ca8a04, #eab308); }
.fg-bar-fill.fg-greed { background: linear-gradient(90deg, #16a34a, #22c55e); }
.fg-bar-fill.fg-extreme-greed { background: linear-gradient(90deg, #059669, #10b981); }
.fg-bar-labels { display: flex; justify-content: space-between; font-size: 11px; color: #999; margin-top: 4px; }
.fg-indicators { display: flex; flex-direction: column; gap: 10px; }
.fg-indicator-row {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 14px; background: #fafbfc; border-radius: 8px;
}
.fg-ind-name { width: 120px; font-size: 13px; font-weight: 500; color: #333; flex-shrink: 0; }
.fg-ind-bar-wrap { flex: 1; }
.fg-ind-bar { height: 8px; background: #e2e8f0; border-radius: 4px; overflow: hidden; }
.fg-ind-bar-fill { height: 100%; border-radius: 4px; transition: width 0.4s; }
.fg-ind-bar-fill.fg-extreme-fear, .fg-ind-bar-fill.fg-fear { background: #ef4444; }
.fg-ind-bar-fill.fg-neutral { background: #f59e0b; }
.fg-ind-bar-fill.fg-greed, .fg-ind-bar-fill.fg-extreme-greed { background: #10b981; }
.fg-ind-score {
  width: 40px; text-align: right; font-size: 14px; font-weight: 700;
  font-family: 'SF Mono', 'Menlo', monospace;
}
.fg-ind-detail { flex: 1; font-size: 12px; color: #909399; min-width: 0; }

/* 政策 */
.policy-ai-summary {
  background: linear-gradient(135deg, #f5f7fa 0%, #e8ecf1 100%);
  border-radius: 8px; padding: 16px; margin-bottom: 16px;
  border-left: 3px solid #409eff;
}
.policy-sentiment { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.sentiment-label { font-size: 13px; color: #606266; }
.sentiment-value { font-size: 14px; font-weight: 700; color: #303133; }
.policy-summary-text { font-size: 13px; color: #333; line-height: 1.6; margin: 0 0 12px; }
.policy-outlook, .policy-advice { margin-top: 10px; }
.policy-outlook h4, .policy-advice h4 { margin: 0 0 6px; font-size: 13px; color: #333; }
.policy-outlook p, .policy-advice p { margin: 0; font-size: 12px; color: #606266; line-height: 1.5; }
.policy-impacts { margin-bottom: 20px; }
.policy-impacts h4, .policy-list h4 { margin: 0 0 12px; font-size: 14px; color: #333; }
.policy-impact-detail { font-size: 12px; color: #909399; margin-left: 4px; }
.policy-impact-row {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 12px; background: #fafbfc; border-radius: 6px; margin-bottom: 6px;
}
.impact-keyword { font-weight: 600; color: #333; min-width: 80px; }
.impact-direction { font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 3px; }
.impact-direction.up { background: #fef0f0; color: #f56c6c; }
.impact-direction.down { background: #f0f9eb; color: #67c23a; }
.impact-sector { font-size: 12px; color: #909399; }
.policy-item-row {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 8px 0; border-bottom: 1px solid #f0f0f0;
}
.policy-time { font-size: 11px; color: #c0c4cc; white-space: nowrap; min-width: 60px; }
.policy-title { flex: 1; font-size: 13px; color: #333; line-height: 1.4; }
.policy-keywords { display: flex; gap: 4px; flex-shrink: 0; }
.policy-kw-tag {
  font-size: 10px; padding: 1px 6px; background: #ecf5ff; color: #409eff;
  border-radius: 3px; white-space: nowrap;
}

/* 板块历史 */
.sh-controls { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; }
.board-type-btn {
  padding: 6px 14px; background: #f4f4f5; color: #666; border: 1px solid #e4e7ed;
  border-radius: 4px; cursor: pointer; font-size: 13px;
}
.board-type-btn.active { background: #ecf5ff; color: #409eff; border-color: #b3d8ff; font-weight: 600; }
.sh-days-select {
  padding: 5px 10px; border: 1px solid #dcdfe6; border-radius: 4px;
  font-size: 13px; color: #333; background: #fff;
}
.sh-table-wrap { overflow-x: auto; }
.data-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.data-table th, .data-table td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #ebeef5; }
.data-table th { background: #f8f9fc; font-weight: 600; color: #333; white-space: nowrap; }
.data-table tbody tr:hover { background-color: #f5f7fa; }
.up { color: #f56c6c; }
.down { color: #67c23a; }
.sh-pagination {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 0; margin-top: 8px;
}
.sh-pagination button {
  padding: 4px 12px; background: #ecf5ff; color: #409eff; border: 1px solid #b3d8ff;
  border-radius: 4px; cursor: pointer; font-size: 12px;
}
.sh-pagination button:disabled { opacity: 0.5; cursor: not-allowed; }
.sh-pagination span { font-size: 13px; color: #666; }
.sh-total { font-size: 12px; color: #999; margin-left: auto; }

@media (max-width: 768px) {
  .overall-score { flex-direction: column; text-align: center; }
  .macro-score-col { width: 80px; }
  .fg-overall { flex-direction: column; text-align: center; }
  .fg-indicator-row { flex-wrap: wrap; }
  .fg-ind-name { width: 100%; }
  .fg-ind-detail { width: 100%; }
}
</style>
