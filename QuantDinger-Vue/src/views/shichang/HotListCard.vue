<template>
  <div class="module-card">
    <header class="module-header">
      <h3>同花顺热榜</h3>
      <button @click="refresh" :disabled="loading" class="btn-refresh">
        {{ loading ? '加载中...' : '刷新' }}
      </button>
    </header>
    <div class="module-content table-container">
      <table class="data-table">
        <thead><tr>
          <th>排名</th>
          <th @click="sortTable('code')">股票代码 ▼</th>
          <th @click="sortTable('name')">股票名称 ▼</th>
          <th @click="sortTable('hot')">热度 ▼</th>
          <th @click="sortTable('change')">涨跌幅 ▼</th>
        </tr></thead>
        <tbody>
          <tr
            v-for="(item, index) in sortedList.slice(0, 10)"
            :key="item.code"
          >
            <td>#{{ index + 1 }}</td>
            <td>{{ item.code }}</td>
            <td>{{ item.name }}</td>
            <td>{{ item.hot }}</td>
            <td :class="item.change.startsWith('+') ? 'up' : 'down'">{{ item.change }}</td>
          </tr>
        </tbody>
      </table>
      <div class="table-footer">
        <span>共{{ list.length }}条</span>
        <button v-if="list.length > 10" class="btn-more" @click="showDetail = true">更多...</button>
      </div>
    </div>
    <DetailModal
      :visible="showDetail"
      title="同花顺热榜详情"
      :columns="detailColumns"
      :data="list"
      @close="showDetail = false"
    />
  </div>
</template>

<script>
import DetailModal from './DetailModal.vue'

export default {
  name: 'HotListCard',
  components: { DetailModal },
  data () {
    return {
      list: [],
      loading: false,
      sortKey: '',
      sortOrder: 1,
      showDetail: false,
      detailColumns: [
        { key: 'rank', label: '排名' },
        { key: 'code', label: '股票代码' },
        { key: 'name', label: '股票名称' },
        { key: 'hot', label: '热度' },
        { key: 'change', label: '涨跌幅' },
        { key: 'price', label: '最新价' },
        { key: 'current_rank_change', label: '排名变化' }
      ]
    }
  },
  computed: {
    sortedList () {
      if (!this.sortKey) return this.list
      const arr = [...this.list]
      arr.sort((a, b) => {
        let va = a[this.sortKey]
        let vb = b[this.sortKey]
        if (this.sortKey === 'hot' || this.sortKey === 'change') {
          va = parseFloat(va) || 0; vb = parseFloat(vb) || 0
        }
        const cmp = typeof va === 'number' ? (va > vb ? 1 : -1) : va.toString().localeCompare(vb.toString())
        return this.sortOrder * cmp
      })
      return arr
    }
  },
  methods: {
    async refresh () {
      this.loading = true
      try {
        const r = await fetch('/api/shichang/hot')
        if (!r.ok) return
        const d = await r.json()
        this.list = (d.hotList || []).map((item, i) => ({ ...item, rank: i + 1, change: String(item.change ?? '0') }))
      } catch (e) {
        console.error('热榜刷新失败:', e)
      } finally {
        this.loading = false
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
  mounted () { this.refresh() }
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
.table-container { overflow-x: auto; }
.data-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.data-table th, .data-table td { padding: 6px; text-align: left; border-bottom: 1px solid var(--border-color); }
.data-table th { background: #f8f9fc; font-weight: 600; color: #333; cursor: pointer; }
.data-table tbody tr:hover { background-color: #f5f7fa; }
.table-footer { padding: 4px 8px; text-align: right; font-size: 12px; color: #999; display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
.up { color: #f56c6c; }
.down { color: #67c23a; }
.btn-more {
  background: none; border: none; color: #409eff; cursor: pointer;
  font-size: 12px; padding: 4px 8px;
}
.btn-more:hover { text-decoration: underline; }
</style>
