# QuantDinger Harness 改造计划

> 用 smolagents 替换 `backend_api_python/app/agent/` 核心，向 Harness 架构靠齐

## 一、项目现状

- **项目**: https://github.com/yuguonet/QuantDinger
- **目标**: A股量化交易，小资金快速复利
- **环境**: Windows + 40核，D:\QuantDinger\

### 真实架构（非脚本集合，是完整量化平台）

```
QuantDinger/
├── backend_api_python/              ← 核心后端 (Flask API + Agent)
│   └── app/
│       ├── agent/                   ← ⭐ Agent 系统 (需替换的核心)
│       │   ├── executor.py          # ReAct Agent 执行器 (867行)
│       │   ├── runner.py            # Agent Loop + 并行工具调用 (443行)
│       │   ├── factory.py           # Agent 工厂，组装 ToolRegistry (153行)
│       │   ├── session_store.py     # 会话持久化 (305行)
│       │   ├── workspace.py         # 工作空间管理 (779行)
│       │   ├── tool_context.py      # 工具上下文注入
│       │   └── tools/               ← 11类工具 (保留，不替换)
│       │       ├── data_tools.py    # 行情/历史/股票信息
│       │       ├── analysis_tools.py # MACD/RSI/BOLL/K线
│       │       ├── backtest_tools.py # 回测引擎
│       │       ├── market_tools.py  # 指数/板块
│       │       ├── indicator_tools.py # 指标信号
│       │       ├── news_search_tools.py  # 新闻/舆情
│       │       ├── screening_tools.py # 选股筛选
│       │       ├── trading_tools.py # 策略启停/交易
│       │       ├── python_exec.py   # Python沙箱
│       │       ├── code_workspace_tools.py # 脚本版本
│       │       └── registry.py      # ToolRegistry
│       │
│       ├── data_sources/            ← 多数据源适配
│       │   ├── akshare.py / cn_stock.py / us_stock.py / crypto.py
│       │   └── provider/            # 东方财富/同花顺/新浪/腾讯/通达信
│       │
│       ├── market_cn/               ← A股市场分析
│       │   └── cards/               # AI分析/龙虎榜/情绪/热点
│       │
│       ├── services/                ← 业务服务
│       │   ├── live_trading/        # 实盘 (OKX/Binance/Bitget/...)
│       │   ├── backtest.py          # 回测
│       │   ├── llm.py               # LLM 调用
│       │   └── experiment/          # A/B实验/策略进化
│       │
│       └── routes/                  ← 20+ API 路由
│
├── optimizer/                       ← 策略优化框架 (48个Python文件)
│   ├── runner.py                    # 自动策略优化 (多进程)
│   ├── strategy_templates_ashare.py # A股策略模板
│   ├── strategy_dragon_v1.py        # V1 首板→抢二板
│   ├── walk_forward.py              # Walk-Forward 验证
│   ├── llm_strategy_generator.py    # LLM 策略生成
│   └── indicator_strategy_builder.py # 指标策略构建器
│
├── scripts/                         ← 定时任务 (daily_scan / sync_*)
├── QuantDinger-Vue/                 ← Vue 前端
└── test_dragon*.py / analysis_v1_*.py ← 早期脚本 (已被 optimizer/ 替代)
```

## 二、替换目标

**只替换 Agent 核心，保留全部工具和业务逻辑。**

```
替换前:
  executor.py (ReAct Agent) + runner.py (Agent Loop) + factory.py (组装)
  → 自研实现，无子Agent，无自迭代反馈

替换后:
  smolagents CodeAgent + AgentTool 子Agent编排
  → 多Agent协作 + 自迭代反馈循环
```

### 替换范围

| 文件 | 操作 | 说明 |
|---|---|---|
| `agent/executor.py` | 🔄 替换 | 用 smolagents.CodeAgent 替代自研 ReAct |
| `agent/runner.py` | 🔄 替换 | smolagents 内置 Agent Loop |
| `agent/factory.py` | 🔄 改造 | 改为 smolagents Agent 工厂 |
| `agent/session_store.py` | ✅ 保留 | 会话持久化，smolagents 可复用 |
| `agent/workspace.py` | ✅ 保留 | 工作空间管理 |
| `agent/tool_context.py` | ✅ 保留 | 工具上下文 |
| `agent/tools/*` | ✅ 保留 | 全部11类工具不动 |
| `agent/tools/registry.py` | 🔄 适配 | 适配 smolagents Tool 接口 |

