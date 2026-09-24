# -*- coding: utf-8 -*-
"""app/agent/execution/dual_run.py — F2 双跑交叉验证

触发（方案 F2）：
  - critic 判敏感（涉及买卖金额/账户/方向）
  - claims 数字落在容差边界
  - 显式 `dual_run=True` 的 phase 契约

仲裁表（采纳设计原则「分歧摆出来比赌一个强」）：
  | 情况 | 处置 |
  |---|---|
  | 数值差 ≤ 容差 | 采信主跑；confidence 升档 |
  | 单点超差 | 第三方仲裁或标 uncertain |
  | **方向相反** | 两口径**并列** + 分歧原因，**不硬选** |

deliverable_schema：diff 前必须先按 schema 归一，否则全是格式假阳性。
本模块只做**对账与仲裁**，不碰 correct/calibration（② 闭环写保护）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

#: 默认相对容差（0.01 = 1%）
DEFAULT_REL_TOL = 0.01
#: 默认绝对容差（金额/百分点）
DEFAULT_ABS_TOL = 0.05


def is_sensitive(task: str) -> bool:
    """critic 敏感启发式：买卖/金额/账户/仓位/下单。"""
    t = (task or "").lower()
    keys = ("买入", "卖出", "清仓", "加仓", "减仓", "下单", "成交",
            "账户", "持仓", "仓位", "止损", "止盈", "buy", "sell", "order", "position")
    return any(k in t for k in keys)


def should_dual_run(*, phase: Optional[Dict[str, Any]] = None, task: str = "",
                    claims_boundary: bool = False) -> bool:
    if phase and (phase.get("dual_run") or phase.get("sensitive")):
        return True
    if claims_boundary:
        return True
    return is_sensitive(task)


def _numbers_equal(a: Any, b: Any, *, rel: float, abs_tol: float) -> bool:
    try:
        fa, fb = float(a), float(b)
    except Exception:
        return a == b
    diff = abs(fa - fb)
    if diff <= abs_tol:
        return True
    scale = max(abs(fa), abs(fb), 1e-9)
    return (diff / scale) <= rel


def _walk_diff(a: Any, b: Any, path: str = "") -> List[Dict[str, Any]]:
    """结构化 diff（只报数值/字符串叶子）。"""
    out: List[Dict[str, Any]] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            out.extend(_walk_diff(a.get(k), b.get(k), f"{path}.{k}" if path else str(k)))
        return out
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        n = max(len(a), len(b))
        for i in range(n):
            av = a[i] if i < len(a) else None
            bv = b[i] if i < len(b) else None
            out.extend(_walk_diff(av, bv, f"{path}[{i}]"))
        return out
    if a != b and not _numbers_equal(a, b, rel=DEFAULT_REL_TOL, abs_tol=DEFAULT_ABS_TOL):
        out.append({"path": path, "a": a, "b": b})
    return out


def arbitrate(result_a: Any, result_b: Any, *, rel: float = DEFAULT_REL_TOL,
              abs_tol: float = DEFAULT_ABS_TOL, deliverable_schema: Optional[Dict] = None
              ) -> Dict[str, Any]:
    """双跑仲裁。

    Returns:
        {verdict: agree|uncertain|divergent, primary, confidence: low|mid|high,
         diffs: [...], note: str, both: bool}
    """
    # deliverable_schema 先归一（裁决 #10：schema 是 diff 前提）
    a, b = result_a, result_b
    if deliverable_schema:
        a = _project(a, deliverable_schema)
        b = _project(b, deliverable_schema)

    diffs = _walk_diff(a, b, "")
    if not diffs:
        return {"verdict": "agree", "primary": a, "confidence": "high",
                "diffs": [], "note": "双跑一致", "both": False}

    # 方向相反（涨/跌、买/卖）→ 并列不硬选
    if _direction_conflict(a, b):
        return {"verdict": "divergent", "primary": None, "confidence": "low",
                "diffs": diffs[:20], "note": "方向相反：两口径并列（不硬选）", "both": True,
                "result_a": a, "result_b": b}

    numeric = [d for d in diffs if isinstance(d.get("a"), (int, float))
               and isinstance(d.get("b"), (int, float))]
    if numeric and all(_numbers_equal(d["a"], d["b"], rel=rel * 5, abs_tol=abs_tol * 5)
                       for d in numeric):
        return {"verdict": "uncertain", "primary": a, "confidence": "mid",
                "diffs": diffs[:20], "note": "单点超差（放大容差内）：标 uncertain", "both": False}

    return {"verdict": "divergent", "primary": a, "confidence": "low",
            "diffs": diffs[:20], "note": "超差：建议第三方仲裁", "both": False}


def _project(obj: Any, schema: Dict[str, Any]) -> Any:
    """按 deliverable_schema 投影（只保留契约键，去格式噪声）。"""
    props = schema.get("properties") or schema.get("fields") or {}
    if not isinstance(obj, dict) or not props:
        return obj
    return {k: obj.get(k) for k in props}


_DIR_KEYS = ("direction", "side", "action", "方向", "操作", "建议")


def _direction_conflict(a: Any, b: Any) -> bool:
    def _dir(x: Any) -> Optional[str]:
        if not isinstance(x, dict):
            return None
        for k in _DIR_KEYS:
            if k in x and x[k]:
                s = str(x[k]).strip().lower()
                for neg in ("卖", "空", "sell", "short", "reduce", "exit"):
                    if neg in s:
                        return "neg"
                for pos in ("买", "多", "buy", "long", "add", "hold", "持"):
                    if pos in s:
                        return "pos"
        return None

    da, db = _dir(a), _dir(b)
    return da is not None and db is not None and da != db
