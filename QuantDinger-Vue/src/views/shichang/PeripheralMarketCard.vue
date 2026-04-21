<template>
  <div class="module-card peripheral-card">
    <header class="module-header">
      <h3>🌍 外围市场</h3>
      <div class="header-right">
        <span v-if="lastUpdate" class="update-time">{{ lastUpdate }}</span>
        <button @click="doRefresh" :disabled="loading" class="btn-refresh">
          {{ loading ? '加载中...' : '刷新' }}
        </button>
      </div>
    </header>
    <div class="module-content peripheral-content">
      <!-- 整体评分 -->
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
            外围市场整体评分
          </div>
          <div class="desc-detail">{{ getOverallDesc(overallScore) }}</div>
        </div>
      </div>

      <!-- 市场列表 — 始终显示 3 项 -->
      <div class="market-list">
        <div
          v-for="m in displayItems"
          :key="m.key"
          class="market-row"
          :class="{ 'anomaly-alert': m.anomaly, 'replaced-item': m.isReplaced }"
        >
          <div class="market-icon">{{ m.emoji }}</div>
          <div class="market-info">
            <div class="market-name">
              {{ m.name }}
              <span v-if="m.anomaly" class="anomaly-icon">⚠️</span>
              <span v-if="m.isReplaced" class="replaced-badge">替换</span>
            </div>
            <div class="market-detail">
              <span class="price">{{ m.priceText }}</span>
              <span class="change" :class="m.change >= 0 ? 'up' : 'down'">
                {{ m.change >= 0 ? '+' : '' }}{{ m.changeText }}%
              </span>
            </div>
          </div>
          <div class="market-score-col">
            <div class="sentiment-bar-wrap">
              <div class="sentiment-bar" :class="getSentimentClass(m.sentiment)"
                :style="{ width: m.sentiment + '%' }">
              </div>
            </div>
            <div class="sentiment-value" :class="getSentimentClass(m.sentiment)">
              {{ m.sentiment }}
            </div>
          </div>
          <div v-if="m.anomaly" class="anomaly-tag" :class="m.anomalyDir">
            {{ m.anomalyDir === 'bullish' ? '📈 看多' : '📉 看空' }}
          </div>
        </div>
      </div>

      <!-- 空状态 -->
      <div v-if="!loading && displayItems.length === 0" class="empty-state">
        <span>暂无数据</span>
      </div>
    </div>
  </div>
</template>

<script>
import request from '@/utils/request'

// 最多显示条目数
const MAX_DISPLAY = 3
// 异动检测阈值（百分比）
const ANOMALY_THRESHOLD = 2.0
// 默认三项 key
const DEFAULT_KEYS = ['sent_fear_greed', 'sent_vix', 'sent_dxy']
// 定时轮询间隔（ms）
const POLL_INTERVAL = 60 * 1000

