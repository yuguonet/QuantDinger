# -*- coding: utf-8 -*-
"""
腾讯财经数据源 Provider

模块职责:
  通过腾讯财经 API 获取 A股/港股 的 K线和实时行情数据。
  腾讯是国内访问速度最快、最稳定的数据源之一，作为 A股首选源（priority=10）。

能力:
  - K线: 全周期（1m/5m/15m/30m/1H/1D/1W），支持前/后复权
  - 单只行情: 实时行情快照
  - 批量行情: 单次HTTP获取多只股票行情（每批最多500只）
  - 港股: 通过 fqkline API 获取港股K线

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → TencentDataSource（本模块）

关键依赖:
  - requests: HTTP 请求
  - app.data_sources.normalizer: 股票代码标准化
  - app.data_sources.rate_limiter: 限流器 + 请求头 + 重试装饰器
"""

from __future__ import annotations

import itertools
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from app.data_sources.normalizer import (
    to_tencent_code, normalize_hk_code,
)
from app.data_sources.rate_limiter import (
    get_request_headers, retry_with_backoff, get_tencent_limiter,
)
from app.data_sources.provider import register
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# Referer 轮换池 — 提高访问成功率
# ================================================================

class _RefererPool:
    """
    线程安全的 Referer 轮换池。

    每次调用 next() 返回下一个 Referer，循环轮换。
    不同 API 端点使用不同的 Referer 池，模拟不同来源的访问。

    Args:
        referers: Referer 列表
    """

    def __init__(self, referers: List[str]):
        self._referers = referers
        self._cycle = itertools.cycle(referers)
        self._lock = threading.Lock()

    def next(self) -> str:
        """返回下一个 Referer"""
        with self._lock:
            return next(self._cycle)


# 腾讯 K线接口 Referer 池
_tc_kline_referers = _RefererPool([
    "https://gu.qq.com/",
    "https://finance.qq.com/",
    "https://stockapp.finance.qq.com/",
    "https://stock.qq.com/",
])

# 腾讯行情接口 Referer 池
_tc_quote_referers = _RefererPool([
    "https://qt.gtimg.cn/",
    "https://gu.qq.com/",
    "https://finance.qq.com/",
    "https://stockapp.finance.qq.com/",
])


def _lower(code: str) -> str:
    """将股票代码转为小写并去除首尾空格，用于腾讯API参数"""
    return (code or "").strip().lower()


# 内部周期 → 腾讯API参数映射
# 格式: (API端点类型, 腾讯周期参数)
#   mkline: 分钟级K线接口
#   fqkline: 复权日/周K线接口
_TF_MAP = {
    "1m": ("mkline", "m1"),   "5m": ("mkline", "m5"),
    "15m": ("mkline", "m15"), "30m": ("mkline", "m30"),
    "1H": ("mkline", "m60"),
    "1D": ("fqkline", "day"), "1W": ("fqkline", "week"),
}


