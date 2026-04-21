<template>
  <div class="module-card">
    <header class="module-header">
      <h3>连板</h3>
      <div title="连板高度/炸板数">
        <template v-if="!showYesterday">{{ streakHeight || '-' }} / {{ brokenBoard || '-' }}</template>
        <template v-else>昨 {{ yesterdayStreakHeight || '-' }}板</template>
      </div>
      <button class="btn-compare" :class="{ active: showYesterday }" @click="showYesterday = !showYesterday">
        {{ showYesterday ? '昨日连板' : '今日连板' }}
      </button>
      <button @click="refresh" :disabled="loading" class="btn-refresh">
        {{ loading ? '加载中...' : '刷新' }}
      </button>
    </header>
    <div class="module-content table-container">
      <table class="data-table">
        <thead><tr>
          <th @click="sortTable('code')">股票代码 ▼</th>
          <th @click="sortTable('name')">股票名称 ▼</th>
          <th @click="sortTable('days')">连板数 ▼</th>
        </tr></thead>
        <tbody>
          <tr
            v-for="item in sortedList.slice(0, 10)"
            :key="item.code"
          >
            <td>{{ item.code }}</td>
            <td>{{ item.name }}</td>
            <td>{{ item.days }}</td>
          </tr>
        </tbody>
      </table>
      <div class="table-footer">
        <span>共{{ displayList.length }}只{{ showYesterday ? '昨' : '' }}连板股</span>
        <button v-if="displayList.length > 10" class="btn-more" @click="showDetail = true">更多...</button>
      </div>
    </div>
    <DetailModal
      :visible="showDetail"
      :title="showYesterday ? '昨日连板详情' : '今日连板详情'"
      :columns="detailColumns"
      :data="displayList"
      @close="showDetail = false"
    />
  </div>
</template>

<script>
import DetailModal from './DetailModal.vue'
import request from '@/utils/request'

export default {
  name: 'StreakCard',
  components: { DetailModal },
  props: {
    brokenBoard: { type: Number, default: 0 }
  },
  data () {
    return {
      streakStocks: [],
      yesterdayStreakStocks: [],
      streakHeight: 0,
      yesterdayStreakHeight: 0,
      loading: false,
      showYesterday: false,
      sortKey: '',
      sortOrder: 1,
      showDetail: false,
      detailColumns: [
        { key: 'code', label: '股票代码' },
        { key: 'name', label: '股票名称' },
        { key: 'days', label: '连板数' },
        { key: 'price', label: '最新价' },
        { key: 'change', label: '涨跌幅' },
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
    displayList () {
      return this.showYesterday ? this.yesterdayStreakStocks : this.streakStocks
    },
    sortedList () {
      if (!this.sortKey) return this.displayList
      const arr = [...this.displayList]
      arr.sort((a, b) => {
        let va = a[this.sortKey]
        let vb = b[this.sortKey]
        if (this.sortKey === 'days') {
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
        const d = await request({ url: '/api/shichang/streak', method: 'GET' })
        this.streakStocks = d.streakStocks || []
        this.streakHeight = d.streakHeight || 0
        this.yesterdayStreakStocks = d.yesterdayStreakStocks || []
        this.yesterdayStreakHeight = d.yesterdayStreakHeight || 0
      } catch (e) {
        console.error('连板刷新失败:', e)
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
.btn-compare {
  padding: 4px 10px; background: #f0f2f5; color: #666; border: 1px solid #dcdfe6;
  border-radius: 4px; cursor: pointer; font-size: 12px; user-select: none; transition: all 0.15s ease;
}
.btn-compare.active { background: #409eff; color: #fff; border-color: #409eff; }
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
.btn-more {
  background: none; border: none; color: #409eff; cursor: pointer;
  font-size: 12px; padding: 4px 8px;
}
.btn-more:hover { text-decoration: underline; }
</style>
