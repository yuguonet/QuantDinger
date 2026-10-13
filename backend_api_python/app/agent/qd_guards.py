# -*- coding: utf-8 -*-
"""安全守卫 —— 交易人工确认闸（闭环③）+ 危险面守卫。

从 `qd_agent.py` 拆出（2026-10-13 P3 拆分）。两者都以 **action interceptor**
形式挂上（`QDAgent.__init__` 里 `add_action_interceptor`），在工具派发前拦截：
返回 dict = 短路该次调用（结果直接当观察值回给模型）；返回 None = 放行。

【为什么交易确认做成 interceptor 而不是函数内硬闸】
两层，各管一半：
  · 本 interceptor 拦的是"模型想调但没带 confirm=true"——在派发前就把话术
    回给模型，让它去问用户；
  · 函数内的 confirm 硬闸是最后一道（即便绕过 interceptor 也过不去）。
只留一层会漏：只有函数内闸 ⇒ 模型拿到的是"报错"，不知道该怎么补救。

【危险面口径】沿用修订三②：凭据路径 deny + 破坏性命令 deny。
★ `2>/dev/null` 必须放行 —— 它是无害重定向，`which x 2>/dev/null` 被误拒过一次；
  正则里的 `(?!null\b)` 就是那次事故留下的，删掉会回归。
"""
from __future__ import annotations

import re
from typing import Optional

from mimoagent.tools.base import ToolOutput
from .tools._fn_adapter import FnToolAdapter


_DENY_PATH_RE = re.compile(
    r"(~/?\.ssh|/etc/(passwd|shadow|sudoers)|\.git-credentials|\.aws/credentials"
    r"|\.kube/config|\.docker/config\.json|authorized_keys|id_(rsa|ed25519)"
    r"|openclaw\.json|(^|/)\.env($|/)|credentials|private[_-]?key)",
    re.IGNORECASE,
)

_DENY_CMD_RE = re.compile(
    r"(\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r|\bmkfs\b|\bdd\s+if=|\bshred\b"
    # (?!null\b)：`2>/dev/null` 是无害重定向（日志中 `which ... 2>/dev/null`
    # 被误拒），只拦写设备（/dev/sda 等）；/dev/sd 分支不受影响。
    r"|\bwipefs\b|/dev/sd|>\s*/dev/(?!null\b)|\bvisudo\b|\bcrontab\b|\b(useradd|usermod|passwd)\b"
    r"|chmod\s+.*authorized_keys|chown\s+.*authorized_keys"
    r"|\bsystemctl\s+(enable|disable)\b|curl[^|]*\|\s*(ba)?sh|wget[^|]*\|\s*(ba)?sh)",
    re.IGNORECASE,
)


class GuardMixin:
    """安全守卫：以 mixin 形式并进 QDAgent（必须排在 MimocodeAgent 之前）。"""

    # ── 闭环③：交易人工确认闸 ──────────────────────────────────
    def _trading_confirm_guard(self, action: dict) -> Optional[dict]:
        tool_name = action.get("tool", "")
        try:
            tool = self.tool_registry.get(tool_name)
        except Exception:
            return None
        if not isinstance(tool, FnToolAdapter) or not tool.requires_confirm:
            return None
        if (action.get("params") or {}).get("confirm") is True:
            return None  # 已确认，放行（函数内硬闸仍校验）
        return ToolOutput(
            output=(
                "requires_confirmation: 该操作涉及真实交易动作，未获用户确认。"
                "请先向用户复述标的/数量/方向并获得明确同意，"
                "再以 confirm=true 重新调用。"
            ),
            success=True,
            metadata={"tool": tool_name, "requires_confirmation": True},
        ).to_dict()

    # ── 危险面守卫 ─────────────────────────────────────────────
    def _danger_guard(self, action: dict) -> Optional[dict]:
        params = action.get("params") or {}
        tool_name = action.get("tool", "")
        # 路径类工具：凭据路径 deny
        for key in ("path", "file", "file_path", "relative_path", "target"):
            val = params.get(key)
            if isinstance(val, str) and _DENY_PATH_RE.search(val):
                return self._deny(tool_name, f"敏感路径拒绝访问: {val}")
        # bash/命令类：破坏性命令 + 凭据路径双重 deny
        for key in ("command", "cmd"):
            val = params.get(key)
            if isinstance(val, str) and (_DENY_CMD_RE.search(val) or _DENY_PATH_RE.search(val)):
                return self._deny(tool_name, f"危险命令拒绝执行: {val[:120]}")
        return None

    @staticmethod
    def _deny(tool_name: str, reason: str) -> dict:
        return ToolOutput(
            output=f"Error: 安全策略拒绝 —— {reason}",
            success=False,
            metadata={"tool": tool_name, "danger_denied": True},
        ).to_dict()
