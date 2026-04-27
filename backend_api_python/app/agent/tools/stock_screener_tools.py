# -*- coding: utf-8 -*-
"""
Stock Screener tools — 选股器 Agent 工具（完整移植版）

将前端 Vue 选股器的全部筛选逻辑移植为 Python：
- getDefaultFilters() → 空筛选条件默认值（130+ 字段）
- build_keyword_from_filters() ← updateAiQuery()（结构化条件 → 自然语言）
- parse_filters_from_text() ← parseFilterFromText()（自然语言 → 结构化条件）
- screen_stocks() — 调用东方财富 search-code API
- SLIDER_CONFIGS — 滑块配置常量
- INDUSTRY_OPTIONS / CONCEPT_OPTIONS — 行业/概念选项

数据源：东方财富 search-code API（与前端共用同一数据源）
"""
from __future__ import annotations

import json
import logging
import random
import re
import string
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
#  东方财富智能选股 API
# ══════════════════════════════════════════════════════════════

_EASTMONEY_SEARCH_URL = "https://np-tjxg-b.eastmoney.com/api/smart-tag/stock/v3/pw/search-code"

MARKET_FILTER_MAP = {
    "全部": "全部", "A股": "A股", "沪深300": "沪深300",
    "中证500": "中证500", "科创板": "科创板", "创业板": "创业板",
    "港股": "港股", "美股": "美股", "ETF基金": "ETF基金",
}

# ══════════════════════════════════════════════════════════════
#  行业 / 概念选项（与前端 FilterPanel.vue 完全一致）
# ══════════════════════════════════════════════════════════════

INDUSTRY_OPTIONS = [
    "新能源", "人工智能", "半导体", "医药生物", "食品饮料", "金融",
    "房地产", "交通运输", "公用事业", "钢铁", "有色金属", "化工",
    "建筑材料", "电子", "电气设备", "机械设备", "汽车", "纺织服装",
    "轻工制造", "商业贸易", "休闲服务", "传媒", "计算机", "通信",
    "农林牧渔", "国防军工", "建筑装饰",
]

CONCEPT_OPTIONS = [
    "国企改革", "一带一路", "碳中和", "新能源车", "光伏", "储能",
    "元宇宙", "芯片", "5G", "云计算", "大数据", "区块链",
]

# ══════════════════════════════════════════════════════════════
#  滑块配置常量（与前端 SLIDER_CONFIGS 一致）
# ══════════════════════════════════════════════════════════════

SLIDER_CONFIGS = {
    "pe": {"min": -100, "max": 500, "step": 1},
    "pb": {"min": -50, "max": 100, "step": 0.1},
    "mi_volume_ratio": {"min": 0, "max": 100, "step": 0.1},
    "mi_turnover_rate": {"min": 0, "max": 100, "step": 0.1},
    "mi_amplitude": {"min": 0, "max": 50, "step": 0.1},
    "mi_volume": {"min": 0, "max": 10000000, "step": 10000},
    "mi_amount": {"min": 0, "max": 500000000000, "step": 100000000},
    "mi_pe": {"min": -200, "max": 500, "step": 1},
    "mi_float_mc": {"min": 0, "max": 20000, "step": 10},
    "mi_total_mc": {"min": 0, "max": 50000, "step": 10},
    "mi_comp_ratio": {"min": -100, "max": 100, "step": 1},
    "mi_today_up": {"min": -10, "max": 10, "step": 0.1},
    "mi_change_5d": {"min": -50, "max": 50, "step": 0.1},
    "mi_change_10d": {"min": -50, "max": 50, "step": 0.1},
    "mi_change_60d": {"min": -100, "max": 100, "step": 0.1},
    "mi_change_ytd": {"min": -200, "max": 500, "step": 0.1},
    "mi_close": {"min": 0, "max": 2000, "step": 0.01},
    "mi_net_in": {"min": -10000000000, "max": 10000000000, "step": 100000000},
    "ch_cost_price": {"min": 0, "max": 2000, "step": 0.01},
    "ch_profit_ratio": {"min": 0, "max": 100, "step": 0.1},
    "ch_avg_cost": {"min": 0, "max": 2000, "step": 0.01},
    "ch_conc_90": {"min": 0, "max": 100, "step": 0.1},
    "ch_conc_70": {"min": 0, "max": 100, "step": 0.1},
    "ch_holder_count": {"min": 0, "max": 2000000, "step": 10000},
    "tiger_buy": {"min": 0, "max": 100000000000, "step": 100000000},
    "tiger_sell": {"min": 0, "max": 100000000000, "step": 100000000},
    "tiger_net": {"min": -50000000000, "max": 50000000000, "step": 100000000},
    "tiger_dept_buy": {"min": 0, "max": 50000000000, "step": 100000000},
    "tiger_inst_buy": {"min": 0, "max": 50000000000, "step": 100000000},
    "ti_ma5": {"min": 0, "max": 2000, "step": 0.01},
    "ti_ma10": {"min": 0, "max": 2000, "step": 0.01},
    "ti_ma20": {"min": 0, "max": 2000, "step": 0.01},
    "ti_ma60": {"min": 0, "max": 2000, "step": 0.01},
    "ti_ma120": {"min": 0, "max": 2000, "step": 0.01},
}

# ══════════════════════════════════════════════════════════════
#  空筛选条件默认值（与前端 getDefaultFilters() 完全一致）
# ══════════════════════════════════════════════════════════════

def get_default_filters() -> Dict[str, Any]:
    """
    返回空筛选条件的默认值字典（130+ 字段）。
    与前端 FilterPanel.vue 的 getDefaultFilters() 完全一致。
    """
    return {
        # ── 基本面：估值 ──
        "pe_min": None, "pe_max": None,
        "pb_min": None, "pb_max": None,
        "dividend_min": None,
        # ── 基本面：成长 ──
        "growth_indicators": [],      # netprofit_yoy_ratio, toi_yoy_ratio, basiceps_yoy_ratio, income_growthrate_3y, netprofit_growthrate_3y
        "quality_indicators": [],     # per_netcash_operate
        # ── 基本面：盈利 ──
        "roe_min": None,
        "sale_gpr_min": None,
        "sale_npr_min": None,         # 前端滑块（筛选面板用）
        "sale_npr_min_filter": None,  # 文本解析用
        # ── 技术面：均线突破 ──
        "ma_breakthrough": [],        # breakup_ma_5days/10days/20days/60days, long_avg_array
        "ma_30_break": [],            # breakup_ma_30days
        # ── 技术面：技术指标 ──
        "tech_signals": [],           # macd_golden_fork, kdj_golden_fork, break_through, upper_large_volume, down_narrow_volume
        "kdj_signals": [],            # kdj_golden_forkz, kdj_golden_forky, macd_golden_forkz, macd_golden_forky
        # ── 技术面：K线形态 ──
        "k_classic": [],              # one_dayang_line, two_dayang_lines, rise_sun, morning_star, ...
        "k_intraday": [],             # tail_plate_rise, intraday_pressure, intraday_rise, quick_rebound
        "k_other": [],                # limit_up, limit_down
        "pattern_signals": [],        # power_fulgun, pregnant, black_cloud_tops, ...
        # ── 技术面：其他 ──
        "volume_trend": [],           # short_avg_array, restore_justice
        "consecutive_signals": [],    # down_7days, upper_8days, upper_9days, upper_4days
        # ── 资金面 ──
        "capital_flow": [],           # low_funds_inflow, high_funds_outflow, netinflow_3days, netinflow_5days
        "volume_ratio_min": None,
        "turnoverrate_min": None,
        "institutional_holding": [],  # org_survey_3m, allcorp_fund_ratio, allcorp_qs_ratio
        # ── 资金面：数值 ──
        "net_inflow_min": None,
        "ddx_min": None,
        "netinflow_min_3d": None,
        "netinflow_min_5d": None,
        "changerate_3d_min": None,
        "changerate_5d_min": None,
        "changerate_10d_min": None,
        "changerate_ty_min": None,
        "changerate_ty_max": None,
        # ── 概念/行业 ──
        "industry": [],
        "concept": [],
        # ── 行情指标 ──
        "mi_volume_ratio_min": None, "mi_volume_ratio_max": None,
        "mi_turnover_rate_min": None, "mi_turnover_rate_max": None,
        "mi_amplitude_min": None, "mi_amplitude_max": None,
        "mi_volume_min": None, "mi_volume_max": None,
        "mi_amount_min": None, "mi_amount_max": None,
        "mi_pe_min": None, "mi_pe_max": None,
        "mi_float_mc_min": None, "mi_float_mc_max": None,
        "mi_total_mc_min": None, "mi_total_mc_max": None,
        "mi_comp_ratio_min": None, "mi_comp_ratio_max": None,
        "mi_today_up_min": None, "mi_today_up_max": None,
        "mi_change_5d_min": None, "mi_change_5d_max": None,
        "mi_change_10d_min": None, "mi_change_10d_max": None,
        "mi_change_60d_min": None, "mi_change_60d_max": None,
        "mi_change_ytd_min": None, "mi_change_ytd_max": None,
        "mi_close_min": None, "mi_close_max": None,
        "mi_net_in_min": None, "mi_net_in_max": None,
        # ── 新增基本面 ──
        "ps_min": None, "ps_max": None,
        "pcf_min": None, "pcf_max": None,
        "dtsyl_min": None, "dtsyl_max": None,
        "total_market_cap_min": None, "total_market_cap_max": None,
        "free_cap_min": None, "free_cap_max": None,
        "basic_eps_min": None,
        "bvps_min": None,
        "per_fcfe_min": None,
        "parent_netprofit_min": None,
        "deduct_netprofit_min": None,
        "total_operate_income_min": None,
        "jroa_min": None,
        "roic_min": None,
        "debt_asset_ratio_min": None, "debt_asset_ratio_max": None,
        "current_ratio_min": None,
        "speed_ratio_min": None,
        "total_shares_min": None, "total_shares_max": None,
        "free_shares_min": None, "free_shares_max": None,
        "holder_newest_min": None, "holder_newest_max": None,
        # ── 机构/股东 ──
        "holder_change_3m_min": None,
        "executive_change_3m_min": None,
        "org_rating_filter": "",
        "allcorp_ratio_min": None,
        "allcorp_fund_ratio_min": None,
        "allcorp_qs_ratio_min": None,
        "allcorp_qfii_ratio_min": None,
        # ── 新高新低 ──
        "new_high_filter": [],        # now_newhigh, now_newlow, high_recent_3days~30days, low_recent_3days~30days
        "win_market_filter": [],      # win_market_3days~30days
        "hs_board_filter": [],        # is_sz50, is_zz1000, is_cy50, is_bps_break, is_issue_break
        # ── 派息/质押/商誉 ──
        "par_dividend_min": None,
        "pledge_ratio_max": None,
        "goodwill_max": None,
        # ── 限价/定增/质押 ──
        "limited_lift_filter": [],    # limited_lift_6m, limited_lift_1y
        "directional_seo_filter": [], # directional_seo_1m/3m/6m/1y
        "equity_pledge_filter": [],   # equity_pledge_1m/3m/6m/1y
        # ── 筹码指标 ──
        "ch_cost_price_min": None, "ch_cost_price_max": None,
        "ch_profit_ratio_min": None, "ch_profit_ratio_max": None,
        "ch_avg_cost_min": None, "ch_avg_cost_max": None,
        "ch_conc_90_min": None, "ch_conc_90_max": None,
        "ch_conc_70_min": None, "ch_conc_70_max": None,
        "ch_holder_count_min": None, "ch_holder_count_max": None,
        # ── 龙虎榜 ──
        "tiger_date_min": None, "tiger_date_max": None,
        "tiger_buy_min": None, "tiger_buy_max": None,
        "tiger_sell_min": None, "tiger_sell_max": None,
        "tiger_net_min": None, "tiger_net_max": None,
        "tiger_dept_buy_min": None, "tiger_dept_buy_max": None,
        "tiger_inst_buy_min": None, "tiger_inst_buy_max": None,
        "tiger_participant": [],      # inst_participated, dept_participated
        # ── 技术指标（均线价格） ──
        "ti_ma5_min": None, "ti_ma5_max": None,
        "ti_ma10_min": None, "ti_ma10_max": None,
        "ti_ma20_min": None, "ti_ma20_max": None,
        "ti_ma60_min": None, "ti_ma60_max": None,
        "ti_ma120_min": None, "ti_ma120_max": None,
        # ── 滑块范围（前端 UI 绑定用，后端可忽略） ──
        "pe_range": [0, 100],
        "pb_range": [0, 20],
        "mi_amplitude_range": [0, 50],
        "mi_pe_range": [-200, 500],
        "mi_float_mc_range": [0, 20000],
        "mi_total_mc_range": [0, 50000],
        "mi_comp_ratio_range": [-100, 100],
        "mi_today_up_range": [-10, 10],
        "mi_change_5d_range": [-50, 50],
        "mi_change_10d_range": [-50, 50],
        "mi_change_60d_range": [-100, 100],
        "mi_change_ytd_range": [-200, 500],
        "mi_close_range": [0, 2000],
        "mi_net_in_range": [-10000000000, 10000000000],
        "ch_cost_price_range": [0, 2000],
        "ch_profit_ratio_range": [0, 100],
        "ch_avg_cost_range": [0, 2000],
        "ch_conc_90_range": [0, 100],
        "ch_conc_70_range": [0, 100],
        "ch_holder_count_range": [0, 2000000],
        "tiger_buy_range": [0, 100000000000],
        "tiger_sell_range": [0, 100000000000],
        "tiger_net_range": [-50000000000, 50000000000],
        "tiger_dept_buy_range": [0, 50000000000],
        "tiger_inst_buy_range": [0, 50000000000],
        "ti_ma5_range": [0, 2000],
        "ti_ma10_range": [0, 2000],
        "ti_ma20_range": [0, 2000],
        "ti_ma60_range": [0, 2000],
        "ti_ma120_range": [0, 2000],
    }


