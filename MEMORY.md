# MEMORY.md - 长期记忆

## 用户信息
- 系统：Windows + PowerShell
- CPU：40 核
- 量化交易目标：A 股小资金快速复利
- 项目：QuantDinger（GitHub: yuguonet/QuantDinger）
- 项目路径：`D:\QuantDinger\`

## QuantDinger 项目关键信息
- 下载地址：`https://ghfast.top/` 镜像加速
- 代码量：~24,500 行 Python
- 数据库：PostgreSQL（后端 `backend_api_python`）
- 模板分类：
  - `original`（6个）：rsi_oversold, bollinger_breakout, macd_crossover, supertrend, kdj_crossover, dual_rsi
  - `ashare`（10个）：atr_breakout, volume_price_div, dual_ma_volume, macd_kdj_resonance 等
  - `llm`（5个）：基于数据洞察生成的策略
  - `mine`（5个）：vol_price_resonance, trend_pullback_buy, limit_up_next_day, low_vol_reversal, dragon_pullback
- 模板代码路径：
  - `build_strategy` → IndicatorStrategy 直接代码（param_space.py 的原始模板）
  - `build_config` → JSON config → StrategyCompiler（mine/ashare/llm 模板）
- 评分函数 `composite`：收益 30% + Calmar 20% + 盈亏比 15% + 胜率 10% + Sharpe 15% - 回撤 25% - 交易频率惩罚

## 策略优化经验
- 日线可行（300750.SZ + macd_crossover Sharpe 2.47）
- 15m 全部负收益（A 股 T+1 + 噪音）
- 小资金选标的比选策略更重要（波动大的创业板/科创板优先）
- scoring.py 惩罚：totalTrades < 3 → -10.0，avgProfit < 2% → -5.0
- ashare 模板（build_config 路径）参数太保守，大部分零交易或 1-3 笔
- WF 验证对低频策略不友好（每 fold 测试期太短）
- 评分用 sharpe 比 composite 更适合低频策略（不惩罚交易频率）

## 当前进度（2026-05-20 21:11）
- 第四轮完成：原始模板 + 100 只股票 + 日线 2023-2026 + 300 trials + sharpe 评分
- **重大突破：300054.SZ + kdj_crossover，WF 测试 2.24（第一个 WF > 0 的组合）**
- Sharpe 2.97, 胜率 84.6%, 回撤 -3.8%, 收益 25.7%, 13 笔交易
- kdj_crossover 是最有效的模板
- 下一步：详细 WF 验证 + 扩大股票池扫描
