<template>
  <div class="module-card">
    <header class="module-header">
      <h3>龙虎榜</h3>
      <button @click="refresh" :disabled="loading" class="btn-refresh">
        {{ loading ? '加载中...' : '刷新' }}
      </button>
    </header>
    <div class="module-content table-container">
      <table class="data-table">
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
      <div class="table-footer">
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

export default {
  name: 'DragonTigerCard',
  components: { DetailModal },
  data () {
    return {
      list: [],
      loading: false,
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
      this.loading = true
      try {
        const d = await request({ url: '/api/shichang/dragon', method: 'GET' })
        this.list = (d.dragonTigerList || []).map(item => {
          const full = (item.reason || '').replace(/成功率/g, '')
          return {
            ...item,
            reasonFull: full,
            reason: full.slice(0, 10),
            change: String(item.change ?? '0')
          }
        })
      } catch (e) {
        console.error('龙虎榜刷新失败:', e)
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
