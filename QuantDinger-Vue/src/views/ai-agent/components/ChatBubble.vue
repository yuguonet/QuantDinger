<template>
  <div class="chat-bubble" :class="[message.role, { streaming: message.streaming }]">
    <div class="avatar">
      <a-icon :type="message.role === 'user' ? 'user' : 'robot'" />
    </div>
    <div class="bubble-body">
      <div class="bubble-meta">
        <span class="role-label">{{ message.role === 'user' ? 'You' : 'Agent' }}</span>
        <span class="time-label" v-if="message.time">{{ message.time }}</span>
      </div>

      <!-- 工具调用过程 -->
      <div v-if="message.toolEvents && message.toolEvents.length" class="tool-events">
        <div v-for="(ev, i) in message.toolEvents" :key="i" class="tool-event" :class="{ 'has-stream': ev.streamOutput }">
          <div class="tool-header">
            <a-icon v-if="ev.status === 'done'" type="check-circle" class="done" />
            <a-icon v-else-if="ev.status === 'error'" type="close-circle" class="error" />
            <a-icon v-else type="loading" class="loading" />
            <span class="tool-name">{{ ev.display_name || ev.tool }}</span>
            <span v-if="ev.info" class="tool-info">{{ ev.info }}</span>
          </div>
          <!-- 流式输出面板 -->
          <div v-if="ev.streamOutput" class="tool-stream-panel">
            <pre class="stream-output">{{ ev.streamOutput }}</pre>
          </div>
          <!-- 错误恢复建议 -->
          <div v-if="ev.recovery" class="tool-recovery">
            <a-icon type="bulb" /> {{ ev.recovery }}
          </div>
        </div>
      </div>

      <!-- 消息内容：JSON 分析结果渲染为卡片，否则 markdown -->
      <div v-if="analysisResult" class="analysis-card">
        <div class="card-header">
          <span class="card-title">{{ analysisResult.stock_name || analysisResult.stock_code || '分析结果' }}</span>
          <span v-if="analysisResult.stock_code" class="card-code">{{ analysisResult.stock_code }}</span>
          <a-tag :color="actionColor(analysisResult.action)" class="card-action">{{ actionLabel(analysisResult.action) }}</a-tag>
        </div>
        <div class="card-score-row">
          <div class="score-circle" :class="scoreClass(analysisResult.score)">
            <span class="score-num">{{ analysisResult.score }}</span>
            <span class="score-label">评分</span>
          </div>
          <div class="score-meta">
            <div v-if="analysisResult.direction"><span class="meta-label">方向</span> <a-tag :color="dirColor(analysisResult.direction)">{{ analysisResult.direction }}</a-tag></div>
            <div v-if="analysisResult.confidence"><span class="meta-label">置信度</span> {{ analysisResult.confidence }}</div>
            <div v-if="analysisResult.timeframe"><span class="meta-label">周期</span> {{ analysisResult.timeframe }}</div>
          </div>
        </div>
        <div v-if="analysisResult.signal" class="card-signal">{{ analysisResult.signal }}</div>
        <div v-if="analysisResult.factors && analysisResult.factors.length" class="card-factors">
          <div v-for="f in analysisResult.factors" :key="f.name" class="factor-item">
            <span class="factor-name">{{ f.name }}</span>
            <a-progress :percent="f.score" :show-info="false" :stroke-color="factorColor(f.score)" size="small" />
            <span class="factor-score">{{ f.score }}</span>
          </div>
        </div>
        <div v-if="analysisResult.analysis" class="card-analysis">{{ analysisResult.analysis }}</div>
      </div>
      <div v-else class="bubble-content" v-html="renderedContent"></div>

      <!-- 图表渲染（来自 SSE chart 事件） -->
      <div v-for="(chartHtml, ci) in (message.charts || [])" :key="'c'+ci" class="chart-container">
        <iframe :srcdoc="chartHtml" frameborder="0" sandbox="allow-scripts" scrolling="no"></iframe>
      </div>

      <!-- 打字指示器 -->
      <div v-if="message.streaming && !message.content" class="typing-indicator">
        <span></span><span></span><span></span>
      </div>
    </div>
  </div>
</template>

<script>
import { computed, defineComponent } from 'vue'

