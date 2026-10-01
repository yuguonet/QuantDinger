# -*- coding: utf-8 -*-
"""weight_hints — 权重消费端的**唯一读取口**（2026-10-01 闭环消费侧）。

【它解决什么】
`qd_agent_weights` 此前是**断头路**：盘后 `evaluator.update_weights` 每夜写入，
但没有任何运行时代码读它（旧系统的消费点在 `agents/task_agent.py` 的 plan 阶段，
随 nodes/task_agent 退役整体消失）⇒ 权重表 57 行 `sample_count` 全 0、weight 恒 1.0
也无人察觉。追责系统因此只是"审计账本"，不是"学习回路"。

本模块把"读权重"收敛成一个进程内缓存的快照函数，供两处消费：
  1. 技能选择（`qd_service._prefetch`）—— 词典分打平时用历史权重打破平局；
  2. 工具预选（`qd_agent._maybe_preselect_tools`）—— 低权重工具/链路进提示段。

【为什么必须缓存】消费点在**每条用户消息**的路径上。权重一夜才变一次，
每轮查库是纯浪费；缓存在进程内、TTL 到期才回源，查库失败沿用旧值（fail-open）。

【为什么不在 chain/store.py 里加】store.py 是 traces 树的持久化层（重量级导入），
而消费点在消息热路径上。本模块只依赖 `app.utils.db`，导入成本≈0。

★ 阈值单一事实源：`chain/skill_brewer.LOW_WEIGHT_THRESHOLD`（不在此处复制常量）。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 快照 TTL（秒）。一夜一变的量，10 分钟足够新鲜；调 0 可强制每次回源（测试用）。
_SNAPSHOT_TTL = float(os.getenv("QD_WEIGHT_TTL", "600") or 600)

_cache: dict = {"ts": 0.0, "skill": {}, "tool": {}, "chain": {}, "domain": {}}
_cache_bad = {"ts": 0.0}   # 上次回源失败时间（避免故障时每轮都打库）


def _threshold() -> float:
    """低权重阈值（单一事实源 = chain.skill_brewer.LOW_WEIGHT_THRESHOLD）。"""
    try:
        from app.agent.chain.skill_brewer import LOW_WEIGHT_THRESHOLD
    except ImportError:
        try:
            from chain.skill_brewer import LOW_WEIGHT_THRESHOLD
        except ImportError:
            return 0.7
    return float(LOW_WEIGHT_THRESHOLD)


def _fetch() -> Optional[dict]:
    """一次性取回 skill/tool/chain/domain 四层权重（一次连接，四次查询）。"""
    try:
        from app.utils.db import get_db_connection
    except ImportError:
        return None
    out = {"skill": {}, "tool": {}, "chain": {}, "domain": {}}
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()          # ★ 行是 RealDictRow：必须 row['k']
            for layer in ("skill", "tool", "chain", "domain"):
                cur.execute(
                    "SELECT name, weight FROM qd_agent_weights WHERE layer = %s",
                    (layer,))
                for row in cur.fetchall():
                    out[layer][row["name"]] = float(row["weight"])
            cur.close()
    except Exception as e:
        logger.debug("[weight_hints] 权重回源失败（沿用旧快照）: %s: %s",
                     type(e).__name__, e)
        return None
    return out


def snapshot(force: bool = False) -> dict:
    """返回权重快照 {"skill":{..},"tool":{..},"chain":{..},"domain":{..},"ts":..}。

    fail-open：回源失败时**保留上一次成功的值**（首轮失败则为空字典，
    消费方按"无历史信号"处理，不改变任何选择行为）。
    """
    now = time.time()
    if not force and _cache["ts"] and (now - _cache["ts"]) < _SNAPSHOT_TTL:
        return _cache
    # 故障退避：刚失败过就别每轮都打库（30s 内不重试）
    if not force and _cache_bad["ts"] and (now - _cache_bad["ts"]) < 30:
        return _cache
    got = _fetch()
    if got is None:
        _cache_bad["ts"] = now
        return _cache
    _cache.update(got)
    _cache["ts"] = now
    return _cache


def weight_of(layer: str, name: str, default: float = 1.0) -> float:
    """取单个权重（无记录 = 无历史信号，返回 default 保持中性）。"""
    try:
        return float(snapshot().get(layer, {}).get(name, default))
    except Exception:
        return default


def low_weight(layer: str, names: Optional[List[str]] = None,
               limit: int = 12) -> List[str]:
    """低权重名单（升序，最差的在前）。names 给定时只在其范围内筛。"""
    th = _threshold()
    try:
        w = snapshot().get(layer, {}) or {}
    except Exception:
        return []
    items = ((n, w[n]) for n in (names or sorted(w)) if n in w)
    return [n for n, v in sorted(items, key=lambda kv: kv[1])
            if v < th][:limit]


def hint_text(available_tools: Optional[List[str]] = None) -> str:
    """给预选 LLM 看的「历史表现提示」段（没有异常就返回空串，不占位）。

    只做**提示**不做**过滤**：低权重工具仍可被点名——历史胜率低不等于这次不需要，
    硬过滤会让模型在确实需要该工具时拿不到它（fail-open 一致性）。
    """
    parts = []
    low_tools = low_weight("tool", available_tools)
    if low_tools:
        parts.append("以下工具近期参与链路的验证胜率偏低，点名前请确认确有必要："
                     + "、".join(low_tools))
    low_chains = low_weight("chain", limit=8)
    if low_chains:
        parts.append("以下历史工具组合胜率偏低，规划时避免原样沿用："
                     + "、".join(low_chains))
    dom = snapshot().get("domain", {}) or {}
    if dom:
        th = _threshold()
        bad = sorted((k for k, v in dom.items() if v < th))
        good = sorted(((k, v) for k, v in dom.items() if v >= th),
                      key=lambda kv: -kv[1])[:3]
        if bad:
            parts.append("以下领域的近期判定命中率偏低，结论请多给一句不确定性说明："
                         + "、".join(bad))
        if good:
            parts.append("领域历史判定命中率：" +
                         "，".join(f"{k} {v:.0%}" for k, v in good))
    return "；".join(parts)


def report() -> dict:
    """自检/排查用：快照摘要（各层条数 + 低权重数 + 新鲜度）。"""
    s = snapshot()
    return {
        "ts": s.get("ts", 0),
        "age_sec": round(time.time() - float(s.get("ts", 0) or 0), 1),
        "counts": {k: len(s.get(k, {}) or {}) for k in
                   ("skill", "tool", "chain", "domain")},
        "low": {k: low_weight(k) for k in ("skill", "tool", "chain", "domain")},
        "threshold": _threshold(),
    }


def reset() -> None:
    """清空缓存（测试用）。"""
    _cache.update({"ts": 0.0, "skill": {}, "tool": {}, "chain": {}, "domain": {}})
    _cache_bad["ts"] = 0.0
