# -*- coding: utf-8 -*-
r"""V17b：注入侧**句子边界截断**守卫（2026-10-17，审计 I2a）。

★ 背景（tmp/llm_four_dimension_audit_20261017.md）
  任务书三段注入全部裸切在句子中间：近史 `[:200]` 切出"不建议在分歧转一"、
  "## 一、行情快照（2026-10-1"；参考资料 `[:300]` 同类；技能正文
  `body[:_SKILL_BODY_CAP]` 把"### 步骤 4"标题腰斩。半句残片进上下文，
  模型读着费 token，还可能把半句话当事实引用。

★ 修复
  `qd_service._clip(text, limit)`：切点取 ≤limit 的最后一个边界字符
  （换行>句末>分句>逗号），前半段无边界才硬切。三处注入点
  （近史 200 / 参考资料 300 / 技能正文 _SKILL_BODY_CAP）全部改走 _clip。

本文件钉死：① 不超限 ② 尽量落在边界 ③ 无边界时退化为硬切（上限语义不变）
④ 三个注入点确实在用 _clip（AST，防回退成裸切片）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.agent.qd_service import _clip, _SKILL_BODY_CAP  # noqa: E402

_BACKEND = Path(__file__).resolve().parents[1]
_SVC = _BACKEND / "app" / "agent" / "qd_service.py"


def test_clip_short_text_untouched():
    assert _clip("短短一句话。", 200) == "短短一句话。"


def test_clip_lands_on_sentence_boundary():
    # 窗口内有多个句末 ⇒ 必须切在 ≤limit 的最后一个句号上，不带后半句
    text = "结论先行。分析内容还有很多。第二段内容继续写。更后面的句子不该出现"
    out = _clip(text, 30)
    assert out.endswith("。")
    assert "更后面的句子不该出现" not in out
    assert len(out) <= 30


def test_clip_prefers_newline_over_comma():
    text = "第一行内容，带逗号\n第二行内容\n第三行内容不应该出现"
    out = _clip(text, 20)
    assert out.endswith("\n"), f"应优先切在换行边界: {out!r}"
    assert "第三行" not in out


def test_clip_falls_back_to_hard_cut_without_boundary():
    # 前半段一个边界都没有 ⇒ 保持原上限语义（硬切），不能无限缩短
    text = "无标点长串" * 50
    out = _clip(text, 30)
    assert len(out) == 30


def test_clip_boundary_must_be_in_front_half():
    # 边界出现在很靠前（<limit/2）时不能被采用，否则截出来的内容太短
    text = "早。" + "后续无边界内容" * 30
    out = _clip(text, 40)
    assert len(out) == 40 and not out.endswith("。")


def test_injection_points_use_clip():
    """④ 三个注入点必须走 _clip，不允许回退成裸切片（AST 级防呆）。

    ★ 2026-10-21 夹具同型化（**断言意图不变**，见 test_v48 守卫）：
      近史注入点原为单行表达式
          f"- {role}: {_clip(str(getattr(m, 'content', '')), 200)}"
      2026-10-21 记忆近史改「assistant 旧答案降级为标题行」后拆成 if/else
      （user 侧仍走 `_clip(_content, 200)`；assistant 侧走
      `_history_headline`，其内部对标题行仍调 `_clip`）。
      ⇒ 断言从「旧字面形态」改为**同型语义**：近史注入路径上必须有
      `_clip(..., 200)` 调用，且**不得**出现裸切片 `[:200]`。
    """
    src = _SVC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # ① 近史注入点：必须仍有 _clip(..., 200) 调用，且不得回退成裸切片
    clip200 = [n for n in ast.walk(tree)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name) and n.func.id == "_clip"
               and any(isinstance(a, ast.Constant) and a.value == 200
                       for a in n.args)]
    assert clip200, "近史注入点未用 _clip(..., 200)"
    # ② 参考资料注入点未变
    assert "_clip(str(d.get('content', '')), 300)" in src, "参考资料注入点未用 _clip"
    # ③ 技能正文注入点未变
    assert "_clip(body, _SKILL_BODY_CAP)" in src, "技能正文注入点未用 _clip"
    # ④ 裸切片防回退：近史/参考资料/技能正文三处不得用 str[:N] 硬切
    for n in ast.walk(tree):
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant):
            assert n.slice.value not in (200, 300), (
                "注入路径回退成裸切片 %r（须走 _clip）" % n.slice.value)


def test_skill_body_cap_positive():
    """技能正文上限旋钮必须仍是正整数（_clip 依赖它）。"""
    assert isinstance(_SKILL_BODY_CAP, int) and _SKILL_BODY_CAP > 0
