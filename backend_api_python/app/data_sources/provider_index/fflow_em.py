# -*- coding: utf-8 -*-
"""provider_index / fflow_em —— 指数族东财资金流源（index_fflow）。

API: push2.eastmoney.com/api/qt/stock/fflow/kline/get (klt=1, 当日 240 根 1 分钟)

⚠ TLS 指纹硬约束（2026-10-18 实证，自 scripts/sync_index_fflow.py 迁入）：
  东财 CDN 对 urllib/requests 的 JA3 指纹封杀（RemoteDisconnected 无状态码），
  必须走 curl_cffi 的 edge101 指纹；Referer 必须带 quote.eastmoney.com，
  UA 不覆盖（edge101 自带 UA 与指纹一致）。

⚠ IP 级限频（2026-10-27 实测）：push2 对连续请求打 RST 突发窗（约连发 2 个后
  整段 curl 56 拒绝，窗口分钟级翻转）——不是永久死，属上游风控。对应策略：
  单 host 重试退避 3s 递增跨窗，putdb 层失败指数隔 8s 补抓一轮，
  盘中每 5 分钟全日重写幂等由下一轮收敛。封锁窗内任何指纹/协议都不通。

字段语义（2026-09-12 实证, tmp/_fflow_depth.out）：
  f51=时刻（09:31 bar 起始口径，与 kline_index_5m 的 bar 结束时刻差 1 根）；
  f52~f56 = 主力/小单/中单/大单/超大单 净流入（元，**当日累计值**）；
  自洽校验：主力=大单+超大单，四类之和=0。特征工程用时需差分。
  仅返回当日 240 根 → 每日增量即全量，断采不可回补（只能向前攒）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

# f52~f56 净流入字段 → 行键（与 kline_index_fflow 库列名同名，累计值、元）
NET_COLS = ("main_net", "small_net", "mid_net", "big_net", "super_net")

# host 优先级: 实时主站优先 (秒级), 封锁时自动落延迟镜像 (~15 分钟滞后)
FFLOW_HOSTS = (
    "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
    "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get",
)


def _to_secid(symbol: str) -> str:
    """'000001.SH' → '1.000001'; '399001.SZ' → '0.399001' (EM secid 规则)"""
    code, suffix = str(symbol).split(".")
    return ("1" if suffix.upper() == "SH" else "0") + "." + code


def _em_get(url: str, timeout: float = 15, retries: int = 3) -> Optional[str]:
    """东财 GET — curl_cffi edge101 TLS 指纹封装，返回响应文本，失败 None。"""
    import time as _t
    try:
        from curl_cffi import requests as _cr
    except ImportError:
        logger.debug("[fflow_em] curl_cffi 未安装 (pip install curl_cffi)")
        return None
    headers = {"Referer": "https://quote.eastmoney.com/"}
    last_err = None
    for i in range(retries):
        try:
            s = _cr.Session(impersonate="edge101")
            r = s.get(url, timeout=timeout, headers=headers)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            if i < retries - 1:
                # 退避跨限频窗（RST 突发窗分钟级翻转，1.5s 递增常撞同一关闭窗）
                _t.sleep(3.0 * (i + 1))
    err_str = str(last_err)[:120]
    if "Connection closed" in err_str or "curl: (56)" in err_str or "RST" in err_str:
        logger.debug("[fflow_em] _em_get 重试 %d 次失败(限频窗口): %s", retries, err_str)
    else:
        logger.warning("[fflow_em] _em_get 重试 %d 次失败: %s", retries, err_str)
    return None


def fetch_index_fflow(code: str, limit: int = 240) -> Dict[str, Any]:
    """指数当日分钟资金流（EM fflow kline, klt=1）。

    返回 {"rows": [{time, main_net, small_net, mid_net, big_net, super_net}, ...]}，
    time="YYYY-MM-DD HH:MM"（bar 起始时刻），净额=当日累计（元）。仅当日数据。
    """
    symbol = code if "." in str(code) else (
        "%s.%s" % (code, "SZ" if str(code).startswith("399") else "SH"))
    txt = None
    for base in FFLOW_HOSTS:
        url = (base
               + "?lmt=0&klt=1&fields1=f1,f2,f3,f7"
               + "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
               + "&secid=%s" % _to_secid(symbol))
        txt = _em_get(url, timeout=15, retries=3)
        if txt:
            break  # 任一 host 成功即用 (实时优先, 封锁自动落 delay)
    if not txt:
        return {"rows": [], "error": "全部 host 无响应"}
    try:
        d = json.loads(txt)
    except Exception as e:
        return {"rows": [], "error": "JSON 解析失败: %s" % e}
    kl = ((d.get("data") or {}).get("klines")) or []
    if not kl:
        return {"rows": [], "error": "无数据"}
    rows: List[Dict[str, Any]] = []
    for line in kl:
        p = line.split(",")
        try:
            row = {"time": p[0][:16]}
            for i, c in enumerate(NET_COLS):
                row[c] = float(p[i + 1])
            rows.append(row)
        except (ValueError, IndexError):
            continue
    if limit and len(rows) > int(limit):
        rows = rows[-int(limit):]
    return {"rows": rows}


from app.data_sources.provider_index import register  # noqa: E402


@register
class FflowEmIndexProvider:
    name = "em_fflow"
    priority = 10
    markets = {"CNStock"}
    capabilities = {
        "index_fflow": fetch_index_fflow,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
