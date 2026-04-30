<template>
  <div class="ai-agent-page">
    <!-- 顶部操作栏 -->
    <div class="agent-header">
      <div class="header-left">
        <RobotOutlined class="header-icon" />
        <span class="header-title">AI Agent</span>
        <a-tag :color="connected ? 'green' : 'default'" class="status-tag">
          {{ connected ? 'ONLINE' : 'OFFLINE' }}
        </a-tag>
      </div>
      <div class="header-right">
        <!-- 策略选择下拉菜单 -->
        <!-- eslint-disable-next-line vue/no-v-model-argument -->
        <a-select
          v-model:value="selectedStrategy"
          placeholder="选择策略"
          style="width: 160px"
          allow-clear
          @change="onStrategyChange"
        >
          <a-select-opt-group v-if="groupedStrategies.main.length > 0" label="分析策略">
            <a-select-option v-for="s in groupedStrategies.main" :key="s.id" :value="s.id">
              {{ s.name }}
            </a-select-option>
          </a-select-opt-group>
          <a-select-opt-group v-if="groupedStrategies.other.length > 0" label="工具">
            <a-select-option v-for="s in groupedStrategies.other" :key="s.id" :value="s.id">
              {{ s.name }}
            </a-select-option>
          </a-select-opt-group>
        </a-select>

        <!-- 股票代码快速输入 -->
        <!-- eslint-disable-next-line vue/no-v-model-argument -->
        <a-input
          v-model:value="stockCode"
          placeholder="输入股票代码 (如 000001)"
          style="width: 200px"
          allow-clear
          @pressEnter="sendAnalysis"
        >
          <template #prefix>
            <StockOutlined />
          </template>
        </a-input>
        <a-button type="primary" @click="sendAnalysis" :loading="analyzing" style="margin-left: 8px">
          <template #icon><ThunderboltOutlined /></template>
          分析
        </a-button>
        <a-dropdown style="margin-left: 8px">
          <a-button>
            <SettingOutlined />
            <DownOutlined />
          </a-button>
          <template #overlay>
            <a-menu @click="handleMenuClick">
              <a-menu-item key="clear"><DeleteOutlined /> 清空对话</a-menu-item>
              <a-menu-item key="sessions"><HistoryOutlined /> 历史会话</a-menu-item>
            </a-menu>
          </template>
        </a-dropdown>
      </div>
    </div>

    <!-- 当前策略提示条 -->
    <div v-if="currentStrategyInfo" class="strategy-banner">
      <ExperimentOutlined />
      <span>当前策略：<strong>{{ currentStrategyInfo.name }}</strong></span>
      <span v-if="currentStrategyInfo.description" class="strategy-desc">— {{ currentStrategyInfo.description }}</span>
      <CloseCircleOutlined class="strategy-clear" @click="clearStrategy" />
    </div>

    <!-- 聊天区域 -->
    <div class="chat-container" ref="chatContainerRef">
      <div class="chat-messages">
        <!-- 欢迎消息 -->
        <div v-if="messages.length === 0" class="welcome-message">
          <RobotOutlined class="welcome-icon" />
          <h2>AI Agent 助手</h2>
          <p>选择策略并输入股票代码，或直接提问：</p>
          <div class="quick-actions">
            <a-button
              v-for="action in quickActions"
              :key="action.label"
              size="small"
              @click="quickAsk(action.text)"
              class="quick-btn"
            >
              {{ action.label }}
            </a-button>
          </div>
          <!-- 策略快捷选择 -->
          <div v-if="strategies.length > 0" class="strategy-chips">
            <span class="chip-label">策略：</span>
            <a-tag
              v-for="s in strategies.slice(0, 6)"
              :key="s.id"
              :color="selectedStrategy === s.id ? 'blue' : 'default'"
              class="strategy-chip"
              @click="selectStrategy(s.id)"
            >
              {{ s.name }}
            </a-tag>
          </div>
        </div>

        <!-- 消息列表 -->
        <ChatBubble v-for="(msg, idx) in messages" :key="idx" :message="msg" />
      </div>
    </div>

    <!-- 输入区域 -->
    <div class="input-area">
      <div class="input-wrapper">
        <!-- eslint-disable-next-line vue/no-v-model-argument -->
        <a-textarea
          v-model:value="inputText"
          :placeholder="$t('ai-agent.inputPlaceholder') || '输入消息... (Enter 发送, Shift+Enter 换行)'"
          :auto-size="{ minRows: 1, maxRows: 4 }"
          @keydown.enter.exact.prevent="sendMessage"
          :disabled="streaming"
        />
        <a-button
          type="primary"
          shape="circle"
          :loading="streaming"
          :disabled="!inputText.trim() && !streaming"
          @click="sendMessage"
          class="send-btn"
        >
          <template #icon><ArrowUpOutlined /></template>
        </a-button>
      </div>
      <div class="input-hint">
        <span>流式模式</span>
        <!-- eslint-disable-next-line vue/no-v-model-argument -->
        <a-switch v-model:checked="useStream" size="small" />
      </div>
    </div>

    <!-- 任务状态抽屉 -->
    <a-drawer
      title="分析任务"
      :open="showTaskDrawer"
      @close="showTaskDrawer = false"
      placement="right"
      :width="420"
    >
      <a-spin :spinning="taskLoading">
        <a-list :data-source="taskList" size="small">
          <template #renderItem="{ item: task }">
            <a-list-item>
              <a-list-item-meta>
                <template #title>
                  <div class="task-item">
                    <span class="task-code">{{ task.stock_code }}</span>
                    <a-tag :color="taskStatusColor(task.status)">
                      {{ task.status.toUpperCase() }}
                    </a-tag>
                  </div>
                </template>
                <template #description>
                  <a-progress
                    :percent="task.progress"
                    :status="task.status === 'failed' ? 'exception' : task.status === 'completed' ? 'success' : 'active'"
                    size="small"
                  />
                </template>
              </a-list-item-meta>
            </a-list-item>
          </template>
        </a-list>
        <a-empty v-if="taskList.length === 0" description="暂无任务" />
      </a-spin>
    </a-drawer>
  </div>
