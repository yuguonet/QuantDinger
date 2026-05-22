# MEMORY.md - 长期记忆

## 用户
- 做 A 股量化交易，目标小资金快速复利
- 本地环境：Windows + 40核，D:\QuantDinger\
- 有 2 年 15 分钟数据库
- 对 A 股板块轮动有经验认知
- 倾向于先打磨一个策略，再推广到其他

## QuantDinger 项目
- GitHub: https://github.com/yuguonet/QuantDinger
- 后端 Python (Flask)，前端 Vue，PostgreSQL 数据库
- optimizer/ 目录是策略优化器，sector_aggregator.py 做板块聚合回测
- runner.py 串联所有模块，支持 IndicatorStrategy 和旧版 JSON config

## 核心方法论

### 三层关系
1. **个股策略信号** — 被验证的对象，产生候选交易
2. **行业/概念** — 横向扩展样本量。同概念 20 只股票跑同一策略 → 5笔×20只=100笔
3. **大盘** — 去噪工具，判断个股信号是真强还是搭大盘便车

### 出场机制设计原则（重要！）
- 出场条件必须独立于入场条件，不能用入场条件取反！
- 出场比入场宽松：入场要求多条件AND，出场只需趋势明显破坏
- 让利润奔跑：追踪止损保护浮盈，不要一有回调就跑
- 好的出场 > 好的入场（出场决定盈亏比）
- 出场条件用 OR：多个出场理由独立触发

### Sharpe 为负的根因（2026-05-21 发现）
1. **做空污染**：A 股模板缺 tradeDirection="long"，回测偷偷做空 → 已修复
2. **出场逻辑错误**：8/9 策略用入场条件取反做出场 → 小赚大亏 → 部分修复

## 2026-05-22 连板策略研究（完整）

### 新增文件
- `optimizer/strategy_dragon_filter.py` — 连板猎手 v2 双分支独立策略
- `optimizer/validate_full_market.py` — 全市场假阳性验证
- `optimizer/optimize_filter.py` — 过滤规则优化搜索
- `optimizer/strategy_templates_ashare.py` — dragon_filter IS模板（双分支）
- `optimizer/test_dragon_filter.py` — 测试框架（随机数据+逐日推进）
- `optimizer/optimize_mainboard.py` — 主板参数优化脚本

### 双分支架构（09:26确认）
- 旧策略全部信号来自创业板/科创板，主板0信号（min_return>=20%过滤掉主板）
- 改为双分支：`BOARD_PARAMS = {"10pct": {...}, "20pct": {...}}`
- 自动检测板块，各自最优参数

### 主板参数优化
- **量比是核心筛选维度**：量比<1（缩量涨停）胜率79.5%，量比>3胜率<56%
- 最优：`seal≤8% + 波动≤3% + 量比≤1` → 174笔 78.2%胜率 +5.10%均值
- 旧参数 `seal≤5.5% + vol≤8%` → 322笔 59.6%胜率

### 横向分析发现（dragon_pattern_features.csv）
- 主板：开板日跌>5% → 胜率仅26%（可做出场条件）
- 创/科：连板期振幅小(<3.4%) → 均值+27.7%；实体比低(<0.35) → 胜率86%
- 创/科上影线反直觉：高上影(>31%)均值+30.5%
- 注意：振幅/实体比是连板窗口均值，无法在首板日单独计算

### 最终回测结果（CSV验证，2026-01~2026-05）
- 主板：174笔 78.2%胜率 +5.10%均值 盈亏比1.70
- 创/科：34笔 73.5%胜率 +13.07%均值 盈亏比9.37（需runner确认81%）
- 合计：208笔 77.4%胜率 +6.41%均值 盈亏比2.36

### 策略框架（双分支）
```
买: 第一板涨停（非一字板）
  主板(10%): 涨幅≥9.8% + 封板≤8% + 波动≤3% + 量比≤1
  创/科(20%): 涨幅≥19.8% + 封板≤2.8% + 上影2~8% + 波动≤10%
持: 涨停就拿着
卖: 开板 / 止损10% / 追踪止损 / 止盈15%
```

## 技术记录
- 新浪 HTTP API 零依赖可拉 5 大指数日线（sh000001/sz399001/sz399006/sh000688/bj899050）
- sector_aggregator.py 有 monkey-patch _direct_fetch，必须调用 df.set_index("time")
- runner.py L572: `_trade_dir = _tmpl_defaults.get('tradeDirection', 'both')` — 没有 strategy_defaults 就默认 both
- backtest.py 信号归一化：buy/sell 在 trade_direction='long' 时映射为 open_long/close_long
- strategy_compiler.py 出场条件用 OR 连接（多个出场理由独立触发）
- get_regime() 阈值：20日累计 < -3% 才算 down，可能太严
- IndicatorStrategy 模板用 `render_indicator_strategy` 生成代码，注册到 ASHARE_STRATEGY_TEMPLATES
- runner 的 ALL_TEMPLATES = STRATEGY_TEMPLATES + ASHARE_STRATEGY_TEMPLATES + LLM + MY + GENERATED