# ══════════════════════════════════════════════════════════════
#  结构化条件 → 自然语言关键词（移植自 updateAiQuery）
# ══════════════════════════════════════════════════════════════

def build_keyword_from_filters(filters: Dict[str, Any]) -> str:
    """
    将结构化筛选条件转换为自然语言关键词字符串。
    移植自前端 index.vue 的 updateAiQuery() 方法。
    """
    parts: List[str] = []

    # ── 基本面：估值 ──
    pe_min = filters.get("pe_min")
    pe_max = filters.get("pe_max")
    if pe_min is not None or pe_max is not None:
        parts.append(f"PE在{pe_min or 0}到{pe_max or '∞'}之间")

    pb_min = filters.get("pb_min")
    pb_max = filters.get("pb_max")
    if pb_min is not None or pb_max is not None:
        parts.append(f"PB在{pb_min or 0}到{pb_max or '∞'}之间")

    if filters.get("dividend_min") is not None and filters["dividend_min"] > 0:
        parts.append(f"股息率不低于{filters['dividend_min']}%")

    if filters.get("roe_min") is not None and filters["roe_min"] > -50:
        parts.append(f"ROE不低于{filters['roe_min']}%")

    if filters.get("sale_gpr_min") is not None and filters["sale_gpr_min"] > -50:
        parts.append(f"毛利率不低于{filters['sale_gpr_min']}%")

    # ── 基本面：成长 checkbox ──
    growth_map = {
        "netprofit_yoy_ratio": "净利增长>15%",
        "toi_yoy_ratio": "营收增长>15%",
        "basiceps_yoy_ratio": "每股收益增长>10%",
        "income_growthrate_3y": "营收3年复合增长 > 10%",
        "netprofit_growthrate_3y": "净利润3年复合增长 > 10%",
    }
    for k in filters.get("growth_indicators", []):
        if k in growth_map:
            parts.append(growth_map[k])

    # ── 基本面：质量 checkbox ──
    for k in filters.get("quality_indicators", []):
        if k == "per_netcash_operate":
            parts.append("经营现金流为正")

    # ── 技术面：均线突破 ──
    ma_map = {
        "breakup_ma_5days": "突破5日线",
        "breakup_ma_10days": "突破10日线",
        "breakup_ma_20days": "突破20日线",
        "breakup_ma_60days": "突破60日线",
        "long_avg_array": "长期均线多头排列",
    }
    for k in filters.get("ma_breakthrough", []):
        if k in ma_map:
            parts.append(ma_map[k])

    # ── 技术面：技术指标 ──
    tech_map = {
        "macd_golden_fork": "MACD金叉",
        "kdj_golden_fork": "KDJ金叉",
        "break_through": "突破形态",
        "upper_large_volume": "放量上涨",
        "down_narrow_volume": "缩量下跌",
    }
    for k in filters.get("tech_signals", []):
        if k in tech_map:
            parts.append(tech_map[k])

    # ── 技术面：经典K线形态 ──
    k_classic_map = {
        "one_dayang_line": "大阳线", "two_dayang_lines": "两阳夹一阴",
        "rise_sun": "阳包阴", "morning_star": "早晨之星",
        "evening_star": "黄昏之星", "shooting_star": "射击之星",
        "three_black_crows": "三只乌鸦", "hammer": "锤头",
        "inverted_hammer": "倒锤头", "doji": "十字星",
        "long_legged_doji": "长腿十字线", "gravestone": "墓碑线",
        "dragonfly": "蜻蜓线", "two_flying_crows": "双飞乌鸦",
        "lotus_emerge": "出水芙蓉", "low_open_high": "低开高走",
        "huge_volume": "巨量",
        "bottom_cross_harami": "底部十字孕线",
        "top_cross_harami": "顶部十字孕线",
    }
    for k in filters.get("k_classic", []):
        if k in k_classic_map:
            parts.append(k_classic_map[k])

    # ── 技术面：分时K线形态 ──
    k_intraday_map = {
        "tail_plate_rise": "尾盘拉升", "intraday_pressure": "盘中打压",
        "intraday_rise": "盘中拉升", "quick_rebound": "快速反弹",
    }
    for k in filters.get("k_intraday", []):
        if k in k_intraday_map:
            parts.append(k_intraday_map[k])

    # ── 技术面：其它形态 ──
    for k in filters.get("k_other", []):
        if k == "limit_up":
            parts.append("一字涨停")
        if k == "limit_down":
            parts.append("一字跌停")

    # ── 资金面：资金流向 ──
    flow_map = {
        "low_funds_inflow": "主力资金净流入",
        "high_funds_outflow": "主力资金净流出",
        "netinflow_3days": "近3日资金净流入",
        "netinflow_5days": "近5日资金净流入",
    }
    for k in filters.get("capital_flow", []):
        if k in flow_map:
            parts.append(flow_map[k])

    if filters.get("volume_ratio_min") is not None and filters["volume_ratio_min"] > 0:
        parts.append(f"量比不低于{filters['volume_ratio_min']}")
    if filters.get("turnoverrate_min") is not None and filters["turnoverrate_min"] > 0:
        parts.append(f"换手率不低于{filters['turnoverrate_min']}%")

    # ── 资金面：机构持股 ──
    inst_map = {
        "org_survey_3m": "近3月有机构调研",
        "allcorp_fund_ratio": "基金重仓",
        "allcorp_qs_ratio": "券商重仓",
    }
    for k in filters.get("institutional_holding", []):
        if k in inst_map:
            parts.append(inst_map[k])

    # ── 行情指标 ──
    if filters.get("mi_volume_ratio_min") is not None and filters["mi_volume_ratio_min"] > 0:
        parts.append(f"量比≥{filters['mi_volume_ratio_min']}")
    if filters.get("mi_turnover_rate_min") is not None and filters["mi_turnover_rate_min"] > 0:
        parts.append(f"换手率≥{filters['mi_turnover_rate_min']}%")
    if filters.get("mi_volume_min") is not None and filters["mi_volume_min"] > 0:
        parts.append(f"成交量≥{filters['mi_volume_min']}手")
    if filters.get("mi_amount_min") is not None and filters["mi_amount_min"] > 0:
        parts.append(f"成交额≥{filters['mi_amount_min']}元")

    # ── 龙虎榜参与方 ──
    for k in filters.get("tiger_participant", []):
        if k == "inst_participated":
            parts.append("机构参与")
        if k == "dept_participated":
            parts.append("营业部参与")

    # ── 概念/行业 ──
    industries = filters.get("industry", [])
    if industries:
        parts.append(f"属于行业({', '.join(industries)})")
    concepts = filters.get("concept", [])
    if concepts:
        parts.append(f"涉及概念({', '.join(concepts)})")

    # ── 新增基本面 ──
    ps_min, ps_max = filters.get("ps_min"), filters.get("ps_max")
    if ps_min is not None or ps_max is not None:
        parts.append(f"市销率{ps_min or 0}~{ps_max or '∞'}")
    pcf_min, pcf_max = filters.get("pcf_min"), filters.get("pcf_max")
    if pcf_min is not None or pcf_max is not None:
        parts.append(f"市现率{pcf_min or 0}~{pcf_max or '∞'}")
    dtsyl_min, dtsyl_max = filters.get("dtsyl_min"), filters.get("dtsyl_max")
    if dtsyl_min is not None or dtsyl_max is not None:
        parts.append(f"动态PE{dtsyl_min or 0}~{dtsyl_max or '∞'}")
    tmc_min, tmc_max = filters.get("total_market_cap_min"), filters.get("total_market_cap_max")
    if tmc_min is not None or tmc_max is not None:
        parts.append(f"总市值{tmc_min or 0}~{tmc_max or '∞'}")
    fmc_min, fmc_max = filters.get("free_cap_min"), filters.get("free_cap_max")
    if fmc_min is not None or fmc_max is not None:
        parts.append(f"流通市值{fmc_min or 0}~{fmc_max or '∞'}")
    if filters.get("basic_eps_min") is not None:
        parts.append(f"每股收益≥{filters['basic_eps_min']}")
    if filters.get("bvps_min") is not None:
        parts.append(f"每股净资产≥{filters['bvps_min']}")
    if filters.get("per_fcfe_min") is not None:
        parts.append(f"每股自由现金流≥{filters['per_fcfe_min']}")
    if filters.get("parent_netprofit_min") is not None:
        parts.append(f"归母净利润≥{filters['parent_netprofit_min']}")
    if filters.get("deduct_netprofit_min") is not None:
        parts.append(f"扣非净利润≥{filters['deduct_netprofit_min']}")
    if filters.get("total_operate_income_min") is not None:
        parts.append(f"营业收入≥{filters['total_operate_income_min']}")
    if filters.get("jroa_min") is not None:
        parts.append(f"总资产报酬率≥{filters['jroa_min']}%")
    if filters.get("roic_min") is not None:
        parts.append(f"投资回报率≥{filters['roic_min']}%")
    if filters.get("sale_npr_min_filter") is not None:
        parts.append(f"销售净利率≥{filters['sale_npr_min_filter']}%")
    da_min, da_max = filters.get("debt_asset_ratio_min"), filters.get("debt_asset_ratio_max")
    if da_min is not None or da_max is not None:
        if da_min is not None and da_max is not None:
            parts.append(f"资产负债率{da_min}~{da_max}%")
        elif da_max is not None:
            parts.append(f"资产负债率≤{da_max}%")
    if filters.get("current_ratio_min") is not None:
        parts.append(f"流动比率≥{filters['current_ratio_min']}")
    if filters.get("speed_ratio_min") is not None:
        parts.append(f"速动比率≥{filters['speed_ratio_min']}")
    ts_min, ts_max = filters.get("total_shares_min"), filters.get("total_shares_max")
    if ts_min is not None or ts_max is not None:
        parts.append(f"总股本{ts_min or 0}~{ts_max or '∞'}")
    fs_min, fs_max = filters.get("free_shares_min"), filters.get("free_shares_max")
    if fs_min is not None or fs_max is not None:
        parts.append(f"流通股本{fs_min or 0}~{fs_max or '∞'}")
    hn_min, hn_max = filters.get("holder_newest_min"), filters.get("holder_newest_max")
    if hn_min is not None or hn_max is not None:
        parts.append(f"股东数{hn_min or 0}~{hn_max or '∞'}")

    # ── 技术指标补充 ──
    for k in filters.get("ma_30_break", []):
        if k == "breakup_ma_30days":
            parts.append("突破30日线")

    kdj_map = {
        "kdj_golden_forkz": "KDJ金叉Z", "kdj_golden_forky": "KDJ金叉Y",
        "macd_golden_forkz": "MACD金叉Z", "macd_golden_forky": "MACD金叉Y",
    }
    for k in filters.get("kdj_signals", []):
        if k in kdj_map:
            parts.append(kdj_map[k])

    pattern_map = {
        "power_fulgun": "乌云盖顶", "pregnant": "孕线",
        "black_cloud_tops": "黑云压顶", "narrow_finish": "窄幅整理",
        "reversing_hammer": "反转锤子", "first_dawn": "第一天黎明",
        "bearish_engulfing": "看跌吞没", "upside_volume": "上攻放量",
        "heaven_rule": "天道法则",
    }
    for k in filters.get("pattern_signals", []):
        if k in pattern_map:
            parts.append(pattern_map[k])

    consec_map = {
        "down_7days": "连续7天下跌", "upper_8days": "连续8天上涨",
        "upper_9days": "连续9天上涨", "upper_4days": "连续4天上涨",
    }
    for k in filters.get("consecutive_signals", []):
        if k in consec_map:
            parts.append(consec_map[k])

    for k in filters.get("volume_trend", []):
        if k == "short_avg_array":
            parts.append("短期均线多头")
        if k == "restore_justice":
            parts.append("复权")

    # ── 资金面数值 ──
    if filters.get("net_inflow_min") is not None:
        parts.append(f"净流入≥{filters['net_inflow_min']}")
    if filters.get("ddx_min") is not None:
        parts.append(f"大单动向≥{filters['ddx_min']}")
    if filters.get("netinflow_min_3d") is not None:
        parts.append(f"3日净流入≥{filters['netinflow_min_3d']}")
    if filters.get("netinflow_min_5d") is not None:
        parts.append(f"5日净流入≥{filters['netinflow_min_5d']}")
    if filters.get("changerate_3d_min") is not None:
        parts.append(f"3日涨幅≥{filters['changerate_3d_min']}%")
    if filters.get("changerate_5d_min") is not None:
        parts.append(f"5日涨幅≥{filters['changerate_5d_min']}%")
    if filters.get("changerate_10d_min") is not None:
        parts.append(f"10日涨幅≥{filters['changerate_10d_min']}%")
    cty_min, cty_max = filters.get("changerate_ty_min"), filters.get("changerate_ty_max")
    if cty_min is not None or cty_max is not None:
        parts.append(f"年度涨幅{cty_min if cty_min is not None else '-∞'}~{cty_max if cty_max is not None else '∞'}%")

    # ── 机构/股东 ──
    if filters.get("holder_change_3m_min") is not None:
        parts.append(f"3月持股变动≥{filters['holder_change_3m_min']}%")
    if filters.get("executive_change_3m_min") is not None:
        parts.append(f"3月高管持股变动≥{filters['executive_change_3m_min']}%")
    if filters.get("org_rating_filter"):
        parts.append(f"机构评级={filters['org_rating_filter']}")
    if filters.get("allcorp_ratio_min") is not None:
        parts.append(f"机构持股比例≥{filters['allcorp_ratio_min']}%")
    if filters.get("allcorp_fund_ratio_min") is not None:
        parts.append(f"基金持股≥{filters['allcorp_fund_ratio_min']}%")
    if filters.get("allcorp_qs_ratio_min") is not None:
        parts.append(f"券商持股≥{filters['allcorp_qs_ratio_min']}%")
    if filters.get("allcorp_qfii_ratio_min") is not None:
        parts.append(f"QFII持股≥{filters['allcorp_qfii_ratio_min']}%")

    # ── 新高新低 ──
    hl_map = {
        "now_newhigh": "当前新高", "now_newlow": "当前新低",
        "high_recent_3days": "3天新高", "high_recent_5days": "5天新高",
        "high_recent_10days": "10天新高", "high_recent_20days": "20天新高",
        "high_recent_30days": "30天新高",
        "low_recent_3days": "3天新低", "low_recent_5days": "5天新低",
        "low_recent_10days": "10天新低", "low_recent_20days": "20天新低",
        "low_recent_30days": "30天新低",
    }
    for k in filters.get("new_high_filter", []):
        if k in hl_map:
            parts.append(hl_map[k])

    # ── 战胜大盘 ──
    for k in filters.get("win_market_filter", []):
        m = re.match(r"win_market_(\d+)days", k)
        if m:
            parts.append(f"{m.group(1)}天战胜大盘")

    # ── 板块标识 ──
    board_map = {
        "is_sz50": "上证50成分股", "is_zz1000": "中证1000成分股",
        "is_cy50": "创业板50成分股", "is_bps_break": "已破净",
        "is_issue_break": "已破板",
    }
    for k in filters.get("hs_board_filter", []):
        if k in board_map:
            parts.append(board_map[k])

    # ── 派息/质押/商誉 ──
    if filters.get("par_dividend_min") is not None:
        parts.append(f"派息率≥{filters['par_dividend_min']}%")
    if filters.get("pledge_ratio_max") is not None:
        parts.append(f"质押比例≤{filters['pledge_ratio_max']}%")
    if filters.get("goodwill_max") is not None:
        parts.append(f"商誉≤{filters['goodwill_max']}")

    # ── 限价/定增/质押 ──
    for k in filters.get("limited_lift_filter", []):
        if k == "limited_lift_6m":
            parts.append("限价上涨6月")
        if k == "limited_lift_1y":
            parts.append("限价上涨1年")
    seo_map = {
        "directional_seo_1m": "定向增发1月", "directional_seo_3m": "定向增发3月",
        "directional_seo_6m": "定向增发6月", "directional_seo_1y": "定向增发1年",
    }
    for k in filters.get("directional_seo_filter", []):
        if k in seo_map:
            parts.append(seo_map[k])
    pledge_map = {
        "equity_pledge_1m": "股权质押1月", "equity_pledge_3m": "股权质押3月",
        "equity_pledge_6m": "股权质押6月", "equity_pledge_1y": "股权质押1年",
    }
    for k in filters.get("equity_pledge_filter", []):
        if k in pledge_map:
            parts.append(pledge_map[k])

    return "; ".join(parts)


