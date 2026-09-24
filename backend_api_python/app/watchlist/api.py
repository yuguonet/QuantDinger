# -*- coding: utf-8 -*-
"""app/watchlist/api.py — label 对外**唯一接口层**（3 个符号，方案 §5.4）

| 符号 | 调用方 | 说明 |
|---|---|---|
| `get_labels` | `routes/market.py` `/watchlist/get` | 读路径唯一入口 |
| `write_system_facts` | `job.py`（每日盘后）/ 新增自选后即时补算 | system 自算，grade=1 兜底；默认范围 = 自选股并集 |
| `submit` | **auto / agent** | 上级唯一写入口；`source` 决定 grade，**不接受外部传 grade** |

**接口即唯一写通道**：`submit` 之外没有第二条写路径 —— 等级映射、TTL 换算、
4 段结构校验若散在多处必然漂移。**不允许人工写**：`source` 白名单 = agent/auto，
路由侧不开写接口，无 grade 4。

纪律：本文件**不 import** `app.agent.*` / `app.market_cn.auto.*`（禁令 1）。
"""
from __future__ import annotations

import traceback
from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional

from app.utils import trading_calendar as tcal
from app.utils.logger import get_logger
from app.watchlist import model, store
from app.watchlist.render import render_one
from app.watchlist.render import section_empty as render_section_empty
from app.watchlist.render import to_dt as render_to_dt

logger = get_logger(__name__)


def _today(ref: Optional[str] = None) -> str:
    return ref or datetime.now().strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════════════
# 读路径（唯一入口）
# ═══════════════════════════════════════════════════════════════════

def get_labels(user_id: int, *, group_name: Optional[str] = None,
               with_overlay: bool = True, asof: Optional[str] = None) -> List[Dict[str, Any]]:
    """读路径唯一入口：关系 + 候选 + 接管 + 覆盖 + 渲染 → 每票一条（含 4 段）。

    1. 关系：`qd_watchlist`（可选分组）
    2. 候选：一次取全部行（已按 grade DESC, created_at DESC 排好）
    3. 接管：Python 侧按票遍历，取第一个未失效的；失效记录进 `stale`
    4. 覆盖：实时快照（可选）
    5. 渲染：4 段
    """
    asof = asof or _today()
    relations = store.list_user_rows(user_id, group_name)
    if not relations:
        return []

    pairs = list({(r["market"], r["symbol"]) for r in relations})
    cands = store.fetch_candidates(pairs, asof)

    by_key: Dict[tuple, List[Dict[str, Any]]] = {}
    for c in cands:
        by_key.setdefault((c["market"], c["symbol"]), []).append(c)

    quotes = {}
    if with_overlay:
        from app.watchlist.overlay import fetch_quotes, quote_view
        raw = fetch_quotes(pairs)
        quotes = {k: quote_view(v) for k, v in raw.items()}

    out: List[Dict[str, Any]] = []
    for rel in relations:
        key = (rel["market"], rel["symbol"])
        rows = by_key.get(key) or []
        pick, stale = _pick(rows)
        taken_over = bool(stale) and pick is not None and pick.get("source") == "system"
        parts = _fill_parts(rows, pick)
        out.append(render_one(pick, rel, quotes.get(key), parts=parts,
                              taken_over=taken_over, stale=stale))
    return out


def _fill_parts(rows: List[Dict[str, Any]], pick: Optional[Dict[str, Any]]
                ) -> Dict[str, Dict[str, Any]]:
    """**段级回填**：每段单独选"答了这一段的最高等级行"（未答的等级不参与该段）。

    依据：用户裁定"系统自算等级最低，上级负责给更好的答案" —— 那么一个**没答某一段**的上级
    不应把次等级已有的答案抹掉。`rows` 已按 grade DESC, created_at DESC 排好，故取第一个
    满足"未失效 且 该段非空"的行即可；`pick` 本身已答的段自然仍归 `pick`。
    """
    if pick is None:
        return {}
    parts: Dict[str, Dict[str, Any]] = {}
    for sec_key in model.SECTION_KEYS:
        if not render_section_empty(sec_key, pick):
            continue                      # 最高等级已答这一段 ⇒ 无需回填
        for r in rows:
            if _is_expired(r):
                continue
            if not render_section_empty(sec_key, r):
                parts[sec_key] = r
                break
    return parts