export default {
  name: 'PeripheralMarketCard',
  data () {
    return {
      loading: false,
      lastUpdate: null,
      overallScore: 50,
      allMarketItems: [],
      isDestroyed: false,
      prevData: {},
      abortController: null,
      pollTimer: null
    }
  },
  computed: {
    displayItems () {
      const all = this.allMarketItems
      if (!all || all.length === 0) return []

      const anomalyPool = all
        .filter(i => i.anomaly && !DEFAULT_KEYS.includes(i.key))
        .sort((a, b) => Math.abs(b.change) - Math.abs(a.change))

      const defaults = DEFAULT_KEYS
        .map(k => all.find(i => i.key === k))
        .filter(Boolean)

      const result = [...defaults.slice(0, MAX_DISPLAY).map(d => ({ ...d, isReplaced: false }))]

      while (result.length < MAX_DISPLAY) {
        const fill = all.find(i => !result.some(r => r.key === i.key))
        if (!fill) break
        result.push({ ...fill, isReplaced: false })
      }
      result.length = Math.min(result.length, MAX_DISPLAY)

      for (let i = 0; i < anomalyPool.length && i < MAX_DISPLAY; i++) {
        const replaceIdx = result.length - 1 - i
        if (replaceIdx >= 0) {
          result[replaceIdx] = { ...anomalyPool[i], isReplaced: true }
        }
      }

      return result
    }
  },
  mounted () {
    // 启动：读缓存
    this.fetchCache()
    // 定时器：轮询比对时间戳，变了才刷新显示
    this.pollTimer = setInterval(() => this.pollCache(), POLL_INTERVAL)
  },
  beforeDestroy () {
    this.isDestroyed = true
    if (this.pollTimer) {
      clearInterval(this.pollTimer)
      this.pollTimer = null
    }
    if (this.abortController) {
      this.abortController.abort()
      this.abortController = null
    }
  },
  methods: {
    // ─── 写：点刷新按钮 → 取远端 → 写缓存 ───
    async doRefresh () {
      if (this.isDestroyed || this.loading) return
      this.loading = true
      try {
        await request({
          url: '/api/global-market/refresh',
          method: 'post',
          data: { target: 'all' }
        })
        // 写缓存成功后，重新读缓存
        await this.fetchCache()
      } catch (e) {
        console.error('[外围市场] 刷新失败:', e)
      } finally {
        if (!this.isDestroyed) this.loading = false
      }
    },

    // ─── 读：从后端缓存拿数据 ───
    async fetchCache () {
      if (this.isDestroyed) return
      if (this.abortController) this.abortController.abort()
      this.abortController = new AbortController()
      const signal = this.abortController.signal
      try {
        const [overviewRes, sentimentRes] = await Promise.all([
          this.readOverview(signal),
          this.readSentiment(signal)
        ])
        if (this.isDestroyed || signal.aborted) return
        this.processData(overviewRes, sentimentRes)
        this.lastUpdate = new Date().toLocaleTimeString()
      } catch (e) {
        if (e.name === 'AbortError' || e.code === 'ERR_CANCELED') return
        console.error('[外围市场] 读缓存失败:', e)
      }
    },

    // ─── 定时器：轮询读缓存，后端自己判断是否过期刷新 ───
    async pollCache () {
      await this.fetchCache()
    },

    async readOverview (signal) {
      try {
        const res = await request({ url: '/api/global-market/overview', method: 'get', signal })
        return res.code === 1 ? res.data : null
      } catch (e) {
        if (e.name !== 'AbortError' && e.code !== 'ERR_CANCELED') {
          console.error('[外围市场] overview 读取失败:', e)
        }
        return null
      }
    },

    async readSentiment (signal) {
      try {
        const res = await request({ url: '/api/global-market/sentiment', method: 'get', signal })
        return res.code === 1 ? res.data : null
      } catch (e) {
        if (e.name !== 'AbortError' && e.code !== 'ERR_CANCELED') {
          console.error('[外围市场] sentiment 读取失败:', e)
        }
        return null
      }
    },

    // ─── 数据处理（不变） ───

    processData (overview, sentiment) {
      const items = []

      if (overview && overview.indices) {
        for (const idx of overview.indices) {
          const change = parseFloat(idx.change_pct || idx.changePct || idx.change || 0)
          const price = parseFloat(idx.price || idx.current || 0)
          const key = `idx_${idx.symbol || idx.code}`
          const anomaly = this.detectAnomaly(key, change)
          items.push({
            key,
            name: idx.name || idx.name_cn || idx.symbol || '指数',
            emoji: this.getIndexEmoji(idx.symbol || idx.code || idx.name),
            price, priceText: this.formatPrice(price),
            change, changeText: Math.abs(change).toFixed(2),
            sentiment: this.calcIndexSentiment(change),
            anomaly: anomaly.isAnomaly, anomalyDir: anomaly.direction
          })
        }
      }

      if (overview && overview.crypto) {
        for (const c of overview.crypto) {
          const change = parseFloat(c.change_24h || c.change_pct || c.changePct || c.change || 0)
          const price = parseFloat(c.price || c.current || 0)
          const key = `crypto_${c.symbol || c.code}`
          const anomaly = this.detectAnomaly(key, change)
          items.push({
            key,
            name: c.name || c.name_cn || c.symbol || '加密',
            emoji: this.getCryptoEmoji(c.symbol || c.code),
            price, priceText: this.formatPrice(price),
            change, changeText: Math.abs(change).toFixed(2),
            sentiment: this.calcCryptoSentiment(change),
            anomaly: anomaly.isAnomaly, anomalyDir: anomaly.direction
          })
        }
      }

      if (overview && overview.forex) {
        for (const f of overview.forex) {
          const change = parseFloat(f.change_pct || f.changePct || f.change || 0)
          const price = parseFloat(f.price || f.current || 0)
          const key = `forex_${f.symbol || f.code}`
          const anomaly = this.detectAnomaly(key, change)
          items.push({
            key,
            name: f.name || f.name_cn || f.symbol || '外汇',
            emoji: '💱',
            price, priceText: price.toFixed(4),
            change, changeText: Math.abs(change).toFixed(3),
            sentiment: this.calcForexSentiment(change),
            anomaly: anomaly.isAnomaly, anomalyDir: anomaly.direction
          })
        }
      }

      if (overview && overview.commodities) {
        for (const c of overview.commodities) {
          const change = parseFloat(c.change_pct || c.changePct || c.change || 0)
          const price = parseFloat(c.price || c.current || 0)
          const key = `comm_${c.symbol || c.code}`
          const anomaly = this.detectAnomaly(key, change)
          items.push({
            key,
            name: c.name || c.name_cn || c.symbol || '商品',
            emoji: this.getCommodityEmoji(c.symbol || c.code),
            price, priceText: this.formatPrice(price),
            change, changeText: Math.abs(change).toFixed(2),
            sentiment: this.calcCommoditySentiment(change, c.symbol || c.code),
            anomaly: anomaly.isAnomaly, anomalyDir: anomaly.direction
          })
        }
      }

      if (sentiment) {
        if (sentiment.fear_greed && sentiment.fear_greed.value !== undefined) {
          const fg = sentiment.fear_greed
          const val = parseInt(fg.value) || 50
          const prevVal = this.prevData['sent_fear_greed']
          const fgChange = prevVal !== undefined ? val - prevVal : 0
          const anomaly = this.detectAnomaly('sent_fear_greed', fgChange)
          items.push({
            key: 'sent_fear_greed', name: '恐贪指数',
            emoji: val > 60 ? '😎' : val < 40 ? '😱' : '😐',
            price: val, priceText: String(val),
            change: fgChange, changeText: Math.abs(fgChange).toFixed(1),
            sentiment: val,
            anomaly: anomaly.isAnomaly, anomalyDir: anomaly.direction
          })
        }
        if (sentiment.vix && sentiment.vix.value !== undefined) {
          const vix = parseFloat(sentiment.vix.value) || 0
          const vixSentiment = Math.max(0, Math.min(100, 100 - (vix - 10) * 2.5))
          const vixChange = parseFloat(sentiment.vix.change_pct || sentiment.vix.change || 0)
          const anomaly = this.detectAnomaly('sent_vix', vixChange)
          items.push({
            key: 'sent_vix', name: 'VIX 波动率',
            emoji: vix > 25 ? '🔴' : vix > 18 ? '🟡' : '🟢',
            price: vix, priceText: vix.toFixed(2),
            change: vixChange, changeText: Math.abs(vixChange).toFixed(2),
            sentiment: Math.round(vixSentiment),
            anomaly: anomaly.isAnomaly, anomalyDir: anomaly.direction
          })
        }
        if (sentiment.dxy && sentiment.dxy.value !== undefined) {
          const dxy = parseFloat(sentiment.dxy.value) || 0
          const dxyChange = parseFloat(sentiment.dxy.change_pct || sentiment.dxy.change || 0)
          const dxySentiment = Math.max(0, Math.min(100, 50 - dxyChange * 10 + 50))
          const anomaly = this.detectAnomaly('sent_dxy', dxyChange)
          items.push({
            key: 'sent_dxy', name: '美元指数 DXY',
            emoji: '💵',
            price: dxy, priceText: dxy.toFixed(2),
            change: dxyChange, changeText: Math.abs(dxyChange).toFixed(3),
            sentiment: Math.round(dxySentiment),
            anomaly: anomaly.isAnomaly, anomalyDir: anomaly.direction
          })
        }
      }

      this.allMarketItems = items

      if (items.length > 0) {
        const totalSentiment = items.reduce((sum, i) => sum + i.sentiment, 0)
        this.overallScore = Math.round(totalSentiment / items.length)
      }

      const alerts = items.filter(i => i.anomaly)
      if (alerts.length > 0) {
        this.sendAnomalyNotification(alerts)
      }
    },

    detectAnomaly (key, change) {
      const absChange = Math.abs(change)
      const isAnomaly = absChange >= ANOMALY_THRESHOLD
      const direction = change > 0 ? 'bullish' : change < 0 ? 'bearish' : 'neutral'
      if (Object.keys(this.prevData).length > 200) {
        this.prevData = {}
      }
      this.prevData[key] = change
      return { isAnomaly, direction }
    },

    calcIndexSentiment (change) {
      return Math.round(Math.max(0, Math.min(100, 50 + change * (50 / 3))))
    },
    calcCryptoSentiment (change) {
      return Math.round(Math.max(0, Math.min(100, 50 + change * (50 / 8))))
    },
    calcForexSentiment (change) {
      return Math.round(Math.max(0, Math.min(100, 50 + change * 50)))
    },
    calcCommoditySentiment (change, symbol) {
      if (symbol && (symbol.includes('GC') || symbol.includes('gold'))) {
        return Math.round(Math.max(0, Math.min(100, 50 - change * (50 / 3))))
      }
      return Math.round(Math.max(0, Math.min(100, 50 + change * (50 / 3))))
    },

    sendAnomalyNotification (alerts) {
      const lines = alerts.map(a => {
        const dir = a.anomalyDir === 'bullish' ? '📈 看多' : '📉 看空'
        return `${a.emoji} ${a.name}: ${a.change >= 0 ? '+' : ''}${a.changeText}% ${dir}`
      })
      const title = `⚠️ 外围市场异动 (${alerts.length}个)`
      const body = lines.join('\n')

      if ('Notification' in window && Notification.permission === 'granted') {
        // eslint-disable-next-line no-new
        new Notification(title, { body, icon: '/logo.png' })
      } else if ('Notification' in window && Notification.permission !== 'denied') {
        Notification.requestPermission().then(p => {
          // eslint-disable-next-line no-new
          if (p === 'granted') new Notification(title, { body, icon: '/logo.png' })
        })
      }
      if (this.$notification) {
        this.$notification.warn({ message: title, description: lines.map((l, i) => `${i + 1}. ${l}`).join('\n'), duration: 10 })
      }
      console.log('[外围市场异动]', title, body)
    },

    // ====== 工具方法 ======

    getScoreClass (score) {
      if (score >= 70) return 'score-high'
      if (score >= 40) return 'score-neutral'
      return 'score-low'
    },
    getOverallLabel (score) {
      if (score >= 85) return '极度乐观'
      if (score >= 70) return '偏多'
      if (score >= 55) return '中性偏多'
      if (score >= 45) return '中性'
      if (score >= 30) return '中性偏空'
      if (score >= 15) return '偏空'
      return '极度悲观'
    },
    getOverallDesc (score) {
      if (score >= 70) return '外围市场整体走强，风险偏好上升，有利于A股开盘'
      if (score >= 55) return '外围市场偏暖，对A股影响中性偏好'
      if (score >= 45) return '外围市场方向不明，关注后续走势'
      if (score >= 30) return '外围市场偏弱，注意A股开盘承压'
      return '外围市场整体走弱，警惕系统性风险传导'
    },
    getSentimentClass (score) {
      if (score >= 70) return 'sentiment-bull'
      if (score >= 40) return 'sentiment-neutral'
      return 'sentiment-bear'
    },
    formatPrice (price) {
      if (!price || isNaN(price)) return '--'
      if (price >= 10000) return price.toLocaleString('en-US', { maximumFractionDigits: 0 })
      if (price >= 100) return price.toFixed(2)
      return price.toFixed(4)
    },
    getIndexEmoji (symbol) {
      const s = String(symbol).toUpperCase()
      if (s.includes('GSPC') || s.includes('SPX') || s.includes('SPY')) return '🇺🇸'
      if (s.includes('DJI') || s.includes('DOW')) return '🇺🇸'
      if (s.includes('IXIC') || s.includes('NASDAQ')) return '🇺🇸'
      if (s.includes('N225') || s.includes('NIKKEI')) return '🇯🇵'
      if (s.includes('HSI') || s.includes('HANG')) return '🇭🇰'
      if (s.includes('FTSE')) return '🇬🇧'
      if (s.includes('GDAXI') || s.includes('DAX')) return '🇩🇪'
      if (s.includes('FCHI') || s.includes('CAC')) return '🇫🇷'
      if (s.includes('KS11') || s.includes('KOSPI')) return '🇰🇷'
      if (s.includes('AS51') || s.includes('ASX')) return '🇦🇺'
      if (s.includes('SENSEX') || s.includes('NIFTY')) return '🇮🇳'
      return '📊'
    },
    getCryptoEmoji (symbol) {
      const s = String(symbol).toUpperCase()
      if (s.includes('BTC')) return '₿'
      if (s.includes('ETH')) return 'Ξ'
      if (s.includes('SOL')) return '◎'
      if (s.includes('BNB')) return '🔶'
      return '🪙'
    },
    getCommodityEmoji (symbol) {
      const s = String(symbol).toUpperCase()
      if (s.includes('GC') || s.includes('GOLD')) return '🥇'
      if (s.includes('SI') || s.includes('SILVER')) return '🥈'
      if (s.includes('CL') || s.includes('OIL') || s.includes('CRUDE')) return '🛢️'
      if (s.includes('NG') || s.includes('GAS')) return '🔥'
      return '📦'
    }
  }
}
</script>

