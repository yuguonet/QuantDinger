# -*- coding: utf-8 -*-
"""
Data tools — real-time quotes, K-lines, stock info, market indices.
Wraps DataSourceFactory into OpenAI-function-callable tools.
"""
from __future__ import annotations

import json
import os
from ...log import logger
from typing import Any, Dict, List, Optional

from ._analysis_utils import _get_ds
from ...utils.md_format import _batch_execute, _to_md

# ── 指数行情 / 市场概览 ──────────────────────────────────────

def _get_market_indices() -> dict:
    """指数行情：返回上证/深证/创业板/科创/北证五大指数的价格、涨跌幅、成交量。

    Returns:
        {"count": N, "indices": [{code, name, price, change_percent, ...}]} —— dict，
        指数列表在二级键 indices（list）；失败 → {"error": ...}
    """
    from app.market_cn.index import get_index_realtime as _get
    try:
        data = _get()
        return {"count": len(data), "indices": data}
    except Exception as e:
        logger.warning("_get_market_indices failed: %s", e)
        return {"error": str(e)}

def get_market_overview() -> dict:
    """市场概览：市场情绪指数、主力资金流向、指数涨跌概况。不含北向资金字段（北向实时
    披露已停，属数据源性死亡而非临时故障；日级历史 get_northbound_daily 不受影响）。

    注意字段语义：up_count/down_count 是两大指数（上证/深成）的涨跌抽样，不是全市场
    涨跌家数——全市场维度请用 get_index_quote(scope="indices") 汇总。

    Returns:
        dict: {up_count, down_count(两大指数抽样涨跌家数), emotion(0-100情绪),
        main_net_yi(主力净流亿元), main_pct}。单源失败则该键缺失，无 error 键
    """
    result = {}

    # 指数行情 → 涨跌概况（仅两大指数抽样，非全市场）
    try:
        from app.market_cn.index import get_index_realtime
        indices = get_index_realtime(["000001", "399001"])
        up = sum(1 for i in (indices or []) if i.get("change_percent", 0) > 0)
        down = sum(1 for i in (indices or []) if i.get("change_percent", 0) < 0)
        result["up_count"] = up
        result["down_count"] = down
    except Exception:
        pass

    # 情绪指数
    try:
        from app.market_cn.fear_greed_index import fear_greed_index
        fg = fear_greed_index()
        result["emotion"] = int(fg.get("composite_score", 50))
    except Exception:
        pass

    # 主力资金流
    try:
        from app.market_cn.index import get_market_fund_flow_realtime
        mf = get_market_fund_flow_realtime()
        result["main_net_yi"] = round(mf.get("main_net", 0) / 1e8, 2)
        result["main_pct"] = round(mf.get("main_pct", 0), 2)
    except Exception:
        pass

    return result

# ── Tool functions ────────────────────────────────────────────

