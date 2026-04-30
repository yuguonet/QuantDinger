"""
新模板的默认参数 + runner 注册补丁

在 runner.py 中添加：
    from optimizer.strategy_templates_new import NEW_STRATEGY_TEMPLATES

在 list_templates 中合并：
    all_templates.update(NEW_STRATEGY_TEMPLATES)
"""

# 默认参数（用于 --use-defaults 或参数缺失时回退）
NEW_DEFAULT_PARAMS = {
    "vwap_rsi_confirm": {
        "vwap_dev_pct": 2.0,
        "rsi_period": 14,
        "rsi_level": 33,
        "use_vol_filter": True,
        "vol_ma_period": 15,
        "vol_ratio": 1.3,
        "stop_loss_pct": 3.0,
    },
    "rsi_bollinger_support": {
        "rsi_period": 14,
        "rsi_level": 33,
        "bb_period": 18,
        "bb_std": 2.0,
        "use_vol_filter": True,
        "vol_ma_period": 15,
        "vol_ratio": 1.3,
        "stop_loss_pct": 3.0,
    },
    "vwap_macd_volume": {
        "vwap_dev_pct": 2.0,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "vol_ma_period": 15,
        "vol_ratio": 1.3,
        "stop_loss_pct": 3.5,
    },
    "kdj_vwap_reversal": {
        "kdj_k": 9,
        "kdj_d": 3,
        "kdj_j": 3,
        "kdj_oversold": 20,
        "vwap_dev_pct": 2.0,
        "use_rsi_filter": True,
        "rsi_period": 14,
        "rsi_level": 33,
        "stop_loss_pct": 3.5,
    },
    "ema_rsi_pullback": {
        "ema_fast": 10,
        "ema_slow": 30,
        "rsi_period": 14,
        "rsi_pullback": 42,
        "stop_loss_pct": 3.5,
    },
}


# ============================================================
# 完整的集成步骤
# ============================================================
"""
1. 将 new_template_indicators.py 中的 5 个 _gen_xxx 函数复制到
   wf_validate_direct.py 的 generate_indicator_code 函数之前

2. 在 wf_validate_direct.py 的 generate_indicator_code 函数中添加 5 个 elif：
    elif template_key == "vwap_rsi_confirm":
        return _gen_vwap_rsi_confirm(p)
    elif template_key == "rsi_bollinger_support":
        return _gen_rsi_bollinger_support(p)
    elif template_key == "vwap_macd_volume":
        return _gen_vwap_macd_volume(p)
    elif template_key == "kdj_vwap_reversal":
        return _gen_kdj_vwap_reversal(p)
    elif template_key == "ema_rsi_pullback":
        return _gen_ema_rsi_pullback(p)

3. 在 DEFAULT_PARAMS 字典中添加 NEW_DEFAULT_PARAMS 的内容

4. 在 runner.py 中导入并注册新模板：
    from optimizer.strategy_templates_new import NEW_STRATEGY_TEMPLATES

5. 运行回测：
    python -m optimizer.runner -t vwap_rsi_confirm -m CNStock -s "002371.SZ" -tf 1D \
        --start 2024-01-01 --end 2025-12-31 --trials 100

6. 全量回测新模板：
    python -m optimizer.runner --all --set new -m CNStock --all-local -tf 1D \
        --start 2024-01-01 --end 2025-12-31 --trials 100 -j 35
"""
