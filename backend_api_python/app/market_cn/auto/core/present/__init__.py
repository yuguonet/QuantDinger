"""auto/core/present — 展示模块: T-1 夜预计算 + 盘中增量 (策略不写任何展示内容)。

⚠️ **本包是 façade（转出），不是实现**。实现分在两处：
  - `pipeline.py` —— 夜预计算 / 盘中增量 / 分段自检（原 `ide/present.py`）
  - `intraday.py` —— IDE 盘中通道（原 `ide/intraday.py`）

**为什么要转出这一层**：M5 目录重组把原来的**单模块** `ide/present.py` 变成了**包**
`core/present/`。这类"模块 → 包"的搬迁有个隐蔽陷阱：`from ... import present as P`
这种**取模块对象**的旧写法，在包形态下**不会报错**，但 `P.precompute_night` 会
`AttributeError`（只在下游被真正调用时才炸）。把它转出成本包的属性，旧写法与
`present.pipeline.X` 两种形态同时可用；新增调用方**请直接 import 实现模块**。

易错：不要在本文件里重复定义实现（会造成第二份实现）—— 一律 `from ... import` 转出。
"""

from __future__ import annotations

# 实现模块本身（`present.pipeline.X` / `present.intraday.X` 可用）
from app.market_cn.auto.core.present import intraday as intraday  # noqa: F401
from app.market_cn.auto.core.present import pipeline as pipeline  # noqa: F401

# --- 公共面转出（旧 `core.present` 单模块写法的调用点零改动）---
from app.market_cn.auto.core.present.pipeline import (  # noqa: F401
    EXT_PROVIDERS,
    BarsCache,
    DayHit,
    GatePlan,
    NightHit,
    StageStats,
    audit_needs_d0,
    build_ext,
    decision_offset,
    intraday_cycle,
    plan_gates,
    precompute_night,
    reads_decision_bar,
    register_ext,
    required_min_len,
    verify_split,
    _load_specs,
)

__all__ = [
    "pipeline",
    "intraday",
    "EXT_PROVIDERS",
    "BarsCache",
    "DayHit",
    "GatePlan",
    "NightHit",
    "StageStats",
    "audit_needs_d0",
    "build_ext",
    "decision_offset",
    "intraday_cycle",
    "plan_gates",
    "precompute_night",
    "reads_decision_bar",
    "register_ext",
    "required_min_len",
    "verify_split",
]
