# -*- coding: utf-8 -*-
"""数字规范条件注入测试（2026-09-30 提智 #5）。

背景：
  - 规则 18/19（CLAIMS/数据自检）全量常驻 system_prompt，是 8.9k 膨胀的大头之一；
  - 规则 18 里的 `submit_claims(...)` 是**幽灵工具名**（全库无此函数，§7.17.4 同款坑）。
现改为：完整规范移 prompts/numeric_rules.txt，分析类任务才注入；yaml 留一行
溯源纪律；幽灵调用名删除。本测试锁住三个不变量，防回归。
"""
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1] / "app" / "agent"
YAML_PATH = AGENT_DIR / "prompts" / "code_agent.yaml"
RULES_PATH = AGENT_DIR / "prompts" / "numeric_rules.txt"


def test_numeric_rules_file_exists_and_real():
    text = RULES_PATH.read_text(encoding="utf-8")
    assert "CLAIMS" in text
    assert "validate_df" in text          # 数据自检是真的（utils/data_check.py）
    assert "output_claims" in text
    assert "submit_claims" not in text    # 幽灵工具名不得回归


def test_yaml_rules_compact_and_numbered():
    text = YAML_PATH.read_text(encoding="utf-8")
    assert "submit_claims" not in text
    assert "output_claims = [" not in text   # claims 细则已移出常驻面
    assert "**数字必须可溯源**" in text        # 紧凑版规则 18 保留
    assert "【需重规划】" in text              # 规则 19（原 20）死路自报仍在
    # 编号连续性（§7.17.3：删中间规则须重排保持连续）：17→18→19
    for n in ("17.", "18.", "19."):
        assert f"  {n} " in text, f"规则编号 {n} 缺失"
    assert "  20. " not in text
