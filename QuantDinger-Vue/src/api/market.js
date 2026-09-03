import request, { ANALYSIS_TIMEOUT } from '@/utils/request'

const marketApi = {
  // Watchlist
  GetWatchlist: '/api/market/watchlist/get',
  AddWatchlist: '/api/market/watchlist/add',
  RemoveWatchlist: '/api/market/watchlist/remove',
  RenameWatchlistGroup: '/api/market/watchlist/rename-group',
  RemoveWatchlistGroup: '/api/market/watchlist/remove-group',
  GetWatchlistPrices: '/api/market/watchlist/prices',
  GetDragonToday: '/api/market/dragon/today',
  GetDragonMarkers: '/api/market/dragon/markers',
  // Analysis
  MultiAnalysis: '/api/analysis/multiAnalysis',
  CreateAnalysisTask: '/api/analysis/createTask',
  GetAnalysisTaskStatus: '/api/analysis/getTaskStatus',
  GetAnalysisHistoryList: '/api/analysis/getHistoryList',
  DeleteAnalysisTask: '/api/analysis/deleteTask',
  ReflectAnalysis: '/api/analysis/reflect',
  // Public config
  GetConfig: '/api/market/config',
  GetMenuFooterConfig: '/api/market/menuFooterConfig',
  // Market metadata
  GetMarketTypes: '/api/market/types',
  // Symbol search
  SearchSymbols: '/api/market/symbols/search',
  GetHotSymbols: '/api/market/symbols/hot'
}

/**
 * 获取自选股列表
 * @param parameter { userid: number }
 * @returns {*}
 */
export function getWatchlist (parameter) {
  return request({
    url: marketApi.GetWatchlist,
    method: 'get',
    params: parameter
  })
}

/**
 * 添加自选股
 * @param parameter { userid: number, market: string, symbol: string }
 * @returns {*}
 */
export function addWatchlist (parameter) {
  return request({
    url: marketApi.AddWatchlist,
    method: 'post',
    data: parameter
  })
}

/**
 * 删除自选股
 * @param parameter { userid: number, symbol: string }
 * @returns {*}
 */
export function removeWatchlist (parameter) {
  return request({
    url: marketApi.RemoveWatchlist,
    method: 'post',
    data: parameter
  })
}

/**
 * 重命名自选股分组
 * @param parameter { userid: number, old_name: string, new_name: string }
 * @returns {*}
 */
export function renameWatchlistGroup (parameter) {
  return request({
    url: marketApi.RenameWatchlistGroup,
    method: 'post',
    data: parameter
  })
}

/**
 * 删除自选股分组（同时删除组内全部股票）
 * @param parameter { userid: number, group_name: string }
 * @returns {*}
 */
export function removeWatchlistGroup (parameter) {
  return request({
    url: marketApi.RemoveWatchlistGroup,
    method: 'post',
    data: parameter
  })
}

/**
 * 获取自选股价格
 * @param parameter { watchlist: array } watchlist格式：[{market: 'USStock', symbol: 'AAPL'}, ...]
 * @returns {*}
 */
export function getWatchlistPrices (parameter) {
  return request({
    url: marketApi.GetWatchlistPrices,
    method: 'get',
    params: {
      watchlist: JSON.stringify(parameter.watchlist || [])
    }
  })
}

/**
 * 龙回头Pro: 今日分层信号 (action=买入/持仓/卖出, watch=观察池)
 */
export function getDragonToday () {
  return request({
    url: marketApi.GetDragonToday,
    method: 'get'
  })
}

/**
 * 龙回头Pro: K线图买卖点标记
 * @param parameter { symbol: string, days: number }
 */
export function getDragonMarkers (parameter) {
  return request({
    url: marketApi.GetDragonMarkers,
    method: 'get',
    params: parameter
  })
}

/**
 * 执行多维度分析
 * @param parameter { userid: number, market: string, symbol: string }
 * @returns {*}
 */
export function multiAnalysis (parameter) {
  return request({
    url: marketApi.MultiAnalysis,
    method: 'post',
    data: parameter,
    timeout: ANALYSIS_TIMEOUT // Extended timeout for AI analysis
  })
}

/**
 * 创建分析任务
 * @param parameter { userid: number, market: string, symbol: string }
 * @returns {*}
 */
export function createAnalysisTask (parameter) {
  return request({
    url: marketApi.CreateAnalysisTask,
    method: 'post',
    data: parameter
  })
}

/**
 * 获取分析任务状态
 * @param parameter { task_id: number }
 * @returns {*}
 */
export function getAnalysisTaskStatus (parameter) {
  return request({
    url: marketApi.GetAnalysisTaskStatus,
    method: 'get',
    params: parameter
  })
}

/**
 * 获取历史分析列表
 * @param parameter { userid: number, page?: number, pagesize?: number }
 * @returns {*}
 */
export function getAnalysisHistoryList (parameter) {
  return request({
    url: marketApi.GetAnalysisHistoryList,
    method: 'get',
    params: parameter
  })
}

/**
 * Delete analysis task
 * @param parameter { task_id: number }
 * @returns {*}
 */
export function deleteAnalysisTask (parameter) {
  return request({
    url: marketApi.DeleteAnalysisTask,
    method: 'post',
    data: parameter
  })
}

/**
 * 反思学习
 * @param parameter { market: string, symbol: string, decision: string, returns?: number, result?: string }
 * @returns {*}
 */
export function reflectAnalysis (parameter) {
  return request({
    url: marketApi.ReflectAnalysis,
    method: 'post',
    data: parameter
  })
}

/**
 * 获取插件配置
 * @returns {*}
 */
export function getConfig () {
  return request({
    url: marketApi.GetConfig,
    method: 'get'
  })
}

/**
 * 获取菜单底部配置
 * @returns {*}
 */
export function getMenuFooterConfig () {
  return request({
    url: marketApi.GetMenuFooterConfig,
    method: 'get'
  })
}

/**
 * 获取股票类型列表
 * @returns {*}
 */
export function getMarketTypes () {
  return request({
    url: marketApi.GetMarketTypes,
    method: 'get'
  })
}

/**
 * 搜索金融产品
 * @param parameter { market: string, keyword: string, limit?: number }
 * @returns {*}
 */
export function searchSymbols (parameter) {
  return request({
    url: marketApi.SearchSymbols,
    method: 'get',
    params: parameter
  })
}

/**
 * 获取热门标的
 * @param parameter { market: string, limit?: number }
 * @returns {*}
 */
export function getHotSymbols (parameter) {
  return request({
    url: marketApi.GetHotSymbols,
    method: 'get',
    params: parameter
  })
}
