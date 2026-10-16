<template>
  <div class="module-card">
    <header class="module-header">
      <h3>龙虎榜</h3>
      <button @click="refresh" :disabled="loading" class="btn-refresh">
        {{ loading ? '加载中...' : '刷新' }}
      </button>
    </header>
    <div class="module-content table-container">
      <table v-if="list.length > 0" class="data-table">
        <thead><tr>
          <th @click="sortTable('code')">代码 ▼</th>
          <th @click="sortTable('name')">名称 ▼</th>
          <th @click="sortTable('reason')">上榜原因 ▼</th>
          <th @click="sortTable('change')">涨跌幅 ▼</th>
        </tr></thead>
        <tbody>
          <tr v-for="item in filteredList.slice(0, 10)" :key="item.code">
            <td>{{ item.code }}</td>
            <td>{{ item.name }}</td>
            <td class="reason-cell" :title="item.reasonFull">{{ item.reason }}</td>
            <td :class="item.change.startsWith('+') ? 'up' : 'down'">{{ item.change }}</td>
          </tr>
        </tbody>
      </table>
      <!-- 错误态: 请求失败时明确提示 + 重试，不再静默空白 -->
      <div v-else-if="error" class="state-block">
        <p class="state-title state-error">⚠️ 加载失败</p>
        <p class="state-desc">{{ errorText }}</p>
        <button class="btn-retry" @click="refresh">重试</button>
      </div>
      <!-- 空态: 后端返回空列表(库中暂无任何龙虎榜记录 / 当日榜单尚未落库) -->
      <div v-else-if="!loading" class="state-block">
        <p class="state-title">暂无龙虎榜数据</p>
        <p class="state-desc">
          数据库暂无龙虎榜记录。当日榜单通常在收盘后写入，可稍后点击"刷新"。
        </p>
        <button class="btn-retry" @click="refresh">刷新</button>
      </div>
      <div v-else class="state-block state-loading">加载中...</div>
      <div v-if="list.length > 0" class="table-footer">
        <span>共{{ list.length }}条</span>
        <button v-if="list.length > 10" class="btn-more" @click="showDetail = true">更多...</button>
      </div>
    </div>
    <DetailModal
      :visible="showDetail"
      title="龙虎榜详情"
      :columns="detailColumns"
      :data="list"
      @close="showDetail = false"
    />
  </div>
</template>

<script>
import DetailModal from './DetailModal.vue'
import request from '@/utils/request'

// 空结果自动重试（次数 / 间隔 ms）— 后端已在缓存未命中时回退 DB 最近交易日
// （见 backend cards/dragon_tiger.py），正常不会为空; 此处仅兜底数据库瞬时不可用
const EMPTY_RETRY_MAX = 3
const EMPTY_RETRY_DELAY = 20000

