# -*- coding: utf-8 -*-
"""qd_dispatch_ab — 差距 B 验收测量：分发 vs 不分发 对照实验（2026-10-10 立）。

【它测什么】B 的验收原文：「一次全市场筛选任务，主 agent 消耗 ≤ 无分发方案的
40%，结论质量不降（对照 evaluator 评分）」。这句话需要**对照实验**才能出数，
本脚本把实验固化成一条命令：

  · 实验臂（dispatch）：生产同款工具面（run_skill 在场，重活走子进程分发）
  · 对照臂（inline）  ：同款工具面 **摘掉 run_skill**（= 无分发方案，模型只能
    在主上下文里用普通金融工具硬跑批量任务）

两臂同 query、同预算，量三个数：主上下文 token / 步数 / 耗时，再用 qd_bench
对结论打质量分（能力标尺，2026-10-10 立）。判定：

  成本  ratio_tokens = dispatch.tokens / inline.tokens ≤ COST_MAX（默认 0.40）
  质量  quality_delta = dispatch.quality - inline.quality ≥ -QUALITY_TOL（默认 5）

【用法】
  python scripts/qd_dispatch_ab.py                     # 默认重活任务（screen_01 等）
  python scripts/qd_dispatch_ab.py --tasks screen_01
  python scripts/qd_dispatch_ab.py --query "帮我做一次全市场短线强势股筛选"
  python scripts/qd_dispatch_ab.py --rejudge x.json    # 只重算判定（改阈值后复核）

【退出码】0=两臂结论达标；1=成本或质量不达标；2=环境不可用（无模型端点）。

【为什么对照臂摘工具而不是改 prompt】B 测的是"通道值不值"，不是"纪律写没写"：
摘掉 run_skill 后模型行为自然退化成主上下文硬跑，与"无分发方案"的历史基线等价；
而改 prompt 造对照会把 prompt 变量混进实验（说不清差异来自通道还是文案）。

【易错点】
  1. 两臂必须**同预算**（step_limit 一致），否则对照臂跑飞烧 token，ratio 失真；
  2. token 取 **delta**（跑前/跑后 token_stats 差值），同臂多任务累计会污染后一题；
  3. quality 用 qd_bench.score_answer（与 C 同一把尺），别在本脚本另写评分口径——
     两把尺子量同一实验，结论没法跟 benchmark 历史对齐；
  4. run_skill 是**按需层**工具（QD_ON_DEMAND_TOOLS），摘除后预选目录里也没它，
     这正是对照臂想要的：没有通道，也没有"发现这个通道"的开销。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent          # app/agent/scripts
AGENT_DIR = HERE.parent                          # app/agent
BACKEND_ROOT = AGENT_DIR.parent.parent           # backend_api_python
for _p in (str(BACKEND_ROOT), str(AGENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from app.agent import mimo_boot
except Exception:
    pass

# 【必须在建模前加载 .env】否则首个 QDAgentService 建模时 OPENAI_BASE_URL/API_KEY
# 缺失 → 先跑的臂全部请求挂死超时；跑到第二个 svc 时工具模块 import 已顺手把
# dotenv 载入 → 后跑的臂"莫名其妙"成功——2026-10-10 三轮"先跑臂必炸"假象的根因。
from dotenv import load_dotenv  # noqa: E402
load_dotenv(BACKEND_ROOT / ".env", override=False)

REPORT_DIR = HERE / "bench" / "dispatch_ab"

# B 验收阈值（env 可覆盖；--rejudge 改阈值复核旧报告时不必重跑实验）
COST_MAX = float(os.getenv("QD_DISPATCH_COST_MAX", "0.40") or "0.40")
QUALITY_TOL = float(os.getenv("QD_DISPATCH_QUALITY_TOL", "5") or "5")

# 默认重活任务：从能力任务集里取"批量筛选"类（与 plan_dispatch 回归探针同源题面）
DEFAULT_TASKS = ["screen_01", "screen_04"]
FALLBACK_BASELINE = {"must": ["has_screen_criteria", "has_numbers", "has_risk_note"]}


# ═══════════════════════════════════════════════════════════════
#  判定（纯函数——smoke 覆盖，改阈值不重跑实验）
# ═══════════════════════════════════════════════════════════════

def _verdict(records: List[dict], cost_max: float = COST_MAX,
             quality_tol: float = QUALITY_TOL) -> dict:
    """逐题 + 总体判定。records 每项含 dispatch/inline 两臂的 tokens 与 quality。"""
    per, failures = [], []
    for r in records:
        d, i = r.get("dispatch") or {}, r.get("inline") or {}
        # 臂失败（端点重试耗尽等）：成本/质量都判负——tokens=0 会让成本比假通过
        if d.get("error") or i.get("error"):
            err = d.get("error") or i.get("error")
            per.append({"task_id": r.get("task_id"), "ratio_tokens": None,
                        "quality_delta": None, "pass_cost": False,
                        "pass_quality": False})
            failures.append(f"{r.get('task_id')}: 臂失败 {str(err)[:120]}")
            continue
        dtok, itok = float(d.get("tokens") or 0), float(i.get("tokens") or 0)
        dq, iq = float(d.get("quality") or 0), float(i.get("quality") or 0)
        ratio = round(dtok / itok, 4) if itok > 0 else None
        delta = round(dq - iq, 2)
        pass_cost = ratio is not None and ratio <= cost_max
        pass_quality = delta >= -quality_tol
        row = {"task_id": r.get("task_id"), "ratio_tokens": ratio,
               "quality_delta": delta, "pass_cost": pass_cost,
               "pass_quality": pass_quality}
        per.append(row)
        if not pass_cost:
            failures.append(f"{r.get('task_id')}: 成本比 {ratio} > {cost_max}")
        if not pass_quality:
            failures.append(f"{r.get('task_id')}: 质量降 {delta} < -{quality_tol}")
    return {"pass": not failures, "failures": failures, "per_task": per,
            "cost_max": cost_max, "quality_tol": quality_tol}


# ═══════════════════════════════════════════════════════════════
#  实验臂
# ═══════════════════════════════════════════════════════════════

def _all_tool_names() -> List[str]:
    """生产工具面全名单（函数工具 + 元工具）——对照臂从这里摘 run_skill。"""
    from app.agent.tools._base import ToolProvider
    p = ToolProvider.get_or_build()
    names = set(p.get_functions() or {}) | set(p.get_meta_functions() or {})
    return sorted(names)


def arm_whitelist(arm: str, names: Optional[List[str]] = None) -> List[str]:
    """实验臂工具白名单：dispatch=全量；inline=全量-{run_skill}（无分发方案）。"""
    names = names if names is not None else _all_tool_names()
    if arm == "inline":
        return [n for n in names if n != "run_skill"]
    return list(names)


def _build_service(arm: str, step_limit: int):
    from app.agent.qd_service import QDAgentService
    from app.agent.memory import LocalMemory
    cfg: Dict[str, Any] = {"tool_whitelist": arm_whitelist(arm)}
    if step_limit:
        cfg["step_limit"] = int(step_limit)
    return QDAgentService(memory=LocalMemory(max_messages=200), agent_config=cfg)


def _run_arm(svc, query: str, session: str) -> Dict[str, Any]:
    """跑一臂 → 成本与结论。token 取 delta（同臂多任务累计会污染，易错点 2）。"""
    import asyncio
    agent = svc._get_agent(session)
    stats = getattr(agent.model, "token_stats", None)
    t0_tokens = int(getattr(stats, "total_tokens", 0) or 0)
    t0 = time.time()
    answer = asyncio.run(svc.chat(query, session_id=session)) or ""
    if not isinstance(answer, str):
        answer = getattr(answer, "content", None) or ""   # chat() 返回 _AgentResponse
    # LLM 端点重试耗尽时 chat() 不抛异常，把 RetryError/traceback 当"答案"返回——
    # 拿它去打分会造出"质量崩塌"假象（实测 6671 字报错文本得 38.9 分）。识别为臂失败。
    if "RetryError[" in answer or answer.lstrip().startswith("Traceback"):
        raise RuntimeError(f"LLM 端点重试耗尽: {answer[:120].replace(chr(10), ' ')}")
    elapsed = round(time.time() - t0, 2)
    agent = svc._get_agent(session)
    stats = getattr(agent.model, "token_stats", None)
    tokens = int(getattr(stats, "total_tokens", 0) or 0) - t0_tokens
    return {"tokens": tokens, "steps": int(getattr(agent, "_steps_taken", 0) or 0),
            "elapsed_s": elapsed, "answer_len": len(answer), "answer": answer,
            "corpus": agent._observation_corpus() if hasattr(agent, "_observation_corpus") else ""}


def _quality(task_id: str, query: str, answer: str, corpus: str) -> float:
    """质量分：与 C 同一把尺（qd_bench.score_answer），任务基线取能力任务集。"""
    import qd_bench as B
    try:
        task = next((t for t in B.load_tasks() if t.get("id") == task_id), None)
    except Exception:
        task = None
    if task is None:
        task = {"id": task_id or "ad_hoc", "category": "选股", "query": query,
                "baseline": FALLBACK_BASELINE}
    return float(B.score_answer(task, answer, corpus)["total"])


# ═══════════════════════════════════════════════════════════════
#  实验主流程
# ═══════════════════════════════════════════════════════════════

def run_experiment(queries: List[Tuple[str, str]], step_limit: int = 0,
                   model: str = "") -> dict:
    """queries = [(task_id, query)]；每题两臂各跑一次。"""
    import qd_bench as B
    records = []
    svc_dispatch = _build_service("dispatch", step_limit)
    svc_inline = _build_service("inline", step_limit)
    for tid, query in queries:
        row: Dict[str, Any] = {"task_id": tid, "query": query}
        # 臂顺序可用 env QD_AB_ARMS=inline,dispatch 对调——「先跑的臂超时」和
        # 「工具面含 run_skill」两个变量在固定顺序下不可分，对调才能判别。
        _order = [a.strip() for a in os.getenv("QD_AB_ARMS", "dispatch,inline").split(",")
                  if a.strip() in ("dispatch", "inline")] or ["dispatch", "inline"]
        for arm in _order:
            svc = svc_dispatch if arm == "dispatch" else svc_inline
            sid = f"ab_{arm}_{tid}"
            r = None
            for attempt in (1, 2):   # 端点瞬时超时重试一次（每次新会话，避免脏上下文）
                try:
                    r = _run_arm(svc, query, f"{sid}_try{attempt}")
                    r["quality"] = _quality(tid, query, r.pop("answer"), r.pop("corpus"))
                    break
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    print(f"  [{tid}/{arm}] 尝试{attempt}失败 {err[:140]}")
                    if attempt == 2:
                        r = {"error": err}
            row[arm] = r
            if r is not None and "error" not in r:
                print(f"  [{tid}/{arm}] tokens={r['tokens']} steps={r['steps']} "
                      f"{r['elapsed_s']}s quality={r['quality']}")
        records.append(row)
    v = _verdict(records)
    return {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "model": model or "default",
            "step_limit": step_limit, "records": records, "verdict": v,
            "thresholds": {"cost_max": v["cost_max"], "quality_tol": v["quality_tol"]}}


def save_report(rep: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    p = REPORT_DIR / ("dispatch_ab_" + time.strftime("%Y%m%d_%H%M%S") + ".json")
    p.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load_report(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def _print_report(rep: dict) -> None:
    v = rep.get("verdict") or {}
    print(f"\n== 分发 A/B 对照 · {'PASS' if v.get('pass') else 'FAIL'} "
          f"（成本阈值 ≤{v.get('cost_max')} · 质量容差 -{v.get('quality_tol')}）==")
    for row in v.get("per_task") or []:
        # 臂失败时 ratio/quality 为 None（端点重试耗尽等）——不能走 :+/格式化
        _r = row.get("ratio_tokens")
        _d = row.get("quality_delta")
        _rs = "—" if _r is None else str(_r)
        _ds = "臂失败" if _d is None else f"{_d:+}"
        print(f"  {row['task_id']:<12} 成本比 {_rs} "
              f"[{'✓' if row['pass_cost'] else '✗'}]  质量Δ {_ds} "
              f"[{'✓' if row['pass_quality'] else '✗'}]")
    for f in v.get("failures") or []:
        print(f"  [FAIL] {f}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="qd_dispatch_ab",
                                 description="差距 B 验收：分发 vs 不分发 对照实验")
    ap.add_argument("--tasks", default="", help="逗号分隔 bench 任务 id（默认 screen_01,screen_04）")
    ap.add_argument("--query", default="", help="自定义重活题面（与 --tasks 二选一）")
    ap.add_argument("--step-limit", type=int, default=0)
    ap.add_argument("--model", default="")
    ap.add_argument("--rejudge", default="", help="重算既有报告判定（可配 --cost-max 等）")
    ap.add_argument("--cost-max", type=float, default=COST_MAX)
    ap.add_argument("--quality-tol", type=float, default=QUALITY_TOL)
    args = ap.parse_args(argv)

    if args.rejudge:
        rep = load_report(args.rejudge)
        rep["verdict"] = _verdict(rep.get("records") or [], args.cost_max, args.quality_tol)
        _print_report(rep)
        return 0 if rep["verdict"]["pass"] else 1

    try:
        import qd_bench as B
        from app.agent.qd_service import QDAgentService
    except Exception as e:
        print(f"[ENV] 运行条件不满足: {type(e).__name__}: {e}")
        return 2

    queries: List[Tuple[str, str]] = []
    if args.query:
        queries = [("ad_hoc", args.query)]
    else:
        want = [t.strip() for t in (args.tasks or ",".join(DEFAULT_TASKS)).split(",") if t.strip()]
        try:
            tasks = {t.get("id"): t for t in B.load_tasks()}
        except Exception:
            tasks = {}
        for tid in want:
            t = tasks.get(tid)
            if t and (t.get("query") or "").strip():
                queries.append((tid, str(t["query"])))
            else:
                print(f"  [SKIP] 任务 {tid} 不在能力任务集，改用 fallback 题面")
                queries.append((tid, "帮我做一次全市场短线强势股筛选，总结条件和要点"))
        if not queries:
            print("[ENV] 无可用任务题面")
            return 2

    rep = run_experiment(queries, step_limit=args.step_limit, model=args.model)
    rep["verdict"] = _verdict(rep["records"], args.cost_max, args.quality_tol)
    p = save_report(rep)
    _print_report(rep)
    print(f"\n报告: {p}")
    return 0 if rep["verdict"]["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
