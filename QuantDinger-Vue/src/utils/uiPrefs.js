/**
 * uiPrefs.js — 用户级 UI 偏好持久化 (纯前端方案, localStorage, 零安装/全浏览器兼容)
 *
 * 用途: 记住用户界面状态 (最后停留栏目/自选组/K线配置/...), 刷新或重开浏览器后恢复。
 *      为将来其它栏目扩展预留统一入口 (如: 智能体上下文记忆、布局偏好、表格列宽)。
 *
 * 关键设计点:
 *  - 两个作用域, 按数据性质选择:
 *    · device 域 (readPrefs/writePrefs): 界面偏好 (排序/视口/布局/栏目记忆),
 *      主键 qd.ui.v1.<clientId>.<module>, 与登录态解耦, 长效第一;
 *      读侧对账号私有键/历史 shared 键兜底并一次性迁移。
 *    · user 域 (readUserPrefs/writeUserPrefs): 登录用户私有数据 (聊天记录/IDE 状态),
 *      键 qd.uiv1.<uid>.<module> (独立命名空间, 杜绝与 device 域 uid 双写键碰撞),
 *      严格按账号隔离不共享, 未登录拒绝读写。
 *  - clientId 是设备级一次性标识 (UUID, 直存 localStorage 永不过期): 命名空间前缀与
 *    登录态/用户id/服务重启全部解耦 —— 这是"设置长效"的根本: 任何导致 userInfo 短暂
 *    缺失的时序 (登出/401/接口挂/USER_INFO 过期被清) 都不会让读写落到不同 key 上。
 *  - 读侧兜底 + 一次性迁移: 主键未命中时依次尝试 账号私有键(<userId>) / 历史 shared 键,
 *    命中即提升写回主键。旧版本数据 (按 userId 或 shared 存的) 不丢, 且只迁移一次。
 *  - 账号私有双写: 登录态就绪时除主键外镜像一份 <userId> 键, 支持同一浏览器多账号隔离;
 *    未登录/登出时读主键, 行为与单人单账号场景一致。
 *  - 自管理滑动TTL: JSON 内嵌 _t 时间戳, 每次写入刷新; 读取超期视为无数据。
 *  - 存储通道: 统一走 store 包 (storages 链 localStorage→cookie→sessionStorage→memory),
 *    不再自建内存降级 —— 进程内对象在页面刷新/服务重启后必然丢失, 那是"设置不长效"的
 *    直接机制; store 链至少能在 localStorage 不可用时落到 cookie/sessionStorage。
 *
 * 易错点:
 *  - localStorage 明文可见, 只存界面偏好, 不要放敏感信息 (密码/密钥等严禁)
 *  - 大对象注意 ~5MB 配额, 超限时写入告警(不再完全静默, 便于排查"设置不保存")
 *  - clientId 用 localStorage 直存(不经 store): store 的 expire 插件对 set(key,val) 不传
 *    过期时间的行为是清除过期登记(永不过期), 正合预期; 但直存更直白, 少一层依赖
 *  - origin (协议+域名+端口) 变化会让 localStorage 换桶: dev server 端口漂移时任何
 *    纯前端方案都救不回, 需固定 devServer 端口 (见 vue.config.js)
 */
import storage from 'store'
import store from '@/store'

const NS = 'qd.ui.v1'
const USER_NS = 'qd.uiv1' // 账号私有域独立命名空间: 杜绝与 device 域 uid 双写键同名碰撞
const VERSION = 1
const MODULE_TTL_MS = 90 * 24 * 60 * 60 * 1000 // 90天滑动: 活跃使用的偏好一直保留

/** 设备级稳定标识的存储键 (直存 localStorage, 无 TTL, 不参与 store 的 JSON 序列化) */
const CLIENT_ID_KEY = 'qd.clientId.v1'
let _clientIdCache = null

function _genClientId () {
  try {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID()
    }
  } catch (e) { /* 非安全上下文等: 走降级 */ }
  return 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10)
}

/**
 * 设备级稳定标识: 同一浏览器恒定, 与登录态/用户id/前后端重启无关。
 * localStorage 不可用时退进程内缓存 —— 至少保证同一次页面会话内前后台一致
 * (比"每次读都落到不同键"强, 页面一刷新仍会丢, 属存储被禁的降级上限)。
 */
function getClientId () {
  if (_clientIdCache) return _clientIdCache
  let cid = null
  try {
    cid = window.localStorage.getItem(CLIENT_ID_KEY)
    if (!cid) {
      cid = _genClientId()
      window.localStorage.setItem(CLIENT_ID_KEY, cid)
    }
  } catch (e) {
    cid = _clientIdCache || _genClientId()
  }
  _clientIdCache = cid
  return cid
}

