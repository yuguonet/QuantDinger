<template>
  <div class="ai-asset-analysis-page" :class="{ 'theme-dark': isDarkTheme }">

    <!-- ======== Floating Quick Trade Button (only show when analyzing a Crypto symbol) ======== -->
    <a-tooltip :title="$t('quickTrade.openPanel')" placement="left">
      <div class="qt-floating-btn" @click="openQuickTradeFromCurrent" v-if="!showQuickTrade && currentAnalysisSymbol">
        <a-icon type="thunderbolt" theme="filled" />
      </div>
    </a-tooltip>

    <!-- ======== Quick Trade Panel ======== -->
    <quick-trade-panel
      :visible="showQuickTrade"
      :symbol="qtSymbol"
      :preset-side="qtSide"
      :preset-price="qtPrice"
      :source="qtSource"
      market-type="swap"
      @close="showQuickTrade = false"
      @order-success="onQuickTradeSuccess"
      @update:symbol="handleQuickTradeSymbolChange"
    />

    <!-- ======== Main Workspace Card with Tabs ======== -->
    <a-card :bordered="false" class="workspace-card">
      <a-tabs v-model="activeTab" class="workspace-tabs" size="large">
        <a-tab-pane key="quick">
          <span slot="tab">
            <a-icon type="thunderbolt" />
            {{ $t('aiAssetAnalysis.tabs.quick') }}
          </span>
          <div class="tab-body">
            <AnalysisView
              v-if="activeTab === 'quick'"
              :embedded="true"
              :preset-symbol="presetSymbol"
              :auto-analyze-signal="autoAnalyzeSignal"
              @symbol-change="onAnalysisSymbolChange"
            />
          </div>
        </a-tab-pane>
        <a-tab-pane key="polymarket">
          <span slot="tab">
            <a-icon type="radar-chart" />
            {{ $t('aiAssetAnalysis.tabs.polymarket') }}
          </span>
          <div class="tab-body">
            <div class="polymarket-tab-content">
              <div class="polymarket-placeholder">
                <div class="placeholder-icon"><a-icon type="radar-chart" /></div>
                <h3>{{ $t('polymarket.analysis.title') }}</h3>
                <p>{{ $t('polymarket.analysis.description') }}</p>
                <a-button
                  type="primary"
                  size="large"
                  icon="thunderbolt"
                  @click="showPolymarketModal = true"
                  style="margin-top: 16px;"
                >
                  {{ $t('polymarket.analysis.startAnalysis') }}
                </a-button>
              </div>
            </div>
          </div>
        </a-tab-pane>
      </a-tabs>
    </a-card>

    <!-- Polymarket分析对话框 -->
    <PolymarketAnalysisModal
      :visible="showPolymarketModal"
      @close="showPolymarketModal = false"
    />

  </div>
</template>

<script>
import { mapState } from 'vuex'
import AnalysisView from '@/views/ai-analysis'
import QuickTradePanel from '@/components/QuickTradePanel/QuickTradePanel'
import PolymarketAnalysisModal from '@/components/PolymarketAnalysisModal'

