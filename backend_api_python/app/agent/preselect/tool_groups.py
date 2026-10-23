# -*- coding: utf-8 -*-
"""工具组注册表 —— T7 group 单位的唯一事实源（2026-10-17）。

【为什么要有它】`tmp/t7_improvement_gaps_20261017.md` G3/G8：T7 的
`QD_TOOL_CONNECT` 声明表支持四类单位（tool/group/skill/capability），但
group 单位此前**没有注册表**——`tool_connect("finance")` 恒 resident，
组级开关是空话。且 O2 锤实的病根正是两套域口径：`_tool_domain` 返回粗粒度
（finance），`DATA_DOMAIN_TOOLS` 用细粒度（quote/kline…）⇒ 对不上。

【单一事实源纪律】本表的 data 组成员**从 `tool_preselect.DATA_DOMAIN_TOOLS`
派生**（不另维护名单——那正是 shrink_catalog 白写一年的漂移形态）；
组描述（检索面）与 `qd_tool_face._DEFERRED_DOMAIN_HINTS` 语义同向
（地图侧按 domain 查描述，组表侧按组名查；两侧文案若漂移由守卫测试发现）。

【三类成员】
  · data 域（15 个）：quote/kline/trend/… —— 来自 DATA_DOMAIN_TOOLS；
  · coding（8 个）：mimo 原生编码工具（task/bash/edit/write/read/glob/
    grep/compact），与 `qd_tool_face._DEFERRED_NATIVE_LABELS` 同源；
  · capability（动态）：注册表里 domain==capability_domain() 的工具。

【展开规则】`expand_overrides`：组级条目展开到成员；**单项条目优先于组**；
组间冲突（一个工具属两个组）**disabled 最严格胜出**（安全侧：任一组关停即关停）。
"""
from __future__ import annotations

from typing import Dict, List

from .tool_preselect import DATA_DOMAIN_TOOLS, capability_domain

# 组描述（检索面）：模型主要靠它决定"要不要这一组" ⇒ 写用途，不罗列工具名
# （口径对齐 OpenAI namespace 的 description）。域键与 DATA_DOMAIN_TOOLS 对齐。
GROUP_DESC: Dict[str, str] = {
    "quote": "实时行情/报价/指数点位",
    "kline": "K线/多周期/盘中结构（日线、分钟、VWAP、开盘区间）",
    "trend": "技术面趋势/形态/指标（MACD KDJ 均线）",
    "fundflow": "资金流/主力净流入/融资融券",
    "sector": "板块题材概念行业",
    "screen": "选股筛选/榜单排行",
    "valuation": "估值/市盈率市净率/财报业绩",
    "chip": "筹码分布/成本/集中度",
    "volume": "成交量能/换手/盘口",
    "market": "大盘概览/两市统计",
    "dragon": "龙虎榜席位",
    "strategy": "策略管理/回测/交易信号",
    "intel": "新闻公告研报舆情",
    "capital": "账户资金/持仓/市值盈亏",
    "knowledge": "本地知识库检索",
    "schedule": "定时任务/条件告警（到点执行、参数达阈值才提醒）",
    "coding": "读写改文件/跑 shell/派子任务（编码与调试）",
    "capability": "外部能力包（域内无覆盖时的兜底）",
    "native": "agent 原生工具",
}

# 编码组（mimo 原生，不在 DATA_DOMAIN_TOOLS 里）——与 qd_tool_face 的
# `_DEFERRED_NATIVE_LABELS` **键集同源**（那边是名字→标签文案，供系统提示
# 地图用；本常量是名字集，供组展开用）。漂移由 v17i 守卫测试锁死。
CODING_NATIVE_TOOLS = (
    "task", "bash", "edit", "write", "read", "glob", "grep", "compact",
)

# 状态严格度（组间冲突裁决）：disabled 最严格胜出
_STATE_RANK = {"resident": 0, "deferred": 1, "capability": 1, "disabled": 2}


def _domain_of(registry, name: str) -> str:
    try:
        return getattr(registry.get(name), "domain", "") or ""
    except Exception:
        return ""


def group_members(registry) -> Dict[str, List[str]]:
    """组 → 成员名（只收**已注册**的工具；data 静态 + coding 原生 + capability 动态）。

    未注册的域不进组表——不能禁用不存在的东西（`check_domain_dict`
    另守词典-注册表漂移，这里只做存在性过滤）。
    """
    registered = set(registry.list_tools())
    out: Dict[str, List[str]] = {}
    for dom, tools in DATA_DOMAIN_TOOLS.items():
        members = [t for t in tools if t in registered]
        if members:
            out[dom] = members
    coding = [n for n in CODING_NATIVE_TOOLS if n in registered]
    if coding:
        out["coding"] = coding
    cap_dom = capability_domain()
    caps = [n for n in registry.list_tools() if _domain_of(registry, n) == cap_dom]
    if caps:
        out["capability"] = caps
    return out


def expand_overrides(overrides: Dict[str, str],
                     members: Dict[str, List[str]]) -> Dict[str, str]:
    """把组级条目展开到成员名（单项优先；组间冲突 disabled 最严格胜出）。"""
    out: Dict[str, str] = {}
    for group, names in members.items():
        state = overrides.get(group)
        if not state:
            continue
        for n in names:
            if _STATE_RANK.get(state, 0) > _STATE_RANK.get(out.get(n, "resident"), 0):
                out[n] = state
    for name, state in overrides.items():
        if name not in members:          # 不是组名 ⇒ 单项条目，无条件优先
            out[name] = state
    return out
