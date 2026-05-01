"""
从 elite_strategies_adaptive_vol.json 生成可执行策略代码。

生成内容：
1. strategies/ 目录下每只股票一个独立策略文件
2. strategies/_portfolio_runner.py — 组合管理器，同时运行所有策略
3. strategies/_signal_scanner.py — 信号扫描器，检查当前哪些股票有买入信号

用法（在项目根目录执行）：
    python -m optimizer.generate_strategies
"""

import json
import os
import sys
import time


def generate_strategies():
    # 导入编译器和模板
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from optimizer.strategy_compiler import StrategyCompiler
    from optimizer.strategies_generated import GENERATED_TEMPLATES

    elite_path = os.path.join(os.path.dirname(__file__), 'elite_strategies_adaptive_vol.json')
    if not os.path.exists(elite_path):
        print(f'错误: 找不到 {elite_path}')
        print('请先运行 python -m optimizer.extract_elite_stocks')
        return

    with open(elite_path, 'r', encoding='utf-8') as f:
        elite = json.load(f)

    stocks = elite.get('stocks', {})
    if not stocks:
        print('错误: 没有双优股票')
        return

    # 获取 adaptive_volatility 模板
    template = GENERATED_TEMPLATES.get('adaptive_volatility')
    if not template:
        print('错误: 找不到 adaptive_volatility 模板')
        return

    build_config = template['build_config']
    compiler = StrategyCompiler()

    # 创建输出目录
    out_dir = os.path.join(os.path.dirname(__file__), 'strategies')
    os.makedirs(out_dir, exist_ok=True)

    print(f'开始生成 {len(stocks)} 只股票的策略代码...')

    strategy_map = {}  # {symbol: {params, code, config, ...}}
    failed = []

    for i, (symbol, info) in enumerate(stocks.items()):
        params = info.get('best_params', {})
        if not params:
            failed.append(symbol)
            continue

        try:
            # 1. 参数 → 配置
            config = build_config(params)

            # 2. 注入 A 股规则
            config['risk_management']['stop_loss']['enabled'] = True
            config['risk_management']['trailing_stop'] = {'enabled': False}

            # 3. 编译为代码
            code = compiler.compile(config)

            # 4. 包装为独立策略文件
            safe_sym = symbol.replace('.', '_')
            file_code = _wrap_strategy(code, symbol, params, config)

            file_path = os.path.join(out_dir, f'{safe_sym}.py')
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(file_code)

            strategy_map[symbol] = {
                'file': f'{safe_sym}.py',
                'params': params,
                'config': config,
                'score': info.get('best_score'),
                'wf_score': info.get('wf_test_score'),
                'sharpe': info.get('metrics', {}).get('sharpeRatio'),
                'return_pct': info.get('metrics', {}).get('totalReturn'),
                'win_rate': info.get('metrics', {}).get('winRate'),
            }

        except Exception as e:
            failed.append(f'{symbol}: {e}')

        if (i + 1) % 20 == 0:
            print(f'  进度 {i+1}/{len(stocks)} ...')

    # 保存策略映射
    map_path = os.path.join(out_dir, '_strategy_map.json')
    with open(map_path, 'w', encoding='utf-8') as f:
        json.dump(strategy_map, f, ensure_ascii=False, indent=2)

    # 生成组合管理器
    _generate_portfolio_runner(out_dir, strategy_map)

    # 生成信号扫描器
    _generate_signal_scanner(out_dir, strategy_map)

    print(f'\n完成: 成功 {len(strategy_map)}, 失败 {len(failed)}')
    print(f'输出目录: {out_dir}')
    if failed:
        print(f'失败列表: {failed[:10]}{"..." if len(failed) > 10 else ""}')

    print(f'\n生成文件:')
    print(f'  {len(strategy_map)} 个独立策略文件')
    print(f'  strategies/_strategy_map.json — 策略映射表')
    print(f'  strategies/_portfolio_runner.py — 组合管理器')
    print(f'  strategies/_signal_scanner.py — 信号扫描器')


