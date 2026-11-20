"""chain/source_budget.py — 来源优先级与每 run 提取预算倾斜（方案 §13）。

按失败改写>用户纠正>成功复盘>自由文本显式排序分配每 run 提取名额，高价值来源
优先入选；超预算的高价值来源仍标记 must_quality_check（不跳过入库质检），低价值
超预算标 overflow 而非丢弃。预算数值、高价值集合走版本化配置、null=未预注册时
维持全部 selected、不倾斜（fail-closed，不看到结果后调参）。纯函数，不碰 DB/LLM、
无生产调用方；同级稳定排序，输入顺序不被打乱。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

SCHEMA_VERSION = "source-budget-v1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("source_budget.json")
CONFIG_PATH_ENV = "QD_SOURCE_BUDGET"
_UNKNOWN_TIER = 10 ** 6


def config_path() -> Path:
    raw = os.getenv(CONFIG_PATH_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_CONFIG_PATH


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    p = Path(path) if path else config_path()
    with open(p, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("source budget config must be an object")
    if cfg.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_version mismatch: %r" % cfg.get("schema_version"))
    for name in ("priority", "high_value", "budget_per_run"):
        if name not in cfg:
            raise ValueError("source budget config missing %s" % name)
    return copy.deepcopy(cfg)


def allocate(items: Sequence[Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按来源优先级分配每 run 名额；预算未预注册则全部入选、不倾斜。"""
    if cfg is None:
        try:
            cfg = load_config()
        except (OSError, ValueError, json.JSONDecodeError) as e:
            return {"ok": False, "reason": "config_unavailable:%s" % type(e).__name__}
    if cfg.get("schema_version") != SCHEMA_VERSION:
        return {"ok": False, "reason": "schema_mismatch"}
    priority = cfg.get("priority")
    high = set(cfg.get("high_value") or ())
    budget = cfg.get("budget_per_run")
    if not isinstance(priority, list):
        return {"ok": False, "reason": "invalid_priority"}
    tier = {name: i for i, name in enumerate(priority)}

    valid = [it for it in items if isinstance(it, dict)]
    indexed = list(enumerate(valid))
    indexed.sort(key=lambda pair: (tier.get(str(pair[1].get("source_kind")), _UNKNOWN_TIER),
                                   pair[0]))

    if budget is None:
        out = [{"index": orig, "item": it, "source_kind": it.get("source_kind"),
                "priority_tier": tier.get(str(it.get("source_kind")), _UNKNOWN_TIER),
                "selected": True, "overflow": False,
                "must_quality_check": str(it.get("source_kind")) in high}
               for orig, it in indexed]
        out.sort(key=lambda r: r["index"])
        return {"ok": True, "tilted": False, "budget": None,
                "selected": len(out), "total": len(out), "rows": out}

    if not isinstance(budget, int) or budget < 0:
        return {"ok": False, "reason": "invalid_budget"}
    rows: List[Dict[str, Any]] = []
    for rank, (orig, it) in enumerate(indexed):
        kind = str(it.get("source_kind"))
        is_high = kind in high
        selected = rank < budget
        rows.append({"index": orig, "item": it, "source_kind": it.get("source_kind"),
                     "priority_tier": tier.get(kind, _UNKNOWN_TIER), "selected": selected,
                     "overflow": not selected,
                     "must_quality_check": is_high})
    rows.sort(key=lambda r: r["index"])
    return {"ok": True, "tilted": True, "budget": budget,
            "selected": sum(1 for r in rows if r["selected"]),
            "total": len(rows), "rows": rows,
            "high_value_overflow": sum(1 for r in rows
                                       if r["overflow"] and r["must_quality_check"])}
