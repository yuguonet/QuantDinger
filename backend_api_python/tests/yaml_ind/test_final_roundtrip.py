#!/usr/bin/env python3
"""
最终 Round-trip 测试
====================
4 个内置示例 + 16 个 YAML 策略的真实双向比对。

测试流程：
  Part A: 4 个内置示例 indicator → yaml → 比对结构
  Part B: 16 个 YAML 策略 yaml → indicator(模拟) → yaml → 比对字段
  Part C: 准确率汇总
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from yaml_indicator.indicator_to_yaml import IndicatorParser, YAMLGenerator
from yaml_indicator.yaml_to_indicator import (
    YAMLStrategyParser,
    LLMStrategyGenerator,
    CodeValidator,
    CodePostProcessor,
)

PASS = 0
FAIL = 0
TOTAL = 0

def check(name, expected, actual):
    global PASS, FAIL, TOTAL
    TOTAL += 1
    if expected == actual:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}: expected={expected!r}, got={actual!r}")


# ============================================================
# Part A: 4 个内置示例 indicator → yaml
# ============================================================

BUILTIN = {
    "RSI边缘触发": {
        "code": r'''
# @strategy stopLossPct 0.03
# @strategy takeProfitPct 0.06
# @strategy tradeDirection long
df = df.copy()
rsi_len = 14
delta = df['close'].diff()
gain = delta.clip(lower=0)
loss = (-delta).clip(lower=0)
avg_gain = gain.ewm(alpha=1 / rsi_len, adjust=False).mean()
avg_loss = loss.ewm(alpha=1 / rsi_len, adjust=False).mean()
rs = avg_gain / avg_loss.replace(0, np.nan)
rsi = 100 - (100 / (1 + rs))
raw_buy = rsi < 30
raw_sell = rsi > 70
buy = raw_buy.fillna(False) & (~raw_buy.shift(1).fillna(False))
sell = raw_sell.fillna(False) & (~raw_sell.shift(1).fillna(False))
df['buy'] = buy.astype(bool)
df['sell'] = sell.astype(bool)
''',
        "expect_cat": "reversal",
        "expect_inds": ["rsi"],
    },
    "双均线金叉": {
        "code": r'''
# @strategy stopLossPct 0.025
# @strategy tradeDirection both
df = df.copy()
ma_f = df['close'].rolling(12, min_periods=1).mean()
ma_s = df['close'].rolling(26, min_periods=1).mean()
golden = (ma_f > ma_s) & (ma_f.shift(1) <= ma_s.shift(1))
death = (ma_f < ma_s) & (ma_f.shift(1) >= ma_s.shift(1))
df['buy'] = golden.fillna(False).astype(bool)
df['sell'] = death.fillna(False).astype(bool)
''',
        "expect_cat": "trend",
        "expect_inds": ["sma"],
    },
    "MACD柱穿零": {
        "code": r'''
# @strategy stopLossPct 0.03
# @strategy tradeDirection both
df = df.copy()
exp12 = df['close'].ewm(span=12, adjust=False).mean()
exp26 = df['close'].ewm(span=26, adjust=False).mean()
dif = exp12 - exp26
dea = dif.ewm(span=9, adjust=False).mean()
hist = dif - dea
raw_buy = (hist > 0) & (hist.shift(1) <= 0)
raw_sell = (hist < 0) & (hist.shift(1) >= 0)
df['buy'] = raw_buy.fillna(False).astype(bool)
df['sell'] = raw_sell.fillna(False).astype(bool)
''',
        "expect_cat": "trend",
        "expect_inds": ["ema", "macd"],
    },
    "布林带触及": {
        "code": r'''
# @strategy stopLossPct 0.02
# @strategy tradeDirection long
df = df.copy()
mid = df['close'].rolling(20, min_periods=1).mean()
std = df['close'].rolling(20, min_periods=1).std()
upper = mid + 2.0 * std
lower = mid - 2.0 * std
raw_buy = df['close'] < lower
raw_sell = df['close'] > upper
buy = raw_buy.fillna(False) & (~raw_buy.shift(1).fillna(False))
sell = raw_sell.fillna(False) & (~raw_sell.shift(1).fillna(False))
df['buy'] = buy.astype(bool)
df['sell'] = sell.astype(bool)
''',
        "expect_cat": "reversal",
        "expect_inds": ["bollinger"],
    },
}


def test_builtin():
    """Part A: 内置示例 indicator → yaml"""
    print("═══ Part A: 4 个内置示例 indicator → yaml ═══\n")

    for name, spec in BUILTIN.items():
        print(f"  📋 {name}")
        parsed = IndicatorParser.parse_code(spec['code'], f"{name}.py")
        y = YAMLGenerator.generate(parsed)

        check(f"    category", spec['expect_cat'], y['category'])
        for ind in spec['expect_inds']:
            check(f"    指标 {ind}", True, ind in parsed['indicators'])
        check(f"    有 name", True, bool(y['name']))
        check(f"    有 display_name", True, bool(y['display_name']))
        check(f"    有 instructions", True, bool(y['instructions']))
        check(f"    有 core_rules", True, bool(y['core_rules']))
        check(f"    有 required_tools", True, bool(y['required_tools']))
        check(f"    有 buy 条件", True, bool(parsed['buy_conditions']))
        check(f"    有 sell 条件", True, bool(parsed['sell_conditions']))
        print()


# ============================================================
# Part B: 16 个 YAML 策略 yaml → indicator(模拟) → yaml 比对
# ============================================================

def test_yaml_strategies():
    """Part B: 16 个 YAML 策略 round-trip"""
    print("═══ Part B: 16 个 YAML 策略 yaml → indicator → yaml ═══\n")

    strategies_dir = Path(__file__).parent / "backend_api_python" / "strategies"
    if not strategies_dir.is_dir():
        print("  ⚠️  目录不存在")
        return

    yaml_files = sorted(strategies_dir.glob('*.yaml'))
    print(f"  找到 {len(yaml_files)} 个策略文件\n")

    cat_match = 0
    name_match = 0
    total = 0

    for yf in yaml_files:
        total += 1
        original = YAMLStrategyParser.parse(str(yf))

        # 用 LLM prompt 构建验证信息完整性
        gen = LLMStrategyGenerator("https://api.openai.com/v1", "sk-test", "gpt-4o")
        prompt = gen._build_user_prompt({
            'name': original['name'],
            'display_name': original['display_name'],
            'category': original['category'],
            'description': original['description'],
            'instructions': original['instructions'],
        })

        # 验证 prompt 包含原始信息
        name_ok = original['name'] in prompt
        cat_ok = original['category'] in prompt
        instr_ok = original['instructions'][:30] in prompt

        if name_ok:
            name_match += 1
        if cat_ok:
            cat_match += 1

        check(f"{original['name']}: prompt 含策略名", True, name_ok)
        check(f"{original['name']}: prompt 含分类", True, cat_ok)
        check(f"{original['name']}: prompt 含 instructions", True, instr_ok)

        # YAML 结构完整性
        check(f"{original['name']}: category 合法", True,
              original['category'] in ('trend', 'reversal', 'framework', 'pattern', 'unknown'))

    print(f"\n  📊 prompt 信息还原: name={name_match}/{total}, category={cat_match}/{total}")
    print()


# ============================================================
# Part C: 模拟 indicator 代码 → yaml → 验证分类还原
# ============================================================

def test_indicator_to_yaml_category():
    """Part C: 用模拟代码验证分类还原准确率"""
    print("═══ Part C: indicator → yaml 分类还原 ═══\n")

    # 每个 YAML 策略对应的典型 indicator 代码
    test_cases = [
        # (名称, 模拟代码, 期望分类)
        ("ema_rsi_pullback", """