# ══════════════════════════════════════════════════════════════
#  自然语言 → 结构化条件（移植自 parseFilterFromText）
# ══════════════════════════════════════════════════════════════

def _parse_number(s: str) -> Optional[float]:
    """解析数字字符串，支持中文单位（亿、万、千亿等）。"""
    if not s:
        return None
    s = s.replace(",", "").strip()
    if s in ("∞", "-∞"):
        return None
    try:
        if s.endswith("千亿"):
            return float(s[:-3]) * 100000000000
        if s.endswith("亿"):
            return float(s[:-1]) * 100000000
        if s.endswith("万"):
            return float(s[:-1]) * 10000
        if s.endswith("手"):
            return float(s[:-1])
        if s.endswith("元"):
            return float(s[:-1])
        if s.endswith("%"):
            return float(s[:-1])
        return float(s)
    except ValueError:
        return None


def _parse_range(s: str) -> Tuple[Optional[float], Optional[float]]:
    """解析 'X~Y' 范围字符串。"""
    parts = s.split("~")
    lo = _parse_number(parts[0]) if len(parts) > 0 else None
    hi = _parse_number(parts[1]) if len(parts) > 1 else None
    return lo, hi


def parse_filters_from_text(text: str) -> Dict[str, Any]:
    """
    将自然语言文本解析为结构化筛选条件。
    移植自前端 index.vue 的 parseFilterFromText() 方法。
    """
    filters = get_default_filters()
    if not text or not text.strip():
        return filters

    parts = [s.strip() for s in re.split(r"[;；]", text) if s.strip()]

    for part in parts:
        m = None  # reusable match variable

        # ── 市场选择 ──
        if part in MARKET_FILTER_MAP:
            filters["_market"] = part
            continue

        # ── 范围: PE/PB在X到Y之间 ──
        m = re.match(r"PE在(.+?)到(.+?)之间", part)
        if m:
            filters["pe_min"] = _parse_number(m.group(1))
            filters["pe_max"] = _parse_number(m.group(2))
            continue

        m = re.match(r"PB在(.+?)到(.+?)之间", part)
        if m:
            filters["pb_min"] = _parse_number(m.group(1))
            filters["pb_max"] = _parse_number(m.group(2))
            continue

        # ── 不低于 ──
        m = re.match(r"股息率不低于(.+?)%", part)
        if m:
            filters["dividend_min"] = float(m.group(1))
            continue
        m = re.match(r"ROE不低于(.+?)%", part)
        if m:
            filters["roe_min"] = float(m.group(1))
            continue
        m = re.match(r"毛利率不低于(.+?)%", part)
        if m:
            filters["sale_gpr_min"] = float(m.group(1))
            continue
        m = re.match(r"量比不低于(.+)", part)
        if m:
            filters["volume_ratio_min"] = _parse_number(m.group(1))
            continue
        m = re.match(r"换手率不低于(.+?)%", part)
        if m:
            filters["turnoverrate_min"] = float(m.group(1))
            continue

        # ── 成长/质量 checkbox ──
        if part == "净利增长>15%":
            filters["growth_indicators"].append("netprofit_yoy_ratio"); continue
        if part == "营收增长>15%":
            filters["growth_indicators"].append("toi_yoy_ratio"); continue
        if part == "每股收益增长>10%":
            filters["growth_indicators"].append("basiceps_yoy_ratio"); continue
        if part == "经营现金流为正":
            filters["quality_indicators"].append("per_netcash_operate"); continue

        # ── 均线突破 ──
        ma_text_map = {
            "突破5日线": "breakup_ma_5days", "突破10日线": "breakup_ma_10days",
            "突破20日线": "breakup_ma_20days", "突破60日线": "breakup_ma_60days",
            "长期均线多头排列": "long_avg_array",
        }
        if part in ma_text_map:
            filters["ma_breakthrough"].append(ma_text_map[part]); continue

        # ── 技术指标 ──
        tech_text_map = {
            "MACD金叉": "macd_golden_fork", "KDJ金叉": "kdj_golden_fork",
            "放量上涨": "upper_large_volume", "缩量下跌": "down_narrow_volume",
            "突破形态": "break_through",
        }
        if part in tech_text_map:
            filters["tech_signals"].append(tech_text_map[part]); continue

        # ── 经典K线形态 ──
        k_classic_text = {
            "大阳线": "one_dayang_line", "两阳夹一阴": "two_dayang_lines",
            "阳包阴": "rise_sun", "早晨之星": "morning_star",
            "黄昏之星": "evening_star", "射击之星": "shooting_star",
            "三只乌鸦": "three_black_crows", "锤头": "hammer",
            "倒锤头": "inverted_hammer", "十字星": "doji",
            "长腿十字线": "long_legged_doji", "墓碑线": "gravestone",
            "蜻蜓线": "dragonfly", "双飞乌鸦": "two_flying_crows",
            "出水芙蓉": "lotus_emerge", "低开高走": "low_open_high",
            "巨量": "huge_volume",
            "底部十字孕线": "bottom_cross_harami",
            "顶部十字孕线": "top_cross_harami",
        }
        if part in k_classic_text:
            filters["k_classic"].append(k_classic_text[part]); continue

        # ── 分时K线形态 ──
        k_intraday_text = {
            "尾盘拉升": "tail_plate_rise", "盘中打压": "intraday_pressure",
            "盘中拉升": "intraday_rise", "快速反弹": "quick_rebound",
        }
        if part in k_intraday_text:
            filters["k_intraday"].append(k_intraday_text[part]); continue

        # ── 其它形态 ──
        if part == "一字涨停":
            filters["k_other"].append("limit_up"); continue
        if part == "一字跌停":
            filters["k_other"].append("limit_down"); continue

        # ── 资金面 ──
        flow_text = {
            "主力资金净流入": "low_funds_inflow",
            "主力资金净流出": "high_funds_outflow",
            "近3日资金净流入": "netinflow_3days",
            "近5日资金净流入": "netinflow_5days",
        }
        if part in flow_text:
            filters["capital_flow"].append(flow_text[part]); continue
        if part == "近3月有机构调研":
            filters["institutional_holding"].append("org_survey_3m"); continue
        if part == "基金重仓":
            filters["institutional_holding"].append("allcorp_fund_ratio"); continue
        if part == "券商重仓":
            filters["institutional_holding"].append("allcorp_qs_ratio"); continue

        # ── 概念/行业 ──
        m = re.match(r"属于行业\((.+)\)", part)
        if m:
            filters["industry"] = [x.strip() for x in m.group(1).split(",")]
            continue
        m = re.match(r"涉及概念\((.+)\)", part)
        if m:
            filters["concept"] = [x.strip() for x in m.group(1).split(",")]
            continue

        # ── 新高新低 ──
        hl_text = {
            "当前新高": "now_newhigh", "当前新低": "now_newlow",
            "3天新高": "high_recent_3days", "5天新高": "high_recent_5days",
            "10天新高": "high_recent_10days", "20天新高": "high_recent_20days",
            "30天新高": "high_recent_30days",
            "3天新低": "low_recent_3days", "5天新低": "low_recent_5days",
            "10天新低": "low_recent_10days", "20天新低": "low_recent_20days",
            "30天新低": "low_recent_30days",
        }
        if part in hl_text:
            filters["new_high_filter"].append(hl_text[part]); continue

        # ── 战胜大盘 ──
        m = re.match(r"(\d+)天战胜大盘", part)
        if m:
            filters["win_market_filter"].append(f"win_market_{m.group(1)}days")
            continue

        # ── 连涨连跌 ──
        consec_text = {
            "连续4天上涨": "upper_4days", "连续8天上涨": "upper_8days",
            "连续9天上涨": "upper_9days", "连续7天下跌": "down_7days",
        }
        if part in consec_text:
            filters["consecutive_signals"].append(consec_text[part]); continue

        # ── 限价/定增/质押 ──
        if part == "限价上涨6月":
            filters["limited_lift_filter"].append("limited_lift_6m"); continue
        if part == "限价上涨1年":
            filters["limited_lift_filter"].append("limited_lift_1y"); continue
        m = re.match(r"定向增发(\d+[月年])", part)
        if m:
            seo_map = {"1月": "directional_seo_1m", "3月": "directional_seo_3m",
                       "6月": "directional_seo_6m", "1年": "directional_seo_1y"}
            if m.group(1) in seo_map:
                filters["directional_seo_filter"].append(seo_map[m.group(1)])
            continue
        m = re.match(r"股权质押(\d+[月年])", part)
        if m:
            pl_map = {"1月": "equity_pledge_1m", "3月": "equity_pledge_3m",
                      "6月": "equity_pledge_6m", "1年": "equity_pledge_1y"}
            if m.group(1) in pl_map:
                filters["equity_pledge_filter"].append(pl_map[m.group(1)])
            continue

        # ── 板块标识 ──
        board_text = {
            "上证50成分股": "is_sz50", "中证1000成分股": "is_zz1000",
            "创业板50成分股": "is_cy50", "已破净": "is_bps_break",
            "已破板": "is_issue_break",
        }
        if part in board_text:
            filters["hs_board_filter"].append(board_text[part]); continue

        # ── 龙虎榜参与方 ──
        if part == "机构参与":
            filters["tiger_participant"].append("inst_participated"); continue
        if part == "营业部参与":
            filters["tiger_participant"].append("dept_participated"); continue

        # ── 技术指标补充 ──
        if part == "突破30日线":
            filters["ma_30_break"].append("breakup_ma_30days"); continue
        kdj_text = {
            "KDJ金叉Z": "kdj_golden_forkz", "KDJ金叉Y": "kdj_golden_forky",
            "MACD金叉Z": "macd_golden_forkz", "MACD金叉Y": "macd_golden_forky",
        }
        if part in kdj_text:
            filters["kdj_signals"].append(kdj_text[part]); continue

        # ── pattern_signals ──
        pattern_text = {
            "乌云盖顶": "power_fulgun", "孕线": "pregnant",
            "黑云压顶": "black_cloud_tops", "窄幅整理": "narrow_finish",
            "反转锤子": "reversing_hammer", "第一天黎明": "first_dawn",
            "看跌吞没": "bearish_engulfing", "上攻放量": "upside_volume",
            "天道法则": "heaven_rule",
        }
        if part in pattern_text:
            filters["pattern_signals"].append(pattern_text[part]); continue

        # ── 基本面 ≥/≤ ──
        m = re.match(r"每股收益≥(.+)", part)
        if m:
            filters["basic_eps_min"] = _parse_number(m.group(1)); continue
        m = re.match(r"每股净资产≥(.+)", part)
        if m:
            filters["bvps_min"] = _parse_number(m.group(1)); continue
        m = re.match(r"每股自由现金流≥(.+)", part)
        if m:
            filters["per_fcfe_min"] = _parse_number(m.group(1)); continue
        m = re.match(r"归母净利润≥(.+)", part)
        if m:
            filters["parent_netprofit_min"] = _parse_number(m.group(1)); continue
        m = re.match(r"扣非净利润≥(.+)", part)
        if m:
            filters["deduct_netprofit_min"] = _parse_number(m.group(1)); continue
        m = re.match(r"营业收入≥(.+)", part)
        if m:
            filters["total_operate_income_min"] = _parse_number(m.group(1)); continue
        m = re.match(r"总资产报酬率≥(.+?)%", part)
        if m:
            filters["jroa_min"] = float(m.group(1)); continue
        m = re.match(r"投资回报率≥(.+?)%", part)
        if m:
            filters["roic_min"] = float(m.group(1)); continue
        m = re.match(r"销售净利率≥(.+?)%", part)
        if m:
            filters["sale_npr_min_filter"] = float(m.group(1)); continue
        m = re.match(r"资产负债率≤(.+?)%", part)
        if m:
            filters["debt_asset_ratio_max"] = float(m.group(1)); continue
        m = re.match(r"资产负债率(.+?)~(.+?)%", part)
        if m:
            filters["debt_asset_ratio_min"] = _parse_number(m.group(1))
            filters["debt_asset_ratio_max"] = _parse_number(m.group(2))
            continue
        m = re.match(r"流动比率≥(.+)", part)
        if m:
            filters["current_ratio_min"] = float(m.group(1)); continue
        m = re.match(r"速动比率≥(.+)", part)
        if m:
            filters["speed_ratio_min"] = float(m.group(1)); continue
        m = re.match(r"派息率≥(.+?)%", part)
        if m:
            filters["par_dividend_min"] = float(m.group(1)); continue
        m = re.match(r"质押比例≤(.+?)%", part)
        if m:
            filters["pledge_ratio_max"] = float(m.group(1)); continue
        m = re.match(r"商誉≤(.+)", part)
        if m:
            filters["goodwill_max"] = _parse_number(m.group(1)); continue

        # ── 机构股东 ≥ ──
        m = re.match(r"3月持股变动≥(.+?)%", part)
        if m:
            filters["holder_change_3m_min"] = float(m.group(1)); continue
        m = re.match(r"3月高管持股变动≥(.+?)%", part)
        if m:
            filters["executive_change_3m_min"] = float(m.group(1)); continue
        m = re.match(r"机构评级=(.+)", part)
        if m:
            filters["org_rating_filter"] = m.group(1); continue
        m = re.match(r"机构持股比例≥(.+?)%", part)
        if m:
            filters["allcorp_ratio_min"] = float(m.group(1)); continue
        m = re.match(r"基金持股≥(.+?)%", part)
        if m:
            filters["allcorp_fund_ratio_min"] = float(m.group(1)); continue
        m = re.match(r"券商持股≥(.+?)%", part)
        if m:
            filters["allcorp_qs_ratio_min"] = float(m.group(1)); continue
        m = re.match(r"QFII持股≥(.+?)%", part)
        if m:
            filters["allcorp_qfii_ratio_min"] = float(m.group(1)); continue

        # ── 资金面数值 ≥ ──
        m = re.match(r"净流入≥(.+)", part)
        if m:
            filters["net_inflow_min"] = _parse_number(m.group(1)); continue
        m = re.match(r"大单动向≥(.+)", part)
        if m:
            filters["ddx_min"] = float(m.group(1)); continue
        m = re.match(r"3日净流入≥(.+)", part)
        if m:
            filters["netinflow_min_3d"] = _parse_number(m.group(1)); continue
        m = re.match(r"5日净流入≥(.+)", part)
        if m:
            filters["netinflow_min_5d"] = _parse_number(m.group(1)); continue
        m = re.match(r"3日涨幅≥(.+?)%", part)
        if m:
            filters["changerate_3d_min"] = float(m.group(1)); continue
        m = re.match(r"5日涨幅≥(.+?)%", part)
        if m:
            filters["changerate_5d_min"] = float(m.group(1)); continue
        m = re.match(r"10日涨幅≥(.+?)%", part)
        if m:
            filters["changerate_10d_min"] = float(m.group(1)); continue

        # ── 行情指标 ≥ ──
        m = re.match(r"量比≥(.+)", part)
        if m:
            filters["mi_volume_ratio_min"] = _parse_number(m.group(1)); continue
        m = re.match(r"换手率≥(.+?)%", part)
        if m:
            filters["mi_turnover_rate_min"] = float(m.group(1)); continue
        m = re.match(r"成交量≥(.+?)手", part)
        if m:
            filters["mi_volume_min"] = _parse_number(m.group(1)); continue
        m = re.match(r"成交额≥(.+)", part)
        if m:
            filters["mi_amount_min"] = _parse_number(m.group(1)); continue

        # ── 范围格式 ──
        m = re.match(r"总市值(.+)", part)
        if m:
            lo, hi = _parse_range(m.group(1))
            filters["total_market_cap_min"] = lo
            filters["total_market_cap_max"] = hi
            continue
        m = re.match(r"流通市值(.+)", part)
        if m:
            lo, hi = _parse_range(m.group(1))
            filters["free_cap_min"] = lo
            filters["free_cap_max"] = hi
            continue
        m = re.match(r"市销率(.+)", part)
        if m:
            lo, hi = _parse_range(m.group(1))
            filters["ps_min"] = lo
            filters["ps_max"] = hi
            continue
        m = re.match(r"市现率(.+)", part)
        if m:
            lo, hi = _parse_range(m.group(1))
            filters["pcf_min"] = lo
            filters["pcf_max"] = hi
            continue
        m = re.match(r"动态PE(.+)", part)
        if m:
            lo, hi = _parse_range(m.group(1))
            filters["dtsyl_min"] = lo
            filters["dtsyl_max"] = hi
            continue
        m = re.match(r"总股本(.+)", part)
        if m:
            lo, hi = _parse_range(m.group(1))
            filters["total_shares_min"] = lo
            filters["total_shares_max"] = hi
            continue
        m = re.match(r"流通股本(.+)", part)
        if m:
            lo, hi = _parse_range(m.group(1))
            filters["free_shares_min"] = lo
            filters["free_shares_max"] = hi
            continue
        m = re.match(r"股东数(.+)", part)
        if m:
            lo, hi = _parse_range(m.group(1))
            filters["holder_newest_min"] = lo
            filters["holder_newest_max"] = hi
            continue
        m = re.match(r"年度涨幅(.+?)%", part)
        if m:
            p = m.group(1).split("~")
            filters["changerate_ty_min"] = _parse_number(p[0])
            filters["changerate_ty_max"] = _parse_number(p[1]) if len(p) > 1 else None
            continue

    return filters


