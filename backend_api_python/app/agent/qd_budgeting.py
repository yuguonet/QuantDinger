# -*- coding: utf-8 -*-
"""成本记账 + 硬熔断（2026-10-12 P0：单轮烧 40 万 token 且零产出）。

从 `qd_agent.py` 拆出（2026-10-13 P3 拆分）。挂在 `after_step`（埋点之前）——
超预算必须**立刻**终止，不能再往下走。

【为什么成本计数器不能跟着 `_steps_taken` 归零】
那两处归零（`_run_inner` 的空回复轻推轮 / grounding 重写轮）本意是"给重试轮
独立步数预算"，否则主轮耗尽后重试一进来就 `LimitsExceeded`。若成本也跟着归零，
等于重试轮能**无限续杯** —— 21 步 × 3 轮 × ~29k tokens/步 的口子就是这么来的。
故本模块的三个计数器按**请求**计（在 `_run_inner` 开头归零，重试轮不动）。

【熔断方式】抛 `LimitsExceeded`（`TerminatingException` 子类）：
mimo `base.run` 捕获后返回 `(type(e).__name__, str(e))`，`_run_inner` 的兜底分支
会因 message 非空而把提示原样交给用户 —— 软收尾，不硬崩。
"""
from __future__ import annotations

from mimoagent.agents.base import LimitsExceeded

from .upstream_compat import invoke_compaction


class BudgetMixin:
    """成本记账：以 mixin 形式并进 QDAgent（必须排在 MimocodeAgent 之前）。"""

    def _maybe_turn_compact(self) -> int:
        """轮界主动压缩（F12，C2）。非首轮 + 历史超阈值时调用 _do_compact()，fail-open。

        Return: 压缩前 token 估算（>0 表示已尝试压缩）。
        """
        try:
            from .qd_config import _turn_compact_threshold
        except Exception:
            return 0
        thr = _turn_compact_threshold()
        if thr <= 0:
            return 0
        try:
            msgs = getattr(self, "messages", None)
            if not msgs:
                return 0
            has_prior_round = any(m.get("role") in ("assistant", "tool") for m in msgs)
            if not has_prior_round:
                return 0
            from mimoagent.utils.cal_token import rough_token_count_estimation_for_messages
            pre = rough_token_count_estimation_for_messages(msgs)
            if pre < thr:
                return 0
            # F15b：经 upstream_compat 调用 —— `_do_compact` 缺失时显式告警（F12 静默失效 → 可见）
            invoke_compaction(self)
            return int(pre)
        except Exception as e:
            try:
                logger.debug("[turn_compact] 主动压缩失败(fail-open): %s", e)
            except Exception:
                pass
            return 0

    def _account_budget(self) -> None:
        """累计本请求成本，超预算即抛 LimitsExceeded（软收尾）。"""
        # 记账源 = 框架原生 `_last_request_tokens`（base.query 每次请求后写入
        # input+output 增量），因此覆盖主轮 + 空回复轻推轮 + 溯源重写轮。
        self._qd_run_tokens += int(getattr(self, "_last_request_tokens", 0) or 0)
        self._qd_run_steps_total += 1
        self._qd_run_tool_calls += self._last_step_tool_calls()
        try:
            from .utils.budget import check_budget, budget_wrapup_hint
        except Exception:
            return  # 预算模块不可用时静默退化（fail-open，同本仓红线）
        reason = check_budget(run_tokens=self._qd_run_tokens,
                              tool_calls=self._qd_run_tool_calls)
        if reason is None and self.config.run_step_cap > 0 \
                and self._qd_run_steps_total >= self.config.run_step_cap:
            reason = "run_steps:%d/%d" % (self._qd_run_steps_total, self.config.run_step_cap)
        if reason is None:
            return
        if self.trace is not None:
            try:
                self.trace.emit("budget_exceeded", reason=reason,
                                run_tokens=self._qd_run_tokens,
                                steps=self._qd_run_steps_total)
            except Exception:
                pass
        raise LimitsExceeded(budget_wrapup_hint(reason))

    def _last_step_tool_calls(self) -> int:
        """本步模型实际发起的工具调用数（取最后一条 assistant 消息）。"""
        try:
            for msg in reversed(self.messages):
                if msg.get("role") == "assistant":
                    return len(msg.get("tool_calls") or [])
        except Exception:
            pass
        return 0
