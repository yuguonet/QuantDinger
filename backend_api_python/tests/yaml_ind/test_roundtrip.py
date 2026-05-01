#!/usr/bin/env python3
"""
Round-trip 测试：模拟 YAML → IndicatorStrategy → YAML 的分类还原
=================================================================

用 16 个真实策略的模拟代码（手写代表性片段），
测试 indicator_to_yaml.py 的 IndicatorParser + YAMLGenerator 能否正确还原 category。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from yaml_indicator.indicator_to_yaml import IndicatorParser, YAMLGenerator

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


# ── 模拟的 IndicatorStrategy 代码片段 ──

STRATEGIES = {
    # === TREND ===
    "ema_rsi_pullback": {
        "expected": "trend",
        "code": """
# IndicatorStrategy: EMA趋势+RSI回调
# @strategy stopLossPct 0.025
# @strategy tradeDirection long
# @param ema_fast int 10 短期EMA
# @param rsi_threshold int 42 RSI阈值

import pandas as pd
import numpy as np

df['ema_fast'] = df['close'].ewm(span=params.get('ema_fast', 10), adjust=False).mean()
df['ema_slow'] = df['close'].ewm(span=30, adjust=False).mean()
delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(window=14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))

df['buy'] = ((df['ema_fast'] > df['ema_slow']) & (df['rsi'] < params.get('rsi_threshold', 42))).fillna(False)
df['sell'] = (df['rsi'] > 70).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
    },
    "ma_golden_cross": {
        "expected": "trend",
        "code": """
# IndicatorStrategy: 均线金叉
# @strategy stopLossPct 0.03
# @param ma_fast int 5 快线
# @param ma_slow int 20 慢线

import pandas as pd
import numpy as np

df['ma_fast'] = df['close'].rolling(window=params.get('ma_fast', 5)).mean()
df['ma_slow'] = df['close'].rolling(window=params.get('ma_slow', 20)).mean()

df['buy'] = (df['ma_fast'] > df['ma_slow']).fillna(False)
df['sell'] = (df['ma_fast'] < df['ma_slow']).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
    },
    "volume_breakout": {
        "expected": "trend",
        "code": """
# IndicatorStrategy: 放量突破
# @strategy stopLossPct 0.03
# @param vol_ratio float 2.0 量比

import pandas as pd
import numpy as np

df['vol_ma'] = df['volume'].rolling(window=5).mean()
df['resistance'] = df['high'].rolling(window=20).max()

df['buy'] = ((df['close'] > df['resistance'].shift(1)) & (df['volume'] > df['vol_ma'] * params.get('vol_ratio', 2.0))).fillna(False)
df['sell'] = (df['close'] < df['close'].rolling(window=10).mean()).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
    },
    "shrink_pullback": {
        "expected": "trend",
        "code": """
# IndicatorStrategy: 缩量回踩
# @strategy stopLossPct 0.03
# @param ma_period int 10 均线周期

import pandas as pd
import numpy as np

df['ema'] = df['close'].ewm(span=params.get('ma_period', 10), adjust=False).mean()
df['vol_ma'] = df['volume'].rolling(window=5).mean()

df['buy'] = ((df['close'] > df['ema']) & (df['volume'] < df['vol_ma'] * 0.7)).fillna(False)
df['sell'] = (df['close'] < df['ema'] * 0.97).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
    },
    "bull_trend": {
        "expected": "trend",
        "code": """
# IndicatorStrategy: 默认多头趋势
# @strategy stopLossPct 0.03
# @param ema_fast int 10

import pandas as pd
import numpy as np

df['ema_fast'] = df['close'].ewm(span=params.get('ema_fast', 10), adjust=False).mean()
df['ema_slow'] = df['close'].ewm(span=30, adjust=False).mean()
df['macd_dif'] = df['close'].ewm(span=12, adjust=False).mean() - df['close'].ewm(span=26, adjust=False).mean()
df['macd_dea'] = df['macd_dif'].ewm(span=9, adjust=False).mean()
df['macd_hist'] = df['macd_dif'] - df['macd_dea']

df['buy'] = ((df['ema_fast'] > df['ema_slow']) & (df['macd_hist'] > 0)).fillna(False)
df['sell'] = (df['macd_hist'] < 0).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
    },
    "dragon_head": {
        "expected": "trend",
        "code": """
# IndicatorStrategy: 龙头策略
# @strategy stopLossPct 0.05
# @param vol_ratio float 1.5 量比

import pandas as pd
import numpy as np

df['vol_ma'] = df['volume'].rolling(window=5).mean()
df['ema'] = df['close'].ewm(span=20, adjust=False).mean()

df['buy'] = ((df['close'] > df['ema']) & (df['volume'] > df['vol_ma'] * params.get('vol_ratio', 1.5))).fillna(False)
df['sell'] = (df['close'] < df['ema'] * 0.95).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
    },

    # === REVERSAL ===
    "kdj_vwap_reversal": {
        "expected": "reversal",
        "code": """
# IndicatorStrategy: KDJ+VWAP反转
# @strategy stopLossPct 0.03
# @param kdj_period int 9 KDJ周期

import pandas as pd
import numpy as np

period = params.get('kdj_period', 9)
low_min = df['low'].rolling(window=period).min()
high_max = df['high'].rolling(window=period).max()
rsv = (df['close'] - low_min) / (high_max - low_min) * 100
k = rsv.ewm(alpha=1/3, adjust=False).mean()
d = k.ewm(alpha=1/3, adjust=False).mean()
df['kdj_j'] = 3 * k - 2 * d
df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()

df['buy'] = ((df['kdj_j'] < 20) & (df['close'] < df['vwap'] * 0.98)).fillna(False)
df['sell'] = ((df['close'] > df['vwap']) | (df['kdj_j'] > 80)).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
    },
    "rsi_bollinger_support": {
        "expected": "reversal",
        "code": """
# IndicatorStrategy: RSI+布林带支撑
# @strategy stopLossPct 0.03
# @param rsi_period int 14 RSI周期

import pandas as pd
import numpy as np

delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(window=params.get('rsi_period', 14)).mean()
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
""",
    },
    "bottom_volume": {
        "expected": "reversal",
        "code": """
# IndicatorStrategy: 底部放量
# @strategy stopLossPct 0.03

import pandas as pd
import numpy as np

delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(window=14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))
df['vol_ma'] = df['volume'].rolling(window=5).mean()

df['buy'] = ((df['rsi'] < 30) & (df['volume'] > df['vol_ma'] * 3)).fillna(False)
df['sell'] = (df['rsi'] > 70).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
    },
    "vwap_rsi_confirm": {
        "expected": "reversal",
        "code": """
# IndicatorStrategy: VWAP+RSI双确认
# @strategy stopLossPct 0.03

import pandas as pd
import numpy as np

df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()
delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(window=14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))