export default defineComponent({
  name: 'ChatBubble',
  props: {
    message: {
      type: Object,
      required: true
    }
  },
  setup (props) {
    // 通用 JSON 分析结果检测：任何含 score 字段的 JSON 对象
    // 优先从 finalContent 取（混合模式），否则从 content 取
    const analysisResult = computed(() => {
      const raw = props.message.finalContent || props.message.content || ''
      if (!raw || raw.length > 50000) return null
      try {
        let text = raw
        // 处理双重转义
        if (typeof text === 'string' && text.includes('\\"')) {
          text = text.replace(/\\"/g, '"')
        }
        const obj = typeof text === 'string' ? JSON.parse(text) : text
        if (obj && typeof obj === 'object' && !Array.isArray(obj) && 'score' in obj) {
          return obj
        }
      } catch (_) {}
      return null
    })

    function actionColor (action) {
      const map = { buy: 'green', sell: 'red', hold: 'blue', skip: 'default' }
      return map[action] || 'default'
    }
    function actionLabel (action) {
      const map = { buy: '买入', sell: '卖出', hold: '持有', skip: '回避' }
      return map[action] || action || '-'
    }
    function scoreClass (score) {
      if (score >= 70) return 'score-high'
      if (score <= 30) return 'score-low'
      return 'score-mid'
    }
    function dirColor (d) {
      const map = { bullish: 'green', bearish: 'red', neutral: 'blue' }
      return map[d] || 'default'
    }
    function factorColor (score) {
      if (score >= 70) return '#52c41a'
      if (score <= 30) return '#ff4d4f'
      return '#1890ff'
    }

    const renderedContent = computed(() => {
      const raw = props.message.content || ''

      // Step 0: 提取图表标记（在 HTML 转义之前）
      const chartSlots = []
      const text = raw.replace(/__CHART_B64__([A-Za-z0-9+/=]+)__END_CHART__/g, (_, b64) => {
        const idx = chartSlots.length
        try {
          chartSlots.push(atob(b64))
        } catch (e) {
          chartSlots.push('')
        }
        return `%%CHART_${idx}%%`
      })

      // Step 1: HTML entity encode
      let safe = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#x27;')

      // Step 2: Apply allowed markdown transforms
      safe = safe
        // Fenced code blocks (with optional language)
        .replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
          const langLabel = lang ? `<span class="code-lang">${lang}</span>` : ''
          return `<div class="code-block-wrapper">${langLabel}<pre class="code-block"><code>${code}</code></pre></div>`
        })
        .replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        // Headers (### / ## / #)
        .replace(/^### (.+)$/gm, '<h4 class="md-h3">$1</h4>')
        .replace(/^## (.+)$/gm, '<h3 class="md-h2">$1</h3>')
        .replace(/^# (.+)$/gm, '<h2 class="md-h1">$1</h2>')
        // Bullet lists
        .replace(/^- (.+)$/gm, '<li>$1</li>')
        .replace(/(<li>.*<\/li>)/gs, '<ul>$1</ul>')

      // Markdown tables: parse before \n → <br>
      const nonTableLines = []
      const lines = safe.split('\n')
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i].trim()
        if (line.startsWith('|') && line.endsWith('|')) {
          // Check if next line is separator (|---|---|)
          const next = (lines[i + 1] || '').trim()
          if (/^\|[\s\-:|]+\|$/.test(next)) {
            // This is a table header, collect entire table
            const tableRows = []
            tableRows.push(lines[i]) // header
            i++ // skip separator
            tableRows.push(lines[i]) // separator
            while (i + 1 < lines.length && (lines[i + 1] || '').trim().startsWith('|')) {
              i++
              tableRows.push(lines[i])
            }
            // Build HTML table
            const parseCells = (row) => row.replace(/^\||\|$/g, '').split('|').map(c => c.trim())
            const headerCells = parseCells(tableRows[0])
            const bodyRows = tableRows.slice(2) // skip separator
            let tbl = '<div class="md-table-wrapper"><table class="md-table"><thead><tr>'
            headerCells.forEach(c => { tbl += `<th>${c}</th>` })
            tbl += '</tr></thead><tbody>'
            bodyRows.forEach(row => {
              const cells = parseCells(row)
              tbl += '<tr>'
              cells.forEach(c => { tbl += `<td>${c}</td>` })
              tbl += '</tr>'
            })
            tbl += '</tbody></table></div>'
            nonTableLines.push(tbl)
            continue
          }
        }
        nonTableLines.push(lines[i])
      }
      safe = nonTableLines.join('\n')
        .replace(/\n/g, '<br>')

      // Step 3: Strip disallowed HTML tags
      safe = safe.replace(/<(?!\/?(strong|code|pre|br|h[2-4]|ul|li|div|span|iframe|table|thead|tbody|tr|th|td)\b)[^>]+>/gi, '')

      // Step 4: 还原图表为 iframe
      chartSlots.forEach((html, idx) => {
        const srcdoc = html.replace(/"/g, '&quot;')
        const iframe = `<div class="chart-container"><iframe srcdoc="${srcdoc}" frameborder="0" sandbox="allow-scripts" scrolling="no"></iframe></div>`
        safe = safe.replace(`%%CHART_${idx}%%`, iframe)
      })

      return safe
    })

    return { renderedContent, analysisResult, actionColor, actionLabel, scoreClass, dirColor, factorColor }
  }
})
</script>

<style scoped lang="less">
.chat-bubble {
  display: flex;
  gap: 12px;
  margin-bottom: 20px;
  animation: fadeInUp 0.3s ease;

  &.assistant {
    .avatar {
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    }
    .bubble-body {
      max-width: 72%;
    }
    .bubble-content {
      background: #f0f2f5;
      color: #333;
    }
  }

  &.user {
    flex-direction: row-reverse;
    .avatar {
      background: linear-gradient(135deg, #1890ff 0%, #096dd9 100%);
    }
    .bubble-body {
      align-items: flex-end;
    }
    .bubble-content {
      background: linear-gradient(135deg, #1890ff 0%, #096dd9 100%);
      color: #fff;
      border-radius: 18px 18px 4px 18px;
    }
  }

  &.streaming .bubble-content::after {
    content: '▊';
    animation: blink 1s infinite;
    margin-left: 2px;
  }
}

.avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: #fff;
  font-size: 16px;
}

.bubble-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  flex: 1;
}

.bubble-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #999;
}

