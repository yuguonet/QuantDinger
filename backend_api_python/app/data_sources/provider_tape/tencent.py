# -*- coding: utf-8 -*-
"""provider_tape / tencent —— 分笔与五档盘口（tencent 上游）。

【定位】只做单次 I/O + 解析；不重试 / 不熔断 / 不缓存。

【代码来源（2026-10-26）】
  取自 `app/market_cn/tape.py`，AST 按函数边界原样抽出，未改一字。
  运行期依赖：`app.utils.mootdx_client`（唯一 TDX 客户端管理点，用户裁定允许）
  与 stdlib `urllib`。**不 import market_cn**。

【与生产实现的唯一差别】
  生产 `get_ticks_*` 在外面再包一层内存缓存；本层不带缓存，返回新鲜数据。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def _market(code: str) -> int:
    """股票/指数代码 → 通达信市场号。

    通达信市场编码:
      1 = 上海证券交易所
      0 = 深圳证券交易所

    判断规则（2026-10-26 修复: 原版只认指数前缀 000/88/99，600xxx 沪市股误判为深市）:
      上海: 6xxxxx（股票 600/601/603/605/688/689）+ 000/88/99（指数）
      深圳: 0xxxxx（股票 000/001/002/003）+ 3xxxxx（创业板 300/301）+ 399（指数）

    注意: 000xxx 双义（深市股 000001 平安银行 vs 沪市指数 000001 上证指数），
    本函数优先按**股票**规则处理（000→深），指数场景请传带前缀代码（如 sh000001）。
    """
    if code[:1] == "6":
        return 1
    if code[:3] in ("88", "99"):
        return 1
    return 0

def _quote_tencent(code: str) -> Optional[Dict[str, Any]]:
    """数据源2: 腾讯财经获取五档盘口 + 实时快照（备用）。

    腾讯接口返回的字段布局（~ 分隔，共 50+ 字段）:
      v[1]  = 名称
      v[3]  = 最新价    v[4]  = 昨收
      v[5]  = 今开      v[6]  = 成交量(手)
      v[9]  = 买一价    v[10] = 买一量    # 买盘五档: v[9]-v[18]
      v[11] = 买二价    v[12] = 买二量
      v[13] = 买三价    v[14] = 买三量
      v[15] = 买四价    v[16] = 买四量
      v[17] = 买五价    v[18] = 买五量
      v[19] = 卖一价    v[20] = 卖一量    # 卖盘五档: v[19]-v[28]
      v[21] = 卖二价    v[22] = 卖二量
      v[23] = 卖三价    v[24] = 卖三量
      v[25] = 卖四价    v[26] = 卖四量
      v[27] = 卖五价    v[28] = 卖五量
      v[31] = 涨跌额    v[32] = 涨跌幅(%)
      v[33] = 最高      v[34] = 最低
      v[36] = 成交量    v[37] = 成交额

    Args:
        code: 股票/指数代码

    Returns:
        同 _quote_mootdx 格式，source 为 "tencent"
        失败: None
    """
    import urllib.request

    # 添加沪深前缀
    pfx = "sh" if _market(code) == 1 else "sz"
    try:
        req = urllib.request.Request(f"https://qt.gtimg.cn/q={pfx}{code}")
        req.add_header("User-Agent", "Mozilla/5.0")
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
    except Exception as e:
        logger.warning("[tencent] 五档行情请求失败: %s", e)
        return None

    # 解析第一条记录（单只股票查询只有一条）
    line = raw.strip().split(";")[0]
    if '"' not in line:
        return None

    v = line.split('"')[1].split("~")
    if len(v) < 50:
        return None  # 字段不足，数据异常

    price = float(v[3]) if v[3] else 0
    last_close = float(v[4]) if v[4] else 0

    return {
        "code": code,
        "name": v[1],
        "price": price,
        "open": float(v[5]) if v[5] else 0,
        "high": float(v[33]) if v[33] else 0,
        "low": float(v[34]) if v[34] else 0,
        "last_close": last_close,
        "change": float(v[31]) if v[31] else 0,
        "change_percent": float(v[32]) if v[32] else 0,
        "volume": float(v[36]) if v[36] else 0,
        "amount": float(v[37]) if v[37] else 0,
        # 买盘五档: v[9]~v[18]，奇数位=价格，偶数位=量
        "bid": {
            "bid1": {"price": float(v[9]) if v[9] else 0, "vol": float(v[10]) if v[10] else 0},
            "bid2": {"price": float(v[11]) if v[11] else 0, "vol": float(v[12]) if v[12] else 0},
            "bid3": {"price": float(v[13]) if v[13] else 0, "vol": float(v[14]) if v[14] else 0},
            "bid4": {"price": float(v[15]) if v[15] else 0, "vol": float(v[16]) if v[16] else 0},
            "bid5": {"price": float(v[17]) if v[17] else 0, "vol": float(v[18]) if v[18] else 0},
        },
        # 卖盘五档: v[19]~v[28]，奇数位=价格，偶数位=量
        "ask": {
            "ask1": {"price": float(v[19]) if v[19] else 0, "vol": float(v[20]) if v[20] else 0},
            "ask2": {"price": float(v[21]) if v[21] else 0, "vol": float(v[22]) if v[22] else 0},
            "ask3": {"price": float(v[23]) if v[23] else 0, "vol": float(v[24]) if v[24] else 0},
            "ask4": {"price": float(v[25]) if v[25] else 0, "vol": float(v[26]) if v[26] else 0},
            "ask5": {"price": float(v[27]) if v[27] else 0, "vol": float(v[28]) if v[28] else 0},
        },
        "source": "tencent",
    }

# ── 信封包装 ───────────────────────────────────────────────────

def _orderbook(code: str) -> Dict[str, Any]:
    data = _quote_tencent(code)
    return {"object": data} if data else {"object": None}


# ── 分笔成交 (ticks) ─────────────────────────────────────────
#
# 腾讯分笔 API: https://stock.gtimg.cn/data/index.php?appn=detail&action=data&c=sh600519&p=0&l=2000
# 返回 JS 变量赋值格式，每条: [时间, 价格, 成交量(股), 成交额(元), 买卖方向]
# 买卖方向: "B"=主动买 "S"=主动卖 "N"=中性
# 归一化为 mootdx 格式: buy_sell "0"=买 "1"=卖 "2"=中性


def _parse_tencent_ticks(raw: str, code: str) -> List[Dict[str, Any]]:
    """解析腾讯分笔响应 → 标准 tick 列表（与 mootdx 格式对齐）。

    腾讯格式: v_detail_data_sh600519=[0,"0/09:25:02/1325.40/-14.59/905/119948700/S|..."]
    每条: index/time/price/change/vol/amount/direction
    """
    import re

    # 提取引号内的数据串
    m = re.search(r'"([^"]+)"', raw)
    if not m:
        return []
    data_str = m.group(1)

    out = []
    for seg in data_str.split("|"):
        parts = seg.split("/")
        if len(parts) < 6:
            continue
        try:
            t = parts[1]           # HH:MM:SS
            p = float(parts[2])    # 价格
            # parts[3] = 涨跌幅，跳过
            v = float(parts[4])    # 成交量（股）
            a = float(parts[5])    # 成交额（元）
            bs_raw = parts[6] if len(parts) > 6 else ""  # 买卖方向
        except (ValueError, IndexError):
            continue

        # 买卖方向: "B"=主动买 "S"=主动卖 "M"=中性
        bs_upper = str(bs_raw).upper()
        if bs_upper == "B":
            buy_sell = "0"
        elif bs_upper == "S":
            buy_sell = "1"
        else:
            buy_sell = "2"

        out.append({
            "time": t,
            "price": p,
            "vol": v,
            "amount": a,
            "buy_sell": buy_sell,
        })
    return out


def fetch_ticks_today(code: str, limit: int = 2000) -> Dict[str, Any]:
    """当日分笔成交（腾讯 stock.gtimg.cn）。"""
    import urllib.request

    pfx = "sh" if _market(code) == 1 else "sz"
    tc_code = f"{pfx}{code}"
    url = f"https://stock.gtimg.cn/data/index.php?appn=detail&action=data&c={tc_code}&p=0&l={min(limit, 2000)}"

    try:
        req = urllib.request.Request(url, headers=dict(_HTTP_HEADERS))
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="replace")
    except Exception as e:
        logger.warning("[tencent] ticks_today %s 失败: %s", code, e)
        return {"rows": [], "error": str(e)}

    rows = _parse_tencent_ticks(raw, code)
    return {"rows": rows[:limit]}


def fetch_ticks_history(code: str, date: str, limit: int = 2000) -> Dict[str, Any]:
    """历史分笔成交（腾讯，同 ticks_today 接口，腾讯不支持指定日期，返回最近数据）。

    注意: 腾讯分笔 API 无日期参数，只能返回当日/最近数据。
    历史分笔需走 mootdx（当前不可用）或其他源。
    """
    # 腾讯无历史分笔 API，返回空并注明
    return {"rows": [], "error": "tencent 无历史分笔 API，仅支持当日"}


# ── Provider 声明 ──────────────────────────────────────────────

from app.data_sources.provider_tape import register   # noqa: E402


@register
class TencentTapeProvider:
    """腾讯财经（HTTP）：盘口 + 当日分笔。"""

    name = "tencent"
    priority = 20
    markets = {"CNStock"}

    capabilities = {
        "orderbook": _orderbook,
        "ticks_today": fetch_ticks_today,
        "ticks_history": fetch_ticks_history,
    }

    # 非 RST 上游：声明 batch_kinds ⇒ run_batch 把整批 codes 一次交给本源，
    # 由 dispatch.fanout_codes 多股并发取数（效率远高于逐码串行）。
    batch_kinds = {"orderbook", "ticks_today", "ticks_history"}

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        codes = params.get("codes")
        if codes is not None and kind in self.batch_kinds:
            rest = {k: v for k, v in params.items() if k not in ("code", "codes")}
            from app.data_sources.dispatch import fanout_codes
            return fanout_codes(fn, list(codes), rest, name=self.name, timeout=8.0)
        return fn(**params)
