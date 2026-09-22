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

# 跨阶段变量续承由框架内部存储自动投影，元工具不参与熔断（模型自救通道必须始终可用）
_META_TOOLS = {"search_tools", "list_tools", "format_result", "web_search", "final_answer"}


class ToolCircuitBreaker:
    """同一工具连续失败 N 次 → 熔断（短路返回降级指令）。

    2026-09-22（缺陷清单 Bug4）：加 cooldown 自动半开——熔断超过 COOLDOWN_SECONDS
    后自动复位计数，允许下一次调用真实试探（成功→彻底恢复；再失败→重新熔断）。
    修复"临时故障（网络抖动/数据源闪断）导致工具整个 session 锁死"的问题；
    有意失败的工具仍会被重新熔断，防护语义不变。
    """

    COOLDOWN_SECONDS = 300  # 熔断 5 分钟后允许试探

    def __init__(self, threshold: int = 2):
        self.threshold = threshold
        self._fail_streak: dict = {}
        self._open: dict = {}
        self._open_at: dict = {}   # 熔断打开时间戳（half-open 判定用）
        self._lock = threading.Lock()

    def is_open(self, name: str) -> bool:
        with self._lock:
            if not self._open.get(name, False):
                return False
            # half-open：超过冷却期 → 自动复位，放行一次试探
            import time as _time
            opened_at = self._open_at.get(name, 0)
            if _time.time() - opened_at >= self.COOLDOWN_SECONDS:
                logger.info("[Breaker] 工具 %s 熔断冷却期已过（%ds），半开放行试探",
                            name, self.COOLDOWN_SECONDS)
                self._open[name] = False
                self._fail_streak[name] = 0
                self._open_at.pop(name, None)
                return False
            return True

    def record(self, name: str, ok: bool):
        with self._lock:
            if ok:
                self._fail_streak[name] = 0
                return
            streak = self._fail_streak.get(name, 0) + 1
            self._fail_streak[name] = streak
            if streak >= self.threshold and not self._open.get(name):
                self._open[name] = True
                import time as _time
                self._open_at[name] = _time.time()
                logger.warning("[Breaker] 工具 %s 连续失败 %d 次，熔断打开（%ds 后半开试探）",
                               name, streak, self.COOLDOWN_SECONDS)

    def wrap(self, name: str, fn):
        """返回包装后的调用函数（供 executor 使用）。"""
        if name in _META_TOOLS:
            return fn

        def _call(*args, **kwargs):
            if self.is_open(name):
                # 2026-09-14：改为**抛异常**而非返回字符串。实测模型把返回的熔断提示
                # 当成了"调用成功"（拿到 str 打印后继续换参数轰炸，6 个 indicator
                # 全试一遍）；异常语义（本次调用失败）才能让模型正确分支、不再重试。
                # 异常会被 executor 包装捕获 → 记失败（熔断保持打开）→ 以 error 返回。
                raise RuntimeError(
                    f"[熔断] 工具 {name} 已连续失败 {self.threshold} 次（数据源可能不可用），"
                    f"本次调用被拦截。请勿再调用该工具；改用其它工具、基于已有数据继续，"
                    f"或在答复中说明该数据暂不可获取。"
                )
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
