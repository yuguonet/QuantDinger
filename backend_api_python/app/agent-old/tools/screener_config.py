# -*- coding: utf-8 -*-
"""
Screener Config — 选股器常量配置。

包含：滑块配置、行业/概念选项、市场映射。
与前端 FilterPanel.vue 完全一致。
"""
from __future__ import annotations


MARKET_FILTER_MAP = {
    "全部": "全部", "A股": "A股", "沪深300": "沪深300",
    "中证500": "中证500", "科创板": "科创板", "创业板": "创业板",
    "港股": "港股", "美股": "美股", "ETF基金": "ETF基金",
}

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
