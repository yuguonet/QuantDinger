"""
五档盘口 / 当日分笔 / 历史分笔 / 个股资金流向 — mootdx + 东财

功能:
  1. 五档实时行情 — 买一~买五 / 卖一~卖五 + 实时快照（最新价/涨跌/成交量等）
  2. 当日分笔成交 — 逐笔成交明细（仅交易时段可用，非交易时段返回空）
  3. 历史分笔成交 — 指定日期的逐笔成交明细（可用于复盘分析）
  4. 个股资金流向 — 当日分钟级 + 近120日日级（东财 push2 API）

数据源:
  五档行情:  mootdx(TCP) → 腾讯财经(HTTP)
  分笔数据:  mootdx(TCP) — 仅此一个源
  资金流向:  东财 push2 API — 分钟级实时 + 日级历史

依赖: pip install mootdx
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
#  mootdx 客户端管理（独立单例，与 index/finance 模块互不干扰）
# ══════════════════════════════════════════════════════════════

_client = None       # mootdx Quotes 实例
_client_ts = 0       # 上次连接成功的时间戳
_CLIENT_TTL = 3600   # 连接有效期: 1小时


def _get_client():
    """获取 mootdx 客户端单例（tape 模块专用）。

    与 index.py / finance.py 各自维护独立连接，
    避免一个模块的连接异常影响其他模块。

    Returns:
        mootdx.quotes.Quotes 实例，或 None（连接失败时）
    """
    global _client, _client_ts

    # 检查现有连接: 未过期 + 未关闭 → 复用
    if _client is not None and (time.time() - _client_ts) < _CLIENT_TTL:
        try:
            if not _client.closed:
                return _client
        except Exception:
            pass
        _client = None

    # 创建新连接
    try:
        from mootdx.quotes import Quotes
        _client = Quotes.factory(market='std', timeout=10, heartbeat=True)
        _client_ts = time.time()
        logger.info("[mootdx:tape] 连接成功")
        return _client
    except Exception as e:
        logger.warning("[mootdx:tape] 连接失败: %s", e)
        _client = None
        return None


def _market(code: str) -> int:
    """股票/指数代码 → 通达信市场号。

    通达信市场编码:
      1 = 上海证券交易所（代码以 000/88/99 开头的指数，或 60xxxx 开头的股票）
      0 = 深圳证券交易所（代码以 399 开头的指数，或 00xxxx/30xxxx 开头的股票）

    注意: 此函数对指数和股票都适用，但判断逻辑略有不同。
    这里用前3位判断，对股票来说 000 开头的会被判为上海（实际是深圳），
    但这个函数主要服务于指数场景，股票场景请用 _secid()。

    Args:
        code: 6位代码

    Returns:
        0 或 1（通达信市场号）
    """
    return 1 if code[:3] in ("000", "88", "99") else 0


# ══════════════════════════════════════════════════════════════
#  1. 五档实时行情（双数据源降级）
# ══════════════════════════════════════════════════════════════

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
            "volume": float(r.get("vol", 0)),        # 成交量（手）
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


# ══════════════════════════════════════════════════════════════
#  2. 当日分笔成交
# ══════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════
#  3. 历史分笔成交
# ══════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════
#  对外接口 — 分笔相关
# ══════════════════════════════════════════════════════════════

def get_order_book(code: str) -> Dict[str, Any]:
    """获取五档盘口 + 实时行情快照（自动降级）

    依次尝试 mootdx → 腾讯财经。

    Args:
        code: 股票/指数代码，如 "600519"、"000001"

    Returns:
        成功: {
            code, name, price, open, high, low, last_close, change, change_percent,
            volume, amount,
            bid: {bid1~bid5: {price, vol}},  # 买盘五档
            ask: {ask1~ask5: {price, vol}},  # 卖盘五档
        }
        失败: {code, error: "获取失败"}

    Example:
        >>> book = get_order_book("600519")
        >>> print(book["bid"]["bid1"]["price"])  # 买一价
    """
    data = _quote_mootdx(code)
    if data:
        return data

    data = _quote_tencent(code)
    if data:
        return data

    logger.error("所有数据源获取五档行情均失败: %s", code)
    return {"code": code, "error": "获取失败"}


def get_ticks_today(code: str, limit: int = 2000) -> List[Dict[str, Any]]:
    """获取当日分笔成交（仅交易时段可用）

    Args:
        code: 股票代码，如 "600519"
        limit: 最大条数，默认 2000（通达信协议上限）

    Returns:
        成功: [{time, price, vol, amount, buy_sell}, ...]
              buy_sell: "0"=主动买(外盘) "1"=主动卖(内盘) "2"=中性
        失败: []（非交易时段或网络异常）

    Note:
        - 仅在交易时段(9:15-15:00)有数据
        - 主动买: 以卖价成交（买方主动追高）
        - 主动卖: 以买价成交（卖方主动砸低）
        - 中性: 买卖价格相同（集合竞价等）
    """
    data = _ticks_today_mootdx(code, limit)
    if data:
        return data

    logger.error("获取当日分笔失败: %s", code)
    return []


def get_ticks_history(code: str, date: str, limit: int = 2000) -> List[Dict[str, Any]]:
    """获取历史分笔成交

    Args:
        code: 股票代码，如 "600519"
        date: 日期，支持 "YYYY-MM-DD" 或 "YYYYMMDD" 格式
        limit: 最大条数，默认 2000

    Returns:
        成功: [{time, price, vol, amount, buy_sell}, ...]
        失败: []

    Example:
        >>> ticks = get_ticks_history("600519", "2025-01-15")
        >>> big_buys = [t for t in ticks if t["buy_sell"] == "0" and t["amount"] > 1000000]
    """
    data = _ticks_history_mootdx(code, date, limit)
    if data:
        return data

    logger.error("获取历史分笔失败: %s %s", code, date)
    return []


# ══════════════════════════════════════════════════════════════
#  4. 个股资金流向
# ══════════════════════════════════════════════════════════════

# ── 4a. 基于分笔数据的资金流向计算（mootdx 主源）──

# 单笔成交金额阈值（元），用于划分订单类型
# 划分标准参考东方财富的分类方式
_SUPER_THRESHOLD = 2_000_000   # 超大单: 单笔 >= 200万元（通常是机构/游资大单）
_LARGE_THRESHOLD = 200_000     # 大单:   单笔 >= 20万元（中等规模资金）
_MID_THRESHOLD = 40_000        # 中单:   单笔 >= 4万元（散户大户级别）
# 小单: 单笔 < 4万元（普通散户）


def _classify_order(amount: float) -> str:
    """单笔成交金额 → 订单类型分类。

    分类标准:
      超大单(super) >= 200万:  通常是机构投资者、大型游资
      大单(large)   >= 20万:   中等规模资金、大户
      中单(mid)     >= 4万:    散户中的大户、小机构
      小单(small)   <  4万:    普通散户

    Args:
        amount: 单笔成交金额（元）

    Returns:
        "super" | "large" | "mid" | "small"
    """
    if amount >= _SUPER_THRESHOLD:
        return "super"
    if amount >= _LARGE_THRESHOLD:
        return "large"
    if amount >= _MID_THRESHOLD:
        return "mid"
    return "small"


def _calc_flow_from_ticks(ticks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从分笔数据计算资金流向统计。

    计算逻辑:
      1. 按 buy_sell 字段区分主动买入/卖出
      2. 按单笔金额分类为超大/大/中/小单
      3. 统计各类型的买入额、卖出额、净流入
      4. 主力净流入 = 超大单净流入 + 大单净流入

    净流入含义:
      正值 = 该类型资金净流入（买入 > 卖出）
      负值 = 该类型资金净流出（卖出 > 买入）

    Args:
        ticks: 分笔数据列表，每项需含 amount、buy_sell 字段

    Returns:
        {
            ticks_count: 分笔总条数,
            total_buy: 总主动买入额,
            total_sell: 总主动卖出额,
            total_net: 总净流入（= 买入 - 卖出）,
            main_net: 主力净流入（= 超大单 + 大单）,
            super_net: 超大单净流入,
            large_net: 大单净流入,
            mid_net: 中单净流入,
            small_net: 小单净流入,
            detail: {super/large/mid/small: {buy, sell, net}}
        }
    """
    # 初始化各类型的买入/卖出/净额累加器
    flow = {
        "super_net": 0.0, "super_buy": 0.0, "super_sell": 0.0,
        "large_net": 0.0,  "large_buy": 0.0,  "large_sell": 0.0,
        "mid_net": 0.0,    "mid_buy": 0.0,    "mid_sell": 0.0,
        "small_net": 0.0,  "small_buy": 0.0,  "small_sell": 0.0,
    }
    total_buy = 0.0
    total_sell = 0.0

    for t in ticks:
        amount = float(t.get("amount", 0))
        buy_sell = str(t.get("buy_sell", "2"))
        cat = _classify_order(amount)

        if buy_sell == "0":  # 主动买入（外盘）
            flow[f"{cat}_buy"] += amount
            flow[f"{cat}_net"] += amount
            total_buy += amount
        elif buy_sell == "1":  # 主动卖出（内盘）
            flow[f"{cat}_sell"] += amount
            flow[f"{cat}_net"] -= amount
            total_sell += amount
        # "2" 中性成交不计入买卖方向（如集合竞价撮合）

    # 主力净流入 = 超大单净流入 + 大单净流入
    main_net = flow["super_net"] + flow["large_net"]

    return {
        "ticks_count": len(ticks),
        "total_buy": round(total_buy, 2),
        "total_sell": round(total_sell, 2),
        "total_net": round(total_buy - total_sell, 2),
        "main_net": round(main_net, 2),
        "super_net": round(flow["super_net"], 2),
        "large_net": round(flow["large_net"], 2),
        "mid_net": round(flow["mid_net"], 2),
        "small_net": round(flow["small_net"], 2),
        "detail": {
            "super": {"buy": round(flow["super_buy"], 2), "sell": round(flow["super_sell"], 2), "net": round(flow["super_net"], 2)},
            "large": {"buy": round(flow["large_buy"], 2), "sell": round(flow["large_sell"], 2), "net": round(flow["large_net"], 2)},
            "mid":   {"buy": round(flow["mid_buy"], 2),   "sell": round(flow["mid_sell"], 2),   "net": round(flow["mid_net"], 2)},
            "small": {"buy": round(flow["small_buy"], 2), "sell": round(flow["small_sell"], 2), "net": round(flow["small_net"], 2)},
        },
    }


