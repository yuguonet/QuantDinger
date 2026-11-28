# -*- coding: utf-8 -*-
"""
market-screener 选人 / deep 层探针（2026-11-27 用户增补）

用途：直接运行 Agent CLI（app/agent/cli.py）输入选股消息，跑通完整流水线
（run_skill(market-screener) → 漏斗+多轮 deep），把 skill 流水线**完整载荷**
取回，供审查：选人层选股是否合理、deep 层各环节（漏斗/粗筛/每轮 deep/交付）
是否合适、是否是最优解、有无改进空间。

链路说明：CLI 走 message_queue 执行完整 agent（LLM + run_skill），skill 全量结果
落 QD_SKILL_JOB_DIR（result.json，交付载荷）与 tmp/skill_output 证据快照
（coarse_audit 完整 picked_detail / fine_audit 每轮完整 rank+满卡）。本探针只负责
读回并组装探针 dump，不参与选股取舍。

用法（在 backend_api_python/ 下，建议用项目 .venv 的 python 运行，保证依赖齐全）：
    python scripts/market_screener_probe.py "明天买什么股?"
    python scripts/market_screener_probe.py            # 默认消息「明天买什么股?」

输出：
  1) stdout 打印结构化探针摘要（selector 层漏斗/粗筛/板块/涨停参考 + deep 层各轮/满卡/
     交付总量 + 全量证据块计数 + 最终报告摘要）；
  2) 全量载荷（含 coarse_audit + fine_audit 全量证据）写 <job_dir>/probe_dump.json，
     供逐层审阅（选人层漏斗/粗筛/板块/涨停参考；deep 层每轮 deep 明细与满卡证据）。
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SKILL_OUT = _PROJECT_ROOT / "tmp" / "skill_output"
_DEFAULT_JOB_SUBDIR = "probe_jobs"
_DEFAULT_MESSAGE = "明天买什么股?"
_SNAPSHOT_LATEST = _SKILL_OUT / "latest_market-screener.json"


def _venv_python() -> str:
    """优先用项目 .venv 的 python（依赖齐全），否则回落 sys.executable。

    .venv 可能在项目根（_PROJECT_ROOT/.venv）或 backend_api_python 同级（父目录 .venv），
    两处都探一下。
    """
    for base in ((_PROJECT_ROOT / ".venv"), (_PROJECT_ROOT.parent / ".venv")):
        py = base / "Scripts" / "python.exe"
        if py.exists():
            return str(py)
    return sys.executable


def _run_cli(message: str, job_dir: Path) -> dict:
    """运行 Agent CLI（单次消息），返回 {stdout, stderr, returncode, elapsed_s}。

    不设 QD_SKILL_JOB_DIR：run_skill 会按父进程作业目录写 result.json，但会覆盖该
    环境变量（指向 agent 自己的 jobs/<ts>/ 作业目录），故交付载荷从 jobs/ 取最新
    market-screener 的 result.json。
    """
    py = _venv_python()
    cmd = [py, "-m", "app.agent.cli", message, "--session", "market_screener_probe"]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(_PROJECT_ROOT),
                              capture_output=True, text=True, timeout=1800)
        return {"stdout": proc.stdout, "stderr": proc.stderr,
                "returncode": proc.returncode, "elapsed_s": round(time.time() - t0, 2)}
    except subprocess.TimeoutExpired as e:
        return {"stdout": (e.stdout or "") if isinstance(e.stdout, str) else "",
                "stderr": (e.stderr or "") if isinstance(e.stderr, str) else "",
                "returncode": -1, "elapsed_s": round(time.time() - t0, 2),
                "timeout": "1800s"}


def _load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"__load_error__": f"读取 {path.name} 失败: {e}"}


def _collect_snapshots(snapshot_dir: Path) -> dict:
    """读取证据快照，按 meta.round 区分 coarse/fine，返回 {coarse_audit, fine_audit}。"""
    snaps = {}
    if not snapshot_dir.exists():
        return snaps
    for p in sorted(snapshot_dir.glob("*_market-screener.json")):
        if p.name == "latest_market-screener.json":
            continue
        d = _load_json(p)
        if not isinstance(d, dict):
            continue
        meta = d.get("meta") or {}
        round_ = str(meta.get("round") or "").lower()
        deliver = d.get("deliver", d)
        if not isinstance(deliver, dict):
            continue
        if round_ == "coarse" and "coarse_audit" in deliver:
            snaps["coarse_audit"] = deliver["coarse_audit"]
        elif round_ == "fine" and "fine_audit" in deliver:
            snaps["fine_audit"] = deliver["fine_audit"]
    # latest 固定名（最近一次，多为 fine 轮）再覆盖一次
    latest = _load_json(_SNAPSHOT_LATEST)
    if isinstance(latest, dict):
        deliver = latest.get("deliver", latest)
        if isinstance(deliver, dict) and "fine_audit" in deliver:
            snaps.setdefault("fine_audit", deliver["fine_audit"])
    return snaps


def _selector_summary(coarse_audit: dict, delivered: dict) -> dict:
    """selector 层摘要：漏斗统计 + 粗筛 picked_detail/limit_up_ref + topdown_notes。"""
    s = {}
    funnel = delivered.get("funnel") or {}
    s["funnel"] = {k: funnel.get(k) for k in
                   ("pool_raw", "baseline", "deep_n", "zt_ref_n", "limit",
                    "dup_max", "delivered_cards", "delivered_rank_rows",
                    "fine_rounds", "rounds", "not_delivered_n", "coarse_kept") if k in funnel}
    if coarse_audit:
        pd = coarse_audit.get("picked_detail") or []
        kinds = collections.Counter(x.get("kind") for x in pd if isinstance(x, dict))
        s["coarse_audit"] = {
            "picked_detail_n": len(pd),
            "picked_detail_kinds": dict(sorted(kinds.items())),
            "limit_up_ref_n": len(coarse_audit.get("limit_up_ref") or []),
            "topdown_notes_n": len(coarse_audit.get("topdown_notes") or []),
        }
    return s


def _deep_summary(delivered: dict, full: dict) -> dict:
    """deep 层摘要：交付载荷（rounds/满卡/紧凑 rank/notes）+ 全量 per-round 证据块。"""
    s = {}
    rounds = delivered.get("rounds") or []
    s["rounds"] = {
        "n_rounds": len(rounds),
        "per_round": [{"round": r.get("round"), "codes": len(r.get("codes") or []),
                       "deep_n": r.get("deep_n"), "cards": r.get("cards"),
                       "rank_rows": r.get("rank_rows"),
                       "delivered": r.get("delivered")} for r in rounds],
    }
    for k in ("analyzed", "analyzed_rank", "deliverability_note", "analyzed_note",
              "candidate_integrity", "delivery_note", "notes", "fine_audit",
              "market", "mood_regime", "selection_snapshot", "run_started_at"):
        if k in delivered:
            s[k] = delivered[k]
    if full:
        s["full_per_round"] = {
            "n_rounds": len(full.get("per_round") or []),
            "total_rank_rows": sum(len(r.get("analyzed_rank") or [])
                                    for r in (full.get("per_round") or [])),
            "total_cards": sum(len(r.get("analyzed") or [])
                               for r in (full.get("per_round") or [])),
        }
    return s


def _print_summary(dump: dict) -> None:
    """stdout 打印结构化探针摘要（含关键计数与最终报告）。"""
    final = dump.get("final_report") or ""
    print("=" * 70)
    print("[market-screener 选人/deep 探针] 消息:", dump.get("message"))
    print("=" * 70)
    print("最终报告（stdout）前 4000 字:\n")
    print((final or "(无输出)")[:4000])
    sel = dump.get("selector_summary") or {}
    deep = dump.get("deep_summary") or {}
    print("\n--- selector 层 ---")
    print(json.dumps(sel, ensure_ascii=False, indent=1)[:4000])
    print("\n--- deep 层 ---")
    print(json.dumps(deep, ensure_ascii=False, indent=1)[:6000])
    print("\n探针 dump 全量载荷（含 coarse_audit/fine_audit 全量证据）已写:",
          dump.get("probe_dump_path"))


def main() -> int:
    message = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].strip() else _DEFAULT_MESSAGE
    job_dir = _SKILL_OUT / _DEFAULT_JOB_SUBDIR / f"probe_{int(time.time())}"
    job_dir.mkdir(parents=True, exist_ok=True)
    dump_res = _run_cli(message, job_dir)
    stdout = dump_res["stdout"]
    if dump_res["returncode"] not in (0, None):
        print(f"[probe] CLI 退出码 {dump_res['returncode']}（{dump_res.get('timeout','')}）",
              file=sys.stderr)
        print(stdout, end="")
        return dump_res["returncode"]

    # 交付载荷：取 jobs/ 里最新的 market-screener result.json（run_skill 落盘，
    # 含漏斗/粗筛/细筛/满卡/notes 等交付载荷；fine_audit/coarse_audit 在证据快照里）。
    jobs_dir = _SKILL_OUT / "jobs"
    cand = []
    if jobs_dir.exists():
        for p in jobs_dir.rglob("*market-screener*/result.json"):
            cand.append((p, p.stat().st_mtime if p.stat().st_mtime > 0 else 0))
    cand.sort(key=lambda x: x[1], reverse=True)
    delivered_path = cand[0][0] if cand else None
    delivered = _load_json(delivered_path) if delivered_path else None
    if not isinstance(delivered, dict):
        print(f"[probe] 未取到交付载荷（最新 market-screener result.json: {delivered_path}）",
              file=sys.stderr)
        return 1
    snaps = _collect_snapshots(_SKILL_OUT)
    dump = {
        "message": message,
        "final_report": stdout,
        "elapsed_s": dump_res["elapsed_s"],
        "returncode": dump_res["returncode"],
        "probe_job_dir": str(job_dir),
        "delivered": delivered,
        "selector_summary": _selector_summary(snaps.get("coarse_audit"), delivered),
        "deep_summary": _deep_summary(delivered, snaps.get("fine_audit")),
        "full_evidence": {
            "coarse_audit": snaps.get("coarse_audit"),
            "fine_audit": snaps.get("fine_audit"),
        },
    }
    probe_dump_path = job_dir / "probe_dump.json"
    probe_dump_path.write_text(json.dumps(dump, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    dump["probe_dump_path"] = str(probe_dump_path)
    _print_summary(dump)
    return 0


if __name__ == "__main__":
    sys.exit(main())
