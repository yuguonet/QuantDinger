# -*- coding: utf-8 -*-
"""Prompt 回归任务集跑批（2026-10-01 立）—— 改 prompt 后必须跑一遍对 diff。

【为什么不是 smoke】qd_smoke.py 用**脚本化假模型**验证机制（工具挂载/拦截/溯源），
它证明不了"模型看了 prompt 会怎么答"。最近 4 个 bug 没有一个逻辑错，全是 prompt
条目互相逼出来的行为 ⇒ 必须有真实端点 + 真实工具的行为回归。

【它做什么】
  1. 读 prompt_tasks.yaml 的固定任务集（跑马灯/天气/选股/续聊/交易确认）
  2. 接真实端点（.env 的 LLM）+ 真实工具面跑一遍
  3. 对每条任务执行**可机检断言**（调没调某工具/有没有说某句话/token 是否超预算）
  4. 输出 JSON 报告，与 baseline.json 对 diff：PASS→FAIL = 回归（非零退出）
  5. 记录每轮 token 消耗 —— 同时充当 token 爆炸的量化护栏

【用法】
  python app/agent/scripts/qd_prompt_eval.py                 # 全量跑 + 对 baseline diff
  python app/agent/scripts/qd_prompt_eval.py --only weather_realtime,continuation
  python app/agent/scripts/qd_prompt_eval.py --update-baseline   # 本次结果存为新基线
  python app/agent/scripts/qd_prompt_eval.py --validate      # 只校验任务集，不调 LLM
  python app/agent/scripts/qd_prompt_eval.py --profile minimal   # 不接生产装配（DB/RAG）

【退出码】0=无回归；1=存在 FAIL 或回归；2=环境不可用（缺 LLM 配置等）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from concurrent.futures import TimeoutError as _TimeoutError
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 注意：__file__ 是**文件**路径，路径推导必须先 .parent 拿目录（否则 TASKS_FILE 会被
# 拼成 ".../qd_prompt_eval.py/prompt_tasks.yaml"，报一个看不懂的 FileNotFoundError）。
HERE = Path(__file__).resolve().parent         # app/agent/scripts
AGENT_DIR = HERE.parent                        # app/agent
BACKEND_ROOT = AGENT_DIR.parent.parent         # backend_api_python
for _p in (str(BACKEND_ROOT), str(AGENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import mimo_boot  # noqa: F401,E402  统一依赖引导（找不到 mimoagent 时给安装指引）

from dotenv import load_dotenv  # noqa: E402
load_dotenv(BACKEND_ROOT / ".env", override=False)

from app.agent.qd_service import QDAgentService  # noqa: E402

REPORT_DIR = HERE / "prompt_eval"
TASKS_FILE = HERE / "prompt_tasks.yaml"
BASELINE_FILE = REPORT_DIR / "baseline.json"
LATEST_FILE = REPORT_DIR / "latest.json"

DEFAULT_TIMEOUT = int(os.getenv("QD_EVAL_TIMEOUT", "240"))  # 单任务秒数


# ═══════════════════════════════════════════════════════════════
#  任务集
# ═══════════════════════════════════════════════════════════════

def load_tasks(path: Path) -> List[dict]:
    try:
        import yaml
    except ImportError:
        print("缺 pyyaml，无法读取任务集", file=sys.stderr)
        raise SystemExit(2)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tasks = data.get("tasks") or []
    if not tasks:
        raise SystemExit(f"任务集为空: {path}")
    return tasks


def validate_tasks(tasks: List[dict]) -> List[str]:
    """只做结构校验（不调 LLM）：字段合法性 + 断言是否空转。"""
    errs = []
    seen = set()
    known = {"must_call", "must_call_any", "must_not_call", "must_call_domain",
             "must_not_call_domain", "must_contain", "must_contain_any",
             "must_not_contain", "max_total_tokens"}
    for t in tasks:
        tid = t.get("id")
        if not tid:
            errs.append("任务缺 id")
            continue
        if tid in seen:
            errs.append(f"任务 id 重复: {tid}")
        seen.add(tid)
        if not t.get("turns"):
            errs.append(f"{tid}: 缺 turns")
        if not t.get("session"):
            errs.append(f"{tid}: 缺 session")
        checks = t.get("checks") or {}
        if not checks:
            errs.append(f"{tid}: 没有断言（回归集不允许空转）")
        for k in checks:
            if k not in known:
                errs.append(f"{tid}: 未知断言 {k}")
    return errs


# ═══════════════════════════════════════════════════════════════
#  服务装配
# ═══════════════════════════════════════════════════════════════

def build_service(profile: str) -> Tuple[QDAgentService, str]:
    """装配被测服务。

    auto（默认）/prod：直接用生产装配 app/agent/agent.py（真实记忆/RAG/技能，
    与线上同路径）。失败则退回 minimal，并在报告里标注（避免"装配问题"被误读成
    "prompt 回归"）。
    minimal：只用 LocalMemory + 真实 LLM，不接 DB/RAG —— 快，适合只改 prompt 的场合。
    """
    if profile in ("auto", "prod"):
        try:
            from app.agent.agent import agent as prod_agent  # noqa
            return prod_agent, "production"
        except Exception as e:
            if profile == "prod":
                raise
            print(f"[warn] 生产装配失败，退回 minimal: {e!r}")
    from app.agent.memory import LocalMemory
    from app.agent.llm import QDSkillAdapter
    svc = QDAgentService(memory=LocalMemory(max_messages=200),
                         retriever=None, skills=QDSkillAdapter(),
                         agent_config={"service_availability": {"search_knowledge": False}})
    return svc, "minimal"


def _model_stats(svc: QDAgentService) -> Dict[str, int]:
    st = getattr(getattr(svc, "_model", None), "token_stats", None)
    if st is None:
        return {"input": 0, "output": 0, "total": 0}
    return {"input": int(getattr(st, "input_tokens", 0) or 0),
            "output": int(getattr(st, "output_tokens", 0) or 0),
            "total": int(getattr(st, "total_tokens", 0) or 0)}


def _domain_map() -> Dict[str, str]:
    """工具名 → 域（按需层判定用；mimo 原生归 'mimo'）。"""
    try:
        from app.agent.tools.base import ToolProvider
        p = ToolProvider.get_or_build()
        dom = {n: p.get_domain(n) for n in p.get_functions()}
        for n in p.get_meta_functions():
            dom[n] = "meta"
        return dom
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════
#  执行
# ═══════════════════════════════════════════════════════════════

def run_task(svc: QDAgentService, task: dict, dom: Dict[str, str],
             availability: Dict[str, Any], timeout: int, step_limit: int = 0) -> dict:
    session = task.get("session") or f"reg_{task['id']}"
    out: Dict[str, Any] = {
        "id": task["id"], "desc": task.get("desc", ""), "session": session,
        "status": "ok", "turns": [], "tools": [], "tool_domains": {},
        "steps": 0, "tokens": {"input": 0, "output": 0, "total": 0},
        "elapsed_s": 0.0, "answer": "", "prefetch": [], "error": None,
    }

    # 环境闸门：依赖不可用 → SKIP（不记 FAIL，否则 KEY 一撤满屏假红）
    need = task.get("requires_availability") or []
    missing = [n for n in need if not (availability.get(n) or {}).get("available")]
    if missing:
        out["status"] = "skip"
        out["error"] = f"依赖工具不可用: {', '.join(missing)}"
        return out

    # 步数预算：跑批必须给上限，否则一条"取数不顺"的任务能跑 20+ 步 / 50 万 token
    # （实测 stock_screen 未限步时跑到 575k input 仍未收敛）。
    if step_limit > 0:
        svc.agent_config["step_limit"] = step_limit

    try:
        agent = svc._get_agent(session)   # 先建会话（顺便把 model 建出来，便于取 token 基线）
    except Exception as e:
        out["status"] = "error"
        out["error"] = f"建模失败: {e!r}"
        return out

    before = _model_stats(svc)
    t0 = time.time()
    # 【为什么不用 asyncio.wait_for】svc.chat 内部是 asyncio.to_thread：wait_for 触发后
    # 只取消协程，**工作线程仍在跑**，随后 asyncio.run 关闭 loop 时还会
    # shutdown_default_executor() 阻塞等它 —— 实测超时 240s 的任务实际卡了 540s。
    # 故改为"每任务一个单线程 executor + future.result(timeout)"，超时即弃（wait=False）。
    from concurrent.futures import ThreadPoolExecutor

    def _job():
        agent_local = svc._get_agent(session)
        cursor = len(agent_local.messages)
        local = {"turns": [], "tools": [], "answer": "", "prefetch": [], "preselect": []}
        for turn in task.get("turns") or []:
            resp = asyncio.run(svc.chat(turn, session_id=session))
            text = getattr(resp, "content", "") or ""
            new_tools = _tools_since(agent_local, cursor)
            cursor = len(agent_local.messages)
            local["turns"].append({"prompt": turn, "answer_len": len(text), "tools": new_tools})
            local["tools"] += new_tools
            local["answer"] = text
            local["prefetch"].append(getattr(agent_local, "last_prefetch_report", {}) or {})
            # 工具预选留痕：观测"预选点名了哪些工具 / 是否失败回退"，
            # 与 token、步数一起构成"分层是否真的省了"的判据。
            local["preselect"].append(getattr(agent_local, "last_preselect_report", {}) or {})
        return local

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        fut = executor.submit(_job)
        try:
            local = fut.result(timeout=timeout)
            out.update(local)
        except Exception as e:  # TimeoutError 及任务内异常
            out["status"] = "error"
            out["error"] = ("超时（>%ds，已弃线程）" % timeout
                            if isinstance(e, _TimeoutError) else f"{type(e).__name__}: {e}")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    out["steps"] = int(getattr(agent, "_steps_taken", 0) or 0)
    out["elapsed_s"] = round(time.time() - t0, 2)

    after = _model_stats(svc)
    out["tokens"] = {k: max(0, after[k] - before[k]) for k in ("input", "output", "total")}
    # 域归属统计（must_(not_)call_domain 断言用）
    for n in out["tools"]:
        d = dom.get(n, "mimo")
        out["tool_domains"][d] = out["tool_domains"].get(d, 0) + 1
    return out


def _tools_since(agent, cursor: int) -> List[str]:
    """从 messages 的第 cursor 条之后，摘出本次调用过的工具名（保序去重）。"""
    names: List[str] = []
    for msg in (getattr(agent, "messages", []) or [])[cursor:]:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            n = ((tc or {}).get("function") or {}).get("name")
            if n and n not in names:
                names.append(n)
    return names


# ═══════════════════════════════════════════════════════════════
#  断言
# ═══════════════════════════════════════════════════════════════

def evaluate(task: dict, res: dict) -> List[dict]:
    if res["status"] != "ok":
        return [{"name": f"<{res['status']}>", "ok": False, "detail": res.get("error", "")}]

    c = task.get("checks") or {}
    answer = res.get("answer") or ""
    tools = res.get("tools") or []
    doms = res.get("tool_domains") or {}
    checks: List[dict] = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    for n in (c.get("must_call") or []):
        add(f"must_call:{n}", n in tools, f"实际调用: {tools}")
    if c.get("must_call_any"):
        hit = [n for n in c["must_call_any"] if n in tools]
        add("must_call_any:" + "|".join(c["must_call_any"]), hit, f"命中 {hit}，实际 {tools}")
    for n in (c.get("must_not_call") or []):
        add(f"must_not_call:{n}", n not in tools, f"不该调用却调了: {n}")
    for d in (c.get("must_call_domain") or []):
        add(f"must_call_domain:{d}", doms.get(d, 0) > 0, f"域调用统计: {doms}")
    for d in (c.get("must_not_call_domain") or []):
        add(f"must_not_call_domain:{d}", doms.get(d, 0) == 0, f"域调用统计: {doms}")
    for s in (c.get("must_contain") or []):
        add(f"must_contain:{s[:20]}", s in answer, f"答案前 200 字: {answer[:200]}")
    if c.get("must_contain_any"):
        hit = [s for s in c["must_contain_any"] if s in answer]
        add("must_contain_any:" + "|".join(x[:12] for x in c["must_contain_any"]), hit,
            f"命中 {hit}")
    for s in (c.get("must_not_contain") or []):
        add(f"must_not_contain:{s[:20]}", s not in answer, f"命中禁用词: {s}")
    if c.get("max_total_tokens"):
        tot = res["tokens"]["total"]
        add(f"max_total_tokens:{c['max_total_tokens']}", tot <= c["max_total_tokens"],
            f"实际 {tot}")
    return checks


# ═══════════════════════════════════════════════════════════════
#  报告 / diff
# ═══════════════════════════════════════════════════════════════

def flatten(report: dict) -> Dict[str, bool]:
    out = {}
    for r in report.get("results", []):
        for c in r.get("checks", []):
            out[f"{r['id']}::{c['name']}"] = bool(c["ok"])
    return out


def diff(baseline: dict, current: dict) -> dict:
    b, c = flatten(baseline), flatten(current)
    regressions = [k for k, v in b.items() if v and not c.get(k, False)]
    fixed = [k for k, v in c.items() if v and b.get(k) is False]
    new = [k for k in c if k not in b]
    # token 回归（宽松阈值：不同模型/网络抖动会有 ±20% 噪声，取 +30% 报警）
    token_notes = []
    for r in current.get("results", []):
        base = next((x for x in baseline.get("results", []) if x["id"] == r["id"]), None)
        if not base:
            continue
        btok = (base.get("tokens") or {}).get("total", 0) or 0
        ctok = (r.get("tokens") or {}).get("total", 0) or 0
        if btok > 0 and ctok > btok * 1.3:
            token_notes.append(f"{r['id']}: {btok} → {ctok} tokens (+{round((ctok/btok-1)*100)}%)")
    return {"regressions": regressions, "fixed": fixed, "new": new,
            "token_notes": token_notes}


def print_report(report: dict, d: Optional[dict]) -> None:
    print(f"\n=== Prompt 回归报告 · profile={report['profile']} · {report['finished_at']} ===")
    print(f"{'任务':<18}{'状态':<8}{'步':<4}{'tokens':<9}{'秒':<7}断言")
    for r in report["results"]:
        ok = sum(1 for c in r["checks"] if c["ok"])
        tot = len(r["checks"])
        print(f"{r['id']:<18}{r['status']:<8}{r['steps']:<4}"
              f"{r['tokens']['total']:<9}{r['elapsed_s']:<7}{ok}/{tot}")
        for c in r["checks"]:
            if not c["ok"]:
                print(f"    [FAIL] {c['name']} — {str(c['detail'])[:160]}")
        if r["status"] != "ok":
            print(f"    [{r['status'].upper()}] {r.get('error')}")
        if r.get("tools"):
            print(f"    工具: {', '.join(r['tools'])}")
    if d:
        print(f"\n--- 对 baseline 的 diff ---")
        print(f"回归(PASS→FAIL): {len(d['regressions'])}")
        for k in d["regressions"]:
            print(f"    ✗ {k}")
        if d["fixed"]:
            print(f"修复(FAIL→PASS): {len(d['fixed'])}")
            for k in d["fixed"]:
                print(f"    ✓ {k}")
        if d["new"]:
            print(f"新增断言: {len(d['new'])}")
        for n in d["token_notes"]:
            print(f"    [token 回归] {n}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Prompt 回归任务集跑批")
    ap.add_argument("--only", default="", help="只跑指定任务 id（逗号分隔）")
    ap.add_argument("--tasks", default=str(TASKS_FILE), help="任务集文件路径")
    ap.add_argument("--profile", default="auto", choices=["auto", "prod", "minimal"])
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--step-limit", type=int,
                    default=int(os.getenv("QD_EVAL_STEP_LIMIT", "8")),
                    help="单轮步数预算（跑批必须限步，否则取数卡住的任务会跑到几十万 token）")
    ap.add_argument("--update-baseline", action="store_true", help="把本次结果存为基线")
    ap.add_argument("--no-diff", action="store_true", help="不与 baseline 对比")
    ap.add_argument("--validate", action="store_true", help="只校验任务集结构，不调 LLM")
    ap.add_argument("--out", default="", help="报告输出路径（默认 latest.json）")
    args = ap.parse_args()

    tasks = load_tasks(Path(args.tasks))
    errs = validate_tasks(tasks)
    if errs:
        print("任务集校验失败:")
        for e in errs:
            print("  -", e)
        return 2
    if args.validate:
        print(f"任务集校验通过：{len(tasks)} 条，断言合计 "
              f"{sum(len(t.get('checks') or {}) for t in tasks)} 项")
        return 0

    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        tasks = [t for t in tasks if t["id"] in want]
        if not tasks:
            print(f"--only 未命中任何任务: {args.only}")
            return 2

    if not (os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")):
        print("未配置 OPENAI_API_KEY，无法跑真实端点回归（先配 .env）", file=sys.stderr)
        return 2

    try:
        from app.agent.tools.availability import probe_tools
    except ImportError:
        from tools.availability import probe_tools
    availability = probe_tools()
    print("工具可用性:", {k: v.get("available") for k, v in availability.items()})

    svc, profile = build_service(args.profile)
    dom = _domain_map()

    results = []
    for t in tasks:
        print(f"[{t['id']}] 运行中…", flush=True)
        res = run_task(svc, t, dom, availability, args.timeout, args.step_limit)
        res["checks"] = evaluate(t, res)
        results.append(res)

    report = {
        "version": 1,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "profile": profile,
        "model": os.getenv("OPENAI_MODEL", ""),
        "tasks_file": str(args.tasks),
        "availability": {k: v.get("available") for k, v in availability.items()},
        "results": results,
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else LATEST_FILE
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    d = None
    if args.update_baseline:
        BASELINE_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已更新基线: {BASELINE_FILE}")
    elif not args.no_diff and BASELINE_FILE.exists():
        baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        d = diff(baseline, report)

    print_report(report, d)

    failed = [r for r in results if r["status"] != "ok"
              or any(not c["ok"] for c in r["checks"])]
    if d and d["regressions"]:
        print(f"❌ 存在回归 {len(d['regressions'])} 项")
        return _finish(1)
    if failed:
        # 无基线时的 FAIL 也算红（首次建立基线后以 diff 为准）
        print(f"❌ {len(failed)} 条任务未通过（无基线时以绝对断言为准）")
        return _finish(1)
    print("✅ 无回归")
    return _finish(0)


def _finish(code: int) -> int:
    """收尾强退。

    【为什么 os._exit】跑批会留下非守护线程：DB 连接池（trace_collector → chain.store）、
    盘后评估 worker、以及超时被弃的任务线程。实测报告已落盘但进程挂住 20+ 分钟不退，
    CI 里就是永久 hang。故手动 flush 后强退（报告文件已 write_text 关闭）。
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
