#!/usr/bin/env python3
"""
yaml_to_indicator.py 完整正确性测试套件

覆盖 11 个测试维度，共 94 项检查：
    1.  QuantDingerStandards 完整性（与 safe_exec.py 源码对齐）
    2.  YAMLStrategyParser（解析/异常/边界）
    3.  CodeValidator — 合法代码通过
    4.  CodeValidator — 危险代码拒绝（import/调用/dunder/on_bar/__name__）
    5.  CodeValidator — 边界情况（双引号/注释/合法模块）
    6.  CodePostProcessor（import 注入/fillna/去重/注释保序）
    7.  LLMStrategyGenerator._clean_code_output（markdown 清理/尾部截断）
    8.  ConversionPipeline._auto_fix（raw_buy→buy/on_bar 修复）
    9.  端到端 Dry Run（单文件/目录/统计）
    10. 真实指标代码校验（ATR 突破/KDJ+VWAP）
    11. LLM API 调用结构（prompt 内容/api_base 去斜杠）

用法：
    python3 test_yaml_to_indicator.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

# 确保能 import 被测模块
sys.path.insert(0, str(Path(__file__).parent))
from yaml_to_indicator import (
    QuantDingerStandards,
    YAMLStrategyParser,
    CodeValidator,
    CodePostProcessor,
    LLMStrategyGenerator,
    ConversionPipeline,
)

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def test_standards_completeness():
    """验证 QuantDingerStandards 与源码 safe_exec.py 一致"""
    print("\n═══ 1. QuantDingerStandards 完整性 ═══")

    # SAFE_IMPORT_MODULES 应包含 numpy/pandas
    check("numpy 在白名单", 'numpy' in QuantDingerStandards.SAFE_IMPORT_MODULES)
    check("pandas 在白名单", 'pandas' in QuantDingerStandards.SAFE_IMPORT_MODULES)
    check("math 在白名单", 'math' in QuantDingerStandards.SAFE_IMPORT_MODULES)

    # 不应包含危险模块
    check("os 不在白名单", 'os' not in QuantDingerStandards.SAFE_IMPORT_MODULES)
    check("sys 不在白名单", 'sys' not in QuantDingerStandards.SAFE_IMPORT_MODULES)
    check("requests 不在白名单", 'requests' not in QuantDingerStandards.SAFE_IMPORT_MODULES)

    # DANGEROUS_MODULES 应完整
    check("os 在危险模块集", 'os' in QuantDingerStandards.DANGEROUS_MODULES)
    check("subprocess 在危险模块集", 'subprocess' in QuantDingerStandards.DANGEROUS_MODULES)
    check("ctypes 在危险模块集", 'ctypes' in QuantDingerStandards.DANGEROUS_MODULES)

    # DANGEROUS_CALLS 应完整
    check("eval 在危险调用集", 'eval' in QuantDingerStandards.DANGEROUS_CALLS)
    check("exec 在危险调用集", 'exec' in QuantDingerStandards.DANGEROUS_CALLS)
    check("open 在危险调用集", 'open' in QuantDingerStandards.DANGEROUS_CALLS)
    check("getattr 在危险调用集", 'getattr' in QuantDingerStandards.DANGEROUS_CALLS)

    # @strategy 注解 key 应完整
    check("stopLossPct 合法", 'stopLossPct' in QuantDingerStandards.STRATEGY_ANNOTATION_KEYS)
    check("tradeDirection 合法", 'tradeDirection' in QuantDingerStandards.STRATEGY_ANNOTATION_KEYS)
    check("invalidKey 不合法", 'invalidKey' not in QuantDingerStandards.STRATEGY_ANNOTATION_KEYS)

    # @param 类型应完整
    check("int 类型合法", 'int' in QuantDingerStandards.PARAM_TYPES)
    check("float 类型合法", 'float' in QuantDingerStandards.PARAM_TYPES)
    check("bool 类型合法", 'bool' in QuantDingerStandards.PARAM_TYPES)
    check("str 类型合法", 'str' in QuantDingerStandards.PARAM_TYPES)
    check("string 类型合法", 'string' in QuantDingerStandards.PARAM_TYPES)
    check("list 类型不合法", 'list' not in QuantDingerStandards.PARAM_TYPES)


def test_yaml_parser():
    """测试 YAML 解析器"""
    print("\n═══ 2. YAMLStrategyParser ═══")

    strategies_dir = Path(__file__).parent / "backend_api_python" / "strategies"
    if not strategies_dir.is_dir():
        print("  ⚠️  跳过（目录不存在）")
        return

    # 解析单个文件
    sample = strategies_dir / "ema_rsi_pullback.yaml"
    if sample.exists():
        s = YAMLStrategyParser.parse(str(sample))
        check("解析 ema_rsi_pullback.yaml", s['name'] == 'ema_rsi_pullback', f"got: {s['name']}")
        check("有 display_name", bool(s['display_name']))
        check("有 instructions", bool(s['instructions']), "instructions 为空")
        check("有 category", s['category'] in ('trend', 'reversal', 'framework', 'pattern', 'unknown'),
              f"got: {s['category']}")
        check("source_file 正确", s['source_file'].endswith('ema_rsi_pullback.yaml'))

    # 解析目录
    all_strategies = YAMLStrategyParser.parse_directory(str(strategies_dir))
    check("目录解析数量 >= 15", len(all_strategies) >= 15, f"got: {len(all_strategies)}")
    check("每个策略都有 name", all(s['name'] for s in all_strategies))
    check("每个策略都有 instructions", all(s['instructions'] for s in all_strategies))

    # 不存在的文件
    try:
        YAMLStrategyParser.parse("/nonexistent/file.yaml")
        check("不存在文件应抛异常", False)
    except FileNotFoundError:
        check("不存在文件抛 FileNotFoundError", True)

    # 不存在的目录
    try:
        YAMLStrategyParser.parse_directory("/nonexistent/dir/")
        check("不存在目录应抛异常", False)
    except NotADirectoryError:
        check("不存在目录抛 NotADirectoryError", True)

    # 空 YAML 文件
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write("")
        f.flush()
        try:
            YAMLStrategyParser.parse(f.name)
            check("空文件应抛异常", False)
        except Exception:
            check("空文件抛异常", True)
        finally:
            os.unlink(f.name)

    # 缺少 instructions 字段的 YAML
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write("name: test\ndescription: test\n")
        f.flush()
        try:
            YAMLStrategyParser.parse(f.name)
            check("缺 instructions 应抛异常", False)
        except ValueError as e:
            check("缺 instructions 抛 ValueError", 'instructions' in str(e))
        finally:
            os.unlink(f.name)


def test_code_validator_clean():
    """测试校验器对合法代码的通过"""
    print("\n═══ 3. CodeValidator — 合法代码 ═══")

    clean_code = """
