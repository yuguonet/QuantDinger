"""
Walk-Forward 验证 — 直接调用 BacktestService（无需启动后端）

直接 import 回测引擎，在进程内跑 WF 验证。
不需要后端服务运行，只需要项目代码 + 本地数据仓库。

用法（在项目根目录运行）:
    # Top 50 分层抽样
    python -m optimizer.wf_validate_direct --summary optimizer/optimizer_output/_summary.json --top 50

    # 全量 + 并行
    python -m optimizer.wf_validate_direct --summary optimizer/optimizer_output/_summary.json --all -j 10

    # 用默认参数（无回测结果文件时）
    python -m optimizer.wf_validate_direct --summary optimizer/optimizer_output/_summary.json --top 50 --use-defaults

    # 指定参数文件
    python -m optimizer.wf_validate_direct --summary optimizer/optimizer_output/_summary.json --params-file best_params.json

输出:
    wf_direct_results.json  — 完整结果
    wf_direct_summary.txt   — 可读报告
    wf_direct_passed.json   — 通过验证的股票
"""
import argparse
import json
import math
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

# ============================================================
# 路径设置（同 runner.py）
# ============================================================
_optimizer_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_optimizer_dir)
_backend_root = os.path.join(_project_root, "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Monkey-patch: 让 DataSourceFactory 优先读本地 data_warehouse
def _patch_datasource_warehouse():
    try:
        from app.data_sources.factory import DataSourceFactory
        from optimizer.data_warehouse.storage import read_local
        _orig_get_kline = DataSourceFactory.get_kline.__func__

        def _get_kline_with_warehouse(cls, market, symbol, timeframe, limit, before_time=None, after_time=None):
            try:
                data = read_local(
                    market=market, timeframe=timeframe, symbol=symbol,
                    limit=limit, before_time=before_time, after_time=after_time,
                )
                if data and len(data) >= 10:
                    return data
            except Exception:
                pass
            return _orig_get_kline(cls, market, symbol, timeframe, limit, before_time=before_time, after_time=after_time)

        DataSourceFactory.get_kline = classmethod(_get_kline_with_warehouse)
    except Exception as e:
        print(f"  ⚠️ Monkey-patch 失败: {e}")

_patch_datasource_warehouse()

# 延迟导入回测引擎
_BacktestService = None

def _get_backtest_service():
    global _BacktestService
    if _BacktestService is None:
        from app.services.backtest import BacktestService
        _BacktestService = BacktestService()
    return _BacktestService


# ============================================================
# WF 分割
# ============================================================

def generate_wf_splits(start_date, end_date, n_splits=3, train_ratio=0.7):
    total_days = (end_date - start_date).days
    if total_days < 30:
        raise ValueError(f"数据范围太短 ({total_days} 天)")
    split_size = total_days // n_splits
    splits = []
    for i in range(n_splits):
        seg_start = start_date + timedelta(days=i * split_size)
        seg_end = start_date + timedelta(days=(i + 1) * split_size)
        if i == n_splits - 1:
            seg_end = end_date
        seg_days = (seg_end - seg_start).days
        train_days = int(seg_days * train_ratio)
        train_end = seg_start + timedelta(days=train_days)
        test_start = train_end
        test_end = seg_end
        if test_start >= test_end:
            continue
        splits.append({
            "train_start": seg_start, "train_end": train_end,
            "test_start": test_start, "test_end": test_end,
        })
    return splits


def compute_wf_score(metrics, score_fn="composite", min_trades=1):
    sharpe = float(metrics.get("sharpeRatio", 0))
    win_rate = float(metrics.get("winRate", 0)) / 100.0
    max_dd = abs(float(metrics.get("maxDrawdown", 0))) / 100.0
    total_return = float(metrics.get("totalReturn", 0)) / 100.0
    total_trades = int(metrics.get("totalTrades", 0))
    profit_factor = float(metrics.get("profitFactor", 0))
    if total_trades < min_trades:
        return -10.0
    if score_fn == "sharpe":
        return sharpe
    if score_fn == "return_dd_ratio":
        return total_return / max(max_dd, 0.001)
    return sharpe * 0.4 + win_rate * 2.0 + min(profit_factor, 5.0) * 0.4 - max_dd * 2.0


# ============================================================
# 指标代码生成（内联，5 个 LLM 模板）
# ============================================================

def generate_indicator_code(template_key, p):
    if template_key == "rsi_volume_divergence":
        return _gen_rsi_volume_divergence(p)
    elif template_key == "triple_rsi_momentum":
        return _gen_triple_rsi_momentum(p)
    elif template_key == "vwap_volume_confirm":
        return _gen_vwap_volume_confirm(p)
    elif template_key == "vwap_bollinger_squeeze":
        return _gen_vwap_bollinger_squeeze(p)
    elif template_key == "macd_vol_divergence":
        return _gen_macd_vol_divergence(p)
    else:
        raise ValueError(f"未知模板: {template_key}")


def _gen_rsi_volume_divergence(p):
    rsi_period = p.get("rsi_period", 14)
    rsi_oversold = p.get("rsi_oversold", 30)
    lookback = p.get("lookback_period", 20)
    price_ma = p.get("price_ma_period", 10)
    vol_ma = p.get("vol_ma_period", 20)
    use_ma = p.get("use_ma_filter", False)
    ma_period = p.get("ma_period", 60)

    code = f"""
import pandas as pd
import numpy as np

delta = df['close'].diff()
gain = (delta.where(delta > 0, 0)).rolling(window={rsi_period}).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window={rsi_period}).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))

df['pvd_price_low'] = df['low'].rolling(window={lookback}).min()
df['pvd_price_high'] = df['high'].rolling(window={lookback}).max()
_delta = df['close'].diff()
_gain = _delta.clip(lower=0)
_loss = (-_delta).clip(lower=0)
_avg_gain = _gain.rolling(window=14, min_periods=14).mean()
_avg_loss = _loss.rolling(window=14, min_periods=14).mean()
_rs = _avg_gain / _avg_loss.replace(0, 0.0001)
df['pvd_rsi'] = 100 - (100 / (1 + _rs))
df['pvd_price_at_low'] = df['close'] <= df['pvd_price_low'] * 1.02
df['pvd_rsi_floor'] = df['pvd_rsi'].rolling(window={lookback}, min_periods=5).min()
df['pvd_rsi_higher'] = df['pvd_rsi'] > df['pvd_rsi_floor'] * 1.05
df['pvd_vol_ma'] = df['volume'].rolling(window={vol_ma}).mean()
df['pvd_vol_shrink'] = df['volume'] < df['pvd_vol_ma'] * 1.0
df['pvd_bullish'] = df['pvd_price_at_low'] & df['pvd_rsi_higher']
df['pvd_price_at_high'] = df['close'] >= df['pvd_price_high'] * 0.98
df['pvd_rsi_ceil'] = df['pvd_rsi'].rolling(window={lookback}, min_periods=5).max()
df['pvd_rsi_lower'] = df['pvd_rsi'] < df['pvd_rsi_ceil'] * 0.95
df['pvd_bearish'] = df['pvd_price_at_high'] & df['pvd_rsi_lower']

df['buy'] = (df['rsi'] < {rsi_oversold}) & df['pvd_bullish']
df['sell'] = df['pvd_bearish']
"""
    if use_ma:
        code += f"""
df['ma_{ma_period}'] = df['close'].ewm(span={ma_period}, adjust=False).mean()
df['buy'] = df['buy'] & (df['close'] > df['ma_{ma_period}'])
"""
    return code


def _gen_triple_rsi_momentum(p):
    rsi_fast = p.get("rsi_fast", 7)
    rsi_mid = p.get("rsi_mid", 14)
    rsi_slow = p.get("rsi_slow", 21)
    rsi_entry = p.get("rsi_entry", 30)
    rsi_trend_mid = p.get("rsi_trend_mid", 50)
    rsi_trend_slow = p.get("rsi_trend_slow", 50)
    return f"""
import pandas as pd
import numpy as np
def RSI(series, period):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))
df['rsi_fast'] = RSI(df['close'], {rsi_fast})
df['rsi_mid'] = RSI(df['close'], {rsi_mid})
df['rsi_slow'] = RSI(df['close'], {rsi_slow})
df['buy'] = (df['rsi_fast'] > {rsi_entry}) & (df['rsi_fast'].shift(1) <= {rsi_entry}) & (df['rsi_mid'] > {rsi_trend_mid}) & (df['rsi_slow'] > {rsi_trend_slow})
df['sell'] = (df['rsi_fast'] < {100 - rsi_entry})
"""


def _gen_vwap_volume_confirm(p):
    vwap_dev = p.get("vwap_dev_pct", 2.0)
    vol_ma_period = p.get("vol_ma_period", 20)
    vol_ratio = p.get("vol_ratio", 1.5)
    use_rsi = p.get("use_rsi_filter", False)
    rsi_period = p.get("rsi_period", 14)
    rsi_level = p.get("rsi_level", 35)
    code = f"""
import pandas as pd
import numpy as np
df['vwap_pv'] = (df['close'] * df['volume']).rolling(window=20).sum()
df['vwap_vol'] = df['volume'].rolling(window=20).sum()
df['vwap'] = df['vwap_pv'] / df['vwap_vol'].replace(0, np.nan)
df['vwap_lower'] = df['vwap'] * (1 - {vwap_dev} / 100)
df['vwap_upper'] = df['vwap'] * (1 + {vwap_dev} / 100)
df['vol_ma'] = df['volume'].rolling(window={vol_ma_period}).mean()
df['vol_ratio'] = df['volume'] / df['vol_ma'].replace(0, np.nan)
df['buy'] = (df['close'] < df['vwap_lower']) & (df['vol_ratio'] > {vol_ratio})
df['sell'] = (df['close'] > df['vwap_upper'])
"""
    if use_rsi:
        code += f"""
delta = df['close'].diff()
gain = (delta.where(delta > 0, 0)).rolling(window={rsi_period}).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window={rsi_period}).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))
df['buy'] = df['buy'] & (df['rsi'] < {rsi_level})
"""
    return code


def _gen_vwap_bollinger_squeeze(p):
    vwap_dev = p.get("vwap_dev_pct", 2.0)
    bb_period = p.get("bb_period", 20)
    bb_std = p.get("bb_std", 2.0)
    return f"""
import pandas as pd
import numpy as np
df['vwap_pv'] = (df['close'] * df['volume']).rolling(window=20).sum()
df['vwap_vol'] = df['volume'].rolling(window=20).sum()
df['vwap'] = df['vwap_pv'] / df['vwap_vol'].replace(0, np.nan)
df['vwap_lower'] = df['vwap'] * (1 - {vwap_dev} / 100)
df['vwap_upper'] = df['vwap'] * (1 + {vwap_dev} / 100)
sma = df['close'].rolling(window={bb_period}).mean()
std = df['close'].rolling(window={bb_period}).std()
df['bb_upper'] = sma + ({bb_std} * std)
df['bb_lower'] = sma - ({bb_std} * std)
df['buy'] = (df['close'] < df['vwap_lower']) & (df['close'] < df['bb_lower'])
df['sell'] = (df['close'] > df['vwap_upper'])
"""


def _gen_macd_vol_divergence(p):
    macd_fast = p.get("macd_fast", 12)
    macd_slow = p.get("macd_slow", 26)
    macd_signal = p.get("macd_signal", 9)
    lookback = p.get("lookback_period", 20)
    vol_ma = p.get("vol_ma_period", 20)
    use_rsi = p.get("use_rsi_confirm", True)
    rsi_period = p.get("rsi_period", 14)
    rsi_level = p.get("rsi_level", 40)
    code = f"""
import pandas as pd
import numpy as np
exp1 = df['close'].ewm(span={macd_fast}, adjust=False).mean()
exp2 = df['close'].ewm(span={macd_slow}, adjust=False).mean()
df['macd_line'] = exp1 - exp2
df['macd_signal_line'] = df['macd_line'].ewm(span={macd_signal}, adjust=False).mean()

# 量价背离（底背离 + 顶背离，与 strategy_compiler 一致）
df['pvd_price_low'] = df['low'].rolling(window={lookback}).min()
df['pvd_price_high'] = df['high'].rolling(window={lookback}).max()
_delta = df['close'].diff()
_gain = _delta.clip(lower=0)
_loss = (-_delta).clip(lower=0)
_avg_gain = _gain.rolling(window=14, min_periods=14).mean()
_avg_loss = _loss.rolling(window=14, min_periods=14).mean()
_rs = _avg_gain / _avg_loss.replace(0, 0.0001)
df['pvd_rsi'] = 100 - (100 / (1 + _rs))

# 底背离: 价格在低位 + RSI没创新低
df['pvd_price_at_low'] = df['close'] <= df['pvd_price_low'] * 1.02
df['pvd_rsi_floor'] = df['pvd_rsi'].rolling(window={lookback}, min_periods=5).min()
df['pvd_rsi_higher'] = df['pvd_rsi'] > df['pvd_rsi_floor'] * 1.05
df['pvd_bullish'] = df['pvd_price_at_low'] & df['pvd_rsi_higher']

# 顶背离: 价格在高位 + RSI没创新高（sell 信号，与 compiler 一致）
df['pvd_price_at_high'] = df['close'] >= df['pvd_price_high'] * 0.98
df['pvd_rsi_ceil'] = df['pvd_rsi'].rolling(window={lookback}, min_periods=5).max()
df['pvd_rsi_lower'] = df['pvd_rsi'] < df['pvd_rsi_ceil'] * 0.95
df['pvd_bearish'] = df['pvd_price_at_high'] & df['pvd_rsi_lower']

df['buy'] = (df['macd_line'] < df['macd_signal_line']) & df['pvd_bullish']
df['sell'] = df['pvd_bearish']
"""
    if use_rsi:
        code += f"""
delta = df['close'].diff()
gain = (delta.where(delta > 0, 0)).rolling(window={rsi_period}).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window={rsi_period}).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))
df['buy'] = df['buy'] & (df['rsi'] < {rsi_level})
"""
    return code


DEFAULT_PARAMS = {
    "rsi_volume_divergence": {
        "rsi_period": 14, "rsi_oversold": 30, "lookback_period": 20,
        "price_ma_period": 10, "vol_ma_period": 20,
        "use_ma_filter": False, "ma_period": 60, "stop_loss_pct": 4.0,
    },
    "triple_rsi_momentum": {
        "rsi_fast": 7, "rsi_mid": 14, "rsi_slow": 21,
        "rsi_entry": 35, "rsi_trend_mid": 50, "rsi_trend_slow": 50,
        "stop_loss_pct": 3.5,
    },
    "vwap_volume_confirm": {
        "vwap_dev_pct": 2.0, "vol_ma_period": 20, "vol_ratio": 1.5,
        "use_rsi_filter": False, "rsi_period": 14, "rsi_level": 35,
        "stop_loss_pct": 3.0,
    },
    "vwap_bollinger_squeeze": {
        "vwap_dev_pct": 2.0, "bb_period": 20, "bb_std": 2.0,
        "use_squeeze_filter": False, "squeeze_percentile": 20,
        "stop_loss_pct": 2.5,
    },
    "macd_vol_divergence": {
        "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
        "lookback_period": 20, "price_ma_period": 10, "vol_ma_period": 20,
        "use_rsi_confirm": True, "rsi_period": 14, "rsi_level": 40,
        "stop_loss_pct": 4.0,
    },
}


# ============================================================
# 直接回测（不走 HTTP）
# ============================================================

def run_backtest_direct(
    indicator_code, symbol, market, timeframe,
    start_date, end_date,
    initial_capital=10000.0, commission=0.001,
):
    """直接调用 BacktestService.run()"""
    bt = _get_backtest_service()
    result = bt.run(
        indicator_code=indicator_code,
        market=market,
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        commission=commission,
    )
    return {
        "sharpeRatio": result.get("sharpeRatio", 0),
        "totalReturn": result.get("totalReturn", 0),
        "winRate": result.get("winRate", 0),
        "maxDrawdown": result.get("maxDrawdown", 0),
        "profitFactor": result.get("profitFactor", 0),
        "totalTrades": result.get("totalTrades", 0),
    }


# ============================================================
# 单只股票 WF
# ============================================================

def run_wf_for_stock(
    symbol_raw, template_key, best_params,
    timeframe, start_str, end_str, score_fn,
    n_splits, train_ratio, min_trades=1, **kwargs,
):
    if ":" in symbol_raw:
        market, symbol = symbol_raw.split(":", 1)
    else:
        market, symbol = "CNStock", symbol_raw

    start_date = datetime.strptime(start_str, "%Y-%m-%d")
    end_date = datetime.strptime(end_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    try:
        splits = generate_wf_splits(start_date, end_date, n_splits, train_ratio)
        indicator_code = generate_indicator_code(template_key, best_params)

        import numpy as np
        train_scores, test_scores = [], []
        split_details = []

        for i, s in enumerate(splits):
            # 训练集
            try:
                train_result = run_backtest_direct(
                    indicator_code, symbol, market, timeframe,
                    s["train_start"], s["train_end"],
                )
                train_score = compute_wf_score(train_result, score_fn, min_trades)
            except Exception:
                train_result = None
                train_score = -10.0

            # 测试集
            try:
                test_result = run_backtest_direct(
                    indicator_code, symbol, market, timeframe,
                    s["test_start"], s["test_end"],
                )
                test_score = compute_wf_score(test_result, score_fn, min_trades)
            except Exception:
                test_result = None
                test_score = -10.0

            train_scores.append(train_score)
            test_scores.append(test_score)

            split_details.append({
                "fold": i + 1,
                "train_period": f"{s['train_start'].strftime('%Y-%m-%d')} ~ {s['train_end'].strftime('%Y-%m-%d')}",
                "test_period": f"{s['test_start'].strftime('%Y-%m-%d')} ~ {s['test_end'].strftime('%Y-%m-%d')}",
                "train_score": round(train_score, 4),
                "test_score": round(test_score, 4),
                "train_trades": (train_result or {}).get("totalTrades", 0),
                "test_trades": (test_result or {}).get("totalTrades", 0),
                "train_sharpe": (train_result or {}).get("sharpeRatio", 0),
                "test_sharpe": (test_result or {}).get("sharpeRatio", 0),
                "train_return": (train_result or {}).get("totalReturn", 0),
                "test_return": (test_result or {}).get("totalReturn", 0),
                "status": "ok" if train_result and test_result else "partial",
            })

        avg_train = float(np.mean(train_scores)) if train_scores else 0
        avg_test = float(np.mean(test_scores)) if test_scores else 0

        if avg_train > 0:
            overfitting_ratio = 1 - (avg_test / avg_train)
        else:
            overfitting_ratio = 1.0

        consistency = 1 - (float(np.std(test_scores)) / max(abs(avg_test), 0.01))
        consistency = max(0, min(1, consistency))

        if overfitting_ratio > 0.5:
            verdict = "❌ 严重过拟合"
        elif overfitting_ratio > 0.3:
            verdict = "⚠️ 中度过拟合"
        elif avg_test < 0:
            verdict = "❌ 样本外亏损"
        elif consistency < 0.5:
            verdict = "⚠️ 不稳定"
        elif overfitting_ratio < 0.1 and avg_test > 0:
            verdict = "✅ 通过"
        else:
            verdict = "⚠️ 边缘"

        return {
            "symbol": symbol_raw,
            "template": template_key,
            "wf_result": {
                "n_splits": len(splits),
                "splits": split_details,
                "avg_train_score": round(avg_train, 4),
                "avg_test_score": round(avg_test, 4),
                "overfitting_ratio": round(overfitting_ratio, 4),
                "consistency": round(consistency, 4),
                "verdict": verdict,
            },
            "status": "ok",
        }
    except Exception as e:
        return {
            "symbol": symbol_raw,
            "template": template_key,
            "wf_result": None,
            "status": f"error: {str(e)}",
        }


# ============================================================
# Worker + 参数提取
# ============================================================

def _worker_init():
    """每个子进程初始化：monkey-patch + 模块导入"""
    _patch_datasource_warehouse()
    # 确保模板模块被导入（多进程 fork 后可能丢失）
    try:
        import optimizer.strategy_templates_llm
        import optimizer.strategy_templates_ashare
    except Exception:
        pass
    # 重置 BacktestService 单例，让每个进程创建自己的实例
    global _BacktestService
    _BacktestService = None


def _worker_run(args_tuple):
    return run_wf_for_stock(*args_tuple)


def extract_best_params(input_dir, template_filter=None):
    params_map = {}
    for root, dirs, files in os.walk(input_dir):
        for fname in files:
            if not fname.endswith(".json") or fname.startswith("_"):
                continue
            if template_filter and f"_{template_filter}.json" not in fname:
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if "best" not in data or "params" not in data["best"]:
                    continue
                symbol = data.get("symbol", "")
                market = data.get("market", "CNStock")
                template = data.get("template", "")
                best_params = data["best"]["params"]
                score = data["best"].get("score", 0)
                if ":" not in symbol:
                    symbol_raw = f"{market}:{symbol}"
                else:
                    symbol_raw = symbol
                if symbol_raw not in params_map or score > params_map[symbol_raw].get("_score", 0):
                    params_map[symbol_raw] = {"template": template, "params": best_params, "_score": score}
            except Exception:
                continue
    return params_map


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Walk-Forward 直接验证（无需后端服务）")
    parser.add_argument("--summary", "-s", required=True)
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--template", "-t", type=str, default=None)
    parser.add_argument("--splits", type=int, default=3)
    parser.add_argument("--train-ratio", type=float, default=0.7)
    parser.add_argument("--score", type=str, default="composite",
                        choices=["sharpe", "return_dd_ratio", "composite"])
    parser.add_argument("--jobs", "-j", type=int, default=1)
    parser.add_argument("--output", "-o", type=str, default=None)
    parser.add_argument("--params-file", type=str, default=None)
    parser.add_argument("--params-dir", type=str, default=None)
    parser.add_argument("--use-defaults", action="store_true")
    parser.add_argument("--min-trades", type=int, default=1, help="WF窗口最低交易笔数（低于此返回-10，默认1）")
    parser.add_argument("--capital", type=float, default=10000.0)
    parser.add_argument("--commission", type=float, default=0.001)

    args = parser.parse_args()

    with open(args.summary, "r", encoding="utf-8") as f:
        summary = json.load(f)

    timeframe = summary.get("timeframe", "1D")
    period = summary.get("period", "2024-01-01 ~ 2025-12-31")
    start_str = period.split("~")[0].strip()
    end_str = period.split("~")[1].strip()
    output_dir = args.output or os.path.dirname(os.path.abspath(args.summary))

    # 加载参数
    custom_params = {}
    if args.params_file and os.path.isfile(args.params_file):
        with open(args.params_file, "r", encoding="utf-8") as f:
            custom_params = json.load(f)
        print(f"  📄 参数文件: {args.params_file} ({len(custom_params)} 条)")
    elif args.params_dir:
        custom_params = extract_best_params(args.params_dir, args.template)
        print(f"  📂 提取参数: {len(custom_params)} 条")

    # 构建任务
    all_ranked = summary.get("all_ranked", [])
    if args.template:
        ranked = [r for r in all_ranked if r["template"] == args.template]
    else:
        ranked = all_ranked
    ranked = [r for r in ranked if r["metrics"].get("totalTrades", 0) >= 4]

    if not args.all:
        n = args.top
        if len(ranked) > n:
            high = [r for r in ranked if r["best_score"] >= 4]
            mid = [r for r in ranked if 2.5 <= r["best_score"] < 4]
            low = [r for r in ranked if r["best_score"] < 2.5]
            n_each = n // 3
            selected = high[:n_each] + mid[:n_each] + low[:n - n_each * 2]
            selected_set = set(id(r) for r in selected)
            for r in ranked:
                if len(selected) >= n:
                    break
                if id(r) not in selected_set:
                    selected.append(r)
            ranked = selected[:n]

    print(f"\n{'='*60}")
    print(f"  Walk-Forward 直接验证（BacktestService）")
    print(f"  股票数: {len(ranked)}")
    print(f"  WF: {args.splits} folds, train={args.train_ratio}")
    print(f"  评分: {args.score}")
    print(f"  并行: {args.jobs}")
    print(f"{'='*60}")

    # 预热回测引擎（加载模块）
    print(f"\n  🔧 预热回测引擎...")
    try:
        _get_backtest_service()
        print(f"  ✅ 引擎就绪")
    except Exception as e:
        print(f"  ❌ 引擎加载失败: {e}")
        sys.exit(1)

    # 构建任务列表
    tasks = []
    for r in ranked:
        symbol_raw = r["symbol"]
        template_key = r["template"]

        if symbol_raw in custom_params:
            entry = custom_params[symbol_raw]
            best_params = entry.get("params", entry)
        else:
            market, symbol = symbol_raw.split(":") if ":" in symbol_raw else ("CNStock", symbol_raw)
            tf_dir_map = {"1D": "daily", "1H": "hourly", "4H": "4h", "1W": "weekly"}
            tf_dir = tf_dir_map.get(timeframe, timeframe.lower())
            detail_path = os.path.join(
                output_dir, market, tf_dir,
                f"{symbol.replace('/', '_').replace(':', '_')}_{template_key}.json"
            )
            if os.path.isfile(detail_path):
                with open(detail_path, "r", encoding="utf-8") as f:
                    detail = json.load(f)
                best_params = detail.get("best", {}).get("params", {})
            elif args.use_defaults:
                best_params = DEFAULT_PARAMS.get(template_key, {})
            else:
                print(f"  ⚠️ {symbol_raw} 无参数，跳过（用 --use-defaults）")
                continue

        if not best_params:
            continue

        tasks.append((
            symbol_raw, template_key, best_params,
            timeframe, start_str, end_str, args.score,
            args.splits, args.train_ratio, args.min_trades,
        ))

    if not tasks:
        print(f"\n  ❌ 没有可用任务")
        sys.exit(1)

    print(f"\n  📋 有效任务: {len(tasks)}")

    # 执行
    t0 = time.time()
    results = []

    if args.jobs <= 1:
        for i, task in enumerate(tasks):
            symbol_raw = task[0]
            print(f"  [{i+1}/{len(tasks)}] {symbol_raw} ...", end=" ", flush=True)
            result = _worker_run(task)
            results.append(result)
            if result["status"] == "ok":
                wf = result["wf_result"]
                print(f"test={wf['avg_test_score']:.3f} overfit={wf['overfitting_ratio']:.3f} {wf['verdict']}")
            else:
                print(f"❌ {result['status']}")
    else:
        import multiprocessing
        os.environ["PYTHONUNBUFFERED"] = "1"
        with multiprocessing.Pool(processes=args.jobs, initializer=_worker_init) as pool:
            results = pool.map(_worker_run, tasks)

    elapsed = time.time() - t0

    # ── 汇总 ──
    ok = [r for r in results if r["status"] == "ok"]
    err = [r for r in results if r["status"] != "ok"]

    print(f"\n{'='*60}")
    print(f"  📊 Walk-Forward 验证结果")
    print(f"  耗时: {elapsed:.1f}s | 成功: {len(ok)} | 失败: {len(err)}")
    print(f"{'='*60}")

    if not ok:
        _save_results(output_dir, results, args)
        return

    ok.sort(key=lambda r: r["wf_result"]["avg_test_score"], reverse=True)

    import numpy as np
    test_scores = [r["wf_result"]["avg_test_score"] for r in ok]
    overfit_ratios = [r["wf_result"]["overfitting_ratio"] for r in ok]
    consistencies = [r["wf_result"]["consistency"] for r in ok]

    print(f"\n  测试得分: 均值={np.mean(test_scores):.3f} 中位={np.median(test_scores):.3f}")
    print(f"  过拟合比: 均值={np.mean(overfit_ratios):.3f} 中位={np.median(overfit_ratios):.3f}")
    print(f"  一致性:   均值={np.mean(consistencies):.3f} 中位={np.median(consistencies):.3f}")

    from collections import Counter
    verdicts = [r["wf_result"]["verdict"] for r in ok]
    print(f"\n  判定分布:")
    for v, cnt in Counter(verdicts).most_common():
        print(f"    {v}: {cnt} ({cnt/len(ok)*100:.1f}%)")

    passed = sum(1 for r in ok
                 if r["wf_result"]["overfitting_ratio"] < 0.5
                 and r["wf_result"]["avg_test_score"] > 0)
    print(f"\n  ✅ 通过 (overfit<0.5 & test>0): {passed}/{len(ok)} ({passed/len(ok)*100:.1f}%)")

    strict = sum(1 for r in ok
                 if r["wf_result"]["overfitting_ratio"] < 0.3
                 and r["wf_result"]["avg_test_score"] > 0
                 and r["wf_result"]["consistency"] > 0.5)
    print(f"  ✅ 严格通过: {strict}/{len(ok)} ({strict/len(ok)*100:.1f}%)")

    # Top 20
    print(f"\n  Top 20:")
    print(f"  {'#':<4} {'股票':<18} {'模板':<25} {'Train':<8} {'Test':<8} {'Overfit':<8} {'Consist':<8} {'判定'}")
    print(f"  {'-'*105}")
    for i, r in enumerate(ok[:20], 1):
        wf = r["wf_result"]
        print(f"  {i:<4} {r['symbol']:<18} {r['template']:<25} "
              f"{wf['avg_train_score']:<8.3f} {wf['avg_test_score']:<8.3f} "
              f"{wf['overfitting_ratio']:<8.3f} {wf['consistency']:<8.3f} "
              f"{wf['verdict']}")

    _save_results(output_dir, results, args)

    passed_list = [r for r in ok
                   if r["wf_result"]["overfitting_ratio"] < 0.5
                   and r["wf_result"]["avg_test_score"] > 0]
    if passed_list:
        passed_path = os.path.join(output_dir, "wf_direct_passed.json")
        with open(passed_path, "w", encoding="utf-8") as f:
            json.dump([{
                "symbol": r["symbol"], "template": r["template"],
                "wf_test_score": r["wf_result"]["avg_test_score"],
                "overfitting_ratio": r["wf_result"]["overfitting_ratio"],
                "consistency": r["wf_result"]["consistency"],
            } for r in passed_list], f, ensure_ascii=False, indent=2)
        print(f"\n  📄 通过列表: {passed_path}")


def _save_results(output_dir, results, args):
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "wf_direct_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "config": {"splits": args.splits, "train_ratio": args.train_ratio, "score": args.score},
            "total": len(results),
            "results": results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  📄 完整结果: {json_path}")

    report_path = os.path.join(output_dir, "wf_direct_summary.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"Walk-Forward 直接验证报告\n")
        f.write(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"配置: {args.splits} folds, train={args.train_ratio}, score={args.score}\n\n")
        ok = [r for r in results if r["status"] == "ok"]
        if ok:
            ok.sort(key=lambda r: r["wf_result"]["avg_test_score"], reverse=True)
            f.write(f"{'#':<4} {'股票':<18} {'模板':<25} {'Train':<8} {'Test':<8} {'Overfit':<8} {'Consist':<8} {'判定'}\n")
            f.write("-" * 105 + "\n")
            for i, r in enumerate(ok, 1):
                wf = r["wf_result"]
                f.write(f"{i:<4} {r['symbol']:<18} {r['template']:<25} "
                        f"{wf['avg_train_score']:<8.3f} {wf['avg_test_score']:<8.3f} "
                        f"{wf['overfitting_ratio']:<8.3f} {wf['consistency']:<8.3f} "
                        f"{wf['verdict']}\n")
    print(f"  📄 报告: {report_path}")


if __name__ == "__main__":
    main()
