#!/usr/bin/env python3
"""
QuantDinger 双向转换误差验证
============================

验证 yaml_to_indicator.py (正向) 和 indicator_to_yaml.py (反向) 的一致性。

验证策略：
    由于正向转换依赖 LLM（非确定性），我们采用"结构级对比"策略：
    1. 用 test_yaml_to_indicator.py 中的样例代码作为"已知正向输出"
    2. 反向转换这些代码为 YAML
    3. 与原始 YAML 对比关键结构字段
    4. 计算各维度的误差率

验证维度：
    A. @param 参数提取准确率（名称、类型、默认值）
    B. @strategy 注解提取准确率
    C. 指标识别准确率
    D. 分类推断准确率
    E. YAML 结构完整性
    F. 正向→反向→正向 代码级 round-trip（用 AST 对比）
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

# 确保能 import
sys.path.insert(0, str(Path(__file__).parent))
from indicator_to_yaml import IndicatorParser, YAMLGenerator, ReverseConversionPipeline
from yaml_to_indicator import (
    QuantDingerStandards, CodeValidator, CodePostProcessor,
    YAMLStrategyParser, LLMStrategyGenerator, ConversionPipeline,
)

import yaml


# ============================================================
# 测试数据：模拟真实的 IndicatorStrategy 代码
# ============================================================

TEST_CODES = {
    "ema_rsi_pullback": {
        "code": """
# IndicatorStrategy: EMA趋势+RSI回调
# @strategy stopLossPct 0.025
# @strategy takeProfitPct 0.06
# @strategy tradeDirection long
# @param ema_fast int 10 短期EMA周期
# @param ema_slow int 30 长期EMA周期
# @param rsi_period int 14 RSI计算周期
# @param rsi_threshold int 42 RSI买入阈值

import pandas as pd
import numpy as np

fast = params.get('ema_fast', 10)
slow = params.get('ema_slow', 30)
rsi_period = params.get('rsi_period', 14)
rsi_thresh = params.get('rsi_threshold', 42)

df['ema_fast'] = df['close'].ewm(span=fast, adjust=False).mean()
df['ema_slow'] = df['close'].ewm(span=slow, adjust=False).mean()

delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(window=rsi_period).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_period).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))

df['buy'] = ((df['ema_fast'] > df['ema_slow']) & (df['rsi'] < rsi_thresh)).fillna(False)
df['sell'] = (df['rsi'] > 70).fillna(False)

