# -*- coding: utf-8 -*-
"""
index_cons.py — 指数成分股统一接口 (中证官网/新浪)

用途:
  给定指数代码, 返回当前成分股列表 (新浪格式 "sh600000"/"sz000001")。
  供指数维度聚合类任务使用 (成分股映射、指数权重聚合等)。

来源/任务: 指数资金流日级回补(方案2) / 2026-10-20 / OpenClaw 代编
(共享区协作约定: 新增文件, 不动他人内容)

数据源与降级策略 (按权威度排序, 全部免费源, 统一输出):
  1. 中证官网 xls   — oss-ch.csindex.com.cn .../cons/{code}cons.xls, **官方权威**
                     样本清单 (xlrd 解析, 依赖 akshare 传递的 xlrd, 无则降级),
                     覆盖中证/上证系列 (000001/000016/000300/000905/000852/000688)
  2. node=zhishu_XXX — Market_Center.getHQNodeDataSimple, JSON, 1 请求,
                     实测量数与官方一致 (399 系列走这里; 000300/000852 返回空)
  3. 老接口 phtml 页 — vII_NewestComponent, GBK HTML 表 + 翻页, 最后兜底。
                     ⚠ 实测 000300 表内有重复行 (300行仅289只) 且无排序键,
                     只作前两路失败时的近似兑底, 不要当权威源。
  实测样本量校验: 000001→2210, 000016→50, 000300→300, 000905→500,
                  000852→1000, 000688→50, 399001→500, 399005→100, 399006→100

易错点 (改动前先读):
  1. hq.sinajs.cn / vip.stock.finance.sina.com.cn 部分接口校验 Referer,
     必须带 "https://finance.sina.com.cn/", 否则 403 / 空数据。
  2. phtml 页面是 **GBK** 编码, r.encoding 必须显式设 "gbk", 否则中文乱码;
     行情代码本身是 ASCII 不受影响, 但页面里混有导航栏的 **指数链接**
     (sh000001/sh000300 等), 全文正则会被污染 — 必须先切出 id="NewStockTable"
     表格区段再提代码, 只认 realstock/company/(sh|sz)\\d{6}/nc.shtml 行情链接。
  3. phtml 翻页总数在 "当前页面是第1页,共N页" 文案里, 翻页 URL 是
     /corp/view/vII_NewestComponent.php?page=N&indexid=XXXXXX (注意是 view 路径,
     go.php/.../phtml 只是跳转入口)。页内抓取失败时 **整路作废返回 None**,
     不得返回部分结果 (2026-10-20 实测: 部分会静默缺成分)。
  4. 成分股是 **当前时点** 成分, 不是历史各期成分 (免费源均无历史成分)。
     聚合历史资金流时用当前成分做近似, 与东财官方口径可能有样本差异。
  5. 两次请求之间限速 _THROTTLE_SEC, 避免触发新浪/中证封禁。
  6. csindex xls 是 OLE2 老式 .xls, 必须用 xlrd 解析 (项目经 akshare 传递
     xlrd>=1.2.0); xlrd 缺失时自动降级到下一通道, 不硬性依赖。
     列序: 0日期 1指数代码 2指数名称 3英文名 4成份券代码 5成份券名称
     6英文名 7交易所 8交易所英文名; 交易所中文串→sh/sz/bj 前缀映射。

用法:
    from app.market_cn.index_cons import get_index_cons
    cons = get_index_cons("000852")        # -> ["sh688001", "sz002001", ...]
    cons = get_index_cons("000300.SH")     # 带后缀也行, 内部取前 6 位
"""
from __future__ import annotations

import random
import re
import time
from typing import Dict, List, Optional

import requests

from app.utils.logger import get_logger

logger = get_logger(__name__)

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://finance.sina.com.cn/",
}
_THROTTLE_SEC = 0.6          # 两次请求最小间隔 (秒)
_NODE_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "Market_Center.getHQNodeDataSimple")
_PHTML_URL = "https://vip.stock.finance.sina.com.cn/corp/view/vII_NewestComponent.php"
_CSINDEX_URL = ("https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
                "file/autofile/cons/{code}cons.xls")
_EXCHANGE_PREFIX = {"深圳证券交易所": "sz", "上海证券交易所": "sh",
                    "北京证券交易所": "bj"}
_CODE_PREFIX = (("6", "sh"), ("0", "sz"), ("3", "sz"), ("4", "bj"), ("8", "bj"),
                ("9", "sh"))

_last_req_ts = 0.0


def _throttle() -> None:
    global _last_req_ts
    wait = _THROTTLE_SEC - (time.time() - _last_req_ts)
    if wait > 0:
        time.sleep(wait + random.uniform(0, 0.3))
    _last_req_ts = time.time()


