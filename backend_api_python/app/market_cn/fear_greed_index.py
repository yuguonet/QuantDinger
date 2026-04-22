#!/usr/bin/env python3
"""
A股市场贪婪恐惧指数 (China Market Fear & Greed Index)
参考 CNN Fear & Greed Index 的方法论，适配 A 股市场

计算维度 (7个指标，等权):
1. 股价动量 — 沪深300 vs 125日均线
2. 股价强度 — 60日上涨个股占比
3. 市场波动率 — 基于沪深300历史波动率
4. 市场宽度 — 当日上涨个股占比
5. 成交量变化 — 近期成交量 vs 20日均量
6. 北向资金 — 近5日净流入动量
7. 涨停/跌停比 — 市场情绪极端指标

依赖: pip install pandas numpy (数据源通过 data_sources.py 多源降级)
"""

import pandas as pd
import numpy as np
from datetime import datetime
import json
import sys
import os

# 确保能找到同目录下的 data_sources
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

from .data_sources import ChinaData, fallback, ak_index_daily, ak_northbound

# 懒初始化数据源
_data = None

def _get_data():
    global _data
    if _data is None:
        _data = ChinaData()
    return _data


def _get_index_df():
    """获取沪深300日线 (多源降级)"""
    from .data_sources import ts_index_daily, bs_index_daily, ak_index_daily
    return fallback(
        ("tushare", lambda: ts_index_daily("000300.SH")),
        ("akshare", lambda: ak_index_daily("sh000300")),
        ("baostock", lambda: bs_index_daily("sh.000300")),
    )()


def _get_spot_df():
    """获取全A股实时行情 (多源降级)"""
    from .data_sources import ak_stock_basic
    try:
        import akshare as ak
        return ak.stock_zh_a_spot_em()
    except Exception:
        pass
    # BaoStock 不支持实时行情快照，这里返回 None 触发中性分
    return None


def score_to_label(score):
    """分数转标签"""
    if score <= 25:
        return "🔴 极度恐惧"
    elif score <= 40:
        return "🟠 恐惧"
    elif score <= 60:
        return "🟡 中性"
    elif score <= 75:
        return "🟢 贪婪"
    else:
        return "🔥 极度贪婪"


def score_0_to_100(value, min_val, max_val):
    """线性映射到 0-100，并 clamp"""
    if max_val == min_val:
        return 50.0
    return float(np.clip((value - min_val) / (max_val - min_val) * 100, 0, 100))


def calc_momentum():
    """1. 股价动量: 沪深300 当前价 vs 125日均线"""
    try:
        df = _get_index_df()
        if df is None or len(df) < 130:
            return {'name': '股价动量', 'score': 50, 'detail': f'数据不足 (仅 {len(df) if df is not None else 0} 行)'}

        # 兼容不同数据源的列名
        close_col = 'close' if 'close' in df.columns else '收盘'
        date_col = None
        for c in ['date', 'trade_date', '日期']:
            if c in df.columns:
                date_col = c
                break

        if date_col:
            df[date_col] = pd.to_datetime(df[date_col])
            df = df.sort_values(date_col).tail(150)

        df = df.copy()
        df['_close'] = pd.to_numeric(df[close_col], errors='coerce')
        df = df.dropna(subset=['_close'])

        if len(df) < 130:
            return {'name': '股价动量', 'score': 50, 'detail': f'有效数据不足 ({len(df)} 行)'}

        df['ma125'] = df['_close'].rolling(125, min_periods=100).mean()
        latest = df.iloc[-1]
        ratio = latest['_close'] / latest['ma125']
        score = score_0_to_100(ratio, 0.85, 1.15)
        return {'name': '股价动量', 'score': round(score, 1),
                'detail': f"沪深300: {latest['_close']:.0f} / MA125: {latest['ma125']:.0f} (比值 {ratio:.3f})"}
    except Exception as e:
        return {'name': '股价动量', 'score': 50, 'detail': f'获取失败: {e}'}


