# QuantDinger 自动策略优化器

## 权限边界

- 只允许调用 `optimizer/` 以外的内容，不允许修改 `optimizer/` 文件夹以外的文件
- 可以在 `optimizer/` 下进行任意改动
- 存储文件必须放在 `optimizer/` 目录内，如需修改外部文件，必须先同意
- 每次创建或修改文件后将文件按目录打包成.tar.gz格式文件,删除的文件则告知
# 工作环境
- windows10 + vscode + powershell + 40cpu + RAM:64G

## 文件结构

```
optimizer/
├── __init__.py                  # 模块入口
├── param_space.py               # 7 种原始策略模板 + 参数空间定义
├── strategy_templates_ashare.py # A 股扩展模板（10 个）
├── strategy_templates_llm.py    # LLM 生成模板（6 个，含 limitup_continuation）
├── strategy_compiler.py         # 策略配置 → 可执行代码编译器
├── strategy_optimizer.py        # 优化引擎（随机搜索 + Optuna）
├── walk_forward.py              # Walk-Forward 验证（防过拟合）
├── ashare_adapter.py            # A 股规则适配（T+1、涨跌停、佣金）
├── llm_strategy_generator.py    # LLM 策略发现（Phase 2）
├── phase2_strategy_discovery.py # 数据驱动的 Phase 2 策略发现脚本
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
- **llm** (6 个)：LLM 生成的模板，基于 Phase 1 数据洞察（含 limitup_continuation 涨停追涨策略）

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

### Phase 1.5：涨停追涨策略 🔄 进行中

**目标**：设计并验证短线涨停追涨策略（大涨后吃延续溢价）

**设计思路**：不预测涨停，涨停/大涨是已发生事件，信号确定性高。关键在于过滤掉烂板（无量大涨）和超买追高。

#### 模板演进

| 版本 | 模板名 | 信号逻辑 | 结果 | 问题 |
|:--:|:--|:--|:--:|:--|
| v1 | limitup_catcher | RSI上穿 + 放量 + 布林收缩 + 均线 | ❌ 0%胜率 | 信号太泛，预测涨停命中率极低 |
| v2 | limitup_continuation | 涨停(>=9.5%) + 放量 + RSI<75 | ⚠️ 41/1300触发 | 固定阈值导致大部分股票永远不触发 |
| v3 | limitup_continuation | 滚动百分位排名(top 5%) + 放量 + RSI | 🔄 待验证 | 自适应阈值，每只股票都有机会触发 |

#### v2 全量回测结果（1300 只 × 1 模板 × 100 trials）

- **触发率**：41/1300（3.2%），96.7% 股票无信号
- **有交易的 41 只策略**：平均得分 1.59，39% 正收益
- **Top 策略**：
  - 688498.SH：200.2% 收益，Sharpe 1.20，胜率 60%
  - 300580.SZ：62.4% 收益，Sharpe 0.72，胜率 80%
  - 300475.SZ：35.0% 收益，胜率 100%
- **关键发现**：Top 策略集中在创业板/科创板（20% 涨跌幅），大涨日更频繁

#### v3 改动

- **核心变化**：用滚动百分位排名替代固定涨幅阈值
  - 旧：`今日涨幅 >= 9.5%`（固定，大部分股票触发不了）
  - 新：`今日涨幅在近60天中排名前5%`（自适应每只股票波动）
- **新增指标**：`limitup_detect`（滚动百分位排名检测）
- **修改文件**：`strategy_templates_llm.py`、`strategy_compiler.py`

#### v3 全量回测结果（1260 只 × 1 模板 × 100 trials，2023-05-01 ~ 2026-03-31）

| 指标 | 数值 |
|---|---|
| 有交易信号 | 729/1260（57.9%）✅ 相比 v2 的 3.2% 大幅提升 |
| 正得分率 | 694/729（95.2%）|
| 平均得分 | 1.55 |
| 平均 Sharpe | -0.26 |
| 平均胜率 | 32.8% |
| 平均交易次数 | 5.9 |

**Top 5 策略**：

| 股票 | 得分 | Sharpe | 收益 | 胜率 |
|:--|:--:|:--:|:--:|:--:|
| 603583.SH | 4.77 | 0.67 | +62.2% | 100% |
| 603119.SH | 4.60 | 1.30 | +154.4% | 80% |
| 000833.SZ | 4.47 | 0.66 | +66.6% | 60% |
| 301155.SZ | 4.44 | 0.47 | +23.6% | 100% |
| 688331.SH | 4.38 | 0.69 | +55.2% | 80% |

#### v3 Walk-Forward 验证结果

| 指标 | 数值 |
|---|---|
| WF 已测试 | 16 只（1260 只中） |
| WF 通过（score>0）| **0** |
| WF 失败（score≤0）| **16（100%）** |
| WF 得分范围 | -6.94 ~ -8.47 |
| 回测得分 vs WF 相关系数 | 0.175（不相关） |

**结论**：❌ **limitup_continuation 在日线上不具备泛化能力**，WF 全部失败，严重过拟合。自适应阈值解决了触发率问题，但信号本身没有持续预测力。

#### 命令

```powershell
# v3 全量回测
python -m optimizer.runner -t limitup_continuation -m CNStock --all-local -tf 1D `
  --start 2023-05-01 --end 2026-03-31 --trials 100 --score composite -j 35

# v3 Walk-Forward 验证（已执行，结果见上）
```

