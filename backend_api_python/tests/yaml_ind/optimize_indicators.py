#!/usr/bin/env python3
"""
指标关键词优化器
================
1. 从 16 个 YAML instructions 中穷举所有指标相关词汇
2. 用多种 pattern 分别测试命中率
3. 选出最优 pattern 组合
4. 输出标准化映射表
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

import yaml


# 16 个 YAML 的 instructions 内容
def load_instructions() -> Dict[str, str]:
    yaml_dir = Path(__file__).parent / "backend_api_python" / "strategies"
    data = {}
    for yf in sorted(yaml_dir.glob('*.yaml')):
        with open(yf, 'r', encoding='utf-8') as f:
            d = yaml.safe_load(f)
        data[yf.stem] = d.get('instructions', '')
    return data


# 穷举所有可能的指标关键词 pattern
CANDIDATE_PATTERNS: Dict[str, List[str]] = {
    'ema': [
        r'\bEMA\b', r'\bema\b', r'指数移动平均', r'指数均线',
        r'EMA\(', r'ewm\(.*span', r'ema_fast', r'ema_slow',
        r'\.ewm\(', r'指数平滑',
    ],
    'sma': [
        r'\bSMA\b', r'\bsma\b', r'\bMA\d', r'\bMA\b', r'\bma\b',
        r'均线', r'简单移动平均', r'简单均线', r'移动平均线',
        r'MA5', r'MA10', r'MA20', r'MA60', r'MA120', r'MA250',
        r'\.rolling\(.*\)\.mean', r'均线系统', r'多头排列', r'空头排列',
        r'金叉', r'死叉', r'乖离率',
    ],
    'rsi': [
        r'\bRSI\b', r'\brsi\b', r'相对强弱', r'相对强弱指数',
        r'RSI\(', r'超买', r'超卖', r'RSI\s*\d',
    ],
    'macd': [
        r'\bMACD\b', r'\bmacd\b', r'DIF', r'DEA', r'MACD柱',
        r'红柱', r'绿柱', r'柱面积', r'信号线',
        r'macd_diff', r'macd_dea', r'histogram', r'快线.*慢线',
        r'动量转向',
    ],
    'kdj': [
        r'\bKDJ\b', r'\bkdj\b', r'\bJ值\b', r'\bRSV\b', r'\brsv\b',
        r'随机指标', r'K值', r'D值', r'J\s*值',
        r'kdj_j', r'kdj_period',
    ],
    'bollinger': [
        r'布林', r'\bBB\b', r'\bbb\b', r'Bollinger', r'bollinger',
        r'上轨', r'下轨', r'中轨', r'标准差', r'带宽',
        r'bb_upper', r'bb_lower', r'bb_mid', r'bb_std',
        r'布林带宽度', r'BOLL',
    ],
    'atr': [
        r'\bATR\b', r'\batr\b', r'真实波幅', r'平均真实波幅',
        r'波动率', r'ATR\(', r'atr_period', r'波幅',
        r'\btr\b.*maximum', r'TR\b',
    ],
    'vwap': [
        r'\bVWAP\b', r'\bvwap\b', r'成交量加权', r'成交量加权均价',
        r'VWAP\(', r'vwap_dev', r'均值回归',
    ],
    'volume': [
        r'成交量', r'量比', r'放量', r'缩量', r'量能',
        r'\bvolume\b', r'\bvol\b', r'换手率', r'成交额',
        r'量价', r'天量', r'地量', r'量价关系',
        r'底部放量', r'顶部放量',
    ],
    'supertrend': [
        r'supertrend', r'SuperTrend', r'超级趋势', r'上轨线', r'下轨线',
    ],
    'donchian': [
        r'donchian', r'Donchian', r'唐奇安', r'通道突破',
        r'最高.*最低', r'上轨.*下轨',
    ],
}


def test_pattern(pattern: str, texts: Dict[str, str]) -> Tuple[int, List[str]]:
    """测试单个 pattern 在所有 YAML 中的命中情况。"""
    hits = []
    for name, instr in texts.items():
        if re.search(pattern, instr, re.IGNORECASE):
            hits.append(name)
    return len(hits), hits


def find_best_patterns(texts: Dict[str, str]) -> Dict[str, Dict]:
    """为每个指标找到最优 pattern 组合。"""
    results = {}

    for indicator, patterns in CANDIDATE_PATTERNS.items():
        pattern_results = []
        for pat in patterns:
            count, hits = test_pattern(pat, texts)
            pattern_results.append({
                'pattern': pat,
                'hit_count': count,
                'hit_files': hits,
            })

        # 按命中数排序
        pattern_results.sort(key=lambda x: -x['hit_count'])

        # 找最优组合：逐步添加 pattern，看新增命中数
        covered = set()
        best_combo = []
        for pr in pattern_results:
            if pr['hit_count'] == 0:
                continue
            new_hits = set(pr['hit_files']) - covered
            if new_hits:
                best_combo.append({
                    'pattern': pr['pattern'],
                    'new_hits': list(new_hits),
                    'total_hits': pr['hit_count'],
                })
                covered |= set(pr['hit_files'])

        results[indicator] = {
            'all_patterns': pattern_results,
            'best_combo': best_combo,
            'total_covered': len(covered),
            'covered_files': sorted(covered),
        }

    return results


def main():
    print("📊 指标关键词优化分析")
    print("=" * 60)

    texts = load_instructions()
    print(f"加载 {len(texts)} 个策略文件\n")

    results = find_best_patterns(texts)

    # 输出每个指标的分析
    for indicator, data in results.items():
        total = len(texts)
        covered = data['total_covered']
        rate = covered / total * 100

        print(f"{'─' * 56}")
        print(f"🔍 {indicator}  覆盖: {covered}/{total} ({rate:.0f}%)")

        # 最优组合
        if data['best_combo']:
            print(f"  最优 pattern 组合:")
            for i, combo in enumerate(data['best_combo'], 1):
                print(f"    {i}. {combo['pattern']:30s}  命中 {combo['total_hits']} 个  "
                      f"新增: {combo['new_hits']}")
        else:
            print(f"  ⚠️  无有效 pattern")

        # 未覆盖的文件
        all_files = set(texts.keys())
        uncovered = all_files - set(data['covered_files'])
        if uncovered:
            print(f"  未覆盖: {sorted(uncovered)}")

    # 生成最优 pattern 映射表
    print(f"\n{'═' * 56}")
    print(f"📋 最终推荐的 pattern 映射表")
    print(f"{'═' * 56}\n")

    optimal = {}
    for indicator, data in results.items():
        patterns = [c['pattern'] for c in data['best_combo']]
        if patterns:
            optimal[indicator] = patterns
            print(f"  '{indicator}': [")
            for p in patterns:
                print(f"      r'{p}',")
            print(f"  ],")
    print()

    # 保存结果
    output = {
        'optimal_patterns': optimal,
        'detailed_analysis': {
            ind: {
                'coverage': f"{data['total_covered']}/{len(texts)}",
                'covered_files': data['covered_files'],
                'best_combo': [
                    {'pattern': c['pattern'], 'total_hits': c['total_hits']}
                    for c in data['best_combo']
                ],
            }
            for ind, data in results.items()
        },
    }

    out_path = Path(__file__).parent / 'indicator_optimization.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"  📄 详细结果: {out_path}")

    # 验证：用最优 pattern 重新检测所有 YAML，计算覆盖率
    print(f"\n{'═' * 56}")
    print(f"🧪 用最优 pattern 重新验证")
    print(f"{'═' * 56}\n")

    total_checks = 0
    total_hits = 0
    for name, instr in texts.items():
        detected = []
        for indicator, patterns in optimal.items():
            for pat in patterns:
                if re.search(pat, instr, re.IGNORECASE):
                    detected.append(indicator)
                    break
        detected = sorted(set(detected))
        total_checks += 1
        if detected:
            total_hits += 1
        print(f"  {name:30s} → {detected}")

    print(f"\n  覆盖率: {total_hits}/{total_checks} ({total_hits/total_checks*100:.0f}%)")


if __name__ == '__main__':
    main()