export default {
  name: 'DragonTigerCard',
  components: { DetailModal },
  data () {
    return {
      list: [],
      loading: false,
      error: null,
      retryCount: 0,
      retryTimer: null,
      sortKey: '',
      sortOrder: 1,
      showDetail: false,
      detailColumns: [
        { key: 'trade_date', label: '上榜日期' },
        { key: 'code', label: '股票代码' },
        { key: 'name', label: '股票名称' },
        { key: 'reason', label: '上榜原因' },
        { key: 'change', label: '涨跌幅' },
        { key: 'buy_amount', label: '买入金额' },
        { key: 'sell_amount', label: '卖出金额' },
        { key: 'net_amount', label: '净买入额' },
        { key: 'buy_seat_count', label: '买入席位数' },
        { key: 'sell_seat_count', label: '卖出席位数' }
      ]
    }
  },
  computed: {
    errorText () {
      const e = this.error
      if (!e) return ''
      return e.message || String(e)
    },
    filteredList () {
      const arr = [...this.list]
      if (this.sortKey) {
        arr.sort((a, b) => {
          let va = a[this.sortKey]
        let vb = b[this.sortKey]
          if (this.sortKey === 'change') {
            va = parseFloat(va) || 0; vb = parseFloat(vb) || 0
          }
          const cmp = typeof va === 'number' ? (va > vb ? 1 : -1) : va.toString().localeCompare(vb.toString())
          return this.sortOrder * cmp
        })
      }
      return arr
    }
  },
  methods: {
    async refresh () {
      this._clearRetryTimer()
      this.loading = true
      this.error = null
      try {
        const resp = await request({ url: '/api/shichang/cards/dragon-tiger', method: 'GET' })
        const d = resp.data || resp
        const list = (d.dragonTigerList || []).map(item => {
          const full = (item.reason || '').replace(/成功率/g, '')
          return {
            ...item,
            reasonFull: full,
            reason: full.slice(0, 10),
            change: String(item.change ?? '0')
          }
        })
        this.list = list
        this.retryCount = 0
        // 空结果: 有限自动重试，仅兜底数据库瞬时不可用（正常不会走到）
        if (list.length === 0) this._scheduleEmptyRetry()
      } catch (e) {
        console.error('龙虎榜刷新失败:', e)
        this.error = e
      } finally {
        this.loading = false
      }
    },
    _scheduleEmptyRetry () {
      if (this.retryCount >= EMPTY_RETRY_MAX) return
      this.retryTimer = setTimeout(() => {
        this.retryCount += 1
        this.refresh()
      }, EMPTY_RETRY_DELAY)
    },
    _clearRetryTimer () {
      if (this.retryTimer) {
        clearTimeout(this.retryTimer)
        this.retryTimer = null
      }
    },
    sortTable (k) {
      if (this.sortKey === k) {
        this.sortOrder *= -1
      } else {
        this.sortKey = k; this.sortOrder = 1
      }
    }
  },
  mounted () { this.refresh() },
  beforeDestroy () {
    this._clearRetryTimer()
  }
}
</script>

<style scoped>
.module-card {
  background: var(--card-bg); border-radius: 8px; box-shadow: var(--shadow);
  border: 1px solid #dbdbdb; padding: 8px; overflow: hidden;
}
.module-header {
  padding: 12px 16px; background: #f8f9fc; border-bottom: 1px solid var(--border-color);
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px;
}
.module-header h3 { margin: 0; font-size: 16px; font-weight: 600; color: #333; }
.btn-refresh {
  padding: 4px 8px; background: #ecf5ff; color: #409eff; border: 1px solid #b3d8ff;
  border-radius: 4px; cursor: pointer; font-size: 12px;
}
.btn-refresh:disabled { opacity: 0.5; cursor: not-allowed; }
/* 空态 / 错误态 / 加载态 */
.state-block { padding: 24px 16px; text-align: center; color: #999; font-size: 13px; }
.state-block .state-title { margin: 0 0 6px; font-size: 14px; font-weight: 600; color: #666; }
.state-block .state-title.state-error { color: #f56c6c; }
.state-block .state-desc { margin: 0 0 10px; font-size: 12px; line-height: 1.6; }
.state-loading { color: #409eff; }
.btn-retry {
  padding: 4px 14px; background: #ecf5ff; color: #409eff; border: 1px solid #b3d8ff;
  border-radius: 4px; cursor: pointer; font-size: 12px;
}
.btn-retry:hover { background: #409eff; color: #fff; }
.table-container { overflow-x: auto; }
.data-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.data-table th, .data-table td { padding: 6px; text-align: left; border-bottom: 1px solid var(--border-color); }
.data-table th { background: #f8f9fc; font-weight: 600; color: #333; cursor: pointer; }
.data-table tbody tr:hover { background-color: #f5f7fa; }
.reason-cell {
  max-width: 100px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.table-footer { padding: 4px 8px; text-align: right; font-size: 12px; color: #999; display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
.up { color: #f56c6c; }
.down { color: #67c23a; }
.btn-more {
  background: none; border: none; color: #409eff; cursor: pointer;
  font-size: 12px; padding: 4px 8px;
}
.btn-more:hover { text-decoration: underline; }
</style>