def get_fund_flow_from_ticks(code: str, limit: int = 2000) -> Dict[str, Any]:
    """通过 mootdx 分笔数据计算当日资金流向（主源）

    原理: 拉取当日所有分笔成交，按单笔金额分类统计买卖方向，
    得出各类型资金的净流入/流出情况。

    适用场景:
      - 实时监控盘中资金动向
      - 判断主力（超大单+大单）是在吸筹还是出货
      - 仅交易时段有数据

    Args:
        code: 股票代码，如 "600519"
        limit: 最大分笔条数，默认 2000

    Returns:
        成功: {code, source, ticks_count, total_buy, total_sell, total_net,
               main_net, super_net, large_net, mid_net, small_net, detail}
        失败: {code, source, error: "非交易时段或无数据"}

    Example:
        >>> flow = get_fund_flow_from_ticks("600519")
        >>> if flow.get("main_net", 0) > 0:
        ...     print(f"主力净流入 {flow['main_net']/10000:.0f} 万")
    """
    ticks = _ticks_today_mootdx(code, limit)
    if not ticks:
        return {"code": code, "source": "mootdx", "error": "非交易时段或无数据"}

    result = _calc_flow_from_ticks(ticks)
    result["code"] = code
    result["source"] = "mootdx"
    return result


def get_fund_flow_history_from_ticks(code: str, date: str, limit: int = 2000) -> Dict[str, Any]:
    """通过 mootdx 历史分笔计算指定日期的资金流向

    用于复盘分析：查看某天的主力资金动向。

    Args:
        code: 股票代码
        date: 日期 "YYYY-MM-DD" 或 "YYYYMMDD"
        limit: 最大分笔条数

    Returns:
        同 get_fund_flow_from_ticks，额外包含 date 字段

    Example:
        >>> flow = get_fund_flow_history_from_ticks("600519", "2025-01-15")
    """
    ticks = _ticks_history_mootdx(code, date, limit)
    if not ticks:
        return {"code": code, "date": date, "source": "mootdx", "error": "无数据"}

    result = _calc_flow_from_ticks(ticks)
    result["code"] = code
    result["date"] = date
    result["source"] = "mootdx"
    return result