# IndicatorStrategy: test
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
"""
    is_valid, errors = CodeValidator.validate(clean_code)
    check("合法代码校验通过", is_valid, f"errors: {errors}")


def test_code_validator_dangerous():
    """测试校验器对危险代码的拒绝"""
    print("\n═══ 4. CodeValidator — 危险代码 ═══")

    # 危险 import
    for mod in ['os', 'sys', 'subprocess', 'requests', 'socket', 'ctypes', 'pickle']:
        code = f"import {mod}\ndf['buy'] = True\ndf['sell'] = False"
        is_valid, errors = CodeValidator.validate(code)
        check(f"拒绝 import {mod}", not is_valid, f"errors: {errors}")

    # 危险函数调用
    for func in ['eval', 'exec', 'open', 'getattr', 'setattr']:
        code = f"x = {func}('test')\ndf['buy'] = True\ndf['sell'] = False"
        is_valid, errors = CodeValidator.validate(code)
        check(f"拒绝 {func}()", not is_valid)

    # dunder 属性访问
    code = "x = obj.__dict__\ndf['buy'] = True\ndf['sell'] = False"
    is_valid, errors = CodeValidator.validate(code)
    check("拒绝 __dict__ 访问", not is_valid)

    # on_bar 函数定义
    code = "def on_bar(ctx, bar):\n    df['buy'] = True\ndf['sell'] = False"
    is_valid, errors = CodeValidator.validate(code)
    check("拒绝 on_bar 定义", not is_valid)

    # if __name__ 块
    code = "df['buy'] = True\ndf['sell'] = False\nif __name__ == '__main__':\n    pass"
    is_valid, errors = CodeValidator.validate(code)
    check("拒绝 if __name__", not is_valid)

    # 缺少 df['buy']
    code = "df['sell'] = df['close'] < 100"
    is_valid, errors = CodeValidator.validate(code)
    check("拒绝缺少 df['buy']", not is_valid)

    # 缺少 df['sell']
    code = "df['buy'] = df['close'] > 100"
    is_valid, errors = CodeValidator.validate(code)
    check("拒绝缺少 df['sell']", not is_valid)

    # 无效 @strategy key
    code = "# @strategy invalidKey 0.5\ndf['buy'] = True\ndf['sell'] = False"
    is_valid, errors = CodeValidator.validate(code)
    check("拒绝无效 @strategy key", not is_valid)

    # 无效 @param 类型
    code = "# @param x list 5 bad\ndf['buy'] = True\ndf['sell'] = False"
    is_valid, errors = CodeValidator.validate(code)
    check("拒绝无效 @param 类型", not is_valid)

    # 语法错误
    code = "def foo(\ndf['buy'] = True\ndf['sell'] = False"
    is_valid, errors = CodeValidator.validate(code)
    check("拒绝语法错误代码", not is_valid)


def test_code_validator_edge_cases():
    """测试校验器边界情况"""
    print("\n═══ 5. CodeValidator — 边界情况 ═══")

    # df["buy"] 双引号也应识别
    code = 'df["buy"] = True\ndf["sell"] = False'
    is_valid, errors = CodeValidator.validate(code)
    check("双引号 df[\"buy\"] 识别", is_valid, f"errors: {errors}")

    # 注释中的 eval 不应触发 AST 检查（但正则会触发 —— 这是与 safe_exec.py 的已知差异）
    code = "# This uses eval() for testing\ndf['buy'] = True\ndf['sell'] = False"
    is_valid, errors = CodeValidator.validate(code)
    # 正则会匹配注释中的 eval，这是保守行为（与 safe_exec.py 一致）
    check("注释中 eval 触发正则（保守策略）", not is_valid, "与 safe_exec.py 行为一致")

    # numpy/pandas import 合法
    code = "import numpy as np\nimport pandas as pd\ndf['buy'] = True\ndf['sell'] = False"
    is_valid, errors = CodeValidator.validate(code)
    check("numpy/pandas import 合法", is_valid, f"errors: {errors}")

    # math import 合法
    code = "import math\nimport json\ndf['buy'] = True\ndf['sell'] = False"
    is_valid, errors = CodeValidator.validate(code)
    check("math/json import 合法", is_valid, f"errors: {errors}")


def test_post_processor():
    """测试后处理器"""
    print("\n═══ 6. CodePostProcessor ═══")

    # 基本处理
    code = "df['buy'] = condition\ndf['sell'] = condition2"
    result = CodePostProcessor.process(code, "test_strategy")
    check("添加头部注释", "# IndicatorStrategy: test_strategy" in result)
    check("添加 import pandas", "import pandas as pd" in result)
    check("添加 import numpy", "import numpy as np" in result)
    check("添加 fillna buy", "df['buy'].fillna(False)" in result)
    check("添加 fillna sell", "df['sell'].fillna(False)" in result)
    check("添加 astype bool", ".astype(bool)" in result)

    # 已有 fillna 不重复添加
    code2 = "df['buy'] = cond.fillna(False)\ndf['sell'] = cond2.fillna(False)"
    result2 = CodePostProcessor.process(code2, "test2")
    # fillna 应该只出现一次（在原始代码中），加上尾部的 fillna+astype
    # 但尾部检查 "df['buy'].fillna" 已在 code2 中，所以不追加
    check("已有 fillna 不重复添加 buy", result2.count("df['buy'].fillna") == 1)
    check("已有 fillna 不重复添加 sell", result2.count("df['sell'].fillna") == 1)

    # 已有 import 不重复添加
    code3 = "import pandas as pd\nimport numpy as np\ndf['buy'] = True\ndf['sell'] = False"
    result3 = CodePostProcessor.process(code3, "test3")
    check("不重复 import pandas", result3.count("import pandas as pd") == 1)
    check("不重复 import numpy", result3.count("import numpy as np") == 1)

    # @param 注释应保留在 import 之前
    code4 = "# @param period int 14 RSI周期\nimport pandas as pd\ndf['buy'] = True\ndf['sell'] = False"
    result4 = CodePostProcessor.process(code4, "test4")
    lines = result4.split('\n')
    param_line = next((i for i, l in enumerate(lines) if '@param' in l), -1)
    import_line = next((i for i, l in enumerate(lines) if 'import pandas' in l), -1)
    check("@param 在 import 之前", param_line < import_line, f"param={param_line}, import={import_line}")


def test_llm_clean_output():
    """测试 LLM 输出清理"""
    print("\n═══ 7. LLMStrategyGenerator._clean_code_output ═══")

    # markdown 代码块
    content = "Here is the code:\n```python\ndf['buy'] = True\ndf['sell'] = False\n```\nHope this helps!"
    result = LLMStrategyGenerator._clean_code_output(content)
    check("提取 ```python 代码块", "df['buy'] = True" in result)
    check("去除 Hope this helps", "Hope this helps" not in result)

    # 无代码块标记
    content2 = "Here is the strategy:\ndf['buy'] = True\ndf['sell'] = False\nThis is a great strategy."
    result2 = LLMStrategyGenerator._clean_code_output(content2)
    check("无代码块时提取代码", "df['buy'] = True" in result2)
    # 尾部解释文字应被截断（如果最后一行代码后有纯文字）
    check("截断尾部解释", "great strategy" not in result2, f"got: {result2[-100:]}")

    # 纯代码无标记
    content3 = "import pandas as pd\ndf['buy'] = True\ndf['sell'] = False"
    result3 = LLMStrategyGenerator._clean_code_output(content3)
    check("纯代码保持原样", result3.strip() == content3)


def test_auto_fix():
    """测试自动修复"""
    print("\n═══ 8. ConversionPipeline._auto_fix ═══")

    pipeline = ConversionPipeline(dry_run=True)

    # 修复 raw_buy → buy
    code1 = "df['raw_buy'] = condition\ndf['raw_sell'] = condition2"
    fixed1, count1 = pipeline._auto_fix(code1, ["缺少 df['buy'] 信号定义", "缺少 df['sell'] 信号定义"])
    check("raw_buy → buy", "df['buy']" in fixed1 and "df['raw_buy']" not in fixed1)
    check("raw_sell → sell", "df['sell']" in fixed1 and "df['raw_sell']" not in fixed1)
    check("修复计数 = 2", count1 == 2, f"got: {count1}")

    # 修复 signal_buy → buy
    code2 = "df['signal_buy'] = condition\ndf['signal_sell'] = condition2"
    fixed2, count2 = pipeline._auto_fix(code2, ["缺少 df['buy'] 信号定义", "缺少 df['sell'] 信号定义"])
    check("signal_buy → buy", "df['buy']" in fixed2 and "df['signal_buy']" not in fixed2)

    # 修复 on_bar
    code3 = "def on_bar(ctx, bar):\n    df['buy'] = True\n    df['sell'] = False"
    fixed3, count3 = pipeline._auto_fix(code3, ["不应定义 on_bar 函数（IndicatorStrategy 是顶层脚本）"])
    check("on_bar 去除", "def on_bar" not in fixed3)
    check("函数体保留", "df['buy'] = True" in fixed3)


def test_end_to_end_dry_run():
    """端到端 dry run 测试"""
    print("\n═══ 9. 端到端 Dry Run ═══")

    strategies_dir = Path(__file__).parent / "backend_api_python" / "strategies"
    if not strategies_dir.is_dir():
        print("  ⚠️  跳过（目录不存在）")
        return

    pipeline = ConversionPipeline(dry_run=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        # 转换单个文件
        result = pipeline.convert_file(str(strategies_dir / "ema_rsi_pullback.yaml"), tmpdir)
        check("单文件 dry run 返回 None", result is None)
        check("统计: skipped=1", pipeline.stats['skipped'] >= 1)

    # 重置统计
    pipeline2 = ConversionPipeline(dry_run=True)
    results = pipeline2.convert_directory(str(strategies_dir))
    check("目录 dry run 返回空列表", results == [])
    check(f"统计: total={pipeline2.stats['total']}", pipeline2.stats['total'] >= 15)
    check(f"统计: skipped={pipeline2.stats['skipped']}", pipeline2.stats['skipped'] >= 15)


def test_validator_against_real_indicators():
    """用 QuantDinger 项目中真实的指标代码测试校验器"""
    print("\n═══ 10. 真实指标代码校验 ═══")

    # 从 optimizer/strategy_templates_ashare.py 中提取一个真实策略 config
    # 模拟 ATR 突破策略的 IndicatorStrategy 代码
    atr_code = """