export default {
  name: 'AIAssetAnalysis',
  components: {
    AnalysisView,
    QuickTradePanel,
    PolymarketAnalysisModal
  },
  data () {
    return {
      activeTab: 'quick',
      // Props passed to AnalysisView
      presetSymbol: '',
      autoAnalyzeSignal: 0,
      // Quick Trade Panel
      showQuickTrade: false,
      qtSymbol: '',
      qtSide: '',
      qtPrice: 0,
      qtSource: 'ai_radar',
      // Current analysis symbol (from AnalysisView)
      currentAnalysisSymbol: '',
      currentAnalysisMarket: '',
      // Polymarket Analysis Modal
      showPolymarketModal: false
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
    onAnalysisSymbolChange (value) {
      if (!value) {
        this.currentAnalysisSymbol = ''
        this.currentAnalysisMarket = ''
        return
      }
      const parts = value.split(':')
      const market = parts.length > 1 ? parts[0] : 'Crypto'
      const symbol = parts.length > 1 ? parts[1] : parts[0]
      this.currentAnalysisMarket = market
      if (market === 'Crypto') {
        this.currentAnalysisSymbol = symbol
      } else {
        this.currentAnalysisSymbol = ''
      }
    },
    openQuickTradeFromCurrent () {
      if (!this.currentAnalysisSymbol) return
      this.qtSymbol = this.currentAnalysisSymbol
      this.qtSide = ''
      this.qtPrice = 0
      this.qtSource = 'ai_analysis'
      this.showQuickTrade = true
    },
    onQuickTradeSuccess () {
      this.$message.success(this.$t('quickTrade.orderSuccess'))
    },
    handleQuickTradeSymbolChange (newSymbol) {
      if (newSymbol) {
        this.qtSymbol = newSymbol
      }
    }
  }
}
</script>

<style lang="less" scoped>
.ai-asset-analysis-page {
  padding: 20px;
  min-height: calc(100vh - 120px);
  background: #f0f2f5;
  width: 100%;
  max-width: 100%;
  box-sizing: border-box;
  overflow-x: hidden;

  /* ===== Floating QT Button ===== */
  .qt-floating-btn {
    position: fixed;
    right: 24px;
    bottom: 80px;
    width: 48px;
    height: 48px;
    border-radius: 14px;
    background: linear-gradient(135deg, #6366f1, #8b5cf6);
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 22px;
    cursor: pointer;
    box-shadow: 0 4px 20px rgba(99, 102, 241, 0.35);
    z-index: 1000;
    transition: all 0.3s;
    animation: qt-float-pulse 2.5s ease-in-out infinite;
    &:hover {
      transform: scale(1.08);
      box-shadow: 0 6px 28px rgba(99, 102, 241, 0.5);
    }
  }
  @keyframes qt-float-pulse {
    0%, 100% { transform: translateY(0); }
    50% { transform: translateY(-4px); }
  }

  /* ===== Workspace Card ===== */
  .workspace-card {
    border-radius: 14px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
    border: 1px solid #e8e8e8;

    ::v-deep .ant-card-body { padding: 0; }

    .workspace-tabs {
      ::v-deep .ant-tabs-bar {
        margin-bottom: 0;
        padding: 0 20px;
        border-bottom: 1px solid #f0f0f0;
      }
      ::v-deep .ant-tabs-tab {
        font-size: 15px;
        font-weight: 600;
        padding: 14px 16px;
      }
    }

    .tab-body {
      ::v-deep .ai-analysis-container.embedded,
      ::v-deep .portfolio-container.embedded {
        border-radius: 0;
        overflow: hidden;
      }

      .polymarket-tab-content {
        padding: 40px 20px;
        text-align: center;

        .polymarket-placeholder {
          .placeholder-icon { font-size: 64px; color: #6366f1; margin-bottom: 24px; }
          h3 { font-size: 20px; font-weight: 700; margin-bottom: 12px; color: rgba(0, 0, 0, 0.85); }
          p  { font-size: 14px; color: rgba(0, 0, 0, 0.55); margin-bottom: 24px; }
        }
      }
    }
  }

  /* ===== Dark Theme ===== */
  &.theme-dark {
    background: #141414;

    .workspace-card {
      background: #1c1c1c;
      border-color: #2a2a2a;

      .workspace-tabs {
        ::v-deep .ant-tabs-bar { border-bottom-color: #2a2a2a; }
        ::v-deep .ant-tabs-tab { color: #8b949e; &:hover { color: #c9d1d9; } }
        ::v-deep .ant-tabs-tab-active { color: #a78bfa; }
        ::v-deep .ant-tabs-ink-bar { background-color: #a78bfa; }
      }

      .polymarket-tab-content .polymarket-placeholder {
        h3 { color: #d4d4d4; }
        p  { color: #a3a3a3; }
      }
    }
  }
}

/* ========== 移动端自适应 ========== */
@media (max-width: 768px) {
  .ai-asset-analysis-page {
    padding: 8px;
    min-height: auto;

    .qt-floating-btn {
      right: ~"max(8px, env(safe-area-inset-right, 0px))";
      bottom: ~"max(68px, calc(52px + env(safe-area-inset-bottom, 0px)))";
      width: 44px;
      height: 44px;
      font-size: 20px;
    }

    .workspace-card {
      border-radius: 10px;

      .workspace-tabs {
        ::v-deep .ant-tabs-bar {
          padding: 0 6px;
        }

        ::v-deep .ant-tabs-nav-scroll {
          overflow-x: auto;
          -webkit-overflow-scrolling: touch;
        }

        ::v-deep .ant-tabs-tab {
          font-size: 14px;
          padding: 10px 8px;
          margin-right: 2px;
          white-space: nowrap;
        }
      }

      .tab-body {
        .polymarket-tab-content {
          padding: 16px 8px;

          .polymarket-placeholder {
            .placeholder-icon { font-size: 48px; margin-bottom: 12px; }
            h3 { font-size: 17px; }
            p { font-size: 13px; }
          }
        }
      }
    }
  }
}

@media (max-width: 480px) {
  .ai-asset-analysis-page {
    padding: 4px;

    .workspace-card {
      .workspace-tabs {
        ::v-deep .ant-tabs-bar {
          padding: 0 4px;
        }
        ::v-deep .ant-tabs-tab {
          font-size: 13px;
          padding: 8px 6px;
        }
      }
      .tab-body .polymarket-tab-content {
        padding: 12px 6px;
      }
    }
  }
}
</style>
