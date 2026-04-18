<template>
  <div v-if="visible" class="modal-overlay" @click.self="$emit('close')">
    <div class="modal-container">
      <header class="modal-header">
        <h3>{{ title }}</h3>
        <button class="modal-close" @click="$emit('close')">&times;</button>
      </header>
      <div class="modal-body">
        <table class="data-table">
          <thead>
            <tr>
              <th
                v-for="col in columns"
                :key="col.key"
                @click="toggleSort(col.key)"
              >
                {{ col.label }}
                <span v-if="sortKey === col.key">{{ sortOrder === 1 ? '▲' : '▼' }}</span>
                <span v-else class="sort-hint">▼</span>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(item, i) in pagedData"
              :key="i"
            >
              <td
                v-for="col in columns"
                :key="col.key"
                :class="isUpDown(item[col.key], col.key)"
              >
                {{ formatCell(item[col.key], col.key) }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <footer class="modal-footer">
        <div class="pagination">
          <button :disabled="page<=1" @click="page--">上一页</button>
          <span>{{ page }} / {{ totalPages }}</span>
          <button :disabled="page>=totalPages" @click="page++">下一页</button>
        </div>
        <span class="total-info">共 {{ sortedData.length }} 条</span>
      </footer>
    </div>
  </div>
</template>

<script>
export default {
  name: 'DetailModal',
  props: {
    visible: Boolean,
    title: { type: String, default: '' },
    columns: { type: Array, default: () => [] },
    data: { type: Array, default: () => [] }
  },
  emits: ['close'],
  data () {
    return { page: 1, sortKey: '', sortOrder: 1 }
  },
  computed: {
    sortedData () {
      if (!this.sortKey) return this.data
      const arr = [...this.data]
      arr.sort((a, b) => {
        let va = a[this.sortKey]
        let vb = b[this.sortKey]
        const numKeys = ['days', 'hot', 'change', 'gain', 'price', 'rank',
          'seal_amount', 'turnover_rate', 'open_count', 'volume', 'amount',
          'buy_amount', 'sell_amount', 'net_amount', 'buy_seat_count', 'sell_seat_count']
        if (numKeys.includes(this.sortKey)) {
          va = parseFloat(String(va).replace(/[+%]/g, '')) || 0
          vb = parseFloat(String(vb).replace(/[+%]/g, '')) || 0
          return this.sortOrder * (va - vb)
        }
        return this.sortOrder * String(va).localeCompare(String(vb))
      })
      return arr
    },
    totalPages () { return Math.max(1, Math.ceil(this.sortedData.length / 20)) },
    pagedData () {
      const start = (this.page - 1) * 20
      return this.sortedData.slice(start, start + 20)
    }
  },
  methods: {
    toggleSort (key) {
      if (this.sortKey === key) {
        if (this.sortOrder === 1) this.sortOrder = -1
        else { this.sortKey = ''; this.sortOrder = 1 }
      } else {
        this.sortKey = key; this.sortOrder = 1
      }
      this.page = 1
    },
    isUpDown (val, key) {
      if (['change', 'gain'].includes(key)) {
        return String(val).startsWith('+') ? 'up' : 'down'
      }
      if (['net_amount', 'buy_amount', 'sell_amount', 'seal_amount',
           'buy_seat_count', 'sell_seat_count'].includes(key)) {
        const num = parseFloat(val) || 0
        if (num > 0) return 'up'
        if (num < 0) return 'down'
      }
      return ''
    },
    formatCell (val, key) {
      if (['buy_amount', 'sell_amount', 'net_amount', 'seal_amount', 'amount'].includes(key)) {
        const num = parseFloat(val) || 0
        if (Math.abs(num) >= 1e8) return (num / 1e8).toFixed(2) + '亿'
        if (Math.abs(num) >= 1e4) return (num / 1e4).toFixed(2) + '万'
        return num.toFixed(2)
      }
      if (key === 'volume') {
        const num = parseFloat(val) || 0
        if (Math.abs(num) >= 1e4) return (num / 1e4).toFixed(2) + '万手'
        return num.toFixed(0)
      }
      return val
    }
  },
  watch: {
    visible (v) { if (v) { this.page = 1; this.sortKey = ''; this.sortOrder = 1 } }
  }
}
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.modal-container {
  background: #fff;
  border-radius: 8px;
  width: 95vw;
  max-width: 1200px;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.2);
}
.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid #ebeef5;
}
.modal-header h3 { margin: 0; font-size: 16px; font-weight: 600; color: #333; }
.modal-close {
  background: none; border: none; font-size: 24px; cursor: pointer;
  color: #999; line-height: 1; padding: 0 4px;
}
.modal-close:hover { color: #333; }
.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
}
.data-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.data-table th, .data-table td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #ebeef5; }
.data-table th { background: #f8f9fc; font-weight: 600; color: #333; cursor: pointer; user-select: none; white-space: nowrap; }
.data-table th:hover { background: #ecf5ff; }
.data-table tbody tr:hover { background-color: #f5f7fa; }
.sort-hint { color: #c0c4cc; }
.up { color: #f56c6c; }
.down { color: #67c23a; }
.modal-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  border-top: 1px solid #ebeef5;
}
.pagination { display: flex; align-items: center; gap: 12px; }
.pagination button {
  padding: 4px 12px; background: #ecf5ff; color: #409eff; border: 1px solid #b3d8ff;
  border-radius: 4px; cursor: pointer; font-size: 12px;
}
.pagination button:disabled { opacity: 0.5; cursor: not-allowed; }
.pagination span { font-size: 13px; color: #666; }
.total-info { font-size: 12px; color: #999; }
</style>