# ── 4b. 东财资金流向 API（分钟级实时 + 日级历史）──

# 通用 User-Agent，模拟浏览器请求
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _secid(code: str) -> str:
    """股票代码 → 东财 secid 编码。

    东财接口需要 secid 格式: "市场号.代码"
      - 上海股票: "1.600519"（60xxxx 开头）
      - 深圳股票: "0.000858"（00xxxx/30xxxx 开头）

    注意: 与通达信的市场号规则相同但格式不同。

    Args:
        code: 6位股票代码

    Returns:
        "1.代码" 或 "0.代码"
    """
    # 60开头 = 上海(1)，其余(00/30开头) = 深圳(0)
    return f"1.{code}" if code.startswith("6") else f"0.{code}"


def _safe_float(v) -> float:
    """安全的浮点数转换，异常时返回 0.0。

    东财 API 返回的数值可能是字符串、None 或空值，
    用此函数统一处理，避免 try/except 散落在各处。

    Args:
        v: 任意值

    Returns:
        四舍五入到4位小数的浮点数，或 0.0
    """
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return 0.0


def get_fund_flow_realtime(code: str) -> Dict[str, Any]:
    """获取当日分钟级资金流向（东财 push2 API）

    数据来源: 东方财富数据中心
    接口: push2.eastmoney.com/api/qt/stock/fflow/kline/get

    返回的每一行代表一分钟内的资金流向:
      main_net   = 主力净流入（超大单 + 大单）
      small_net  = 小单净流入
      mid_net    = 中单净流入
      large_net  = 大单净流入
      super_net  = 超大单净流入

    Args:
        code: 股票代码，如 "600519"

    Returns:
        成功: {
            code: "600519",
            points: 240,          # 数据点数（一个交易日约 240 分钟）
            total_main_net: 1.5e8, # 全日主力净流入（元）
            data: [{
                time: "09:30",
                main_net: 1234567.89,   # 主力净流入（元）
                small_net: -500000.00,  # 小单净流出
                mid_net: 200000.00,
                large_net: 800000.00,
                super_net: 434567.89,
            }, ...]
        }
        失败: {code, error: "..."}

    Example:
        >>> flow = get_fund_flow_realtime("600519")
        >>> print(f"主力净流入: {flow['total_main_net']/10000:.0f}万")
    """
    import requests

    url = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
    params = {
        "secid": _secid(code),  # 东财编码，如 "1.600519"
        "klt": 1,               # 1 = 分钟级别
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }
    headers = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}

    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        d = r.json()

        rows = []
        for line in d.get("data", {}).get("klines", []):
            p = line.split(",")  # 逗号分隔: time,main,small,mid,large,super
            if len(p) >= 6:
                rows.append({
                    "time": p[0],
                    "main_net": _safe_float(p[1]),      # 主力净流入
                    "small_net": _safe_float(p[2]),      # 小单净流入
                    "mid_net": _safe_float(p[3]),        # 中单净流入
                    "large_net": _safe_float(p[4]),      # 大单净流入
                    "super_net": _safe_float(p[5]),      # 超大单净流入
                })

        # 汇总全日主力净流入
        total_main = sum(r["main_net"] for r in rows)

        return {
            "code": code,
            "points": len(rows),
            "total_main_net": round(total_main, 2),
            "data": rows,
        }
    except Exception as e:
        logger.warning("[eastmoney] 分钟资金流失败(%s): %s", code, e)
        return {"code": code, "error": str(e)}


