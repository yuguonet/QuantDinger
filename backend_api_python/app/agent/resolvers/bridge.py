# -*- coding: utf-8 -*-
"""resolvers/bridge — 实体解析接线（2026-10-01）：让 resolvers/ 从死代码变在线。

【它原本为什么是死的】
`resolvers/`（base/composite/stock/time）的调用方在旧系统是
`agents/task_agent.py` 里的 `NodeContext(entity_resolver=CompositeResolver(...))`
—— 随 nodes.py / task_agent.py 退役，整条链**没有任何调用点**（grep 只剩
domain_meta.py 的一句注释）。后果：
  · 股票名/代码消歧与"标的待澄清反问"从未在现网跑过（"帮我看看平安" 会被静默
    当成某个平安系标的，整份分析作废甚至据此下单）；
  · 相对时间（"最近/明天/上周三"）的交易日口径与"时间窗不明"反问同样失效。

【为什么值得接回来】这是**防错**，不是润色：代价不对称——猜错标的/猜错时间窗
会让整份结论作废，反问只花一轮对话（base.ResolveResult 的澄清契约）。

【挂在哪】`QDAgentService._run_sync` 的**取数之前**（chat 阶段等价物）：
  ① clarify_question 非空 → 直接反问，**不进执行**（与旧 chat_node 同语义）；
  ② 解析出的 entity / 时间口径 → 作为上下文块注入，并顺带供
     技能选择（有标的 → 个股类技能）与结果格式化（domain 选择）使用。

【成本】一次 DB 模糊搜索（10s 超时 + 线程池隔离）+ 纯正则，无 LLM 调用；
  失败一律 fail-open（按"没解析出东西"处理，不改变原行为）。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_resolver = None          # 进程内单例（CompositeResolver 构造极轻，但 children 固定）
_resolver_built = False


def _env_on(key: str, default: str = "1") -> bool:
    return os.getenv(key, default).strip().lower() not in ("0", "false", "no", "off")


def enabled() -> bool:
    """总开关 QD_RESOLVER=0 可整体关闭（排障时用）。"""
    return _env_on("QD_RESOLVER")


def clarify_enabled() -> bool:
    """澄清反问开关 QD_RESOLVER_CLARIFY（默认开）。

    关掉后仍会解析实体/时间（注入上下文），但**不阻断**执行 —— 用于澄清误伤
    过多时的降级，而不是默认姿势。
    """
    return _env_on("QD_RESOLVER_CLARIFY")


def _build():
    """构造 CompositeResolver（顺序即语义：先标的、后时间）。"""
    global _resolver, _resolver_built
    if _resolver_built:
        return _resolver
    with _lock:
        if _resolver_built:
            return _resolver
        try:
            from .composite import CompositeResolver
            from .stock import StockResolver
            from .time import TimeResolver
        except ImportError:  # 直跑（app/agent 在 sys.path）时的裸导入
            from resolvers.composite import CompositeResolver
            from resolvers.stock import StockResolver
            from resolvers.time import TimeResolver

        def _time_factory(ctx):
            """时间解析必须拿到"由标的反推的领域"，否则交易日口径不生效。"""
            types = (ctx or {}).get("entity_types") or []
            return TimeResolver(entity_type=types[0] if types else "")

        _resolver = CompositeResolver([StockResolver(), _time_factory])
        _resolver_built = True
        logger.debug("[resolver] CompositeResolver 已装配（stock → time）")
    return _resolver


def resolve(text: str) -> Dict[str, object]:
    """解析用户输入。**永不抛异常**，返回一个可预测的 dict。

    Returns:
        {
          "ran": bool,              # 是否真的跑了解析（开关/异常时为 False）
          "clarify": str,           # 非空 = 必须先反问用户，不得继续执行
          "entity_code": str, "entity_name": str, "entity_type": str,
          "domain": str,            # 由实体类型倒推（空 = 没推出来）
          "time_note": str,         # 时间口径说明（相对日/交易日口径）
          "effective_input": str,   # 扩写后的输入（含 名称(代码) 与默认参数）
        }
    """
    empty = {"ran": False, "clarify": "", "entity_code": "", "entity_name": "",
             "entity_type": "", "domain": "", "time_note": "", "effective_input": ""}
    if not enabled():
        return empty
    r = _build()
    if r is None:
        return empty
    try:
        res = r.resolve(text or "")
    except Exception as e:
        # fail-open：解析失败按"没解析出东西"处理，绝不阻断用户
        logger.warning("[resolver] 解析异常（已忽略）: %s: %s", type(e).__name__, e)
        return empty
    if res is None:
        return dict(empty, ran=True)

    out = dict(empty)
    out["ran"] = True
    out["clarify"] = str(getattr(res, "clarify_question", "") or "")
    out["entity_code"] = str(getattr(res, "entity_code", "") or "")
    out["entity_name"] = str(getattr(res, "entity_name", "") or "")
    out["entity_type"] = str(getattr(res, "entity_type", "") or "")
    out["effective_input"] = str(getattr(res, "effective_input", "") or "")
    # 领域倒推：chat 阶段拿不到 selected_domain，只能由实体类型反推
    if out["entity_type"] and out["entity_type"] not in ("entity_clarify",):
        try:
            from domain_registry import entity_to_domain
        except ImportError:
            try:
                from app.agent.domain_registry import entity_to_domain
            except ImportError:
                entity_to_domain = None       # type: ignore
        if entity_to_domain is not None:
            try:
                out["domain"] = str(entity_to_domain(out["entity_type"]) or "")
            except Exception:
                pass
    return out


def context_block(info: Dict[str, object]) -> str:
    """把解析结果渲染成一段**可注入**的上下文（无信息时返回空串，不占位）。"""
    if not info.get("ran"):
        return ""
    parts = []
    if info.get("entity_code"):
        nm = info.get("entity_name") or ""
        parts.append("已识别标的：%s" % (f"{nm}({info['entity_code']})" if nm
                                     else info["entity_code"]))
    if info.get("domain"):
        parts.append("领域：%s" % info["domain"])
    if info.get("time_note"):
        parts.append(info["time_note"])
    if not parts:
        return ""
    return "[实体解析]\n" + "\n".join("- " + p for p in parts)
