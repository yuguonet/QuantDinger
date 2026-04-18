<template>
  <a-modal
    :visible="visible"
    :title="$t('polymarket.analysis.title')"
    :width="900"
    :footer="null"
    :wrapClassName="isDarkTheme ? 'qd-dark-modal' : ''"
    @cancel="handleClose"
    :maskClosable="false"
  >
    <div class="polymarket-analysis-modal" :class="{ 'theme-dark': isDarkTheme }">
      <a-tabs default-active-key="analyze" @change="handleTabChange">
        <a-tab-pane key="analyze" :tab="$t('polymarket.analysis.tabAnalyze')">
          <!-- 输入区域 -->
          <div class="input-section" v-if="!analysisResult">
            <a-alert
              :message="$t('polymarket.analysis.inputHint')"
              type="info"
              show-icon
              style="margin-bottom: 16px;"
            />
            <a-textarea
              v-model="inputText"
              :placeholder="$t('polymarket.analysis.inputPlaceholder')"
              :rows="3"
              :disabled="analyzing"
              @pressEnter="handleAnalyze"
            />
            <div class="input-actions">
              <a-button
                type="primary"
                size="large"
                icon="thunderbolt"
                :loading="analyzing"
                :disabled="!inputText.trim()"
                @click="handleAnalyze"
                style="width: 100%; margin-top: 12px;"
              >
                {{ $t('polymarket.analysis.analyzeButton') }}
              </a-button>
            </div>
          </div>

          <!-- 分析结果区域 -->
          <div class="result-section" v-if="analysisResult">
            <!-- 市场信息 -->
            <div class="market-info">
              <h3>{{ analysisResult.market.question }}</h3>
              <div class="market-meta">
                <a-tag :color="getStatusColor(analysisResult.market.status)">
                  {{ analysisResult.market.status }}
                </a-tag>
                <span class="meta-item">
                  <a-icon type="percentage" />
                  {{ analysisResult.market.current_probability }}%
                </span>
                <span class="meta-item" v-if="analysisResult.market.volume_24h">
                  <a-icon type="dollar" />
                  {{ formatVolume(analysisResult.market.volume_24h) }}
                </span>
                <a-button
                  type="link"
                  size="small"
                  icon="link"
                  :href="analysisResult.market.polymarket_url"
                  target="_blank"
                  v-if="analysisResult.market.polymarket_url"
                >
                  {{ $t('polymarket.analysis.viewOnPolymarket') }}
                </a-button>
              </div>
            </div>

            <!-- AI分析结果 -->
            <div class="analysis-result">
              <a-divider>{{ $t('polymarket.analysis.aiAnalysis') }}</a-divider>

              <!-- 预测概率对比 -->
              <div class="probability-comparison">
                <div class="prob-item">
                  <div class="prob-label">{{ $t('polymarket.analysis.marketProbability') }}</div>
                  <div class="prob-value">{{ analysisResult.analysis.market_probability }}%</div>
                </div>
                <div class="prob-item">
                  <div class="prob-label">{{ $t('polymarket.analysis.aiPredictedProbability') }}</div>
                  <div class="prob-value ai-prob">{{ analysisResult.analysis.ai_predicted_probability }}%</div>
                </div>
                <div class="prob-item">
                  <div class="prob-label">{{ $t('polymarket.analysis.divergence') }}</div>
                  <div class="prob-value" :class="getDivergenceClass(analysisResult.analysis.divergence)">
                    {{ analysisResult.analysis.divergence >= 0 ? '+' : '' }}{{ analysisResult.analysis.divergence.toFixed(2) }}%
                  </div>
                </div>
              </div>

              <!-- 推荐和评分 -->
              <div class="recommendation-section">
                <a-row :gutter="16">
                  <a-col :span="8">
                    <div class="rec-card">
                      <div class="rec-label">{{ $t('polymarket.analysis.recommendation') }}</div>
                      <a-tag
                        :color="getRecommendationColor(analysisResult.analysis.recommendation)"
                        style="font-size: 16px; padding: 4px 12px;"
                      >
                        {{ getRecommendationLabel(analysisResult.analysis.recommendation) }}
                      </a-tag>
                    </div>
                  </a-col>
                  <a-col :span="8">
                    <div class="rec-card">
                      <div class="rec-label">{{ $t('polymarket.analysis.confidenceScore') }}</div>
                      <div class="rec-value">{{ analysisResult.analysis.confidence_score.toFixed(0) }}</div>
                    </div>
                  </a-col>
                  <a-col :span="8">
                    <div class="rec-card">
                      <div class="rec-label">{{ $t('polymarket.analysis.opportunityScore') }}</div>
                      <div class="rec-value">{{ analysisResult.analysis.opportunity_score.toFixed(0) }}</div>
                    </div>
                  </a-col>
                </a-row>
              </div>

              <!-- 分析理由 -->
              <div class="reasoning-section" v-if="analysisResult.analysis.reasoning">
                <h4>{{ $t('polymarket.analysis.reasoning') }}</h4>
                <p class="reasoning-text">{{ analysisResult.analysis.reasoning }}</p>
              </div>

              <!-- 关键因素 -->
              <div class="key-factors-section" v-if="analysisResult.analysis.key_factors && analysisResult.analysis.key_factors.length > 0">
                <h4>{{ $t('polymarket.analysis.keyFactors') }}</h4>
                <a-tag
                  v-for="(factor, idx) in analysisResult.analysis.key_factors"
                  :key="idx"
                  color="blue"
                  style="margin: 4px;"
                >
                  {{ factor }}
                </a-tag>
              </div>

              <!-- 计费信息 -->
              <div class="billing-info" v-if="analysisResult.credits_charged > 0">
                <a-alert
                  :message="$t('polymarket.analysis.creditsCharged', { credits: analysisResult.credits_charged })"
                  type="success"
                  show-icon
                  style="margin-top: 16px;"
                />
                <div v-if="analysisResult.remaining_credits !== undefined" style="margin-top: 8px; text-align: right;" :style="{ color: isDarkTheme ? 'rgba(255,255,255,0.75)' : 'rgba(0,0,0,0.65)' }">
                  {{ $t('polymarket.analysis.remainingCredits', { credits: analysisResult.remaining_credits.toFixed(0) }) }}
                </div>
              </div>
            </div>

            <!-- 操作按钮 -->
            <div class="result-actions">
              <a-button @click="handleNewAnalysis" style="margin-right: 8px;">
                {{ $t('polymarket.analysis.newAnalysis') }}
              </a-button>
              <a-button type="primary" @click="handleClose">
                {{ $t('common.close') }}
              </a-button>
            </div>
          </div>

          <!-- 加载状态 -->
          <div class="loading-section" v-if="analyzing">
            <a-spin size="large" />
            <p :style="{ marginTop: '16px', color: isDarkTheme ? 'rgba(255,255,255,0.75)' : 'rgba(0,0,0,0.65)' }">
              {{ $t('polymarket.analysis.analyzing') }}
            </p>
          </div>
        </a-tab-pane>

        <a-tab-pane key="history" :tab="$t('polymarket.analysis.tabHistory')">
          <div class="history-section">
            <a-spin :spinning="historyLoading">
              <a-table
                :columns="historyColumns"
                :data-source="historyList"
                :pagination="historyPagination"
                :row-key="record => record.id"
                @change="handleHistoryTableChange"
                size="small"
              >
                <template slot="market_title" slot-scope="text, record">
                  <a-button type="link" @click="viewHistoryItem(record)" style="padding: 0;">
                    {{ text }}
                  </a-button>
                </template>
                <template slot="recommendation" slot-scope="text">
                  <a-tag :color="getRecommendationColor(text)">
                    {{ getRecommendationLabel(text) }}
                  </a-tag>
                </template>
                <template slot="created_at" slot-scope="text">
                  {{ formatDateTime(text) }}
                </template>
              </a-table>
            </a-spin>
          </div>
        </a-tab-pane>
      </a-tabs>
    </div>
  </a-modal>