def _wrap_strategy(code, symbol, params, config):
    """将编译后的代码包装为可独立运行的策略文件"""
    # 将编译后的代码每行缩进 4 空格，使其成为函数体
    indented_code = '\n'.join(
        '    ' + line if line.strip() else ''
        for line in code.split('\n')
    )
    return f'''"""
策略: adaptive_volatility
股票: {symbol}
生成时间: {time.strftime("%Y-%m-%d %H:%M")}

参数:
{json.dumps(params, indent=2, ensure_ascii=False)}

使用方法:
    import pandas as pd
    from strategies.{symbol.replace(".", "_")} import generate_signals

    df = pd.read_csv("your_data.csv")  # 需要 open, high, low, close, volume 列
    signals = generate_signals(df)
    # signals 包含 raw_buy, raw_sell 列
"""

import pandas as pd
import numpy as np


def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    在 DataFrame 上生成交易信号。

    输入: df 需要包含 open, high, low, close, volume 列
    输出: df 增加 raw_buy, raw_sell 列（布尔值）
    """
    df = df.copy()

    # ===========================
    # 策略代码（由 StrategyCompiler 编译）
    # ===========================
{indented_code}

    return df


def get_latest_signal(df: pd.DataFrame) -> dict:
    """获取最新一根 K 线的信号状态"""
    df = generate_signals(df)
    last = df.iloc[-1]
    return {{
        "symbol": "{symbol}",
        "date": str(last.get("date", last.name)),
        "close": last["close"],
        "raw_buy": bool(last.get("raw_buy", False)),
        "raw_sell": bool(last.get("raw_sell", False)),
    }}


# 参数（方便外部读取）
PARAMS = {json.dumps(params, ensure_ascii=False)}
CONFIG = {json.dumps(config, ensure_ascii=False, default=str)}
'''


def _generate_portfolio_runner(out_dir, strategy_map):
    """生成组合管理器"""
    symbols = list(strategy_map.keys())
    code = f'''"""
组合管理器 — 同时运行 {len(symbols)} 只股票的 adaptive_volatility 策略

功能：
1. 加载所有股票数据
2. 逐只运行策略生成信号
3. 汇总输出：哪些股票当前有买入/卖出信号
4. 计算组合级别的统计信息

使用方法：
    python strategies/_portfolio_runner.py --data-dir ./data/daily

数据格式：
    每只股票一个 CSV 文件，文件名格式: 000603.SZ.csv
    列: date, open, high, low, close, volume
"""

import json
import os
import sys
import glob
import argparse
import importlib.util

# 策略列表
STRATEGIES = {json.dumps({s: strategy_map[s]['file'] for s in symbols}, ensure_ascii=False, indent=2)}


def load_strategy(symbol):
    """动态加载策略模块"""
    safe_sym = symbol.replace('.', '_')
    file_path = os.path.join(os.path.dirname(__file__), f'{{safe_sym}}.py')
    if not os.path.exists(file_path):
        return None
    spec = importlib.util.spec_from_file_location(safe_sym, file_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scan_signals(data_dir, date=None):
    """扫描所有股票的信号"""
    import pandas as pd

    results = {{"buy": [], "sell": [], "hold": [], "no_data": [], "error": []}}

    for symbol, file in STRATEGIES.items():
        # 加载数据
        safe_sym = symbol.replace('.', '_')
        data_file = os.path.join(data_dir, f'{{safe_sym}}.csv')
        if not os.path.exists(data_file):
            # 尝试原始格式
            data_file = os.path.join(data_dir, f'{{symbol}}.csv')
        if not os.path.exists(data_file):
            results["no_data"].append(symbol)
            continue

        try:
            df = pd.read_csv(data_file)
            mod = load_strategy(symbol)
            if mod is None:
                results["error"].append(f"{{symbol}}: 策略文件不存在")
                continue

            sig = mod.get_latest_signal(df)
            if sig["raw_buy"]:
                results["buy"].append(sig)
            elif sig["raw_sell"]:
                results["sell"].append(sig)
            else:
                results["hold"].append(sig)

        except Exception as e:
            results["error"].append(f"{{symbol}}: {{e}}")

    return results


def main():
    parser = argparse.ArgumentParser(description='组合策略信号扫描')
    parser.add_argument('--data-dir', required=True, help='股票数据目录')
    args = parser.parse_args()

    print(f'扫描 {{len(STRATEGIES)}} 只股票的信号...')
    results = scan_signals(args.data_dir)

    print(f'\\n===== 信号汇总 =====')
    print(f'买入信号: {{len(results["buy"])}} 只')
    for sig in results["buy"]:
        print(f'  🟢 {{sig["symbol"]}}  收盘 {{sig["close"]:.2f}}  日期 {{sig["date"]}}')

    print(f'\\n卖出信号: {{len(results["sell"])}} 只')
    for sig in results["sell"]:
        print(f'  🔴 {{sig["symbol"]}}  收盘 {{sig["close"]:.2f}}  日期 {{sig["date"]}}')

    print(f'\\n无信号: {{len(results["hold"])}} 只')
    print(f'无数据: {{len(results["no_data"])}} 只')
    if results["error"]:
        print(f'错误: {{len(results["error"])}} 只')
        for e in results["error"][:5]:
            print(f'  ⚠️ {{e}}')


if __name__ == '__main__':
    main()
'''
    path = os.path.join(out_dir, '_portfolio_runner.py')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(code)


