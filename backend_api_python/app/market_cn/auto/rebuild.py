"""rebuild.py — 应然信号集重建 + 写库校准 (--apply)

用途:
  策略规则或参数变更后, 库里的旧行仍是旧判定 ⇒ 重算最近 M 个交易日的"应然信号集" T,
  与 qd_dragon_signals 现状做双向 diff, 回答三个问题:
    - missing: 按当前规则该有、库里却没有的 (漏发)
    - ghost  : 库里有、按当前规则不该有的 (幽灵/残留)
    - drift  : 两边都有但关键字段漂了的 (score/price/extra)

  `--apply` 把 T 落库 (校准 UI 显示); 不带则只出报告 (影子审计)。

为什么需要它 (起因):
  后端重启后 UI 仍是旧信号; break 两条 buy_today 卡死、relay3 禁用后留僵尸行。
  根因是"实盘只在扫描当天判定一次", 规则改了历史行不会自己更新。

★ 与实盘同源 (缺一层就不是实盘口径):
  完整复刻 scan.run_scan 的判定后链路 ——
    判定 → U1~U4 unified_prefilter → _dedupe_family 同族去重 → daily_limit 截断
  (daily_limit 与 dedupe 都是**按日**做的, 必须先按日聚齐再处理)

★ 写库范围 (build_plan / apply_plan, 见其 docstring):
  唯一门槛 = 只碰「未推进」行 (`state='watch_pending' AND entry_date IS NULL`)。
  已入场行 (buy_today/holding/exit_today/closed/expired) 一律不动 —— 改它们等于伪造
  历史账; 停用策略的已入场行还必须继续可见 (实盘资金安全硬约束, 见 startup.py)。
  ⚠ 不复用 store.upsert_scan_signals: 它 `DO UPDATE SET state=EXCLUDED.state` 会把
  已推进的 holding 打回 watch_pending (丢持仓)。

★ 成本已与 M 解耦:
  g56 走 `scan_days` 批量路径 (一次 f+mask, 逐日 O(1)) 而非逐日 scan_signals (O(n²))。
  池**一次建好** (锚=序列末日) 逐日查 —— 实证 232 个 (板,日) 点 |Δrmed|=|Δscore_r|=0、
  阈值翻转 0; 端到端 103 个 (code,date) 命中逐位一致、提速 96.4x
  (`tmp/_g56_pool_anchor.py`)。

易错点:
  1. 取数 days 必须 = 320 (对齐 scan.run_scan), 短窗口会通过递归指标初值制造漂移
  2. knife_catch / tail_oversold 是 intraday_window 策略, 日线路径扫不到 → 显式跳过并报告
  3. unified_prefilter **必须传 stock_info** (漏传会误杀信号, 已踩过)
  4. 逐日切片用 bisect 取索引, 不要每天重扫 320 根做字符串比较 (5235×30×320 太慢)
  5. 池口径: 本文件传 bars_batch (days=320) 而非实盘的 hub.daily(200) —— P1 实证已覆盖
     320 内任意截断点 Δ=0, 未直接测 200; 若将来怀疑口径漂移, 加 --check-pool 复核

手动运行:
  python -m app.market_cn.auto.rebuild --window 30 [--days 320] [--strategy KEY]
                                       [--limit N] [--out 路径]
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
import time
from collections import defaultdict

from app.utils.logger import get_logger

logger = get_logger(__name__)

try:
    from dotenv import load_dotenv
    for _p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '.env'),
               os.path.join(os.getcwd(), '.env')):
        if os.path.isfile(_p):
            load_dotenv(_p, override=False)
            break
except Exception:
    pass


# ================================================================
# 活跃策略集 (与 run_scan 同一判定: enabled 且 kind=daily_close)
# ================================================================

def _active_strategies(keys=None):
    """{key: strat} — enabled 且日线收盘路径。kind=intraday_window 的策略不在此列。"""
    from app.market_cn.auto import strategies as strat_reg
    strat_reg.autodiscover()
    active = {k: s for k, s in strat_reg.all_strategies().items()
              if strat_reg.is_enabled(k) and s.scan_spec.kind == "daily_close"}
    if keys:
        want = set(keys)
        active = {k: v for k, v in active.items() if k in want}
    return active


def _skipped_intraday(keys=None):
    """被跳过的盘中策略 (日线路径扫不到) — 显式列出, 避免"没信号"被误读成"无异常"。"""
    from app.market_cn.auto import strategies as strat_reg
    strat_reg.autodiscover()
    out = []
    for k, s in strat_reg.all_strategies().items():
        if not strat_reg.is_enabled(k):
            continue
        if keys and k not in set(keys):
            continue
        if s.scan_spec.kind != "daily_close":
            out.append((k, s.scan_spec.kind))
    return out


# ================================================================
# 应然集构建
# ================================================================

def _load_bars(codes, days, progress=True):
    """一次性批量取数 (O(N) 一次, 后续全部切片复用)。返回 {code: bars}。"""
    from app.market_cn.auto.core.data.kline import fetch_kline_db
    out = {}
    t0 = time.time()
    for i, c in enumerate(codes):
        try:
            b = fetch_kline_db(c, days)
        except Exception:
            b = []
        if b and len(b) >= 30:
            out[c] = b
        if progress and (i + 1) % 1000 == 0:
            logger.info("[rebuild] 取数进度 %d/%d (%.0fs)", i + 1, len(codes), time.time() - t0)
    logger.info("[rebuild] 取数完成 %d/%d 票, days=%d, %.0fs",
                len(out), len(codes), days, time.time() - t0)
    return out


def _date_axis(bars_map):
    """交易日轴 (取样本最长的那只为轴; 个股停牌日缺失不影响交易日集合)。"""
    if not bars_map:
        return []
    ref = max(bars_map.values(), key=len)
    return sorted({str(b["time"])[:10] for b in ref})


def _index_of(bars):
    """每票日期 → 切片索引 (bisect 复用, 避免逐日 O(n) 字符串比较)。"""
    return [str(b["time"])[:10] for b in bars]


def build_expected(days=320, window=30, keys=None, limit=None, progress=True):
    """重算最近 window 个交易日的应然信号集。

    返回 (expected, meta):
      expected: {(date, strategy, code): row}  row 与 store.signal_row 同构
      meta    : 诊断信息 (轴/窗口/成本/跳过项)
    """
    from app.market_cn.auto import store
    from app.market_cn.auto.core.filters import unified_prefilter
    from app.market_cn.auto.scan import _anchor_idx, _dedupe_family, _stock_info, all_codes

    t0 = time.time()
    active = _active_strategies(keys)
    if not active:
        return {}, {"error": "无活跃日线策略", "skipped": _skipped_intraday(keys)}

    codes = [c for c in all_codes() if not c.startswith(("8", "4", "92"))]
    if limit:
        codes = codes[:limit]

    bars_map = _load_bars(codes, days, progress)
    if not bars_map:
        return {}, {"error": "取数为空"}
    axis = _date_axis(bars_map)
    if len(axis) < window:
        window = len(axis)
    win = axis[-window:]
    lo, hi = win[0], win[-1]
    logger.info("[rebuild] 交易日轴 %s..%s (%d 天), 窗口 %s..%s (%d 天)",
                axis[0], axis[-1], len(axis), lo, hi, len(win))

    try:
        stock_info = _stock_info()
    except Exception as e:
        logger.warning("[rebuild] stock_basic_info 加载失败(%s), U1~U4 降级", e)
        stock_info = {}

    idx_map = {c: _index_of(b) for c, b in bars_map.items()}

    # ---- 横截面预热 (声明制): 一次建好, 后续逐票 scan_days 命中单槽缓存 ----
    # 策略实现 `prewarm(bars_map, hi_date)` 即可参与, 编排层**不硬编码任何策略 key**。
    # 不预热的后果: 判定依赖全市场聚合量的策略 (g56 的 rmed/score_r) 会每票重建全市场池
    # (5235 票 × 114s)。单槽缓存按 target 命中, 锚统一=hi ⇒ 全批只建一次。
    for key, strat in active.items():
        pw = getattr(strat, "prewarm", None)
        if not callable(pw):
            continue
        tp = time.time()
        try:
            pw(bars_map, hi)
            logger.info("[rebuild] %s 横截面预热完成 (%.1fs)", key, time.time() - tp)
        except Exception as e:
            logger.warning("[rebuild] %s 横截面预热失败(%s) — 该策略可能产出空集", key, e)

    # ---- 判定: 统一调 scan_days(lo, hi), 每票每策略一次, 产出窗口内所有命中日 ----
    # ★ 不给任何策略开专属分支: scan_days 是基类统一契约, 默认实现 = 逐日截断 +
    #   scan_signals (即"语义基准"); 判定昂贵的策略 (g56) 在自己的模块里覆盖它做一次
    #   预计算。编排层对所有 enabled 策略一视同仁, 不因某个策略内部贵就长分支。
    #   反过来若逐日调 scan_signals: g56 的池锚每天变 ⇒ 单槽缓存每票每天重建全市场池。
    per_date = defaultdict(list)         # date -> [(key, Signal)]
    t_scan = time.time()
    for code, bars in bars_map.items():
        for key, strat in active.items():
            try:
                sigs = strat.scan_days(bars, code, lo_date=lo, hi_date=hi)
            except Exception as e:
                logger.debug("[rebuild] %s %s scan_days 异常: %s", code, key, e)
                continue
            for s in sigs or []:
                per_date[str(s.time)[:10]].append((key, s))
    logger.info("[rebuild] 判定完成 %.0fs (统一 scan_days 契约)", time.time() - t_scan)

    # ---- 逐日: U1~U4 → 同族去重 → daily_limit (与 run_scan 同序) ----
    # U1~U4 只对**已产出的信号**切片 (信号数远小于 5235×30), 不为每票每天切一次。
    from app.market_cn.auto import strategies as strat_reg
    expected = {}
    stat = defaultdict(lambda: {"raw": 0, "prefilter": 0, "kept": 0})
    for date in win:
        rows = []
        for key, s in per_date.get(date, []):
            strat = active[key]
            stat[key]["raw"] += 1
            if getattr(strat, "use_unified_prefilter", True):
                sub = bars_map[s.code][:bisect.bisect_right(idx_map[s.code], date)]
                ai = _anchor_idx(sub, s, strat)
                if ai is None:
                    continue
                ok, _ = unified_prefilter(sub, ai, s.code, stock_info.get(s.code))
                if not ok:
                    stat[key]["prefilter"] += 1
                    continue
            name = (stock_info.get(s.code) or {}).get("name", "")
            rows.append(store.signal_row(key, s, name))
            stat[key]["kept"] += 1
        rows = _dedupe_family(rows)
        for key in active:
            grp = [r for r in rows if r["strategy"] == key]
            cap = strat_reg.daily_limit(key)
            if cap and len(grp) > cap:
                grp = sorted(grp, key=lambda r: r["score"], reverse=True)[:cap]
            for r in grp:
                # signal_row 不含 trade_date (它以 signal_date 表达); 写库需要显式 trade_date
                r = dict(r, trade_date=date)
                expected[(date, r["strategy"], r["code"])] = r

    meta = {
        "days": days, "window": len(win), "win_from": lo, "win_to": hi,
        "win_dates": win, "limit": limit,
        "codes": len(bars_map), "strategies": sorted(active),
        "path": "scan_days 统一契约 (无策略专属分支)",
        "skipped": _skipped_intraday(keys),
        "n_expected": len(expected),
        "stat": {k: dict(v) for k, v in stat.items()},
        "elapsed_sec": round(time.time() - t0, 1),
        # 账本重放要复用这份上下文 (取数一次, 所有策略/所有日共享);
        # 落 json 时须剔除 (体积大且不可序列化友好)。
        "bars_map": bars_map,
        "idx_map": idx_map,
    }
    return expected, meta


# ================================================================
# 现状 / diff
# ================================================================

def load_actual(win, keys=None):
    """从库里读窗口内的现状行 → {(date, strategy, code): row}。"""
    from app.market_cn.auto import store
    wset = set(win)
    out = {}
    for r in store.list_signals(days=len(win) + 10, strategies=list(keys) if keys else None):
        d = str(r.get("trade_date"))[:10]
        if d in wset:
            out[(d, r.get("strategy"), r.get("code"))] = r
    return out


def _drift_fields(e, a):
    """关键字段漂移 (score/price/lu_date/pullback_days)。"""
    out = []
    if int(a.get("score") or 0) != int(e.get("score") or 0):
        out.append(("score", a.get("score"), e.get("score")))
    ap, ep = a.get("signal_price"), e.get("signal_price")
    if (ap is None) != (ep is None) or (
            ap is not None and ep is not None and abs(float(ap) - float(ep)) > 1e-6):
        out.append(("signal_price", ap, ep))
    for f in ("lu_date", "pullback_days"):
        av, ev = str(a.get(f) or ""), str(e.get(f) or "")
        if av != ev:
            out.append((f, av, ev))
    return out


def diff(expected, actual):
    """双向 diff → dict(missing/ghost/drift)。"""
    ek, ak = set(expected), set(actual)
    missing = sorted(ek - ak)
    ghost = sorted(ak - ek)
    drift = []
    for k in sorted(ek & ak):
        f = _drift_fields(expected[k], actual[k])
        if f:
            drift.append((k, f))
    return {"missing": missing, "ghost": ghost, "drift": drift}


# ================================================================
# 写库计划 (--apply): 校准 qd_dragon_signals 到应然集
# ================================================================
#
# 为什么不能用 store.upsert_scan_signals:
#   它 `ON CONFLICT ... DO UPDATE SET state = EXCLUDED.state`, 会把已推进的
#   holding / exit_today 行**打回 watch_pending** ⇒ 丢持仓。历史重建必须专用逻辑。
#
# ★ 唯一的写入门槛: 只碰「未推进」行 = `state='watch_pending' AND entry_date IS NULL`。
#   已入场行 (buy_today / holding / exit_today / closed / expired) 一律不动 ——
#   改它们等于伪造历史账; 停用策略的已入场行还必须继续可见 (实盘资金安全硬约束,
#   见 startup.py 模块 docstring)。

# 终态 (组内不显示, 留历史) —— 不在 ACTIVE_GROUP_STATES 里
_TERMINAL = ("closed", "expired")


def build_plan(expected, actual, meta):
    """应然集 vs 现状 → 可执行写库计划。

    Args:
        expected: {(date, strategy, code): row}  应然集
        actual:   {(date, strategy, code): row}  库现状 (窗口内)
        meta:     build_expected 的 meta

    Returns:
        dict:
          disabled_sweep: [{strategy, code, trade_date, id}]  停用策略未入场行 (全表)
          insert : [row]                    补写 (窗口内 missing, 且未被 cleanup 清理过)
          expire : [(id, key)]              作废 (未推进 ghost)
          fix    : [(id, key, fields)]      修正 (未推进行字段漂移)
          keep_settled / keep_purged: [...] 跳过项 (已推进历史账 / 补了也会被再删)
    """
    from app.market_cn.auto import registry
    from app.market_cn.auto.store import S_WATCH_PENDING, cleanup_cutoff

    d = diff(expected, actual)
    keep_from = cleanup_cutoff(DB_KEEP_DAYS)

    # ---- A. 停用策略清扫 (全表, 不依赖窗口) ----
    # 停用 = 不再提名新买入; 但已入场行保留 (由 monitor 走完生命周期)。
    active_keys = set(meta.get("strategies") or [])
    all_keys = set(registry.strategy_keys())
    disabled = sorted(all_keys - set(registry.enabled_keys()))
    disabled_sweep = []
    if disabled:
        from app.utils.db import get_db_connection
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                cur.execute(
                    "SELECT id, strategy, code, trade_date FROM qd_dragon_signals "
                    "WHERE strategy = ANY(%s) AND state = %s AND entry_date IS NULL",
                    (disabled, S_WATCH_PENDING),
                )
                disabled_sweep = [_row_plain(r) for r in cur.fetchall()]
                cur.close()
        except Exception as e:
            logger.warning("[rebuild] 停用策略清扫查询失败: %s", e)

    # ---- B. 窗口内 missing / ghost / drift ----
    def _settled(row):
        """已推进 (有入场日) 或已入终态 ⇒ 历史账, 一律不动。"""
        return bool(row.get("entry_date")) or row.get("state") in _TERMINAL

    insert, expire, fix, keep_settled, keep_purged = [], [], [], [], []
    for k in d["missing"]:
        row = expected[k]
        if k[1] not in active_keys:
            keep_purged.append((k, "策略已停用"))       # 由 A 段清扫负责
        elif k[0] < keep_from:
            keep_purged.append((k, "早于 cleanup 边界, 补了也会再删"))
        else:
            insert.append(row)

    for k in d["ghost"]:
        a = actual.get(k) or {}
        if k[1] not in active_keys:
            keep_settled.append((k, a.get("state"), "策略已停用"))
        elif _settled(a):
            keep_settled.append((k, a.get("state"), "已推进/终态 (历史账)"))
        elif a.get("state") == S_WATCH_PENDING and not a.get("entry_date"):
            expire.append((a.get("id"), k))
        else:
            keep_settled.append((k, a.get("state"), "非未推进态, 保守跳过"))

    for k, fields in d["drift"]:
        a = actual.get(k) or {}
        if k[1] not in active_keys:
            continue
        if a.get("state") == S_WATCH_PENDING and not a.get("entry_date"):
            fix.append((a.get("id"), k, fields))
        else:
            keep_settled.append((k, a.get("state"), "drift 但已推进, 不改历史"))

    return {
        "disabled_sweep": disabled_sweep,
        "insert": insert,
        "expire": expire,
        "fix": fix,
        "keep_settled": keep_settled,
        "keep_purged": keep_purged,
        "keep_from": keep_from,
        "disabled": disabled,
        "n_expected": len(expected),
        "n_actual": len(actual),
    }


def _row_plain(r):
    """游标行 → 普通 dict (本项目 cursor 返回 dict 行, 统一取 values 语义)。"""
    return {k: (str(v)[:10] if hasattr(v, "isoformat") else v)
            for k, v in dict(r).items()}


def apply_plan(plan, dry_run=True):
    """执行写库计划。dry_run=True 时只统计不落库。

    Returns:
        dict: {inserted, expired, fixed, sweep_expired, skipped, dry_run}
    """
    from app.market_cn.auto.store import S_EXPIRED, S_WATCH_PENDING
    stat = {"inserted": 0, "expired": 0, "fixed": 0, "sweep_expired": 0,
            "insert_skipped": 0, "dry_run": bool(dry_run)}

    if dry_run:
        stat.update({
            "inserted": len(plan["insert"]),
            "expired": len(plan["expire"]),
            "fixed": len(plan["fix"]),
            "sweep_expired": len(plan["disabled_sweep"]),
        })
        return stat

    from app.utils.db import get_db_connection
    import json as _json

    with get_db_connection() as db:
        cur = db.cursor()

        # ---- A. 停用策略未入场行 → expired (全表) ----
        for r in plan["disabled_sweep"]:
            cur.execute(
                "UPDATE qd_dragon_signals SET state = %s, updated_at = NOW(), "
                "extra = extra || %s::jsonb "
                "WHERE id = %s AND state = %s AND entry_date IS NULL",
                (S_EXPIRED,
                 _json.dumps({"reason": "策略已停用, 未入场信号作废"},
                             ensure_ascii=False),
                 r["id"], S_WATCH_PENDING),
            )
            stat["sweep_expired"] += cur.rowcount

        # ---- B. 补写 missing → watch_pending ----
        # ON CONFLICT DO NOTHING (不是 DO UPDATE): 竞赛窗口里若已有行, 绝不覆盖其 state
        # (覆盖=把 holding 打回 watch_pending, 即 upsert_scan_signals 的病)。
        for r in plan["insert"]:
            extra = {k: v for k, v in (r.get("extra") or {}).items() if v is not None}
            cur.execute(
                "INSERT INTO qd_dragon_signals "
                "(trade_date, strategy, code, name, board, entry_style, score, state, "
                " signal_date, signal_price, lu_date, pullback_days, extra, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb, NOW()) "
                "ON CONFLICT (trade_date, strategy, code, entry_style) DO NOTHING",
                (r["trade_date"], r["strategy"], r["code"], r.get("name", ""),
                 r.get("board", ""), r.get("style", "a"), int(r.get("score") or 0),
                 S_WATCH_PENDING, r.get("signal_date"), r.get("signal_price"),
                 r.get("lu_date"), r.get("pullback_days"),
                 _json.dumps(extra, ensure_ascii=False, default=str)),
            )
            if cur.rowcount:
                stat["inserted"] += 1
            else:
                stat["insert_skipped"] += 1

        # ---- C. 未推进 ghost → expired ----
        for sid, _k in plan["expire"]:
            cur.execute(
                "UPDATE qd_dragon_signals SET state = %s, updated_at = NOW(), "
                "extra = extra || %s::jsonb "
                "WHERE id = %s AND state = %s AND entry_date IS NULL",
                (S_EXPIRED,
                 _json.dumps({"reason": "重建校准: 当前规则下不成立, 作废"},
                             ensure_ascii=False),
                 sid, S_WATCH_PENDING),
            )
            stat["expired"] += cur.rowcount

        # ---- D. 未推进行字段修正 ----
        for sid, _k, fields in plan["fix"]:
            sets, vals = [], []
            for name, _old, new in fields:
                sets.append("%s = %%s" % name)
                vals.append(new)
            if not sets:
                continue
            vals.append(sid)
            cur.execute(
                "UPDATE qd_dragon_signals SET %s, updated_at = NOW() "
                "WHERE id = %%s AND state = %%s AND entry_date IS NULL"
                % ", ".join(sets),
                vals + [S_WATCH_PENDING],
            )
            stat["fixed"] += cur.rowcount

        db.commit()
        cur.close()

    # 收尾与 run_scan 同序 (展示投影 + 历史清理), 幂等
    try:
        from app.market_cn.auto import store as _st
        _st.sync_watchlist_group(_st.get_active_signals())
        stat["cleanup"] = _st.cleanup_old(days=DB_KEEP_DAYS)
    except Exception as e:
        logger.warning("[rebuild] 收尾 (watchlist/cleanup) 失败: %s", e)
    return stat


# ================================================================
# 账本重放 (--ledger): 信号 → 入场 → 确认 → 出场, 产出完整终态
# ================================================================
#
# 为什么必须做 (2026-09-23 用户追问暴露):
#   信号层重建只产 watch_pending(观察), 而**状态推进只在 monitor, 且每步锚定"今天"**:
#     step1  pending→buy_today: cand 只取 `trade_date == _last_trade_day()`,
#            更早的 pending **一律 expired**("隔日未处理,过期")
#     step5  buy_today→holding: 只认 `entry_date == today`
#   ⇒ 补出来的历史行**永远不会被推进**, 停在"观察", 且下个交易日开盘就被扫掉。
#   要显示"持有/卖出", 只能自己把状态机重放一遍。
#
# 与实盘同源 (复用生产判定函数, 不重写任何规则):
#   1. 入场 = monitor step1 的 `s_obj.entry_decision(row, snap)`
#      snap 由 D1 日线合成: {"open": D1开盘, "previousClose": D0收盘, "last": D1开盘}
#   2. 确认 = monitor step5 的 `s_obj.confirm_decision(row, {"series":[...]})`
#      series 由 D1 bar 合成 (实测各策略只取 series[-1]["last"] 与 any(x["high"]))
#   3. 出场 = monitor step4 的 `s_obj.exit_decision(row, snap_day_close)`
#      逐日推进 (snap 的 bars 截至当日), 首次 action=="exit" 即出场
#
# ⚠ 口径限制 (诚实声明, 报告里输出):
#   - 日线重放**无法复现盘中动作**: monitor step2 (盘中硬止损) 与 step3 (14:30 预确认)
#     需要分钟快照, 本重放不覆盖 ⇒ 出场时点由日线判定决定, 可能比实盘更晚/更早
#   - D1 竞价用开盘价合成, 实盘用 9:26 实时价 (通常接近但不总等)
#   - confirm 的 series 只有 1 个点 ⇒ 依赖"日内序列形状"的规则 (如 relay3 封板需 high)
#     用 D1 的 high 近似

REPLAY_WINDOW = 20      # 账本重放默认窗口 (交易日)


def replay_ledger(expected, meta, bars_map, idx_map):
    """对每个应然信号重放「入场 → 确认 → 出场」, 产出带完整生命周期字段的行。

    Returns:
        (rows, stat): rows = {(date, key, code): row}; stat = 各分支计数
    """
    active = _active_strategies(meta.get("strategies"))
    axis = meta.get("win_dates") or []
    last_date = axis[-1] if axis else None
    rows, stat = {}, defaultdict(int)

    for k, row in expected.items():
        date, key, code = k
        strat = active.get(key)
        bars = bars_map.get(code) or []
        times = idx_map.get(code) or []
        if strat is None or not bars:
            rows[k] = dict(row, state="watch_pending")
            stat["无策略/无数据"] += 1
            continue
        try:
            d0 = times.index(date)
        except ValueError:
            continue
        if d0 + 1 >= len(bars):
            rows[k] = dict(row, state="watch_pending")
            stat["待次日确认(末根)"] += 1
            continue

        d1 = d0 + 1
        b0, b1 = bars[d0], bars[d1]
        open_px = float(b1.get("open") or 0)
        prev_close = float(b0.get("close") or 0)
        if open_px <= 0 or prev_close <= 0:
            rows[k] = dict(row, state="expired",
                           extra=dict(row.get("extra") or {}, replay_reason="D1开盘/前收缺失"))
            stat["入场被拒(数据缺失)"] += 1
            continue
        # entry_gap 是 v1 confirm 的输入 (实盘由 monitor step1 写入 detail)
        gap = round((open_px / prev_close - 1) * 100, 2)
        ex = dict(row.get("extra") or {}, entry_gap=gap)

        dec = strat.entry_decision(dict(row, extra=ex),
                                   {"open": open_px, "last": open_px,
                                    "previousClose": prev_close})
        if dec is None or not dec.buyable:
            rows[k] = dict(row, state="expired",
                           extra=dict(ex, replay_reason=(dec.reason if dec else "entry=None")))
            stat["入场被拒(gap越界)"] += 1
            continue

        entry_price = round(open_px, 3)
        entry_date = times[d1]
        r = dict(row, state="holding", extra=ex, entry_date=entry_date,
                 entry_price=entry_price, stop_price=strat.initial_stop(code, entry_price))

        # ---- D1 收盘确认 (monitor step5) ----
        series = [{"last": b1.get("close"), "high": b1.get("high"), "open": b1.get("open"),
                   "previousClose": prev_close}]
        try:
            cdec = strat.confirm_decision(r, {"series": series})
        except Exception:
            cdec = None
        if cdec is not None and not cdec.confirmed:
            # 不确认 ⇒ monitor 转 exit_today (D1 收盘价) ⇒ 次日平账成 closed (日线口径即当日了结)
            ep = getattr(cdec, "exit_price", None) or b1.get("close")
            r.update(state="closed", confirm_date=entry_date, exit_date=entry_date,
                     exit_reason=cdec.reason,
                     exit_price=round(float(ep), 3) if ep else None)
            rows[k] = r
            stat["确认未过→当日平仓"] += 1
            continue

        # ---- 逐日出场重放 (monitor step4, 首次 exit 即出场) ----
        hit = None
        for j in range(d1, len(bars)):
            try:
                edec = strat.exit_decision(r, {"mode": "day_close",
                                               "bars": bars[:j + 1], "entry_idx": d1})
            except Exception:
                edec = None
            if edec is not None and edec.action == "exit":
                hit = (j, edec)
                break
        if hit is None:
            stat["持有中"] += 1                       # 未触发出场 ⇒ 仍持仓
        else:
            j, edec = hit
            ex_date = times[j]
            r.update(exit_date=ex_date,
                     exit_price=round(float(edec.price), 3) if edec.price else None,
                     exit_reason=edec.reason)
            # 出场日=最新交易日 ⇒ 待执行平账(exit_today); 更早 ⇒ 已平仓(closed)
            r["state"] = "exit_today" if ex_date == last_date else "closed"
            stat["已出场"] += 1
        rows[k] = r

    stat["合计"] = len(rows)
    return rows, dict(stat)


# 账本写库涉及的字段 (UPDATE 用)
_LEDGER_COLS = ("state", "entry_date", "entry_price", "stop_price",
                "exit_date", "exit_price", "exit_reason", "confirm_date", "extra")


def build_ledger_plan(replay, actual, meta):
    """账本重放 → 写库计划 (完全以重放为准)。

    窗口内**启用策略**的库落以重放集为唯一真相源:
      upsert : 重放有 & 库有 → UPDATE 全部生命周期字段 (**含改写已推进行**, 用户已裁定)
      insert : 重放有 & 库无 → INSERT (落重放终态, 不是 watch_pending)
      expire : 重放无 & 库有 → 该行不应存在 → expired
    停用策略: 未入场行 → expired; 已入场行**保留** (历史账, 不属本次校准范围)

    Returns:
        dict: {upsert, insert, expire, disabled_sweep, keep_disabled, vanish_settled, ...}
    """
    from app.market_cn.auto import registry
    from app.market_cn.auto.store import S_WATCH_PENDING

    active_keys = set(meta.get("strategies") or [])
    disabled = sorted(set(registry.strategy_keys()) - set(registry.enabled_keys()))

    upsert, insert, expire, keep_disabled, vanish_settled = [], [], [], [], []
    for k, r in replay.items():
        a = actual.get(k)
        if a is None:
            insert.append(r)
        else:
            upsert.append((a.get("id"), k, r))
            if a.get("entry_date") and r.get("state") != a.get("state"):
                vanish_settled.append((k, a.get("state"), r.get("state")))
    for k, a in actual.items():
        if k in replay:
            continue
        if k[1] not in active_keys:
            keep_disabled.append((k, a.get("state")))
        else:
            expire.append((a.get("id"), k, a.get("state"), bool(a.get("entry_date"))))

    # 停用策略未入场行清扫 (全表, 不依赖窗口)
    disabled_sweep = []
    if disabled:
        from app.utils.db import get_db_connection
        try:
            with get_db_connection() as db:
                cur = db.cursor()
                cur.execute(
                    "SELECT id, strategy, code, trade_date FROM qd_dragon_signals "
                    "WHERE strategy = ANY(%s) AND state = %s AND entry_date IS NULL",
                    (disabled, S_WATCH_PENDING))
                disabled_sweep = [_row_plain(r) for r in cur.fetchall()]
                cur.close()
        except Exception as e:
            logger.warning("[rebuild] 停用策略清扫查询失败: %s", e)

    return {"upsert": upsert, "insert": insert, "expire": expire,
            "disabled_sweep": disabled_sweep, "keep_disabled": keep_disabled,
            "vanish_settled": vanish_settled, "disabled": disabled,
            "n_replay": len(replay), "n_actual": len(actual)}


def apply_ledger_plan(plan, dry_run=True):
    """执行账本写库计划。only 索引 0/1/2 (id,key,row) 形态见 build_ledger_plan。"""
    from app.market_cn.auto.store import S_EXPIRED, S_WATCH_PENDING
    stat = {"upserted": 0, "inserted": 0, "expired": 0, "sweep_expired": 0,
            "insert_skipped": 0, "dry_run": bool(dry_run)}
    if dry_run:
        stat.update({"upserted": len(plan["upsert"]), "inserted": len(plan["insert"]),
                     "expired": len(plan["expire"]),
                     "sweep_expired": len(plan["disabled_sweep"])})
        return stat

    from app.utils.db import get_db_connection
    import json as _json
    from app.market_cn.auto.store import _row_to_dict  # noqa: F401

    with get_db_connection() as db:
        cur = db.cursor()

        for r in plan["disabled_sweep"]:
            cur.execute(
                "UPDATE qd_dragon_signals SET state = %s, updated_at = NOW(), "
                "extra = extra || %s::jsonb "
                "WHERE id = %s AND state = %s AND entry_date IS NULL",
                (S_EXPIRED, _json.dumps({"reason": "策略已停用, 未入场信号作废"},
                                        ensure_ascii=False),
                 r["id"], S_WATCH_PENDING))
            stat["sweep_expired"] += cur.rowcount

        # ---- INSERT (落重放终态) ----
        for r in plan["insert"]:
            extra = {k: v for k, v in (r.get("extra") or {}).items() if v is not None}
            extra["replayed_at"] = _now_str()
            cur.execute(
                "INSERT INTO qd_dragon_signals "
                "(trade_date, strategy, code, name, board, entry_style, score, state, "
                " signal_date, signal_price, lu_date, pullback_days, extra, "
                " entry_date, entry_price, stop_price, exit_date, exit_price, "
                " exit_reason, confirm_date, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,"
                "        %s,%s,%s,%s,%s,%s,%s, NOW()) "
                "ON CONFLICT (trade_date, strategy, code, entry_style) DO NOTHING",
                (r["trade_date"], r["strategy"], r["code"], r.get("name", ""),
                 r.get("board", ""), r.get("style", "a"), int(r.get("score") or 0),
                 r["state"], r.get("signal_date"), r.get("signal_price"),
                 r.get("lu_date"), r.get("pullback_days"),
                 _json.dumps(extra, ensure_ascii=False, default=str),
                 r.get("entry_date"), r.get("entry_price"), r.get("stop_price"),
                 r.get("exit_date"), r.get("exit_price"), r.get("exit_reason"),
                 r.get("confirm_date")))
            if cur.rowcount:
                stat["inserted"] += 1
            else:
                stat["insert_skipped"] += 1

        # ---- UPDATE (完全以重放为准; extra 合并以保留真实审计字段) ----
        for sid, _k, r in plan["upsert"]:
            if sid is None:
                continue
            extra = {k: v for k, v in (r.get("extra") or {}).items() if v is not None}
            extra["replayed_at"] = _now_str()
            cur.execute(
                "UPDATE qd_dragon_signals SET "
                "state = %s, entry_date = %s, entry_price = %s, stop_price = %s, "
                "exit_date = %s, exit_price = %s, exit_reason = %s, confirm_date = %s, "
                "score = %s, signal_price = COALESCE(%s, signal_price), "
                "extra = extra || %s::jsonb, updated_at = NOW() "
                "WHERE id = %s",
                (r["state"], r.get("entry_date"), r.get("entry_price"), r.get("stop_price"),
                 r.get("exit_date"), r.get("exit_price"), r.get("exit_reason"),
                 r.get("confirm_date"), int(r.get("score") or 0), r.get("signal_price"),
                 _json.dumps(extra, ensure_ascii=False, default=str), sid))
            stat["upserted"] += cur.rowcount

        # ---- 重放集外的行 → expired (该行在当前规则下不成立) ----
        for sid, _k, _st, settled in plan["expire"]:
            if sid is None:
                continue
            cur.execute(
                "UPDATE qd_dragon_signals SET state = %s, updated_at = NOW(), "
                "extra = extra || %s::jsonb WHERE id = %s",
                (S_EXPIRED,
                 _json.dumps({"reason": "账本重放: 当前规则下不成立, 作废"},
                             ensure_ascii=False),
                 sid))
            stat["expired"] += cur.rowcount

        db.commit()
        cur.close()

    try:
        from app.market_cn.auto import store as _st
        _st.sync_watchlist_group(_st.get_active_signals())
        stat["cleanup"] = _st.cleanup_old(days=DB_KEEP_DAYS)
    except Exception as e:
        logger.warning("[rebuild] 收尾 (watchlist/cleanup) 失败: %s", e)
    return stat


def _now_str():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def render_ledger(plan, stat, replay_stat):
    L = []
    p = L.append
    p("")
    p("=" * 72)
    p("账本重放%s" % ("预演 (dry-run, 未落库)" if stat.get("dry_run") else "写库结果"))
    p("=" * 72)
    p("  重放分支: %s" % ", ".join("%s=%s" % (a, b) for a, b in sorted(replay_stat.items())))
    p("  重放集 %d 行   库现状 %d 行" % (plan["n_replay"], plan["n_actual"]))
    p("")
    p("  改写/更新 (upsert) = %d" % stat.get("upserted", 0))
    p("  新增 (insert)      = %d   (跳过 %d: 冲突已存在)"
      % (stat.get("inserted", 0), stat.get("insert_skipped", 0)))
    p("  作废 (重放集外)    = %d   (启用策略: 当前规则下不成立)" % stat.get("expired", 0))
    p("  停用策略未入场作废 = %d" % stat.get("sweep_expired", 0))
    p("  保留 (停用策略已入场) = %d" % len(plan.get("keep_disabled") or []))
    if stat.get("cleanup"):
        p("  收尾 cleanup_old    = %s" % stat["cleanup"])
    vs = plan.get("vanish_settled") or []
    if vs:
        p("")
        p("  ⚠ 已推进行状态被改写 = %d 条 (完全以重放为准的后果, 最多列 20):" % len(vs))
        for k, old, new in vs[:20]:
            p("     %s %-14s %s : %s → %s" % (k[0], k[1], k[2], old, new))
        if len(vs) > 20:
            p("     ... 其余 %d 条" % (len(vs) - 20))
    return "\n".join(L)


def render_apply(plan, stat):
    L = []
    p = L.append
    p("")
    p("=" * 72)
    p("写库%s" % ("预演 (dry-run, 未落库)" if stat.get("dry_run") else "结果"))
    p("=" * 72)
    p("  停用策略未入场行 作废 = %d   (策略: %s)"
      % (stat.get("sweep_expired", 0), ", ".join(plan.get("disabled") or []) or "无"))
    p("  补写 missing       = %d   (跳过 %d: 冲突已存在)"
      % (stat.get("inserted", 0), stat.get("insert_skipped", 0)))
    p("  作废未推进 ghost   = %d" % stat.get("expired", 0))
    p("  修正未推进行字段   = %d" % stat.get("fixed", 0))
    p("  跳过·已推进历史账  = %d" % len(plan.get("keep_settled") or []))
    p("  跳过·早于清理边界  = %d   (边界 %s)" % (len(plan.get("keep_purged") or []),
                                              plan.get("keep_from")))
    if stat.get("cleanup"):
        p("  收尾 cleanup_old   = %s" % stat["cleanup"])
    return "\n".join(L)


# ================================================================
# 报告 / CLI
# ================================================================

DB_KEEP_DAYS = 15
# 库内行的保留窗口: scan.run_scan 每轮结尾调 `store.cleanup_old(days=15)` **物理删除**
# 更早的行 (只保留 holding 等活跃态)。⇒ 窗口开得比 15 大时, missing 里绝大部分是
# "已被清理"而非"漏发"。不分开列就会被误读成系统漏了 69 条信号。


def render(meta, d, expected, actual, top=25):
    # 未在推进中的状态 = 还没买入 (这类行才是真正可以修正的"幽灵")
    from app.market_cn.auto.store import S_BUY_TODAY, S_WATCH_PENDING
    UNSETTLED = {S_WATCH_PENDING, S_BUY_TODAY}

    L = []
    p = L.append
    p("=" * 72)
    p("应然信号集重建 · 影子审计 (dry-run, 未写库)")
    p("=" * 72)
    if meta.get("error"):
        p("ERROR: %s" % meta["error"])
        return "\n".join(L)
    if meta.get("limit"):
        p("⚠ --limit=%s: 样本被截断, 横截面策略 (g56) 的池只由这 %s 票聚合 ⇒ "
          "rmed/score_r 与实盘(全市场)不同, 其判定结果**不可信**。"
          % (meta["limit"], meta["codes"]))
    p("取数 days=%d  窗口 %s..%s (%d 个交易日)  样本 %d 票  用时 %.0fs"
      % (meta["days"], meta["win_from"], meta["win_to"], meta["window"],
         meta["codes"], meta["elapsed_sec"]))
    p("策略: %s   [%s]" % (", ".join(meta["strategies"]), meta.get("path", "")))
    if meta["skipped"]:
        p("  ⚠ 跳过 (非日线路径, 本重建扫不到): %s"
          % ", ".join("%s(%s)" % kv for kv in meta["skipped"]))
    p("")
    p("--- 判定漏斗 (raw → 被U1~U4拒 → 保留) ---")
    for k, v in sorted(meta["stat"].items()):
        p("  %-18s raw=%-6d prefilter拒=%-6d kept=%d" % (k, v["raw"], v["prefilter"], v["kept"]))
    p("")
    # ghost 三分: 未推进(可修) / 已推进(历史账, 不该动) / 策略已不启用(残留)
    act_keys = set(meta["strategies"])
    g_open, g_settled, g_disabled = [], [], []
    for k in d["ghost"]:
        st = (actual.get(k) or {}).get("state", "")
        if k[1] not in act_keys:
            g_disabled.append(k)
        elif st in UNSETTLED:
            g_open.append(k)
        else:
            g_settled.append(k)

    p("--- 结论 ---")
    p("  应然集 T = %d 行   现状 = %d 行" % (meta["n_expected"], len(actual)))
    p("  missing (该有却没有)          = %d" % len(d["missing"]))
    p("  ghost   未推进(可修)          = %d" % len(g_open))
    p("  ghost   已推进(历史账,别动)   = %d" % len(g_settled))
    p("  ghost   策略已不启用(残留)    = %d" % len(g_disabled))
    p("  drift   (字段漂移)            = %d" % len(d["drift"]))

    def _block(title, items, show_state=False):
        p("")
        p("--- %s (最多列 %d 条) ---" % (title, top))
        if not items:
            p("  (无)")
        for k in items[:top]:
            r = expected.get(k) or actual.get(k)
            st = (actual.get(k) or {}).get("state", "-")
            p("  %s %-16s %s  score=%s price=%s%s"
              % (k[0], k[1], k[2], r.get("score"), r.get("signal_price"),
                 ("  state=%s" % st) if show_state else ""))
        if len(items) > top:
            p("  ... 其余 %d 条" % (len(items) - top))

    # missing 二分: 库保留窗口内的才是真漏发; 更早的是被 cleanup_old 物理删掉的 (预期)
    # ⚠ 按**日历日**算, 不能取 win_dates[-15]: cleanup_old 是 `trade_date >= CURRENT_DATE - 15`
    # (日历日), 而交易日比日历日稀疏 ⇒ 取交易日索引会把窗口算宽, 把已被清理的行误标成漏发。
    from datetime import date, timedelta
    wd = meta.get("win_dates") or []
    keep_from = str(date.today() - timedelta(days=DB_KEEP_DAYS))
    m_recent = [k for k in d["missing"] if k[0] >= keep_from]
    m_purged = [k for k in d["missing"] if k[0] < keep_from]

    _block("MISSING·真漏发 (库保留窗口 %s..%s 内该有却没有 — 这才是要修的)"
           % (keep_from, wd[-1] if wd else ""), m_recent)
    _block("MISSING·已被清理 (早于 %s; cleanup_old(days=%d) 物理删过 — 预期, 非漏发)"
           % (keep_from, DB_KEEP_DAYS), m_purged)
    _block("GHOST·未推进 (库里有、规则说不该有 — 真正可修的)", g_open, True)
    _block("GHOST·已推进 (历史账: 已买入/平仓/过期, 不属本次修复范围)", g_settled, True)
    _block("GHOST·策略已不启用 (配置关停后的残留行)", g_disabled, True)

    p("")
    p("--- DRIFT (最多列 %d 条) ---" % top)
    if not d["drift"]:
        p("  (无)")
    for k, f in d["drift"][:top]:
        p("  %s %-14s %s : %s" % (k[0], k[1], k[2],
                                  "; ".join("%s 库=%s→应然=%s" % x for x in f)))
    if len(d["drift"]) > top:
        p("  ... 其余 %d 条" % (len(d["drift"]) - top))
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="应然信号集重建 / 账本重放")
    ap.add_argument("--window", type=int, default=None,
                    help="重建最近 N 个交易日 (默认: 信号层 30 / 账本重放 %d)" % REPLAY_WINDOW)
    ap.add_argument("--days", type=int, default=320, help="取数窗口 (对齐 run_scan, 默认320)")
    ap.add_argument("--strategy", action="append", default=None,
                    help="只跑指定策略 (可多次)")
    ap.add_argument("--limit", type=int, default=None, help="只取前 N 只票 (调试用)")
    ap.add_argument("--out", default=None, help="报告落盘路径")
    ap.add_argument("--json", default=None, help="结构化结果落盘路径 (json)")
    ap.add_argument("--apply", action="store_true",
                    help="写库校准 (默认只 dry-run 出报告)")
    ap.add_argument("--ledger", action="store_true",
                    help="账本重放模式: 信号→入场→确认→出场重放完整终态 "
                         "(可产出 买入/持有/卖出, 而非只有 观察)。"
                         "写库语义=完全以重放为准, 会改写已推进行")
    a = ap.parse_args(argv)
    window = a.window or (REPLAY_WINDOW if a.ledger else 30)

    expected, meta = build_expected(days=a.days, window=window,
                                    keys=a.strategy, limit=a.limit)
    if meta.get("error"):
        print(render(meta, {"missing": [], "ghost": [], "drift": []}, {}, {}))
        return 1
    actual = load_actual(meta.get("win_dates") or [], keys=a.strategy)
    d = diff(expected, actual)
    txt = render(meta, d, expected, actual)

    if a.ledger:
        replay, rstat = replay_ledger(expected, meta, meta["bars_map"], meta["idx_map"])
        plan = build_ledger_plan(replay, actual, meta)
        stat = apply_ledger_plan(plan, dry_run=not a.apply)
        txt += "\n" + render_ledger(plan, stat, rstat)
    else:
        plan = build_plan(expected, actual, meta)
        stat = apply_plan(plan, dry_run=not a.apply)
        txt += "\n" + render_apply(plan, stat)
    if a.apply:
        # 写库后立刻清一次"过期瞬时标记" (pre_confirm/pre_ts/pre_reason 只在当日买入行有效)。
        # monitor 每 tick 也会清, 但那要等首次 tick; 这里收尾让"重启后端即校准"立即可见。
        from app.market_cn.auto import store as _st
        from datetime import datetime as _dt
        _n = _st.purge_stale_detail(("pre_confirm", "pre_ts", "pre_reason"), _st.S_BUY_TODAY,
                                    _dt.now().strftime("%Y-%m-%d"))
        txt += "\n清理过期预判标记(pre_confirm/pre_ts/pre_reason): %d 行" % _n
    print(txt)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(txt + "\n")
        print("\n报告已落盘: %s" % a.out)
    if a.json and not a.ledger:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({
                "meta": {k: v for k, v in meta.items()
                         if k not in ("bars_map", "idx_map")},
                "missing": ["%s|%s|%s" % k for k in d["missing"]],
                "ghost": ["%s|%s|%s" % k for k in d["ghost"]],
                "drift": [{"key": "%s|%s|%s" % k,
                           "fields": [{"f": x[0], "db": str(x[1]), "exp": str(x[2])}
                                      for x in f]} for k, f in d["drift"]],
            }, fh, ensure_ascii=False, indent=2, default=str)
        print("结构化结果已落盘: %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
