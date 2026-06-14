<template>
  <div class="shichang-container" :class="isLightTheme ? 'theme-light' : 'theme-dark'">
    <div class="main-content-full">
      <div class="main-body">
        <main class="content-area">
          <!-- 市场总览卡片群（顶部 8 小格，独立于卡片系统） -->
          <section class="market-overview">
            <div class="overview-card">
              <h4>上证指数</h4>
              <p class="value">
                {{ overview.sse.index }}
                <span :class="overview.sse.change >= 0 ? 'up' : 'down'"> {{ formatChange(overview.sse.change) }}% </span>
              </p>
            </div>
            <div class="overview-card">
              <h4>深证成指</h4>
              <p class="value">
                {{ overview.szse.index }}
                <span :class="overview.szse.change >= 0 ? 'up' : 'down'"> {{ formatChange(overview.szse.change) }}% </span>
              </p>
            </div>
            <div class="overview-card">
              <h4>创业板指</h4>
              <p class="value">
                {{ overview.cyse.index }}
                <span :class="overview.cyse.change >= 0 ? 'up' : 'down'"> {{ formatChange(overview.cyse.change) }}% </span>
              </p>
            </div>
            <div class="overview-card">
              <h4>北证50</h4>
              <p class="value">
                {{ overview.bzse.index }}
                <span :class="overview.bzse.change >= 0 ? 'up' : 'down'"> {{ formatChange(overview.bzse.change) }}% </span>
              </p>
            </div>
            <div class="overview-card">
              <h4>市场热度</h4>
              <p class="value" :class="getHeatClass(overview.heat)">{{ overview.heat }}</p>
            </div>
            <div class="overview-card">
              <h4>涨停 / 跌停</h4>
              <p class="value">{{ overview.limitUp }} / {{ overview.limitDown }}</p>
            </div>
            <div class="overview-card">
              <h4>北向资金</h4>
              <p class="value">{{ overview.northBound }}亿</p>
            </div>
            <div class="overview-card">
              <h4>情绪指数</h4>
              <p class="value">
                {{ overview.emotionIndex }}
                <small>{{ getEmotionLevel(overview.emotionIndex) }}</small>
              </p>
            </div>
          </section>

          <!-- 动态卡片网格 — 由后端 /api/shichang/cards 驱动 -->
          <section class="module-grid">
            <component
              v-for="card in activeCards"
              :is="card.component"
              :key="card.id"
              v-bind="card.props || {}"
            />
          </section>
        </main>
      </div>
    </div>
  </div>
</template>

<script>
import request from '@/utils/request'

// 卡片组件注册表 — 新增卡片只需在这里加一行
import AiAnalysisCard from './AiAnalysisCard.vue'
// import EmotionCycleCard from './EmotionCycleCard.vue'
import DailyScanCard from './DailyScanCard.vue'
import MacroCard from './MacroCard.vue'
import PeripheralMarketCard from './PeripheralMarketCard.vue'
import StreakCard from './StreakCard.vue'
import DragonTigerCard from './DragonTigerCard.vue'
import HotListCard from './HotListCard.vue'
import StrongStocksCard from './StrongStocksCard.vue'

const CARD_COMPONENTS = {
  'ai_analysis': AiAnalysisCard,
//  'emotion_cycle': EmotionCycleCard,
  'daily_scan': DailyScanCard,           // 板块每日扫描
  'macro': MacroCard,
  'peripheral': PeripheralMarketCard,
  'streak': StreakCard,
  'dragon_tiger': DragonTigerCard,
  'hot_list': HotListCard,
  'strong_stocks': StrongStocksCard
}

// ==================== 腾讯行情（前端直连，10秒刷新） ====================

async function fetchIndexFromTencent () {
  const codes = 'sh000001,sz399001,sz399006,bj899050'
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 15000)
  try {
    const res = await fetch(`https://qt.gtimg.cn/q=${codes}`, { signal: controller.signal })
    if (!res.ok) throw new Error(`腾讯行情请求失败: ${res.status}`)
    const buf = await res.arrayBuffer()
    const text = new TextDecoder('gbk').decode(buf)
    return parseTencentIndex(text)
  } finally {
    clearTimeout(timer)
  }
}

