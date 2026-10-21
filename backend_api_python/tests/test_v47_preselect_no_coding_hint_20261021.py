# -*- coding: utf-8 -*-
"""预选层撤回编码工具线索（coding_hint）守卫 —— 2026-10-21 用户裁定（方案 A）。

【背景】预选第一步的 user message 里，模板末尾的续写引导
`用户"{query}" →` 之后被**追加**了一段编码工具提示
（"提示：如需读取/修改文件、运行脚本或调试代码，优先从目录里选择：bash, edit, …"）。
实测证据（tmp/seq1_payload_dump.txt 第 59-60 行，纯股票任务「分析郑州煤电」）：
    用户"分析郑州煤电" →
    提示：如需读取/修改文件、运行脚本或调试代码，优先从目录里选择：bash, edit, glob, grep, read, task, write
⇒ 两个问题：① 无关 token（纯金融任务不需要编码工具）；② **把续写引导顶断**，
   模型看到的是半句示例 + 无关提示。

【根因（实测复刻，tmp/probe_coding_hint_impact2_20261021.py）】
  判据 `n in valid` 里的 `valid` 是**全网**合法点名集（按需层全集），**不是**目录集
  ⇒ cli 模式下编码工具**恒命中** ⇒ 提示每轮必注入（我上一轮口头说"valid 不含它们"
  是错的，此处已按实测更正）。
  裁剪链路实测（catalog_cap=24）：
    · 纯金融 query（分析郑州煤电）→ 目录 9 条，**无**编码工具；
    · 纯编码 query（看看 qd_agent.py 怎么实现的）→ 目录 48 条，**含**全部 7 个
      编码工具 ⇒ 提示纯冗余；
    · 混合 query（分析郑州煤电并用 python 画个 K 线图）→ 目录 9 条，无编码工具。

【裁定与边界】
  · 撤回的是**生产调用点的传值**（`qd_preselect` 不再传 `coding_hint=`），
    **不是**删参数/删能力——`build_preselect_messages(coding_hint=...)` 的拼装
    能力保留（设计文档 §3.3 第 5 步把 coding_hint 登记为 plan 契约，删它=破坏
    契约+降智），回滚只需在调用处补回一行。
  · 能力损失已核对（不阻断）：编码类任务目录**本来就含**编码工具 ⇒ 模型照常点名；
    目录被裁掉编码工具时，主循环系统提示的"延迟层清单"仍列着这些名字，
    模型可自行 `activate_tools` 补回。

【本文件锁三件事】
  ① **AST 接线守卫**：`qd_preselect` 里调 `build_preselect_messages` 的那次调用
     **不得**出现 `coding_hint` 关键字（查 Call 节点的 keywords，不靠全文匹配——
     docstring 里写着"coding_hint"也会被全文匹配误判为已引用）。
  ② **能力保留守卫**：`build_preselect_messages` 仍接受 `coding_hint` 形参，且
     传值时确实会拼进 body（防止有人"顺手把参数也删了"=降智+破契约）。
  ③ **续写引导完整守卫**：预选 body 的**最后一行**必须是模板的续写引导
     `用户"{query}" →`，其后再无任何内容（结构不被顶断）。

测试纪律：纯静态 + 纯函数，不建 agent、不联网、不碰真库。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
QD_PRESELECT = BACKEND / "app" / "agent" / "qd_preselect.py"
TOOL_PRESELECT = BACKEND / "app" / "agent" / "preselect" / "tool_preselect.py"

_HINT_MARK = "提示：如需读取/修改文件"


# ── ① AST 接线守卫：调用点不得传 coding_hint ──────────────────────────

def _preselect_build_calls(tree: ast.Module) -> list:
    """返回 qd_preselect 里所有 `build_preselect_messages(...)` 的 Call 节点。"""
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "build_preselect_messages"
    ]


def test_preselect_does_not_pass_coding_hint() -> None:
    """生产调用点不再传 coding_hint（方案 A 的落地本体）。"""
    tree = ast.parse(QD_PRESELECT.read_text(encoding="utf-8"))
    calls = _preselect_build_calls(tree)
    assert calls, "未找到 build_preselect_messages 调用点（守卫失效，请更新）"
    for call in calls:
        kw = {k.arg for k in call.keywords if k.arg}
        assert "coding_hint" not in kw, (
            "预选调用点仍在传 coding_hint ⇒ 方案 A 被回退（用户裁定 2026-10-21）")


def test_coding_hint_never_assembled_in_preselect_module() -> None:
    """qd_preselect 里不应再出现"拼装 hint"的代码（只允许文档性提及）。

    查的是**字符串字面量**（会进 body 的那段文案），不是"coding_hint"这个词
    ——docstring 里的字样是合法的说明文字。
    """
    tree = ast.parse(QD_PRESELECT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert _HINT_MARK not in node.value, (
                "qd_preselect 仍在构造编码提示文案 ⇒ 方案 A 被回退")


# ── ② 能力保留守卫：参数与拼装仍在 ──────────────────────────────────

def test_coding_hint_parameter_still_supported() -> None:
    """`build_preselect_messages` 仍接受 coding_hint，且传值会拼进 body。

    这是红线「禁止降智」的落点：撤的是**调用点的传值**，不是能力本身。
    """
    from app.agent.preselect import tool_preselect as T

    sig = inspect.signature(T.build_preselect_messages)
    assert "coding_hint" in sig.parameters, (
        "build_preselect_messages 丢了 coding_hint 形参 ⇒ 破坏设计文档 §3.3 "
        "第 5 步的 plan 契约（降智）")

    base = T.build_preselect_messages("分析郑州煤电", "（目录略）", 8)
    with_hint = T.build_preselect_messages(
        "分析郑州煤电", "（目录略）", 8,
        coding_hint=_HINT_MARK + "：bash, edit")
    assert _HINT_MARK not in base[1]["content"], "空 hint 时不该出现该段"
    assert _HINT_MARK in with_hint[1]["content"], (
        "传了 coding_hint 却没拼进 body ⇒ 能力被删（降智）")


# ── ③ 续写引导完整守卫：末行不被顶断 ────────────────────────────────

def test_prompt_tail_continuation_line_is_last() -> None:
    """预选 body 末行必须是续写引导 `用户"{query}" →`，其后无内容。

    这是本次污染的**结构性症状**（hint 追加在末尾 ⇒ 顶断引导）。
    断言用"末行**必须精确等于**引导行"而非"含引导字样"——否则删掉引导行
    （body 末行退化成上一行）会 SURVIVED（变异 M4 实测过）。
    """
    from app.agent.preselect import tool_preselect as T

    body = T.build_preselect_messages(
        "分析郑州煤电", "（目录略）", 8, already=["web_search"])[1]["content"]
    lines = [ln for ln in body.split("\n") if ln.strip()]
    assert lines, "body 不应为空"
    assert lines[-1].strip() == '用户"分析郑州煤电" →', (
        "预选 body 末行不是续写引导（实际=%r）⇒ 模板结构被顶断" % lines[-1])


def test_prompt_tail_keeps_example_and_planning_blocks() -> None:
    """模板的「【示例】」与「【待规划】」两块必须都在，且【待规划】是最后一块。

    守卫③的补充：只查末行不够——若有人把「【待规划】/引导行」整块删掉，
    末行会退化成示例行。故显式断言三块齐全 + 末块是【待规划】。
    """
    from app.agent.preselect import tool_preselect as T

    body = T.build_preselect_messages(
        "分析郑州煤电", "（目录略）", 8, already=["web_search"])[1]["content"]
    assert "【示例】" in body, "模板缺少【示例】块"
    assert "【待规划】" in body, "模板缺少【待规划】块"
    assert body.index("【示例】") < body.index("【待规划】"), (
        "【示例】应在【待规划】之前（模板结构被打乱）")
    # 示例里的 JSON 必须完整收尾（不被 hint 半句截断）
    assert '"skill":"auto-stock-analyze"}' in body, (
        "示例 JSON 未闭合 ⇒ 示例段被截断")



def test_prompt_has_no_coding_hint_tail_by_default() -> None:
    """默认（生产路径）产出的 body 不含编码提示文案。"""
    from app.agent.preselect import tool_preselect as T

    body = T.build_preselect_messages(
        "分析郑州煤电", "（目录略）", 8, already=["web_search"])[1]["content"]
    assert _HINT_MARK not in body, "预选 body 仍含编码提示文案"