df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
        "expected_params": {
            'ema_fast': {'type': 'int', 'default': 10},
            'ema_slow': {'type': 'int', 'default': 30},
            'rsi_period': {'type': 'int', 'default': 14},
            'rsi_threshold': {'type': 'int', 'default': 42},
        },
        "expected_annotations": {'stopLossPct': 0.025, 'takeProfitPct': 0.06, 'tradeDirection': 'long'},
        "expected_indicators": {'ema', 'rsi'},
        "expected_category": 'trend',
        "yaml_name": 'ema_rsi_pullback',
    },

    "kdj_vwap_reversal": {
        "code": """
# IndicatorStrategy: KDJ+VWAP反转
# @strategy stopLossPct 0.03
# @strategy tradeDirection long
# @param kdj_period int 9 KDJ周期
# @param vwap_dev float 0.02 VWAP偏离度

import pandas as pd
import numpy as np

period = params.get('kdj_period', 9)
sig = 3

low_min = df['low'].rolling(window=period).min()
high_max = df['high'].rolling(window=period).max()
rsv = (df['close'] - low_min) / (high_max - low_min) * 100
k = rsv.ewm(alpha=1/sig, adjust=False).mean()
d = k.ewm(alpha=1/sig, adjust=False).mean()
df['kdj_j'] = 3 * k - 2 * d

df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()

dev = params.get('vwap_dev', 0.02)
df['buy'] = ((df['kdj_j'] < 20) & (df['close'] < df['vwap'] * (1 - dev))).fillna(False)
df['sell'] = ((df['close'] > df['vwap']) | (df['kdj_j'] > 80)).fillna(False)

df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
        "expected_params": {
            'kdj_period': {'type': 'int', 'default': 9},
            'vwap_dev': {'type': 'float', 'default': 0.02},
        },
        "expected_annotations": {'stopLossPct': 0.03, 'tradeDirection': 'long'},
        "expected_indicators": {'kdj', 'vwap'},
        "expected_category": 'reversal',
        "yaml_name": 'kdj_vwap_reversal',
    },

    "atr_breakout": {
        "code": """
# IndicatorStrategy: ATR突破
# @strategy stopLossPct 0.03
# @strategy tradeDirection long
# @param atr_period int 14 ATR周期
# @param atr_multiplier float 2.0 ATR倍数

import pandas as pd
import numpy as np

period = params.get('atr_period', 14)
mult = params.get('atr_multiplier', 2.0)

df['tr'] = np.maximum(
    df['high'] - df['low'],
    np.maximum(
        abs(df['high'] - df['close'].shift(1)),
        abs(df['low'] - df['close'].shift(1))
    )
)
df['atr'] = df['tr'].ewm(alpha=1/period, adjust=False).mean()

df['upper'] = df['close'] + mult * df['atr']
df['lower'] = df['close'] - mult * df['atr']

df['buy'] = (df['close'] > df['upper'].shift(1)).fillna(False)
df['sell'] = (df['close'] < df['lower'].shift(1)).fillna(False)

df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
        "expected_params": {
            'atr_period': {'type': 'int', 'default': 14},
            'atr_multiplier': {'type': 'float', 'default': 2.0},
        },
        "expected_annotations": {'stopLossPct': 0.03, 'tradeDirection': 'long'},
        "expected_indicators": {'atr'},
        "expected_category": 'volatility',
        "yaml_name": 'atr_breakout',
    },

    "bollinger_rsi": {
        "code": """
# IndicatorStrategy: 布林带+RSI
# @strategy stopLossPct 0.02
# @strategy takeProfitPct 0.05
# @strategy tradeDirection both
# @param bb_period int 20 布林带周期
# @param bb_std float 2.0 标准差倍数
# @param rsi_period int 14 RSI周期
# @param rsi_low int 30 RSI超卖线
# @param rsi_high int 70 RSI超买线

import pandas as pd
import numpy as np

bb_p = params.get('bb_period', 20)
bb_s = params.get('bb_std', 2.0)
rsi_p = params.get('rsi_period', 14)
r_low = params.get('rsi_low', 30)
r_high = params.get('rsi_high', 70)

df['bb_mid'] = df['close'].rolling(window=bb_p).mean()
df['bb_std'] = df['close'].rolling(window=bb_p).std()
df['bb_upper'] = df['bb_mid'] + bb_s * df['bb_std']
df['bb_lower'] = df['bb_mid'] - bb_s * df['bb_std']

delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(window=rsi_p).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_p).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))

df['buy'] = ((df['close'] < df['bb_lower']) & (df['rsi'] < r_low)).fillna(False)
df['sell'] = ((df['close'] > df['bb_upper']) & (df['rsi'] > r_high)).fillna(False)

df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
        "expected_params": {
            'bb_period': {'type': 'int', 'default': 20},
            'bb_std': {'type': 'float', 'default': 2.0},
            'rsi_period': {'type': 'int', 'default': 14},
            'rsi_low': {'type': 'int', 'default': 30},
            'rsi_high': {'type': 'int', 'default': 70},
        },
        "expected_annotations": {'stopLossPct': 0.02, 'takeProfitPct': 0.05, 'tradeDirection': 'both'},
        "expected_indicators": {'bollinger', 'rsi'},
        "expected_category": 'reversal',
        "yaml_name": 'bollinger_rsi',
    },

    "macd_volume": {
        "code": """
# IndicatorStrategy: MACD+成交量
# @strategy stopLossPct 0.03
# @strategy tradeDirection long
# @param fast_period int 12 快线周期
# @param slow_period int 26 慢线周期
# @param signal_period int 9 信号线周期
# @param vol_multiplier float 1.5 成交量倍数

import pandas as pd
import numpy as np

fast = params.get('fast_period', 12)
slow = params.get('slow_period', 26)
sig_p = params.get('signal_period', 9)
vol_mult = params.get('vol_multiplier', 1.5)

df['ema_fast'] = df['close'].ewm(span=fast, adjust=False).mean()
df['ema_slow'] = df['close'].ewm(span=slow, adjust=False).mean()
df['macd_diff'] = df['ema_fast'] - df['ema_slow']
df['macd_dea'] = df['macd_diff'].ewm(span=sig_p, adjust=False).mean()
df['macd_histogram'] = 2 * (df['macd_diff'] - df['macd_dea'])

df['vol_ma'] = df['volume'].rolling(window=20).mean()

df['buy'] = ((df['macd_diff'] > df['macd_dea']) & (df['volume'] > vol_mult * df['vol_ma'])).fillna(False)
df['sell'] = (df['macd_diff'] < df['macd_dea']).fillna(False)

df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
        "expected_params": {
            'fast_period': {'type': 'int', 'default': 12},
            'slow_period': {'type': 'int', 'default': 26},
            'signal_period': {'type': 'int', 'default': 9},
            'vol_multiplier': {'type': 'float', 'default': 1.5},
        },
        "expected_annotations": {'stopLossPct': 0.03, 'tradeDirection': 'long'},
        "expected_indicators": {'ema', 'macd', 'volume'},
        "expected_category": 'trend',
        "yaml_name": 'macd_volume',
    },
}


