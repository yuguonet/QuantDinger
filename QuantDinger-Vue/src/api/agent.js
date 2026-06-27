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

// ── SSE 工具函数 ──────────────────────────────────────────

/**
 * 创建 Agent 流式连接（SSE，统一混合模式）
 * @param {Object} params - { message, session_id, strategy_id?, context? }
 * @param {Object} callbacks - {
 *   onNodeStart, onNodeDone,     // 节点生命周期
 *   onProgress, onStepContent,   // 进度 + 步骤内容
 *   onToolStart, onToolDone,     // 工具调用
 *   onDone, onError              // 最终结果
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

  fetch('/api/agent/chat', {
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
            case 'node_start': callbacks.onNodeStart?.(event); break
            case 'node_done': callbacks.onNodeDone?.(event); break
            case 'progress': callbacks.onProgress?.(event); break
            case 'step_content': callbacks.onStepContent?.(event); break
            case 'tool_start': callbacks.onToolStart?.(event); break
            case 'tool_done': callbacks.onToolDone?.(event); break
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

// ── 会话管理 ──────────────────────────────────────────────

/**
 * 删除会话（清空后端上下文）
 * DELETE /api/agent/chat/sessions/:sessionId
 */
export function deleteChatSession (sessionId) {
  return request({ url: `/api/agent/chat/sessions/${sessionId}`, method: 'delete' })
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