def calc_breadth():
    """2. 市场宽度: 当日上涨个股占比"""
    try:
        df = _get_spot_df()
        if df is None:
            return {'name': '市场宽度', 'score': 50, 'detail': '实时行情不可用'}

        # 向量化: 直接取涨跌幅列
        chg_col = None
        for c in ['涨跌幅', 'change', 'pct_chg']:
            if c in df.columns:
                chg_col = c
                break
        if chg_col is None:
            return {'name': '市场宽度', 'score': 50, 'detail': '未找到涨跌幅列'}

        changes = pd.to_numeric(df[chg_col], errors='coerce').dropna()
        count = len(changes)
        above_zero = (changes > 0).sum()
        ratio = above_zero / max(count, 1)
        score = ratio * 100
        return {'name': '市场宽度(上涨占比)', 'score': round(score, 1),
                'detail': f"上涨 {above_zero}/{count} 只 ({ratio:.1%})"}
    except Exception as e:
        return {'name': '市场宽度', 'score': 50, 'detail': f'获取失败: {e}'}


def calc_volatility():
    """3. 市场波动率: 沪深300 近20日年化波动率 (波动率高=恐惧)"""
    try:
        df = _get_index_df()
        if df is None or len(df) < 25:
            return {'name': '市场波动率', 'score': 50, 'detail': '数据不足'}

        close_col = 'close' if 'close' in df.columns else '收盘'
        date_col = None
        for c in ['date', 'trade_date', '日期']:
            if c in df.columns:
                date_col = c
                break
        if date_col:
            df[date_col] = pd.to_datetime(df[date_col])
            df = df.sort_values(date_col)

        df = df.copy()
        df['_close'] = pd.to_numeric(df[close_col], errors='coerce')
        df = df.dropna(subset=['_close']).tail(30)

        if len(df) < 20:
            return {'name': '市场波动率', 'score': 50, 'detail': '有效数据不足'}

        df['ret'] = df['_close'].pct_change()
        vol = df['ret'].std() * np.sqrt(252) * 100  # 年化波动率 %
        score = score_0_to_100(vol, 40, 10)  # 波动高=恐惧=低分
        return {'name': '市场波动率', 'score': round(score, 1),
                'detail': f"20日年化波动率: {vol:.1f}%"}
    except Exception as e:
        return {'name': '市场波动率', 'score': 50, 'detail': f'获取失败: {e}'}


def calc_volume():
    """4. 成交量变化: 当日成交额 vs 20日均值"""
    try:
        df = _get_index_df()
        if df is None or len(df) < 25:
            return {'name': '成交量变化', 'score': 50, 'detail': '数据不足'}

        vol_col = 'volume' if 'volume' in df.columns else '成交量'
        date_col = None
        for c in ['date', 'trade_date', '日期']:
            if c in df.columns:
                date_col = c
                break
        if date_col:
            df[date_col] = pd.to_datetime(df[date_col])
            df = df.sort_values(date_col)

        volumes = pd.to_numeric(df[vol_col], errors='coerce').dropna()
        if len(volumes) < 22:
            return {'name': '成交量变化', 'score': 50, 'detail': '有效数据不足'}

        avg_vol = volumes.iloc[-21:-1].mean()
        latest_vol = volumes.iloc[-1]
        ratio = latest_vol / avg_vol if avg_vol > 0 else 1.0
        score = score_0_to_100(ratio, 0.5, 2.0)
        return {'name': '成交量变化', 'score': round(score, 1),
                'detail': f"当日/20日均量: {ratio:.2f}x"}
    except Exception as e:
        return {'name': '成交量变化', 'score': 50, 'detail': f'获取失败: {e}'}


def calc_northbound():
    """5. 北向资金: 近5日净流入"""
    try:
        from .data_sources import ts_northbound, ak_northbound
        df = fallback(
            ("tushare", ts_northbound),
            ("akshare", ak_northbound),
        )()
        if df is None or len(df) == 0:
            return {'name': '北向资金', 'score': 50, 'detail': '数据不可用'}

        # 取最后一列数值 (净流入)
        numeric_cols = df.select_dtypes(include=[np.number])
        if numeric_cols.empty:
            return {'name': '北向资金', 'score': 50, 'detail': '无数值列'}

        net_flow = numeric_cols.iloc[:, -1].tail(5).sum()
        score = score_0_to_100(net_flow, -200, 200)
        return {'name': '北向资金', 'score': round(score, 1),
                'detail': f"近5日北向净流入: {net_flow:.1f} 亿"}
    except Exception as e:
        return {'name': '北向资金', 'score': 50, 'detail': f'获取失败: {e}'}