def _is_expired(row: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    exp = render_to_dt(row.get("expires_at"))
    return exp is not None and exp <= (now or datetime.now())


#: 挂载到既有行上的 label 字段（**只增不改**，旧字段一个不动）
ATTACH_KEYS = ("sections", "grade", "source", "taken_over", "stale", "age_days",
               "label_asof", "label_price", "label_change_pct")


def attach_labels(rows: List[Dict[str, Any]], user_id: int,
                  *, with_overlay: bool = False) -> List[Dict[str, Any]]:
    """把 label 4 段**加法式**挂到已取好的自选行上（就地修改并返回 rows）。

    ⚠️ 关键不变量：**旧字段（含 `strategy_state` / `strategy_detail`）一个不改** ——
    它们仍供前端「自动策略组」的冻结呈现格式（竖排 tag / 迷你框 / 星级 / 机器人图标）使用。
    本函数只新增 `ATTACH_KEYS`。label 侧失败时**降级为空白**（绝不阻断自选列表）。

    ⚠️ `with_overlay` **默认关**（与方案 §8 的差别，理由记录在案）：
    `/watchlist/get` 是前端**轮询**接口（`WatchlistPanel` 周期性 refresh，比较 signature），
    而实时快照在盘中"每次都必拉"（`cn_stock.get_tickers` 的语义）⇒ 开着会让**每次轮询**
    都额外触发一次批量拉取。实时价格本就由 `/watchlist/prices` 单独轮询提供，
    故列表接口只发**盘后事实**（各段带 `asof.facts` 如实标注），overlay 保留给
    **非轮询**的消费者按需开启（`get_labels(..., with_overlay=True)`）。
    """
    try:
        label_rows = {r["id"]: r for r in get_labels(user_id, with_overlay=with_overlay)}
    except Exception:
        logger.error("[label] 统一出口失败，自选列表降级为空白标签")
        logger.error(traceback.format_exc())
        label_rows = {}

    for row in rows:
        lab = label_rows.get(row.get("id"))
        if not lab:
            row["sections"] = []
            row["grade"] = model.GRADE_BLANK
            row["source"] = None
            row["taken_over"] = False
            row["stale"] = {}
            row["age_days"] = None
            row["label_asof"] = None
            row["label_price"] = None
            row["label_change_pct"] = None
            continue
        row["sections"] = lab["sections"]
        row["grade"] = lab["grade"]
        row["source"] = lab["source"]
        row["taken_over"] = lab["taken_over"]
        row["stale"] = lab["stale"]
        row["age_days"] = lab["age_days"]
        row["label_asof"] = lab["asof"]
        row["label_price"] = lab["price"]
        row["label_change_pct"] = lab["change_pct"]
    return rows


def _pick(rows: List[Dict[str, Any]]):
    """按票挑选：取第一个未失效的行（行已按 grade DESC, created_at DESC 排序）。

    Returns:
        (pick | None, stale: {source: "N 个交易日未更新"})
    """
    from datetime import timedelta

    now = datetime.now()
    pick: Optional[Dict[str, Any]] = None
    stale: Dict[str, str] = {}
    for r in rows:
        if _is_expired(r, now):
            age = None
            start = render_to_dt(r.get("updated_at"))
            if start is not None:
                try:
                    nxt = (start.date() + timedelta(days=1)).strftime("%Y-%m-%d")
                    age = tcal.trading_days_count(nxt, now.strftime("%Y-%m-%d"))
                except Exception:
                    age = None
            stale[r.get("source")] = (f"{age} 个交易日未更新" if age is not None else "已失效")
            continue
        if pick is None:
            pick = r
    return pick, stale


# ═══════════════════════════════════════════════════════════════════
# 写路径 1：system 自算（grade=1，唯一）
# ═══════════════════════════════════════════════════════════════════

def write_system_facts(asof: Optional[str] = None,
                       pairs: Optional[List[tuple]] = None) -> Dict[str, Any]:
    """system 自算并落库（grade=1）。

    - 默认（`pairs=None`）：**每日全量**，范围 = 自选股并集（盘后 job）
    - 传 `pairs=[(market, symbol), ...]`：只补算指定标的 —— 供**新增自选后即时出标签**，
      不必干等下一次盘后 job（否则新票会空窗到次日）

    不产出（市场不支持 / 数据不足 / `W_eff<0.60`）⇒ 该票**不落行**（= 空白）。
    单票失败不阻断整批。
    """
    from app.watchlist.compute import KLINE_LIMIT, system_label
    from app.watchlist.predict import load_market_klines
    from app.services.kline import KlineService

    asof = asof or _today()
    ks = KlineService()
    batch: List[Dict[str, Any]] = []
    failures: List[str] = []
    skipped: List[str] = []

    # 大盘只拉一次，供全部个股的 beta/反向/庄-抗跌共用
    market_klines = load_market_klines(120)

    if pairs is not None:
        pairs = [(m, s) for m, s in pairs if m and s]
    else:
        pairs = store.list_watchlist_symbols()
    for market, symbol in pairs:
        try:
            klines = ks.get_kline(market=market, symbol=symbol, timeframe="1D",
                                  limit=KLINE_LIMIT)
            lab = system_label(market, symbol, klines or [], asof=asof,
                               market_klines=market_klines)
            if lab is None:
                skipped.append(f"{market}:{symbol}")
                continue
            batch.append({"market": market, "symbol": symbol, **lab})
        except Exception as e:
            failures.append(f"{market}:{symbol}")
            logger.warning("[label] system 自算失败 %s:%s: %s", market, symbol, e)

    written = store.upsert_system(batch) if batch else 0
    stats = {
        "asof": asof,
        "scope": len(pairs),
        "written": written,
        "skipped": len(skipped),
        "failed": len(failures),
        "skipped_detail": skipped[:20],
        "failed_detail": failures[:20],
    }
    logger.info("[label] system 每日刷新: %s", stats)
    return stats


# ═══════════════════════════════════════════════════════════════════
# 写路径 2：上级提交（agent / auto 唯一写入口）
# ═══════════════════════════════════════════════════════════════════

def submit(source: str, market: str, symbol: str, payload: Dict[str, Any], *,
           ttl_days: Optional[int] = None, trade_date: Optional[str] = None) -> None:
    """上级（`source ∈ {agent, auto}`）**唯一写入口**。

    - `source → grade` 由 `model.GRADE_TABLE` 映射，**不接受外部传 grade**（无 grade 4）
    - `payload` 按 4 段契约校验（`supports/resistances` 必须带 `price`；`score` 0~100；
      `extras` 单元必须是 `fields`/`table`）
    - `ttl_days`：**完全由提交方决定**（label 侧不加默认、不设上限）。
      `N` ⇒ `expires_at = 第 N 个后续交易日 00:00`（N=2 ⇒ T+1 仍有效、T+2 起失效，即
      "2 个交易日未更新即失效"）；`None` ⇒ 永不过期（NULL）。
      交易日换算在 label 侧完成 —— 提交方只需说"N 个交易日"，不必懂交易日历。
    """
    if source not in model.SUBMIT_SOURCES:
        raise ValueError(f"source 必须是 {model.SUBMIT_SOURCES} 之一，得到 {source!r}")
    if market not in model.SUPPORTED_MARKETS:
        raise ValueError(f"market 必须是 {model.SUPPORTED_MARKETS} 之一，得到 {market!r}")
    if not symbol:
        raise ValueError("symbol 不能为空")
    if ttl_days is not None and (not isinstance(ttl_days, int) or ttl_days <= 0):
        raise ValueError("ttl_days 必须是正整数或 None")

    clean = model.validate_payload(payload, source=source)
    grade = model.GRADE_TABLE[source]
    tdate = trade_date or _today()

    expires_at = None
    if ttl_days is not None:
        try:
            exp_day = tcal.next_trading_day(tdate, int(ttl_days))
        except Exception as e:
            raise ValueError(f"ttl_days 换算失败: {e}")
        expires_at = datetime.combine(datetime.strptime(exp_day, "%Y-%m-%d").date(), dtime.min)

    store.upsert_submitted(
        source=source, market=market, symbol=symbol, trade_date=tdate, grade=grade,
        score=clean["score"], score_version=clean["score_version"],
        supports=clean["supports"], resistances=clean["resistances"], extras=clean["extras"],
        facts_asof=datetime.now(), expires_at=expires_at,
    )