function parseTencentIndex (raw) {
  const result = {
    sse: { index: '--', change: 0 },
    szse: { index: '--', change: 0 },
    cyse: { index: '--', change: 0 },
    bzse: { index: '--', change: 0 }
  }
  const lines = raw.split(';').filter(l => l.includes('='))
  for (const line of lines) {
    const match = line.match(/"(.+)"/)
    if (!match) continue
    const fields = match[1].split('~')
    if (fields.length < 35) continue
    const name = fields[1]
    const price = fields[3]
    const pct = parseFloat(fields[32]) || 0
    if (name.includes('上证')) result.sse = { index: price, change: pct }
    else if (name.includes('深证')) result.szse = { index: price, change: pct }
    else if (name.includes('创业')) result.cyse = { index: price, change: pct }
    else if (name.includes('北证')) result.bzse = { index: price, change: pct }
  }
  return result
}

// ==================== 组件 ====================

export default {
  name: 'ShichangDashboard',
  data () {
    return {
      isLightTheme: true,
      isDestroyed: false,
      indexFetching: false,
      indexTimer: null,
      overview: {
        sse: { index: '--', change: 0 },
        szse: { index: '--', change: 0 },
        cyse: { index: '--', change: 0 },
        bzse: { index: '--', change: 0 },
        heat: '未知', limitUp: 0, limitDown: 0,
        northBound: 0, emotionIndex: 50
      },
      // 动态卡片列表
      cardList: [],
      activeCards: []
    }
  },
  methods: {
    formatChange (v) {
      const n = parseFloat(v)
      if (isNaN(n)) return '0.00'
      return (n >= 0 ? '+' : '') + n.toFixed(2)
    },
    getHeatClass (h) { return h === '火热' ? 'up' : h === '寒冷' ? 'down' : '' },
    getEmotionLevel (i) { return i < 30 ? '低迷' : i < 70 ? '温和' : '高涨' },

    // 大盘指数（腾讯直连）
    async fetchIndex () {
      if (this.isDestroyed || this.indexFetching) return
      this.indexFetching = true
      try {
        let idx = null
        for (let i = 0; i <= 1; i++) {
          try { idx = await fetchIndexFromTencent(); break } catch (e) {
            if (i < 1) await new Promise(resolve => setTimeout(resolve, 3000))
          }
        }
        if (this.isDestroyed || !idx) return
        this.overview.sse = idx.sse
        this.overview.szse = idx.szse
        this.overview.cyse = idx.cyse
        this.overview.bzse = idx.bzse
      } finally {
        this.indexFetching = false
      }
    },

    // 总览数据（涨停/跌停/北向/情绪）
    async fetchOverview () {
      try {
        const resp = await request({ url: '/api/shichang/cards/overview', method: 'GET' })
        const d = resp.data || resp
        if (!d || this.isDestroyed) return
        this.overview.limitUp = d.limitUp || 0
        this.overview.limitDown = d.limitDown || 0
        this.overview.northBound = typeof d.northBound === 'number' ? d.northBound.toFixed(2) : String(d.northBound || 0)
        this.overview.emotionIndex = d.emotionIndex || 50
        // 热度：前端根据涨跌比重新算
        const up = d.upCount || 0
        const down = d.downCount || 0
        const total = up + down
        const ratio = total > 0 ? up / total : 0.5
        if (ratio > 0.7) this.overview.heat = '火热'
        else if (ratio > 0.55) this.overview.heat = '偏暖'
        else if (ratio > 0.45) this.overview.heat = '中性'
        else if (ratio > 0.3) this.overview.heat = '偏冷'
        else this.overview.heat = '寒冷'
      } catch (e) {
        console.error('[总览] 刷新失败:', e)
      }
    },

    // 从后端获取卡片列表，合并本地组件注册
    async fetchCardList () {
      try {
        const resp = await request({ url: '/api/shichang/cards', method: 'GET' })
        const list = (resp && resp.data) || []
        this.cardList = list

        // 合并：后端列表 ∩ 本地组件
        this.activeCards = list
          .filter(c => CARD_COMPONENTS[c.id])
          .map(c => ({
            id: c.id,
            component: CARD_COMPONENTS[c.id],
            props: c.id === 'streak' ? { brokenBoard: this.overview.limitUp } : {}
          }))

        console.log('[卡片] 已加载', this.activeCards.length, '个卡片:', this.activeCards.map(c => c.id).join(', '))
      } catch (e) {
        console.error('[卡片] 获取卡片列表失败，使用默认顺序:', e)
        // 降级：用默认列表
        const defaults = ['ai_analysis', 'emotion_cycle', 'macro', 'peripheral', 'streak', 'dragon_tiger', 'hot_list', 'strong_stocks']
        this.activeCards = defaults
          .filter(id => CARD_COMPONENTS[id])
          .map(id => ({ id, component: CARD_COMPONENTS[id], props: {} }))
      }
    }
  },

  async mounted () {
    // 先拉总览（给 overview 小卡片用），再拉卡片列表
    await Promise.all([this.fetchIndex(), this.fetchOverview()])
    await this.fetchCardList()

    this.indexTimer = setInterval(() => {
      this.fetchIndex()
      this.fetchOverview()
    }, 60000)
  },

  activated () {
    // keep-alive: 从其它页面切回来时重启定时器
    this.isDestroyed = false
    if (!this.indexTimer) {
      this.fetchIndex()
      this.fetchOverview()
      this.indexTimer = setInterval(() => {
        this.fetchIndex()
        this.fetchOverview()
      }, 60000)
    }
  },

  deactivated () {
    // keep-alive: 离开页面时暂停定时器
    this.isDestroyed = true
    if (this.indexTimer) {
      clearInterval(this.indexTimer)
      this.indexTimer = null
    }
  },

  beforeDestroy () {
    this.isDestroyed = true
    if (this.indexTimer) clearInterval(this.indexTimer)
  }
}
</script>

