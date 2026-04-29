"""
QuantDinger Auto Strategy Optimizer
自动策略优化器

Phase 1: IndicatorStrategy + 参数搜索（原始 7 模板）
Phase 2: A 股扩展模板 + LLM 策略生成
Phase 3: LLM 动态策略发现

模块结构:
    optimizer.py              - 优化引擎（Random Search + Optuna）
    param_space.py            - 原始策略模板（7 个通用指标策略）
    strategy_templates_ashare.py - A 股扩展模板（10 个 A 股专用策略）
    llm_strategy_generator.py - LLM 策略模板生成器
    ashare_adapter.py         - A 股市场适配器（T+1/涨跌停/数据源）
    walk_forward.py           - Walk-Forward 验证（防过拟合）
    runner.py                 - 主入口脚本
    mock_data.py              - 本地模拟数据

用法:
    # 原始模板优化
    python -m app.optimizer.runner --symbol "Crypto:BTC/USDT" --timeframe 4H

    # A 股模板优化
    python -m app.optimizer.runner --template atr_breakout --symbol "A_SHARE:000001.SZ" --timeframe 1D

    # 列出所有模板
    python -m app.optimizer.llm_strategy_generator list

    # LLM 批量生成新策略
    python -m app.optimizer.llm_strategy_generator batch
"""
