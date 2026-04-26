<template>
  <div class="watchlist-panel" :class="{ 'theme-dark': isDarkTheme }">
    <div class="panel-header">
      <span class="panel-title"><a-icon type="star" theme="filled" /> {{ $t('dashboard.analysis.watchlist.title') }}</span>
      <span class="panel-header-actions">
        <a-tooltip :title="$t('aiAssetAnalysis.tasks.manage')">
          <a-badge :count="monitors.length" :offset="[-2, 2]" :number-style="{ fontSize: '9px', minWidth: '14px', height: '14px', lineHeight: '14px', padding: '0 3px' }">
            <a-icon type="unordered-list" class="panel-header-icon" @click="showTaskDrawer = true" />
          </a-badge>
        </a-tooltip>
        <a-tooltip :title="$t('aiAssetAnalysis.batch.schedule')">
          <a-icon type="schedule" class="panel-header-icon" @click="toggleBatchMode" />
        </a-tooltip>
        <a-icon type="plus" class="panel-header-icon" @click="showAddStockModal = true" />
      </span>
    </div>

    <!-- 汇总统计条 -->
    <div class="panel-summary" v-if="watchlist && watchlist.length > 0">
      <div class="summary-chip">
        <span class="sc-num">{{ watchlist.length }}</span>
        <span class="sc-label">{{ $t('aiAssetAnalysis.watchlist.totalAssets') }}</span>
      </div>
      <div class="summary-chip" v-if="watchlistPositionCount > 0">
        <span class="sc-num">{{ watchlistPositionCount }}</span>
        <span class="sc-label">{{ $t('aiAssetAnalysis.watchlist.positionCount') }}</span>
      </div>
      <div class="summary-chip" v-if="watchlistTaskCount > 0">
        <span class="sc-num">{{ watchlistTaskCount }}</span>
        <span class="sc-label">{{ $t('aiAssetAnalysis.watchlist.taskCount') }}</span>
      </div>
      <div class="summary-chip pnl" v-if="watchlistTotalPnl !== 0">
        <span class="sc-num" :class="watchlistTotalPnl >= 0 ? 'up' : 'down'">{{ watchlistTotalPnl >= 0 ? '+' : '' }}{{ formatNum(watchlistTotalPnl) }}</span>
        <span class="sc-label">P&amp;L</span>
      </div>
    </div>

    <!-- 批量勾选栏 -->
    <div class="batch-bar" v-if="batchMode">
      <a-checkbox :checked="batchSelectedAll" :indeterminate="batchIndeterminate" @change="onBatchSelectAll" class="batch-all-cb">
        {{ $t('aiAssetAnalysis.batch.selectAll') }}
      </a-checkbox>
      <a-button type="primary" size="small" :disabled="batchSelectedKeys.length === 0" @click="openBatchScheduleModal">
        {{ $t('aiAssetAnalysis.batch.schedule') }}<template v-if="batchSelectedKeys.length > 0"> {{ batchSelectedKeys.length }}</template>
      </a-button>
      <a-button size="small" @click="toggleBatchMode">{{ $t('common.cancel') }}</a-button>
    </div>

    <div class="watchlist-list">
      <div
        v-for="stock in (watchlist || [])"
        :key="`wl-${stock.market}-${stock.symbol}`"
        class="wl-card"
        :class="{ active: selectedKey === `${stock.market}:${stock.symbol}` }"
        @click="selectWatchlistItem(stock)"
      >
        <a-checkbox
          v-if="batchMode"
          class="wl-card-cb"
          :checked="batchSelectedKeys.includes(`${stock.market}:${stock.symbol}`)"
          @change="onBatchItemToggle(stock, $event)"
          @click.native.stop
        />
        <div class="wl-card-body" :class="{ 'with-cb': batchMode }">
          <div class="wl-row-main">
            <div class="wl-info-left">
              <div class="wl-symbol-line">
                <span class="wl-symbol">{{ stock.symbol }}</span>
                <span class="wl-market">{{ getMarketName(stock.market) }}</span>
              </div>
              <div class="wl-name" v-if="stock.name && stock.name !== stock.symbol">{{ stock.name }}</div>
            </div>
            <div class="wl-sparkline-wrap" v-if="watchlistPrices[`${stock.market}:${stock.symbol}`]">
              <svg class="wl-sparkline" viewBox="0 0 60 20" preserveAspectRatio="none">
                <polyline
                  :points="getSparklinePoints(stock)"
                  fill="none"
                  :stroke="(watchlistPrices[`${stock.market}:${stock.symbol}`]?.change || 0) >= 0 ? '#10b981' : '#ef4444'"
                  stroke-width="1.5"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                />
              </svg>
            </div>
            <span v-else class="wl-spacer"></span>
            <div class="wl-info-right" v-if="watchlistPrices[`${stock.market}:${stock.symbol}`]">
              <span class="wl-price">{{ formatPrice(watchlistPrices[`${stock.market}:${stock.symbol}`].price) }}</span>
              <span class="wl-change" :class="(watchlistPrices[`${stock.market}:${stock.symbol}`]?.change || 0) >= 0 ? 'up' : 'down'">
                {{ (watchlistPrices[`${stock.market}:${stock.symbol}`]?.change || 0) >= 0 ? '+' : '' }}{{ formatNum(watchlistPrices[`${stock.market}:${stock.symbol}`]?.change) }}%
              </span>
            </div>
          </div>
          <div class="wl-row-pnl" v-if="positionSummaryMap[`${stock.market}:${stock.symbol}`]">
            <span class="wl-pnl-qty">{{ formatNum(positionSummaryMap[`${stock.market}:${stock.symbol}`].quantity, 4) }} @ {{ formatPrice(positionSummaryMap[`${stock.market}:${stock.symbol}`].avgEntry || 0) }}</span>
            <span class="wl-pnl-val" :class="positionSummaryMap[`${stock.market}:${stock.symbol}`].pnl >= 0 ? 'up' : 'down'">
              {{ positionSummaryMap[`${stock.market}:${stock.symbol}`].pnl >= 0 ? '+' : '' }}{{ formatNum(positionSummaryMap[`${stock.market}:${stock.symbol}`].pnl || 0) }}
              ({{ positionSummaryMap[`${stock.market}:${stock.symbol}`].pnlPercent >= 0 ? '+' : '' }}{{ formatNum(positionSummaryMap[`${stock.market}:${stock.symbol}`].pnlPercent || 0) }}%)
            </span>
          </div>
          <div class="wl-row-task" v-if="getMonitorMeta(stock)">
            <span class="wl-task-badge" :class="getMonitorMeta(stock).activeCount > 0 ? 'active' : 'paused'" @click.stop="toggleStockMonitor(stock)">
              <a-icon :type="getMonitorMeta(stock).activeCount > 0 ? 'sync' : 'pause-circle'" :spin="getMonitorMeta(stock).activeCount > 0" />
              {{ getMonitorMeta(stock).activeCount > 0 ? ($t('aiAssetAnalysis.monitor.running')) : ($t('aiAssetAnalysis.monitor.paused')) }}
            </span>
            <span class="wl-task-next" v-if="getMonitorMeta(stock).nextRunAtText">{{ getMonitorMeta(stock).nextRunAtText }}</span>
          </div>
        </div>
        <div class="wl-card-hover-actions">
          <a-tooltip :title="$t('aiAssetAnalysis.position.quickAdd')"><span class="wl-hover-btn" @click.stop="openPositionModal(stock)"><a-icon type="wallet" /></span></a-tooltip>
          <a-tooltip :title="$t('aiAssetAnalysis.monitor.quickTask')"><span class="wl-hover-btn" @click.stop="openMonitorModal(stock)"><a-icon type="clock-circle" /></span></a-tooltip>
          <span class="wl-hover-btn danger" @click.stop="removeFromWatchlist(stock)"><a-icon type="delete" /></span>
        </div>
      </div>
      <div v-if="!watchlist || watchlist.length === 0" class="watchlist-empty">
        <div class="we-icon"><a-icon type="star" /></div>
        <p>{{ $t('dashboard.analysis.empty.noWatchlist') }}</p>
        <a-button type="primary" size="small" icon="plus" @click="showAddStockModal = true">
          {{ $t('dashboard.analysis.watchlist.add') }}
        </a-button>
      </div>
    </div>

    <!-- 添加股票弹窗 -->
    <a-modal
      :title="$t('dashboard.analysis.modal.addStock.title')"
      :visible="showAddStockModal"
      @ok="handleAddStock"
      @cancel="handleCloseAddStockModal"
      :confirmLoading="addingStock"
      width="600px"
      :wrapClassName="isDarkTheme ? 'qd-dark-modal' : ''"
      :okText="$t('dashboard.analysis.modal.addStock.confirm')"
      :cancelText="$t('dashboard.analysis.modal.addStock.cancel')"
    >
      <div class="add-stock-modal-content">
        <a-tabs v-model="selectedMarketTab" @change="handleMarketTabChange" class="market-tabs">
          <a-tab-pane
            v-for="marketType in marketTypes"
            :key="marketType.value"
            :tab="$t(marketType.i18nKey || `dashboard.analysis.market.${marketType.value}`)"
          />
        </a-tabs>
        <div class="symbol-search-section">
          <a-input-search
            v-model="symbolSearchKeyword"
            :placeholder="$t('dashboard.analysis.modal.addStock.searchOrInputPlaceholder')"
            @search="handleSearchOrInput"
            @change="handleSymbolSearchInput"
            :loading="searchingSymbols"
            size="large"
            allow-clear
          >
            <a-button slot="enterButton" type="primary" icon="search">
              {{ $t('dashboard.analysis.modal.addStock.search') }}
            </a-button>
          </a-input-search>
        </div>
        <div v-if="symbolSearchResults.length > 0" class="search-results-section">
          <div class="section-title">
            <a-icon type="search" style="margin-right: 4px;" />
            {{ $t('dashboard.analysis.modal.addStock.searchResults') }}
          </div>
          <a-list :data-source="symbolSearchResults" :loading="searchingSymbols" size="small" class="symbol-list">
            <a-list-item slot="renderItem" slot-scope="item" class="symbol-list-item" @click="selectSymbol(item)">
              <a-list-item-meta>
                <template slot="title">
                  <div class="symbol-item-content">
                    <span class="symbol-code">{{ item.symbol }}</span>
                    <span class="symbol-name">{{ item.name }}</span>
                    <a-tag v-if="item.exchange" size="small" color="blue" style="margin-left: 8px;">{{ item.exchange }}</a-tag>
                  </div>
                </template>
              </a-list-item-meta>
            </a-list-item>
          </a-list>
        </div>
        <div class="hot-symbols-section">
          <div class="section-title">
            <a-icon type="fire" style="color: #ff4d4f; margin-right: 4px;" />
            {{ $t('dashboard.analysis.modal.addStock.hotSymbols') }}
          </div>
          <a-spin :spinning="loadingHotSymbols">
            <a-list v-if="hotSymbols.length > 0" :data-source="hotSymbols" size="small" class="symbol-list">
              <a-list-item slot="renderItem" slot-scope="item" class="symbol-list-item" @click="selectSymbol(item)">
                <a-list-item-meta>
                  <template slot="title">
                    <div class="symbol-item-content">
                      <span class="symbol-code">{{ item.symbol }}</span>
                      <span class="symbol-name">{{ item.name }}</span>
                      <a-tag v-if="item.exchange" size="small" color="orange" style="margin-left: 8px;">{{ item.exchange }}</a-tag>
                    </div>
                  </template>
                </a-list-item-meta>
              </a-list-item>
            </a-list>
            <a-empty v-else :description="$t('dashboard.analysis.modal.addStock.noHotSymbols')" :image="false" />
          </a-spin>
        </div>
        <div v-if="selectedSymbolForAdd" class="selected-symbol-section">
          <a-alert :message="$t('dashboard.analysis.modal.addStock.selectedSymbol')" type="info" show-icon closable @close="selectedSymbolForAdd = null">
            <template slot="description">
              <div class="selected-symbol-info">
                <a-tag :color="getMarketColor(selectedSymbolForAdd.market)" style="margin-right: 8px;">
                  {{ $t(`dashboard.analysis.market.${selectedSymbolForAdd.market}`) }}
                </a-tag>
                <strong>{{ selectedSymbolForAdd.symbol }}</strong>
                <span v-if="selectedSymbolForAdd.name" style="color: #999; margin-left: 8px;">{{ selectedSymbolForAdd.name }}</span>
              </div>
            </template>
          </a-alert>
        </div>
      </div>
    </a-modal>

    <!-- 持仓弹窗 -->
    <a-modal
      :visible="showPositionModal"
      :title="`${($i18n && $i18n.locale === 'zh-CN') ? '创建持仓（虚拟仓）' : 'Create Position (Virtual)'} - ${targetStockForOps ? targetStockForOps.symbol : ''}`"
      @ok="savePosition"
      @cancel="showPositionModal = false"
      :wrapClassName="isDarkTheme ? 'qd-dark-modal' : ''"
    >
      <a-form layout="vertical">
        <a-form-item :label="$t('portfolio.positions.side') || 'Direction'">
          <a-select v-model="positionForm.side">
            <a-select-option value="long">{{ $t('portfolio.positions.long') || 'Long' }}</a-select-option>
            <a-select-option value="short">{{ $t('portfolio.positions.short') || 'Short' }}</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item :label="($i18n && $i18n.locale === 'zh-CN') ? '数量' : 'Quantity'">
          <a-input-number v-model="positionForm.quantity" :min="0" :step="0.01" style="width: 100%;" />
        </a-form-item>
        <a-form-item :label="($i18n && $i18n.locale === 'zh-CN') ? '买入单价' : 'Entry Price'">
          <a-input-number v-model="positionForm.entryPrice" :min="0" :step="0.01" style="width: 100%;" />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- 监控任务弹窗 -->
    <a-modal
      :visible="showMonitorModal"
      :title="`${$t('aiAssetAnalysis.monitor.quickTask')} - ${targetStockForOps ? targetStockForOps.symbol : ''}`"
      @ok="saveMonitorTask"
      @cancel="showMonitorModal = false"
      :wrapClassName="isDarkTheme ? 'qd-dark-modal' : ''"
    >
      <a-form layout="vertical">
        <a-form-item :label="$t('aiAssetAnalysis.batch.intervalLabel')">
          <a-select v-model="monitorForm.interval_min" style="width: 100%;">
            <a-select-option :value="60">{{ $t('aiAssetAnalysis.batch.interval1h') }}</a-select-option>
            <a-select-option :value="240">{{ $t('aiAssetAnalysis.batch.interval4h') }}</a-select-option>
            <a-select-option :value="720">{{ $t('aiAssetAnalysis.batch.interval12h') }}</a-select-option>
            <a-select-option :value="1440">{{ $t('aiAssetAnalysis.batch.interval24h') }}</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item :label="$t('aiAssetAnalysis.batch.notifyLabel')">
          <a-checkbox-group v-model="monitorForm.notify_channels" style="width: 100%;">
            <a-row :gutter="8">
              <a-col :span="8"><a-checkbox value="email"><a-icon type="mail" /> Email</a-checkbox></a-col>
              <a-col :span="8"><a-checkbox value="telegram"><a-icon type="send" /> Telegram</a-checkbox></a-col>
              <a-col :span="8"><a-checkbox value="webhook"><a-icon type="api" /> Webhook</a-checkbox></a-col>
            </a-row>
          </a-checkbox-group>
        </a-form-item>
        <a-alert :message="$t('aiAssetAnalysis.monitor.tip')" type="info" show-icon />
      </a-form>
    </a-modal>

    <!-- 批量定时任务弹窗 -->
    <a-modal
      :visible="showBatchScheduleModal"
      :title="$t('aiAssetAnalysis.batch.scheduleTitle')"
      @ok="saveBatchSchedule"
      @cancel="showBatchScheduleModal = false"
      :confirmLoading="batchRunning"
      width="520px"
      :wrapClassName="isDarkTheme ? 'qd-dark-modal' : ''"
    >
      <div class="batch-modal-summary">
        <p>{{ $t('aiAssetAnalysis.batch.scheduleDesc', { count: batchSelectedKeys.length }) }}</p>
        <div class="batch-symbols-preview">
          <a-tag v-for="key in batchSelectedKeys" :key="key" color="blue" style="margin-bottom: 4px;">{{ key.split(':')[1] }}</a-tag>
        </div>
      </div>
      <a-form layout="vertical">
        <a-form-item :label="$t('aiAssetAnalysis.batch.intervalLabel')">
          <a-select v-model="batchScheduleForm.interval_min" style="width: 100%;">
            <a-select-option :value="60">{{ $t('aiAssetAnalysis.batch.interval1h') }}</a-select-option>
            <a-select-option :value="240">{{ $t('aiAssetAnalysis.batch.interval4h') }}</a-select-option>
            <a-select-option :value="720">{{ $t('aiAssetAnalysis.batch.interval12h') }}</a-select-option>
            <a-select-option :value="1440">{{ $t('aiAssetAnalysis.batch.interval24h') }}</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item :label="$t('aiAssetAnalysis.batch.notifyLabel')">
          <a-checkbox-group v-model="batchScheduleForm.notify_channels" style="width: 100%;">
            <a-row :gutter="8">
              <a-col :span="8"><a-checkbox value="email"><a-icon type="mail" /> Email</a-checkbox></a-col>
              <a-col :span="8"><a-checkbox value="telegram"><a-icon type="send" /> Telegram</a-checkbox></a-col>
              <a-col :span="8"><a-checkbox value="webhook"><a-icon type="api" /> Webhook</a-checkbox></a-col>
            </a-row>
          </a-checkbox-group>
        </a-form-item>
      </a-form>
      <a-alert :message="$t('aiAssetAnalysis.batch.scheduleTip')" type="info" show-icon style="margin-top: 8px;" />
    </a-modal>

    <!-- 任务管理抽屉 -->
    <a-drawer
      :title="$t('aiAssetAnalysis.tasks.manage')"
      :visible="showTaskDrawer"
      @close="showTaskDrawer = false"
      width="420"
      placement="right"
      :wrapClassName="isDarkTheme ? 'qd-dark-drawer' : ''"
    >
      <div v-if="monitors.length === 0" class="task-drawer-empty">
        <a-icon type="inbox" style="font-size: 40px; color: #ccc;" />
        <p>{{ $t('aiAssetAnalysis.tasks.empty') }}</p>
      </div>
      <div v-else class="task-drawer-list">
        <div v-for="m in monitors" :key="m.id" class="task-item">
          <div class="task-item-header">
            <span class="task-item-name">{{ m.name || 'AI Task' }}</span>
            <a-tag :color="m.is_active ? 'green' : 'default'" size="small">
              {{ m.is_active ? $t('aiAssetAnalysis.monitor.running') : $t('aiAssetAnalysis.monitor.paused') }}
            </a-tag>
          </div>
          <div class="task-item-meta">
            <span v-if="m.config && m.config.run_interval_minutes">
              <a-icon type="clock-circle" /> {{ formatIntervalText(m.config.run_interval_minutes) }}
            </span>
            <span v-if="m.next_run_at">
              <a-icon type="calendar" /> {{ _formatNextRunText(m.next_run_at) }}
            </span>
          </div>
          <div class="task-item-actions">
            <a-button size="small" :type="m.is_active ? 'default' : 'primary'" icon="poweroff" @click="handleToggleTask(m)">
              {{ m.is_active ? $t('aiAssetAnalysis.tasks.pause') : $t('aiAssetAnalysis.tasks.resume') }}
            </a-button>
            <a-button size="small" icon="edit" @click="handleEditTask(m)">{{ $t('aiAssetAnalysis.tasks.edit') }}</a-button>
            <a-popconfirm :title="$t('aiAssetAnalysis.tasks.deleteConfirm')" @confirm="handleDeleteTask(m)" :okText="$t('common.confirm')" :cancelText="$t('common.cancel')">
              <a-button size="small" type="danger" icon="delete">{{ $t('aiAssetAnalysis.tasks.delete') }}</a-button>
            </a-popconfirm>
          </div>
        </div>
      </div>
    </a-drawer>

    <!-- 编辑任务弹窗 -->
    <a-modal
      :visible="showEditTaskModal"
      :title="$t('aiAssetAnalysis.tasks.edit')"
      @ok="saveEditTask"
      @cancel="showEditTaskModal = false"
      :confirmLoading="editTaskLoading"
      :wrapClassName="isDarkTheme ? 'qd-dark-modal' : ''"
    >
      <a-form layout="vertical" v-if="editTaskForm">
        <a-form-item :label="$t('aiAssetAnalysis.tasks.name')">
          <a-input v-model="editTaskForm.name" />
        </a-form-item>
        <a-form-item :label="$t('aiAssetAnalysis.batch.intervalLabel')">
          <a-select v-model="editTaskForm.interval_min" style="width: 100%;">
            <a-select-option :value="60">{{ $t('aiAssetAnalysis.batch.interval1h') }}</a-select-option>
            <a-select-option :value="240">{{ $t('aiAssetAnalysis.batch.interval4h') }}</a-select-option>
            <a-select-option :value="720">{{ $t('aiAssetAnalysis.batch.interval12h') }}</a-select-option>
            <a-select-option :value="1440">{{ $t('aiAssetAnalysis.batch.interval24h') }}</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item :label="$t('aiAssetAnalysis.batch.notifyLabel')">
          <a-checkbox-group v-model="editTaskForm.notify_channels" style="width: 100%;">
            <a-row :gutter="8">
              <a-col :span="8"><a-checkbox value="email"><a-icon type="mail" /> Email</a-checkbox></a-col>
              <a-col :span="8"><a-checkbox value="telegram"><a-icon type="send" /> Telegram</a-checkbox></a-col>
              <a-col :span="8"><a-checkbox value="webhook"><a-icon type="api" /> Webhook</a-checkbox></a-col>
            </a-row>
          </a-checkbox-group>
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script>
import { mapGetters, mapState } from 'vuex'
import { getUserInfo } from '@/api/login'
import { getWatchlist, addWatchlist, removeWatchlist, getWatchlistPrices, getMarketTypes, searchSymbols, getHotSymbols } from '@/api/market'
import { getPositions, addPosition, getMonitors, addMonitor, updateMonitor, deleteMonitor } from '@/api/portfolio'

