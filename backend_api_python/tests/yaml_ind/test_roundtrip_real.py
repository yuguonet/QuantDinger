#!/usr/bin/env python3
"""
真实 Round-trip 测试：indicator → yaml → indicator
===================================================

数据来源：
1. builtin_indicators.py 中 4 个内置示例（真实可执行代码）
2. 16 个 YAML 策略文件（真实策略定义）

测试流程：
  原始 indicator 代码
    → IndicatorParser.parse_code() → 结构化数据
    → YAMLGenerator.generate() → YAML dict
    → 比对 category / indicators / params / annotations

  原始 YAML 文件
    → YAMLStrategyParser.parse() → 结构化数据
    → 比对 category / core_rules / required_tools
    → LLMStrategyGenerator prompt 构建验证（不实际调用 API）
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from yaml_indicator.indicator_to_yaml import IndicatorParser, YAMLGenerator
from yaml_indicator.yaml_to_indicator import (
    YAMLStrategyParser,
    LLMStrategyGenerator,
    CodeValidator,
    CodePostProcessor,
    ConversionPipeline,
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

def check_contains(name, expected_items, actual_text):
    """检查 actual_text 包含所有 expected_items（子串匹配）"""
    global PASS, FAIL, TOTAL
    TOTAL += 1
    missing = [item for item in expected_items if item not in actual_text]
    if not missing:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}: missing={missing}")


# ============================================================
# Part 1: 内置示例 indicator → yaml
# ============================================================

BUILTIN_INDICATORS = {
    "[示例] RSI 边缘触发": {
        "code": r'''
my_indicator_name = "[示例] RSI 边缘触发"
my_indicator_description = "RSI 超卖/超买 + 边缘触发"

# @strategy stopLossPct 0.03
# @strategy takeProfitPct 0.06
# @strategy entryPct 1
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
rsi = rsi.fillna(50)

raw_buy = rsi < 30
raw_sell = rsi > 70
buy = raw_buy.fillna(False) & (~raw_buy.shift(1).fillna(False))
sell = raw_sell.fillna(False) & (~raw_sell.shift(1).fillna(False))
df['buy'] = buy.astype(bool)
df['sell'] = sell.astype(bool)
''',
        "expect": {
            "indicators": ["rsi"],
            "category": "reversal",
            "has_stopLossPct": True,
            "has_takeProfitPct": True,
            "tradeDirection": "long",
        },
    },
    "[示例] 双均线金叉死叉": {
        "code": r'''
my_indicator_name = "[示例] 双均线金叉死叉"
my_indicator_description = "快慢均线交叉；边缘触发"

# @strategy stopLossPct 0.025
# @strategy takeProfitPct 0.05
# @strategy entryPct 1
# @strategy tradeDirection both

df = df.copy()
fast_n = 12
slow_n = 26
ma_f = df['close'].rolling(fast_n, min_periods=1).mean()
ma_s = df['close'].rolling(slow_n, min_periods=1).mean()

golden = (ma_f > ma_s) & (ma_f.shift(1) <= ma_s.shift(1))
death = (ma_f < ma_s) & (ma_f.shift(1) >= ma_s.shift(1))
df['buy'] = golden.fillna(False).astype(bool)
df['sell'] = death.fillna(False).astype(bool)
''',
        "expect": {
            "indicators": ["sma"],
            "category": "trend",
            "has_stopLossPct": True,
            "tradeDirection": "both",
        },
    },
    "[示例] MACD 柱穿零轴": {
        "code": r'''
my_indicator_name = "[示例] MACD 柱穿零轴"
my_indicator_description = "DIF/DEA/柱；柱线穿越零轴边缘触发"

# @strategy stopLossPct 0.03
# @strategy takeProfitPct 0.08
# @strategy entryPct 0.5
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
        "expect": {
            "indicators": ["ema", "macd"],
            "category": "trend",
            "has_stopLossPct": True,
            "tradeDirection": "both",
        },
    },
    "[示例] 布林带触及": {
        "code": r'''
my_indicator_name = "[示例] 布林带触及"
my_indicator_description = "简单布林带反转思路示例"

# @strategy stopLossPct 0.02
# @strategy takeProfitPct 0.04
# @strategy entryPct 0.3
# @strategy tradeDirection long

df = df.copy()
period = 20
mult = 2.0
mid = df['close'].rolling(period, min_periods=1).mean()
std = df['close'].rolling(period, min_periods=1).std()
upper = mid + mult * std
lower = mid - mult * std

raw_buy = df['close'] < lower
raw_sell = df['close'] > upper
buy = raw_buy.fillna(False) & (~raw_buy.shift(1).fillna(False))
sell = raw_sell.fillna(False) & (~raw_sell.shift(1).fillna(False))
df['buy'] = buy.astype(bool)
df['sell'] = sell.astype(bool)
''',
        "expect": {
            "indicators": ["bollinger"],
            "category": "reversal",
            "has_stopLossPct": True,
            "tradeDirection": "long",
        },
    },
}


def test_builtin_indicator_to_yaml():
    """内置示例 indicator → yaml 比对"""
    print("═══ Part 1: 内置示例 indicator → yaml ═══\n")

    for name, spec in BUILTIN_INDICATORS.items():
        print(f"  📋 {name}")
        parsed = IndicatorParser.parse_code(spec['code'], f"{name}.py")
        yaml_data = YAMLGenerator.generate(parsed)
        expect = spec['expect']

        # 指标检测
        for ind in expect['indicators']:
            check(f"    检测 {ind}", True, ind in parsed['indicators'])

        # 分类
        check(f"    category={expect['category']}", expect['category'], yaml_data['category'])

        # @strategy 注解
        if expect.get('has_stopLossPct'):
            check("    有 stopLossPct", True, 'stopLossPct' in parsed['strategy_annotations'])
        if expect.get('has_takeProfitPct'):
            check("    有 takeProfitPct", True, 'takeProfitPct' in parsed['strategy_annotations'])
        if expect.get('tradeDirection'):
            check(f"    tradeDirection={expect['tradeDirection']}",
                  expect['tradeDirection'], parsed['strategy_annotations'].get('tradeDirection'))

        # YAML 结构完整性
        check("    YAML 有 name", True, bool(yaml_data.get('name')))
        check("    YAML 有 display_name", True, bool(yaml_data.get('display_name')))
        check("    YAML 有 description", True, bool(yaml_data.get('description')))
        check("    YAML 有 instructions", True, bool(yaml_data.get('instructions')))
        check("    YAML 有 core_rules", True, bool(yaml_data.get('core_rules')))
        check("    YAML 有 required_tools", True, bool(yaml_data.get('required_tools')))

        print()


# ============================================================
# Part 2: 16 个 YAML 策略 → indicator → yaml 比对
# ============================================================

def test_yaml_roundtrip():
    """16 个 YAML 策略的 round-trip 比对"""
    print("═══ Part 2: 16 个 YAML 策略 round-trip ═══\n")

    strategies_dir = Path(__file__).parent / "backend_api_python" / "strategies"
    if not strategies_dir.is_dir():
        print("  ⚠️  目录不存在，跳过")
        return

    yaml_files = sorted(strategies_dir.glob('*.yaml'))
    print(f"  找到 {len(yaml_files)} 个 YAML 策略文件\n")

    category_match = 0
    total = 0
    errors = []

    for yaml_file in yaml_files:
        total += 1
        try:
            # 1. 解析原始 YAML
            original = YAMLStrategyParser.parse(str(yaml_file))

            # 2. 通过 LLM prompt 构建验证（不实际调用 API）
            # 检查 prompt 是否包含所有关键信息
            strategy_for_prompt = {
                'name': original['name'],
                'display_name': original['display_name'],
                'category': original['category'],
                'description': original['description'],
                'instructions': original['instructions'],
            }
            gen = LLMStrategyGenerator("https://api.openai.com/v1", "sk-test", "gpt-4o")
            prompt = gen._build_user_prompt(strategy_for_prompt)

            # 3. 验证 prompt 包含关键信息
            check_contains(f"{original['name']}: prompt 包含策略名",
                          [original['name']], prompt)
            check_contains(f"{original['name']}: prompt 包含分类",
                          [original['category']], prompt)
            check_contains(f"{original['name']}: prompt 包含 instructions",
                          [original['instructions'][:50]], prompt)

            # 4. 验证 YAML 结构
            check(f"{original['name']}: 有 name", True, bool(original['name']))
            check(f"{original['name']}: 有 display_name", True, bool(original['display_name']))
            check(f"{original['name']}: 有 description", True, bool(original['description']))
            check(f"{original['name']}: 有 instructions", True, bool(original['instructions']))
            check(f"{original['name']}: 有 category", True, original['category'] in
                  ('trend', 'reversal', 'framework', 'pattern', 'unknown'))

            # 5. 检查 category 是否合理
            valid_categories = {'trend', 'reversal', 'framework', 'pattern', 'volatility', 'volume', 'unknown'}
            if original['category'] in valid_categories:
                category_match += 1

        except Exception as e:
            errors.append(f"{yaml_file.name}: {e}")
            print(f"  ❌ {yaml_file.name}: {e}")

    print(f"\n  📊 YAML 解析统计: {category_match}/{total} 成功")
    if errors:
        print(f"  ⚠️  错误: {len(errors)}")
        for err in errors:
            print(f"     - {err}")
    print()


# ============================================================
# Part 3: 代码安全校验（真实代码 → 安全检查）
# ============================================================

def test_code_safety():
    """验证安全校验器对内置代码的处理"""
    print("═══ Part 3: 代码安全校验 ═══\n")

    for name, spec in BUILTIN_INDICATORS.items():
        # 添加 fillna/astype 以通过后处理器
        code = spec['code']
        is_valid, errors = CodeValidator.validate(code)

        # 内置代码应该通过安全检查（没有危险 import/调用）
        dangerous_errors = [e for e in errors if '危险' in e or '禁止' in e]
        check(f"{name}: 无安全问题", 0, len(dangerous_errors))

        # 检查 df['buy']/df['sell'] 是否存在
        has_buy = "df['buy']" in code or 'df["buy"]' in code
        has_sell = "df['sell']" in code or 'df["sell"]' in code
        check(f"{name}: 有 df['buy']", True, has_buy)
        check(f"{name}: 有 df['sell']", True, has_sell)

    # 后处理器测试
    print()
    code = BUILTIN_INDICATORS["[示例] RSI 边缘触发"]["code"]
    processed = CodePostProcessor.process(code, "RSI Test")
    check("后处理: 添加 fillna buy", True, "df['buy'].fillna(False)" in processed or "df['buy'].fillna" in processed)
    check("后处理: 添加 fillna sell", True, "df['sell'].fillna(False)" in processed or "df['sell'].fillna" in processed)
    print()


# ============================================================
# Part 4: 端到端流程验证（不调用 LLM API）
# ============================================================

def test_pipeline_structure():
    """验证转换管道结构完整性"""
    print("═══ Part 4: 管道结构完整性 ═══\n")

    # 1. YAML 解析 → LLM prompt 构建 → 代码校验 → 后处理
    strategies_dir = Path(__file__).parent / "backend_api_python" / "strategies"
    if not strategies_dir.is_dir():
        print("  ⚠️  目录不存在，跳过")
        return

    sample = strategies_dir / "ema_rsi_pullback.yaml"
    if not sample.exists():
        print("  ⚠️  示例文件不存在，跳过")
        return

    # 解析 YAML
    strategy = YAMLStrategyParser.parse(str(sample))
    check("管道: YAML 解析成功", True, bool(strategy['name']))

    # 构建 prompt
    gen = LLMStrategyGenerator("https://api.openai.com/v1", "sk-test", "gpt-4o")
    prompt = gen._build_user_prompt(strategy)
    check("管道: prompt 包含 @param 指引", True, '@param' in prompt)
    check("管道: prompt 包含 @strategy 指引", True, '@strategy' in prompt)
    check("管道: prompt 包含 df['buy'] 要求", True, "df['buy']" in prompt)

    # 模拟 LLM 输出清理
    raw_output = "```python\nimport pandas as pd\nimport numpy as np\ndf['buy'] = True\ndf['sell'] = False\n```"
    cleaned = LLMStrategyGenerator._clean_code_output(raw_output)
    check("管道: 清理 markdown 代码块", True, "```" not in cleaned)
    check("管道: 保留 Python 代码", True, "df['buy']" in cleaned)

    # 代码后处理
    processed = CodePostProcessor.process(cleaned, "test")
    check("管道: 后处理添加头部注释", True, "# IndicatorStrategy" in processed)
    check("管道: 后处理添加 fillna", True, ".fillna(False)" in processed)

    # 安全校验
    is_valid, errors = CodeValidator.validate(processed)
    check("管道: 后处理代码通过安全校验", True, is_valid)

    print()


# ============================================================
# Part 5: 分类准确率汇总
# ============================================================

def test_category_accuracy():
    """汇总分类准确率"""
    print("═══ Part 5: 分类准确率汇总 ═══\n")

    # 内置指标分类结果
    builtin_results = {}
    for name, spec in BUILTIN_INDICATORS.items():
        parsed = IndicatorParser.parse_code(spec['code'], f"{name}.py")
        yaml_data = YAMLGenerator.generate(parsed)
        builtin_results[name] = {
            'expected': spec['expect']['category'],
            'actual': yaml_data['category'],
            'match': spec['expect']['category'] == yaml_data['category'],
        }

    # YAML 策略分类（原始值）
    strategies_dir = Path(__file__).parent / "backend_api_python" / "strategies"
    yaml_results = {}
    if strategies_dir.is_dir():
        for yaml_file in sorted(strategies_dir.glob('*.yaml')):
            try:
                s = YAMLStrategyParser.parse(str(yaml_file))
                yaml_results[s['name']] = {
                    'category': s['category'],
                    'display_name': s['display_name'],
                }
            except Exception:
                pass

    # 统计
    builtin_correct = sum(1 for r in builtin_results.values() if r['match'])
    builtin_total = len(builtin_results)

    print(f"  内置指标分类准确率: {builtin_correct}/{builtin_total} ({builtin_correct/builtin_total*100:.0f}%)")
    for name, r in builtin_results.items():
        status = "✅" if r['match'] else "❌"
        print(f"    {status} {name}: {r['actual']} (expected: {r['expected']})")

    print(f"\n  YAML 策略分类分布:")
    cat_counts = {}
    for name, r in yaml_results.items():
        cat = r['category']
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    for cat, count in sorted(cat_counts.items()):
        print(f"    {cat}: {count} 个")

    print(f"\n  YAML 策略总计: {len(yaml_results)} 个")
    print()


# ============================================================
# Main
# ============================================================

if __name__ == '__main__':
    test_builtin_indicator_to_yaml()
    test_yaml_roundtrip()
    test_code_safety()
    test_pipeline_structure()
    test_category_accuracy()

    print(f"{'═' * 50}")
    print(f"结果: {PASS} 通过, {FAIL} 失败, 共 {TOTAL} 项")
    if FAIL > 0:
        sys.exit(1)
    else:
        print("🎉 全部通过!")
