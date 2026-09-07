#!/usr/bin/env python3
"""U1~U4 统一前置过滤 (auto/common) —— 从 dragon_core.py 逐字提取 (2026-09-07)

用途: 全策略共用的"防杂毛"准入过滤, 在信号判定日收盘可知数据上判定。
  U1 非ST | U2 换手率>=3% | U3 流通市值20~500亿 | U4 前期热度(20日涨幅>=10% 或 前20日有涨停)

关键设计点:
  - **锚定日由策略声明** (prefilter_anchor): 龙回头锚涨停日、V1/断板锚信号日 ——
    D0 是缩量小阴日, @D0 评估 U2 会误杀 (回测实证 43.1% vs 44.7%), 扫描器按策略取锚, 不在本层硬编码;
  - code_info 缺失时跳过 U1/U2/U3 (不误杀), U4 仍生效。
易错点: code_info 是单票字典 (name/circ_shares), 不是全量映射; volume 单位是股。
"""
from __future__ import annotations

from app.market_cn.auto.common.market import get_board_type, is_limit_up

PREFILTER_PARAMS = {
    'turnover_min': 3.0,        # U2 换手率% 下限 (全市场验证: +0.7pp; 用户经验口径5%更严, 会误杀低换手大盘样本)
    'float_mv_min': 20.0,       # U3 流通市值下限(亿) (统一层20~500亿; 严格30~300会误杀600105)
    'float_mv_max': 500.0,      # U3 流通市值上限(亿)
    'heat_ret20_min': 10.0,     # U4 前期热度: 20日涨幅% 下限 (与prior_lu或关系)
    'heat_prior_lu_min': 1,     # U4 前20日涨停次数下限 (或关系, 不含D0)
}


def unified_prefilter(bars, i, code, code_info=None):
    """统一前置过滤 U1~U4, 在判定日 i 收盘可知数据上判定。

    code_info 为该股的 stock_basic_info 字典 (含 name/circ_shares), 不是全量映射。
    返回 (ok, fail_reasons)。code_info 缺失时跳过 U1/U2/U3 (不误杀), U4 仍生效。
    """
    p = PREFILTER_PARAMS
    fails = []
    # U1 非ST (名称兜底; 涨停阈值已自然排除ST, 此处防漏)
    if code_info and code_info.get('name') and 'ST' in str(code_info['name']).upper():
        fails.append('U1_ST')
    # U2 换手率 / U3 流通市值
    if code_info and code_info.get('circ_shares'):
        turnover = bars[i]['volume'] / code_info['circ_shares'] * 100
        if turnover < p['turnover_min']:
            fails.append(f'U2换手{turnover:.1f}')
        float_mv = code_info['circ_shares'] * bars[i]['close'] / 1e8
        if not (p['float_mv_min'] <= float_mv <= p['float_mv_max']):
            fails.append(f'U3市值{float_mv:.0f}亿')
    # U4 前期热度: 20日涨幅>=10% 或 前20日有涨停 (不含D0)
    bt = get_board_type(code)
    has_lu = any(is_limit_up(bars[j]['close'], bars[j-1]['close'], bt)
                 for j in range(max(1, i - 19), i))
    ret20 = bars[i]['close'] / bars[i - 20]['close'] - 1 if i >= 20 and bars[i - 20]['close'] > 0 else None
    if not has_lu and (ret20 is None or ret20 * 100 < p['heat_ret20_min']):
        fails.append('U4冷门')
    return (not fails), fails
