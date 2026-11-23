# -*- coding: utf-8 -*-
"""
screen_query — 横向（横截面）只读查询层：某一时刻 × 一批标的 × 某组特征

═══════════════════════════════════════════════════════════════
  🚫 分层红线（同 cn_stock_ext）：本文件属 data_sources 层（最底层），
     **禁止 import app.market_cn.***；只读，不写任何表（不建物化、不做预计算）。

  与 cn_stock_ext 的分工 —— 两者互补，不重叠：
    · cn_stock_ext  = **纵向**：一只标的的时间序列（get_kline / index_kline ...）
    · screen_query  = **横向**：某一时刻的一批标的（同刻量能 / 横截面筛选 / 板块聚合）

  为什么需要横向层（现状痛点，均为实测）：
    · 换手率筛选无处可写：换手率 = volume ÷ circ_shares，两列分属
      realtime_snapshot 与 stock_basic_info（同库可 JOIN，但无统一下沉层）；
    · 「盘中最近 5 天含今天此刻」要跨 realtime_snapshot(今日) + kline_1D(历史)；
    · 「全盘此刻量比」要全市场逐拍窗口聚合——原先散落在 skill 层直接写 SQL。

  ── 数据口径矩阵（任何组合都必须能回答「这数从哪来、截到几点」）──
    表                     粒度      可用时段        当日实时  历史深度
    realtime_snapshot      60s/拍    09:15~15:01     是        KEEP_DAYS(8天)
    kline_1D_YYYY          日        盘后落库        盘中缺    多年(分年表)
    kline_1m               1分钟     盘后回填        盘中空    较长
    qd_fund_flow_sector    日×板块   按需同步落库    否        snapshot 8天
    stock_basic_info       静态      随同步更新      —        当前快照

    ⇒ 今日那根「日线」盘中不存在，只能由 snapshot 现算；本模块统一在
      caliber 里标明来源，**绝不把半截快照当收盘价**（cn_stock.py 事故同型）。

  ── 职责边界：什么该进这里，什么不该 ──
    ✅ 收：**本地表**的 SQL 取数与聚合
       realtime_snapshot / kline_1D_YYYY / kline_1m / kline_index_5m /
       stock_basic_info / qd_fund_flow_market / qd_fund_flow_sector；
       以及连接池预热这类池内部细节。
       · qd_fund_flow_sector（来源：概念资金流接入，2026-11-05）：只读接口
         `sector_fund_flow()`，按 board_type + source 硬隔离口径（本地 snapshot
         量价净额 vs 历史残留的东财四档主力不可混排），trade_date <= day 是硬上界。
         该表由 fund_flow_api 按需同步，本层不写、不建物化、不做定时刷新。

    ❌ 不收（各有归属，别顺手塞进来）：
       · 远端行情 get_index_realtime / get_fund_flow_realtime / get_hot_sectors
         远端慢且不稳，要配缓存/超时/降级策略，与本层「本地只读、被动按需」的
         定位冲突；且本文件禁 import app.market_cn，本就进不来。
       · 写库 get_market_kline_writer
         读写分离：写作业只在 cn_stock_putdb，只读层不碰写句柄。
       · 库+远端拼接的 get_kline
         属 cn_stock_ext（纵向时间序列）的职责，横向层不重复实现。
       · 已封装好的 limit_pool / query_dragon_tiger_last_days / basicinfo
         它们各自的 data_sources/utils 模块内部已有 SQL，上层直接调函数即可；
         再包一层只是多一跳，且会把别人的口径复制成本层的负债。
       · 业务模型 predict_next_day
         模型推理不是取数。

    判据一句话：**看「SQL 字面量与连接池操作」落在哪一层**。
      上层调下层函数 = 正常分层；上层自己写 SQL / 摸 pool = 越层。

  ── 返回契约 ──
    所有公开函数返回 dict，且**必带** meta：{caliber, as_of, missing}
    · caliber：数据来源与截断口径（"snapshot_intraday" / "kline_1d_final" / ...）
    · missing：查不到/被排除的标的与原因——覆盖率不到 100%，必须显式告知，
      不得静默当作 0 参与筛选。
"""

from __future__ import annotations

import hashlib
import os
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── snapshot 扫描窗口（自然日）：够盖 6 个交易日 + 长假余量 ──
_SNAP_WINDOW_DAYS = 30
# 进程内被动缓存（非物化表）：as_of 精确到分钟，同分钟内复用
_CACHE_TTL_S = float(os.getenv("QD_SCREEN_QUERY_TTL", "60") or 0)
_CACHE: Dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()


def _pool():
    from app.utils.db_market import get_market_db_manager
    return get_market_db_manager()._get_pool("CNStock")


def warm_pool(workers: int = 8) -> int:
    """把 CNStock 连接池的空闲连接预热到 workers 条，返回最终空闲数（失败 -1）。

    先抬 minconn 再补连接：`_putconn` 见空闲已满 minconn 会把多出的连接直接 close，
    顺序反了预热白做。串行持有 workers 条再统一归还（不用并发 wave：快查询的连接
    会被后续任务弹走复用，wave 结束后空闲数不确定，实测 8 任务只补到 6）。
    预热是优化不是关键路径，失败只记 warning。
    """
    try:
        inner = getattr(_pool(), "_pool", None)
        if inner is None:
            return -1
        want = max(1, int(workers))
        if inner.minconn >= want:
            return len(inner._pool)
        inner.minconn = want
        held = []
        try:
            for _ in range(want):
                held.append(inner.getconn())
        finally:
            for c in held:
                inner.putconn(c)
        logger.info("[screen_query] CNStock 连接池预热: 空闲 %d 条（对齐并发 %d）",
                    len(inner._pool), want)
        return len(inner._pool)
    except Exception as e:
        logger.warning("[screen_query] 连接池预热失败（按原池行为继续）: %s", e)
        return -1


def resolve_as_of(as_of: Optional[str] = None) -> str:
    """分时截断时刻 HH:MM：交易时段内取当前时刻，否则取收盘 15:00。

    非交易时段一律 15:00 —— 盘前当日无 tick，用当前时刻会把「上一交易日收盘」
    截成「上一交易日同一时刻」；非交易日同理只有 15:00 才代表整日。
    """
    if as_of:
        return str(as_of)[:5]
    try:
        from app.utils.trading_calendar import is_trading_day_today
        if not is_trading_day_today():
            return "15:00"
    except Exception:
        return "15:00"
    now = datetime.now().strftime("%H:%M")
    if "09:15" <= now <= "15:01":
        return now
    return "15:00"


def _cache_get(key: str):
    if _CACHE_TTL_S <= 0:
        return None
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if not hit:
        return None
    at, val = hit
    if datetime.now().timestamp() - at > _CACHE_TTL_S:
        return None
    return val


def _cache_put(key: str, val):
    if _CACHE_TTL_S <= 0:
        return
    with _CACHE_LOCK:
        _CACHE[key] = (datetime.now().timestamp(), val)


def _codes_key(codes: Optional[Iterable[str]]) -> str:
    """缓存键里的标的指纹。

    ⚠ 不能只用票数（旧写法 `C{len}`）：`codes=["000001"]` 与 `codes=["002594"]`
      票数都是 1，会命中同一条缓存 —— 单票查询互相污染，实测把 002594/300750 的
      同刻量比/净占比全算成了 000001 的值。必须含标的集合本身（排序后取指纹）。
    """
    if not codes:
        return "ALL"
    lst = sorted({str(c).split(".")[0] for c in codes})
    digest = hashlib.md5(",".join(lst).encode("utf-8")).hexdigest()[:12]
    return f"C{len(lst)}-{digest}"


_SQL_VOL_PANEL = """
SELECT symbol, "time"::date AS d, max(volume) AS v
  FROM realtime_snapshot
 WHERE "time" >= %s::timestamp AND "time" < %s::timestamp + interval '1 day'
   AND "time"::time <= %s::time{flt}
 GROUP BY symbol, "time"::date
"""

_SQL_NET_DAY = """
WITH t AS (
    SELECT symbol, "time"::date AS d, "time", last, volume,
           lag(last) OVER w AS p_last,
           lag(volume) OVER w AS p_vol,
           "previousClose" AS pclose
      FROM realtime_snapshot
     WHERE "time" >= %s::timestamp AND "time" < %s::timestamp + interval '1 day'
       AND "time"::time <= %s::time{flt}
    WINDOW w AS (PARTITION BY symbol, "time"::date ORDER BY "time")
), s AS (
    SELECT symbol, d, "time", last, volume, pclose,
           CASE WHEN p_last IS NOT NULL THEN p_last ELSE last END AS ref,
           volume - coalesce(p_vol, 0) AS dv
      FROM t
), per AS (
    SELECT symbol, d, max(volume) AS v, max(pclose) AS pclose{px},
           sum(CASE WHEN dv > 0 AND last > 0 AND last > ref THEN dv * last
                    WHEN dv > 0 AND last > 0 AND last < ref THEN -(dv * last)
                    ELSE 0 END) AS net,
           sum(CASE WHEN dv > 0 AND last > 0 THEN dv * last ELSE 0 END) AS amt
      FROM s GROUP BY symbol, d
)
SELECT symbol, d, v, pclose, last_px, net, amt FROM per
"""


