"""tools/projection_shadow.py — P5-② 影子对账：Record 投影 vs 现表。

定位（改进方案 §3.5/§3.7 切口 7）：
  P5 链路切换 = 「认定 Record 为判定事实源 + signals 表降为物化投影」的操作。
  切换**不可一步到位** —— 本工具是切换前的影子期：读 `core/present/runner` 的切片
  （每 (策略,票) 的 events 流水），按 §2.7 投影成规则行，与 `qd_dragon_signals`
  现状做**双向 diff**，观察 N 个交易日 0 不一致才允许切 writer。

★ 与 rebuild 的关系（不重复实现）：
  `rebuild` 的 expected 来自**重算规则**（scan_days）；本工具的 expected 来自
  **Record 投影**。二者是同一集合的两种事实源 ⇒ diff/plan/apply 完全共用
  （`rebuild.diff` / `build_plan` / `apply_plan`）。本文件只做三件事：
    ① 从切片根读出 Record → `store.load_projection`
    ② 覆盖度护栏（见下）
    ③ 报告

★★ 覆盖度护栏（本工具存在的核心理由）：
  投影是「全量再生」语义 —— 切片里没有的票/日 ⇒ expected 里没有 ⇒ 库内行被判 ghost
  ⇒ `--apply` 时**批量作废真实信号**。切片覆盖不全（影子期刚开始跑、某策略未接折叠、
  根目录选错）正是最常见状态，且它以「ghost 一堆」的形式出现，与「规则真的变了」
  长得一模一样。⇒ 按策略核算「库内未推进行被投影命中的比例」，低于阈值（默认 50%）
  直接拒绝 apply，并在报告顶部打出 DANGER。

★★ **影子期不该等时间（2026-10-07 修正）**：
  投影与生产判定都是「bars + 规则」的**纯函数** —— 同一份 bars、同一版规则，任意历史日
  都能重算出同一条判定 ⇒ 「跑够 10 个交易日」只是把同一件事重复了 N 遍，是仪式不是证据。
  ⇒ 用 `--replay` 一次历史回放证完（见 `replay_ready_days`），比的是**判定**（P5 唯一
  可能改坏的东西）；后处理链（U1~U4/去重/限额）两侧共用同一份代码，不在比对面。

★★ **两级证据（P5-③ 门禁「signals 零差异」）**：
  ① **判定日集合**：某 (策略,票) 在窗口内哪些日出 ready —— 两侧双向差为 0；
  ② **规则列逐字段**（2026-10-07 加）：重合日上比 `signal_row`(生产 Signal) vs
     `rule_row_core`(投影 ready payload)。只比①会漏掉 payload 分叉（score/price/extra
     不同但日期相同 ⇒ signals 表长相不同）。暖机头内的差与①同口径单列，不计入退出码。

用法:
    # ★ 回放对账（首选；不需要等时间，也不需要库里有历史数据）
    python -m app.market_cn.auto.tools.projection_shadow --replay --days 320 --window 60
    # 与**库现状**对账（看切片 vs 现表；窗口须 ≤ 库保留期）
    python -m app.market_cn.auto.tools.projection_shadow --root <切片根> --window 15
    # 复核通过后落库（只碰未推进行；操作列原地保留）
    python -m app.market_cn.auto.tools.projection_shadow --root <切片根> --apply

退出码: 0=一致(或 apply 成功) / 1=有差异或护栏拦截（影子期未过）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from app.market_cn.auto import rebuild, store
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: 对账窗口（交易日）。**必须 ≤ 库的保留期**：`store.cleanup_old(days=15)` 每轮物理
#: 删除 `trade_date < cutoff(15)` 的行 ⇒ 窗口开成 30 时，第 16~30 天在库里**本来
#: 就不存在**，投影侧却有 ⇒ 每天都报一串 missing，影子期永远过不去（假差异）。
DEFAULT_WINDOW = 15
#: 未推进行投影覆盖率硬下界。低于此 = 切片覆盖面不足 ⇒ 拒绝 apply（见文件头 ★★）。
MIN_COVERAGE = 0.5
#: 回放对账的默认窗口（交易日）。比 DB 差异模式的 15 大：① 回放不依赖库保留期；
#: ② ≥25 才能让 g56 的滚动台账（ROLL=20）在窗口内热起来，否则窗口开头的差异是暖机假象。
REPLAY_WINDOW = 60
#: 回放对账时剔掉窗口开头的交易日数（= g56 横截面台账的滚动窗 ROLL=20）。
#: 台账是**增量**喂出来的，批量 `scan_days` 用的是锚在窗口末日的整池 ⇒ 开头几天两侧
#: 必然不同。把这段单列，避免把暖机假象当成「投影写错了」。
WARM_SKIP = 20


def _name_map():
    """{code: 证券简称} —— Record 里**没有** name（它是写库侧从 stock_info 补的展示列），
    投影必须由调用方补齐；否则 `--apply` 的 insert 会把 name 写成空串
    （`rebuild.apply_plan` 用 `r.get("name", "")`）。

    取数失败只告警不阻断：`name` 不参与 `rebuild.diff` 的字段比对，不影响影子期判定；
    但会让新插入的行缺简称 —— 故**不静默**。
    """
    try:
        from app.market_cn.auto.core.data.hub import stock_info
        return {c: (v or {}).get("name", "") for c, v in (stock_info() or {}).items()}
    except Exception as e:                              # noqa: BLE001
        logger.warning("[投影影子] 证券简称加载失败(%s) ⇒ 投影行 name 为空", e)
        return {}


def load_expected(root, keys=None):
    """切片根 → (expected, axis)。expected 与 rebuild.build_expected 同构。"""
    if not root:
        raise SystemExit("[projection_shadow] 必须给 --root（切片根目录），不猜测路径")
    if not os.path.isdir(root):
        raise SystemExit(f"[projection_shadow] 切片根不存在: {root}")
    expected = store.load_projection(root, strategies=keys, names=_name_map())
    axis = sorted({k[0] for k in expected})
    return expected, axis


def coverage_report(expected, actual, keys):
    """按策略核算「库内未推进行」被投影命中的比例（护栏核心，见文件头 ★★）。

    Returns:
        {strategy: {"pending": n, "covered": n, "ratio": float}}；pending=0 的策略不列。
    """
    exp_keys = set(expected)
    per = {}
    for k, a in actual.items():
        if a.get("state") != store.S_WATCH_PENDING or a.get("entry_date"):
            continue                      # 只看未推进行（唯一会被写的一类）
        r = per.setdefault(k[1], {"pending": 0, "covered": 0})
        r["pending"] += 1
        if k in exp_keys:
            r["covered"] += 1
    for _key, r in per.items():
        r["ratio"] = (r["covered"] / r["pending"]) if r["pending"] else 1.0
    return per


def prune_insert(plan):
    """投影专属裁剪：**只 INSERT 投影 state=watch_pending 的行**。

    ⚠ 这是投影相对 rebuild 的**唯一**额外语义（rebuild 的 expected 只产 watch_pending，
    故无此问题）：`rebuild.apply_plan` 的 B 段硬写 `state=watch_pending`，若把投影里
    state=holding/closed 的「库外历史链行」原样喂进去，就会**凭空造出一条观察票** ——
    已入场/终态是实盘资金事实，投影不得无中生有（§2.7 操作事实归库）。

    ghost/expire/fix 三段**不改**（基于完整 expected 判定）：过滤 expected 会反过来
    把「库内 watch_pending 但投影链已推进」的行误判 ghost ⇒ 误作废真实信号。

    Returns:
        (plan', dropped) —— dropped = 被裁下的 missing 行（报告可见，不静默）。
    """
    kept, dropped = [], []
    for row in (plan.get("insert") or []):
        if (row.get("state") or store.S_WATCH_PENDING) == store.S_WATCH_PENDING:
            kept.append(row)
        else:
            dropped.append(row)
    out = dict(plan)
    out["insert"] = kept
    out["not_inserted"] = dropped
    return out, dropped


def _render(stat, expected, actual, d, plan, cover, top=25):
    out = []
    out.append(f"[投影影子] 切片投影 {len(expected)} 行 / 库现状 {len(actual)} 行")
    out.append(f"[投影影子] missing={len(d['missing'])} ghost={len(d['ghost'])} "
               f"drift={len(d['drift'])}")

    bad = {k: v for k, v in cover.items() if v["ratio"] < MIN_COVERAGE}
    if bad:
        out.append("")
        out.append("!! DANGER 切片覆盖度不足（投影集不全 ⇒ ghost 是假的，禁止 apply）:")
        for k, v in sorted(bad.items()):
            out.append(f"   {k}: 未推进行 {v['pending']} 条, 投影命中 {v['covered']} "
                       f"(覆盖 {v['ratio']:.0%})")
    if cover:
        out.append("")
        out.append("覆盖度（未推进行）:")
        for k, v in sorted(cover.items()):
            out.append(f"   {k}: {v['covered']}/{v['pending']} ({v['ratio']:.0%})")

    for title, items, show in (("missing (该有而库无)", d["missing"], True),
                               ("ghost (库有而投影无)", d["ghost"], True)):
        if not items:
            continue
        out.append("")
        out.append(f"{title} 前 {min(top, len(items))}/{len(items)}:")
        for k in items[:top]:
            row = (expected if title.startswith("missing") else actual).get(k) or {}
            extra = f" state={row.get('state')}" if show else ""
            out.append(f"   {k[0]} {k[1]} {k[2]}{extra}")
    if d["drift"]:
        out.append("")
        out.append(f"drift (字段漂移) 前 {min(top, len(d['drift']))}/{len(d['drift'])}:")
        for k, fields in d["drift"][:top]:
            out.append(f"   {k[0]} {k[1]} {k[2]}: " +
                       ", ".join(f"{n}:{a}→{e}" for n, a, e in fields))
    out.append("")
    out.append(f"[投影影子] 写库计划: insert={len(plan.get('insert') or [])} "
               f"expire={len(plan.get('expire') or [])} fix={len(plan.get('fix') or [])} "
               f"keep_settled={len(plan.get('keep_settled') or [])}")
    if plan.get("not_inserted"):
        out.append(f"[投影影子] 裁下 {len(plan['not_inserted'])} 条「库外历史链行」"
                   f"（投影 state≠watch_pending，不补写；属 cleanup 正常结果）")
    if stat:
        out.append(f"[投影影子] apply: {json.dumps(stat, ensure_ascii=False)}")
    return "\n".join(out)


def _hold_spans(rec):
    """从事件流水推出**持仓区间** [(entry_date, exit_date|None)]（None = 窗口末仍持仓）。

    用途：给「只生产有」的判定日**归因** —— `Record` 走 stateful，持仓期 `evaluate` 只产
    exit 不产 ready（`_fold_one` 的 prev=rec.current）⇒ 落在区间内的生产信号日会被投影
    抑制。归因不上的残余必须单独列出（否则「已知口径差」会变成万能挡箭牌）。
    """
    spans, cur = [], None
    for e in (rec.events or []):
        st, d = e.get("stage"), str(e.get("date"))[:10]
        pl = e.get("payload") or {}
        if st == "exec":
            cur = str(pl.get("entry_date") or d)[:10]
            if "exit_price" in pl:                       # 两段链（knife/tail）：当日闭合
                spans.append((cur, d))
                cur = None
        elif st == "exit" and cur is not None:
            spans.append((cur, str(pl.get("exit_date") or d)[:10]))
            cur = None
    if cur is not None:
        spans.append((cur, None))
    return spans


def _in_position(spans, day):
    return any(a <= day and (b is None or day <= b) for a, b in spans)


def _jv(v):
    """字段值的可比形态（dict/list 顺序无关；None 与缺键同视）。"""
    return json.dumps(v, sort_keys=True, default=str)


def replay_ready_days(days=320, window=60, keys=None, limit=None, root=None,
                      progress=True):
    """★★ **历史回放**影子对账 —— 替代「等 10 个交易日」。

    为什么不该等时间: 投影与生产判定都是「bars + 规则」的**纯函数** —— 给定同一份 bars
    与同一版规则，任意历史日都能重算出同一条判定。⇒ 「影子期」不需要真实时间流逝，
    用一次历史回放就能证完；靠日历天数建立的"证据"只是把同一件事重复了 N 遍。

    比什么: **判定**（P5 唯一可能改坏的东西）= 某 (策略, 票) 在窗口内**哪些日出 ready**。
    后处理链（U1~U4 预筛 → 同族去重 → daily_limit）P5 不动、两侧共用同一份代码 ⇒ 不进
    比对面（比它只会掩盖真正的分歧）。

    两侧来源:
      生产 = `strat.scan_days(bars, code, lo..hi)`（**唯一**判定入口；g56 在自己的模块里
             覆盖它做一次预计算，故先 `prewarm`，与 rebuild 同序）
      投影 = 冷切片回放 `present_daily.persist_days(win)` 后读 `Record.events` 的 ready 日
             （⚠ 取**全部** ready 日，不是 `project_record` 只取的首条 —— 首条只是"当前
             生命周期位置"，会把同一票多次出信号的日子漏掉）

    Returns:
        {"win": (lo, hi), "window": n, "root": root, "codes": n,
         "per_strategy": {key: {"codes": .., "prod_days": .., "proj_days": ..,
                                "both": .., "only_prod": .., "only_proj": ..,
                                "examples_*": [...]}},
         "errors": [...], "skipped": [...], "total": {...}}
    """
    import tempfile

    from app.market_cn.auto import present_daily, store
    from app.market_cn.auto.scan import all_codes

    active = rebuild._active_strategies(keys)
    if not active:
        raise SystemExit("[projection_shadow] 无活跃日线策略（keys 写错 / 全停用？）")

    codes = [c for c in all_codes() if not c.startswith(("8", "4", "92"))]
    if limit and len(codes) > limit:
        # **均匀抽样**，不是"前 N 票"：code 有序 ⇒ 前 N 全是 000xxx（深主板），样本有偏。
        step = len(codes) / limit
        codes = [codes[int(i * step)] for i in range(limit)]
    bars_map = rebuild._load_bars(codes, days, progress)
    if not bars_map:
        raise SystemExit("[projection_shadow] 回放取数为空")
    axis = rebuild._date_axis(bars_map)
    win = axis[-window:]
    lo, hi = win[0], win[-1]
    win_set = set(win)
    logger.info("[回放影子] 轴 %s..%s (%d 天) → 窗口 %s..%s (%d 天), %d 票, 策略 %s",
                axis[0], axis[-1], len(axis), lo, hi, len(win), len(bars_map),
                sorted(active))

    # ---- 横截面预热（与 rebuild.build_expected 同序，否则 g56 每票重建全市场池）----
    for key, strat in active.items():
        pw = getattr(strat, "prewarm", None)
        if callable(pw):
            try:
                pw(bars_map, hi)
            except Exception as e:                          # noqa: BLE001
                logger.warning("[回放影子] %s 预热失败(%s) — 该策略可能产空集", key, e)

    # ---- ① 生产侧: 判定 (唯一入口 scan_days) ----
    # 保留 Signal 本体（不只是日期）⇒ 下游可做**规则列逐字段**比对（P5-③ 门禁
    # 「signals 零差异」的行级版本；只比日期集合会漏掉 payload 分叉）。
    prod: dict[tuple, dict] = {}
    errors = []
    for code, bars in bars_map.items():
        for key, strat in active.items():
            try:
                sigs = strat.scan_days(bars, code, lo_date=lo, hi_date=hi) or []
            except Exception as e:                          # noqa: BLE001 - 单票不拖垮全局
                errors.append(f"{key}:{code} {type(e).__name__}: {e}")
                continue
            if sigs:
                prod[(key, code)] = {str(s.time)[:10]: s for s in sigs}

    # ---- ② 投影侧: 冷切片回放 (warmup=0 —— 窗口之前的 bars 本身就是暖机) ----
    root = root or tempfile.mkdtemp(prefix="shadow_replay_")
    st = present_daily.persist_days(active, win, bars_map, root=root, warmup=0,
                                    logger_=logger)
    if st.get("errors"):
        errors.extend(f"persist: {x}" for x in st["errors"])
    recs = store.load_records(root, list(active))
    proj: dict[tuple, dict] = {}
    spans: dict[tuple, list] = {}
    for key, rs in recs.items():
        for code, rec in rs.items():
            ev = {str(e.get("date"))[:10]: e for e in (rec.events or [])
                  if e.get("stage") == "ready"}
            days_ = {d: e for d, e in ev.items() if d in win_set}
            if days_:
                proj[(key, code)] = days_
            spans[(key, code)] = _hold_spans(rec)

    # ---- ③ 按策略对账 (判定日集合的双向差) ----
    # `ex_warm`：剔除窗口开头 WARM_SKIP 个交易日后的合计。g56 的横截面台账滚动窗
    #   ROLL=20 ⇒ 回放窗口开头那几天台账是冷的（而批量 scan_days 用的是锚在 hi 的整池）
    #   ⇒ 这批差异是**暖机假象**，必须与真分歧分开报，否则一天到晚在追假问题。
    per = {}
    tot = {"codes": 0, "prod_days": 0, "proj_days": 0, "both": 0,
           "only_prod": 0, "only_proj": 0, "attr_in_position": 0,
           "row_compared": 0, "row_mismatch": 0}
    tot_x = {"only_prod": 0, "only_proj": 0, "attr_in_position": 0,
             "row_mismatch": 0, "skip_days": min(WARM_SKIP, len(win))}
    warm_cut = win[min(WARM_SKIP, len(win) - 1)]
    for key in active:
        pk = {k: v for k, v in prod.items() if k[0] == key}
        jk = {k: v for k, v in proj.items() if k[0] == key}
        r = {"codes": len(set(pk) | set(jk)), "prod_days": sum(len(v) for v in pk.values()),
             "proj_days": sum(len(v) for v in jk.values()), "both": 0,
             "only_prod": 0, "only_proj": 0, "only_prod_ex_warm": 0, "only_proj_ex_warm": 0,
             "attr_in_position": 0, "unattributed": [],
             "row_compared": 0, "row_mismatch": 0, "row_mismatch_ex_warm": 0,
             "row_examples": [], "row_fields": {},
             "by_date_prod_only": {}, "by_date_proj_only": {},
             "examples_prod_only": [], "examples_proj_only": []}
        for k in sorted(set(pk) | set(jk)):
            a, b = set(pk.get(k) or ()), set(jk.get(k) or ())
            r["both"] += len(a & b)
            op, oj = sorted(a - b), sorted(b - a)
            r["only_prod"] += len(op)
            r["only_proj"] += len(oj)
            r["only_prod_ex_warm"] += sum(1 for d in op if d >= warm_cut)
            r["only_proj_ex_warm"] += sum(1 for d in oj if d >= warm_cut)
            sp = spans.get(k) or []
            for d in op:
                r["by_date_prod_only"][d] = r["by_date_prod_only"].get(d, 0) + 1
                if _in_position(sp, d):
                    r["attr_in_position"] += 1
                    r["attr_in_position_ex_warm"] = (
                        r.get("attr_in_position_ex_warm", 0) + (1 if d >= warm_cut else 0))
                elif len(r["unattributed"]) < 8:
                    r["unattributed"].append((k[1], d, sp[:2]))
            for d in oj:
                r["by_date_proj_only"][d] = r["by_date_proj_only"].get(d, 0) + 1
            if op and len(r["examples_prod_only"]) < 5:
                r["examples_prod_only"].append((k[1], op[:4]))
            if oj and len(r["examples_proj_only"]) < 5:
                r["examples_proj_only"].append((k[1], oj[:4]))
            # ---- ★ P5-③ 门禁的行级版本: 重合日上比**规则列逐字段** ----
            # 只比「哪些日出 ready」是不够的 —— payload(score/price/extra) 分叉同样会
            # 让 signals 表长相不同, 而日期集合对得上。两侧共用 `store.rule_row_core`
            # 这一份映射 ⇒ 此处若有差, 差只可能来自「事件本身不同」。
            ps, js = pk.get(k) or {}, jk.get(k) or {}
            for d in sorted(a & b):
                p_row = store.signal_row(key, ps[d], "")
                pl = (js[d].get("payload") or {})
                j_row = store.rule_row_core(key, k[1], "", pl.get("price"),
                                            pl.get("score"), pl.get("extra"))
                j_row["signal_date"] = d
                r["row_compared"] += 1
                bad = sorted(f for f in set(p_row) | set(j_row)
                             if _jv(p_row.get(f)) != _jv(j_row.get(f)))
                if bad:
                    r["row_mismatch"] += 1
                    for f in bad:
                        r["row_fields"][f] = r["row_fields"].get(f, 0) + 1
                    if d >= warm_cut:
                        r["row_mismatch_ex_warm"] += 1
                    if len(r["row_examples"]) < 5:
                        r["row_examples"].append(
                            (k[1], d, bad, {f: p_row.get(f) for f in bad},
                             {f: j_row.get(f) for f in bad}))
        per[key] = r
        for f in tot:
            tot[f] += r[f]
        tot_x["only_prod"] += r["only_prod_ex_warm"]
        tot_x["only_proj"] += r["only_proj_ex_warm"]
        tot_x["attr_in_position"] += r.get("attr_in_position_ex_warm", 0)
        tot_x["row_mismatch"] += r["row_mismatch_ex_warm"]
    return {"win": (lo, hi), "window": len(win), "root": root, "bars": len(bars_map),
            "warm_cut": warm_cut, "per_strategy": per, "total": tot,
            "total_ex_warm": tot_x, "errors": errors,
            "row_parity": {"compared": tot["row_compared"],
                           "mismatch": tot["row_mismatch"],
                           "mismatch_ex_warm": tot_x["row_mismatch"],
                           "fields": {f: sum(r["row_fields"].get(f, 0) for r in per.values())
                                      for f in {f for r in per.values()
                                                for f in r["row_fields"]}},
                           "examples": [e for r in per.values()
                                        for e in r["row_examples"]][:8]},
            # 两类别名登记，不静默：① 无折叠契约（切片落不下）② 盘中策略（日线路径扫不到）
            "skipped_no_fold": st.get("skipped") or [],
            "skipped_intraday": rebuild._skipped_intraday(keys)}


def _render_replay(res, top=5):
    out = []
    lo, hi = res["win"]
    out.append(f"[回放影子] 窗口 {lo}..{hi} ({res['window']} 交易日), "
               f"{res['bars']} 票, 切片根 {res['root']}")
    out.append("[回放影子] 判定日集合对账（生产 scan_days vs 投影 Record.ready）:")
    out.append("  策略                    生产日  投影日   重合  只生产  只投影")
    for key, r in sorted(res["per_strategy"].items()):
        out.append(f"  {key:<22} {r['prod_days']:>6} {r['proj_days']:>7} "
                   f"{r['both']:>6} {r['only_prod']:>7} {r['only_proj']:>7}")
    t = res["total"]
    out.append(f"  {'合计':<22} {t['prod_days']:>6} {t['proj_days']:>7} "
               f"{t['both']:>6} {t['only_prod']:>7} {t['only_proj']:>7}")
    tx = res.get("total_ex_warm") or {}
    if tx:
        out.append(f"[回放影子] 剔除暖机头 {tx['skip_days']} 日（< {res['warm_cut']}）："
                   f"只生产 {tx['only_prod']} / 只投影 {tx['only_proj']}  ← 这批才算「真分歧」")
        out.append(f"[回放影子] 其中「只生产」归因: 持仓期抑制(已知口径差) "
                   f"{tx.get('attr_in_position', 0)} 条 / "
                   f"**未归因 {tx['only_prod'] - tx.get('attr_in_position', 0)} 条**"
                   "  ← 未归因才是要查的新问题")
    rp = res.get("row_parity") or {}
    if rp:
        # ★ P5-③ 门禁: 重合日上比规则列（只比日期集合会漏掉 payload 分叉）
        out.append(f"[回放影子] 重合日**规则列逐字段**: 比对 {rp.get('compared', 0)} 条, "
                   f"不一致 {rp.get('mismatch', 0)} 条 "
                   f"（剔暖机后 {rp.get('mismatch_ex_warm', 0)}）"
                   f"  ← 这才是「signals 零差异」的行级口径"
                   + (f"; 字段分布 {rp.get('fields')}" if rp.get("fields") else ""))
        for e in (rp.get("examples") or [])[:3]:
            out.append(f"   规则列分叉例: {e[0]} {e[1]} 字段={e[2]} 生产={e[3]} 投影={e[4]}")
    for key, r in sorted(res["per_strategy"].items()):
        ep, ej = r.get("only_prod_ex_warm", 0), r.get("only_proj_ex_warm", 0)
        if ep or ej:
            out.append(f"   {key} 剔除暖机后: 只生产 {ep}（其中持仓期抑制 "
                       f"{r.get('attr_in_position_ex_warm', 0)}） / 只投影 {ej}")
        if r.get("unattributed"):
            out.append(f"   {key} **未归因**例 (票, 日, 投影持仓区间): {r['unattributed'][:4]}")
        if r.get("by_date_prod_only"):
            hist = sorted(r["by_date_prod_only"].items(), key=lambda x: -x[1])[:6]
            out.append(f"   {key} 只生产(漏) 日期分布: {hist}")
        if r.get("by_date_proj_only"):
            hist = sorted(r["by_date_proj_only"].items(), key=lambda x: -x[1])[:6]
            out.append(f"   {key} 只投影(多) 日期分布: {hist}")
    if res["skipped_no_fold"]:
        out.append(f"[回放影子] 无折叠契约, 切片落不下, 跳过: "
                   f"{', '.join(res['skipped_no_fold'])}")
    if res["skipped_intraday"]:
        out.append("[回放影子] 盘中策略 (日线路径扫不到, 须盘中回放, 本模式不覆盖): "
                   + ", ".join(f"{k}({kind})" for k, kind in res["skipped_intraday"]))
    if res["errors"]:
        out.append(f"!! [回放影子] 错误 {len(res['errors'])} 条（不静默）:")
        for e in res["errors"][:10]:
            out.append(f"   {e}")
    for key, r in sorted(res["per_strategy"].items()):
        if r["examples_prod_only"]:
            out.append(f"   {key} 只生产有(投影漏) 例: {r['examples_prod_only'][:top]}")
        if r["examples_proj_only"]:
            out.append(f"   {key} 只投影有(投影多) 例: {r['examples_proj_only'][:top]}")
    out.append("[回放影子] 判定: 只生产有 = 投影会漏发; 只投影有 = 投影会多发。"
               "两者都为 0 才算判定等价。")
    out.append("[回放影子] 行级判定 (P5-③ 门禁): 重合日的规则列必须**逐字段**相同 —— "
               "日期集合相同但 payload 分叉同样会让 signals 表长相不同。")
    return "\n".join(out)


def _replay_rc(res) -> int:
    """回放退出码 = **剔除暖机头后**的真分歧（与报告口径一致）。

    ⚠ 2026-10-07 修：原先取 `res["total"]`（暖机剔除**前**的原始差）⇒ 冷回放窗口
    开头必含 g56 台账暖机假象（ledger ROLL=20）⇒ 恒 rc=1，与报告自身「这批才算
    真分歧」的判定自相矛盾 —— 恒定假警报会训练人忽略退出码（「噪声废掉 fail-fast」）。
    原始 total 仍完整打印在报告里（可见），只是不再当判定。

    ★ P5-③ 起**同时**计入「重合日规则列不一致」（`row_parity.mismatch_ex_warm`）——
    它是同一件事的行级口径，不该只在报告里可见而放行退出码。
    """
    tx = res.get("total_ex_warm") or res["total"]
    row_bad = (res.get("row_parity") or {}).get("mismatch_ex_warm", 0)
    return 0 if not (tx["only_prod"] or tx["only_proj"] or row_bad) else 1


def main(argv=None):
    # 独立进程跑（`python -m ...`）时 .env 还没被 app 初始化加载 ⇒ 这里补一次
    # （否则取数/读库报"没密码"，误导成配置缺失）。与 tools/debug.py 同口径。
    try:
        from app.market_cn.auto.core._paths import load_env_first_found
        load_env_first_found(os.path.join(os.getcwd(), ".env"))
    except Exception:                                       # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(prog="projection_shadow")
    ap.add_argument("--root",
                    help="StateStore 切片根目录（文件名 = 策略 key.json）。"
                         "回放模式可省 ⇒ 落临时目录")
    ap.add_argument("--replay", action="store_true",
                    help="★★ 历史回放对账（替代「等 10 个交易日」）：回放窗口切片并比"
                         "「生产 scan_days vs 投影 Record.ready」的判定日集合")
    ap.add_argument("--days", type=int, default=320,
                    help="回放取数天数（对齐 run_scan / build_expected，默认 320）")
    ap.add_argument("--limit", type=int, default=None, help="回放只取前 N 票（冒烟用）")
    ap.add_argument("--strategy", action="append", default=None,
                    help="只对账这些策略 key（可重复）；缺省=切片根内全部")
    ap.add_argument("--window", type=int, default=None,
                    help=f"对账窗口（交易日）。DB 差异模式默认 {DEFAULT_WINDOW}"
                         f"（=库保留期上限）；回放模式默认 {REPLAY_WINDOW}")
    ap.add_argument("--apply", action="store_true",
                    help="写库（默认 dry-run 只报告；覆盖度护栏不通过时拒绝执行）")
    ap.add_argument("--top", type=int, default=25, help="明细打印条数")
    args = ap.parse_args(argv)

    if args.replay:
        res = replay_ready_days(days=args.days,
                                window=args.window or REPLAY_WINDOW,
                                keys=args.strategy, limit=args.limit,
                                root=args.root, progress=True)
        print(_render_replay(res, top=args.top))
        return _replay_rc(res)

    if not args.root:
        print("[投影影子] 必须给 --root（切片根目录），不猜测路径")
        return 1
    args.window = args.window or DEFAULT_WINDOW

    expected, axis = load_expected(args.root, args.strategy)
    if not expected:
        # 空集对空集是假绿（记忆铁律：零样本 ≠ PASS）—— 显式失败，不静默 0 差异。
        print("[投影影子] NO_SAMPLE: 切片投影为空 ⇒ 无法对账（根目录/策略选择有误？）")
        return 1
    win = axis[-args.window:]
    keys = sorted({k[1] for k in expected})
    actual = rebuild.load_actual(win, keys=keys)
    d = rebuild.diff(expected, actual)
    cover = coverage_report(expected, actual, keys)
    meta = {"strategies": keys}
    plan = rebuild.build_plan(expected, actual, meta)
    plan, _dropped = prune_insert(plan)          # 投影专属：只补未推进行（见 prune_insert）

    stat = None
    blocked = any(v["ratio"] < MIN_COVERAGE for v in cover.values())
    if args.apply and blocked:
        print("[投影影子] 覆盖度护栏拦截，拒绝 --apply（先让切片覆盖到位）")
    elif args.apply:
        stat = rebuild.apply_plan(plan, dry_run=False)

    print(_render(stat, expected, actual, d, plan, cover, top=args.top))

    if not args.apply:
        # 影子期判定：0 不一致 = PASS（且必须有样本，上面已挡 NO_SAMPLE）
        return 0 if not (d["missing"] or d["ghost"] or d["drift"]) else 1
    return 0 if stat is not None else 1


if __name__ == "__main__":
    sys.exit(main())
