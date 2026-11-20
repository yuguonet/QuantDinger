<template>
  <div class="ai-agent-page" ref="pageRef" :style="pageStyle">
    <!-- 顶部操作栏 -->
    <div class="agent-header">
      <div class="header-left">
        <a-icon type="robot" class="header-icon" />
        <span class="header-title">AI Agent</span>
        <a-tag :color="connected ? 'green' : 'default'" class="status-tag">
          {{ connected ? 'ONLINE' : 'OFFLINE' }}
        </a-tag>
      </div>
      <div class="header-right">
        <!-- 指标策略选择下拉（与指标IDE共用） -->
        <!-- eslint-disable-next-line vue/no-v-model-argument -->
        <a-select
          v-model:value="selectedStrategy"
          placeholder="选择指标策略"
          style="width: 200px"
          allow-clear
          show-search
          option-filter-prop="children"
          :loading="loadingIndicators"
          @change="onStrategyChange"
        >
          <a-select-option
            v-for="ind in indicators"
            :key="ind.id"
            :value="ind.id"
          >
            <span>{{ ind.name || ('Indicator #' + ind.id) }}</span>
            <a-tag
              v-if="Number(ind.is_buy) === 1"
              color="purple"
              style="margin-left: 6px; font-size: 10px; line-height: 16px; padding: 0 4px;"
            >已购</a-tag>
          </a-select-option>
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
            <a-icon type="stock" />
          </template>
        </a-input>
        <a-button type="primary" @click="sendAnalysis" :loading="analyzing" style="margin-left: 8px">
          <template #icon><a-icon type="thunderbolt" /></template>
          分析
        </a-button>
        <a-dropdown style="margin-left: 8px">
          <a-button>
            <a-icon type="setting" />
            <a-icon type="down" />
          </a-button>
          <template #overlay>
            <a-menu @click="handleMenuClick">
              <a-menu-item key="clear"><a-icon type="delete" /> 清空对话</a-menu-item>
              <a-menu-item key="sessions"><a-icon type="history" /> 历史会话</a-menu-item>
            </a-menu>
          </template>
        </a-dropdown>
      </div>
    </div>

    <!-- 当前策略提示条 -->
    <div v-if="currentStrategyInfo" class="strategy-banner">
      <a-icon type="experiment" />
      <span>当前策略：<strong>{{ currentStrategyInfo.name }}</strong></span>
      <span v-if="currentStrategyInfo.description" class="strategy-desc">— {{ currentStrategyInfo.description }}</span>
      <a-icon type="close-circle" class="strategy-clear" @click="clearStrategy" />
    </div>

    <!-- 聊天区域 -->
    <div class="chat-container" ref="chatContainerRef">
      <div class="chat-messages">
        <!-- 欢迎消息 -->
        <div v-if="messages.length === 0" class="welcome-message">
          <a-icon type="robot" class="welcome-icon" />
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
          <div v-if="indicators.length > 0" class="strategy-chips">
            <span class="chip-label">策略：</span>
            <a-tag
              v-for="s in indicators.slice(0, 6)"
              :key="s.id"
              :color="selectedStrategy === s.id ? 'blue' : 'default'"
              class="strategy-chip"
              @click="selectStrategy(s.id)"
            >
              {{ s.name }}
            </a-tag>
          </div>
        </div>

        <!-- 消息列表（按天分组：跨天插入日期分隔条） -->
        <template v-for="item in timeline">
          <div v-if="item.type === 'divider'" :key="item.key" class="date-divider">
            <span class="date-chip">{{ item.label }}</span>
          </div>
          <ChatBubble v-else :key="item.key" :message="item.msg" />
        </template>
      </div>
    </div>

    <!-- 输入区域 -->
    <div class="input-area">
      <div class="input-wrapper">
        <!-- eslint-disable-next-line vue/no-v-model-argument -->
        <a-textarea
          v-model:value="inputText"
          :placeholder="$t('ai-agent.inputPlaceholder') || '输入消息…'"
          :auto-size="{ minRows: 1, maxRows: 6 }"
          @keydown="onInputKeydown"
          :disabled="streaming"
        />
        <!-- 发送 / 停止 二合一按钮 -->
        <a-button
          v-if="!streaming"
          type="primary"
          shape="circle"
          :disabled="!inputText.trim()"
          @click="sendMessage"
          class="send-btn"
        >
          <template #icon><a-icon type="arrow-up" /></template>
        </a-button>
        <a-button
          v-else
          type="danger"
          shape="circle"
          @click="stopStream"
          class="send-btn stop-btn"
        >
          <template #icon><a-icon type="stop" /></template>
        </a-button>
      </div>
      <div class="input-hint">
        <span><kbd>Enter</kbd> 发送</span>
        <span class="hint-sep">·</span>
        <span><kbd>Shift</kbd> + <kbd>Enter</kbd> 换行</span>
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
import { ref, computed, nextTick, onBeforeUnmount, onMounted, watch, reactive } from 'vue'
import { message } from 'ant-design-vue'
// icons: using <a-icon type="xxx" /> (ant-design-vue 1.x pattern)

