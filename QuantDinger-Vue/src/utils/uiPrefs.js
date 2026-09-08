/**
 * uiPrefs.js — 用户级 UI 偏好持久化 (纯前端方案, localStorage, 零安装/全浏览器兼容)
 *
 * 用途: 记住用户界面状态 (最后停留栏目/自选组/K线配置/...), 刷新或重开浏览器后恢复。
 *      为将来其它栏目扩展预留统一入口 (如: 智能体上下文记忆、布局偏好、表格列宽)。
 *
 * 关键设计点:
 *  - 命名空间: qd.ui.v1.<userId>.<module>, 每模块一个 JSON 对象 (键少, 新栏目零成本接入)
 *  - 用户隔离: userId 取自 vuex userInfo, 未登录归入 'shared'
 *  - 自管理滑动TTL: JSON 内嵌 _t 时间戳, 每次写入刷新; 读取超期视为无数据。
 *    不依赖 store/plugins/expire 的挂载顺序, 本模块自包含
 *  - 容错: 读写全程 try/catch; 数据损坏静默丢弃; localStorage 不可用(隐私模式)降级为内存
 *
 * 易错点:
 *  - localStorage 明文可见, 只存界面偏好, 不要放敏感信息 (密码/密钥等严禁)
 *  - 大对象注意 ~5MB 配额, 超限时写入静默丢弃, 调用方需容忍丢失 (读侧永远安全)
 */
import storage from 'store'
import store from '@/store'

const NS = 'qd.ui.v1'
const VERSION = 1
const MODULE_TTL_MS = 90 * 24 * 60 * 60 * 1000 // 90天滑动: 活跃使用的偏好一直保留

const _memoryFallback = {} // localStorage 不可用时的进程内降级
let _lsOk = null

function _lsAvailable () {
  if (_lsOk !== null) return _lsOk
  try {
    const probe = `${NS}.__probe`
    storage.set(probe, 1)
    _lsOk = storage.get(probe) === 1
    storage.remove(probe)
  } catch (e) {
    _lsOk = false
  }
  return _lsOk
}

/** 当前偏好归属用户 (登录后为用户id, 未登录为 shared) */
export function getPrefsUserId () {
  try {
    const info = store.getters.userInfo || {}
    const id = info.id != null ? info.id : (info.user_id != null ? info.user_id : '')
    return id !== '' ? String(id) : 'shared'
  } catch (e) {
    return 'shared'
  }
}

function _key (module) {
  return `${NS}.${getPrefsUserId()}.${module}`
}

/** 读整个模块的偏好对象 (损坏/超期/无数据 → 返回 def, 默认 {}) */
export function readPrefs (module, def) {
  const empty = def === undefined ? {} : def
  try {
    const key = _key(module)
    const raw = _lsAvailable() ? storage.get(key) : _memoryFallback[key]
    if (raw == null || raw === '') return empty
    const s = typeof raw === 'string' ? JSON.parse(raw) : raw
    if (!s || typeof s !== 'object' || s._v !== VERSION) return empty
    if (!s._t || Date.now() - s._t > MODULE_TTL_MS) return empty
    const out = Object.assign({}, s)
    delete out._v
    delete out._t
    return out
  } catch (e) {
    return empty
  }
}

/** 写模块偏好 (merge=true 增量合并, false 整体替换); 滑动刷新TTL */
export function writePrefs (module, patch, merge = true) {
  try {
    const base = merge ? readPrefs(module) : {}
    const payload = Object.assign(base, patch, { _v: VERSION, _t: Date.now() })
    const key = _key(module)
    const raw = JSON.stringify(payload)
    if (_lsAvailable()) storage.set(key, raw)
    else _memoryFallback[key] = raw
  } catch (e) { /* 配额/序列化失败: 静默丢弃, 读侧安全 */ }
}

/** 读模块内单个值 */
export function prefValue (module, key, def) {
  const v = readPrefs(module)[key]
  return v === undefined ? def : v
}

/** 写模块内单个值 */
export function setPrefValue (module, key, val) {
  writePrefs(module, { [key]: val })
}

/** 清除模块 (登出清理某栏目/版本升级时用) */
export function clearModule (module) {
  try {
    if (_lsAvailable()) storage.remove(_key(module))
  } catch (e) { /* ignore */ }
  delete _memoryFallback[_key(module)]
}
