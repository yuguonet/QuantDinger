# -*- coding: utf-8 -*-
"""provider_misc / sina —— 个股资金流（新浪 lscjfb 日级四档 + zjlrqs 当日近似）。"""
from __future__ import annotations

import time
import json
import random
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from app.data_sources.rate_limiter import get_akshare_limiter
from app.utils.logger import get_logger

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


def fetch_stock_flow_daily(code: str, days: int = 120) -> Dict[str, Any]:
    """近 N 日日级四档净额（元）。

    `get_stock_daily_flow` 返回**升序**（旧→新）全历史，故取尾部 N 条
    = 最近 N 个交易日（取头部会拿到上市初期的最老 N 条，2026-10-28 修正）。
    """
    rows = get_stock_daily_flow(to_daima(code))
    return {"rows": rows[-max(1, int(days)):] if rows else []}


_ZJLRQS_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
               "json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs")


def fetch_stock_flow_rt(code: str) -> Dict[str, Any]:
    """当日个股资金流（新浪 zjlrqs，取最新一条日级记录）。

    该接口只有两个量：`netamount`（四档净额总和）与 `r0_net`（超大单净额），
    **没有**大/中/小拆分，也推不出"主力=超大+大"。

    ⚠ 槽位沿用历史约定（2026-10-20 已在日级路径纠正，实时路径未动）：
        main_net  = netamount（四档总和，**不是**主力）
        large_net = r0_net（实际是超大单，槽位名叫大单）
        small/mid/super = 0
      这是"近似映射"而非真实四档，改口径会改变 agent 可见数值，需单独拍板。

    返回 {"rows": [{time, main_net, small_net, mid_net, large_net, super_net}]}。
    """
    data = _get_json(_ZJLRQS_URL, params={
        "page": 1, "num": 1, "sort": "opendate", "asc": 0, "daima": to_daima(code),
    })
    if not isinstance(data, list) or not data:
        raise RuntimeError("新浪 zjlrqs 返回空（%s）" % code)
    row = data[0]
    main_net = float(row.get("netamount") or 0)
    return {"rows": [{
        "time": str(row.get("opendate", "") or ""),
        "main_net": main_net,
        "small_net": 0.0,
        "mid_net": 0.0,
        "large_net": float(row.get("r0_net") or 0),
        "super_net": 0.0,
    }]}


from app.data_sources.provider_misc import register   # noqa: E402


@register
class SinaMiscProvider:
    name = "sina"
    priority = 50
    markets = {"CNStock"}
    capabilities = {
        "stock_flow_daily": fetch_stock_flow_daily,
        "stock_flow_rt": fetch_stock_flow_rt,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
