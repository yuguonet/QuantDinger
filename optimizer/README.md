# QuantDinger 自动策略优化器
这个项目只允许调用optimizer/以外的内容,不允许修改optimizer/文件夹以外的文件,可以在optimizer/下进行任意改动.
也不允许将存储文件放到这个目录以外,如果必须要修改或创建,建议将需要的文件复制到当前目录下再修改,
如果实在不行,在修改或创建前必须同意才行

## 文件结构
```
optimizer/
├── __init__.py       # 模块入口
├── param_space.py    # 7 种策略模板 + 参数空间定义
├── optimizer.py      # 优化引擎（随机搜索 + Optuna 可选）
├── walk_forward.py   # Walk-Forward 验证（防过拟合）
├── runner.py         # 主入口脚本
└── mock_data.py      # 本地模拟数据（无需外部 API）
```

## 放置位置

解压到 `optimizer/` 目录即可。

## 使用方法

### 1. 真实数据回测（需要能访问交易所 API）

```bash
cd backend_api_python

# 单个模板
python -m optimizer.runner \
    --template ma_crossover \
    --symbol "Crypto:BTC/USDT" \
    --timeframe 4H \
    --start 2025-01-01 \
    --end 2025-12-31 \
    --trials 100 \
    --score composite \
    --validate

# 所有模板对比
python -m optimizer.runner \
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
from optimizer.mock_data import generate_mock_klines, patch_backtest_with_mock
from optimizer.runner import BacktestObjective
from optimizer.optimizer import StrategyOptimizer

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

## 工作进度
先把https://github.com/yuguonet/QuantDinger项目以zip的形式趴下来,optimizer/目录下runner.py,分析已经完成,

Phase 1（现在）：纯 IndicatorStrategy + Optuna
  → 跑通整个循环，建立基线
  → 产出：批量策略 + 回测数据

Phase 2（验证可行性后）：加入 LLM 策略发现
  → 用 LLM 推荐指标组合，替代人工定义参数空间
  → 产出：更丰富的策略候选

Phase 3（数据积累后）：LLM 生成 ScriptStrategy
  → 用 Phase 1/2 的回测数据作为 LLM 的上下文
  → 产出：高质量的 ScriptStrategy
Phase 1 是基础，不管走哪条路都要先做。 要不要我先把 Phase 1 的原型写出来？

tar xzf auto-strategy-optimizer.tar.gz -C backend_api_python/
# 然后直接用
cd backend_api_python
python -m optimizer.runner --template ma_crossover --trials 100
核心流程：参数空间定义 → 随机/Optuna 搜索 → 回测评估 → Walk-Forward 验证 → 输出最优策略 JSON。7 个模板全部编译验证通过。

1.先让 LLM 批量生成 A 股适用的 IndicatorStrategy 模板（扩充 param_space.py）
2.然后跑一轮 Optuna 看哪些模板在 A 股数据上有效
3.最后把表现好的模板和 YAML 策略做融合

市场数据
    ↓  特征提取（量价、技术指标、市场状态）
    ↓  LLM 分析模式（基于 prompt + 数据摘要）
    ↓  输出结构化 DSL（不是自然语言）
    ↓  compiler 编译
    ↓  回测验证
    ↓  筛选保留好策略
    ↓  参数优化（optimizer）
    ↓  迭代循环

总结

层次	         解决什么	             用什么
策略发现	从数据中找新模式	        LLM + 数据特征
策略表达	统一格式，可编译可回测	        DSL
参数优化	在模板内找最优参数	        optuna / random search
回测验证	淘汰烂策略	                ScriptStrategy

真正的第一步：


Step 1：跑通全量模板回测，收集策略表现数据

bash
bash
cd backend_api_python

# 对一只股票跑全部 17 个模板，每个模板 100 组参数
python -m optimizer.runner \
  --all \
  --symbol "A_SHARE:000001.SZ" \
  --timeframe 1D \
  --start 2023-01-01 \
  --end 2025-12-31 \
  --trials 100 \
  --validate

这一步产出的是：每个模板在特定股票上的最优参数 + Sharpe/回撤/胜率等指标。但这是"人定模板的参数搜索"，不是从数据里发现新模式。


Step 2：扩展到多股票 × 多模板，构建训练数据集

写一个脚本批量跑：


text
text
对 N 只股票 × 17 个模板 × M 组参数 → 回测结果矩阵

这个矩阵才是"大数据"——它告诉你：

哪些股票适合哪些策略类型
哪些指标组合在哪些市场环境下表现好
参数敏感性如何

Step 3：从回测结果中提炼模式，喂给 LLM

把 Step 2 的结果做摘要（不是原始数据，是聚合后的模式），比如：

"银行股在 MA 交叉策略上 Sharpe > 1.5 的概率最高"
"布林带突破在高波动率环境下胜率提升 40%"
"MACD + KDJ 共振策略在创业板上回撤最小"

这些摘要作为 prompt 输入给 llm_strategy_generator，让它基于数据驱动的洞察生成新策略模板。


Step 4：新模板回测验证，循环迭代

LLM 生成 → 编译 → 回测 → 筛选 → 保留好策略 → 再喂数据让 LLM 改进



所以总结：第一步是跑 runner.py --all，但目的不是"提取特征码"，而是建立一个策略表现基准数据集，作为后续 LLM 策略生成的训练信号。


要我直接写 Step 2 的批量跑批脚本吗？

中证1000跑完了，那 Step 1 基线数据集已经有了。下一步你想：

修改readme的问题,将每一步骤操作指令加进去,readme中错误修改,工作进度只需要标记到哪一步就可以了