df['buy'] = ((df['close'] < df['vwap'] * 0.97) & (df['rsi'] < 30)).fillna(False)
df['sell'] = (df['close'] > df['vwap']).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
    },
    "vwap_macd_volume": {
        "expected": "reversal",
        "code": """
# IndicatorStrategy: VWAP+MACD+放量
# @strategy stopLossPct 0.03

import pandas as pd
import numpy as np

df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()
df['ema_fast'] = df['close'].ewm(span=12, adjust=False).mean()
df['ema_slow'] = df['close'].ewm(span=26, adjust=False).mean()
df['macd_dif'] = df['ema_fast'] - df['ema_slow']
df['macd_dea'] = df['macd_dif'].ewm(span=9, adjust=False).mean()
df['macd_hist'] = df['macd_dif'] - df['macd_dea']
df['vol_ma'] = df['volume'].rolling(window=5).mean()

df['buy'] = ((df['close'] < df['vwap'] * 0.98) & (df['macd_hist'] > df['macd_hist'].shift(1)) & (df['volume'] > df['vol_ma'] * 1.5)).fillna(False)
df['sell'] = ((df['close'] > df['vwap']) | (df['macd_hist'] < 0)).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
    },

    # === FRAMEWORK ===
    "chan_theory": {
        "expected": "framework",
        "code": """
# IndicatorStrategy: 缠论
# @strategy stopLossPct 0.03
# @param macd_fast int 12

import pandas as pd
import numpy as np

df['ema_fast'] = df['close'].ewm(span=params.get('macd_fast', 12), adjust=False).mean()
df['ema_slow'] = df['close'].ewm(span=26, adjust=False).mean()
df['macd_dif'] = df['ema_fast'] - df['ema_slow']
df['macd_dea'] = df['macd_dif'].ewm(span=9, adjust=False).mean()
df['macd_hist'] = df['macd_dif'] - df['macd_dea']

# 缠论：背驰判断 - 价格新低但MACD柱面积缩小
df['price_low'] = df['close'].rolling(window=20).min()
df['macd_area'] = df['macd_hist'].rolling(window=20).sum()

df['buy'] = ((df['close'] <= df['price_low']) & (df['macd_area'] > df['macd_area'].shift(20))).fillna(False)
df['sell'] = ((df['close'] >= df['close'].rolling(window=20).max()) & (df['macd_area'] < df['macd_area'].shift(20))).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
    },
    "box_oscillation": {
        "expected": "framework",
        "code": """
# IndicatorStrategy: 箱体震荡
# @strategy stopLossPct 0.03
# @param box_period int 20 箱体周期

import pandas as pd
import numpy as np

period = params.get('box_period', 20)
df['box_top'] = df['high'].rolling(window=period).max()
df['box_bottom'] = df['low'].rolling(window=period).min()
df['box_mid'] = (df['box_top'] + df['box_bottom']) / 2

# 箱底买入，箱顶卖出
df['buy'] = (df['close'] < df['box_bottom'] * 1.02).fillna(False)
df['sell'] = (df['close'] > df['box_top'] * 0.98).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
    },
    "emotion_cycle": {
        "expected": "framework",
        "code": """
# IndicatorStrategy: 情绪周期
# @strategy stopLossPct 0.03
# @param vol_period int 20 量能周期

import pandas as pd
import numpy as np

df['vol_ma'] = df['volume'].rolling(window=params.get('vol_period', 20)).mean()
df['vol_ratio'] = df['volume'] / df['vol_ma']
df['turnover'] = df['volume'] / df['close']  # 简化换手率

# 情绪底部：缩量 + 换手率低
df['buy'] = ((df['vol_ratio'] < 0.5) & (df['turnover'] < df['turnover'].rolling(window=60).quantile(0.1))).fillna(False)
# 情绪顶部：放量 + 换手率高
df['sell'] = ((df['vol_ratio'] > 2.0) & (df['turnover'] > df['turnover'].rolling(window=60).quantile(0.9))).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
    },
    "wave_theory": {
        "expected": "framework",
        "code": """
# IndicatorStrategy: 波浪理论
# @strategy stopLossPct 0.03
# @param wave_period int 20 浪型周期

import pandas as pd
import numpy as np

period = params.get('wave_period', 20)
df['ema'] = df['close'].ewm(span=period, adjust=False).mean()
df['high_n'] = df['high'].rolling(window=period).max()
df['low_n'] = df['low'].rolling(window=period).min()

# 推动浪：价格突破前期高点
df['buy'] = (df['close'] > df['high_n'].shift(1)).fillna(False)
# 调整浪：价格跌破均线支撑
df['sell'] = (df['close'] < df['ema'] * 0.95).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
    },

    # === PATTERN ===
    "one_yang_three_yin": {
        "expected": "pattern",
        "code": """
# IndicatorStrategy: 一阳夹三阴
# @strategy stopLossPct 0.03
# @param body_pct float 0.02 实体比例

import pandas as pd
import numpy as np

# 一阳夹三阴形态检测
body = abs(df['close'] - df['open'])
body_pct = body / df['close']
is_yang = df['close'] > df['open']

# 第1日大阳线
yang1 = is_yang.shift(4) & (body_pct.shift(4) > params.get('body_pct', 0.02))
# 第2-4日小阴线
small_yin2 = (body_pct.shift(3) < 0.01)
small_yin3 = (body_pct.shift(2) < 0.01)
small_yin4 = (body_pct.shift(1) < 0.01)
# 第5日阳线突破
yang5 = is_yang & (df['close'] > df['close'].shift(4))

df['buy'] = (yang1 & small_yin2 & small_yin3 & small_yin4 & yang5).fillna(False)
df['sell'] = (df['close'] < df['close'].rolling(window=5).min()).fillna(False)
df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
""",
    },
}


def test_roundtrip():
    """测试所有策略的 round-trip 分类还原"""
    print("═══ Round-trip 分类还原测试 ═══\n")

    for name, spec in STRATEGIES.items():
        expected = spec["expected"]
        code = spec["code"]

        # 解析代码
        parsed = IndicatorParser.parse_code(code, source_file=f"{name}.py")

        # 生成 YAML
        yaml_data = YAMLGenerator.generate(parsed)
        actual = yaml_data['category']

        check(name, expected, actual)

    print()


def test_parse_quality():
    """测试解析质量：参数、注解、指标是否正确提取"""
    print("═══ 解析质量测试 ═══\n")

    # EMA+RSI 策略应该正确提取参数和指标
    code = STRATEGIES["ema_rsi_pullback"]["code"]
    parsed = IndicatorParser.parse_code(code, "ema_rsi_pullback.py")

    check("提取策略名", "EMA趋势+RSI回调", parsed['strategy_name'])
    check("提取 ema_fast 参数", True, any(p['name'] == 'ema_fast' for p in parsed['params']))
    check("提取 rsi_threshold 参数", True, any(p['name'] == 'rsi_threshold' for p in parsed['params']))
    check("检测 ema 指标", True, 'ema' in parsed['indicators'])
    check("检测 rsi 指标", True, 'rsi' in parsed['indicators'])
    check("有 stopLossPct", True, 'stopLossPct' in parsed['strategy_annotations'])
    check("有 tradeDirection", True, 'tradeDirection' in parsed['strategy_annotations'])
    check("有 buy 条件", True, bool(parsed['buy_conditions']))
    check("有 sell 条件", True, bool(parsed['sell_conditions']))

    print()


if __name__ == '__main__':
    test_roundtrip()
    test_parse_quality()

    print(f"{'═' * 50}")
    print(f"结果: {PASS} 通过, {FAIL} 失败, 共 {PASS + FAIL} 项")
    if FAIL > 0:
        sys.exit(1)
    else:
        print("🎉 全部通过!")
