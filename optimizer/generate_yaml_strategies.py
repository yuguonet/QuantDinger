"""
从 elite_strategies_adaptive_vol.json 生成 YAML 策略文件。
直接放到 backend_api_python/strategies/ 目录，LLM Agent 可直接加载使用。

用法：
    python optimizer/generate_yaml_strategies.py

输出：
    backend_api_python/strategies/adaptive_vol_*.yaml (每只股票一个)
"""

import json
import os
import time


def generate_yaml():
    elite_path = os.path.join(os.path.dirname(__file__), 'elite_strategies_adaptive_vol.json')
    if not os.path.exists(elite_path):
        print(f'错误: 找不到 {elite_path}')
        return

    with open(elite_path, 'r', encoding='utf-8') as f:
        elite = json.load(f)

    stocks = elite.get('stocks', {})
    if not stocks:
        print('错误: 没有双优股票')
        return

    # 输出目录
    out_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'backend_api_python', 'strategies'
    )
    os.makedirs(out_dir, exist_ok=True)

    print(f'开始生成 {len(stocks)} 个 YAML 策略文件...')
    print(f'输出目录: {out_dir}')

    count = 0
    for symbol, info in stocks.items():
        params = info.get('best_params', {})
        if not params:
            continue

        safe_sym = symbol.replace('.', '_').replace(':', '_')
        yaml_content = _build_yaml(symbol, info, params)

        file_path = os.path.join(out_dir, f'adaptive_vol_{safe_sym}.yaml')
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(yaml_content)
        count += 1

    print(f'\n完成: 生成 {count} 个 YAML 策略文件')

    # 生成索引文件
    _generate_index(out_dir, stocks)
    print(f'索引: {out_dir}/adaptive_vol_index.json')


def _build_yaml(symbol, info, params):
    """构建单个 YAML 策略文件内容"""
    score = info.get('best_score', 0)
    wf = info.get('wf_test_score', 0)
    sharpe = info.get('metrics', {}).get('sharpeRatio', 0)
    ret = info.get('metrics', {}).get('totalReturn', 0)
    win_rate = info.get('metrics', {}).get('winRate', 0)
    drawdown = info.get('metrics', {}).get('maxDrawdown', 0)
    trades = info.get('metrics', {}).get('totalTrades', 0)

    # 参数
    rsi_period = params.get('rsi_period', 14)
    rsi_threshold = params.get('rsi_threshold', 30)
    vol_ma_period = params.get('vol_ma_period', 20)
    vol_ratio = params.get('vol_ratio', 1.5)
    stop_loss = params.get('stop_loss_pct', 3.0)

    name = f'adaptive_vol_{symbol.replace(".", "_").replace(":", "_")}'
    display_name = f'自适应波动率 {symbol}'

    return f'''# adaptive_volatility 策略 — {symbol}
# 自动生成于 {time.strftime("%Y-%m-%d %H:%M")}
# 回测得分: {score:.2f} | WF验证: {wf:+.3f} | Sharpe: {sharpe:+.2f} | 收益: {ret:+.1f}% | 胜率: {win_rate:.1f}%

name: {name}
display_name: {display_name}
description: |
  自适应波动率策略（adaptive_volatility），针对 {symbol} 优化参数。
  核心逻辑：RSI 超卖 + 布林下轨 + 放量确认 = 均值回归买入信号。
  
  回测表现（2023-05 ~ 2026-03）：
  - 综合得分: {score:.2f}
  - Walk-Forward验证: {wf:+.3f}
  - Sharpe比率: {sharpe:+.2f}
  - 总收益: {ret:+.1f}%
  - 胜率: {win_rate:.1f}%
  - 最大回撤: {drawdown:+.1f}%
  - 交易次数: {trades}

category: reversal
core_rules: [1, 2, 3, 5]
required_tools:
  - get_daily_history
  - get_realtime_quote
  - analyze_trend

default_active: false
default_priority: 50

instructions: |
  **自适应波动率策略 — {symbol}**
  
  核心逻辑：当 RSI 进入超卖区域 + 价格跌破布林带下轨 + 成交量放大时，
  判定为超跌反弹信号，买入等待均值回归。
  
  ## 优化参数（针对 {symbol} 专用）
  
  | 参数 | 值 | 说明 |
  |---|---|---|
  | RSI周期 | {rsi_period} | 计算RSI的K线数 |
  | RSI超卖阈值 | {rsi_threshold} | RSI低于此值视为超卖 |
  | 布林带周期 | 20 | 布林带均线周期 |
  | 布林带标准差 | 2.0 | 布林带宽度 |
  | 成交量均线周期 | {vol_ma_period} | 成交量MA周期 |
  | 放量倍数 | {vol_ratio} | 成交量 > MA × 此倍数 |
  | 止损比例 | {stop_loss}% | 固定止损 |
  
  ## 信号判定
  
  ### 买入条件（全部满足）
  1. **RSI超卖**：RSI({rsi_period}) < {rsi_threshold}
  2. **布林下轨**：收盘价 < 布林带下轨（SMA20 - 2σ）
  3. **放量确认**：成交量 > {vol_ma_period}日均量 × {vol_ratio}
  
  ### 卖出条件
  1. RSI > {100 - rsi_threshold}（超买区域）
  2. 或价格突破布林带上轨
  3. 或触发止损（-{stop_loss}%）
  
  ## 评分调整
  - 三重确认（RSI+布林+量能）：sentiment_score +12
  - 在 buy_reason 中注明"自适应波动率三重确认"
  
  ## 注意事项
  - 此策略参数已针对 {symbol} 做过 100 次 Optuna 优化 + Walk-Forward 验证
  - 回测期间交易 {trades} 次，胜率 {win_rate:.0f}%
  - 适合捕捉超跌反弹，不适合趋势行情
  - 建议配合大盘走势和板块热度使用
  
  ## 使用示例
  当分析 {symbol} 时，使用以下步骤：
  1. 用 `get_daily_history` 获取近 60 日 K 线数据
  2. 计算 RSI({rsi_period})、布林带(20,2)、{vol_ma_period}日成交量均线
  3. 判断是否同时满足三个买入条件
  4. 如果满足，在 buy_reason 中说明"自适应波动率信号触发"
'''


def _generate_index(out_dir, stocks):
    """生成索引文件"""
    index = {
        'description': 'adaptive_volatility 双优策略索引',
        'generated_at': time.strftime('%Y-%m-%d %H:%M'),
        'total': len(stocks),
        'strategies': {}
    }
    for symbol, info in stocks.items():
        safe_sym = symbol.replace('.', '_').replace(':', '_')
        index['strategies'][symbol] = {
            'file': f'adaptive_vol_{safe_sym}.yaml',
            'score': info.get('best_score'),
            'wf_score': info.get('wf_test_score'),
            'sharpe': info.get('metrics', {}).get('sharpeRatio'),
            'return_pct': info.get('metrics', {}).get('totalReturn'),
            'win_rate': info.get('metrics', {}).get('winRate'),
        }

    path = os.path.join(out_dir, 'adaptive_vol_index.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    generate_yaml()
