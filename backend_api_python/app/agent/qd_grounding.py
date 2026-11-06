# -*- coding: utf-8 -*-
"""闭环②结论门 —— 数字须可溯源，禁用的确定性承诺须重写或拦截。

从 `qd_agent.py` 拆出（2026-10-13 P3 拆分）。

【为什么要有这道门】A 股研究助手最贵的失败不是"答错"，而是**数字看着很像真的
但没人说得清从哪来**。grounding gate 在 run 收尾时比对答复文本与工具语料，
不可溯源就要求模型重写（最多 `grounding_max_retries` 次）。

【数字溯源 fail-open】判定模块不可用 / 抛异常 ⇒ 放行，避免误拦正常答复。
禁用承诺检查是 live 合规门，判定异常不静默放行。
"""
from __future__ import annotations

import time as _time
from typing import Optional


class GroundingMixin:
    """结论校验：以 mixin 形式并进 QDAgent（必须排在 MimocodeAgent 之前）。"""

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

    @staticmethod
    def _banned_violation(text: str) -> Optional[str]:
        """命中禁用的确定性承诺时返回整改指引；None 表示通过。"""
        from .utils.grounding import check_banned_phrases

        hits = check_banned_phrases(text)
        if not hits:
            return None
        return "命中禁止的确定性/营销式表述：" + "、".join(hits) + "。"

    def _run_grounding_gate(self, status: str, message: str) -> tuple[str, str]:
        """重写不可溯源数字或禁用承诺；多次不合规时不交付原始答复。"""
        if not self.config.grounding_gate or status in ("truncated", "empty", "budget_wrapped"):
            return status, message

        corpus = self._observation_corpus()
        check_numbers = bool(corpus.strip())
        t0 = _time.perf_counter()
        guide = self._grounding_violation_timed(message, corpus) if check_numbers else None
        banned = self._banned_violation(message)
        retries = 0
        while (guide is not None or banned is not None) \
                and retries < self.config.grounding_max_retries:
            retries += 1
            self._steps_taken = 0
            t1 = _time.perf_counter()
            self._g_mark("REWRITE#%d 前 messages=%d" % (
                retries, len(getattr(self, "messages", []) or [])), t0)
            if banned is not None:
                prompt = (
                    f"【结论合规拦截】{banned}"
                    "请删除或改写这些承诺式表述，改用有条件、概率性的表述，不要复述命中词。"
                )
                if guide is not None:
                    prompt += (
                        "\n另外，数字也须可溯源：" + guide +
                        "无法溯源的数字改为\"未获取\"并说明缺失口径。"
                    )
            else:
                prompt = (
                    f"【数字溯源拦截】{guide}\n"
                    "请只依据本轮工具输出中的数字重写结论；无法溯源的数字改为"
                    "\"未获取\"并说明缺失口径。"
                )
            status, message = super().run(prompt)
            self._g_mark("REWRITE#%d 后" % retries, t1)
            guide = self._grounding_violation_timed(message, corpus) if check_numbers else None
            banned = self._banned_violation(message)

        if banned is not None:
            return "grounding_failed", (
                "本次答复包含未通过合规检查的确定性或营销式表述，原答复未展示。"
                "请重试并使用基于数据、条件和不确定性的表述。"
            )
        if guide is not None:
            return "grounding_failed", message
        return status, message

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
