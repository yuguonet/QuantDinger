# -*- coding: utf-8 -*-
"""judge 一致性观测 —— **先证明它有必要，再谈人工校准**。

背景（2026-10-01 用户裁定）：
    judge 阶段 B 默认关闭（`QD_CLAIM_JUDGE=1` 且域 `judge_enabled` 才开）。
    它存在的唯一理由是「代码算得出数值，但判不了语义/归因」——可没人知道
    **这种情况到底占多少**。如果 judge 和代码锚点 95% 一致，那它就是纯浪费
    的钱和延迟，该一直关着；如果分歧集中在某类 claim（比如 level/timing），
    那才值得针对性校准。

    ⇒ 先装计数器跑一段，用**分歧率**决定要不要开、要不要建人工校准集。

怎么读这个模块的数字：
    * `calls` 上不去 → judge 没真开，先别谈校准（查 QD_CLAIM_JUDGE / judge_enabled）
    * `disagree_rate` < 10% 且样本 >100 → **保持关闭**，省下来的钱比修正的多
    * `disagree_rate` > 20% → 值得做① 找分歧集中类型 ② 建人工校准集
    * `fail_rate` > 5%    → 先修调用稳定性（judge 挂得多会把分辨率拉低）

人工校准怎么做（**等数据达标后再动手，别提前建**）：
    Step 1  导出分歧样本：读 `qd_agent_resolutions`，筛 `judge_raw` 非空且
            `verdict != (同 claim 的规则判 verdict)`。规则判结果需要重算，用
            `verdict_by_rule(claim, actual, dev)` 离线跑一遍比对。
    Step 2  人工标注 30~50 条（不必多），写 `qd_agent_resolutions.human_verdict`。
            ⚠ 表里的 `human_verdict` 列已留但当前**无任何写入路径**，就是给这一步用的。
    Step 3  算 judge-vs-human 一致率。**这个**才是 judge 的真实准确率；贬低于
            80% 就别让 judge 覆盖规则判，改成"judge 只给意见、不改写 verdict"。
    Step 4  一致率达标后，把 Judge 配置从"覆盖"切成"仅填充 attribution_note"，
            避免 LLM 对数值型判定的随机性污染慢调信号。

⚠ 本模块只做观测，**不自动改任何判定行为**。
"""

import time
from collections import Counter
from typing import Any, Dict

logger = __import__("logging").getLogger(__name__)

_CNT: Counter = Counter()
_STARTED = time.time()

# judge 与规则锚点的分歧按 claim_type 分组 —— 分歧若集中在某类型，说明该补规则而不是加 LLM
_DISAGREE_BY_TYPE: Counter = Counter()


def record_call() -> None:
    """judge 被调用一次（分母：所有尝试）。"""
    _CNT["calls"] += 1


def record_result(ok: bool, agreed: bool = True, claim_type: str = "") -> None:
    """judge 返回结果后记一笔。

    Args:
        ok: judge 是否成功返回（False = 失败回退到规则锚点）。
        agreed: judge 的 verdict 是否与规则判一致。ok=False 时无意义。
        claim_type: 分歧按类型分组用（direction / magnitude / level / range）。
    """
    if ok:
        _CNT["success"] += 1
        if agreed:
            _CNT["agree"] += 1
        else:
            _CNT["disagree"] += 1
            _DISAGREE_BY_TYPE[claim_type or "unknown"] += 1
            logger.info("[judge-disagree] type=%s (%d 次累计) —— "
                        "规则与 LLM 判不一致，进入人工校准候选集",
                        claim_type, _CNT["disagree"])
    else:
        _CNT["fail"] += 1


def rates() -> Dict[str, float]:
    """关键比率（0~1）。"""
    c = _CNT
    n_ok = c["success"]
    return {
        "fail_rate": round(c["fail"] / c["calls"], 4) if c["calls"] else 0.0,
        "disagree_rate": round(c["disagree"] / n_ok, 4) if n_ok else 0.0,
        "agree_rate": round(c["agree"] / n_ok, 4) if n_ok else 0.0,
    }


def report() -> Dict[str, Any]:
    """judge 健康视图 + 该不该动手的结论。"""
    r = rates()
    return {
        "calls": _CNT["calls"], "success": _CNT["success"], "fail": _CNT["fail"],
        "agree": _CNT["agree"], "disagree": _CNT["disagree"],
        "disagree_by_type": dict(_DISAGREE_BY_TYPE.most_common()),
        "uptime_sec": int(time.time() - _STARTED),
        **r,
        "verdict": _verdict(),
    }


def _verdict() -> str:
    c, r = _CNT, rates()
    if c["calls"] == 0:
        return "judge 未启用（默认关闭）：先 accum? 若要评估请 QD_CLAIM_JUDGE=1 跑一段"
    if c["success"] < 100:
        return "样本不足(<100)：继续观测，勿据此开关 judge"
    if r["fail_rate"] > 0.05:
        return "调用不稳(>5%)：先修 judge 可用性，再谈一致性"
    if r["disagree_rate"] > 0.20:
        return "分歧偏大(>20%)：值得建人工校准集，见模块 docstring Step 1~4"
    if r["disagree_rate"] > 0.10:
        return "有分歧(10~20%)：看 disagree_by_type，优先给集中类型补规则"
    return "judge 与规则高度一致(<10%)：建议保持关闭，省成本"


def reset() -> None:
    """清零（仅供测试）。"""
    _CNT.clear()
    _DISAGREE_BY_TYPE.clear()


__all__ = ["record_call", "record_result", "rates", "report", "reset"]
