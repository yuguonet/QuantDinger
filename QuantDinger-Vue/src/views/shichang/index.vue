<template>
  <div class="shichang-container" :class="isLightTheme ? 'theme-light' : 'theme-dark'">

    <div class="main-content-full">
      <div class="main-body">
        <main class="content-area">
          <!-- 市场总览卡片群 -->
          <section class="market-overview">
            <div class="overview-card">
              <h4>上证指数</h4>
              <p class="value">
                {{ marketData.sse.index }}
                <span :class="marketData.sse.change >= 0 ? 'up' : 'down'"> {{ formatChange(marketData.sse.change) }}% </span>
              </p>
            </div>
            <div class="overview-card">
              <h4>深证成指</h4>
              <p class="value">
                {{ marketData.szse.index }}
                <span :class="marketData.szse.change >= 0 ? 'up' : 'down'"> {{ formatChange(marketData.szse.change) }}% </span>
              </p>
            </div>
            <div class="overview-card">
              <h4>创业板指</h4>
              <p class="value">
                {{ marketData.cyse.index }}
                <span :class="marketData.cyse.change >= 0 ? 'up' : 'down'"> {{ formatChange(marketData.cyse.change) }}% </span>
              </p>
            </div>
            <div class="overview-card">
              <h4>北证50</h4>
              <p class="value">
                {{ marketData.bzse.index }}
                <span :class="marketData.bzse.change >= 0 ? 'up' : 'down'"> {{ formatChange(marketData.bzse.change) }}% </span>
              </p>
            </div>
            <div class="overview-card">
              <h4>市场热度</h4>
              <p class="value" :class="getHeatClass(marketData.heat)">{{ marketData.heat }}</p>
            </div>
            <div class="overview-card">
              <h4>涨停 / 跌停</h4>
              <p class="value">{{ marketData.limitUp }} / {{ marketData.limitDown }}</p>
            </div>
            <div class="overview-card">
              <h4>北向资金</h4>
              <p class="value">{{ marketData.northBound }}亿</p>
            </div>
            <div class="overview-card">
              <h4>情绪指数</h4>
              <p class="value">
                {{ marketData.emotionIndex }}
                <small>{{ getEmotionLevel(marketData.emotionIndex) }}</small>
              </p>
            </div>
          </section>

          <!-- 多列网格模块（各自独立接口） -->
          <section class="module-grid">
            <!-- AI市场分析 -->
            <div class="module-card">
              <header class="module-header">
                <h3>AI市场分析</h3>
                <div class="ai-label">AI分析</div>
              </header>
              <div class="module-content ai-analysis">
                <div class="confidence-score">
                  温和置信度 {{ aiAnalysis.confidence }}%
                  <span>建议: {{ aiAnalysis.advice }}</span>
                </div>
                <p class="market-phase">{{ aiAnalysis.phase }}</p>
                <div class="metrics-row">
                  <div class="metric-item">
                    <div class="metric-value">{{ aiAnalysis.temperature }}</div>
                    <div class="metric-label">市场温度<br /><small>{{ getLevelText(aiAnalysis.temperature, ['低温', '适中', '高温']) }}</small></div>
                  </div>
                  <div class="metric-item">
                    <div class="metric-value">{{ aiAnalysis.profitEffect }}</div>
                    <div class="metric-label">赚钱效应<br /><small>{{ getLevelText(aiAnalysis.profitEffect, ['差', '一般', '好']) }}</small></div>
                  </div>
                  <div class="metric-item">
                    <div class="metric-value">{{ aiAnalysis.riskScore }}</div>
                    <div class="metric-label">风险等级<br /><small>{{ aiAnalysis.riskLevel }}</small></div>
                  </div>
                </div>
                <div class="hot-sectors">
                  <h4>● 热门板块</h4>
                  <div class="sector-list">
                    <div
                      v-for="s in aiAnalysis.hotSectors"
                      :key="s.name"
                      class="sector-item"
                    >
                      <strong>{{ s.name }}</strong><br />{{ s.driver }}<br /><span class="score">{{ s.score }}</span>
                    </div>
                  </div>
                </div>
                <div class="operation-advice">
                  <h4>● 操作建议</h4>
                  <ul>
                    <li
                      v-for="(a, i) in aiAnalysis.operationAdvice"
                      :key="i"
                    >{{ a }}</li>
                  </ul>
                </div>
              </div>
            </div>

            <!-- 情绪周期 -->
            <div class="module-card">
              <header class="module-header">
                <h3>情绪周期</h3>
                <button @click="refreshEmotionChart" :disabled="chartLoading" class="btn-refresh">
                  {{ chartLoading ? '加载中...' : '刷新' }}
                </button>
              </header>
              <div class="module-content chart-container">
                <div ref="emotionChart" class="echart-wrapper"></div>
              </div>
            </div>

            <!-- 国内宏观数据 -->
            <MacroCard />

            <!-- 外围市场 -->
            <PeripheralMarketCard />

            <!-- 连板天梯 -->
            <StreakCard :brokenBoard="marketData.brokenBoard" />

            <!-- 龙虎榜 -->
            <DragonTigerCard />

            <!-- 同花顺热榜 -->
            <HotListCard />

            <!-- 强势股 -->
            <StrongStocksCard />
          </section>
        </main>
      </div>
    </div>
  </div>
