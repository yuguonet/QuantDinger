# -*- coding: utf-8 -*-
"""provider_index / mootdx —— 指数族 TDX 直连源（index_kline 1D/5m + index_quote）。

⚠ **2026-10-28 停用**：TDX 行情数据面已断（9 台 HQ 候选全「协议通、bars 恒空」，
get_client() 冷启 43.2s 返 None）⇒ 本文件的 provider 类已清空 capabilities 表，
不再进任何取数链路；取数实现原样保留，恢复方法见类 docstring。

mootdx index_bars() 拉指数K线（frequency=4 日线 / 0 五分钟），index 实时行情走 quotes()。
volume 单位: TDX 指数日线 vol 为"手"，与 tencent/sina 对齐。

⚠ 5m 的 vol 字段错位（2026-09-12 定案, tmp/_em_crosscheck.out）：
  pytdx GetIndexBars 对指数分钟包解码 "vol" 实际是 成交额(元)/100，**不是成交量**。
  OHLC/涨跌家数解码正确 ⇒ 5m 只作 sina 死亡时的价格兜底（tf_priority 5m=40，
  必须排在 sina 之后），volume 值带系统性偏差，后续 sina 同步窗口 UPSERT 会归一。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger
from app.utils.mootdx_client import get_client as _get_client

logger = get_logger(__name__)


def _fetch_daily(code: str, limit: int) -> Dict[str, Any]:
    """指数日K线（mootdx index_bars，frequency=4=日线）。

    ⚠ 失败一律 raise，不返回空信封：is_rows 只校验 rows 是 list，空 rows 会被判
    「成功」并终止 sequential 换源。mootdx 是 index_kline 的第一优先级（10），
    它返空信封等于把 tencent/sina/baostock/akshare 全挡在门外（2026-10-28 1.log）。
    TDX 指数用**裸码**（000001），不入参带后缀 —— 统一走 split_index_code 剥。
    """
    from app.data_sources.normalizer import split_index_code

    base, _ = split_index_code(code)
    cli = _get_client()
    if cli is None:
        raise RuntimeError("mootdx 不可用")
    try:
        df = cli.index_bars(symbol=base, frequency=4, start=0, offset=min(limit, 800))
        if df is None or df.empty:
            raise RuntimeError("mootdx index_kline 空表（%s）" % base)
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "date": str(r.get("datetime", r.get("date", "")))[:10],
                "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)),
                "volume": float(r.get("vol", r.get("volume", 0))),
            })
        return {"rows": rows[-limit:]}
    except Exception as e:
        logger.warning("[mootdx] index_kline %s 失败: %s", code, e)
        raise


def _fetch_5m(code: str, limit: int) -> Dict[str, Any]:
    """指数 5m K线（mootdx index_bars, frequency=0）。time=bar 结束时刻。

    ⚠ vol 字段错位=成交额/100（见文件头），只作价格兜底；up/down_count 解码正确。
    单次上限 800 根 ≈ 17 个交易日（TDX 协议限制）。失败一律 raise（同 _fetch_daily）。
    """
    from app.data_sources.normalizer import split_index_code

    base, _ = split_index_code(code)
    cli = _get_client()
    if cli is None:
        raise RuntimeError("mootdx 不可用")
    limit = min(int(limit or 800), 800)
    try:
        df = cli.index_bars(symbol=base, frequency=0, start=0, offset=limit)
        if df is None or df.empty:
            raise RuntimeError("mootdx index_kline 5m 空表（%s）" % base)
        # 列名防御: 原始列可能同时含 vol 与 volume（2026-09-11 实测）
        if "volume" not in df.columns and "vol" in df.columns:
            df = df.rename(columns={"vol": "volume"})
        tcol = "datetime" if "datetime" in df.columns else df.columns[0]
        rows = []
        for _, r in df.iterrows():
            t = str(r.get(tcol, ""))[:16].replace("T", " ")
            try:
                row = {
                    "time": t,
                    "open": float(r.get("open", 0)),
                    "high": float(r.get("high", 0)),
                    "low": float(r.get("low", 0)),
                    "close": float(r.get("close", 0)),
                    "volume": float(r.get("volume", r.get("vol", 0)) or 0),  # ⚠ vol 错位，见文件头
                }
            except (ValueError, TypeError):
                continue
            for c in ("up_count", "down_count"):
                if c in df.columns and r.get(c) is not None:
                    try:
                        row[c] = int(r.get(c))
                    except (ValueError, TypeError):
                        pass
            rows.append(row)
        return {"rows": rows[-limit:]}
    except Exception as e:
        logger.warning("[mootdx] index_kline 5m %s 失败: %s", code, e)
        raise


def fetch_index_kline(code: str, timeframe: str = "1D", limit: int = 250) -> Dict[str, Any]:
    """指数K线（timeframe 分发：1D → frequency=4 / 5m → frequency=0）。"""
    from app.data_sources.normalizer import normalize_chart_timeframe
    from app.data_sources.dispatch import NotSupported

    tf = normalize_chart_timeframe(timeframe)
    if tf == "1D":
        return _fetch_daily(code, limit)
    if tf == "5m":
        return _fetch_5m(code, limit)
    return NotSupported("index_kline/%s" % tf)


def fetch_index_quote(code: Optional[str] = None,
                     codes: Optional[List[str]] = None) -> Dict[str, Any]:
    """指数实时行情（mootdx quotes）。

    接受 codes=[...] 或 code=单码（run_batch 串行分支按 code= 注入）；不传返回
    默认 4 指数。code=/codes= 双入口与腾讯同构，确保 index_quote 批量路由下
    mootdx 作串行兜底时不被 TypeError 打断（2026-10-27 接通）。
    """
    cli = _get_client()
    if cli is None:
        return {"rows": [], "error": "mootdx 不可用"}
    if not codes:
        codes = [code] if code else None
    if not codes:
        codes = ["000001", "399001", "399006", "000300"]
    rows = []
    for c in codes:
        try:
            df = cli.quotes(symbol=c)
            if df is not None and not df.empty:
                r = df.iloc[0]
                price = float(r.get("price", 0))
                last_close = float(r.get("last_close", 0))
                if price <= 0:
                    # 实测脏数据（price=0）不下发，否则会短路 coord 的后续源
                    logger.debug("[mootdx] index_quote %s: price<=0，丢弃", c)
                    continue
                rows.append({
                    "code": c,
                    "name": str(r.get("name", "")),
                    "price": price,
                    "last_close": last_close,
                    "open": float(r.get("open", 0)),
                    "high": float(r.get("high", 0)),
                    "low": float(r.get("low", 0)),
                    "volume": float(r.get("vol", 0)),          # 手（与 tencent provider 同口径）
                    "amount": float(r.get("amount", 0)),       # 元
                    "change": round(price - last_close, 4),
                    "change_percent": float(r.get("percent", 0)),
                })
        except Exception as e:
            logger.debug("[mootdx] index_quote %s: %s", c, e)
    return {"rows": rows}


from app.data_sources.provider_index import register  # noqa: E402


@register
class MootdxIndexProvider:
    """指数族 TDX 直连源。

    2026-10-28 起 **能力表清空（停用）**：TDX 行情数据面已断 —— 本机实测 9 台 HQ
    候选全部「协议通、bars 恒空」，`get_client()` 冷启 43.2s 后返 None。此前它顶着
    `index_kline` 第一顺位（priority=10），每条指数取数都要先白等一轮判死
    （实测前 3 个指数各 12~13s，1.log 15:57:49→15:58:23 那 34s 就是它）。

    `fetch_index_kline` / `fetch_index_quote` 两个实现**原样保留**（本目录是快速拔插
    结构，供恢复接线与单测直接调用）：把 `capabilities` 改回
    `{"index_kline": fetch_index_kline, "index_quote": fetch_index_quote}` 即恢复，
    同时恢复 `tf_priority = {"5m": 40}`（5m vol 字段错位 ⇒ 排在 sina 之后作价格兜底）
    与 `kind_priority = {"index_quote": 40}`（指数实时有脏数据 price=0/假价 ⇒ tencent 先行）。
    `fetch()` 对任何 kind 都回 NotSupported（能力表空 ⇒ fn 为 None），不会触网。
    """

    name = "mootdx"
    priority = 10
    markets = {"CNStock"}

    # 空能力表 = 不进任何 kind 的候选链（get_providers 按「kind 是否声明」筛选）
    capabilities: Dict[str, Any] = {}

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