# IndicatorStrategy: ATR Breakout
# @strategy stopLossPct 0.03
# @strategy tradeDirection long
# @param atr_period int 14 ATR周期
# @param atr_multiplier float 2.0 ATR倍数

import pandas as pd
import numpy as np

period = params.get('atr_period', 14)
mult = params.get('atr_multiplier', 2.0)

# ATR 计算
df['tr'] = np.maximum(
    df['high'] - df['low'],
    np.maximum(
        abs(df['high'] - df['close'].shift(1)),
        abs(df['low'] - df['close'].shift(1))
    )
)
df['atr'] = df['tr'].ewm(alpha=1/period, adjust=False).mean()

# 通道
df['upper'] = df['close'] + mult * df['atr']
df['lower'] = df['close'] - mult * df['atr']

# 信号
df['buy'] = (df['close'] > df['upper'].shift(1)).fillna(False)
df['sell'] = (df['close'] < df['lower'].shift(1)).fillna(False)

df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
"""
    is_valid, errors = CodeValidator.validate(atr_code)
    check("ATR 突破策略校验通过", is_valid, f"errors: {errors}")

    # KDJ + VWAP 策略
    kdj_vwap_code = """
# IndicatorStrategy: KDJ+VWAP Reversal
# @strategy stopLossPct 0.03
# @strategy tradeDirection long
# @param kdj_period int 9 KDJ周期
# @param vwap_dev float 0.02 VWAP偏离度