# ══════════════════════════════════════════════════════════════
#  东方财富 API 调用
# ══════════════════════════════════════════════════════════════

def _gen_id(length: int = 32) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def _safe_float(val) -> Optional[float]:
    if val is None or val == "" or val == "-" or val == "--":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _call_eastmoney_api(keyword: str, page_size: int = 200, page_no: int = 1,
                         proxy: Optional[str] = None) -> Dict[str, Any]:
    """调用东方财富 search-code API（与前端 performEastMoneySearch 完全一致）"""
    body = {
        "needAmbiguousSuggest": True,
        "pageSize": page_size,
        "pageNo": page_no,
        "fingerprint": _gen_id(32),
        "matchWord": "",
        "shareToGuba": False,
        "timestamp": str(int(time.time() * 1000)),
        "requestId": _gen_id(32) + str(int(time.time() * 1000)),
        "removedConditionIdList": [],
        "ownSelectAll": False,
        "needCorrect": True,
        "client": "WEB",
        "product": "",
        "needShowStockNum": False,
        "biz": "web_ai_select_stocks",
        "xcId": "",
        "gids": [],
        "dxInfoNew": [],
        "keyWordNew": keyword,
        "customDataNew": json.dumps([{"type": "text", "value": keyword, "extra": ""}]),
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = requests.post(_EASTMONEY_SEARCH_URL, json=body, headers=headers,
                             proxies=proxies, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.error("EastMoney API request failed: %s", e)
        return {"code": -1, "msg": f"请求东方财富API失败: {e}"}


def _parse_stock_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """将东方财富返回的单只股票解析为标准格式（与前端 performEastMoneySearch 的 map 一致）"""
    return {
        "code": item.get("SECURITY_CODE", ""),
        "name": item.get("SECURITY_SHORT_NAME", ""),
        "industry": item.get("INDUSTRY", ""),
        "concept": item.get("CONCEPT", ""),
        "new_price": _safe_float(item.get("NEWEST_PRICE")),
        "change_rate": _safe_float(item.get("CHG")),
        "high_price": _safe_float(item.get("HIGH_PRICE")),
        "low_price": _safe_float(item.get("LOW_PRICE")),
        "pre_close_price": _safe_float(item.get("PRE_CLOSE_PRICE")),
        "volume": _safe_float(item.get("TRADE_VOLUME")),
        "deal_amount": item.get("TRADING_VOLUMES") or item.get("TRADE_AMOUNT"),
        "volume_ratio": item.get("QRR"),
        "turnover_rate": _safe_float(item.get("TURNOVER_RATE")),
        "amplitude": _safe_float(item.get("AMPLITUDE")),
        "pe_dynamic": item.get("PE_DYNAMIC") or item.get("PE9"),
        "pb_mrq": item.get("PB_NEW_MRQ"),
        "total_market_cap": item.get("TOEAL_MARKET_VALUE") or item.get("TOTAL_MARKET_CAP"),
        "free_cap": item.get("FREE_CAP"),
    }


# ══════════════════════════════════════════════════════════════
#  Agent Tool 函数
# ══════════════════════════════════════════════════════════════

def screen_stocks(
    keyword: str = "",
    market: str = "全部",
    filters: Optional[Dict[str, Any]] = None,
    page_size: int = 50,
    page_no: int = 1,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """
    智能选股：根据自然语言条件或结构化筛选条件搜索股票。

    两种调用方式（二选一）：
    1. keyword 模式：直接传自然语言，如 "市盈率低于20的科技股"
    2. filters 模式：传结构化条件字典（get_default_filters() 的格式），内部自动拼接关键词

    支持的筛选维度（与前端选股器完全一致）：
    - 基本面：PE/PB/股息率/ROE/毛利率/净利增长/营收增长/每股收益等
    - 技术面：均线突破(5/10/20/30/60日)/MACD金叉/KDJ金叉/K线形态等
    - 资金面：主力资金净流入/量比/换手率/机构持股等
    - 行业/概念：27个行业 + 12个概念板块
    - 行情指标：成交额/振幅/委比/涨幅(今日/5日/10日/60日/年初至今)等
    - 特殊筛选：新高新低/战胜大盘/连涨连跌/限价/定增/质押/板块标识等
    - 筹码指标：成本价/获利比例/集中度/股东数等
    - 龙虎榜：买卖额/净买/机构参与/营业部参与等

    Args:
        keyword: 自然语言选股条件（与 filters 二选一）
        market: 市场筛选，可选：全部/A股/沪深300/中证500/科创板/创业板/港股/美股/ETF基金
        filters: 结构化筛选条件字典（可选，优先级高于 keyword）
        page_size: 返回数量，默认50，最大200
        page_no: 页码，默认1
        proxy: 可选代理地址

    Returns:
        匹配的股票列表及元信息
    """
    # 如果传了结构化条件，先转成关键词
    if filters:
        keyword = build_keyword_from_filters(filters)
        # 从 filters 中提取市场（如果有）
        if market == "全部" and filters.get("_market"):
            market = filters["_market"]

    if not keyword or not keyword.strip():
        return {"error": "选股条件不能为空（keyword 或 filters 至少传一个）", "retriable": False}

    search_keyword = keyword.strip()
    if market and market != "全部" and market in MARKET_FILTER_MAP:
        search_keyword = f"{market} {search_keyword}"

    page_size = min(max(page_size, 1), 200)

    raw = _call_eastmoney_api(search_keyword, page_size=page_size, page_no=page_no, proxy=proxy)

    if str(raw.get("code")) != "100":
        return {
            "error": raw.get("msg", "选股搜索失败"),
            "retriable": True,
            "raw_code": raw.get("code"),
        }

    data = raw.get("data", {})
    result = data.get("result", {})
    stocks_raw = result.get("dataList", [])
    total = result.get("total", len(stocks_raw))

    stocks = [_parse_stock_item(s) for s in stocks_raw]

    return {
        "keyword": keyword,
        "market": market,
        "total": total,
        "page": page_no,
        "page_size": page_size,
        "count": len(stocks),
        "stocks": stocks,
    }


def get_screener_presets() -> Dict[str, Any]:
    """
    获取选股器支持的所有筛选条件分类和示例（与前端 FilterPanel 的 Tab 结构一致）。
    """
    return {
        "categories": {
            "基本面": {
                "tab": "fundamental",
                "description": "估值、成长、盈利能力指标",
                "groups": {
                    "估值指标": ["PE在X到Y之间", "PB在X到Y之间", "股息率不低于X%"],
                    "成长能力": ["净利增长>15%", "营收增长>15%", "每股收益增长>10%",
                              "营收3年复合增长>10%", "净利润3年复合增长>10%"],
                    "盈利能力": ["ROE不低于X%", "毛利率不低于X%", "销售净利率≥X%",
                              "经营现金流为正"],
                },
            },
            "技术面": {
                "tab": "technical",
                "description": "均线突破、技术指标、K线形态",
                "groups": {
                    "均线突破": ["突破5日线", "突破10日线", "突破20日线", "突破30日线",
                              "突破60日线", "长期均线多头排列"],
                    "技术指标": ["MACD金叉", "KDJ金叉", "突破形态", "放量上涨", "缩量下跌",
                              "KDJ金叉Z", "KDJ金叉Y", "MACD金叉Z", "MACD金叉Y"],
                    "经典K线形态": ["大阳线", "两阳夹一阴", "阳包阴", "早晨之星", "黄昏之星",
                                "射击之星", "三只乌鸦", "锤头", "倒锤头", "十字星",
                                "长腿十字线", "墓碑线", "蜻蜓线", "双飞乌鸦", "出水芙蓉",
                                "低开高走", "巨量", "底部十字孕线", "顶部十字孕线"],
                    "分时K线形态": ["尾盘拉升", "盘中打压", "盘中拉升", "快速反弹"],
                    "其它形态": ["一字涨停", "一字跌停"],
                    "特殊形态": ["乌云盖顶", "孕线", "黑云压顶", "窄幅整理", "反转锤子",
                              "第一天黎明", "看跌吞没", "上攻放量", "天道法则"],
                },
            },
            "资金面": {
                "tab": "capital",
                "description": "资金流向、成交量、机构持股",
                "groups": {
                    "资金流向": ["主力资金净流入", "主力资金净流出", "近3日资金净流入",
                              "近5日资金净流入"],
                    "成交量": ["量比不低于X", "换手率不低于X%"],
                    "机构持股": ["近3月有机构调研", "基金重仓", "券商重仓"],
                    "资金数值": ["净流入≥X", "大单动向≥X", "3日净流入≥X", "5日净流入≥X",
                              "3日涨幅≥X%", "5日涨幅≥X%", "10日涨幅≥X%"],
                },
            },
            "概念/行业": {
                "tab": "concept",
                "description": "按行业或概念板块筛选",
                "groups": {
                    "行业分类": INDUSTRY_OPTIONS,
                    "概念题材": CONCEPT_OPTIONS,
                },
            },
            "行情指标": {
                "tab": "market_indicator",
                "description": "量价、估值市值、涨跌、价格资金",
                "groups": {
                    "量价指标": ["量比≥X", "换手率≥X%", "成交量≥X手", "振幅X~Y%"],
                    "估值&市值": ["成交额≥X元", "市盈率X~Y", "流通市值X~Y亿", "总市值X~Y亿"],
                    "涨跌": ["委比X~Y%", "今日涨幅X~Y%", "5日涨幅X~Y%", "10日涨幅X~Y%"],
                    "价格&资金": ["60日涨幅X~Y%", "年初至今X~Y%", "收盘价X~Y元", "净流入X~Y元"],
                },
            },
            "特殊筛选": {
                "tab": "special_filter",
                "description": "新高新低、战胜大盘、连涨连跌、限价定增质押、板块标识",
                "groups": {
                    "新高新低": ["当前新高", "当前新低", "3天新高", "5天新高", "10天新高",
                              "20天新高", "30天新高", "3天新低", "5天新低", "10天新低",
                              "20天新低", "30天新低"],
                    "战胜大盘": ["3天战胜大盘", "5天战胜大盘", "10天战胜大盘",
                              "20天战胜大盘", "30天战胜大盘"],
                    "连涨连跌": ["连续4天上涨", "连续8天上涨", "连续9天上涨", "连续7天下跌"],
                    "限价/定增/质押": ["限价上涨6月", "限价上涨1年",
                                  "定向增发1月/3月/6月/1年", "股权质押1月/3月/6月/1年"],
                    "板块标识": ["上证50成分股", "中证1000成分股", "创业板50成分股",
                              "已破净", "已破板"],
                    "龙虎榜": ["机构参与", "营业部参与"],
                },
            },
        },
        "markets": list(MARKET_FILTER_MAP.keys()),
        "industry_options": INDUSTRY_OPTIONS,
        "concept_options": CONCEPT_OPTIONS,
        "tips": [
            "多个条件用分号(;)分隔，如：'PE在5到20之间; ROE不低于15%'",
            "可混合使用不同类别的条件",
            "市场筛选可加在条件前面，如：'科创板 净利增长>15%'",
            "也可直接用自然语言描述，如：'市盈率低于20的银行股'",
        ],
    }


# ══════════════════════════════════════════════════════════════
#  OpenAI tool declarations
# ══════════════════════════════════════════════════════════════

SCREENER_TOOLS = [
    {
        "fn": screen_stocks,
        "name": "screen_stocks",
        "description": (
            "智能选股：根据自然语言条件筛选A股/港股/美股。"
            "支持两大类调用方式：\n"
            "1. keyword 模式：直接传自然语言，如 '市盈率低于20的科技股'\n"
            "2. filters 模式：传结构化条件字典（参考 get_default_filters）\n"
            "支持130+筛选维度：基本面(PE/PB/ROE/股息率/净利增长/毛利率等)、"
            "技术面(均线突破/MACD/KDJ/K线形态等)、资金面(主力资金/量比/换手率/机构持股)、"
            "行业(27个)/概念(12个)、行情指标(成交额/振幅/涨幅)、"
            "特殊筛选(新高新低/战胜大盘/连涨连跌/限价/定增/质押/板块标识)、"
            "筹码(成本/集中度/股东数)、龙虎榜(买卖/净买/机构参与)。"
            "返回匹配股票列表（代码、名称、价格、涨跌幅、市值等）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": (
                        "自然语言选股条件。多个条件用分号分隔。"
                        "示例：'市盈率低于20的科技股'、'PE在5到20之间; ROE不低于15%'、"
                        "'近5日突破60日均线的银行股'。"
                        "与 filters 参数二选一，keyword 优先。"
                    ),
                },
                "market": {
                    "type": "string",
                    "description": "市场筛选",
                    "default": "全部",
                    "enum": ["全部", "A股", "沪深300", "中证500", "科创板", "创业板", "港股", "美股", "ETF基金"],
                },
                "filters": {
                    "type": "object",
                    "description": (
                        "结构化筛选条件字典（可选）。格式参考 get_default_filters() 返回值。"
                        "传入后内部自动拼接为自然语言关键词。与 keyword 二选一。"
                    ),
                },
                "page_size": {
                    "type": "integer",
                    "description": "返回数量，默认50，最大200",
                    "default": 50,
                },
                "page_no": {
                    "type": "integer",
                    "description": "页码，默认1",
                    "default": 1,
                },
            },
            "required": [],
        },
    },
    {
        "fn": get_screener_presets,
        "name": "get_screener_presets",
        "description": (
            "获取选股器支持的所有筛选条件分类和示例（9大类130+维度）。"
            "当用户想了解有哪些选股条件可用，或不确定该用什么条件时调用。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "fn": get_default_filters,
        "name": "get_default_filters",
        "description": (
            "获取选股器筛选条件的完整结构（130+字段的默认值字典）。"
            "用于了解 screen_stocks 的 filters 参数的完整格式。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "fn": build_keyword_from_filters,
        "name": "build_keyword_from_filters",
        "description": (
            "将结构化筛选条件字典转换为自然语言关键词字符串。"
            "用于调试或查看 filters 对应的搜索文本。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filters": {
                    "type": "object",
                    "description": "结构化筛选条件字典",
                },
            },
            "required": ["filters"],
        },
    },
    {
        "fn": parse_filters_from_text,
        "name": "parse_filters_from_text",
        "description": (
            "将自然语言选股文本解析为结构化筛选条件字典。"
            "用于将用户输入的文本转换为可编辑的结构化条件。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "自然语言选股条件文本，用分号分隔多个条件",
                },
            },
            "required": ["text"],
        },
    },
]


