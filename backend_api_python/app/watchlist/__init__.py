# -*- coding: utf-8 -*-
"""app/watchlist/ — 自选股标签（watchlist label）：QuantDinger 基座的**扩展层**

设计依据：`docs/自选股标签统一产出方案.md`

定位（三个含义）
  1. **站在基座上，不站在上级上**：只依赖 `services/` / `utils/` / `data_sources/`
     ⇒ 与 `app.agent` / `app.market_cn.auto` **零代码关系**（`model/store/compute/
     overlay/render/api/job` 全部不 import 它们；有 AST 闭包断言）
  2. **可独立运行**：上级全停摆时，system 自算仍给出**用户可接受**的结果；
     上级接入后结果**更优**，是增强而非依赖
  3. **单一出口**：前端只认本模块发放的 **4 段**（`supports` / `resistances` / `score` / `extras`）

对外只导出 3 个**接口**符号（`get_labels` / `write_system_facts` / `submit`）——
`submit` 是 auto/agent 的**唯一写通道**（禁止直写 SQL）。

`attach_labels` 不是给 auto/agent 的接口，而是 `routes/market.py` 把 4 段**加法式**挂到
自带关系行上的挂载助手（它就是 `get_labels` 的调用方）；独立成函数是为了可测 ——
路由逻辑不再内联，"旧字段一个不改"可以被直接断言。
"""
from __future__ import annotations

from app.watchlist.api import attach_labels, get_labels, submit, write_system_facts

__all__ = ["get_labels", "write_system_facts", "submit", "attach_labels"]