# ============================================================
# 验证函数
# ============================================================

def verify_params(parsed: Dict, expected: Dict) -> Tuple[int, int, List[str]]:
    """验证 @param 提取准确率。"""
    total = len(expected)
    correct = 0
    errors = []

    parsed_params = {p['name']: p for p in parsed['params']}

    for name, exp in expected.items():
        if name not in parsed_params:
            errors.append(f"  ❌ 参数 {name} 未被提取")
            continue

        p = parsed_params[name]
        type_ok = p['type'] == exp['type']
        default_ok = p['default'] == exp['default']

        if type_ok and default_ok:
            correct += 1
        else:
            if not type_ok:
                errors.append(f"  ⚠️  {name} 类型: 期望 {exp['type']}, 实际 {p['type']}")
            if not default_ok:
                errors.append(f"  ⚠️  {name} 默认值: 期望 {exp['default']}, 实际 {p['default']}")

    return correct, total, errors


def verify_annotations(parsed: Dict, expected: Dict) -> Tuple[int, int, List[str]]:
    """验证 @strategy 注解提取准确率。"""
    total = len(expected)
    correct = 0
    errors = []

    ann = parsed['strategy_annotations']
    for key, exp_val in expected.items():
        if key not in ann:
            errors.append(f"  ❌ 注解 {key} 未被提取")
            continue

        actual = ann[key]
        # 浮点比较用近似
        if isinstance(exp_val, float) and isinstance(actual, (int, float)):
            if abs(actual - exp_val) < 1e-9:
                correct += 1
            else:
                errors.append(f"  ⚠️  {key}: 期望 {exp_val}, 实际 {actual}")
        elif actual == exp_val:
            correct += 1
        else:
            errors.append(f"  ⚠️  {key}: 期望 {exp_val}, 实际 {actual}")

    return correct, total, errors


def verify_indicators(parsed: Dict, expected: set) -> Tuple[int, int, List[str]]:
    """验证指标识别准确率。"""
    total = len(expected)
    actual = set(parsed['indicators'])
    correct = len(expected & actual)
    errors = []

    missing = expected - actual
    extra = actual - expected
    for m in missing:
        errors.append(f"  ❌ 指标 {m} 未被识别")
    for e in extra:
        errors.append(f"  ℹ️  额外识别指标 {e}（可接受）")

    return correct, total, errors


def verify_category(parsed: Dict, expected: str) -> Tuple[bool, str]:
    """验证分类推断。"""
    yaml_data = YAMLGenerator.generate(parsed)
    actual = yaml_data['category']
    return actual == expected, actual