# ══════════════════════════════════════════════════════════════
#  5. 实时换手率
# ══════════════════════════════════════════════════════════════

# 缓存流通股本，避免每次换手率计算都调 finance 接口
# 格式: {code: (liutongguben_万股, cache_timestamp)}
_finance_cache: Dict[str, tuple] = {}
_FINANCE_CACHE_TTL = 86400  # 流通股本数据缓存 24 小时（股本变动频率很低）


def _get_liutongguben(code: str) -> Optional[float]:
    """获取流通股本（万股），带 24h 缓存。

    流通股本来源于通达信 get_finance_info 接口。
    股本变动频率很低（增发/回购/解禁才会变），所以缓存 24 小时足够。

    Args:
        code: 股票代码

    Returns:
        流通股本（万股），失败返回 None
    """
    now = time.time()

    # 检查缓存
    if code in _finance_cache:
        val, ts = _finance_cache[code]
        if (now - ts) < _FINANCE_CACHE_TTL:
            return val

    # 从 mootdx finance 接口拉取
    try:
        from mootdx.quotes import Quotes
        cli = _get_client()
        if cli is None:
            return None

        mkt = 1 if code.startswith("6") else 0
        result = cli.client.get_finance_info(mkt, code)
        if not result:
            return None

        r = result[0] if isinstance(result, list) and result else result
        if isinstance(r, dict):
            val = float(r.get("liutongguben", 0))
            if val > 0:
                _finance_cache[code] = (val, now)
                return val
    except Exception as e:
        logger.warning("[tape] 获取流通股本失败(%s): %s", code, e)

    return None


