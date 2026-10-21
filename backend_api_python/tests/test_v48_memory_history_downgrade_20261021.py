# -*- coding: utf-8 -*-
"""守卫：记忆近史的 assistant 旧答案必须**降级为标题行**（2026-10-21）。

【背景】步数测试（cli 链路，tmp/llm_capture_step2_20261021.jsonl）实测：
预选/主循环的 user 消息里，`[会话记忆近史]` 块注入的是上一轮 assistant 的
**完整诊断**（被 `_clip(200)` 拦腰切断），实测注入内容为半截表格：

    - assistant: | **今日表现** | 跌停收盘（-9.96%），光头光脚大阴线 |

而本轮实际行情是 **+2.83%**。两个问题：
  ① **过时数字混入** —— 违反 system 数字纪律「价格/涨跌停等关键锚点必须本轮复核」；
  ② **身份不可辨** —— 旧答案与模型自己刚产出的最终答案格式完全同构
     （`## 标题` + `| 表格 |`）⇒ 模型无法快速判断这是"历史"还是"本轮事实"。

【裁定】2026-10-21 用户裁定：**只做降级，不加警示头尾**（注入块形态不变）。
  · assistant 侧旧答案 → 一行标题（含标的/周期/**时点**），丢正文；
  · user 侧原样保留（用户原话，无过时数字问题，且是相关度判定依据）。

【本文件锁三件事】
  ① `_history_headline` 把旧答案降级成单行，且**不含行情数字**；
  ② 降级后仍**保留时点**（模型据此判断这条历史有多旧）；
  ③ 记忆近史注入块：assistant 段走降级、user 段原样（接线位置正确）。

【为什么用 AST 查接线】记忆注入在 `qd_service._prefetch` 里是运行时的，
不能靠全文匹配（docstring 里写 "assistant" 也会被误判为已引用）。
本文件用**直接调函数 + 静态读源码断言**双保险，不建 agent、不联网。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
QD_SERVICE = BACKEND / "app" / "agent" / "qd_service.py"

# 实测捕获里的真实旧答案（含过时行情数字与时点）
REAL_OLD_ANSWER = (
    "## 郑州煤电(600121) T+3 诊断\n\n"
    "**数据状态**：时点 2026-10-21 10:15（盘中未收盘，最新 4.72 元，+2.83%）；"
    "K线 120 日前复权；财报报告期 2026-06-30。\n\n"
    "### 一、结论\n\n"
    "| 周期 | 方向 | 关键位 |\n|---|---|---|\n"
    "| T+1 | 中性偏弱 | 压力 4.90、5.05；支撑 4.61 |\n"
)

# 旧答案里出现的**过时行情数字**（降级后必须全部消失）
STALE_NUMBERS = ["4.72", "+2.83%", "4.90", "5.05", "4.61", "2026-06-30"]


def _headline():
    from app.agent.qd_service import _history_headline
    return _history_headline


# ── ① 降级成单行 + 不含行情数字 ───────────────────────────────────

def test_history_headline_drops_stale_market_numbers() -> None:
    """旧答案降级后不得残留任何行情/财务数字。"""
    h = _headline()(REAL_OLD_ANSWER)
    assert h, "降级结果不得为空（旧答案有 ## 标题）"
    for num in STALE_NUMBERS:
        assert num not in h, f"降级后仍残留过时数字 {num!r}：{h!r}"
    # 也不能残留表格管道
    assert "|" not in h, f"降级后仍含表格行：{h!r}"
    # 不得是多行（降级 = 一行）
    assert "\n" not in h, f"降级结果应是一行，实际多行：{h!r}"


def test_history_headline_keeps_target_and_cycle() -> None:
    """降级后必须仍保留标的与周期（这是续接真正需要的上下文）。"""
    h = _headline()(REAL_OLD_ANSWER)
    assert "郑州煤电" in h, f"丢了标的：{h!r}"
    assert "600121" in h, f"丢了代码：{h!r}"
    assert "T+3" in h, f"丢了周期：{h!r}"


# ── ② 保留时点 ──────────────────────────────────────────────────

def test_history_headline_keeps_timestamp() -> None:
    """降级后仍保留时点（模型据此判断这条历史有多旧）。

    `MemoryMessage` 只有 role/content、无时间戳字段 ⇒ 时点只能从旧答案正文
    的「分析时点/诊断日期/时点」里抽。
    """
    h = _headline()(REAL_OLD_ANSWER)
    assert "2026-10-21" in h, f"丢了时点：{h!r}"
    assert "10:15" in h, f"丢了时点中的时分：{h!r}"


def test_history_headline_falls_back_to_any_date() -> None:
    """没有「时点/诊断日期」字样时，回落到正文里任意日期。"""
    h = _headline()("## 某某诊断\n\n报告日期 2026-01-02 的一些内容")
    assert "2026-01-02" in h, f"未回落到任意日期：{h!r}"


# ── ③ 接线位置：assistant 走降级、user 原样 ────────────────────────

def test_memory_block_uses_downgrade_for_assistant_only() -> None:
    """记忆近史接线：assistant 段走 `_history_headline`，user 段走 `_clip`。

    静态断言源码里的分支结构（记忆注入在运行时的 _prefetch 内，无法直接调）。
    """
    src = QD_SERVICE.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 找到用了 _history_headline 的位置
    call_nodes = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name)
                  and n.func.id == "_history_headline"]
    assert call_nodes, "_history_headline 未被任何地方调用 ⇒ 降级未接线"

    # 该调用必须在一个以 role == "assistant" 为条件的分支里
    guarded = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        cond_src = ast.unparse(node.test)
        if "assistant" in cond_src and "role" in cond_src:
            inside = [n for n in ast.walk(node)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Name)
                      and n.func.id == "_history_headline"]
            if inside:
                guarded = True
                break
    assert guarded, ("_history_headline 的调用不在 'role == assistant' 分支内 ⇒ "
                     "可能把 user 原话也降级了（会丢用户意图）")

    # user 分支必须仍走 _clip（原样保留）
    assert re.search(r'_clip\(_content,\s*200\)', src), (
        "user 侧应仍走 _clip(200) 原样保留（相关度判定依据）")


def test_injection_block_shape_unchanged() -> None:
    """注入块形态不变（用户裁定：只做降级，不加警示头尾）。"""
    src = QD_SERVICE.read_text(encoding="utf-8")
    assert '"[会话记忆近史]\\n" + block' in src, (
        "注入块标签/形态被改动（用户裁定：只做降级）")
    # 不应出现新增的警示头（本次明确不做）
    for forbidden in ["【会话记忆近史——仅作参考", "会话记忆近史——仅作参考"]:
        assert forbidden not in src, f"擅自加了警示头：{forbidden}"
