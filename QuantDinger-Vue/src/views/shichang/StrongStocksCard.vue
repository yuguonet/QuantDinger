<template>
  <div class="module-card">
    <header class="module-header">
      <h3>强势股</h3>
      <input v-model="searchQuery" placeholder="搜索..." class="search-input" />
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
          <th @click="sortTable('gain')">累计涨幅 ▼</th>
          <th @click="sortTable('price')">最新价 ▼</th>
        </tr></thead>
        <tbody>
          <tr
            v-for="item in filteredList.slice(0, 10)"
            :key="item.rank"
          >
            <td>#{{ item.rank }}</td>
            <td>{{ item.code }}</td>
            <td>{{ item.name }}</td>
            <td :class="item.gain.startsWith('+') ? 'up' : 'down'">{{ item.gain }}</td>
            <td>{{ item.price }}</td>
          </tr>
        </tbody>
      </table>
      <div class="table-footer">
        <span>共 {{ list.length }} 只强势股</span>
        <button v-if="list.length > 10" class="btn-more" @click="showDetail = true">更多...</button>
      </div>
    </div>
    <DetailModal
      :visible="showDetail"
      title="强势股详情"
      :columns="detailColumns"
      :data="list"
      @close="showDetail = false"
    />
  </div>
</template>

<script>
import DetailModal from './DetailModal.vue'

export default {
  name: 'StrongStocksCard',
  components: { DetailModal },
  data () {
    return {
      list: [],
      loading: false,
      searchQuery: '',
      sortKey: '',
      sortOrder: 1,
      showDetail: false,
      detailColumns: [
        { key: 'rank', label: '排名' },
        { key: 'code', label: '股票代码' },
        { key: 'name', label: '股票名称' },
        { key: 'days', label: '连板数' },
        { key: 'gain', label: '累计涨幅' },
        { key: 'price', label: '最新价' },
        { key: 'sector', label: '所属行业' },
        { key: 'reason', label: '涨停原因' },
        { key: 'seal_amount', label: '封板资金' },
        { key: 'turnover_rate', label: '换手率' },
        { key: 'zt_time', label: '涨停时间' },
        { key: 'open_count', label: '打开次数' },
        { key: 'volume', label: '成交量' },
        { key: 'amount', label: '成交额' }
      ]
    }
  },
  computed: {
    filteredList () {
      let arr = [...this.list]
      if (this.searchQuery) {
        const q = this.searchQuery.toLowerCase()
        arr = arr.filter(item =>
          item.code.toLowerCase().includes(q) ||
          item.name.toLowerCase().includes(q)
        )
      }
      if (this.sortKey) {
        arr.sort((a, b) => {
          let va = a[this.sortKey]
        let vb = b[this.sortKey]
          if (this.sortKey === 'gain' || this.sortKey === 'price') {
            va = parseFloat(String(va).replace(/[+%]/g, '')) || 0
            vb = parseFloat(String(vb).replace(/[+%]/g, '')) || 0
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
      this.loading = true
      try {
        const r = await fetch('/api/shichang/strong')
        if (!r.ok) return
        const d = await r.json()
        this.list = (d.strongStocks || []).map(item => ({ ...item, gain: String(item.gain ?? '0') }))
      } catch (e) {
        console.error('强势股刷新失败:', e)
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
.search-input { padding: 4px 8px; border: 1px solid var(--border-color); border-radius: 4px; font-size: 12px; margin: 0 4px; }
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