def resolve_stock(keyword: str, market: str = "CNStock", limit: int = 10) -> Dict[str, Any]:
    """股票名称/代码双向解析。输入名称返回代码，输入代码返回名称，支持模糊搜索。

    Args:
        keyword: 搜索关键词（中文名称、代码、简拼等）
        market: 市场，默认 CNStock
        limit: 返回数量上限，默认10

    Returns:
        单只 → {"code": "...", "name": "...", "market": "..."}
        多只 → {"count": N, "data": [{"code": "...", ...}, ...]}
        失败 → {"error": "...", "retriable": false}
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return {"error": "搜索关键词不能为空", "retriable": False}

    limit = min(max(limit, 1), 50)
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        matches = get_stock_basic_db().search_stocks(keyword, limit=limit)
        items = [
            {"code": m["symbol"], "name": m.get("name", ""), "market": m.get("market_cn", market)}
            for m in matches
        ]
        if len(items) == 1:
            return items[0]
        return {"count": len(items), "data": items}
    except Exception as e:
        logger.error("resolve_stock(%s) failed: %s", keyword, e)
        return {"error": str(e), "retriable": False}


# volume 归一化细节（2026-10-22 自 docstring 移出，注入瘦身）：腾讯/搜狐/tdx
# 原始为手已 ×100，新浪/同花顺/百度原生为股——六个 provider 出口已各自归一化为股。
def get_realtime_quote(codes: str) -> Dict[str, Any]:
    """实时行情：最新价、涨跌幅、成交量、开高低收、名称、行情时间 + 涨跌停价。

    Args:
        codes: 多股用逗号分隔

    Returns:
        统一结构（单/多代码一致）：{"count": N, "data": {代码: 行情dict},
        "error": None}；行情dict 字段：{stock_code, last, changePercent, volume, name,
        high, low, open, previousClose, time, limit_up, limit_down}
        （limit_* 来自腾讯源增补，供涨跌停判定）；volume 单位见 volume_unit（恒 "股"，
        六个 provider 出口已各自归一化为股）；失败 → {"error": "...", "retriable": False}

    ⚠ **不含**换手率/量比/PE/PB/总市值（描述是唯一进 LLM 的契约，写了就等于教模型去
    承诺拿不到的数据）。要这些口径请用：
      · 换手率/PE/PB/总市值 → `get_stock_info`（turnover_pct/pe_ttm/pb/mcap_yi）
      · 量比/量能 → `technical_analysis`（量价维聚合）
      · 盘中 VWAP/开盘区间/分时均线 → `get_intraday_structure`（本工具只有报价快照）
    """
    # 归一化：剥离市场后缀（.SZ/.SH）与市场前缀（sh/sz），与 get_tickers 的
    # symbol 键对齐（get_tickers 返回的 symbol 是纯代码，如 "300497"）。
    # 若不归一化，.SZ 后缀输入会查不到 → 误报"未获取到行情"（2026-09-22 修复）。
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}
    code_list = [_strip_prefix(c) for c in code_list]

    ds = _get_ds("CNStock")

    # 批量接口：一次 coordinate_tickers 拉全部
    try:
        tickers = ds.get_tickers(code_list)
    except Exception as e:
        logger.error("get_realtime_quote batch failed: %s", e)
        tickers = []

    # 按 symbol 索引
    ticker_map: Dict[str, Dict] = {}
    for t in tickers:
        sym = t.get("symbol", "")
        if sym:
            ticker_map[sym] = t

    results: Dict[str, Any] = {}
    for code in code_list:
        t = ticker_map.get(code)
        if t:
            # volume_unit 放 **t 之后：provider 若自带单位键不得覆盖工具层口径
            results[code] = {"stock_code": code, "market": "CNStock", **t,
                             "volume_unit": "股"}
        else:
            results[code] = {"error": "未获取到行情", "stock_code": code}

    # 2026-09-24：并入统一三键契约（与 _batch_execute 系一致，消除单/多二义性）；
    # 并增补涨跌停价（get_tickers 无此字段，模型做创业板 20% 判定需要）——
    # 一次批量腾讯调用补全，失败不阻塞主结果（limit_up 缺省 None）。
    try:
        raw_q = _tencent_quote_raw(code_list)
        for code in code_list:
            tq = raw_q.get(code) or {}
            item = results.get(code)
            if isinstance(item, dict) and "error" not in item:
                item.setdefault("limit_up", tq.get("limit_up"))
                item.setdefault("limit_down", tq.get("limit_down"))
    except Exception as e:
        logger.debug("get_realtime_quote 涨跌停增补跳过: %s", e)
    return {"count": len(results), "data": results, "error": None}

def agent_get_kline(codes, timeframe: str = "1D", days: int = 30, adj: str = "qfq") -> Dict[str, Any]:
    """K线数据：返回统一结构的 OHLCV 序列，支持 A 股，1D/1W 走本地库直连。
    ⚠ 仅在需要原始数据或自定义计算时调用（趋势/指标/形态/量价/筹码分析已内置取数）。
    ⚠ 盘中结构/多周期方向不要从本工具原始序列自己算，用现成结论：
      · VWAP/开盘区间/分时均线 → `get_intraday_structure`
      · 日线+15m+5m 方向一致/冲突 → `get_multiframe_direction`

    Args:
        codes: 股票代码，字符串（如 "603466"）或代码列表，多股逗号分隔
        timeframe: 1m/5m/15m/30m/1H/4H/1D/1W，默认 1D
        days: 天数，默认 30，最大 250
        adj: 复权口径 qfq(前复权,默认)/hfq(后复权)/none(不复权)。data 各 bar 含 adj 标注。

    Returns:
        {"count": N, "data": {代码: [{"t","o","h","l","c","v"}, ...]},
         "summary": {代码: {ma5/ma10/ma20/ma60/ma120, high_20d/low_20d(含日期),
         high_60d/low_60d, vol_ma5/vol_ratio, total_days}}}；
        summary 是聚合摘要（窗口不足的档位**不给该键**）；data 恒为"代码→K线列表"映射，
        取单只用 data[codes]；失败 → {"error": "...", "retriable": bool}（error 键恒参与）。
    """
    # codes 容错：模型可能传 list / list[tuple] / 数字 / 带前后缀串
    # ── days 默认 30 的依据（2026-10-21 实测；论证留此处不进 docstring：
    #    docstring 是工具契约，模型逐轮读，只该讲"干什么/怎么传/返回什么"）──
    #   ① MA5..MA120 由 technical_analysis（ma20/ma60）与 bull_bear_research
    #      （ma5..ma120）结构化字段直接给出，不需要从长表自己算；
    #   ② 实测 120 天表 4,212 字符，而模型输出只引用 4 个日期（最近 6–20 个交易日
    #      内的前高/前低/套牢区/顶底分型）⇒ 30 天覆盖有余。
    # 权威口径落工具层：auto-* 技能会被 chain/skill_brewer.maybe_revise 重写，
    #   写在 skills/*/SKILL.md 里的 days 会被覆盖。
    if isinstance(codes, (list, tuple)):
        _parts = []
        for c in codes:
            if isinstance(c, (list, tuple)) and c:
                c = c[0]
            _parts.append(str(c))
        codes = ",".join(_parts)
    else:
        codes = str(codes)

    raw_codes = [c.strip() for c in codes.split(",") if c.strip()][:20]
    # 查询用裸码（DB/远端按裸码），返回键用原始入参码（模型视角一致）——
    # 2026-09-22 修复：入参 '600519.SH' strip 后用裸码做返回键，模型按
    # data['600519.SH'] 取值落空，指标全缺（07:19 轮红字根因）。
    code_list = [_strip_prefix(c) for c in raw_codes]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    valid_timeframes = {"1m", "5m", "15m", "30m", "1H", "4H", "1D", "1W"}
    if timeframe not in valid_timeframes:
        return {"error": f"无效周期: {timeframe}，可选: {','.join(sorted(valid_timeframes))}", "retriable": False}
    _days = min(max(int(days), 1), 250)

    # 直连 CNStockDataSource（cn_stock.py）：1D/1W 本地 DB + TTL 快路径，
    # 盘中分钟线走远端。不再经 coordinator 二次包装。
    from app.data_sources.cn_stock import CNStockDataSource

    def _fetch(stock_code: str) -> list:
        try:
            ds = CNStockDataSource()
            bars = ds.get_kline(stock_code, timeframe, _days) or []
            # ds.get_kline 内部已 unadj→qfq；adj 要求 hfq/none 时再换算（单一事实源 adjustment）
            _adj = (adj or "qfq").lower()
            if _adj in ("none", "raw", "unadj"):
                try:
                    from app.data_sources.provider.adjustment import reverse_fwd_adjust
                    bars = reverse_fwd_adjust(bars, stock_code) or bars
                except Exception as _adj_err:
                    logger.warning("agent_get_kline adj=none 转换失败(%s): %s", stock_code, _adj_err)
            elif _adj == "hfq":
                try:
                    from app.data_sources.provider.adjustment import (
                        reverse_fwd_adjust, unadj_to_hfq,
                    )
                    raw = reverse_fwd_adjust(bars, stock_code) or bars
                    bars = unadj_to_hfq(raw, stock_code) or raw
                except Exception as _adj_err:
                    logger.warning("agent_get_kline adj=hfq 转换失败(%s): %s", stock_code, _adj_err)
            return bars
        except Exception as e:
            logger.error("agent_get_kline.get_kline(%s, %s, %d) failed: %s",
                         stock_code, timeframe, _days, e)
            return []

    def _ts_to_date(ts) -> str:
        try:
            from datetime import datetime
            return datetime.fromtimestamp(int(ts)).strftime("%m-%d")
        except Exception:
            return str(ts)

    results: Dict[str, Any] = {}
    summaries: Dict[str, Any] = {}
    for raw, code in zip(raw_codes, code_list):
        klines = _fetch(code)
        rows = [{
            "t": k.get("date") or _ts_to_date(k.get("time", 0)),
            "o": round(k.get("open", 0), 2),
            "h": round(k.get("high", 0), 2),
            "l": round(k.get("low", 0), 2),
            "c": round(k.get("close", 0), 2),
            "v": k.get("volume", 0),
        } for k in klines]
        results[raw] = rows
        # ★ 2026-10-21 聚合摘要：均线 / 关键高低点 / 量能 —— 模型不必再从
        #   明细表逐行自己算（实测"剔除某日后合计 -2.40 亿（算式：-1.01-0.33…）"
        #   就是把明细当账本翻）。窗口不足的档位**不给键**（不回落 0）。
        try:
            from ._analysis_utils import _kline_summary
            # 2026-10-22：透传 timeframe/末根时间戳 —— 量比盘中归一化只对日线生效，
            # 且要判「末根是否今天」（分钟线/周线语义不同，见 volume_ratio_summary）。
            summaries[raw] = _kline_summary(
                rows, timeframe=timeframe,
                last_bar_ts=(klines[-1].get("time") if klines else None))
        except Exception as e:
            logger.debug("[agent_get_kline] summary 失败(fail-open) %s: %s",
                         type(e).__name__, e)

    # 统一形态：单/多代码都是 {count, data: {code: [...]}}，error 键恒在
    # （2026-09-21 修复：旧版单代码返回裸 list、多代码返回 dict 的二义性导致
    #  模型拿 list 当 dict 切片 → TypeError "string indices must be integers"，
    #  连续两次触发熔断，是 step 爆炸的主要推手之一）
    # ★ summary 同构：{代码: {ma5/ma10/ma20/ma60/ma120/high_20d/…}}，多代码平铺。
    return {"count": len(results), "data": results,
            "summary": summaries,
            "adj": (adj or "qfq").lower(), "error": None}

# ── 核心字段集（Agent 日常分析最常用的 ~15 个字段） ──────────────────────
_STOCK_INFO_CORE_FIELDS = {
    "stock_code", "name", "industry", "concepts",
    "price", "change_pct",
    "pe_ratio", "pe_ttm", "pe_static", "pb_ratio",
    "market_cap", "mcap_yi", "float_market_cap", "float_mcap_yi",
    "roe", "eps", "bvps",
    "total_shares", "circ_shares",
    "turnover_pct", "vol_ratio",
    "list_date", "main_business",
    "source", "market_cn",
}
def _filter_stock_info(info: Dict[str, Any], detail: bool = False) -> Dict[str, Any]:
    """过滤股票信息，去除 None 值；非 detail 模式只保留核心字段。"""
    if not isinstance(info, dict):
        return info
    # error 直接透传，不过滤
    if "error" in info:
        return info
    if detail:
        return {k: v for k, v in info.items() if v is not None}
    return {k: v for k, v in info.items() if v is not None and k in _STOCK_INFO_CORE_FIELDS}

def get_stock_info(codes: str, detail: bool = False) -> Dict[str, Any]:
    """股票基本信息（精简模式，节省 token）：默认返回核心字段——名称、行业、价格、PE/PB、
    市值、ROE、EPS、股本等。数据量小，Agent 直接评估更靠谱，不出评分。如需完整财务数据
    （利润表/资产负债表/现金流/股东/杜邦），设置 detail=true。

    Args:
        codes: 多股用逗号分隔
        detail: false=精简（默认），true=完整（50+字段）

    Returns:
        单代码 → 扁平 dict（核心字段 name/price/pe_ratio/mcap_yi 等，detail=true 时
        50+字段）；多代码 → {"count": N, "data": {代码: dict}}；失败 → {"error": ...}
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    # 缓存检查已移除：basicinfo_db 本地快读 + HTTP 5s 竞赛替代

    def _one(stock_code: str) -> Dict[str, Any]:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        from app.data_sources.normalizer import strip_market_prefix

        sym = strip_market_prefix(stock_code)

        # ── 1) basicinfo_db 本地快读（毫秒级）──
        db_result: Dict[str, Any] = {}
        try:
            from app.utils.basicinfo_db import get_stock_basic_db
            db = get_stock_basic_db()
            stock = db.get_stock(sym)
            if stock:
                db_result = {"stock_code": sym}
                for fld in ("name", "industry", "total_shares", "circ_shares",
                            "market_cn", "list_date"):
                    val = stock.get(fld)
                    if val is not None and val != "":
                        db_result[fld] = val
                concepts_str = stock.get("concepts", "")
                if concepts_str:
                    db_result["concepts"] = [c.strip() for c in concepts_str.split(",") if c.strip()]
                db_result["source"] = "basicinfo_db"
        except Exception as e:
            logger.warning("get_stock_info(%s) basicinfo_db failed: %s", stock_code, e)

        # ── 2) HTTP 实时拉取（5s 超时竞赛）──
        def _http_fetch() -> Dict[str, Any]:
            # 数据源原生
            try:
                ds = _get_ds("CNStock")
                if hasattr(ds, "get_stock_info"):
                    info = ds.get_stock_info(stock_code)
                    if isinstance(info, dict) and not info.get("error"):
                        return info
                    elif isinstance(info, str):
                        try:
                            parsed = json.loads(info)
                            if isinstance(parsed, dict):
                                return parsed
                        except (ValueError, TypeError):
                            pass
            except (NotImplementedError, Exception) as e:
                logger.debug("get_stock_info(%s) datasource failed: %s", stock_code, e)

            # cn_stock_info 兜底
            try:
                from app.utils.cn_stock_info import get_cn_stock_info
                info = get_cn_stock_info(stock_code)
                if info and not info.get("error"):
                    return info
            except Exception as e:
                logger.debug("get_stock_info(%s) cn_stock_info failed: %s", stock_code, e)
            return {}

        # 2026-09-26: DB 不再存 pe_ratio/pb_ratio，HTTP 竞赛仅用于 DB 未命中时兜底
        # DB 命中（有 name）→ 跳过 20s 大竞赛，实时估值由腾讯 3s 补全负责
        # DB 未命中 → 走完整 HTTP 竞赛兜底
        _needs_http = not db_result or not db_result.get("name")
        _http_timeout = int(os.getenv("STOCK_INFO_HTTP_TIMEOUT", "20"))

        http_result: Dict[str, Any] = {}
        if _needs_http:
            _pool = ThreadPoolExecutor(max_workers=1)
            try:
                future = _pool.submit(_http_fetch)
                http_result = future.result(timeout=_http_timeout)
            except FuturesTimeout:
                logger.info("get_stock_info(%s) HTTP %ds 超时，使用 basicinfo_db", stock_code, _http_timeout)
            except Exception as e:
                logger.debug("get_stock_info(%s) HTTP fetch error: %s", stock_code, e)
            finally:
                _pool.shutdown(wait=False)

        # ── 3) 合并：basicinfo_db 为底，HTTP 补充新字段 ──
        result = {**db_result, **{k: v for k, v in http_result.items() if v is not None}}

        if not result:
            return {"error": f"无法获取 {stock_code} 的基本面信息", "retriable": False}

        if http_result:
            result["source"] = "http"
        # else: 保持 basicinfo_db source

        # ── 4) 腾讯实时估值补全（3s 超时保护）──
        try:
            def _tencent_fetch():
                return _tencent_quote_raw([sym])
            _tpool = ThreadPoolExecutor(max_workers=1)
            try:
                tq = _tpool.submit(_tencent_fetch).result(timeout=3)
            finally:
                _tpool.shutdown(wait=False)
            q = tq.get(sym)
            if q:
                result.setdefault("price", q.get("price"))
                result.setdefault("change_pct", q.get("change_pct"))
                if q.get("pe_ttm"):
                    result["pe_ttm"] = q["pe_ttm"]
                if q.get("pe_static"):
                    result["pe_static"] = q["pe_static"]
                if q.get("pb"):
                    result["pb"] = q["pb"]
                if q.get("mcap_yi"):
                    result["mcap_yi"] = q["mcap_yi"]
                if q.get("float_mcap_yi"):
                    result["float_mcap_yi"] = q["float_mcap_yi"]
                if q.get("turnover_pct") is not None:
                    result["turnover_pct"] = q["turnover_pct"]
                if q.get("vol_ratio") is not None:
                    result["vol_ratio"] = q["vol_ratio"]
                if q.get("amplitude_pct") is not None:
                    result["amplitude_pct"] = q["amplitude_pct"]
                if q.get("amount_wan"):
                    result["amount_wan"] = q["amount_wan"]
                if q.get("limit_up"):
                    result["limit_up"] = q["limit_up"]
                if q.get("limit_down"):
                    result["limit_down"] = q["limit_down"]
        except FuturesTimeout:
            logger.info("get_stock_info(%s) 腾讯估值 3s 超时，跳过补全", stock_code)
        except Exception as e:
            logger.debug("get_stock_info(%s) 腾讯估值补全跳过: %s", stock_code, e)

        # ── 5) ROE / EPS / BVPS 补全 + PE/PB 本地计算 ──────────────
        # 2026-09-21: _sina_finance 0.75s 即含 ROE/EPS/bvps
        # 2026-09-28: EPS/BVPS 不再丢弃——用 price/eps 算 PE、price/bvps 算 PB
        if not result.get("roe") or not result.get("eps") or not result.get("bvps"):
            try:
                def _fin_fetch():
                    from app.utils.cn_stock_info import _sina_finance
                    return _sina_finance(sym) or {}
                _rpool = ThreadPoolExecutor(max_workers=1)
                try:
                    _fin = _rpool.submit(_fin_fetch).result(timeout=4)
                finally:
                    _rpool.shutdown(wait=False)
                if _fin:
                    result.setdefault("roe", _fin.get("roe"))
                    result.setdefault("eps", _fin.get("eps"))
                    result.setdefault("bvps", _fin.get("bvps"))
            except FuturesTimeout:
                logger.debug("get_stock_info(%s) 财务指标补全 4s 超时", stock_code)
            except Exception as e:
                logger.debug("get_stock_info(%s) 财务指标补全跳过: %s", stock_code, e)

        # ── 5b) PE/PB 本地计算（腾讯没给时用 price÷eps/bvps 算）──
        _px = result.get("price")
        if _px and not result.get("pe_ttm") and result.get("eps"):
            if result["eps"] > 0:
                result["pe_ttm"] = round(_px / result["eps"], 2)
        if _px and not result.get("pb") and result.get("bvps"):
            if result["bvps"] > 0:
                result["pb"] = round(_px / result["bvps"], 2)

        # ── 5c) 换手率/总市值 fallback（腾讯没给时用 mootdx 本地算）──
        if not result.get("turnover_pct"):
            try:
                from app.market_cn.tape import get_realtime_turnover
                _tr = get_realtime_turnover(sym)
                if isinstance(_tr, dict) and _tr.get("turnover_rate") is not None:
                    result["turnover_pct"] = _tr["turnover_rate"]
            except Exception as e:
                logger.debug("get_stock_info(%s) 换手率 fallback 跳过: %s", stock_code, e)

        if not result.get("mcap_yi") and _px and result.get("total_shares"):
            # 总市值(亿) = 价格 × 总股本(股) / 1e8
            result["mcap_yi"] = round(_px * result["total_shares"] / 1e8, 2)
        if not result.get("float_mcap_yi") and _px and result.get("circ_shares"):
            # 流通市值(亿) = 价格 × 流通股本(股) / 1e8
            result["float_mcap_yi"] = round(_px * result["circ_shares"] / 1e8, 2)

        return result

    if len(code_list) == 1:
        from .._base import as_code_envelope
        one = _filter_stock_info(_one(code_list[0]), detail)
        _NUM_KEYS = ("pe_ratio", "pb_ratio", "market_cn", "mcap_yi", "float_mcap_yi",
                     "roe", "price", "change_pct", "pe_ttm", "pe_static", "turnover_pct",
                     "vol_ratio", "total_shares", "circ_shares")
        for _k in _NUM_KEYS:
            if _k in one and one[_k] is not None and not isinstance(one[_k], (int, float)):
                try:
                    one[_k] = float(one[_k])
                except Exception:
                    pass
        return as_code_envelope(code_list[0], one)

    results = {}
    for code in code_list:
        try:
            results[code] = _filter_stock_info(_one(code), detail)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}