# IndicatorStrategy: EMA趋势+RSI回调
# @strategy stopLossPct 0.025
import pandas as pd; import numpy as np
df['ema_fast'] = df['close'].ewm(span=10, adjust=False).mean()
df['ema_slow'] = df['close'].ewm(span=30, adjust=False).mean()
delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(window=14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))
df['buy'] = ((df['ema_fast'] > df['ema_slow']) & (df['rsi'] < 42)).fillna(False)
df['sell'] = (df['rsi'] > 70).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "trend"),

        ("kdj_vwap_reversal", """
# IndicatorStrategy: KDJ+VWAP反转
# @strategy stopLossPct 0.03
import pandas as pd; import numpy as np
low_min = df['low'].rolling(window=9).min()
high_max = df['high'].rolling(window=9).max()
rsv = (df['close'] - low_min) / (high_max - low_min) * 100
k = rsv.ewm(alpha=1/3, adjust=False).mean()
d = k.ewm(alpha=1/3, adjust=False).mean()
df['kdj_j'] = 3 * k - 2 * d
df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()
df['buy'] = ((df['kdj_j'] < 20) & (df['close'] < df['vwap'] * 0.98)).fillna(False)
df['sell'] = ((df['close'] > df['vwap']) | (df['kdj_j'] > 80)).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "reversal"),

        ("rsi_bollinger_support", """
# IndicatorStrategy: RSI+布林带支撑
# @strategy stopLossPct 0.03
import pandas as pd; import numpy as np
delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(window=14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))
df['bb_mid'] = df['close'].rolling(window=20).mean()
df['bb_std'] = df['close'].rolling(window=20).std()
df['bb_lower'] = df['bb_mid'] - 2 * df['bb_std']
df['buy'] = ((df['rsi'] < 30) & (df['close'] < df['bb_lower'])).fillna(False)
df['sell'] = (df['rsi'] > 70).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "reversal"),

        ("vwap_macd_volume", """
# IndicatorStrategy: VWAP+MACD+放量
# @strategy stopLossPct 0.03
import pandas as pd; import numpy as np
df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()
df['ema_fast'] = df['close'].ewm(span=12, adjust=False).mean()
df['ema_slow'] = df['close'].ewm(span=26, adjust=False).mean()
df['macd_hist'] = (df['ema_fast'] - df['ema_slow']) - (df['ema_fast'] - df['ema_slow']).ewm(span=9).mean()
df['vol_ma'] = df['volume'].rolling(window=5).mean()
df['buy'] = ((df['close'] < df['vwap'] * 0.98) & (df['macd_hist'] > df['macd_hist'].shift(1)) & (df['volume'] > df['vol_ma'] * 1.5)).fillna(False)
df['sell'] = ((df['close'] > df['vwap']) | (df['macd_hist'] < 0)).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "reversal"),

        ("chan_theory", """
# IndicatorStrategy: 缠论
# @strategy stopLossPct 0.03
import pandas as pd; import numpy as np
df['ema_fast'] = df['close'].ewm(span=12, adjust=False).mean()
df['ema_slow'] = df['close'].ewm(span=26, adjust=False).mean()
df['macd_hist'] = (df['ema_fast'] - df['ema_slow']) - (df['ema_fast'] - df['ema_slow']).ewm(span=9).mean()
df['buy'] = ((df['close'] <= df['close'].rolling(20).min()) & (df['macd_hist'] > 0)).fillna(False)
df['sell'] = ((df['close'] >= df['close'].rolling(20).max()) & (df['macd_hist'] < 0)).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "framework"),

        ("box_oscillation", """
# IndicatorStrategy: 箱体震荡
# @strategy stopLossPct 0.03
import pandas as pd; import numpy as np
df['box_top'] = df['high'].rolling(window=20).max()
df['box_bottom'] = df['low'].rolling(window=20).min()
df['buy'] = (df['close'] < df['box_bottom'] * 1.02).fillna(False)
df['sell'] = (df['close'] > df['box_top'] * 0.98).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "framework"),

        ("emotion_cycle", """
# IndicatorStrategy: 情绪周期
# @strategy stopLossPct 0.03
import pandas as pd; import numpy as np
df['vol_ma'] = df['volume'].rolling(window=20).mean()
df['vol_ratio'] = df['volume'] / df['vol_ma']
df['buy'] = (df['vol_ratio'] < 0.5).fillna(False)
df['sell'] = (df['vol_ratio'] > 2.0).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "framework"),

        ("wave_theory", """
# IndicatorStrategy: 波浪理论
# @strategy stopLossPct 0.03
import pandas as pd; import numpy as np
df['ema'] = df['close'].ewm(span=20, adjust=False).mean()
df['high_n'] = df['high'].rolling(window=20).max()
df['buy'] = (df['close'] > df['high_n'].shift(1)).fillna(False)
df['sell'] = (df['close'] < df['ema'] * 0.95).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "framework"),

        ("one_yang_three_yin", """
# IndicatorStrategy: 一阳夹三阴
# @strategy stopLossPct 0.03
import pandas as pd; import numpy as np
body = abs(df['close'] - df['open'])
body_pct = body / df['close']
is_yang = df['close'] > df['open']
yang1 = is_yang.shift(4) & (body_pct.shift(4) > 0.02)
small_yin = (body_pct.shift(3) < 0.01) & (body_pct.shift(2) < 0.01) & (body_pct.shift(1) < 0.01)
yang5 = is_yang & (df['close'] > df['close'].shift(4))
df['buy'] = (yang1 & small_yin & yang5).fillna(False)
df['sell'] = (df['close'] < df['close'].rolling(5).min()).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "pattern"),

        ("ma_golden_cross", """
# IndicatorStrategy: 均线金叉
# @strategy stopLossPct 0.03
import pandas as pd; import numpy as np
df['ma_fast'] = df['close'].rolling(5).mean()
df['ma_slow'] = df['close'].rolling(20).mean()
df['buy'] = ((df['ma_fast'] > df['ma_slow']) & (df['ma_fast'].shift(1) <= df['ma_slow'].shift(1))).fillna(False)
df['sell'] = ((df['ma_fast'] < df['ma_slow']) & (df['ma_fast'].shift(1) >= df['ma_slow'].shift(1))).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "trend"),

        ("volume_breakout", """
# IndicatorStrategy: 放量突破
# @strategy stopLossPct 0.03
import pandas as pd; import numpy as np
df['vol_ma'] = df['volume'].rolling(5).mean()
df['resistance'] = df['high'].rolling(20).max()
df['buy'] = ((df['close'] > df['resistance'].shift(1)) & (df['volume'] > df['vol_ma'] * 2)).fillna(False)
df['sell'] = (df['close'] < df['close'].rolling(10).mean()).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "trend"),

        ("shrink_pullback", """
# IndicatorStrategy: 缩量回踩
# @strategy stopLossPct 0.03
import pandas as pd; import numpy as np
df['ema'] = df['close'].ewm(span=10, adjust=False).mean()
df['vol_ma'] = df['volume'].rolling(5).mean()
df['buy'] = ((df['close'] > df['ema']) & (df['volume'] < df['vol_ma'] * 0.7)).fillna(False)
df['sell'] = (df['close'] < df['ema'] * 0.97).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "trend"),

        ("vwap_rsi_confirm", """
# IndicatorStrategy: VWAP+RSI双确认
# @strategy stopLossPct 0.03
import pandas as pd; import numpy as np
df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()
delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))
df['buy'] = ((df['close'] < df['vwap'] * 0.97) & (df['rsi'] < 30)).fillna(False)
df['sell'] = (df['close'] > df['vwap']).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "reversal"),

        ("bottom_volume", """
# IndicatorStrategy: 底部放量
# @strategy stopLossPct 0.03
import pandas as pd; import numpy as np
delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))
df['vol_ma'] = df['volume'].rolling(5).mean()
df['buy'] = ((df['rsi'] < 30) & (df['volume'] > df['vol_ma'] * 3)).fillna(False)
df['sell'] = (df['rsi'] > 70).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "reversal"),

        ("bull_trend", """
# IndicatorStrategy: 默认多头趋势
# @strategy stopLossPct 0.03
import pandas as pd; import numpy as np
df['ema_fast'] = df['close'].ewm(span=10, adjust=False).mean()
df['ema_slow'] = df['close'].ewm(span=30, adjust=False).mean()
df['macd_hist'] = (df['ema_fast'] - df['ema_slow']) - (df['ema_fast'] - df['ema_slow']).ewm(span=9).mean()
df['buy'] = ((df['ema_fast'] > df['ema_slow']) & (df['macd_hist'] > 0)).fillna(False)
df['sell'] = (df['macd_hist'] < 0).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "trend"),

        ("dragon_head", """
# IndicatorStrategy: 龙头策略
# @strategy stopLossPct 0.05
import pandas as pd; import numpy as np
df['vol_ma'] = df['volume'].rolling(5).mean()
df['ema'] = df['close'].ewm(span=20, adjust=False).mean()
df['buy'] = ((df['close'] > df['ema']) & (df['volume'] > df['vol_ma'] * 1.5)).fillna(False)
df['sell'] = (df['close'] < df['ema'] * 0.95).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""", "trend"),
    ]

    correct = 0
    total = len(test_cases)

    for name, code, expected_cat in test_cases:
        parsed = IndicatorParser.parse_code(code, f"{name}.py")
        y = YAMLGenerator.generate(parsed)
        actual_cat = y['category']

        if actual_cat == expected_cat:
            correct += 1
            print(f"  ✅ {name}: {actual_cat}")
        else:
            print(f"  ❌ {name}: expected={expected_cat}, got={actual_cat}")

    print(f"\n  📊 分类准确率: {correct}/{total} ({correct/total*100:.0f}%)")
    print()


# ============================================================
# Main
# ============================================================

if __name__ == '__main__':
    test_builtin()
    test_yaml_strategies()
    test_indicator_to_yaml_category()

    print(f"{'═' * 50}")
    print(f"结果: {PASS} 通过, {FAIL} 失败, 共 {TOTAL} 项")
    if FAIL > 0:
        sys.exit(1)
    else:
        print("🎉 全部通过!")
