# -*- coding: utf-8 -*-
"""闭环②数字溯源门 —— 结论里的数字必须能在工具观察值里找到出处。

从 `qd_agent.py` 拆出（2026-10-13 P3 拆分）。

【为什么要有这道门】A 股研究助手最贵的失败不是"答错"，而是**数字看着很像真的
但没人说得清从哪来**。grounding gate 在 run 收尾时比对答复文本与工具语料，
不可溯源就要求模型重写（最多 `grounding_max_retries` 次）。

【★ 全部 fail-open】判定模块不可用 / 抛异常 ⇒ 返回 None（放行）。
溯源是增益纪律，绝不能因为门坏了让用户收不到答复。
"""
from __future__ import annotations

from typing import Optional


class GroundingMixin:
    """数字溯源：以 mixin 形式并进 QDAgent（必须排在 MimocodeAgent 之前）。"""

    def _grounding_violation_timed(self, text: str, corpus: str):
        """带分时的判定（QD_GROUND_TIMING=1 时打点）。判定本体仍走静态方法。"""
        import time as _t
        t0 = _t.perf_counter()
        out = self._grounding_violation(text, corpus)
        self._g_mark("check", t0, "verdict=%s corpus=%d字 answer=%d字" % (
            "违规" if out else "通过", len(corpus or ""), len(text or "")))
        return out

    def _observation_corpus(self) -> str:
        """闭环②语料：本轮全部工具观察值（取代旧 executor.state 语料源）。"""
        parts = []
        for msg in self.messages:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
        return "\n".join(parts)

    # ── 溯源分时埋点（QD_GROUND_TIMING=1 / 2026-10-16）──────────────
    # 用户实测：复杂问题答案段 95 秒、期间零工具调用 ⇒ 需分辨
    # "规则判定慢" 还是 "重写轮（super().run 带全上下文）慢"。
    def _g_mark(self, tag: str, t0: float, extra: str = "") -> None:
        import os as _os, time as _time, logging as _lg
        if (_os.getenv("QD_GROUND_TIMING") or "").strip() in ("", "0", "false", "False"):
            return
        try:
            _lg.getLogger("app.agent.grounding").warning(
                "[GROUND] %s 耗时 %.1fs %s", tag, _time.perf_counter() - t0, extra)
        except Exception:
            pass

    @staticmethod
    def _grounding_violation(text: str, corpus: str) -> Optional[str]:
        """返回 None = 通过；返回 str = 违规，内容是给模型的整改指引。"""
        try:
            from .utils.grounding import check_grounding
        except ImportError:
            try:
                from .utils.grounding import check_grounding
            except ImportError:
                return None
        try:
            ok, guide = check_grounding(text, corpus)
            return None if ok else guide
        except Exception:
            return None