### 新增

| 文件 | 说明 |
|---|---|
| `agent/agents/factor_agent.py` | 因子分析子Agent |
| `agent/agents/entry_agent.py` | 入场时机子Agent |
| `agent/agents/risk_agent.py` | 风险评估子Agent |
| `agent/agents/backtest_agent.py` | 独立审计子Agent |
| `agent/agents/orchestrator.py` | 主编排Agent |
| `agent/constraints.py` | 约束层 |
| `agent/feedback.py` | 反馈循环 |

## 三、核心发现（来自项目）

### 策略体系（quick-resume.md）

| 策略 | 定位 | 入场时机 | 700只回测 |
|---|---|---|---|
| V1 | 首板→抢二板 | D0首板涨停，D+1开盘买 | 74笔 91.9% +7.16% |
| 龙回头 | 二波段 | 涨停后回调3-11天 | 5笔 80% +7.73% |
| 断板 | 接力板 | 连板≥2后断板 | 21笔 66.7% +5.36% |

### V1 最终回测
- 513笔 95.1%胜率 +12.28%均收益 盈亏比1.65
- 主板: 472笔 95.1% +10.91%
- 创科: 41笔 95.1% +28.06%

### 筛选逻辑
D0盘后: 第一板涨停 + 量比>2x + 上影线<0.5% + 排除一字板
D1早盘: 主板开盘<2% / 创科开盘<5% → 买入

## 四、Harness 架构设计

```
┌─────────────────────────────────────────────────────────────┐
│              smolagents 替换后的 Agent 架构                   │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Orchestrator Agent (CodeAgent)                       │  │
│  │  调度子Agent，实现自迭代分析循环                        │  │
│  └─────────┬──────────┬──────────┬──────────┬───────────┘  │
│            │          │          │          │               │
│  ┌─────────▼──┐ ┌─────▼─────┐ ┌─▼────────┐ ┌▼──────────┐  │
│  │ Factor     │ │ Entry     │ │ Risk     │ │ Backtest  │  │
│  │ Agent      │ │ Agent     │ │ Agent    │ │ Agent     │  │
│  │ (子Agent)  │ │ (子Agent) │ │(子Agent) │ │(独立审计) │  │
│  └─────────┬──┘ └─────┬─────┘ └────┬─────┘ └─────┬─────┘  │
│            │          │            │              │         │
│  ┌─────────▼──────────▼────────────▼──────────────▼─────┐  │
│  │  原有 tools/ (11类, 30+工具) — 全部保留               │  │
│  │  data_tools / analysis_tools / backtest_tools / ...  │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Constraints (约束层)                                 │  │
│  │  - V1: 量比>2 + 上影线<0.5% + 排除一字板              │  │
│  │  - 仓位 ≤ 20%，止损 -15%                             │  │
│  │  - D1收阴排除                                        │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Feedback Loop (反馈循环)                             │  │
│  │  - 预测记录 → 实际验证 → 自动复盘                    │  │
│  │  - 策略参数自动调优                                  │  │
│  │  - 独立审计Agent不接受自评                           │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## 五、改造路径

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 1 | 记录分析文档（本文） | ✅ |
| Phase 2 | 用 smolagents 替换 agent 核心 | 🔄 |
| Phase 3 | 适配 tools/registry.py → smolagents Tool 接口 | ⬜ |
| Phase 4 | 组装子 Agents (Factor/Entry/Risk/Backtest) | ⬜ |
| Phase 5 | 搭建 Orchestrator 自迭代循环 | ⬜ |
| Phase 6 | 接入 feedback loop + 约束层 | ⬜ |

## 六、参考

- smolagents: https://github.com/huggingface/smolagents
- Harness 范式: https://www.cnblogs.com/ycfenxi/p/20061396
- 原始仓库: https://github.com/yuguonet/QuantDinger
