# -*- coding: utf-8 -*-
"""_skill_block 注入层修正（2026-10-20）：超大技能不再盲截正文。

验证方向（用户裁定）：
  · 小技能（≤ _SKILL_BODY_CAP）全文注入，行为不变；
  · 超大技能 = 【使用场景（首个 ## 段）】 + 【小节清单】 + 【按需读路标】，
    不再 `_clip(body, cap)` 把正文前 N 字砍一段塞进去；
  · read_skill(heading=...) 小节选择机制（skills_adapter.load_section）被注入层
    真正用上——通过「小节清单 + 指针」告知模型可单节按需读取；
  · 无 ## 结构 / 取段落失败 ⇒ 降级取首段，绝不盲截整篇。

不碰真技能目录、不联网；fake 复用 skills_adapter._split_sections 造结构。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent.qd_service import _skill_block, _SKILL_BODY_CAP  # noqa: E402
from app.agent.skills_adapter import _split_sections            # noqa: E402


class _FakeSkills:
    """最小替身：load_sections 复用真 _split_sections；list_resources 可控。"""

    def __init__(self, body, resources=(), raise_on_load=False):
        self._body = body
        self._resources = list(resources)
        self._raise = raise_on_load

    def load_sections(self, name):
        if self._raise:
            raise RuntimeError("boom")
        return _split_sections(self._body)

    def list_resources(self, name):
        return list(self._resources)


# 一个 > cap 的超大技能：首个 ## 段是"使用场景"，后面还有多个 ## 段与长内容。
_LARGE_BODY = (
    "# 示例技能\n\n"
    "## 使用场景\n\n"
    "本技能用于【唯一使用场景标记】多维度资本面尽调；用户问\"分析XX\"时优先匹配。\n\n"
    "## 分析框架\n\n"
    + ("分析框架内容行，用于撑长正文。\n" * 80)
    + "\n## 步骤\n\n"
    + ("步骤细节，进一步撑长正文以确保超过上限。\n" * 80)
    + "\n## 注意事项\n\n"
    + ("注意事项，继续撑长。\n" * 40)
)


def test_small_skill_injected_verbatim():
    """小技能全文注入，输出即 body（回归：小技能行为不变）。"""
    body = "## 使用场景\n\n短技能，无截断。\n"
    out = _skill_block("small", body, _FakeSkills(body))
    assert out == body
    assert "已截断" not in out


def test_large_skill_gives_scenario_and_outline_not_blind_clip():
    """超大技能：给使用场景 + 小节清单，不盲截、不出现"已截断"。"""
    out = _skill_block("big", _LARGE_BODY, _FakeSkills(_LARGE_BODY))
    # ① 使用场景段被注入（首个 ## 段内容）
    assert "【唯一使用场景标记】" in out, "首个 ## 段（使用场景）必须注入"
    # ② 小节清单含后续 ## 段标题
    assert "分析框架" in out and "步骤" in out and "注意事项" in out, "小节清单应列后续 ## 段"
    assert "技能大纲" in out, "应给出小节清单（[技能大纲] 段）"
    # ③ 按需读路标指向 read_skill(heading=...)
    assert 'read_skill(name="big", heading=' in out, "应告知可用 read_skill(heading=) 单节读取"
    assert "已截断" not in out, "不得盲截：旧'已截断'标记必须消失"
    # 关键负向：输出不得是把正文前 2200 字硬砍一段（使用场景段之后的长内容不应整段丢）
    assert "[技能目录]" not in out, "旧'盲截+目录'形态必须消失"


def test_large_skill_outline_uses_section_headings():
    """大纲用真实 ## 段标题，而非盲截正文的前缀。"""
    out = _skill_block("big", _LARGE_BODY, _FakeSkills(_LARGE_BODY))
    # 正文里"分析框架内容行"是长填充，不应出现在大纲前的正文区作为盲截片段
    assert "分析框架内容行" not in out.split("[技能大纲]")[0], \
        "使用场景段之后才是长填充；盲截会把填充带进决策段，这里必须没有"


def test_no_structure_falls_back_to_first_paragraph():
    """无 ## 结构（>cap）⇒ 降级取首段，绝不盲截整篇、不谎称有大纲。"""
    body = "首段引导语，说明这个技能干嘛用。\n\n" + ("长填充内容行。\n" * 300)
    assert len(body) > _SKILL_BODY_CAP, "前置：必须超过注入上限才走大技能分支"
    out = _skill_block("nostruct", body, _FakeSkills(body))
    assert "首段引导语" in out
    assert "（无）" in out, "无 ## 结构 ⇒ 大纲应为（无）"
    assert "已截断" not in out


def test_load_sections_failure_is_fail_open():
    """取段落失败（raise）⇒ fail-open：降级首段，不抛、不静默成空。"""
    body = "首段引导语。\n\n" + ("长填充。\n" * 500)
    assert len(body) > _SKILL_BODY_CAP, "前置：必须超过注入上限才走大技能分支"
    out = _skill_block("broken", body, _FakeSkills(body, raise_on_load=True))
    assert "首段引导语" in out
    assert "已截断" not in out


def test_resource_hint_only_when_resources_exist():
    """有资源时才给 resource 路标；无资源不诱导必然失败的工具调用。"""
    out_no = _skill_block("big", _LARGE_BODY, _FakeSkills(_LARGE_BODY, resources=()))
    assert "references/<文件>" not in out_no
    out_yes = _skill_block("big", _LARGE_BODY,
                           _FakeSkills(_LARGE_BODY, resources=("references/x.md",)))
    assert "references/<文件>" in out_yes
