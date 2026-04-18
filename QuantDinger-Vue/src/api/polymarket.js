/**
 * Polymarket预测市场API
 * 仅保留按需分析功能
 */
import request from '@/utils/request'

const BASE_URL = '/api/polymarket'

/**
 * 分析Polymarket预测市场（从链接或标题）
 * @param {Object} data - 请求数据
 * @param {string} data.input - Polymarket链接或市场标题
 * @param {string} data.language - 语言 (zh-CN/en-US)
 */
export function analyzePolymarketMarket (data) {
  return request({
    url: `${BASE_URL}/analyze`,
    method: 'post',
    data,
    timeout: 120000 // 2分钟超时
  })
}

/**
 * 获取Polymarket分析历史记录
 * @param {Object} params - 查询参数
 * @param {number} params.page - 页码
 * @param {number} params.page_size - 每页数量
 */
export function getPolymarketHistory (params) {
  return request({
    url: `${BASE_URL}/history`,
    method: 'get',
    params
  })
}