def snap_panel(as_of: Optional[str] = None, codes: Optional[Iterable[str]] = None,
               days: int = 6, with_net: bool = True, net_days: int = 1,
               end_day: Optional[str] = None, full_net: bool = False,
               with_last: bool = True) -> Dict[str, Any]:
    """同刻面板：截至 as_of 的「每票 × 每日」量能（可选资金净额）。

    量能查询（跨 days 天）与净额查询（跨 net_days 天）分开跑 —— 实测成本与
    **扫描天数成正比**：全市场量能 6 天约 4s；净额 1 天约 4s、6 天约 24s。
    故默认 net_days=1（只有今日有 net/amt，历史日留 None），
    需要「资金净占比 vs 前5日」这类对比时才把 net_days 调大或置 full_net=True。

    量价方向净额口径（与 fund_flow_api._flow_from_1m_day 同构，**不是东财主力口径**）：
      net = Σ 相邻拍 last 变化 × 该拍增量额；amt = Σ 该拍增量额

    Args:
        as_of: 截断时刻 HH:MM；None=此刻（非交易时段自动落 15:00）
        codes: 标的集合；None=全市场，给定则只扫这批（约 0.8s/500）
        days: 量能覆盖的交易日数（含今日）
        with_net: 是否取资金净额（False 时只出量能，省一半时间）
        net_days: 净额覆盖的交易日数（1=只今日，6=含前5日对比，后者慢 6 倍）
        end_day: 扫描截止日 YYYY-MM-DD；None=今天。
                 给历史日即回看那一日，用于回测/历史对账（缺则退化为今天）。
        full_net: True 时净额覆盖与量能相同的整个窗口（每天都有 net/amt）。
                  等价于「一条跨日窗口一次算全」，比 net_days 推断更准且不浪费，
                  代价是慢（全市场约 24s）。需要对比历史日资金净占比时必须用它。
        with_last: 是否产出每票末拍价（`last`）。取它要给每个分组做一次全排序，
                   全市场约多 6s；只要「按日汇总」（如大盘量比/净占比）时置 False 省这笔钱。

    Returns:
        {as_of, days:[...], caliber, rows:{code:{day:{vol,net,amt,last,prev_close}}},
         missing:[{code,reason}], meta}
    """
    t = resolve_as_of(as_of)
    cls = _codes_key(codes)
    day_key = str(end_day)[:10] if end_day else "-"
    key = (f"snap:{t}:{cls}:{days}:{int(with_net)}:{net_days}:"
           f"{day_key}:{int(full_net)}:{int(with_last)}")
    hit = _cache_get(key)
    if hit is not None:
        return hit

    if end_day:
        try:
            end = datetime.strptime(str(end_day)[:10], "%Y-%m-%d")
        except ValueError:
            logger.warning("[screen_query] end_day 非法(%r)，回落今天", end_day)
            end = datetime.now()
    else:
        end = datetime.now()
    multi = bool(codes)
    codes_list = [str(c).split(".")[0] for c in codes] if multi else None
    flt = " AND symbol = ANY(%s)" if multi else ""

    rows: Dict[str, Dict[str, Dict[str, Any]]] = {}
    try:
        with _pool().cursor() as c:
            start = end - timedelta(days=max(_SNAP_WINDOW_DAYS, int(days * 2) + 14))
            # full_net 走「单条净额 SQL 覆盖整窗」：该 SQL 已同时产出 max(volume)/prev_close/
            # 末拍价，再跑一次量能查询纯属浪费（实测 24.6s → 32.0s 的那 7.4s 就是它）。
            net_covers_vol = with_net and full_net
            if not net_covers_vol:
                args: List[Any] = [start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), t]
                if multi:
                    args.append(codes_list)
                c.execute(_SQL_VOL_PANEL.replace("{flt}", flt), tuple(args))
                for sym, d, vol in c.fetchall():
                    code = str(sym).split(".")[0]
                    rows.setdefault(code, {})[str(d)[:10]] = {
                        "vol": float(vol or 0), "net": None, "amt": None,
                        "last": None, "prev_close": None,
                    }
            if with_net:
                if full_net:
                    nstart = start.strftime("%Y-%m-%d")
                elif net_days <= 1:
                    nstart = end.strftime("%Y-%m-%d")
                else:
                    nstart = (end - timedelta(days=int(net_days * 2) + 4)).strftime("%Y-%m-%d")
                nargs: List[Any] = [nstart, end.strftime("%Y-%m-%d"), t]
                if multi:
                    nargs.append(codes_list)
                px_sql = (', (array_agg(last ORDER BY "time" DESC))[1] AS last_px'
                          if with_last else ", NULL::double precision AS last_px")
                c.execute(_SQL_NET_DAY.replace("{flt}", flt).replace("{px}", px_sql),
                          tuple(nargs))
                for sym, d, vol, pclose, last_px, net, amt in c.fetchall():
                    code = str(sym).split(".")[0]
                    rec = rows.setdefault(code, {}).setdefault(
                        str(d)[:10], {"vol": 0.0, "net": None, "amt": None,
                                      "last": None, "prev_close": None})
                    rec["net"] = float(net or 0)
                    rec["amt"] = float(amt or 0)
                    rec["prev_close"] = pclose
                    rec["last"] = last_px
                    if vol:
                        rec["vol"] = float(vol)
    except Exception as e:
        logger.warning("[screen_query] snap_panel 失败: %s", e)
        return {"as_of": t, "days": [], "caliber": "snapshot_intraday", "rows": {},
                "missing": [{"code": "*", "reason": f"{type(e).__name__}: {e}"}],
                "meta": {"error": True}}

    all_days = sorted({d for v in rows.values() for d in v})
    missing: List[Dict[str, str]] = []
    if multi:
        got = set(rows)
        for c0 in codes_list or []:
            if c0 not in got:
                missing.append({"code": c0, "reason": "snapshot 无该标的数据"})
    out = {
        "as_of": t,
        "days": all_days,
        "caliber": "snapshot_intraday" if with_net else "snapshot_intraday_vol_only",
        "rows": {k: {d: v for d, v in m.items() if d in all_days} for k, m in rows.items()},
        "missing": missing,
        "meta": {"trade_days": len(all_days), "codes": len(rows),
                 "with_net": with_net, "net_days": net_days},
    }
    _cache_put(key, out)
    return out


def market_same_t(as_of: Optional[str] = None, days: int = 6,
                  net_days: int = 1, end_day: Optional[str] = None) -> Dict[str, Any]:
    """全市场同刻：按日汇总的量 / 净额 / 成交额 + 量比基准（L1 用）。

    Args:
        net_days: 净额覆盖天数。1（默认）= 只算今日，快；
                  6 = 含前 5 日（用于 fp_prev_pct 对比），慢约 6 倍。

    Returns:
        {as_of, days:[...], by_day:{day:{vol,net,amt,fp_pct}},
         vr, fp_pct, fp_prev_pct, caliber, meta}
        vr = 最新日累计量 ÷ 前若干日同刻均值（缺基准为 None，不伪造 1.0）
    """
    panel = snap_panel(as_of=as_of, codes=None, days=days,
                       with_net=True, net_days=net_days, end_day=end_day)
    by_day: Dict[str, Dict[str, Any]] = {}
    for _code, m in (panel.get("rows") or {}).items():
        for day, v in m.items():
            a = by_day.setdefault(day, {"vol": 0.0, "net": 0.0, "amt": 0.0, "_has_net": False})
            a["vol"] += v["vol"] or 0.0
            if v.get("net") is not None:
                a["net"] += v["net"]
                a["_has_net"] = True
            if v.get("amt") is not None:
                a["amt"] += v["amt"]
    days_sorted = sorted(by_day)
    for d in days_sorted:
        a = by_day[d]
        a["fp_pct"] = (round(a["net"] / a["amt"] * 100, 2)
                       if a["_has_net"] and a["amt"] else None)
    vr = None
    fp_prev = None
    if len(days_sorted) >= 2:
        today = days_sorted[-1]
        prevs = days_sorted[-6:-1] if len(days_sorted) >= 6 else days_sorted[:-1]
        base = [by_day[d]["vol"] for d in prevs if by_day[d]["vol"] > 0]
        if base and by_day[today]["vol"] > 0:
            vr = round(by_day[today]["vol"] / (sum(base) / len(base)), 3)
        fps = [by_day[d]["fp_pct"] for d in prevs if by_day[d].get("fp_pct") is not None]
        if fps:
            fp_prev = round(sum(fps) / len(fps), 2)
    return {
        "as_of": panel.get("as_of"),
        "days": days_sorted,
        "by_day": by_day,
        "vr": vr,
        "fp_pct": by_day.get(days_sorted[-1], {}).get("fp_pct") if days_sorted else None,
        "fp_prev_pct": fp_prev,
        "caliber": panel.get("caliber"),
        "meta": panel.get("meta") or {},
    }