# ── 腾讯财经批量行情（内部共用） ──────────────────────────────

def _strip_prefix(s):
    from app.data_sources.normalizer import strip_market_prefix
    return strip_market_prefix(s)

def _tencent_quote_raw(codes: list) -> dict:
    """腾讯财经批量行情原始接口。返回 {code: {全部字段dict}}。"""
    import urllib.request

    prefixed = []
    for c in codes:
        c = _strip_prefix(c)
        # 2026-09-22 修复：旧逻辑把 000/001 开头的深市主板股映射成 sh 前缀
        # （如 000858 五粮液 → sh000858），腾讯对不存在标的返回错位字段，
        # 解析出 price=13765/pb=0 的坏数据。市场前缀统一按 normalizer 规则：
        # 沪市 6/9/5 开头 → sh；北交所 8/4 开头 → bj；其余（深市 0/2/3）→ sz。
        if c.startswith(("6", "9", "5")):
            prefixed.append(f"sh{c}")
        elif c.startswith(("8", "4")):
            prefixed.append(f"bj{c}")
        else:
            prefixed.append(f"sz{c}")

    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    resp = urllib.request.urlopen(req, timeout=10)
    data = resp.read().decode("gbk")

    result = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]
        result[code] = {
            "name":           vals[1],
            "price":          float(vals[3]) if vals[3] else 0,
            "last_close":     float(vals[4]) if vals[4] else 0,
            "open":           float(vals[5]) if vals[5] else 0,
            "buy_vol":        float(vals[6]) if vals[6] else 0,
            "sell_vol":       float(vals[7]) if vals[7] else 0,
            "bid1_price":     float(vals[9]) if vals[9] else 0,
            "bid1_vol":       float(vals[10]) if vals[10] else 0,
            "bid2_price":     float(vals[11]) if vals[11] else 0,
            "bid2_vol":       float(vals[12]) if vals[12] else 0,
            "bid3_price":     float(vals[13]) if vals[13] else 0,
            "bid3_vol":       float(vals[14]) if vals[14] else 0,
            "bid4_price":     float(vals[15]) if vals[15] else 0,
            "bid4_vol":       float(vals[16]) if vals[16] else 0,
            "bid5_price":     float(vals[17]) if vals[17] else 0,
            "bid5_vol":       float(vals[18]) if vals[18] else 0,
            "ask1_price":     float(vals[19]) if vals[19] else 0,
            "ask1_vol":       float(vals[20]) if vals[20] else 0,
            "ask2_price":     float(vals[21]) if vals[21] else 0,
            "ask2_vol":       float(vals[22]) if vals[22] else 0,
            "ask3_price":     float(vals[23]) if vals[23] else 0,
            "ask3_vol":       float(vals[24]) if vals[24] else 0,
            "ask4_price":     float(vals[25]) if vals[25] else 0,
            "ask4_vol":       float(vals[26]) if vals[26] else 0,
            "ask5_price":     float(vals[27]) if vals[27] else 0,
            "ask5_vol":       float(vals[28]) if vals[28] else 0,
            "change_amt":     float(vals[31]) if vals[31] else 0,
            "change_pct":     float(vals[32]) if vals[32] else 0,
            "high":           float(vals[33]) if vals[33] else 0,
            "low":            float(vals[34]) if vals[34] else 0,
            "amount_wan":     float(vals[37]) if vals[37] else 0,
            "turnover_pct":   float(vals[38]) if vals[38] else 0,
            "pe_ttm":         float(vals[39]) if vals[39] else 0,
            "amplitude_pct":  float(vals[43]) if vals[43] else 0,
            "mcap_yi":        float(vals[44]) if vals[44] else 0,
            "float_mcap_yi":  float(vals[45]) if vals[45] else 0,
            "pb":             float(vals[46]) if vals[46] else 0,
            "limit_up":       float(vals[47]) if vals[47] else 0,
            "limit_down":     float(vals[48]) if vals[48] else 0,
            "vol_ratio":      float(vals[49]) if vals[49] else 0,
            "pe_static":      float(vals[52]) if vals[52] else 0,
        }
    return result

