"""
plugin_api.py — 本地行情存储 API（路由层）

改造后:
  - 不再自己 fetch 数据（yfinance 轮子已移除）
  - POST /fetch → 调用上游 GET /api/global-market/overview + /sentiment
  - 格式转换后写入 MarketStore（纯存储层）

集成方式:
  将本文件注册为 Flask Blueprint，url_prefix="/api/market-local"

API 端点:
  GET  /api/market-local/overview     — 最新市场概览 + 评分
  GET  /api/market-local/query        — 按条件查询历史数据
  GET  /api/market-local/score        — 市场评分 (CFGI/MHS/Vol)
  GET  /api/market-local/sentiment    — 恐贪 + VIX + DXY 最新值
  GET  /api/market-local/symbol/<s>   — 单标的历史走势
  GET  /api/market-local/anomalies    — 近期急剧变化记录
  GET  /api/market-local/stats        — 存储统计信息
  POST /api/market-local/fetch        — 从上游 global-market 拉取并写入
  POST /api/market-local/prune        — 手动清理过期数据
"""

from __future__ import annotations

import os
import sys
import json
import traceback
import time
import requests as http_requests
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, jsonify, request

# ---------------------------------------------------------------------------
# 导入 MarketStore（纯存储层）
# ---------------------------------------------------------------------------

_PLUGIN_DIR = Path(__file__).parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

try:
    from market_store import MarketStore, RETENTION_DAYS, COLUMNS
    from market_scorer import MarketScorer
    _HAS_DEPS = True
except ImportError as _e:
    _HAS_DEPS = False
    _IMPORT_ERROR = str(_e)

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

try:
    from app.utils.logger import get_logger
    log = get_logger("market_local")
except ImportError:
    import logging
    log = logging.getLogger("market_local")
    if not log.handlers:
        logging.basicConfig(level=logging.INFO)
        log.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

market_local_bp = Blueprint("market_local", __name__)

# ---------------------------------------------------------------------------
# 认证装饰器
# ---------------------------------------------------------------------------

try:
    from app.utils.auth import login_required
except ImportError:
    def login_required(f):
        return f

# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_store = None

def _get_store() -> "MarketStore":
    global _store
    if _store is None:
        data_dir = os.getenv("FEATHER_DATA_DIR", "./data/feather")
        _store = MarketStore(data_dir=data_dir)
    return _store


def _check_deps():
    if not _HAS_DEPS:
        return False, f"market_store not available: {_IMPORT_ERROR}"
    return True, None


# ===================================================================
# 上游数据获取 & 格式转换
# ===================================================================

# 同进程调用（不走 HTTP）: 直接 import global_market 的 data_providers
_USE_INPROC = os.getenv("MARKET_STORE_INPROC", "1") == "1"

def _get_base_url() -> str:
    """返回本应用的 base URL（用于 HTTP 回调模式）。"""
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = os.getenv("FLASK_PORT", "5000")
    scheme = os.getenv("FLASK_SCHEME", "http")
    return f"{scheme}://{host}:{port}"


# ---- 方式 A: 同进程直调 data_providers（更快，无 HTTP 开销）----

def _fetch_overview_inproc() -> Dict[str, Any]:
    """直接调用 data_providers 获取 overview 数据。"""
    from app.data_providers.crypto import fetch_crypto_prices
    from app.data_providers.forex import fetch_forex_pairs
    from app.data_providers.commodities import fetch_commodities
    from app.data_providers.indices import fetch_stock_indices

    result = {"indices": [], "forex": [], "crypto": [], "commodities": []}

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(fetch_stock_indices): "indices",
            pool.submit(fetch_forex_pairs):   "forex",
            pool.submit(fetch_crypto_prices): "crypto",
            pool.submit(fetch_commodities):    "commodities",
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                data = fut.result()
                result[key] = data if data else []
            except Exception as e:
                log.error("inproc fetch %s failed: %s", key, e)
                result[key] = []

    return result


