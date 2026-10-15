# -*- coding: utf-8 -*-
"""exp_grind — 小量刷经验驱动器（2026-10-15）。

【它是什么】追责-校准闭环的**经验水龙头**：挑一小批股票走**真实** agent 分析，
把闭环跑起来攒样本。设计依据：
  · AGENT_DESIGN_v3.md §21.3 —— 经验来源与分层配额（活跃/中热/低热+随机），
    "样本若集中在少数活跃股，情境维在样本里近乎常数 ⇒ 条件化失效"；
  · AGENT_DESIGN_v3.md §21.4 —— P3 接线细则（pairs × resolutions → samples
    → cells → β₀ → coefs），观测入口是 `calib_feed.build_report()`；
  · 数据流模拟报告 S-5 —— `daily_universe` **只写不读**的产线缺环
    （sampler 每日采 150 只，`collector.load_universe` 零消费方）。
    本脚本就是那个消费方的小量版。

【它不是什么】★ 不写任何合成数据 ★
  pairs / claims / resolutions 全部由**生产链路自己**产出，本脚本只是驱动器：
    run_agent → QDAgent.run → _finish_collector（qd_aftercare）
      → _intake_accountability   （decisions / claims 落 PG）
      → _c3p_pipeline            （pairs JSONL：p_base / condition_key / claim_id）
      → _record_case             （案例记忆，label=pending 待 T+N 回填）
    auto_evaluate（evaluator 盘后闭环的同款入口）
      → resolve_due_claims       （T+N 真值判定，取价走本地日线）
      → weight_feed.feed         （resolutions → qd_agent_weights layer='domain'）
  ⇒ **不存在**"编数据让 Δp 动起来"的路径。冷启动期 Δp≡0 是设计行为
    （§21.4：判别接线生效不看 Δp，看格子与簇数），别为看效果造假样本。

【为什么分层轮采】§21.3：小批量也要 hot/warm/cold 都有 —— 全抽活跃层等于
  主动放弃情境维变异，条件化校准的前提直接不成立。抽样用 **date 作种子**
  的随机（sampler 同款幂等范式）：同日重跑选出同一批（可对账），跨日自动换票。

【为什么每票独立 session】A-01 per-session 锁的正确用法 + 提取卫生：
  同 session 连问多票，后一票的分析会引用前一票上下文（"同上"），
  claim 的 subject 归属被污染。一票一个 `exp-grind:<日期>:<序号>:<代码>`。

【成本护栏】"小量"是硬约束不是客气话：
  · 默认 batch=3，env `QD_EXP_GRIND_BATCH` 可调；
  · 单次调用上限 `QD_EXP_GRIND_MAX`（默认 10），`--batch` 超了直接钳制；
  · 单票超时 `QD_EXP_GRIND_TIMEOUT`（默认 300s），单票失败**不中断**批次；
  · `--dry-run` 只列计划，零 LLM 成本（守卫测试盯着这一点）。

【预期节奏】T+1 claim 次日才到期 ⇒ 当天 `build_report()` 多半仍
  `n_samples=0`，这是设计行为。样本开始出现要等到期判定跑过 —— 盘后 worker
  每天 15:30 自动做，或重跑本脚本立即驱动一次 `auto_evaluate`。

【用法】
  cd backend_api_python
  python -m app.agent.scripts.exp_grind                          # 默认 batch=3 分层抽
  python -m app.agent.scripts.exp_grind --batch 5                # 本批 5 只
  python -m app.agent.scripts.exp_grind --codes 600519,300750    # 显式代码（跳过抽样）
  python -m app.agent.scripts.exp_grind --dry-run                # 只列计划不执行
  python -m app.agent.scripts.exp_grind --no-eval                # 跳过盘后判定驱动
  python -m app.agent.scripts.exp_grind --universe-date 2026-10-13

【退出码】0=跑完（含单票部分失败，明细在输出里）；1=整批全失败；2=环境不可用
（universe 空且未给 --codes）。
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

# 路径引导：与 qd_bench / weekly_panel 同款（BACKEND_ROOT 让 `import app.*` 可用；
# 两路径都插是历史约定，agent 包内一律全名导入，不会长出双模块实例）。
HERE = Path(__file__).resolve().parent          # app/agent/scripts
AGENT_DIR = HERE.parent                          # app/agent
BACKEND_ROOT = AGENT_DIR.parent.parent           # backend_api_python
for _p in (str(BACKEND_ROOT), str(AGENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv  # noqa: E402
# 直跑本模块时 .env 未加载（qd_dispatch_ab 踩过：先跑臂必炸的假象就是漏这句）
load_dotenv(BACKEND_ROOT / ".env", override=False)

from app.agent.log import logger  # noqa: E402

# ── 环境配置（禁止硬编码，一律可从 env 改；脏值回落默认，不崩）────────────
_DEFAULT_BATCH = 3          # ★ 默认必须小："小量"是本脚本的存在理由
_DEFAULT_MAX = 10           # 单次调用硬顶（手滑 --batch 100 的保险丝）
_DEFAULT_TIMEOUT = 300      # 单票超时秒数（实测单票约 60s，留 5x 余量）


def _env_int(name: str, default: int) -> int:
    try:
        v = int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def batch_size() -> int:
    """默认批次大小（env `QD_EXP_GRIND_BATCH`）。"""
    return _env_int("QD_EXP_GRIND_BATCH", _DEFAULT_BATCH)


def max_batch() -> int:
    """单次调用批次硬顶（env `QD_EXP_GRIND_MAX`）—— 驱动器自己就是成本闸。"""
    return _env_int("QD_EXP_GRIND_MAX", _DEFAULT_MAX)


def run_timeout() -> int:
    """单票超时秒数（env `QD_EXP_GRIND_TIMEOUT`）。"""
    return _env_int("QD_EXP_GRIND_TIMEOUT", _DEFAULT_TIMEOUT)


# 提问模板：显式带"明日（T+1）"——校准默认只收 T+1 口径的 claim
# （§21.4：p_base 来自 predict_next_day，拿 T+3 判定校它是 A 尺子量 B）。
_PROMPT = ("分析 {code} 短线技术面与资金面，给出明日（T+1）涨跌方向判断、"
           "关键价位和风险提示")


def resolve_batch(requested: int) -> int:
    """定本次批次大小：请求值 → env 默认 → 上限钳制（永不低于 1）。"""
    try:
        n = int(requested)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        n = batch_size()
    cap = max_batch()
    if n > cap:
        logger.warning("[ExpGrind] batch=%d 超过上限 %d，已钳制（成本护栏）", n, cap)
        n = cap
    return max(1, n)


def _session_of(day: str, idx: int, code: str) -> str:
    """每票独立 session（A-01 per-session 锁 + claim 归属卫生）。"""
    return f"exp-grind:{day}:{idx:02d}:{code}"


def pick_batch(n: int, codes: str = "", universe_date: str = "") -> List[Dict[str, str]]:
    """选本批股票：显式 codes 优先，否则从 daily_universe **分层轮采**。

    ★ 分层（§21.3）：hot/warm/cold 各取，round-robin 补足 n —— 全抽活跃层
      会让 theme_heat / vol_bucket 等情境维在样本里近常数 ⇒ 条件化失效。
    ★ 确定性：随机种子 = 消费的 universe 日期 ⇒ 同日重跑选出同一批（可对账），
      跨日自动换票（不天天分析同一只）。sampler 同款幂等范式。
    ★ 去重：同一代码在一批里只出现一次（重复分析 = 重复计票）。
    """
    n = max(1, int(n or 1))
    if codes and codes.strip():
        out: List[Dict[str, str]] = []
        for c in codes.split(","):
            c = c.strip()
            if c and c not in {x["code"] for x in out}:
                out.append({"code": c, "sample_layer": "explicit"})
        return out[:n]

    from app.agent.chain import collector as _col
    try:
        rows = _col.load_universe(universe_date or None) or []
    except Exception as e:
        logger.warning("[ExpGrind] 读 daily_universe 失败: %s: %s", type(e).__name__, e)
        return []
    recs = [r for r in rows if (r.get("universe") or {}).get("items")]
    if not recs:
        return []
    day = (universe_date or max(str(r.get("as_of") or "")[:10] for r in recs))[:10]
    recs = [r for r in recs if str(r.get("as_of") or "")[:10] == day]

    by_layer: Dict[str, List[str]] = {}
    for r in recs:
        for it in (r.get("universe") or {}).get("items") or []:
            code = str(it.get("code") or "").strip()
            layer = str(it.get("sample_layer") or "").strip() or "unknown"
            if code:
                by_layer.setdefault(layer, []).append(code)
    if not by_layer:
        return []

    rng = random.Random(f"exp-grind:{day}")
    for layer in by_layer:
        rng.shuffle(by_layer[layer])

    out, seen = [], set()
    layers = sorted(by_layer)          # 稳定序，输出可复现
    while len(out) < n:
        progressed = False
        for layer in layers:
            if len(out) >= n:
                break
            while by_layer[layer] and by_layer[layer][0] in seen:
                by_layer[layer].pop(0)             # 跨层重复代码直接丢弃
            if by_layer[layer]:
                code = by_layer[layer].pop(0)
                seen.add(code)
                out.append({"code": code, "sample_layer": layer})
                progressed = True
        if not progressed:
            break                                # 池子抽干，不编造
    return out


def build_plan(batch: int, codes: str = "",
               universe_date: str = "") -> List[Dict[str, Any]]:
    """出一份可打印的执行计划（dry-run 与真跑共用，保证"看到的即将执行的"）。"""
    n = resolve_batch(batch)
    picks = pick_batch(n, codes=codes, universe_date=universe_date)
    day = date.today().isoformat()
    return [{**p, "idx": i, "session": _session_of(day, i, p["code"])}
            for i, p in enumerate(picks)]


def run_one(item: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    """跑一只：走统一同步入口 `run_agent`（Flask/CLI/Cron 的同款路径）。

    ★ `run_agent` 的 import 放在**函数内**：dry-run 不付执行核装配成本
      （工具面预热 + eval worker 启动），测试也才能裸跑不依赖 LLM。
    ★ 单票异常**不外抛**：由 main 记 errors 继续下一票（一票失败不是批次的失败）。
    """
    from app.agent.agent import run_agent
    code, sid = item["code"], item["session"]
    msg = _PROMPT.format(code=code)
    t0 = time.perf_counter()
    try:
        answer = run_agent(msg, session_id=sid, timeout=timeout)
        return {"code": code, "session": sid, "ok": True,
                "answer_len": len(answer or ""),
                "elapsed_s": round(time.perf_counter() - t0, 1)}
    except Exception as e:
        return {"code": code, "session": sid, "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "elapsed_s": round(time.perf_counter() - t0, 1)}


def drive_eval() -> Dict[str, Any]:
    """驱动一次盘后闭环（`evaluator.auto_evaluate` 的同款组合，不自己拼步骤）。

    内部：evaluate_pending（旧 traces）+ resolve_due_claims（T+N 真值）
    + weight_feed.feed（domain 权重）+ 报告；每步独立 try，互不带崩。
    """
    from app.agent.chain.evaluator import auto_evaluate
    try:
        r = auto_evaluate() or {}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    acc = r.get("accountability") or {}
    wf = r.get("weight_feed") or {}
    return {"due": acc.get("due"), "resolved": acc.get("resolved"),
            "undecidable": acc.get("undecidable"), "errors": acc.get("errors"),
            "weight_updated": wf.get("updated")}


def snapshot() -> Dict[str, Any]:
    """两条闭环的当前状态；**每个数据源独立 fail-open**（一个挂了不毁全份报告）。

    字段对应 §21.4 指定的观测入口：pairs / 待判定 claim / resolutions /
    n_samples / n_cells / n_clusters_total / usable_cells / cells。
    """
    snap: Dict[str, Any] = {"pairs": None, "due_claims": None, "resolutions": None,
                            "n_samples": None, "n_cells": None,
                            "n_clusters_total": None, "usable_cells": None,
                            "cells": {}}
    try:
        from app.agent.chain import aggregator as _ag
        snap["pairs"] = len(_ag.load_pairs() or [])
    except Exception as e:
        snap["pairs_err"] = f"{type(e).__name__}"
    try:
        from app.agent.chain import account_store as _as
        snap["due_claims"] = len(_as.query_due_claims(limit=500) or [])
        snap["resolutions"] = len(_as.load_latest_resolutions() or [])
    except Exception as e:
        snap["db_err"] = f"{type(e).__name__}"
    try:
        from app.agent.chain import calib_feed as _cf
        rep = _cf.build_report() or {}
        snap.update({"n_samples": rep.get("n_samples"),
                     "n_cells": rep.get("n_cells"),
                     "n_clusters_total": rep.get("n_clusters_total"),
                     "usable_cells": rep.get("n_usable"),
                     "cells": {ck: {"n_clusters": c.get("n_clusters"),
                                    "n_obs": c.get("n_obs"),
                                    "win_rate": c.get("win_rate")}
                               for ck, c in (rep.get("cells") or {}).items()}})
    except Exception as e:
        snap["calib_err"] = f"{type(e).__name__}"
    return snap


# ── 输出（stdout = 报告通道；logger 已由 app.agent.log 收口 stderr）────────

def _print_plan(plan: List[Dict[str, Any]], dry_run: bool) -> None:
    tag = "（dry-run，不执行）" if dry_run else ""
    print(f"[exp_grind] 本批计划{tag}：{len(plan)} 只")
    for p in plan:
        print(f"  {p['idx']:02d}  {p['code']:<12} layer={p['sample_layer']:<8} "
              f"session={p['session']}")


def _print_result(r: Dict[str, Any]) -> None:
    if r.get("ok"):
        print(f"  ✓ {r['code']:<12} {r['elapsed_s']:>6.1f}s  answer={r['answer_len']} chars")
    else:
        print(f"  ✗ {r['code']:<12} {r['elapsed_s']:>6.1f}s  {r.get('error')}")


def _print_eval(ev: Dict[str, Any]) -> None:
    if ev.get("error"):
        print(f"[exp_grind] 盘后驱动失败: {ev['error']}")
        return
    print(f"[exp_grind] 盘后驱动 auto_evaluate: due={ev.get('due')} "
          f"resolved={ev.get('resolved')} undecidable={ev.get('undecidable')} "
          f"errors={ev.get('errors')} weight_updated={ev.get('weight_updated')}")


def _fmt(v: Any) -> str:
    return "n/a" if v is None else str(v)


def _print_snapshot(tag: str, s: Dict[str, Any]) -> None:
    print(f"[exp_grind] 快照·{tag}: pairs={_fmt(s['pairs'])} "
          f"due_claims={_fmt(s['due_claims'])} resolutions={_fmt(s['resolutions'])} "
          f"n_samples={_fmt(s['n_samples'])} n_cells={_fmt(s['n_cells'])} "
          f"n_clusters={_fmt(s['n_clusters_total'])} usable={_fmt(s['usable_cells'])}")
    for ck, c in (s.get("cells") or {}).items():
        print(f"    · {ck}: n_clusters={c['n_clusters']} n_obs={c['n_obs']} "
              f"win_rate={c['win_rate']}")


def _print_delta(before: Dict[str, Any], after: Dict[str, Any]) -> None:
    keys = ["pairs", "due_claims", "resolutions", "n_samples", "n_cells",
            "n_clusters_total", "usable_cells"]
    diffs = []
    for k in keys:
        b, a = before.get(k), after.get(k)
        if isinstance(b, int) and isinstance(a, int) and a != b:
            diffs.append(f"{k} {b}→{a}")
    print(f"[exp_grind] 本次增量: {'，'.join(diffs) if diffs else '（无数值变化）'}")


def _print_next_step() -> None:
    print("[exp_grind] 下一步: 本批 T+1 claim 下一个交易日到期；盘后 worker（每天 15:30）"
          "会自动判定，或重跑本脚本立即驱动一次。样本进格子后 "
          "`calib_feed.build_report()` 的 n_samples / n_clusters 才会动（§21.4）。")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="exp_grind",
        description="小量刷经验驱动器：真实分析 → T+N 判定 → 校准观测（不写任何合成数据）")
    ap.add_argument("--batch", type=int, default=0,
                    help="本批股票数（0=env QD_EXP_GRIND_BATCH，默认 3；"
                         "上限 env QD_EXP_GRIND_MAX 默认 10）")
    ap.add_argument("--codes", default="",
                    help="显式代码列表（逗号分隔），跳过 universe 分层抽样")
    ap.add_argument("--universe-date", default="",
                    help="指定 universe 日期（默认最新一天）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列计划不执行（零 LLM 成本）")
    ap.add_argument("--no-eval", action="store_true",
                    help="跳过盘后闭环驱动（auto_evaluate）")
    args = ap.parse_args(argv)

    plan = build_plan(args.batch, codes=args.codes, universe_date=args.universe_date)
    if not plan:
        print("[exp_grind] 批次为空：daily_universe 没有可消费样本"
              "（先跑盘后采集 run_daily_collection），或用 --codes 显式指定")
        return 2

    _print_plan(plan, dry_run=args.dry_run)
    if args.dry_run:
        print("[exp_grind] dry-run 完成：未执行任何分析（零 LLM 成本）")
        return 0

    before = snapshot()
    _print_snapshot("跑之前", before)

    results = [run_one(item, run_timeout()) for item in plan]
    for r in results:
        _print_result(r)

    if not args.no_eval:
        _print_eval(drive_eval())

    after = snapshot()
    _print_snapshot("跑之后", after)
    _print_delta(before, after)
    _print_next_step()

    ok = sum(1 for r in results if r.get("ok"))
    print(f"[exp_grind] 完成：{ok}/{len(results)} 只分析成功；"
          f"经验已由生产链路落库（claims/pairs/resolutions），本脚本未写任何数据")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
