#!/usr/bin/env python3
"""adapters/markets/registry.py — MarketSpec 加载器（L1 适配层）。

职责：把 `adapters/markets/*.yaml` 的**声明式**市场规则读成 `core.market.MarketSpec`，
并注入为进程默认市场。**core 不认识任何具体市场**；本文件是 core 与"市场取值"之间
唯一的桥（依赖方向 adapters → core，不回指）。

设计要点：
- **一个市场 = 一个 YAML**：接新市场 = 加一份 YAML（+ 数据源适配器），core 零改动。
- **fail-fast 不静默降级**：`runnable: false` 的市场（HK / US / Polymarket —— 数据源未接）
  被真正要求运行时**直接报错**，绝不"悄悄用 A 股阈值"跑出一个看似成功的错结果。
- 档位摊平（名义幅度 + 容差 → 元组）由 `core.market.build_bands` 完成，语义只此一处。
- import 本模块即注入默认市场 A（覆盖冻结 `.py` 插件的裸调用口径）。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import yaml

from app.market_cn.auto.core._paths import MARKETS_DIR
from app.market_cn.auto.core.market import (
    DEFAULT_BOARD,
    MarketSpec,
    build_bands,
    build_nominal,
    set_default_market,
)

_CACHE: Dict[str, MarketSpec] = {}


class MarketNotRunnable(RuntimeError):
    """该市场当前不可运行（数据源未接）—— 不允许静默按 A 股口径跑。"""


def _pairs(raw: Any) -> List[tuple]:
    """`[[前缀, 值], ...]` → list[tuple]（YAML 里用列表便于书写与 diff）。"""
    out: List[tuple] = []
    for it in (raw or []):
        if isinstance(it, (list, tuple)) and len(it) == 2:
            out.append((str(it[0]), str(it[1])))
    return out


def _from_doc(key: str, doc: Dict[str, Any]) -> MarketSpec:
    doc = doc or {}
    bound = doc.get("price_bound")
    return MarketSpec(
        key=str(doc.get("key", key)),
        name=str(doc.get("name", key)),
        intraday_t0=bool(doc.get("intraday_t0", False)),
        settlement_days=int(doc.get("settlement_days", 0) or 0),
        settlement_basis=str(doc.get("settlement_basis", "calendar")),
        direction=str(doc.get("direction", "long_only")),
        short_rule=str(doc.get("short_rule", "none")),
        band_kind=str(doc.get("band_kind", "none")),
        bands=build_bands(doc.get("bands") or {}),
        nominal=build_nominal(doc.get("bands") or {}),
        band_default=(str(doc["band_default"]) if doc.get("band_default") else None),
        price_bound=(tuple(float(x) for x in bound) if isinstance(bound, (list, tuple))
                     and len(bound) == 2 else None),
        lot_size=int(doc.get("lot_size", 1) or 0),
        tick_size=float(doc.get("tick_size", 0.01) or 0.0),
        currency=str(doc.get("currency", "")),
        tz=str(doc.get("tz", "")),
        fee_model=str(doc.get("fee_model", "")),
        board_rules=_pairs(doc.get("board_rules")),
        board_default=str(doc.get("board_default", DEFAULT_BOARD)),
        board_names=_pairs(doc.get("board_names")),
        board_name_default=str(doc.get("board_name_default", "未知")),
        source=str(doc.get("source", "") or ""),
        runnable=bool(doc.get("runnable", False)),
        note=str(doc.get("note", "") or ""),
    )


def market_keys() -> List[str]:
    """所有已声明的市场 key（按文件名升序）。"""
    if not os.path.isdir(MARKETS_DIR):
        return []
    return sorted(os.path.splitext(f)[0] for f in os.listdir(MARKETS_DIR)
                  if f.endswith(".yaml"))


def load_market(key: str, refresh: bool = False) -> MarketSpec:
    """加载（并缓存）一个市场。未知 key → KeyError（fail-fast，不猜）。"""
    k = str(key or "A").strip() or "A"
    if not refresh and k in _CACHE:
        return _CACHE[k]
    path = os.path.join(MARKETS_DIR, f"{k}.yaml")
    if not os.path.isfile(path):
        raise KeyError(f"未声明的市场 market={k!r}（已声明: {market_keys()}）")
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    spec = _from_doc(k, doc)
    _CACHE[k] = spec
    return spec


def require_runnable(spec: MarketSpec) -> MarketSpec:
    """要求该市场可实际运行；数据源未接 → 明确报错（绝不静默按 A 股口径跑）。"""
    if not spec.runnable:
        raise MarketNotRunnable(
            f"市场 {spec.key}({spec.name}) 尚未接入数据源，无法运行。"
            f"（source={spec.source or '未指定'}；{spec.note or '见 adapters/markets/*.yaml'}）"
        )
    return spec


# --- import 即注入默认市场 A（冻结 `.py` 插件的 3 参数裸调用口径）---
set_default_market(load_market("A"))