def get_realtime_turnover(code: str) -> Dict[str, Any]:
    """获取实时换手率。

    换手率 = 当日成交量 / 流通股本 × 100%

    含义:
      - 反映股票当日的交易活跃度
      - 换手率 > 5%: 较为活跃
      - 换手率 > 10%: 高度活跃（可能是游资接力、新股、题材炒作）
      - 换手率 < 1%: 低迷（可能是大盘蓝筹或冷门股）

    计算方式:
      1. 从 mootdx quotes 获取当日成交量（单位: 股）
      2. 从 mootdx finance 获取流通股本（单位: 万股）
      3. 换手率(%) = 成交量(股) / (流通股本(万股) × 10000) × 100

    Args:
        code: 股票代码，如 "600519"

    Returns:
        成功: {
            code: "600519",
            name: "贵州茅台",
            volume: 12345678,          # 当日成交量（股）
            liutongguben: 125619.78,   # 流通股本（万股）
            turnover_rate: 0.98,       # 换手率（%）
            price: 1800.00,            # 最新价
            change_percent: 1.23,      # 涨跌幅(%)
        }
        失败: {code, error: "..."}

    Example:
        >>> t = get_realtime_turnover("600519")
        >>> print(f"换手率: {t['turnover_rate']:.2f}%")
    """
    # Step 1: 获取实时行情（含成交量）
    quote = _quote_mootdx(code) or _quote_tencent(code)
    if not quote:
        return {"code": code, "error": "获取实时行情失败"}

    volume = quote.get("volume", 0)  # 成交量（股）
    if volume <= 0:
        return {
            "code": code,
            "name": quote.get("name", ""),
            "volume": 0,
            "turnover_rate": 0.0,
            "price": quote.get("price", 0),
            "change_percent": quote.get("change_percent", 0),
            "note": "非交易时段或无成交",
        }

    # Step 2: 获取流通股本
    liutong = _get_liutongguben(code)
    if not liutong or liutong <= 0:
        return {
            "code": code,
            "name": quote.get("name", ""),
            "volume": volume,
            "liutongguben": None,
            "turnover_rate": None,
            "price": quote.get("price", 0),
            "change_percent": quote.get("change_percent", 0),
            "error": "无法获取流通股本",
        }

    # Step 3: 计算换手率
    # volume 单位是股，liutong 单位是万股，统一为股
    liutong_shares = liutong * 10000  # 万股 → 股
    turnover_rate = round(volume / liutong_shares * 100, 4)

    return {
        "code": code,
        "name": quote.get("name", ""),
        "volume": volume,                  # 当日成交量（股）
        "liutongguben": liutong,           # 流通股本（万股）
        "turnover_rate": turnover_rate,    # 换手率（%）
        "price": quote.get("price", 0),
        "change_percent": quote.get("change_percent", 0),
    }


# ══════════════════════════════════════════════════════════════
#  6. 实时主力净流入（整合版）
# ══════════════════════════════════════════════════════════════

