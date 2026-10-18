# -*- coding: utf-8 -*-
"""qd_bench — 能力级评测（改进路线 C，2026-10-10 立）。

【它是什么】质量标尺：跑固定任务集 → 输出**总分 + 分项**，落 JSONL 可历史对比。
解决的是"升级模型/prompt 后更好还是更坏只能靠感觉"——E/F/G/K 调优四件与将来 SFT
都以它为标尺。

【与 qd_prompt_eval.py 的分工】（不要合并，两者判据不同）：
  qd_prompt_eval  回归级：可机检断言（调没调工具/说没说某句），PASS/FAIL，防改坏
  qd_bench        能力级：连续分（0~100），总分+分项，防"没改坏但也没变好"

【评分维度】（权重见 DIM_WEIGHTS；任务可用 `dims:` 声明子集，human 维度走标注文件）
  coverage  30  baseline.must 最低要求命中率（好答案的硬门槛，逐条机器可判）
  grounding 25  数字可溯源 + 合规禁用词 + 无绝对化承诺（复用 utils/grounding）
  claims    20  可验证声明密度：方向/幅度/价位/置信度（复用 chain/claims）
  structure 15  结论先行 / 风险提示 / 多维分析 / 篇幅
  human     10  人工标注分（`qd_bench agreement` 对账两人一致率 ≥80% 是验收线）

【用法】
  python -m scripts.qd_bench validate                # 校验任务集（不调模型）
  python -m scripts.qd_bench score --preds p.jsonl   # 对已有输出打分（确定性）
  python -m scripts.qd_bench run [--only id1,id2]    # 真实端点跑任务集 + 打分
  python -m scripts.qd_bench run --num-rollouts 3    # 重复采样 3 次聚合成 mean±std
  python -m scripts.qd_bench trend                   # 历史曲线（总分+分项）
  python -m scripts.qd_bench diff --a ID --b ID      # 两次 run 的任务级对比
  python -m scripts.qd_bench agreement --a a.json --b b.json --tol 10
  python -m scripts.qd_bench selftest                # 内置 fixture 自洽校验（零网络）
  python -m scripts.qd_bench import-traces --limit 30 --out extra_tasks.yaml

【preds 文件格式】JSONL，每行：
  {"task_id": "stock_01", "answer": "...", "corpus": "工具输出拼接（可选，供溯源）",
   "meta": {"model": "...", "prompt_rev": "..."}}

【退出码】0=正常；1=校验/对比/selftest 不通过；2=环境或预算条件不满足
（缺模型端点/DB，或 --num-rollouts 超 QD_BENCH_MAX_ROLLOUTS 上限被拒——
拒绝执行而非失败，防误触烧钱）。

【落库】`score`/`run` 默认只落本地 JSONL（`scripts/bench/runs/`）；加 `--to-db` 才
挂 qd_traces 树（EvalNode：root=bench_run，child=任务）。DB 不可用一律 fail-open
跳过——benchmark 是标尺，标尺自己不能因为环境缺件而不出分。

【selftest】`qd_bench selftest` 用内置 fixture（好/坏/缺项三档答案 + 标注对）校验
评分机制自身：可分辨性 / 确定性 / must 逐条可判 / agreement 口径，零网络零外部依赖。
改评分函数后先跑它——红了说明标尺自己坏了，跑分结果不可信。

【rollouts】`run --num-rollouts N` 把任务集重复采样 N 次：逐轮各落一个 run
（run_id 后缀 `_r{i}`，trend/diff/agreement 可直接消费），另存聚合记录
（mean±std，落 `runs/rollouts/`，trend/diff 不读它——聚合是统计记录不是可比 run）。
N 受 env `QD_BENCH_MAX_ROLLOUTS` 上限约束（缺省 5，非正/非法回落缺省并告警），
超限在触服务前直接拒绝（exit 2）。每轮本身仍受 `AGENT_RUN_TOKEN_BUDGET`
单 run 预算约束 ⇒ N 轮总成本 ≈ N × 单轮。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 路径引导：与 qd_prompt_eval 同款（AGENT_DIR 必须排在 BACKEND_ROOT 前，
# 否则 `import utils` 命中 app/utils —— 详见 qd_smoke.py 头部导入约定）。
HERE = Path(__file__).resolve().parent          # app/agent/scripts
AGENT_DIR = HERE.parent                          # app/agent
BACKEND_ROOT = AGENT_DIR.parent.parent           # backend_api_python
for _p in (str(BACKEND_ROOT), str(AGENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from app.agent import mimo_boot
    _BOOT_ERR = ""
except Exception as _e:            # validate/score/agreement 不依赖执行核，软失败
    _BOOT_ERR = f"{type(_e).__name__}: {_e}"

TASKS_FILE = HERE / "bench_tasks.yaml"
BENCH_DIR = Path(os.getenv("QD_BENCH_DIR", str(HERE / "bench")) or (HERE / "bench"))
RUNS_DIR = BENCH_DIR / "runs"
LATEST_FILE = BENCH_DIR / "latest.json"

# 维度权重（Σ=100；human 未标注时按比例摊回其余维度——不许"没标注=白扣分"）
DIM_WEIGHTS: Dict[str, int] = {
    "coverage": 30, "grounding": 25, "claims": 20, "structure": 15, "human": 10,
}
DEFAULT_DIMS = ["grounding", "claims", "structure", "coverage"]

# ── 机检规则（baseline.must 的检查项 id → 判据）──────────────────
_FAKE_CERT = re.compile(r"必然|必定|保证(收益|盈利)|稳赚|包赚|100%|百分百|必涨|必跌|无风险套利")
_DIR_RE = re.compile(r"看多|看空|偏多|偏空|上涨|下跌|震荡|中性|买入|卖出|加仓|减仓|"
                     r"持有|补仓|观望|止盈|止损|割肉|突破|回调")
_HORIZON_RE = re.compile(r"T\+\d|未来[一二三四五六七1-7]+[天日周月]|短线|中线|本周|"
                         r"明天|明日|下周|一周|三天|一个月|季度")
_LEVEL_RE = re.compile(r"支撑|压力|目标价|止损位|区间|上轨|下轨|均线|年线|缺口")
_SCREEN_RE = re.compile(r"筛选|条件|入选|优选|选出|挑选|符合.{0,8}(条件|标准)|门槛")
_BASIS_RE = re.compile(r"依据|数据|显示|统计|来看|口径|来源|据.{0,8}(显示|统计)")
_RISK_RE = re.compile(r"风险|不构成投资建议|谨慎|注意|波动|亏损|回撤|不确定性")
_ACTION_RE = re.compile(r"买入|卖出|加仓|减仓|清仓|建仓|止盈|止损|持有|补仓|观望|"
                        r"减磅|出货|挂单|割肉")
_CONCL_RE = re.compile(r"结论|判断|综合|总体|建议|看多|看空|偏多|偏空|可以买|不建议|倾向于")
_DIMWORD_RE = re.compile(r"技术面|资金面|基本面|情绪面|估值|筹码|板块|消息面|量价|业绩")
_SOURCE_MARK_RE = re.compile(r"来源|据.{0,8}(显示|统计|数据)|口径|（[a-z_]{3,}）|\([a-z_]{3,}\)")


def _chk_direction(a: str, t: dict) -> bool:
    return bool(_DIR_RE.search(a[:800]))


def _chk_numbers(a: str, t: dict) -> bool:
    from app.agent.utils.grounding import extract_numbers
    return len(extract_numbers(a)) >= 3


def _chk_horizon(a: str, t: dict) -> bool:
    return bool(_HORIZON_RE.search(a))


def _chk_risk(a: str, t: dict) -> bool:
    return bool(_RISK_RE.search(a))


def _chk_levels(a: str, t: dict) -> bool:
    return bool(_LEVEL_RE.search(a))


def _chk_screen(a: str, t: dict) -> bool:
    return bool(_SCREEN_RE.search(a))


def _chk_basis(a: str, t: dict) -> bool:
    return bool(_BASIS_RE.search(a))


def _chk_action(a: str, t: dict) -> bool:
    return bool(_ACTION_RE.search(a))


def _chk_no_fake(a: str, t: dict) -> bool:
    return not _FAKE_CERT.search(a)


def _chk_compliance(a: str, t: dict) -> bool:
    from app.agent.utils.grounding import check_banned_phrases
    return not check_banned_phrases(a)


_CHECKS: Dict[str, Callable[[str, dict], bool]] = {
    "has_direction": _chk_direction,
    "has_numbers": _chk_numbers,
    "has_horizon": _chk_horizon,
    "has_risk_note": _chk_risk,
    "has_levels": _chk_levels,
    "has_screen_criteria": _chk_screen,
    "has_data_basis": _chk_basis,
    "action_defined": _chk_action,
    "no_fake_certainty": _chk_no_fake,
    "compliance_clean": _chk_compliance,
}


# ═══════════════════════════════════════════════════════════════
#  任务集
# ═══════════════════════════════════════════════════════════════

def load_tasks(path: Path = TASKS_FILE) -> List[dict]:
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tasks = data.get("tasks") or []
    if not isinstance(tasks, list):
        raise ValueError("tasks 字段必须是列表")
    return tasks


def validate_tasks(tasks: List[dict]) -> Tuple[bool, List[str]]:
    """任务集体检：数量/分类覆盖/唯一 id/检查项已知/权重有效。"""
    errs: List[str] = []
    if len(tasks) < 20:
        errs.append(f"任务数 {len(tasks)} < 20（路线 C 要求 20~50）")
    if len(tasks) > 50:
        errs.append(f"任务数 {len(tasks)} > 50（跑批成本上限）")
    seen = set()
    cats = set()
    for t in tasks:
        tid = str(t.get("id") or "")
        if not tid:
            errs.append("存在无 id 任务")
            continue
        if tid in seen:
            errs.append(f"id 重复: {tid}")
        seen.add(tid)
        cats.add(str(t.get("category") or ""))
        if not (t.get("query") or "").strip():
            errs.append(f"{tid}: query 为空")
        if float(t.get("weight", 1.0) or 0) <= 0:
            errs.append(f"{tid}: weight 非法")
        for d in (t.get("dims") or DEFAULT_DIMS):
            if d not in DIM_WEIGHTS or d == "human":
                errs.append(f"{tid}: 未知维度 {d}")
        must = ((t.get("baseline") or {}).get("must")) or []
        for m in must:
            if isinstance(m, str):
                if m not in _CHECKS:
                    errs.append(f"{tid}: 未知检查项 {m}")
            elif isinstance(m, dict):
                if not any(k in m for k in ("kw_any", "kw_all", "regex")):
                    errs.append(f"{tid}: 自定义规则缺 kw_any/kw_all/regex: {m}")
            else:
                errs.append(f"{tid}: must 条目类型非法: {m!r}")
    missing = {"个股分析", "选股", "复盘", "交易确认", "通用问答"} - cats
    if missing:
        errs.append(f"分类覆盖缺口: {sorted(missing)}")
    return not errs, errs


# ═══════════════════════════════════════════════════════════════
#  打分（确定性：同输入同输出，可回归对比）
# ═══════════════════════════════════════════════════════════════

def _eval_must_item(item: Any, answer: str, task: dict) -> bool:
    if isinstance(item, str):
        fn = _CHECKS.get(item)
        return bool(fn(answer, task)) if fn else False
    if isinstance(item, dict):
        if "kw_any" in item:
            return any(str(k) in answer for k in (item["kw_any"] or []))
        if "kw_all" in item:
            return all(str(k) in answer for k in (item["kw_all"] or []))
        if "regex" in item:
            return bool(re.search(str(item["regex"]), answer))
    return False


def _score_coverage(task: dict, answer: str) -> Tuple[float, dict]:
    must = ((task.get("baseline") or {}).get("must")) or []
    if not must:
        return 100.0, {"pass": 0, "total": 0, "missing": []}
    hits = [bool(_eval_must_item(m, answer, task)) for m in must]
    missing = [m if isinstance(m, str) else json.dumps(m, ensure_ascii=False)
               for m, h in zip(must, hits) if not h]
    return round(100.0 * sum(hits) / len(must), 1), {
        "pass": sum(hits), "total": len(must), "missing": missing}


def _score_grounding(task: dict, answer: str, corpus: str = "") -> float:
    from app.agent.utils.grounding import check_banned_phrases, check_grounding, extract_numbers
    banned = check_banned_phrases(answer)
    compliance = 100.0 if not banned else 50.0
    certainty = 100.0 if not _FAKE_CERT.search(answer) else 50.0
    if (corpus or "").strip():
        ok, _why = check_grounding(answer, corpus)
        numbers = 100.0 if ok else 30.0
    else:
        nums = extract_numbers(answer)
        if not nums:
            numbers = 80.0          # 无数字=无可编造数字；但也不给满分（无数据支撑）
        else:
            marks = len(_SOURCE_MARK_RE.findall(answer))
            numbers = 80.0 if marks >= 2 else 60.0 if marks == 1 else 35.0
    return round(0.4 * compliance + 0.3 * certainty + 0.3 * numbers, 1)


def _score_claims(task: dict, answer: str) -> float:
    try:
        from app.agent.chain.claims import extract_claims
        cs = extract_claims(task.get("query", ""), answer, "finance") or []
    except Exception:
        return 0.0
    if not cs:
        return 0.0
    types = {c.get("claim_type") for c in cs}
    s = 0.0
    s += 40.0 if "direction" in types else 0.0
    s += 20.0 if "magnitude" in types else 0.0
    s += 20.0 if "level" in types else 0.0
    s += 10.0 if any(c.get("confidence") is not None for c in cs) else 0.0
    s += 10.0 if any(c.get("horizon") for c in cs) else 0.0
    return round(s, 1)


def _score_structure(task: dict, answer: str) -> float:
    s = 0.0
    s += 30.0 if _CONCL_RE.search(answer[:150]) else 0.0
    s += 25.0 if _RISK_RE.search(answer) else 0.0
    dims = len(set(_DIMWORD_RE.findall(answer)))
    s += 25.0 if dims >= 2 else 12.0 if dims == 1 else 0.0
    n = len(answer or "")
    s += 20.0 if 200 <= n <= 4000 else (n / 200.0 * 20.0 if n < 200 else 10.0)
    return round(min(s, 100.0), 1)


def score_answer(task: dict, answer: str, corpus: str = "",
                 human_score: Optional[float] = None) -> dict:
    """单任务打分。返回 {dims, total, must} ——同输入恒同输出（确定性）。"""
    answer = answer or ""
    dims_want = list(task.get("dims") or DEFAULT_DIMS)
    dims: Dict[str, float] = {}
    if "coverage" in dims_want:
        cov, must = _score_coverage(task, answer)
        dims["coverage"] = cov
    else:
        must = {"pass": 0, "total": 0, "missing": []}
    if "grounding" in dims_want:
        dims["grounding"] = _score_grounding(task, answer, corpus)
    if "claims" in dims_want:
        dims["claims"] = _score_claims(task, answer)
    if "structure" in dims_want:
        dims["structure"] = _score_structure(task, answer)
    weights = {k: DIM_WEIGHTS[k] for k in dims}
    if human_score is not None:
        dims["human"] = round(float(human_score), 1)
        weights["human"] = DIM_WEIGHTS["human"]
    wsum = sum(weights.values()) or 1
    total = round(sum(dims[k] * weights[k] for k in dims) / wsum, 1)
    return {"dims": dims, "total": total, "must": must}


def score_batch(tasks: List[dict], preds: List[dict],
                annotations: Optional[Dict[str, float]] = None) -> dict:
    """整批打分 → run 记录（含总分/分项均分/逐任务）。"""
    by_id = {p.get("task_id"): p for p in preds if isinstance(p, dict)}
    annotations = annotations or {}
    rows: List[dict] = []
    for t in tasks:
        p = by_id.get(t.get("id"))
        if p is None:
            rows.append({"task_id": t.get("id"), "category": t.get("category"),
                         "status": "missing", "total": 0.0, "dims": {},
                         "must": {"pass": 0, "total": 0, "missing": []}})
            continue
        r = score_answer(t, str(p.get("answer") or ""), str(p.get("corpus") or ""),
                         human_score=annotations.get(t.get("id")))
        r.update({"task_id": t.get("id"), "category": t.get("category"),
                  "status": "ok", "weight": float(t.get("weight", 1.0) or 1.0)})
        rows.append(r)

    done = [r for r in rows if r["status"] == "ok"]
    wsum = sum(r.get("weight", 1.0) for r in done) or 1.0
    total = round(sum(r["total"] * r.get("weight", 1.0) for r in done) / wsum, 1) \
        if done else 0.0
    dim_avg: Dict[str, float] = {}
    for d in DIM_WEIGHTS:
        vals = [(r["dims"].get(d), r.get("weight", 1.0)) for r in done
                if d in r["dims"]]
        if vals:
            dim_avg[d] = round(sum(v * w for v, w in vals) / (sum(w for _, w in vals) or 1), 1)
    return {"total": total, "dims": dim_avg, "tasks": rows,
            "n_scored": len(done), "n_total": len(tasks)}


# ═══════════════════════════════════════════════════════════════
#  selftest（F19：内置 fixture 自洽校验，零网络零外部依赖）
# ═══════════════════════════════════════════════════════════════

# fixture 任务与 bench_tasks.yaml 的 stock_01 同构（dims 全开，must 四条），
# 答案按"全中/全不中/缺一项"三档构造——三档的差就是评分机制的判据本身。
_SELFTEST_TASK = {
    "id": "selftest_01",
    "category": "个股分析",
    "query": "帮我分析一下贵州茅台600519，未来三天怎么看",
    "weight": 1.0,
    "baseline": {
        "must": ["has_direction", "has_numbers", "has_horizon", "has_risk_note"],
    },
}
# 好答案：四条 must 全中，且无绝对化承诺/合规禁用词
_SELFTEST_GOOD = (
    "结论：贵州茅台600519短线偏多，未来三天以震荡上行为主，建议持有观望。"
    "依据数据显示，现价报1329.00元，日涨幅1.80%，成交量12.5万手，换手率0.45%。"
    "技术面站稳五日与十日年线，资金面主力净流入，上方压力1365元，下方支撑1300元，"
    "跌破止损位1288元可考虑减仓。"
    "风险提示：以上仅供参考，不构成投资建议，注意市场波动风险。"
)
# 坏答案：四条 must 全不中（与好答案形成严格可分差，防"满分壳"）
_SELFTEST_BAD = "建议关注该股。"
# 缺项答案：好答案删去风险提示句 ⇒ has_risk_note 精确减 1（验证 must 逐条可判）
_SELFTEST_MISSING = (
    "结论：贵州茅台600519短线偏多，未来三天以震荡上行为主，建议持有观望。"
    "依据数据显示，现价报1329.00元，日涨幅1.80%，成交量12.5万手，换手率0.45%。"
    "技术面站稳五日与十日年线，资金面主力净流入，上方压力1365元，下方支撑1300元，"
    "跌破止损位1288元可考虑减仓。"
)


def cmd_selftest(args) -> int:
    """内置 fixture 自洽校验（改评分函数后先跑它；红了 = 标尺自己坏了）。

    六项判据：评分可分辨（好>坏）/ 打分确定性 / must 逐条可判（缺一项精确减 1）/
    agreement 自比 100% / 容差内外可判 / 无交集 n=0。零网络零外部依赖。
    """
    checks: List[Tuple[str, bool, str]] = []

    good = score_answer(_SELFTEST_TASK, _SELFTEST_GOOD)
    bad = score_answer(_SELFTEST_TASK, _SELFTEST_BAD)
    checks.append(("评分可分辨（好答案 > 坏答案）", good["total"] > bad["total"],
                   f"good={good['total']} > bad={bad['total']}"))

    again = score_answer(_SELFTEST_TASK, _SELFTEST_GOOD)
    checks.append(("打分确定性（同输入同输出）", good == again,
                   "两次调用返回逐项相等"))

    cov_good, det_good = _score_coverage(_SELFTEST_TASK, _SELFTEST_GOOD)
    cov_miss, det_miss = _score_coverage(_SELFTEST_TASK, _SELFTEST_MISSING)
    checks.append(("must 逐条可判（缺一项精确减 1）",
                   det_good["pass"] == 4 and det_miss["pass"] == 3
                   and cov_miss < cov_good
                   and "has_risk_note" in det_miss["missing"],
                   f"{det_good['pass']}/4 → {det_miss['pass']}/4"
                   f"（coverage {cov_good} → {cov_miss}，缺项 "
                   f"{'、'.join(map(str, det_miss['missing']))}）"))

    ann = {"t1": 80, "t2": 60, "t3": 95}
    rate, mad, agree, n = agreement_rate(ann, ann, 10)
    checks.append(("agreement 自比 100%",
                   (rate, mad, agree, n) == (1.0, 0.0, 3, 3),
                   f"rate={rate:.0%} mad={mad} agree={agree}/{n}"))

    near = {"t1": 85, "t2": 66, "t3": 90}      # |Δ| 全部 ≤ tol=10
    far = {"t1": 95, "t2": 40, "t3": 90}       # 两条 |Δ| > tol=10
    r_near, _, _, _ = agreement_rate(ann, near, 10)
    r_far, _, _, _ = agreement_rate(ann, far, 10)
    checks.append(("agreement 容差内外可判", r_near == 1.0 and r_far < 1.0,
                   f"tol 内 {r_near:.0%} / tol 外 {r_far:.0%}"))

    r_none, mad_none, a_none, n_none = agreement_rate({"t1": 80}, {"t9": 80}, 10)
    checks.append(("agreement 无交集 n=0",
                   (r_none, mad_none, a_none, n_none) == (0.0, 0.0, 0, 0),
                   "无交集返 (0,0,0,0)，不把空集谎报成 0% 一致率"))

    fails = [c for c in checks if not c[1]]
    print("== qd_bench selftest（内置 fixture，零网络零外部依赖）==")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}：{detail}")
    if fails:
        print(f"[FAIL] selftest {len(fails)}/{len(checks)} 项失败——评分标尺自身可疑，"
              f"先修评分再谈跑分")
        return 1
    print(f"[PASS] selftest 全部通过（{len(checks)}/{len(checks)}）")
    return 0


# ═══════════════════════════════════════════════════════════════
#  存档 / 历史
# ═══════════════════════════════════════════════════════════════

def _save_run(summary: dict, meta: dict) -> str:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_id = meta.get("run_id") or ("bench_" + time.strftime("%Y%m%d_%H%M%S"))
    meta = dict(meta, run_id=run_id, ts=time.strftime("%Y-%m-%d %H:%M:%S"))
    record = dict(meta, **summary)
    (RUNS_DIR / f"{run_id}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    LATEST_FILE.write_text(json.dumps(record, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    return run_id


def _load_runs() -> List[dict]:
    if not RUNS_DIR.is_dir():
        return []
    out = []
    for p in sorted(RUNS_DIR.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _try_db_attach(summary: dict, meta: dict) -> bool:
    """把 run 挂到 qd_traces 树（root=bench_run, child=任务）。fail-open。"""
    try:
        from app.agent.chain.schema import EvalNode, Layer
        from app.agent.chain import store
        root = EvalNode(layer=Layer.CHAIN.value, name=f"qd_bench:{meta.get('run_id')}",
                        analysis=json.dumps({"total": summary["total"],
                                             "dims": summary["dims"]},
                                            ensure_ascii=False)[:2000],
                        score=summary["total"], user_query="(benchmark)")
        for r in summary.get("tasks", []):
            root.add_child(EvalNode(
                layer=Layer.SKILL.value, name=str(r.get("task_id")),
                score=r.get("total"),
                output_data={"dims": r.get("dims"), "must": r.get("must"),
                             "category": r.get("category")}))
        store.save_tree(root)
        return True
    except Exception as e:
        print(f"  [SKIP] 落库跳过（fail-open）: {type(e).__name__}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
#  rollouts（F19：重复采样 N 次聚合成 mean±std，绑预算上限）
# ═══════════════════════════════════════════════════════════════

DEFAULT_MAX_ROLLOUTS = 5      # QD_BENCH_MAX_ROLLOUTS 缺省上限（防误触烧钱）


def _max_rollouts() -> int:
    """`--num-rollouts` 上限：env `QD_BENCH_MAX_ROLLOUTS`（缺省 5）。

    未设 ⇒ 缺省；非正/非法 ⇒ 告警并回落缺省（配置错误不静默，但不当成"禁止跑"）。
    上限本身是预算闸门：N 超它 ⇒ cmd_run 在触服务前拒绝（exit 2）。
    """
    raw = (os.getenv("QD_BENCH_MAX_ROLLOUTS") or "").strip()
    if not raw:
        return DEFAULT_MAX_ROLLOUTS
    try:
        v = int(raw)
    except ValueError:
        print(f"[WARN] QD_BENCH_MAX_ROLLOUTS 非法（{raw!r}），"
              f"回落缺省 {DEFAULT_MAX_ROLLOUTS}")
        return DEFAULT_MAX_ROLLOUTS
    if v <= 0:
        print(f"[WARN] QD_BENCH_MAX_ROLLOUTS={v} 非正数，回落缺省 {DEFAULT_MAX_ROLLOUTS}")
        return DEFAULT_MAX_ROLLOUTS
    return v


def _ms_stats(vals: List[float]) -> dict:
    """均值±标准差（总体 std）+ min/max/n。空列表返全 0（不造数）。"""
    n = len(vals)
    if n == 0:
        return {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0, "n": 0}
    m = sum(vals) / n
    std = (sum((v - m) ** 2 for v in vals) / n) ** 0.5
    return {"mean": round(m, 1), "std": round(std, 1),
            "min": round(min(vals), 1), "max": round(max(vals), 1), "n": n}


def _aggregate_rollouts(summaries: List[dict]) -> dict:
    """N 次 rollout 的 summary 聚合：总分 + 分项 + 逐任务的 mean±std。

    用途：单次跑分的波动要先和噪声比——std 大说明分差可能是采样噪声而非改动。
    """
    agg: dict = {"n_rollouts": len(summaries), "total": _ms_stats(
        [float(s.get("total", 0.0)) for s in summaries]), "dims": {}, "tasks": {}}
    dims = sorted({d for s in summaries for d in (s.get("dims") or {})})
    for d in dims:
        agg["dims"][d] = _ms_stats(
            [float((s.get("dims") or {}).get(d, 0.0)) for s in summaries])
    tids = sorted({str(r.get("task_id")) for s in summaries
                   for r in s.get("tasks", [])})
    for tid in tids:
        agg["tasks"][tid] = _ms_stats(
            [float(r.get("total", 0.0)) for s in summaries
             for r in s.get("tasks", []) if str(r.get("task_id")) == tid])
    return agg


def _save_rollouts(agg: dict, meta: dict) -> Path:
    """聚合记录落 `runs/rollouts/`（trend/diff/agreement 不读它：统计记录 ≠ 可比 run）。"""
    out_dir = RUNS_DIR / "rollouts"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = meta.get("run_id") or ("bench_" + time.strftime("%Y%m%d_%H%M%S"))
    record = dict(meta, run_id=run_id, ts=time.strftime("%Y-%m-%d %H:%M:%S"),
                  kind="rollouts", **agg)
    path = out_dir / f"{run_id}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


def _print_rollouts(agg: dict, run_ids: List[str]) -> None:
    t = agg["total"]
    print(f"\n== rollouts ×{agg['n_rollouts']} · 总分 mean={t['mean']} "
          f"std={t['std']}（min {t['min']} / max {t['max']}）==")
    for d, st in sorted(agg["dims"].items()):
        print(f"  {d:<10} mean={st['mean']:<6} std={st['std']:<6} "
              f"（{st['min']}~{st['max']}）")
    noisy = sorted(((tid, st) for tid, st in agg["tasks"].items()),
                   key=lambda x: -x[1]["std"])[:5]
    if noisy:
        print("  波动最大任务（std）：")
        for tid, st in noisy:
            print(f"    {tid:<12} mean={st['mean']:>5} std={st['std']:>5}")
    print(f"  逐轮 run：{' '.join(run_ids)}")


# ═══════════════════════════════════════════════════════════════
#  子命令
# ═══════════════════════════════════════════════════════════════

def cmd_validate(args) -> int:
    tasks = load_tasks()
    ok, errs = validate_tasks(tasks)
    cats: Dict[str, int] = {}
    for t in tasks:
        cats[str(t.get("category"))] = cats.get(str(t.get("category")), 0) + 1
    print(f"任务集 {len(tasks)} 条 · 分类分布 {cats}")
    if ok:
        print("[PASS] 任务集体检通过")
        return 0
    for e in errs:
        print(f"[FAIL] {e}")
    return 1


def cmd_score(args) -> int:
    preds = [json.loads(l) for l in
             Path(args.preds).read_text(encoding="utf-8").splitlines() if l.strip()]
    ann = {}
    if args.annotations:
        raw = json.loads(Path(args.annotations).read_text(encoding="utf-8"))
        ann = {k: (v.get("score") if isinstance(v, dict) else v) for k, v in raw.items()}
    tasks = load_tasks()
    summary = score_batch(tasks, preds, ann)
    meta = {"model": args.model or (preds[0].get("meta", {}) or {}).get("model", "")
            if preds else "", "source": f"preds:{Path(args.preds).name}",
            "run_id": args.run_id or ""}
    run_id = _save_run(summary, meta)
    if args.to_db:
        _try_db_attach(summary, meta)
    _print_summary(run_id, summary)
    return 0


def _rollout_preds(svc, tasks: List[dict], model: str, tag: str) -> List[dict]:
    """单轮 rollout：逐任务过真实端点收集 preds（tag 用于 session_id 后缀防串味）。"""
    import asyncio
    preds = []
    for t in tasks:
        sid = f"bench_{t['id']}" + (f"_{tag}" if tag else "")
        t0 = time.time()
        try:
            resp = asyncio.run(svc.chat(str(t.get("query")), session_id=sid))
            ans = resp if isinstance(resp, str) else (getattr(resp, "content", None) or "")
            agent = svc._get_agent(sid)
            corpus = agent._observation_corpus() if hasattr(agent, "_observation_corpus") else ""
            print(f"  [{t['id']}] {time.time() - t0:.1f}s len={len(ans)}")
        except Exception as e:
            ans, corpus = f"(run error: {type(e).__name__}: {e})", ""
            print(f"  [{t['id']}] ERROR {type(e).__name__}")
        preds.append({"task_id": t["id"], "answer": ans, "corpus": corpus,
                      "meta": {"model": model or "default"}})
    return preds


def _rollout_suffix(path: str, tag: str) -> str:
    """preds 输出文件按轮次加后缀（p.jsonl → p_r0.jsonl）；无 tag 原样返回。"""
    if not tag:
        return path
    p = Path(path)
    return str(p.with_name(f"{p.stem}_{tag}{p.suffix}"))


def cmd_run(args) -> int:
    """真实端点跑任务集。环境/预算条件不满足 → exit 2（不是失败，是没条件跑）。"""
    # 预算闸门：超 QD_BENCH_MAX_ROLLOUTS 上限在触服务前拒绝（fail-loud 防烧钱）
    n_roll = max(1, int(getattr(args, "num_rollouts", 1) or 1))
    cap = _max_rollouts()
    if n_roll > cap:
        print(f"[REFUSE] --num-rollouts {n_roll} 超过上限 {cap}"
              f"（env QD_BENCH_MAX_ROLLOUTS；每轮仍受 AGENT_RUN_TOKEN_BUDGET "
              f"单 run 预算约束，拒跑是防误触烧钱，不是失败）")
        return 2
    try:
        from dotenv import load_dotenv
        load_dotenv(BACKEND_ROOT / ".env", override=False)
        from app.agent.qd_service import QDAgentService
        from app.agent.memory import LocalMemory
    except Exception as e:
        print(f"[ENV] 运行条件不满足: {type(e).__name__}: {e}" +
              (f" | 执行核: {_BOOT_ERR}" if _BOOT_ERR else ""))
        return 2
    tasks = load_tasks()
    if args.only:
        keep = {x.strip() for x in args.only.split(",")}
        tasks = [t for t in tasks if t.get("id") in keep]
    try:
        svc = QDAgentService(memory=LocalMemory(max_messages=200),
                             agent_config={"service_availability": {"search_knowledge": False}})
        if args.step_limit:
            svc.agent_config["step_limit"] = int(args.step_limit)
    except Exception as e:
        print(f"[ENV] 服务装配失败: {type(e).__name__}: {e}")
        return 2
    ann = {}
    if args.annotations:
        raw = json.loads(Path(args.annotations).read_text(encoding="utf-8"))
        ann = {k: (v.get("score") if isinstance(v, dict) else v) for k, v in raw.items()}
    base_id = "bench_" + time.strftime("%Y%m%d_%H%M%S")
    summaries: List[dict] = []
    run_ids: List[str] = []
    for i in range(n_roll):
        tag = f"r{i}" if n_roll > 1 else ""
        if n_roll > 1:
            print(f"\n-- rollout {i + 1}/{n_roll} --")
        preds = _rollout_preds(svc, tasks, args.model, tag)
        if args.save_preds:
            Path(_rollout_suffix(args.save_preds, tag)).write_text(
                "\n".join(json.dumps(p, ensure_ascii=False) for p in preds) + "\n",
                encoding="utf-8")
        summary = score_batch(load_tasks(), preds, ann)
        meta = {"model": args.model or "default", "source": "run",
                "run_note": args.note or "",
                "run_id": f"{base_id}_{tag}" if tag else ""}
        run_id = _save_run(summary, meta)
        run_ids.append(run_id)
        summaries.append(summary)
        if args.to_db:
            _try_db_attach(summary, meta)
        if n_roll == 1:
            _print_summary(run_id, summary)
    if n_roll > 1:
        agg = _aggregate_rollouts(summaries)
        path = _save_rollouts(agg, {"model": args.model or "default",
                                    "source": "rollouts",
                                    "run_note": args.note or "",
                                    "run_id": base_id, "run_ids": run_ids})
        _print_rollouts(agg, run_ids)
        print(f"  聚合记录 → {path}")
        print(f"  （每轮仍受 AGENT_RUN_TOKEN_BUDGET 单 run 预算约束；"
              f"{n_roll} 轮总成本 ≈ {n_roll} × 单轮）")
    return 0


def cmd_trend(args) -> int:
    runs = _load_runs()
    if not runs:
        print("（无历史 run；先跑 score/run）")
        return 0
    print(f"{'run_id':<26} {'时间':<20} {'model':<14} {'总分':>6}  分项")
    for r in runs:
        dims = " ".join(f"{k}={v}" for k, v in sorted((r.get("dims") or {}).items()))
        print(f"{r.get('run_id',''):<26} {r.get('ts',''):<20} "
              f"{str(r.get('model',''))[:14]:<14} {r.get('total', 0):>6}  {dims}")
    if len(runs) >= 2:
        d = round(runs[-1]["total"] - runs[-2]["total"], 1)
        print(f"\n最近一次较上次：{'+' if d >= 0 else ''}{d} 分")
    return 0


def cmd_diff(args) -> int:
    runs = {r.get("run_id"): r for r in _load_runs()}
    a, b = runs.get(args.a), runs.get(args.b)
    if not a or not b:
        print(f"[FAIL] 找不到 run（可用：{sorted(runs)}）")
        return 1
    ta = {t["task_id"]: t for t in a.get("tasks", [])}
    tb = {t["task_id"]: t for t in b.get("tasks", [])}
    print(f"{args.a} → {args.b}  总分 {a.get('total')} → {b.get('total')}")
    moved = []
    for tid in sorted(set(ta) | set(tb)):
        d = round((tb.get(tid, {}).get("total", 0) or 0) - (ta.get(tid, {}).get("total", 0) or 0), 1)
        if abs(d) >= 3:
            moved.append((tid, d))
    if not moved:
        print("（无任务级显著变化：|Δ| < 3 分）")
        return 0
    for tid, d in sorted(moved, key=lambda x: -abs(x[1])):
        print(f"  {tid:<14} {'+' if d >= 0 else ''}{d}")
    return 0


def agreement_rate(a: dict, b: dict, tol: float) -> tuple:
    """两人标注一致率（F19 从 cmd_agreement 内联逻辑抽出，供 selftest 复用）。

    |a-b| ≤ tol 记一致。返回 (rate, mad, agree, n)；无交集时 rate=0、n=0。
    """
    keys = sorted(set(a) & set(b))

    def _s(v):
        return float(v.get("score") if isinstance(v, dict) else v)

    if not keys:
        return 0.0, 0.0, 0, 0
    agree = sum(1 for k in keys if abs(_s(a[k]) - _s(b[k])) <= tol)
    rate = agree / len(keys)
    mad = sum(abs(_s(a[k]) - _s(b[k])) for k in keys) / len(keys)
    return rate, mad, agree, len(keys)


def cmd_agreement(args) -> int:
    """两人标注一致率（验收线 ≥80%）：|a-b| ≤ tol 记一致。"""
    a = json.loads(Path(args.a).read_text(encoding="utf-8"))
    b = json.loads(Path(args.b).read_text(encoding="utf-8"))
    rate, mad, agree, n = agreement_rate(a, b, args.tol)
    if not n:
        print("[FAIL] 两份标注无交集")
        return 1
    print(f"交集任务 {n} 条 · 一致 {agree} · 一致率 {rate:.0%} · 平均差 {mad:.1f}")
    print(f"[{'PASS' if rate >= 0.8 else 'FAIL'}] 一致率 {'≥' if rate >= 0.8 else '<'} 80%")
    return 0 if rate >= 0.8 else 1


def cmd_import_traces(args) -> int:
    """从 qd_traces 拉真实任务题面（DB 可用才有产出；缺 DB → exit 2）。"""
    try:
        from dotenv import load_dotenv
        load_dotenv(BACKEND_ROOT / ".env", override=False)   # 直跑本模块时 env 未加载
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            # DISTINCT + ORDER BY id 违反 PG 42P10（排序列必须在选择列表）；
            # GROUP BY + MAX(id) 等价取「每条题面的最新一次」。
            # 过滤 bench_/ab_ 会话：评测器自己跑出来的题面会自我污染导入源。
            cur.execute(
                "SELECT user_query FROM ("
                "  SELECT user_query, MAX(id) AS id FROM qd_agent_traces"
                "  WHERE user_query IS NOT NULL AND user_query <> ''"
                "    AND (session_id IS NULL OR session_id !~ '^(bench_|ab_)')"
                "  GROUP BY user_query) t"
                " ORDER BY id DESC LIMIT %s", (int(args.limit),))
            rows = [r["user_query"] for r in cur.fetchall()]
            cur.close()
    except Exception as e:
        print(f"[ENV] DB 不可用: {type(e).__name__}: {e}")
        return 2
    lines = ["# import-traces 生成（人工补齐 baseline.must/should 后再并入主任务集）"]
    for i, q in enumerate(rows, 1):
        lines += [f"  - id: trace_{i:02d}", "    category: 通用问答",
                  "    source: traces", f"    query: {json.dumps(q, ensure_ascii=False)}",
                  "    baseline:", "      must: [has_data_basis]"]
    out = Path(args.out or (BENCH_DIR / "traces_import.yaml"))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[OK] 导出 {len(rows)} 条 → {out}（需人工补 baseline 后并入）")
    return 0


def _print_summary(run_id: str, summary: dict) -> None:
    print(f"\n== {run_id} · 总分 {summary['total']} "
          f"（{summary['n_scored']}/{summary['n_total']} 条计分）==")
    for d, v in sorted(summary["dims"].items()):
        print(f"  {d:<10} {v}")
    weak = sorted((r for r in summary["tasks"] if r.get("status") == "ok"),
                  key=lambda r: r["total"])[:5]
    if weak:
        print("  最弱任务：")
        for r in weak:
            miss = "、".join(map(str, (r.get("must") or {}).get("missing") or []))[:60]
            print(f"    {r['task_id']:<12} {r['total']:>5}  缺：{miss or '—'}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="qd_bench", description="能力级评测（路线 C）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="校验任务集（不调模型）")

    p = sub.add_parser("score", help="对 preds JSONL 打分")
    p.add_argument("--preds", required=True)
    p.add_argument("--annotations", help="人工标注 JSON {task_id: score}")
    p.add_argument("--model", default="")
    p.add_argument("--to-db", action="store_true")
    p.add_argument("--run-id", default="")

    p = sub.add_parser("run", help="真实端点跑任务集 + 打分")
    p.add_argument("--only", default="")
    p.add_argument("--model", default="")
    p.add_argument("--step-limit", default="")
    p.add_argument("--save-preds", default="")
    p.add_argument("--annotations", default="")
    p.add_argument("--note", default="")
    p.add_argument("--num-rollouts", type=int, default=1,
                   help="重复采样轮数（聚合成 mean±std；上限 QD_BENCH_MAX_ROLLOUTS）")
    p.add_argument("--to-db", action="store_true")

    sub.add_parser("trend", help="历史曲线（总分+分项）")

    p = sub.add_parser("diff", help="两次 run 任务级对比")
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)

    p = sub.add_parser("agreement", help="两人标注一致率")
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)
    p.add_argument("--tol", type=float, default=10.0)

    sub.add_parser("selftest", help="内置 fixture 自洽校验（零网络零外部依赖）")

    p = sub.add_parser("import-traces", help="从 qd_traces 拉真实任务题面")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--out", default="")

    args = ap.parse_args(argv)
    return {"validate": cmd_validate, "score": cmd_score, "run": cmd_run,
            "trend": cmd_trend, "diff": cmd_diff, "agreement": cmd_agreement,
            "selftest": cmd_selftest,
            "import-traces": cmd_import_traces}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
