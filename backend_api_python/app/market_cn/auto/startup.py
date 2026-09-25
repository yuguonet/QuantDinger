#!/usr/bin/env python3
"""startup.py - 自动策略组「配置 / 规则变更 → 启动对账补偿」(auto/startup.py)

背景 (2026-09-23 事故):
  config.json 把 relay3 改成 enabled=false 之后, 库里该策略的 watch_pending / exit_today
  活跃行原地不动, 前端「今日信号」照旧展示僵尸行。旧版唯一的补丁在 monitor.py 第 1 步
  (09-15 事故加的「禁用策略存量 pending 作废」), 但它嵌在开盘窗口 09:25~09:35 内,
  依赖定时任务恰好踩中窗口 —— 周末 / 节假日 / 后端没在该窗口启动就永远漏掉。
  本模块补的是「启动即对齐」, 不替代 monitor 窗口逻辑。

职责:
  1. 计算策略指纹 —— **两段, 边界 = 语义边界**:
       rules   (判定链)  = config.json strategies 段的 enabled/daily_limit/params
                           + 从判定入口 (rebuild/scan/monitor/store/...) 出发做
                             AST import 闭包得到的 .py 清单, 各取内容 sha256。
                           变化 ⇒ 库里是旧规则算出的结果 ⇒ **必须重建**。
       display (展示层)  = strategies/*.yaml (门表) + core/present/*.py
                           + core/display_meta.py, 各取内容 sha256。
                           展示链每次请求实时读 ⇒ 重启即生效, **不重建**。
     为什么按语义边界切: 展示口径与判定契约曾混在同一文件 (base.py), 文件级 hash
     切不开 ⇒ 改一次档位映射就白跑一次全量重建 (实证 2026-09-23 19:33)。
  2. 与上次持久化快照比对, 得出变更集
  3. 按变更类型分流补偿:
     - 策略被禁用 或 从 config 移除 → 该策略「未入场」的活跃行 → expired
     - 策略新启用 / 参数变更 / 规则文件变更 → 后台线程「补扫当日 + 应然集重建校准」
  4. 全程非阻塞: 补偿在 daemon 线程里跑, 任何异常只记日志, 绝不拖累后端启动

★ 为什么补偿要跑 rebuild 而不只是 run_scan (2026-09-23 追加):
  run_scan `as_of=None` **只判末根** ⇒ 规则改了, 窗口内**历史行**仍是旧判定, UI 照旧
  显示旧信号 —— 这就是"代码改了/重启后界面还是老样子"的根因。rebuild 重算窗口内
  全部交易日的应然集并写库校准 (见 rebuild.py: build_plan/apply_plan), 才是真正的对齐。
  两者都跑且顺序固定: 先 run_scan (当日权威写入 + 数据就绪等待 + watchlist/cleanup 收尾),
  后 rebuild (历史校准)。rebuild 只碰未推进行, 不覆盖 run_scan 刚写的当日行。

★ 退休范围硬约束 (2026-09-23 取证, 勿放宽):
  只有「未入场」的行可作废 —— 即 state=watch_pending 且 entry_date IS NULL。
  buy_today / holding / exit_today 一律不得动。理由:
    - monitor 第 2/4/5/6 步只用 strat_reg.get_strategy(row.strategy) 取对象,
      与 enabled 无关; 只有第 1 步 (pending → buy_today) 显式拦 enabled。
    - 故既有设计意图是「停用 = 禁止新增入场, 不打断已入场持仓的生命周期」。
  实证: relay3 两条 exit_today 行 entry_price 非空 (真实持仓, 待卖出), 若被 expired
  → UI 不再提示卖出 → 用户会遗忘手上还有这两只票。这是实盘资金事故, 不是脏数据。

刻意不做的边界:
  - **展示链** (core/present/、core/display_meta.py、strategies/*.yaml、tools/) 不进
    判定指纹: UI 实时读, 重启即生效, 重建是纯浪费。详见 core/display_meta.py 头注实证。
  - 判定闭包里**不可达**的 core 模块 (如 core/runtime/evaluate.py、expr.py —— 只被展示链
    与 tools 引用) 自然不在指纹内; 它们的守卫是 market_spec_check / path_parity。
  - 首次运行 (无快照) 同样触发一次后台校准 —— 首次恰是库最可能与代码不一致的时刻。

★ 快照 = 「这份指纹已经校准过」的凭据 (2026-09-23 修):
  旧实现在触发 rebuild 后**立即**落盘, 而 rebuild 是后台线程 —— 从落盘到跑完有数分钟
  窗口 (实证: 19:33:45 落盘 / 19:38:45 才完成)。窗口内进程被杀、或 worker 命中
  rebuild.py 的 "取数为空" / "无活跃日线策略" 早退, 指纹都已推进 ⇒ 下次启动同指纹跳过
  ⇒ **永久漏补** (最危险的失败方向: 判据的依据本身没兑现)。
  现在指纹只在 worker **校准成功后**推进; 失败则下次启动自动重试。

用法 (启动钩子 —— 由调用方在调度器起 broker 之前调用一次, 幂等):
    from app.market_cn.auto.startup import reconcile_startup
    reconcile_startup()            # 非阻塞; 内部兜底异常, 失败不影响业务启动
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import threading
import time
from datetime import datetime

from app.utils.logger import get_logger

logger = get_logger(__name__)

_STATE_TABLE = "qd_auto_strategy_state"
_SNAPSHOT_KEY = "strategy_fingerprint"

# 可被「退休」的状态 —— 见模块 docstring 硬约束, 只有未入场的行
# 2026-09-26: 唯一实现 = store.retire_unfilled (本常量仅作语义备忘, 不再被 SQL 使用)
_RETIREABLE_STATES = ("watch_pending",)

# 补扫最长等待数据就绪的时间 (后台线程, 不阻塞启动)
_RESCAN_MAX_WAIT_SEC = 1800

_reconcile_lock = threading.Lock()
_reconciled_once = False


# ================================================================
# 指纹计算
# ================================================================

def _strategies_dir():
    """自动策略插件目录 (app/market_cn/auto/strategies)。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategies")


