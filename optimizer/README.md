# QuantDinger 自动策略优化器

## 权限边界

- 只允许调用 `optimizer/` 以外的内容，不允许修改 `optimizer/` 文件夹以外的文件
- 可以在 `optimizer/` 下进行任意改动
- 存储文件必须放在 `optimizer/` 目录内，如需修改外部文件，必须先同意

# 工作环境
- windows10 + vscode + powershell + 40cpu + RAM:64G

## 文件结构

```
optimizer/
├── __init__.py                  # 模块入口
├── param_space.py               # 7 种原始策略模板 + 参数空间定义
├── strategy_templates_ashare.py # A 股扩展模板（10 个）
├── strategy_templates_llm.py    # LLM 生成模板（5 个）
├── strategy_compiler.py         # 策略配置 → 可执行代码编译器
├── strategy_optimizer.py        # 优化引擎（随机搜索 + Optuna）
├── walk_forward.py              # Walk-Forward 验证（防过拟合）
├── ashare_adapter.py            # A 股规则适配（T+1、涨跌停、佣金）
├── llm_strategy_generator.py    # LLM 策略发现（Phase 2）
├── runner.py                    # 主入口脚本
├── mock_data.py                 # 本地模拟数据
├── analyze_results.py           # 结果分析工具
├── param_space.py               # 原始 7 模板参数空间
├── data_warehouse/              # 本地数据仓库
│   ├── storage.py               # 存储读写
│   ├── downloader.py            # 数据下载器
│   └── factory2.py              # 数据源工厂
└── README.md                    # 本文件
```

## 使用方法

### 查看可用模板

```bash
python -m optimizer.runner --list
```

模板集合：
- **original** (7 个)：ma_crossover, rsi_oversold, bollinger_breakout, macd_crossover, supertrend, kdj_crossover, dual_rsi
- **ashare** (10 个)：A 股专用模板
- **llm** (5 个)：LLM 生成的模板，基于 Phase 1 数据洞察

### 单模板回测

```bash
python -m optimizer.runner -t triple_rsi_momentum -m CNStock -s "000001.SZ" -tf 1D \
  --start 2024-01-01 --end 2025-12-31 --trials 100
```

### 全量回测（多股票 × 多模板）

```bash
python -m optimizer.runner --all -m CNStock --all-local -tf 1D \
  --start 2024-01-01 --end 2025-12-31 --trials 100 --score composite -j 35
```

### 关键参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-t, --template` | - | 策略模板名 |
| `--all` | - | 运行所有模板 |
| `--set` | all | 模板集合: all/original/ashare/llm |
| `-m, --market` | CNStock | 市场类型 |
| `-s, --symbol` | - | 交易标的，逗号分隔 |
| `--all-local` | - | 自动扫描本地仓库全部股票 |
| `--random-sample N` | - | 随机抽取 N 只股票 |
| `-tf, --timeframe` | 1D | K 线周期 |
| `--start` | 2024-01-01 | 回测起始日期 |
| `--end` | 2025-12-31 | 回测结束日期 |
| `-n, --trials` | 100 | 搜索次数 |
| `--score` | composite | 评分函数: sharpe/return_dd_ratio/composite |
| `--no-validate` | false | 跳过 Walk-Forward 验证 |
| `-j, --jobs` | 1 | 并行进程数 |

## 评分函数

- **sharpe**: 夏普比率（收益/波动）
- **return_dd_ratio**: 收益/最大回撤比
- **composite**: 综合评分 = sharpe×0.4 + winRate×0.2 + profitFactor×0.2 - maxDD×0.2

## 输出结构

```
optimizer_output/
  CNStock/
    daily/
      000001.SZ_triple_rsi_momentum.json
  _summary.json   ← 全量汇总（含每个模板的最优参数 + Walk-Forward 验证）
```

---

## 工作进度

### Phase 1：纯 IndicatorStrategy + Optuna ✅ 基本完成

**目标**：跑通整个循环，建立策略表现基线数据集

#### 已完成

| 步骤 | 内容 | 状态 | 产出 |
|---|---|---|---|
| Step 1 | 项目搭建、runner.py 分析 | ✅ | 理解全量代码结构 |
| Step 2 | 5 个 LLM 模板 × 200 只中证1000成分股全量回测 | ✅ | `_summary.json` (1000 组回测结果) |
| Step 3 | 模板表现分析 + 问题诊断 | ✅ | `phase1_patterns.json` |
| Step 3.1 | macd_vol_divergence 修复 | ✅ | `histogram_negative` → `diff_lt_dea`，RSI 默认开启 |
| Step 3.2 | triple_rsi_momentum Walk-Forward 验证 | 🔄 进行中 | 等待 overfitting_ratio + consistency |

#### Phase 1 全量回测结果（200 只 × 5 模板 × 50 trials）