export default {
  name: 'WatchlistPanel',
  props: {
    value: {
      type: String,
      default: ''
    }
  },
  data () {
    return {
      watchlistPriceTimer: null,
      watchlistPrices: {},
      localUserInfo: {},
      loadingUserInfo: false,
      userId: 1,
      watchlist: [],
      loadingWatchlist: false,
      showAddStockModal: false,
      addingStock: false,
      selectedKey: '',
      marketTypes: [],
      selectedMarketTab: '',
      symbolSearchKeyword: '',
      symbolSearchResults: [],
      searchingSymbols: false,
      hotSymbols: [],
      loadingHotSymbols: false,
      selectedSymbolForAdd: null,
      searchTimer: null,
      hasSearched: false,
      positions: [],
      monitors: [],
      positionSummaryMap: {},
      showPositionModal: false,
      showMonitorModal: false,
      targetStockForOps: null,
      positionForm: {
        side: 'long',
        quantity: null,
        entryPrice: null
      },
      monitorForm: {
        interval_min: 240,
        notify_channels: []
      },
      batchMode: false,
      batchSelectedKeys: [],
      batchRunning: false,
      showBatchScheduleModal: false,
      batchScheduleForm: {
        interval_min: 240,
        notify_channels: []
      },
      showTaskDrawer: false,
      showEditTaskModal: false,
      editTaskLoading: false,
      editTaskId: null,
      editTaskForm: {
        name: '',
        interval_min: 240,
        notify_channels: []
      }
    }
  },
  computed: {
    ...mapGetters(['userInfo']),
    ...mapState({
      navTheme: state => state.app.theme
    }),
    isDarkTheme () {
      return this.navTheme === 'dark' || this.navTheme === 'realdark'
    },
    storeUserInfo () {
      return this.userInfo || {}
    },
    watchlistTotalPnl () {
      return Object.values(this.positionSummaryMap).reduce((s, v) => s + (Number(v.pnl) || 0), 0)
    },
    watchlistPositionCount () {
      return Object.values(this.positionSummaryMap).filter(v => v.quantity > 0).length
    },
    watchlistTaskCount () {
      return Object.values(this.positionSummaryMap).reduce((s, v) => s + (v.monitorCount || 0), 0)
    },
    batchSelectedAll () {
      return this.watchlist && this.watchlist.length > 0 && this.batchSelectedKeys.length === this.watchlist.length
    },
    batchIndeterminate () {
      return this.batchSelectedKeys.length > 0 && this.batchSelectedKeys.length < (this.watchlist || []).length
    }
  },
  created () {
    this.selectedKey = this.value || ''
    this.loadUserInfo()
    this.loadMarketTypes()
    this.loadWatchlist()
    this.loadPositionData()
  },
  mounted () {
    this.startWatchlistPriceRefresh()
  },
  beforeDestroy () {
    if (this.watchlistPriceTimer) clearInterval(this.watchlistPriceTimer)
  },
  watch: {
    value (val) {
      this.selectedKey = val || ''
    }
  },
  methods: {
    _displayDateTimeLocaleOptions () {
      const tz = String((this.storeUserInfo && this.storeUserInfo.timezone) || '').trim()
      const base = { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }
      if (!tz) return base
      try {
        Intl.DateTimeFormat(undefined, { timeZone: tz }).format(new Date())
        return { ...base, timeZone: tz }
      } catch (e) { return base }
    },
    _parseInstantForDisplay (s) {
      s = String(s || '').trim()
      if (!s) return null
      const hasTz = /[zZ]$/.test(s) || /[+-]\d{2}:?\d{2}$/.test(s)
      if (!hasTz) { const norm = s.replace(' ', 'T'); s = norm.endsWith('Z') ? norm : `${norm}Z` }
      const d = new Date(s)
      return Number.isNaN(d.getTime()) ? null : d
    },
    _formatNextRunText (iso) {
      try {
        const d = this._parseInstantForDisplay(iso)
        if (!d) return ''
        return d.toLocaleString(undefined, this._displayDateTimeLocaleOptions())
      } catch (e) { return '' }
    },
    buildPositionSummary () {
      const map = {}
      const positions = Array.isArray(this.positions) ? this.positions : []
      const monitors = Array.isArray(this.monitors) ? this.monitors : []
      const monitorPositionIds = new Set()
      const positionKeyById = {}
      positions.forEach(pos => { positionKeyById[Number(pos.id)] = `${pos.market}:${pos.symbol}` })
      monitors.forEach(m => {
        const ids = Array.isArray(m.position_ids) ? m.position_ids : []
        ids.forEach(id => monitorPositionIds.add(Number(id)))
        const active = !!m.is_active
        const nextRunAt = m.next_run_at || ''
        ids.forEach(id => {
          const key = positionKeyById[Number(id)]
          if (!key) return
          if (!map[key]) map[key] = { quantity: 0, weightedEntry: 0, pnl: 0, marketValue: 0, monitorCount: 0, activeMonitorCount: 0, nextRunAtText: '' }
          map[key].monitorCount += 1
          if (active) map[key].activeMonitorCount += 1
          if (!map[key].nextRunAtText && nextRunAt) map[key].nextRunAtText = this._formatNextRunText(nextRunAt)
        })
      })
      positions.forEach(pos => {
        const key = `${pos.market}:${pos.symbol}`
        const qty = Number(pos.quantity || 0)
        const entry = Number(pos.entry_price || 0)
        if (!map[key]) map[key] = { quantity: 0, weightedEntry: 0, pnl: 0, marketValue: 0, monitorCount: 0, activeMonitorCount: 0, nextRunAtText: '' }
        map[key].quantity += qty
        map[key].weightedEntry += qty * entry
        map[key].pnl += Number(pos.pnl || 0)
        map[key].marketValue += Number(pos.market_value || 0)
        if (monitorPositionIds.has(Number(pos.id))) map[key].monitorCount += 1
      })
      Object.keys(map).forEach(k => {
        const x = map[k]
        x.avgEntry = x.quantity > 0 ? x.weightedEntry / x.quantity : 0
        const cost = x.quantity > 0 ? x.weightedEntry : 0
        x.pnlPercent = cost > 0 ? (x.pnl / cost) * 100 : 0
      })
      this.positionSummaryMap = map
    },
    getMonitorMeta (stock) {
      if (!stock) return null
      const key = `${stock.market}:${stock.symbol}`
      const summary = this.positionSummaryMap[key]
      if (!summary || summary.monitorCount <= 0) return null
      return { activeCount: summary.activeMonitorCount || 0, nextRunAtText: summary.nextRunAtText || '' }
    },
    async toggleStockMonitor (stock) {
      const key = `${stock.market}:${stock.symbol}`
      const ids = (this.positions || []).filter(p => `${p.market}:${p.symbol}` === key).map(p => Number(p.id)).filter(Boolean)
      if (ids.length === 0) return
      const targetMonitors = (this.monitors || []).filter(m => {
        const mids = Array.isArray(m.position_ids) ? m.position_ids.map(x => Number(x)) : []
        return mids.some(id => ids.includes(id))
      })
      if (targetMonitors.length === 0) return
      const shouldEnable = !targetMonitors.some(m => !!m.is_active)
      try {
        await Promise.all(targetMonitors.map(m => updateMonitor(m.id, { is_active: shouldEnable })))
        this.$message.success(shouldEnable ? (this.$t('aiAssetAnalysis.monitor.enabled') || '已启用任务') : (this.$t('aiAssetAnalysis.monitor.disabled') || '已暂停任务'))
        await this.loadPositionData()
      } catch (e) {
        this.$message.error(e?.response?.data?.msg || e?.message || 'Toggle monitor failed')
      }
    },
    async loadPositionData () {
      try {
        const [posRes, monRes] = await Promise.all([getPositions(), getMonitors()])
        this.positions = posRes && posRes.code === 1 ? (posRes.data || []) : []
        this.monitors = monRes && monRes.code === 1 ? (monRes.data || []) : []
        this.buildPositionSummary()
      } catch (e) {
        this.positions = []; this.monitors = []; this.positionSummaryMap = {}
      }
    },
    openPositionModal (stock) {
      this.targetStockForOps = stock
      const key = `${stock.market}:${stock.symbol}`
      const existingPos = (this.positions || []).find(p => `${p.market}:${p.symbol}` === key)
      if (existingPos) {
        const qty = Number(existingPos.quantity || 0)
        this.positionForm = { side: existingPos.side || (qty < 0 ? 'short' : 'long'), quantity: Math.abs(qty) || null, entryPrice: Number(existingPos.entry_price || 0) || null }
      } else {
        this.positionForm = { side: 'long', quantity: null, entryPrice: null }
      }
      this.showPositionModal = true
    },
    async savePosition () {
      const stock = this.targetStockForOps
      if (!stock) return
      const quantity = Number(this.positionForm.quantity || 0)
      const entryPrice = Number(this.positionForm.entryPrice || 0)
      if (!(quantity > 0) || !(entryPrice > 0)) {
        this.$message.warning(this.$i18n && this.$i18n.locale === 'zh-CN' ? '请输入有效的数量和买入单价' : 'Please enter valid quantity and entry price')
        return
      }
      try {
        const res = await addPosition({ market: stock.market, symbol: stock.symbol, name: stock.name || stock.symbol, side: this.positionForm.side || 'long', quantity, entry_price: entryPrice })
        if (res && res.code === 1) {
          this.$message.success(this.$t('portfolio.positions.add') + ' OK')
          this.showPositionModal = false
          await this.loadPositionData()
        } else { this.$message.error(res?.msg || 'Add position failed') }
      } catch (e) { this.$message.error(e?.response?.data?.msg || e?.message || 'Add position failed') }
    },
    openMonitorModal (stock) {
      this.targetStockForOps = stock
      this.monitorForm = { interval_min: 240, notify_channels: [] }
      this.showMonitorModal = true
    },
    async saveMonitorTask () {
      const stock = this.targetStockForOps
      if (!stock) return
      const key = `${stock.market}:${stock.symbol}`
      const interval = this.monitorForm.interval_min
      const notifyChannels = this.monitorForm.notify_channels || []
      const positionIds = (this.positions || []).filter(p => `${p.market}:${p.symbol}` === key).map(p => Number(p.id)).filter(Boolean)
      try {
        const res = await addMonitor({
          name: `AI-${stock.symbol}-${interval}m`,
          position_ids: positionIds,
          monitor_type: 'ai',
          config: { run_interval_minutes: interval, symbol: stock.symbol, market: stock.market, language: this.$store.getters.lang || this.$i18n.locale || 'en-US' },
          notification_config: { channels: notifyChannels },
          is_active: true
        })
        if (res && res.code === 1) {
          this.$message.success(this.$t('aiAssetAnalysis.monitor.created'))
          this.showMonitorModal = false
          await this.loadPositionData()
        } else { this.$message.error(res?.msg || 'Create monitor failed') }
      } catch (e) { this.$message.error(e?.response?.data?.msg || e?.message || 'Create monitor failed') }
    },
    toggleBatchMode () {
      this.batchMode = !this.batchMode
      if (!this.batchMode) this.batchSelectedKeys = []
    },
    onBatchSelectAll (e) {
      if (e.target.checked) { this.batchSelectedKeys = (this.watchlist || []).map(s => `${s.market}:${s.symbol}`) } else { this.batchSelectedKeys = [] }
    },
    onBatchItemToggle (stock, e) {
      const key = `${stock.market}:${stock.symbol}`
      if (e.target.checked) { if (!this.batchSelectedKeys.includes(key)) this.batchSelectedKeys.push(key) } else { this.batchSelectedKeys = this.batchSelectedKeys.filter(k => k !== key) }
    },
    openBatchScheduleModal () {
      if (this.batchSelectedKeys.length === 0) return
      this.batchScheduleForm = { interval_min: 240, notify_channels: [] }
      this.showBatchScheduleModal = true
    },
    async saveBatchSchedule () {
      const keys = [...this.batchSelectedKeys]
      if (keys.length === 0) return
      this.batchRunning = true
      const interval = this.batchScheduleForm.interval_min
      const notifyChannels = this.batchScheduleForm.notify_channels || []
      let created = 0
      for (const key of keys) {
        const [market, symbol] = key.split(':')
        const stock = (this.watchlist || []).find(s => s.market === market && s.symbol === symbol)
        if (!stock) continue
        const positionIds = (this.positions || []).filter(p => `${p.market}:${p.symbol}` === key).map(p => Number(p.id)).filter(Boolean)
        try {
          await addMonitor({
            name: `AI-${symbol}-${interval}m`,
            position_ids: positionIds,
            monitor_type: 'ai',
            config: { run_interval_minutes: interval, symbol, market, language: this.$store.getters.lang || this.$i18n.locale || 'en-US' },
            notification_config: { channels: notifyChannels },
            is_active: true
          })
          created++
        } catch (_) {}
      }
      this.batchRunning = false
      this.showBatchScheduleModal = false
      this.batchMode = false
      this.batchSelectedKeys = []
      await this.loadPositionData()
      this.$message.success(this.$t('aiAssetAnalysis.batch.done') + ` (${created}/${keys.length})`)
    },
    formatIntervalText (minutes) {
      if (minutes >= 1440) return `${Math.round(minutes / 1440)}d`
      if (minutes >= 60) return `${Math.round(minutes / 60)}h`
      return `${minutes}m`
    },
    async handleToggleTask (m) {
      try {
        await updateMonitor(m.id, { is_active: !m.is_active })
        this.$message.success(m.is_active ? this.$t('aiAssetAnalysis.tasks.paused') : this.$t('aiAssetAnalysis.tasks.resumed'))
        await this.loadPositionData()
      } catch (e) { this.$message.error(e?.response?.data?.msg || e?.message || 'Failed') }
    },
    handleEditTask (m) {
      this.editTaskId = m.id
      this.editTaskForm = { name: m.name || '', interval_min: (m.config && m.config.run_interval_minutes) || 240, notify_channels: (m.notification_config && m.notification_config.channels) || [] }
      this.showEditTaskModal = true
    },
    async saveEditTask () {
      if (!this.editTaskId) return
      this.editTaskLoading = true
      try {
        await updateMonitor(this.editTaskId, { name: this.editTaskForm.name, config: { run_interval_minutes: this.editTaskForm.interval_min }, notification_config: { channels: this.editTaskForm.notify_channels } })
        this.$message.success('OK')
        this.showEditTaskModal = false
        await this.loadPositionData()
      } catch (e) { this.$message.error(e?.response?.data?.msg || e?.message || 'Failed') } finally { this.editTaskLoading = false }
    },
    async handleDeleteTask (m) {
      try {
        await deleteMonitor(m.id)
        this.$message.success(this.$t('aiAssetAnalysis.tasks.deleted'))
        await this.loadPositionData()
      } catch (e) { this.$message.error(e?.response?.data?.msg || e?.message || 'Failed') }
    },
    getSparklinePoints (stock) {
      const key = `${stock.market}:${stock.symbol}`
      const pd = this.watchlistPrices[key]
      if (!pd || !pd.price) return '0,10 60,10'
      const change = pd.change || 0
      const endPrice = pd.price
      const startPrice = endPrice / (1 + change / 100)
      const numPts = 20; const w = 60; const h = 20
      const seed = stock.symbol.split('').reduce((a, c) => a + c.charCodeAt(0), 0)
      const priceDiff = Math.abs(endPrice - startPrice)
      const minAmplitude = endPrice * 0.003
      const amplitude = Math.max(priceDiff, minAmplitude)
      const prices = []
      for (let i = 0; i <= numPts; i++) {
        const t = i / numPts
        const base = startPrice + (endPrice - startPrice) * t
        const noise = (Math.sin(i * 2.7 + seed) + Math.sin(i * 1.3 + seed * 0.3)) * amplitude * 0.25
        prices.push(base + noise)
      }
      const min = Math.min(...prices); const max = Math.max(...prices)
      const range = max - min || 1
      return prices.map((p, i) => {
        const x = (i / numPts) * w
        const y = h - ((p - min) / range) * (h - 4) - 2
        return `${x.toFixed(1)},${y.toFixed(1)}`
      }).join(' ')
    },
    formatNum (num, digits = 2) {
      if (num === undefined || num === null || isNaN(num)) return '--'
      return Number(num).toFixed(digits)
    },
    formatPrice (price) {
      if (!price) return '--'
      if (price >= 10000) return (price / 1000).toFixed(1) + 'K'
      if (price >= 1000) return price.toFixed(0)
      return price.toFixed(2)
    },
    getMarketColor (market) {
      const colors = { 'USStock': 'green', 'CNStock': 'blue', 'HKStock': 'geekblue', 'Crypto': 'purple', 'Forex': 'gold', 'Futures': 'cyan' }
      return colors[market] || 'default'
    },
    getMarketName (market) {
      return this.$t(`dashboard.analysis.market.${market}`) || market
    },
    async refreshUserInfoFromServer () {
      try {
        const res = await getUserInfo()
        if (res && res.code === 1 && res.data) {
          this.localUserInfo = res.data
          this.userId = res.data.id
          this.$store.commit('SET_INFO', res.data)
        }
      } catch (e) { /* silent */ }
    },
    selectWatchlistItem (stock) {
      this.selectedKey = `${stock.market}:${stock.symbol}`
      this.$emit('input', this.selectedKey)
      this.$emit('select', stock)
    },
    async loadUserInfo () {
      this.loadingUserInfo = true
      try {
        if (this.storeUserInfo && this.storeUserInfo.email) {
          this.localUserInfo = this.storeUserInfo
          this.userId = this.storeUserInfo.id
          this.loadingUserInfo = false
          this.loadWatchlist()
          return
        }
        const res = await getUserInfo()
        if (res && res.code === 1 && res.data) {
          this.localUserInfo = res.data
          this.userId = res.data.id
          this.$store.commit('SET_INFO', res.data)
          this.loadWatchlist()
        }
      } catch (error) { /* silent */ } finally { this.loadingUserInfo = false }
    },
    async loadWatchlist () {
      if (!this.userId) return
      this.loadingWatchlist = true
      try {
        const res = await getWatchlist({ userid: this.userId })
        if (res && res.code === 1 && res.data) {
          this.watchlist = res.data.map(item => ({ ...item, price: 0, change: 0, changePercent: 0 }))
          await this.loadWatchlistPrices()
        }
      } catch (error) { /* silent */ } finally { this.loadingWatchlist = false }
    },
    async loadWatchlistPrices () {
      if (!this.watchlist || this.watchlist.length === 0) return
      try {
        const watchlistData = this.watchlist.map(item => ({ market: item.market, symbol: item.symbol }))
        const res = await getWatchlistPrices({ watchlist: watchlistData })
        if (res && res.code === 1 && res.data) {
          const priceMap = {}; const pricesObj = {}
          res.data.forEach(item => {
            priceMap[`${item.market}-${item.symbol}`] = item
            pricesObj[`${item.market}:${item.symbol}`] = { price: item.price || 0, change: item.changePercent || 0 }
          })
          this.watchlistPrices = pricesObj
          this.watchlist = this.watchlist.map(item => {
            const key = `${item.market}-${item.symbol}`
            const priceData = priceMap[key]
            if (priceData) return { ...item, price: priceData.price || 0, change: priceData.change || 0, changePercent: priceData.changePercent || 0 }
            return item
          })
        }
      } catch (error) { /* silent */ }
    },
    startWatchlistPriceRefresh () {
      this.watchlistPriceTimer = setInterval(() => {
        if (this.watchlist && this.watchlist.length > 0) this.loadWatchlistPrices()
      }, 30000)
      if (this.watchlist && this.watchlist.length > 0) this.loadWatchlistPrices()
    },
    async handleAddStock () {
      let market = ''; let symbol = ''; let name = ''
      if (this.selectedSymbolForAdd) {
        market = this.selectedSymbolForAdd.market; symbol = this.selectedSymbolForAdd.symbol.toUpperCase(); name = this.selectedSymbolForAdd.name || ''
      } else if (this.symbolSearchKeyword && this.symbolSearchKeyword.trim()) {
        if (!this.selectedMarketTab) { this.$message.warning(this.$t('dashboard.analysis.modal.addStock.pleaseSelectMarket')); return }
        market = this.selectedMarketTab; symbol = this.symbolSearchKeyword.trim().toUpperCase(); name = ''
      } else { this.$message.warning(this.$t('dashboard.analysis.modal.addStock.pleaseSelectOrEnterSymbol')); return }
      this.addingStock = true
      try {
        const res = await addWatchlist({ userid: this.userId, market, symbol, name })
        if (res && res.code === 1) {
          this.$message.success(this.$t('dashboard.analysis.message.addStockSuccess'))
          this.handleCloseAddStockModal()
          await this.loadWatchlist()
          this.$emit('refresh')
        } else { this.$message.error(res?.msg || this.$t('dashboard.analysis.message.addStockFailed')) }
      } catch (error) { this.$message.error(error?.response?.data?.msg || error?.message || this.$t('dashboard.analysis.message.addStockFailed')) } finally { this.addingStock = false }
    },
    handleCloseAddStockModal () {
      this.showAddStockModal = false; this.selectedSymbolForAdd = null; this.symbolSearchKeyword = ''; this.symbolSearchResults = []; this.hasSearched = false
      this.selectedMarketTab = this.marketTypes.length > 0 ? this.marketTypes[0].value : ''
    },
    handleMarketTabChange (activeKey) {
      this.selectedMarketTab = activeKey; this.symbolSearchKeyword = ''; this.symbolSearchResults = []; this.selectedSymbolForAdd = null; this.hasSearched = false
      this.loadHotSymbols(activeKey)
    },
    handleSymbolSearchInput (e) {
      const keyword = e.target.value; this.symbolSearchKeyword = keyword
      if (this.searchTimer) clearTimeout(this.searchTimer)
      if (!keyword || keyword.trim() === '') { this.symbolSearchResults = []; this.hasSearched = false; this.selectedSymbolForAdd = null; return }
      this.searchTimer = setTimeout(() => { this.searchSymbolsInModal(keyword) }, 500)
    },
    handleSearchOrInput (keyword) {
      if (!keyword || !keyword.trim()) return
      if (!this.selectedMarketTab) { this.$message.warning(this.$t('dashboard.analysis.modal.addStock.pleaseSelectMarket')); return }
      if (this.symbolSearchResults.length > 0) return
      if (this.hasSearched && this.symbolSearchResults.length === 0) { this.handleDirectAdd() } else { this.searchSymbolsInModal(keyword) }
    },
    async searchSymbolsInModal (keyword) {
      if (!keyword || keyword.trim() === '') { this.symbolSearchResults = []; this.hasSearched = false; return }
      if (!this.selectedMarketTab) { this.$message.warning(this.$t('dashboard.analysis.modal.addStock.pleaseSelectMarket')); return }
      this.searchingSymbols = true; this.hasSearched = true
      try {
        const res = await searchSymbols({ market: this.selectedMarketTab, keyword: keyword.trim(), limit: 20 })
        if (res && res.code === 1 && res.data && res.data.length > 0) { this.symbolSearchResults = res.data } else {
          this.symbolSearchResults = []; this.selectedSymbolForAdd = { market: this.selectedMarketTab, symbol: keyword.trim().toUpperCase(), name: '' }
        }
      } catch (error) { this.symbolSearchResults = []; this.selectedSymbolForAdd = { market: this.selectedMarketTab, symbol: keyword.trim().toUpperCase(), name: '' } } finally { this.searchingSymbols = false }
    },
    handleDirectAdd () {
      if (!this.symbolSearchKeyword || !this.symbolSearchKeyword.trim()) { this.$message.warning(this.$t('dashboard.analysis.modal.addStock.pleaseEnterSymbol')); return }
      if (!this.selectedMarketTab) { this.$message.warning(this.$t('dashboard.analysis.modal.addStock.pleaseSelectMarket')); return }
      this.selectedSymbolForAdd = { market: this.selectedMarketTab, symbol: this.symbolSearchKeyword.trim().toUpperCase(), name: '' }
    },
    selectSymbol (symbol) {
      this.selectedSymbolForAdd = { market: symbol.market, symbol: symbol.symbol, name: symbol.name || symbol.symbol }
    },
    async loadHotSymbols (market) {
      if (!market) market = this.selectedMarketTab || (this.marketTypes.length > 0 ? this.marketTypes[0].value : '')
      if (!market) return
      this.loadingHotSymbols = true
      try {
        const res = await getHotSymbols({ market, limit: 10 })
        if (res && res.code === 1 && res.data) { this.hotSymbols = res.data } else { this.hotSymbols = [] }
      } catch (error) { this.hotSymbols = [] } finally { this.loadingHotSymbols = false }
    },
    async removeFromWatchlist (stock) {
      if (!this.userId) return
      const symbol = typeof stock === 'object' ? stock.symbol : stock
      const market = typeof stock === 'object' ? stock.market : arguments[1]
      try {
        const res = await removeWatchlist({ userid: this.userId, symbol, market })
        if (res && res.code === 1) {
          this.$message.success(this.$t('dashboard.analysis.message.removeStockSuccess'))
          await this.loadWatchlist()
          this.$emit('refresh')
        } else { this.$message.error(res?.msg || this.$t('dashboard.analysis.message.removeStockFailed')) }
      } catch (error) { this.$message.error(this.$t('dashboard.analysis.message.removeStockFailed')) }
    },
    async loadMarketTypes () {
      try {
        const res = await getMarketTypes()
        if (res && res.code === 1 && res.data && Array.isArray(res.data)) {
          this.marketTypes = res.data.map(item => ({ value: item.value, i18nKey: item.i18nKey || `dashboard.analysis.market.${item.value}` }))
        } else {
          this.marketTypes = [
            { value: 'USStock', i18nKey: 'dashboard.analysis.market.USStock' },
            { value: 'CNStock', i18nKey: 'dashboard.analysis.market.CNStock' },
            { value: 'HKStock', i18nKey: 'dashboard.analysis.market.HKStock' },
            { value: 'Crypto', i18nKey: 'dashboard.analysis.market.Crypto' },
            { value: 'Forex', i18nKey: 'dashboard.analysis.market.Forex' },
            { value: 'Futures', i18nKey: 'dashboard.analysis.market.Futures' }
          ]
        }
      } catch (error) {
        this.marketTypes = [
          { value: 'USStock', i18nKey: 'dashboard.analysis.market.USStock' },
          { value: 'CNStock', i18nKey: 'dashboard.analysis.market.CNStock' },
          { value: 'HKStock', i18nKey: 'dashboard.analysis.market.HKStock' },
          { value: 'Crypto', i18nKey: 'dashboard.analysis.market.Crypto' },
          { value: 'Forex', i18nKey: 'dashboard.analysis.market.Forex' },
          { value: 'Futures', i18nKey: 'dashboard.analysis.market.Futures' }
        ]
      }
      if (this.marketTypes.length > 0 && !this.selectedMarketTab) this.selectedMarketTab = this.marketTypes[0].value
    }
  }
}
</script>