def _sha256_file(path):
    """文件内容 sha256 前 32 位 (够用于变更检测, 不占空间)。"""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:32]
    except OSError:
        return "missing"


# ── 判定链入口: 生产链 (实盘 scan / 盘中 monitor / 启动 rebuild / 查询 store) 的起点 ──
_JUDGE_ENTRIES = (
    "__init__.py", "api.py", "monitor.py", "probe.py", "rebuild.py",
    "registry.py", "scan.py", "sched.py", "store.py", "strategies/base.py",
)

# ── 展示层: 显式排除在判定指纹外 (改了只需重启) ──
#   core/present/        展示链管线 (runtime.evaluate / expr 只被它引用)
#   core/display_meta.py 展示口径映射 (预确认档位归一, 与判定解耦)
#   tools/               诊断脚本 (debug/explain/gate_try/...), 不参与生产链
_JUDGE_EXCLUDE = ("core/present/", "core/display_meta.py", "tools/")

_PKG = "app.market_cn.auto"


def _auto_root():
    """auto/ 包根目录 (本文件所在目录)。"""
    return os.path.dirname(os.path.abspath(__file__))


def _is_excluded(rel):
    rel = rel.replace(os.sep, "/")
    return any(rel.startswith(x) for x in _JUDGE_EXCLUDE)


def _mod_path(mod):
    """`app.market_cn.auto.core.exec` → `core/exec.py`; 非本包 / 文件不存在 → None。"""
    if mod != _PKG and not mod.startswith(_PKG + "."):
        return None
    rel = mod[len(_PKG):].strip(".")
    if not rel:
        return "__init__.py"
    base = os.path.join(_auto_root(), *rel.split("."))
    for cand in (base + ".py", os.path.join(base, "__init__.py")):
        if os.path.isfile(cand):
            return os.path.relpath(cand, _auto_root()).replace(os.sep, "/")
    return None