/** 当前偏好归属用户 (登录后为用户id, 未登录为 shared) —— 仅用于账号私有键, 不作主键 */
export function getPrefsUserId () {
  try {
    const info = store.getters.userInfo || {}
    const id = info.id != null ? info.id : (info.user_id != null ? info.user_id : '')
    return id !== '' ? String(id) : 'shared'
  } catch (e) {
    return 'shared'
  }
}

/** 模块的候选键 (按优先级): 账号私有 → 设备级主键 → 历史 shared */
function _candidateKeys (module) {
  const keys = []
  const uid = getPrefsUserId()
  if (uid && uid !== 'shared') keys.push(`${NS}.${uid}.${module}`)
  keys.push(`${NS}.${getClientId()}.${module}`)
  keys.push(`${NS}.shared.${module}`)
  return keys
}

/** 设备级主键 (写入的唯一权威键) */
function _primaryKey (module) {
  return `${NS}.${getClientId()}.${module}`
}

/** 读整个模块的偏好对象 (损坏/超期/无数据 → 返回 def, 默认 {}) */
export function readPrefs (module, def) {
  const empty = def === undefined ? {} : def
  try {
    const keys = _candidateKeys(module)
    let raw = null
    let hitKey = null
    for (let i = 0; i < keys.length; i++) {
      const v = storage.get(keys[i])
      if (v != null && v !== '') { raw = v; hitKey = keys[i]; break }
    }
    if (raw == null) return empty
    const s = typeof raw === 'string' ? JSON.parse(raw) : raw
    if (!s || typeof s !== 'object' || s._v !== VERSION) return empty
    if (!s._t || Date.now() - s._t > MODULE_TTL_MS) return empty
    // 兜底键命中(账号私有/历史shared): 提升到设备级主键, 完成一次性迁移 ——
    // 此后该模块读写都稳定在主键, 不再受登录态时序影响
    if (hitKey !== _primaryKey(module)) {
      try { storage.set(_primaryKey(module), JSON.stringify(s)) } catch (e) { /* 迁移失败不影响本次读取 */ }
    }
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
    const raw = JSON.stringify(payload)
    // 主键必写; 登录态就绪时双写账号私有键 (多账号隔离; 未登录只写主键)
    storage.set(_primaryKey(module), raw)
    const uid = getPrefsUserId()
    if (uid && uid !== 'shared') {
      storage.set(`${NS}.${uid}.${module}`, raw)
    }
  } catch (e) {
    // 配额/序列化失败: 不再完全静默 —— 一次性告警便于排查"设置不保存" (读侧永远安全)
    console.warn('[uiPrefs] 偏好写入失败:', module, e)
  }
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

/** 清除模块 (登出清理某栏目/版本升级时用): 连兜底键一起清, 避免迁移复活 */
export function clearModule (module) {
  try {
    _candidateKeys(module).forEach(k => storage.remove(k))
  } catch (e) { /* ignore */ }
}

/* ────────────────────────────────────────────────────────────────────────────
 * 账号私有域 (user scope) ── 与 device 域并行的第二个作用域
 *
 * 用途: 仅登录用户可见的数据 (AI 聊天记录 / 指标IDE工作状态等隐私内容)。
 * 与 device 域的关键区别: **不做设备级主键兜底** —— 严格按账号隔离,
 * 不跨账号共享 (device 域的读侧兜底会让第二个账号读到第一个账号的私有数据)。
 * 未登录 (uid='shared') 时: 读返回空, 写静默拒绝 —— 调用方均为登录后产物
 * (页面有 userId 守卫), 不存在"未登录也要存"的场景。
 * 与 device 域共享: _v 版本 / 90天滑动TTL / merge 语义 / store storages 链。
 * ──────────────────────────────────────────────────────────────────────────── */

/** 账号私有域 key (未登录返回 null, 调用方据此拒绝读写) */
function _userKey (module) {
  const uid = getPrefsUserId()
  if (!uid || uid === 'shared') return null
  return `${USER_NS}.${uid}.${module}`
}

/** 读账号私有模块 (未登录/损坏/超期/无数据 → 返回 def, 默认 {}) */
export function readUserPrefs (module, def) {
  const empty = def === undefined ? {} : def
  try {
    const key = _userKey(module)
    if (!key) return empty
    const raw = storage.get(key)
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

/** 写账号私有模块 (merge=true 增量合并, false 整体替换); 未登录静默拒绝 */
export function writeUserPrefs (module, patch, merge = true) {
  try {
    const key = _userKey(module)
    if (!key) return
    const base = merge ? readUserPrefs(module) : {}
    const payload = Object.assign(base, patch, { _v: VERSION, _t: Date.now() })
    storage.set(key, JSON.stringify(payload))
  } catch (e) {
    console.warn('[uiPrefs] 账号私有偏好写入失败:', module, e)
  }
}

/** 清除账号私有模块 (登出时清理隐私数据用) */
export function clearUserPrefs (module) {
  try {
    const key = _userKey(module)
    if (key) storage.remove(key)
  } catch (e) { /* ignore */ }
}