</template>

<script>
import * as echarts from 'echarts'
import request from '@/utils/request'
import StreakCard from './StreakCard.vue'
import DragonTigerCard from './DragonTigerCard.vue'
import HotListCard from './HotListCard.vue'
import StrongStocksCard from './StrongStocksCard.vue'
import PeripheralMarketCard from './PeripheralMarketCard.vue'
import MacroCard from './MacroCard.vue'

// ==================== 数据源配置 ====================

/** 腾讯行情指数代码 */
// ==================== 大盘指数（前端直连腾讯，10秒刷新） ====================

/**
 * 获取大盘指数 — 前端浏览器直连腾讯行情接口，不走后端/代理
 * 腾讯返回 GBK 编码，需手动解码；接口允许 CORS (access-control-allow-origin: *)
 */
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

/**
 * 解析腾讯行情返回的文本
 * 腾讯 ~ 分隔字段:
 *  [0]市场  [1]名称  [2]代码  [3]当前价  [4]昨收  [5]开盘
 *  [6]-[28]五档+外盘内盘
 *  [29]??  [30]日期时间  [31]涨跌额  [32]涨跌幅%
 *  [33]最高  [34]最低
 */
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

    if (name.includes('上证')) {
      result.sse = { index: price, change: pct }
    } else if (name.includes('深证')) {
      result.szse = { index: price, change: pct }
    } else if (name.includes('创业')) {
      result.cyse = { index: price, change: pct }
    } else if (name.includes('北证')) {
      result.bzse = { index: price, change: pct }
    }
  }
  return result
}

/**
 * 计算情绪指数（综合涨停/跌停/涨跌比）
 * @returns {number} 情绪指数 0~100
 */
function calcEmotionIndex (limitUp, limitDown, upRatio) {
  const ztScore = Math.min((limitUp || 0) / 80, 1) * 40
  const dtScore = Math.min((limitDown || 0) / 40, 1) * 40
  const ratioScore = (upRatio ?? 0.5) * 20
  const emotion = Math.round(ztScore + ratioScore - dtScore + 20)
  return Math.max(0, Math.min(100, emotion))
}

/**
 * 一次性获取所有情绪数据（改用后端 /api/shichang/overview，不依赖 devServer proxy）
 */