def kline_batch(codes: Iterable[str], days: int = 30, as_of: Optional[str] = None,
                fill_intraday: bool = False) -> Dict[str, Any]:
    """批量日线：{code: [bar...]}，bar 含 time/open/high/low/close/volume。

    只读 kline_1D 分年表，按 start~end 覆盖的年份逐年取数后合并，不受当前年份限制；
    年表存在性由 information_schema 判定，不存在的年不产生查询。**不回落远端**：
    取不到就进 missing，由调用方决定是否逐票兜底（保持本层「只读、被动、可预期」的定位）。
    fill_intraday=True 时，若库内缺「今日」bar 而 snapshot 有今日数据，
    用 snapshot 现算一根**未收盘** bar 补上，并在 caliber 标注 snapshot_intraday。

    Args:
        codes: 标的集合（裸 6 位码）
        days: 回溯交易日数
        as_of: 只取该日（含）之前的 bar；None=最新
        fill_intraday: 是否用 snapshot 补当日未收盘 bar
    """
    codes_list = [str(c).split(".")[0] for c in codes if str(c).strip()]
    if not codes_list:
        return {"codes": {}, "missing": [], "caliber": "kline_1d_final", "meta": {}}
    end = str(as_of)[:10] if as_of else datetime.now().strftime("%Y-%m-%d")
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=int(days * 1.8) + 20)
             ).strftime("%Y-%m-%d")
    wanted_years = set(range(int(start[:4]), int(end[:4]) + 1))
    out: Dict[str, List[Dict[str, Any]]] = {}
    tables: List[str] = []
    try:
        with _pool().cursor() as c:
            c.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                  AND table_name LIKE 'kline_1D_%'
            """)
            existing = set()
            for (name,) in c.fetchall():
                suffix = str(name).rsplit("_", 1)[-1]
                if suffix.isdigit():
                    existing.add(int(suffix))
            years = sorted(wanted_years & existing)
            sql = ('SELECT symbol, "time", open, high, low, close, volume FROM "{}" '
                   'WHERE symbol = ANY(%s) AND "time" >= %s::timestamp '
                   'AND "time" < %s::timestamp + interval \'1 day\' ORDER BY symbol, "time"')
            for year in years:
                table = f"kline_1D_{year}"
                tables.append(table)
                c.execute(sql.format(table), (codes_list, start, end))
                for sym, ts, o, h, low, cl, vol in c.fetchall():
                    code = str(sym).split(".")[0]
                    bar_day = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
                    out.setdefault(code, []).append({
                        "time": bar_day, "date": bar_day, "open": o, "high": h,
                        "low": low, "close": cl, "volume": vol,
                    })
    except Exception as e:
        logger.warning("[screen_query] kline_batch 失败: %s", e)
        return {"codes": {}, "missing": [{"code": "*", "reason": f"{type(e).__name__}: {e}"}],
                "caliber": "kline_1d_final", "meta": {"error": True}}

    for arr in out.values():
        arr.sort(key=lambda b: b["time"])
        if len(arr) > days:
            del arr[:len(arr) - days]

    reason = ",".join(tables) if tables else f"kline_1D {start[:4]}~{end[:4]}"
    missing = [{"code": c, "reason": f"{reason} 无数据"} for c in codes_list if c not in out]
    caliber = "kline_1d_final"
    if fill_intraday:
        t = resolve_as_of(as_of)
        panel = snap_panel(as_of=t, codes=[c for c in codes_list if c in out], days=2)
        today = datetime.now().strftime("%Y-%m-%d")
        filled = 0
        for code, arr in out.items():
            if arr and arr[-1]["time"] == today:
                continue
            rec = ((panel.get("rows") or {}).get(code) or {}).get(today)
            if not rec or not rec.get("last"):
                continue
            arr.append({"time": today, "date": today, "open": None,
                        "high": None, "low": None, "close": rec["last"],
                        "volume": rec["vol"], "partial": True})
            filled += 1
        if filled:
            caliber = "kline_1d_final+snapshot_partial"
    return {"codes": out, "missing": missing, "caliber": caliber,
            "meta": {"requested": len(codes_list), "got": len(out)}}


def kline_batch_qfq(codes: Iterable[str], days: int = 30, as_of: Optional[str] = None,
                    fill_intraday: bool = False) -> Dict[str, Any]:
    """批量日线的前复权视图：复用 kline_batch 的原始 bar，不另起 SQL、不写库。

    ★ 为什么独立接口而不是给 kline_batch 默认复权：连板高度（_topdown）与尾部
      标签（recall_diag）必须用**不复权真实成交价** —— 除权日 qfq 会按比例改写
      前一日价格，涨停判定/次日收益全部失真；GBDT 特征才要求跨除权可比。
      两种口径并存，kline_batch 默认仍返回原始价。

    复权源是 adjustment 的 qfq 因子缓存（只读）。只有缓存里已有的标的才换算，
    缺失标的保持原样并列入 meta.no_factor —— 绝不为此触网，保持本层被动只读。
    volume 沿用 adjustment.unadj_to_qfq 口径：不随价格缩放，与训练侧一致。
    adjustment 在函数内延迟 import：它在模块加载时会拉起因子后台更新线程，
    不能让所有 screen_query 调用方都被动触发。

    Returns: 同 kline_batch，另带 as_of / meta.factor_sig（缓存指纹，供溯源与
    缓存失效）/ meta.qfq_n / meta.no_factor_n / meta.no_factor_sample。
    """
    from app.data_sources.provider import adjustment

    batch = kline_batch(codes, days=days, as_of=as_of, fill_intraday=fill_intraday)
    out = dict(batch)
    bars_by_code = batch.get("codes") or {}
    try:
        raw = adjustment.factor_cache_json()
        cached = {k for k, val in raw.items()
                  if not k.startswith("_") and isinstance(val, list)}
        factor_sig = adjustment.factor_cache_fingerprint()
    except Exception as e:
        logger.warning("[screen_query] kline_batch_qfq 因子缓存读取失败: %s", e)
        cached, factor_sig = set(), "missing"

    adjusted: Dict[str, List[Dict[str, Any]]] = {}
    no_factor: List[str] = []
    for code, bars in bars_by_code.items():
        sina = adjustment.to_sina_code(str(code))
        if not sina or sina not in cached:
            no_factor.append(str(code))
            adjusted[code] = bars
            continue
        try:
            converted = adjustment.unadj_to_qfq(bars, str(code))
        except Exception as e:
            logger.warning("[screen_query] kline_batch_qfq 复权失败 %s: %s", code, e)
            no_factor.append(str(code))
            adjusted[code] = bars
            continue
        if len(converted) == len(bars):
            adjusted[code] = [
                {**bar, **{k: a[k] for k in ("open", "high", "low", "close", "volume")}}
                for bar, a in zip(bars, converted)
            ]
        else:
            adjusted[code] = converted

    qfq_n = len(bars_by_code) - len(no_factor)
    base_caliber = str(batch.get("caliber") or "kline_1d_final")
    if bars_by_code:
        caliber = base_caliber + ("+qfq_cache" if qfq_n else "+qfq_unavailable")
    else:
        caliber = base_caliber
    actual_asof = (str(as_of)[:10] if as_of else
                   max((str(b.get("time"))[:10] for arr in adjusted.values() for b in arr),
                       default=""))
    out.update({
        "codes": adjusted,
        "as_of": actual_asof,
        "caliber": caliber,
        "meta": {
            **(batch.get("meta") or {}),
            "factor_sig": factor_sig,
            "qfq_n": qfq_n,
            "no_factor_n": len(no_factor),
            "no_factor_sample": no_factor[:10],
        },
    })
    return out


_SQL_INDEX_SAME_T = """
SELECT d, c FROM (
    SELECT DISTINCT ON ("time"::date) "time"::date AS d, close AS c
      FROM "kline_index_5m"
     WHERE symbol = %s AND "time" >= %s::timestamp
       AND "time" < %s::timestamp + interval '1 day'
       AND "time"::time <= %s::time
     ORDER BY "time"::date, "time" DESC) x
 ORDER BY d