</template>

<script>
import { mapState } from 'vuex'
import { analyzePolymarketMarket, getPolymarketHistory } from '@/api/polymarket'

export default {
  name: 'PolymarketAnalysisModal',
  props: {
    visible: {
      type: Boolean,
      default: false
    }
  },
  data () {
    return {
      inputText: '',
      analyzing: false,
      analysisResult: null,
      activeTab: 'analyze',
      historyLoading: false,
      historyList: [],
      historyPagination: {
        current: 1,
        pageSize: 20,
        total: 0,
        showSizeChanger: true,
        showTotal: (total) => this.$t('polymarket.analysis.historyTotal', { total })
      },
      historyColumns: []
    }
  },
  computed: {
    ...mapState({
      navTheme: state => state.app.theme
    }),
    isDarkTheme () {
      return this.navTheme === 'dark' || this.navTheme === 'realdark'
    }
  },
  methods: {
    handleAnalyze () {
      if (!this.inputText.trim()) {
        this.$message.warning(this.$t('polymarket.analysis.inputRequired'))
        return
      }

      this.analyzing = true
      this.analysisResult = null

      analyzePolymarketMarket({
        input: this.inputText.trim(),
        language: this.$i18n.locale || 'zh-CN'
      })
        .then(res => {
          if (res.code === 1) {
            this.analysisResult = res.data
            this.$message.success(this.$t('polymarket.analysis.success'))
          } else {
            if (res.msg === 'Insufficient credits') {
              this.$message.error(this.$t('polymarket.analysis.insufficientCredits'))
              // 跳转到充值页面
              this.$router.push('/billing')
            } else {
              this.$message.error(res.msg || this.$t('polymarket.analysis.failed'))
            }
          }
        })
        .catch(err => {
          console.error('Polymarket analysis error:', err)
          if (err.response && err.response.data && err.response.data.msg === 'Insufficient credits') {
            this.$message.error(this.$t('polymarket.analysis.insufficientCredits'))
            this.$router.push('/billing')
          } else {
            this.$message.error(this.$t('polymarket.analysis.failed'))
          }
        })
        .finally(() => {
          this.analyzing = false
        })
    },
    handleNewAnalysis () {
      this.inputText = ''
      this.analysisResult = null
    },
    handleClose () {
      this.$emit('close')
      // 延迟重置，避免关闭动画时闪烁
      setTimeout(() => {
        this.inputText = ''
        this.analysisResult = null
        this.analyzing = false
      }, 300)
    },
    getStatusColor (status) {
      const colors = {
        active: 'green',
        closed: 'default',
        resolved: 'blue'
      }
      return colors[status] || 'default'
    },
    getRecommendationColor (rec) {
      const colors = {
        YES: 'green',
        NO: 'red',
        HOLD: 'orange'
      }
      return colors[rec] || 'default'
    },
    getRecommendationLabel (rec) {
      const labels = {
        YES: this.$t('polymarket.analysis.recommendationYes'),
        NO: this.$t('polymarket.analysis.recommendationNo'),
        HOLD: this.$t('polymarket.analysis.recommendationHold')
      }
      return labels[rec] || rec
    },
    getDivergenceClass (divergence) {
      if (divergence > 5) return 'divergence-positive'
      if (divergence < -5) return 'divergence-negative'
      return 'divergence-neutral'
    },
    formatVolume (volume) {
      if (volume >= 1000000) {
        return `$${(volume / 1000000).toFixed(2)}M`
      } else if (volume >= 1000) {
        return `$${(volume / 1000).toFixed(2)}K`
      }
      return `$${volume.toFixed(2)}`
    },
    handleTabChange (key) {
      this.activeTab = key
      if (key === 'history' && this.historyList.length === 0) {
        this.loadHistory()
      }
    },
    async loadHistory () {
      this.historyLoading = true
      try {
        const res = await getPolymarketHistory({
          page: this.historyPagination.current,
          page_size: this.historyPagination.pageSize
        })
        if (res.code === 1 && res.data) {
          this.historyList = res.data.items || []
          this.historyPagination.total = res.data.total || 0
        }
      } catch (err) {
        console.error('Load history error:', err)
        this.$message.error(this.$t('polymarket.analysis.loadHistoryFailed'))
      } finally {
        this.historyLoading = false
      }
    },
    handleHistoryTableChange (pagination) {
      this.historyPagination.current = pagination.current
      this.historyPagination.pageSize = pagination.pageSize
      this.loadHistory()
    },
    viewHistoryItem (record) {
      // 切换到分析标签页并显示历史记录
      this.activeTab = 'analyze'
      // 从历史记录中恢复分析结果
      this.analysisResult = {
        market: {
          question: record.market_title,
          market_id: record.market_id,
          polymarket_url: record.market_url,
          current_probability: record.market_probability,
          status: 'active'
        },
        analysis: {
          ai_predicted_probability: record.ai_predicted_probability,
          market_probability: record.market_probability,
          recommendation: record.recommendation,
          opportunity_score: record.opportunity_score,
          confidence_score: record.confidence_score
        }
      }
    },
    formatDateTime (dateStr) {
      if (!dateStr) return ''
      const date = new Date(dateStr)
      return date.toLocaleString(this.$i18n.locale === 'zh-CN' ? 'zh-CN' : 'en-US')
    }
  },
  mounted () {
    // 初始化历史记录列标题
    this.historyColumns = [
      {
        title: this.$t('polymarket.analysis.historyMarket'),
        dataIndex: 'market_title',
        key: 'market_title',
        scopedSlots: { customRender: 'market_title' }
      },
      {
        title: this.$t('polymarket.analysis.historyRecommendation'),
        dataIndex: 'recommendation',
        key: 'recommendation',
        width: 120,
        scopedSlots: { customRender: 'recommendation' }
      },
      {
        title: this.$t('polymarket.analysis.historyOpportunityScore'),
        dataIndex: 'opportunity_score',
        key: 'opportunity_score',
        width: 120,
        align: 'right'
      },
      {
        title: this.$t('polymarket.analysis.historyCreatedAt'),
        dataIndex: 'created_at',
        key: 'created_at',
        width: 180,
        scopedSlots: { customRender: 'created_at' }
      }
    ]
  }
}
</script>

