# -*- coding: utf-8 -*-
"""blackbox_runner — F21 标准对照臂：外部 blackbox 后端 vs 主链 QD agent（2026-10-18）。

【它是什么】提智可验收的**外部基线**：把同一批能力任务分别喂给外部 blackbox
编码 agent（opencode / claude_code / codex，走各自 CLI）与主链 QD agent，用
**同一把尺**（qd_bench.score_answer）打质量分，判定「被测后端质量不低于主链
-QUALITY_TOL」。它不碰主链路任何代码——只是一个跑批+灌尺的 scripts 侧工具。

【为什么需要它】qd_dispatch_ab 的对照臂是「摘 run_skill 的内生对照」，回答的是
"分发通道值不值"；回答不了"我们的 agent 和外部通用编码 agent 差多少"。F21 补的
就是这个外部基线（总账 I2/R5：引入标准化对照臂，喂同款尺，不动主链路）。

【与 qd_dispatch_ab 的关系（复用，不重写）】
  · 评分：`qd_bench.score_answer`（能力标尺，同 C 项口径——两把尺子量同一实验，
    结论没法跟 benchmark 历史对齐，故禁止另写评分）；
  · 判定：`qd_dispatch_ab._verdict`（同一个函数对象，阈值语义逐字沿用）；
  · 主链臂：`qd_dispatch_ab._build_service` / `_run_arm`（同款装配与 token delta 口径）。
  报告 schema 与 qd_dispatch_ab **同构**（ts/model/step_limit/records/verdict/
  thresholds），records 的臂槽位语义一致：**dispatch 槽 = 被测方，inline 槽 = 基线**。
  此处 dispatch 槽由外部 blackbox 后端占据，inline 槽是主链 QD agent。

【⚠️ 成本维对 CLI 臂不判（cost_judged=false）】外部 CLI 不暴露 token 计量 ⇒
blackbox 臂无 tokens 字段 ⇒ `_verdict` 的成本比恒为 0.0（`float(x or 0)` 的机械
结果，**不是**"零消耗"）。本工具只认质量维判定；per_task 打印与报告里成本一律
显示「—（CLI 无计量）」。要成本对照请用 qd_dispatch_ab（内生双臂都有 token）。

【输出两种，各侍其主】
  1. preds JSONL（`--out`）：qd_bench preds 格式（task_id/answer/corpus/meta）⇒
     `python -m app.agent.scripts.qd_bench score --preds x.jsonl --model <backend>`
     落 run ⇒ `qd_bench diff --a <qd_run> --b <blackbox_run>` / `agreement` 直接消费；
  2. 报告 JSON（`--report`，缺省落 `tmp/blackbox_arm/`）：qd_dispatch_ab 同构 schema，
     `--rejudge` 可换阈值复判（不必重跑实验）。

【用法】
  python scripts/blackbox_runner.py                       # 自动选第一个可用后端
  python scripts/blackbox_runner.py --backend opencode    # 指定后端
  python scripts/blackbox_runner.py --backend claude_code --tasks screen_01,screen_04
  python scripts/blackbox_runner.py --backend codex --query "帮我做一次全市场短线强势股筛选"
  python scripts/blackbox_runner.py --rejudge report.json --quality-tol 8

【退出码】0=判定通过；1=质量不达标；2=环境不可用（PATH 无任何后端 CLI / 服务装配失败）。
【不进 CI】依赖外部 CLI，缺可执行即 skip 并明示（exit 2，不是失败）。

【易错点】
  1. 后端命令模板按「query 作位置参数」的通用形态拼；各 CLI 版本参数漂移常见，
     实测不通用 env `QD_BLACKBOX_CMD_<BACKEND>` 覆写整条命令（`{query}` 占位），
     **不要改检测口径**（检测只认可执行名在不在 PATH）；
  2. 主链臂与 blackbox 臂**必须同题面同预算**（step_limit 一致），否则质量Δ不可归因；
  3. `--rejudge` 只复判**本工具产出的双臂报告**（records 含 dispatch/inline 槽）；
  4. qd 臂 tokens=0（模型无 token_stats）时 `_verdict` 成本维记 None ⇒ failures 里
     会出现一行「成本比 None > 0.4」——本工具成本维不判（cost_judged=false），
     该行可忽略；要成本对照用 qd_dispatch_ab（内生双臂都有 token）。
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 路径引导：只插 backend_api_python 根（P5 口径——不插 app/agent，避免 M-1 双模块）。
HERE = Path(__file__).resolve().parent          # backend_api_python/scripts
BACKEND_ROOT = HERE.parent                       # backend_api_python
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# 复用（不重写）：评分尺 / 判定 / 主链臂装配与执行 / 兜底题面基线 / 默认任务
from app.agent.scripts.qd_bench import load_tasks, score_answer            # noqa: E402
from app.agent.scripts.qd_dispatch_ab import (                             # noqa: E402
    DEFAULT_TASKS, FALLBACK_BASELINE, _build_service, _run_arm, _verdict,
)

REPORT_DIR = BACKEND_ROOT / "tmp" / "blackbox_arm"

# 后端顺序 = 自动选择的优先级；命令模板可被 QD_BLACKBOX_CMD_<BACKEND> 覆写
_BACKEND_ORDER = ["opencode", "claude_code", "codex"]
_DEFAULT_CMD_TEMPLATES: Dict[str, str] = {
    "opencode": "opencode run {query}",
    "claude_code": "claude -p {query}",
    "codex": "codex exec {query}",
}


def _backend_timeout() -> int:
    """单后端跑批墙钟（秒）。QD_BLACKBOX_TIMEOUT，缺省 300；非正/非法回落缺省。"""
    raw = (os.getenv("QD_BLACKBOX_TIMEOUT") or "").strip()
    if not raw:
        return 300
    try:
        v = int(raw)
    except ValueError:
        print(f"[WARN] QD_BLACKBOX_TIMEOUT 非法（{raw!r}），回落 300")
        return 300
    if v <= 0:
        print(f"[WARN] QD_BLACKBOX_TIMEOUT={v} 非正，回落 300")
        return 300
    return v


def _cmd_template(backend: str) -> str:
    """后端命令模板：env `QD_BLACKBOX_CMD_<BACKEND>` 优先，否则内置通用形态。"""
    override = (os.getenv(f"QD_BLACKBOX_CMD_{backend.upper()}") or "").strip()
    if override:
        return override
    return _DEFAULT_CMD_TEMPLATES.get(backend, "")


def _backend_exe(backend: str) -> str:
    """模板首个 token = 可执行名（检测口径：PATH 里有没有它）。"""
    tpl = _cmd_template(backend)
    try:
        parts = shlex.split(tpl)
    except ValueError:
        return ""
    return parts[0] if parts else ""


def detect_backends() -> List[str]:
    """PATH 里可用的后端（按 _BACKEND_ORDER 序）。缺哪个都明示，不静默跳过。"""
    return [b for b in _BACKEND_ORDER
            if _backend_exe(b) and shutil.which(_backend_exe(b))]


def _run_backend(backend: str, query: str, timeout_s: int) -> str:
    """跑一个 blackbox 后端 → stdout 全文作为答案。

    seam：测试 monkeypatch 本函数（不真起 CLI）。运行时任何失败都抛 RuntimeError
    （上层按臂失败记入 verdict，与 qd_dispatch_ab 臂失败同口径）。
    """
    tpl = _cmd_template(backend)
    if not tpl:
        raise RuntimeError(f"未知后端 {backend!r}（可用：{_BACKEND_ORDER}）")
    if "{query}" not in tpl:
        raise RuntimeError(f"后端 {backend} 命令模板缺 {{query}} 占位：{tpl}")
    parts = shlex.split(tpl.replace("{query}", query))
    # ⚠️ Windows：shutil.which 解析出带扩展名的全路径（npm shim 是 .CMD），
    # 而 subprocess 列表形式不过 PATHEXT——裸名 "opencode" 会 WinError 2。
    # 必须用 which() 的返回值作 argv[0]（实测 opencode 1.19.3：全路径 .CMD 可直接跑）。
    exe = shutil.which(parts[0])
    if not exe:
        raise RuntimeError(f"后端可执行不在 PATH：{parts[0]}")
    cmd = [exe] + parts[1:]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"后端超时 {timeout_s}s（QD_BLACKBOX_TIMEOUT 可调）") from e
    out = (p.stdout or "").strip()
    if p.returncode != 0 and not out:
        raise RuntimeError(f"后端退出 {p.returncode}: {(p.stderr or '')[:200]}")
    return out


def _quality(task_id: str, query: str, answer: str, corpus: str = "") -> float:
    """质量分：与 qd_dispatch_ab/qd_bench 同一把尺（score_answer），任务基线取能力任务集。

    与 qd_dispatch_ab._quality 同逻辑（任务查找 + fallback 基线 + score_answer），
    但走包内绝对导入的 score_answer——不借 qd_dispatch_ab 的 `import qd_bench`
    顶层副本（那是入口脚本 sys.path 引导的产物，共享进程里会长出双模块）。
    """
    try:
        task = next((t for t in load_tasks() if t.get("id") == task_id), None)
    except Exception:
        task = None
    if task is None:
        task = {"id": task_id or "ad_hoc", "category": "选股", "query": query,
                "baseline": FALLBACK_BASELINE}
    return float(score_answer(task, answer, corpus)["total"])


def _queries(args) -> List[Tuple[str, str]]:
    """题面清单：--query 单发 / --tasks 过滤能力任务集（同 qd_dispatch_ab 口径）。"""
    if args.query:
        return [("ad_hoc", args.query)]
    want = [t.strip() for t in (args.tasks or ",".join(DEFAULT_TASKS)).split(",")
            if t.strip()]
    try:
        tasks = {t.get("id"): t for t in load_tasks()}
    except Exception:
        tasks = {}
    out: List[Tuple[str, str]] = []
    for tid in want:
        t = tasks.get(tid)
        if t and (t.get("query") or "").strip():
            out.append((tid, str(t["query"])))
        else:
            print(f"  [SKIP] 任务 {tid} 不在能力任务集，改用 fallback 题面")
            out.append((tid, "帮我做一次全市场短线强势股筛选，总结条件和要点"))
    return out


def run_experiment(backend: str, queries: List[Tuple[str, str]], step_limit: int = 0,
                   model: str = "", timeout_s: int = 0) -> dict:
    """双臂对照：dispatch 槽 = blackbox 后端（被测），inline 槽 = 主链 QD（基线）。"""
    timeout_s = timeout_s or _backend_timeout()
    svc = _build_service("dispatch", step_limit)   # 主链全工具面（与 qd_dispatch_ab 同口径）
    records: List[dict] = []
    preds: List[dict] = []
    for tid, query in queries:
        row: Dict[str, Any] = {"task_id": tid, "query": query}
        # ── dispatch 槽：被测 blackbox 后端 ──
        try:
            t0 = time.time()
            ans = _run_backend(backend, query, timeout_s)
            row["dispatch"] = {"elapsed_s": round(time.time() - t0, 2),
                               "answer_len": len(ans), "answer": ans,
                               "quality": _quality(tid, query, ans)}
            preds.append({"task_id": tid, "answer": ans, "corpus": "",
                          "meta": {"model": backend, "arm": "blackbox"}})
        except Exception as e:
            row["dispatch"] = {"error": f"{type(e).__name__}: {e}"}
        # ── inline 槽：主链 QD agent（对照基线；token delta 口径同 qd_dispatch_ab）──
        try:
            r = _run_arm(svc, query, f"bb_{backend}_{tid}")
            r["quality"] = _quality(tid, query, r.pop("answer"), r.pop("corpus"))
            row["inline"] = r
        except Exception as e:
            row["inline"] = {"error": f"{type(e).__name__}: {e}"}
        records.append(row)
        d, i = row.get("dispatch") or {}, row.get("inline") or {}
        if "error" in d or "error" in i:
            print(f"  [{tid}] 臂失败 dispatch={d.get('error', '-')} "
                  f"inline={i.get('error', '-')}")
        else:
            print(f"  [{tid}] {backend} quality={d['quality']} "
                  f"({d['elapsed_s']}s) · qd quality={i['quality']} "
                  f"(tokens={i['tokens']} steps={i['steps']})")
    v = _verdict(records)
    return {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "model": model or "default",
            "backend": backend, "step_limit": step_limit, "records": records,
            "verdict": v, "preds": preds,
            "thresholds": {"cost_max": v["cost_max"], "quality_tol": v["quality_tol"],
                           "cost_judged": False},
            "kind": "blackbox_vs_qd"}


def save_report(rep: dict, path: str = "") -> Path:
    out = Path(path) if path else REPORT_DIR / (
        "blackbox_" + rep.get("backend", "x") + "_"
        + time.strftime("%Y%m%d_%H%M%S") + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def save_preds(rep: dict, path: str) -> Path:
    """blackbox 臂答案落 qd_bench preds JSONL（供 score/diff/agreement 消费）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rep["preds"])
                 + "\n", encoding="utf-8")
    return p