def _generate_signal_scanner(out_dir, strategy_map):
    """生成信号扫描器（轻量版，只检查最新信号）"""
    symbols = list(strategy_map.keys())
    code = f'''"""
信号扫描器 — 快速检查哪些股票有买入信号

用法：
    python strategies/_signal_scanner.py --data-dir ./data/daily --top 20

输出：按得分排序的买入信号列表
"""

import json
import os
import sys
import argparse
import importlib.util

# 加载策略映射
MAP_PATH = os.path.join(os.path.dirname(__file__), '_strategy_map.json')
with open(MAP_PATH, 'r', encoding='utf-8') as f:
    STRATEGY_MAP = json.load(f)


def load_strategy(symbol):
    safe_sym = symbol.replace('.', '_')
    file_path = os.path.join(os.path.dirname(__file__), f'{{safe_sym}}.py')
    if not os.path.exists(file_path):
        return None
    spec = importlib.util.spec_from_file_location(safe_sym, file_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scan(data_dir, top_n=20):
    import pandas as pd

    buy_signals = []

    for symbol, meta in STRATEGY_MAP.items():
        safe_sym = symbol.replace('.', '_')
        data_file = os.path.join(data_dir, f'{{safe_sym}}.csv')
        if not os.path.exists(data_file):
            data_file = os.path.join(data_dir, f'{{symbol}}.csv')
        if not os.path.exists(data_file):
            continue

        try:
            df = pd.read_csv(data_file)
            mod = load_strategy(symbol)
            if mod is None:
                continue
            sig = mod.get_latest_signal(df)
            if sig["raw_buy"]:
                sig["score"] = meta.get("score", 0)
                sig["wf_score"] = meta.get("wf_score", 0)
                sig["sharpe"] = meta.get("sharpe", 0)
                sig["return_pct"] = meta.get("return_pct", 0)
                sig["win_rate"] = meta.get("win_rate", 0)
                buy_signals.append(sig)
        except Exception:
            pass

    # 按得分排序
    buy_signals.sort(key=lambda x: x.get("score", 0), reverse=True)

    print(f'\\n===== 当前买入信号 (Top {{top_n}}) =====')
    print(f'共 {{len(buy_signals)}} 只股票有买入信号\\n')
    print(f'{{"排名":>4s}}  {{"股票":>12s}}  {{"收盘":>8s}}  {{"得分":>6s}}  {{"WF":>7s}}  {{"Sharpe":>7s}}  {{"收益":>8s}}  {{"胜率":>5s}}')
    print('-' * 75)

    for i, sig in enumerate(buy_signals[:top_n]):
        print(
            f'{{i+1:>4d}}  {{sig["symbol"]:>12s}}  {{sig["close"]:>8.2f}}  '
            f'{{sig.get("score",0):>6.2f}}  {{sig.get("wf_score",0):>+7.2f}}  '
            f'{{sig.get("sharpe",0):>+7.2f}}  {{sig.get("return_pct",0):>+7.1f}}%  '
            f'{{sig.get("win_rate",0):>5.1f}}%'
        )

    return buy_signals


def main():
    parser = argparse.ArgumentParser(description='快速信号扫描')
    parser.add_argument('--data-dir', required=True, help='股票数据目录')
    parser.add_argument('--top', type=int, default=20, help='显示前N个')
    args = parser.parse_args()

    scan(args.data_dir, args.top)


if __name__ == '__main__':
    main()
'''
    path = os.path.join(out_dir, '_signal_scanner.py')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(code)


if __name__ == '__main__':
    generate_strategies()
