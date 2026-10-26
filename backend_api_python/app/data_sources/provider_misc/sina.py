# -*- coding: utf-8 -*-
"""provider_misc —— 交易日历（委托 app.utils，允许）+ 个股日级资金流（新浪，AST 抽取）。"""
from __future__ import annotations

import time
import json
import random
from pathlib import Path
from typing import Any, Dict

import requests

from app.data_sources.rate_limiter import get_akshare_limiter
from app.utils.logger import get_logger
from app.utils import trading_calendar

logger = get_logger(__name__)


_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://finance.sina.com.cn/",
}
_LSCJFB_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "MoneyFlow.ssl_qsfx_lscjfb")
_THROTTLE_SEC = 1.0           # 个股全历史请求较大, 限速宽松些防封禁
_HIST_LIMIT = 10000           # 见易错点1: 全历史条数上限
_CLASS_MAP = (("r0_net", "super_net"), ("r1_net", "big_net"),
              ("r2_net", "mid_net"), ("r3_net", "small_net"))
_FLOW_KEYS = ("main_net", "small_net", "mid_net", "big_net", "super_net")
_PROJECT_ROOT = Path(__file__).resolve().parents[3]   # .../backend_api_python/app/market_cn/ → 项目根
_RESUME_PATH = _PROJECT_ROOT / "tmp" / "fflow_sina_resume.json"
_last_req_ts = 0.0

def _throttle() -> None:
    global _last_req_ts
    wait = _THROTTLE_SEC - (time.time() - _last_req_ts)
    if wait > 0:
        time.sleep(wait + random.uniform(0, 0.5))
    _last_req_ts = time.time()

def to_daima(code: str) -> str:
    """股票代码 → 新浪 daima 格式。

    "600519"/"600519.SH"/"sh600519" → "sh600519"。
    ⚠ 不能只看首位是否 '6' (旧实现在 tape._sina_moneyflow_rows 里把 8/4 开头
    的北交所错归 sz): 6/9→sh, 0/3→sz, 4/8→bj。
    """
    c = code.strip().split(".")[0].lower()
    if c.startswith(("sh", "sz", "bj")) and len(c) == 8:
        return c
    c = c.zfill(6)
    if c.startswith(("6", "9")):
        return "sh" + c
    if c.startswith(("4", "8")):
        return "bj" + c
    return "sz" + c

def _get_json(url: str, params: Optional[Dict] = None, retries: int = 3):
    last_err = None
    for i in range(retries):
        _throttle()
        try:
            s = requests.Session()
            s.trust_env = False   # 境外代理出口会被新浪拒绝, 强制直连
            r = s.get(url, params=params, headers=_HEADERS, timeout=30)
            r.raise_for_status()
            return json.loads(r.text)
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    logger.warning("[sina_fflow] 请求失败 %s params=%s: %r", url, params, last_err)
    return None

def get_stock_daily_flow(daima: str) -> List[Dict]:
    """个股日级四档净额 (全历史)。

    返回升序 [{"date": "YYYY-MM-DD", "super_net", "big_net", "mid_net",
    "small_net", "main_net"}], 单位元; 失败返回 []。
    """
    data = _get_json(_LSCJFB_URL, params={"daima": daima, "num": _HIST_LIMIT})
    if not isinstance(data, list) or not data:
        return []
    rows = []
    for rec in data:
        try:
            item = {"date": rec["opendate"]}
            for src, dst in _CLASS_MAP:
                item[dst] = float(rec[src])
            item["main_net"] = item["super_net"] + item["big_net"]
            rows.append(item)
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("[sina_fflow] %s 行解析跳过: %r", daima, e)
    return rows


def fetch_trading_calendar(date: str = "") -> Dict[str, Any]:
    """交易日历：委托 `app.utils.trading_calendar`（用户裁定允许的基础接口）。"""
    return {"object": {
        "date": date,
        "is_trading_day": trading_calendar.is_trading_day(date) if date
        else trading_calendar.is_trading_day_today(),
    }}


def fetch_stock_flow_daily(code: str, days: int = 120) -> Dict[str, Any]:
    return {"rows": get_stock_daily_flow(to_daima(code))[: max(1, days)]}


from app.data_sources.provider_misc import register   # noqa: E402


@register
class SinaMiscProvider:
    name = "sina"
    priority = 50
    markets = {"CNStock"}
    capabilities = {
        "stock_flow_daily": fetch_stock_flow_daily,
        "trading_calendar": fetch_trading_calendar,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
