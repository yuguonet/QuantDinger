"""chain/prediction_contract.py — P0-1 决策快照冻结 + 版本化预测契约校验。

【为什么独立成模块】
1118 方案 §5.1 / §5.3 要求「在写入快照和计算指标前，必须运行预测契约校验」，
且校验器必须在 `app/agent/` 内实现并版本化。本模块是**纯函数层**：
不碰 DB、不调 LLM、不写库、不读环境变量以外的运行时状态。
只做两件事：把一次预测的输入冻结成不可变快照，再按版本化规则校验它。

【冻结语义】
`build_snapshot` 返回全新 dict（深拷贝输入的可变部分），不提供任何 update 接口。
`snapshot_sha` 由 canonical JSON（sort_keys + 统一日期格式）算 SHA-256 前 32 位，
同一份输入在任何进程得到同一个 sha；输入变化 sha 必变。
需要修正时调用方构造新快照，不覆盖旧快照（§5.1「生成后禁止覆盖」）。

【契约版本】
CONTRACT_VERSION 是校验规则的语义版本：规则口径变更必须升版本，
并把旧版本规则保留为 `validate_contract_v1` 之类的具名函数。
当前只有 `v1`，`validate_contract` 是它的别名 —— 调用方永远绑到具名版本，
避免规则悄悄变化而 outcome 表上的 contract_version 还是旧值。

【五条规则（§5.3）】
1. prediction_target / subject / horizon / as_of / label_rule_version 齐全且一致；
2. 概率存在时：有限、在 [0,1]、target/horizon 与 claim 一致、source 口径合法。
   direction 预测的 p_target 是方向细分 up/down（claims.py / claim_extract_llm.py
   的生产口径），不是 prediction_target 字面量；v1 曾按字面等值校验 ⇒ calibrated
   概率全被误判 mismatch、Brier 恒 N/A，v2 修正；
3. 任一概率契约不匹配 ⇒ 方向指标照常，Brier 记 N/A，coverage_gap 记
   `probability_target_mismatch`（含 source/target/horizon），不换概率、不静默剔除；
4. 概率缺失 ⇒ Brier N/A、`probability_missing`；confidence 不自动填概率；
5. probability_source 只认 `calibrated` / `heuristic_confidence`；
   其余记 `unknown_p_source`、概率作废，不参与任何概率指标。

【不在这里做什么】
- 不判定 verdict / 数据成熟度（那是 resolver 的事）；
- 不决定写不写 outcome（写入层 fail-soft 自行决定）；
- 不读取经验库、不 import 任何 skills 下的模块（11-17 用户裁定：零关系）。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

CONTRACT_VERSION = "prediction-contract-v2"
SNAPSHOT_VERSION = "snapshot-v1"

CALIBRATED = "calibrated"
HEURISTIC_CONFIDENCE = "heuristic_confidence"
PROBABILITY_SOURCES = frozenset({CALIBRATED, HEURISTIC_CONFIDENCE})
BRIER_SOURCES = frozenset({CALIBRATED})
DIRECTION_TARGETS = frozenset({"up", "down"})

REQUIRED_FIELDS = ("prediction_target", "subject", "horizon", "as_of",
                   "label_rule_version")


def _canon(value: Any) -> Any:
    """日期/日期时间转 ISO 字符串，其余递归处理，供 canonical JSON 使用。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in sorted(value.items(),
                                                     key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    return value


def snapshot_sha(snapshot: Dict[str, Any]) -> str:
    """快照内容哈希：canonical JSON → SHA-256 前 32 位。"""
    body = json.dumps(_canon(snapshot), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def build_snapshot(*, decision_id: Any = None, claim_id: Any = None,
                   subject: Any = "", subject_kind: Any = "",
                   horizon: Any = "", prediction_target: Any = "",
                   as_of: Any = None, data_as_of: Any = None,
                   created_at: Any = None,
                   model_name: Any = "", prompt_sha: Any = "",
                   skill_id: Any = "", skill_version: Any = "",
                   experience_bundle_id: Any = "",
                   experience_bundle_sha: Any = "",
                   experience_injection_ids: Sequence[Any] = (),
                   experience_snapshot_at: Any = None,
                   tool_chain: Any = None, tool_chain_sha: Any = "",
                   query_type: Any = "", label_rule_version: Any = "",
                   forecast_capture_status: Any = "",
                   probability_source: Any = "", probability_target: Any = "",
                   probability_horizon: Any = "",
                   sample_layer: Any = "", condition_key: Any = "",
                   predicted_direction: Any = "", predicted_probability: Any = None,
                   entry_rule: Any = "", entry_price: Any = None,
                   stop_rule: Any = "", stop_price: Any = None,
                   target_rule: Any = "", target_price: Any = None,
                   is_actionable: bool = False,
                   adapter_data: Optional[Dict[str, Any]] = None,
                   **extra: Any) -> Dict[str, Any]:
    """冻结一份决策快照（§5.1 字段全集）。

    返回全新 dict；可变输入一律深拷贝，调用方后续修改原对象不影响快照。
    `extra` 收领域 adapter 的扩展字段（P0 不消费，但允许随快照冻结）。
    """
    snap: Dict[str, Any] = {
        "decision_id": decision_id,
        "claim_id": claim_id,
        "snapshot_version": SNAPSHOT_VERSION,
        "created_at": created_at,
        "as_of": as_of,
        "data_as_of": data_as_of,
        "model_name": model_name,
        "prompt_sha": prompt_sha,
        "skill_id": skill_id,
        "skill_version": skill_version,
        "experience_bundle_id": experience_bundle_id,
        "experience_bundle_sha": experience_bundle_sha,
        "experience_injection_ids": copy.deepcopy(list(experience_injection_ids or ())),
        "experience_snapshot_at": experience_snapshot_at,
        "tool_chain": copy.deepcopy(tool_chain),
        "tool_chain_sha": tool_chain_sha,
        "query_type": query_type,
        "subject": subject,
        "subject_kind": subject_kind,
        "horizon": horizon,
        "prediction_target": prediction_target,
        "label_rule_version": label_rule_version,
        "forecast_capture_status": forecast_capture_status,
        "probability_source": probability_source,
        "probability_target": probability_target,
        "probability_horizon": probability_horizon,
        "sample_layer": sample_layer,
        "condition_key": condition_key,
        "predicted_direction": predicted_direction,
        "predicted_probability": predicted_probability,
        "entry_rule": entry_rule,
        "entry_price": entry_price,
        "stop_rule": stop_rule,
        "stop_price": stop_price,
        "target_rule": target_rule,
        "target_price": target_price,
        "is_actionable": bool(is_actionable),
        "adapter_data": copy.deepcopy(dict(adapter_data or {})),
    }
    for k, v in (extra or {}).items():
        if k not in snap:
            snap[k] = v
    snap = copy.deepcopy(snap)
    snap["snapshot_sha"] = snapshot_sha(snap)
    return snap


def verify_snapshot(snapshot: Dict[str, Any]) -> bool:
    """校验收到的快照未被改写：重算 canonical hash 与自带 snapshot_sha 比对。"""
    if not isinstance(snapshot, dict):
        return False
    stored = str(snapshot.get("snapshot_sha") or "")
    if not stored:
        return False
    body = {k: v for k, v in snapshot.items() if k != "snapshot_sha"}
    return snapshot_sha(body) == stored


def _p_value(raw: Any) -> Optional[float]:
    """概率解析：只接受有限数且在 [0,1]；其余返回 None。"""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or not 0.0 <= v <= 1.0:
        return None
    return v


def _validate_impl(snapshot: Dict[str, Any], version: str,
                   accept_direction_subtarget: bool) -> Dict[str, Any]:
    """按 §5.3 五条规则校验快照；失败不改写输入，只报告 coverage_gap。"""
    s = snapshot if isinstance(snapshot, dict) else {}
    gaps: List[str] = []
    errors: List[str] = []

    missing = [f for f in REQUIRED_FIELDS
               if s.get(f) is None or str(s.get(f) or "").strip() == ""]
    if missing:
        errors.append("missing_fields:" + ",".join(missing))
        gaps.append("incomplete_snapshot")

    raw_p = s.get("predicted_probability")
    p = _p_value(raw_p)
    if raw_p is not None and p is None:
        gaps.append("p_invalid")
    source = str(s.get("probability_source") or "").strip()
    usable = False
    brier = False

    if p is not None:
        if source not in PROBABILITY_SOURCES:
            gaps.append("unknown_p_source")
        else:
            usable = True
            if source in BRIER_SOURCES:
                p_target = str(s.get("probability_target") or "").strip()
                p_horizon = str(s.get("probability_horizon") or "").strip()
                target = str(s.get("prediction_target") or "").strip()
                horizon = str(s.get("horizon") or "").strip()
                mismatches = []
                if p_target and target:
                    if accept_direction_subtarget and target == "direction":
                        if p_target not in DIRECTION_TARGETS:
                            mismatches.append("target:%s!=direction(up|down)" % p_target)
                    elif p_target != target:
                        mismatches.append("target:%s!=%s" % (p_target, target))
                if p_horizon and horizon and p_horizon != horizon:
                    mismatches.append("horizon:%s!=%s" % (p_horizon, horizon))
                if mismatches:
                    gaps.append("probability_target_mismatch:%s" % ";".join(mismatches))
                else:
                    brier = True
    else:
        gaps.append("probability_missing")

    return {
        "ok": not errors,
        "contract_version": version,
        "snapshot_sha": str(s.get("snapshot_sha") or ""),
        "errors": errors,
        "coverage_gap": gaps,
        "probability_usable": usable,
        "probability_value": p if usable else None,
        "probability_source_clean": source if usable else "",
        "brier_eligible": brier,
        "direction_eligible": True,
    }


def validate_contract_v1(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """v1（历史口径）：p_target 必须与 prediction_target 字面相等。"""
    return _validate_impl(snapshot, "prediction-contract-v1", False)


def validate_contract_v2(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """v2：direction 预测的 p_target 是方向细分 up/down（生产口径），非字面等值。"""
    return _validate_impl(snapshot, CONTRACT_VERSION, True)


validate_contract = validate_contract_v2


def verdict_eligibility(contract: Dict[str, Any], verdict: str) -> Dict[str, Any]:
    """把契约结果与 verdict 合成学习资格（§5.3 三类分离）。

    prediction_verdict 来自 resolver，不受概率契约影响；
    learning_eligibility 要求快照完整 + 概率可用方向时才成立。
    """
    v = str(verdict or "")
    c = contract if isinstance(contract, dict) else {}
    return {
        "prediction_verdict": v,
        "learning_eligibility": (
            bool(c.get("ok")) and bool(c.get("probability_usable"))
            and v in ("hit", "miss", "partial", "delisted")),
        "brier_eligible": bool(c.get("brier_eligible")),
    }