def _imports_of(path, rel):
    """静态提取文件的 import 目标模块名 (含相对导入)。解析失败返回空集 (由闭包校验兜住)。"""
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except Exception:
        return set()
    out = set()
    cur_dir = os.path.dirname(rel)          # '' | 'strategies' | 'core/runtime'
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add(a.name)
        elif isinstance(n, ast.ImportFrom):
            if n.level:                     # 相对导入: from ..base import x
                parts = cur_dir.split("/") if cur_dir else []
                parts = parts[:max(len(parts) - (n.level - 1), 0)]
                base = _PKG + ("." + ".".join(parts) if parts else "")
            else:
                base = n.module or ""
            if base:
                out.add(base)
                for a in n.names:           # from pkg import sub / from mod import name
                    out.add(base + "." + a.name)
    return out


def _closure_judge_files():
    """AST 闭包: 从判定入口 + strategies/*.py 出发递归收 import。

    为什么动态算而不是写死清单: 写死清单在"新增策略 / 新增 core 模块 / 挪 import"时必漏,
    而漏 = 该重建却不重建 (最危险的失败方向)。闭包每次冷启动重算 (<0.1s), 边界跟随代码。

    Returns:
        [(relpath, abspath)]; 结果不可信时 None (由 _iter_judge_files 回退全扫)。
    """
    try:
        entries = list(_JUDGE_ENTRIES)
        sd = _strategies_dir()
        entries += ["strategies/" + fn for fn in sorted(os.listdir(sd))
                    if fn.endswith(".py") and fn != "__init__.py"]
        seen, queue = set(), []
        for e in entries:
            e = e.replace(os.sep, "/")
            if _is_excluded(e) or not os.path.isfile(os.path.join(_auto_root(), e)):
                continue
            seen.add(e)
            queue.append(e)
        while queue:
            rel = queue.pop()
            for mod in _imports_of(os.path.join(_auto_root(), rel), rel):
                r2 = _mod_path(mod)
                if r2 and r2 not in seen and not _is_excluded(r2):
                    seen.add(r2)
                    queue.append(r2)
        # 安全校验: 关键入口缺一即视为不可信 (宁可回退全扫多跑, 不可漏跑)
        if not {"monitor.py", "rebuild.py", "scan.py", "strategies/base.py"} <= seen:
            return None
        if len(seen) < 15:
            return None
        return [(r, os.path.join(_auto_root(), r)) for r in sorted(seen)]
    except Exception as e:
        logger.warning("[auto_startup] 判定链闭包分析失败: %s", e)
        return None


def _sweep_judge_files():
    """兜底: 扫 auto/ 下全部 .py (排除 _JUDGE_EXCLUDE)。保守 —— 宁可多跑, 不可漏跑。"""
    out = []
    for dp, dn, fn in os.walk(_auto_root()):
        dn[:] = [d for d in dn if d != "__pycache__"]
        for f in fn:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, _auto_root()).replace(os.sep, "/")
            if _is_excluded(rel):
                continue
            out.append((rel, p))
    return sorted(out)


def _iter_judge_files():
    """判定链文件清单 [(relpath, abspath)]: AST 闭包优先, 不可信则全扫。"""
    got = _closure_judge_files()
    if got:
        return got
    logger.warning("[auto_startup] 判定链闭包不可信 → 回退全扫 auto/ (保守)")
    return _sweep_judge_files()


def _iter_display_files():
    """展示层文件清单 [(relpath, abspath)]: strategies/*.yaml + core/present/**.py
    + core/display_meta.py。

    单独成段是为了把「展示变更」与「判定变更」分开: 展示链每次请求实时读, 改了重启即可,
    不需要重跑 rebuild。旧实现把 *.yaml 混在判定指纹里 ⇒ 只改门表也会白跑一次全量重建。
    """
    out = []
    sd = _strategies_dir()
    try:
        for fn in sorted(os.listdir(sd)):
            p = os.path.join(sd, fn)
            if fn.endswith(".yaml") and os.path.isfile(p):
                out.append(("strategies/" + fn, p))
    except OSError:
        pass
    for dp, dn, fn in os.walk(os.path.join(_auto_root(), "core", "present")):
        dn[:] = [d for d in dn if d != "__pycache__"]
        for f in fn:
            if f.endswith(".py"):
                p = os.path.join(dp, f)
                out.append((os.path.relpath(p, _auto_root()).replace(os.sep, "/"), p))
    dm = os.path.join(_auto_root(), "core", "display_meta.py")
    if os.path.isfile(dm):
        out.append(("core/display_meta.py", dm))
    return sorted(out)


