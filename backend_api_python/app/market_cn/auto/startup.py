#!/usr/bin/env python3
"""startup.py - 自动策略组「配置 / 规则变更 → 启动对账补偿」(auto/startup.py)

背景 (2026-09-23 事故):
  config.json 把 relay3 改成 enabled=false 之后, 库里该策略的 watch_pending / exit_today
  活跃行原地不动, 前端「今日信号」照旧展示僵尸行。旧版唯一的补丁在 monitor.py 第 1 步
  (09-15 事故加的「禁用策略存量 pending 作废」), 但它嵌在开盘窗口 09:25~09:35 内,
  依赖定时任务恰好踩中窗口 —— 周末 / 节假日 / 后端没在该窗口启动就永远漏掉。
  本模块补的是「启动即对齐」, 不替代 monitor 窗口逻辑。

职责:
  1. 计算策略指纹: config.json strategies 段 (enabled / daily_limit / params)
     + strategies/ 下规则文件 (*.py / *.yaml, 不含 __init__.py) 的内容 sha256
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
  - core/ 框架代码变更不在指纹内。框架改动通常伴随整体发版, 真正的守卫是
    market_spec_check / path_parity, 而非重启补算。
  - strategies/__init__.py 排除在规则指纹外 (注册表机制本身属框架层)。
  - 首次运行 (无快照) 同样触发一次后台校准 —— 首次恰是库最可能与代码不一致的时刻。

用法 (启动钩子 —— 由调用方在调度器起 broker 之前调用一次, 幂等):
    from app.market_cn.auto.startup import reconcile_startup
    reconcile_startup()            # 非阻塞; 内部兜底异常, 失败不影响业务启动
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from app.utils.logger import get_logger

logger = get_logger(__name__)

_STATE_TABLE = "qd_auto_strategy_state"
_SNAPSHOT_KEY = "strategy_fingerprint"

# 可被「退休」的状态 —— 见模块 docstring 硬约束, 只有未入场的行
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


def _iter_rule_files():
    """规则文件清单 [(relname, abspath)] —— *.py / *.yaml, 排除 __init__.py 与 __pycache__。"""
    d = _strategies_dir()
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for fn in names:
        if not (fn.endswith(".py") or fn.endswith(".yaml")):
            continue
        if fn == "__init__.py":
            continue
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            out.append((fn, p))
    return out


def fingerprint():
    """计算当前策略指纹。

    Returns:
        dict: {"strategies": {key: {"enabled": bool, "daily_limit": int, "params": dict}},
               "rules": {filename: sha256_32}}
        config / 插件目录双双异常时返回空 dict (调用方应视作「无可比对」并跳过补偿)。
    """
    fp = {"strategies": {}, "rules": {}}
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

    for fn, p in _iter_rule_files():
        fp["rules"][fn] = _sha256_file(p)
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


def _save_snapshot(fp):
    """落盘当前指纹快照 (幂等 upsert)。"""
    from app.utils.db import get_db_connection
    payload = json.dumps({"hash": _fp_hash(fp), "detail": fp},
                         sort_keys=True, ensure_ascii=False, default=str)
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
    return out


# ================================================================
# 补偿动作 A: 停用策略的未入场行 → expired
# ================================================================

def retire_unfilled(keys, reasons=None):
    """作废指定策略「未入场」的活跃行 (watch_pending 且 entry_date IS NULL)。

    Args:
        keys: 策略 key 列表 (应为已被禁用或从 config 移除的 key)
        reasons: {key: 作废原因文案}

    Returns:
        dict: {策略key: 作废行数}
    """
    keys = [k for k in (keys or []) if k]
    if not keys:
        return {}
    from app.utils.db import get_db_connection
    retired = {}
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            for k in keys:
                cur.execute(
                    "SELECT id, code, trade_date FROM qd_dragon_signals "
                    "WHERE strategy = %s AND state = ANY(%s) AND entry_date IS NULL",
                    (k, list(_RETIREABLE_STATES)),
                )
                rows = [dict(r) for r in cur.fetchall()]
                reason = (reasons or {}).get(k) or "策略已停用, 未入场信号作废"
                for r in rows:
                    cur.execute(
                        "UPDATE qd_dragon_signals SET state = %s, updated_at = NOW(), "
                        "extra = extra || %s::jsonb WHERE id = %s",
                        ("expired", json.dumps({"reason": reason}, ensure_ascii=False), r["id"]),
                    )
                retired[k] = len(rows)
            db.commit()
            cur.close()
    except Exception as e:
        logger.warning("[auto_startup] 停用策略行作废失败: %s", e)
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


def _rebuild_worker(why):
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

    try:
        from app.market_cn.auto import rebuild as _rb
        logger.info("[auto_startup] 账本重建开始 (window=%d, %s)", _REBUILD_WINDOW, why)
        t0 = time.time()
        expected, meta = _rb.build_expected(window=_REBUILD_WINDOW)
        if meta.get("error"):
            logger.warning("[auto_startup] 重建跳过: %s", meta["error"])
            return
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
    except Exception as e:
        logger.warning("[auto_startup] 账本重建失败 (不影响启动): %s", e)


def trigger_rebuild(why, background=True):
    """触发「补扫 + 重建校准」。默认后台线程; background=False 时同步 (CLI/调试)。"""
    if background:
        t = threading.Thread(target=_rebuild_worker, args=(why,), daemon=True,
                             name="auto-startup-rebuild")
        t.start()
        return {"mode": "background", "why": why}
    _rebuild_worker(why)
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
              "rebuild": None, "reasons": [], "open_rows_removed": []}

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
        _save_snapshot(now_fp)
        report["changed"] = True
        report["reasons"].append("首次运行: 已落指纹基准, 并触发一次后台重建校准")
        logger.info("[auto_startup] %s", report["reasons"][-1])
        report["rebuild"] = trigger_rebuild("首次运行校准", background=async_)
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
        report["rebuild"] = trigger_rebuild(why, background=async_)
        logger.info("[auto_startup] 检测到 %s → 已触发补扫+重建校准", why)

    _save_snapshot(now_fp)
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
