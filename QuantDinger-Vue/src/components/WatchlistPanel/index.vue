<template>
  <div class="watchlist-panel" :class="{ 'theme-dark': isDarkTheme }">
    <div class="panel-header">
      <a-dropdown :trigger="['click']" placement="bottomLeft" :visible="groupDropdownVisible" @visibleChange="onGroupDropdownVisibleChange">
        <span class="panel-title wl-group-switcher">
          <a-icon type="star" theme="filled" />
          <span class="wl-group-name">{{ groupLabel(currentGroup) }}</span>
          <a-icon type="down" class="wl-group-caret" />
        </span>
        <a-menu slot="overlay" class="wl-group-menu" @click="onGroupMenuClick">
          <template v-for="g in watchlistGroups">
            <a-menu-item :key="`switch:${g.name}`" :class="{ 'wl-group-current': g.name === currentGroup }">
              <div class="wl-group-item">
                <span class="wl-group-item-name">{{ groupLabel(g.name) }}</span>
                <span class="wl-group-item-count">{{ g.count }}</span>
                <a-icon v-if="g.name === currentGroup" type="check" class="wl-group-item-check" />
                <span v-if="g.name !== defaultGroupName" class="wl-group-item-ops" @click.stop>
                  <a-tooltip :title="$t('dashboard.analysis.watchlist.group.rename')">
                    <a-icon type="edit" class="wl-group-op" @click="openRenameGroup(g.name)" />
                  </a-tooltip>
                  <a-popconfirm
                    :title="$t('dashboard.analysis.watchlist.group.deleteConfirm', { count: g.count })"
                    :okText="$t('common.confirm')"
                    :cancelText="$t('common.cancel')"
                    @confirm="removeGroup(g.name)"
                  >
                    <a-tooltip :title="$t('dashboard.analysis.watchlist.group.delete')">
                      <a-icon type="delete" class="wl-group-op" />
                    </a-tooltip>
                  </a-popconfirm>
                </span>
              </div>
            </a-menu-item>
          </template>
        </a-menu>
      </a-dropdown>
      <span class="panel-header-actions">
        <a-tooltip :title="$t('aiAssetAnalysis.tasks.manage')">
          <a-badge :count="monitors.length" :offset="[-2, 2]" :number-style="{ fontSize: '9px', minWidth: '14px', height: '14px', lineHeight: '14px', padding: '0 3px' }">
            <a-icon type="unordered-list" class="panel-header-icon" @click="showTaskDrawer = true" />
          </a-badge>
        </a-tooltip>
        <a-tooltip :title="$t('aiAssetAnalysis.batch.schedule')">
          <a-icon type="schedule" class="panel-header-icon" @click="toggleBatchMode" />
        </a-tooltip>
        <a-icon type="plus" class="panel-header-icon" @click="openAddStockModal" />
      </span>
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
        v-for="stock in visibleWatchlist"
        :key="`wl-${stock.group_name || ''}-${stock.market}-${stock.symbol}`"
        class="wl-card"
        :class="{ active: selectedKey === `${stock.market}:${stock.symbol}`, 'drag-over': dragOverKey === `${stock.market}:${stock.symbol}` }"
        :draggable="stock.strategy_state ? 'false' : 'true'"
        @dragstart="onDragStart(stock, $event)"
        @dragover.prevent="onDragOver(stock, $event)"
        @drop="onDrop(stock, $event)"
        @dragend="onDragEnd"
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
          <div class="wl-row-main" :class="{ 'negative-news': stock.news_score !== undefined && stock.news_score < -4 }">
            <div class="wl-info-left">
              <div class="wl-symbol-line">
                <span class="wl-symbol" v-if="stock.name && stock.name !== stock.symbol">{{ stock.name }}</span>
                <span class="wl-symbol" v-else>{{ stock.symbol }}</span>
                <span class="wl-market">{{ getMarketName(stock.market) }}</span>
                <template v-if="stock.strategy_state">
                  <a-popover trigger="hover" placement="right">
                    <template slot="content">
                      <div class="wl-strategy-pop" v-html="strategyDetailHtml(stock)"></div>
                    </template>
                    <span class="wl-strategy-tag" :class="strategyTagClass(stock.strategy_state)">{{ strategyTagText(stock) }}</span>
                  </a-popover>
                  <span class="wl-strategy-mini" v-if="strategyEntryPrice(stock) !== null && strategyEntryPrice(stock) !== undefined">买{{ formatPrice(strategyEntryPrice(stock)) }}</span>
                  <span class="wl-strategy-mini" v-if="strategyStopPrice(stock)">损{{ formatPrice(strategyStopPrice(stock)) }}</span>
                  <span class="wl-strategy-pre" v-if="strategyPreConfirm(stock)">预{{ strategyPreConfirmText(stock) }}</span>
                </template>
              </div>
              <div class="wl-name" v-if="stock.name && stock.name !== stock.symbol">{{ stock.symbol }}</div>
            </div>
            <div class="wl-info-right" v-if="watchlistPrices[`${stock.market}:${stock.symbol}`]">
              <span class="wl-price">{{ formatPrice(watchlistPrices[`${stock.market}:${stock.symbol}`].price) }}</span>
              <span class="wl-change" :class="(watchlistPrices[`${stock.market}:${stock.symbol}`]?.change || 0) >= 0 ? 'up' : 'down'">
                {{ (watchlistPrices[`${stock.market}:${stock.symbol}`]?.change || 0) >= 0 ? '+' : '' }}{{ formatNum(watchlistPrices[`${stock.market}:${stock.symbol}`]?.change) }}%
              </span>
            </div>
            <div class="wl-news-score" v-if="stock.news_score !== undefined">
              <span v-if="stock.news_score > 4" class="wl-news-heart">♥</span>
              <span v-else class="wl-news-num" :class="{ 'news-negative': stock.news_score < -4 }">{{ stock.news_score }}</span>
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
          <span class="wl-hover-btn danger" v-if="!stock.strategy_state" @click.stop="removeFromWatchlist(stock)"><a-icon type="delete" /></span>
          <a-tooltip v-if="stock.strategy_state" title="龙回头Pro策略组: 由系统自动管理 (买/持/卖自动增删)"><span class="wl-hover-btn strategy-managed"><a-icon type="robot" /></span></a-tooltip>
        </div>
      </div>
      <div v-if="!watchlist || visibleWatchlist.length === 0" class="watchlist-empty">
        <div class="we-icon"><a-icon type="star" /></div>
        <p>{{ $t('dashboard.analysis.empty.noWatchlist') }}</p>
        <a-button type="primary" size="small" icon="plus" @click="openAddStockModal">
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
            :placeholder="selectedMarketTab === 'CNStock' ? $t('dashboard.analysis.modal.addStock.searchPlaceholderCN') : $t('dashboard.analysis.modal.addStock.searchOrInputPlaceholder')"
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

        <div v-if="selectedSymbolsForAdd.length > 0" class="selected-symbol-section">
          <a-alert :message="$t('dashboard.analysis.modal.addStock.selectedSymbol') + ' (' + selectedSymbolsForAdd.length + ')'" type="info" show-icon closable @close="selectedSymbolsForAdd = []">
            <template slot="description">
              <div class="selected-symbols-batch">
                <div v-for="(s, idx) in selectedSymbolsForAdd" :key="idx" class="selected-symbol-item">
                  <a-tag :color="getMarketColor(s.market)" size="small">
                    {{ $t(`dashboard.analysis.market.${s.market}`) }}
                  </a-tag>
                  <strong>{{ s.symbol }}</strong>
                  <span v-if="s.name" style="color: #999; margin-left: 4px;">{{ s.name }}</span>
                  <a-icon type="close-circle" class="remove-symbol-btn" @click="removeSelectedSymbol(idx)" />
                </div>
              </div>
            </template>
          </a-alert>
        </div>
        <div v-else-if="selectedSymbolForAdd" class="selected-symbol-section">
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
        <div class="group-fields-section">
          <div class="group-field">
            <span class="group-field-label">{{ $t('dashboard.analysis.watchlist.group.join') }}</span>
            <div class="group-field-control">
              <a-select
                v-model="addTargetGroup"
                style="width: 100%;"
                :placeholder="$t('dashboard.analysis.watchlist.group.newNamePlaceholder')"
                allow-clear
              >
                <a-select-option v-for="g in watchlistGroups" :key="g.name" :value="g.name">{{ groupLabel(g.name) }}</a-select-option>
              </a-select>
            </div>
          </div>
        </div>
      </div>
    </a-modal>

    <!-- 分组重命名弹窗 -->
    <a-modal
      :title="$t('dashboard.analysis.watchlist.group.renameModalTitle')"
      :visible="showRenameGroupModal"
      :confirmLoading="renamingGroup"
      @ok="submitRenameGroup"
      @cancel="showRenameGroupModal = false"
      :wrapClassName="isDarkTheme ? 'qd-dark-modal' : ''"
      :okText="$t('common.confirm')"
      :cancelText="$t('common.cancel')"
    >
      <a-input v-model="renameTargetGroup" :max-length="50" @pressEnter="submitRenameGroup" />
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
import { getWatchlist, addWatchlist, removeWatchlist, renameWatchlistGroup, removeWatchlistGroup, getWatchlistPrices, reorderWatchlist, getMarketTypes, searchSymbols, getHotSymbols } from '@/api/market'
import { getPositions, addPosition, getMonitors, addMonitor, updateMonitor, deleteMonitor } from '@/api/portfolio'