def _parse_time(ds: str) -> Optional[int]:
    """
    解析时间字符串为 Unix 时间戳。

    支持多种格式: "2024-01-01 10:30:00", "2024-01-01", "2024/01/01",
    以及毫秒级时间戳（自动转换为秒级）。

    Args:
        ds: 时间字符串或时间戳字符串

    Returns:
        Unix 时间戳（秒），解析失败返回 None
    """
    raw = str(ds or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return int(datetime.strptime(raw, fmt).timestamp())
        except ValueError:
            continue
    # 兼容时间戳（可能是秒级或毫秒级）
    try:
        ts = int(float(raw))
        return int(ts / 1000) if ts > 10**12 else ts
    except Exception:
        return None


def _rows_to_dicts(rows: list) -> List[Dict[str, Any]]:
    """
    将腾讯API返回的原始行数据转换为标准化K线字典列表。

    腾讯API返回格式: [[date, open, close, high, low, volume, ...], ...]
    注意: 腾讯的字段顺序是 [日期, 开盘, 收盘, 最高, 最低, 成交量]，
    与标准的 OHLC 顺序不同（收盘在第2位，不是第4位）。

    Args:
        rows: 腾讯API返回的原始二维数组

    Returns:
        标准化K线字典列表，每个元素包含 time/open/high/low/close/volume
    """
    out = []
    for r in rows:
        # 跳过格式不正确的行
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            continue
        ts = _parse_time(r[0])
        if ts is None:
            continue
        try:
            # 腾讯API字段顺序: [日期, 开盘, 收盘, 最高, 最低, 成交量]
            o, c, h, low, vol = float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
        except (TypeError, ValueError):
            continue
        out.append({
            "time": ts, "open": round(o, 4), "high": round(h, 4),
            "low": round(low, 4), "close": round(c, 4), "volume": round(vol, 2),
        })
    return out


@register(priority=10)
class TencentDataSource:
    """
    腾讯财经数据源 — A股首选数据源（priority=10）。

    能力:
      - K线: 全周期（分钟/日/周），通过 mkline 和 fqkline 两个API
      - 行情: 单只实时行情（qt.gtimg.cn）
      - 批量行情: 单次HTTP获取多只（最多500只/批）
      - 港股: 支持港股K线和行情

    线程安全性:
      - 实例方法无状态，线程安全
      - 通过 get_tencent_limiter() 进行全局限流
      - @retry_with_backoff 自动重试失败请求

    限流策略:
      通过 get_tencent_limiter() 获取全局限流器，控制请求频率。
    """

    name = "tencent"
    priority = 10

    capabilities = {
        "kline": True,
        "kline_priority": 15,
        "kline_tf": {"1m", "5m", "15m", "30m", "1H", "1D", "1W"},
        "quote": True,
        "quote_priority": 10,
        "batch_quote": True,
        "batch_quote_priority": 10,
        "hk": True,
        "markets": {"CNStock", "HKStock"},
    }

    @retry_with_backoff(max_attempts=3, base_delay=1.2, max_delay=8.0, exceptions=(Exception,))
    def fetch_kline(
        self, code: str, timeframe: str = "1D", count: int = 300,
        adj: str = "qfq", timeout: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        获取单只股票K线数据。

        根据 timeframe 选择不同的API端点:
          - 分钟线: proxy.finance.qq.com 的 mkline 接口
          - 日/周线: web.ifzq.gtimg.cn 的 fqkline 接口

        Args:
            code:      股票代码（如 "sh600519"）
            timeframe: K线周期（如 "1D", "5m"）
            count:     请求数据条数
            adj:       复权方式（腾讯 fqkline 接口自动处理复权）
            timeout:   请求超时秒数

        Returns:
            K线数据列表，失败返回空列表
        """
        c = _lower(code)
        if not c:
            return []

        # 根据周期选择API端点和参数
        endpoint, tc_tf = _TF_MAP.get(timeframe, (None, None))
        if not endpoint:
            return []

        # 限流等待
        limiter = get_tencent_limiter()
        limiter.wait()

        # 构建请求URL和参数
        if endpoint == "mkline":
            # 分钟级K线接口
            url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline"
            params = {"param": f"{c},{tc_tf},{int(count)}"}
        else:
            # 日/周级复权K线接口
            url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            params = {"param": f"{c},{tc_tf},,,{int(count)}"}

        resp = requests.get(
            url, headers=get_request_headers(referer=_tc_kline_referers.next()),
            params=params, timeout=timeout,
        )

        # 解析JSON响应
        try:
            data = resp.json()
        except Exception:
            return []

        if not isinstance(data, dict) or int(data.get("code", 0)) != 0:
            return []

        # 提取 data.{code} 下的K线数据
        root = (data.get("data") or {}).get(c)
        if not isinstance(root, dict):
            return []

        # 从响应中提取K线行数据
        # mkline: 直接按周期 key 取值（如 root["m5"]）
        # fqkline: 按周期 key 取值，可能需要遍历查找
        rows = None
        if endpoint == "mkline":
            rows = root.get(tc_tf)
        else:
            arr = root.get(tc_tf)
            if isinstance(arr, list) and arr:
                rows = arr
            # fqkline 的 key 可能带复权前缀，遍历查找
            if rows is None:
                for k, v in root.items():
                    if isinstance(v, list) and v and str(k).lower().endswith(tc_tf):
                        rows = v
                        break

        return _rows_to_dicts(rows) if isinstance(rows, list) else []

    @retry_with_backoff(max_attempts=3, base_delay=1.2, max_delay=8.0, exceptions=(Exception,))
    def fetch_quote(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """
        获取单只股票实时行情。

        通过腾讯 qt.gtimg.cn 接口获取行情快照。
        返回的文本格式为: v_sh600519="1~贵州茅台~600519~1800.00~..."
        字段以 ~ 分隔，需要按索引提取。

        Args:
            code:    股票代码
            timeout: 请求超时秒数

        Returns:
            行情字典，包含 last/change/changePercent/high/low/open/previousClose/name/symbol
        """
        c = _lower(code)
        if not c:
            return None

        get_tencent_limiter().wait()
        resp = requests.get(
            f"https://qt.gtimg.cn/q={c}",
            headers=get_request_headers(referer=_tc_quote_referers.next()),
            timeout=timeout,
        )
        try:
            resp.encoding = "gbk"
        except Exception:
            pass

        text = (resp.text or "").strip()
        if not text or "~" not in text:
            return None

        # 解析响应文本，提取 ~ 分隔的字段
        try:
            start = text.index('="') + 2
            end = text.rindex('"')
            parts = text[start:end].split("~")
        except Exception:
            return None

        if len(parts) < 6:
            return None

        def _f(i, d=0.0):
            """安全提取浮点字段，越界或空值返回默认值"""
            try:
                return float(parts[i]) if i < len(parts) and parts[i] else d
            except Exception:
                return d

        # 腾讯行情字段索引:
        # parts[1] = 名称, parts[2] = 代码, parts[3] = 最新价, parts[4] = 昨收
        # parts[5] = 开盘, parts[33] = 最高, parts[34] = 最低
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

    def fetch_quotes_batch(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        """
        批量获取多只股票实时行情 — 单次HTTP请求。

        通过逗号拼接多个代码，一次请求获取所有行情。
        每批最多500只（避免URL过长）。

        Args:
            codes:   股票代码列表
            timeout: 请求超时秒数

        Returns:
            {code: quote_dict} — 仅包含成功获取的代码
        """
        if not codes:
            return {}
        lowered = [_lower(c) for c in codes if c]
        if not lowered:
            return {}

        result: Dict[str, Dict[str, Any]] = {}
        batch_size = 500  # 腾讯单次最多500只

        # 分批请求（每批500只）
        for i in range(0, len(lowered), batch_size):
            batch = lowered[i:i + batch_size]
            get_tencent_limiter().wait()
            try:
                resp = requests.get(
                    f"https://qt.gtimg.cn/q={','.join(batch)}",
                    headers=get_request_headers(referer=_tc_quote_referers.next()),
                    timeout=timeout,
                )
                resp.encoding = "gbk"
            except Exception:
                continue

            # 逐行解析响应（每行一只股票）
            for line in (resp.text or "").strip().split("\n"):
                line = line.strip().rstrip(";")
                if "=" not in line or '""' in line:
                    continue
                try:
                    var_name, data = line.split("=", 1)
                    parts = data.strip('"').split("~")
                    if len(parts) < 6 or not parts[1]:
                        continue
                    # 匹配代码: 变量名中包含代码
                    for c in batch:
                        if c in var_name:
                            last = float(parts[3]) if parts[3] else 0
                            if last <= 0:
                                break
                            prev = float(parts[4]) if parts[4] else 0
                            chg = round(last - prev, 4) if prev else 0
                            result[c] = {
                                "last": last, "change": chg,
                                "changePercent": round(chg / prev * 100, 2) if prev else 0,
                                "high": float(parts[33]) if len(parts) > 33 and parts[33] else last,
                                "low": float(parts[34]) if len(parts) > 34 and parts[34] else last,
                                "open": float(parts[5]) if parts[5] else last,
                                "previousClose": prev,
                                "name": parts[1].strip(),
                                "symbol": parts[2].strip(),
                            }
                            break
                except Exception:
                    continue

        return result