</template>

<script>
import { ref, computed, nextTick, onBeforeUnmount } from 'vue'
import { message } from 'ant-design-vue'
import {
  RobotOutlined,
  StockOutlined,
  ThunderboltOutlined,
  SettingOutlined,
  DownOutlined,
  DeleteOutlined,
  HistoryOutlined,
  ExperimentOutlined,
  CloseCircleOutlined,
  ArrowUpOutlined
} from '@ant-design/icons-vue'

import ChatBubble from './components/ChatBubble.vue'
import {
  agentChat,
  triggerAnalysis,
  getAnalysisTasks,
  createAgentStream,
  createTaskStream,
  getStrategies
} from '@/api/agent'

export default {
  name: 'AiAgent',
  components: {
    ChatBubble,
    RobotOutlined,
    StockOutlined,
    ThunderboltOutlined,
    SettingOutlined,
    DownOutlined,
    DeleteOutlined,
    HistoryOutlined,
    ExperimentOutlined,
    CloseCircleOutlined,
    ArrowUpOutlined
  },
  setup () {
    // ── 响应式状态 ──────────────────────────────────────

    // 聊天
    const messages = ref([])
    const inputText = ref('')
    const streaming = ref(false)
    const useStream = ref(true)
    const connected = ref(false)
    const sessionId = ref(null)

    // 分析
    const stockCode = ref('')
    const analyzing = ref(false)

    // 策略
    const strategies = ref([])
    const selectedStrategy = ref(null)

    // 任务
    const showTaskDrawer = ref(false)
    const taskLoading = ref(false)
    const taskList = ref([])
    const taskStream = ref(null)

    // 快捷操作
    const quickActions = [
      { label: '📈 查行情', text: '帮我查一下000001的实时行情' },
      { label: '📊 看K线', text: '获取000001最近30天的K线数据' },
      { label: '🔍 做分析', text: '帮我分析一下贵州茅台的技术趋势' },
      { label: '💡 问问题', text: '什么是MACD指标？怎么用？' }
    ]

    const streamController = ref(null)

    // 模板引用
    const chatContainerRef = ref(null)

    // ── 计算属性 ────────────────────────────────────────

    const currentStrategyInfo = computed(() => {
      if (!selectedStrategy.value) return null
      return strategies.value.find((s) => s.id === selectedStrategy.value) || null
    })

    const groupedStrategies = computed(() => {
      const CATEGORY_MAP = {
        analyze_trend: 'trend',
        get_realtime_quote: 'quote',
        get_daily_history: 'quote',
        analyze_pattern: 'pattern'
      }

      const main = []
      const other = []

      for (const s of strategies.value) {
        const cat = s.category || CATEGORY_MAP[s.id] || ''
        if (['trend', 'framework', 'pattern', 'reversal'].includes(cat)) {
          main.push(s)
        } else {
          other.push(s)
        }
      }

      return { main, other }
    })

    // ── 策略 ────────────────────────────────────────────

    async function fetchStrategies () {
      try {
        const res = await getStrategies()
        strategies.value = res?.data?.strategies || res?.strategies || []
      } catch (e) {
        console.warn('加载策略列表失败:', e)
        strategies.value = []
      }
    }

    function selectStrategy (id) {
      selectedStrategy.value = id === selectedStrategy.value ? null : id
    }

    function onStrategyChange (val) {
      selectedStrategy.value = val || null
    }

    function clearStrategy () {
      selectedStrategy.value = null
    }

    // ── 聊天 ────────────────────────────────────────────

    async function sendMessage () {
      const text = inputText.value.trim()
      if (!text || streaming.value) return

      inputText.value = ''
      pushMessage('user', text)

      if (useStream.value) {
        await sendStream(text)
      } else {
        await sendNormal(text)
      }
    }

    function quickAsk (text) {
      inputText.value = text
      sendMessage()
    }

    async function sendNormal (text) {
      streaming.value = true
      pushMessage('assistant', '', { streaming: true })

      try {
        const { data } = await agentChat({
          message: text,
          session_id: sessionId.value,
          strategy_id: selectedStrategy.value || undefined,
          context: stockCode.value ? { stock_code: stockCode.value } : undefined
        })
        updateLastMessage(data.content || '无响应')
        connected.value = true
      } catch (e) {
        updateLastMessage('请求失败: ' + (e.message || '未知错误'), true)
      } finally {
        streaming.value = false
      }
    }

    async function sendStream (text) {
      streaming.value = true
      const toolEvents = []
      pushMessage('assistant', '', { streaming: true, toolEvents })

      streamController.value = createAgentStream(
        {
          message: text,
          session_id: sessionId.value,
          strategy_id: selectedStrategy.value || undefined,
          context: stockCode.value ? { stock_code: stockCode.value } : undefined
        },
        {
          onThinking: () => {
            updateLastMessage('🤔 思考中...')
          },
          onToolStart: (ev) => {
            toolEvents.push({ ...ev, status: 'loading' })
            connected.value = true
          },
          onToolDone: (ev) => {
            const item = toolEvents.find((t) => t.tool === ev.tool && t.status === 'loading')
            if (item) item.status = 'done'
          },
          onGenerating: () => {
            updateLastMessage('')
          },
          onDone: (ev) => {
            updateLastMessage(ev.content || '完成')
            streaming.value = false
          },
          onError: (ev) => {
            updateLastMessage('❌ ' + (ev.message || '流式连接错误'), true)
            streaming.value = false
          }
        }
      )
    }

    function pushMessage (role, content, extra = {}) {
      const now = new Date()
      const time = `${now.getHours().toString().padStart(2, '0')}:${now.getMinutes().toString().padStart(2, '0')}`
      messages.value.push({ role, content, time, ...extra })
      nextTick(() => scrollToBottom())
    }

    function updateLastMessage (content, isError = false) {
      const last = messages.value[messages.value.length - 1]
      if (last && last.role === 'assistant') {
        last.content = content
        last.streaming = false
        if (isError) last.error = true
      }
      nextTick(() => scrollToBottom())
    }

    function scrollToBottom () {
      const el = chatContainerRef.value
      if (el) el.scrollTop = el.scrollHeight
    }

    // ── 分析 ────────────────────────────────────────────

    async function sendAnalysis () {
      const code = stockCode.value.trim()
      if (!code) {
        message.warning('请输入股票代码')
        return
      }

      analyzing.value = true
      const strategyNote = selectedStrategy.value
        ? ` (策略: ${currentStrategyInfo.value?.name || selectedStrategy.value})`
        : ''
      pushMessage('user', `分析股票 ${code}${strategyNote}`)

      try {
        const { data } = await triggerAnalysis({
          stock_code: code,
          strategy_id: selectedStrategy.value || undefined,
          async_mode: false
        })

        if (data.code === 1) {
          const report = data.data || {}
          let text = `**${code} 分析结果**\n`
          if (selectedStrategy.value) {
            text += `策略: ${currentStrategyInfo.value?.name}\n`
          }
          if (report.ticker) {
            const r = report.ticker
            text += `最新价: ${r.last || '-'}\n`
            text += `涨跌幅: ${r.changePercent || '-'}%\n`
          }
          if (report.kline_summary) {
            text += `K线数据: ${report.kline_summary.count} 条\n`
          }
          pushMessage('assistant', text)
        } else {
          pushMessage('assistant', `分析失败: ${data.msg || '未知错误'}`, { error: true })
        }
      } catch (e) {
        pushMessage('assistant', `分析请求异常: ${e.message}`, { error: true })
      } finally {
        analyzing.value = false
      }
    }

    // ── 任务流 ──────────────────────────────────────────

    function initTaskStream () {
      taskStream.value = createTaskStream({
        onConnected: () => {},
        onTaskCreated: (task) => {
          taskList.value.unshift(task)
        },
        onTaskProgress: (data) => {
          const task = taskList.value.find((t) => t.task_id === data.task_id)
          if (task) {
            task.progress = data.progress
            task.status = data.status
          }
        },
        onTaskCompleted: (task) => {
          const idx = taskList.value.findIndex((t) => t.task_id === task.task_id)
          if (idx >= 0) taskList.value[idx] = task
        }
      })
    }

    function taskStatusColor (status) {
      const map = { pending: 'orange', processing: 'blue', completed: 'green', failed: 'red' }
      return map[status] || 'default'
    }

    // ── 其它 ────────────────────────────────────────────

    function handleMenuClick ({ key }) {
      if (key === 'clear') {
        messages.value = []
      } else if (key === 'sessions') {
        showTaskDrawer.value = true
        loadTasks()
      }
    }

    async function loadTasks () {
      taskLoading.value = true
      try {
        const { data } = await getAnalysisTasks({ limit: 20 })
        taskList.value = data.tasks || []
      } catch (e) {
        message.error('加载任务失败')
      } finally {
        taskLoading.value = false
      }
    }

    // ── 生命周期 ────────────────────────────────────────

    sessionId.value = 'session_' + (crypto.randomUUID?.() || Date.now().toString(36) + Math.random().toString(36).slice(2))
    initTaskStream()
    fetchStrategies()

    onBeforeUnmount(() => {
      streamController.value?.close()
      taskStream.value?.close()
    })

    return {
      messages,
      inputText,
      streaming,
      useStream,
      connected,
      stockCode,
      analyzing,
      strategies,
      selectedStrategy,
      showTaskDrawer,
      taskLoading,
      taskList,
      quickActions,
      chatContainerRef,
      currentStrategyInfo,
      groupedStrategies,
      selectStrategy,
      onStrategyChange,
      clearStrategy,
      sendMessage,
      quickAsk,
      sendAnalysis,
      taskStatusColor,
      handleMenuClick
    }
  }
}
</script>