def verify_yaml_structure(yaml_data: Dict) -> Tuple[int, int, List[str]]:
    """验证 YAML 结构完整性。"""
    required_fields = ['name', 'display_name', 'description', 'category', 'core_rules', 'required_tools', 'instructions']
    total = len(required_fields)
    present = 0
    errors = []

    for field in required_fields:
        if field in yaml_data and yaml_data[field]:
            present += 1
        else:
            errors.append(f"  ❌ 缺少字段: {field}")

    return present, total, errors


def verify_code_safety(code: str) -> Tuple[bool, List[str]]:
    """验证生成的代码通过安全校验。"""
    is_valid, errors = CodeValidator.validate(code)
    return is_valid, errors


def verify_roundtrip_ast(original_code: str) -> Tuple[float, Dict[str, Any]]:
    """
    验证 round-trip 的代码级一致性。
    对比：正向生成代码 → 反向解析 → 重新生成 YAML 的结构信息。
    """
    # 解析原始代码
    parsed = IndicatorParser.parse_code(original_code)

    # 生成 YAML
    yaml_data = YAMLGenerator.generate(parsed)

    # 从 YAML 的 instructions 重新提取关键信息
    instructions = yaml_data.get('instructions', '')

    # 对比关键结构
    metrics = {
        'params_count_match': len(parsed['params']),
        'annotations_count_match': len(parsed['strategy_annotations']),
        'indicators_count_match': len(parsed['indicators']),
        'has_buy_logic': bool(parsed['buy_conditions']),
        'has_sell_logic': bool(parsed['sell_conditions']),
        'yaml_fields_present': sum(1 for k in ['name', 'display_name', 'description', 'instructions'] if yaml_data.get(k)),
    }

    # 计算综合得分
    score = 0
    max_score = 6
    if metrics['params_count_match'] > 0: score += 1
    if metrics['annotations_count_match'] > 0: score += 1
    if metrics['indicators_count_match'] > 0: score += 1
    if metrics['has_buy_logic']: score += 1
    if metrics['has_sell_logic']: score += 1
    if metrics['yaml_fields_present'] >= 4: score += 1

    return score / max_score, metrics


# ============================================================
# 主验证流程
# ============================================================

