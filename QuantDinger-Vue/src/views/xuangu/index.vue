<template>
  <div class="xuangu-container">
    <div class="stock-screener-app">
      <!-- 顶部市场选择 -->
      <div class="market-filters">
        <el-radio-group v-model="selectedMarket" size="medium" @change="updateAiQuery">
          <el-radio-button label="全部"></el-radio-button>
          <el-radio-button label="A股"></el-radio-button>
          <el-radio-button label="沪深300"></el-radio-button>
          <el-radio-button label="中证500"></el-radio-button>
          <el-radio-button label="科创板"></el-radio-button>
          <el-radio-button label="创业板"></el-radio-button>
          <el-radio-button label="港股"></el-radio-button>
          <el-radio-button label="美股"></el-radio-button>
          <el-radio-button label="ETF基金"></el-radio-button>
        </el-radio-group>
      </div>

      <!-- AI选股输入框 -->
      <div class="ai-search-container">
        <el-popover
          placement="bottom-start"
          width="100%"
          trigger="click"
          v-model="filterDialogVisible"
          popper-class="filter-popover"
          :visible-arrow="false">
          <el-button slot="reference" type="info" icon="el-icon-setting" class="filter-trigger-btn">筛选条件</el-button>
          <FilterPanel
            :filters="filters"
            ref="filterPanel"
            @update:filters="onFiltersUpdate"
            @change="updateAiQuery"
          />
        </el-popover>
        <el-input
          v-model="aiQuery"
          type="textarea"
          :rows="2"
          placeholder="输入自然语言选股条件，如：'市盈率低于20的科技股' 或 '近5日突破60日均线的银行股'"
          class="ai-input"
          @input="handleAiInput"
        ></el-input>
        <el-button type="primary" @click="onSearch" icon="el-icon-search" :loading="searchLoading" class="search-btn">智能搜索</el-button>
      </div>

      <!-- ===== 自选股 + 搜索结果 左右排列 ===== -->
      <div class="content-body">

        <!-- 左侧自选股 -->
        <div class="watchlist-side">
          <div class="watchlist-side-header">
            <span class="wl-side-title"><i class="el-icon-star-on"></i> 自选股</span>
            <el-button type="text" size="mini" icon="el-icon-plus" @click="showAddWatchlistDialog = true"></el-button>
          </div>
          <div class="watchlist-side-list" v-loading="watchlistLoading">
            <div
              v-for="stock in watchlist"
              :key="`${stock.market}-${stock.symbol}`"
              class="wl-side-item"
              :class="{ active: selectedWatchlistKey === `${stock.market}:${stock.symbol}` }"
              @click="selectWatchlistStock(stock)"
            >
              <div class="wl-si-left">
                <span class="wl-si-symbol">{{ stock.symbol }}</span>
                <span class="wl-si-name" v-if="stock.name && stock.name !== stock.symbol">{{ stock.name }}</span>
              </div>
              <div class="wl-si-right">
                <span class="wl-si-price" v-if="watchlistPrices[`${stock.market}:${stock.symbol}`]">
                  {{ formatPrice(watchlistPrices[`${stock.market}:${stock.symbol}`].price) }}
                </span>
                <span
                  class="wl-si-change"
                  v-if="watchlistPrices[`${stock.market}:${stock.symbol}`]"
                  :class="(watchlistPrices[`${stock.market}:${stock.symbol}`].change || 0) >= 0 ? 'up' : 'down'"
                >
                  {{ (watchlistPrices[`${stock.market}:${stock.symbol}`].change || 0) >= 0 ? '+' : '' }}{{ formatRate(watchlistPrices[`${stock.market}:${stock.symbol}`].change) }}%
                </span>
              </div>
              <span class="wl-si-remove" @click.stop="removeFromWatchlist(stock)"><i class="el-icon-close"></i></span>
            </div>
            <div v-if="!watchlistLoading && watchlist.length === 0" class="wl-side-empty">
              暂无自选股
            </div>
          </div>
        </div>

        <!-- 右侧结果表格 -->
        <div class="result-panel">
          <div class="table-toolbar">
            <el-button type="warning" size="small" icon="el-icon-star-off" @click="addSelectedToWatchlist" :disabled="selectedRows.length === 0 || !selectedRows.some(r => r.code)">
              加自选 <span v-if="selectedRows.length > 0">({{ selectedRows.length }})</span>
            </el-button>
            <div class="toolbar-right">
              <el-button type="success" size="small" icon="el-icon-star-on" @click="openSaveDialog">保存策略</el-button>
              <el-button type="info" size="small" icon="el-icon-folder-opened" @click="openMyStrategies">我的策略</el-button>
            </div>
          </div>
          <el-table
            :data="paginatedData"
            style="width: 100%"
            :default-sort="{ prop: 'code', order: 'ascending' }"
            @sort-change="handleSortChange"
            @selection-change="handleSelectionChange"
            stripe
            size="small"
            :row-class-name="tableRowClassName"
          >
            <el-table-column type="selection" width="40" fixed></el-table-column>
            <el-table-column prop="code" label="代码" sortable="custom" min-width="10" fixed>
              <template slot-scope="{ row }">
                <span class="stock-code-cell">{{ row.code }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="name" label="名称" sortable="custom" min-width="10">
              <template slot-scope="{ row }">
                <span class="stock-name-cell">{{ row.name }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="new_price" label="最新价" sortable="custom" min-width="10" align="right">
              <template slot-scope="{ row }">
                <span class="price-cell" v-if="row.new_price != null">{{ formatPrice(row.new_price) }}</span>
                <span v-else class="cell-na">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="change_rate" label="涨跌幅" sortable="custom" min-width="10" align="right">
              <template slot-scope="{ row }">
                <span v-if="row.change_rate != null" :class="row.change_rate >= 0 ? 'text-red' : 'text-green'" class="change-cell">
                  {{ row.change_rate >= 0 ? '+' : '' }}{{ formatRate(row.change_rate) }}%
                </span>
                <span v-else class="cell-na">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="turnoverrate" label="换手率" sortable="custom" min-width="10" align="right">
              <template slot-scope="{ row }">
                <span v-if="row.turnoverrate != null">{{ formatRate(row.turnoverrate) }}%</span>
                <span v-else class="cell-na">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="volume_ratio" label="量比" sortable="custom" min-width="8" align="right">
              <template slot-scope="{ row }">
                <span v-if="row.volume_ratio != null">{{ row.volume_ratio }}</span>
                <span v-else class="cell-na">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="deal_amount" label="成交额" sortable="custom" min-width="10" align="right">
              <template slot-scope="{ row }">
                <span v-if="row.deal_amount != null">{{ formatAmount(row.deal_amount) }}</span>
                <span v-else class="cell-na">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="pe9" label="市盈率" sortable="custom" min-width="8" align="right">
              <template slot-scope="{ row }">
                <span v-if="row.pe9 != null">{{ row.pe9 }}</span>
                <span v-else class="cell-na">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="total_market_cap" label="总市值" sortable="custom" min-width="10" align="right">
              <template slot-scope="{ row }">
                <span v-if="row.total_market_cap != null">{{ formatBigAmount(row.total_market_cap) }}</span>
                <span v-else class="cell-na">—</span>
              </template>
            </el-table-column>
          </el-table>
          <div class="pagination-container">
            <el-pagination
              @size-change="handleSizeChange"
              @current-change="handleCurrentChange"
              :current-page="currentPage"
              :page-sizes="[10, 20, 50, 100]"
              :page-size="pageSize"
              layout="total, sizes, prev, pager, next, jumper"
              :total="totalItems"
            ></el-pagination>
          </div>
        </div>

      </div>

      <!-- 添加自选股弹窗 -->
      <el-dialog title="添加自选股" :visible.sync="showAddWatchlistDialog" width="400px">
        <el-form @submit.native.prevent>
          <el-form-item label="股票代码">
            <el-input v-model="watchlistInputCode" placeholder="输入股票代码，如 000001" @keyup.enter.native="addToWatchlistByInput"></el-input>
          </el-form-item>
          <el-form-item label="股票名称">
            <el-input v-model="watchlistInputName" placeholder="可选"></el-input>
          </el-form-item>
        </el-form>
        <span slot="footer">
          <el-button @click="showAddWatchlistDialog = false">取消</el-button>
          <el-button type="primary" @click="addToWatchlistByInput">添加</el-button>
        </span>
      </el-dialog>

      <!-- 保存策略对话框 -->
      <el-dialog title="保存选股策略" :visible.sync="saveDialogVisible" width="420px">
        <el-form :model="saveForm" label-width="70px">
          <el-form-item label="名称">
            <el-input v-model="saveForm.name" placeholder="如：低估值银行股" maxlength="100" show-word-limit></el-input>
          </el-form-item>
          <el-form-item label="描述">
            <el-input v-model="saveForm.description" type="textarea" :rows="2" placeholder="可选" maxlength="500"></el-input>
          </el-form-item>
          <el-form-item label="条件">
            <div class="strategy-conditions-preview">{{ aiQuery || '（无筛选条件）' }}</div>
          </el-form-item>
        </el-form>
        <span slot="footer">
          <el-button @click="saveDialogVisible = false">取消</el-button>
          <el-button type="primary" @click="doSaveStrategy" :loading="saving">保存</el-button>
        </span>
      </el-dialog>

      <!-- 我的策略对话框 -->
      <el-dialog title="我的策略" :visible.sync="strategiesDialogVisible" width="600px">
        <div v-if="strategiesLoading" style="text-align:center;padding:20px;">
          <i class="el-icon-loading" style="font-size:24px;"></i>
        </div>
        <div v-else-if="myStrategies.length === 0" style="text-align:center;color:#909399;padding:30px;">
          暂无保存的策略
        </div>
        <el-table v-else :data="myStrategies" style="width:100%" :show-header="false" size="small">
          <el-table-column prop="name" label="名称" width="160">
            <template slot-scope="{ row }">
              <strong>{{ row.name }}</strong>
              <div style="color:#909399;font-size:12px;">{{ row.updated_at }}</div>
            </template>
          </el-table-column>
          <el-table-column prop="description" label="描述">
            <template slot-scope="{ row }">
              <div style="color:#606266;font-size:13px;">{{ row.description || '—' }}</div>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="160" align="right">
            <template slot-scope="{ row }">
              <el-button size="mini" type="primary" @click="loadStrategy(row)">加载</el-button>
              <el-button size="mini" type="danger" @click="deleteStrategy(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <span slot="footer">
          <el-button @click="strategiesDialogVisible = false">关闭</el-button>
        </span>
      </el-dialog>

    </div>
  </div>
</template>

<script>
import Vue from 'vue'
import ElementUI from 'element-ui'
import 'element-ui/lib/theme-chalk/index.css'
import FilterPanel, { getDefaultFilters } from './components/FilterPanel.vue'
import { getWatchlist, addWatchlist, removeWatchlist, getWatchlistPrices } from '@/api/market'
import { getUserInfo } from '@/api/login'

Vue.config.productionTip = false
Vue.use(ElementUI)

export default {
  name: 'StockScreener',
  components: { FilterPanel },
  data () {
    return {
      selectedMarket: '全部',
      aiQuery: '',
      searchLoading: false,
      filterDialogVisible: false,
      isUpdatingFromAi: false,

      // 自选股（API驱动）
      userId: null,
      watchlist: [],
      watchlistLoading: false,
      watchlistPrices: {},
      selectedWatchlistKey: null,
      showAddWatchlistDialog: false,
      watchlistInputCode: '',
      watchlistInputName: '',

      filters: getDefaultFilters(),

      tableData: [],
      selectedRows: [],
      currentPage: 1,
      pageSize: 50,
      totalItems: 0,
      lastUpdateTimestamp: 0,

      saveDialogVisible: false,
      saveForm: { name: '', description: '' },
      saving: false,
      strategiesDialogVisible: false,
      strategiesLoading: false,
      myStrategies: []
    }
  },
  computed: {
    paginatedData () {
      const start = (this.currentPage - 1) * this.pageSize
      return this.tableData.slice(start, start + this.pageSize)
    }
  },
  methods: {
    // 格式化工具
    formatPrice (v) {
      if (v == null) return '—'
      const n = Number(v)
      if (isNaN(n)) return v
      if (n >= 10000) return (n / 10000).toFixed(1) + '万'
      return n.toFixed(2)
    },
    formatRate (v) {
      if (v == null) return '—'
      return Number(v).toFixed(2)
    },
    formatVolume (v) {
      if (v == null) return '—'
      const n = Number(v)
      if (n >= 100000000) return (n / 100000000).toFixed(1) + '亿'
      if (n >= 10000) return (n / 10000).toFixed(1) + '万'
      return n.toFixed(0)
    },
    formatAmount (v) {
      if (v == null) return '—'
      const n = Number(v)
      if (isNaN(n)) return v
      if (n >= 100000000) return (n / 100000000).toFixed(2) + '亿'
      if (n >= 10000) return (n / 10000).toFixed(1) + '万'
      return n.toFixed(0)
    },
    formatBigAmount (v) {
      if (v == null) return '—'
      const n = Number(v)
      if (isNaN(n)) return v
      if (n >= 1000000000000) return (n / 1000000000000).toFixed(2) + '万亿'
      if (n >= 100000000) return (n / 100000000).toFixed(1) + '亿'
      if (n >= 10000) return (n / 10000).toFixed(1) + '万'
      return n.toFixed(0)
    },
    _genId (len) {
      const chars = 'abcdefghijklmnopqrstuvwxyz0123456789'
      return Array.from({ length: len }, () => chars[Math.floor(Math.random() * chars.length)]).join('')
    },
    _safeParseFloat (val) {
      if (val == null || val === '' || val === '-' || val === '--') return null
      const n = parseFloat(val)
      return isNaN(n) ? null : n
    },
    _authHeaders () {
      const token = localStorage.getItem('token')
      const h = { 'Content-Type': 'application/json' }
      if (token) h['Authorization'] = `Bearer ${token}`
      return h
    },

    tableRowClassName ({ row }) {
      if (row.change_rate != null && row.change_rate > 0) return 'row-up'
      if (row.change_rate != null && row.change_rate < 0) return 'row-down'
      return ''
    },

    onFiltersUpdate (newFilters) {
      this.filters = { ...this.filters, ...newFilters }
    },

    // ===== 搜索 =====
    async onSearch () {
      const kw = (this.aiQuery || '').trim()
      if (!kw) {
        this.$message.warning('请输入选股关键词')
        return
      }
      this.currentPage = 1
      await this.performEastMoneySearch(kw)
    },

    async performEastMoneySearch (kw) {
      const API_URL = 'https://np-tjxg-b.eastmoney.com/api/smart-tag/stock/v3/pw/search-code'
      this.searchLoading = true
      try {
        const body = {
          needAmbiguousSuggest: true, pageSize: 200, pageNo: 1,
          fingerprint: this._genId(32), matchWord: '', shareToGuba: false,
          timestamp: String(Date.now()),
          requestId: this._genId(32) + String(Date.now()),
          removedConditionIdList: [], ownSelectAll: false, needCorrect: true,
          client: 'WEB', product: '', needShowStockNum: false,
          biz: 'web_ai_select_stocks', xcId: '', gids: [], dxInfoNew: [],
          keyWordNew: kw,
          customDataNew: JSON.stringify([{ type: 'text', value: kw, extra: '' }])
        }
        const resp = await fetch(API_URL, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        })
        if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${resp.statusText}`)
        const data = await resp.json()

        if (String(data.code) !== '100') {
          this.$message.error(data.msg || '搜索失败')
          this.tableData = []
          this.totalItems = 0
          return
        }

        const res = data.data && data.data.result
        const stocks = (res && res.dataList) || []
        this.totalItems = (res && res.total) || stocks.length

        this.tableData = stocks.map(s => ({
          code: s.SECURITY_CODE || '',
          name: s.SECURITY_SHORT_NAME || '',
          industry: s.INDUSTRY || '',
          concept: s.CONCEPT || '',
          new_price: this._safeParseFloat(s.NEWEST_PRICE),
          change_rate: this._safeParseFloat(s.CHG),
          high_price: this._safeParseFloat(s.HIGH_PRICE),
          low_price: this._safeParseFloat(s.LOW_PRICE),
          pre_close_price: this._safeParseFloat(s.PRE_CLOSE_PRICE),
          volume: this._safeParseFloat(s.TRADE_VOLUME),
          deal_amount: s.TRADING_VOLUMES || s.TRADE_AMOUNT || null,
          volume_ratio: s.QRR || null,
          turnoverrate: this._safeParseFloat(s.TURNOVER_RATE),
          amplitude: this._safeParseFloat(s.AMPLITUDE),
          pe9: s.PE_DYNAMIC || s.PE9 || null,
          pbnewmrq: s.PB_NEW_MRQ || null,
          total_market_cap: s.TOEAL_MARKET_VALUE || s.TOTAL_MARKET_CAP || null,
          free_cap: s.FREE_CAP || null
        }))

        if (this.tableData.length === 0) {
          this.$message.info('未找到匹配的股票')
        }
      } catch (err) {
        if (err.name === 'TypeError' && err.message === 'Failed to fetch') {
          this.$message.error('搜索失败：网络请求被阻止（可能是CORS限制），请检查网络或使用代理')
        } else {
          this.$message.error('搜索失败: ' + err.message)
        }
        this.tableData = []
        this.totalItems = 0
      } finally {
        this.searchLoading = false
      }
    },

    // ===== 自选股管理（API驱动，参考ai-analysis） =====
    async loadUserInfo () {
      try {
        const res = await getUserInfo()
        if (res && res.code === 1 && res.data) {
          this.userId = res.data.id
          this.$store.commit('SET_INFO', res.data)
        }
      } catch (e) { /* silent */ }
      this.loadWatchlist()
    },

    async loadWatchlist () {
      if (!this.userId) return
      this.watchlistLoading = true
      try {
        const res = await getWatchlist({ userid: this.userId })
        if (res && res.code === 1 && res.data) {
          this.watchlist = res.data
          this.loadWatchlistPrices()
        }
      } catch (e) { /* silent */ } finally {
        this.watchlistLoading = false
      }
    },

    async loadWatchlistPrices () {
      if (!this.watchlist || this.watchlist.length === 0) return
      try {
        const watchlistData = this.watchlist.map(item => ({ market: item.market, symbol: item.symbol }))
        const res = await getWatchlistPrices({ watchlist: watchlistData })
        if (res && res.code === 1 && res.data) {
          const pricesObj = {}
          res.data.forEach(item => {
            pricesObj[`${item.market}:${item.symbol}`] = { price: item.price || 0, change: item.changePercent || 0 }
          })
          this.watchlistPrices = pricesObj
        }
      } catch (e) { /* silent */ }
    },

    startWatchlistPriceRefresh () {
      // 加载一次即可，不需要定时刷新
      this.loadWatchlistPrices()
    },

    selectWatchlistStock (stock) {
      this.selectedWatchlistKey = `${stock.market}:${stock.symbol}`
      this.aiQuery = stock.name || stock.symbol
      this.$nextTick(() => this.onSearch())
    },

    async addToWatchlistByInput () {
      const code = (this.watchlistInputCode || '').trim()
      if (!code) { this.$message.warning('请输入股票代码'); return }
      if (!this.userId) { this.$message.warning('用户信息未加载'); return }
      try {
        const res = await addWatchlist({ userid: this.userId, market: 'CNStock', symbol: code.toUpperCase(), name: (this.watchlistInputName || '').trim() })
        if (res && res.code === 1) {
          this.$message.success(`已添加自选: ${code}`)
          this.showAddWatchlistDialog = false
          this.watchlistInputCode = ''
          this.watchlistInputName = ''
          this.loadWatchlist()
        } else {
          this.$message.error(res?.msg || '添加失败')
        }
      } catch (e) { this.$message.error('添加失败: ' + e.message) }
    },

    async addSelectedToWatchlist () {
      if (this.selectedRows.length === 0) return
      if (!this.userId) { this.$message.warning('用户信息未加载'); return }
      let added = 0
      for (const r of this.selectedRows.filter(x => x.code)) {
        try {
          const res = await addWatchlist({ userid: this.userId, market: 'CNStock', symbol: r.code, name: r.name || '' })
          if (res && res.code === 1) added++
        } catch (e) { /* skip */ }
      }
      if (added > 0) {
        this.$message.success(`已添加 ${added} 只股票到自选股`)
      } else {
        this.$message.info('选中的股票可能已在自选股中')
      }
      this.loadWatchlist()
    },

    async removeFromWatchlist (stock) {
      try {
        const res = await removeWatchlist({ userid: this.userId, symbol: stock.symbol, market: stock.market })
        if (res && res.code === 1) {
          this.$message.success(`已移除: ${stock.name || stock.symbol}`)
          if (this.selectedWatchlistKey === `${stock.market}:${stock.symbol}`) this.selectedWatchlistKey = null
          this.loadWatchlist()
        } else {
          this.$message.error(res?.msg || '移除失败')
        }
      } catch (e) { this.$message.error('移除失败: ' + e.message) }
    },

    // ===== 策略管理 =====
    openSaveDialog () {
      if (!this.aiQuery.trim()) {
        this.$message.warning('请先输入搜索关键词再保存')
        return
      }
      this.saveForm = { name: this.aiQuery.trim().substring(0, 20), description: '' }
      this.saveDialogVisible = true
    },

    async doSaveStrategy () {
      if (!this.saveForm.name.trim()) {
        this.$message.warning('请输入策略名称')
        return
      }
      this.saving = true
      try {
        const resp = await fetch('/api/xuangu/favorites', {
          method: 'POST',
          headers: this._authHeaders(),
          body: JSON.stringify({
            name: this.saveForm.name.trim(),
            conditions: { keyword: this.aiQuery },
            description: this.saveForm.description.trim()
          })
        })
        const data = await resp.json()
        if (data.code === 0) {
          this.$message.success(data.msg || '保存成功')
          this.saveDialogVisible = false
        } else {
          this.$message.error(data.msg || '保存失败')
        }
      } catch (e) {
        this.$message.error('保存失败: ' + e.message)
      } finally {
        this.saving = false
      }
    },

    async openMyStrategies () {
      this.strategiesDialogVisible = true
      this.strategiesLoading = true
      try {
        const resp = await fetch('/api/xuangu/favorites', { method: 'GET', headers: this._authHeaders() })
        const data = await resp.json()
        this.myStrategies = data.code === 0 ? (data.data || []) : []
      } catch (e) {
        this.$message.error('加载失败: ' + e.message)
      } finally {
        this.strategiesLoading = false
      }
    },

    loadStrategy (strategy) {
      let cond = strategy.conditions
      if (typeof cond === 'string') { try { cond = JSON.parse(cond) } catch (_) { cond = {} } }
      if (!cond || (!cond.keyword && !cond.query)) {
        this.$message.warning('策略条件格式不兼容')
        return
      }
      this.aiQuery = cond.keyword || cond.query || ''
      this.strategiesDialogVisible = false
      if (this.aiQuery.trim()) {
        this.$nextTick(() => this.onSearch())
      } else {
        this.$message.info('已加载策略条件，请点击智能搜索')
      }
    },

    async deleteStrategy (strategy) {
      try { await this.$confirm(`确定删除策略「${strategy.name}」？`, '确认删除', { type: 'warning' }) } catch (_) { return }
      try {
        const resp = await fetch(`/api/xuangu/favorites/${strategy.id}`, { method: 'DELETE', headers: this._authHeaders() })
        const data = await resp.json()
        if (data.code === 0) {
          this.$message.success('已删除')
          this.myStrategies = this.myStrategies.filter(s => s.id !== strategy.id)
        } else {
          this.$message.error(data.msg || '删除失败')
        }
      } catch (e) {
        this.$message.error('删除失败: ' + e.message)
      }
    },

    // ===== 输入框双向同步（简化版 - 保留原有解析逻辑） =====
    parseNumber (str) {
      if (!str) return null
      str = String(str).replace(/,/g, '').trim()
      if (str === '∞' || str === '-∞') return null
      if (str.endsWith('千亿')) return parseFloat(str) * 100000000000
      if (str.endsWith('亿')) return parseFloat(str) * 100000000
      if (str.endsWith('万')) return parseFloat(str) * 10000
      if (str.endsWith('手')) return parseFloat(str)
      if (str.endsWith('元')) return parseFloat(str)
      if (str.endsWith('%')) return parseFloat(str)
      return parseFloat(str)
    },

    handleAiInput (value) {
      this.isUpdatingFromAi = true
      this.resetAllFilters()
      this.parseFilterFromText(value)
      setTimeout(() => { this.isUpdatingFromAi = false }, 100)
    },

    updateAiQuery () {
      if (this.isUpdatingFromAi) return
      const now = Date.now()
      if (now - this.lastUpdateTimestamp < 200) return
      this.lastUpdateTimestamp = now

      const parts = []
      if (this.selectedMarket !== '全部') parts.push(this.selectedMarket)

      // 基本面
      if (this.filters.pe_min != null || this.filters.pe_max != null) parts.push(`PE在${this.filters.pe_min || 0}到${this.filters.pe_max || '∞'}之间`)
      if (this.filters.pb_min != null || this.filters.pb_max != null) parts.push(`PB在${this.filters.pb_min || 0}到${this.filters.pb_max || '∞'}之间`)
      if (this.filters.dividend_min != null && this.filters.dividend_min > 0) parts.push(`股息率不低于${this.filters.dividend_min}%`)
      if (this.filters.roe_min != null && this.filters.roe_min > -50) parts.push(`ROE不低于${this.filters.roe_min}%`)
      if (this.filters.sale_gpr_min != null && this.filters.sale_gpr_min > -50) parts.push(`毛利率不低于${this.filters.sale_gpr_min}%`)
      // 成长
      ;(this.filters.growth_indicators || []).forEach(k => {
        const m = { netprofit_yoy_ratio: '净利增长>15%', toi_yoy_ratio: '营收增长>15%', basiceps_yoy_ratio: '每股收益增长>10%', income_growthrate_3y: '营收3年复合增长 > 10%', netprofit_growthrate_3y: '净利润3年复合增长 > 10%' }
        if (m[k]) parts.push(m[k])
      })
      ;(this.filters.quality_indicators || []).forEach(k => { if (k === 'per_netcash_operate') parts.push('经营现金流为正') })

      // 技术面 - 均线突破
      ;(this.filters.ma_breakthrough || []).forEach(k => {
        const m = { breakup_ma_5days: '突破5日线', breakup_ma_10days: '突破10日线', breakup_ma_20days: '突破20日线', breakup_ma_60days: '突破60日线', long_avg_array: '长期均线多头排列' }
        if (m[k]) parts.push(m[k])
      })
      ;(this.filters.tech_signals || []).forEach(k => {
        const m = { macd_golden_fork: 'MACD金叉', kdj_golden_fork: 'KDJ金叉', break_through: '突破形态', upper_large_volume: '放量上涨', down_narrow_volume: '缩量下跌' }
        if (m[k]) parts.push(m[k])
      })
      // K线形态
      ;(this.filters.k_classic || []).forEach(k => {
        const m = { one_dayang_line: '大阳线', two_dayang_lines: '两阳夹一阴', rise_sun: '阳包阴', morning_star: '早晨之星', evening_star: '黄昏之星', shooting_star: '射击之星', three_black_crows: '三只乌鸦', hammer: '锤头', inverted_hammer: '倒锤头', doji: '十字星', long_legged_doji: '长腿十字线', gravestone: '墓碑线', dragonfly: '蜻蜓线', two_flying_crows: '双飞乌鸦', lotus_emerge: '出水芙蓉', low_open_high: '低开高走', huge_volume: '巨量', bottom_cross_harami: '底部十字孕线', top_cross_harami: '顶部十字孕线' }
        if (m[k]) parts.push(m[k])
      })
      ;(this.filters.k_intraday || []).forEach(k => {
        const m = { tail_plate_rise: '尾盘拉升', intraday_pressure: '盘中打压', intraday_rise: '盘中拉升', quick_rebound: '快速反弹' }
        if (m[k]) parts.push(m[k])
      })
      ;(this.filters.k_other || []).forEach(k => { if (k === 'limit_up') parts.push('一字涨停'); if (k === 'limit_down') parts.push('一字跌停') })

      // 资金面
      ;(this.filters.capital_flow || []).forEach(k => {
        const m = { low_funds_inflow: '主力资金净流入', high_funds_outflow: '主力资金净流出', netinflow_3days: '近3日资金净流入', netinflow_5days: '近5日资金净流入' }
        if (m[k]) parts.push(m[k])
      })
      if (this.filters.volume_ratio_min != null && this.filters.volume_ratio_min > 0) parts.push(`量比不低于${this.filters.volume_ratio_min}`)
      if (this.filters.turnoverrate_min != null && this.filters.turnoverrate_min > 0) parts.push(`换手率不低于${this.filters.turnoverrate_min}%`)
      // 行情指标
      if (this.filters.mi_volume_ratio_min != null && this.filters.mi_volume_ratio_min > 0) parts.push(`量比≥${this.filters.mi_volume_ratio_min}`)
      if (this.filters.mi_turnover_rate_min != null && this.filters.mi_turnover_rate_min > 0) parts.push(`换手率≥${this.filters.mi_turnover_rate_min}%`)
      if (this.filters.mi_volume_min != null && this.filters.mi_volume_min > 0) parts.push(`成交量≥${this.filters.mi_volume_min}手`)
      if (this.filters.mi_amount_min != null && this.filters.mi_amount_min > 0) parts.push(`成交额≥${this.filters.mi_amount_min}元`)
      ;(this.filters.institutional_holding || []).forEach(k => {
        const m = { org_survey_3m: '近3月有机构调研', allcorp_fund_ratio: '基金重仓', allcorp_qs_ratio: '券商重仓' }
        if (m[k]) parts.push(m[k])
      })
      ;(this.filters.tiger_participant || []).forEach(k => {
        if (k === 'inst_participated') parts.push('机构参与')
        if (k === 'dept_participated') parts.push('营业部参与')
      })

      // 概念/行业
      if ((this.filters.industry || []).length > 0) parts.push(`属于行业(${this.filters.industry.join(', ')})`)
      if ((this.filters.concept || []).length > 0) parts.push(`涉及概念(${this.filters.concept.join(', ')})`)

      // 新增基本面
      if (this.filters.ps_min != null || this.filters.ps_max != null) parts.push(`市销率${this.filters.ps_min || 0}~${this.filters.ps_max || '∞'}`)
      if (this.filters.pcf_min != null || this.filters.pcf_max != null) parts.push(`市现率${this.filters.pcf_min || 0}~${this.filters.pcf_max || '∞'}`)
      if (this.filters.dtsyl_min != null || this.filters.dtsyl_max != null) parts.push(`动态PE${this.filters.dtsyl_min || 0}~${this.filters.dtsyl_max || '∞'}`)
      if (this.filters.total_market_cap_min != null || this.filters.total_market_cap_max != null) parts.push(`总市值${this.filters.total_market_cap_min || 0}~${this.filters.total_market_cap_max || '∞'}`)
      if (this.filters.free_cap_min != null || this.filters.free_cap_max != null) parts.push(`流通市值${this.filters.free_cap_min || 0}~${this.filters.free_cap_max || '∞'}`)
      if (this.filters.basic_eps_min != null) parts.push(`每股收益≥${this.filters.basic_eps_min}`)
      if (this.filters.bvps_min != null) parts.push(`每股净资产≥${this.filters.bvps_min}`)
      if (this.filters.per_fcfe_min != null) parts.push(`每股自由现金流≥${this.filters.per_fcfe_min}`)
      if (this.filters.parent_netprofit_min != null) parts.push(`归母净利润≥${this.filters.parent_netprofit_min}`)
      if (this.filters.deduct_netprofit_min != null) parts.push(`扣非净利润≥${this.filters.deduct_netprofit_min}`)
      if (this.filters.total_operate_income_min != null) parts.push(`营业收入≥${this.filters.total_operate_income_min}`)
      if (this.filters.jroa_min != null) parts.push(`总资产报酬率≥${this.filters.jroa_min}%`)
      if (this.filters.roic_min != null) parts.push(`投资回报率≥${this.filters.roic_min}%`)
      if (this.filters.sale_npr_min_filter != null) parts.push(`销售净利率≥${this.filters.sale_npr_min_filter}%`)
      if (this.filters.debt_asset_ratio_max != null) parts.push(`资产负债率≤${this.filters.debt_asset_ratio_max}%`)
      if (this.filters.current_ratio_min != null) parts.push(`流动比率≥${this.filters.current_ratio_min}`)
      if (this.filters.speed_ratio_min != null) parts.push(`速动比率≥${this.filters.speed_ratio_min}`)
      if (this.filters.total_shares_min != null || this.filters.total_shares_max != null) parts.push(`总股本${this.filters.total_shares_min || 0}~${this.filters.total_shares_max || '∞'}`)
      if (this.filters.free_shares_min != null || this.filters.free_shares_max != null) parts.push(`流通股本${this.filters.free_shares_min || 0}~${this.filters.free_shares_max || '∞'}`)
      if (this.filters.holder_newest_min != null || this.filters.holder_newest_max != null) parts.push(`股东数${this.filters.holder_newest_min || 0}~${this.filters.holder_newest_max || '∞'}`)

      // 新增技术面
      ;(this.filters.ma_30_break || []).forEach(k => { if (k === 'breakup_ma_30days') parts.push('突破30日线') })
      ;(this.filters.kdj_signals || []).forEach(k => {
        const m = { kdj_golden_forkz: 'KDJ金叉Z', kdj_golden_forky: 'KDJ金叉Y', macd_golden_forkz: 'MACD金叉Z', macd_golden_forky: 'MACD金叉Y' }
        if (m[k]) parts.push(m[k])
      })
      ;(this.filters.pattern_signals || []).forEach(k => {
        const m = { power_fulgun: '乌云盖顶', pregnant: '孕线', black_cloud_tops: '黑云压顶', narrow_finish: '窄幅整理', reversing_hammer: '反转锤子', first_dawn: '第一天黎明', bearish_engulfing: '看跌吞没', upside_volume: '上攻放量', heaven_rule: '天道法则' }
        if (m[k]) parts.push(m[k])
      })
      ;(this.filters.consecutive_signals || []).forEach(k => {
        const m = { down_7days: '连续7天下跌', upper_8days: '连续8天上涨', upper_9days: '连续9天上涨', upper_4days: '连续4天上涨' }
        if (m[k]) parts.push(m[k])
      })
      ;(this.filters.volume_trend || []).forEach(k => {
        if (k === 'short_avg_array') parts.push('短期均线多头')
        if (k === 'restore_justice') parts.push('复权')
      })

      // 新增资金面
      if (this.filters.net_inflow_min != null) parts.push(`净流入≥${this.filters.net_inflow_min}`)
      if (this.filters.ddx_min != null) parts.push(`大单动向≥${this.filters.ddx_min}`)
      if (this.filters.netinflow_min_3d != null) parts.push(`3日净流入≥${this.filters.netinflow_min_3d}`)
      if (this.filters.netinflow_min_5d != null) parts.push(`5日净流入≥${this.filters.netinflow_min_5d}`)
      if (this.filters.changerate_3d_min != null) parts.push(`3日涨幅≥${this.filters.changerate_3d_min}%`)
      if (this.filters.changerate_5d_min != null) parts.push(`5日涨幅≥${this.filters.changerate_5d_min}%`)
      if (this.filters.changerate_10d_min != null) parts.push(`10日涨幅≥${this.filters.changerate_10d_min}%`)
      if (this.filters.changerate_ty_min != null || this.filters.changerate_ty_max != null) {
        parts.push(`年度涨幅${this.filters.changerate_ty_min != null ? this.filters.changerate_ty_min : '-∞'}~${this.filters.changerate_ty_max != null ? this.filters.changerate_ty_max : '∞'}%`)
      }

      // 股东机构
      if (this.filters.holder_change_3m_min != null) parts.push(`3月持股变动≥${this.filters.holder_change_3m_min}%`)
      if (this.filters.executive_change_3m_min != null) parts.push(`3月高管持股变动≥${this.filters.executive_change_3m_min}%`)
      if (this.filters.org_rating_filter) parts.push(`机构评级=${this.filters.org_rating_filter}`)
      if (this.filters.allcorp_ratio_min != null) parts.push(`机构持股比例≥${this.filters.allcorp_ratio_min}%`)
      if (this.filters.allcorp_fund_ratio_min != null) parts.push(`基金持股≥${this.filters.allcorp_fund_ratio_min}%`)
      if (this.filters.allcorp_qs_ratio_min != null) parts.push(`券商持股≥${this.filters.allcorp_qs_ratio_min}%`)
      if (this.filters.allcorp_qfii_ratio_min != null) parts.push(`QFII持股≥${this.filters.allcorp_qfii_ratio_min}%`)

      // 新高新低
      ;(this.filters.new_high_filter || []).forEach(k => {
        const m = { now_newhigh: '当前新高', now_newlow: '当前新低', high_recent_3days: '3天新高', high_recent_5days: '5天新高', high_recent_10days: '10天新高', high_recent_20days: '20天新高', high_recent_30days: '30天新高', low_recent_3days: '3天新低', low_recent_5days: '5天新低', low_recent_10days: '10天新低', low_recent_20days: '20天新低', low_recent_30days: '30天新低' }
        if (m[k]) parts.push(m[k])
      })
      ;(this.filters.win_market_filter || []).forEach(k => {
        if (k === 'win_market_3days') parts.push('3天战胜大盘')
        if (k === 'win_market_5days') parts.push('5天战胜大盘')
        if (k === 'win_market_10days') parts.push('10天战胜大盘')
        if (k === 'win_market_20days') parts.push('20天战胜大盘')
        if (k === 'win_market_30days') parts.push('30天战胜大盘')
      })
      ;(this.filters.hs_board_filter || []).forEach(k => {
        const m = { is_sz50: '上证50成分股', is_zz1000: '中证1000成分股', is_cy50: '创业板50成分股', is_issue_break: '已破板', is_bps_break: '已破净' }
        if (m[k]) parts.push(m[k])
      })

      // 派息与质押
      if (this.filters.par_dividend_min != null) parts.push(`派息率≥${this.filters.par_dividend_min}%`)
      if (this.filters.pledge_ratio_max != null) parts.push(`质押比例≤${this.filters.pledge_ratio_max}%`)
      if (this.filters.goodwill_max != null) parts.push(`商誉≤${this.filters.goodwill_max}`)

      // 限价/定增/质押
      ;(this.filters.limited_lift_filter || []).forEach(k => {
        const m = { limited_lift_6m: '限价上涨6月', limited_lift_1y: '限价上涨1年' }
        if (m[k]) parts.push(m[k])
      })
      ;(this.filters.directional_seo_filter || []).forEach(k => {
        const m = { directional_seo_1m: '定向增发1月', directional_seo_3m: '定向增发3月', directional_seo_6m: '定向增发6月', directional_seo_1y: '定向增发1年' }
        if (m[k]) parts.push(m[k])
      })
      ;(this.filters.equity_pledge_filter || []).forEach(k => {
        const m = { equity_pledge_1m: '股权质押1月', equity_pledge_3m: '股权质押3月', equity_pledge_6m: '股权质押6月', equity_pledge_1y: '股权质押1年' }
        if (m[k]) parts.push(m[k])
      })

      this.aiQuery = parts.join('; ')
    },

    resetAllFilters () {
      this.filters = getDefaultFilters()
      this.selectedMarket = '全部'
    },

    parseFilterFromText (text) {
      if (!text || !text.trim()) return
      const parts = text.split(/[;；]/).map(s => s.trim()).filter(Boolean)

      const parseRange = (str) => {
        const p = str.split('~')
        return { min: this.parseNumber(p[0]), max: p[1] ? this.parseNumber(p[1]) : null }
      }

      for (const part of parts) {
        let m

        // 市场选择
        if (/^(全部|A股|沪深300|中证500|科创板|创业板|港股|美股|ETF基金)$/.test(part)) { this.selectedMarket = part; continue }

        // 范围: PE/PB在X到Y之间
        if ((m = part.match(/PE在(.+?)到(.+?)之间/))) { this.filters.pe_min = this.parseNumber(m[1]); this.filters.pe_max = this.parseNumber(m[2]); continue }
        if ((m = part.match(/PB在(.+?)到(.+?)之间/))) { this.filters.pb_min = this.parseNumber(m[1]); this.filters.pb_max = this.parseNumber(m[2]); continue }

        // 不低于
        if ((m = part.match(/股息率不低于(.+?)%/))) { this.filters.dividend_min = parseFloat(m[1]); continue }
        if ((m = part.match(/ROE不低于(.+?)%/))) { this.filters.roe_min = parseFloat(m[1]); continue }
        if ((m = part.match(/毛利率不低于(.+?)%/))) { this.filters.sale_gpr_min = parseFloat(m[1]); continue }
        if ((m = part.match(/量比不低于(.+)/))) { this.filters.volume_ratio_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/换手率不低于(.+?)%/))) { this.filters.turnoverrate_min = parseFloat(m[1]); continue }

        // 成长/质量 checkbox
        if (part === '净利增长>15%') { this.filters.growth_indicators.push('netprofit_yoy_ratio'); continue }
        if (part === '营收增长>15%') { this.filters.growth_indicators.push('toi_yoy_ratio'); continue }
        if (part === '每股收益增长>10%') { this.filters.growth_indicators.push('basiceps_yoy_ratio'); continue }
        if (part === '经营现金流为正') { this.filters.quality_indicators.push('per_netcash_operate'); continue }

        // 均线突破
        if (part === '突破5日线') { this.filters.ma_breakthrough.push('breakup_ma_5days'); continue }
        if (part === '突破10日线') { this.filters.ma_breakthrough.push('breakup_ma_10days'); continue }
        if (part === '突破20日线') { this.filters.ma_breakthrough.push('breakup_ma_20days'); continue }
        if (part === '突破60日线') { this.filters.ma_breakthrough.push('breakup_ma_60days'); continue }
        if (part === '长期均线多头排列') { this.filters.ma_breakthrough.push('long_avg_array'); continue }

        // 技术指标
        if (part === 'MACD金叉') { this.filters.tech_signals.push('macd_golden_fork'); continue }
        if (part === 'KDJ金叉') { this.filters.tech_signals.push('kdj_golden_fork'); continue }
        if (part === '放量上涨') { this.filters.tech_signals.push('upper_large_volume'); continue }
        if (part === '缩量下跌') { this.filters.tech_signals.push('down_narrow_volume'); continue }
        if (part === '突破形态') { this.filters.tech_signals.push('break_through'); continue }

        // K线形态
        const kClassicMap = { '大阳线': 'one_dayang_line', '两阳夹一阴': 'two_dayang_lines', '阳包阴': 'rise_sun', '早晨之星': 'morning_star', '黄昏之星': 'evening_star', '射击之星': 'shooting_star', '三只乌鸦': 'three_black_crows', '锤头': 'hammer', '倒锤头': 'inverted_hammer', '十字星': 'doji', '长腿十字线': 'long_legged_doji', '墓碑线': 'gravestone', '蜻蜓线': 'dragonfly', '双飞乌鸦': 'two_flying_crows', '出水芙蓉': 'lotus_emerge', '低开高走': 'low_open_high', '巨量': 'huge_volume', '底部十字孕线': 'bottom_cross_harami', '顶部十字孕线': 'top_cross_harami' }
        if (kClassicMap[part]) { this.filters.k_classic.push(kClassicMap[part]); continue }

        const kIntradayMap = { '尾盘拉升': 'tail_plate_rise', '盘中打压': 'intraday_pressure', '盘中拉升': 'intraday_rise', '快速反弹': 'quick_rebound' }
        if (kIntradayMap[part]) { this.filters.k_intraday.push(kIntradayMap[part]); continue }

        const kOtherMap = { '一字涨停': 'limit_up', '一字跌停': 'limit_down' }
        if (kOtherMap[part]) { this.filters.k_other.push(kOtherMap[part]); continue }

        // 资金面
        if (part === '主力资金净流入') { this.filters.capital_flow.push('low_funds_inflow'); continue }
        if (part === '主力资金净流出') { this.filters.capital_flow.push('high_funds_outflow'); continue }
        if (part === '近3日资金净流入') { this.filters.capital_flow.push('netinflow_3days'); continue }
        if (part === '近5日资金净流入') { this.filters.capital_flow.push('netinflow_5days'); continue }
        if (part === '近3月有机构调研') { this.filters.institutional_holding.push('org_survey_3m'); continue }
        if (part === '基金重仓') { this.filters.institutional_holding.push('allcorp_fund_ratio'); continue }
        if (part === '券商重仓') { this.filters.institutional_holding.push('allcorp_qs_ratio'); continue }

        // 概念/行业
        if ((m = part.match(/属于行业\((.+)\)/))) { this.filters.industry = m[1].split(', '); continue }
        if ((m = part.match(/涉及概念\((.+)\)/))) { this.filters.concept = m[1].split(', '); continue }

        // 新高新低
        const highLowMap = { '当前新高': 'now_newhigh', '当前新低': 'now_newlow', '3天新高': 'high_recent_3days', '5天新高': 'high_recent_5days', '10天新高': 'high_recent_10days', '20天新高': 'high_recent_20days', '30天新高': 'high_recent_30days', '3天新低': 'low_recent_3days', '5天新低': 'low_recent_5days', '10天新低': 'low_recent_10days', '20天新低': 'low_recent_20days', '30天新低': 'low_recent_30days' }
        if (highLowMap[part]) { this.filters.new_high_filter.push(highLowMap[part]); continue }

        // 战胜大盘
        if ((m = part.match(/(\d+)天战胜大盘/))) { this.filters.win_market_filter.push(`win_market_${m[1]}days`); continue }

        // 连涨连跌
        const consecMap = { '连续4天上涨': 'upper_4days', '连续8天上涨': 'upper_8days', '连续9天上涨': 'upper_9days', '连续7天下跌': 'down_7days' }
        if (consecMap[part]) { this.filters.consecutive_signals.push(consecMap[part]); continue }

        // 限价/定增/质押
        if (part === '限价上涨6月') { this.filters.limited_lift_filter.push('limited_lift_6m'); continue }
        if (part === '限价上涨1年') { this.filters.limited_lift_filter.push('limited_lift_1y'); continue }
        if ((m = part.match(/定向增发(\d+[月年])/))) {
          const dmap = { '1月': 'directional_seo_1m', '3月': 'directional_seo_3m', '6月': 'directional_seo_6m', '1年': 'directional_seo_1y' }
          if (dmap[m[1]]) this.filters.directional_seo_filter.push(dmap[m[1]])
          continue
        }
        if ((m = part.match(/股权质押(\d+[月年])/))) {
          const dmap = { '1月': 'equity_pledge_1m', '3月': 'equity_pledge_3m', '6月': 'equity_pledge_6m', '1年': 'equity_pledge_1y' }
          if (dmap[m[1]]) this.filters.equity_pledge_filter.push(dmap[m[1]])
          continue
        }

        // 板块标识
        const boardMap = { '上证50成分股': 'is_sz50', '中证1000成分股': 'is_zz1000', '创业板50成分股': 'is_cy50', '已破净': 'is_bps_break', '已破板': 'is_issue_break' }
        if (boardMap[part]) { this.filters.hs_board_filter.push(boardMap[part]); continue }

        // 龙虎榜参与方
        if (part === '机构参与') { this.filters.tiger_participant.push('inst_participated'); continue }
        if (part === '营业部参与') { this.filters.tiger_participant.push('dept_participated'); continue }

        // 技术指标补充
        if (part === '突破30日线') { this.filters.ma_30_break.push('breakup_ma_30days'); continue }
        if (part === 'KDJ金叉Z') { this.filters.kdj_signals.push('kdj_golden_forkz'); continue }
        if (part === 'KDJ金叉Y') { this.filters.kdj_signals.push('kdj_golden_forky'); continue }
        if (part === 'MACD金叉Z') { this.filters.kdj_signals.push('macd_golden_forkz'); continue }
        if (part === 'MACD金叉Y') { this.filters.kdj_signals.push('macd_golden_forky'); continue }

        // pattern_signals
        const patternMap = { '乌云盖顶': 'power_fulgun', '孕线': 'pregnant', '黑云压顶': 'black_cloud_tops', '窄幅整理': 'narrow_finish', '反转锤子': 'reversing_hammer', '第一天黎明': 'first_dawn', '看跌吞没': 'bearish_engulfing', '上攻放量': 'upside_volume', '天道法则': 'heaven_rule' }
        if (patternMap[part]) { this.filters.pattern_signals.push(patternMap[part]); continue }

        // 基本面 ≥/≤
        if ((m = part.match(/每股收益≥(.+)/))) { this.filters.basic_eps_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/每股净资产≥(.+)/))) { this.filters.bvps_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/每股自由现金流≥(.+)/))) { this.filters.per_fcfe_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/归母净利润≥(.+)/))) { this.filters.parent_netprofit_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/扣非净利润≥(.+)/))) { this.filters.deduct_netprofit_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/营业收入≥(.+)/))) { this.filters.total_operate_income_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/总资产报酬率≥(.+?)%/))) { this.filters.jroa_min = parseFloat(m[1]); continue }
        if ((m = part.match(/投资回报率≥(.+?)%/))) { this.filters.roic_min = parseFloat(m[1]); continue }
        if ((m = part.match(/销售净利率≥(.+?)%/))) { this.filters.sale_npr_min_filter = parseFloat(m[1]); continue }
        if ((m = part.match(/资产负债率≤(.+?)%/))) { this.filters.debt_asset_ratio_max = parseFloat(m[1]); continue }
        if ((m = part.match(/资产负债率(.+?)~(.+?)%/))) { this.filters.debt_asset_ratio_min = this.parseNumber(m[1]); this.filters.debt_asset_ratio_max = this.parseNumber(m[2]); continue }
        if ((m = part.match(/流动比率≥(.+)/))) { this.filters.current_ratio_min = parseFloat(m[1]); continue }
        if ((m = part.match(/速动比率≥(.+)/))) { this.filters.speed_ratio_min = parseFloat(m[1]); continue }
        if ((m = part.match(/派息率≥(.+?)%/))) { this.filters.par_dividend_min = parseFloat(m[1]); continue }
        if ((m = part.match(/质押比例≤(.+?)%/))) { this.filters.pledge_ratio_max = parseFloat(m[1]); continue }
        if ((m = part.match(/商誉≤(.+)/))) { this.filters.goodwill_max = this.parseNumber(m[1]); continue }

        // 机构股东 ≥
        if ((m = part.match(/3月持股变动≥(.+?)%/))) { this.filters.holder_change_3m_min = parseFloat(m[1]); continue }
        if ((m = part.match(/3月高管持股变动≥(.+?)%/))) { this.filters.executive_change_3m_min = parseFloat(m[1]); continue }
        if ((m = part.match(/机构评级=(.+)/))) { this.filters.org_rating_filter = m[1]; continue }
        if ((m = part.match(/机构持股比例≥(.+?)%/))) { this.filters.allcorp_ratio_min = parseFloat(m[1]); continue }
        if ((m = part.match(/基金持股≥(.+?)%/))) { this.filters.allcorp_fund_ratio_min = parseFloat(m[1]); continue }
        if ((m = part.match(/券商持股≥(.+?)%/))) { this.filters.allcorp_qs_ratio_min = parseFloat(m[1]); continue }
        if ((m = part.match(/QFII持股≥(.+?)%/))) { this.filters.allcorp_qfii_ratio_min = parseFloat(m[1]); continue }

        // 资金面数值 ≥
        if ((m = part.match(/净流入≥(.+)/))) { this.filters.net_inflow_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/大单动向≥(.+)/))) { this.filters.ddx_min = parseFloat(m[1]); continue }
        if ((m = part.match(/3日净流入≥(.+)/))) { this.filters.netinflow_min_3d = this.parseNumber(m[1]); continue }
        if ((m = part.match(/5日净流入≥(.+)/))) { this.filters.netinflow_min_5d = this.parseNumber(m[1]); continue }
        if ((m = part.match(/3日涨幅≥(.+?)%/))) { this.filters.changerate_3d_min = parseFloat(m[1]); continue }
        if ((m = part.match(/5日涨幅≥(.+?)%/))) { this.filters.changerate_5d_min = parseFloat(m[1]); continue }
        if ((m = part.match(/10日涨幅≥(.+?)%/))) { this.filters.changerate_10d_min = parseFloat(m[1]); continue }

        // 行情指标 ≥
        if ((m = part.match(/量比≥(.+)/))) { this.filters.mi_volume_ratio_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/换手率≥(.+?)%/))) { this.filters.mi_turnover_rate_min = parseFloat(m[1]); continue }
        if ((m = part.match(/成交量≥(.+?)手/))) { this.filters.mi_volume_min = this.parseNumber(m[1]); continue }
        if ((m = part.match(/成交额≥(.+)/))) { this.filters.mi_amount_min = this.parseNumber(m[1]); continue }

        // 范围格式
        if ((m = part.match(/总市值(.+)/))) { const r = parseRange(m[1]); this.filters.total_market_cap_min = r.min; this.filters.total_market_cap_max = r.max; continue }
        if ((m = part.match(/流通市值(.+)/))) { const r = parseRange(m[1]); this.filters.free_cap_min = r.min; this.filters.free_cap_max = r.max; continue }
        if ((m = part.match(/市销率(.+)/))) { const r = parseRange(m[1]); this.filters.ps_min = r.min; this.filters.ps_max = r.max; continue }
        if ((m = part.match(/市现率(.+)/))) { const r = parseRange(m[1]); this.filters.pcf_min = r.min; this.filters.pcf_max = r.max; continue }
        if ((m = part.match(/动态PE(.+)/))) { const r = parseRange(m[1]); this.filters.dtsyl_min = r.min; this.filters.dtsyl_max = r.max; continue }
        if ((m = part.match(/总股本(.+)/))) { const r = parseRange(m[1]); this.filters.total_shares_min = r.min; this.filters.total_shares_max = r.max; continue }
        if ((m = part.match(/流通股本(.+)/))) { const r = parseRange(m[1]); this.filters.free_shares_min = r.min; this.filters.free_shares_max = r.max; continue }
        if ((m = part.match(/股东数(.+)/))) { const r = parseRange(m[1]); this.filters.holder_newest_min = r.min; this.filters.holder_newest_max = r.max; continue }
        if ((m = part.match(/年度涨幅(.+?)%/))) { const p = m[1].split('~'); this.filters.changerate_ty_min = this.parseNumber(p[0]); this.filters.changerate_ty_max = p[1] ? this.parseNumber(p[1]) : null; continue }
      }
    },

    // ===== 分页 =====
    handleSizeChange (val) { this.pageSize = val; this.currentPage = 1 },
    handleCurrentChange (val) { this.currentPage = val },
    handleSortChange ({ prop, order }) {
      if (!prop || !order) return
      const dir = order === 'ascending' ? 1 : -1
      this.tableData.sort((a, b) => {
        const va = a[prop]; const vb = b[prop]
        if (va == null && vb == null) return 0
        if (va == null) return dir
        if (vb == null) return -dir
        if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir
        return String(va).localeCompare(String(vb)) * dir
      })
    },
    handleSelectionChange (val) { this.selectedRows = val },

    handleSliderChange (rangeKey, val) {
      const minKey = rangeKey.replace('_range', '_min')
      const maxKey = rangeKey.replace('_range', '_max')
      this.filters[minKey] = val[0]
      this.filters[maxKey] = val[1]
      this.updateAiQuery()
    }
  },
  watch: {
    filters: {
      handler () { this.updateAiQuery() },
      deep: true
    },
    selectedMarket () { this.updateAiQuery() },
    filterDialogVisible (val) {
      if (val) {
        this.isUpdatingFromAi = true
        this.resetAllFilters()
        this.parseFilterFromText(this.aiQuery)
        this.$nextTick(() => { this.isUpdatingFromAi = false })
      }
    }
  },
  created () {
    this.loadUserInfo()
  },
  mounted () {
    this.aiQuery = ''
    this.startWatchlistPriceRefresh()
  },
  beforeDestroy () {
  }
}
</script>

<style scoped>
.xuangu-container {
  font-family: 'Helvetica Neue', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  min-height: 100vh;
  background: linear-gradient(135deg, #f0f2f5 0%, #e8ecf1 100%);
  color: #303133;
}

.stock-screener-app {
  max-width: 1900px;
  margin: 0 auto;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.08);
  overflow: hidden;
}

/* --- 市场选择 --- */
.market-filters {
  padding: 16px 24px;
  background: linear-gradient(180deg, #fafbfc 0%, #f5f6f8 100%);
  border-bottom: 1px solid #ebeef5;
}

.market-filters ::v-deep .el-radio-button__inner {
  border-radius: 20px;
  padding: 8px 20px;
  font-size: 13px;
  transition: all 0.25s ease;
  border: 1px solid #dcdfe6;
  margin-right: 8px;
}

.market-filters ::v-deep .el-radio-button__orig-radio:checked + .el-radio-button__inner {
  background: linear-gradient(135deg, #409eff 0%, #337ab7 100%);
  border-color: transparent;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.35);
}

.market-filters ::v-deep .el-radio-button:first-child .el-radio-button__inner,
.market-filters ::v-deep .el-radio-button:last-child .el-radio-button__inner {
  border-radius: 20px;
}

/* ====== 左右布局：自选股 + 结果表格 ====== */
.content-body {
  display: flex;
  gap: 0;
  min-height: 0;
  flex: 1;
}

/* 左侧自选股 */
.watchlist-side {
  width: 220px;
  flex-shrink: 0;
  border-right: 1px solid #ebeef5;
  background: #fafbfc;
  display: flex;
  flex-direction: column;
  max-height: calc(100vh - 240px);
}

.watchlist-side-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid #ebeef5;
  background: #f5f6f8;
}

.wl-side-title {
  font-size: 13px;
  font-weight: 700;
  color: #303133;
}

.wl-side-title i {
  color: #f7ba2a;
  margin-right: 4px;
}

.watchlist-side-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 6px;
}

.watchlist-side-list::-webkit-scrollbar { width: 3px; }
.watchlist-side-list::-webkit-scrollbar-thumb { background: #d4d8dd; border-radius: 2px; }

.wl-side-item {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 8px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s;
  margin-bottom: 2px;
  border: 1px solid transparent;
}

.wl-side-item:hover {
  background: #ecf5ff;
  border-color: #d9ecff;
}

.wl-side-item.active {
  background: linear-gradient(135deg, #ecf5ff 0%, #e6f7ff 100%);
  border-color: #409eff;
}

.wl-si-left {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}

.wl-si-symbol {
  font-size: 12px;
  font-weight: 700;
  color: #303133;
  font-family: 'SF Mono', Monaco, monospace;
}

.wl-si-name {
  font-size: 10px;
  color: #909399;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.wl-si-right {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  flex-shrink: 0;
  margin-left: 4px;
}

.wl-si-price {
  font-size: 11px;
  font-weight: 600;
  color: #303133;
  font-family: 'SF Mono', Monaco, monospace;
}

.wl-si-change {
  font-size: 10px;
  font-weight: 600;
  font-family: 'SF Mono', Monaco, monospace;
  padding: 0 3px;
  border-radius: 2px;
}

.wl-si-change.up { color: #f56c6c; background: rgba(245, 108, 108, 0.08); }
.wl-si-change.down { color: #67c23a; background: rgba(103, 194, 58, 0.08); }

.wl-si-remove {
  position: absolute;
  top: 50%;
  right: 4px;
  transform: translateY(-50%);
  opacity: 0;
  transition: opacity 0.15s;
  color: #c0c4cc;
  cursor: pointer;
  font-size: 12px;
}

.wl-side-item:hover .wl-si-remove { opacity: 1; }
.wl-si-remove:hover { color: #f56c6c; }

.wl-side-empty {
  text-align: center;
  padding: 20px 8px;
  color: #c0c4cc;
  font-size: 12px;
}

/* 右侧结果面板 */
.result-panel {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  padding: 12px 16px;
}

/* --- AI搜索区域 --- */
.ai-search-container {
  display: flex;
  padding: 16px 20px;
  gap: 10px;
  border-bottom: 1px solid #ebeef5;
  align-items: flex-start;
  flex-wrap: wrap;
}

.filter-trigger-btn {
  flex-shrink: 0;
  height: 56px;
  font-size: 13px;
  border-radius: 8px;
  background: linear-gradient(135deg, #909399 0%, #606266 100%);
  border: none;
  letter-spacing: 1px;
  transition: all 0.3s ease;
}

.filter-trigger-btn:hover {
  background: linear-gradient(135deg, #606266 0%, #404246 100%);
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(96, 98, 102, 0.3);
}

.ai-input {
  flex: 1;
  min-width: 280px;
}

.ai-input ::v-deep .el-textarea__inner {
  border-radius: 8px;
  border: 1px solid #dcdfe6;
  transition: all 0.3s ease;
  font-size: 14px;
  line-height: 1.6;
  padding: 10px 14px;
}

.ai-input ::v-deep .el-textarea__inner:focus {
  border-color: #409eff;
  box-shadow: 0 0 0 3px rgba(64, 158, 255, 0.12);
}

/* 关键修复：搜索按钮与输入框等高对齐 */
.search-btn {
  flex-shrink: 0;
  height: 56px;
  padding: 0 24px;
  font-size: 14px;
  font-weight: 600;
  border-radius: 8px;
  letter-spacing: 1px;
  transition: all 0.3s ease;
  background: linear-gradient(135deg, #409eff 0%, #337ab7 100%);
  border: none;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.3);
  white-space: nowrap;
}

.search-btn:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 14px rgba(64, 158, 255, 0.4);
}

/* --- 筛选器面板样式 --- */
.filter-popover {
  padding: 0 !important;
  max-width: calc(100% - 40px);
  left: 20px !important;
  border-radius: 8px !important;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.12) !important;
  border: 1px solid #ebeef5 !important;
}

/* --- 表格工具栏 --- */
.table-toolbar {
  margin-bottom: 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.toolbar-right {
  display: flex;
  gap: 8px;
}

.table-toolbar .el-button--warning {
  background: linear-gradient(135deg, #e6a23c 0%, #cf8e2e 100%);
  border: none;
  border-radius: 6px;
  font-weight: 600;
  transition: all 0.3s ease;
}

.table-toolbar .el-button--warning:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(230, 162, 60, 0.35);
}

/* 表格铺满 */
.result-panel ::v-deep .el-table {
  font-size: 13px;
  flex: 1;
}

.result-panel ::v-deep .el-table th {
  background: #f5f7fa !important;
  font-weight: 600;
  color: #303133;
  font-size: 12px;
  padding: 8px 0;
}

.result-panel ::v-deep .el-table tr:hover > td {
  background: #ecf5ff !important;
}

.result-panel ::v-deep .el-table td {
  padding: 6px 0;
  transition: background 0.15s ease;
}

.result-panel ::v-deep .el-table .row-up td {
  background: rgba(245, 108, 108, 0.03);
}

.result-panel ::v-deep .el-table .row-down td {
  background: rgba(103, 194, 58, 0.03);
}

.stock-code-cell {
  font-weight: 600;
  color: #409eff;
  font-family: 'SF Mono', Monaco, monospace;
  font-size: 12px;
}

.stock-name-cell {
  font-weight: 600;
  color: #303133;
}

.price-cell {
  font-weight: 600;
  font-family: 'SF Mono', Monaco, monospace;
}

.change-cell {
  font-weight: 700;
  font-family: 'SF Mono', Monaco, monospace;
  padding: 2px 6px;
  border-radius: 3px;
}

.text-red {
  color: #f56c6c;
  background: rgba(245, 108, 108, 0.06);
}

.text-green {
  color: #67c23a;
  background: rgba(103, 194, 58, 0.06);
}

.cell-na {
  color: #c0c4cc;
}

.pagination-container {
  margin-top: 12px;
  display: flex;
  justify-content: center;
  padding-bottom: 12px;
}

.strategy-conditions-preview {
  padding: 8px 12px;
  background: #f5f7fa;
  border-radius: 4px;
  color: #606266;
  font-size: 13px;
  line-height: 1.6;
  max-height: 120px;
  overflow-y: auto;
  word-break: break-all;
}

/* --- 响应式 --- */
@media (max-width: 768px) {
  .ai-search-container {
    flex-direction: column;
    padding: 12px;
  }

  .filter-trigger-btn, .search-btn {
    width: 100%;
    height: 44px;
  }

  .ai-input {
    min-width: 100%;
  }

  .content-body {
    flex-direction: column;
  }

  .watchlist-side {
    width: 100%;
    max-height: 200px;
    border-right: none;
    border-bottom: 1px solid #ebeef5;
  }
}
</style>