<style scoped lang="less">
.ai-agent-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 120px);
  background: #fafafa;
}

.agent-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 24px;
  background: #fff;
  border-bottom: 1px solid #f0f0f0;
  flex-shrink: 0;

  .header-left {
    display: flex;
    align-items: center;
    gap: 10px;

    .header-icon {
      font-size: 24px;
      color: #1890ff;
    }

    .header-title {
      font-size: 18px;
      font-weight: 600;
      color: #333;
    }
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 8px;
  }
}

.strategy-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 24px;
  background: #e6f7ff;
  border-bottom: 1px solid #91d5ff;
  font-size: 13px;
  color: #0050b3;
  flex-shrink: 0;

  .strategy-desc {
    color: #69c0ff;
    font-size: 12px;
  }

  .strategy-clear {
    margin-left: auto;
    cursor: pointer;
    color: #69c0ff;
    &:hover {
      color: #ff4d4f;
    }
  }
}

.chat-container {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  scroll-behavior: smooth;
}

.chat-messages {
  max-width: 99%;
  margin: 0 auto;
}

.welcome-message {
  text-align: center;
  padding: 60px 20px;

  .welcome-icon {
    font-size: 48px;
    color: #1890ff;
    margin-bottom: 16px;
  }

  h2 {
    font-size: 24px;
    color: #333;
    margin-bottom: 8px;
  }

  p {
    color: #666;
    margin-bottom: 24px;
  }

  .quick-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 8px;
    margin-bottom: 20px;

    .quick-btn {
      border-radius: 16px;
      font-size: 13px;
    }
  }

  .strategy-chips {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    align-items: center;
    gap: 6px;
    margin-top: 8px;

    .chip-label {
      font-size: 12px;
      color: #999;
    }

    .strategy-chip {
      cursor: pointer;
      font-size: 12px;
      transition: all 0.2s;
      &:hover {
        opacity: 0.8;
      }
    }
  }
}

.input-area {
  flex-shrink: 0;
  padding: 12px 24px 16px;
  background: #fff;
  border-top: 1px solid #f0f0f0;

  .input-wrapper {
    max-width: 900px;
    margin: 0 auto;
    display: flex;
    gap: 8px;
    align-items: flex-end;

    :deep(.ant-input) {
      border-radius: 20px;
      padding: 8px 16px;
      resize: none;
    }

    .send-btn {
      flex-shrink: 0;
      width: 36px;
      height: 36px;
    }
  }

  .input-hint {
    max-width: 900px;
    margin: 6px auto 0;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 6px;
    font-size: 12px;
    color: #999;
  }
}

.task-item {
  display: flex;
  align-items: center;
  gap: 8px;

  .task-code {
    font-weight: 600;
  }
}

// scrollbar
.chat-container::-webkit-scrollbar {
  width: 6px;
}
.chat-container::-webkit-scrollbar-thumb {
  background: #d9d9d9;
  border-radius: 3px;
}
</style>