import pandas as pd
import numpy as np

period = params.get('kdj_period', 9)
sig = 3

# KDJ
low_min = df['low'].rolling(window=period).min()
high_max = df['high'].rolling(window=period).max()
rsv = (df['close'] - low_min) / (high_max - low_min) * 100
k = rsv.ewm(alpha=1/sig, adjust=False).mean()
d = k.ewm(alpha=1/sig, adjust=False).mean()
df['kdj_j'] = 3 * k - 2 * d

# VWAP
df['vwap'] = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()

dev = params.get('vwap_dev', 0.02)
df['buy'] = ((df['kdj_j'] < 20) & (df['close'] < df['vwap'] * (1 - dev))).fillna(False)
df['sell'] = ((df['close'] > df['vwap']) | (df['kdj_j'] > 80)).fillna(False)

df['buy'] = df['buy'].astype(bool)
df['sell'] = df['sell'].astype(bool)
"""
    is_valid2, errors2 = CodeValidator.validate(kdj_vwap_code)
    check("KDJ+VWAP 策略校验通过", is_valid2, f"errors: {errors2}")


def test_urllib_api_call_structure():
    """测试 LLM API 调用的请求结构（不实际调用）"""
    print("\n═══ 11. LLM API 调用结构 ═══")

    gen = LLMStrategyGenerator(
        api_base="https://api.openai.com/v1",
        api_key="sk-test-key",
        model="gpt-4o",
    )

    # 验证 prompt 构建
    strategy = {
        'name': 'test_strat',
        'display_name': '测试策略',
        'category': 'trend',
        'description': '测试描述',
        'instructions': '买入条件：RSI < 30\n卖出条件：RSI > 70',
    }
    prompt = gen._build_user_prompt(strategy)
    check("prompt 包含策略名", 'test_strat' in prompt)
    check("prompt 包含分类", 'trend' in prompt)
    check("prompt 包含 instructions", 'RSI < 30' in prompt)
    check("prompt 包含 @param 要求", '@param' in prompt)
    check("prompt 包含 @strategy 要求", '@strategy' in prompt)

    # 验证 api_base 去尾斜杠
    gen2 = LLMStrategyGenerator("https://api.openai.com/v1/", "sk-test", "gpt-4o")
    check("api_base 去尾斜杠", gen2.api_base == "https://api.openai.com/v1")


if __name__ == '__main__':
    test_standards_completeness()
    test_yaml_parser()
    test_code_validator_clean()
    test_code_validator_dangerous()
    test_code_validator_edge_cases()
    test_post_processor()
    test_llm_clean_output()
    test_auto_fix()
    test_end_to_end_dry_run()
    test_validator_against_real_indicators()
    test_urllib_api_call_structure()

    print(f"\n{'═' * 50}")
    print(f"结果: {PASS} 通过, {FAIL} 失败, 共 {PASS + FAIL} 项")
    if FAIL > 0:
        sys.exit(1)
    else:
        print("🎉 全部通过!")
