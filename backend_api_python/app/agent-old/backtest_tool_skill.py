#!/usr/bin/env python3
"""
Tool/Skill 历史回测引擎

用历史行情数据批量验证 tool/skill 的预测准确率。
独立 CLI 程序，不侵入 agent。

用法：
  # 回测单个 tool
  python -m app.agent.tools.backtest_tool_skill --tool analyze_trend --stock-pool all --days 90

  # 回测多个 tool
  python -m app.agent.tools.backtest_tool_skill --tool analyze_trend,get_indicator_snapshot --stock-pool all --days 90

  # 指定板块
  python -m app.agent.tools.backtest_tool_skill --tool analyze_trend --stock-pool 科创 --days 90

  # 指定周期
  python -m app.agent.tools.backtest_tool_skill --tool analyze_trend --stock-pool all --days 90 --periods T+1,T+3,T+5

  # 回测 skill
  python -m app.agent.tools.backtest_tool_skill --skill technical_agent --stock-pool all --days 90

  # 使用 15m 线
  python -m app.agent.tools.backtest_tool_skill --tool analyze_trend --stock-pool all --days 60 --timeframe 15m

  # 输出详细结果
  python -m app.agent.tools.backtest_tool_skill --tool analyze_trend --stock-pool all --days 90 --verbose

  # 指定输出目录
  python -m app.agent.tools.backtest_tool_skill --tool analyze_trend --stock-pool all --days 90 --output-dir ./my_results

  # 快速测试（随机抽样）
  python -m app.agent.tools.backtest_tool_skill --tool get_indicator_snapshot --sample 100 --days 30

  # 中断: Ctrl+C 可随时安全退出

输出文件说明：
  - 汇总文件 (--output): 包含所有 tool/skill 的统计摘要
  - 高胜率股票文件 (--output-dir): 每个 tool/skill × 周期独立文件
    - 文件名格式: {tool_skill名}_{周期}.json
    - 内容: 该组合下出现>=3次且100%胜率的股票列表
    - 用途: 针对性优化，找到特定 tool 在特定周期下的高胜率股票
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── 加载 .env ──
try:
    from dotenv import load_dotenv
    _this_dir = os.path.dirname(os.path.abspath(__file__))
    _backend_dir = os.path.dirname(os.path.dirname(_this_dir))
    load_dotenv(os.path.join(_backend_dir, ".env"), override=False)
    load_dotenv(os.path.join(os.path.dirname(_backend_dir), ".env"), override=False)
except Exception:
    pass

from app.utils.logger import get_logger

logger = get_logger(__name__)

# K 线内存缓存 key=(stock_code, timeframe) → 前复权后的完整 K 线列表
# 跨 backtest_single 调用复用，大幅减少 DB 查询
_kline_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

# 日期索引 key=(stock_code, timeframe) → {date_str: index_in_kline_list}
# 用于 O(1) 查找决策日在 K 线列表中的位置
_kline_date_index: Dict[Tuple[str, str], Dict[str, int]] = {}

# 批量预加载的 K 线数据 key=(stock_code) → {"1D": [...], "15m": [...]}
_bulk_kline_cache: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

# ═══════════════════════════════════════════════════════════════
#  数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class PredictionResult:
    """单次预测结果。"""
    stock_code: str
    stock_name: str
    decision_date: str  # YYYY-MM-DD
    tool_name: str
    score: Optional[float] = None  # 0-100
    direction: str = ""  # bullish / bearish / neutral
    action: str = ""  # buy / sell / hold / skip
    confidence: float = 0.0
    raw_output: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class VerifyResult:
    """验证结果。"""
    prediction: PredictionResult
    period: str  # T+1 / T+3 / T+5 / T+10 / T+20
    actual_return_pct: float = 0.0  # 实际涨跌幅 %
    actual_direction: str = ""  # bullish / bearish / neutral
    correct: Optional[bool] = None  # 方向是否正确
    pnl_pct: float = 0.0  # 盈亏幅度（买入信号用正数，卖出信号用负数）


@dataclass
class ToolSkillStats:
    """tool/skill 统计结果。"""
    name: str
    type: str  # tool / skill
    period: str
    total_samples: int = 0
    valid_samples: int = 0  # 有预测结果的样本
    correct_count: int = 0
    wrong_count: int = 0
    neutral_count: int = 0
    win_rate: float = 0.0
    weighted_accuracy: float = 0.0  # 置信度加权准确率（score越极端权重越高）
    avg_pnl_pct: float = 0.0
    avg_hold_days: float = 0.0
    return_per_day: float = 0.0
    avg_confidence: float = 0.0
    error_count: int = 0
    # 高胜率股票记录
    stock_stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # {"code": {count, correct, wrong}}


# ═══════════════════════════════════════════════════════════════
#  股票池
# ═══════════════════════════════════════════════════════════════

def get_stock_pool(pool_type: str = "all", sector: str = "") -> List[Dict[str, str]]:
    """获取股票池（从 CNStock_db.stock_basic_info）。

    Args:
        pool_type: all / 沪深 / 创业板 / 科创 / 北证
        sector: 板块名称（概念/行业）

    Returns:
        [{"code": "600519", "name": "贵州茅台"}, ...]
    """
    from app.utils.db_market import get_market_db_manager

    mgr = get_market_db_manager()
    pool = mgr._get_pool("CNStock")

    conditions = ["symbol IS NOT NULL", "name IS NOT NULL", "status = 'active'"]
    params = []

    if pool_type == "沪深":
        conditions.append("symbol SIMILAR TO '(60|00)%'")
    elif pool_type == "创业板":
        conditions.append("symbol LIKE '30%'")
    elif pool_type == "科创板":
        conditions.append("symbol LIKE '68%'")
    elif pool_type == "北证":
        conditions.append("symbol LIKE '8%'")

    where = " AND ".join(conditions)
    with pool.connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT symbol, name FROM stock_basic_info WHERE {where} ORDER BY symbol", params)
        rows = cur.fetchall()

    stocks = [{"code": r[0], "name": r[1]} for r in rows]

    # 板块筛选
    if sector:
        # 从概念/行业字段筛选
        filtered = []
        for s in stocks:
            # 这里需要根据实际数据库结构调整
            # 假设 stock_basic_info 表有 concepts 字段
            pass
        if filtered:
            stocks = filtered

    return stocks


def get_random_stocks(count: int = 50, seed: int = 42) -> List[Dict[str, str]]:
    """随机选取 N 只股票（用于快速测试）。"""
    import random
    random.seed(seed)
    all_stocks = get_stock_pool("all")
    return random.sample(all_stocks, min(count, len(all_stocks)))


# ═══════════════════════════════════════════════════════════════
#  行情数据（从 CNStock_db 读取）
# ═══════════════════════════════════════════════════════════════

_market_writer = None

def _get_market_writer():
    """延迟初始化 MarketKlineWriter。"""
    global _market_writer
    if _market_writer is None:
        from app.utils.db_market import get_market_kline_writer
        _market_writer = get_market_kline_writer()
    return _market_writer


def get_kline_from_db(
    stock_code: str,
    timeframe: str = "1D",
    start_date: str = "",
    end_date: str = "",
    adjust: bool = True,
) -> List[Dict[str, Any]]:
    """从 CNStock_db 获取 K 线数据（默认前复权）。

    Args:
        stock_code: 股票代码
        timeframe: 1D / 15m
        start_date: 开始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD
        adjust: 是否前复权（默认 True）

    Returns:
        [{"date": "2025-01-01", "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1000000}, ...]
    """
    from datetime import datetime

    writer = _get_market_writer()

    # 转换日期参数
    start_dt = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
    end_dt = datetime.strptime(end_date, "%Y-%m-%d") if end_date else None

    # 从 CNStock_db 查询
    rows = writer.query(
        market="CNStock",
        symbol=stock_code,
        timeframe=timeframe,
        start_time=start_dt,
        end_time=end_dt,
        limit=10000,
    )

    if not rows:
        return []

    # 转换格式
    klines = []
    for r in rows:
        t = r.get("time")
        if isinstance(t, datetime):
            date_str = t.strftime("%Y-%m-%d") if timeframe == "1D" else t.strftime("%Y-%m-%d %H:%M")
        else:
            date_str = str(t)[:16]
        klines.append({
            "time": date_str,
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": int(r.get("volume", 0)),
        })

    # 前复权处理
    if adjust and klines:
        from app.data_sources.provider.adjustment import unadj_to_qfq
        klines = unadj_to_qfq(klines, stock_code)

    # 返回标准格式
    return [
        {
            "date": k["time"],
            "open": k["open"],
            "high": k["high"],
            "low": k["low"],
            "close": k["close"],
            "volume": k["volume"],
        }
        for k in klines
    ]


def _get_kline_cached(
    stock_code: str,
    timeframe: str = "1D",
    start_date: str = "",
    end_date: str = "",
    adjust: bool = True,
) -> List[Dict[str, Any]]:
    """带进程级缓存的 K 线查询，避免同一股票跨决策日重复查 DB。

    缓存策略: key=(stock_code, timeframe)，保留最早起始日期的数据。
    当请求的 start_date 落在缓存范围内时，直接从内存二分查找返回。
    同时构建日期索引用于 O(1) 查找。
    """
    global _kline_cache, _kline_date_index
    cache_key = (stock_code, timeframe)

    if cache_key in _kline_cache:
        cached = _kline_cache[cache_key]
        if cached and (not start_date or cached[0]["date"][:10] <= start_date[:10]):
            result = cached
            if start_date:
                # 二分查找第一个 >= start_date 的位置
                lo, hi = 0, len(cached)
                while lo < hi:
                    mid = (lo + hi) // 2
                    if cached[mid]["date"][:10] < start_date[:10]:
                        lo = mid + 1
                    else:
                        hi = mid
                result = cached[lo:]
            if end_date and result:
                # 二分查找第一个 > end_date 的位置
                lo, hi = 0, len(result)
                while lo < hi:
                    mid = (lo + hi) // 2
                    if result[mid]["date"][:10] <= end_date[:10]:
                        lo = mid + 1
                    else:
                        hi = mid
                result = result[:lo]
            if result:
                # 确保日期索引已构建
                if cache_key not in _kline_date_index:
                    _build_date_index(cache_key, cached)
                return result

    # 缓存未命中 → 查 DB
    klines = get_kline_from_db(stock_code, timeframe, start_date, end_date, adjust)
    if not klines:
        return []

    # 保留范围更广的数据（起始日期更早）进缓存
    if cache_key not in _kline_cache:
        _kline_cache[cache_key] = klines
        _build_date_index(cache_key, klines)
    elif klines and klines[0]["date"] < _kline_cache[cache_key][0]["date"]:
        _kline_cache[cache_key] = klines
        _build_date_index(cache_key, klines)

    return klines


def _build_date_index(cache_key: Tuple[str, str], klines: List[Dict[str, Any]]):
    """为 K 线列表构建日期索引 {date_str: index}，用于 O(1) 查找。"""
    global _kline_date_index
    index = {}
    for i, k in enumerate(klines):
        d = k["date"][:10]
        if d not in index:
            index[d] = i
    _kline_date_index[cache_key] = index


def preload_bulk_klines(
    stock_codes: List[str],
    timeframe: str = "1D",
    start_date: str = "",
    end_date: str = "",
    batch_size: int = 50,
) -> int:
    """批量预加载 K 线数据到缓存。

    优化策略：
    1. 直接 SQL 批量查询，避免逐只股票调用 API
    2. 复用 DB 连接，减少连接开销
    3. 前复权处理在内存中完成

    Args:
        stock_codes: 股票代码列表
        timeframe: K 线周期
        start_date: 开始日期
        end_date: 结束日期
        batch_size: 批量查询大小（未使用，保留兼容）

    Returns:
        成功加载的股票数量
    """
    global _kline_cache
    loaded = 0
    total = len(stock_codes)

    # 预加载复权因子缓存（避免回测时触发网络请求）
    print("  预加载复权因子缓存...")
    try:
        from app.data_sources.provider.adjustment import _load as load_factors
        load_factors()
        print("    复权因子缓存已加载")
    except Exception as e:
        logger.warning("加载复权因子缓存失败: %s", e)

    print(f"  预加载 K 线数据: {total} 只股票, 周期={timeframe}")

    # 直接 SQL 批量查询
    from datetime import datetime
    from app.utils.db_market import get_market_db_manager

    mgr = get_market_db_manager()
    pool = mgr._get_pool("CNStock")

    # 解析日期
    start_dt = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None

    # 确定要查询的年份（加载最近3年，确保有足够历史数据）
    years = set()
    current_year = datetime.now().year
    if start_dt:
        for y in range(start_dt.year, current_year + 1):
            years.add(y)
    else:
        # 默认加载最近3年，确保指标计算有足够的历史数据
        for y in range(current_year - 2, current_year + 1):
            years.add(y)

    with pool.connection() as conn:
        cur = conn.cursor()

        # 检查表是否存在（缓存结果）
        tables_exist = set()
        for year in years:
            table = f"kline_{timeframe}_{year}"
            if table in tables_exist:
                continue
            cur.execute("""
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s
            """, (table,))
            if cur.fetchone():
                tables_exist.add(table)

        print(f"    可用表: {tables_exist}")

        # 批量查询每张表
        for table in sorted(tables_exist):
            # 构建 IN 条件
            placeholders = ", ".join(["%s"] * len(stock_codes))
            conditions = [f"symbol IN ({placeholders})"]
            params = list(stock_codes)

            if start_dt:
                conditions.append("time >= %s")
                params.append(start_dt)

            query = f"""
                SELECT symbol, time, open, high, low, close, volume
                FROM "{table}"
                WHERE {' AND '.join(conditions)}
                ORDER BY symbol, time ASC
            """

            cur.execute(query, params)
            rows = cur.fetchall()

            # 按股票分组
            stock_data = {}
            for row in rows:
                code = row[0]
                if code not in stock_data:
                    stock_data[code] = []
                stock_data[code].append({
                    "time": row[1],
                    "open": float(row[2]),
                    "high": float(row[3]),
                    "low": float(row[4]),
                    "close": float(row[5]),
                    "volume": float(row[6]),
                })

            # 前复权处理并存入缓存
            from app.data_sources.provider.adjustment import unadj_to_qfq
            for code, klines in stock_data.items():
                if not klines:
                    continue

                # 转换日期格式
                formatted = []
                for k in klines:
                    t = k["time"]
                    if isinstance(t, datetime):
                        date_str = t.strftime("%Y-%m-%d")
                    else:
                        date_str = str(t)[:10]
                    formatted.append({
                        "time": date_str,
                        "open": k["open"],
                        "high": k["high"],
                        "low": k["low"],
                        "close": k["close"],
                        "volume": k["volume"],
                    })

                # 前复权
                adjusted = unadj_to_qfq(formatted, code)

                # 转换为标准格式
                result = [{
                    "date": k["time"],
                    "open": k["open"],
                    "high": k["high"],
                    "low": k["low"],
                    "close": k["close"],
                    "volume": k["volume"],
                } for k in adjusted]

                # 存入缓存（合并多张表的数据）
                cache_key = (code, timeframe)
                if cache_key in _kline_cache:
                    existing = _kline_cache[cache_key]
                    # 合并并去重
                    merged = {k["date"]: k for k in existing}
                    for k in result:
                        merged[k["date"]] = k
                    _kline_cache[cache_key] = sorted(merged.values(), key=lambda x: x["date"])
                else:
                    _kline_cache[cache_key] = result
                    loaded += 1

                # 构建日期索引
                _build_date_index(cache_key, _kline_cache[cache_key])

            print(f"    表 {table}: 查询 {len(rows)} 行, 覆盖 {len(stock_data)} 只股票")

    print(f"  预加载完成: {loaded}/{total} 只股票")
    return loaded


def get_future_return(
    stock_code: str,
    from_date: str,
    hold_days: int,
    timeframe: str = "1D",
) -> Optional[Dict[str, Any]]:
    """获取未来 N 天的实际涨跌。

    Args:
        stock_code: 股票代码
        from_date: 决策日 YYYY-MM-DD
        hold_days: 持有天数
        timeframe: 1D / 15m

    Returns:
        {"pnl_pct": 2.5, "hold_days": 3, "direction": "bullish", "exit_date": "2025-01-04"}
    """
    # 获取决策日之后的 K 线
    klines = get_kline_from_db(stock_code, timeframe, from_date)
    if not klines or len(klines) < 2:
        return None

    # 找到决策日的索引
    base_idx = None
    for i, k in enumerate(klines):
        if k["date"][:10] >= from_date:
            base_idx = i
            break

    if base_idx is None:
        return None

    # 计算持有期收益
    exit_idx = min(base_idx + hold_days, len(klines) - 1)
    if exit_idx <= base_idx:
        return None

    base_close = klines[base_idx]["close"]
    exit_close = klines[exit_idx]["close"]

    if base_close <= 0:
        return None

    pnl_pct = round((exit_close - base_close) / base_close * 100, 2)
    actual_hold = exit_idx - base_idx

    # 方向分类
    if pnl_pct > 1.0:
        direction = "bullish"
    elif pnl_pct < -1.0:
        direction = "bearish"
    else:
        direction = "neutral"

    return {
        "pnl_pct": pnl_pct,
        "hold_days": actual_hold,
        "direction": direction,
        "exit_date": klines[exit_idx]["date"][:10],
    }


def _compute_return_from_klines(
    klines: List[Dict[str, Any]],
    from_date: str,
    hold_days: int,
    stock_code: str = "",
    timeframe: str = "1D",
) -> Optional[Dict[str, Any]]:
    """从已获取的 K 线数据计算未来 N 天涨跌（避免重复查询 DB）。

    使用日期索引 O(1) 查找 base_idx，避免线性扫描。
    """
    if not klines or len(klines) < 2:
        return None

    base_idx = None

    # 优先使用日期索引 O(1)
    if stock_code:
        cache_key = (stock_code, timeframe)
        idx_map = _kline_date_index.get(cache_key)
        if idx_map:
            base_idx = idx_map.get(from_date)

    # 回退到二分查找 O(log n)
    if base_idx is None:
        lo, hi = 0, len(klines)
        while lo < hi:
            mid = (lo + hi) // 2
            if klines[mid]["date"][:10] < from_date:
                lo = mid + 1
            else:
                hi = mid
        base_idx = lo

    if base_idx is None or base_idx >= len(klines):
        return None

    exit_idx = min(base_idx + hold_days, len(klines) - 1)
    if exit_idx <= base_idx:
        return None

    base_close = klines[base_idx]["close"]
    exit_close = klines[exit_idx]["close"]

    if base_close <= 0:
        return None

    pnl_pct = round((exit_close - base_close) / base_close * 100, 2)
    actual_hold = exit_idx - base_idx

    if pnl_pct > 1.0:
        direction = "bullish"
    elif pnl_pct < -1.0:
        direction = "bearish"
    else:
        direction = "neutral"

    return {
        "pnl_pct": pnl_pct,
        "hold_days": actual_hold,
        "direction": direction,
        "exit_date": klines[exit_idx]["date"][:10],
    }


# ═══════════════════════════════════════════════════════════════
#  Tool/Skill 调用
# ═══════════════════════════════════════════════════════════════

# tool 注册表（延迟导入）
_tool_registry = None

def _get_tool_fn(tool_name: str) -> Optional[Callable]:
    """获取 tool 函数。"""
    global _tool_registry
    if _tool_registry is None:
        from app.agent.tools.registry import ToolRegistry
        _tool_registry = ToolRegistry()
        _tool_registry.discover()

    spec = _tool_registry.get(tool_name)
    return spec.fn if spec else None


def call_tool(tool_name: str, stock_code: str, stock_name: str = "", decision_date: str = "") -> PredictionResult:
    """调用 tool 并提取预测结果。

    Args:
        tool_name: tool 名称
        stock_code: 股票代码
        stock_name: 股票名称
        decision_date: 决策日期（用于从缓存获取历史K线）

    Returns:
        PredictionResult
    """
    today = datetime.now().strftime("%Y-%m-%d")
    result = PredictionResult(
        stock_code=stock_code,
        stock_name=stock_name,
        decision_date=decision_date or today,
        tool_name=tool_name,
    )

    fn = _get_tool_fn(tool_name)
    if not fn:
        result.error = f"tool '{tool_name}' 不存在"
        return result

    try:
        # 注入缓存数据源到 DataSourceFactory
        from app.data_sources.factory import DataSourceFactory
        original_source = DataSourceFactory._sources.get("CNStock")

        # 创建缓存数据源
        class CachedDataSource:
            def __init__(self, cache, target_date):
                self._cache = cache
                self._target_date = target_date

            def get_kline(self, symbol, timeframe, limit, **kwargs):
                cache_key = (symbol, timeframe)
                if cache_key not in self._cache:
                    return []
                klines = self._cache[cache_key]
                if not klines:
                    return []
                # 截取到决策日期
                if self._target_date:
                    klines = [k for k in klines if k["date"] <= self._target_date]
                # 转换为数据源格式
                result = []
                for k in klines:
                    result.append({
                        "time": k["date"],
                        "open": k["open"],
                        "high": k["high"],
                        "low": k["low"],
                        "close": k["close"],
                        "volume": k["volume"],
                    })
                # 返回最后 limit 根
                if len(result) > limit:
                    result = result[-limit:]
                return result

            # 添加其他可能需要的方法
            def __getattr__(self, name):
                # 委托给原始数据源
                if original_source:
                    return getattr(original_source, name)
                raise AttributeError(f"CachedDataSource 没有属性 {name}")

        # 注入缓存数据源
        cached_source = CachedDataSource(_kline_cache, decision_date)
        DataSourceFactory._sources["CNStock"] = cached_source

        try:
            # 调用 tool
            raw = fn(stock_code)
            result.raw_output = raw
        finally:
            # 恢复原始数据源
            if original_source:
                DataSourceFactory._sources["CNStock"] = original_source
            else:
                DataSourceFactory._sources.pop("CNStock", None)

        # 提取预测结果
        if isinstance(raw, dict):
            if "error" in raw:
                result.error = raw["error"]
                return result

            # 标准化输出格式
            if "evaluation" in raw:
                ev = raw["evaluation"]
                result.score = ev.get("score")
                result.direction = ev.get("direction", "")
                result.confidence = ev.get("confidence", 0.5)
            elif "score" in raw:
                result.score = raw.get("score")
                result.direction = raw.get("direction", "")
                result.confidence = raw.get("confidence", 0.5)

            # 如果没有 direction，根据 score 推断
            if not result.direction and result.score is not None:
                if result.score >= 65:
                    result.direction = "bullish"
                elif result.score <= 35:
                    result.direction = "bearish"
                else:
                    result.direction = "neutral"

            # 如果没有 action，根据 direction 推断
            if not result.action:
                if result.direction == "bullish":
                    result.action = "buy"
                elif result.direction == "bearish":
                    result.action = "sell"
                else:
                    result.action = "hold"

            # 提取 action
            if "action" in raw:
                result.action = raw["action"]
            elif "signal" in raw:
                signal = raw["signal"]
                if "买" in str(signal) or "bullish" in str(signal).lower():
                    result.action = "buy"
                elif "卖" in str(signal) or "bearish" in str(signal).lower():
                    result.action = "sell"
                else:
                    result.action = "hold"

    except Exception as e:
        result.error = str(e)

    return result


def call_skill(skill_name: str, stock_code: str, stock_name: str = "") -> PredictionResult:
    """调用 skill 并提取预测结果。

    Args:
        skill_name: skill 名称
        stock_code: 股票代码
        stock_name: 股票名称

    Returns:
        PredictionResult
    """
    today = datetime.now().strftime("%Y-%m-%d")
    result = PredictionResult(
        stock_code=stock_code,
        stock_name=stock_name,
        decision_date=today,
        tool_name=skill_name,
    )

    try:
        # 加载 skill
        from app.agent.semantics import get_all_skill_metas
        metas = get_all_skill_metas()

        if skill_name not in metas:
            result.error = f"skill '{skill_name}' 不存在"
            return result

        meta = metas[skill_name]
        tools = meta.tools or []

        # 调用 skill 的所有工具
        all_scores = []
        all_directions = []
        raw_outputs = {}

        for tool_name in tools:
            fn = _get_tool_fn(tool_name)
            if fn:
                try:
                    raw = fn(stock_code)
                    raw_outputs[tool_name] = raw

                    if isinstance(raw, dict):
                        if "evaluation" in raw:
                            ev = raw["evaluation"]
                            if ev.get("score") is not None:
                                all_scores.append(ev["score"])
                            if ev.get("direction"):
                                all_directions.append(ev["direction"])
                        elif "score" in raw:
                            if raw.get("score") is not None:
                                all_scores.append(raw["score"])
                            if raw.get("direction"):
                                all_directions.append(raw["direction"])
                except Exception as e:
                    raw_outputs[tool_name] = {"error": str(e)}

        result.raw_output = raw_outputs

        # 综合评分
        if all_scores:
            result.score = sum(all_scores) / len(all_scores)

        # 综合方向（投票）
        if all_directions:
            bullish = sum(1 for d in all_directions if d == "bullish")
            bearish = sum(1 for d in all_directions if d == "bearish")
            if bullish > bearish:
                result.direction = "bullish"
            elif bearish > bullish:
                result.direction = "bearish"
            else:
                result.direction = "neutral"

        # 推断 action
        if result.direction == "bullish":
            result.action = "buy"
        elif result.direction == "bearish":
            result.action = "sell"
        else:
            result.action = "hold"

    except Exception as e:
        result.error = str(e)

    return result


# ═══════════════════════════════════════════════════════════════
#  回测引擎
# ═══════════════════════════════════════════════════════════════

def backtest_single(
    stock_code: str,
    stock_name: str,
    decision_date: str,
    tool_name: str,
    periods: List[str],
    is_skill: bool = False,
) -> List[VerifyResult]:
    """对单只股票单个日期做回测。

    Args:
        stock_code: 股票代码
        stock_name: 股票名称
        decision_date: 决策日 YYYY-MM-DD
        tool_name: tool/skill 名称
        periods: 验证周期列表 ["T+1", "T+3", "T+5"]
        is_skill: 是否为 skill

    Returns:
        [VerifyResult, ...]
    """
    results = []

    # 调用 tool/skill
    if is_skill:
        pred = call_skill(tool_name, stock_code, stock_name)
    else:
        pred = call_tool(tool_name, stock_code, stock_name, decision_date)

    # 如果有错误，返回带错误的结果
    if pred.error:
        for period in periods:
            vr = VerifyResult(prediction=pred, period=period)
            results.append(vr)
        return results

    # 只查一次 K 线，所有周期复用（避免重复 DB 查询）
    klines = _get_kline_cached(stock_code, "1D", decision_date)

    # 验证每个周期
    for period in periods:
        hold_days = int(period.replace("T+", "").replace("T-", ""))
        future = _compute_return_from_klines(klines, decision_date, hold_days, stock_code, "1D")

        vr = VerifyResult(prediction=pred, period=period)

        if future:
            vr.actual_return_pct = future["pnl_pct"]
            vr.actual_direction = future["direction"]

            # 判断方向是否正确
            if pred.direction:
                if pred.direction == "neutral":
                    vr.correct = None  # 中性预测无法判断
                elif pred.direction == future["direction"]:
                    vr.correct = True
                else:
                    vr.correct = False

            # 计算盈亏
            if pred.action == "buy":
                vr.pnl_pct = future["pnl_pct"]
            elif pred.action == "sell":
                vr.pnl_pct = -future["pnl_pct"]
            else:
                vr.pnl_pct = 0.0

        results.append(vr)

    return results


def _call_tool_once(code: str, name: str, tool_name: str, is_skill: bool) -> Dict[str, Any]:
    """对单只股票执行一次 tool/skill 预测（独立于决策日，结果跨日期复用）。"""
    if is_skill:
        pred = call_skill(tool_name, code, name)
    else:
        pred = call_tool(tool_name, code, name)
    return {
        "score": pred.score,
        "direction": pred.direction,
        "action": pred.action,
        "confidence": pred.confidence,
        "raw_output": pred.raw_output,
        "error": pred.error,
    }


def _verify_one(
    code: str, name: str, date: str, tool_name: str,
    pred_data: Dict[str, Any], periods: List[str],
) -> List[VerifyResult]:
    """对已缓存的预测结果做单日期验证（不含 tool 调用）。"""
    results = []

    pred = PredictionResult(
        stock_code=code, stock_name=name, decision_date=date,
        tool_name=tool_name,
        score=pred_data["score"],
        direction=pred_data["direction"],
        action=pred_data["action"],
        confidence=pred_data["confidence"],
        raw_output=pred_data["raw_output"],
        error=pred_data["error"],
    )

    if pred.error:
        for period in periods:
            results.append(VerifyResult(prediction=pred, period=period))
        return results

    # 只查一次 K 线，所有周期复用
    klines = _get_kline_cached(code, "1D", date)

    for period in periods:
        hold_days = int(period.replace("T+", "").replace("T-", ""))
        future = _compute_return_from_klines(klines, date, hold_days, code, "1D")
        vr = VerifyResult(prediction=pred, period=period)

        if future:
            vr.actual_return_pct = future["pnl_pct"]
            vr.actual_direction = future["direction"]

            if pred.direction:
                if pred.direction == "neutral":
                    vr.correct = None
                elif pred.direction == future["direction"]:
                    vr.correct = True
                else:
                    vr.correct = False

            if pred.action == "buy":
                vr.pnl_pct = future["pnl_pct"]
            elif pred.action == "sell":
                vr.pnl_pct = -future["pnl_pct"]
            else:
                vr.pnl_pct = 0.0

        results.append(vr)

    return results


def _backtest_batch_worker(args: Tuple) -> List[Dict[str, Any]]:
    """进程池 worker：处理一只股票在所有决策日期的回测。

    每个 worker 进程有独立的内存空间，不存在 GIL 竞争，
    也不存在 DataSourceFactory 线程安全问题。
    """
    import sys as _sys
    # 确保子进程 stdout/stderr 使用 UTF-8（Windows 默认可能是 GBK）
    if hasattr(_sys.stdout, 'reconfigure'):
        try:
            _sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    if hasattr(_sys.stderr, 'reconfigure'):
        try:
            _sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    stock_code, stock_name, decision_dates, tool_name, periods, is_skill, klines_raw = args

    # 重建进程内的 K 线缓存和日期索引
    global _kline_cache, _kline_date_index
    _kline_cache = {}
    _kline_date_index = {}
    for key_str, klines in klines_raw.items():
        parts = key_str.split("|", 1)
        if len(parts) == 2:
            cache_key = (parts[0], parts[1])
            _kline_cache[cache_key] = klines
            # 构建日期索引
            idx = {}
            for i, k in enumerate(klines):
                d = k["date"][:10]
                if d not in idx:
                    idx[d] = i
            _kline_date_index[cache_key] = idx

    results = []

    for date in decision_dates:
        try:
            if is_skill:
                pred = call_skill(tool_name, stock_code, stock_name)
            else:
                pred = call_tool(tool_name, stock_code, stock_name, date)

            # 安全序列化 raw_output（移除不可 pickle 的对象）
            raw = pred.raw_output
            safe_raw = _sanitize_for_pickle(raw)

            pred_dict = {
                "score": pred.score,
                "direction": pred.direction,
                "action": pred.action,
                "confidence": pred.confidence,
                "raw_output": safe_raw,
                "error": pred.error,
            }

            if pred.error:
                for period in periods:
                    results.append({
                        "code": stock_code, "name": stock_name, "date": date,
                        "period": period, "prediction": pred_dict, "verify": None,
                    })
                continue

            klines = _get_kline_cached(stock_code, "1D", date)

            for period in periods:
                hold_days = int(period.replace("T+", "").replace("T-", ""))
                future = _compute_return_from_klines(klines, date, hold_days, stock_code, "1D")

                verify = None
                if future:
                    correct = None
                    if pred.direction and pred.direction != "neutral":
                        correct = pred.direction == future["direction"]

                    pnl_pct = 0.0
                    if pred.action == "buy":
                        pnl_pct = future["pnl_pct"]
                    elif pred.action == "sell":
                        pnl_pct = -future["pnl_pct"]

                    verify = {
                        "actual_return_pct": future["pnl_pct"],
                        "actual_direction": future["direction"],
                        "correct": correct,
                        "pnl_pct": pnl_pct,
                    }

                results.append({
                    "code": stock_code, "name": stock_name, "date": date,
                    "period": period, "prediction": pred_dict, "verify": verify,
                })
        except Exception as e:
            # 单个日期失败不影响其他日期
            pred_dict = {
                "score": None, "direction": "", "action": "",
                "confidence": 0.0, "raw_output": {}, "error": str(e),
            }
            for period in periods:
                results.append({
                    "code": stock_code, "name": stock_name, "date": date,
                    "period": period, "prediction": pred_dict, "verify": None,
                })

    return results


def _sanitize_for_pickle(obj: Any, _depth: int = 0) -> Any:
    """将对象转换为可 pickle / JSON 安全的格式。

    处理：
    - datetime → ISO 字符串
    - bytes → UTF-8 字符串（errors=replace）
    - Decimal → float
    - 集合 → list
    - 嵌套 dict/list 递归处理
    - 其他不可处理的对象 → str(obj)
    """
    if _depth > 10:
        return str(obj)

    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj

    if isinstance(obj, bytes):
        return obj.decode('utf-8', errors='replace')

    if isinstance(obj, datetime):
        return obj.isoformat()

    try:
        from decimal import Decimal
        if isinstance(obj, Decimal):
            return float(obj)
    except ImportError:
        pass

    if isinstance(obj, dict):
        return {
            _sanitize_for_pickle(k, _depth + 1): _sanitize_for_pickle(v, _depth + 1)
            for k, v in obj.items()
        }

    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_pickle(item, _depth + 1) for item in obj]

    if isinstance(obj, set):
        return [_sanitize_for_pickle(item, _depth + 1) for item in obj]

    # numpy 等特殊对象
    try:
        if hasattr(obj, 'tolist'):
            return obj.tolist()
        if hasattr(obj, 'item'):
            return obj.item()
    except Exception:
        pass

    # 兜底：转字符串
    try:
        return str(obj)
    except Exception:
        return "<unserializable>"


def backtest_batch(
    stocks: List[Dict[str, str]],
    decision_dates: List[str],
    tool_name: str,
    periods: List[str],
    is_skill: bool = False,
    max_workers: int = 16,
    verbose: bool = False,
    timeframe: str = "1D",
) -> List[VerifyResult]:
    """批量回测（多进程版，绕过 GIL）。

    优化设计：
    1. ProcessPoolExecutor 绕过 GIL，真正利用多核 CPU
    2. 每只股票一个任务（而非每个 (stock,date) 一个），减少 IPC 开销
    3. K 线数据序列化传入子进程，子进程内重建缓存
    4. 子进程内 DataSourceFactory 独立，无线程安全问题
    5. 失败时自动回退到串行执行
    """
    all_results = []
    n_stocks = len(stocks)
    n_dates = len(decision_dates)
    total_tasks = n_stocks * n_dates
    max_hold = max(int(p.replace("T+", "").replace("T-", "")) for p in periods)

    # ── 阶段 0: 预加载 K 线数据 ──
    print(f"  阶段0/2: 预加载 K 线数据...")
    earliest_date = decision_dates[0] if decision_dates else ""
    if earliest_date:
        dt = datetime.strptime(earliest_date, "%Y-%m-%d") - timedelta(days=120 + max_hold + 30)
        earliest_date = dt.strftime("%Y-%m-%d")
        print(f"    最早决策日: {decision_dates[0]}, 预加载起点: {earliest_date}")

    stock_codes = [s["code"] for s in stocks]
    try:
        preload_bulk_klines(stock_codes, timeframe, earliest_date, "", batch_size=100)
    except KeyboardInterrupt:
        print("\n⚠️  用户中断，停止预加载")
        return all_results

    # ── 阶段 1: 序列化 K 线缓存（主进程 → 子进程）──
    klines_by_stock = {}
    for s in stocks:
        code = s["code"]
        stock_klines = {}
        for tf in [timeframe]:
            key = (code, tf)
            if key in _kline_cache:
                stock_klines[f"{code}|{tf}"] = _kline_cache[key]
        klines_by_stock[code] = stock_klines

    # ── 阶段 2: 多进程预测 + 验证 ──
    print(f"  阶段1/1: 多进程预测 + 验证（{n_stocks} 只股票 × {n_dates} 天 = {total_tasks} 样本, {max_workers} 进程）...")
    completed_tasks = 0
    completed_stocks = 0
    start_time = time.time()

    worker_args = []
    for s in stocks:
        code = s["code"]
        worker_args.append((
            code, s["name"], decision_dates, tool_name, periods, is_skill,
            klines_by_stock.get(code, {}),
        ))

    def _collect_worker_results(worker_results):
        """将 worker 返回的 dict 列表转为 VerifyResult。"""
        for wr in worker_results:
            pred = PredictionResult(
                stock_code=wr["code"], stock_name=wr["name"],
                decision_date=wr["date"], tool_name=tool_name,
                score=wr["prediction"]["score"], direction=wr["prediction"]["direction"],
                action=wr["prediction"]["action"], confidence=wr["prediction"]["confidence"],
                raw_output=wr["prediction"]["raw_output"], error=wr["prediction"]["error"],
            )
            vr = VerifyResult(prediction=pred, period=wr["period"])
            v = wr.get("verify")
            if v:
                vr.actual_return_pct = v["actual_return_pct"]
                vr.actual_direction = v["actual_direction"]
                vr.correct = v["correct"]
                vr.pnl_pct = v["pnl_pct"]
            all_results.append(vr)

            if verbose:
                status = "✓" if vr.correct else ("✗" if vr.correct is False else "·")
                print(f"  {status} {vr.prediction.stock_code} {vr.prediction.decision_date} "
                      f"{vr.period}: pred={vr.prediction.direction} actual={vr.actual_direction} "
                      f"pnl={vr.pnl_pct:+.2f}%")

    try:
        actual_workers = min(max_workers, n_stocks)
        with ProcessPoolExecutor(max_workers=actual_workers) as executor:
            fut_map = {}
            for args in worker_args:
                fut = executor.submit(_backtest_batch_worker, args)
                fut_map[fut] = args[0]  # stock_code

            for fut in as_completed(fut_map):
                stock_code = fut_map[fut]
                completed_stocks += 1

                try:
                    _collect_worker_results(fut.result())
                    completed_tasks += n_dates
                except Exception as e:
                    logger.error("回测失败 %s: %s", stock_code, e)
                    completed_tasks += n_dates

                if completed_stocks % 10 == 0 or completed_stocks == n_stocks:
                    elapsed = time.time() - start_time
                    speed = completed_tasks / elapsed if elapsed > 0 else 0
                    eta = (total_tasks - completed_tasks) / speed if speed > 0 else 0
                    print(f"    进度: {completed_stocks}/{n_stocks} 只股票 "
                          f"({completed_tasks}/{total_tasks} 样本, {completed_tasks/total_tasks*100:.1f}%) "
                          f"速度: {speed:.0f} 样本/秒, ETA: {eta:.0f}秒")
    except KeyboardInterrupt:
        print(f"\n⚠️  用户中断，已完成 {completed_stocks}/{n_stocks} 只股票")
    except Exception as e:
        # ProcessPoolExecutor 可能因 pickle 失败等回退到串行
        logger.warning("多进程回测失败，回退到串行执行: %s", e)
        print(f"  ⚠️  多进程失败 ({e})，回退到串行执行...")
        for args in worker_args:
            try:
                _collect_worker_results(_backtest_batch_worker(args))
            except Exception as e2:
                logger.error("串行回测失败 %s: %s", args[0], e2)

    elapsed_total = time.time() - start_time
    speed = len(all_results) / elapsed_total if elapsed_total > 0 else 0
    print(f"  完成: {len(all_results)} 条结果, 耗时 {elapsed_total:.1f}s ({speed:.0f} 样本/秒)")

    return all_results


# ═══════════════════════════════════════════════════════════════
#  统计分析
# ═══════════════════════════════════════════════════════════════

def calculate_stats(results: List[VerifyResult], tool_name: str, period: str) -> ToolSkillStats:
    """计算统计结果。"""
    stats = ToolSkillStats(name=tool_name, type="tool", period=period)

    period_results = [r for r in results if r.period == period]
    stats.total_samples = len(period_results)

    valid_results = [r for r in period_results if not r.prediction.error]
    stats.valid_samples = len(valid_results)

    if not valid_results:
        return stats

    # 方向统计 + 股票级别统计
    stock_stats = {}  # {"code": {"name": "", "count": 0, "correct": 0, "wrong": 0, "neutral": 0}}
    for r in valid_results:
        code = r.prediction.stock_code
        if code not in stock_stats:
            stock_stats[code] = {
                "name": r.prediction.stock_name,
                "count": 0,
                "correct": 0,
                "wrong": 0,
                "neutral": 0,
                "pnl_list": [],
            }
        stock_stats[code]["count"] += 1

        if r.correct is True:
            stats.correct_count += 1
            stock_stats[code]["correct"] += 1
        elif r.correct is False:
            stats.wrong_count += 1
            stock_stats[code]["wrong"] += 1
        else:
            stats.neutral_count += 1
            stock_stats[code]["neutral"] += 1

        if r.pnl_pct != 0:
            stock_stats[code]["pnl_list"].append(r.pnl_pct)

    # 整理股票统计（去掉 pnl_list，计算胜率）
    for code, s in stock_stats.items():
        directional = s["correct"] + s["wrong"]
        s["win_rate"] = s["correct"] / directional if directional > 0 else 0.0
        s["avg_pnl"] = sum(s["pnl_list"]) / len(s["pnl_list"]) if s["pnl_list"] else 0.0
        del s["pnl_list"]
    stats.stock_stats = stock_stats

    # 胜率（排除中性）
    directional = stats.correct_count + stats.wrong_count
    if directional > 0:
        stats.win_rate = stats.correct_count / directional

    # 置信度加权准确率
    # 权重 = |score - 50| / 50，score 越极端权重越高
    # score=100 或 0 → 权重 1.0；score=50 → 权重 0.0
    weighted_correct = 0.0
    weighted_total = 0.0
    for r in valid_results:
        if r.correct is None:
            continue
        score = r.prediction.score
        if score is None:
            continue
        w = abs(score - 50) / 50.0
        if w < 0.01:
            continue  # score≈50 的预测不参与（无方向性）
        weighted_total += w
        if r.correct:
            weighted_correct += w
    if weighted_total > 0:
        stats.weighted_accuracy = weighted_correct / weighted_total

    # 盈亏统计
    pnl_list = [r.pnl_pct for r in valid_results if r.pnl_pct != 0]
    if pnl_list:
        stats.avg_pnl_pct = sum(pnl_list) / len(pnl_list)

    # 平均持有天数
    hold_days_list = []
    for r in valid_results:
        if r.period.startswith("T+"):
            hold_days_list.append(int(r.period.replace("T+", "")))
    stats.avg_hold_days = sum(hold_days_list) / len(hold_days_list) if hold_days_list else 1

    # 时间收益率
    if stats.avg_hold_days > 0:
        stats.return_per_day = stats.avg_pnl_pct / stats.avg_hold_days

    # 平均置信度
    conf_list = [r.prediction.confidence for r in valid_results if r.prediction.confidence > 0]
    if conf_list:
        stats.avg_confidence = sum(conf_list) / len(conf_list)

    # 错误数
    stats.error_count = len([r for r in period_results if r.prediction.error])

    return stats


def get_high_win_rate_stocks(stats: ToolSkillStats, min_count: int = 3) -> List[Dict[str, Any]]:
    """筛选高胜率股票（出现次数 >= min_count 且胜率 = 100%）。"""
    result = []
    for code, s in stats.stock_stats.items():
        directional = s["correct"] + s["wrong"]
        if directional >= min_count and s["wrong"] == 0:
            result.append({
                "code": code,
                "name": s["name"],
                "count": s["count"],
                "correct": s["correct"],
                "wrong": s["wrong"],
                "win_rate": 1.0,
                "avg_pnl": round(s["avg_pnl"], 2),
            })
    # 按出现次数降序
    result.sort(key=lambda x: x["count"], reverse=True)
    return result


def print_stats(stats: ToolSkillStats):
    """打印统计结果。"""
    print(f"\n{'='*60}")
    print(f"Tool/Skill: {stats.name} ({stats.type})")
    print(f"周期: {stats.period}")
    print(f"{'='*60}")
    print(f"总样本数:     {stats.total_samples}")
    print(f"有效样本数:   {stats.valid_samples}")
    print(f"错误数:       {stats.error_count}")
    print(f"方向正确:     {stats.correct_count}")
    print(f"方向错误:     {stats.wrong_count}")
    print(f"方向中性:     {stats.neutral_count}")
    print(f"胜率:         {stats.win_rate:.2%}")
    print(f"加权准确率:   {stats.weighted_accuracy:.2%}")
    print(f"平均盈亏:     {stats.avg_pnl_pct:+.2f}%")
    print(f"平均持有天数: {stats.avg_hold_days:.1f}")
    print(f"时间收益率:   {stats.return_per_day:+.4f}%/天")
    print(f"平均置信度:   {stats.avg_confidence:.2f}")

    # 打印高胜率股票
    high_wr = get_high_win_rate_stocks(stats)
    if high_wr:
        print(f"\n高胜率股票（出现>=3次且100%胜率）: {len(high_wr)} 只")
        print(f"  {'代码':<10} {'名称':<12} {'出现次数':<10} {'正确':<8} {'平均盈亏':<12}")
        print(f"  {'-'*52}")
        for s in high_wr[:20]:  # 最多显示20只
            print(f"  {s['code']:<10} {s['name']:<12} {s['count']:<10} {s['correct']:<8} {s['avg_pnl']:<+12.2f}")
        if len(high_wr) > 20:
            print(f"  ... 还有 {len(high_wr) - 20} 只")


def print_summary(all_stats: List[ToolSkillStats]):
    """打印汇总表格。"""
    print(f"\n{'='*90}")
    print("回测汇总")
    print(f"{'='*90}")
    print(f"{'Tool/Skill':<25} {'周期':<8} {'样本':<8} {'胜率':<10} {'加权准确率':<12} {'平均盈亏':<12} {'时间收益率':<12}")
    print("-" * 90)

    for stats in all_stats:
        high_wr = get_high_win_rate_stocks(stats)
        high_wr_mark = f" [高胜率:{len(high_wr)}只]" if high_wr else ""
        print(f"{stats.name:<25} {stats.period:<8} {stats.valid_samples:<8} "
              f"{stats.win_rate:<10.2%} {stats.weighted_accuracy:<12.2%} {stats.avg_pnl_pct:<+12.2f} {stats.return_per_day:<+12.4f}{high_wr_mark}")


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════

def main():
    total_start = time.time()

    parser = argparse.ArgumentParser(
        description="Tool/Skill 历史回测引擎",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument("--tool", type=str, help="tool 名称（逗号分隔多个）")
    parser.add_argument("--skill", type=str, help="skill 名称（逗号分隔多个）")
    parser.add_argument("--stock-pool", type=str, default="all",
                        help="股票池: all/沪深/创业板/科创板/北证/随机N")
    parser.add_argument("--days", type=int, default=90, help="回测天数（默认90天）")
    parser.add_argument("--periods", type=str, default="T+1,T+3,T+5",
                        help="验证周期（默认 T+1,T+3,T+5）")
    parser.add_argument("--timeframe", type=str, default="1D",
                        help="K线周期: 1D/15m（默认1D）")
    parser.add_argument("--workers", type=int, default=0, help="并发进程数（默认=CPU核心数）")
    parser.add_argument("--sample", type=int, default=0,
                        help="随机抽样数量（0=全量，>0 时从股票池随机抽取 N 只）")
    parser.add_argument("--verbose", action="store_true", help="输出详细结果")
    parser.add_argument("--output", type=str, help="汇总结果 JSON 文件路径")
    parser.add_argument("--output-dir", type=str, default="backtest_results",
                        help="高胜率股票输出目录（默认 backtest_results）")

    args = parser.parse_args()

    if not args.tool and not args.skill:
        parser.error("必须指定 --tool 或 --skill")

    # 解析参数
    periods = [p.strip() for p in args.periods.split(",") if p.strip()]
    tools = [t.strip() for t in args.tool.split(",")] if args.tool else []
    skills = [s.strip() for s in args.skill.split(",")] if args.skill else []

    # 默认进程数 = CPU 核心数
    if args.workers <= 0:
        args.workers = os.cpu_count() or 4
        print(f"并发进程数: {args.workers}（自动检测 CPU 核心数）")

    # 获取股票池
    print(f"获取股票池: {args.stock_pool}")
    if args.stock_pool.startswith("随机"):
        count = int(args.stock_pool.replace("随机", ""))
        stocks = get_random_stocks(count)
    else:
        stocks = get_stock_pool(args.stock_pool)

    # 随机抽样
    if args.sample > 0 and args.sample < len(stocks):
        import random
        random.seed(42)
        stocks = random.sample(stocks, args.sample)
        print(f"随机抽样: {args.sample} 只")

    print(f"股票数量: {len(stocks)}")

    # 生成决策日期
    end_date = datetime.now()
    start_date = end_date - timedelta(days=args.days)
    decision_dates = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:  # 跳过周末
            decision_dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    print(f"决策日期: {len(decision_dates)} 天 ({start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')})")
    print(f"验证周期: {periods}")
    print(f"K线周期: {args.timeframe}")

    # 回测
    all_stats = []

    for tool_name in tools:
        print(f"\n{'='*60}")
        print(f"回测 tool: {tool_name}")
        print(f"{'='*60}")

        t0 = time.time()
        results = backtest_batch(
            stocks, decision_dates, tool_name, periods,
            is_skill=False, max_workers=args.workers, verbose=args.verbose,
            timeframe=args.timeframe,
        )
        elapsed = time.time() - t0

        print(f"完成: {len(results)} 条结果, 耗时 {elapsed:.1f}s")

        for period in periods:
            stats = calculate_stats(results, tool_name, period)
            stats.type = "tool"
            all_stats.append(stats)
            print_stats(stats)

    for skill_name in skills:
        print(f"\n{'='*60}")
        print(f"回测 skill: {skill_name}")
        print(f"{'='*60}")

        t0 = time.time()
        results = backtest_batch(
            stocks, decision_dates, skill_name, periods,
            is_skill=True, max_workers=args.workers, verbose=args.verbose,
            timeframe=args.timeframe,
        )
        elapsed = time.time() - t0

        print(f"完成: {len(results)} 条结果, 耗时 {elapsed:.1f}s")

        for period in periods:
            stats = calculate_stats(results, skill_name, period)
            stats.type = "skill"
            all_stats.append(stats)
            print_stats(stats)

    # 汇总
    print_summary(all_stats)

    # 保存高胜率股票到独立文件（每个 tool/skill × 周期一个文件）
    high_wr_dir = args.output_dir or "backtest_results"
    os.makedirs(high_wr_dir, exist_ok=True)

    for stats in all_stats:
        high_wr = get_high_win_rate_stocks(stats)
        if high_wr:
            # 文件名: tool_skill名_周期.json
            filename = f"{stats.name}_{stats.period}.json".replace("/", "_")
            filepath = os.path.join(high_wr_dir, filename)
            data = {
                "tool_skill": stats.name,
                "type": stats.type,
                "period": stats.period,
                "total_samples": stats.total_samples,
                "valid_samples": stats.valid_samples,
                "win_rate": round(stats.win_rate, 4),
                "weighted_accuracy": round(stats.weighted_accuracy, 4),
                "avg_pnl_pct": round(stats.avg_pnl_pct, 4),
                "return_per_day": round(stats.return_per_day, 6),
                "high_win_rate_stocks": high_wr,
            }
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"  保存: {filepath} ({len(high_wr)} 只高胜率股票)")

    # 保存汇总结果
    if args.output:
        output_data = {
            "config": {
                "tools": tools,
                "skills": skills,
                "stock_pool": args.stock_pool,
                "sample": args.sample,
                "days": args.days,
                "periods": periods,
                "timeframe": args.timeframe,
            },
            "stats": [
                {
                    "name": s.name,
                    "type": s.type,
                    "period": s.period,
                    "total_samples": s.total_samples,
                    "valid_samples": s.valid_samples,
                    "correct_count": s.correct_count,
                    "wrong_count": s.wrong_count,
                    "neutral_count": s.neutral_count,
                    "win_rate": round(s.win_rate, 4),
                    "weighted_accuracy": round(s.weighted_accuracy, 4),
                    "avg_pnl_pct": round(s.avg_pnl_pct, 4),
                    "avg_hold_days": round(s.avg_hold_days, 1),
                    "return_per_day": round(s.return_per_day, 6),
                    "avg_confidence": round(s.avg_confidence, 4),
                    "error_count": s.error_count,
                    "high_win_rate_stocks_count": len(get_high_win_rate_stocks(s)),
                }
                for s in all_stats
            ],
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        print(f"\n汇总结果已保存到: {args.output}")

    # 总耗时统计
    total_elapsed = time.time() - total_start
    print(f"\n{'='*80}")
    print(f"回测完成！总耗时: {total_elapsed:.1f}s ({total_elapsed/60:.1f} 分钟)")
    print(f"{'='*80}")


# 捕获 Ctrl+C，确保 ThreadPoolExecutor 能被正确清理
_original_main = main

def _main_with_signal():
    """包装 main，支持键盘中断。"""
    import signal

    def _sigint_handler(signum, frame):
        print("\n\n⚠️  收到中断信号，正在退出...")
        # 直接抛出 KeyboardInterrupt，让 ThreadPoolExecutor 清理
        raise KeyboardInterrupt

    # 注册信号处理器
    old_handler = signal.signal(signal.SIGINT, _sigint_handler)

    try:
        _original_main()
    except KeyboardInterrupt:
        print("\n回测已中断。")
    finally:
        # 恢复原始信号处理器
        signal.signal(signal.SIGINT, old_handler)


if __name__ == "__main__":
    _main_with_signal()
