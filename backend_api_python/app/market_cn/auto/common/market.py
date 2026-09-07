#!/usr/bin/env python3
"""板块判定与涨停识别 (auto/common) —— 从 dragon_core.py 逐字提取 (2026-09-07)

用途: 涨停阈值按板块区分 (主板10%/创业科创20%), 供信号判定与 U1~U4 预过滤共用。
关键设计点: is_limit_up 用 0.98 系数容错 (浮点/四舍五入边界), 阈值 9.8%/19.8%。
易错点: bars 元素为 dict (time/open/high/low/close/volume), volume 单位是股。
"""
from __future__ import annotations


def get_board_type(code):
    c = str(code)[:3]
    return "gem_star" if c.startswith("30") or c.startswith("68") else "main"


def get_board_name(code):
    c = str(code)[:3]
    if c.startswith("68"): return "科创板"
    elif c.startswith("30"): return "创业板"
    elif c.startswith("6"): return "沪主板"
    elif c.startswith(("0", "2")): return "深主板"
    return "未知"


def is_limit_up(close, prev_close, board_type):
    threshold = 0.098 if board_type == "main" else 0.198
    if prev_close <= 0: return False
    return (close / prev_close - 1) >= threshold * 0.98


def find_limit_ups(bars, board_type):
    """找到所有涨停日索引。"""
    result = []
    for i in range(1, len(bars)):
        if is_limit_up(bars[i]['close'], bars[i-1]['close'], board_type):
            result.append(i)
    return result