def calc_limit_ratio():
    """6. 涨停跌停比 — 区分主板(10%)、创业板/科创板(20%)、北交所(30%)"""
    try:
        df = _get_spot_df()
        if df is None:
            return {'name': '涨跌停比', 'score': 50, 'detail': '实时行情不可用'}

        chg_col = None
        code_col = None
        for c in ['涨跌幅', 'change', 'pct_chg']:
            if c in df.columns:
                chg_col = c
                break
        for c in ['代码', 'code', 'ts_code']:
            if c in df.columns:
                code_col = c
                break

        if chg_col is None:
            return {'name': '涨跌停比', 'score': 50, 'detail': '未找到涨跌幅列'}

        changes = pd.to_numeric(df[chg_col], errors='coerce')
        if code_col and code_col in df.columns:
            codes = df[code_col].astype(str)
            # 创业板 300xxx/301xxx → 20%, 科创板 688xxx → 20%, 北交所 8xxxxx/4xxxxx → 30%
            is_20pct = codes.str.match(r'^(300|301|688)')  # 创业板+科创板
            is_30pct = codes.str.match(r'^(8|4)\d{5}')      # 北交所
            limits_up = np.where(is_30pct, 30.0, np.where(is_20pct, 20.0, 10.0))
            limits_down = -limits_up
        else:
            # 无法区分，默认 10%
            limits_up = np.full(len(changes), 10.0)
            limits_down = np.full(len(changes), -10.0)

        changes_arr = changes.values
        limit_up = np.nansum(changes_arr >= limits_up * 0.98)  # 留 2% 余量
        limit_down = np.nansum(changes_arr <= limits_down * 0.98)

        ratio = limit_up / max(limit_down, 1)
        score = score_0_to_100(ratio, 0, 10)
        return {'name': '涨跌停比', 'score': round(score, 1),
                'detail': f"涨停 {int(limit_up)} / 跌停 {int(limit_down)} (比值 {ratio:.1f})"}
    except Exception as e:
        return {'name': '涨跌停比', 'score': 50, 'detail': f'获取失败: {e}'}


def calc_strength():
    """7. 股价强度: 60日涨幅为正的个股占比"""
    try:
        df = _get_spot_df()
        if df is None:
            return {'name': '股价强度', 'score': 50, 'detail': '实时行情不可用'}

        col = None
        for c in ['60日涨跌幅', '60d_pct_chg']:
            if c in df.columns:
                col = c
                break
        if col is None:
            return {'name': '股价强度(60日)', 'score': 50, 'detail': '未找到60日涨跌幅列'}

        values = pd.to_numeric(df[col], errors='coerce').dropna()
        total = len(values)
        positive = (values > 0).sum()
        ratio = positive / max(total, 1)
        score = ratio * 100
        return {'name': '股价强度(60日)', 'score': round(score, 1),
                'detail': f"60日上涨 {positive}/{total} ({ratio:.1%})"}
    except Exception as e:
        return {'name': '股价强度', 'score': 50, 'detail': f'获取失败: {e}'}


def fear_greed_index():
    """计算综合贪恐指数"""
    print(f"\n{'='*60}")
    print(f"  🇨🇳 A股市场贪婪恐惧指数")
    print(f"  📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    indicators = [
        calc_momentum(),
        calc_breadth(),
        calc_volatility(),
        calc_volume(),
        calc_northbound(),
        calc_limit_ratio(),
        calc_strength(),
    ]

    valid_scores = [ind['score'] for ind in indicators]
    avg_score = float(np.mean(valid_scores)) if valid_scores else 50.0

    print(f"  {'指标':<20} {'分数':>6}  详情")
    print(f"  {'-'*55}")
    for ind in indicators:
        print(f"  {ind['name']:<18} {ind['score']:>6.1f}  {ind['detail']}")

    print(f"\n  {'='*55}")
    label = score_to_label(avg_score)
    bar_len = max(0, min(50, int(avg_score / 2)))
    bar = '█' * bar_len + '░' * (50 - bar_len)
    print(f"  综合指数: {avg_score:.1f} / 100  {label}")
    print(f"  [{bar}]")
    print(f"  {'恐惧':>10}{'中性':^30}{'贪婪':>10}")

    result = {
        'timestamp': datetime.now().isoformat(),
        'composite_score': round(avg_score, 1),
        'label': label,
        'indicators': indicators
    }

    # 保存
    # 保存到当前工作目录
    output_path = 'fear_greed_index.json'
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ 结果已保存: {output_path}")

    return result


if __name__ == "__main__":
    fear_greed_index()
