# -*- coding: utf-8 -*-
"""
返回结构契约速查表（Return-shape contracts）— 执行期提示词注入源。

背景（2026-09-17 根因修复）：
  agent 的自定义提示词模板 prompts/code_agent.yaml 整体覆盖了 smolagents 默认模板，
  默认模板里负责把工具签名/返回结构渲染进 system_prompt 的 {% for tool in tools %}
  {{ tool.to_code_prompt() }} 块被我们删掉了（业务工具注册在 ToolProvider，不在
  smolagents.tools，官方循环对我们只能看到 5 个内部工具）。planning 段的 {{tool_list}}
  也只渲染 name(params) — desc[:80]，**不含返回结构**。

  后果：模型在 CodeAgent 沙箱里对每个业务工具的真实返回形态（dict 顶层键、list 在哪个
  二级键、单/多代码返回结构差异）一无所知，只能靠"取数据→print→看类型→下一步再引用"
  的 REPL 式试探，6~11 步把 200K token 烧在类型探查上。这是"变严重了"的真正根因。

修复：把所有沙箱业务工具的【真实返回结构】集中登记在此，由 task_agent._sandbox_instructions
在构造执行 agent 时，针对本阶段点名的工具，把对应契约拼进 system_prompt（模型每一步都看得到）。
这样模型无需再 print 探查类型，可直接 `hot_sectors['industry']`、`hsr['stocks']` 一步取全。

契约写法规范（保持精炼，每行一个工具）：
  - 说明返回值是 dict 还是 list；
  - 指明**数据列表所在键**（最重要：stocks / sectors / data / concepts 等二级键）；
  - 标注单/多参数返回结构不一致的高危工具（如 get_realtime_quote）；
  - 标注取值前需 .get() 判空的键；
  - 不超过 ~120 字符/工具，避免把 token 压力从执行转移到提示词。
"""
from __future__ import annotations

TOOL_RETURN_CONTRACTS: dict[str, str] = {
    # ── 大盘/指数 ──
    "get_market_overview": (
        "dict: {up_count, down_count(两大指数抽样涨跌家数), emotion(0-100情绪), "
        "main_net_yi(主力净流亿元), main_pct}。直接 ov['emotion'] / ov['main_net_yi']。"
    ),
    "get_market_fund_flow": (
        "dict(全市场实时净流) 或 {error}。结构随底层接口，取前先 isinstance 判 error。"
    ),
    "get_hot_sectors": (
        "dict: {timestamp, industry:[{code,name,change_pct,limit_up_count,leading_stock}], "
        "concept:[同上], analysis:{}}。板块列表是 hs['industry']/['concept']（list），"
        "勿用 .get('data')；取名字 hs['industry'][0]['name']。"
    ),
    "get_industry_ranking": (
        "dict: {top:[{name,change_pct,leading_stock,...}], total}。行业列表在 top['top']（list）。"
    ),
    "get_sector_fund_flow": (
        "dict: {indicator, count, sectors:[{name,change_pct,main_net,lead_stock,...}]}。"
        "行业资金列表在 sf['sectors']（list）。"
    ),
    "get_concept_fund_flow": (
        "dict: {indicator, count, concepts:[{name,change_pct,main_net,lead_stock,...}]}。"
        "概念资金列表在 cf['concepts']（list，不是 'sectors'）。"
    ),
    "get_sector_prediction": (
        "dict：预测走强板块（结构随底层，含板块名/强度）。取前先判 error；无统一键名，先 print(keys) 一次。"
    ),
    # ── 个股行情/资金 ──
    "get_realtime_quote": (
        "⚠单/多代码结构不同：单代码→扁平行情 dict{stock_code,last,changePercent,...}；"
        "多代码→{count, data:{代码:行情dict}}，个股在 q['data'][code]。codes 必填非空。"
    ),
    "get_fund_flow": (
        "dict: {count, data:{代码:{主力净流入,散户净流入,趋势,...}}}。个股明细在 ff['data'][code]；"
        "某股失败其值为 {error}。codes 必填非空。"
    ),
    "get_capital_summary": (
        "多代码→{count, data:{代码:{summary:{margin,block_trade,holders,dividend,financials,overall_signal}}}}；"
        "overall_signal∈{中长线偏多/偏空/中性}。codes 必填。"
    ),
    # ── 热点/涨停/龙虎 ──
    "get_hot_stocks_with_reason": (
        "dict: {date, market_state, total, stocks:[{code,name,change_pct,reason}], hot_tags:[(题材,次数)]}。"
        "候选在 hsr['stocks']（list）；每只字段是 code/name（不是 stock_code/stock_name！）；"
        "market_state 可能为 closed_today；change_pct 盘前可能为 0。"
    ),
    "get_limit_pool": (
        "dict: {date, zt:{count,stocks:[...]}, dt:{...}, broken:{...}}（按 pool_type）。"
        "涨停股在 lp['zt']['stocks']（list）；无效类型或缺数据时对应键缺失，用 .get() 判空。"
    ),
    "get_dragon_tiger": (
        "codes 空→{date,count,stocks:[...]}（全市场）；codes 非空→{stock_code,count,records:[...]}。"
        "全市场列表在 dt['stocks']，个股记录在 dt['records']。"
    ),
    "get_hot_rank": (
        "dict: {count, stocks:[{code,name,rank,hot_score,...}]}。人气股在 hr['stocks']（list）。"
    ),
    # ── 选股/概念/情报 ──
    "search_stocks": (
        "dict: {source, keyword, total, count, stocks:[{code,name,industry,price,pe,pb,...}]}。"
        "结果在 ss['stocks'][i]['code']；query 与 filters 至少传一个，都空返回 error（勿传空 filters 当全部）。"
    ),
    "get_stock_concept_blocks": (
        "多代码→{count, data:{代码:{stock_code,total,boards:[{name,code,change_pct,lead_stock}],concept_tags:[...]}}}。"
        "概念在 cb['data'][code]['boards']。"
    ),
    "search_stock_intel": (
        "单代码→{items:[{title,time,summary,url,...}], summary}；多代码→{count, data:{代码:上述}}。"
        "新闻在 si['items']（单）或 si['data'][code]['items']（多）。"
    ),
    "resolve_stock": (
        "单只→{code,name,market}；多只→{count, data:[{code,name,market}]}。失败→{error}。"
    ),
    "technical_analysis": (
        "dict: {score(0-100), direction(bullish/bearish/neutral), confidence, signal, factors, analysis}。"
        "综合评分在 ta['score']，方向 ta['direction']；单股深度分析用，codes 为单只代码。"
    ),
}


def get_return_contract(name: str) -> str | None:
    """返回某个工具的真实返回结构契约；未登记返回 None。"""
    return TOOL_RETURN_CONTRACTS.get(name)


def build_return_contract_block(names: set[str]) -> str:
    """为本阶段点名的工具拼出返回结构速查段（仅含已登记的）。"""
    lines = [name for name in sorted(names) if name in TOOL_RETURN_CONTRACTS]
    if not lines:
        return ""
    out = [
        "【工具返回结构速查 — 取数后直接按键访问，勿再逐个 print 探查类型】",
    ]
    for name in lines:
        out.append(f"- {name}() -> {TOOL_RETURN_CONTRACTS[name]}")
    out.append(
        "（凡返回含 error 键或 market_state='closed_today' 的工具，先判空/判 error 再使用；"
        "列表型结果一律通过上面标注的二级键访问，不要对顶层 dict 直接切片/迭代）"
    )
    return "\n".join(out)