.bubble-content {
  padding: 12px 16px;
  border-radius: 18px 18px 18px 4px;
  font-size: 14px;
  line-height: 1.7;
  word-break: break-word;
  max-width: 100%;

  :deep(.code-block-wrapper) {
    position: relative;
    margin: 8px 0;

    .code-lang {
      position: absolute;
      top: 4px;
      right: 8px;
      font-size: 11px;
      color: #858585;
      text-transform: uppercase;
    }

    pre.code-block {
      background: #1e1e1e;
      color: #d4d4d4;
      padding: 12px;
      border-radius: 8px;
      overflow-x: auto;
      font-size: 13px;
      margin: 0;
      max-height: 400px;

      code {
        font-family: 'Fira Code', 'Consolas', monospace;
      }
    }
  }

  :deep(code.inline-code) {
    background: rgba(0, 0, 0, 0.06);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 13px;
    font-family: 'Fira Code', 'Consolas', monospace;
  }

  :deep(h2.md-h1), :deep(h3.md-h2), :deep(h4.md-h3) {
    margin: 12px 0 6px;
    font-weight: 600;
  }

  :deep(ul) {
    padding-left: 20px;
    margin: 4px 0;
  }

  :deep(.md-table-wrapper) {
    overflow-x: auto;
    margin: 8px 0;
    border-radius: 8px;
    border: 1px solid #e8e8e8;
  }

  :deep(table.md-table) {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    line-height: 1.6;

    th, td {
      padding: 6px 12px;
      text-align: left;
      border-bottom: 1px solid #f0f0f0;
      white-space: nowrap;
    }

    th {
      background: #fafafa;
      font-weight: 600;
      color: #333;
      border-bottom: 2px solid #e8e8e8;
    }

    tr:hover td {
      background: #f5f7fa;
    }

    tr:last-child td {
      border-bottom: none;
    }
  }
}

/* ── 工具调用事件 ─────────────────────────── */

.tool-events {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 6px;
}

