# -*- coding: utf-8 -*-
"""难度路由回归测试（2026-09-30 提智：单标的综合分析 L2 地板）。

背景：T2 难度路由上线后，"分析一下600929"这类单股综合分析落 L1（单候选规划、
无 critic、deterministic verify）——个股分析忽好忽坏的路由层根因。地板规则：
分析类动作 + 实体 + 非单点数据查询 → L2（best-of-N + critic + 全量 linter）。
"""
import importlib.util
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1] / "app" / "agent"
sys.path.insert(0, str(AGENT_DIR))

# 按文件路径直载（agents/__init__ 会拉 task_agent→smolagents 重依赖；
# routing_policy 本身零重依赖）
_spec = importlib.util.spec_from_file_location(
    "routing_policy_standalone", AGENT_DIR / "agents" / "routing_policy.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
score_difficulty = _mod.score_difficulty


def _lv(text):
    return score_difficulty(text)[0]


def test_comprehensive_analysis_floors_l2():
    assert _lv("分析一下600929") == "L2"
    assert _lv("300497怎么样") == "L2"
    assert _lv("看看这只票怎么样") == "L2"
    assert _lv("帮我研究下贵州茅台这只股票，技术面和基本面如何") == "L2"
    assert _lv("明天买什么股") == "L2"
    assert _lv("研究一下半导体板块的景气度") == "L2"   # 板块/行业同样多数据域


def test_narrow_query_stays_low():
    assert _lv("600519的市盈率是多少") in ("L0", "L1")
    assert _lv("分析600519的市盈率") in ("L0", "L1")   # 单点数据 → 不抬档
    assert _lv("今天天气怎么样") in ("L0", "L1")        # 无实体 → 不抬档


def test_l3_direct_shortcut_unchanged():
    assert _lv("对全市场做多空回测研究") == "L3"


def test_detect_self_replan_marker():
    """T2 升级信号第三路：只认【需重规划】标记，不猜语义。"""
    detect = _mod.detect_self_replan
    assert detect("【需重规划】主力资金接口全部失败，无法完成资金面维度")
    assert not detect("分析完成。风险提示：无法获取盘口逐笔数据，已用资金流替代。")
    assert not detect("")


def test_tier_model_env():
    """模型档位接线：strong 配置即生效；small 受降档闸（默认关）。"""
    import os
    assert _mod.tier_model("L2") == ""            # 未配置 strong → 默认 LLM
    assert _mod.tier_model("L0") == ""            # 降档闸默认关
    os.environ["AGENT_MODEL_TIER_STRONG"] = "glm-strong-test"
    os.environ["AGENT_MODEL_TIER_SMALL"] = "glm-small-test"
    try:
        assert _mod.tier_model("L2") == "glm-strong-test"
        assert _mod.tier_model("L3") == "glm-strong-test"
        assert _mod.tier_model("L0") == ""        # 闸未开 → 不降档
        os.environ["AGENT_ROUTING_DOWNGRADE"] = "1"
        assert _mod.tier_model("L0") == "glm-small-test"
    finally:
        for k in ("AGENT_MODEL_TIER_STRONG", "AGENT_MODEL_TIER_SMALL",
                  "AGENT_ROUTING_DOWNGRADE"):
            os.environ.pop(k, None)