<style scoped>
.shichang-container {
  font-family: 'Helvetica Neue', Arial, sans-serif;
  min-height: 100vh;
  background-color: var(--bg-color, #f5f7fa);
  color: var(--text-color, #333);
  transition: background-color 0.3s ease;
}
.theme-light {
  --bg-color: #ffffff; --text-color: #333333; --card-bg: #ffffff;
  --border-color: #ebeef5; --shadow: 0 2px 10px rgba(0,0,0,0.1);
  --header-bg: #f8f9fc; --row-bg: #fafbfc;
}
.theme-dark {
  --bg-color: #1a1a2e; --text-color: #e0e0e0; --card-bg: #16213e;
  --border-color: #2a2a4a; --shadow: 0 2px 10px rgba(0,0,0,0.3);
  --header-bg: #0f3460; --row-bg: #1a2744;
}
.theme-dark .module-header { background: #0f3460; border-color: #2a2a4a; }
.theme-dark .module-header h3 { color: #e0e0e0; }
.theme-dark .overview-card h4 { color: #a0a0a0; }
.main-content-full { display: flex; flex-direction: column; height: 100vh; }
.main-body { display: flex; flex: 1; overflow: hidden; }
.content-area { flex: 1; overflow-y: auto; padding: 8px; background-color: var(--bg-color); }
.market-overview { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 8px; margin-bottom: 8px; }
.overview-card {
  background: var(--card-bg); border-radius: 8px; padding: 10px;
  box-shadow: var(--shadow); text-align: center; border: 1px solid #dbdbdb;
}
.overview-card h4 { margin: 0 0 8px; font-size: 14px; color: #666; }
.overview-card .value { margin: 0; font-size: 18px; font-weight: 600; }
.overview-card .value small { font-size: 12px; color: #999; }
.up { color: #f56c6c; }
.down { color: #67c23a; }
.module-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; }
@media (max-width: 1200px) { .module-grid { grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); } }
@media (max-width: 768px) { .content-area { padding: 10px; } }
</style>
