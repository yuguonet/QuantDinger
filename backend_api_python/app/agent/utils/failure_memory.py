# -*- coding: utf-8 -*-
"""失败记忆（提智方案 二波 B2，run 内闭环）——单一事实源。

设计（2026-09-24）：
  · **error_type 定死词表**（同时是评测 L3 的统计口径）：同一错误重复 ≤1 次/run。
  · **采集**：GuidedCPythonExecutor 在错误改写处分类登记（本模块 `classify_error`），
    逐 step 由执行层 drain 出 (error_type, detail) 事件。
  · **注入**：**单一注入通道**——task_agent 的 step_callback 把本 step 新出现的错误类型
    合成一段【失败记忆】追加到 `memory_step.observations`（不改写模型代码、不新增工具）。
    约束：≤ MAX_ITEMS 条、每条 ≤ MAX_LEN 字、含"禁止令 + 已确认事实"、不给方案；
    已 resolved 的压缩成一行保留（防"换个工具名再犯"）。
  · **禁膨胀**：同一 error_type 只注入一次首现提示；再次出现只累计计数并升级为"重复告警"，
    不重复堆叠正文。

注：原方案引用的 `_inject_tool_failures` 全仓不存在（幻觉引用），故本模块同时承担
"注入口"职责；旧 `_failed_tool` 标记是"无生产者的消费者"（审计 P1-4），不复用。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# ── error_type 定死词表（新增须评审；评测 L3 按此统计）──
ERROR_TYPES = (
    "hallucinated_tool",     # 未定义名字：拼错/幻觉工具名或变量名
    "sandbox_unavailable",   # 执行环境受限：敏感路径 deny / import 被拒
    "wrong_column",          # 属性/字段误用（把 list 当 dict、列名错）
    "wrong_frequency",       # 周期/频率参数非法
    "empty_result",          # 工具或数据源返回空/无数据
    "self_check_failed",     # 数据自检未过（B3 validate_df / SelfCheckError）
    "data_repair_failed",    # 修复代理放弃（B4 上线后启用）
)

MAX_ITEMS = 5
MAX_LEN = 80

# 分类正则（与 guided_executor 的改写分支同源；仅在无法更精确时回落）
_NAME_ERR_RE = re.compile(r"NameError:\s*name\s+'([^']+)'")
_ATTR_ERR_RE = re.compile(r"AttributeError:\s*'[^']*'\s+object has no attribute")
_FREQ_HINT_RE = re.compile(r"(周期|频率|timeframe|frequency|interval|bar\s*size)", re.I)
_SANDBOX_HINT_RE = re.compile(
    r"(PermissionError|ImportError|ModuleNotFoundError|被禁止|deny-list|"
    r"not allowed|unauthorized import)", re.I)
_EMPTY_HINT_RE = re.compile(
    r"(未返回|无数据|返回空|空值|数据缺失|no data|empty result|暂无数据)", re.I)


def classify_error(err_text: str) -> Optional[str]:
    """把执行错误文本归类到 ERROR_TYPES 之一；无法归类返回 None。"""
    s = str(err_text or "")
    if not s:
        return None
    if "SelfCheckError" in s or "self_check" in s.lower():
        return "self_check_failed"
    if _NAME_ERR_RE.search(s):
        return "hallucinated_tool"
    if _SANDBOX_HINT_RE.search(s):
        return "sandbox_unavailable"
    if _ATTR_ERR_RE.search(s):
        # 周期类属性误用优先归 wrong_frequency
        return "wrong_frequency" if _FREQ_HINT_RE.search(s) else "wrong_column"
    if _FREQ_HINT_RE.search(s) and re.search(r"(非法|invalid|无效|not in)", s, re.I):
        return "wrong_frequency"
    if _EMPTY_HINT_RE.search(s):
        return "empty_result"
    return None


def _clip(text: str, n: int = MAX_LEN) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


# 每类错误的"禁止令 + 已确认事实"模板（不给具体方案，避免越权指导）
_DIRECTIVE = {
    "hallucinated_tool": "该名字不存在；不要再用它或其变体，改用可用工具清单内的名称。",
    "sandbox_unavailable": "该能力在执行环境被禁止（凭据路径/受限模块）；不要再尝试同类调用。",
    "wrong_column": "字段/属性误用；先确认变量类型（print(type(x))）与字段名再取值。",
    "wrong_frequency": "周期/频率参数非法；只在工具声明的合法取值内选择。",
    "empty_result": "该数据源此次返回空；不要再重复同一调用，改换数据源或用已有数据说明缺口。",
    "self_check_failed": "数据自检未过；不要把未通过校验的数据写入结论，缺口记入 missing_data。",
    "data_repair_failed": "自动修复已放弃；不要继续盲目重试，按已知缺口降级交付。",
}


class FailureMemory:
    """run 内的失败记忆（进程内对象，随 agent 实例存活）。"""

    def __init__(self) -> None:
        self.counts: Dict[str, int] = {}
        self.details: Dict[str, str] = {}
        self._injected: set = set()
        self.repeated: Dict[str, int] = {}   # 重复发生次数（>0 即"再次犯"）

    def record(self, error_type: str, detail: str = "") -> bool:
        """登记一次失败。返回 True 表示该 error_type 是**本 run 首次**出现。"""
        if error_type not in ERROR_TYPES:
            return False
        first = error_type not in self.counts
        self.counts[error_type] = self.counts.get(error_type, 0) + 1
        if detail and error_type not in self.details:
            self.details[error_type] = _clip(detail)
        if not first:
            self.repeated[error_type] = self.repeated.get(error_type, 0) + 1
        return first

    def new_types(self) -> List[str]:
        """尚未注入过的错误类型（保持词表顺序，最多 MAX_ITEMS 条）。"""
        out = [t for t in ERROR_TYPES if self.counts.get(t) and t not in self._injected]
        return out[:MAX_ITEMS]

    def mark_injected(self, error_types) -> None:
        for t in error_types:
            self._injected.add(t)

    def render(self) -> str:
        """合成注入文本（≤MAX_ITEMS 条，每条 ≤MAX_LEN）；无可注入返回 ""。"""
        items = []
        for t in ERROR_TYPES[:MAX_ITEMS]:
            if not self.counts.get(t):
                continue
            line = f"- [{t}] {_DIRECTIVE.get(t, '')}"
            det = self.details.get(t)
            if det:
                line += f"（已确认：{det}）"
            items.append(_clip(line, MAX_LEN))
        if not items:
            return ""
        return "【失败记忆】本 run 已发生的错误（禁止令；不要重复犯同样错误）：\n" + "\n".join(items)

    def snapshot(self) -> dict:
        return {"counts": dict(self.counts), "repeated": dict(self.repeated),
                "types": [t for t in ERROR_TYPES if self.counts.get(t)]}


def drain_executor_events(executor) -> List[Tuple[str, str]]:
    """从执行器取回并清空本 step 登记的 (error_type, detail) 事件。

    执行器可能不存在（无沙箱路径）或未接线 → 安全返回 []。
    """
    if executor is None:
        return []
    ev = getattr(executor, "_failure_events", None)
    if not ev:
        return []
    try:
        out = list(ev)
        del ev[:]
        return out
    except Exception:
        return []