def get_realtime_main_flow(code: str) -> Dict[str, Any]:
    """获取实时主力净流入（整合东财 + mootdx 双源）。

    主力 = 超大单(>=200万) + 大单(>=20万)
    净流入 = 主动买入金额 - 主动卖出金额

    数据源优先级:
      1. 东财 push2 API — 分钟级精度，覆盖面广
      2. mootdx 分笔计算 — TCP 直连，精度高但仅交易时段可用

    Args:
        code: 股票代码，如 "600519"

    Returns:
        成功: {
            code: "600519",
            name: "贵州茅台",
            source: "eastmoney" | "mootdx",
            main_net: 123456789.00,       # 主力净流入（元）
            main_net_wan: 12345.68,       # 主力净流入（万元）
            main_net_yi: 1.23,            # 主力净流入（亿元）
            super_net: 80000000.00,       # 超大单净流入（元）
            large_net: 43456789.00,       # 大单净流入（元）
            mid_net: -20000000.00,        # 中单净流入（元）
            small_net: -50000000.00,      # 小单净流入（元）
            total_buy: 300000000.00,      # 总主动买入（元）
            total_sell: 250000000.00,     # 总主动卖出（元）
            price: 1800.00,               # 最新价
            change_percent: 1.23,         # 涨跌幅(%)
            points: 240,                  # 数据点数（东财模式）
        }
        失败: {code, error: "..."}

    Example:
        >>> flow = get_realtime_main_flow("600519")
        >>> yi = flow.get("main_net_yi", 0)
        >>> print(f"主力净流入: {yi:.2f} 亿")
    """
    result = None
    source = None

    # 数据源1: 东财分钟级资金流（覆盖面广，非交易时段也有历史数据）
    ef = get_fund_flow_realtime(code)
    if not ef.get("error") and ef.get("data"):
        # 汇总主力净流入
        total_main = sum(r.get("main_net", 0) for r in ef["data"])
        last = ef["data"][-1] if ef["data"] else {}
        result = {
            "main_net": round(total_main, 2),
            "super_net": round(sum(r.get("super_net", 0) for r in ef["data"]), 2),
            "large_net": round(sum(r.get("large_net", 0) for r in ef["data"]), 2),
            "mid_net": round(sum(r.get("mid_net", 0) for r in ef["data"]), 2),
            "small_net": round(sum(r.get("small_net", 0) for r in ef["data"]), 2),
            "points": ef.get("points", 0),
        }
        source = "eastmoney"

    # 数据源2: mootdx 分笔计算（精度高，仅交易时段）
    if result is None:
        mf = get_fund_flow_from_ticks(code)
        if not mf.get("error"):
            result = {
                "main_net": mf.get("main_net", 0),
                "super_net": mf.get("super_net", 0),
                "large_net": mf.get("large_net", 0),
                "mid_net": mf.get("mid_net", 0),
                "small_net": mf.get("small_net", 0),
                "total_buy": mf.get("total_buy", 0),
                "total_sell": mf.get("total_sell", 0),
                "ticks_count": mf.get("ticks_count", 0),
            }
            source = "mootdx"

    if result is None:
        return {"code": code, "error": "所有数据源获取主力资金流均失败"}

    # 获取行情快照（最新价、涨跌幅）
    quote = _quote_mootdx(code) or _quote_tencent(code)

    main_net = result["main_net"]
    return {
        "code": code,
        "name": quote.get("name", "") if quote else "",
        "source": source,
        "main_net": main_net,                          # 元
        "main_net_wan": round(main_net / 10000, 2),    # 万元
        "main_net_yi": round(main_net / 1e8, 2),       # 亿元
        "super_net": result.get("super_net", 0),
        "large_net": result.get("large_net", 0),
        "mid_net": result.get("mid_net", 0),
        "small_net": result.get("small_net", 0),
        "total_buy": result.get("total_buy", 0),
        "total_sell": result.get("total_sell", 0),
        "points": result.get("points", result.get("ticks_count", 0)),
        "price": quote.get("price", 0) if quote else None,
        "change_percent": quote.get("change_percent", 0) if quote else None,
    }


# ══════════════════════════════════════════════════════════════
#  7. 实时综合快照（一次调用拿全部指标）
# ══════════════════════════════════════════════════════════════