"""

_INDEX_MAIN = "000001.SH"


def index_same_t(day: Optional[str] = None, t: Optional[str] = None,
                 symbol: str = _INDEX_MAIN, lookback_days: int = 15) -> Optional[float]:
    """指数在 day 日「截至 t」的涨跌%（相对前一交易日同一时刻的收盘）。

    本地 `kline_index_5m`（9 指数 × 130+ 交易日，5 分钟对齐，毫秒级），零外网。
    取「每天 ≤t 的最后一根 5 分钟收盘」，相邻两天相除即同刻涨跌 —— 与全天涨跌
    在 t=15:00 时等价，在盘中则是「同一时刻」口径，不会拿今天 10:00 比昨天收盘。

    ⚠ 上界必须有：只写 `"time" >= d0` 而不写 `< day+1day` 时，库里一旦有更晚的
      交易日，`rows[-1]` 永远是那天 ⇒ 历史日恒返回 None（旧实现即此缺陷）。

    Args:
        day: 目标交易日 YYYY-MM-DD；None=今天
        t: 截断时刻 HH:MM；None=此刻（非交易时段落 15:00）
        symbol: 指数代码，默认上证综指
        lookback_days: 向前回看自然日（只需覆盖上一交易日，15 天够跨长假）

    Returns:
        涨跌%（如 2.19）；数据不足/目标日无数据返回 None（不伪造 0）。
    """
    d_target = str(day)[:10] if day else datetime.now().strftime("%Y-%m-%d")
    hhmm = str(t)[:5] if t else resolve_as_of(None)
    key = f"idxsame:{d_target}:{hhmm}:{symbol}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        d0 = (datetime.strptime(d_target, "%Y-%m-%d")
              - timedelta(days=max(2, int(lookback_days)))).strftime("%Y-%m-%d")
        with _pool().cursor() as c:
            c.execute(_SQL_INDEX_SAME_T, (symbol, d0, d_target, hhmm))
            rows = c.fetchall()
        val: Optional[float] = None
        if (len(rows) >= 2 and str(rows[-1][0])[:10] == d_target
                and rows[-1][1] and rows[-2][1]):
            val = round((float(rows[-1][1]) / float(rows[-2][1]) - 1) * 100, 2)
    except Exception as e:
        logger.warning("[screen_query] index_same_t 失败: %s", e)
        return None
    _cache_put(key, val)
    return val


def cross_section(as_of: Optional[str] = None, features: Optional[List[str]] = None,
                  where: Optional[str] = None, order_by: Optional[str] = None,
                  limit: Optional[int] = None, codes: Optional[Iterable[str]] = None,
                  end_day: Optional[str] = None) -> Dict[str, Any]:
    """横截面：每票一行，含所选特征，可按条件筛选 / 排序 / 截断。

    已支持的特征（其余名称会被忽略并进 missing_features，不静默给 0）：
        change_pct      当日涨跌幅%（snapshot.changePercent）
        turnover_pct    换手率%（volume ÷ stock_basic_info.circ_shares × 100）
        vol_ratio       同刻量比（今日累计量 ÷ 前若干日同刻均值）
        amount          当日成交额（元，逐拍累计）
        net             量价方向净额（元）
        net_pct         净额占成交额%
        fund_circ_ratio 资金流通比% = net ÷ 流通市值 × 100
                        = net ÷ (circ_shares × last) × 100
                        「该股当日（截至 as_of）净流入占其流通市值的比例」，
                        已按盘子大小归一，可跨票/跨板块直接比大小。
        fund_per_share  每股净流入（元/股）= net ÷ circ_shares
                        = fund_circ_ratio ÷ 100 × last，仅作参考；含股价因子，跨票不可比

    资金口径是**量价方向净额**（Σ 相邻拍 last 变化 × 该拍增量额），不是东财四档主力；
    需要「流入总额」口径时用 amount 自行换算。

    Args:
        where: 简单谓词串，形如 "turnover_pct BETWEEN 5 AND 10"；只允许特征名、
               数字与 AND/OR/BETWEEN/比较符，由本模块解析后拼参数，不接受任意 SQL。
        order_by: 特征名，前缀 "-" 表示降序。
    """
    feats = [f for f in (features or ["change_pct"]) if str(f).strip()]
    panel = snap_panel(as_of=as_of, codes=codes, days=6, end_day=end_day)
    rows_in = panel.get("rows") or {}
    days = panel.get("days") or []
    today = days[-1] if days else None
    prev = days[:-1][-5:] if len(days) > 1 else []

    base_mkt = (market_same_t(as_of=as_of, days=6, end_day=end_day)
                if "vol_ratio" in feats else None)
    mkt_by_day = (base_mkt or {}).get("by_day") or {}

    need_shares = any(f in feats for f in ("turnover_pct", "fund_circ_ratio", "fund_per_share"))
    shares = _shares_map(list(rows_in) if rows_in else None) if need_shares else {}

    out_rows: List[Dict[str, Any]] = []
    for code, m in rows_in.items():
        cur = m.get(today) or {}
        vol = cur.get("vol") or 0.0
        net_v = cur.get("net")
        row: Dict[str, Any] = {"code": code}
        row["change_pct"] = _change_pct(cur)
        row["amount"] = cur.get("amt")
        row["net"] = net_v
        row["net_pct"] = round(cur["net"] / cur["amt"] * 100, 2) if cur.get("amt") else None
        if need_shares:
            cs = shares.get(code)
            if "turnover_pct" in feats:
                row["turnover_pct"] = round(vol / cs * 100, 2) if cs else None
            if "fund_circ_ratio" in feats:
                px = _num(cur.get("last"))
                row["fund_circ_ratio"] = (round(net_v / (cs * px) * 100, 4)
                                          if cs and px and net_v is not None else None)
            if "fund_per_share" in feats:
                row["fund_per_share"] = (round(net_v / cs, 4)
                                         if cs and net_v is not None else None)
        if "vol_ratio" in feats:
            base = [m[d]["vol"] for d in prev if d in m and m[d].get("vol")]
            row["vol_ratio"] = round(vol / (sum(base) / len(base)), 3) if base and vol else None
        out_rows.append(row)

    unknown = [f for f in feats if f not in ("change_pct", "turnover_pct", "vol_ratio",
                                             "amount", "net", "net_pct",
                                             "fund_circ_ratio", "fund_per_share")]
    if where:
        out_rows = _apply_where(out_rows, where)
    if order_by:
        desc = str(order_by).startswith("-")
        fname = str(order_by).lstrip("-")
        out_rows.sort(key=lambda r: (r.get(fname) is None,
                                     -(r.get(fname) or 0) if desc else (r.get(fname) or 0)))
    if limit:
        out_rows = out_rows[:int(limit)]

    missing = list(panel.get("missing") or [])
    share_feats = [f for f in ("turnover_pct", "fund_circ_ratio", "fund_per_share") if f in feats]
    if share_feats:
        no_share = [r["code"] for r in out_rows if r.get(share_feats[0]) is None]
        if no_share:
            missing.append({
                "code": "*",
                "reason": (f"circ_shares 缺失 {len(no_share)}/{len(out_rows)} 只，"
                           f"{'/'.join(share_feats)} 为 None（北证 920 段结构性缺失，"
                           f"其余为每日同步偶发失败）"),
                "codes_sample": no_share[:20],
            })
    return {
        "as_of": panel.get("as_of"),
        "day": today,
        "caliber": panel.get("caliber"),
        "rows": out_rows,
        "n": len(out_rows),
        "missing": missing,
        "missing_features": unknown,
        "meta": {"market_vr": (base_mkt or {}).get("vr") if base_mkt else None},
    }


def _group_by_tags(field: str, label: str, as_of: Optional[str] = None,
                   top: Optional[int] = None, codes: Optional[Iterable[str]] = None,
                   sort_by: str = "net", min_members: Optional[int] = None,
                   max_members: Optional[int] = None,
                   end_day: Optional[str] = None) -> Dict[str, Any]:
    """按某标签字段聚合板块同刻量能与资金的通用实现（industry / concepts 共用）。

    标签是逗号多值串，一只票会同时计入多个板块 ⇒ 各板块只数之和 > 全市场只数，
    这是设计如此，不是重复计数。

    sort_by: "net"（净额绝对值）| "net_pct"（净额 ÷ 成交额，剔除板块大小差异）。
      ⚠ 选板块建议用 net_pct —— net 绝对值会让成分多的大板块永远排前面。
    min_members / max_members: 成分数区间过滤（None=不过滤）。用途：剔除
      「融资融券 / 深股通 / 专精特新」这类属性标签（成分上千只，等于全市场），
      以及只有几只票的小概念（净占比被 1~2 只暴涨票主导）。
    """
    panel = snap_panel(as_of=as_of, codes=codes, days=6, end_day=end_day)
    rows_in = panel.get("rows") or {}
    days = panel.get("days") or []
    today = days[-1] if days else None
    prev = days[:-1][-5:] if len(days) > 1 else []
    tmap = _tag_map(field, [str(c).split(".")[0] for c in codes] if codes else None)

    agg: Dict[str, Dict[str, Any]] = {}
    for code, m in rows_in.items():
        cur = m.get(today) or {}
        vol, net, amt = cur.get("vol") or 0.0, cur.get("net") or 0.0, cur.get("amt") or 0.0
        base = [m[d]["vol"] for d in prev if d in m and m[d].get("vol")]
        vr = (vol / (sum(base) / len(base))) if base and vol else None
        for tag in tmap.get(code, []):
            a = agg.setdefault(tag, {"n": 0, "vol": 0.0, "net": 0.0, "amt": 0.0, "_vr": []})
            a["n"] += 1; a["vol"] += vol; a["net"] += net; a["amt"] += amt
            if vr is not None:
                a["_vr"].append(vr)
    boards = []
    dropped = {"too_small": 0, "too_wide": 0}
    for tag, a in agg.items():
        if min_members and a["n"] < int(min_members):
            dropped["too_small"] += 1
            continue
        if max_members and a["n"] > int(max_members):
            dropped["too_wide"] += 1
            continue
        boards.append({
            label: tag, "n": a["n"],
            "amt": a["amt"], "net": a["net"],
            "net_pct": round(a["net"] / a["amt"] * 100, 2) if a["amt"] else None,
            "vol_ratio": round(sum(a["_vr"]) / len(a["_vr"]), 3) if a["_vr"] else None,
        })
    key = "net_pct" if str(sort_by) == "net_pct" else "net"
    boards.sort(key=lambda b: -(b.get(key) if b.get(key) is not None else -1e18))
    if top:
        boards = boards[:int(top)]
    missing = list(panel.get("missing") or [])
    no_tag = [c for c in rows_in if not tmap.get(c)]
    if no_tag:
        missing.append({"code": "*",
                        "reason": f"{field} 未归类 {len(no_tag)}/{len(rows_in)} 只，"
                                  f"未计入任何板块",
                        "codes_sample": no_tag[:20]})
    return {"as_of": panel.get("as_of"), "boards": boards, "n": len(boards),
            "missing": missing,
            "meta": {"field": field, "codes": len(rows_in), "day": today,
                     "sort_by": key, "min_members": min_members,
                     "max_members": max_members, "dropped": dropped,
                     "coded_tags": len({t for v in tmap.values() for t in v})}}


def group_by_industry(as_of: Optional[str] = None, top: Optional[int] = None,
                      codes: Optional[Iterable[str]] = None,
                      sort_by: str = "net",
                      min_members: Optional[int] = None,
                      max_members: Optional[int] = None,
                      end_day: Optional[str] = None) -> Dict[str, Any]:
    """按本地 industry 标签聚合板块同刻量能与资金（零外网）。

    min_members / max_members: 成分数区间过滤（None=不过滤）。行业标签是申万三级，
      「白银」「光学元件」这类可能全 A 只有 1 只票，选股场景应传 min_members 过滤。

    Returns:
        {as_of, boards:[{industry, n, amt, net, net_pct, vol_ratio}], missing, meta}
    """
    return _group_by_tags("industry", "industry", as_of=as_of, top=top,
                          codes=codes, sort_by=sort_by,
                          min_members=min_members, max_members=max_members,
                          end_day=end_day)


def group_by_concept(as_of: Optional[str] = None, top: Optional[int] = None,
                     codes: Optional[Iterable[str]] = None,
                     sort_by: str = "net_pct",
                     min_members: Optional[int] = None,
                     max_members: Optional[int] = None,
                     end_day: Optional[str] = None) -> Dict[str, Any]:
    """按本地 concepts 标签聚合概念板块同刻量能与资金（零外网）。

    与 group_by_industry 同构，只是标签源换成 `stock_basic_info.concepts`
    （覆盖 92.7%，约 405 个概念标签）。字段名用 `concept` 以区分行业轨。

    **默认成分数 8~300 + 按 net_pct 排序**，这是实测定的：
      · 按 net 排序时前排全是属性标签 —— 融资融券(3460) / 深股通(1710) /
        沪股通(1522) / 专精特新(1073)，等于全市场，没有选股意义；
      · 按 net_pct 排序又会被 10 只成分的小概念主导（1~2 只暴涨票即 25%+）。
    区间与排序键都可用 env / 入参覆盖（QD_CONCEPT_MIN_MEMBERS / MAX_MEMBERS）。

    Returns:
        {as_of, boards:[{concept, n, amt, net, net_pct, vol_ratio}], missing, meta}
    """
    mn = _CONCEPT_MIN_MEMBERS if min_members is None else int(min_members)
    mx = _CONCEPT_MAX_MEMBERS if max_members is None else int(max_members)
    return _group_by_tags("concepts", "concept", as_of=as_of, top=top,
                          codes=codes, sort_by=sort_by,
                          min_members=mn or None, max_members=mx or None,
                          end_day=end_day)


def board_series(codes: Iterable[str], day: Optional[str] = None,
                 t_from: str = "09:30", t_to: Optional[str] = None,
                 min_members_at_time: int = 5) -> Dict[str, Any]:
    """板块合成序列：成员逐票 ticks → 逐分钟「等权涨幅中位数」。

    供「跟随判定」做基准。用**中位数而非均值**：一只票暴涨不会把基准拉飞，
    这对「板块整体在动」的判断更稳（均值会被 1 只妖股主导）。

    ⚠ 调用方负责剔除涨停票再传进来 —— 封死不动的票会把中位数压平，失去基准意义。
      本函数不知道「涨停」是什么，只做纯合成（数据层不含策略语义）。

    Args:
        codes: 板块成员
        day: 目标日；None=今天
        t_from / t_to: 分钟窗口
        min_members_at_time: 某分钟参与合成的成员数下限；不足则该分钟 pct=None。
          尾盘集合竞价时只有少数票还有 tick（实测 15:00 只剩 19/157），
          此时算中位数没有代表性。

    Returns:
        {"times": ["09:30","09:31",...],
         "pct":   [0.12, -0.05, ...],        # 逐分钟成员涨幅中位数（%）
         "n": {分钟: 参与合成的票数},
         "members": 有数据的成员数, "caliber": "snapshot_ticks", "missing": [...]}
    """
    d = str(day)[:10] if day else datetime.now().strftime("%Y-%m-%d")
    key = f"bser:{_codes_key(codes)}:{d}:{str(t_from)[:5]}-{str(t_to)[:5] if t_to else '-'}"
    hit = _cache_get(key)
    if hit is not None:
        return hit

    got = ticks(codes, day=d, t_from=t_from, t_to=t_to)
    # 逐票：每分钟取该分钟最后一拍价格（同一分钟可能有 1~2 拍）
    per_min: Dict[str, Dict[str, float]] = {}
    prev_close: Dict[str, float] = {}
    for code, box in got.items():
        pc = box.get("prev_close")
        if not pc:
            continue
        prev_close[code] = float(pc)
        m = per_min.setdefault(code, {})
        for hhmm, px, _cum in box.get("ticks") or []:
            m[hhmm] = float(px)          # 后写覆盖 ⇒ 该分钟最后一拍

    times = sorted({t for m in per_min.values() for t in m})
    pct: List[Optional[float]] = []
    cnt: Dict[str, int] = {}
    for t in times:
        vals = [ (per_min[c][t] / prev_close[c] - 1) * 100
                 for c in per_min if t in per_min[c] and prev_close.get(c) ]
        cnt[t] = len(vals)
        if len(vals) < max(1, int(min_members_at_time)):
            pct.append(None)
        else:
            vals.sort()
            n = len(vals)
            pct.append(round(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2, 4))

    missing: List[Dict[str, str]] = []
    no_data = [str(c).split(".")[0] for c in codes if str(c).split(".")[0] not in per_min]
    if no_data:
        missing.append({"code": "*", "reason": f"无当日 ticks {len(no_data)} 只，未参与合成",
                        "codes_sample": no_data[:20]})
    out = {"day": d, "times": times, "pct": pct, "n": cnt,
           "members": len(per_min), "caliber": "snapshot_ticks",
           "min_members_at_time": int(min_members_at_time), "missing": missing}
    _cache_put(key, out)
    return out


_SQL_WINDOW = """
WITH t AS (
    SELECT symbol, "time"::date AS d, "time", last, volume,
           lag(last) OVER w AS p_last,
           lag(volume) OVER w AS p_vol,
           "previousClose" AS pclose
      FROM realtime_snapshot
     WHERE "time" >= %s::timestamp AND "time" < %s::timestamp + interval '1 day'
       AND "time"::time <= %s::time{flt}
    WINDOW w AS (PARTITION BY symbol, "time"::date ORDER BY "time")
), s AS (
    SELECT symbol, d, "time", last, volume, pclose,
           CASE WHEN p_last IS NOT NULL THEN p_last ELSE last END AS ref,
           volume - coalesce(p_vol, 0) AS dv
      FROM t
)
SELECT symbol, d,
       sum(CASE WHEN "time"::time >= %s::time THEN dv ELSE 0 END) AS vol_win,
       sum(CASE WHEN "time"::time >= %s::time AND dv > 0 AND last > 0 AND last > ref
                THEN dv * last
                WHEN "time"::time >= %s::time AND dv > 0 AND last > 0 AND last < ref
                THEN -(dv * last) ELSE 0 END) AS net,
       sum(CASE WHEN "time"::time >= %s::time AND dv > 0 AND last > 0
                THEN dv * last ELSE 0 END) AS amt,
       max("time") AS last_ts,
       (array_agg(last ORDER BY "time" DESC)
        FILTER (WHERE "time"::time >= %s::time))[1] AS last_px
  FROM s GROUP BY symbol, d