import ChatBubble from './components/ChatBubble.vue'
import {
  triggerAnalysis,
  getAnalysisTasks,
  createAgentStream,
  createTaskStream,
  deleteChatSession
} from '@/api/agent'
import request from '@/utils/request'
import { readUserPrefs, writeUserPrefs } from '@/utils/uiPrefs'

// ── 模块级流式连接（跨组件生命周期存活）────────────────
let _activeStream = null          // 当前活跃的流式连接 { close() }
let _pendingToolEvents = []       // 流式进行中的工具事件（累积）
let _streamActive = false         // 后端是否正在流式输出
let _currentCallbacks = {}        // 当前组件注册的回调（可热替换）

function _detachComponent () {
  // 组件卸载时：清空回调，但连接继续跑
  _currentCallbacks = {}
}

function _reattachComponent (callbacks) {
  // 组件重新挂载：注册新回调
  _currentCallbacks = callbacks
}

export default {
  name: 'AiAgent',
  components: {
    ChatBubble
  },
  setup () {
    // ── 响应式状态 ──────────────────────────────────────

    // 聊天
    const messages = ref([])
    const inputText = ref('')
    const streaming = ref(false)
    const connected = ref(false)
    const sessionId = ref(null)

    // 分析
    const stockCode = ref('')
    const analyzing = ref(false)

    // 策略（复用指标IDE的指标列表）
    const indicators = ref([])
    const loadingIndicators = ref(false)
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

    // 模板引用
    const chatContainerRef = ref(null)
    const pageRef = ref(null)

    // ── 页面铺满窗口 ────────────────────────────────────
    // 布局层（pro-layout）的 header / 外边距会随主题与屏宽变化，写死 100vh-Npx 一定会留白或溢出。
    // 这里直接实测页面元素距视口顶部的偏移，用剩余高度作为页面高度，随窗口尺寸同步。
    const pageHeight = ref(0)
    const pageStyle = computed(() => (
      pageHeight.value > 0 ? { height: pageHeight.value + 'px' } : { height: 'calc(100vh - 120px)' }
    ))

    function syncPageHeight () {
      const el = pageRef.value
      if (!el) return
      const top = el.getBoundingClientRect().top
      if (!Number.isFinite(top) || top < 0) return
      pageHeight.value = Math.max(360, Math.round(window.innerHeight - top))
    }

    // ── 计算属性 ────────────────────────────────────────

    const currentStrategyInfo = computed(() => {
      if (!selectedStrategy.value) return null
      return indicators.value.find((s) => s.id === selectedStrategy.value) || null
    })

    // ── 会话时间轴（跨天插入日期分隔条）──────────────────
    // 旧版本消息只存了 HH:mm（无 ts），这类消息不产生日期分隔，仅显示原时间。
    const pad2 = (n) => String(n).padStart(2, '0')
    const dayKeyOf = (ts) => {
      const d = new Date(ts)
      return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
    }
    function dayLabelOf (ts) {
      const d = new Date(ts)
      const now = new Date()
      const key = dayKeyOf(ts)
      if (key === dayKeyOf(now.getTime())) return '今天'
      if (key === dayKeyOf(now.getTime() - 86400000)) return '昨天'
      if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日`
      return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`
    }

    const timeline = computed(() => {
      const out = []
      let lastDay = ''
      messages.value.forEach((msg, idx) => {
        const day = msg.ts ? dayKeyOf(msg.ts) : ''
        if (day && day !== lastDay) {
          out.push({ type: 'divider', key: 'd-' + day + '-' + idx, label: dayLabelOf(msg.ts) })
          lastDay = day
        }
        out.push({ type: 'msg', key: 'm-' + idx, msg })
      })
      return out
    })

    // ── 消息持久化（账号私有域: 严格按用户隔离, 未登录不落盘）────────────

    /** 持久化消息条数上限: 防长会话把 localStorage 撑爆(超配额后写入静默丢=不长效) */
    const MAX_PERSISTED_MESSAGES = 200

    function saveMessages () {
      try {
        // 只截断持久化副本, 不动内存中的完整会话
        const list = messages.value.length > MAX_PERSISTED_MESSAGES
          ? messages.value.slice(-MAX_PERSISTED_MESSAGES)
          : messages.value
        writeUserPrefs('ai-agent', { messages: list, sessionId: sessionId.value })
      } catch (_) {}
    }

    /**
     * 历史遗留消息迁移：老版本只持久化 HH:mm（无 ts），数据里确实没有日期信息。
     * 按会话内时间单调性反推补全：从最新一条往前遍历，若更早一条的 HH:mm 大于
     * 后一条的 HH:mm（同一天不可能倒序），说明它落在前一天，日偏移 +1。
     * 已有 ts 的消息以其真实日期为锚，其后更早的无 ts 消息继续按此规则推算。
     * 补全结果随下一次 saveMessages 落盘，因此只在首次升级时计算一次。
     */
    function backfillLegacyTimestamps (list) {
      const DAY = 86400000
      const now = new Date()
      const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
      const minutesOf = (s) => {
        const m = /^(\d{1,2}):(\d{2})$/.exec(String(s || ''))
        return m ? Number(m[1]) * 60 + Number(m[2]) : null
      }
      let dayOffset = 0
      let prevMinutes = null
      let dirty = false
      for (let i = list.length - 1; i >= 0; i--) {
        const msg = list[i]
        if (!msg || typeof msg !== 'object') continue
        if (msg.ts) {
          const d = new Date(msg.ts)
          if (Number.isFinite(d.getTime())) {
            const dayStart = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
            dayOffset = Math.round((todayStart - dayStart) / DAY)
            prevMinutes = d.getHours() * 60 + d.getMinutes()
          }
          continue
        }
        const mins = minutesOf(msg.time)
        if (mins == null) continue
        if (prevMinutes != null && mins > prevMinutes) dayOffset += 1
        msg.ts = todayStart - dayOffset * DAY + mins * 60000
        prevMinutes = mins
        dirty = true
      }
      return dirty
    }

    function restoreMessages () {
      try {
        const saved = readUserPrefs('ai-agent')
        if (Array.isArray(saved.messages) && saved.messages.length > 0) {
          // 恢复时清掉 streaming 标记：上次若在流式输出中关闭页面，否则会一直显示光标/打字动画
          messages.value = saved.messages.map((m) => (m && m.streaming ? { ...m, streaming: false } : m))
          const backfilled = backfillLegacyTimestamps(messages.value)
          if (backfilled) nextTick(saveMessages)
        }
        if (saved.sessionId) {
          sessionId.value = saved.sessionId
        }
      } catch (_) {}
    }

    // ── 指标策略（与指标IDE共用）────────────────────────

    async function loadIndicators () {
      loadingIndicators.value = true
      try {
        const res = await request({ url: '/api/indicator/getIndicators', method: 'get' })
        if (res && res.data && Array.isArray(res.data)) {
          indicators.value = res.data
        }
      } catch (e) {
        console.warn('加载指标列表失败:', e)
        indicators.value = []
      } finally {
        loadingIndicators.value = false
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
      await sendStream(text)
    }

    /**
     * 输入框回车策略：
     * - Enter             → 发送
     * - Shift/Ctrl/Alt+Enter → 原生换行（不拦截，交给 textarea 自己插入 \n）
     * - 输入法组合中（中文候选确认）的 Enter → 必须忽略，否则会打断拼音输入
     */
    function onInputKeydown (e) {
      if (e.isComposing || e.keyCode === 229) return
      if (e.key !== 'Enter' && e.keyCode !== 13) return
      if (e.shiftKey || e.ctrlKey || e.altKey || e.metaKey) return
      e.preventDefault()
      sendMessage()
    }

    function quickAsk (text) {
      inputText.value = text
      sendMessage()
    }

    async function sendStream (text) {
      streaming.value = true
      _streamActive = true
      const toolEvents = reactive([]) // reactive 包装：push/字段变更都能被模板响应（原闭包数组 Vue 检测不到）
      _pendingToolEvents = toolEvents
      pushMessage('assistant', '', { streaming: true, toolEvents, progressSteps: [], stepContents: [] })

      // 注册模块级回调（通过 _currentCallbacks 代理，组件卸载也不中断）
      const makeCb = (name) => (...args) => _currentCallbacks[name]?.(...args)

      _activeStream = createAgentStream(
        {
          message: text,
          session_id: sessionId.value,
          strategy_id: selectedStrategy.value || undefined,
          context: stockCode.value ? { stock_code: stockCode.value } : undefined
        },
        {
          onNodeStart: makeCb('onNodeStart'),
          onNodeDone: makeCb('onNodeDone'),
          onProgress: makeCb('onProgress'),
          onStepContent: makeCb('onStepContent'),
          onToolStart: makeCb('onToolStart'),
          onToolDone: makeCb('onToolDone'),
          onDone: makeCb('onDone'),
          onError: makeCb('onError')
        }
      )

      // 绑定实际回调
      _currentCallbacks = {
        onNodeStart: (ev) => {
          // 节点生命周期进入进度列表（2026-09-11 SSE 过程事件）
          const last = messages.value[messages.value.length - 1]
          if (last) {
            if (!last.progressSteps) last.progressSteps = []
            const labels = { chat: '解析意图', plan: '规划任务', execute: '执行工具', finalize: '整理结果' }
            const label = labels[ev.node] || (ev.node || '处理中')
            last.progressSteps.push('▶ ' + label + '…')
            nextTick(() => scrollToBottom())
          }
        },
        onNodeDone: (ev) => {
          // 节点完成：对应进度行打勾（与 onNodeStart 的 ▶ 行一一对应）
          const last = messages.value[messages.value.length - 1]
          if (last && last.progressSteps && last.progressSteps.length) {
            const labels = { chat: '解析意图', plan: '规划任务', execute: '执行工具', finalize: '整理结果' }
            const label = labels[ev.node] || ev.node || '完成'
            const idx = last.progressSteps.findIndex((s) => typeof s === 'string' && s.indexOf('▶ ') === 0 && s.indexOf(label) > 0)
            if (idx >= 0) last.progressSteps.splice(idx, 1, '✓ ' + label)
            else last.progressSteps.push('✓ ' + label)
            nextTick(() => scrollToBottom())
          }
        },
        onProgress: (ev) => {
          // 工具规划进度
          const last = messages.value[messages.value.length - 1]
          if (last && ev.message) {
            if (!last.progressSteps) last.progressSteps = []
            last.progressSteps.push(ev.message)
          }
          nextTick(() => scrollToBottom())
        },
        onStepContent: (ev) => {
          // 步骤输出内容
          const last = messages.value[messages.value.length - 1]
          if (last && ev.content) {
            if (!last.stepContents) last.stepContents = []
            last.stepContents.push(ev.content)
            updateLastMessage(last.stepContents.join('\n\n'))
          }
        },
        onToolStart: (ev) => {
          toolEvents.push({ ...ev, status: 'loading', streamOutput: '', recovery: ev.recovery || '' })
          connected.value = true
        },
        onToolDone: (ev) => {
          const item = toolEvents.find((t) => t.tool === ev.tool && t.status === 'loading')
          if (item) {
            item.status = ev.success === false ? 'error' : 'done'
            if (ev.recovery) item.recovery = ev.recovery
          }
        },
        onDone: (ev) => {
          // 处理图表数据
          if (ev.charts && ev.charts.length > 0) {
            const last = messages.value[messages.value.length - 1]
            if (last) {
              if (!last.charts) last.charts = []
              ev.charts.forEach(b64 => {
                try { last.charts.push(atob(b64)) } catch (e) { /* ignore */ }
              })
            }
          }
          // 最终结果写入 finalContent，并更新显示
          const last = messages.value[messages.value.length - 1]
          if (last && last.role === 'assistant') {
            last.finalContent = ev.content || ''
            last.streaming = false
            // 如果没有 step_content 事件（如闲聊快速通道），用 finalContent 填充 content
            if (!last.content && ev.content) {
              last.content = ev.content
            }
          }
          streaming.value = false
          _streamActive = false
          _activeStream = null
          _pendingToolEvents = []
          saveMessages()
        },
        onError: (ev) => {
          updateLastMessage('❌ ' + (ev.message || '连接错误'), true)
          streaming.value = false
          _streamActive = false
          _activeStream = null
          _pendingToolEvents = []
          saveMessages()
        }
      }
    }

    function stopStream () {
      if (_activeStream) {
        _activeStream.close()
        _activeStream = null
      }
      _streamActive = false
      _pendingToolEvents = []
      streaming.value = false
      if (sessionId.value) {
        request({ url: `/api/agent/interrupt/${sessionId.value}`, method: 'post' }).catch(() => {})
      }
      updateLastMessage('⏹ 已中断', false)
    }

    function pushMessage (role, content, extra = {}) {
      const now = new Date()
      const time = `${now.getHours().toString().padStart(2, '0')}:${now.getMinutes().toString().padStart(2, '0')}`
      // ts 用于历史会话按天分组显示日期（旧消息无 ts，只显示 time）
      messages.value.push({ role, content, time, ts: now.getTime(), ...extra })
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

    /**
     * 滚到底部。
     * instant=true 时临时关闭 CSS smooth（scroll-behavior 对 scrollTop 赋值同样生效），
     * 否则首次进入历史会话会从顶部"滑"到底，观感很怪。
     */
    function scrollToBottom (instant = false) {
      const el = chatContainerRef.value
      if (!el) return
      if (!instant) {
        el.scrollTop = el.scrollHeight
        return
      }
      const prev = el.style.scrollBehavior
      el.style.scrollBehavior = 'auto'
      el.scrollTop = el.scrollHeight
      el.style.scrollBehavior = prev
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

    async function handleMenuClick ({ key }) {
      if (key === 'clear') {
        // 1. 调后端清除 session 上下文（对话历史 + 工具结果 + 意图路由上下文）
        const oldSessionId = sessionId.value
        try {
          await deleteChatSession(oldSessionId)
        } catch (_) {}
        // 2. 生成新 session_id（避免残留的 ContextManager 状态影响新对话）
        sessionId.value = 'session_' + (crypto.randomUUID?.() || Date.now().toString(36) + Math.random().toString(36).slice(2))
        // 3. 清本地
        messages.value = []
        try {
          writeUserPrefs('ai-agent', { messages: [], sessionId: sessionId.value })
        } catch (_) {}
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

    sessionId.value = readUserPrefs('ai-agent').sessionId ||
      'session_' + (crypto.randomUUID?.() || Date.now().toString(36) + Math.random().toString(36).slice(2))
    initTaskStream()
    loadIndicators()
    restoreMessages()

    // 消息变化时自动保存
    watch(messages, saveMessages, { deep: true })

    // 如果有正在进行的流式输出，续接回调
    if (_streamActive) {
      streaming.value = true
      // 找到最后一条 assistant 消息的 toolEvents
      const lastMsg = messages.value[messages.value.length - 1]
      const toolEvents = (lastMsg && lastMsg.role === 'assistant' && lastMsg.toolEvents) || _pendingToolEvents || []
      _reattachComponent({
        onThinking: () => {
          updateLastMessage('🤔 思考中...')
        },
        onToolStart: (ev) => {
          toolEvents.push({ ...ev, status: 'loading', streamOutput: '' })
          connected.value = true
        },
        onToolDone: (ev) => {
          const item = toolEvents.find((t) => t.tool === ev.tool && t.status === 'loading')
          if (item) {
            item.status = ev.success === false ? 'error' : 'done'
            if (ev.recovery) item.recovery = ev.recovery
          }
        },
        onToolStream: (ev) => {
          const item = toolEvents.find((t) => t.tool === ev.tool && t.status === 'loading')
          if (item) {
            item.streamOutput = (item.streamOutput || '') + (ev.output || '')
            nextTick(() => scrollToBottom())
          }
        },
        onToolInfo: (ev) => {
          const item = toolEvents.find((t) => t.tool === ev.tool)
          if (item) item.info = ev.message
        },
        onChart: (ev) => {
          const last = messages.value[messages.value.length - 1]
          if (last) {
            if (!last.charts) last.charts = []
            try { last.charts.push(atob(ev.b64)) } catch (e) { /* ignore */ }
          }
        },
        onGenerating: () => {
          updateLastMessage('')
        },
        onDone: (ev) => {
          updateLastMessage(ev.content || '完成')
          streaming.value = false
          _streamActive = false
          _activeStream = null
          _pendingToolEvents = []
          saveMessages()
        },
        onError: (ev) => {
          updateLastMessage('❌ ' + (ev.message || '流式连接错误'), true)
          streaming.value = false
          _streamActive = false
          _activeStream = null
          _pendingToolEvents = []
          saveMessages()
        }
      })
    }

    onMounted(() => {
      syncPageHeight()
      window.addEventListener('resize', syncPageHeight)
      // 进入页面（含恢复历史会话）后直接落到最后一条消息，避免默认停在会话开头
      nextTick(() => {
        scrollToBottom(true)
        // 图表 iframe / 字体加载会改变高度，下一帧再贴一次底
        window.requestAnimationFrame(() => scrollToBottom(true))
      })
    })

    onBeforeUnmount(() => {
      // 不关闭流式连接！只断开回调，后端继续跑
      _detachComponent()
      window.removeEventListener('resize', syncPageHeight)
      taskStream.value?.close()
      saveMessages()
    })

    return {
      messages,
      timeline,
      pageRef,
      pageStyle,
      onInputKeydown,
      inputText,
      streaming,
      connected,
      stockCode,
      analyzing,
      indicators,
      loadingIndicators,
      selectedStrategy,
      showTaskDrawer,
      taskLoading,
      taskList,
      quickActions,
      chatContainerRef,
      currentStrategyInfo,
      selectStrategy,
      onStrategyChange,
      clearStrategy,
      sendMessage,
      stopStream,
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
  /* 高度由 setup 里实测的 pageStyle 提供（视口剩余高度），这里只兜底最小高度 */
  min-height: 360px;
  background: #fff;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
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
  min-height: 0;
  overflow-y: auto;
  padding: 20px 24px;
  background: #fafbfc;
}

.chat-messages {
  max-width: 99%;
  margin: 0 auto;
}

/* 跨天日期分隔条 */
.date-divider {
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 6px 0 18px;

  .date-chip {
    padding: 2px 12px;
    font-size: 12px;
    color: #8c8c8c;
    background: rgba(0, 0, 0, 0.04);
    border-radius: 10px;
    line-height: 20px;
  }
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
  padding: 12px 24px 10px;
  background: #fff;
  border-top: 1px solid #f0f0f0;
  box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.02);

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
      line-height: 1.6;
      transition: box-shadow 0.2s, border-color 0.2s;

      &:focus {
        box-shadow: 0 0 0 3px rgba(24, 144, 255, 0.12);
      }
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
    justify-content: center;
    gap: 6px;
    font-size: 12px;
    color: #b0b0b0;

    kbd {
      padding: 0 4px;
      font-size: 11px;
      font-family: inherit;
      color: #8c8c8c;
      background: #f5f5f5;
      border: 1px solid #e8e8e8;
      border-bottom-width: 2px;
      border-radius: 4px;
    }

    .hint-sep {
      color: #d9d9d9;
    }
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
