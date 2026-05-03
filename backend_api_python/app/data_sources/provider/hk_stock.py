# -*- coding: utf-8 -*-
"""
港股数据源 Provider

降级链（国内优先）:
  日/周线 → 腾讯 fqkline → yfinance → AkShare → Twelve Data
  分钟线  → yfinance → AkShare → Twelve Data

能力: K线(全周期) / 单只行情(腾讯)
熔断保护: 海外源熔断器 (2次失败 / 15min冷却)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.data_sources.normalizer import normalize_hk_code
from app.data_sources.rate_limiter import (
    get_request_headers, get_tencent_limiter,
)
from app.data_sources.provider import register
from app.data_sources.circuit_breaker import get_overseas_circuit_breaker
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 内部工具 — 腾讯港股K线解析
# ================================================================

def _fetch_tencent_hk_kline(
    code: str, period: str, count: int, adj: str = "qfq", timeout: int = 10,
) -> List[Dict[str, Any]]:
    """
    通过腾讯 fqkline API 获取港股K线数据。

    解析逻辑:
      1. 请求 web.ifzq.gtimg.cn 的 fqkline 接口
      2. 优先查找带复权前缀的键 (如 "qfqday")，回退到不带前缀的键 ("day")
      3. 遍历 rows 解析为标准 K线格式

    Args:
        code:    腾讯格式港股代码 (如 "hk00700")
        period:  周期 ("day" / "week")
        count:   数据条数
        adj:     复权方式 ("qfq" / "hfq" / "")
        timeout: 请求超时 (秒)

    Returns:
        K线列表，每条包含 time/open/high/low/close/volume
    """
    import requests
    from datetime import datetime

    get_tencent_limiter().wait()

    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{code},{period},,,{int(count)}"}

    resp = requests.get(
        url,
        headers=get_request_headers(referer="https://gu.qq.com/"),
        params=params, timeout=timeout,
    )

    try:
        data = resp.json()
    except Exception:
        return []

    if not isinstance(data, dict) or int(data.get("code", 0)) != 0:
        return []

    root = (data.get("data") or {}).get(code)
    if not isinstance(root, dict):
        return []

    rows = None
    for key in ([f"{adj}{period}", period] if adj else [period]):
        arr = root.get(key)
        if isinstance(arr, list) and arr:
            rows = arr
            break
    if rows is None:
        for k, v in root.items():
            if isinstance(v, list) and v and str(k).lower().endswith(period):
                rows = v
                break

    if not isinstance(rows, list):
        return []

    out = []
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            continue
        try:
            dt_str = str(r[0]).strip()
            ts = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
                try:
                    ts = int(datetime.strptime(dt_str, fmt).timestamp())
                    break
                except ValueError:
                    continue
            if ts is None:
                continue
            o, c, h, low, vol = float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
            out.append({
                "time": ts, "open": round(o, 4), "high": round(h, 4),
                "low": round(low, 4), "close": round(c, 4), "volume": round(vol, 2),
            })
        except (ValueError, TypeError, IndexError):
            continue

    out.sort(key=lambda x: x["time"])
    return out[-count:] if len(out) > count else out


def _fetch_tencent_hk_quote(code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
    """
    通过腾讯行情接口获取港股实时行情。

    解析 qt.gtimg.cn 返回的 "~" 分隔文本:
      - parts[1]: 股票名称
      - parts[2]: 股票代码
      - parts[3]: 最新价
      - parts[4]: 昨收价
      - parts[5]: 开盘价
      - parts[33]: 最高价
      - parts[34]: 最低价

    Args:
        code:    腾讯格式港股代码 (如 "hk00700")
        timeout: 请求超时 (秒)

    Returns:
        行情字典 (last/change/changePercent/high/low/open/previousClose/name/symbol)
    """
    import requests

    get_tencent_limiter().wait()
    resp = requests.get(
        f"https://qt.gtimg.cn/q={code}",
        headers=get_request_headers(referer="https://qt.gtimg.cn/"),
        timeout=timeout,
    )
    try:
        resp.encoding = "gbk"
    except Exception:
        pass

    text = (resp.text or "").strip()
    if not text or "~" not in text:
        return None

    try:
        start = text.index('="') + 2
        end = text.rindex('"')
        parts = text[start:end].split("~")
    except Exception:
        return None

    if len(parts) < 6:
        return None

    def _f(i, d=0.0):
        """安全获取 parts[i] 并转 float，越界/异常返回默认值"""
        try:
            return float(parts[i]) if i < len(parts) and parts[i] else d
        except Exception:
            return d

    last, prev = _f(3), _f(4)
    chg = round(last - prev, 4) if prev else 0
    return {
        "last": last, "change": chg,
        "changePercent": round(chg / prev * 100, 2) if prev else 0,
        "high": _f(33, last), "low": _f(34, last),
        "open": _f(5) or last, "previousClose": prev,
        "name": (parts[1] or "").strip(),
        "symbol": (parts[2] or "").strip(),
    }


# ================================================================
# Provider
# ================================================================

@register(priority=40)
class HKStockDataSource:
    """
    港股数据源 — 腾讯直连 + 海外源降级。

    降级策略:
      日/周线: 腾讯 fqkline → yfinance → AkShare → Twelve Data
      分钟线:  yfinance → AkShare → Twelve Data

    熔断保护:
      海外源使用独立熔断器 (2次失败 / 15min冷却)，
      避免海外源故障拖慢整个港股数据获取。

    Attributes:
        name:     数据源名称 "hk_stock"
        priority: 优先级 40 (腾讯直连优先，海外源兜底)
        cb:       海外源熔断器实例
    """

    name = "hk_stock"
    priority = 60

    capabilities = {
        "kline": True,
        "kline_priority": 60,
        "kline_tf": {"1m", "5m", "15m", "30m", "1H", "1D", "1W"},
        "quote": True,
        "quote_priority": 60,
        "batch_quote": False,
        "hk": True,
        "markets": {"HKStock"},
    }

    def __init__(self):
        """初始化港股数据源，获取海外源熔断器实例"""
        self.cb = get_overseas_circuit_breaker()

    def fetch_quote(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """
        获取港股实时行情 — 直接调用腾讯接口。

        Args:
            code:    港股代码 (任意格式，自动标准化为 hk00700)
            timeout: 请求超时 (秒)

        Returns:
            行情字典，失败返回 None
        """
        hk_code = normalize_hk_code(code)
        if not hk_code:
            return None
        return _fetch_tencent_hk_quote(hk_code, timeout)

    def fetch_kline(
        self, code: str, timeframe: str = "1D", count: int = 300,
        adj: str = "qfq", timeout: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        获取港股K线 — 降级链策略。

        流程:
          1. 日/周线优先用腾讯 fqkline (国内直连，速度快)
          2. 腾讯失败 → 依次尝试 yfinance / AkShare / Twelve Data
          3. 每个源成功后记录熔断器成功，失败记录失败

        Args:
            code:      港股代码 (任意格式)
            timeframe: 周期 ("1D" / "1W" / "1m" / "5m" 等)
            count:     数据条数
            adj:       复权方式
            timeout:   请求超时 (秒)

        Returns:
            K线列表，全部源失败返回空列表
        """
        hk_code = normalize_hk_code(code)
        if not hk_code:
            return []
        lim = max(int(count or 300), 1)

        # 日/周线: 先尝试腾讯 fqkline (国内直连)
        if timeframe in ("1D", "1W"):
            tf_map = {"1D": "day", "1W": "week"}
            period = tf_map.get(timeframe, "day")
            bars = _fetch_tencent_hk_kline(hk_code, period, lim, adj, timeout)
            if bars:
                self.cb.record_success(self.name)
                return bars

        # 降级链: yfinance → AkShare → Twelve Data
        bars = self._try_yfinance(hk_code, timeframe, lim, timeout)
        if bars:
            self.cb.record_success(self.name)
            return bars

        bars = self._try_akshare(hk_code, timeframe, lim, timeout)
        if bars:
            self.cb.record_success(self.name)
            return bars

        bars = self._try_twelvedata(hk_code, timeframe, lim, timeout)
        if bars:
            self.cb.record_success(self.name)
            return bars

        return []

    def fetch_quotes_batch(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        """
        批量获取港股行情 — 逐只调用 (港股无原生批量接口)。

        Args:
            codes:   港股代码列表
            timeout: 每只请求超时 (秒)

        Returns:
            {code: 行情字典}，失败的不包含在结果中
        """
        result = {}
        for code in codes:
            q = self.fetch_quote(code, timeout)
            if q:
                result[code] = q
        return result

    def _try_yfinance(self, hk_code: str, timeframe: str, limit: int, timeout: int) -> List[Dict[str, Any]]:
        """
        降级尝试 1: 通过 yfinance 获取港股K线。

        yfinance 是海外源，使用独立熔断器保护。
        需要 asia_stock_kline 模块支持。
        """
        if not self.cb.is_available("yfinance"):
            return []
        try:
            from app.data_sources.asia_stock_kline import fetch_yfinance_klines
            rows = fetch_yfinance_klines(
                is_hk=True, tencent_code=hk_code,
                timeframe=timeframe, limit=limit,
            )
            if rows:
                return rows
        except ImportError:
            logger.debug("[港股] yfinance 不可用，跳过")
        except Exception as e:
            self.cb.record_failure("yfinance", str(e))
        return []

    def _try_akshare(self, hk_code: str, timeframe: str, limit: int, timeout: int) -> List[Dict[str, Any]]:
        """
        降级尝试 2: 通过 AkShare 获取港股K线。

        AkShare 是国内源，支持分钟线和周线。
        """
        if not self.cb.is_available("akshare"):
            return []
        try:
            from app.data_sources.asia_stock_kline import (
                fetch_akshare_minute_klines, fetch_akshare_weekly_klines,
            )
            if timeframe in ("1m", "5m", "15m", "30m", "1H", "4H"):
                rows = fetch_akshare_minute_klines(
                    is_hk=True, tencent_code=hk_code,
                    timeframe=timeframe, limit=limit,
                )
            elif timeframe == "1W":
                rows = fetch_akshare_weekly_klines(
                    is_hk=True, tencent_code=hk_code, limit=limit,
                )
            else:
                rows = []
            if rows:
                return rows
        except ImportError:
            logger.debug("[港股] AkShare 不可用，跳过")
        except Exception as e:
            self.cb.record_failure("akshare", str(e))
        return []

    def _try_twelvedata(self, hk_code: str, timeframe: str, limit: int, timeout: int) -> List[Dict[str, Any]]:
        """
        降级尝试 3: 通过 Twelve Data 获取港股K线。

        Twelve Data 是海外源，最终兜底。
        """
        if not self.cb.is_available("twelvedata"):
            return []
        try:
            from app.data_sources.asia_stock_kline import fetch_twelvedata_klines
            rows = fetch_twelvedata_klines(
                is_hk=True, tencent_code=hk_code,
                timeframe=timeframe, limit=limit,
            )
            if rows:
                return rows
        except ImportError:
            logger.debug("[港股] Twelve Data 不可用，跳过")
        except Exception as e:
            self.cb.record_failure("twelvedata", str(e))
        return []