---

### Phase 1.6：龙回头策略 🆕

**目标**：大涨后回调缩量买入，吃第二波拉升

**设计思路**：
- 大涨/涨停后有辨识度（主力资金介入），回调是获利盘消化
- 缩量回调 = 卖压衰竭（非主力出货），二次启动概率高
- 比追涨策略风险更低（回调后买入），收益潜力大（吃第二波）

**策略逻辑**：
```
买入条件（全部满足）:
  1. 近N天内有过大涨（单日涨幅 >= M%）
  2. 从近期高点回调 X%~Y%（回调到位但趋势未破）
  3. 回调时缩量（成交量 < 均量的 Z 倍 = 卖压衰竭）
  4. 可选: RSI > 25（避免超卖接飞刀）
  5. 可选: 收盘 > EMA(N)（中期趋势保护）

卖出条件:
  - Signal Exit: 买入条件不再满足时卖出
  - Stop Loss: 固定止损兜底
```

**与 limitup_continuation 的区别**：
| | limitup_continuation | dragon_pullback |
|---|---|---|
| 买入时机 | 涨停当天 | 涨停后回调 |
| 风险 | 高（追涨） | **低（低吸）** |
| 持仓 | 1-3 天 | 3-10 天 |
| 核心逻辑 | 吃延续溢价 | 吃第二波拉升 |

**新增指标**：
- `recent_surge`: 近N天最大单日涨幅是否超阈值（替代 `limitup_detect`，解决信号不重叠问题）
- `dragon_pullback`: 从近期高点的回撤幅度（回撤区间可配置）

**使用命令**：
```powershell
# 单股票测试
python -m optimizer.runner -t dragon_pullback -m CNStock -s "000001.SZ" -tf 1D `
  --start 2023-05-01 --end 2026-03-31 --trials 100

# 全量回测
python -m optimizer.runner -t dragon_pullback -m CNStock --all-local -tf 1D `
  --start 2023-05-01 --end 2026-03-31 --trials 100 --score composite -j 35
```

**状态**：🔄 待回测验证

---

### Phase 1.7：尾盘抢筹隔夜溢价策略 🆕

**目标**：设计并验证"尾盘买入 → 次日开盘卖出"的隔夜溢价策略，追求高胜率和稳定溢价

**设计思路**：
- 不追涨停，不抄底，而是捕捉"尾盘有资金抢筹"的确定性信号
- 收盘在当日K线高位 = 主力尾盘拉升/吸筹 → 次日高开概率大
- 放量确认 = 非散户行为，有真实资金介入
- 涨停封板 = 极端强势信号，继续持有吃连板溢价

**策略逻辑**：
```
买入条件（全部满足）:
  1. close_position > 0.7（收盘在当日K线上 70% 以上位置）
  2. volume_ratio > 1.2（成交量 > 20日均量的 1.2 倍）
  3. RSI < 75（未超买）
  4. 可选: 收盘价 > EMA(N)（趋势向上）

卖出逻辑（T+1 隔夜模式）:
  - 次日开盘: 如果开盘价 >= 前收 × 1.095（接近涨停）→ 涨停封板，继续持有
  - 次日开盘: 否则 → 开盘价卖出（吃隔夜溢价）
  - 涨停持有期间: 每天检查涨停板是否打开，打开则次日开盘卖出
  - 追踪止损: 涨停持有期间，从最高点回撤 N% 则卖出
  - 固定止损: 任何持仓期间，亏损达 N% 则卖出
```

