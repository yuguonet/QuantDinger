"""
Walk-Forward 验证 — 直接调用后端 API 版

绕过 optimizer 依赖链，直接用 /backtest API 跑 WF 验证。
只需要: _summary.json + 回测结果文件(含最优参数) + 后端服务运行中

用法（在项目根目录运行）:
    # Top 50 分层抽样
    python -m optimizer.wf_validate_api --summary optimizer/optimizer_output/_summary.json --top 50

    # 全量 + 并行
    python -m optimizer.wf_validate_api --summary optimizer/optimizer_output/_summary.json --all -j 10

    # 自定义 API 地址
    python -m optimizer.wf_validate_api --summary optimizer/optimizer_output/_summary.json --top 50 --api http://localhost:5000

    # 跳过需要参数文件的股票（用模板默认参数）
    python -m optimizer.wf_validate_api --summary optimizer/optimizer_output/_summary.json --top 50 --use-defaults

    # 只跑 WF，不重新提取参数
    python -m optimizer.wf_validate_api --summary optimizer/optimizer_output/_summary.json --params-file best_params.json

输出:
    wf_api_results.json  — 完整结果
    wf_api_summary.txt   — 可读报告
    wf_api_passed.json   — 通过验证的股票
"""
import argparse
import json
import math
import multiprocessing
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

import requests

# ============================================================
# WF 分割逻辑（独立实现，不依赖 walk_forward.py）
# ============================================================

def generate_wf_splits(
    start_date: datetime,
    end_date: datetime,
    n_splits: int = 3,
    train_ratio: float = 0.7,
) -> List[Dict[str, datetime]]:
    """生成 Walk-Forward 分割"""
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

        train_start = seg_start
        train_end = seg_start + timedelta(days=train_days)
        test_start = train_end
        test_end = seg_end

        if test_start >= test_end:
            continue

        splits.append({
            "train_start": train_start,
            "train_end": train_end,
            "test_start": test_start,
            "test_end": test_end,
        })

    return splits


def compute_wf_score(metrics: dict, score_fn: str = "composite") -> float:
    """计算单次回测的评分"""
    sharpe = float(metrics.get("sharpeRatio", 0))
    win_rate = float(metrics.get("winRate", 0)) / 100.0
    max_dd = abs(float(metrics.get("maxDrawdown", 0))) / 100.0
    total_return = float(metrics.get("totalReturn", 0)) / 100.0
    total_trades = int(metrics.get("totalTrades", 0))
    profit_factor = float(metrics.get("profitFactor", 0))

    if total_trades < 2:  # 放宽到 2 笔（原版是 3）
        return -10.0

    if score_fn == "sharpe":
        return sharpe
    if score_fn == "return_dd_ratio":
        return total_return / max(max_dd, 0.001)
    # composite
    return sharpe * 0.4 + win_rate * 2.0 + min(profit_factor, 5.0) * 0.4 - max_dd * 2.0


# ============================================================
# 策略代码生成（内联，不依赖 strategy_compiler）
# ============================================================

def generate_indicator_code(template_key: str, params: dict) -> str:
    """从模板 + 参数生成 indicator 代码"""

    if template_key == "rsi_volume_divergence":
        return _gen_rsi_volume_divergence(params)
    elif template_key == "triple_rsi_momentum":
        return _gen_triple_rsi_momentum(params)
    elif template_key == "vwap_volume_confirm":
        return _gen_vwap_volume_confirm(params)
    elif template_key == "vwap_bollinger_squeeze":
        return _gen_vwap_bollinger_squeeze(params)
    elif template_key == "macd_vol_divergence":
        return _gen_macd_vol_divergence(params)
    else:
        raise ValueError(f"未知模板: {template_key}")


def _gen_rsi_volume_divergence(p: dict) -> str:
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

# RSI
delta = df['close'].diff()
gain = (delta.where(delta > 0, 0)).rolling(window={rsi_period}).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window={rsi_period}).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))

# Price-Volume Divergence
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

# Signal logic
df['raw_buy'] = (df['rsi'] < {rsi_oversold}) & df['pvd_bullish']
df['raw_sell'] = df['pvd_bearish']
"""
    if use_ma:
        code += f"""