# ══════════════════════════════════════════════════════════════
#  后端数据接口工具（AShareDataHub 直连）
#  龙虎榜、热榜、涨停池、跌停池、炸板池、市场快照、个股资金流
# ══════════════════════════════════════════════════════════════

_hub_lock = __import__("threading").Lock()
_hub_instance = None
_hub_init_failed = False


def _get_hub():
    """
    懒加载 AShareDataHub 实例（进程内单例，线程安全）。

    初始化失败时设置标志，避免每次调用都重复尝试并报错。
    调用 reset_hub() 可重置以重新初始化。
    """
    global _hub_instance, _hub_init_failed
    if _hub_instance is not None:
        return _hub_instance
    if _hub_init_failed:
        raise RuntimeError("AShareDataHub 初始化失败，已标记为不可用。调用 reset_hub() 可重试。")
    with _hub_lock:
        # 双重检查锁定
        if _hub_instance is not None:
            return _hub_instance
        if _hub_init_failed:
            raise RuntimeError("AShareDataHub 初始化失败，已标记为不可用。")
        try:
            from app.interfaces.cn_stock_extent import AShareDataHub
            from app.data_sources.factory import DataSourceFactory
            source = DataSourceFactory.get_source("CNStock")
            _hub_instance = AShareDataHub(sources=[source])
            logger.info("AShareDataHub 实例初始化成功")
            return _hub_instance
        except Exception as e:
            _hub_init_failed = True
            logger.error("AShareDataHub 初始化失败: %s", e, exc_info=True)
            raise RuntimeError(f"AShareDataHub 初始化失败: {e}") from e