"""


def window_panel(as_of: Optional[str] = None, t1: Optional[str] = None,
                 t2: Optional[str] = None, codes: Optional[Iterable[str]] = None,
                 lookback: int = 5, with_fund_ratio: bool = True) -> Dict[str, Any]:
    """任意时段 [t1, t2] 的量能 / 资金净额，及与前 lookback 日**同一时段**的比值。

    这是「不同时段资金流 / 资金同比 / 实时量比 / 资金流通比」的统一入口：
      · 不同时段：改 t1/t2（开盘段 09:30~10:00、尾盘 14:30~15:00、最近 30 分钟…）
      · 资金同比：`ratio[code]["net"]` = 今日时段净额 ÷ 前 N 日同一时段均值
      · 实时量比：`ratio[code]["vol"]` = 今日时段成交量 ÷ 前 N 日同时段均值
      · 资金流通比：`today[code]["fund_circ_ratio"]` = 该时段净额 ÷ 流通市值 × 100（%），
        已按盘子归一，可跨票比较。分母取「流通股本 × 该时段末价格」。
        历史日不产出该字段（前 N 日只有均值、没有对应价格，硬算会引入隐含误差），
        要看趋势请用 `ratio[code]["net"]`（净额同比）——分母不变时它与资金流通比同比等价。

    ⚠ 边界陷阱（实现要点）：窗口起点不是开盘时，`lag()` 那条前值必须由窗口外的上一拍
      提供，否则首拍 dv 会退化成「全天累计量」而严重高估。故 SQL 的 WHERE 只切到 t2，
      t1 之前的数据仍参与 lag，仅在累加时用 `"time"::time >= t1` 过滤。

    成本：扫描量 ∝ 到 t2 的拍数 × 天数。早盘 t2=10:00 约 3s，全天 t2=15:00 约 24s。

    Args:
        as_of: 时刻；None=此刻。t2 缺省时取 as_of
        t1: 窗口起点 HH:MM；缺省 09:15（= 当日开盘起）
        t2: 窗口终点 HH:MM；缺省 as_of
        codes: 标的；None=全市场
        lookback: 前 N 个交易日做同窗对比（0=不对比，只出今日）
        with_fund_ratio: 是否附带资金流通比（需查一次流通股本；缺 circ_shares 时为 None）

    Returns:
        {
          "t1","t2","as_of","days":[...],
          "today": {code:{vol,net,amt,pct,last,last_ts,fund_circ_ratio}},
          "prev":  {code:{vol,net,amt,pct}},                  # 前 N 日同窗**均值**
          "ratio": {code:{"vol":量比,"net":同比,"pct":占比差}},
          "caliber":"snapshot_window", "missing":[...]
        }
    """
    as_of_r = resolve_as_of(as_of)
    t_end = str(t2)[:5] if t2 else as_of_r
    t_beg = str(t1)[:5] if t1 else "09:15"
    if t_beg > t_end:
        t_beg, t_end = t_end, t_beg
    lb = max(0, int(lookback))
    cls = _codes_key(codes)
    key = f"win:{as_of_r}:{t_beg}-{t_end}:{cls}:{lb}:{int(with_fund_ratio)}"
    hit = _cache_get(key)
    if hit is not None:
        return hit

    end = datetime.now()
    start = end - timedelta(days=int(lb * 2) + 12)
    multi = bool(codes)
    codes_list = [str(c).split(".")[0] for c in codes] if multi else None
    flt = " AND symbol = ANY(%s)" if multi else ""
    args: List[Any] = [start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), t_end]
    if multi:
        args.append(codes_list)
    args.extend([t_beg] * 5)

    per_day: Dict[str, Dict[str, Dict[str, Any]]] = {}
    try:
        with _pool().cursor() as c:
            c.execute(_SQL_WINDOW.replace("{flt}", flt), tuple(args))
            for sym, d, vol, net, amt, last_ts, last_px in c.fetchall():
                code = str(sym).split(".")[0]
                per_day.setdefault(code, {})[str(d)[:10]] = {
                    "vol": float(vol or 0), "net": float(net or 0),
                    "amt": float(amt or 0),
                    "pct": round(float(net or 0) / float(amt) * 100, 2) if amt else None,
                    "last": last_px, "last_ts": str(last_ts)[:19] if last_ts else None,
                }
    except Exception as e:
        logger.warning("[screen_query] window_panel 失败: %s", e)
        return {"t1": t_beg, "t2": t_end, "as_of": as_of_r, "days": [],
                "today": {}, "prev": {}, "ratio": {},
                "caliber": "snapshot_window",
                "missing": [{"code": "*", "reason": f"{type(e).__name__}: {e}"}]}

    today: Dict[str, Dict[str, Any]] = {}
    prev: Dict[str, Dict[str, Any]] = {}
    ratio: Dict[str, Dict[str, Any]] = {}
    all_days = sorted({d for m in per_day.values() for d in m})
    today_day = all_days[-1] if all_days else None
    prev_days = all_days[:-1][-lb:] if lb else []
    shares = _shares_map(list(per_day)) if (with_fund_ratio and per_day) else {}
    for code, m in per_day.items():
        cur = m.get(today_day) if today_day else None
        cs = shares.get(code)
        if cur:
            rec = dict(cur)
            px = _num(rec.get("last"))
            rec["fund_circ_ratio"] = (round(rec["net"] / (cs * px) * 100, 4)
                                      if cs and px else None)
            today[code] = rec
        if prev_days:
            vals = [m[d] for d in prev_days if d in m]
            if vals:
                av = {k: sum(v[k] or 0 for v in vals) / len(vals)
                      for k in ("vol", "net", "amt")}
                av["pct"] = (round(av["net"] / av["amt"] * 100, 2)
                             if av["amt"] else None)
                prev[code] = av
        t, p = today.get(code), prev.get(code)
        if t and p:
            ratio[code] = {
                "vol": round(t["vol"] / p["vol"], 3) if p["vol"] else None,
                "net": round(t["net"] / p["net"], 3) if p["net"] else None,
                "pct": (round(t["pct"] - p["pct"], 2)
                        if t["pct"] is not None and p["pct"] is not None else None),
            }

    missing: List[Dict[str, str]] = []
    if multi:
        for c0 in codes_list or []:
            if c0 not in today:
                missing.append({"code": c0, "reason": "该时段无 snapshot 数据"})
    if lb and not prev_days:
        missing.append({"code": "*", "reason": "前 N 日无同窗数据，ratio 为空"})

    out = {
        "t1": t_beg, "t2": t_end, "as_of": as_of_r,
        "days": all_days,
        "prev_days": prev_days,
        "today": today, "prev": prev, "ratio": ratio,
        "caliber": "snapshot_window",
        "missing": missing,
        "meta": {"codes": len(today), "lookback": lb, "today_day": today_day},
    }
    _cache_put(key, out)
    return out


def _num(v: Any) -> Optional[float]:
    """安全转 float：None/空串/非数字一律 None（不返回 0，避免把缺失当真实值）。"""
    try:
        if v is None or v == "":
            return None
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _change_pct(cur: Dict[str, Any]) -> Optional[float]:
    last, pclose = cur.get("last"), cur.get("prev_close")
    try:
        if last and pclose:
            return round((float(last) / float(pclose) - 1) * 100, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return None


def _shares_map(codes: Optional[List[str]] = None) -> Dict[str, float]:
    """流通股本（股）。codes=None 时取全市场 active（一次全表，比 ANY 大数组快）。

    覆盖率实测 82.6%：北证 920 段 100% 缺（结构性），其余板块约 10~18% 缺
    （每日同步时东财单票接口偶发失败）。缺失票不补默认值，由调用方显式标 None。

    ★ 这是**当前快照**而非历史时点值：`stock_basic_info` 全表每日刷新
    `updated_at`，库内不存在按日版本表。回测回看历史日时用它算
    `fund_circ_ratio` 属已知静态数据残留（见 docs §13.4），不要当作
    as_of 时点的真实流通股本。
    """
    try:
        with _pool().cursor() as c:
            if codes:
                c.execute("SELECT symbol, circ_shares FROM stock_basic_info "
                          "WHERE symbol = ANY(%s) AND circ_shares > 0", (list(codes),))
            else:
                c.execute("SELECT symbol, circ_shares FROM stock_basic_info "
                          "WHERE circ_shares > 0 AND status = 'active'")
            return {str(s).split(".")[0]: float(v) for s, v in c.fetchall() if v}
    except Exception as e:
        logger.warning("[screen_query] circ_shares 查询失败: %s", e)
        return {}


_TAG_FIELDS = ("industry", "concepts")

# 概念轨的成分数区间：太宽泛（融资融券/深股通这类属性标签动辄上千只，等于全市场）
# 与太窄（<8 只，1~2 只票暴涨就主导净占比）都不具备「从板块选股」的意义。
_CONCEPT_MIN_MEMBERS = int(os.getenv("QD_CONCEPT_MIN_MEMBERS", "20") or 0)
_CONCEPT_MAX_MEMBERS = int(os.getenv("QD_CONCEPT_MAX_MEMBERS", "300") or 0)


def _tag_map(field: str, codes: Optional[List[str]] = None) -> Dict[str, List[str]]:
    """标签集合（逗号多值已拆分）。codes=None 时取全市场 active。

    field 只允许 `industry` / `concepts`（白名单，防 SQL 注入）。
    标签只覆盖有值的 active 票；缺的票不出现，调用方须自行用
    `set(codes) - set(tmap)` 识别未归类标的，不要当成"无标签"。

    ★ 标签是**当前快照**，无历史时点版本（同上 `_shares_map`）。历史回测里
    板块成分是「今天归类」而非「当日归类」，属已知静态数据残留（docs §13.4）。
    """
    if field not in _TAG_FIELDS:
        raise ValueError(f"非法标签字段: {field!r}（只允许 {_TAG_FIELDS}）")
    try:
        with _pool().cursor() as c:
            if codes:
                c.execute(f"SELECT symbol, {field} FROM stock_basic_info "
                          f"WHERE symbol = ANY(%s)", (list(codes),))
            else:
                c.execute(f"SELECT symbol, {field} FROM stock_basic_info "
                          f"WHERE status = 'active' AND {field} IS NOT NULL")
            return {str(s).split(".")[0]: [t.strip() for t in str(v or "").split(",") if t.strip()]
                    for s, v in c.fetchall()}
    except Exception as e:
        logger.warning("[screen_query] %s 查询失败: %s", field, e)
        return {}


def _industry_map(codes: Optional[List[str]] = None) -> Dict[str, List[str]]:
    """行业标签（`_tag_map("industry")` 的语义别名，保留既有调用方）。"""
    return _tag_map("industry", codes)


_ALLOWED_OPS = ("BETWEEN", ">=", "<=", "!=", "=", ">", "<")
_ALLOWED_JOIN = ("AND", "OR")


def _apply_where(rows: List[Dict[str, Any]], where: str) -> List[Dict[str, Any]]:
    """极简谓词解析：<feature> <op> <num> [AND/OR ...]，支持 BETWEEN a AND b。

    刻意不支持任意 SQL —— 本层只做特征筛选，不接受注入面。
    """
    expr = str(where).strip()
    for tok in (";", "--", "/*", "*/", "DROP", "SELECT", "INSERT", "UPDATE", "DELETE"):
        if tok.upper() in expr.upper():
            raise ValueError(f"where 含非法片段: {tok}")

    def _one(r: Dict[str, Any], part: str) -> bool:
        p = part.strip()
        if not p:
            return True
        up = p.upper()
        if " BETWEEN " in up:
            head, rest = up.split(" BETWEEN ", 1)
            lo_s, hi_s = rest.split(" AND ", 1)
            fname = head.strip().lower()
            v = r.get(fname)
            if v is None:
                return False
            return float(lo_s) <= float(v) <= float(hi_s)
        for op in _ALLOWED_OPS:
            if f" {op} " in p:
                fname, val = p.split(f" {op} ", 1)
                v = r.get(fname.strip().lower())
                if v is None:
                    return False
                try:
                    num = float(val)
                except ValueError:
                    return False
                v = float(v)
                return {"=": v == num, ">": v > num, "<": v < num,
                        ">=": v >= num, "<=": v <= num, "!=": v != num}[op]
        return True

    out = []
    for r in rows:
        ok = True
        cur = None
        for chunk in _split_join(expr):
            val, joiner = chunk
            if cur is None:
                cur = _one(r, val)
            elif joiner == "OR":
                cur = cur or _one(r, val)
            else:
                cur = cur and _one(r, val)
        ok = bool(cur)
        if ok:
            out.append(r)
    return out


def _split_join(expr: str):
    """按顶层 AND/OR 切分（BETWEEN .. AND .. 的内层 AND 不切）。"""
    tokens = expr.split()
    chunks, buf, joiner = [], [], None
    i, in_between = 0, False
    for tk in tokens:
        up = tk.upper()
        if up == "BETWEEN":
            in_between = True
            buf.append(tk)
            continue
        if up in _ALLOWED_JOIN:
            if in_between and up == "AND":
                buf.append(tk)
                in_between = False
                continue
            if buf:
                chunks.append((" ".join(buf), joiner))
            buf, joiner = [], up
            continue
        buf.append(tk)
    if buf:
        chunks.append((" ".join(buf), joiner))
    return chunks


# ═══════════════════════════════════════════════════════════════
#  日频 / 分钟 tick 取数（供大盘日线校准、分时形态、尾盘启动扫描）
#  本层只做「拿数」，复权、选择、阈值判定一律留在调用方 —— 数据层不承载业务语义。
# ═══════════════════════════════════════════════════════════════

_SQL_MKT_DAY_VOL = """
SELECT "time"::date AS d, sum(volume) AS v
  FROM "{table}"
 WHERE "time" >= %s::timestamp
 GROUP BY 1 ORDER BY 1
