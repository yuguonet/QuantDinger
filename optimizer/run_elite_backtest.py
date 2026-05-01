"""
用已优化参数批量回测双优股票（不重新优化）。

读取 elite_strategies_adaptive_vol.json 中的最优参数，
直接跑回测验证，跳过 Optuna 搜索。

用法：
    python optimizer/run_elite_backtest.py --start 2024-01-01 --end 2025-12-31
    python optimizer/run_elite_backtest.py --top 10 --score composite
    python optimizer/run_elite_backtest.py --symbol 300925.SZ
"""

import json
import os
import sys
import argparse
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def run_elite_backtest(
    start_date='2024-01-01',
    end_date='2025-12-31',
    score_fn='composite',
    top_n=None,
    symbol_filter=None,
    initial_capital=100000,
    commission=0.001,
):
    # 导入回测引擎
    from optimizer.strategy_compiler import StrategyCompiler
    from optimizer.strategies_generated import GENERATED_TEMPLATES

    # 加载 elite 策略映射
    elite_path = os.path.join(os.path.dirname(__file__), 'elite_strategies_adaptive_vol.json')
    if not os.path.exists(elite_path):
        print(f'错误: 找不到 {elite_path}')
        print('请先运行 python -m optimizer.extract_elite_stocks')
        return

    with open(elite_path, 'r', encoding='utf-8') as f:
        elite = json.load(f)

    stocks = elite.get('stocks', {})

    # 过滤
    if symbol_filter:
        stocks = {k: v for k, v in stocks.items() if k in symbol_filter}
    if top_n:
        sorted_items = sorted(stocks.items(), key=lambda x: x[1].get('wf_test_score', 0), reverse=True)
        stocks = dict(sorted_items[:top_n])

    if not stocks:
        print('没有匹配的股票')
        return

    # 初始化引擎
    template = GENERATED_TEMPLATES.get('adaptive_volatility')
    build_config = template['build_config']
    compiler = StrategyCompiler()

    # 加载 BacktestService
    from app.services.backtest import BacktestService
    backtest = BacktestService()

    print(f'回测 {len(stocks)} 只股票')
    print(f'区间: {start_date} ~ {end_date}')
    print(f'{"="*70}')

    results = []
    errors = []

    for i, (symbol, info) in enumerate(stocks.items()):
        params = info.get('best_params', {})
        if not params:
            errors.append(f'{symbol}: 无参数')
            continue

        try:
            # 1. 参数 → 配置 → 编译
            config = build_config(params)
            code = compiler.compile(config)

            # 2. 回测
            sd = datetime.strptime(start_date, '%Y-%m-%d')
            ed = datetime.strptime(end_date, '%Y-%m-%d')

            result = backtest.run(
                indicator_code=code,
                market='CNStock',
                symbol=symbol,
                timeframe='1D',
                start_date=sd,
                end_date=ed,
                initial_capital=initial_capital,
                commission=commission,
            )

            # 3. 提取指标
            metrics = result.get('metrics', {})
            sharpe = metrics.get('sharpeRatio', 0)
            ret = metrics.get('totalReturn', 0)
            win_rate = metrics.get('winRate', 0)
            max_dd = metrics.get('maxDrawdown', 0)
            trades = metrics.get('totalTrades', 0)

            row = {
                'symbol': symbol,
                'sharpe': sharpe,
                'return_pct': ret,
                'win_rate': win_rate,
                'max_drawdown': max_dd,
                'trades': trades,
                'original_score': info.get('best_score'),
                'original_wf': info.get('wf_test_score'),
            }
            results.append(row)

            wf_mark = '✅' if info.get('wf_test_score', 0) > 0 else '❌'
            print(
                f'{i+1:>3d}/{len(stocks)}  {symbol:>12s}  '
                f'Sharpe={sharpe:>+6.2f}  收益={ret:>+8.1f}%  '
                f'胜率={win_rate:>5.1f}%  回撤={max_dd:>+7.1f}%  '
                f'交易={trades:>3d}  WF{wf_mark}'
            )

        except Exception as e:
            errors.append(f'{symbol}: {e}')
            print(f'{i+1:>3d}/{len(stocks)}  {symbol:>12s}  ❌ {e}')

        time.sleep(0.1)

    # 汇总
    print(f'\n{"="*70}')
    print(f'回测完成: 成功 {len(results)}, 失败 {len(errors)}')

    if results:
        import statistics
        avg_sharpe = statistics.mean(r['sharpe'] for r in results)
        avg_return = statistics.mean(r['return_pct'] for r in results)
        avg_wr = statistics.mean(r['win_rate'] for r in results)
        positive = sum(1 for r in results if r['return_pct'] > 0)

        print(f'\n--- 汇总统计 ---')
        print(f'平均 Sharpe:  {avg_sharpe:+.3f}')
        print(f'平均收益:     {avg_return:+.1f}%')
        print(f'平均胜率:     {avg_wr:.1f}%')
        print(f'正收益占比:   {positive}/{len(results)} ({positive/len(results)*100:.0f}%)')

        # Top 10
        results.sort(key=lambda x: x['sharpe'], reverse=True)
        print(f'\n--- Top 10 (按 Sharpe) ---')
        print(f'{"排名":>3s}  {"股票":>12s}  {"Sharpe":>7s}  {"收益":>8s}  {"胜率":>5s}  {"回撤":>7s}  {"交易":>4s}')
        for i, r in enumerate(results[:10]):
            print(
                f'{i+1:>3d}  {r["symbol"]:>12s}  '
                f'{r["sharpe"]:>+7.2f}  {r["return_pct"]:>+7.1f}%  '
                f'{r["win_rate"]:>5.1f}%  {r["max_drawdown"]:>+7.1f}%  '
                f'{r["trades"]:>4d}'
            )

    # 保存结果
    out_path = os.path.join(os.path.dirname(__file__), 'elite_backtest_results.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({
            'config': {
                'start': start_date,
                'end': end_date,
                'score_fn': score_fn,
                'initial_capital': initial_capital,
                'commission': commission,
            },
            'run_at': time.strftime('%Y-%m-%d %H:%M'),
            'total': len(results),
            'results': results,
            'errors': errors,
        }, f, ensure_ascii=False, indent=2)
    print(f'\n结果已保存: {out_path}')


def main():
    parser = argparse.ArgumentParser(description='用已优化参数批量回测双优股票')
    parser.add_argument('--start', default='2024-01-01', help='回测起始日期')
    parser.add_argument('--end', default='2025-12-31', help='回测结束日期')
    parser.add_argument('--top', type=int, default=None, help='只回测前N只（按WF排序）')
    parser.add_argument('--symbol', type=str, default=None, help='只回测指定股票（逗号分隔）')
    parser.add_argument('--score', default='composite', help='评分函数')
    args = parser.parse_args()

    symbol_filter = None
    if args.symbol:
        symbol_filter = [s.strip() for s in args.symbol.split(',')]

    run_elite_backtest(
        start_date=args.start,
        end_date=args.end,
        score_fn=args.score,
        top_n=args.top,
        symbol_filter=symbol_filter,
    )


if __name__ == '__main__':
    main()