def reset_hub():
    """重置 AShareDataHub 实例，下次调用 _get_hub() 时重新初始化。"""
    global _hub_instance, _hub_init_failed
    with _hub_lock:
        _hub_instance = None
        _hub_init_failed = False
    logger.info("AShareDataHub 实例已重置")


def _validate_date(date_str: str, param_name: str = "date") -> str:
    """
    校验日期格式 YYYY-MM-DD，返回校验后的日期字符串。
    空字符串直接返回（由调用方决定默认值）。
    """
    if not date_str:
        return date_str
    from datetime import datetime
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        raise ValueError(f"参数 {param_name} 格式错误: '{date_str}'，应为 YYYY-MM-DD")


def _today_str() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


def _yesterday_str() -> str:
    """获取最近一个交易日（跳过周末，不处理节假日）。"""
    from datetime import datetime, timedelta
    d = datetime.now() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _safe_hub_call(func_name: str, callable_fn, *args, **kwargs) -> Dict[str, Any]:
    """
    统一的 AShareDataHub 调用封装：
    - 捕获 RuntimeError（初始化失败）
    - 捕获所有异常
    - 返回统一格式的错误字典
    """
    try:
        hub = _get_hub()
        return callable_fn(hub, *args, **kwargs)
    except RuntimeError as e:
        logger.error("%s: AShareDataHub 不可用: %s", func_name, e)
        return {"error": str(e), "retriable": False}
    except Exception as e:
        logger.error("%s failed: %s", func_name, e, exc_info=True)
        return {"error": str(e), "retriable": True}