**与 limitup_continuation 的区别**：
| | limitup_continuation | close_strength_overnight |
|---|---|---|
| 买入时机 | 今天涨停/大涨后 | 今天尾盘收盘在高位 |
| 卖出时机 | 追踪止损 | 次日开盘（除非涨停封板）|
| 持仓周期 | 1-3 天 | 1-2 天 |
| 核心信号 | 涨幅排名 | 收盘价位置 |
| 目标 | 吃延续溢价 | 吃隔夜溢价 |

**使用命令**：
```powershell
# 单股票回测
python -m optimizer.runner -t close_strength_overnight -m CNStock -s "000001.SZ" -tf 1D `
  --start 2023-05-01 --end 2026-03-31 --trials 100 --score composite

# 全量回测
python -m optimizer.runner -t close_strength_overnight -m CNStock --all-local -tf 1D `
  --start 2023-05-01 --end 2026-03-31 --trials 100 --score composite -j 35
```

**状态**：🔄 待回测验证

---

### Phase 2：LLM 策略发现 🔄 进行中

**目标**：用 LLM 分析 Phase 1 数据模式，自动生成新的策略模板

**前置条件**：Phase 1 基本完成 ✅，Phase 1.5 结论明确 ✅

**已完成**：
- [x] `phase2_strategy_discovery.py` — 数据驱动的策略发现脚本
- [x] 5 个数据驱动的 LLM prompt 模板（基于 Phase 1 已验证有效的指标组合）
- [x] 复用 `backend_api_python` 的 LLMService（无需额外配置 API Key）
- [x] LLM 生成策略代码 → `strategies_generated.py`（5 个模板）
- [ ] 全量回测 + Walk-Forward 验证

**生成的 5 个模板**：

| 模板 key | 名称 | 状态 |
|---|---|---|
| rsi_vwap_volume | RSI VWAP Volume 共振 | 🔄 全量回测中 |
| adaptive_volatility | 自适应波动率 | 🔧 已修复 bug，待重跑 |
| ema_rsi_volume | EMA RSI Volume | 🔧 已修复 bug，待重跑 |
| kdj | 均线 KDJ 动量 | ⏳ 待回测 |
| bollinger_macd_volume | 布林 MACD Volume | ⏳ 待回测 |

#### 小范围测试结果（3 只股票 × 50 trials）

| 股票 | 最佳模板 | Sharpe | 胜率 | 最大回撤 | WF 得分 |
|---|---|---|---|---|---|
| 300674.SZ | rsi_vwap_volume | 1.13 | 71.4% | -11.7% | -10.0 ❌ |
| 301215.SZ | rsi_vwap_volume | 0.61 | 60.0% | -15.2% | -10.0 ❌ |

**小结**：回测 Sharpe 尚可，但 WF 全挂（-10.0），严重过拟合。需全量回测验证。

#### 全量回测结果

##### adaptive_volatility v1（2026-05-01）

| 指标 | 数值 |
|---|---|
| 股票数 | 1266 |
| **有交易信号** | **0（0%）** |
| 正得分 | 0 |
| 平均得分 | -10.0（WF 默认失败分） |

**结果：全军覆没，无任何交易信号。**

**根因分析**：入场条件逻辑矛盾 —— `price_below_lower`（布林下轨 = SMA20 - 2σ）AND `price_above`（EMA20 ≈ SMA20），两个条件在数学上不可能同时满足。

**修复**（`strategies_generated.py`）：
- 去掉矛盾的 EMA 条件
- 改为均值回归逻辑：RSI 超卖 + 布林下轨 + 放量确认
- 修复后入场：`RSI < threshold AND close < BB_lower AND volume > vol_ratio × MA`
- 同步修复 `ema_rsi_volume`：EMA 条件重复 + 缺少 volume 条件

**待重跑**：
```powershell
python -m optimizer.runner -t adaptive_volatility -m CNStock --all-local -tf 1D `
  --start 2023-05-01 --end 2026-03-31 --trials 100 --score composite -j 35
```

_其余模板全量回测结果待补充（1260 只 × 5 模板 × 50 trials，2023-05-01 ~ 2026-03-31）_

**流程**：
```
Phase 1 结果 → 模式提取 → 数据驱动的 LLM Prompt
    ↓
5 个策略发现方向：
  1. indicator_combo_innovation  — RSI+VWAP+Volume 三重共振
  2. adaptive_volatility         — ATR/布林宽度自适应参数
  3. volume_price_trend          — 量价背离+趋势确认
  4. ma_kdj_momentum             — 均线支撑+KDJ金叉
  5. bollinger_macd_volume       — 布林带+MACD+成交量三重过滤
    ↓