def fingerprint():
    """计算当前策略指纹 (三段: strategies=配置 / rules=判定链 / display=展示层)。

    Returns:
        dict: {"strategies": {key: {"enabled": bool, "daily_limit": int, "params": dict}},
               "rules":   {relpath: sha256_32}}   ← 判定链, 变化 ⇒ 必须重建
               "display": {relpath: sha256_32}}   ← 展示层, 变化 ⇒ 重启即可
        config / 插件目录双双异常时 strategies 为空 (调用方应视作「无可比对」并跳过补偿)。
    """
    fp = {"strategies": {}, "rules": {}, "display": {}}
    try:
        from app.market_cn.auto import strategies as strat_reg
        strat_reg.autodiscover()
        cfg = strat_reg.load_config(refresh=True).get("strategies", {}) or {}
        # key 取 config ∪ 注册表并集 —— 新增/移除策略两侧都能感知
        keys = sorted(set(cfg) | set(strat_reg.all_strategies()))
        for k in keys:
            c = cfg.get(k) or {}
            fp["strategies"][k] = {
                "enabled": bool(c.get("enabled", False)),
                "daily_limit": c.get("daily_limit"),
                "params": c.get("params") if isinstance(c.get("params"), dict) else {},
            }
    except Exception as e:
        logger.warning("[auto_startup] 指纹-策略段失败: %s", e)
        fp["strategies"] = {}

    for rel, p in _iter_judge_files():
        fp["rules"][rel] = _sha256_file(p)
    for rel, p in _iter_display_files():
        fp["display"][rel] = _sha256_file(p)
    return fp


