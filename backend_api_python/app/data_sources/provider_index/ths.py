# -*- coding: utf-8 -*-
"""provider_index / ths —— 指数族同花顺源（sector_flow 行业资金流）。

接口: data.10jqka.com.cn/funds/hyzjl/field/tradezdf/order/desc/page/{n}/ajax/1/free/1/
必须带 hexin-v 动态 token（由 pywencai.headers.get_token 生成，无 token 直接放弃
——不是失败而是"本环境无法提供该源"，故返 NotSupported 让 dispatch 换源），
响应是 GBK 编码的 HTML 表格。

口径：同花顺「流入/流出/净额」单位为**亿元**，本层统一 ×1e8 换算为元。
仅支持当日、仅行业（概念资金流同花顺无免费通道）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.utils.logger import get_logger

logger = get_logger(__name__)

_MAX_PAGES = 10  # 防止页信息异常导致无限翻页

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0",
    "Referer": "http://data.10jqka.com.cn/funds/hyzjl/",
}


def _hexin_v() -> str:
    """生成 hexin-v token；pywencai 未安装或生成失败返回空串。"""
    try:
        from pywencai.headers import get_token
        return get_token() or ""
    except ImportError:
        logger.debug("[ths] pywencai 未安装，跳过同花顺源")
        return ""
    except Exception as e:
        logger.warning("[ths] hexin-v 生成失败: %s", e)
        return ""


def _fetch_raw(v_code: str) -> List[Dict[str, Any]]:
    """拉全部页并转 records（列名为中文，保持原样交给上层映射）。"""
    import urllib.request
    from io import StringIO

    import pandas as pd

    all_rows: List[Dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        url = ("http://data.10jqka.com.cn/funds/hyzjl/field/tradezdf"
               "/order/desc/page/%d/ajax/1/free/1/" % page)
        req = urllib.request.Request(url)
        for k, v in _HEADERS.items():
            req.add_header(k, v)
        req.add_header("hexin-v", v_code)

        try:
            raw = urllib.request.urlopen(req, timeout=10).read()
            html = raw.decode("gbk", errors="replace")
        except Exception as e:
            logger.warning("[ths] 行业资金流第 %d 页失败: %s", page, e)
            break

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, features="html.parser")
            if not soup.find_all("table"):
                break
            df = pd.read_html(StringIO(html))[0]
            if df.empty:
                break
            all_rows.extend(df.to_dict(orient="records"))

            page_info = soup.find(name="span", attrs={"class": "page_info"})
            if page_info:
                total_pages = int(page_info.text.split("/")[1])
                if page >= total_pages:
                    break
        except Exception as e:
            logger.warning("[ths] 解析第 %d 页失败: %s", page, e)
            break
    return all_rows


def fetch_sector_flow(indicator: str = "今日", board_type: str = "industry") -> Dict[str, Any]:
    """行业板块资金流排名（同花顺 hyzjl，当日；按主力净流入降序）。

    返回 {"rows": [{name, code, change_pct, main_net, main_pct, in_net, out_net,
                    turnover, lead_stock, lead_pct}, ...]}，金额单位**元**。
    """
    from app.data_sources.dispatch import NotSupported

    if board_type != "industry" or indicator != "今日":
        return NotSupported("sector_flow/%s/%s（同花顺仅当日行业）" % (board_type, indicator))

    v_code = _hexin_v()
    if not v_code:
        return NotSupported("sector_flow（本机缺 hexin-v token，无法访问同花顺）")

    raw_rows = _fetch_raw(v_code)
    if not raw_rows:
        # 抛出让 dispatch 换源（同族有 sina/em）
        raise ValueError("ths sector_flow 表格为空")

    import pandas as pd

    df = pd.DataFrame(raw_rows)
    name_col = [c for c in df.columns if "行业" in str(c) and "指数" not in str(c)]
    in_col = [c for c in df.columns if "流入" in str(c) and "资金" in str(c)]
    out_col = [c for c in df.columns if "流出" in str(c) and "资金" in str(c)]
    net_col = [c for c in df.columns if "净额" in str(c)]
    chg_col = [c for c in df.columns if c == "涨跌幅"]
    lead_col = [c for c in df.columns if "领涨" in str(c)]

    if not name_col or not net_col:
        logger.warning("[ths] 行业资金流列名不匹配: %s", list(df.columns))
        raise ValueError("ths sector_flow 列名不匹配")

    rows = []
    for _, row in df.iterrows():
        in_val = float(pd.to_numeric(row[in_col[0]], errors="coerce")) if in_col else 0.0
        out_val = float(pd.to_numeric(row[out_col[0]], errors="coerce")) if out_col else 0.0
        net_val = float(pd.to_numeric(row[net_col[0]], errors="coerce")) if net_col else 0.0
        total = in_val + out_val

        chg_val = 0.0
        if chg_col:
            try:
                chg_val = float(str(row[chg_col[0]]).replace("%", ""))
            except ValueError:
                chg_val = 0.0

        rows.append({
            "name": str(row[name_col[0]]),
            "code": "",
            "change_pct": chg_val,
            "main_net": round(net_val * 1e8, 2),   # 亿→元
            "main_pct": round(net_val / total * 100, 2) if total > 0 else 0.0,
            "in_net": round(in_val * 1e8, 2),
            "out_net": round(out_val * 1e8, 2),
            "turnover": 0.0,
            "lead_stock": str(row[lead_col[0]]) if lead_col else "",
            "lead_pct": 0.0,
        })
    rows.sort(key=lambda x: x["main_net"], reverse=True)
    return {"rows": rows}


def fetch_market_flow_rt() -> Dict[str, Any]:
    """大盘实时资金流（同花顺无大盘接口 ⇒ 由全部行业板块流入/流出汇总）。

    返回 {"rows": [{source, timestamp, main_net, main_pct, in_net, out_net,
                    sectors_count, data}]}；单位元，data 恒空（板块明细由 sina 源提供）。
    """
    from datetime import datetime

    rows = fetch_sector_flow("今日", "industry")["rows"]
    if not rows:
        raise RuntimeError("同花顺行业板块资金流为空，无法汇总大盘资金流")

    total_in = sum(r["in_net"] for r in rows)
    total_out = sum(r["out_net"] for r in rows)
    total_net = total_in - total_out
    total = total_in + total_out
    return {"rows": [{
        "source": "ths",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "main_net": round(total_net, 2),
        "main_pct": round(total_net / total * 100, 2) if total > 0 else 0.0,
        "in_net": round(total_in, 2),
        "out_net": round(total_out, 2),
        "sectors_count": len(rows),
        "data": [],
    }]}


from app.data_sources.provider_index import register  # noqa: E402


@register
class ThsIndexProvider:
    name = "ths"
    priority = 30
    markets = {"CNStock"}
    capabilities = {
        "sector_flow": fetch_sector_flow,
        "market_flow_rt": fetch_market_flow_rt,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