LLM 生成策略代码 → 验证 → 编译 → 回测 → WF 筛选
    ↓
保留好策略，迭代循环
```

**关键设计**：
- 不再用预定义 prompt（旧版 `ASHARE_STRATEGY_PROMPTS`），改为从 Phase 1 数据中提取有效模式驱动 prompt 生成
- 已验证有效的指标组合：RSI、VWAP、Volume MA、Bollinger、EMA
- 引入新指标：OBV、ADX、CCI、MFI
- 目标：正得分率 > 90%，平均 Sharpe > 0.5，WF 通过率 > 50%

**使用方法**：
```powershell
# 全流程
python -m optimizer.phase2_strategy_discovery run --all-local -m CNStock -tf 1D `
  --start 2023-05-01 --end 2026-03-31 --trials 50 --score composite -j 35

# 小范围测试
python -m optimizer.phase2_strategy_discovery run -m CNStock `
  -s "301215.SZ,688686.SH,300674.SZ" -tf 1D --trials 50

# 只看 prompt
python -m optimizer.phase2_strategy_discovery prompts
```

---

### Phase 3：LLM 生成 ScriptStrategy ⏳ 待 Phase 2 数据积累

**目标**：用 Phase 1/2 的回测数据作为 LLM 上下文，生成高质量的 ScriptStrategy

**产出**：高质量的 ScriptStrategy（非模板化，完全由 LLM 生成代码）

---

## 更新日志

### 2026-05-01 19:23 — 新增龙回头策略 + close_position 框架扩展

#### 新增模板

| 模板 | 策略 | 状态 |
|---|---|---|
| `dragon_pullback` | 龙回头（大涨后回调缩量买入） | 🔄 待回测 |
| `close_strength_overnight` | 尾盘抢筹隔夜溢价 | 🔄 待回测 |

#### 框架改动（strategy_compiler.py）

| 改动 | 说明 |
|---|---|
| 新增 `recent_surge` 指标 | 近N天最大单日涨幅是否超阈值，替代 `limitup_detect` 用于"近期大涨"检测 |
| 新增 `dragon_pullback` 指标 | 从近N日高点的回撤幅度，支持配置回撤区间 |
| 新增 `close_position` 指标 | 收盘价在当日K线中的相对位置（0=最低, 1=最高） |
| 新增 `volume_ratio_below` 操作符 | 量比低于阈值（缩量确认），与 `volume_ratio_above` 对称 |
| 新增 `next_bar_open_exit` 退出模式 | 隔夜策略专用：次日开盘价卖出，支持涨停封板检测和持有逻辑 |

#### 模板改动（strategy_templates_llm.py）

- `_build_dragon_pullback_config`: 龙回头配置构建
  - 入场: `recent_surge` + `dragon_pullback` + `volume_ratio_below`
  - 可选: RSI 下限过滤 + EMA 趋势保护
- `_build_close_strength_overnight_config`: 尾盘抢筹配置构建
  - 入场: `close_position` + `volume` + `rsi` + `ma`
  - 退出: `exit_mode: "next_bar_open_exit"` 自定义 core loop
- 模板编号: 6=dragon_pullback, 7=close_strength_overnight, 8=limitup_continuation

#### 设计决策记录

1. **close_position 与次日收益无相关性** — 统计验证（r=0.003），单独使用无预测力，仅作为过滤器配合其他信号
2. **limitup_detect 不适合龙回头** — 只检测当天涨幅排名，回调日永远不触发；改用 `recent_surge`（检测近期是否大涨）
3. **龙回头核心假设** — 大涨后缩量回调 = 卖压衰竭，二次启动概率高（行为金融学支撑，待真实数据验证）
4. **next_bar_open_exit 模式** — 现有 core loop 无法区分"开盘卖"和"收盘卖"，新增自定义 core loop 支持 T+1 开盘价卖出 + 涨停封板持有

#### 文件清单

```
修改: optimizer/strategy_templates_llm.py  (+2 个模板函数 + 模板注册)
修改: optimizer/strategy_compiler.py       (+3 个指标 + 1 个操作符 + 1 个退出模式)
修改: optimizer/README.md                  (+龙回头策略文档 + 更新日志)
新增: optimizer_changes.tar.gz             (打包文件)
```

---

*最后更新：2026-05-01 19:23*