"""

_SQL_MKT_DAY_FLOW = """
SELECT "trade_date"::text, "main_net", "turnover"
  FROM "qd_fund_flow_market"
 WHERE "trade_date" >= %s
"""

_SQL_TICKS = """
SELECT symbol, "time", last, volume, "previousClose"
  FROM realtime_snapshot
 WHERE symbol = ANY(%s) AND "time" >= %s::timestamp
   AND "time" < %s::timestamp + interval '1 day'
   AND "time"::time >= %s::time AND "time"::time <= %s::time
   AND last > 0 AND volume > 0
 ORDER BY symbol, "time"
"""

_SQL_LIMIT_TOUCH = """
WITH w AS (
    SELECT symbol, name, "time", last, "previousClose",
           max(last) OVER (PARTITION BY symbol) AS hi,
           row_number() OVER (PARTITION BY symbol ORDER BY "time" DESC) AS rn
      FROM realtime_snapshot
     WHERE "time"::date = %s::date AND "time"::time <= %s::time
       AND symbol = ANY(%s) AND last > 0
), fh AS (
    SELECT DISTINCT ON (symbol) symbol, "time" AS hi_time
      FROM w WHERE last >= hi - 1e-9
     ORDER BY symbol, "time"
)
SELECT l.symbol, l.name, l."previousClose", l.last, l.hi, f.hi_time
  FROM (SELECT * FROM w WHERE rn = 1) l
  JOIN fh f USING (symbol)