df['ma_{ma_period}'] = df['close'].ewm(span={ma_period}, adjust=False).mean()
df['raw_buy'] = df['raw_buy'] & (df['close'] > df['ma_{ma_period}'])
"""

    code += """
df['buy'] = df['raw_buy']
df['sell'] = df['raw_sell']
"""
    return code


def _gen_triple_rsi_momentum(p: dict) -> str:
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


def _gen_vwap_volume_confirm(p: dict) -> str:
    vwap_dev = p.get("vwap_dev_pct", 2.0)
    vol_ma_period = p.get("vol_ma_period", 20)
    vol_ratio = p.get("vol_ratio", 1.5)
    use_rsi = p.get("use_rsi_filter", False)
    rsi_period = p.get("rsi_period", 14)
    rsi_level = p.get("rsi_level", 35)

    code = f"""
import pandas as pd
import numpy as np

# VWAP
df['vwap_pv'] = (df['close'] * df['volume']).rolling(window=20).sum()
df['vwap_vol'] = df['volume'].rolling(window=20).sum()
df['vwap'] = df['vwap_pv'] / df['vwap_vol'].replace(0, np.nan)
df['vwap_lower'] = df['vwap'] * (1 - {vwap_dev} / 100)
df['vwap_upper'] = df['vwap'] * (1 + {vwap_dev} / 100)

# Volume
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


def _gen_vwap_bollinger_squeeze(p: dict) -> str:
    vwap_dev = p.get("vwap_dev_pct", 2.0)
    bb_period = p.get("bb_period", 20)
    bb_std = p.get("bb_std", 2.0)

    return f"""
import pandas as pd
import numpy as np

# VWAP
df['vwap_pv'] = (df['close'] * df['volume']).rolling(window=20).sum()
df['vwap_vol'] = df['volume'].rolling(window=20).sum()
df['vwap'] = df['vwap_pv'] / df['vwap_vol'].replace(0, np.nan)
df['vwap_lower'] = df['vwap'] * (1 - {vwap_dev} / 100)
df['vwap_upper'] = df['vwap'] * (1 + {vwap_dev} / 100)

# Bollinger Bands
sma = df['close'].rolling(window={bb_period}).mean()
std = df['close'].rolling(window={bb_period}).std()
df['bb_upper'] = sma + ({bb_std} * std)
df['bb_lower'] = sma - ({bb_std} * std)

df['buy'] = (df['close'] < df['vwap_lower']) & (df['close'] < df['bb_lower'])
df['sell'] = (df['close'] > df['vwap_upper'])
"""


def _gen_macd_vol_divergence(p: dict) -> str:
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

# MACD
exp1 = df['close'].ewm(span={macd_fast}, adjust=False).mean()
exp2 = df['close'].ewm(span={macd_slow}, adjust=False).mean()
df['macd_line'] = exp1 - exp2
df['macd_signal'] = df['macd_line'].ewm(span={macd_signal}, adjust=False).mean()

# Price-Volume Divergence
df['pvd_price_low'] = df['low'].rolling(window={lookback}).min()
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

df['buy'] = (df['macd_line'] < df['macd_signal']) & df['pvd_bullish']
df['sell'] = (df['macd_line'] > df['macd_signal'])
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


# ============================================================
# API 回测调用
# ============================================================

def call_backtest_api(
    api_base: str,
    indicator_code: str,
    symbol: str,
    market: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    initial_capital: float = 10000.0,
    commission: float = 0.001,
    timeout: int = 60,
) -> Optional[dict]:
    """调用 /backtest API"""
    url = f"{api_base.rstrip('/')}/backtest"
    payload = {
        "indicatorCode": indicator_code,
        "symbol": symbol,
        "market": market,
        "timeframe": timeframe,
        "startDate": start_date,
        "endDate": end_date,
        "initialCapital": initial_capital,
        "commission": commission,
        "persist": False,  # 不落库，WF 只需要结果
    }

    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 1:
            return data.get("data", {}).get("result", {})
        else:
            return None
    except Exception as e:
        return None


# ============================================================
# 单只股票 WF 验证
# ============================================================

