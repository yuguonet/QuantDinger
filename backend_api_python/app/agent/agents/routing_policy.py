# -*- coding: utf-8 -*-
"""难度路由登记表（提智方案 三波 T2，2026-09-24，署名：OpenClaw agent）。

来源：`agent_tizhi_final_plan_20260923.md` §三 三波 T2 + 用户方案 §三 二（难度路由）。

职责（**判定与政策的单一事实源**；禁在 nodes/task_agent 里散写 if——项目红线）：
  · `score_difficulty()`：确定性特征打分 → L0~L3（零调用）；
  · `judge_difficulty()`：规则得分落在临界带（±10%）时才让模型判一次（廉价、单问）；
  · `planning_samples()`：Best-of-N 的 N 按难度取（L0/L1=1，L2/L3=AGENT_PLAN_BEST_OF_N）；
  · `should_upgrade()`：执行期升级信号（单段工具调用超阈值 / 跨域缺口 / 自报需重规划）
    → 丢弃单段从头 plan（"单段本来就便宜，沉没成本不心疼"——用户方案原文）；
  · `ROUTING_MATRIX`：完整路由矩阵（模型/管线/verify/linter/best-of-N/双跑），
    **登记表形态**，评测集校准后逐格启用。

启用闸（用户方案 + 裁决 #4）：
  · **L0/L1 降档（小模型/合并快跑）默认关闭**（AGENT_ROUTING_DOWNGRADE=1 才开）——
    需评测集先证"小模型掉点 <5pp"才准启用；未达标前降档=无证据降质。
  · best-of-N / critic 的成本闸**照常生效**（L0/L1 单候选省下的预算正是
    "提智不涨总成本"的来源）。
  · 模型档位切换（small/strong）当前**未接线**（每 run 单 LLM 实例），矩阵该列先留档，
    评测达标后接 llm 工厂——不在此处假装生效。

设计点：
  · 规则先行、模型只判临界带（裁决 #4 采纳用户版）：绝大多数消息不花额外调用；
  · 判定结果三处共用：chat 意图（difficulty 进 trace）、_plan（N 的取值）、
    执行期升级（丢弃单段重 plan）；全部只读本模块，不各自造阈值；
  · 分数与特征全量落 trace（difficulty_route 事件），路由质量可复盘（升级率 <10%）。

易错点：
  · 临界带是"模型判"的触发条件，不是覆盖条件：模型判失败/超时一律回退规则结果；
  · difficulty 在 chat 阶段产出，plan/execute 经 state/ctx 传递——不要重新算一遍
    （两次结果可能不同，路由抖动比误判更难查）；
  · 升级信号只对**单段**路径有效：多阶段路径本就有 _phase_replan_request 机制，
    两边都触发会双倍回炉。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "ROUTING_MATRIX", "score_difficulty", "difficulty_block", "parse_level",
    "planning_samples", "best_of_n", "should_upgrade", "policy",
    "LEVELS",
]

LEVELS = ("L0", "L1", "L2", "L3")

# ═══════════════════════════════════════════════════════════════
#  路由矩阵（登记表；行为以列为准，新增档位只加行）
# ═══════════════════════════════════════════════════════════════
# model_tier: small|strong（⚠️ 未接线，见头部"启用闸"）
# pipeline:   single（单段）| phases（多阶段）
# verify:     off | deterministic | full
# plan_linter:off | deterministic | full（R1~R4 确定性层 + LLM critic）
# best_of_n:  on|off（off = N=1）
# cross_run:  on(sensitive)|off（远期 F2，双跑交叉验证）
ROUTING_MATRIX: Dict[str, Dict[str, str]] = {
    "L0": {"model_tier": "small", "pipeline": "single", "verify": "deterministic",
           "plan_linter": "off", "best_of_n": "off", "cross_run": "off"},
    "L1": {"model_tier": "small", "pipeline": "single", "verify": "deterministic",
           "plan_linter": "deterministic", "best_of_n": "off", "cross_run": "off"},
    "L2": {"model_tier": "strong", "pipeline": "phases", "verify": "full",
           "plan_linter": "full", "best_of_n": "on", "cross_run": "off"},
    "L3": {"model_tier": "strong", "pipeline": "phases", "verify": "full",
           "plan_linter": "full", "best_of_n": "on", "cross_run": "on(sensitive)"},
}


def policy(level: str) -> Dict[str, str]:
    """取难度档位的路由矩阵行；未知档位回退 L2 行（保守侧：全开不降档）。"""
    return ROUTING_MATRIX.get(str(level or "").upper(), ROUTING_MATRIX["L2"])


def best_of_n() -> int:
    """Best-of-N 基数 N（env AGENT_PLAN_BEST_OF_N，默认 2，钳制 [1,3]）。

    裁决 #5：N=2 起步（顺带防 planner JSON 损坏）；方差大才升 N=3（评测集校准后再动）。
    """
    raw = (os.getenv("AGENT_PLAN_BEST_OF_N", "2") or "2").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 2
    return max(1, min(3, n))


def planning_samples(level: str) -> int:
    """本次规划的候选采样数：L0/L1=1（矩阵 best_of_n=off），L2/L3=N。"""
    return best_of_n() if policy(level)["best_of_n"] == "on" else 1


def downgrade_enabled() -> bool:
    """L0/L1 降档开关（默认关）：需评测集先证小模型掉点 <5pp（C8/启用闸）。"""
    return (os.getenv("AGENT_ROUTING_DOWNGRADE", "0") or "0").strip().lower() \
        in ("1", "true", "yes", "on")


def _upgrade_tool_calls() -> int:
    """单段工具调用升级阈值（env AGENT_UPGRADE_TOOL_CALLS，默认 8）。"""
    raw = (os.getenv("AGENT_UPGRADE_TOOL_CALLS", "8") or "8").strip()
    try:
        return max(2, int(raw))
    except ValueError:
        return 8


def should_upgrade(level: str, tool_calls: int, *, cross_domain_gap: bool = False,
                   need_replan: bool = False) -> bool:
    """执行期升级信号（用户方案 §三 二）：初始分级偏低 → 丢弃单段从头 plan。

    只对 L0/L1 + 单段路径生效（L2/L3 本就是全开档；多阶段走 _phase_replan_request）。
    任一信号命中即升级：
      ① 单段内工具调用数超阈值（默认 8）；
      ② 数据自检报跨数据域缺口（调用方判定后传入）；
      ③ 模型自报 need_replan（调用方判定后传入）。
    """
    lv = str(level or "").upper()
    if lv not in ("L0", "L1"):
        return False
    return bool(tool_calls >= _upgrade_tool_calls()
                or cross_domain_gap or need_replan)


# ═══════════════════════════════════════════════════════════════
#  确定性难度打分（零调用）
# ═══════════════════════════════════════════════════════════════
# 特征登记表：(特征名, 权重, 关键词)。特征四族与用户方案对齐：
#   series=时间序列类 / research=回测·多空类 / multi=多标的·对比 / report=报告格式。
FEATURE_RULES: Tuple[Tuple[str, float, Tuple[str, ...]], ...] = (
    ("series",  1.0, ("时间序列", "走势", "历史表现", "历史行情", "净值", "收益曲线",
                      "日线", "周线", "月线", "k线", "周期", "区间")),
    ("research", 2.0, ("回测", "多空", "全市场", "策略", "深度研究", "归因",
                      "风险评估", "估值模型", "dcf")),
    ("multi",   1.5, ("对比", "比较", "pk", "排名", "排行", "组合", "批量",
                      "几只", "哪些", "选几", "一篮子")),
    ("report",  0.5, ("报告", "表格", "对比表", "研报", "白皮书", "markdown", "成文")),
)

# 词表（L3 直升：命中任一 + 总分过半即长链条研究）
_L3_DIRECT = ("回测", "多空", "全市场", "策略表现", "净值曲线", "组合优化")

# L0 直降：纯查询语（无分析动作词）
_QUERY_ONLY = ("是多少", "多少钱", "什么价", "现价", "是什么", "什么意思", "查一下",
               "涨了还是跌", "今天怎么样")

# 分析动作词（出现即不是纯查询）
_ANALYSIS_VERBS = ("分析", "评估", "诊断", "筛选", "选出", "推荐", "研究", "归因",
                   "对比", "预测", "判断", "怎么样", "怎么看", "值得")

_FULL_SCALE = sum(w for _n, w, _k in FEATURE_RULES) + 1.0   # 域广度加成上界 1.0
_BAND_EDGES = ((0.0, 1.0, "L0"), (1.0, 2.5, "L1"), (2.5, 10.0, "L2"))
BORDERLINE_PCT = 0.10          # 临界带 = 边界 ±10%（C8：带宽待评测集校准）
_CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")


def score_difficulty(text: str) -> Tuple[str, float, Dict[str, Any], bool]:
    """确定性特征打分（零 LLM）。Returns: (level, score, features, borderline)。

    得分构成：命中特征权重和 + 域广度加成 0.5×(数据域数-1)（封顶 1.0）+ 多标的加成。
    L3 直升 / L0 直降覆盖带内映射（研究类长链条 / 纯查询各自短路）。
    """
    s = str(text or "").lower()
    feats: Dict[str, Any] = {}
    score = 0.0
    for name, weight, kws in FEATURE_RULES:
        hit = [k for k in kws if k in s]
        feats[name] = hit[:5]
        if hit:
            score += weight

    # 域广度（预期工具数代理）：复用 plan_linter 的数据域词典（单一事实源）
    domains: List[str] = []
    try:
        from utils.plan_linter import detect_domains
        domains = detect_domains(text)
    except Exception:
        domains = []
    feats["domains"] = domains
    score += min(1.0, 0.5 * max(0, len(domains) - 1))

    # 多标的：≥3 个代码
    codes = _CODE_RE.findall(str(text or ""))
    feats["codes"] = codes[:8]
    if len(codes) >= 3:
        score += 1.0

    # 带内映射
    level = "L2"
    for lo, hi, lv in _BAND_EDGES:
        if lo <= score < hi:
            level = lv
            break

    # 直升/直降短路
    if any(k in s for k in _L3_DIRECT) and score >= 2.0:
        level = "L3"
    if level == "L0":
        has_query = any(k in s for k in _QUERY_ONLY)
        has_verb = any(k in s for k in _ANALYSIS_VERBS)
        if has_verb or not has_query:
            level = "L1"          # 动作词或非纯查询形态 → 至少单段计算

    # 临界带：距任一边界 ±10%×全量程
    tol = BORDERLINE_PCT * _FULL_SCALE
    borderline = any(abs(score - edge) <= tol for _lo, edge, _lv in _BAND_EDGES[1:])
    feats["score"] = round(score, 3)
    return level, round(score, 3), feats, borderline


# ═══════════════════════════════════════════════════════════════
#  临界带模型判定（廉价单问；失败回退规则结果）
# ═══════════════════════════════════════════════════════════════
DIFFICULTY_JUDGE_SYSTEM = (
    "判断完成此任务最少需要几步工具编排：\n"
    "纯查询 L0（一问一答一个数据点）、单段计算 L1（一段代码流内取数+加工）、\n"
    "跨数据域多阶段 L2（多个数据域、需分阶段编排）、\n"
    "长链条研究（含回测/多标的/长窗口）L3。\n"
    "只回复 L0/L1/L2/L3 四者之一，不要解释。"
)

_LEVEL_RE = re.compile(r"\b(L[0-3])\b", re.I)


def parse_level(text: str) -> str:
    """从模型输出里回收难度档位（L0~L3）；无命中返回 ""。"""
    m = _LEVEL_RE.search(str(text or ""))
    return m.group(1).upper() if m else ""


def difficulty_block() -> str:
    """临界带难度判定文本（拼进意图分类 prompt；零额外调用，裁决 #4）。

    替代了早期草稿的独立 judge_difficulty()（额外 LLM 调用）——裁决 #4 明确
    "路由结果进 intent 分类器输出契约一次带回"，档位由 parse_level() 从同一响应回收。
    """
    return DIFFICULTY_JUDGE_SYSTEM
