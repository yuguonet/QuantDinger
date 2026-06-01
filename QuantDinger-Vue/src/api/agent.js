// ══════════════════════════════════════════════════════════
// @/api/agent.js — 完整版（含策略接口 + 工作区）
// ══════════════════════════════════════════════════════════
import request from '@/utils/request'
import storage from 'store'
import { ACCESS_TOKEN } from '@/store/mutation-types'

// ── 策略 ──────────────────────────────────────────────────

/**
 * 获取可用策略列表（从用户指标加载）
 * GET /api/agent/strategies
 */
export function getStrategies () {
  return request({ url: '/api/agent/strategies', method: 'get' })
}

// ── 聊天 ──────────────────────────────────────────────────

/**
 * 普通聊天
 * POST /api/agent/chat
 */
export function agentChat (data) {
  return request({ url: '/api/agent/chat', method: 'post', data })
}

/**
 * 流式聊天（SSE）
 * POST /api/agent/chat/stream
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
 * @param {Object} callbacks - {
 *   onThinking, onToolStart, onToolDone, onGenerating,
 *   onToolStream, onToolInfo,   // 新增：流式输出 + 工具信息
 *   onDone, onError
 * }
 * @returns {{ close: Function }}
 */
export function createAgentStream (params, callbacks) {
  const controller = new AbortController()

  let token = storage.get(ACCESS_TOKEN)
  if (token && typeof token === 'object') {
    token = token.token || token.value || ''
  }
  const lang = storage.get('lang') || 'en-US'

  fetch('/api/agent/chat/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? {
        'Authorization': `Bearer ${token}`,
        'Access-Token': token,
        'token': token
      } : {}),
      'X-App-Lang': lang,
      'Accept-Language': lang,
      'Cache-Control': 'no-cache'
    },
    body: JSON.stringify(params),
    signal: controller.signal,
    credentials: 'include'
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
            case 'tool_stream': callbacks.onToolStream?.(event); break
            case 'tool_info': callbacks.onToolInfo?.(event); break
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

export function triggerAnalysis (data) {
  return request({ url: '/api/agent-analysis/analyze', method: 'post', data })
}

export function getAnalysisTasks (params) {
  return request({ url: '/api/agent-analysis/tasks', method: 'get', params })
}

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
