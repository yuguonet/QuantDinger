// ══════════════════════════════════════════════════════════
// @/api/agent.js — 完整版（含策略接口）
// ══════════════════════════════════════════════════════════
import request from '@/utils/request'

// ── 策略 ──────────────────────────────────────────────────

/**
 * 获取可用策略列表（从后端 YAML 文件加载）
 * GET /api/agent/strategies
 * 返回: { strategies: [{ id, name, description, category }] }
 */
export function getStrategies () {
  return request({
    url: '/api/agent/strategies',
    method: 'get'
  })
}

// ── 聊天 ──────────────────────────────────────────────────

/**
 * 普通聊天
 * POST /api/agent/chat
 * @param {Object} data - { message, session_id, strategy_id?, context? }
 */
export function agentChat (data) {
  return request({
    url: '/api/agent/chat',
    method: 'post',
    data
  })
}

/**
 * 流式聊天（SSE）
 * POST /api/agent/chat/stream
 * 返回 ReadableStream / EventSource
 */
export function agentChatStream (data) {
  return request({
    url: '/api/agent/chat/stream',
    method: 'post',
    data,
    responseType: 'stream'
  })
}

// ── SSE 工具函数 ──────────────────────────────────────────

/**
 * 创建 Agent 流式连接（SSE）
 * @param {Object} params - { message, session_id, strategy_id?, context? }
 * @param {Object} callbacks - { onThinking, onToolStart, onToolDone, onGenerating, onDone, onError }
 * @returns {{ close: Function }}
 */
export function createAgentStream (params, callbacks) {
  const controller = new AbortController()

  fetch('/api/agent/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
    signal: controller.signal
  }).then(async (response) => {
    if (!response.ok) {
      const err = await response.json().catch(() => ({}))
      callbacks.onError?.({ message: err.error || `HTTP ${response.status}` })
      return
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed.startsWith('data:')) continue
        const dataStr = trimmed.slice(5).trimStart()
        if (!dataStr) continue
        try {
          const event = JSON.parse(dataStr)
          switch (event.type) {
            case 'thinking': callbacks.onThinking?.(event); break
            case 'tool_start': callbacks.onToolStart?.(event); break
            case 'tool_done': callbacks.onToolDone?.(event); break
            case 'generating': callbacks.onGenerating?.(event); break
            case 'done': callbacks.onDone?.(event); break
            case 'error': callbacks.onError?.(event); break
          }
        } catch (e) {
          console.warn('SSE parse error:', e, line)
        }
      }
    }
  }).catch(err => {
    if (err.name !== 'AbortError') {
      callbacks.onError?.({ message: err.message })
    }
  })

  return { close: () => controller.abort() }
}

// ── 分析任务 ──────────────────────────────────────────────

/**
 * 触发股票分析
 * POST /api/agent-analysis/analyze
 * @param {Object} data - { stock_code, async_mode, strategy_id? }
 */
export function triggerAnalysis (data) {
  return request({
    url: '/api/agent-analysis/analyze',
    method: 'post',
    data
  })
}

/**
 * 获取分析任务列表
 * GET /api/agent-analysis/tasks
 */
export function getAnalysisTasks (params) {
  return request({
    url: '/api/agent-analysis/tasks',
    method: 'get',
    params
  })
}

/**
 * 创建任务状态 SSE 流
 * @param {Object} callbacks - { onConnected, onTaskCreated, onTaskProgress, onTaskCompleted }
 * @returns {{ close: Function }}
 */
export function createTaskStream (callbacks) {
  const es = new EventSource('/api/agent-analysis/tasks/stream')

  es.addEventListener('connected', (e) => {
    try { callbacks.onConnected?.(JSON.parse(e.data)) } catch {}
  })
  es.addEventListener('task_created', (e) => {
    try { callbacks.onTaskCreated?.(JSON.parse(e.data)) } catch {}
  })
  es.addEventListener('task_progress', (e) => {
    try { callbacks.onTaskProgress?.(JSON.parse(e.data)) } catch {}
  })
  es.addEventListener('task_completed', (e) => {
    try { callbacks.onTaskCompleted?.(JSON.parse(e.data)) } catch {}
  })
  es.addEventListener('error', (e) => {
    console.warn('Task SSE error:', e)
  })

  return { close: () => es.close() }
}
