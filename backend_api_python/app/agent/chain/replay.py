"""chain/replay.py — P1 离线成对回放接口预留 + PIT 过滤（方案 §11 P1-2/7）。

纯函数层：只定义接口与判据，不跑回测、不碰 DB、不调 LLM、不接生产调用方。
将来具备可复现 decision snapshot 时才由实验脚本注入数据调用。

两个能力：
- pit_available：判断一条经验在某个决策时点是否可用（§5.4.1 PIT 约束）——
  source_available_at / registered_at 必须严格早于决策 created_at；缺时间即不可用。
- replay_pair：把同一决策输入分别送入两个 arm 的成对回放契约，固定数据截止、
  工具结果、模型版本与运行顺序；本层只校验输入齐全并生成配对，不执行。

边界：不能重建历史经验快照时只做登记后的前瞻评估，禁止用今天抽取的经验
回算并声称改善了过去预测（§5.4.1）。本模块据此对缺时间的经验一律返回 False。
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

ARMS = ("A", "B", "C")


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def pit_available(experience: Dict[str, Any],
                  decision_created_at: Any) -> Tuple[bool, str]:
    """经验 e 是否在决策时点 t 已可用；返回 (可用, 原因)。缺时间即 False。"""
    t = _as_datetime(decision_created_at)
    if t is None:
        return False, "decision_time_missing"
    src = _as_datetime(experience.get("source_available_at"))
    reg = _as_datetime(experience.get("registered_at"))
    if src is None and reg is None:
        return False, "availability_time_missing"
    usable = [x for x in (src, reg) if x is not None]
    try:
        if any(x >= t for x in usable):
            return False, "available_at_or_after_decision"
    except TypeError:
        return False, "time_basis_incomparable"
    return True, "ok"


def filter_pit(experiences: Sequence[Dict[str, Any]],
               decision_created_at: Any) -> List[Dict[str, Any]]:
    """按 PIT 判据过滤经验列表；只保留决策时点前已可用的。"""
    return [dict(e) for e in experiences
            if pit_available(e, decision_created_at)[0]]


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def replay_pair(decision_input: Dict[str, Any], arms: Sequence[str] = ARMS,
                *, data_as_of: Any = None, model_version: str = "",
                tool_results_sha: str = "") -> Dict[str, Any]:
    """生成成对回放输入契约（不执行）：每 arm 一份深拷贝 + 冻结哈希。

    校验 data_as_of / model_version / tool_results_sha 齐全，否则返回 ok=False，
    防止不同 arm 用不同数据或模型版本造成伪对照。运行顺序由调用方随机化。
    """
    if not arms or len(set(arms)) != len(arms):
        return {"ok": False, "reason": "invalid_arms"}
    if any(a not in ARMS for a in arms):
        return {"ok": False, "reason": "unknown_arm"}
    if data_as_of is None or not model_version or not tool_results_sha:
        return {"ok": False, "reason": "frozen_inputs_incomplete"}
    payloads = {}
    for arm in arms:
        payloads[arm] = copy.deepcopy(decision_input)
    frozen = _canon({"data_as_of": data_as_of, "model_version": model_version,
                     "tool_results_sha": tool_results_sha,
                     "arms": list(arms)})
    return {"ok": True, "arms": list(arms), "payloads": payloads,
            "frozen_sha": hashlib.sha256(frozen.encode("utf-8")).hexdigest()[:32],
            "note": "caller randomizes run order; identical inputs per arm"}
