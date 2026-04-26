"""
╔══════════════════════════════════════════════════════════════════╗
║                    指标策略自动审核服务                           ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  【触发入口】                                                     ║
║    前端选股器 → 勾选股票 → 选择指标策略 → 点击"自动审核"          ║
║    → POST /api/xuangu/review                                     ║
║                                                                  ║
║  【审核流程（逐只股票）】                                         ║
║                                                                  ║
║    ┌─────────────────────┐                                       ║
║    │ 获取K线 + 执行指标   │                                       ║
║    └──────────┬──────────┘                                       ║
║               ▼                                                  ║
║        现价 <= 买点价格？                                         ║
║          │           │                                           ║
║         YES          NO → 通知"没出现买点" → 跳过                ║
║          ▼                                                      ║
║    ┌─────────────────────┐                                       ║
║    │ 搜索个股新闻+情感    │                                       ║
║    └──────────┬──────────┘                                       ║
║               ▼                                                  ║
║    新闻无 / 中性 / 总分>0？                                      ║
║          │           │                                           ║
║         YES    负面且<=0 → 通知"负面新闻" → 跳过                 ║
║          ▼                                                      ║
║    ┌─────────────────────┐                                       ║
║    │ 加入自选股           │                                       ║
║    └─────────────────────┘                                       ║
║                                                                  ║
║  【通信方式】                                                     ║
║    Server-Sent Events (SSE) — 逐只股票推送进度/结果              ║
║                                                                  ║
║  【中断机制】                                                     ║
║    前端断开 → Flask 在下一个 yield 处抛 GeneratorExit            ║
║    → 当前股票跑完后干净退出，不继续后续                           ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import queue
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Generator, List, Optional

import pandas as pd

from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
#  多周期回测审核配置
# ================================================================

REVIEW_TIMEFRAMES = {
    "short": {
        "label": "短线",
        "periods": [
            {"tf": "1D", "months": 6, "label": "6月线"},
            {"tf": "1D", "months": 3, "label": "3月线"},
            {"tf": "1D", "months": 1, "label": "1月线"},
            {"tf": "1H", "months": 1, "label": "1h线"},
        ],
    },
    "mid": {
        "label": "中线",
        "periods": [
            {"tf": "1W", "months": 12, "label": "年线"},
            {"tf": "1D", "months": 6, "label": "6月线"},
            {"tf": "1D", "months": 3, "label": "3月线"},
        ],
    },
    "long": {
        "label": "长线",
        "periods": [
            {"tf": "1D", "months": 6, "label": "6M"},
            {"tf": "1D", "months": 12, "label": "1年"},
            {"tf": "1M", "months": 36, "label": "3年"},
        ],
    },
}

# ── 回测通过阈值（硬性要求，所有周期必须同时满足） ──
BACKTEST_MIN_WIN_RATE = 70.0   # 最低胜率 70%
BACKTEST_MIN_RETURN = 0.0      # 最低收益率 > 0%（正收益率）


def _run_backtest(cancelled: List[bool], **kwargs) -> Optional[Dict[str, Any]]:
    """
    线程化执行单次回测，支持取消。

    用 threading.Thread 在后台跑回测，主线程每 0.2s 轮询 cancelled 标志。
    cancelled[0] 为 True 时立即返回 None，不等线程结束。
    线程是 daemon，进程退出时自动终止。
    """
    q = queue.Queue()

    def _worker():
        try:
            from app.services.backtest import BacktestService
            svc = BacktestService()
            result = svc.run(**kwargs)
            q.put(("ok", result))
        except Exception as e:
            q.put(("error", str(e)))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    # 轮询：每 0.2s 检查 cancelled，最多等 30s
    result = None
    for _ in range(150):  # 30s / 0.2s = 150
        if cancelled[0]:
            t.join(timeout=2)  # 等线程结束，避免泄漏
            return None
        try:
            status, payload = q.get(timeout=0.2)
            if status == "ok":
                result = payload
            else:
                logger.warning(f"_run_backtest error: {payload}")
            break
        except queue.Empty:
            continue

    if result is None and not cancelled[0]:
        logger.warning("_run_backtest timeout (30s)")

    t.join(timeout=2)  # 确保线程结束
    return result


# ================================================================
#  数据获取层
# ================================================================

def _get_indicator_code(indicator_id: int, user_id: int) -> Optional[str]:
    """
    从 qd_indicator_codes 表获取指标的 Python 源代码。

    权限规则：只能访问自己的指标 或 已发布到社区的指标。
    失败时返回 None（不抛异常，由调用方决定如何处理）。
    """
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT code, name FROM qd_indicator_codes "
                    "WHERE id = %s AND (user_id = %s OR publish_to_community = 1)",
                    (indicator_id, user_id),
                )
                row = cur.fetchone()
                if row:
                    return row.get("code") or ""
            finally:
                cur.close()
        return None
    except Exception as e:
        logger.error(f"_get_indicator_code({indicator_id}) failed: {e}", exc_info=True)
        return None


def _get_stock_kline(market: str, symbol: str, limit: int = 200) -> Optional[pd.DataFrame]:
    """
    获取股票日线K线数据，返回标准 OHLCV DataFrame。

    调用 KlineService.get_kline()，内部走 feather 缓存 → 远端降级链。
    返回的 DataFrame 保证包含 open/high/low/close/volume 五列（数值型）。
    数据不足或网络失败返回 None。
    """
    try:
        from app.services.kline import KlineService

        svc = KlineService()
        klines = svc.get_kline(
            market=market,
            symbol=symbol,
            timeframe="1D",   # 日线；KlineService 内部用 "1D" 而非 "day"
            limit=limit,
        )
        if not klines:
            return None

        df = pd.DataFrame(klines)
        # 确保必要列存在并转为数值（部分数据源返回字符串）
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
            else:
                df[col] = 0.0
        return df
    except Exception as e:
        logger.error(f"_get_stock_kline({market},{symbol}) failed: {e}", exc_info=True)
        return None


# ================================================================
#  买点验证辅助函数
# ================================================================

def _extract_date_from_df(df: pd.DataFrame, idx) -> Optional[datetime]:
    """
    从 DataFrame 行提取交易日期。

    优先级：time 列 → 索引 → 返回 None。
    支持 Unix 时间戳（int/float）和 ISO 日期字符串。
    """
    try:
        # 优先从 time 列获取
        if "time" in df.columns:
            t = df.loc[idx, "time"]
            if isinstance(t, (int, float)) and t > 0:
                return datetime.fromtimestamp(t)
            elif isinstance(t, str) and t:
                try:
                    return datetime.fromisoformat(t.replace("Z", "+00:00"))
                except ValueError:
                    pass
        # 降级：尝试从索引获取
        if hasattr(idx, "to_pydatetime"):
            return idx.to_pydatetime()
        if isinstance(idx, (int, float)) and idx > 1_000_000_000:
            return datetime.fromtimestamp(idx)
    except Exception:
        pass
    return None


def _is_buy_recency_valid(df: pd.DataFrame, last_buy_idx, max_trading_days: int = 3) -> bool:
    """
    验证最新买点是否在 max_trading_days 个交易日内。

    计算方式：从 DataFrame 末尾到买点所在行的行数差 ≤ max_trading_days。
    行数差等价于交易日差（日线 DataFrame 每行 = 1 个交易日）。
    如果买点日期可以解析，则用日期差做精确校验（自然日差 ≤ max_trading_days * 2，
    以覆盖周末/节假日，宽松容错）。
    """
    if df is None or len(df) == 0:
        return False

    n = len(df)
    try:
        buy_pos = df.index.get_loc(last_buy_idx)
    except (KeyError, TypeError):
        return False

    # 行数差检查：买点距末尾超过 max_trading_days 行 → 过期
    if (n - 1 - buy_pos) > max_trading_days:
        return False

    # 日期差双重校验（宽松）
    buy_date = _extract_date_from_df(df, last_buy_idx)
    if buy_date is not None:
        last_date = _extract_date_from_df(df, df.index[-1])
        if last_date is not None:
            delta = (last_date - buy_date).days
            if delta > max_trading_days * 2:  # 宽松容错周末/节假日
                return False

    return True


# ================================================================
#  价格获取层
# ================================================================

def _safe_float(val: Any) -> Optional[float]:
    """
    安全转换为正 float，失败或非正数返回 None。
    用于处理各种数据源返回的价格/评分字段，避免 ValueError 传播。
    """
    if val is None:
        return None
    try:
        v = float(val)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def _get_current_price_from_df(df: pd.DataFrame) -> Optional[float]:
    """
    从已有的 K 线 DataFrame 末尾取 close 作为"当前价"。
    用途：实时报价失败时的降级方案，避免额外网络请求。
    注意：盘中时这可能是昨日收盘价，非实时。
    """
    if df is None or len(df) == 0:
        return None
    try:
        price = float(df.iloc[-1]["close"])
        return price if price > 0 else None
    except (ValueError, TypeError, KeyError):
        return None


def _get_current_price_ticker(
    market: str, symbol: str, fallback_df: pd.DataFrame = None
) -> Optional[float]:
    """
    通过 DataSourceFactory 实时报价获取当前价格。

    优先级：
      1. source.get_ticker(symbol) 实时报价 → 取 "last" 或 "price" 字段
      2. 降级：从传入的 fallback_df 取最后一条 close
      3. 全部失败返回 None

    传入 fallback_df 可避免重复拉取 K 线（调用方已有数据时）。
    """
    try:
        from app.data_sources import DataSourceFactory

        source = DataSourceFactory.get_source(market)
        ticker = source.get_ticker(symbol)
        if ticker:
            price = _safe_float(ticker.get("last")) or _safe_float(ticker.get("price"))
            if price is not None:
                return price
    except Exception as e:
        logger.warning(f"_get_current_price_ticker({market},{symbol}) failed: {e}")

    # Fallback: 已有的 K 线 DataFrame
    if fallback_df is not None and len(fallback_df) > 0:
        return _get_current_price_from_df(fallback_df)
    return None


# ================================================================
#  指标执行层
# ================================================================

def _run_indicator_on_stock(
    indicator_code: str, market: str, symbol: str, user_params: Dict[str, Any] = None,
    _cancelled: List[bool] = None,
) -> Dict[str, Any]:
    """
    在真实股票K线数据上执行用户指标代码，提取买点信号。

    执行流程：
      1. 拉取 200 根日线 K 线 → DataFrame
      2. 获取实时价（失败退回 K 线 close）
      3. 解析指标代码中的 # @param 声明，合并用户传入的参数
      4. 在沙箱环境中执行指标代码（safe_exec，30s 超时）
      5. 从执行后的 df['buy'] 列提取最近一个买点信号的 close 价格

    沙箱安全：
      - 使用 build_safe_builtins() 限制可用内置函数
      - 禁止 import/os/sys/网络操作
      - 超时自动终止

    取消支持：
      - _cancelled 标志在每个耗时步骤前后检查
      - 标志置 True 时立即返回 cancelled 结果，不继续后续步骤
      - 沙箱执行通过 ThreadPoolExecutor 包装，每 0.5s 轮询取消标志

    返回:
      {
        "success": bool,              # 整体是否成功
        "buy_price": float | None,    # 最近买点信号对应的 close
        "buy_date": str | None,       # 买点日期 (ISO 格式)
        "sell_price": float | None,   # 最近卖点信号对应的 close（无卖点则 None）
        "sell_date": str | None,      # 卖点日期 (ISO 格式)
        "current_price": float | None,# 当前价格（实时或K线close）
        "has_buy_signal": bool,       # 指标是否产生了 buy=True 的信号
        "error": str | None,          # 失败原因
        "cancelled": bool,            # 是否被取消（可选）
      }
    """
    from app.services.indicator_params import IndicatorParamsParser
    from app.utils.safe_exec import build_safe_builtins, safe_exec_with_validation

    cancelled = _cancelled or [False]

    result = {
        "success": False,
        "buy_price": None,
        "buy_date": None,
        "sell_price": None,
        "sell_date": None,
        "current_price": None,
        "has_buy_signal": False,
        "error": None,
        "cancelled": False,
    }

    # ── 检查取消 ──
    if cancelled[0]:
        result["cancelled"] = True
        result["error"] = "已取消"
        return result

    # ── 1. 获取 K 线 ──
    df = _get_stock_kline(market, symbol, limit=200)
    if df is None or len(df) == 0:
        result["error"] = f"无法获取 {symbol} 的K线数据"
        return result

    # ── 2. 当前价格（实时优先，K线兜底） ──
    current_price = _get_current_price_ticker(market, symbol, fallback_df=df)
    result["current_price"] = current_price

    # ── 3. 解析指标参数 ──
    declared_params = IndicatorParamsParser.parse_params(indicator_code)
    merged_params = IndicatorParamsParser.merge_params(declared_params, user_params or {})

    # ── 4. 沙箱执行（带取消感知） ──
    df_copy = df.copy()
    exec_env = {
        "df": df_copy,
        "pd": pd,
        "np": np,
        "params": merged_params,
        "output": None,
    }
    exec_env["__builtins__"] = build_safe_builtins()

    try:
        # 使用 ThreadPoolExecutor 包装，支持取消中断
        # 关键：daemon=True + shutdown(wait=False)，取消时立即返回不等后台线程
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

        def _exec_indicator():
            return safe_exec_with_validation(
                code=indicator_code,
                exec_globals=exec_env,
                exec_locals=exec_env,
                timeout=30,
            )

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_exec_indicator)
        try:
            exec_result = None
            for _ in range(60):  # 30s / 0.5s = 60 次
                if cancelled[0]:
                    future.cancel()
                    result["cancelled"] = True
                    result["error"] = "已取消"
                    return result
                try:
                    exec_result = future.result(timeout=0.5)
                    break
                except FuturesTimeoutError:
                    continue

            if exec_result is None:
                result["error"] = "指标执行超时(30s)"
                return result
        finally:
            # 关键：wait=False 不等后台线程，取消时立即返回
            executor.shutdown(wait=False)

        if not exec_result.get("success"):
            result["error"] = f"指标执行失败: {exec_result.get('error', '未知错误')}"
            return result

        # ── 5. 提取买点信号 + 卖点信号 ──
        executed_df = exec_env.get("df", df_copy)
        if "buy" not in executed_df.columns:
            result["error"] = "指标未生成 buy 信号列"
            return result

        buy_series = executed_df["buy"].astype(bool)
        if buy_series.any():
            buy_indices = buy_series[buy_series].index.tolist()
            last_buy_idx = buy_indices[-1]
            try:
                buy_price = float(executed_df.loc[last_buy_idx, "close"])
                result["buy_price"] = buy_price
                result["has_buy_signal"] = True
            except (ValueError, TypeError, KeyError) as e:
                logger.warning(f"_run_indicator_on_stock({symbol}): buy信号存在但取价格失败: {e}")
                result["has_buy_signal"] = False

            # 提取买点日期
            try:
                buy_dt = _extract_date_from_df(executed_df, last_buy_idx)
                if buy_dt is not None:
                    result["buy_date"] = buy_dt.strftime("%Y-%m-%d")
            except Exception as e:
                logger.debug(f"_run_indicator_on_stock({symbol}): 提取买点日期失败: {e}")

        # 提取卖点信号（用于买卖逻辑校验）
        if "sell" in executed_df.columns:
            sell_series = executed_df["sell"].astype(bool)
            if sell_series.any():
                sell_indices = sell_series[sell_series].index.tolist()
                last_sell_idx = sell_indices[-1]
                try:
                    result["sell_price"] = float(executed_df.loc[last_sell_idx, "close"])
                except (ValueError, TypeError, KeyError):
                    pass
                try:
                    sell_dt = _extract_date_from_df(executed_df, last_sell_idx)
                    if sell_dt is not None:
                        result["sell_date"] = sell_dt.strftime("%Y-%m-%d")
                except Exception:
                    pass

        result["success"] = True
        result["_executed_df"] = executed_df  # 供调用方做买点时效性校验
        return result

    except Exception as e:
        result["error"] = f"指标执行异常: {str(e)}"
        logger.error(f"_run_indicator_on_stock({symbol}) failed: {e}", exc_info=True)
        return result


# ================================================================
#  新闻情感分析层
# ================================================================

def _search_stock_news_sentiment(symbol: str, name: str = "", _cancelled: List[bool] = None) -> Dict[str, Any]:
    """
    搜索个股近3天新闻并分析情感倾向。

    调用 SearchService.search_cn_stock_news()，内部并行执行：
      - Web 搜索引擎（百度/博查等）
      - 5 路国内财经直连（东财公告/新浪/腾讯/凤凰/新浪7x24）
    自动去重 + 加权情感评分 + 时间衰减综合评分。

    取消支持：
      - 使用 ThreadPoolExecutor 包装搜索调用
      - 每 0.5s 轮询取消标志，最多等待 15s
      - 取消时立即返回默认中性结果

    返回:
      {
        "has_news": bool,              # 是否找到相关新闻
        "composite_score": float,      # 综合评分 (0-10)，5.0=中性
        "direction": str,              # 利好/偏利好/中性/偏利空/利空
        "news_count": int,             # 新闻条数
        "error": str | None,           # 搜索失败原因
      }

    判断逻辑（由调用方 review_stocks 使用）：
      - has_news=False → 无新闻，视为中性，通过
      - direction=中性/利好/偏利好 → 通过
      - direction=利空/偏利空 且 composite_score<=0 → 不通过
    """
    cancelled = _cancelled or [False]
    result = {
        "has_news": False,
        "composite_score": 5.0,  # 默认中性
        "direction": "中性",
        "news_count": 0,
        "error": None,
    }

    # ── 检查取消 ──
    if cancelled[0]:
        return result

    try:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
        from app.services.search import SearchService

        def _do_search():
            svc = SearchService()
            return svc.search_cn_stock_news(
                stock_code=symbol,
                stock_name=name or "",
                days=3,
                max_web_results=3,
            )

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(_do_search)
        try:
            search_resp = None
            for _ in range(30):  # 15s / 0.5s = 30 次
                if cancelled[0]:
                    future.cancel()
                    logger.info(f"_search_stock_news_sentiment({symbol}): cancelled")
                    return result
                try:
                    search_resp = future.result(timeout=0.5)
                    break
                except FuturesTimeoutError:
                    continue

            if search_resp is None:
                result["error"] = "新闻搜索超时(15s)"
                return result
        finally:
            executor.shutdown(wait=False)

        if not search_resp or not search_resp.success:
            err_msg = getattr(search_resp, "error_message", None)
            if err_msg:
                result["error"] = f"新闻搜索失败: {err_msg}"
            return result

        items = search_resp.results or []
        result["news_count"] = len(items)

        if len(items) == 0:
            return result

        result["has_news"] = True

        metadata = getattr(search_resp, "metadata", None) or {}
        composite_score = metadata.get("composite_score")
        direction = metadata.get("direction")

        if composite_score is not None:
            result["composite_score"] = _safe_float(composite_score) or 5.0
        if direction:
            result["direction"] = direction

        return result

    except Exception as e:
        result["error"] = f"新闻搜索失败: {str(e)}"
        logger.error(f"_search_stock_news_sentiment({symbol}) failed: {e}", exc_info=True)
        return result


# ================================================================
#  自选股写入层
# ================================================================

def _add_to_watchlist(user_id: int, market: str, symbol: str, name: str = "") -> bool:
    """
    将股票加入当前用户的自选股（幂等操作）。

    使用 ON CONFLICT DO UPDATE 实现 upsert：
      - 不存在 → INSERT 新记录
      - 已存在 → UPDATE name 和 updated_at
    不会产生重复数据。
    """
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    """INSERT INTO qd_watchlist (user_id, market, symbol, name)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (user_id, market, symbol) DO UPDATE SET
                           name = EXCLUDED.name, updated_at = NOW()""",
                    (user_id, market, symbol, name),
                )
                conn.commit()
            finally:
                cur.close()
        return True
    except Exception as e:
        logger.error(f"_add_to_watchlist({symbol}) failed: {e}", exc_info=True)
        return False


# ================================================================
#  辅助工具
# ================================================================

def _extract_indicator_name(code: str) -> str:
    """
    从指标 Python 代码中提取 my_indicator_name 变量的值。
    用于前端进度显示（"正在按照 RSI策略 审核..."）。
    提取失败返回空字符串，不影响审核流程。
    """
    if not code:
        return ""
    try:
        m = re.search(r'^\s*my_indicator_name\s*=\s*["\'](.+?)["\']', code, re.MULTILINE)
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def _sse(data: Dict[str, Any]) -> str:
    """
    将字典格式化为 Server-Sent Events 消息。
    格式: "data: {json}\n\n"
    前端通过 EventSource 或 fetch+ReadableStream 接收。
    """
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ================================================================
#  主入口：逐只审核（SSE Generator）
# ================================================================

def review_stocks(
    user_id: int,
    indicator_id: int,
    stocks: List[Dict[str, Any]],
    user_params: Dict[str, Any] = None,
    review_mode: str = "mid",
    _cancelled: List[bool] = None,
) -> Generator[str, None, None]:
    """
    逐个审核股票，通过 SSE 流式返回进度。

    【调用链路】
      前端 POST /api/xuangu/review
        → xuangu.py route 创建 Response(generator)
          → 本函数逐只 yield SSE 消息
            → 前端 fetch ReadableStream 逐条读取并渲染

    【SSE 消息类型】
      progress — 正在处理中（前端显示 loading）
        {"type":"progress","symbol":"000001","status":"checking","msg":"..."}

      result   — 单只股票审核结论（前端取消复选框）
        {"type":"result","symbol":"000001","added":true,"reason":"passed","msg":"..."}

      done     — 全部完成（前端关闭 loading、刷新自选股列表）
        {"type":"done","total":5,"added":2,"skipped":3}

      error    — 致命错误（指标不存在等）
        {"type":"error","msg":"指标ID 999 不存在"}

    【中断机制】
      用户关闭对话框 → 前端 abort fetch → Flask 在下一个 yield 处抛 GeneratorExit
      → 外层 generate() 设置 cancelled[0]=True 并调用 gen.close()
      → 内层在下一个 yield 点收到 GeneratorExit，except 捕获后 return

      【增强】cancelled 标志同时透传到底层耗时操作：
      - _run_indicator_on_stock：在 K线拉取后、沙箱执行前检查取消；
        沙箱执行通过 ThreadPoolExecutor + 0.5s 轮询，最坏 0.5s 内响应取消
      - _search_stock_news_sentiment：同样用 ThreadPoolExecutor 包装，
        每 0.5s 检查取消标志，最多等 15s
      → 用户取消后最多 ~0.5s 即可中断当前操作，无需等满 30s

    【审核规则汇总（全部为硬性门槛，任一不满足即跳过）】
      ├─ 指标代码不存在或无权 → error + done
      ├─ 指标执行失败         → skip (indicator_error)
      ├─ 无 buy 信号          → skip (no_buy_signal)
      ├─ 现价 > 买点价格      → skip (price_above_buy)
      ├─ 买点超过3个交易日    → skip (buy_too_old)
      ├─ 策略无卖点信号       → skip (no_sell_signal) — 交易逻辑不完整
      ├─ 买点价 > 卖点价      → skip (buy_after_sell) — 策略逻辑异常
      ├─ 买点日期 < 卖点日期  → skip (buy_before_sell) — 时间线异常
      ├─ 有负面新闻(<=0分)    → skip (negative_news)
      ├─ 多周期回测不通过     → skip (backtest_failed)
      │   所有周期必须同时满足：收益率 > 0% 且 胜率 >= 70%
      │   任一周期不满足即跳过
      ├─ 加自选失败           → skip (add_failed)
      └─ 全部通过             → 加入自选 (passed)

    【多周期回测（前置硬性门槛）】
      加入自选之前，按 review_mode(短线/中线/长线) 对应的所有周期进行回测。
      每个周期都必须满足：收益率 > 0% 且 胜率 >= 70%，否则直接跳过。
      短线: 6月线, 3月线, 1月线, 1h线
      中线: 年线, 6月线, 3月线
      长线: 6M, 1年, 3年

    【参数】
      review_mode: 审核模式 "short"/"mid"/"long"，默认 "mid"(中线)
    """
    # ── 预初始化所有变量，防止 GeneratorExit 时 except 块引用未定义变量 ──
    total = len(stocks)
    added_count = 0
    skipped_count = 0
    idx = 0
    cancelled = _cancelled or [False]

    # review_mode 标签映射
    _mode_labels = {"short": "短线", "mid": "中线", "long": "长线"}
    review_mode_label = _mode_labels.get(review_mode, "中线")

    # ── 预检：指标代码是否存在 ──
    try:
        indicator_code = _get_indicator_code(indicator_id, user_id)
    except Exception as e:
        logger.error(f"[review] _get_indicator_code failed: {e}", exc_info=True)
        yield _sse({"type": "error", "msg": f"获取指标代码失败: {e}"})
        return

    logger.info(f"review_stocks: indicator_id={indicator_id}, user_id={user_id}, code_len={len(indicator_code) if indicator_code else 0}")
    if not indicator_code:
        yield _sse({"type": "error", "msg": f"指标ID {indicator_id} 不存在或无权访问"})
        return

    # 提取指标名称，用于进度提示
    try:
        indicator_name = _extract_indicator_name(indicator_code)
    except Exception:
        indicator_name = None
    logger.info(f"[review] 开始审核: indicator={indicator_name or '(unnamed)'}, stocks={total}")

    try:
        for idx, stock in enumerate(stocks):
            # ── 检查是否已取消 ──
            if cancelled[0]:
                logger.info(f"[review] cancelled before stock {idx+1}/{total}")
                break

            symbol = str(stock.get("code") or stock.get("symbol") or "").strip()
            market = str(stock.get("market") or "CNStock").strip()
            name = str(stock.get("name") or "").strip()

            # 跳过无代码的空行
            if not symbol:
                skipped_count += 1
                continue

            # ── 通知前端：开始检查这只股票 ──
            yield _sse({
                "type": "progress",
                "symbol": symbol,
                "market": market,
                "name": name,
                "index": idx + 1,
                "total": total,
                "status": "checking",
                "msg": f"正在按照{indicator_name or '指标策略'}审核{market}:{symbol}股票...",
            })

            # ── Step 1: 运行指标分析 ──
            # 拉K线 → 沙箱执行指标代码 → 提取 buy 信号
            logger.info(f"[review] {idx+1}/{total} {symbol} 开始指标分析")
            try:
                indicator_result = _run_indicator_on_stock(
                    indicator_code, market, symbol, user_params, _cancelled=cancelled
                )
                # 检查是否在指标执行过程中被取消
                if indicator_result.get("cancelled"):
                    logger.info(f"[review] {idx+1}/{total} {symbol} 指标执行中被取消")
                    break
                logger.info(f"[review] {idx+1}/{total} {symbol} success={indicator_result['success']} has_buy={indicator_result['has_buy_signal']} buy_price={indicator_result['buy_price']} current={indicator_result['current_price']}")
            except Exception as e:
                logger.error(f"Review indicator failed for {symbol}: {e}", exc_info=True)
                yield _sse({
                    "type": "result", "symbol": symbol, "market": market, "name": name,
                    "index": idx + 1, "total": total, "added": False,
                    "reason": "indicator_error",
                    "msg": f"{symbol} 指标执行异常: {str(e)}",
                })
                skipped_count += 1
                continue

            if not indicator_result["success"]:
                yield _sse({
                    "type": "result", "symbol": symbol, "market": market, "name": name,
                    "index": idx + 1, "total": total, "added": False,
                    "reason": "indicator_error",
                    "msg": f"{symbol} {indicator_result['error']}",
                })
                skipped_count += 1
                continue

            current_price = indicator_result["current_price"]
            buy_price = indicator_result["buy_price"]

            # ── Step 2: 买点信号判断 ──
            # 无信号 → 跳过
            if not indicator_result["has_buy_signal"]:
                yield _sse({
                    "type": "result", "symbol": symbol, "market": market, "name": name,
                    "index": idx + 1, "total": total, "added": False,
                    "reason": "no_buy_signal",
                    "msg": f"{symbol} 股票没出现买点",
                })
                skipped_count += 1
                continue

            # 现价高于买点 → 跳过
            if current_price is not None and buy_price is not None and current_price > buy_price:
                yield _sse({
                    "type": "result", "symbol": symbol, "market": market, "name": name,
                    "index": idx + 1, "total": total, "added": False,
                    "reason": "price_above_buy",
                    "msg": f"{symbol} 现价({current_price:.2f})高于买点价格({buy_price:.2f})，未出现买点",
                })
                skipped_count += 1
                continue

            # 买点时效性检查：最新买点不超过3个交易日
            buy_date_str = indicator_result.get("buy_date") or ""
            executed_df = indicator_result.get("_executed_df")
            if buy_date_str and executed_df is not None and "buy" in executed_df.columns:
                try:
                    buy_series = executed_df["buy"].astype(bool)
                    if buy_series.any():
                        last_buy_idx = buy_series[buy_series].index.tolist()[-1]
                        if not _is_buy_recency_valid(executed_df, last_buy_idx, max_trading_days=3):
                            yield _sse({
                                "type": "result", "symbol": symbol, "market": market, "name": name,
                                "index": idx + 1, "total": total, "added": False,
                                "reason": "buy_too_old",
                                "msg": f"{symbol} 最新买点({buy_date_str})超过3个交易日，已过期",
                            })
                            skipped_count += 1
                            continue
                except Exception:
                    pass  # 提取失败不阻断流程

            # ── 买卖逻辑校验（合并价格+日期，必须同时通过） ──
            sell_price = indicator_result.get("sell_price")
            sell_date_str = indicator_result.get("sell_date") or ""
            buy_date_str = indicator_result.get("buy_date") or ""

            # 策略必须有卖点信号，否则逻辑不完整
            if sell_price is None:
                yield _sse({
                    "type": "result", "symbol": symbol, "market": market, "name": name,
                    "index": idx + 1, "total": total, "added": False,
                    "reason": "no_sell_signal",
                    "msg": f"{symbol} 策略缺少卖点信号，交易逻辑不完整",
                })
                skipped_count += 1
                continue

            # 买点价 > 卖点价 → 策略反了
            if buy_price is not None and buy_price > sell_price:
                yield _sse({
                    "type": "result", "symbol": symbol, "market": market, "name": name,
                    "index": idx + 1, "total": total, "added": False,
                    "reason": "buy_after_sell",
                    "msg": f"{symbol} 策略逻辑异常：买点价({buy_price:.2f})高于卖点价({sell_price:.2f})，不符合基本交易逻辑",
                })
                skipped_count += 1
                continue

            # 买点日期 < 卖点日期 → 时间线反了
            if buy_date_str and sell_date_str:
                try:
                    buy_dt = datetime.strptime(buy_date_str, "%Y-%m-%d")
                    sell_dt = datetime.strptime(sell_date_str, "%Y-%m-%d")
                    if buy_dt < sell_dt:
                        yield _sse({
                            "type": "result", "symbol": symbol, "market": market, "name": name,
                            "index": idx + 1, "total": total, "added": False,
                            "reason": "buy_before_sell",
                            "msg": f"{symbol} 策略逻辑异常：买点日期({buy_date_str})在卖点日期({sell_date_str})之前，不符合基本交易逻辑",
                        })
                        skipped_count += 1
                        continue
                except ValueError:
                    pass

            # ── Step 3: 搜索新闻 ──
            yield _sse({
                "type": "progress", "symbol": symbol, "market": market, "name": name,
                "index": idx + 1, "total": total,
                "status": "checking_news",
                "msg": f"{symbol} 出现买点，正在搜索新闻...",
            })

            logger.info(f"[review] {idx+1}/{total} {symbol} 搜索新闻中...")
            news_result = _search_stock_news_sentiment(symbol, name, _cancelled=cancelled)
            # 检查是否在新闻搜索过程中被取消
            if cancelled[0]:
                logger.info(f"[review] {idx+1}/{total} {symbol} 新闻搜索中被取消")
                break
            logger.info(f"[review] {idx+1}/{total} {symbol} news: has_news={news_result['has_news']} direction={news_result['direction']} score={news_result['composite_score']}")

            # ── Step 4: 新闻情感判断 ──
            # 负面新闻 + 综合评分 <= 0 → 不加入
            if news_result["has_news"]:
                direction = news_result["direction"]
                score = news_result["composite_score"]

                if direction in ("利空", "偏利空") and score <= 0:
                    yield _sse({
                        "type": "result", "symbol": symbol, "market": market, "name": name,
                        "index": idx + 1, "total": total, "added": False,
                        "reason": "negative_news",
                        "msg": f"{symbol} 存在负面新闻(评分:{score:.1f}, {direction})，不加入自选",
                    })
                    skipped_count += 1
                    continue

            # ── 基本检查全部通过，提示用户 ──
            yield _sse({
                "type": "progress", "symbol": symbol, "market": market, "name": name,
                "index": idx + 1, "total": total,
                "status": "basic_passed",
                "msg": f"{symbol} 基本检测通过，正在进行多周期回测验证...",
            })

            # ── Step 5: 多周期回测（前置硬性门槛） ──
            # 所有周期必须同时满足：收益率 > 0% 且 胜率 >= 70%
            # 任一不满足 → 跳过，不加入自选
            yield _sse({
                "type": "progress", "symbol": symbol, "market": market, "name": name,
                "index": idx + 1, "total": total,
                "status": "backtesting",
                "msg": f"{symbol} 正在进行{review_mode_label}多周期回测...",
            })

            bt_pass = True
            bt_fail_reason = ""
            bt_msg_parts = []
            try:
                tf_cfg = REVIEW_TIMEFRAMES.get(review_mode, REVIEW_TIMEFRAMES["mid"])
                now = datetime.now()

                for period_cfg in tf_cfg["periods"]:
                    # 检查取消（每个周期之间）
                    if cancelled[0]:
                        logger.info(f"[review] {idx+1}/{total} {symbol} 回测中被取消")
                        break

                    tf = period_cfg["tf"]
                    months = period_cfg["months"]
                    label = period_cfg["label"]
                    end_date = now
                    start_date = end_date - timedelta(days=months * 30)

                    try:
                        bt_result = _run_backtest(
                            cancelled=cancelled,
                            indicator_code=indicator_code,
                            market=market,
                            symbol=symbol,
                            timeframe=tf,
                            start_date=start_date,
                            end_date=end_date,
                            initial_capital=100000.0,
                            commission=0.001,
                            trade_direction="long",
                            indicator_params=user_params,
                            user_id=user_id,
                            indicator_id=indicator_id,
                        )

                        if bt_result is None:
                            if cancelled[0]:
                                break
                            bt_msg_parts.append(f"{label}:无结果")
                            bt_pass = False
                            bt_fail_reason = f"{label}回测无结果"
                            continue

                        win_rate = (bt_result or {}).get("winRate", 0) or 0
                        total_return = (bt_result or {}).get("totalReturn", 0) or 0

                        period_ok = (win_rate >= BACKTEST_MIN_WIN_RATE and total_return > BACKTEST_MIN_RETURN)
                        status_mark = "✓" if period_ok else "✗"
                        bt_msg_parts.append(
                            f"{label}:{status_mark} 收益{round(total_return, 2)}% 胜率{round(win_rate, 2)}%"
                        )

                        if not period_ok:
                            bt_pass = False
                            if not bt_fail_reason:
                                reasons = []
                                if total_return <= BACKTEST_MIN_RETURN:
                                    reasons.append(f"收益率{round(total_return, 2)}%≤0")
                                if win_rate < BACKTEST_MIN_WIN_RATE:
                                    reasons.append(f"胜率{round(win_rate, 2)}%<{BACKTEST_MIN_WIN_RATE}%")
                                bt_fail_reason = f"{label}: {', '.join(reasons)}"

                    except Exception as period_err:
                        logger.warning(f"[review] {symbol} {label}({tf}) 回测失败: {period_err}")
                        bt_msg_parts.append(f"{label}:异常")
                        bt_pass = False
                        if not bt_fail_reason:
                            bt_fail_reason = f"{label}回测异常: {str(period_err)}"

            except Exception as bt_err:
                logger.warning(f"[review] {idx+1}/{total} {symbol} 回测失败: {bt_err}")
                bt_pass = False
                bt_fail_reason = f"回测异常: {str(bt_err)}"

            bt_summary = " | ".join(bt_msg_parts) if bt_msg_parts else "回测无结果"

            # 回测未通过 → 跳过，不加入自选
            if cancelled[0]:
                logger.info(f"[review] {idx+1}/{total} {symbol} 回测中被取消")
                break

            if not bt_pass:
                yield _sse({
                    "type": "result", "symbol": symbol, "market": market, "name": name,
                    "index": idx + 1, "total": total, "added": False,
                    "reason": "backtest_failed",
                    "backtest_summary": bt_summary,
                    "backtest_mode": review_mode,
                    "msg": f"{symbol} 回测未通过: {bt_fail_reason} | {bt_summary}",
                })
                skipped_count += 1
                continue

            # ── Step 6: 全部回测通过 → 加入自选股 ──
            logger.info(f"[review] {idx+1}/{total} {symbol} 回测全部通过，加入自选股...")
            success = _add_to_watchlist(user_id, market, symbol, name)
            logger.info(f"[review] {idx+1}/{total} {symbol} add_result={success}")
            if not success:
                yield _sse({
                    "type": "result", "symbol": symbol, "market": market, "name": name,
                    "index": idx + 1, "total": total, "added": False,
                    "reason": "add_failed",
                    "msg": f"{symbol} 加入自选股失败",
                })
                skipped_count += 1
                continue
            added_count += 1

            yield _sse({
                "type": "result", "symbol": symbol, "market": market, "name": name,
                "index": idx + 1, "total": total, "added": True,
                "reason": "passed",
                "backtest_summary": bt_summary,
                "backtest_mode": review_mode,
                "msg": f"{symbol} 通过审核 | {bt_summary}",
            })

        # ── 全部处理完毕 ──
        yield _sse({
            "type": "done",
            "total": total,
            "added": added_count,
            "skipped": skipped_count,
            "msg": f"审核完成：共{total}只，通过{added_count}只，跳过{skipped_count}只",
        })

    except GeneratorExit:
        logger.info(f"[review] client disconnected at {idx+1}/{total}")
        return  # 不要 yield — 连接已断开，无处可发
    except Exception as e:
        logger.error(f"[review] unexpected error at {idx+1}/{total}: {e}", exc_info=True)
        return  # 不要 yield — 出错时 yield 可能导致二次异常
