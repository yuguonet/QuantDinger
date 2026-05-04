# -*- coding: utf-8 -*-
"""
BaoStock 数据源 Provider — 基于 baostock SDK (TCP 协议)

模块职责:
  通过 BaoStock Python SDK 获取 A股的 K线和实时行情数据。
  BaoStock 使用 TCP 协议直连，不依赖 HTTP DNS 解析，稳定性更高。

能力:
  - K线: 日线 + 周线，支持前/后复权
  - 单只行情: 实时行情快照
  - 批量行情: 多只股票行情（逐只调用）

特点:
  - TCP 协议直连，无需 API Key，不走 HTTP
  - 数据来源可靠（证券宝）
  - 历史数据丰富，适合回测

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → BaoStockDataSource（本模块）

关键依赖:
  - baostock: BaoStock Python SDK (TCP 协议)
  - app.data_sources.normalizer: 股票代码标准化
  - app.data_sources.rate_limiter: 限流器

注意:
  baostock login 后约 1 分钟无操作会自动断开。
  本模块通过 _BaoStockSession 管理生命周期，每次请求前检查连接状态，
  超过 50 秒自动重新 login，避免请求时遇到断连。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.data_sources.normalizer import to_raw_digits, detect_market
from app.data_sources.rate_limiter import RateLimiter
from app.data_sources.provider import register
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 限流器
# ================================================================

# BaoStock SDK 限流：最小间隔 0.3s + 0.1~0.5s 随机抖动
# SDK 走 TCP 协议，比 HTTP 接口更快，限流可以适当放宽
_baostock_limiter = RateLimiter(
    min_interval=0.3,
    jitter_min=0.1,
    jitter_max=0.5,
)


# ================================================================
# BaoStock Session 生命周期管理
# ================================================================

class _BaoStockSession:
    """
    BaoStock SDK 会话管理器 — 线程安全 + 自动续期。

    baostock login 后约 1 分钟无操作会自动断开。
    本类维护一个全局共享的登录会话，每次请求前检查：
      - 未登录 → 自动 login
      - 已登录但超过 50 秒 → logout 后重新 login
      - 已登录且在有效期内 → 直接复用

    线程安全性:
      使用 threading.Lock 保护所有状态变更，多线程调用安全。
    """

    MAX_AGE = 55  # 秒，baostock 约 60 秒超时，留 5 秒余量

    def __init__(self):
        self._lock = threading.Lock()
        self._bs = None
        self._login_time = 0.0

    def _do_login(self):
        """执行 login（调用方需持有锁）"""
        try:
            import baostock as bs
        except ImportError:
            raise ImportError(
                "baostock 未安装，请执行: pip install baostock"
            )

        # 如果之前已登录，先 logout
        if self._bs is not None:
            try:
                self._bs.logout()
            except Exception:
                pass

        self._bs = bs
        lg = bs.login()
        if lg.error_code != '0':
            raise ConnectionError(
                f"BaoStock 登录失败: {lg.error_msg}"
            )
        self._login_time = time.time()
        logger.debug("[BaoStock] login 成功")

    def ensure_login(self):
        """确保会话有效，必要时重新 login"""
        with self._lock:
            now = time.time()
            if self._bs is None or (now - self._login_time) > self.MAX_AGE:
                self._do_login()

    @contextmanager
    def connection(self):
        """
        上下文管理器 — 确保会话有效，用完不 logout。

        用法:
            with session.connection():
                rs = bs.query_history_k_data_plus(...)
        """
        self.ensure_login()
        yield self._bs

    def logout(self):
        """主动 logout（通常不需要，由 __del__ 或进程退出自动清理）"""
        with self._lock:
            if self._bs is not None:
                try:
                    self._bs.logout()
                except Exception:
                    pass
                self._bs = None
                self._login_time = 0.0


# 全局共享会话（所有 BaoStockDataSource 实例共用）
_session = _BaoStockSession()


# ================================================================
# 代码格式转换
# ================================================================

def _to_baostock_code(code: str) -> str:
    """
    将股票代码转换为 BaoStock 格式: sh.600519 / sz.000001。

    Args:
        code: 任意格式的股票代码

    Returns:
        BaoStock 格式代码，无法识别返回空字符串
    """
    market, digits = detect_market(code)
    if not market or not digits:
        return ""
    return f"{market.lower()}.{digits}"


# BaoStock 周期映射
_BS_PERIOD = {
    "1D": "d", "1W": "w", "1M": "m",
}

# BaoStock 复权映射
_BS_ADJ = {"": "0", "qfq": "1", "hfq": "2"}


# ================================================================
# Provider
# ================================================================

@register(priority=40)
class BaoStockDataSource:
    """
    BaoStock 数据源 — 证券宝 SDK (TCP 协议, priority=40)。

    能力:
      - K线: 日线/周线，支持复权
      - 行情: 单只实时行情
      - 批量行情: 多只股票行情（逐只调用）

    线程安全性:
      - 共享 _BaoStockSession 实例，内部加锁，线程安全
      - 使用独立的限流器

    注意:
      BaoStock 主要提供历史K线数据，实时行情能力有限。
      分钟级K线不支持（BaoStock 仅提供日/周/月线）。
    """

    name = "baostock"
    priority = 40

    capabilities = {
        "kline": True,
        "kline_priority": 5,
        "kline_tf": {"1D", "1W"},
        "quote": True,
        "quote_priority": 50,
        "batch_quote": True,
        "batch_quote_priority": 50,
        "hk": False,
        "markets": {"CNStock"},
    }

    def fetch_kline(
        self, code: str, timeframe: str = "1D", count: int = 300,
        adj: str = "qfq", timeout: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        获取单只股票K线数据。

        通过 BaoStock SDK (TCP) 获取历史K线数据。
        BaoStock 仅支持日线、周线、月线，不支持分钟线。

        Args:
            code:      股票代码
            timeframe: K线周期
            count:     请求数据条数
            adj:       复权方式
            timeout:   请求超时秒数（SDK 无直接 timeout 参数，保留兼容）

        Returns:
            K线数据列表
        """
        bs_code = _to_baostock_code(code)
        if not bs_code:
            return []
        period = _BS_PERIOD.get(timeframe)
        if period is None:
            return []
        adj_type = _BS_ADJ.get(adj, "1")

        _baostock_limiter.wait()

        # 计算日期范围
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=int(count * 1.8))).strftime("%Y-%m-%d")

        fields = "date,open,high,low,close,volume,amount"

        for attempt in range(3):
            try:
                with _session.connection() as bs:
                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        fields,
                        start_date=start_date,
                        end_date=end_date,
                        frequency=period,
                        adjustflag=adj_type,
                    )
                    if rs.error_code != '0':
                        logger.warning(
                            "[BaoStock K线] 查询失败 %s: %s",
                            code, rs.error_msg,
                        )
                        return []

                    out: List[Dict[str, Any]] = []
                    while rs.next():
                        row = rs.get_row_data()
                        # row: [date, open, high, low, close, volume, amount]
                        if len(row) < 6:
                            continue
                        try:
                            dt_str = str(row[0]).strip()
                            if not dt_str:
                                continue
                            ts = int(datetime.strptime(dt_str, "%Y-%m-%d").timestamp())
                            o = float(row[1]) if row[1] and row[1] != '' else 0
                            c = float(row[2]) if row[2] and row[2] != '' else 0
                            h = float(row[3]) if row[3] and row[3] != '' else 0
                            low = float(row[4]) if row[4] and row[4] != '' else 0
                            v = float(row[5]) if len(row) > 5 and row[5] else 0
                            if o == 0 and c == 0:
                                continue
                            out.append({
                                "time": ts,
                                "open": round(o, 4),
                                "high": round(h, 4),
                                "low": round(low, 4),
                                "close": round(c, 4),
                                "volume": round(v, 2),
                            })
                        except (ValueError, TypeError, IndexError):
                            continue

                    out.sort(key=lambda x: x["time"])
                    return out[-count:] if len(out) > count else out

            except (ConnectionError, OSError) as e:
                logger.warning(
                    "[BaoStock K线] %s 连接异常(重试%d): %s",
                    code, attempt + 1, e,
                )
                # 连接异常：强制重连，不等待（重连本身已耗时）
                _session.logout()
                if attempt == 2:
                    return []
            except Exception as e:
                logger.warning("[BaoStock K线] %s 异常: %s", code, e)
                return []

        return []

    def fetch_quote(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """
        获取单只股票实时行情。

        通过 BaoStock SDK 获取最新行情快照。
        如果实时接口无数据，回退到最近一根日线。

        Args:
            code:    股票代码
            timeout: 请求超时秒数（保留兼容）

        Returns:
            行情字典，失败返回 None
        """
        bs_code = _to_baostock_code(code)
        if not bs_code:
            return None

        _baostock_limiter.wait()

        for attempt in range(3):
            try:
                with _session.connection() as bs:
                    # 先尝试实时行情接口
                    rs = bs.query_real_time_quotes(bs_code) if hasattr(bs, 'query_real_time_quotes') else None
                    if rs and hasattr(rs, 'error_code') and rs.error_code == '0' and rs.next():
                        row = rs.get_row_data()
                        # 实时行情字段
                        last = float(row[3]) if len(row) > 3 and row[3] else 0  # 最新价
                        prev = float(row[4]) if len(row) > 4 and row[4] else 0  # 昨收
                        open_p = float(row[5]) if len(row) > 5 and row[5] else 0
                        high = float(row[6]) if len(row) > 6 and row[6] else 0
                        low = float(row[7]) if len(row) > 7 and row[7] else 0
                        vol = float(row[8]) if len(row) > 8 and row[8] else 0
                        name = str(row[1]).strip() if len(row) > 1 else ""

                        if last > 0:
                            chg = round(last - prev, 4) if prev else 0.0
                            return {
                                "symbol": bs_code, "name": name,
                                "last": last, "change": chg,
                                "changePercent": round(chg / prev * 100, 2) if prev else 0.0,
                                "open": open_p, "high": high, "low": low,
                                "previousClose": prev, "volume": vol,
                            }

                    # 实时接口无数据，回退到最新日线
                    end_date = datetime.now().strftime("%Y-%m-%d")
                    start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                    rs2 = bs.query_history_k_data_plus(
                        bs_code,
                        "date,open,high,low,close,volume,amount",
                        start_date=start_date,
                        end_date=end_date,
                        frequency="d",
                        adjustflag="1",
                    )
                    if rs2.error_code != '0':
                        return None

                    last_row = None
                    while rs2.next():
                        last_row = rs2.get_row_data()

                    if last_row and len(last_row) >= 6:
                        last = float(last_row[4]) if last_row[4] else 0  # close
                        open_p = float(last_row[1]) if last_row[1] else 0
                        high = float(last_row[2]) if last_row[2] else 0
                        low = float(last_row[3]) if last_row[3] else 0
                        vol = float(last_row[5]) if last_row[5] else 0

                        if last > 0:
                            return {
                                "symbol": bs_code, "name": "",
                                "last": last, "change": 0.0,
                                "changePercent": 0.0,
                                "open": open_p, "high": high, "low": low,
                                "previousClose": 0.0, "volume": vol,
                            }
                    return None

            except (ConnectionError, OSError) as e:
                logger.warning(
                    "[BaoStock 行情] %s 连接异常(重试%d): %s",
                    code, attempt + 1, e,
                )
                _session.logout()
                if attempt == 2:
                    return None
            except Exception as e:
                logger.warning("[BaoStock 行情] %s 异常: %s", code, e)
                return None

        return None

    def fetch_quotes_batch(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        """
        批量获取多只股票实时行情。

        通过逐只调用 fetch_quote 实现。

        Args:
            codes:   股票代码列表
            timeout: 请求超时秒数

        Returns:
            {code: quote_dict}
        """
        if not codes:
            return {}
        result: Dict[str, Dict[str, Any]] = {}
        for c in codes:
            if not c:
                continue
            q = self.fetch_quote(c, timeout=timeout)
            if q:
                result[c] = q
        return result
