"""
A股数据接口扩展 — 为 CNStockDataSource 补充 interfaces 层所需的全部方法

继承 CNStockDataSource，添加:
- get_index_quotes()      → 指数实时行情 (东财→腾讯→新浪)
- get_market_snapshot()   → 市场涨跌统计 (东财全量→AkShare兜底)
- get_stock_info()        → 个股基本信息 (东财)
- get_all_stock_codes()   → 全部A股代码列表 (东财)
- get_stock_fund_flow()   → 个股资金流向 (东财)
- get_dragon_tiger()      → 龙虎榜 (可插拔多源)
- get_hot_rank()          → 热榜/人气榜 (可插拔多源)
- get_zt_pool()           → 涨停池 (可插拔多源)
- get_limit_down()        → 跌停池 (可插拔多源)
- get_broken_board()      → 炸板池 (可插拔多源)
- AShareDataHub           → 接口层统一入口 (组合所有 Interface 对象)

多源 fallback 链通过 ASTOCK_SOURCE_CHAIN 配置，新增数据源只需:
  1. 在 app/data_sources/ 下新建模块，实现对应 fetch_* 函数
  2. 在 ASTOCK_SOURCE_CHAIN 中加入 (name, module.fetch_func)
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from app.data_sources.cn_stock import CNStockDataSource, _fetch_with_timeout, _get_timeout
from app.data_sources.tencent import normalize_cn_code, fetch_quote
from app.data_sources.eastmoney import (
    _em_secid_from_cn,
    fetch_eastmoney_dragon_tiger,
    fetch_eastmoney_hot_rank,
    fetch_eastmoney_zt_pool,
    fetch_eastmoney_dt_pool,
    fetch_eastmoney_broken_board,
)
from app.data_sources.akshare import (
    fetch_akshare_dragon_tiger,
    fetch_akshare_hot_rank,
    fetch_akshare_hot_rank_wc,
    fetch_akshare_zt_pool,
    fetch_akshare_dt_pool,
    fetch_akshare_broken_board,
)
from app.data_sources.sina_a_stock import (
    fetch_sina_market_snapshot,
    fetch_sina_zt_pool,
    fetch_sina_dt_pool,
)
from app.data_sources.sina import fetch_sina_quote
from app.data_sources.rate_limiter import get_request_headers, get_eastmoney_limiter, get_akshare_limiter
from app.data_sources.cache_manager import get_stock_info_cache
from app.data_sources.normalizer import safe_float, safe_int
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 主要指数代码
INDEX_CODES = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "899050": "北证50",
    "000688": "科创50",
}

# 指数专用 secid 映射 (东财)
# 指数的交易所编码与股票不同: 上证=1. 深证=0. 北证=0.
_INDEX_SECID = {
    "000001": "1.000001",   # 上证指数
    "399001": "0.399001",   # 深证成指
    "399006": "0.399006",   # 创业板指
    "899050": "0.899050",   # 北证50
    "000688": "1.000688",   # 科创50
    "000016": "1.000016",   # 上证50
    "000300": "1.000300",   # 沪深300
    "000905": "1.000905",   # 中证500
}


def _index_secid(code: str) -> str:
    """获取指数的东财 secid，优先查表，兜底用前缀判断"""
    c = (code or "").strip()
    if c in _INDEX_SECID:
        return _INDEX_SECID[c]
    # 兜底: 上证指数以 0/1 开头走上海, 其余走深圳
    if c and c[0] in ("0", "1"):
        return f"1.{c}"
    return f"0.{c}"


class AStockDataSource(CNStockDataSource):
    """
    A股完整数据源 — 继承 CNStockDataSource 的多源 fallback，
    补充 interfaces 层所需的扩展方法。
    """

    name = "AStock/multi-source"
    enabled = True

    def __init__(self):
        super().__init__()
        # DataCache 支持 per-key TTL，用于长缓存场景
        self._info_cache = get_stock_info_cache()  # 默认 TTL=86400s

    # ================================================================
    # 指数实时行情
    # ================================================================

    def get_index_quotes(self, codes: List[str]) -> List[Dict[str, Any]]:
        """
        获取指数实时行情。
        fallback: 东财(批量) → 腾讯(逐个) → 新浪(逐个) → AkShare

        Returns:
            [{"code": "000001", "name": "上证指数", "price": 3200.0,
              "change": 15.0, "change_percent": 0.47}, ...]
        """
        cache_key = f"index_quotes:{','.join(sorted(codes or []))}"
        cached = self._info_cache.get(cache_key)
        if cached:
            return cached

        cb = self.circuit_breaker
        timeout = min(_get_timeout(), 10)

        sources = [
            ("eastmoney_index", lambda: self._fetch_index_eastmoney(codes)),
            ("tencent_index",   lambda: self._fetch_index_tencent(codes)),
            ("sina_index",      lambda: self._fetch_index_sina(codes)),
            ("akshare_index",   lambda: self._fetch_index_akshare(codes)),
        ]

        for source_name, fetcher in sources:
            if not cb.is_available(source_name):
                continue

            result, err = _fetch_with_timeout(
                fetcher, timeout=timeout, source_name=source_name,
            )
            if result and len(result) > 0:
                cb.record_success(source_name)
                self._info_cache.set(cache_key, result, ttl=120)
                return result
            cb.record_failure(source_name, err or "empty")

        return []

    def _fetch_index_eastmoney(self, codes: List[str]) -> List[Dict[str, Any]]:
        """通过东财批量获取指数行情"""
        results = []
        for code in (codes or []):
            secid = _index_secid(code)
            try:
                limiter = get_eastmoney_limiter()
                limiter.wait()
                resp = requests.get(
                    "https://push2.eastmoney.com/api/qt/stock/get",
                    headers=get_request_headers(referer="https://quote.eastmoney.com/"),
                    params={
                        "secid": secid,
                        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
                        "fields": "f43,f44,f45,f46,f57,f58,f60,f170",
                    },
                    timeout=5,
                )
                d = (resp.json() or {}).get("data")
                if not d:
                    continue

                price = safe_float(d.get("f43"))
                prev = safe_float(d.get("f60"))
                change = round(price - prev, 2) if prev else 0.0

                results.append({
                    "code": code,
                    "name": INDEX_CODES.get(code, str(d.get("f58", ""))),
                    "price": price,
                    "change": change,
                    "change_percent": safe_float(d.get("f170")),
                    "open": safe_float(d.get("f46")),
                    "high": safe_float(d.get("f44")),
                    "low": safe_float(d.get("f45")),
                })
            except Exception as e:
                logger.debug(f"东财获取指数 {code} 失败: {e}")
                continue

        return results

    def _fetch_index_tencent(self, codes: List[str]) -> List[Dict[str, Any]]:
        """通过腾讯逐个获取指数行情"""
        results = []
        for code in (codes or []):
            try:
                tencent_code = normalize_cn_code(code)
                parts = fetch_quote(tencent_code)
                if not parts or len(parts) < 5:
                    continue

                last = safe_float(parts[3])
                prev = safe_float(parts[4])
                change = round(last - prev, 2) if prev else 0.0
                change_pct = round(change / prev * 100, 2) if prev else 0.0

                results.append({
                    "code": code,
                    "name": INDEX_CODES.get(code, str(parts[1]) if len(parts) > 1 else ""),
                    "price": last,
                    "change": change,
                    "change_percent": change_pct,
                    "open": safe_float(parts[5]) if len(parts) > 5 else 0,
                    "high": safe_float(parts[33]) if len(parts) > 33 else last,
                    "low": safe_float(parts[34]) if len(parts) > 34 else last,
                })
            except Exception as e:
                logger.debug(f"腾讯获取指数 {code} 失败: {e}")
                continue

        return results

    def _fetch_index_sina(self, codes: List[str]) -> List[Dict[str, Any]]:
        """通过新浪逐个获取指数行情"""
        results = []
        for code in (codes or []):
            try:
                q = fetch_sina_quote(code)
                if not q or q.get("last", 0) <= 0:
                    continue
                results.append({
                    "code": code,
                    "name": INDEX_CODES.get(code, q.get("name", "")),
                    "price": q.get("last", 0),
                    "change": q.get("change", 0),
                    "change_percent": q.get("changePercent", 0),
                    "open": q.get("open", 0),
                    "high": q.get("high", 0),
                    "low": q.get("low", 0),
                })
            except Exception as e:
                logger.debug(f"新浪获取指数 {code} 失败: {e}")
                continue

        return results

    def _fetch_index_akshare(self, codes: List[str]) -> List[Dict[str, Any]]:
        """通过 AkShare 获取指数实时行情"""
        import akshare as ak
        get_akshare_limiter().wait()

        df = ak.stock_zh_index_spot_em()
        if df is None or df.empty:
            return []

        code_set = set(codes or [])
        results = []

        code_col = "代码" if "代码" in df.columns else "code"
        name_col = "名称" if "名称" in df.columns else "name"
        price_col = "最新价" if "最新价" in df.columns else "close"
        pct_col = "涨跌幅" if "涨跌幅" in df.columns else "pct_chg"
        chg_col = "涨跌额" if "涨跌额" in df.columns else "change"

        for _, row in df.iterrows():
            code = str(row.get(code_col, "")).strip()
            if code not in code_set:
                continue

            price = safe_float(row.get(price_col))
            if price <= 0:
                continue

            results.append({
                "code": code,
                "name": INDEX_CODES.get(code, str(row.get(name_col, ""))),
                "price": price,
                "change": safe_float(row.get(chg_col)),
                "change_percent": safe_float(row.get(pct_col)),
                "open": 0,
                "high": 0,
                "low": 0,
            })

        return results

    # ================================================================
    # 市场快照
    # ================================================================

    def get_market_snapshot(self) -> Dict[str, Any]:
        """
        获取全市场涨跌统计。
        fallback: 东财全量 → AkShare

        Returns:
            {"up_count": int, "down_count": int, "flat_count": int,
             "limit_up": int, "limit_down": int, "emotion": int}
        """
        cache_key = "market_snapshot"
        cached = self._info_cache.get(cache_key)
        if cached:
            return cached

        cb = self.circuit_breaker
        timeout = min(_get_timeout(), 20)

        # 方案1: 东财全量
        if cb.is_available("eastmoney_market"):
            result, err = _fetch_with_timeout(
                lambda: self._fetch_market_snapshot_eastmoney(),
                timeout=timeout, source_name="eastmoney_market",
            )
            if result and (result.get("up_count", 0) + result.get("down_count", 0)) > 100:
                cb.record_success("eastmoney_market")
                self._info_cache.set(cache_key, result, ttl=120)
                return result
            cb.record_failure("eastmoney_market", err or "data too small")

        # 方案2: AkShare
        if cb.is_available("akshare_market"):
            result, err = _fetch_with_timeout(
                lambda: self._fetch_market_snapshot_akshare(),
                timeout=timeout, source_name="akshare_market",
            )
            if result and (result.get("up_count", 0) + result.get("down_count", 0)) > 100:
                cb.record_success("akshare_market")
                self._info_cache.set(cache_key, result, ttl=120)
                return result
            cb.record_failure("akshare_market", err or "data too small")

        return self._default_snapshot()

    def _fetch_market_snapshot_akshare(self) -> Dict[str, Any]:
        """通过 AkShare 全市场行情计算涨跌统计"""
        import akshare as ak
        get_akshare_limiter().wait()

        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return self._default_snapshot()

        pct_col = "涨跌幅" if "涨跌幅" in df.columns else "pct_chg"
        amt_col = "成交额" if "成交额" in df.columns else "amount"

        up = down = flat = limit_up = limit_down = 0
        total_amount = 0.0

        for _, row in df.iterrows():
            pct = safe_float(row.get(pct_col), -999)
            amount = safe_float(row.get(amt_col))
            total_amount += amount

            if pct > 0.001:
                up += 1
                if 9.5 <= pct <= 30.5 and (
                    abs(pct - 10) < 0.6 or abs(pct - 20) < 0.6 or abs(pct - 30) < 0.6
                ):
                    limit_up += 1
            elif pct < -0.001:
                down += 1
                if -30.5 <= pct <= -9.5 and (
                    abs(pct + 10) < 0.6 or abs(pct + 20) < 0.6 or abs(pct + 30) < 0.6
                ):
                    limit_down += 1
            else:
                flat += 1

        total = up + down
        emotion = int(up / total * 100) if total > 0 else 50

        return {
            "up_count": up, "down_count": down, "flat_count": flat,
            "limit_up": limit_up, "limit_down": limit_down,
            "total_amount": round(total_amount / 1e8, 2),
            "emotion": emotion, "north_net_flow": 0.0,
        }

    def _fetch_market_snapshot_eastmoney(self) -> Dict[str, Any]:
        """通过东财全市场行情计算涨跌统计"""
        limiter = get_eastmoney_limiter()
        limiter.wait()

        resp = requests.get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            headers=get_request_headers(referer="https://quote.eastmoney.com/"),
            params={
                "pn": 1, "pz": 6000,
                "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2,
                "fid": "f3",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                "fields": "f2,f3,f6,f12,f14",
            },
            timeout=15,
        )

        try:
            data = resp.json()
        except Exception:
            return self._default_snapshot()

        items = (data.get("data") or {}).get("diff")
        if not items:
            return self._default_snapshot()

        up = down = flat = limit_up = limit_down = 0
        total_amount = 0.0

        for item in items:
            if not isinstance(item, dict):
                continue

            price = safe_float(item.get("f2"), -1)
            pct = safe_float(item.get("f3"), -999)
            amount = safe_float(item.get("f6"))

            if price <= 0 or pct == -999:
                continue

            total_amount += amount

            if pct > 0.001:
                up += 1
                # 涨停: 9.9x% / 19.9x% / 29.9x%
                if 9.5 <= pct <= 30.5 and (
                    abs(pct - 10) < 0.6 or abs(pct - 20) < 0.6 or abs(pct - 30) < 0.6
                ):
                    limit_up += 1
            elif pct < -0.001:
                down += 1
                if -30.5 <= pct <= -9.5 and (
                    abs(pct + 10) < 0.6 or abs(pct + 20) < 0.6 or abs(pct + 30) < 0.6
                ):
                    limit_down += 1
            else:
                flat += 1

        total = up + down
        emotion = int(up / total * 100) if total > 0 else 50

        return {
            "up_count": up,
            "down_count": down,
            "flat_count": flat,
            "limit_up": limit_up,
            "limit_down": limit_down,
            "total_amount": round(total_amount / 1e8, 2),
            "emotion": emotion,
            "north_net_flow": 0.0,
        }

    def _default_snapshot(self) -> Dict[str, Any]:
        return {
            "up_count": 0, "down_count": 0, "flat_count": 0,
            "limit_up": 0, "limit_down": 0, "total_amount": 0.0,
            "emotion": 50, "north_net_flow": 0.0,
        }

    # ================================================================
    # 个股信息
    # ================================================================

    def get_stock_info(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """
        获取个股基本信息。缓存1小时。
        fallback: AkShare → 东财 direct
        """
        code = (stock_code or "").strip()
        if not code:
            return None

        cache_key = f"stock_info:{code}"
        cached = self._info_cache.get(cache_key)
        if cached:
            return cached

        cb = self.circuit_breaker

        # AkShare fallback
        if cb.is_available("akshare_info"):
            result, err = _fetch_with_timeout(
                lambda: self._fetch_stock_info_akshare(code),
                timeout=min(_get_timeout(), 10),
                source_name="akshare_info",
            )
            if result:
                cb.record_success("akshare_info")
                self._info_cache.set(cache_key, result, ttl=3600)
                return result
            cb.record_failure("akshare_info", err or "empty")
        # 东财 direct
        if cb.is_available("eastmoney_info"):
            result, err = _fetch_with_timeout(
                lambda: self._fetch_stock_info_eastmoney(code),
                timeout=min(_get_timeout(), 8),
                source_name="eastmoney_info",
            )
            if result:
                cb.record_success("eastmoney_info")
                self._info_cache.set(cache_key, result, ttl=3600)
                return result
            cb.record_failure("eastmoney_info", err or "empty")

        return None

    def _fetch_stock_info_akshare(self, code: str) -> Optional[Dict[str, Any]]:
        """通过 AkShare 获取个股基本信息"""
        import akshare as ak
        get_akshare_limiter().wait()

        df = ak.stock_individual_info_em(symbol=code)
        if df is None or df.empty:
            return None

        # df 是 2 列: item + value
        info = {}
        for _, row in df.iterrows():
            item = str(row.iloc[0]).strip() if len(row) > 0 else ""
            val = row.iloc[1] if len(row) > 1 else None
            info[item] = val

        def _get(key, default=None):
            v = info.get(key)
            if v is None:
                return default
            try:
                return float(v)
            except (TypeError, ValueError):
                return str(v).strip()

        name = _get("股票简称", "")
        if not name:
            return None

        return {
            "stock_code": code,
            "stock_name": str(name),
            "industry": str(_get("行业", "")).strip() or None,
            "listed_date": str(_get("上市时间", "")).strip() or None,
            "total_mv": _get("总市值"),
            "circ_mv": _get("流通市值"),
            "fetch_time": int(time.time()),
        }

    def _fetch_stock_info_eastmoney(self, code: str) -> Optional[Dict[str, Any]]:
        """通过东财获取个股基本信息"""
        secid = _em_secid_from_cn(code)
        if not secid:
            return None

        limiter = get_eastmoney_limiter()
        limiter.wait()

        resp = requests.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            headers=get_request_headers(referer="https://quote.eastmoney.com/"),
            params={
                "secid": secid,
                "ut": "fa5fd1943c7b386f172d6893dbfba10b",
                "fields": "f57,f58,f43,f116,f117,f162,f167,f100",
            },
            timeout=5,
        )

        try:
            d = (resp.json() or {}).get("data")
            if not d:
                return None
        except Exception:
            return None

        name = str(d.get("f58", "")).strip()
        if not name:
            return None

        return {
            "stock_code": code,
            "stock_name": name,
            "industry": str(d.get("f100", "")).strip() or None,  # 行业
            "price": safe_float(d.get("f43")),
            "total_mv": safe_float(d.get("f116")),     # 总市值
            "circ_mv": safe_float(d.get("f117")),       # 流通市值
            "pe_ratio": safe_float(d.get("f162")),      # 市盈率
            "pb_ratio": safe_float(d.get("f167")),      # 市净率
            "fetch_time": int(time.time()),
        }

    # ================================================================
    # 全部股票代码列表
    # ================================================================

    def get_all_stock_codes(self) -> List[Dict[str, str]]:
        """
        获取全部A股代码和名称。缓存24小时。
        fallback: 东财 direct → AkShare
        """
        cache_key = "all_stock_codes"
        cached = self._info_cache.get(cache_key)
        if cached:
            return cached

        cb = self.circuit_breaker

        # 东财 direct
        if cb.is_available("eastmoney_stocklist"):
            result, err = _fetch_with_timeout(
                lambda: self._fetch_all_stock_codes_eastmoney(),
                timeout=min(_get_timeout(), 20),
                source_name="eastmoney_stocklist",
            )
            if result and len(result) > 100:
                cb.record_success("eastmoney_stocklist")
                self._info_cache.set(cache_key, result, ttl=86400)
                return result
            cb.record_failure("eastmoney_stocklist", err or f"only {len(result) if result else 0}")

        # AkShare fallback
        if cb.is_available("akshare_stocklist"):
            result, err = _fetch_with_timeout(
                lambda: self._fetch_all_stock_codes_akshare(),
                timeout=min(_get_timeout(), 20),
                source_name="akshare_stocklist",
            )
            if result and len(result) > 100:
                cb.record_success("akshare_stocklist")
                self._info_cache.set(cache_key, result, ttl=86400)
                return result
            cb.record_failure("akshare_stocklist", err or f"only {len(result) if result else 0}")

        return []

    def _fetch_all_stock_codes_akshare(self) -> List[Dict[str, str]]:
        """通过 AkShare 获取全部A股代码和名称"""
        import akshare as ak
        get_akshare_limiter().wait()

        df = ak.stock_info_a_code_name()
        if df is None or df.empty:
            return []

        result = []
        for _, row in df.iterrows():
            code = str(row.get("code", "")).strip()
            name = str(row.get("name", "")).strip()
            if code and name and len(code) == 6:
                result.append({"stock_code": code, "stock_name": name})

        logger.info(f"[AkShare] 获取A股列表: {len(result)} 只")
        return result

    def _fetch_all_stock_codes_eastmoney(self) -> List[Dict[str, str]]:
        """通过东财获取全部A股代码列表"""
        limiter = get_eastmoney_limiter()
        limiter.wait()

        resp = requests.get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            headers=get_request_headers(referer="https://quote.eastmoney.com/"),
            params={
                "pn": 1, "pz": 6000,
                "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2,
                "fid": "f3",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                "fields": "f12,f14",
            },
            timeout=15,
        )

        try:
            items = ((resp.json() or {}).get("data") or {}).get("diff")
        except Exception:
            return []

        if not items:
            return []

        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("f12", "")).strip()
            name = str(item.get("f14", "")).strip()
            if code and name and len(code) == 6:
                result.append({"stock_code": code, "stock_name": name})

        logger.info(f"获取A股列表: {len(result)} 只")
        return result

    # ================================================================
    # 个股资金流向
    # ================================================================

    def get_stock_fund_flow(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """
        获取个股资金流向。缓存5分钟。
        fallback: 东财 direct → AkShare
        """
        code = (stock_code or "").strip()
        if not code:
            return None

        cache_key = f"fund_flow:{code}"
        cached = self._info_cache.get(cache_key)
        if cached:
            return cached

        cb = self.circuit_breaker

        # 东财 direct
        if cb.is_available("eastmoney_fundflow"):
            result, err = _fetch_with_timeout(
                lambda: self._fetch_fund_flow_eastmoney(code),
                timeout=min(_get_timeout(), 8),
                source_name="eastmoney_fundflow",
            )
            if result:
                cb.record_success("eastmoney_fundflow")
                self._info_cache.set(cache_key, result, ttl=300)
                return result
            cb.record_failure("eastmoney_fundflow", err or "empty")

        # AkShare fallback
        if cb.is_available("akshare_fundflow"):
            result, err = _fetch_with_timeout(
                lambda: self._fetch_fund_flow_akshare(code),
                timeout=min(_get_timeout(), 10),
                source_name="akshare_fundflow",
            )
            if result:
                cb.record_success("akshare_fundflow")
                self._info_cache.set(cache_key, result, ttl=300)
                return result
            cb.record_failure("akshare_fundflow", err or "empty")

        return None

    def _fetch_fund_flow_akshare(self, code: str) -> Optional[Dict[str, Any]]:
        """通过 AkShare 获取个股资金流向"""
        import akshare as ak
        get_akshare_limiter().wait()

        # 判断市场: 6开头(含688科创板)→sh, 其余→sz
        market = "sh" if code[:1] == "6" else "sz"

        df = ak.stock_individual_fund_flow(symbol=code, market=market)
        if df is None or df.empty:
            return None

        # 取最新一行
        row = df.iloc[-1]

        def _get(col, default=0.0):
            v = row.get(col)
            if v is None or str(v).strip() in ("-", "", "nan"):
                return default
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        return {
            "stock_code": code,
            "date": str(row.get("日期", "")).strip(),
            "main_net_flow": _get("主力净流入-净额"),
            "main_inflow": _get("主力流入-净额") if "主力流入-净额" in df.columns else 0,
            "main_outflow": _get("主力流出-净额") if "主力流出-净额" in df.columns else 0,
            "large_order_net_flow": _get("大单净流入-净额") if "大单净流入-净额" in df.columns else 0,
            "medium_order_net_flow": _get("中单净流入-净额") if "中单净流入-净额" in df.columns else 0,
            "small_order_net_flow": _get("小单净流入-净额") if "小单净流入-净额" in df.columns else 0,
        }

    def _fetch_fund_flow_eastmoney(self, code: str) -> Optional[Dict[str, Any]]:
        """通过东财获取个股资金流向 (最新3天)"""
        secid = _em_secid_from_cn(code)
        if not secid:
            return None

        limiter = get_eastmoney_limiter()
        limiter.wait()

        resp = requests.get(
            "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
            headers=get_request_headers(referer="https://quote.eastmoney.com/"),
            params={
                "secid": secid,
                "ut": "b2884a393a59ad64002292a3e90d46a5",
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                "klt": 101,
                "lmt": 3,  # 最近3天，取最后一条
            },
            timeout=5,
        )

        try:
            data = resp.json()
        except Exception:
            return None

        klines = (data.get("data") or {}).get("klines")
        if not klines:
            return None

        # 取最新一天
        line = klines[-1]
        parts = line.split(",")
        if len(parts) < 6:
            return None

        try:
            return {
                "stock_code": code,
                "date": parts[0],
                "main_net_flow": safe_float(parts[1]),          # 主力净流入
                "small_order_net_flow": safe_float(parts[2]),   # 小单净流入
                "medium_order_net_flow": safe_float(parts[3]),  # 中单净流入
                "large_order_net_flow": safe_float(parts[4]),   # 大单净流入
                "super_large_net_flow": safe_float(parts[5]),   # 超大单净流入
                "main_inflow": safe_float(parts[6]) if len(parts) > 6 else 0,
            }
        except (ValueError, IndexError):
            return None

    # ================================================================
    # 龙虎榜 / 热榜 / 涨跌停池 / 炸板池 (可插拔多源 fallback)
    # ================================================================
    #
    # 每个方法都走统一的 _try_sources() 调度:
    #   1. 依次尝试 ASTOCK_SOURCE_CHAIN 中的数据源
    #   2. 熔断保护 — 跳过已熔断的数据源
    #   3. 超时保护 — 单源超时自动切换下一源
    #   4. 返回第一个成功的结果
    #
    # 新增数据源只需两步:
    #   a. 在 app/data_sources/ 新模块实现 fetch_xxx 函数
    #   b. 在 ASTOCK_SOURCE_CHAIN 加入 (name, fetcher)

    def _try_sources(
        self,
        chain: List[Tuple[str, Callable]],
        timeout: Optional[float] = None,
        cache_key: Optional[str] = None,
        cache_ttl: int = 300,
    ) -> list:
        """
        统一多源 fallback 调度器。

        Args:
            chain: [(source_name, fetcher_callable), ...] 按优先级排列
            timeout: 单源超时秒数，默认取全局配置
            cache_key: 缓存 key (None 则不缓存)
            cache_ttl: 缓存 TTL 秒数

        Returns:
            第一个成功数据源返回的列表，全部失败返回 []
        """
        if timeout is None:
            timeout = min(_get_timeout(), 15)

        if cache_key:
            cached = self._info_cache.get(cache_key)
            if cached:
                return cached

        cb = self.circuit_breaker
        for source_name, fetcher in chain:
            if not cb.is_available(source_name):
                continue
            result, err = _fetch_with_timeout(
                fetcher, timeout=timeout, source_name=source_name,
            )
            if result:
                cb.record_success(source_name)
                if cache_key:
                    self._info_cache.set(cache_key, result, ttl=cache_ttl)
                return result
            cb.record_failure(source_name, err or "empty")

        return []

    def get_dragon_tiger(self, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """获取龙虎榜。多源 fallback: 东财直连 → AkShare → (未来扩展)"""
        chain = [
            ("eastmoney_dragon_tiger", lambda: fetch_eastmoney_dragon_tiger(start_date, end_date)),
            ("akshare_dragon_tiger",    lambda: fetch_akshare_dragon_tiger(start_date, end_date)),
        ]
        return self._try_sources(chain)

    def get_hot_rank(self) -> List[Dict[str, Any]]:
        """
        获取热榜/人气榜。多源 fallback: 东财直连 → AkShare(东财) → AkShare(问财)

        问财的排行算法与东财不同，可作为补充数据源。
        """
        chain = [
            ("eastmoney_hot_rank",     lambda: fetch_eastmoney_hot_rank()),
            ("akshare_hot_rank_em",    lambda: fetch_akshare_hot_rank()),
            ("akshare_hot_rank_wc",    lambda: fetch_akshare_hot_rank_wc()),
        ]
        return self._try_sources(chain, cache_key="hot_rank", cache_ttl=300)

    def get_zt_pool(self, trade_date: str = None) -> List[Dict[str, Any]]:
        """
        获取涨停池。多源 fallback: 东财直连 → AkShare → 新浪(简易)

        新浪只有涨幅筛选，缺少连板天数/封板资金等细节，作为兜底。
        """
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")
        chain = [
            ("eastmoney_zt_pool", lambda: fetch_eastmoney_zt_pool(trade_date)),
            ("akshare_zt_pool",   lambda: fetch_akshare_zt_pool(trade_date)),
            ("sina_zt_pool",      lambda: fetch_sina_zt_pool(trade_date)),
        ]
        return self._try_sources(chain, cache_key=f"zt_pool:{trade_date}", cache_ttl=60)

    def get_limit_down(self, trade_date: str = None) -> List[Dict[str, Any]]:
        """
        获取跌停池。多源 fallback: 东财直连 → AkShare → 新浪(简易)
        """
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")
        chain = [
            ("eastmoney_dt_pool", lambda: fetch_eastmoney_dt_pool(trade_date)),
            ("akshare_dt_pool",   lambda: fetch_akshare_dt_pool(trade_date)),
            ("sina_dt_pool",      lambda: fetch_sina_dt_pool(trade_date)),
        ]
        return self._try_sources(chain, cache_key=f"limit_down:{trade_date}", cache_ttl=60)

    def get_broken_board(self, trade_date: str = None) -> List[Dict[str, Any]]:
        """获取炸板池。多源 fallback: 东财直连 → AkShare → (未来扩展)"""
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")
        chain = [
            ("eastmoney_broken_board", lambda: fetch_eastmoney_broken_board(trade_date)),
            ("akshare_broken_board",    lambda: fetch_akshare_broken_board(trade_date)),
        ]
        return self._try_sources(chain, cache_key=f"broken_board:{trade_date}", cache_ttl=60)


# ================================================================
# AShareDataHub — 接口层统一入口
# ================================================================

class AShareDataHub:
    """
    A股数据统一入口 — 组合所有 Interface 对象，供 routes/emotion_scheduler 调用。

    属性:
        index:           IndexInterface           指数行情
        market_snapshot: MarketSnapshotInterface   市场快照
        zt_pool:         ZTPoolInterface           涨停池
        limit_down:      LimitDownInterface        跌停池
        broken_board:    BrokenBoardInterface      炸板池
        dragon_tiger:    DragonTigerInterface      龙虎榜
        hot_rank:        HotRankInterface          热榜/人气榜
        stock_info:      StockInfoInterface        个股信息
        stock_fund_flow: StockFundFlowInterface    个股资金流
        fund_flow:       FundFlowInterface         板块资金流
    """

    def __init__(self, sources=None, db=None):
        from app.interfaces.index import IndexInterface
        from app.interfaces.market_snapshot import MarketSnapshotInterface
        from app.interfaces.zt_pool import ZTPoolInterface
        from app.interfaces.limit_down import LimitDownInterface
        from app.interfaces.broken_board import BrokenBoardInterface
        from app.interfaces.dragon_tiger import DragonTigerInterface
        from app.interfaces.hot_rank import HotRankInterface
        from app.interfaces.stock_info import StockInfoInterface
        from app.interfaces.stock_fund_flow import StockFundFlowInterface
        from app.interfaces.fund_flow import FundFlowInterface
        from .cache_file import cache_db

        # 数据源列表: 默认使用 AStockDataSource (多源 fallback)
        if sources is None:
            _ds = AStockDataSource()
            sources = [_ds]

        if db is None:
            db = cache_db()

        # 缓存统一由 AStockDataSource._info_cache 负责，不再使用 RealtimeCache
        self.index = IndexInterface(sources, db)
        self.market_snapshot = MarketSnapshotInterface(sources, db)
        self.zt_pool = ZTPoolInterface(sources, db)
        self.limit_down = LimitDownInterface(sources, db)
        self.broken_board = BrokenBoardInterface(sources, db)
        self.dragon_tiger = DragonTigerInterface(sources, db)
        self.hot_rank = HotRankInterface(sources, db)
        self.stock_info = StockInfoInterface(sources, db)
        self.stock_fund_flow = StockFundFlowInterface(sources)
        self.fund_flow = FundFlowInterface(sources, db)