const DEFAULT_GROUP_NAME = '默认自选'

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
      dragKey: null,
      dragOverKey: null,
      localUserInfo: {},
      loadingUserInfo: false,
      userId: 1,
      watchlist: [],
      currentGroup: DEFAULT_GROUP_NAME,
      defaultGroupName: DEFAULT_GROUP_NAME,
      groupDropdownVisible: false,
      showRenameGroupModal: false,
      renameTargetGroup: '',
      renameOldGroup: '',
      renamingGroup: false,
      addTargetGroup: '',
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
      selectedSymbolsForAdd: [],
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
      return this.visibleWatchlist.length > 0 && this.batchSelectedKeys.length === this.visibleWatchlist.length
    },
    batchIndeterminate () {
      return this.batchSelectedKeys.length > 0 && this.batchSelectedKeys.length < this.visibleWatchlist.length
    },
    watchlistGroups () {
      const map = {}
      const wl = this.watchlist || []
      wl.forEach(s => {
        const name = s.group_name || DEFAULT_GROUP_NAME
        if (!map[name]) map[name] = { name, count: 0 }
        map[name].count += 1
      })
      const groups = Object.values(map)
      groups.sort((a, b) => (a.name === DEFAULT_GROUP_NAME ? -1 : b.name === DEFAULT_GROUP_NAME ? 1 : 0))
      return groups
    },
    visibleWatchlist () {
      const wl = this.watchlist || []
      const rows = wl.filter(s => (s.group_name || DEFAULT_GROUP_NAME) === this.currentGroup)
      const isStrategy = rows.some(s => s.strategy_state)
      if (!isStrategy) return rows
      const weight = { 'exit_today': 0, 'holding': 1, 'buy_today': 2, 'watch_pending': 3 }
      const wrMap = { 'v1': 76.5, 'break': 62.7, 'dragon2': 70.4 }
      return rows.slice().sort((a, b) => {
        const wa = weight[a.strategy_state] !== undefined ? weight[a.strategy_state] : 9
        const wb = weight[b.strategy_state] !== undefined ? weight[b.strategy_state] : 9
        if (wa !== wb) return wa - wb
        const da = a.strategy_detail || {}
        const dbb = b.strategy_detail || {}
        const wra = wrMap[da.strategy] || 0
        const wrb = wrMap[dbb.strategy] || 0
        if (wra !== wrb) return wrb - wra
        return (dbb.score || 0) - (da.score || 0)
      })
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
    },
    watchlist () {
      const names = new Set((this.watchlist || []).map(s => s.group_name || DEFAULT_GROUP_NAME))
      if (!names.has(this.currentGroup)) this.currentGroup = DEFAULT_GROUP_NAME
      this.batchSelectedKeys = this.batchSelectedKeys.filter(k => this.visibleWatchlist.some(s => `${s.market}:${s.symbol}` === k))
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
      if (e.target.checked) { this.batchSelectedKeys = this.visibleWatchlist.map(s => `${s.market}:${s.symbol}`) } else { this.batchSelectedKeys = [] }
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
        const stock = this.visibleWatchlist.find(s => s.market === market && s.symbol === symbol)
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
        const seen = new Set()
        const watchlistData = []
        this.watchlist.forEach(item => {
          const k = `${item.market}:${item.symbol}`
          if (seen.has(k)) return
          seen.add(k)
          watchlistData.push({ market: item.market, symbol: item.symbol })
        })
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
      let tick = 0
      this.watchlistPriceTimer = setInterval(() => {
        tick += 1
        if (this.watchlist && this.watchlist.length > 0) this.loadWatchlistPrices()
        if (tick % 4 === 0) this.refreshWatchlistSilent()   // 每 2 分钟同步策略组增删 (引擎自动管理)
      }, 30000)
      if (this.watchlist && this.watchlist.length > 0) this.loadWatchlistPrices()
    },
    async refreshWatchlistSilent () {
      if (!this.userId) return
      try {
        const res = await getWatchlist({ userid: this.userId })
        if (res && res.code === 1 && res.data) {
          this.watchlist = res.data.map(item => ({ ...item, price: 0, change: 0, changePercent: 0 }))
          await this.loadWatchlistPrices()
        }
      } catch (e) { /* silent */ }
    },
    strategyTagClass (state) {
      // 注意: strategy_state 存的是机器状态 (buy_today/holding/exit_today/watch_pending)
      return { 'buy_today': 'st-buy', 'holding': 'st-hold', 'exit_today': 'st-sell', 'watch_pending': 'st-watch' }[state] || 'st-watch'
    },
    strategyTagText (stock) {
      const d = stock.strategy_detail || {}
      const base = d.state_label || stock.strategy_state || ''
      return d.pre_confirm ? `${base}·预` : base
    },
    strategyEntryPrice (stock) { return (stock.strategy_detail || {}).entry_price },
    strategyStopPrice (stock) { return (stock.strategy_detail || {}).stop_price },
    strategyScore (stock) { const s = (stock.strategy_detail || {}).score; return (s === undefined || s === null) ? null : s },
    strategyPreConfirm (stock) { const pc = (stock.strategy_detail || {}).pre_confirm; return pc || null },
    strategyPreConfirmText (stock) {
      const m = { strong: '强', ok: '中', weak: '弱' }
      const pc = (stock.strategy_detail || {}).pre_confirm
      return m[pc] || pc || ''
    },
    strategyDetailHtml (stock) {
      const d = stock.strategy_detail || {}
      const esc = s => String(s === undefined || s === null ? '' : s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]))
      const row = (k, v) => (v !== undefined && v !== null && v !== '') ? `<tr><td class="k">${k}</td><td class="v">${esc(v)}</td></tr>` : ''
      const pcMap = { strong: '强', ok: '中', weak: '弱' }
      let html = '<table class="wl-dtable">'
      html += row('策略', d.strategy_label)
      html += row('历史胜率', d.winrate !== undefined && d.winrate !== null ? d.winrate + '%' : '')
      html += row('状态', (d.state_label || stock.strategy_state || '') + (d.pre_confirm ? `(预判:${pcMap[d.pre_confirm] || d.pre_confirm})` : ''))
      html += row('形态', d.entry_style)
      html += row('评分', d.score)
      if (d.lu_date) html += row('锚点日', `${d.lu_date}${d.pullback_days ? ` 回调${d.pullback_days}天` : ''}`)
      if (d.signal_date) html += row('信号', `${d.signal_date} @ ${d.signal_price || ''}`)
      if (d.turnover_anchor !== undefined && d.turnover_anchor !== null) html += row('换手(锚)', `${d.turnover_anchor}%${d.turnover_sig !== undefined && d.turnover_sig !== null ? ` / 信${d.turnover_sig}%` : ''}`)
      if (d.float_mcap_yi !== undefined && d.float_mcap_yi !== null) html += row('流通市值', `${d.float_mcap_yi}亿`)
      if (d.ma60_slope !== undefined && d.ma60_slope !== null) html += row('MA60斜率', `${d.ma60_slope}%${d.ma_bull ? ' 多头排列' : ''}`)
      if (d.entry_date) html += row('买入', `${d.entry_date} @ ${d.entry_price || ''}`)
      if (d.stop_price) html += row('止损', d.stop_price)
      if (d.d1_chg !== undefined && d.d1_chg !== null) html += row('D1确认', `${d.d1_chg > 0 ? '+' : ''}${d.d1_chg}%${d.d1_vol_r !== undefined && d.d1_vol_r !== null ? ` 量比${d.d1_vol_r}` : ''}`)
      if (d.exit_reason) html += row('出场', `${d.exit_reason}${d.exit_date ? ` (${d.exit_date} @ ${d.exit_price || ''})` : ''}`)
      html += '</table>'
      html += '<div class="wl-strategy-foot">自动策略组 · 系统自动管理</div>'
      return html
    },
    onDragStart (stock, e) {
      this.dragKey = `${stock.market}:${stock.symbol}`
      try {
        e.dataTransfer.setData('text/plain', this.dragKey)
        e.dataTransfer.effectAllowed = 'move'
      } catch (err) { /* 旧浏览器忽略 */ }
    },
    onDragOver (stock, e) {
      const k = `${stock.market}:${stock.symbol}`
      if (this.dragKey && k !== this.dragKey && !stock.strategy_state) this.dragOverKey = k
    },
    onDragEnd () {
      this.dragKey = null
      this.dragOverKey = null
    },
    async onDrop (stock, e) {
      const fromKey = this.dragKey
      this.dragKey = null
      this.dragOverKey = null
      if (!fromKey) return
      const toKey = `${stock.market}:${stock.symbol}`
      if (fromKey === toKey || stock.strategy_state) return
      const list = this.visibleWatchlist.slice()
      const fromIdx = list.findIndex(s => `${s.market}:${s.symbol}` === fromKey)
      const toIdx = list.findIndex(s => `${s.market}:${s.symbol}` === toKey)
      if (fromIdx < 0 || toIdx < 0) return
      const moving = list[fromIdx]
      if (moving.strategy_state) return
      list.splice(fromIdx, 1)
      list.splice(toIdx, 0, moving)
      const orderIds = list.map(s => s.id)
      this.watchlist = this.watchlist.slice().sort((a, b) => {
        const ia = orderIds.indexOf(a.id)
        const ib = orderIds.indexOf(b.id)
        return (ia < 0 ? 99999 : ia) - (ib < 0 ? 99999 : ib)
      })
      try {
        await reorderWatchlist({ items: list.map((s, i) => ({ id: s.id, sort_order: i + 1 })) })
      } catch (err) { /* 静默: 下次同步按服务器顺序 */ }
    },
    strategyDetailText (stock) {
      const d = stock.strategy_detail || {}
      const pcMap = { strong: '强', ok: '中', weak: '弱' }
      const lines = []
      lines.push(`状态: ${d.state_label || stock.strategy_state || ''}${d.pre_confirm ? `(预判:${pcMap[d.pre_confirm] || d.pre_confirm})` : ''}`)
      if (d.entry_style) lines.push(`形态: ${d.entry_style}`)
      if (d.score !== undefined && d.score !== null) lines.push(`评分: ${d.score}`)
      if (d.turnover_anchor !== undefined && d.turnover_anchor !== null) lines.push(`换手(锚): ${d.turnover_anchor}%${d.turnover_sig !== undefined && d.turnover_sig !== null ? ` / 信${d.turnover_sig}%` : ''}`)
      if (d.float_mcap_yi !== undefined && d.float_mcap_yi !== null) lines.push(`流通市值: ${d.float_mcap_yi}亿`)
      if (d.ma60_slope !== undefined && d.ma60_slope !== null) lines.push(`MA60五日斜率: ${d.ma60_slope}%${d.ma_bull ? ' 多头排列' : ''}`)
      if (d.lu_date) lines.push(`锚点日: ${d.lu_date}${d.pullback_days ? ` 回调${d.pullback_days}天` : ''}`)
      if (d.signal_date) lines.push(`信号: ${d.signal_date} @ ${d.signal_price || ''}`)
      if (d.entry_date) lines.push(`买入: ${d.entry_date} @ ${d.entry_price || ''}`)
      if (d.stop_price) lines.push(`止损: ${d.stop_price}`)
      if (d.d1_chg !== undefined && d.d1_chg !== null) lines.push(`D1确认: ${d.d1_chg > 0 ? '+' : ''}${d.d1_chg}% 量比${d.d1_vol_r || ''}`)
      if (d.exit_reason) lines.push(`出场: ${d.exit_reason}${d.exit_date ? ` (${d.exit_date} @ ${d.exit_price || ''})` : ''}`)
      lines.push('龙回头Pro · 系统自动管理 (买/持/卖自动增删)')
      return lines.join('\n')
    },
    async handleAddStock () {
      // Determine which symbols to add
      const symbolsToAdd = []
      if (this.selectedSymbolsForAdd.length > 0) {
        // Batch mode: use selected symbols from search results
        for (const s of this.selectedSymbolsForAdd) {
          symbolsToAdd.push({ market: s.market, symbol: s.symbol.toUpperCase(), name: s.name || '' })
        }
      } else if (this.selectedSymbolForAdd) {
        // Single mode: picked from search results
        symbolsToAdd.push({ market: this.selectedSymbolForAdd.market, symbol: this.selectedSymbolForAdd.symbol.toUpperCase(), name: this.selectedSymbolForAdd.name || '' })
      } else if (this.symbolSearchKeyword && this.symbolSearchKeyword.trim()) {
        if (!this.selectedMarketTab) { this.$message.warning(this.$t('dashboard.analysis.modal.addStock.pleaseSelectMarket')); return }
        // Comma-separated raw input without search results
        const parts = this.symbolSearchKeyword.split(/[，,]/).map(s => s.trim().toUpperCase()).filter(Boolean)
        for (const sym of parts) {
          symbolsToAdd.push({ market: this.selectedMarketTab, symbol: sym, name: '' })
        }
      } else { this.$message.warning(this.$t('dashboard.analysis.modal.addStock.pleaseSelectOrEnterSymbol')); return }
      const group = (this.addTargetGroup || '').trim() || this.currentGroup || DEFAULT_GROUP_NAME
      if (group.length > 50) { this.$message.warning(this.$t('dashboard.analysis.watchlist.group.nameTooLong')); return }
      if (symbolsToAdd.length === 0) { this.$message.warning(this.$t('dashboard.analysis.modal.addStock.pleaseSelectOrEnterSymbol')); return }
      this.addingStock = true
      try {
        let successCount = 0
        const failedSymbols = []
        for (const { market, symbol, name } of symbolsToAdd) {
          try {
            const res = await addWatchlist({ userid: this.userId, market, symbol, name, group_name: group })
            if (res && res.code === 1) {
              successCount++
            } else if (res && res.data && res.data.candidates) {
              // Single match — auto pick first candidate
              const c = res.data.candidates[0]
              const res2 = await addWatchlist({ userid: this.userId, market: c.market || market, symbol: c.symbol, name: c.name || c.symbol, group_name: group })
              if (res2 && res2.code === 1) successCount++
              else failedSymbols.push(symbol)
            } else {
              failedSymbols.push(symbol)
            }
          } catch (e) {
            failedSymbols.push(symbol)
          }
        }
        if (successCount > 0) {
          const msg = symbolsToAdd.length === 1
            ? this.$t('dashboard.analysis.message.addStockSuccess')
            : `${this.$t('dashboard.analysis.message.addStockSuccess')} (${successCount}/${symbolsToAdd.length})`
          this.$message.success(msg)
          this.handleCloseAddStockModal()
          await this.loadWatchlist()
          this.$emit('refresh')
        }
        if (failedSymbols.length > 0) {
          this.$message.warning(this.$t('dashboard.analysis.message.addStockFailed') + ': ' + failedSymbols.join(', '))
        }
      } catch (error) { this.$message.error(error?.response?.data?.msg || error?.message || this.$t('dashboard.analysis.message.addStockFailed')) } finally { this.addingStock = false }
    },
    handleCloseAddStockModal () {
      this.showAddStockModal = false; this.selectedSymbolForAdd = null; this.selectedSymbolsForAdd = []; this.symbolSearchKeyword = ''; this.symbolSearchResults = []; this.hasSearched = false
      this.addTargetGroup = ''
      this.selectedMarketTab = this.marketTypes.length > 0 ? this.marketTypes[0].value : ''
    },
    handleMarketTabChange (activeKey) {
      this.selectedMarketTab = activeKey; this.symbolSearchKeyword = ''; this.symbolSearchResults = []; this.selectedSymbolForAdd = null; this.hasSearched = false
      this.loadHotSymbols(activeKey)
    },
    handleSymbolSearchInput (e) {
      const keyword = e.target.value; this.symbolSearchKeyword = keyword
      if (this.searchTimer) clearTimeout(this.searchTimer)
      if (!keyword || keyword.trim() === '') { this.symbolSearchResults = []; this.hasSearched = false; this.selectedSymbolForAdd = null; this.selectedSymbolsForAdd = []; return }
      // Check if comma-separated (batch input)
      const parts = keyword.split(/[，,]/).map(s => s.trim()).filter(Boolean)
      if (parts.length >= 2) {
        // Batch mode: search each symbol
        this.searchTimer = setTimeout(() => { this.searchBatchSymbols(parts) }, 500)
        return
      }
      if (keyword.trim().length < 2) { this.symbolSearchResults = []; this.hasSearched = false; return }
      this.searchTimer = setTimeout(() => { this.searchSymbolsInModal(keyword) }, 500)
    },
    handleSearchOrInput (keyword) {
      if (!keyword || !keyword.trim()) return
      if (!this.selectedMarketTab) { this.$message.warning(this.$t('dashboard.analysis.modal.addStock.pleaseSelectMarket')); return }
      if (this.symbolSearchResults.length > 0) return
      // Check if comma-separated (batch input)
      const parts = keyword.split(/[，,]/).map(s => s.trim()).filter(Boolean)
      if (parts.length >= 2) {
        this.searchBatchSymbols(parts)
        return
      }
      if (this.hasSearched && this.symbolSearchResults.length === 0) { this.handleDirectAdd() } else { this.searchSymbolsInModal(keyword) }
    },
    async searchBatchSymbols (symbols) {
      if (!this.selectedMarketTab) { this.$message.warning(this.$t('dashboard.analysis.modal.addStock.pleaseSelectMarket')); return }
      this.searchingSymbols = true; this.hasSearched = true
      const results = []
      try {
        for (const sym of symbols) {
          if (sym.length < 1) continue
          try {
            const res = await searchSymbols({ market: this.selectedMarketTab, keyword: sym, limit: 5 })
            if (res && res.code === 1 && res.data && res.data.length > 0) {
              // Pick best match (first result)
              results.push(res.data[0])
            } else {
              // No match, add as raw input
              results.push({ market: this.selectedMarketTab, symbol: sym.toUpperCase(), name: '' })
            }
          } catch (e) {
            results.push({ market: this.selectedMarketTab, symbol: sym.toUpperCase(), name: '' })
          }
        }
        this.symbolSearchResults = results
        // Auto-select all for batch add
        this.selectedSymbolsForAdd = [...results]
      } catch (error) { this.symbolSearchResults = [] } finally { this.searchingSymbols = false }
    },
    async searchSymbolsInModal (keyword) {
      if (!keyword || keyword.trim().length < 2) { this.symbolSearchResults = []; this.hasSearched = false; return }
      if (!this.selectedMarketTab) { this.$message.warning(this.$t('dashboard.analysis.modal.addStock.pleaseSelectMarket')); return }
      this.searchingSymbols = true; this.hasSearched = true
      try {
        // Use smart search: supports pinyin, Chinese name, and code for CNStock
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
      // Toggle selection in batch mode (click once to select, click again to deselect)
      const idx = this.selectedSymbolsForAdd.findIndex(s => s.symbol === symbol.symbol && s.market === symbol.market)
      if (idx >= 0) {
        this.selectedSymbolsForAdd.splice(idx, 1)
      } else {
        this.selectedSymbolsForAdd.push(symbol)
      }
    },
    removeSelectedSymbol (idx) {
      this.selectedSymbolsForAdd.splice(idx, 1)
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
    openAddStockModal () {
      this.addTargetGroup = this.currentGroup
      this.showAddStockModal = true
    },
    groupLabel (name) {
      if (!name || name === DEFAULT_GROUP_NAME) return this.$t('dashboard.analysis.watchlist.group.default')
      return name
    },
    onGroupDropdownVisibleChange (visible) {
      this.groupDropdownVisible = visible
    },
    onGroupMenuClick ({ key }) {
      if (typeof key === 'string' && key.startsWith('switch:')) this.switchGroup(key.slice(7))
    },
    switchGroup (name) {
      if (name !== this.currentGroup) this.currentGroup = name
      this.batchSelectedKeys = this.batchSelectedKeys.filter(k => this.visibleWatchlist.some(s => `${s.market}:${s.symbol}` === k))
      this.groupDropdownVisible = false
    },
    openRenameGroup (name) {
      this.renameOldGroup = name
      this.renameTargetGroup = name
      this.groupDropdownVisible = false
      this.showRenameGroupModal = true
    },
    async submitRenameGroup () {
      const newName = (this.renameTargetGroup || '').trim()
      if (!newName) { this.$message.warning(this.$t('dashboard.analysis.watchlist.group.nameRequired')); return }
      if (newName.length > 50) { this.$message.warning(this.$t('dashboard.analysis.watchlist.group.nameTooLong')); return }
      if (newName === this.renameOldGroup) { this.showRenameGroupModal = false; return }
      if (this.watchlistGroups.some(g => g.name === newName)) { this.$message.warning(this.$t('dashboard.analysis.watchlist.group.nameExists')); return }
      this.renamingGroup = true
      try {
        const res = await renameWatchlistGroup({ userid: this.userId, old_name: this.renameOldGroup, new_name: newName })
        if (res && res.code === 1) {
          this.$message.success(this.$t('dashboard.analysis.watchlist.group.renamed'))
          this.showRenameGroupModal = false
          if (this.currentGroup === this.renameOldGroup) this.currentGroup = newName
          await this.loadWatchlist()
        } else {
          const msg = res && res.msg
          this.$message.error(msg || this.$t('dashboard.analysis.watchlist.group.renameFailed'))
          if (msg) this.showRenameGroupModal = false
        }
      } catch (error) {
        this.$message.error(error?.response?.data?.msg || error?.message || this.$t('dashboard.analysis.watchlist.group.renameFailed'))
      } finally { this.renamingGroup = false }
    },
    async removeGroup (name) {
      this.groupDropdownVisible = false
      try {
        const res = await removeWatchlistGroup({ userid: this.userId, group_name: name })
        if (res && res.code === 1) {
          this.$message.success(this.$t('dashboard.analysis.watchlist.group.deleted'))
          if (this.currentGroup === name) this.currentGroup = DEFAULT_GROUP_NAME
          await this.loadWatchlist()
        } else {
          this.$message.error(res?.msg || this.$t('dashboard.analysis.watchlist.group.deleteFailed'))
        }
      } catch (error) {
        this.$message.error(error?.response?.data?.msg || error?.message || this.$t('dashboard.analysis.watchlist.group.deleteFailed'))
      }
    },
    async loadMarketTypes () {
      try {
        const res = await getMarketTypes()
        if (res && res.code === 1 && res.data && Array.isArray(res.data)) {
          this.marketTypes = res.data.map(item => ({ value: item.value, i18nKey: item.i18nKey || `dashboard.analysis.market.${item.value}` }))
        } else {
          this.marketTypes = [
            { value: 'CNStock', i18nKey: 'dashboard.analysis.market.CNStock' },
            { value: 'USStock', i18nKey: 'dashboard.analysis.market.USStock' },
            { value: 'HKStock', i18nKey: 'dashboard.analysis.market.HKStock' },
            { value: 'Crypto', i18nKey: 'dashboard.analysis.market.Crypto' },
            { value: 'Forex', i18nKey: 'dashboard.analysis.market.Forex' },
            { value: 'Futures', i18nKey: 'dashboard.analysis.market.Futures' }
          ]
        }
      } catch (error) {
        this.marketTypes = [
          { value: 'CNStock', i18nKey: 'dashboard.analysis.market.CNStock' },
          { value: 'USStock', i18nKey: 'dashboard.analysis.market.USStock' },
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
    padding: 9px 14px; border-bottom: 1px solid #f0f2f5; background: #fafbfc;
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

.batch-bar { display: flex; align-items: center; gap: 8px; padding: 10px 12px; margin: 8px 10px; margin-bottom: 4px; background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); flex-wrap: wrap; }
.batch-all-cb { font-size: 12px; font-weight: 500; color: #475569; margin-right: 4px; }
.batch-bar .ant-btn { border-radius: 6px; font-size: 12px; font-weight: 500; height: 28px; padding: 0 10px; flex-shrink: 0; transition: all 0.2s; }
.batch-bar .ant-btn-primary { box-shadow: 0 1px 2px color-mix(in srgb, var(--primary-color, #1890ff) 20%, transparent); &:hover { filter: brightness(1.05); } }
.batch-bar .ant-btn:not(.ant-btn-primary) { background: #f8fafc; border-color: #e2e8f0; color: #64748b; &:hover { background: #f1f5f9; border-color: #cbd5e1; color: #475569; } }

.wl-card { position: relative; padding: 8px 12px; border-radius: 8px; cursor: pointer; transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1); margin-bottom: 2px; border: 1px solid transparent; }
.wl-card:hover { background: #f5f7fa; border-color: #e8ecf1; }
.wl-card.active { background: linear-gradient(135deg, color-mix(in srgb, var(--primary-color, #1890ff) 6%, #fff) 0%, color-mix(in srgb, var(--primary-color, #1890ff) 4%, #fff) 100%); border-color: color-mix(in srgb, var(--primary-color, #1890ff) 28%, transparent); box-shadow: 0 1px 4px color-mix(in srgb, var(--primary-color, #1890ff) 10%, transparent); }
.wl-card-cb { position: absolute; top: 12px; left: 4px; z-index: 1; }
.wl-card-body { transition: padding-left 0.2s; }
.wl-card-body.with-cb { padding-left: 24px; }
.wl-row-main { display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 4px; }
.wl-info-left { display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
.wl-symbol-line { display: flex; align-items: baseline; gap: 5px; overflow: hidden; }
.wl-name { font-size: 12px; color: #94a3b8; line-height: 1.3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 1px; }
.wl-info-right { display: flex; flex-direction: column; align-items: flex-end; white-space: nowrap; }
.wl-symbol { font-size: 12px; font-weight: 700; color: #0f172a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.wl-market { font-size: 9px; color: #94a3b8; letter-spacing: 0.3px; padding: 1px 4px; background: #f1f5f9; border-radius: 3px; flex-shrink: 0; }

.wl-price { font-size: 11px; font-weight: 600; color: #0f172a; font-family: 'SF Mono', Monaco, monospace; }
.wl-change { font-size: 12px; font-weight: 600; font-family: 'SF Mono', Monaco, monospace; padding: 1px 5px; border-radius: 4px; margin-left: 4px; }
.wl-change.up { color: #ef4444; background: rgba(239,68,68,0.08); }
.wl-change.down { color: #10b981; background: rgba(16,185,129,0.06); }
.wl-row-pnl { display: flex; align-items: center; gap: 8px; margin-top: 4px; font-family: 'SF Mono', Monaco, monospace; }
.wl-pnl-qty { font-size: 10px; color: #94a3b8; }
.wl-pnl-val { font-size: 10px; font-weight: 600; margin-left: auto; }
.wl-pnl-val.up { color: #ef4444; }
.wl-pnl-val.down { color: #10b981; }
.wl-row-task { display: flex; align-items: center; gap: 6px; margin-top: 4px; }
.wl-task-badge { display: inline-flex; align-items: center; gap: 3px; font-size: 10px; padding: 1px 8px; border-radius: 10px; cursor: pointer; transition: all 0.2s; }
.wl-task-badge.active { color: #16a34a; background: rgba(22,163,74,0.08); }
.wl-task-badge.paused { color: #94a3b8; background: #f1f5f9; }
.wl-task-badge:hover { opacity: 0.75; }
.wl-task-next { font-size: 10px; color: #94a3b8; margin-left: auto; }
.wl-row-strategy { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.wl-strategy-tag { display: inline-flex; align-items: center; font-size: 10px; font-weight: 700; padding: 1px 8px; border-radius: 10px; cursor: default; }
.wl-strategy-tag.st-buy { color: #ffffff; background: #15803d; }
.wl-strategy-tag.st-hold { color: #ffffff; background: #2563eb; }
.wl-strategy-tag.st-sell { color: #ffffff; background: #dc2626; }
.wl-strategy-tag.st-watch { color: #94a3b8; background: #f1f5f9; }
.wl-strategy-item { font-size: 10px; color: #64748b; font-family: 'SF Mono', Monaco, monospace; }
.wl-strategy-k { color: #94a3b8; margin-right: 1px; }
.wl-strategy-pre { font-size: 10px; color: #d97706; font-weight: 600; }
.wl-strategy-mini { font-size: 10px; color: #475569; font-family: 'SF Mono', Monaco, monospace; white-space: nowrap; }
.wl-strategy-mini:first-of-type { color: #15803d; }
.wl-card.drag-over { border-color: #2563eb !important; background: rgba(37,99,235,0.06); }
.wl-strategy-pop table.wl-dtable { border-collapse: collapse; font-size: 11px; }
.wl-strategy-pop table.wl-dtable td { padding: 2px 8px; border-bottom: 1px solid #f1f5f9; }
.wl-strategy-pop table.wl-dtable td.k { color: #94a3b8; white-space: nowrap; padding-right: 12px; }
.wl-strategy-pop table.wl-dtable td.v { color: #0f172a; font-weight: 500; }
.wl-strategy-pop .wl-strategy-foot { margin-top: 6px; font-size: 10px; color: #94a3b8; }
.wl-hover-btn.strategy-managed { color: #94a3b8; cursor: default; }

.negative-news { background: rgba(239, 68, 68, 0.08) !important; border-color: rgba(239, 68, 68, 0.2) !important; }
.wl-news-score { display: flex; align-items: center; justify-content: center; min-width: 24px; }
.wl-news-heart { color: #ef4444; font-size: 14px; }
.wl-news-num { font-size: 11px; font-weight: 600; font-family: 'SF Mono', Monaco, monospace; color: #64748b; }
.wl-news-num.news-negative { color: #10b981; }

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
  .selected-symbols-batch { max-height: 120px; overflow-y: auto; .selected-symbol-item { display: flex; align-items: center; padding: 4px 0; border-bottom: 1px solid #f0f0f0; &:last-child { border-bottom: none; } .remove-symbol-btn { margin-left: auto; color: #999; cursor: pointer; &:hover { color: #ff4d4f; } } } }
}

.wl-group-switcher {
  cursor: pointer; display: inline-flex; align-items: center; gap: 5px;
  transition: color 0.2s;
  &:hover { color: var(--primary-color, #1890ff); }
  .wl-group-caret { font-size: 10px; transform: scale(0.85); }
}
.wl-group-name { max-width: 110px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.group-fields-section { margin-top: 16px; }
.group-field { display: flex; align-items: center; gap: 10px; margin-bottom: 10px;
  .group-field-label { flex-shrink: 0; width: 84px; font-size: 13px; color: #595959; }
  .group-field-control { flex: 1; min-width: 0; }
}

</style>

<style lang="less">
.wl-group-menu {
  min-width: 200px;
  max-height: 320px;
  overflow-y: auto;
  .ant-dropdown-menu-item { padding: 0; line-height: 1.4; }
  .wl-group-item { display: flex; align-items: center; gap: 8px; padding: 6px 12px; min-height: 34px;
    .wl-group-item-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
    .wl-group-item-count { flex-shrink: 0; min-width: 18px; text-align: center; font-size: 11px; color: #8c8c8c; background: #f0f2f5; border-radius: 9px; padding: 0 5px; line-height: 18px; }
    .wl-group-item-check { flex-shrink: 0; color: var(--primary-color, #1890ff); }
    .wl-group-item-ops { display: none; flex-shrink: 0; gap: 6px; align-items: center; }
    .wl-group-op { font-size: 14px; color: #8c8c8c; cursor: pointer; transition: color 0.15s;
      &:hover { color: #ff4d4f; } }
  }
  .ant-dropdown-menu-item:hover .wl-group-item-ops { display: inline-flex; }
  .wl-group-current { background: color-mix(in srgb, var(--primary-color, #1890ff) 8%, transparent); }
}
body.dark .wl-group-menu, body.realdark .wl-group-menu {
  background: #1a1a1c; border: 1px solid rgba(255, 255, 255, 0.1);
  box-shadow: 0 3px 12px rgba(0, 0, 0, 0.5);
  .ant-dropdown-menu-item { color: #ccc; &:hover { background: #26262a; } }
  .wl-group-item-count { background: #2a2a2c; color: #888; }
}
</style>