def _fetch_sentiment_inproc() -> Dict[str, Any]:
    """直接调用 data_providers 获取 sentiment 数据。"""
    from app.data_providers.sentiment import (
        fetch_fear_greed_index, fetch_vix, fetch_dollar_index,
    )

    results = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(fetch_fear_greed_index): "fear_greed",
            pool.submit(fetch_vix):              "vix",
            pool.submit(fetch_dollar_index):      "dxy",
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                results[key] = fut.result()
            except Exception as e:
                log.error("inproc fetch %s failed: %s", key, e)
                results[key] = None

    return {
        "fear_greed": results.get("fear_greed") or {"value": 50, "classification": "Neutral"},
        "vix":        results.get("vix") or {"value": 0, "level": "unknown"},
        "dxy":        results.get("dxy") or {"value": 0, "level": "unknown"},
    }


# ---- 方式 B: HTTP 回调（解耦，跨服务场景）----

def _fetch_overview_http() -> Dict[str, Any]:
    """通过 HTTP 调用 /api/global-market/overview。"""
    base = _get_base_url()
    try:
        resp = http_requests.get(f"{base}/api/global-market/overview", timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 1:
            return data.get("data", {})
        else:
            log.error("overview API error: %s", data.get("msg"))
            return {}
    except Exception as e:
        log.error("HTTP fetch overview failed: %s", e)
        return {}


def _fetch_sentiment_http() -> Dict[str, Any]:
    """通过 HTTP 调用 /api/global-market/sentiment。"""
    base = _get_base_url()
    try:
        resp = http_requests.get(f"{base}/api/global-market/sentiment", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 1:
            return data.get("data", {})
        else:
            log.error("sentiment API error: %s", data.get("msg"))
            return {}
    except Exception as e:
        log.error("HTTP fetch sentiment failed: %s", e)
        return {}


# ===================================================================
# 格式转换: global-market 响应 → MarketStore DataFrame
# ===================================================================

def _convert_indices(items: List[Dict]) -> List[Dict[str, Any]]:
    """global-market indices → store schema"""
    rows = []
    for it in items:
        rows.append({
            "category":   "indices",
            "symbol":     it.get("symbol", ""),
            "name":       it.get("name_cn", it.get("name", "")),
            "name_en":    it.get("name_en", ""),
            "price":      float(it.get("price", 0)),
            "change_pct": float(it.get("change", it.get("change_pct", 0))),
            "extra":      "",
        })
    return rows


def _convert_crypto(items: List[Dict]) -> List[Dict[str, Any]]:
    rows = []
    for it in items:
        extra = {}
        if it.get("volume_24h"):
            extra["volume"] = it["volume_24h"]
        if it.get("market_cap"):
            extra["market_cap"] = it["market_cap"]
        rows.append({
            "category":   "crypto",
            "symbol":     it.get("symbol", ""),
            "name":       it.get("name", it.get("name_cn", "")),
            "name_en":    it.get("name", it.get("name_en", "")),
            "price":      float(it.get("price", 0)),
            "change_pct": float(it.get("change_24h", it.get("change", it.get("change_pct", 0)))),
            "extra":      json.dumps(extra) if extra else "",
        })
    return rows


def _convert_forex(items: List[Dict]) -> List[Dict[str, Any]]:
    rows = []
    for it in items:
        rows.append({
            "category":   "forex",
            "symbol":     it.get("symbol", ""),
            "name":       it.get("name_cn", it.get("name", "")),
            "name_en":    it.get("name_en", it.get("name", "")),
            "price":      float(it.get("price", 0)),
            "change_pct": float(it.get("change", it.get("change_pct", 0))),
            "extra":      "",
        })
    return rows


def _convert_commodities(items: List[Dict]) -> List[Dict[str, Any]]:
    rows = []
    for it in items:
        extra = {}
        if it.get("unit"):
            extra["unit"] = it["unit"]
        rows.append({
            "category":   "commodities",
            "symbol":     it.get("symbol", ""),
            "name":       it.get("name_cn", it.get("name", "")),
            "name_en":    it.get("name_en", ""),
            "price":      float(it.get("price", 0)),
            "change_pct": float(it.get("change", it.get("change_pct", 0))),
            "extra":      json.dumps(extra) if extra else "",
        })
    return rows


def _convert_sentiment(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 global-market /sentiment 响应构建 store 格式。"""
    import pandas as pd
    ts = pd.Timestamp.now()
    rows = []

    # Fear & Greed
    fg = data.get("fear_greed", {})
    if fg.get("value") is not None:
        rows.append({
            "category":   "sentiment",
            "symbol":     "fear_greed",
            "name":       "恐贪指数",
            "name_en":    "Fear & Greed Index",
            "price":      float(fg["value"]),
            "change_pct": 0,
            "extra":      json.dumps({
                "classification": fg.get("classification", "Neutral"),
                "source": fg.get("source", "alternative.me"),
            }),
        })

    # VIX
    vix = data.get("vix", {})
    if vix.get("value") is not None:
        rows.append({
            "category":   "sentiment",
            "symbol":     "VIX",
            "name":       "恐慌指数VIX",
            "name_en":    "CBOE VIX",
            "price":      float(vix["value"]),
            "change_pct": float(vix.get("change", 0)),
            "extra":      json.dumps({"level": vix.get("level", "unknown")}),
        })

    # DXY
    dxy = data.get("dxy", {})
    if dxy.get("value") is not None:
        rows.append({
            "category":   "sentiment",
            "symbol":     "DXY",
            "name":       "美元指数",
            "name_en":    "US Dollar Index",
            "price":      float(dxy["value"]),
            "change_pct": float(dxy.get("change", 0)),
            "extra":      json.dumps({"level": dxy.get("level", "unknown")}),
        })

    return rows


def _collect_all(overview: Dict, sentiment: Dict) -> "pd.DataFrame":
    """合并 overview + sentiment → 统一 DataFrame，可直接传入 store.append()。"""
    import pandas as pd
    from market_store import COLUMNS

    all_rows = []
    ts = pd.Timestamp.now()

    all_rows.extend(_convert_indices(overview.get("indices", [])))
    all_rows.extend(_convert_crypto(overview.get("crypto", [])))
    all_rows.extend(_convert_forex(overview.get("forex", [])))
    all_rows.extend(_convert_commodities(overview.get("commodities", [])))
    all_rows.extend(_convert_sentiment(sentiment))

    if not all_rows:
        return pd.DataFrame(columns=COLUMNS)

    df = pd.DataFrame(all_rows, columns=COLUMNS)
    df["timestamp"] = ts
    return df


# ===================================================================
# API 端点
# ===================================================================

@market_local_bp.route("/overview", methods=["GET"])
@login_required
def api_overview():
    """GET /api/market-local/overview — 最新市场数据 + 综合评分。"""
    ok, err = _check_deps()
    if not ok:
        return jsonify({"code": 0, "msg": err, "data": None}), 500

    try:
        store = _get_store()
        hours = float(request.args.get("hours", 6))

        df = store.query(hours=hours)
        if df.empty:
            return jsonify({
                "code": 1,
                "msg": "no data yet — trigger /fetch first",
                "data": {"rows": 0, "score": None, "data": {}},
            })

        scorer = MarketScorer(df)
        report = scorer.report()

        latest = {}
        for cat in ["indices", "crypto", "forex", "commodities", "sentiment"]:
            sub = df[df["category"] == cat]
            if not sub.empty:
                sub = sub.sort_values("timestamp").groupby("symbol").last().reset_index()
                latest[cat] = json.loads(sub.to_json(orient="records", date_format="iso"))

        return jsonify({
            "code": 1, "msg": "success",
            "data": {"score": report, "data": latest, "rows": len(df), "hours": hours},
        })
    except Exception as e:
        log.error("overview failed: %s\n%s", e, traceback.format_exc())
        return jsonify({"code": 0, "msg": str(e), "data": None}), 500


@market_local_bp.route("/query", methods=["GET"])
@login_required
def api_query():
    """GET /api/market-local/query — 条件查询历史数据。"""
    ok, err = _check_deps()
    if not ok:
        return jsonify({"code": 0, "msg": err, "data": None}), 500

    try:
        store = _get_store()
        limit = int(request.args.get("limit", 500))

        df = store.query(
            start=request.args.get("start"),
            end=request.args.get("end"),
            category=request.args.get("category"),
            symbol=request.args.get("symbol"),
            hours=request.args.get("hours", type=float),
        )

        if df.empty:
            return jsonify({"code": 1, "msg": "no data", "data": []})

        if len(df) > limit:
            df = df.tail(limit)

        records = json.loads(df.to_json(orient="records", date_format="iso"))
        return jsonify({"code": 1, "msg": "success", "data": records, "total": len(records)})
    except Exception as e:
        log.error("query failed: %s", e)
        return jsonify({"code": 0, "msg": str(e), "data": None}), 500


@market_local_bp.route("/score", methods=["GET"])
@login_required
def api_score():
    """GET /api/market-local/score — 市场评分。"""
    ok, err = _check_deps()
    if not ok:
        return jsonify({"code": 0, "msg": err, "data": None}), 500

    try:
        store = _get_store()
        hours = float(request.args.get("hours", 6))

        df = store.query(hours=hours)
        if df.empty:
            return jsonify({"code": 0, "msg": "no data available", "data": None}), 404

        scorer = MarketScorer(df)
        report = scorer.report()
        return jsonify({"code": 1, "msg": "success", "data": report})
    except Exception as e:
        log.error("score failed: %s", e)
        return jsonify({"code": 0, "msg": str(e), "data": None}), 500


@market_local_bp.route("/sentiment", methods=["GET"])
@login_required
def api_sentiment():
    """GET /api/market-local/sentiment — 恐贪 + VIX + DXY。"""
    ok, err = _check_deps()
    if not ok:
        return jsonify({"code": 0, "msg": err, "data": None}), 500

    try:
        store = _get_store()
        df = store.query(category="sentiment", hours=24)
        if df.empty:
            return jsonify({"code": 0, "msg": "no sentiment data", "data": None}), 404

        latest = df.sort_values("timestamp").groupby("symbol").last().reset_index()

        result = {}
        for _, row in latest.iterrows():
            sym = row["symbol"]
            extra = {}
            if row.get("extra"):
                try:
                    extra = json.loads(row["extra"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result[sym] = {
                "value":      row["price"],
                "change_pct": row["change_pct"],
                "name":       row["name"],
                "name_en":    row["name_en"],
                "updated_at": row["timestamp"].isoformat() if hasattr(row["timestamp"], "isoformat") else str(row["timestamp"]),
                **extra,
            }

        scorer = MarketScorer(df)
        cfgi = scorer.cfgi()
        result["cfgi"] = {
            "score":      cfgi["score"],
            "label":      cfgi["label"],
            "emoji":      cfgi["emoji"],
            "components": cfgi["components"],
        }

        return jsonify({"code": 1, "msg": "success", "data": result})
    except Exception as e:
        log.error("sentiment failed: %s", e)
        return jsonify({"code": 0, "msg": str(e), "data": None}), 500


@market_local_bp.route("/symbol/<path:symbol>", methods=["GET"])
@login_required
def api_symbol(symbol: str):
    """GET /api/market-local/symbol/BTC — 单标的历史走势。"""
    ok, err = _check_deps()
    if not ok:
        return jsonify({"code": 0, "msg": err, "data": None}), 500

    try:
        store = _get_store()
        hours = float(request.args.get("hours", 168))
        limit = int(request.args.get("limit", 200))
        category = request.args.get("category")

        df = store.query(category=category, symbol=symbol, hours=hours)
        if df.empty:
            return jsonify({"code": 0, "msg": f"no data for {symbol}", "data": None}), 404

        if len(df) > limit:
            df = df.tail(limit)

        records = json.loads(df.to_json(orient="records", date_format="iso"))
        stats = {
            "symbol": symbol, "count": len(df),
            "price_min":  round(float(df["price"].min()), 6),
            "price_max":  round(float(df["price"].max()), 6),
            "price_mean": round(float(df["price"].mean()), 6),
            "price_std":  round(float(df["price"].std()), 6),
            "first_ts":   df["timestamp"].min().isoformat(),
            "last_ts":    df["timestamp"].max().isoformat(),
        }

        return jsonify({"code": 1, "msg": "success", "data": records, "stats": stats})
    except Exception as e:
        log.error("symbol query failed: %s", e)
        return jsonify({"code": 0, "msg": str(e), "data": None}), 500


@market_local_bp.route("/anomalies", methods=["GET"])
@login_required
def api_anomalies():
    """GET /api/market-local/anomalies — 急剧变化检测。"""
    ok, err = _check_deps()
    if not ok:
        return jsonify({"code": 0, "msg": err, "data": None}), 500

    try:
        store = _get_store()
        hours = float(request.args.get("hours", 6))

        df = store.query(hours=hours)
        if df.empty:
            return jsonify({"code": 1, "msg": "no data", "data": []})

        alerts = store.detect_anomalies(df)
        return jsonify({"code": 1, "msg": "success", "data": alerts, "count": len(alerts)})
    except Exception as e:
        log.error("anomalies failed: %s", e)
        return jsonify({"code": 0, "msg": str(e), "data": None}), 500


@market_local_bp.route("/stats", methods=["GET"])
@login_required
def api_stats():
    """GET /api/market-local/stats — 存储统计。"""
    ok, err = _check_deps()
    if not ok:
        return jsonify({"code": 0, "msg": err, "data": None}), 500

    try:
        store = _get_store()
        s = store.stats()
        return jsonify({"code": 1, "msg": "success", "data": s})
    except Exception as e:
        log.error("stats failed: %s", e)
        return jsonify({"code": 0, "msg": str(e), "data": None}), 500


@market_local_bp.route("/fetch", methods=["POST"])
@login_required
def api_fetch():
    """
    POST /api/market-local/fetch
    从上游 global-market 拉取数据（overview + sentiment），格式转换后写入本地存储。

    数据流:
      global-market/overview   → _convert_* → store.append()
      global-market/sentiment  → _convert_sentiment ↗
    """
    ok, err = _check_deps()
    if not ok:
        return jsonify({"code": 0, "msg": err, "data": None}), 500

    try:
        store = _get_store()
        log.info("manual fetch triggered via API (source: %s)",
                 "inproc" if _USE_INPROC else "http")

        # 1. 从上游获取数据
        if _USE_INPROC:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_ov  = pool.submit(_fetch_overview_inproc)
                f_st  = pool.submit(_fetch_sentiment_inproc)
                overview  = f_ov.result()
                sentiment = f_st.result()
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_ov  = pool.submit(_fetch_overview_http)
                f_st  = pool.submit(_fetch_sentiment_http)
                overview  = f_ov.result()
                sentiment = f_st.result()

        if not overview and not sentiment:
            return jsonify({"code": 0, "msg": "upstream returned no data", "data": None}), 502

        # 2. 格式转换
        df = _collect_all(overview, sentiment)
        if df.empty:
            return jsonify({"code": 0, "msg": "converted 0 rows from upstream", "data": None}), 502

        # 3. 写入存储
        store.append(df)

        # 4. 生成评分
        recent = store.query(hours=6)
        scorer = MarketScorer(recent) if not recent.empty else None
        report = scorer.report() if scorer else None

        return jsonify({
            "code": 1, "msg": "success",
            "data": {
                "fetched_rows": len(df),
                "categories": df["category"].value_counts().to_dict(),
                "source": "inproc" if _USE_INPROC else "http",
                "score": report,
            },
        })
    except Exception as e:
        log.error("fetch failed: %s\n%s", e, traceback.format_exc())
        return jsonify({"code": 0, "msg": str(e), "data": None}), 500


@market_local_bp.route("/prune", methods=["POST"])
@login_required
def api_prune():
    """POST /api/market-local/prune — 手动清理过期数据。"""
    ok, err = _check_deps()
    if not ok:
        return jsonify({"code": 0, "msg": err, "data": None}), 500

    try:
        store = _get_store()
        deleted = store.prune()
        return jsonify({"code": 1, "msg": "success", "data": {"deleted_files": deleted}})
    except Exception as e:
        log.error("prune failed: %s", e)
        return jsonify({"code": 0, "msg": str(e), "data": None}), 500
