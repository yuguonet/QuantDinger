#!/usr/bin/env python3
"""
分类推断改进测试
================
验证 indicator_to_yaml.py 的 _infer_category 能正确推断：
- trend: 趋势跟随
- reversal: 反转/均值回归
- framework: 框架类（缠论、箱体、情绪、波浪）
- pattern: 形态类（一阳夹三阴等）

测试方法：用 16 个真实 YAML 策略的名称和模拟买卖条件，
         验证推断结果是否与原始 category 一致。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from yaml_indicator.indicator_to_yaml import YAMLGenerator

PASS = 0
FAIL = 0

def check(name, expected, actual):
    global PASS, FAIL
    if expected == actual:
        PASS += 1
        print(f"  ✅ {name}: {actual}")
    else:
        FAIL += 1
        print(f"  ❌ {name}: expected={expected}, got={actual}")


def test_framework_strategies():
    """测试框架类策略的识别"""
    print("\n═══ Framework 策略 ═══")

    # 缠论
    check("缠论", "framework", YAMLGenerator._infer_category(
        indicators=['macd'],
        buy_conditions="df['close'] > df['macd_hist'].shift(1)",
        sell_conditions="df['macd_hist'] < 0",
        strategy_name="缠论 Chan Theory"
    ))

    # 箱体震荡
    check("箱体震荡", "framework", YAMLGenerator._infer_category(
        indicators=['ema', 'volume'],
        buy_conditions="df['close'] < df['ema'] * 0.97",
        sell_conditions="df['close'] > df['ema'] * 1.03",
        strategy_name="箱体震荡 Box Oscillation"
    ))

    # 情绪周期
    check("情绪周期", "framework", YAMLGenerator._infer_category(
        indicators=['volume'],
        buy_conditions="df['volume'] < df['volume'].rolling(20).mean() * 0.5",
        sell_conditions="df['volume'] > df['volume'].rolling(20).mean() * 2",
        strategy_name="情绪周期 Emotion Cycle"
    ))

    # 波浪理论
    check("波浪理论", "framework", YAMLGenerator._infer_category(
        indicators=['ema'],
        buy_conditions="df['ema_fast'] > df['ema_slow']",
        sell_conditions="df['ema_fast'] < df['ema_slow']",
        strategy_name="波浪理论 Wave Theory"
    ))


def test_pattern_strategies():
    """测试形态类策略的识别"""
    print("\n═══ Pattern 策略 ═══")

    # 一阳夹三阴
    check("一阳夹三阴", "pattern", YAMLGenerator._infer_category(
        indicators=['volume'],
        buy_conditions="(df['close'] > df['open']) & (df['volume'] > df['volume'].rolling(5).mean())",
        sell_conditions="df['close'] < df['open']",
        strategy_name="一阳夹三阴 One Yang Three Yin"
    ))


def test_trend_strategies():
    """测试趋势类策略的识别"""
    print("\n═══ Trend 策略 ═══")

    # EMA趋势+RSI回调
    check("EMA趋势+RSI回调", "trend", YAMLGenerator._infer_category(
        indicators=['ema', 'rsi'],
        buy_conditions="(df['ema_fast'] > df['ema_slow']) & (df['rsi'] < 42)",
        sell_conditions="df['rsi'] > 70",
        strategy_name="EMA趋势+RSI回调"
    ))

    # 均线金叉
    check("均线金叉", "trend", YAMLGenerator._infer_category(
        indicators=['sma', 'volume'],
        buy_conditions="(df['ma_fast'] > df['ma_slow']) & (df['volume'] > df['volume'].rolling(5).mean())",
        sell_conditions="df['ma_fast'] < df['ma_slow']",
        strategy_name="均线金叉"
    ))

    # 放量突破
    check("放量突破", "trend", YAMLGenerator._infer_category(
        indicators=['ema', 'volume'],
        buy_conditions="(df['close'] > df['resistance']) & (df['volume'] > df['volume'].rolling(5).mean() * 2)",
        sell_conditions="df['close'] < df['support']",
        strategy_name="放量突破"
    ))

    # 缩量回踩
    check("缩量回踩", "trend", YAMLGenerator._infer_category(
        indicators=['ema', 'volume'],
        buy_conditions="(df['close'] > df['ema']) & (df['volume'] < df['volume'].rolling(5).mean() * 0.7)",
        sell_conditions="df['close'] < df['ema_slow']",
        strategy_name="缩量回踩"
    ))

    # 默认多头趋势
    check("默认多头趋势", "trend", YAMLGenerator._infer_category(
        indicators=['ema', 'macd'],
        buy_conditions="(df['ema_fast'] > df['ema_slow']) & (df['macd_hist'] > 0)",
        sell_conditions="df['macd_hist'] < 0",
        strategy_name="默认多头趋势"
    ))


def test_reversal_strategies():
    """测试反转类策略的识别"""
    print("\n═══ Reversal 策略 ═══")

    # KDJ+VWAP反转
    check("KDJ+VWAP反转", "reversal", YAMLGenerator._infer_category(
        indicators=['kdj', 'vwap'],
        buy_conditions="(df['kdj_j'] < 20) & (df['close'] < df['vwap'] * 0.98)",
        sell_conditions="(df['close'] > df['vwap']) | (df['kdj_j'] > 80)",
        strategy_name="KDJ+VWAP反转"
    ))

    # RSI+布林带支撑
    check("RSI+布林带支撑", "reversal", YAMLGenerator._infer_category(
        indicators=['rsi', 'bollinger'],
        buy_conditions="(df['rsi'] < 30) & (df['close'] < df['bb_lower'])",
        sell_conditions="df['rsi'] > 70",
        strategy_name="RSI+布林带支撑"
    ))

    # 底部放量
    check("底部放量", "reversal", YAMLGenerator._infer_category(
        indicators=['volume'],
        buy_conditions="(df['rsi'] < 30) & (df['volume'] > df['volume'].rolling(5).mean() * 3)",
        sell_conditions="df['rsi'] > 70",
        strategy_name="底部放量"
    ))

    # VWAP+RSI双确认
    check("VWAP+RSI双确认", "reversal", YAMLGenerator._infer_category(
        indicators=['vwap', 'rsi'],
        buy_conditions="(df['close'] < df['vwap'] * 0.97) & (df['rsi'] < 30)",
        sell_conditions="df['close'] > df['vwap']",
        strategy_name="VWAP+RSI双确认"
    ))


def test_edge_cases():
    """边界情况测试"""
    print("\n═══ 边界情况 ═══")

    # 空指标、空条件
    check("空输入 → unknown", "unknown", YAMLGenerator._infer_category(
        indicators=[], buy_conditions="", sell_conditions="", strategy_name=""
    ))

    # 只有 RSI 但名称含 "趋势" → 应该优先语义
    check("名称含'趋势'但指标是RSI", "trend", YAMLGenerator._infer_category(
        indicators=['rsi'],
        buy_conditions="df['ema_fast'] > df['ema_slow']",
        sell_conditions="df['rsi'] > 70",
        strategy_name="趋势RSI策略"
    ))


if __name__ == '__main__':
    test_framework_strategies()
    test_pattern_strategies()
    test_trend_strategies()
    test_reversal_strategies()
    test_edge_cases()

    print(f"\n{'═' * 50}")
    print(f"结果: {PASS} 通过, {FAIL} 失败, 共 {PASS + FAIL} 项")
    if FAIL > 0:
        sys.exit(1)
    else:
        print("🎉 全部通过!")