# ── 龙虎榜 ──────────────────────────────────────────────────

def get_dragon_tiger_stocks(date: str = "", days: int = 1) -> Dict[str, Any]:
    """
    获取龙虎榜数据：上榜股票代码、名称、买卖金额、净买入额、涨跌幅、上榜原因等。

    数据来源：东方财富 → AkShare 多源 fallback，PostgreSQL 持久存储。

    Args:
        date: 查询日期 YYYY-MM-DD，默认为最近一个交易日
        days: 回溯天数，默认1（仅查当天），可设为3/5/7获取多天

    Returns:
        龙虎榜股票列表
    """
    from datetime import datetime, timedelta

    try:
        date = _validate_date(date)
    except ValueError as e:
        return {"error": str(e), "retriable": False}

    if not date:
        date = _yesterday_str()

    days = max(1, min(days, 30))  # 限制回溯范围

    start_date = date
    end_date = date
    if days > 1:
        d = datetime.strptime(date, "%Y-%m-%d") - timedelta(days=days - 1)
        start_date = d.strftime("%Y-%m-%d")

    def _do(hub):
        data = hub.dragon_tiger.get_history(start_date, end_date)
        return {"date": date, "days": days, "count": len(data), "stocks": data}

    return _safe_hub_call("get_dragon_tiger_stocks", _do)


def get_dragon_tiger_by_stock(stock_code: str, days: int = 30) -> Dict[str, Any]:
    """
    查询某只股票的龙虎榜历史记录。

    Args:
        stock_code: 股票代码（如 000001）
        days: 回溯天数，默认30

    Returns:
        该股票的龙虎榜记录
    """
    from datetime import datetime, timedelta

    if not stock_code or not stock_code.strip():
        return {"error": "stock_code 不能为空", "retriable": False}

    days = max(1, min(days, 365))
    end_date = _today_str()
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    def _do(hub):
        data = hub.dragon_tiger.get_by_stock(stock_code.strip(), start_date, end_date)
        return {"stock_code": stock_code, "days": days, "count": len(data), "records": data}

    return _safe_hub_call("get_dragon_tiger_by_stock", _do)


# ── 热榜/人气榜 ─────────────────────────────────────────────

def get_hot_rank_stocks(top_n: int = 30) -> Dict[str, Any]:
    """
    获取实时股票热榜/人气榜：排名、代码、名称、人气分数、价格、涨跌幅。

    数据来源：东方财富 → AkShare(东财) → AkShare(问财) 多源 fallback。

    Args:
        top_n: 返回前N名，默认30，最大100

    Returns:
        热榜股票列表
    """
    top_n = min(max(int(top_n or 30), 1), 100)

    def _do(hub):
        data = hub.hot_rank.get_realtime()
        return {"count": len(data[:top_n]), "stocks": data[:top_n]}

    return _safe_hub_call("get_hot_rank_stocks", _do)


# ── 涨停池 ──────────────────────────────────────────────────

def get_zt_pool_stocks(date: str = "", min_continuous_days: int = 0) -> Dict[str, Any]:
    """
    获取涨停股票池：代码、名称、涨停价、封板资金、连板天数等。

    数据来源：东方财富 → AkShare → 新浪 多源 fallback。
    可筛选连板股（设置 min_continuous_days）。

    Args:
        date: 交易日期 YYYY-MM-DD，默认今天
        min_continuous_days: 最少连板天数，默认0（全部），设为2可筛选连板股

    Returns:
        涨停股票列表
    """
    try:
        date = _validate_date(date)
    except ValueError as e:
        return {"error": str(e), "retriable": False}

    if not date:
        date = _today_str()
    min_continuous_days = max(0, int(min_continuous_days or 0))

    def _do(hub):
        if min_continuous_days > 0:
            data = hub.zt_pool.get_continuous_zt(min_days=min_continuous_days, trade_date=date)
        else:
            data = hub.zt_pool.get_realtime(date)
        return {"date": date, "min_continuous_days": min_continuous_days, "count": len(data), "stocks": data}

    return _safe_hub_call("get_zt_pool_stocks", _do)


# ── 跌停池 ──────────────────────────────────────────────────

def get_limit_down_stocks(date: str = "") -> Dict[str, Any]:
    """
    获取跌停股票池：代码、名称、跌停价、封单量等。

    数据来源：东方财富 → AkShare → 新浪 多源 fallback。

    Args:
        date: 交易日期 YYYY-MM-DD，默认今天

    Returns:
        跌停股票列表
    """
    try:
        date = _validate_date(date)
    except ValueError as e:
        return {"error": str(e), "retriable": False}

    if not date:
        date = _today_str()

    def _do(hub):
        data = hub.limit_down.get_realtime(date)
        return {"date": date, "count": len(data), "stocks": data}

    return _safe_hub_call("get_limit_down_stocks", _do)


# ── 炸板池 ──────────────────────────────────────────────────

def get_broken_board_stocks(date: str = "") -> Dict[str, Any]:
    """
    获取炸板(开板)股票池：代码、名称、涨停时间、开板时间等。

    炸板 = 曾封涨停但被打开的股票，往往是资金分歧的信号。

    数据来源：东方财富 → AkShare 多源 fallback。

    Args:
        date: 交易日期 YYYY-MM-DD，默认今天

    Returns:
        炸板股票列表
    """
    try:
        date = _validate_date(date)
    except ValueError as e:
        return {"error": str(e), "retriable": False}

    if not date:
        date = _today_str()

    def _do(hub):
        data = hub.broken_board.get_realtime(date)
        return {"date": date, "count": len(data), "stocks": data}

    return _safe_hub_call("get_broken_board_stocks", _do)


# ── 市场快照 ─────────────────────────────────────────────────

def get_market_overview() -> Dict[str, Any]:
    """
    获取全市场涨跌统计快照：上涨/下跌家数、涨停/跌停数、总成交额、情绪指标等。

    数据来源：东方财富全量 → AkShare fallback。

    Returns:
        市场快照字典
    """
    def _do(hub):
        data = hub.market_snapshot.get_realtime()
        return data

    return _safe_hub_call("get_market_overview", _do)


# ── 个股资金流向 ─────────────────────────────────────────────

def get_stock_fund_flow(stock_code: str) -> Dict[str, Any]:
    """
    获取个股资金流向：主力/大单/中单/小单的净流入额。

    数据来源：东方财富 → AkShare fallback。

    Args:
        stock_code: 股票代码（如 000001）

    Returns:
        资金流向字典（主力净流入、大单净流入、中单净流入、小单净流入等）
    """
    if not stock_code or not stock_code.strip():
        return {"error": "stock_code 不能为空", "retriable": False}

    def _do(hub):
        data = hub.stock_fund_flow.get_flow(stock_code.strip())
        if data:
            return data
        return {"stock_code": stock_code, "error": "未获取到资金流数据", "retriable": True}

    return _safe_hub_call("get_stock_fund_flow", _do)


def batch_get_stock_fund_flow(stock_codes: str = "") -> Dict[str, Any]:
    """
    批量获取个股资金流向。

    Args:
        stock_codes: 股票代码，逗号分隔（如 "000001,600519,300750"）

    Returns:
        各股票的资金流向映射
    """
    codes = [c.strip() for c in (stock_codes or "").split(",") if c.strip()]
    if not codes:
        return {"error": "stock_codes 不能为空", "retriable": False}
    if len(codes) > 20:
        return {"error": f"单次最多20只股票，当前 {len(codes)} 只", "retriable": False}

    def _do(hub):
        result = hub.stock_fund_flow.batch_get_flow(codes)
        return {
            "count": len(result),
            "flows": {k: v for k, v in result.items() if v is not None},
            "failed": [k for k, v in result.items() if v is None],
        }

    return _safe_hub_call("batch_get_stock_fund_flow", _do)


# ── 板块资金流向 ─────────────────────────────────────────────

def get_sector_fund_flow(date: str = "") -> Dict[str, Any]:
    """
    获取行业板块资金流向排名。

    Args:
        date: 交易日期 YYYY-MM-DD，默认今天

    Returns:
        板块资金流向列表
    """
    try:
        date = _validate_date(date)
    except ValueError as e:
        return {"error": str(e), "retriable": False}

    if not date:
        date = _today_str()

    def _do(hub):
        data = hub.fund_flow.get_sector_flow(date)
        return {"date": date, "count": len(data), "sectors": data}

    return _safe_hub_call("get_sector_fund_flow", _do)