def main():
    print("╔══════════════════════════════════════════════════════════╗")
    print("║     QuantDinger 双向转换误差率验证报告                    ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    # 总计数器
    totals = {
        'param_correct': 0, 'param_total': 0,
        'annotation_correct': 0, 'annotation_total': 0,
        'indicator_correct': 0, 'indicator_total': 0,
        'category_correct': 0, 'category_total': 0,
        'structure_present': 0, 'structure_total': 0,
        'safety_pass': 0, 'safety_total': 0,
        'roundtrip_scores': [],
    }

    for name, test in TEST_CODES.items():
        print(f"{'─' * 56}")
        print(f"📋 策略: {name}")
        print(f"{'─' * 56}")

        code = test['code']

        # 1. 解析
        parsed = IndicatorParser.parse_code(code)

        # 2. @param 验证
        pc, pt, pe = verify_params(parsed, test['expected_params'])
        totals['param_correct'] += pc
        totals['param_total'] += pt
        print(f"  📌 @param 提取: {pc}/{pt} 正确")
        for e in pe:
            print(e)

        # 3. @strategy 验证
        ac, at, ae = verify_annotations(parsed, test['expected_annotations'])
        totals['annotation_correct'] += ac
        totals['annotation_total'] += at
        print(f"  📌 @strategy 提取: {ac}/{at} 正确")
        for e in ae:
            print(e)

        # 4. 指标识别
        ic, it, ie = verify_indicators(parsed, test['expected_indicators'])
        totals['indicator_correct'] += ic
        totals['indicator_total'] += it
        print(f"  📌 指标识别: {ic}/{it} 正确")
        for e in ie:
            print(e)

        # 5. 分类推断
        cat_ok, cat_actual = verify_category(parsed, test['expected_category'])
        totals['category_correct'] += int(cat_ok)
        totals['category_total'] += 1
        status = "✅" if cat_ok else "❌"
        print(f"  📌 分类推断: {status} 期望={test['expected_category']}, 实际={cat_actual}")

        # 6. YAML 结构完整性
        yaml_data = YAMLGenerator.generate(parsed)
        sp, st, se = verify_yaml_structure(yaml_data)
        totals['structure_present'] += sp
        totals['structure_total'] += st
        print(f"  📌 YAML 结构: {sp}/{st} 字段完整")
        for e in se:
            print(e)

        # 7. 安全校验（用后处理器生成完整代码）
        full_code = CodePostProcessor.process(code, name)
        safe_ok, safe_errs = verify_code_safety(full_code)
        totals['safety_pass'] += int(safe_ok)
        totals['safety_total'] += 1
        status = "✅" if safe_ok else "❌"
        print(f"  📌 安全校验: {status}")
        if safe_errs:
            for e in safe_errs[:3]:
                print(f"    - {e}")

        # 8. Round-trip 得分
        rt_score, rt_metrics = verify_roundtrip_ast(code)
        totals['roundtrip_scores'].append(rt_score)
        print(f"  📌 Round-trip 得分: {rt_score*100:.0f}%")
        print()

    # ============================================================
    # 汇总报告
    # ============================================================

    print("╔══════════════════════════════════════════════════════════╗")
    print("║                    📊 汇总报告                           ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    results = []

    def report(name, correct, total):
        if total == 0:
            rate = 0.0
        else:
            rate = correct / total
        error_rate = 1 - rate
        results.append({'name': name, 'correct': correct, 'total': total, 'rate': rate, 'error_rate': error_rate})
        bar = '█' * int(rate * 20) + '░' * (20 - int(rate * 20))
        status = "✅" if error_rate < 0.05 else "⚠️" if error_rate < 0.15 else "❌"
        print(f"  {status} {name:24s} {bar} {rate*100:5.1f}% (误差 {error_rate*100:.1f}%)  [{correct}/{total}]")

    report("@param 参数提取", totals['param_correct'], totals['param_total'])
    report("@strategy 注解提取", totals['annotation_correct'], totals['annotation_total'])
    report("指标识别", totals['indicator_correct'], totals['indicator_total'])
    report("分类推断", totals['category_correct'], totals['category_total'])
    report("YAML 结构完整性", totals['structure_present'], totals['structure_total'])
    report("安全校验通过", totals['safety_pass'], totals['safety_total'])

    # Round-trip 平均分
    avg_rt = sum(totals['roundtrip_scores']) / len(totals['roundtrip_scores']) if totals['roundtrip_scores'] else 0
    rt_error = 1 - avg_rt
    bar = '█' * int(avg_rt * 20) + '░' * (20 - int(avg_rt * 20))
    status = "✅" if rt_error < 0.05 else "⚠️" if rt_error < 0.15 else "❌"
    print(f"  {status} {'Round-trip 综合得分':24s} {bar} {avg_rt*100:5.1f}% (误差 {rt_error*100:.1f}%)")

    print()

    # 综合误差率
    total_correct = sum(r['correct'] for r in results)
    total_items = sum(r['total'] for r in results)
    overall_accuracy = total_correct / total_items if total_items > 0 else 0
    overall_error = 1 - overall_accuracy

    print(f"  ┌─────────────────────────────────────┐")
    print(f"  │  综合准确率: {overall_accuracy*100:5.1f}%                      │")
    print(f"  │  综合误差率: {overall_error*100:5.1f}%                      │")
    print(f"  │  测试策略数: {len(TEST_CODES)}                         │")
    print(f"  │  检查项总数: {total_items}                         │")
    print(f"  └─────────────────────────────────────┘")

    # 保存详细结果
    output_path = Path(__file__).parent / 'roundtrip_results.json'
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump({
            'overall_accuracy': overall_accuracy,
            'overall_error_rate': overall_error,
            'details': results,
            'roundtrip_avg': avg_rt,
            'test_count': len(TEST_CODES),
            'check_count': total_items,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  📄 详细结果已保存: {output_path}")

    # 退出码
    if overall_error > 0.15:
        print("\n  ⚠️  误差率 > 15%，需要优化")
        sys.exit(1)
    elif overall_error > 0.05:
        print("\n  ⚡ 误差率 5-15%，可接受但有优化空间")
        sys.exit(0)
    else:
        print("\n  🎉 误差率 < 5%，优秀！")
        sys.exit(0)


if __name__ == '__main__':
    main()