def _print_report(rep: dict) -> None:
    v = rep.get("verdict") or {}
    print(f"\n== blackbox 对照（{rep.get('backend')} vs qd）· "
          f"{'PASS' if v.get('pass') else 'FAIL'} "
          f"（质量容差 -{v.get('quality_tol')}；成本不判：CLI 无 token 计量）==")
    for row in v.get("per_task") or []:
        rec = next((r for r in rep.get("records", [])
                    if r.get("task_id") == row.get("task_id")), {})
        d, i = rec.get("dispatch") or {}, rec.get("inline") or {}
        _dq = d.get("quality")
        _iq = i.get("quality")
        _dqs = "臂失败" if _dq is None else str(_dq)
        _iqs = "臂失败" if _iq is None else str(_iq)
        print(f"  {str(row.get('task_id')):<12} {rep.get('backend')}={_dqs:<6} "
              f"qd={_iqs:<6} 质量Δ {row.get('quality_delta')} "
              f"[{'✓' if row['pass_quality'] else '✗'}]  成本 —（CLI 无计量）")
    for f in v.get("failures") or []:
        print(f"  [FAIL] {f}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="blackbox_runner",
        description="F21 标准对照臂：外部 blackbox 后端 vs 主链 QD agent（同款尺）")
    ap.add_argument("--backend", default="",
                    help="blackbox 后端（opencode|claude_code|codex；缺省自动选第一个可用）")
    ap.add_argument("--tasks", default="",
                    help="逗号分隔 bench 任务 id（默认同 qd_dispatch_ab）")
    ap.add_argument("--query", default="", help="自定义题面（与 --tasks 二选一）")
    ap.add_argument("--step-limit", type=int, default=0)
    ap.add_argument("--model", default="", help="报告 meta 用（不影响后端选择）")
    ap.add_argument("--timeout", type=int, default=0,
                    help="单后端墙钟秒（缺省 QD_BLACKBOX_TIMEOUT=300）")
    ap.add_argument("--out", default="", help="blackbox 臂 preds JSONL 输出路径")
    ap.add_argument("--report", default="", help="报告 JSON 输出路径（缺省落 tmp/blackbox_arm/）")
    ap.add_argument("--rejudge", default="", help="复判既有报告（可配 --quality-tol）")
    ap.add_argument("--quality-tol", type=float, default=5.0)
    args = ap.parse_args(argv)

    if args.rejudge:
        rep = json.loads(Path(args.rejudge).read_text(encoding="utf-8"))
        recs = rep.get("records") or []
        if not any("dispatch" in r and "inline" in r for r in recs):
            print("[ENV] 该报告不是双臂记录（records 缺 dispatch/inline 槽），无从复判")
            return 2
        rep["verdict"] = _verdict(recs, quality_tol=args.quality_tol)
        _print_report(rep)
        return 0 if rep["verdict"]["pass"] else 1

    # ── 后端检测：缺可执行 skip 并明示（exit 2，不是失败）──
    available = detect_backends()
    if not available:
        print(f"[SKIP] PATH 无可用 blackbox 后端（检测：{_BACKEND_ORDER}）——"
              f"装一个 CLI 或用 QD_BLACKBOX_CMD_<BACKEND> 指定可执行；"
              f"本工具依赖外部 CLI，不进 CI")
        return 2
    backend = args.backend or available[0]
    if backend not in _BACKEND_ORDER:
        print(f"[ENV] 未知后端 {backend!r}（可用：{_BACKEND_ORDER}）")
        return 2
    if backend not in available:
        print(f"[SKIP] 后端 {backend} 不可用（PATH 无 {_backend_exe(backend)}；"
              f"当前可用：{available}）")
        return 2

    queries = _queries(args)
    if not queries:
        print("[ENV] 无可用任务题面")
        return 2
    try:
        rep = run_experiment(backend, queries, step_limit=args.step_limit,
                             model=args.model, timeout_s=args.timeout)
    except Exception as e:
        print(f"[ENV] 实验装配失败: {type(e).__name__}: {e}")
        return 2
    rep["verdict"] = _verdict(rep["records"], quality_tol=args.quality_tol)
    rep["thresholds"] = {"cost_max": rep["verdict"]["cost_max"],
                         "quality_tol": rep["verdict"]["quality_tol"],
                         "cost_judged": False}
    rp = save_report(rep, args.report)
    if args.out:
        pp = save_preds(rep, args.out)
        print(f"preds（blackbox 臂）: {pp}")
        print(f"  消费：python -m app.agent.scripts.qd_bench score --preds {pp} "
              f"--model {backend}（随后 qd_bench diff/agreement）")
    print(f"报告: {rp}")
    _print_report(rep)
    return 0 if rep["verdict"]["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