.tool-event {
  background: #f8f9fa;
  border: 1px solid #e8e8e8;
  border-radius: 8px;
  overflow: hidden;
  transition: all 0.2s;

  &.has-stream {
    border-color: #d9d9d9;
  }

  .tool-header {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: #555;
    padding: 6px 10px;

    .done { color: #52c41a; font-size: 14px; }
    .error { color: #ff4d4f; font-size: 14px; }
    .loading { color: #1890ff; font-size: 14px; }

    .tool-name {
      font-weight: 500;
    }

    .tool-info {
      color: #888;
      font-size: 11px;
      margin-left: auto;
    }
  }

  .tool-stream-panel {
    border-top: 1px solid #f0f0f0;
    max-height: 200px;
    overflow-y: auto;

    .stream-output {
      margin: 0;
      padding: 8px 10px;
      font-size: 12px;
      font-family: 'Fira Code', 'Consolas', monospace;
      line-height: 1.5;
      color: #444;
      background: #fafafa;
      white-space: pre-wrap;
      word-break: break-all;
    }
  }

  .tool-recovery {
    display: flex;
    align-items: flex-start;
    gap: 4px;
    padding: 6px 10px;
    font-size: 12px;
    color: #fa8c16;
    background: #fffbe6;
    border-top: 1px solid #ffe58f;

    .anticon {
      margin-top: 2px;
      flex-shrink: 0;
    }
  }
}

/* ── 图表容器 ─────────────────────────────── */
:deep(.chart-container) {
  margin: 10px 0;
  border-radius: 10px;
  overflow: hidden;
  border: 1px solid #e0e0e0;
  background: #1a1a2e;

  iframe {
    width: 100%;
    height: 420px;
    border: none;
    display: block;
  }
}

/* ── 打字指示器 ───────────────────────────── */

.typing-indicator {
  display: flex;
  gap: 4px;
  padding: 8px 0;

  span {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #999;
    animation: bounce 1.4s infinite;
    &:nth-child(2) { animation-delay: 0.2s; }
    &:nth-child(3) { animation-delay: 0.4s; }
  }
}

@keyframes fadeInUp {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

@keyframes bounce {
  0%, 80%, 100% { transform: scale(0); }
  40% { transform: scale(1); }
}

@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}
/* ── 分析结果卡片（通用：任何含 score 的 JSON） ─── */

.analysis-card {
  background: #fff;
  border: 1px solid #e8e8e8;
  border-radius: 12px;
  padding: 16px;
  max-width: 100%;

  .card-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 12px;

    .card-title {
      font-size: 16px;
      font-weight: 600;
      color: #333;
    }
    .card-code {
      font-size: 13px;
      color: #888;
    }
    .card-action {
      margin-left: auto;
    }
  }

  .card-score-row {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 12px;

    .score-circle {
      width: 64px;
      height: 64px;
      border-radius: 50%;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;

      .score-num {
        font-size: 22px;
        font-weight: 700;
        line-height: 1;
      }
      .score-label {
        font-size: 10px;
        opacity: 0.7;
      }

      &.score-high {
        background: #f6ffed;
        border: 2px solid #52c41a;
        color: #52c41a;
      }
      &.score-mid {
        background: #e6f7ff;
        border: 2px solid #1890ff;
        color: #1890ff;
      }
      &.score-low {
        background: #fff2f0;
        border: 2px solid #ff4d4f;
        color: #ff4d4f;
      }
    }

    .score-meta {
      display: flex;
      flex-direction: column;
      gap: 4px;
      font-size: 13px;

      .meta-label {
        color: #999;
        margin-right: 4px;
      }
    }
  }

  .card-signal {
    padding: 8px 12px;
    background: #fafafa;
    border-radius: 8px;
    font-size: 14px;
    color: #333;
    margin-bottom: 12px;
    line-height: 1.5;
  }

  .card-factors {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-bottom: 12px;

    .factor-item {
      display: flex;
      align-items: center;
      gap: 8px;

      .factor-name {
        width: 80px;
        font-size: 13px;
        color: #555;
        flex-shrink: 0;
      }
      .factor-score {
        width: 32px;
        text-align: right;
        font-size: 13px;
        font-weight: 500;
        flex-shrink: 0;
      }
      :deep(.ant-progress) {
        flex: 1;
      }
    }
  }

  .card-analysis {
    font-size: 13px;
    color: #666;
    line-height: 1.6;
    border-top: 1px solid #f0f0f0;
    padding-top: 10px;
  }
}

</style>
