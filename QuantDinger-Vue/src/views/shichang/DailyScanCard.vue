<template>
  <div class="module-card">
    <header class="module-header">
      <h3>📊 板块每日扫描</h3>
      <div class="header-right">
        <span class="scan-date" v-if="data.date">{{ data.date }}</span>
        <button @click="showDetail = true" class="btn-more">更多</button>
      </div>
    </header>

    <div class="module-content">
      <!-- 新热点 -->
      <table v-if="data.new_hot && data.new_hot.length" class="data-table">
        <thead><tr>
          <th>🔥新热点</th><th>类型</th><th>热度</th><th>阈值</th>
        </tr></thead>
        <tbody>
          <tr v-for="item in data.new_hot.slice(0, 5)" :key="'n'+item.name">
            <td class="name-cell">{{ item.name }}</td>
            <td><span class="type-tag">{{ item.type }}</span></td>
            <td class="num">{{ item.heat }}</td>
            <td class="num dim">{{ item.threshold }}</td>
          </tr>
        </tbody>
      </table>

      <!-- 上升中 -->
      <table v-if="data.rising && data.rising.length" class="data-table">
        <thead><tr>
          <th>📈上升中</th><th>第几天</th><th>热度</th><th>峰值</th><th>异常</th>
        </tr></thead>
        <tbody>
          <tr v-for="item in data.rising.slice(0, 5)" :key="'r'+item.name">
            <td class="name-cell">{{ item.name }}</td>
            <td class="num">{{ item.day }}天</td>
            <td class="num">{{ item.heat }}</td>
            <td class="num">{{ item.peak }}</td>
            <td class="num" :class="item.anomaly >= 1.3 ? 'highlight' : ''">
              {{ item.anomaly }}x
              <span v-if="item.anomaly >= 1.3">🚀</span>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- 异常板块 -->
      <table v-if="data.anomaly && data.anomaly.length" class="data-table">
        <thead><tr>
          <th>🚀异常</th><th>峰值</th><th>历史均值</th><th>倍数</th><th>持续</th><th>阶段</th>
        </tr></thead>
        <tbody>
          <tr v-for="item in data.anomaly.slice(0, 5)" :key="'a'+item.name">
            <td class="name-cell">{{ item.name }}</td>
            <td class="num highlight">{{ item.peak }}</td>
            <td class="num dim">{{ item.heat }}</td>
            <td class="num highlight">{{ item.anomaly }}x</td>
            <td class="num">{{ item.day }}天</td>
            <td>
              <span :class="phaseClass(item.phase)">
                {{ phaseIcon(item.phase) }} {{ phaseLabel(item.phase) }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- 衰减中 -->
      <table v-if="data.decaying && data.decaying.length" class="data-table">
        <thead><tr>
          <th>⚠️衰减中</th><th>第几天</th><th>热度</th><th>峰值</th><th>回落</th>
        </tr></thead>
        <tbody>
          <tr v-for="item in data.decaying.slice(0, 5)" :key="'d'+item.name">
            <td class="name-cell">{{ item.name }}</td>
            <td class="num">{{ item.day }}天</td>
            <td class="num">{{ item.heat }}</td>
            <td class="num">{{ item.peak }}</td>
            <td class="num down">-{{ dropPct(item) }}%</td>
          </tr>
        </tbody>
      </table>

      <!-- 今日降温 -->
      <table v-if="data.just_ended && data.just_ended.length" class="data-table">
        <thead><tr>
          <th>❄️降温</th><th>今天热度</th><th>阈值</th>
        </tr></thead>
        <tbody>
          <tr v-for="item in data.just_ended.slice(0, 5)" :key="'e'+item.name">
            <td class="name-cell">{{ item.name }}</td>
            <td class="num">{{ item.heat }}</td>
            <td class="num dim">{{ item.threshold }}</td>
          </tr>
        </tbody>
      </table>

      <div v-if="isEmpty" class="empty-state">暂无数据</div>
    </div>

    <!-- 详情弹层 -->
    <div v-if="showDetail" class="detail-overlay" @click.self="showDetail = false">
      <div class="detail-panel">
        <header class="detail-header">
          <h3>📊 板块每日扫描 · 全量</h3>
          <span class="scan-date">{{ data.date }}</span>
          <button @click="showDetail = false" class="btn-close">✕</button>
        </header>

        <div class="detail-body">
          <div class="summary-bar" v-if="data.summary">
            <span class="tag tag-fire">🔥 新热点 {{ data.summary.new_hot }}</span>
            <span class="tag tag-rise">📈 上升中 {{ data.summary.rising }}</span>
            <span class="tag tag-rocket">🚀 异常 {{ data.summary.anomaly }}</span>
            <span class="tag tag-warn">⚠️ 衰减 {{ data.summary.decaying }}</span>
            <span class="tag tag-cold">❄️ 降温 {{ data.summary.just_ended }}</span>
          </div>

          <section v-if="data.new_hot && data.new_hot.length" class="scan-section">
            <h4 class="section-title">🔥 新热点 <small>今天刚起来</small></h4>
            <table class="data-table">
              <thead><tr><th>板块</th><th>类型</th><th>热度</th><th>阈值</th></tr></thead>
              <tbody>
                <tr v-for="item in data.new_hot" :key="'dn'+item.name">
                  <td class="name-cell">{{ item.name }}</td>
                  <td><span class="type-tag">{{ item.type }}</span></td>
                  <td class="num">{{ item.heat }}</td>
                  <td class="num dim">{{ item.threshold }}</td>
                </tr>
              </tbody>
            </table>
          </section>

          <section v-if="data.rising && data.rising.length" class="scan-section">
            <h4 class="section-title">📈 上升中 <small>持续2天+还在涨</small></h4>
            <table class="data-table">
              <thead><tr><th>板块</th><th>第几天</th><th>热度</th><th>峰值</th><th>异常</th></tr></thead>
              <tbody>
                <tr v-for="item in data.rising" :key="'dr'+item.name">
                  <td class="name-cell">{{ item.name }}</td>
                  <td class="num">{{ item.day }}天</td>
                  <td class="num">{{ item.heat }}</td>
                  <td class="num">{{ item.peak }}</td>
                  <td class="num" :class="item.anomaly >= 1.3 ? 'highlight' : ''">
                    {{ item.anomaly }}x <span v-if="item.anomaly >= 1.3">🚀</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </section>

          <section v-if="data.anomaly && data.anomaly.length" class="scan-section">
            <h4 class="section-title">🚀 异常板块 <small>峰值 ≥ 历史1.3倍</small></h4>
            <table class="data-table">
              <thead><tr><th>板块</th><th>峰值</th><th>历史均值</th><th>倍数</th><th>持续</th><th>阶段</th></tr></thead>
              <tbody>
                <tr v-for="item in data.anomaly" :key="'da'+item.name">
                  <td class="name-cell">{{ item.name }}</td>
                  <td class="num highlight">{{ item.peak }}</td>
                  <td class="num dim">{{ item.heat }}</td>
                  <td class="num highlight">{{ item.anomaly }}x</td>
                  <td class="num">{{ item.day }}天</td>
                  <td><span :class="phaseClass(item.phase)">{{ phaseIcon(item.phase) }} {{ phaseLabel(item.phase) }}</span></td>
                </tr>
              </tbody>
            </table>
          </section>

          <section v-if="data.decaying && data.decaying.length" class="scan-section">
            <h4 class="section-title">⚠️ 衰减中 <small>峰值已过, 开始降温</small></h4>
            <table class="data-table">
              <thead><tr><th>板块</th><th>第几天</th><th>热度</th><th>峰值</th><th>回落</th></tr></thead>
              <tbody>
                <tr v-for="item in data.decaying" :key="'dd'+item.name">
                  <td class="name-cell">{{ item.name }}</td>
                  <td class="num">{{ item.day }}天</td>
                  <td class="num">{{ item.heat }}</td>
                  <td class="num">{{ item.peak }}</td>
                  <td class="num down">-{{ dropPct(item) }}%</td>
                </tr>
              </tbody>
            </table>
          </section>

          <section v-if="data.just_ended && data.just_ended.length" class="scan-section">
            <h4 class="section-title">❄️ 今日降温 <small>昨天还在脉冲, 今天跌出</small></h4>
            <table class="data-table">
              <thead><tr><th>板块</th><th>今天热度</th><th>阈值</th></tr></thead>
              <tbody>
                <tr v-for="item in data.just_ended" :key="'de'+item.name">
                  <td class="name-cell">{{ item.name }}</td>
                  <td class="num">{{ item.heat }}</td>
                  <td class="num dim">{{ item.threshold }}</td>
                </tr>
              </tbody>
            </table>
          </section>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import request from '@/utils/request'

export default {
  name: 'DailyScanCard',
  data () {
    return {
      loading: false,
      data: {},
      showDetail: false
    }
  },
  computed: {
    isEmpty () {
      const d = this.data
      if (!d || !d.summary) return true
      const s = d.summary
      return !s.new_hot && !s.rising && !s.anomaly && !s.decaying && !s.just_ended
    }
  },
  methods: {
    async refresh () {
      this.loading = true
      try {
        const resp = await request({ url: '/api/shichang/cards/daily-scan', method: 'GET' })
        const d = resp.data || resp
        this.data = d || {}
      } catch (e) {
        console.error('板块扫描刷新失败:', e)
      } finally {
        this.loading = false
      }
    },
    dropPct (item) {
      if (!item.peak) return 0
      return ((item.peak - item.heat) / item.peak * 100).toFixed(0)
    },
    phaseIcon (p) {
      return { rising: '↑', peak: '→', decay: '↓' }[p] || '?'
    },
    phaseLabel (p) {
      return { rising: '上升', peak: '顶部', decay: '衰减' }[p] || p
    },
    phaseClass (p) {
      return { rising: 'up', peak: 'flat', decay: 'down' }[p] || ''
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
  padding: 10px 14px; background: #f8f9fc; border-bottom: 1px solid var(--border-color, #ebeef5);
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px;
}
.module-header h3 { margin: 0; font-size: 15px; font-weight: 600; color: #333; }
.header-right { display: flex; align-items: center; gap: 8px; }
.scan-date { font-size: 12px; color: #999; }
.btn-more {
  padding: 3px 10px; background: #ecf5ff; color: #409eff; border: 1px solid #b3d8ff;
  border-radius: 4px; cursor: pointer; font-size: 12px;
}
.btn-more:hover { background: #d9ecff; }

/* 锁定10行高度，保留滚动 */
.module-content {
  padding: 6px 8px;
  max-height: 360px;
  overflow-y: auto;
}

/* 表格 */
.data-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 6px; }
.data-table th, .data-table td { padding: 4px 6px; text-align: left; border-bottom: 1px solid #f0f0f0; }
.data-table th { background: #fafbfc; font-weight: 600; color: #666; font-size: 12px; white-space: nowrap; }
.data-table tbody tr:hover { background-color: #f5f7fa; }
.name-cell { font-weight: 500; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.dim { color: #999; }
.type-tag { font-size: 11px; color: #999; }

/* 状态色 */
.up { color: #f56c6c; }
.down { color: #67c23a; }
.flat { color: #e6a23c; }
.highlight { color: #e6a23c; font-weight: 600; }

.empty-state { padding: 30px; text-align: center; color: #999; font-size: 13px; }

/* ========== 详情弹层 ========== */
.detail-overlay {
  position: fixed; inset: 0; z-index: 1000;
  background: rgba(0,0,0,0.45); display: flex; align-items: center; justify-content: center;
}
.detail-panel {
  width: 90vw; max-width: 900px; max-height: 85vh; background: #fff;
  border-radius: 10px; display: flex; flex-direction: column; overflow: hidden;
  box-shadow: 0 8px 30px rgba(0,0,0,0.25);
}
.detail-header {
  padding: 14px 18px; background: #f8f9fc; border-bottom: 1px solid #ebeef5;
  display: flex; align-items: center; gap: 12px;
}
.detail-header h3 { margin: 0; font-size: 16px; font-weight: 600; color: #333; flex: 1; }
.detail-header .scan-date { font-size: 12px; color: #999; }
.btn-close {
  width: 28px; height: 28px; border: none; background: #f0f0f0; border-radius: 50%;
  cursor: pointer; font-size: 14px; color: #666; display: flex; align-items: center; justify-content: center;
}
.btn-close:hover { background: #e0e0e0; }
.detail-body { flex: 1; overflow-y: auto; padding: 14px 18px; }

/* 汇总条 */
.summary-bar { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 12px; }
.tag { padding: 3px 8px; border-radius: 10px; font-size: 12px; font-weight: 500; white-space: nowrap; }
.tag-fire { background: #fff1f0; color: #cf1322; }
.tag-rise { background: #f6ffed; color: #389e0d; }
.tag-rocket { background: #fff7e6; color: #d46b08; }
.tag-warn { background: #fffbe6; color: #d4b106; }
.tag-cold { background: #e6f7ff; color: #096dd9; }

/* 详情分区 */
.scan-section { margin-bottom: 14px; }
.section-title {
  margin: 0 0 6px; font-size: 14px; font-weight: 600; color: #333;
  padding-bottom: 4px; border-bottom: 1px solid #f0f0f0;
}
.section-title small { font-weight: normal; color: #999; font-size: 12px; margin-left: 6px; }
</style>