<style lang="less" scoped>
.watchlist-panel {
  width: 320px;
  flex-shrink: 0;
  align-self: flex-start;
  max-height: calc(100vh - 200px);
  background: #fff;
  border-radius: 10px;
  border: 1px solid #eaeef3;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
  display: flex;
  flex-direction: column;
  overflow: hidden;

  .panel-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 14px; border-bottom: 1px solid #f0f2f5; background: #fafbfc;
    .panel-title { font-size: 13px; font-weight: 700; color: #333; letter-spacing: -0.1px; .anticon { color: #facc15; margin-right: 6px; } }
  }

  .watchlist-list {
    flex: 1; overflow-y: auto; padding: 6px 8px;
    &::-webkit-scrollbar { width: 3px; }
    &::-webkit-scrollbar-thumb { background: #d4d8dd; border-radius: 2px; }
    .watchlist-empty { text-align: center; padding: 24px 12px; color: #94a3b8; .anticon { font-size: 32px; margin-bottom: 8px; display: block; } p { font-size: 12px; margin-bottom: 12px; } }
  }

  &.theme-dark {
    background: #1a1a1c; border-color: rgba(255, 255, 255, 0.06); box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
    .panel-header { background: #141416; border-bottom-color: rgba(255, 255, 255, 0.05); .panel-title { color: #ccc; } }
    .panel-summary { background: #141416; border-bottom-color: rgba(255, 255, 255, 0.05); .summary-chip { border-right-color: rgba(255, 255, 255, 0.05); } .sc-num { color: #d4d4d4; } .sc-label { color: #666; } }
    .batch-bar { background: #1c1c1c; border: 1px solid #2a2a2a; border-radius: 10px; margin: 8px 10px; margin-bottom: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.2); }
    .batch-bar .batch-all-cb { color: #a0a0a8; }
    .batch-bar .ant-btn:not(.ant-btn-primary) { background: #2a2a2c; border-color: #3a3a3c; color: #b0b0b8; &:hover { background: #333336; border-color: var(--primary-color, #1890ff); color: var(--primary-color, #1890ff); } }
    .watchlist-list {
      &::-webkit-scrollbar-thumb { background: #333; }
      .wl-card {
        &:hover { background: #222224; border-color: rgba(255, 255, 255, 0.06); }
        &.active { background: color-mix(in srgb, var(--primary-color, #1890ff) 8%, transparent); border-color: color-mix(in srgb, var(--primary-color, #1890ff) 28%, transparent); }
        .wl-symbol { color: #e0e0e0; }
        .wl-name { color: #666; }
        .wl-market { color: #666; background: rgba(255, 255, 255, 0.06); }
        .wl-price { color: #d4d4d4; }
        .wl-pnl-qty { color: #666; }
        .wl-task-badge.paused { background: rgba(255, 255, 255, 0.05); color: #666; }
        .wl-task-next { color: #555; }
      }
      .wl-card-hover-actions { background: linear-gradient(90deg, transparent 0%, #222224 30%); .wl-hover-btn { background: #1a1a1c; color: #888; box-shadow: 0 1px 3px rgba(0,0,0,0.4); } .wl-hover-btn:hover { color: var(--primary-color, #1890ff); background: color-mix(in srgb, var(--primary-color, #1890ff) 12%, transparent); } .wl-hover-btn.danger:hover { color: #f87171; background: rgba(248, 113, 113, 0.1); } }
      .wl-card.active .wl-card-hover-actions { background: linear-gradient(90deg, transparent 0%, color-mix(in srgb, var(--primary-color, #1890ff) 6%, transparent) 30%); }
      .watchlist-empty { color: #555; }
      .we-icon { color: #333; }
    }
  }
}

.panel-header-actions { display: flex; align-items: center; gap: 4px; }
.panel-header-icon { font-size: 15px; color: #94a3b8; cursor: pointer; padding: 4px; border-radius: 6px; transition: color 0.2s, background 0.2s; }
.panel-header-icon:hover { color: var(--primary-color, #1890ff); background: rgba(24,144,255,0.08); }

.panel-summary { display: flex; gap: 0; padding: 0; border-bottom: 1px solid #f1f5f9; }
.summary-chip { flex: 1; display: flex; flex-direction: column; align-items: center; padding: 8px 4px; border-right: 1px solid #f1f5f9; }
.summary-chip:last-child { border-right: none; }
.sc-num { font-size: 14px; font-weight: 700; color: #0f172a; line-height: 1.2; font-family: 'SF Mono', Monaco, monospace; }
.sc-num.up { color: #16a34a; }
.sc-num.down { color: #dc2626; }
.sc-label { font-size: 9px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; margin-top: 2px; }

.batch-bar { display: flex; align-items: center; gap: 8px; padding: 10px 12px; margin: 8px 10px; margin-bottom: 4px; background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); flex-wrap: wrap; }
.batch-all-cb { font-size: 12px; font-weight: 500; color: #475569; margin-right: 4px; }
.batch-bar .ant-btn { border-radius: 6px; font-size: 12px; font-weight: 500; height: 28px; padding: 0 10px; flex-shrink: 0; transition: all 0.2s; }
.batch-bar .ant-btn-primary { box-shadow: 0 1px 2px color-mix(in srgb, var(--primary-color, #1890ff) 20%, transparent); &:hover { filter: brightness(1.05); } }
.batch-bar .ant-btn:not(.ant-btn-primary) { background: #f8fafc; border-color: #e2e8f0; color: #64748b; &:hover { background: #f1f5f9; border-color: #cbd5e1; color: #475569; } }

.wl-card { position: relative; padding: 10px 12px; border-radius: 8px; cursor: pointer; transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1); margin-bottom: 3px; border: 1px solid transparent; }
.wl-card:hover { background: #f5f7fa; border-color: #e8ecf1; }
.wl-card.active { background: linear-gradient(135deg, color-mix(in srgb, var(--primary-color, #1890ff) 6%, #fff) 0%, color-mix(in srgb, var(--primary-color, #1890ff) 4%, #fff) 100%); border-color: color-mix(in srgb, var(--primary-color, #1890ff) 28%, transparent); box-shadow: 0 1px 4px color-mix(in srgb, var(--primary-color, #1890ff) 10%, transparent); }
.wl-card-cb { position: absolute; top: 12px; left: 4px; z-index: 1; }
.wl-card-body { transition: padding-left 0.2s; }
.wl-card-body.with-cb { padding-left: 24px; }
.wl-row-main { display: grid; grid-template-columns: 1fr 80px auto; align-items: center; gap: 4px; }
.wl-info-left { display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
.wl-symbol-line { display: flex; align-items: baseline; gap: 5px; overflow: hidden; }
.wl-name { font-size: 11px; color: #94a3b8; line-height: 1.3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 1px; }
.wl-info-right { display: flex; flex-direction: column; align-items: flex-end; white-space: nowrap; }
.wl-symbol { font-size: 13px; font-weight: 700; color: #0f172a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.wl-market { font-size: 9px; color: #94a3b8; letter-spacing: 0.3px; padding: 1px 4px; background: #f1f5f9; border-radius: 3px; flex-shrink: 0; }
.wl-sparkline-wrap { width: 80px; padding: 0 2px; display: flex; align-items: center; justify-content: center; .wl-sparkline { width: 100%; height: 20px; } }
.wl-price { font-size: 12px; font-weight: 600; color: #0f172a; font-family: 'SF Mono', Monaco, monospace; }
.wl-change { font-size: 10px; font-weight: 600; font-family: 'SF Mono', Monaco, monospace; padding: 1px 5px; border-radius: 4px; margin-left: 4px; }
.wl-change.up { color: #16a34a; background: rgba(22,163,74,0.08); }
.wl-change.down { color: #dc2626; background: rgba(220,38,38,0.06); }
.wl-row-pnl { display: flex; align-items: center; gap: 8px; margin-top: 4px; font-family: 'SF Mono', Monaco, monospace; }
.wl-pnl-qty { font-size: 10px; color: #94a3b8; }
.wl-pnl-val { font-size: 10px; font-weight: 600; margin-left: auto; }
.wl-pnl-val.up { color: #16a34a; }
.wl-pnl-val.down { color: #dc2626; }
.wl-row-task { display: flex; align-items: center; gap: 6px; margin-top: 4px; }
.wl-task-badge { display: inline-flex; align-items: center; gap: 3px; font-size: 10px; padding: 1px 8px; border-radius: 10px; cursor: pointer; transition: all 0.2s; }
.wl-task-badge.active { color: #16a34a; background: rgba(22,163,74,0.08); }
.wl-task-badge.paused { color: #94a3b8; background: #f1f5f9; }
.wl-task-badge:hover { opacity: 0.75; }
.wl-task-next { font-size: 10px; color: #94a3b8; margin-left: auto; }

.wl-card-hover-actions { position: absolute; top: 0; right: 0; bottom: 0; display: flex; align-items: center; gap: 2px; padding-right: 8px; opacity: 0; transition: opacity 0.15s; background: linear-gradient(90deg, transparent 0%, #f8fafc 30%); border-radius: 0 8px 8px 0; pointer-events: none; }
.wl-card:hover .wl-card-hover-actions { opacity: 1; pointer-events: auto; }
.wl-card.active .wl-card-hover-actions { background: linear-gradient(90deg, transparent 0%, #e6f7ff 30%); }
.wl-hover-btn { display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px; border-radius: 6px; font-size: 13px; color: #64748b; cursor: pointer; transition: all 0.15s; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
.wl-hover-btn:hover { color: var(--primary-color, #1890ff); background: #e6f7ff; }
.wl-hover-btn.danger:hover { color: #dc2626; background: #fef2f2; }

.batch-modal-summary { margin-bottom: 16px; }
.batch-modal-summary p { font-size: 13px; color: #475569; margin-bottom: 8px; }
.batch-symbols-preview { display: flex; flex-wrap: wrap; gap: 4px; max-height: 80px; overflow-y: auto; }

.task-drawer-empty { text-align: center; padding: 48px 16px; color: #94a3b8; p { margin-top: 12px; font-size: 13px; } }
.task-drawer-list { display: flex; flex-direction: column; gap: 12px; }
.task-item { padding: 14px 16px; border: 1px solid #e2e8f0; border-radius: 10px; background: #fafafa; transition: box-shadow 0.2s; }
.task-item:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.06); }
.task-item-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.task-item-name { font-size: 13px; font-weight: 600; color: #0f172a; }
.task-item-meta { display: flex; gap: 16px; font-size: 12px; color: #64748b; margin-bottom: 10px; .anticon { margin-right: 4px; } }
.task-item-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; .ant-btn { border-radius: 6px; font-size: 12px; font-weight: 500; height: 28px; padding: 0 10px; display: inline-flex; align-items: center; gap: 4px; transition: all 0.2s; border-width: 1px; } }

.add-stock-modal-content {
  .market-tabs { margin-bottom: 16px; }
  .symbol-search-section { margin-bottom: 24px; }
  .search-results-section, .hot-symbols-section { margin-bottom: 24px; .section-title { font-size: 14px; font-weight: 600; color: #262626; margin-bottom: 12px; display: flex; align-items: center; } }
  .symbol-list { max-height: 200px; overflow-y: auto; border: 1px solid #e8e8e8; border-radius: 4px; .symbol-list-item { cursor: pointer; padding: 8px 12px; transition: background-color 0.3s; &:hover { background-color: #f5f5f5; } .symbol-item-content { display: flex; align-items: center; gap: 8px; .symbol-code { font-weight: 600; color: #262626; min-width: 80px; } .symbol-name { color: #595959; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; } } } }
  .selected-symbol-section { margin-top: 16px; .selected-symbol-info { display: flex; align-items: center; } }
}
</style>