"""

_SQL_EOD_SNAP = """
WITH t AS (
    SELECT symbol, "time", name, last, high, "previousClose" AS pclose, volume,
           lag(last) OVER (PARTITION BY symbol ORDER BY "time") AS p_last,
           lag(volume) OVER (PARTITION BY symbol ORDER BY "time") AS p_vol
      FROM realtime_snapshot
     WHERE "time" >= %s::timestamp AND "time" < %s::timestamp + interval '1 day'
)
SELECT symbol,
       (array_agg(last ORDER BY "time" DESC))[1] AS last,
       max(name) AS name,
       max(high) AS day_high,
       max(pclose) AS prev_close,
       coalesce(max(CASE WHEN "time"::time < TIME '14:30' THEN volume END), 0) AS v_am,
       coalesce(max(volume), 0)
         - coalesce(max(CASE WHEN "time"::time < TIME '14:30' THEN volume END), 0) AS v_pm,
       sum(CASE WHEN "time"::time >= TIME '14:30' AND p_vol IS NOT NULL THEN
                 CASE WHEN last > p_last THEN volume - p_vol
                      WHEN last < p_last THEN -(volume - p_vol) ELSE 0 END
            ELSE 0 END) AS pm_flow,
       max("time") AS last_t
  FROM t GROUP BY symbol
"""

_SQL_EOD_1M = """
WITH t AS (
    SELECT symbol, "time", high, close, volume,
           lag(close) OVER (PARTITION BY symbol ORDER BY "time") AS p_close
      FROM "{table}"
     WHERE "time" >= %s::timestamp AND "time" < %s::timestamp + interval '1 day'
)
SELECT symbol,
       (array_agg(close ORDER BY "time" DESC))[1] AS last,
       max(high) AS day_high,
       sum(CASE WHEN "time"::time < TIME '14:30' THEN volume ELSE 0 END) AS v_am,
       sum(CASE WHEN "time"::time >= TIME '14:30' THEN volume ELSE 0 END) AS v_pm,
       sum(CASE WHEN "time"::time >= TIME '14:30' AND p_close IS NOT NULL THEN
                 CASE WHEN close > p_close THEN volume
                      WHEN close < p_close THEN -volume ELSE 0 END
            ELSE 0 END) AS pm_flow,
       max("time") AS last_t
  FROM t GROUP BY symbol
"""

_SQL_EOD_PREV = """
SELECT DISTINCT ON (symbol) symbol, close AS prev_close, "time" AS prev_t
  FROM "{table}"
 WHERE "time" >= %s::timestamp AND "time" <= %s::timestamp
 ORDER BY symbol, "time" DESC
"""

_SQL_SECTOR_FLOW = """
WITH d AS (
    SELECT DISTINCT trade_date FROM qd_fund_flow_sector
     WHERE board_type = %s AND source = %s AND trade_date <= %s::date
     ORDER BY trade_date DESC LIMIT %s
)
SELECT trade_date, code, name, main_net, main_pct, in_net, out_net, turnover
  FROM qd_fund_flow_sector
 WHERE board_type = %s AND source = %s AND trade_date IN (SELECT trade_date FROM d)
 ORDER BY trade_date, code