def get_realtime_snapshot(code: str) -> Dict[str, Any]:
    """获取个股实时综合快照 — 一次调用返回行情+五档+换手率+主力资金。

    整合本模块所有实时数据，适合做盯盘面板或实时监控。

    Args:
        code: 股票代码，如 "600519"

    Returns:
        {
            code: "600519",
            name: "贵州茅台",
            price: 1800.00,
            open: 1790.00,
            high: 1810.00,
            low: 1785.00,
            last_close: 1790.00,
            change: 10.00,
            change_percent: 0.56,
            volume: 12345678,
            amount: 22222222222,
            turnover_rate: 0.98,         # 换手率(%)
            liutongguben: 125619.78,     # 流通股本(万股)
            main_net: 123456789.00,      # 主力净流入(元)
            main_net_wan: 12345.68,      # 主力净流入(万元)
            main_net_yi: 1.23,           # 主力净流入(亿元)
            super_net: 80000000.00,      # 超大单净(元)
            large_net: 43456789.00,      # 大单净(元)
            mid_net: -20000000.00,       # 中单净(元)
            small_net: -50000000.00,     # 小单净(元)
            flow_source: "eastmoney",    # 资金数据源
            bid: {bid1~bid5: {price, vol}},
            ask: {ask1~ask5: {price, vol}},
        }

    Example:
        >>> s = get_realtime_snapshot("600519")
        >>> print(f"{s['name']} {s['price']} 换手{s['turnover_rate']:.2f}% 主力{s['main_net_yi']:.2f}亿")
    """
    # 行情 + 五档
    quote = _quote_mootdx(code) or _quote_tencent(code)
    if not quote:
        return {"code": code, "error": "获取行情失败"}

    # 换手率
    volume = quote.get("volume", 0)
    liutong = _get_liutongguben(code)
    turnover_rate = None
    if liutong and liutong > 0 and volume > 0:
        turnover_rate = round(volume / (liutong * 10000) * 100, 4)

    # 主力资金
    flow = get_realtime_main_flow(code)

    return {
        "code": code,
        "name": quote.get("name", ""),
        # 行情
        "price": quote.get("price", 0),
        "open": quote.get("open", 0),
        "high": quote.get("high", 0),
        "low": quote.get("low", 0),
        "last_close": quote.get("last_close", 0),
        "change": quote.get("change", 0),
        "change_percent": quote.get("change_percent", 0),
        "volume": volume,
        "amount": quote.get("amount", 0),
        # 换手率
        "turnover_rate": turnover_rate,
        "liutongguben": liutong,
        # 主力资金
        "main_net": flow.get("main_net"),
        "main_net_wan": flow.get("main_net_wan"),
        "main_net_yi": flow.get("main_net_yi"),
        "super_net": flow.get("super_net"),
        "large_net": flow.get("large_net"),
        "mid_net": flow.get("mid_net"),
        "small_net": flow.get("small_net"),
        "flow_source": flow.get("source"),
        # 五档盘口
        "bid": quote.get("bid"),
        "ask": quote.get("ask"),
    }


def get_fund_flow_daily(code: str, days: int = 120) -> Dict[str, Any]:
    """获取近 N 日日级资金流向（东财 push2his API）

    数据来源: 东方财富数据中心（历史数据）
    接口: push2his.eastmoney.com/api/qt/stock/fflow/daykline/get

    用途:
      - 分析近 120 日的主力资金趋势
      - 判断机构是在持续建仓还是减仓
      - 配合股价走势做资金面分析

    Args:
        code: 股票代码，如 "600519"
        days: 获取天数，默认 120（约半年交易日）

    Returns:
        成功: {
            code: "600519",
            total_days: 120,           # 实际返回天数
            recent_20d_main_net: 5e8,  # 最近20日主力累计净流入（元）
            data: [{
                date: "2025-01-15",
                main_net: 12345678.90,
                small_net: -5000000.00,
                mid_net: 2000000.00,
                large_net: 8000000.00,
                super_net: 4345678.90,
            }, ...]
        }
        失败: {code, error: "..."}

    Example:
        >>> flow = get_fund_flow_daily("600519", 60)
        >>> recent = flow["data"][-5:]  # 最近5天
        >>> for day in recent:
        ...     print(f"{day['date']}: 主力净流入 {day['main_net']/10000:.0f}万")
    """
    import requests

    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "secid": _secid(code),
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "lmt": str(days),  # limit: 返回天数上限
    }
    headers = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}

    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        d = r.json()

        rows = []
        for line in d.get("data", {}).get("klines", []):
            p = line.split(",")
            if len(p) >= 6:
                rows.append({
                    "date": p[0],
                    "main_net": _safe_float(p[1]),
                    "small_net": _safe_float(p[2]),
                    "mid_net": _safe_float(p[3]),
                    "large_net": _safe_float(p[4]),
                    "super_net": _safe_float(p[5]),
                })

        # 计算最近20个交易日的主力累计净流入（约一个交易月）
        recent_20 = rows[-20:] if len(rows) >= 20 else rows

        return {
            "code": code,
            "total_days": len(rows),
            "recent_20d_main_net": round(sum(r["main_net"] for r in recent_20), 2),
            "data": rows,
        }
    except Exception as e:
        logger.warning("[eastmoney] 日级资金流失败(%s): %s", code, e)
        return {"code": code, "error": str(e)}