def _get(url: str, params: Optional[Dict] = None, timeout: float = 20,
         retries: int = 3, quiet: bool = False) -> Optional[requests.Response]:
    """新浪/中证 GET (直连, 不读系统代理)。失败重试, 全败返回 None。

    quiet=True: 预期可失败的探测请求 (如 csindex xls 对 399 系列404) 不刷警告。
    """
    last_err = None
    for i in range(retries):
        _throttle()
        try:
            s = requests.Session()
            s.trust_env = False   # 同 sync_index_fflow: 境外代理出口会被新浪拒绝
            r = s.get(url, params=params, headers=_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001 — 网络层异常统一收敛
            last_err = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    if not quiet:
        logger.warning("[index_cons] 请求失败 %s: %r", url, last_err)
    return None


def _cons_via_csindex(code: str) -> Optional[List[str]]:
    """通道1: 中证官网官方样本 xls (OLE2, xlrd 解析)。404/解析失败返回 None。"""
    r = _get(_CSINDEX_URL.format(code=code), quiet=True)   # 399 系列404属预期
    if r is None:
        return None
    try:
        import xlrd                       # 经 akshare 传递; 缺失则降级 (见易错点6)
        book = xlrd.open_workbook(file_contents=r.content)
    except Exception as e:  # noqa: BLE001 — 含 ImportError/xlr 解析错
        logger.info("[index_cons] %s csindex xls 不可用 (%r), 降级下一通道", code, e)
        return None
    sheet = book.sheet_by_index(0)
    out = []
    for i in range(1, sheet.nrows):       # 第0行是表头
        row = sheet.row_values(i)
        try:
            stock = row[4]
            stock = str(int(stock)) if isinstance(stock, float) else str(stock)
            stock = stock.strip().zfill(6)
            exch = str(row[7]).strip()
        except (IndexError, ValueError):
            continue
        prefix = _EXCHANGE_PREFIX.get(exch)
        if prefix is None:                # 未知交易所串 → 按代码段推断
            prefix = next((p for c, p in _CODE_PREFIX if stock.startswith(c)), None)
        if prefix and re.fullmatch(r"\d{6}", stock):
            out.append(f"{prefix}{stock}")
    return out or None


def _cons_via_node(code: str) -> Optional[List[str]]:
    """通道1: Market_Center node JSON。不支持的指数返回 None (交由后续通道)。"""
    r = _get(_NODE_URL, params={"page": 1, "num": 3000, "sort": "symbol", "asc": 1,
                                "node": f"zhishu_{code}", "_s_r_a": "setlen"})
    if r is None:
        return None
    m = re.search(r"(\[.*\])", r.text.strip(), re.S)   # 响应可能带 JS 包裹
    if not m:
        return None
    try:
        import json
        data = json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, list) or not data:
        return None
    out = []
    for item in data:
        sym = str(item.get("symbol", "")) if isinstance(item, dict) else ""
        if re.fullmatch(r"(sh|sz)\d{6}", sym):
            out.append(sym)
    return out or None


def _cons_via_phtml(code: str) -> Optional[List[str]]:
    """通道3: 老接口 HTML 表翻页 (GBK), 最后兜底 (数据有重复行, 非权威)。"""
    cons: List[str] = []
    page, total_pages = 1, 1
    while page <= total_pages:
        r = _get(_PHTML_URL, params={"page": page, "indexid": code})
        if r is None:
            return None      # 页缺失→整路作废 (见易错点3), 严禁返回部分结果
        r.encoding = "gbk"          # 见易错点2
        txt = r.text
        if page == 1:
            m = re.search(r"共\s*(\d+)\s*页", txt)
            total_pages = int(m.group(1)) if m else 1
        # 切出成分表区段, 避开导航栏的指数链接 (见易错点2)
        start = txt.find('id="NewStockTable"')
        seg = txt[start:txt.find("</table>", start)] if start >= 0 else ""
        for sym in re.findall(r"realstock/company/((?:sh|sz)\d{6})/nc\.shtml", seg):
            if sym not in cons:
                cons.append(sym)
        if total_pages > 1:
            logger.info("[index_cons] %s phtml %d/%d 已得 %d 只", code, page,
                        total_pages, len(cons))
        page += 1
    return cons or None


# 【不动清单 · coord 缺口】中证/新浪「指数成分股」
#   所有 coord_*.py 均无"指数成分股" kind（provider 层也无中证 oss xls 实现），
#   故此处保留中证官网 xls / node / phtml 三路直连。
#   唯一调用方 app/market_cn/sina_moneyflow.py 已于 2026-10-28 归档到
#   del/market_cn_sina_moneyflow_20261028/（新浪资金流单边口径与库表不符），
#   当前无调用方；按「零引用≠可删」保留本能力，不因"看起来干净"而删。
#   收敛前须先在 coord_basic（或新建 coord 族）补成分股 kind 并平移三路通道，
#   不得为"看起来收敛"而删能力（禁止降智红线）。
#   见 docs/数据源外围收敛改造方案_20261028.md §3
def get_index_cons(symbol: str) -> List[str]:
    """指数成分股 → ["sh600000", ...] (当前成分, 去重保序)。

    symbol: 6 位代码, 允许带 .SH/.SZ 后缀 (如 "000852" / "000852.SH")。
    取不到返回空列表 (调用方应视为失败, 不要当"无成分")。
    """
    code = symbol.split(".")[0]
    cons = _cons_via_csindex(code)
    source = "csindex"
    if not cons:
        cons = _cons_via_node(code)
        source = "node"
    if not cons:
        cons = _cons_via_phtml(code)
        source = "phtml(近似)"
    if not cons:
        logger.warning("[index_cons] %s 成分股获取失败 (node/phtml 均空)", symbol)
        return []
    logger.info("[index_cons] %s 成分 %d 只 (通道=%s)", symbol, len(cons), source)
    return cons
