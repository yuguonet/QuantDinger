/**
 * 自选行情轮询开关 — 前端唯一判定入口
 *
 * 权威判定在后端 `/api/market/watchlist/trading-status`（交易日历 + 服务器时区是
 * 单一事实源），前端只做两件事：缓存 5 分钟 + status 首次拉取失败时按本地时间降级。
 *
 * 为什么抽成模块单例：`WatchlistPanel`（15s）与 `ai-analysis`（30s）都轮询自选价格，
 * 原先各写一份 trading-status 判定与降级窗口，必漂成两套口径——后端在 2026-10-29
 * 之前正是这个毛病（`cn_stock._is_in_trading_hours` 与 market 路由都写成
 * 09:15~15:01 连续区间，午休被算作盘中，45 只自选股每轮都打远端）。
 * 降级窗口与后端 `trading_calendar.is_in_quote_session` 同口径：
 * 交易日 09:15~11:31、13:00~15:01，午休不算。
 */
import { getTradingStatus } from '@/api/market'

const STATUS_TTL = 5 * 60 * 1000

let polling = null      // null = 从未成功拿到 status
let fetchedAt = 0
let inflight = null     // 并发去重：两页面同时 tick 只发一次请求

function fallbackPolling () {
  const now = new Date()
  const day = now.getDay()
  if (day === 0 || day === 6) return false
  const m = now.getHours() * 60 + now.getMinutes()
  return (m > 9 * 60 + 15 && m <= 11 * 60 + 31) ||
         (m >= 13 * 60 && m <= 15 * 60 + 1)
}

/**
 * 此刻是否该轮询自选价格。
 * @returns {Promise<boolean>}
 */
export async function shouldPollPrices () {
  if (polling != null && Date.now() - fetchedAt <= STATUS_TTL) return polling
  if (!inflight) {
    inflight = getTradingStatus()
      .then(res => {
        if (res && res.code === 1 && res.data) {
          polling = !!res.data.price_polling
          fetchedAt = Date.now()
        }
      })
      .catch(() => { /* 沿用上次结果；从未成功过则走 fallbackPolling */ })
      .finally(() => { inflight = null })
  }
  await inflight
  return polling != null ? polling : fallbackPolling()
}