async function fetchAllSentiment () {
  const d = await request({ url: '/api/shichang/overview', method: 'GET' })
  const data = d?.data || d

  const limitUp = data.limitUp || 0
  const limitDown = data.limitDown || 0
  const northBound = data.northBound || 0
  const upCount = data.upCount || 0
  const downCount = data.downCount || 0
  const total = upCount + downCount
  const upRatio = total > 0 ? upCount / total : 0.5

  const emotionIndex = calcEmotionIndex(limitUp, limitDown, upRatio)

  let heat
  if (upRatio > 0.7) heat = '火热'
  else if (upRatio > 0.55) heat = '偏暖'
  else if (upRatio > 0.45) heat = '中性'
  else if (upRatio > 0.3) heat = '偏冷'
  else heat = '寒冷'

  return {
    limitUp,
    limitDown,
    northBound: typeof northBound === 'number' ? northBound.toFixed(2) : String(northBound),
    heat,
    emotionIndex,
    brokenBoard: data.brokenBoard || 0
  }
}

// ==================== 组件 ====================

export default {
  name: 'AI综合看板',
  components: { StreakCard, DragonTigerCard, HotListCard, StrongStocksCard, PeripheralMarketCard, MacroCard },
  data () {
    return {
      isLightTheme: true,
      chartLoading: false,
      chartInstance: null,
      emotionHistory: [],
      isDestroyed: false,
      sectorSentiment: '',
      sectorSummary: '',
      hotSectorsData: null,
      sectorTrend: null,
      sectorPrediction: null,
      // 定时器
      indexTimer: null,
      sentimentTimer: null,
      emotionTimer: null,
      sectorTimer: null,
      trendTimer: null,
      predictionTimer: null,

      // 数据
      marketData: {
        sse: { index: '--', change: 0 },
        szse: { index: '--', change: 0 },
        cyse: { index: '--', change: 0 },
        bzse: { index: '--', change: 0 },
        heat: '未知',
        limitUp: 0,
        limitDown: 0,
        streakHeight: 0,
        brokenBoard: 0,
        northBound: 0,
        emotionIndex: 50
      },
      aiAnalysis: {
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
    // ==================== 大盘指数（腾讯接口，10秒刷新） ====================

    async fetchIndex () {
      if (this.isDestroyed || this.indexFetching) return
      this.indexFetching = true
      try {
        let idx = null
        for (let i = 0; i <= 1; i++) {
          try {
            idx = await fetchIndexFromTencent()
            break
          } catch (e) {
            console.error(`[指数] 第${i + 1}次失败:`, e.message)
            if (i < 1) await new Promise(resolve => setTimeout(resolve, 3000))
          }
        }
        if (this.isDestroyed || !idx) return
        this.marketData.sse = idx.sse
        this.marketData.szse = idx.szse
        this.marketData.cyse = idx.cyse
        this.marketData.bzse = idx.bzse
        console.log('[指数] 已刷新:', new Date().toLocaleTimeString(), idx)
      } finally {
        this.indexFetching = false
      }
    },

    // ==================== 情绪数据（东方财富，1分钟刷新） ====================

    async fetchSentiment () {
      if (this.isDestroyed) return
      try {
        const s = await fetchAllSentiment()
        if (this.isDestroyed) return
        this.marketData.limitUp = s.limitUp
        this.marketData.limitDown = s.limitDown
        this.marketData.northBound = s.northBound
        this.marketData.heat = s.heat
        this.marketData.emotionIndex = s.emotionIndex
        this.marketData.brokenBoard = s.brokenBoard
        this.buildAiFromOverview()
        console.log('[情绪] 已刷新:', new Date().toLocaleTimeString())
      } catch (e) {
        console.error('[情绪] 刷新失败:', e)
      }
    },

    // ==================== 热门板块（独立接口，3分钟刷新） ====================

    async fetchHotSectors () {
      if (this.isDestroyed) return
      try {
        const resp = await request({ url: '/api/shichang/hot-sectors', method: 'GET', timeout: 15000 })
        const data = resp?.data || resp
        if (this.isDestroyed || !data?.industry) return

        // 更新 AI 分析卡片的热门板块
        const analysis = data.analysis || {}
        const hotSectors = []

        // 行业主线
        if (analysis.top_industry) {
          for (const s of analysis.top_industry.slice(0, 3)) {
            hotSectors.push({
              name: s.name,
              driver: `${s.change_pct >= 0 ? '+' : ''}${(s.change_pct || 0).toFixed(2)}%`,
              score: Math.min(95, Math.max(30, 50 + (s.change_pct || 0) * 10))
            })
          }
        }

        // 概念板块
        if (analysis.top_concept) {
          for (const s of analysis.top_concept.slice(0, 2)) {
            hotSectors.push({
              name: s.name,
              driver: `概念 ${s.change_pct >= 0 ? '+' : ''}${(s.change_pct || 0).toFixed(2)}%`,
              score: Math.min(90, Math.max(30, 50 + (s.change_pct || 0) * 10))
            })
          }
        }

        // 涨停集中板块
        if (analysis.zt_concentrated) {
          for (const s of analysis.zt_concentrated.slice(0, 2)) {
            hotSectors.push({
              name: s.name,
              driver: `${s.zt_count}家涨停`,
              score: Math.min(95, 60 + (s.zt_count || 0) * 5)
            })
          }
        }

        if (hotSectors.length > 0) {
          this.aiAnalysis.hotSectors = hotSectors.slice(0, 6)
        }

        // 更新市场情绪指标
        if (analysis.sentiment) {
          this.sectorSentiment = analysis.sentiment
          this.sectorSummary = analysis.summary || ''
        }

        // 缓存原始数据供其他组件使用
        this.hotSectorsData = data

        console.log('[板块] 已刷新:', data.industry?.length, '行业 +', data.concept?.length, '概念')
      } catch (e) {
        console.error('[板块] 刷新失败:', e)
      }
    },

    // ==================== 板块趋势分析（独立接口，首次+每天刷新） ====================

    async fetchSectorTrend () {
      if (this.isDestroyed) return
      try {
        const resp = await request({ url: '/api/shichang/sector-trend', method: 'GET', params: { type: 'industry' }, timeout: 20000 })
        const data = resp?.data || resp
        if (this.isDestroyed || !data?.trend) return

        this.sectorTrend = data

        // 用趋势分析增强 AI 卡片的热门板块
        const trendItems = data.trend?.items || []
        const prediction = data.prediction?.candidates || []

        // 合并: 趋势强势 + 预测 Top → 更新 hotSectors
        const merged = []
        // 先加预测 Top3
        for (const c of prediction.slice(0, 3)) {
          merged.push({
            name: c.name,
            driver: c.reasons?.[0] || '综合预测',
            score: Math.round(c.composite_score || 50),
            source: 'prediction'
          })
        }
        // 再加强势趋势（去重）
        for (const t of trendItems.filter(x => x.score > 60).slice(0, 4)) {
          if (!merged.find(m => m.name === t.name)) {
            merged.push({
              name: t.name,
              driver: `${t.direction} | 胜率${t.win_rate}%`,
              score: Math.round(t.score),
              source: 'trend'
            })
          }
        }
        if (merged.length > 0) {
          this.aiAnalysis.hotSectors = merged.slice(0, 6)
        }

        // 重新生成操作建议（融合趋势数据）
        this.buildAiFromOverview()

        console.log('[趋势] 已刷新:', data.data_days, '天数据, 趋势', trendItems.length, '个板块')
      } catch (e) {
        console.error('[趋势] 刷新失败:', e)
      }
    },

    // ==================== 今日板块预测（独立接口，首次+30分钟刷新） ====================

    async fetchSectorPrediction () {
      if (this.isDestroyed) return
      try {
        const resp = await request({ url: '/api/shichang/sector-prediction', method: 'GET', timeout: 30000 })
        const data = resp?.data || resp
        if (this.isDestroyed || !data?.data) return

        this.sectorPrediction = data.data

        // 将预测结果注入 AI 卡片热门板块
        const industry = data.data.industry || {}
        const concept = data.data.concept || {}
        const candidates = [
          ...(industry.candidates || []).map(c => ({ ...c, _type: '行业' })),
          ...(concept.candidates || []).map(c => ({ ...c, _type: '概念' }))
        ]

        if (candidates.length > 0) {
          // 按综合分排序，取 Top
          candidates.sort((a, b) => (b.composite_score || 0) - (a.composite_score || 0))
          const topCandidates = candidates.slice(0, 4)

          // 合并到 hotSectors（prediction 优先，去重）
          const existing = this.aiAnalysis.hotSectors || []
          const merged = []

          for (const c of topCandidates) {
            merged.push({
              name: c.name,
              driver: c.reasons?.[0] || `${c._type}预测`,
              score: Math.round(c.composite_score || 50),
              source: 'prediction'
            })
          }

          // 追加非预测来源的已有板块（去重）
          for (const s of existing) {
            if (!merged.find(m => m.name === s.name) && merged.length < 6) {
              merged.push(s)
            }
          }

          this.aiAnalysis.hotSectors = merged.slice(0, 6)
        }

        // 重新生成操作建议
        this.buildAiFromOverview()

        console.log('[板块预测] 已刷新:', candidates.length, '个候选板块')
      } catch (e) {
        console.error('[板块预测] 刷新失败:', e)
      }
    },

    // ==================== AI 分析构建 ====================

    buildAiFromOverview () {
      const sc = this.marketData.sse.change
      const em = this.marketData.emotionIndex
      let phase = null
      let advice = null
      if (sc > 0.8) {
        phase = '强势上攻'
        advice = '持股待涨，可适当加仓'
      } else if (sc > 0.3) {
        phase = '震荡上行'
        advice = '持股待涨'
      } else if (sc > -0.3) {
        phase = '窄幅震荡'
        advice = '高抛低吸'
      } else if (sc > -0.8) {
        phase = '震荡下行'
        advice = '减仓观望'
      } else {
        phase = '弱势下跌'
        advice = '控制仓位，防御为主'
      }

      // 融合板块情绪（如果已拉取过 hot-sectors）
      if (this.sectorSentiment) {
        const sectorMap = { '全面做多': 80, '偏多震荡': 65, '多空拉锯': 50, '偏空调整': 35, '全面下跌': 20 }
        const sectorEm = sectorMap[this.sectorSentiment] || em
        // 综合情绪 = 加权平均（板块 40% + 涨跌停 60%）
        const blended = Math.round(sectorEm * 0.4 + em * 0.6)
        this.aiAnalysis.temperature = blended
      } else {
        this.aiAnalysis.temperature = em
      }

      this.aiAnalysis.phase = phase
      this.aiAnalysis.advice = advice
      this.aiAnalysis.profitEffect = Math.min(this.aiAnalysis.temperature, 70)
      this.aiAnalysis.riskScore = 100 - this.aiAnalysis.temperature
      this.aiAnalysis.riskLevel = this.aiAnalysis.temperature > 60 ? '低' : (this.aiAnalysis.temperature > 40 ? '中' : '高')
      this.aiAnalysis.confidence = Math.min(85, 50 + Math.abs(sc) * 30 + (this.aiAnalysis.temperature > 50 ? this.aiAnalysis.temperature - 50 : 50 - this.aiAnalysis.temperature))

      // 生成操作建议（融合板块数据）
      const advices = []
      const lup = this.marketData.limitUp || 0
      const ldn = this.marketData.limitDown || 0
      const north = parseFloat(this.marketData.northBound) || 0
      const broken = this.marketData.brokenBoard || 0

      if (lup > 50) advices.push(`涨停${lup}家，赚钱效应极强，顺势而为`)
      else if (lup > 30) advices.push(`涨停${lup}家，赚钱效应较好`)
      else if (lup > 10) advices.push(`涨停${lup}家，赚钱效应一般，精选个股`)
      else advices.push(`仅涨停${lup}家，赚钱效应差，建议观望`)

      if (ldn > 30) advices.push(`跌停${ldn}家，高位补跌风险大，回避高位股`)
      if (broken > 20) advices.push(`炸板${broken}家，封板成功率低，追涨需谨慎`)
      if (north > 50) advices.push(`北向净流入${north.toFixed(1)}亿，外资看好`)
      else if (north < -50) advices.push(`北向净流出${Math.abs(north).toFixed(1)}亿，注意风险`)

      // 板块趋势建议（如果数据已加载）
      if (this.sectorTrend?.prediction?.candidates?.length > 0) {
        const top = this.sectorTrend.prediction.candidates.slice(0, 2)
        const names = top.map(c => c.name).join('、')
        advices.push(`关注主线: ${names}`)
      }

      // 今日板块预测（独立接口，更精确的预测）
      if (this.sectorPrediction) {
        const indPred = this.sectorPrediction.industry || {}
        const conPred = this.sectorPrediction.concept || {}
        const allPred = [
          ...(indPred.candidates || []).map(c => ({ ...c, _type: '行业' })),
          ...(conPred.candidates || []).map(c => ({ ...c, _type: '概念' }))
        ]
        if (allPred.length > 0) {
          allPred.sort((a, b) => (b.composite_score || 0) - (a.composite_score || 0))
          const top3 = allPred.slice(0, 3)
          const predLine = top3.map(c => `${c.name}(${Math.round(c.composite_score || 0)}分)`).join('、')
          advices.push(`🎯 今日预测: ${predLine}`)
        }
      }
      if (this.sectorTrend?.trend?.items?.length > 0) {
        const strong = this.sectorTrend.trend.items.filter(x => x.score > 70).slice(0, 2)
        if (strong.length > 0) {
          advices.push(`持续强势板块: ${strong.map(s => s.name).join('、')}`)
        }
        const weak = this.sectorTrend.trend.items.filter(x => x.score < 30).slice(0, 2)
        if (weak.length > 0) {
          advices.push(`回避走弱板块: ${weak.map(s => s.name).join('、')}`)
        }
      }

      this.aiAnalysis.operationAdvice = advices.slice(0, 8)
    },

    // ==================== 工具方法 ====================

    formatChange (v) {
      const n = parseFloat(v)
      if (isNaN(n)) return '0.00'
      return (n >= 0 ? '+' : '') + n.toFixed(2)
    },

    // ==================== 情绪图表（后端历史接口，1分钟刷新） ====================

    async fetchEmotionHistory () {
      if (this.isDestroyed) return
      try {
        const d = await request({ url: '/api/shichang/emotion/history', method: 'GET', params: { hours: 4 } })
        if (this.isDestroyed) return
        this.emotionHistory = d.history || []
        this.renderChart()
        console.log('[情绪图表] 已刷新:', this.emotionHistory.length, '个点')
      } catch (e) {
        console.error('[情绪图表] 刷新失败:', e)
      }
    },

    refreshEmotionChart () {
      this.chartLoading = true
      this.fetchEmotionHistory().finally(() => {
        this.chartLoading = false
      })
    },

    getHeatClass (h) { return h === '火热' ? 'up' : h === '寒冷' ? 'down' : '' },
    getEmotionLevel (i) { return i < 30 ? '低迷' : i < 70 ? '温和' : '高涨' },
    getLevelText (v, l) { return v < 40 ? l[0] : v < 70 ? l[1] : l[2] },

    renderChart () {
      const el = this.$refs.emotionChart
      if (!el) return

      // 先清理旧的 resize 监听
      if (this.resizeHandler) {
        window.removeEventListener('resize', this.resizeHandler)
      }

      // 复用或创建实例
      if (this.chartInstance) {
        this.chartInstance.dispose()
      }
      this.chartInstance = echarts.init(el)

      const history = this.emotionHistory
      const xData = history.map(d => d.time)
      const yData = history.map(d => d.value)

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
          type: 'value',
          min: 0,
          max: 100,
          splitLine: { lineStyle: { type: 'dashed', color: '#eee' } }
        },
        series: [{
          data: yData,
          type: 'line',
          smooth: true,
          symbol: 'none',
          lineStyle: { width: 2, color: '#409eff' },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(64,158,255,0.25)' },
              { offset: 1, color: 'rgba(64,158,255,0.02)' }
            ])
          },
          markLine: {
            silent: true,
            symbol: 'none',
            data: [
              { yAxis: 70, lineStyle: { color: '#f56c6c', type: 'dashed' }, label: { show: true, formatter: '高涨', fontSize: 10, color: '#f56c6c' } },
              { yAxis: 30, lineStyle: { color: '#67c23a', type: 'dashed' }, label: { show: true, formatter: '低迷', fontSize: 10, color: '#67c23a' } }
            ]
          }
        }]
      })

      this.resizeHandler = () => this.chartInstance?.resize()
      window.addEventListener('resize', this.resizeHandler)
    }
  },

  mounted () {
    this.fetchIndex()
    this.fetchSentiment()
    this.fetchEmotionHistory()
    this.fetchHotSectors()
    this.fetchSectorTrend()
    this.fetchSectorPrediction()

    // 大盘指数：10秒刷新一次
    this.indexTimer = setInterval(() => this.fetchIndex(), 10000)

    // 情绪数据：5分钟刷新一次
    this.sentimentTimer = setInterval(() => this.fetchSentiment(), 300000)

    // 情绪图表：5分钟刷新一次
    this.emotionTimer = setInterval(() => this.fetchEmotionHistory(), 300000)

    // 热门板块：3分钟刷新一次
    this.sectorTimer = setInterval(() => this.fetchHotSectors(), 180000)

    // 趋势分析：30分钟刷新一次（数据变化慢）
    this.trendTimer = setInterval(() => this.fetchSectorTrend(), 1800000)

    // 板块预测：30分钟刷新一次
    this.predictionTimer = setInterval(() => this.fetchSectorPrediction(), 1800000)
  },

  beforeDestroy () {
    this.isDestroyed = true
    if (this.indexTimer) clearInterval(this.indexTimer)
    if (this.sentimentTimer) clearInterval(this.sentimentTimer)
    if (this.emotionTimer) clearInterval(this.emotionTimer)
    if (this.sectorTimer) clearInterval(this.sectorTimer)
    if (this.trendTimer) clearInterval(this.trendTimer)
    if (this.predictionTimer) clearInterval(this.predictionTimer)
    if (this.resizeHandler) window.removeEventListener('resize', this.resizeHandler)
    if (this.chartInstance) {
      this.chartInstance.dispose()
      this.chartInstance = null
    }
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
.theme-dark .data-table th { background: #0f3460; color: #e0e0e0; }
.theme-dark .data-table tbody tr:hover { background-color: rgba(255,255,255,0.05); }
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
.module-card {
  background: var(--card-bg); border-radius: 8px; box-shadow: var(--shadow);
  border: 1px solid #dbdbdb; padding: 8px; overflow: hidden;
}
.module-header {
  padding: 12px 16px; background: #f8f9fc; border-bottom: 1px solid var(--border-color);
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
.chart-container { height: 300px; position: relative; }
.echart-wrapper { width: 100%; height: 100%; }
@media (max-width: 1200px) { .module-grid { grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); } }
@media (max-width: 768px) { .content-area { padding: 10px; } }
</style>
