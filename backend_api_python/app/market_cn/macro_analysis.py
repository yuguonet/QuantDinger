#!/usr/bin/env python3
"""
国内宏观经济数据看板
数据源: AKShare (免费开源财经数据接口)
依赖: pip install akshare pandas tabulate
"""

import akshare as ak
import pandas as pd
from datetime import datetime
import json

pd.set_option('display.max_rows', 50)
pd.set_option('display.width', 120)
pd.set_option('display.unicode.east_asian_width', True)


def get_gdp_data():
    """GDP 季度数据"""
    print("\n📊 GDP 季度同比增速")
    print("=" * 60)
    try:
        df = ak.macro_china_gdp_yearly()
        print(df.tail(10).to_string(index=False))
        return df
    except Exception as e:
        print(f"  ⚠️ 获取失败: {e}")
        return None


def get_cpi_data():
    """CPI 月度数据"""
    print("\n📊 CPI 同比 (%)")
    print("=" * 60)
    try:
        df = ak.macro_china_cpi_monthly()
        print(df.tail(12).to_string(index=False))
        return df
    except Exception as e:
        print(f"  ⚠️ 获取失败: {e}")
        return None


def get_ppi_data():
    """PPI 月度数据"""
    print("\n📊 PPI 同比 (%)")
    print("=" * 60)
    try:
        df = ak.macro_china_ppi_yearly()
        print(df.tail(12).to_string(index=False))
        return df
    except Exception as e:
        print(f"  ⚠️ 获取失败: {e}")
        return None


def get_pmi_data():
    """PMI 月度数据"""
    print("\n📊 制造业 PMI")
    print("=" * 60)
    try:
        df = ak.macro_china_pmi()
        print(df.tail(12).to_string(index=False))
        return df
    except Exception as e:
        print(f"  ⚠️ 获取失败: {e}")
        return None


def get_m2_data():
    """M2 货币供应量"""
    print("\n📊 M2 同比增速 (%)")
    print("=" * 60)
    try:
        df = ak.macro_china_m2_yearly()
        print(df.tail(12).to_string(index=False))
        return df
    except Exception as e:
        print(f"  ⚠️ 获取失败: {e}")
        return None


def get_social_financing():
    """社会融资规模"""
    print("\n📊 社会融资规模增量 (亿元)")
    print("=" * 60)
    try:
        df = ak.macro_china_shrzgm()
        print(df.tail(12).to_string(index=False))
        return df
    except Exception as e:
        print(f"  ⚠️ 获取失败: {e}")
        return None


def get_trade_data():
    """进出口数据"""
    print("\n📊 进出口贸易差额 (亿美元)")
    print("=" * 60)
    try:
        df = ak.macro_china_trade_balance()
        print(df.tail(12).to_string(index=False))
        return df
    except Exception as e:
        print(f"  ⚠️ 获取失败: {e}")
        return None


def get_interest_rate():
    """LPR 利率"""
    print("\n📊 LPR 贷款市场报价利率 (%)")
    print("=" * 60)
    try:
        df = ak.macro_china_lpr()
        print(df.tail(6).to_string(index=False))
        return df
    except Exception as e:
        print(f"  ⚠️ 获取失败: {e}")
        return None


def macro_dashboard():
    """运行完整宏观数据看板"""
    print(f"\n{'='*60}")
    print(f"  🇨🇳 中国宏观经济数据看板")
    print(f"  📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    results = {}
    results['gdp'] = get_gdp_data()
    results['cpi'] = get_cpi_data()
    results['ppi'] = get_ppi_data()
    results['pmi'] = get_pmi_data()
    results['m2'] = get_m2_data()
    results['social_financing'] = get_social_financing()
    results['trade'] = get_trade_data()
    results['lpr'] = get_interest_rate()

    # 保存到 JSON
    timestamp = datetime.now().strftime('%Y%m%d_%H%M')
    output_file = f"macro_data_{timestamp}.json"
    summary = {}
    for key, df in results.items():
        if df is not None and len(df) > 0:
            summary[key] = {
                'latest_rows': min(5, len(df)),
                'columns': list(df.columns),
                'total_records': len(df)
            }
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 数据摘要已保存: {output_file}")

    return results


if __name__ == "__main__":
    macro_dashboard()
