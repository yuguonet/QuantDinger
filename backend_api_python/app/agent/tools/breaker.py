# -*- coding: utf-8 -*-
"""工具失败熔断器（2026-09-12，用户观察：坏工具/坏数据源不收敛，反复重试烧爆步数）。

机制：
  - 包装 provider/skill 工具的调用面；同一工具【连续】失败 ≥2 次 → 熔断打开；
  - 打开后调用立即短路，返回明确指令（禁止再调 + 降级建议），不再发起真实调用；
  - 成功调用清零计数；按 agent 实例隔离（_breakers dict），不跨任务污染；
  - 判定"失败"：返回值含 error 标记（工具层约定）或调用抛异常。

边界：
  - 只熔断"明显坏"的工具；不熔断 search_tools/list_tools/format_result 等元工具
    （它们失败另有原因，且短路它们会让模型失去自救通道）；
  - 熔断状态保留到本次 agent 生命周期结束（阶段重试复用同一 agent → 熔断延续，
    避免重试轮再踩同一坑）。
"""
from __future__ import annotations

import threading

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 元工具不参与熔断（模型的自救通道必须始终可用）
_META_TOOLS = {"search_tools", "list_tools", "format_result", "web_search",
               "final_answer", "stage_write", "stage_read", "stage_list"}


class ToolCircuitBreaker:
    """同一工具连续失败 N 次 → 熔断（短路返回降级指令）。"""

    def __init__(self, threshold: int = 2):
        self.threshold = threshold
        self._fail_streak: dict = {}
        self._open: dict = {}
        self._lock = threading.Lock()

    def is_open(self, name: str) -> bool:
        with self._lock:
            return self._open.get(name, False)

    def record(self, name: str, ok: bool):
        with self._lock:
            if ok:
                self._fail_streak[name] = 0
                return
            streak = self._fail_streak.get(name, 0) + 1
            self._fail_streak[name] = streak
            if streak >= self.threshold and not self._open.get(name):
                self._open[name] = True
                logger.warning("[Breaker] 工具 %s 连续失败 %d 次，熔断打开", name, streak)

    def wrap(self, name: str, fn):
        """返回包装后的调用函数（供 executor 使用）。"""
        if name in _META_TOOLS:
            return fn

        def _call(*args, **kwargs):
            if self.is_open(name):
                return (f"[熔断] 工具 {name} 已连续失败 {self.threshold} 次（数据源可能不可用），"
                        f"本次调用被拦截。请勿再调用该工具；改用其它工具、基于已有数据继续，"
                        f"或在答复中说明该数据暂不可获取。")
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                self.record(name, ok=False)
                return {"error": f"[{name}] 执行异常: {type(e).__name__}: {e}"}
            # 失败判定：工具层约定——返回 dict 且含非空 "error" 键
            if isinstance(result, dict) and result.get("error"):
                self.record(name, ok=False)
            else:
                self.record(name, ok=True)
            return result

        _call.__name__ = getattr(fn, "__name__", name)
        _call.__doc__ = getattr(fn, "__doc__", "")
        return _call