<style lang="less" scoped>
.polymarket-analysis-modal {
  .input-section {
    .input-actions {
      margin-top: 12px;
    }
  }

  .result-section {
    .market-info {
      margin-bottom: 24px;
      h3 {
        margin-bottom: 12px;
        font-size: 18px;
        font-weight: 600;
      }
      .market-meta {
        display: flex;
        align-items: center;
        gap: 16px;
        flex-wrap: wrap;
        .meta-item {
          color: rgba(0, 0, 0, 0.65);
          font-size: 14px;
        }
      }
    }

    .analysis-result {
      .probability-comparison {
        display: flex;
        gap: 16px;
        margin: 24px 0;
        .prob-item {
          flex: 1;
          text-align: center;
          padding: 16px;
          background: #f5f5f5;
          border-radius: 4px;
          .prob-label {
            font-size: 12px;
            color: rgba(0, 0, 0, 0.65);
            margin-bottom: 8px;
          }
          .prob-value {
            font-size: 24px;
            font-weight: 600;
            &.ai-prob {
              color: #1890ff;
            }
            &.divergence-positive {
              color: #52c41a;
            }
            &.divergence-negative {
              color: #ff4d4f;
            }
            &.divergence-neutral {
              color: rgba(0, 0, 0, 0.65);
            }
          }
        }
      }

      .recommendation-section {
        margin: 24px 0;
        .rec-card {
          text-align: center;
          padding: 16px;
          background: #f5f5f5;
          border-radius: 4px;
          .rec-label {
            font-size: 12px;
            color: rgba(0, 0, 0, 0.65);
            margin-bottom: 8px;
          }
          .rec-value {
            font-size: 24px;
            font-weight: 600;
            color: #1890ff;
          }
        }
      }

      .reasoning-section,
      .key-factors-section {
        margin: 24px 0;
        h4 {
          margin-bottom: 12px;
          font-size: 16px;
          font-weight: 600;
        }
        .reasoning-text {
          line-height: 1.8;
          color: rgba(0, 0, 0, 0.85);
          white-space: pre-wrap;
        }
      }
    }

    .result-actions {
      margin-top: 24px;
      text-align: right;
    }
  }

  .loading-section {
    text-align: center;
    padding: 40px 0;
  }
}

