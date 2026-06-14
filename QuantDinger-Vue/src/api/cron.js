// ══════════════════════════════════════════════════════════
// @/api/cron.js — Cron 定时任务管理 API
// ══════════════════════════════════════════════════════════
import request from '@/utils/request'

/**
 * 获取定时任务列表
 * GET /api/agent/cron/jobs
 */
export function getCronJobs (params) {
  return request({ url: '/api/agent/cron/jobs', method: 'get', params })
}

/**
 * 创建定时任务
 * POST /api/agent/cron/jobs
 */
export function createCronJob (data) {
  return request({ url: '/api/agent/cron/jobs', method: 'post', data })
}

/**
 * 更新定时任务
 * PUT /api/agent/cron/jobs/:id
 */
export function updateCronJob (id, data) {
  return request({ url: `/api/agent/cron/jobs/${id}`, method: 'put', data })
}

/**
 * 删除定时任务
 * DELETE /api/agent/cron/jobs/:id
 */
export function deleteCronJob (id) {
  return request({ url: `/api/agent/cron/jobs/${id}`, method: 'delete' })
}

/**
 * 手动触发定时任务
 * POST /api/agent/cron/jobs/:id/trigger
 */
export function triggerCronJob (id) {
  return request({ url: `/api/agent/cron/jobs/${id}/trigger`, method: 'post' })
}

/**
 * 获取 Cron Worker 状态
 * GET /api/cron/status
 */
export function getCronStatus () {
  return request({ url: '/api/cron/status', method: 'get' })
}

/**
 * 创建 SSE 连接，监听 Cron 任务执行事件
 * @param {Object} callbacks - { onStart, onSuccess, onError, onConnected }
 * @returns {{ close: Function }}
 */
export function createCronEventStream (callbacks) {
  const es = new EventSource('/api/cron/events')

  es.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data)
      switch (data.type) {
        case 'connected':
          callbacks.onConnected?.(data)
          break
        case 'job_start':
          callbacks.onStart?.(data)
          break
        case 'job_success':
          callbacks.onSuccess?.(data)
          break
        case 'job_error':
          callbacks.onError?.(data)
          break
      }
    } catch (err) {
      console.warn('Cron SSE parse error:', err)
    }
  }

  es.onerror = (e) => {
    console.warn('Cron SSE connection error:', e)
  }

  return { close: () => es.close() }
}
