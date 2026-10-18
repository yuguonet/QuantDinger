# -*- coding: utf-8 -*-
"""upstream_compat — mimoagent 内部契约的**唯一**访问入口（F15，2026-10-17）。

【为什么必须收口】我们对 mimoagent 内部的访问原本散落在 10+ 处，绝大多数是
`getattr(obj, "字段", 默认)` 形态。上游一旦改名 / 挪位置，**不会报错**，只会
静默拿到默认值 —— 两个已经踩到的实例：
  · F10 的 cached：网关确实回填 `token_stats.cache_read_tokens`；但若上游改名，
    我们写库恒 NULL，缓存命中率被算成 0%，**无人知**；
  · F12 的轮界主动压缩：`hasattr(self, "_do_compact")` 为假时**静默不压缩**，
    长程任务悄悄退化成"不压缩"，同样无人知。

【口径：探测 + 上报 ≠ 兜底】兜底是"掩盖失败继续跑"；本模块字段缺失时返回
`None`/`False`/`()` 并**显式 WARNING + 记痕**（`contract_warnings()` 可审计），
由调用方决定降级。这正是闭环问责要求的"探测 + 记痕 + 问责"。

【约定】本模块**不 import 任何 app 内部模块**（只 logging/typing），因此谁都能
安全 import 它、也不会产生循环导入。
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

# 进程级"已告警过的契约键"——同一条契约漂移只吵一次，避免每轮刷屏
_WARNED: set = set()


def reset_warnings() -> None:
    """清空告警去重表（测试用；生产无需调用）。"""
    _WARNED.clear()


def contract_warnings() -> Tuple[str, ...]:
    """已经上报过的上游契约漂移键（供快照 / 审计）。"""
    return tuple(sorted(_WARNED))


def _warn_once(key: str, msg: str, *args: Any) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    try:
        logger.warning(msg, *args)
    except Exception:
        pass


# ── token 统计 `model.token_stats` ────────────────────────────────────

def read_token_stats(model: Any) -> Optional[Any]:
    """模型实例上的 mimo `token_stats` 对象；缺失 → None + 告警一次。"""
    st = getattr(model, "token_stats", None)
    if st is None:
        _warn_once("token_stats",
                   "[upstream] %s 无 token_stats 属性（上游契约变更）：token 记账全为 0",
                   type(model).__name__)
    return st


def read_total_tokens(model: Any) -> int:
    """累计 total_tokens（含缓存命中）。字段缺失 → 0 + 告警（与旧默认同值）。"""
    st = read_token_stats(model)
    if st is None or not hasattr(st, "total_tokens"):
        _warn_once("total_tokens",
                   "[upstream] token_stats 无 total_tokens（上游契约变更）：记账为 0")
        return 0
    return int(getattr(st, "total_tokens", 0) or 0)


def read_cache_tokens(model: Any) -> Optional[int]:
    """前缀缓存命中的 input token（含在 total 内）。未上报 / 字段缺失 → None。

    ★ 严格区分「字段缺失」（None + 告警）与「真实 0」：None 一路透到落库即 NULL，
      否则"未上报"会被当成"缓存率 0%"，把 F10 的比率指标带偏。
    """
    st = read_token_stats(model)
    if st is None:
        return None
    if not hasattr(st, "cache_read_tokens"):
        _warn_once("cache_read_tokens",
                   "[upstream] token_stats 无 cache_read_tokens"
                   "（上游契约变更，F10 落库将恒 NULL）")
        return None
    return int(getattr(st, "cache_read_tokens", 0) or 0)


# ── 轮界压缩 `agent._do_compact()`（F12）─────────────────────────────

def has_compaction(agent: Any) -> bool:
    """mimo agent 是否提供轮界压缩入口（不产生告警，供探测）。"""
    return callable(getattr(agent, "_do_compact", None))


def invoke_compaction(agent: Any) -> bool:
    """调用 mimo 的轮界压缩 `_do_compact()`；无此方法 → False + 告警（F12 失效）。"""
    fn = getattr(agent, "_do_compact", None)
    if not callable(fn):
        _warn_once("_do_compact",
                   "[upstream] %s 无 _do_compact（上游契约变更）：F12 轮界主动压缩失效",
                   type(agent).__name__)
        return False
    fn()
    return True


# ── 核心工具名 `mimoagent.tools.mimocode.MIMOCODE_CORE_TOOL_NAMES` ────

def core_tool_names() -> Tuple[str, ...]:
    """mimo 常驻核心工具名。导入失败 → () + 告警。"""
    try:
        from mimoagent.tools.mimocode import MIMOCODE_CORE_TOOL_NAMES
    except Exception as e:
        _warn_once("MIMOCODE_CORE_TOOL_NAMES",
                   "[upstream] 无法导入 MIMOCODE_CORE_TOOL_NAMES（上游契约变更）: %s", e)
        return ()
    try:
        return tuple(MIMOCODE_CORE_TOOL_NAMES)
    except Exception as e:
        _warn_once("MIMOCODE_CORE_TOOL_NAMES",
                   "[upstream] MIMOCODE_CORE_TOOL_NAMES 不可迭代（上游契约变更）: %s", e)
        return ()


__all__ = [
    "read_token_stats",
    "read_total_tokens",
    "read_cache_tokens",
    "has_compaction",
    "invoke_compaction",
    "core_tool_names",
    "reset_warnings",
    "contract_warnings",
]