.polymarket-analysis-modal.theme-dark {
    /deep/ .ant-tabs-bar {
      border-bottom-color: #2a2a2a;
    }

    /deep/ .ant-tabs-tab {
      color: #a3a3a3 !important;
    }

    /deep/ .ant-tabs-tab-active {
      color: #d4d4d4 !important;
    }

    /deep/ .ant-tabs-ink-bar {
      background: #58a6ff !important;
    }

    /deep/ .ant-divider {
      color: #d4d4d4;
      border-top-color: #2a2a2a;
    }

    /deep/ .ant-divider-inner-text {
      color: #d4d4d4 !important;
    }

    /deep/ .ant-input,
    /deep/ .ant-input-number,
    /deep/ .ant-input-affix-wrapper,
    /deep/ .ant-input-textarea textarea {
      background: #141414 !important;
      border-color: #2a2a2a !important;
      color: #d4d4d4 !important;
    }

    /deep/ .ant-table {
      background: #1c1c1c;
      color: #d4d4d4;
    }

    /deep/ .ant-table-thead > tr > th {
      background: #252525;
      color: #d4d4d4;
      border-bottom-color: #2a2a2a;
    }

    /deep/ .ant-table-tbody > tr > td {
      background: #1c1c1c;
      color: #d4d4d4;
      border-bottom-color: #2a2a2a;
    }

    /deep/ .ant-btn-link {
      color: #58a6ff !important;
    }

    /deep/ .ant-alert {
      background: #1c1c1c;
      border-color: #2a2a2a;
    }

    /deep/ .ant-alert-message {
      color: #d4d4d4 !important;
    }

    /deep/ .ant-alert-description {
      color: #a3a3a3 !important;
    }

    .market-info {
      h3 {
        color: #d4d4d4;
      }
      .market-meta {
        .meta-item {
          color: #a3a3a3;
        }
      }
    }

    .analysis-result {
      .probability-comparison {
        .prob-item {
          background: #252525;
          border: 1px solid #2a2a2a;
          .prob-label {
            color: #a3a3a3;
          }
          .prob-value {
            color: #e5e5e5;
          }
          .prob-value.divergence-neutral {
            color: #b3b3b3;
          }
        }
      }

      .recommendation-section {
        .rec-card {
          background: #252525;
          border: 1px solid #2a2a2a;
          .rec-label {
            color: #a3a3a3;
          }
          .rec-value {
            color: #e5e5e5;
          }
        }
      }

      .reasoning-section,
      .key-factors-section {
        h4 {
          color: #d4d4d4;
        }
        .reasoning-text {
          color: #d4d4d4;
        }
      }
    }
}
</style>