def get_concept_fund_flow(date: str = "") -> Dict[str, Any]:
    """
    获取概念板块资金流向排名。

    Args:
        date: 交易日期 YYYY-MM-DD，默认今天

    Returns:
        概念资金流向列表
    """
    try:
        date = _validate_date(date)
    except ValueError as e:
        return {"error": str(e), "retriable": False}

    if not date:
        date = _today_str()

    def _do(hub):
        data = hub.fund_flow.get_concept_flow(date)
        return {"date": date, "count": len(data), "concepts": data}

    return _safe_hub_call("get_concept_fund_flow", _do)


# ── 综合选股 ─────────────────────────────────────────────────

def smart_screen(
    mode: str = "eastmoney",
    keyword: str = "",
    market: str = "全部",
    filters: Optional[Dict[str, Any]] = None,
    min_zt_days: int = 0,
    include_dragon_tiger: bool = False,
    include_hot_rank: bool = False,
    include_fund_flow: bool = False,
    top_n: int = 50,
) -> Dict[str, Any]:
    """
    综合选股：可组合多种数据源进行筛选。

    支持的 mode：
    - "eastmoney": 东方财富智能选股（默认，支持130+条件）
    - "zt_pool": 涨停池选股（可筛选连板股）
    - "dragon_tiger": 龙虎榜选股
    - "hot_rank": 热榜选股
    - "limit_down": 跌停池
    - "broken_board": 炸板池
    - "combine": 组合模式（keyword 搜索 + 可选附加龙虎榜/热榜/资金流信息）

    Args:
        mode: 选股模式
        keyword: 自然语言选股条件（eastmoney/combine 模式用）
        market: 市场筛选
        filters: 结构化筛选条件
        min_zt_days: 最少连板天数（zt_pool 模式用）
        include_dragon_tiger: 是否附加龙虎榜信息（combine 模式）
        include_hot_rank: 是否附加热榜信息（combine 模式）
        include_fund_flow: 是否附加资金流信息（combine 模式）
        top_n: 返回数量上限

    Returns:
        选股结果
    """
    hub = _get_hub()
    top_n = min(max(top_n, 1), 200)
    result: Dict[str, Any] = {"mode": mode, "stocks": []}

    try:
        if mode == "eastmoney":
            return screen_stocks(keyword=keyword, market=market, filters=filters, page_size=top_n)

        elif mode == "zt_pool":
            data = hub.zt_pool.get_continuous_zt(min_days=min_zt_days) if min_zt_days > 0 else hub.zt_pool.get_realtime()
            result["stocks"] = data[:top_n]
            result["count"] = len(data)
            result["date"] = _today_str()

        elif mode == "dragon_tiger":
            data = hub.dragon_tiger.get_history(_yesterday_str(), _today_str())
            result["stocks"] = data[:top_n]
            result["count"] = len(data)

        elif mode == "hot_rank":
            data = hub.hot_rank.get_realtime()
            result["stocks"] = data[:top_n]
            result["count"] = len(data)

        elif mode == "limit_down":
            data = hub.limit_down.get_realtime()
            result["stocks"] = data[:top_n]
            result["count"] = len(data)
            result["date"] = _today_str()

        elif mode == "broken_board":
            data = hub.broken_board.get_realtime()
            result["stocks"] = data[:top_n]
            result["count"] = len(data)
            result["date"] = _today_str()

        elif mode == "combine":
            # 先用 eastmoney 搜索
            base = screen_stocks(keyword=keyword, market=market, filters=filters, page_size=top_n)
            if "error" in base:
                return base
            result["stocks"] = base.get("stocks", [])
            result["count"] = base.get("count", 0)
            result["keyword"] = base.get("keyword", "")
            result["market"] = base.get("market", "")

            # 附加信息
            codes = [s.get("code") for s in result["stocks"] if s.get("code")]

            if include_dragon_tiger and codes:
                try:
                    dt_data = hub.dragon_tiger.get_history(_yesterday_str(), _today_str())
                    dt_codes = {d.get("stock_code") for d in dt_data}
                    for s in result["stocks"]:
                        s["on_dragon_tiger"] = s.get("code") in dt_codes
                    result["dragon_tiger_count"] = len(dt_codes)
                except Exception:
                    pass

            if include_hot_rank and codes:
                try:
                    hr_data = hub.hot_rank.get_realtime()
                    hr_map = {h.get("stock_code"): h.get("rank") for h in hr_data if h.get("stock_code")}
                    for s in result["stocks"]:
                        if s.get("code") in hr_map:
                            s["hot_rank"] = hr_map[s["code"]]
                except Exception:
                    pass

            if include_fund_flow and codes:
                try:
                    ff_data = hub.stock_fund_flow.batch_get_flow(codes[:10])
                    for s in result["stocks"]:
                        flow = ff_data.get(s.get("code"))
                        if flow:
                            s["main_net_flow"] = flow.get("main_net_flow")
                    result["fund_flow_note"] = f"资金流数据仅覆盖前 {min(len(codes), 10)} 只股票"
                except Exception:
                    pass

        else:
            return {"error": f"未知模式: {mode}，可选: eastmoney/zt_pool/dragon_tiger/hot_rank/limit_down/broken_board/combine", "retriable": False}

        return result

    except Exception as e:
        logger.error("smart_screen failed: %s", e)
        return {"error": str(e), "retriable": True}


# ══════════════════════════════════════════════════════════════
#  后端数据接口工具 — OpenAI tool declarations（追加）
# ══════════════════════════════════════════════════════════════

BACKEND_DATA_TOOLS = [
    {
        "fn": get_dragon_tiger_stocks,
        "name": "get_dragon_tiger_stocks",
        "description": (
            "获取龙虎榜数据：上榜股票代码、名称、买卖金额、净买入额、涨跌幅、上榜原因。"
            "可按日期查询，默认查最近一个交易日。数据存储在 PostgreSQL 中，支持历史回溯。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "查询日期 YYYY-MM-DD，默认最近交易日"},
                "days": {"type": "integer", "description": "回溯天数，默认1", "default": 1},
            },
        },
    },
    {
        "fn": get_dragon_tiger_by_stock,
        "name": "get_dragon_tiger_by_stock",
        "description": "查询某只股票的龙虎榜历史记录。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "股票代码"},
                "days": {"type": "integer", "description": "回溯天数，默认30", "default": 30},
            },
            "required": ["stock_code"],
        },
    },
    {
        "fn": get_hot_rank_stocks,
        "name": "get_hot_rank_stocks",
        "description": (
            "获取实时股票热榜/人气榜：排名、代码、名称、人气分数、价格、涨跌幅。"
            "反映市场关注度最高的个股。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "top_n": {"type": "integer", "description": "返回前N名，默认30，最大100", "default": 30},
            },
        },
    },
    {
        "fn": get_zt_pool_stocks,
        "name": "get_zt_pool_stocks",
        "description": (
            "获取涨停股票池：代码、名称、涨停价、封板资金、连板天数。"
            "可筛选连板股（设 min_continuous_days>=2）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "交易日期 YYYY-MM-DD，默认今天"},
                "min_continuous_days": {"type": "integer", "description": "最少连板天数，0=全部", "default": 0},
            },
        },
    },
    {
        "fn": get_limit_down_stocks,
        "name": "get_limit_down_stocks",
        "description": "获取跌停股票池：代码、名称、跌停价、封单量。",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "交易日期 YYYY-MM-DD，默认今天"},
            },
        },
    },
    {
        "fn": get_broken_board_stocks,
        "name": "get_broken_board_stocks",
        "description": (
            "获取炸板(开板)股票池：代码、名称、涨停时间、开板时间。"
            "炸板=曾封涨停但被打开，往往是资金分歧信号。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "交易日期 YYYY-MM-DD，默认今天"},
            },
        },
    },
    {
        "fn": get_market_overview,
        "name": "get_market_overview",
        "description": (
            "获取全市场涨跌统计快照：上涨/下跌家数、涨停/跌停数、总成交额(亿)、情绪指标。"
            "用于判断市场整体氛围。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "fn": get_stock_fund_flow,
        "name": "get_stock_fund_flow",
        "description": "获取个股资金流向：主力/大单/中单/小单的净流入额(元)。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "股票代码"},
            },
            "required": ["stock_code"],
        },
    },
    {
        "fn": batch_get_stock_fund_flow,
        "name": "batch_get_stock_fund_flow",
        "description": "批量获取个股资金流向，逗号分隔代码，单次最多20只。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_codes": {"type": "string", "description": "股票代码，逗号分隔（如 '000001,600519'）"},
            },
            "required": ["stock_codes"],
        },
    },
    {
        "fn": get_sector_fund_flow,
        "name": "get_sector_fund_flow",
        "description": "获取行业板块资金流向排名。",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "交易日期 YYYY-MM-DD，默认今天"},
            },
        },
    },
    {
        "fn": get_concept_fund_flow,
        "name": "get_concept_fund_flow",
        "description": "获取概念板块资金流向排名。",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "交易日期 YYYY-MM-DD，默认今天"},
            },
        },
    },
    {
        "fn": smart_screen,
        "name": "smart_screen",
        "description": (
            "综合选股：可组合多种数据源。支持7种模式：\n"
            "- eastmoney: 东方财富智能选股（130+条件）\n"
            "- zt_pool: 涨停池（可筛选连板股）\n"
            "- dragon_tiger: 龙虎榜\n"
            "- hot_rank: 热榜/人气榜\n"
            "- limit_down: 跌停池\n"
            "- broken_board: 炸板池\n"
            "- combine: 组合模式（关键词搜索 + 可选附加龙虎榜/热榜/资金流标注）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "description": "选股模式",
                    "default": "eastmoney",
                    "enum": ["eastmoney", "zt_pool", "dragon_tiger", "hot_rank", "limit_down", "broken_board", "combine"],
                },
                "keyword": {"type": "string", "description": "自然语言选股条件（eastmoney/combine模式）"},
                "market": {"type": "string", "description": "市场筛选", "default": "全部"},
                "filters": {"type": "object", "description": "结构化筛选条件"},
                "min_zt_days": {"type": "integer", "description": "最少连板天数（zt_pool模式）", "default": 0},
                "include_dragon_tiger": {"type": "boolean", "description": "附加龙虎榜标注（combine模式）", "default": False},
                "include_hot_rank": {"type": "boolean", "description": "附加热榜标注（combine模式）", "default": False},
                "include_fund_flow": {"type": "boolean", "description": "附加资金流标注（combine模式）", "default": False},
                "top_n": {"type": "integer", "description": "返回数量上限", "default": 50},
            },
        },
    },
]

# 合并所有工具
SCREENER_TOOLS = SCREENER_TOOLS + BACKEND_DATA_TOOLS
