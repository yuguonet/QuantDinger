# -*- coding: utf-8 -*-
"""provider_tape / mootdx —— 分笔与五档盘口（mootdx 上游）。

⚠ 2026-10-28 停用：TDX 行情数据面已断，本类 `disabled=True`（fetch 直接回
NotSupported，零网络）；capabilities 声明保留以兼容既有候选名单断言，
实现原样保留，改回 `disabled=False` 即恢复。详见类 docstring。

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
from app.utils.mootdx_client import get_client as _get_client

logger = get_logger(__name__)


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

def _quote_mootdx(code: str) -> Optional[Dict[str, Any]]:
    """数据源1: mootdx 获取五档盘口 + 实时快照。

    五档盘口含义:
      买一~买五: 当前挂单买入的最高5个价位及其委托量
      卖一~卖五: 当前挂单卖出的最低5个价位及其委托量
      买一价 ≤ 卖一价（正常情况下买一 < 卖一，相等时有成交）

    Args:
        code: 股票/指数代码，如 "600519"、"000001"

    Returns:
        成功: {
            code, name, price, open, high, low, last_close, change, change_percent,
            volume, amount,
            bid: {bid1~bid5: {price, vol}},  # 买盘五档
            ask: {ask1~ask5: {price, vol}},  # 卖盘五档
            source: "mootdx"
        }
        失败: None
    """
    cli = _get_client()
    if cli is None:
        return None
    try:
        df = cli.quotes(symbol=[code])
        if df is None or df.empty:
            return None
        r = df.iloc[0]

        price = float(r.get("price", 0))
        last_close = float(r.get("last_close", 0))

        return {
            "code": str(r.get("code", code)),
            "name": str(r.get("name", "")),
            "price": price,                          # 最新价
            "open": float(r.get("open", 0)),         # 今开
            "high": float(r.get("high", 0)),         # 最高
            "low": float(r.get("low", 0)),           # 最低
            "last_close": last_close,                # 昨收
            "change": round(price - last_close, 4),  # 涨跌额
            "change_percent": float(r.get("percent", 0)),  # 涨跌幅(%)
            # 单位口径：通达信 quotes 的 vol 为"手"，与 sina(股)/tencent(手×100)/sohu(手×100)
            # 对齐需 ×100（tape 族统一"股"，外围换手率公式 volume÷(流通股数) 依赖此口径）。
            # ⚠ 2026-10-28 起 TDX 数据面已断（disabled=True），本换算**未经实测**，
            #   恢复 disabled=False 时须先用 600519 对拍 sina/tencent 复核。
            "volume": float(r.get("vol", 0)) * 100,  # 成交量（手→股）
            "amount": float(r.get("amount", 0)),     # 成交额（元）
            # 买盘五档: bid1 为最高买价（最接近成交价），bid5 为最低买价
            "bid": {
                "bid1": {"price": float(r.get("bid1", 0)), "vol": float(r.get("bid_vol1", 0))},
                "bid2": {"price": float(r.get("bid2", 0)), "vol": float(r.get("bid_vol2", 0))},
                "bid3": {"price": float(r.get("bid3", 0)), "vol": float(r.get("bid_vol3", 0))},
                "bid4": {"price": float(r.get("bid4", 0)), "vol": float(r.get("bid_vol4", 0))},
                "bid5": {"price": float(r.get("bid5", 0)), "vol": float(r.get("bid_vol5", 0))},
            },
            # 卖盘五档: ask1 为最低卖价（最接近成交价），ask5 为最高卖价
            "ask": {
                "ask1": {"price": float(r.get("ask1", 0)), "vol": float(r.get("ask_vol1", 0))},
                "ask2": {"price": float(r.get("ask2", 0)), "vol": float(r.get("ask_vol2", 0))},
                "ask3": {"price": float(r.get("ask3", 0)), "vol": float(r.get("ask_vol3", 0))},
                "ask4": {"price": float(r.get("ask4", 0)), "vol": float(r.get("ask_vol4", 0))},
                "ask5": {"price": float(r.get("ask5", 0)), "vol": float(r.get("ask_vol5", 0))},
            },
            "source": "mootdx",
        }
    except Exception as e:
        logger.warning("[mootdx] 五档行情失败(%s): %s", code, e)
        return None

def _ticks_today_mootdx(code: str, limit: int = 2000) -> Optional[List[Dict[str, Any]]]:
    """mootdx: 获取当日分笔成交明细。

    分笔(tick)是最低粒度的成交数据，每一条代表一笔实际发生的交易。
    非交易时段（收盘后、节假日）此接口可能返回空数据。

    Args:
        code: 股票代码，如 "600519"
        limit: 最大返回条数，默认 2000（通达信单次上限）

    Returns:
        成功: [{
            time: "2025-01-15 09:30:05",  # 成交时间
            price: 1800.00,               # 成交价格
            vol: 100,                     # 成交量（股）
            amount: 180000.00,            # 成交金额（元）
            buy_sell: "0"                 # 买卖方向: "0"=主动买 "1"=主动卖 "2"=中性
        }, ...]
        失败: None
    """
    cli = _get_client()
    if cli is None:
        return None
    try:
        mkt = _market(code)
        # get_transaction_data: 通达信分笔成交接口
        # 参数: 市场号, 代码, start(0=从头), count(最大2000)
        result = cli.client.get_transaction_data(mkt, code, 0, min(limit, 2000))
        if not result:
            return None

        from mootdx.utils import to_data
        df = to_data(result)
        if df is None or df.empty:
            return None

        out = []
        for _, r in df.iterrows():
            out.append({
                "time": str(r.get("datetime", r.get("time", ""))),
                "price": float(r.get("price", 0)),
                "vol": float(r.get("vol", 0)),          # 成交量
                "amount": float(r.get("amount", 0)),     # 成交金额
                "buy_sell": str(r.get("buyorsell", "")), # "0"=买 "1"=卖 "2"=中性
            })
        logger.info("[mootdx] 当日分笔 %s: %d 条", code, len(out))
        return out
    except Exception as e:
        logger.warning("[mootdx] 当日分笔失败(%s): %s", code, e)
        return None

def _ticks_history_mootdx(code: str, date: str, limit: int = 2000) -> Optional[List[Dict[str, Any]]]:
    """mootdx: 获取指定日期的历史分笔成交。

    用于复盘分析：查看某天的逐笔成交，判断主力行为、大单异动等。

    Args:
        code: 股票代码，如 "600519"
        date: 日期，格式 "YYYYMMDD" 或 "YYYY-MM-DD"
        limit: 最大返回条数，默认 2000

    Returns:
        同 _ticks_today_mootdx 格式
        失败: None
    """
    cli = _get_client()
    if cli is None:
        return None
    try:
        mkt = _market(code)
        date_str = date.replace("-", "")  # 统一为 YYYYMMDD 格式
        # get_history_transaction_data: 通达信历史分笔接口
        # 参数: 市场号, 代码, start, count, 日期(整数)
        result = cli.client.get_history_transaction_data(mkt, code, 0, min(limit, 2000), int(date_str))
        if not result:
            return None

        from mootdx.utils import to_data
        df = to_data(result)
        if df is None or df.empty:
            return None

        out = []
        for _, r in df.iterrows():
            out.append({
                "time": str(r.get("datetime", r.get("time", ""))),
                "price": float(r.get("price", 0)),
                "vol": float(r.get("vol", 0)),
                "amount": float(r.get("amount", 0)),
                "buy_sell": str(r.get("buyorsell", "")),
            })
        logger.info("[mootdx] 历史分笔 %s %s: %d 条", code, date, len(out))
        return out
    except Exception as e:
        logger.warning("[mootdx] 历史分笔失败(%s, %s): %s", code, date, e)
        return None

# ── 信封包装（协议层：把"原始返回值"变成统一信封）──────────────

def _orderbook(code: str) -> Dict[str, Any]:
    data = _quote_mootdx(code)
    return {"object": data} if data else {"object": None}


def _ticks_today(code: str, limit: int = 2000) -> Dict[str, Any]:
    rows = _ticks_today_mootdx(code, limit)
    return {"rows": list(rows or [])}


def _ticks_history(code: str, date: str, limit: int = 2000) -> Dict[str, Any]:
    rows = _ticks_history_mootdx(code, date, limit)
    return {"rows": list(rows or [])}


# ── Provider 声明 ──────────────────────────────────────────────

from app.data_sources.provider_tape import register   # noqa: E402


@register
class MootdxTapeProvider:
    """mootdx（通达信 TCP）：盘口 + 当日/历史分笔。

    2026-10-28 起 **停用（`disabled=True`）**：TDX 行情数据面已断 —— 本机实测 9 台
    HQ 候选全部「协议通、bars 恒空」，`get_client()` 冷启 43.2s 后返 None。它此前是
    orderbook / ticks_* 的第一顺位（priority=10），每次盘口/分笔都先白等一轮判死。

    ⚠ 为什么**保留** `capabilities` 里的三个 kind 而不是摘掉：本族 `get_providers()`
    按「kind 是否声明」筛选，`tests/test_coord_tape.py:43-44` 把候选名单钉死成
    `[mootdx, sina, tencent, eastmoney_dc]`。摘 key 会让那两条断言红（改测试需用户点头），
    所以改用「声明保留 + fetch 入口直接回 NotSupported」：dispatch 判「该源不支持」
    ⇒ 跳过、不计失败、不打熔断、**零网络 IO**（见 dispatch._call_once）。

    ⚠ 遗留：那两条测试的名单已「名存实亡」（实际可用源只剩 sina/tencent/eastmoney_dc），
    等允许改测试时同步成 3 源。

    恢复接线：把 `disabled` 改成 False 即可（实现全部保留，无需改别处）。
    """

    name = "mootdx"
    priority = 10
    markets = {"CNStock"}

    capabilities = {
        "orderbook": _orderbook,
        "ticks_today": _ticks_today,
        "ticks_history": _ticks_history,
    }

    disabled = True

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        from app.data_sources.dispatch import NotSupported
        if self.disabled:
            return NotSupported("%s（mootdx 行情数据面已断，2026-10-28 停用）" % kind)
        fn = self.capabilities.get(kind)
        if fn is None:
            return NotSupported(kind)
        return fn(**params)