# ── 五档盘口 ──────────────────────────────────────────────────

def get_order_book(codes: str) -> dict:
    """五档盘口：买卖各 5 档价格与挂单量，含最新价、涨跌幅。

    ⚠ **不含**换手率/PE/市值（描述是唯一进 LLM 的契约，写了就等于教模型去承诺
    拿不到的数据）。要这些口径用 `get_stock_info`（turnover_pct/pe_ttm/pb/mcap_yi）。

    Returns:
        统一结构（单/多股一致）：{"count": N, "data": {代码: 单股结果},
        "error": None}；单股结果字段：{code, name, price, change_percent, bid, ask}，
        五档在 bid/ask；失败 → {"error": "...", "retriable": False}。

    Args:
        codes: 多股用逗号分隔
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> dict:
        code = _strip_prefix(stock_code)
        try:
            from app.market_cn.tape import get_order_book as _get_order_book
            result = _get_order_book(code)
            if "error" in result:
                return {"stock_code": code, "error": result["error"]}
            return result
        except Exception as e:
            logger.warning("get_order_book(%s) failed: %s", code, e)
            return {"stock_code": code, "error": str(e)}

    return _batch_execute(_one, code_list)

# ── 指数/ETF行情 ──────────────────────────────────────────────

def _get_index_etf_quote(codes: str) -> dict:
    """指数/ETF行情：返回价格、涨跌幅、成交量，支持上证/深证/创业板/沪深300及对应ETF。

    Returns:
        dict: {total, quotes:[{code, name, price, change_pct, change_amt, high, low,
        amount_wan, open, last_close}]}，行情列表在 quotes；失败→{error}。

    Args:
        codes: 逗号分隔的代码，如 "000001,000300,399006,510050"
               指数：000001(上证) 399001(深证) 000300(沪深300) 399006(创业板)
               ETF：510050(上证50) 510300(沪深300) 159919(沪深300) 512880(证券)
    """
    code_list = [_strip_prefix(c.strip()) for c in codes.split(",") if c.strip()]
    if not code_list:
        return {"error": "请提供至少一个代码"}

    try:
        data = _tencent_quote_raw(code_list)
        results = []
        for code in code_list:
            q = data.get(code)
            if q:
                results.append({
                    "code": code,
                    "name": q["name"],
                    "price": q["price"],
                    "change_pct": q["change_pct"],
                    "change_amt": q["change_amt"],
                    "high": q["high"],
                    "low": q["low"],
                    "amount_wan": q["amount_wan"],
                    "open": q["open"],
                    "last_close": q["last_close"],
                })
            else:
                results.append({"code": code, "error": "未获取到数据"})
        _r = {"total": len(results), "quotes": results}
        return _r
    except Exception as e:
        logger.warning("_get_index_etf_quote(%s) failed: %s", codes, e)
        return {"error": str(e)}

# ── 批量估值对比 ──────────────────────────────────────────────

def batch_valuation_compare(codes: str) -> dict:
    """估值对比：返回多只股票的PE/PB/市值/营收并排对比表。

    Returns:
        {"total": N, "stocks": [{code, name, price, pe_ttm, pb, mcap_yi, ...}]，
        "pe_sorted": [按PE升序的 code]}；失败 → {"error": ...}

    Args:
        codes: 逗号分隔的股票代码，如 "600519,000858,688017"
    """
    code_list = [_strip_prefix(c.strip()) for c in codes.split(",") if c.strip()]
    if not code_list:
        return {"error": "请提供至少一个股票代码"}
    if len(code_list) > 20:
        return {"error": "单次最多对比20只股票"}

    try:
        data = _tencent_quote_raw(code_list)
        results = []
        for code in code_list:
            q = data.get(code)
            if q:
                results.append({
                    "code": code,
                    "name": q["name"],
                    "price": q["price"],
                    "change_pct": q["change_pct"],
                    "pe_ttm": q["pe_ttm"],
                    "pb": q["pb"],
                    "mcap_yi": q["mcap_yi"],
                    "float_mcap_yi": q["float_mcap_yi"],
                    "turnover_pct": q["turnover_pct"],
                })
            else:
                results.append({"code": code, "error": "未获取到数据"})

        valid = [r for r in results if r.get("pe_ttm") and r["pe_ttm"] > 0]
        valid.sort(key=lambda x: x["pe_ttm"])

        _r = {
            "total": len(results),
            "stocks": results,
            "pe_sorted": [r["code"] for r in valid],
        }
        return _r
    except Exception as e:
        logger.warning("batch_valuation_compare(%s) failed: %s", codes, e)
        return {"error": str(e)}

# ═══════════════════════════════════════════════════════════════
# 基本面摘要（从 capital_tools.py 合并）
# ═══════════════════════════════════════════════════════════════

def _safe_float(v, default=0.0):
    from app.data_sources.normalizer import safe_float
    return safe_float(v, default)

def _num(v):
    """工具返回值数值化：千分位字符串→float；None/NaN/空→None。

    ⚠️ 与 `_safe_float` 的区别：缺失值**不回落 0**（null≠0，技能注意事项
    第 2 条——把缺失当 0 会虚构"零余额/零营收"）；`safe_float` 不剥千分位
    逗号（'9,227,807.21'→0.0），这里先剥再转。
    """
    import math
    if v is None:
        return None
    if isinstance(v, str):
        v = v.replace(",", "").strip()
        if not v or v.lower() in ("nan", "none", "-"):
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f

def _market_prefix(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    return "sz"

def _get_margin_trading(code: str, days: int = 60) -> Dict[str, Any]:
    """融资融券摘要。提取融资余额趋势+近期变化幅度。

    ⚠️ 单位口径（2026-10-17 修复，审计 O1）：上游 `RZYE`/`RQYE` 为**元**
    （实测 2026-10-16：600519 RZYE=16,450,780,103 元），本函数 /1e4 输出
    **万元**。旧版键名 `rz_balance`/`rq_balance` 不带单位，模型只能猜——
    600519 复跑中模型把「万元」过度泛化到价格/派息等非 money 字段，写出
    「现价 13.29 万元」（真值 1329 元被 ÷100）的幻觉。现按仓内约定
    （`main_net_wan`/`amount_wan` 同款）把单位写进键名，见名知口径。
    ★ 双单位并列（2026-10-17 二次修复，run8 §3.1）：只给万元键仍会错——模型
      把 1,645,078 万元**换算对了**（164.51 亿）却**贴错单位名**（写成
      「164.51 万万元」）。根因是它必须自己做 ÷1e4 再自己起单位名 ⇒ 直接给
      亿元键让它照抄（`main_net_yi`/`mcap_yi` 已是同款先例）。
    """
    from ._em_utils import em_datacenter
    data = em_datacenter(
        "RPTA_WEB_RZRQ_GGMX",
        filter_str=f'(SCODE="{code}")',
        page_size=days,
        sort_columns="DATE", sort_types="-1",
    )
    if not data:
        return {"signal": "无数据"}

    latest = data[0]
    rz_latest = _safe_float(latest.get("RZYE"))  # 融资余额
    rz_5d_ago = _safe_float(data[4].get("RZYE")) if len(data) > 5 else rz_latest
    rz_change_pct = ((rz_latest / rz_5d_ago - 1) * 100) if rz_5d_ago else 0

    rq_latest = _safe_float(latest.get("RQYE"))  # 融券余额

    signal = "融资净流入" if rz_change_pct > 2 else "融资净流出" if rz_change_pct < -2 else "持平"
    return {
        # 键名即口径：融资/融券余额，双单位并列（见上方口径说明）。
        # 万元 = 工具层传统口径；亿元 = 模型直接照抄用的口径（run8 幻觉根因）。
        "rz_balance_wan": round(rz_latest / 1e4, 1),
        "rz_balance_yi": round(rz_latest / 1e8, 2),
        "rz_5d_change_pct": round(rz_change_pct, 1),
        "rq_balance_wan": round(rq_latest / 1e4, 1),
        "rq_balance_yi": round(rq_latest / 1e8, 2),
        "signal": signal,
    }

def _get_block_trades(code: str, page_size: int = 20) -> Dict[str, Any]:
    """大宗交易摘要。提取近期成交笔数、平均溢价率、机构买卖方向。"""
    from ._em_utils import em_datacenter
    data = em_datacenter(
        "RPT_DATA_BLOCKTRADE",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=page_size,
        sort_columns="TRADE_DATE", sort_types="-1",
    )
    if not data:
        return {"signal": "无大宗交易记录"}

    premiums = []
    inst_buy = 0
    inst_sell = 0
    for row in data:
        close = _safe_float(row.get("CLOSE_PRICE"))
        deal_price = _safe_float(row.get("DEAL_PRICE"))
        if close:
            premiums.append((deal_price / close - 1) * 100)
        buyer = str(row.get("BUYER", ""))
        seller = str(row.get("SELLER", ""))
        if "机构" in buyer:
            inst_buy += 1
        if "机构" in seller:
            inst_sell += 1

    avg_premium = sum(premiums) / len(premiums) if premiums else 0
    signal = "溢价成交（正面）" if avg_premium > 5 else "折价成交（负面）" if avg_premium < -5 else "中性"
    return {
        "recent_count": len(data),
        "avg_premium_pct": round(avg_premium, 1),
        "inst_buy": inst_buy,
        "inst_sell": inst_sell,
        "signal": signal,
    }

def _get_holder_count(code: str) -> Dict[str, Any]:
    """股东户数摘要。提取最新户数、环比变化趋势。

    ⚠️ 字段口径（2026-10-17 修复）：上游 `RPT_HOLDERNUMLATEST` **没有**
    `AVG_AMOUNT` 这一列——实测 2026-10-17 该报表实际列为 `AVG_MARKET_CAP`
    （户均持股市值，**元**）与 `AVG_HOLD_NUM`（户均持股数量，**股**）。
    旧版按 `AVG_AMOUNT` 取数恒得 0.0（死字段，"户均持股"语义留空）。
    现按真实列名取数，键名自带单位后缀；字段缺失给 None 而非 0
    （null≠0，技能注意事项第 2 条）。
    另修一个死值：该报表按股票**只返回最新一期**（单股查询 len==1），
    上期户数在同行的 `PRE_HOLDER_NUM` 里（实测 600519：296404 vs
    243159）。旧版只认 `data[1]` ⇒ 单股查询恒取到自己，`change_pct`
    恒 0、`signal` 恒"持平"——把"筹码分散 +21.9%"读成"无变化"。
    """
    from ._em_utils import em_datacenter
    data = em_datacenter(
        "RPT_HOLDERNUMLATEST",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=10,
        sort_columns="END_DATE", sort_types="-1",
    )
    if not data:
        return {"signal": "无数据"}

    latest = data[0]
    latest_count = _safe_float(latest.get("HOLDER_NUM"))
    prev_count = _safe_float(latest.get("PRE_HOLDER_NUM"))
    if not prev_count and len(data) > 1:
        prev_count = _safe_float(data[1].get("HOLDER_NUM"))
    change_pct = ((latest_count / prev_count - 1) * 100) if prev_count else 0
    avg_mcap = latest.get("AVG_MARKET_CAP")    # 户均持股市值（元）
    avg_hold_num = latest.get("AVG_HOLD_NUM")  # 户均持股数量（股）

    signal = "筹码集中" if change_pct < -5 else "筹码分散" if change_pct > 5 else "持平"
    return {
        "latest_date": str(latest.get("END_DATE", ""))[:10],
        "holder_count": int(latest_count),
        "change_pct": round(change_pct, 1),
        # 键名即口径：户均持股市值（元）/ 户均持股数量（股），见上方口径说明
        "avg_hold_market_cap_rmb": round(_safe_float(avg_mcap), 0) if avg_mcap else None,
        "avg_hold_num_shares": round(_safe_float(avg_hold_num), 1) if avg_hold_num else None,
        "signal": signal,
    }

def _get_dividend_history(code: str) -> Dict[str, Any]:
    """分红送转摘要。提取累计分红次数、连续分红年数、近期派息水平。

    ⚠️ 单位口径（2026-10-17 修复，审计 O1）：上游 `PRETAX_BONUS_RMB` 是
    **每 10 股派息（元）**——实测 600519 该字段 280.2423 对应方案文案
    "10派280.2423元(含税)"，不是派息总额。旧版把它累加进名为
    `total_bonus_rmb` 的键，名字像"总额(rmb)"且无单位标注，诱发下游模型
    ÷10 折算成"280.62 亿元"的幻觉（真值：累计每 10 股派息 2806.17 元）。
    现改名自描述 + 显式单位键；要真实派息总额需另乘总股本/10（本函数不
    取股本数据，不硬造第二口径——缺口显式 > 硬凑数字）。
    """
    from ._em_utils import em_datacenter
    data = em_datacenter(
        "RPT_SHAREBONUS_DET",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=20,
        sort_columns="EX_DIVIDEND_DATE", sort_types="-1",
    )
    if not data:
        return {"signal": "无分红记录"}

    years = set()
    cum_bonus_per_10shares = 0.0
    for row in data:
        date_str = str(row.get("EX_DIVIDEND_DATE", ""))[:4]
        if date_str:
            years.add(date_str)
        cum_bonus_per_10shares += _safe_float(row.get("PRETAX_BONUS_RMB"))

    return {
        "record_count": len(data),
        "dividend_years": len(years),
        # 键名即口径：累计「每 10 股」派息，单位元/10股（见上方口径说明）
        "cum_bonus_per_10shares_rmb": round(cum_bonus_per_10shares, 3),
        "bonus_unit": "元/10股",
        "signal": "持续分红" if len(years) >= 3 else "偶有分红" if years else "无分红",
    }

def _get_financial_statements(code: str) -> Dict[str, Any]:
    """财报关键指标摘要。只提取 Agent 关心的几个数，不返回原始表格。

    ⚠️ 单位口径（2026-10-17 修复，审计 O1）：新浪 `vFD_BalanceSheet` /
    `vFD_ProfitStatement` 页面表头明写「单位：万元」（实测 2026-10-17，
    money.finance.sina.com.cn `/displaytype/4.phtml`：600519 营业总收入
    9,227,807.21 = 922.78 亿、资产总计 30,905,078.46 = 3090.51 亿）。
    旧版键名（`revenue`/`total_assets`…）裸名无单位，模型只能猜口径，
    600519 复跑中模型把「万元」泛化到价格/派息等非 money 字段（「现价
    13.29 万元」= 1329 元被 ÷100）。现按仓内约定加 `_wan` 后缀
    （与 `main_net_wan`/`mcap_yi` 同款），并显式给出 `financial_unit`
    与 `report_date`——金额不带报告期同样是裸数字。
    """
    import requests
    _UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    prefix = _market_prefix(code)
    symbol = f"{prefix}{code}"

    summary: Dict[str, Any] = {}

    # 2026-09-15：旧视图 vFD_FinancialGuideLine 已被新浪下线（返回 {"__ERROR":0,"__ERRORMSG":"Invalid view go"}），
    # 改用 money.finance.sina.com.cn 新版三表视图（vFD_BalanceSheet / vFD_ProfitStatement）。
    # 新表结构：最大表为数据表，列0=项目名，列1+=报告期（第0行为日期行）。
    # 单位：万元（页面表头明示，见函数 docstring 口径说明）。
    import requests
    _UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    headers = {"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"}
    code_num = _strip_prefix(code)

    import pandas as pd
    from io import StringIO

    def _fetch_sina_table(path: str):
        try:
            r = requests.get(f"https://money.finance.sina.com.cn/corp/go.php/{path}/stockid/{code_num}/ctrl/part/displaytype/4.phtml",
                             headers=headers, timeout=15)
            r.encoding = "gbk"
            dfs = pd.read_html(StringIO(r.text))
            # 数据表 = 行数最多的那张（导航页表格都是 2~5 行的小表）
            return max(dfs, key=lambda d: d.shape[0]) if dfs else None
        except Exception as e:
            logger.warning("_get_financial_statements(%s) %s 抓取失败: %s", code, path, e)
            return None

    def _latest_value(df, keywords, exclude=None):
        """取最新报告期（列1）中匹配关键词的值（float；None 保持 None）。"""
        if df is None or df.shape[1] < 2:
            return None
        exclude = exclude or []
        for _, row in df.iterrows():
            item_name = str(row.iloc[0])
            if any(k in item_name for k in keywords) and not any(x in item_name for x in exclude):
                val = row.iloc[1]
                return val if pd.notna(val) else None
        return None

    df_bs = df_is = None
    # 资产负债表（键名即口径：单位万元，见函数 docstring）
    try:
        df_bs = _fetch_sina_table('vFD_BalanceSheet')
        summary["total_assets_wan"] = _num(_latest_value(df_bs, ["资产总计", "总资产"]))
        summary["total_liabilities_wan"] = _num(_latest_value(df_bs, ["负债合计", "总负债"]))
        summary["equity_wan"] = _num(_latest_value(df_bs, ["股东权益合计", "所有者权益合计", "归属母公司"]))
        summary["cash_wan"] = _num(_latest_value(df_bs, ["货币资金"]))
    except Exception as e:
        logger.warning("_get_financial_statements(%s) 资产负债表失败: %s", code, e)

    # 利润表（同上，万元口径）
    try:
        df_is = _fetch_sina_table('vFD_ProfitStatement')
        summary["revenue_wan"] = _num(_latest_value(df_is, ["营业总收入", "营业收入"]))
        summary["net_profit_wan"] = _num(_latest_value(df_is, ["净利润"], exclude=["扣非"]))
    except Exception as e:
        logger.warning("_get_financial_statements(%s) 利润表失败: %s", code, e)

    # 报告期：第 0 行日期行第 2 列（实测 600519 → 2026-06-30）
    for _df in (df_bs, df_is):
        if _df is not None and _df.shape[1] >= 2:
            try:
                summary["report_date"] = str(_df.iloc[0, 1])[:10]
                break
            except Exception:
                pass
    summary["financial_unit"] = "万元"

    return summary

# 换算教训（2026-10-22 自 docstring 移出，注入瘦身）：run8 换算对了却把单位名
# 写成「万万元」——引用时取 _yi 键直接照抄，不要自己换算。
# 换算教训（2026-10-22 自 docstring 移出，注入瘦身）：run8 换算对了却把单位名
# 写成「万万元」——引用时取 _yi 键直接照抄，不要自己换算。
def get_capital_summary(codes: str) -> dict:
    """基本面摘要：返回营收/利润增速、ROE、PE/PB估值、机构持仓变化等中长线指标。
    一次调用聚合融资融券、大宗交易、股东户数、分红送转、财报三表五大维度数据，
    并生成结构化摘要供中长线持仓决策参考。

    Returns:
        统一结构（单/多股一致）：{"count": N, "data": {代码: 单股结果},
        "error": None}；失败 → {"error": "...", "retriable": False}。单股结果字段：
        {"summary": {margin, block_trade, holders, dividend, financials,
        overall_signal(中长线偏多/偏空/中性)}}。

    ⚠️ 单位口径（键名即单位，引用时取对应键直接照抄，不要自行换算）：
        - margin: rz_balance_wan/rq_balance_wan（万元）与 rz_balance_yi/rq_balance_yi
          （亿元）各一份；
        - financials: revenue_wan/net_profit_wan/total_assets_wan/total_liabilities_wan/
          equity_wan/cash_wan 财报金额**万元**（附 financial_unit:"万元" 与 report_date）；
        - holders: avg_hold_market_cap_rmb 户均持股市值**元**、avg_hold_num_shares 户均持股**股**；
        - dividend: cum_bonus_per_10shares_rmb 累计派息**元/10股**（附 bonus_unit）。
        上述口径**只适用于对应键**：价格（元）等非 money 字段不得套用万元/亿元口径换算。

    Args:
        codes: 多股用逗号分隔
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> dict:
        code = _strip_prefix(stock_code)

        # ── 并行采集五维数据（单源超时不阻断整体）────────────────────
        def _safe(fn, label):
            try:
                return fn()
            except Exception as e:
                logger.warning("[Capital] %s 超时/失败: %s", label, e)
                return {}

        margin = _safe(lambda: _get_margin_trading(code, days=60), "margin")
        block = _safe(lambda: _get_block_trades(code, page_size=20), "block")
        holders = _safe(lambda: _get_holder_count(code), "holders")
        dividend = _safe(lambda: _get_dividend_history(code), "dividend")
        financials = _safe(lambda: _get_financial_statements(code), "financials")

        # ── 直接组装摘要（子函数已返回浓缩指标，无需二次计算）──
        summary: Dict[str, Any] = {"stock_code": code}

        summary["margin"] = margin
        summary["block_trade"] = block
        summary["holders"] = holders
        summary["dividend"] = dividend
        summary["financials"] = financials

        # ── 综合信号 ─────────────────────────────────────────────
        signals = [
            margin.get("signal", ""),
            block.get("signal", ""),
            holders.get("signal", ""),
            dividend.get("signal", ""),
        ]
        positive = sum(1 for s in signals if "正面" in s or "看多" in s or "集中" in s or "持续" in s or "流入" in s)
        negative = sum(1 for s in signals if "负面" in s or "撤退" in s or "分散" in s or "流出" in s)

        if positive > negative:
            summary["overall_signal"] = "中长线偏多"
        elif negative > positive:
            summary["overall_signal"] = "中长线偏空"
        else:
            summary["overall_signal"] = "中性"

        return {"summary": summary}

    return _batch_execute(_one, code_list)