"""


def _year_table(prefix: str, day: Optional[str] = None) -> str:
    """分年表的表名（`kline_1D_2026`）。年段必须是 4 位数字，防注入。"""
    y = str(day)[:4] if day else datetime.now().strftime("%Y")
    if not (len(y) == 4 and y.isdigit()):
        raise ValueError(f"非法年份: {y!r}")
    return f"{prefix}_{y}"


def market_day_volume(back_days: int = 45, end_day: Optional[str] = None) -> Dict[str, float]:
    """全市场每日成交量（`kline_1D_<年>` 按日汇总）。供大盘日线口径量比的分母。

    ⚠ 只查 `end_day` 当年的分年表：跨年回溯（1 月初看 45 天）会缺上一年，
      与原实现口径一致；需要跨年请显式分两次取再合并。

    Returns: {day: total_volume}；失败返回 {}（调用方按「缺数据」处理，不伪造 0）。
    """
    end = str(end_day)[:10] if end_day else datetime.now().strftime("%Y-%m-%d")
    key = f"mktvol:{end}:{int(back_days)}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        table = _year_table("kline_1D", end)
        start = (datetime.strptime(end, "%Y-%m-%d")
                 - timedelta(days=max(1, int(back_days)))).strftime("%Y-%m-%d")
        with _pool().cursor() as c:
            c.execute(_SQL_MKT_DAY_VOL.replace("{table}", table), (start,))
            out = {str(d)[:10]: float(v or 0) for d, v in c.fetchall()}
    except Exception as e:
        logger.warning("[screen_query] market_day_volume 失败: %s", e)
        return {}
    _cache_put(key, out)
    return out


def market_day_flow_pct(since: Optional[str] = None) -> Dict[str, float]:
    """派生表 `qd_fund_flow_market` 的每日主力净占比%（main_net ÷ turnover × 100）。

    该表写入任务有断档，缺日由调用方自行补算（本层不回落其它源，避免口径混用）。
    Returns: {day: pct}；失败/无表返回 {}。
    """
    key = f"mktflow:{since or '-'}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        with _pool().cursor() as c:
            c.execute(_SQL_MKT_DAY_FLOW, (since or "1900-01-01",))
            out = {}
            for td, mn, tn in c.fetchall():
                if mn is not None and tn:
                    out[str(td)[:10]] = round(float(mn) / float(tn) * 100.0, 2)
    except Exception as e:
        logger.warning("[screen_query] market_day_flow_pct 失败: %s", e)
        return {}
    _cache_put(key, out)
    return out


def sector_fund_flow(board_type: str = "concept", day: Optional[str] = None,
                     days: int = 5, names: Optional[Iterable[str]] = None,
                     top: Optional[int] = None, sort_by: str = "main_pct",
                     source: str = "realtime_snapshot",
                     min_members: Optional[int] = None,
                     max_members: Optional[int] = None) -> Dict[str, Any]:
    """派生表 `qd_fund_flow_sector` 的行业/概念板块按日资金流（被动只读）。

    本层零物化、零定时刷新、不写库、不打远端；数据由
    `fund_flow_api.sector_fund_flow_realtime` + `upsert_sector` 按需同步。

    ★ 口径隔离：默认只取 `source='realtime_snapshot'`（本地量价方向净额按
      `stock_basic_info` 成分聚合）。表内另有 `eastmoney_or_sina` 的东财四档
      主力口径残留，两者不可换算、不可混排，用 source 参数硬隔离。
    ★ `trade_date <= day` 是硬上界，盘中调用不会读到未来交易日。
    ★ 与 `group_by_concept` 不可直接对数值：本表是**全天累计**且个股净额聚合
      排除科创/北证（`_stock_nets_from_snapshot`）；`group_by_concept` 是截至
      as_of 的**同刻**累计且不排除。只能比方向与排名。
    ★ 成分映射取 `stock_basic_info` 当前快照，历史回算有成分偏差（静态残留）。
    ★ 本表不含成员数列，故用 `_tag_map` 现算 `n_members` 并默认套用与
      `group_by_concept` 相同的成分数区间（QD_CONCEPT_MIN_MEMBERS / MAX_MEMBERS，
      默认 20~300）。不设的话 `sort_by="main_pct"` 会被成分极少的小概念主导
      （单日净占比 25%+ 但窗口累计为负是常见形态）。传 0 可关闭单侧过滤。

    Args:
        board_type: "concept"（默认）| "industry"
        day: 目标交易日；None=current_data_day()（非交易日/09:15 前退上一交易日）
        days: 回溯交易日数（取库内 <= day 的最近 N 个有数据的日）
        names: 只取这些板块名；None=全取
        top: 排名截断只数；None=不截断
        sort_by: "main_pct" | "main_net" | "net_sum" | "pct_avg"
        source: 口径过滤值
        min_members / max_members: 成分数区间；None 取概念轨 env 默认值，0=关闭该侧

    Returns:
        {board_type, day, day_used, days, days_available, caliber, rows, series, missing}
        rows 是窗口内最近一日的板块行，每行另带 net_sum（窗口累计净额）与
        pct_avg（窗口净占比均值）；series = {板块名: [按日行...]}。
    """
    bt = str(board_type or "concept").lower()
    if bt not in ("industry", "concept"):
        bt = "concept"
    from app.utils.trading_calendar import current_data_day
    d = str(day or current_data_day())[:10]
    n = max(1, int(days))
    wanted = ({str(x).strip() for x in names if str(x).strip()}
              if names else None)
    mn = _CONCEPT_MIN_MEMBERS if min_members is None else int(min_members)
    mx = _CONCEPT_MAX_MEMBERS if max_members is None else int(max_members)
    key = (f"sflow:{bt}:{d}:{n}:{_codes_key(wanted) if wanted else 'ALL'}:"
           f"{sort_by}:{top or '-'}:{source}:{mn}:{mx}")
    hit = _cache_get(key)
    if hit is not None:
        return hit

    out: Dict[str, Any] = {
        "board_type": bt, "day": d, "day_used": None, "days": n,
        "days_available": [],
        "caliber": ("qd_fund_flow_sector / source=" + str(source) +
                    "：本地量价方向净额按成分聚合，非东财四档主力；全天累计，"
                    "trade_date <= day"),
        "rows": [], "series": {}, "missing": []}
    try:
        with _pool().cursor() as c:
            c.execute(_SQL_SECTOR_FLOW, (bt, source, d, n, bt, source))
            fetched = c.fetchall()
    except Exception as e:
        logger.warning("[screen_query] sector_fund_flow 失败: %s", e)
        out["missing"].append({"code": "*", "reason": f"{type(e).__name__}: {e}"})
        return out

    series: Dict[str, List[Dict[str, Any]]] = {}
    for td, code, name, main_net, mp, inn, outn, to in fetched:
        nm = str(name or code)
        if wanted and nm not in wanted and str(code) not in wanted:
            continue
        rec = {"day": str(td)[:10], "code": code, "name": nm,
               "main_net": float(main_net or 0),
               "main_pct": float(mp) if mp is not None else None,
               "in_net": float(inn or 0), "out_net": float(outn or 0),
               "turnover": float(to or 0)}
        series.setdefault(nm, []).append(rec)
    for arr in series.values():
        arr.sort(key=lambda r: r["day"])

    all_days = sorted({r["day"] for arr in series.values() for r in arr})
    out["days_available"] = all_days
    out["day_used"] = all_days[-1] if all_days else None
    tmap = _tag_map("concepts" if bt == "concept" else "industry", None)
    member_n: Dict[str, int] = {}
    for tags in tmap.values():
        for tag in tags:
            member_n[tag] = member_n.get(tag, 0) + 1
    rows: List[Dict[str, Any]] = []
    for nm, arr in series.items():
        if not arr:
            continue
        n_mem = member_n.get(nm) or member_n.get(str(arr[-1]["code"])) or 0
        if mn and n_mem < mn:
            continue
        if mx and n_mem > mx:
            continue
        pcts = [r["main_pct"] for r in arr if r["main_pct"] is not None]
        rows.append({**arr[-1], "n_members": n_mem,
                     "net_sum": round(sum(r["main_net"] for r in arr), 2),
                     "pct_avg": (round(sum(pcts) / len(pcts), 2) if pcts else None),
                     "n_days": len(arr)})
    k = str(sort_by)
    if k not in ("main_pct", "main_net", "net_sum", "pct_avg"):
        k = "main_pct"
    rows.sort(key=lambda r: -(r.get(k) if r.get(k) is not None else -1e18))
    if top:
        rows = rows[:max(1, int(top))]
    out["rows"] = rows
    out["series"] = series
    if not rows:
        reason = (f"qd_fund_flow_sector 无 {bt}/{source} 且 trade_date<={d} 的数据"
                  if not series else
                  f"{len(series)} 个板块全被成分数区间 [{mn},{mx}] 过滤")
        out["missing"].append({"code": "*", "reason": reason})
    elif len(all_days) < n:
        out["missing"].append({"code": "*",
                               "reason": f"只取到 {len(all_days)}/{n} 个交易日，"
                                         f"窗口不足（库内该 board_type/source 可用日数有限）"})
    _cache_put(key, out)
    return out


def ticks(codes: Iterable[str], day: Optional[str] = None,
          t_from: str = "09:30", t_to: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """一批票在 day 日 `[t_from, t_to]` 的分钟快照序列（供分时形态/分钟级计算）。

    Args:
        codes: 标的集合（去重后批量 `= ANY(...)`，45 只约 60ms，与票数近似无关）
        day: 目标交易日；None=今天
        t_from: 起点 HH:MM（默认 09:30，跳过集合竞价，与既有形态判据一致）
        t_to: 终点 HH:MM；None=此刻（非交易时段落 15:00）

    Returns:
        {code: {"ticks": [(HH:MM, last, cum_volume), ...],  # 时间升序
                "prev_close": float|None, "rows": int, "last_ts": str|None}}
        无数据的票不出现（调用方按「无分时」处理）。
    """
    uni: List[str] = []
    for c in codes or []:
        s = str(c or "").split(".")[0].strip()
        if s and s not in uni:
            uni.append(s)
    if not uni:
        return {}
    d = str(day)[:10] if day else datetime.now().strftime("%Y-%m-%d")
    t_end = str(t_to)[:5] if t_to else resolve_as_of(None)
    key = f"ticks:{_codes_key(uni)}:{d}:{str(t_from)[:5]}-{t_end}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    out: Dict[str, Dict[str, Any]] = {}
    try:
        with _pool().cursor() as c:
            c.execute(_SQL_TICKS, (uni, d, d, str(t_from)[:5], t_end))
            for sym, ts, last, vol, pc in c.fetchall():
                code = str(sym).split(".")[0]
                b = out.setdefault(code, {"ticks": [], "prev_close": None,
                                          "rows": 0, "last_ts": None})
                b["ticks"].append((ts.strftime("%H:%M"), float(last), float(vol)))
                b["rows"] += 1
                b["last_ts"] = str(ts)[:19]
                if b["prev_close"] is None and pc:
                    b["prev_close"] = float(pc)
    except Exception as e:
        logger.warning("[screen_query] ticks 失败: %s", e)
        return {}
    _cache_put(key, out)
    return out


def limit_touch(codes: Iterable[str], day: Optional[str] = None,
                as_of: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """截至 `as_of` 每只票的**最高成交价**与**首次达到该价的时刻**。

    用途：重建「截至某时刻是否曾触板」的集合，替代 `limit_up_pool` 历史日期的
    收盘名单（那个名单只含最终涨停的票，用在盘中决策上是幸存者偏差）。

    本层只出价格事实，不判涨停 —— 涨跌停幅度是规则，由调用方按标的算阈值。

    Args:
        codes: 标的集合（去重后一次 `= ANY(...)`）
        day: 目标交易日；None=今天
        as_of: 信息边界 HH:MM；None=全天（15:00）

    Returns:
        {code: {"hi": 截至 as_of 的最高 last, "hi_time": "HH:MM"（首次达到该价），
                "last": as_of 最近一拍, "prev_close": 前收, "name": 简称}}
        无数据的票不出现。
    """
    uni: List[str] = []
    for c in codes or []:
        s = str(c or "").split(".")[0].strip()
        if s and s not in uni:
            uni.append(s)
    if not uni:
        return {}
    d = str(day)[:10] if day else datetime.now().strftime("%Y-%m-%d")
    t_end = str(as_of)[:5] if as_of else "15:00"
    key = f"ltouch:{_codes_key(uni)}:{d}:{t_end}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    out: Dict[str, Dict[str, Any]] = {}
    try:
        with _pool().cursor() as c:
            c.execute(_SQL_LIMIT_TOUCH, (d, t_end, uni))
            for sym, name, pc, last, hi, hi_time in c.fetchall():
                out[str(sym).split(".")[0]] = {
                    "hi": float(hi) if hi is not None else None,
                    "hi_time": hi_time.strftime("%H:%M") if hi_time else None,
                    "last": float(last) if last is not None else None,
                    "prev_close": float(pc) if pc else None,
                    "name": name or "",
                }
    except Exception as e:
        logger.warning("[screen_query] limit_touch 失败: %s", e)
        return {}
    _cache_put(key, out)
    return out


def eod_start_raw(day: str, which: str = "both") -> Dict[str, Any]:
    """尾盘启动扫描的**原始事实行**：snapshot 路 + 1m 路 + 1m 前收，三条一次给全。

    只做取数，不做复权、不做双路选择 —— 那两件事属业务判定，留在调用方。
    1m 路的前收取「`day` 之前最后一根 1m bar」所在日的收盘，与既有实现同为
    「上一交易日 1m 末拍」，不与 kline_1D 混用（1D 前收有残写不一致的实证）。

    which 控制取哪条路（保留调用方的懒加载语义，避免当日路径白跑 1m 全量）：
      "snap" | "1m" | "both"（默认）。不需要的一路返回空 dict，不报错。

    Returns:
        {"snap": {code: {name,last,day_high,prev_close,v_am,v_pm,pm_flow,last_t}},
         "m1":   {code: {last,day_high,v_am,v_pm,pm_flow,last_t}},
         "prev": {code: (prev_close, prev_t)},
         "missing": [...]}
    """
    d = str(day)[:10]
    which = str(which or "both").lower()
    if which not in ("snap", "1m", "both"):
        which = "both"
    key = f"eodraw:{d}:{which}"
    hit = _cache_get(key)
    if hit is not None:
        return hit

    def _rows(sql: str, args: tuple) -> List[Dict[str, Any]]:
        with _pool().cursor() as c:
            c.execute(sql, args)
            cols = [x[0] for x in c.description]
            return [dict(zip(cols, r)) for r in c.fetchall()]

    snap: Dict[str, Dict[str, Any]] = {}
    m1: Dict[str, Dict[str, Any]] = {}
    prev: Dict[str, tuple] = {}
    missing: List[Dict[str, str]] = []
    if which in ("snap", "both"):
        try:
            for r in _rows(_SQL_EOD_SNAP, (d + " 00:00:00", d + " 00:00:00")):
                code = str(r.get("symbol") or "")
                if code:
                    snap[code] = r
        except Exception as e:
            missing.append({"code": "*", "reason": f"snapshot 路: {type(e).__name__}: {e}"})
    if which in ("1m", "both"):
        try:
            table = _year_table("kline_1m", d)
            for r in _rows(_SQL_EOD_1M.replace("{table}", table),
                           (d + " 00:00:00", d + " 00:00:00")):
                code = str(r.get("symbol") or "")
                if code:
                    m1[code] = r
            if m1:
                pt = _rows(f'SELECT max("time") AS t FROM "{table}" WHERE "time" < %s::timestamp',
                           (d + " 00:00:00",))
                prev_t = pt[0].get("t") if pt else None
                if prev_t:
                    for r in _rows(_SQL_EOD_PREV.replace("{table}", table),
                                   (prev_t - timedelta(days=1), prev_t)):
                        prev[str(r.get("symbol") or "")] = (r.get("prev_close"), r.get("prev_t"))
        except Exception as e:
            missing.append({"code": "*", "reason": f"1m 路: {type(e).__name__}: {e}"})

    out = {"day": d, "snap": snap, "m1": m1, "prev": prev, "missing": missing}
    _cache_put(key, out)
    return out
