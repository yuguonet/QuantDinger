# QuantDinger 自动策略优化器

## 文件结构

```
app/optimizer/
├── __init__.py       # 模块入口
├── param_space.py    # 7 种策略模板 + 参数空间定义
├── optimizer.py      # 优化引擎（随机搜索 + Optuna 可选）
├── walk_forward.py   # Walk-Forward 验证（防过拟合）
├── runner.py         # 主入口脚本
└── mock_data.py      # 本地模拟数据（无需外部 API）
```

## 放置位置

解压到 `backend_api_python/app/optimizer/` 目录即可。

## 使用方法

### 1. 真实数据回测（需要能访问交易所 API）

```bash
cd backend_api_python

# 单个模板
python -m app.optimizer.runner \
    --template ma_crossover \
    --symbol "Crypto:BTC/USDT" \
    --timeframe 4H \
    --start 2025-01-01 \
    --end 2025-12-31 \
    --trials 100 \
    --score composite \
    --validate

# 所有模板对比
python -m app.optimizer.runner \
    --all \
    --symbol "Crypto:BTC/USDT" \
    --timeframe 4H \
    --start 2025-01-01 \
    --end 2025-12-31 \
    --trials 50
```

### 2. 本地 Mock 测试（无需网络）

```python
from datetime import datetime
from app.optimizer.mock_data import generate_mock_klines, patch_backtest_with_mock
from app.optimizer.runner import BacktestObjective
from app.optimizer.optimizer import StrategyOptimizer

# 生成模拟数据（1 年 4H K 线）
mock_data = generate_mock_klines(
    start_date=datetime(2024, 1, 1),
    end_date=datetime(2025, 1, 1),
    timeframe="4H",
    initial_price=60000,
    volatility=0.02,
    trend=0.0001,
)
patch_backtest_with_mock(mock_data)

# 运行优化
objective = BacktestObjective(
    template_key="ma_crossover",
    symbol="BTC/USDT", market="Crypto",
    timeframe="4H",
    start_date=datetime(2024, 1, 1),
    end_date=datetime(2025, 1, 1),
)
optimizer = StrategyOptimizer(
    template_key="ma_crossover",
    objective_fn=objective,
    n_trials=50,
    score_fn="composite",
)
best = optimizer.run()
```

## 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--template` | - | 策略模板名（ma_crossover / rsi_oversold / bollinger_breakout / macd_crossover / supertrend / kdj_crossover / dual_rsi） |
| `--all` | - | 运行所有模板并对比 |
| `--symbol` | Crypto:BTC/USDT | 交易标的 |
| `--timeframe` | 4H | K 线周期 |
| `--start` | 2025-01-01 | 回测起始日期 |
| `--end` | 2025-12-31 | 回测结束日期 |
| `--trials` | 100 | 搜索次数 |
| `--score` | composite | 评分函数（sharpe / return_dd_ratio / composite） |
| `--no-validate` | false | 跳过 Walk-Forward 验证 |
| `--output` | optimizer_output | 结果输出目录 |

## 评分函数

- **sharpe**: 夏普比率（收益/波动）
- **return_dd_ratio**: 收益/最大回撤比
- **composite**: 综合评分 = sharpe×0.4 + winRate×0.2 + profitFactor×0.2 - maxDD×0.2

## 输出结果

运行后在 `optimizer_output/` 目录生成 JSON 文件，包含：
- 最优参数配置
- 回测指标（Sharpe / 胜率 / 最大回撤 / 盈亏比）
- Top 10 参数组合
- Walk-Forward 验证结论

## 依赖

项目已有依赖，无需额外安装：
- pandas, numpy（数据处理）
- optuna（可选，自动检测）

## 下一步

Phase 2: 接入 LLM 做策略发现（自动推荐指标组合）
Phase 3: LLM 生成 ScriptStrategy 升级版