<style scoped>
.peripheral-card {
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

.header-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.update-time {
  font-size: 11px;
  color: #999;
}

.btn-refresh {
  padding: 4px 8px;
  background: #ecf5ff;
  color: #409eff;
  border: 1px solid #b3d8ff;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
}
.btn-refresh:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.peripheral-content {
  padding: 16px;
}

/* === 整体评分 === */
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

.score-ring {
  position: relative;
  width: 100px;
  height: 100px;
  flex-shrink: 0;
}

.score-ring svg {
  width: 100%;
  height: 100%;
  transform: rotate(-90deg);
}

.ring-bg {
  fill: none;
  stroke: #e2e8f0;
  stroke-width: 8;
}

.ring-progress {
  fill: none;
  stroke-width: 8;
  stroke-linecap: round;
  transition: stroke-dasharray 0.6s ease;
}

.ring-progress.score-high { stroke: #10b981; }
.ring-progress.score-neutral { stroke: #f59e0b; }
.ring-progress.score-low { stroke: #ef4444; }

.score-inner {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  text-align: center;
}

.score-value {
  display: block;
  font-size: 28px;
  font-weight: 700;
  line-height: 1;
  color: var(--text-color, #1e293b);
}

.score-label {
  display: block;
  font-size: 11px;
  color: #64748b;
  margin-top: 2px;
}

.score-desc {
  flex: 1;
}

.desc-title {
  font-size: 15px;
  font-weight: 700;
  margin-bottom: 6px;
}
.desc-title.score-high { color: #10b981; }
.desc-title.score-neutral { color: #f59e0b; }
.desc-title.score-low { color: #ef4444; }

.desc-detail {
  font-size: 13px;
  color: #64748b;
  line-height: 1.5;
}

/* === 市场列表 === */
.market-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.market-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background: var(--row-bg, #fafbfc);
  border-radius: 8px;
  border: 1px solid transparent;
  transition: all 0.3s ease;
}

.market-row:hover {
  background: #f0f4f8;
}

/* 异动行高亮 */
.market-row.anomaly-alert {
  background: linear-gradient(135deg, #fef2f2, #fff1f2);
  border: 1px solid #fca5a5;
  animation: pulse-border 2s ease-in-out infinite;
}

@keyframes pulse-border {
  0%, 100% { border-color: #fca5a5; }
  50% { border-color: #ef4444; box-shadow: 0 0 8px rgba(239,68,68,0.15); }
}

/* 被替换的项（异动项） */
.market-row.replaced-item {
  border-left: 3px solid #f59e0b;
}

.market-icon {
  font-size: 20px;
  width: 28px;
  text-align: center;
  flex-shrink: 0;
}

.market-info {
  flex: 1;
  min-width: 0;
}

.market-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-color, #334155);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.anomaly-icon {
  margin-left: 4px;
  font-size: 14px;
}

.replaced-badge {
  display: inline-block;
  margin-left: 6px;
  font-size: 10px;
  font-weight: 600;
  padding: 1px 5px;
  border-radius: 3px;
  background: #fef3c7;
  color: #92400e;
  vertical-align: middle;
}

.market-detail {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}

.price {
  font-size: 12px;
  color: #94a3b8;
  font-family: 'SF Mono', 'Menlo', monospace;
}

.change {
  font-size: 12px;
  font-weight: 600;
  font-family: 'SF Mono', 'Menlo', monospace;
}
.change.up { color: #ef4444; }
.change.down { color: #10b981; }

/* 情绪条 */
.market-score-col {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 120px;
  flex-shrink: 0;
}

.sentiment-bar-wrap {
  flex: 1;
  height: 6px;
  background: #e2e8f0;
  border-radius: 3px;
  overflow: hidden;
}

.sentiment-bar {
  height: 100%;
  border-radius: 3px;
  transition: width 0.4s ease;
}
.sentiment-bar.sentiment-bull { background: linear-gradient(90deg, #10b981, #34d399); }
.sentiment-bar.sentiment-neutral { background: linear-gradient(90deg, #f59e0b, #fbbf24); }
.sentiment-bar.sentiment-bear { background: linear-gradient(90deg, #ef4444, #f87171); }

.sentiment-value {
  font-size: 13px;
  font-weight: 700;
  width: 28px;
  text-align: right;
  font-family: 'SF Mono', 'Menlo', monospace;
}
.sentiment-value.sentiment-bull { color: #10b981; }
.sentiment-value.sentiment-neutral { color: #f59e0b; }
.sentiment-value.sentiment-bear { color: #ef4444; }

/* 异动标签 */
.anomaly-tag {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
  white-space: nowrap;
  flex-shrink: 0;
}
.anomaly-tag.bullish {
  background: #dcfce7;
  color: #166534;
}
.anomaly-tag.bearish {
  background: #fee2e2;
  color: #991b1b;
}

.empty-state {
  text-align: center;
  padding: 24px;
  color: #94a3b8;
  font-size: 14px;
}
</style>