| 排名 | 模板 | 平均Sharpe | 平均胜率 | 平均回撤 | 正得分率 | 平均得分 |
|:--:|:--|:--:|:--:|:--:|:--:|:--:|
| 1 | triple_rsi_momentum | 0.630 | 57.6% | -22.4% | 100% | 3.421 |
| 2 | vwap_bollinger_squeeze | 1.101 | 51.5% | -14.0% | 100% | 3.334 |
| 3 | rsi_volume_divergence | 0.719 | 45.0% | -23.8% | 99% | 2.874 |
| 4 | vwap_volume_confirm | 0.838 | 52.2% | -13.0% | 91.5% | 2.197 |
| 5 | macd_vol_divergence ❌ | -0.309 | 29.7% | -15.5% | 32.5% | -5.912 |

#### 关键发现

- **184/200 只股票**在 4+ 模板上正得分，策略模式具有普适性
- **Top 万能股票**：301215.SZ、688686.SH、300674.SZ（5/5 模板全胜）
- 前 4 模板高度相关（91-100%），macd_vol_divergence 仅 32% 相关（修复后可提供差异化信号）
- **macd_vol_divergence 失败根因**：`histogram_negative`（零轴穿越）在日线上极罕见，叠加 `bullish_divergence` 双重稀有条件 → 已修复

#### 待完成

- [ ] triple_rsi_momentum Walk-Forward 验证结果确认（overfitting_ratio < 0.5, consistency > 0.6）
- [ ] vwap_bollinger_squeeze Walk-Forward 验证
- [ ] rsi_volume_divergence Walk-Forward 验证
- [ ] 修复后的 macd_vol_divergence 重跑验证

---

### Phase 2：LLM 策略发现 ⏳ 待启动

**目标**：用 LLM 分析 Phase 1 数据模式，自动生成新的策略模板

**前置条件**：Phase 1 Walk-Forward 验证通过

**流程**：
```
Phase 1 结果矩阵 (phase1_patterns.json)
    ↓  数据模式提炼（哪些指标组合在哪些股票上有效）
    ↓  LLM 分析模式（prompt + 数据摘要）
    ↓  生成新策略模板（扩充 strategy_templates_llm.py）
    ↓  编译 → 回测 → 筛选 → 参数优化
    ↓  保留好策略，迭代循环
```

**产出**：更丰富的策略候选，超越人工定义的参数空间

---

### Phase 3：LLM 生成 ScriptStrategy ⏳ 待 Phase 2 数据积累

**目标**：用 Phase 1/2 的回测数据作为 LLM 上下文，生成高质量的 ScriptStrategy

**产出**：高质量的 ScriptStrategy（非模板化，完全由 LLM 生成代码）


### 特定策略
完全可以，这是一个很好的思路。有两种实现方式：


方式 1：筛选特定股票 + 特定时间段回测

在现有框架上，只需要：


1.筛选目标股票 — 找出在某时间段内涨停过的股票
2.限定回测窗口 — 用 --start--end 只跑那个时间段
3.定制评分函数 — 给涨停捕获更高权重

powershell
powershell
# 比如只跑 2024年9月（A股大反弹期间）
python -m optimizer.runner -t vwap_rsi_confirm -m CNStock -s "000001.SZ,000002.SZ" -tf 1D --start 2024-09-01 --end 2024-09-30 --trials 200

方式 2：写一个涨停板专用策略模板

更直接的做法——专门设计一个抓涨停的模板。我来分析一下涨停板的特征：## 回答


可以，而且现有框架已经支持。 有两种玩法：


玩法 1：时间段限定训练

直接用 --start--end 跑特定行情区间：


powershell
powershell
# 2024年9月大反弹期间训练
python -m optimizer.runner -t vwap_rsi_confirm -m CNStock --all-local -tf 1D --start 2024-09-01 --end 2024-09-30 --trials 200 -j 35

# 2025年1月震荡期训练
python -m optimizer.runner -t vwap_rsi_confirm -m CNStock --all-local -tf 1D --start 2025-01-01 --end 2025-01-31 --trials 200 -j 35

然后对不同时间段的最优参数做集合分析——哪些参数在多个行情周期都有效，哪些只在特定行情有效。


玩法 2：涨停板专用策略模板

从数据看，收益 >100% 的 792 组回测有明确规律：


低频高弹性：平均 6.4 笔交易，单笔收益极高
模板偏好：rsi_volume_divergence(240) 和 macd_vol_divergence(195) 最多，因为它们捕捉反转
科创板/创业板占比高（688xxx、300xxx），因为 20% 涨跌幅

要不要我基于这些数据设计一个涨停板专用模板？思路是：


1.筛选有过涨停历史的股票作为训练集
2.用涨停日作为信号触发点训练参数
3.评分函数改为以捕获大涨为核心（而非夏普比率）

你想做哪个？