def _fp_hash(fp):
    """指纹 → 稳定 hash 串 (用于快速比对, 明细另存便于出报告)。"""
    blob = json.dumps(fp, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ================================================================
# 快照持久化 (幂等 DDL)
# ================================================================

def _ensure_state_table(cur):
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {_STATE_TABLE} (
            k           VARCHAR(64) PRIMARY KEY,
            v           JSONB,
            updated_at  TIMESTAMP DEFAULT NOW()
        )
    """)


def _load_snapshot():
    """读取上次指纹快照; 无/异常返回 None。"""
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            _ensure_state_table(cur)
            db.commit()
            cur.execute(f"SELECT v FROM {_STATE_TABLE} WHERE k = %s", (_SNAPSHOT_KEY,))
            row = cur.fetchone()
            cur.close()
            if not row:
                return None
            v = row["v"] if isinstance(row, dict) else row[0]
            if isinstance(v, str):
                v = json.loads(v)
            return v if isinstance(v, dict) else None
    except Exception as e:
        logger.warning("[auto_startup] 快照读取失败 (按首次运行处理): %s", e)
        return None


def _save_snapshot(fp, calibrated_for=None):
    """落盘指纹快照 (幂等 upsert) —— **只在校准成功后调用**。

    快照的语义是「这份指纹已经校准过了」的凭据, 不是「这份指纹已经见过了」。提前落盘会把
    失败的校准误判为已完成 ⇒ 下次启动指纹相同 ⇒ 跳过 ⇒ 永久漏补 (见模块 docstring ★)。

    Args:
        fp: 本次的指纹 (fingerprint() 的返回)
        calibrated_for: 本次校准覆盖到的目标交易日 (YYYY-MM-DD); 仅作可观测性记录
    """
    from app.utils.db import get_db_connection
    payload = json.dumps({
        "hash": _fp_hash(fp),
        "calibrated_at": datetime.now().isoformat(timespec="seconds"),
        "calibrated_for": calibrated_for,
        "detail": fp,
    }, sort_keys=True, ensure_ascii=False, default=str)
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            _ensure_state_table(cur)
            # 必须显式 RETURNING: db 层游标包装 (app/utils/db_postgres.py) 对
            # "simple INSERT ... VALUES" 会自动 append `RETURNING id`, 而本表主键是
            # k (无 id 列) → UndefinedColumn。已含 RETURNING 时不追加, 故显式声明 k。
            # 同类无 id 表参照 qd_scheduler_done。
            cur.execute(
                f"INSERT INTO {_STATE_TABLE} (k, v, updated_at) VALUES (%s, %s::jsonb, NOW()) "
                f"ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = NOW() "
                f"RETURNING k",
                (_SNAPSHOT_KEY, payload),
            )
            db.commit()
            cur.close()
    except Exception as e:
        logger.warning("[auto_startup] 快照落盘失败 (下次仍会对账): %s", e)


# ================================================================
# 变更检测
# ================================================================

def diff(prev_detail, now_detail):
    """比对两份指纹明细, 得出变更集。

    Returns:
        dict: {"disabled": [...], "removed": [...], "enabled_new": [...],
               "changed": [...], "rules_changed": bool}
    """
    p = (prev_detail or {}).get("strategies", {}) or {}
    n = (now_detail or {}).get("strategies", {}) or {}
    pkeys, nkeys = set(p), set(n)
    out = {
        "disabled": sorted(k for k in (pkeys & nkeys)
                           if p[k].get("enabled") and not n[k].get("enabled")),
        "removed": sorted(pkeys - nkeys),
        "enabled_new": sorted(k for k in (pkeys & nkeys)
                              if not p[k].get("enabled") and n[k].get("enabled")),
        "changed": sorted(k for k in (pkeys & nkeys)
                          if {kk: vv for kk, vv in p[k].items() if kk != "enabled"}
                          != {kk: vv for kk, vv in n[k].items() if kk != "enabled"}),
        "added": sorted(nkeys - pkeys),
    }
    out["rules_changed"] = ((prev_detail or {}).get("rules") or {}) != \
                           ((now_detail or {}).get("rules") or {})
    # 展示层变更 (门表 yaml / core/present / display_meta): 展示链实时读, 重启即生效,
    # **不需要**重建 —— 单独标记只为在日志里与"必须重建"区分开。
    out["display_changed"] = ((prev_detail or {}).get("display") or {}) != \
                             ((now_detail or {}).get("display") or {})
    return out


# ================================================================
# 补偿动作 A: 停用策略的未入场行 → expired
# ================================================================

def retire_unfilled(keys, reasons=None):
    """作废指定策略「未入场」的活跃行 (watch_pending 且 entry_date IS NULL)。

    2026-09-26: SQL 唯一实现已收编至 ``store.retire_unfilled``; 本函数保留原签名作
    薄包装 (startup 只关心 {key: count}, 详细行集见 store)。

    Args:
        keys: 策略 key 列表 (应为已被禁用或从 config 移除的 key)
        reasons: {key: 作废原因文案}

    Returns:
        dict: {策略key: 作废行数}
    """
    keys = [k for k in (keys or []) if k]
    if not keys:
        return {}
    from app.market_cn.auto.store import retire_unfilled as _retire
    rows = _retire(keys=keys, reason_by_key=reasons or {})
    retired = {k: 0 for k in keys}
    for r in rows:
        k = r.get("strategy")
        if k in retired:
            retired[k] += 1
    return retired


def _active_rows_of_removed(keys):
    """仅供诊断: 列出被移除策略仍持有的已入场活跃行 (不动作, 交由 monitor 生命周期处理)。"""
    keys = [k for k in (keys or []) if k]
    if not keys:
        return []
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                "SELECT strategy, code, state, entry_date, entry_price FROM qd_dragon_signals "
                "WHERE strategy = ANY(%s) AND state = ANY(%s)",
                (keys, ["watch_pending", "buy_today", "holding", "exit_today"]),
            )
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
        return rows
    except Exception as e:
        logger.warning("[auto_startup] 活跃行诊断失败: %s", e)
        return []


# ================================================================
# 补偿动作 B: 后台补扫 / 重建校准
# ================================================================

# 重建窗口 (交易日): 略大于 store.cleanup_old 的库保留范围, 保证"补了不会被立刻再删"。
_REBUILD_WINDOW = 20


class _RebuildIncomplete(Exception):
    """重建未完成 (数据未就绪等可恢复原因) —— 用于跳过「推进指纹」那一步。"""


def _rebuild_worker(why, fingerprint=None):
    """后台线程体: 先补扫当日 (幂等), 再跑**账本重放**并写库校准。

    为什么两件都做:
      - run_scan 是实盘口径的当日写入权威 (含 wait_data 等数据就绪), 且带
        sync_watchlist_group / cleanup_old 收尾;
      - rebuild 账本重放覆盖**窗口内全部历史行**, 是 run_scan 做不到的 (它只判当天)。
      - 顺序不可颠倒: 先让当日入库, rebuild 读到的现状才是最新的 (缺失判定才准)。
    为什么是账本重放而不是信号层:
      信号层只产 watch_pending(观察); 状态推进只在 monitor 且**每步锚定"今天"**
      ⇒ 历史行永不推进, 补出来下个交易日开盘就被 expired。要显示 买入/持有/卖出,
      只能自己把状态机重放一遍 (rebuild.replay_ledger)。
    """
    try:
        from app.market_cn.auto.scan import run_scan
        logger.info("[auto_startup] 补扫当日 (%s)", why)
        stat = run_scan(days=320, wait_data=True, max_wait_sec=_RESCAN_MAX_WAIT_SEC)
        logger.info("[auto_startup] 补扫完成 (%s): %s", why, stat)
    except Exception as e:
        logger.warning("[auto_startup] 补扫失败 (不影响重建): %s", e)

    done, calibrated_for = False, None
    try:
        from app.market_cn.auto import rebuild as _rb
        logger.info("[auto_startup] 账本重建开始 (window=%d, %s)", _REBUILD_WINDOW, why)
        t0 = time.time()
        expected, meta = _rb.build_expected(window=_REBUILD_WINDOW)
        if meta.get("error"):
            # ★ 早退路径: 不推进指纹 —— 否则"取数为空"这类可恢复失败会被永久记成已完成
            logger.warning("[auto_startup] 重建跳过: %s (指纹不推进, 下次启动会重试)",
                           meta["error"])
            raise _RebuildIncomplete(meta["error"])
        actual = _rb.load_actual(meta.get("win_dates") or [])
        # 账本重放 (而非信号层): 只有它能把状态推到 买入/持有/卖出。
        # 信号层只产 watch_pending(观察), 且 monitor 不推进历史行 (每步锚定"今天"),
        # 补出来的老信号下个交易日开盘就被 expired 扫掉 —— 修不了"界面还是旧状态"。
        replay, rstat = _rb.replay_ledger(expected, meta,
                                           meta["bars_map"], meta["idx_map"])
        plan = _rb.build_ledger_plan(replay, actual, meta)
        stat = _rb.apply_ledger_plan(plan, dry_run=False)
        logger.info("[auto_startup] 账本重建完成 (%.0fs): %s | 重放分支=%s",
                    time.time() - t0, stat, rstat)
        done = True
        calibrated_for = meta.get("target") or (meta.get("win_dates") or [None])[-1]
    except _RebuildIncomplete:
        pass
    except Exception as e:
        logger.warning("[auto_startup] 账本重建失败 (不影响启动): %s", e)

    # ★ 指纹只在**校准成功后**推进 (见模块 docstring "快照 = 已校准的凭据")。
    #   中途夭折 / 数据未就绪 都保持旧快照 ⇒ 下次启动同指纹仍会重跑, 不会永久漏补。
    if fingerprint is not None:
        if done:
            _save_snapshot(fingerprint, calibrated_for=calibrated_for)
            logger.info("[auto_startup] 校准成功 → 指纹已推进 (校准至 %s); "
                        "下次以同指纹启动将跳过重建", calibrated_for)
        else:
            logger.warning("[auto_startup] 校准未完成 → 指纹**不推进**, 下次启动会重试 "
                           "(why=%s)", why)


def trigger_rebuild(why, background=True, fingerprint=None):
    """触发「补扫 + 重建校准」。默认后台线程; background=False 时同步 (CLI/调试)。

    Args:
        fingerprint: 本次待推进的指纹; **只在校准成功后**由 worker 落盘 (见 _save_snapshot)。
                     None = 不推进 (仅供"只看效果不落账"的调试调用)。
    """
    if background:
        t = threading.Thread(target=_rebuild_worker, args=(why, fingerprint), daemon=True,
                             name="auto-startup-rebuild")
        t.start()
        return {"mode": "background", "why": why}
    _rebuild_worker(why, fingerprint)
    return {"mode": "sync", "why": why}


def _rescan_worker(why):
    """后台线程体: 只补扫当日 (不带历史校准)。保留给"只缺当天数据"的轻量场景。"""
    try:
        from app.market_cn.auto.scan import run_scan
        logger.info("[auto_startup] 触发补扫 (%s)", why)
        stat = run_scan(days=320, wait_data=True, max_wait_sec=_RESCAN_MAX_WAIT_SEC)
        logger.info("[auto_startup] 补扫完成 (%s): %s", why, stat)
    except Exception as e:
        logger.warning("[auto_startup] 补扫失败 (不影响启动): %s", e)


def trigger_rescan(why, background=True):
    """触发补扫。默认后台线程 (不阻塞启动); background=False 时同步执行 (仅供 CLI/调试)。"""
    if background:
        t = threading.Thread(target=_rescan_worker, args=(why,), daemon=True,
                             name="auto-startup-rescan")
        t.start()
        return {"mode": "background", "why": why}
    _rescan_worker(why)
    return {"mode": "sync", "why": why}


# ================================================================
# 主入口
# ================================================================

def reconcile_startup(async_=True, once=True, force=False):
    """启动对账: 检测策略/规则变更并按类型补偿。幂等、非阻塞、异常兜底。

    Args:
        async_: True=补偿放后台线程 (默认); False=同步等完成 (CLI 用)
        once: True=单进程内只执行一次对账 (防重复挂载重复跑)
        force: True=即使指纹无变更也强制校准一次 (用于"库与代码已知不一致"时手动对齐;
               注意对账结尾会落盘当前指纹, 所以正常的变更只会被消费一次)

    Returns:
        dict: 对账报告 {"changed": bool, "diff": {...}, "retired": {...},
                        "rebuild": {...}|None, "reasons": [...]}
    """
    global _reconciled_once
    report = {"changed": False, "diff": None, "retired": {}, "rescan": None,
              "rebuild": None, "snapshot_deferred": False, "reasons": [],
              "open_rows_removed": []}

    if once:
        with _reconcile_lock:
            if _reconciled_once:
                report["reasons"].append("本进程已对账过, 跳过")
                return report
            _reconciled_once = True

    try:
        now_fp = fingerprint()
    except Exception as e:
        logger.warning("[auto_startup] 指纹计算失败, 跳过对账: %s", e)
        report["reasons"].append(f"指纹失败: {e}")
        return report

    if not now_fp.get("strategies"):
        report["reasons"].append("无可比对策略指纹 (config/插件均异常), 跳过补偿")
        logger.warning("[auto_startup] %s", report["reasons"][-1])
        return report

    prev = _load_snapshot()
    if prev is None:
        # 首次运行: 落基准 + 校准一次
        # (原设计只落基准不补偿, 理由是"避免每次发版全量重扫"; 但首次恰恰是库最可能
        #  与代码不一致的时刻 —— 用户正是在反复改策略代码, 所以首次也校准, 后台跑。)
        # ★ 指纹不在这里落盘: 交给 worker 在**校准成功后**推进 —— 否则首次校准失败
        #   (数据未就绪等) 会留下一个"看似已校准"的基准, 后续启动全部跳过。
        report["changed"] = True
        report["reasons"].append("首次运行: 触发一次后台重建校准 (成功后落指纹基准)")
        logger.info("[auto_startup] %s", report["reasons"][-1])
        report["rebuild"] = trigger_rebuild("首次运行校准", background=async_,
                                            fingerprint=now_fp)
        report["snapshot_deferred"] = True
        return report

    if prev.get("hash") == _fp_hash(now_fp) and not force:
        report["reasons"].append("配置与规则无变更")
        return report

    d = diff(prev.get("detail") or {}, now_fp)
    report["changed"] = True
    report["diff"] = d

    # ── A. 停用 / 移除 → 作废未入场行 ──
    retire_keys = sorted(set(d["disabled"]) | set(d["removed"]))
    reasons = {}
    for k in d["disabled"]:
        reasons[k] = f"策略已禁用({k}), 未入场信号作废"
    for k in d["removed"]:
        reasons[k] = f"策略已移出配置({k}), 未入场信号作废"
    if retire_keys:
        report["retired"] = retire_unfilled(retire_keys, reasons)
        logger.info("[auto_startup] 停用/移除 %s → 作废未入场行: %s", retire_keys, report["retired"])
        # 已入场行不作废, 仅提示交由 monitor 生命周期处理 (见模块 docstring 硬约束)
        open_rows = _active_rows_of_removed(retire_keys)
        if open_rows:
            report["open_rows_removed"] = [
                {k2: (str(v2) if v2 is not None else None) for k2, v2 in r.items()}
                for r in open_rows
            ]
            logger.info("[auto_startup] %s 仍有已入场活跃行 %d 条, 保留交由 monitor 生命周期处理",
                        retire_keys, len(open_rows))

    # ── B. 启用/参数/规则变更 → 补扫覆盖 ──
    need_scan = []
    if d["enabled_new"]:
        need_scan.append(f"新启用: {d['enabled_new']}")
    if d["added"]:
        need_scan.append(f"新增策略: {d['added']}")
    if d["changed"]:
        need_scan.append(f"参数/限额变更: {d['changed']}")
    if d["rules_changed"]:
        need_scan.append("规则文件变更")
    if force and not need_scan:
        need_scan.append("强制校准 (force)")
    if need_scan:
        why = "; ".join(need_scan)
        # 用「补扫 + 重建校准」而非只补扫: run_scan 只判当天, 规则改了历史行不会更新,
        # UI 仍是旧信号 —— 这正是"重启后端仍显示旧信号"的根因。rebuild 覆盖窗口内全部行。
        # ★ 指纹延后到校准成功后落盘 (见 _rebuild_worker): 否则 rebuild 中途夭折 / 命中
        #   "取数为空" 早退时指纹也被推进 ⇒ 下次启动跳过 ⇒ 永久漏补。
        report["rebuild"] = trigger_rebuild(why, background=async_, fingerprint=now_fp)
        report["snapshot_deferred"] = True
        logger.info("[auto_startup] 检测到 %s → 已触发补扫+重建校准 (指纹待校准成功后推进)",
                    why)
    else:
        # 无重建需求 → 立即推进指纹。典型: 只改了展示层 (门表 yaml / 展示管线 / 档位映射),
        # 展示链每次请求实时读, 重启即生效, 重建纯属浪费。
        _save_snapshot(now_fp, calibrated_for=(prev or {}).get("calibrated_for"))
        if d.get("display_changed"):
            logger.info("[auto_startup] 仅展示层变更 (门表/展示管线/档位映射) → "
                        "无需重建, 重启即生效; 指纹已推进")
        else:
            logger.info("[auto_startup] 无重建需求 (停用类变更已同步处理) → 指纹已推进")

    return report


if __name__ == "__main__":
    # CLI: python -m app.market_cn.auto.startup [--force]
    #   首次运行落基准并校准一次; 之后检测变更并同步补偿 (--force 无视变更强制校准)
    import argparse
    _ap = argparse.ArgumentParser(description="自动策略组启动对账 (指纹变更 → 重建校准)")
    _ap.add_argument("--force", action="store_true",
                     help="指纹无变更时也强制校准一次 (库与代码已知不一致时用)")
    _a = _ap.parse_args()
    print(json.dumps(reconcile_startup(async_=False, force=_a.force),
                     ensure_ascii=False, indent=2, default=str))