def run_wf_for_stock(
    symbol_raw: str,
    template_key: str,
    best_params: dict,
    timeframe: str,
    start_str: str,
    end_str: str,
    score_fn: str,
    n_splits: int,
    train_ratio: float,
    api_base: str,
) -> Dict[str, Any]:
    """对单只股票跑 Walk-Forward 验证"""

    # 解析 symbol
    if ":" in symbol_raw:
        parts = symbol_raw.split(":", 1)
        market, symbol = parts[0], parts[1]
    else:
        market, symbol = "CNStock", symbol_raw

    start_date = datetime.strptime(start_str, "%Y-%m-%d")
    end_date = datetime.strptime(end_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    try:
        # 1. 生成 WF 分割
        splits = generate_wf_splits(start_date, end_date, n_splits, train_ratio)

        # 2. 生成 indicator 代码
        indicator_code = generate_indicator_code(template_key, best_params)

        # 3. 对每个分割跑回测
        train_scores = []
        test_scores = []
        split_details = []

        for i, s in enumerate(splits):
            # 训练集回测
            train_result = call_backtest_api(
                api_base, indicator_code, symbol, market, timeframe,
                s["train_start"].strftime("%Y-%m-%d"),
                s["train_end"].strftime("%Y-%m-%d"),
            )

            # 测试集回测
            test_result = call_backtest_api(
                api_base, indicator_code, symbol, market, timeframe,
                s["test_start"].strftime("%Y-%m-%d"),
                s["test_end"].strftime("%Y-%m-%d"),
            )

            if train_result is None or test_result is None:
                train_scores.append(-10.0)
                test_scores.append(-10.0)
                split_details.append({
                    "fold": i + 1,
                    "train_period": f"{s['train_start'].strftime('%Y-%m-%d')} ~ {s['train_end'].strftime('%Y-%m-%d')}",
                    "test_period": f"{s['test_start'].strftime('%Y-%m-%d')} ~ {s['test_end'].strftime('%Y-%m-%d')}",
                    "train_score": -10.0,
                    "test_score": -10.0,
                    "train_trades": 0,
                    "test_trades": 0,
                    "status": "api_error",
                })
                continue

            train_score = compute_wf_score(train_result, score_fn)
            test_score = compute_wf_score(test_result, score_fn)
            train_scores.append(train_score)
            test_scores.append(test_score)

            split_details.append({
                "fold": i + 1,
                "train_period": f"{s['train_start'].strftime('%Y-%m-%d')} ~ {s['train_end'].strftime('%Y-%m-%d')}",
                "test_period": f"{s['test_start'].strftime('%Y-%m-%d')} ~ {s['test_end'].strftime('%Y-%m-%d')}",
                "train_score": round(train_score, 4),
                "test_score": round(test_score, 4),
                "train_trades": train_result.get("totalTrades", 0),
                "test_trades": test_result.get("totalTrades", 0),
                "train_sharpe": train_result.get("sharpeRatio", 0),
                "test_sharpe": test_result.get("sharpeRatio", 0),
                "train_return": train_result.get("totalReturn", 0),
                "test_return": test_result.get("totalReturn", 0),
                "status": "ok",
            })

        # 4. 计算汇总指标
        import numpy as np
        avg_train = float(np.mean(train_scores)) if train_scores else 0
        avg_test = float(np.mean(test_scores)) if test_scores else 0

        if avg_train > 0:
            overfitting_ratio = 1 - (avg_test / avg_train)
        else:
            overfitting_ratio = 1.0

        consistency = 1 - (float(np.std(test_scores)) / max(abs(avg_test), 0.01))
        consistency = max(0, min(1, consistency))

        # 判定
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
# Worker
# ============================================================

def _worker_run(args_tuple):
    return run_wf_for_stock(*args_tuple)


# ============================================================
# 参数提取（从回测结果文件）
# ============================================================

def extract_best_params(input_dir: str, template_filter: str = None) -> dict:
    """从回测结果文件提取最优参数"""
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
                    params_map[symbol_raw] = {
                        "template": template,
                        "params": best_params,
                        "_score": score,
                    }
            except Exception:
                continue

    return params_map


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Walk-Forward API 验证")
    parser.add_argument("--summary", "-s", required=True, help="_summary.json 路径")
    parser.add_argument("--top", type=int, default=50, help="Top N 股票")
    parser.add_argument("--all", action="store_true", help="全部股票")
    parser.add_argument("--template", "-t", type=str, default=None, help="只验证特定模板")
    parser.add_argument("--api", type=str, default="http://localhost:5000", help="后端 API 地址")
    parser.add_argument("--splits", type=int, default=3, help="WF 分割数（默认 3，比 5 更适合低频策略）")
    parser.add_argument("--train-ratio", type=float, default=0.7, help="训练集比例")
    parser.add_argument("--score", type=str, default="composite",
                        choices=["sharpe", "return_dd_ratio", "composite"])
    parser.add_argument("--jobs", "-j", type=int, default=1, help="并行进程数")
    parser.add_argument("--output", "-o", type=str, default=None, help="输出目录")
    parser.add_argument("--params-file", type=str, default=None, help="参数文件")
    parser.add_argument("--params-dir", type=str, default=None, help="回测结果目录（自动提取参数）")
    parser.add_argument("--use-defaults", action="store_true", help="无参数时用模板默认值")
    parser.add_argument("--token", type=str, default=None, help="API auth token（如需要）")

    args = parser.parse_args()

    # 加载 summary
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
        print(f"  📂 从 {args.params_dir} 提取参数: {len(custom_params)} 条")

    # 构建任务列表
    all_ranked = summary.get("all_ranked", [])
    if args.template:
        ranked = [r for r in all_ranked if r["template"] == args.template]
    else:
        ranked = all_ranked

    # 过滤零交易
    ranked = [r for r in ranked if r["metrics"].get("totalTrades", 0) >= 4]

    if not args.all:
        # 分层抽样：高/中/低各取 1/3
        n = args.top
        if len(ranked) > n:
            high = [r for r in ranked if r["best_score"] >= 4]
            mid = [r for r in ranked if 2.5 <= r["best_score"] < 4]
            low = [r for r in ranked if r["best_score"] < 2.5]
            n_each = n // 3
            n_rest = n - n_each * 2
            selected = high[:n_each] + mid[:n_each] + low[:n_rest]
            # 不够的话从剩余补
            selected_set = set(id(r) for r in selected)
            for r in ranked:
                if len(selected) >= n:
                    break
                if id(r) not in selected_set:
                    selected.append(r)
            ranked = selected[:n]

    print(f"\n{'='*60}")
    print(f"  Walk-Forward API 验证")
    print(f"  股票数: {len(ranked)}")
    print(f"  API: {args.api}")
    print(f"  WF 分割: {args.splits} folds, 训练比例 {args.train_ratio}")
    print(f"  评分: {args.score}")
    print(f"  并行: {args.jobs}")
    print(f"{'='*60}")

    # 检查 API 是否可达
    print(f"\n  🔍 检查 API 连接...")
    try:
        resp = requests.get(f"{args.api.rstrip('/')}/health", timeout=5)
        if resp.status_code == 200:
            print(f"  ✅ API 可达")
        else:
            print(f"  ⚠️ API 返回 {resp.status_code}，继续尝试...")
    except Exception as e:
        print(f"  ❌ API 不可达: {e}")
        print(f"  请确保后端服务在 {args.api} 运行中")
        sys.exit(1)

    # 构建任务
    tasks = []
    for r in ranked:
        symbol_raw = r["symbol"]
        template_key = r["template"]

        # 获取最优参数
        if symbol_raw in custom_params:
            entry = custom_params[symbol_raw]
            best_params = entry.get("params", entry)
        else:
            # 尝试从单独结果文件读
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
                # 用模板默认参数（中间值）
                best_params = _get_default_params(template_key)
            else:
                print(f"  ⚠️ {symbol_raw} 无参数，跳过（用 --use-defaults 可用默认值）")
                continue

        if not best_params:
            print(f"  ⚠️ {symbol_raw} 参数为空，跳过")
            continue

        tasks.append((
            symbol_raw, template_key, best_params,
            timeframe, start_str, end_str, args.score,
            args.splits, args.train_ratio, args.api,
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
        os.environ["PYTHONUNBUFFERED"] = "1"
        with multiprocessing.Pool(processes=args.jobs) as pool:
            results = pool.map(_worker_run, tasks)

    elapsed = time.time() - t0

    # ── 汇总 ──
    ok_results = [r for r in results if r["status"] == "ok"]
    err_results = [r for r in results if r["status"] != "ok"]

    print(f"\n{'='*60}")
    print(f"  📊 Walk-Forward 验证结果")
    print(f"  耗时: {elapsed:.1f}s")
    print(f"  成功: {len(ok_results)}, 失败: {len(err_results)}")
    print(f"{'='*60}")

    if not ok_results:
        _save_results(output_dir, results, args)
        return

    # 排序
    ok_results.sort(key=lambda r: r["wf_result"]["avg_test_score"], reverse=True)

    # 统计
    import numpy as np
    test_scores = [r["wf_result"]["avg_test_score"] for r in ok_results]
    overfit_ratios = [r["wf_result"]["overfitting_ratio"] for r in ok_results]
    consistencies = [r["wf_result"]["consistency"] for r in ok_results]

    print(f"\n  测试得分: 均值={np.mean(test_scores):.3f}, 中位={np.median(test_scores):.3f}")
    print(f"  过拟合比: 均值={np.mean(overfit_ratios):.3f}, 中位={np.median(overfit_ratios):.3f}")
    print(f"  一致性:   均值={np.mean(consistencies):.3f}, 中位={np.median(consistencies):.3f}")

    # 判定分布
    from collections import Counter
    verdicts = [r["wf_result"]["verdict"] for r in ok_results]
    print(f"\n  判定分布:")
    for v, cnt in Counter(verdicts).most_common():
        print(f"    {v}: {cnt} ({cnt/len(ok_results)*100:.1f}%)")

    # 通过率
    passed = sum(1 for r in ok_results
                 if r["wf_result"]["overfitting_ratio"] < 0.5
                 and r["wf_result"]["avg_test_score"] > 0)
    print(f"\n  ✅ 通过 (overfit<0.5 & test>0): {passed}/{len(ok_results)} ({passed/len(ok_results)*100:.1f}%)")

    strict = sum(1 for r in ok_results
                 if r["wf_result"]["overfitting_ratio"] < 0.3
                 and r["wf_result"]["avg_test_score"] > 0
                 and r["wf_result"]["consistency"] > 0.5)
    print(f"  ✅ 严格通过: {strict}/{len(ok_results)} ({strict/len(ok_results)*100:.1f}%)")

    # Top 20
    print(f"\n  Top 20:")
    print(f"  {'#':<4} {'股票':<18} {'模板':<25} {'Train':<8} {'Test':<8} {'Overfit':<8} {'Consist':<8} {'判定'}")
    print(f"  {'-'*105}")
    for i, r in enumerate(ok_results[:20], 1):
        wf = r["wf_result"]
        print(f"  {i:<4} {r['symbol']:<18} {r['template']:<25} "
              f"{wf['avg_train_score']:<8.3f} {wf['avg_test_score']:<8.3f} "
              f"{wf['overfitting_ratio']:<8.3f} {wf['consistency']:<8.3f} "
              f"{wf['verdict']}")

    # 保存
    _save_results(output_dir, results, args)

    # 通过列表
    passed_list = [r for r in ok_results
                   if r["wf_result"]["overfitting_ratio"] < 0.5
                   and r["wf_result"]["avg_test_score"] > 0]
    if passed_list:
        passed_path = os.path.join(output_dir, "wf_api_passed.json")
        with open(passed_path, "w", encoding="utf-8") as f:
            json.dump([{
                "symbol": r["symbol"],
                "template": r["template"],
                "wf_test_score": r["wf_result"]["avg_test_score"],
                "overfitting_ratio": r["wf_result"]["overfitting_ratio"],
                "consistency": r["wf_result"]["consistency"],
            } for r in passed_list], f, ensure_ascii=False, indent=2)
        print(f"\n  📄 通过列表: {passed_path}")


def _get_default_params(template_key: str) -> dict:
    """获取模板的默认参数（取参数范围中间值）"""
    defaults = {
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
    return defaults.get(template_key, {})


def _save_results(output_dir, results, args):
    """保存结果"""
    os.makedirs(output_dir, exist_ok=True)

    # JSON
    json_path = os.path.join(output_dir, "wf_api_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "config": {
                "splits": args.splits,
                "train_ratio": args.train_ratio,
                "score": args.score,
                "api": args.api,
            },
            "total": len(results),
            "results": results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  📄 完整结果: {json_path}")

    # 报告
    report_path = os.path.join(output_dir, "wf_api_summary.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"Walk-Forward API 验证报告\n")
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
