# -*- coding: utf-8 -*-
"""app/watchlist/store.py — `qd_watchlist_label` 的**唯一读写出口**（全仓唯一 SQL 处）

常驻断言（方案 §10）：
  - 字符串 `qd_watchlist_label` **只允许出现在本文件**（grep 闸门）⇒ 唯一写通道
  - 本文件**不 import** `app.agent.*` / `app.market_cn.auto.*`

表结构由 `migrations/qd_watchlist_label.sql` 建立（建表入口唯一），本文件**不建表**。
写路径只有两条：`upsert_system`（grade=1，只由 job/compute 调用）与 `upsert_submitted`
（grade=2/3，只由 submit 调用）；`UNIQUE (market, symbol, trade_date, source)` 保证
system 的 upsert **物理上不可能**改到 auto/agent 的行（方案 §3.2 结构性落地）。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: 表名常量（本文件是唯一使用处）
TABLE = "qd_watchlist_label"

_SELECT_COLS = ("market, symbol, trade_date, source, grade, score, score_version, "
                "supports, resistances, extras, facts_asof, expires_at, created_at, updated_at")


def _as_dict(v: Any) -> Any:
    """psycopg2 对 JSONB 一般已解成 dict/list，但文本型驱动会回字符串 —— 统一兜底。"""
    if isinstance(v, (dict, list)) or v is None:
        return v
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("utf-8")
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return None


def _iso(v: Any) -> Any:
    """时间戳 → ISO 字符串。**必须在 store 边界做掉**：Flask 对 `date`/`datetime`
    默认序列化成 HTTP-date（`Wed, 23 Sep 2026 00:00:00 GMT`），前端不可用。"""
    if v is None or isinstance(v, str):
        return v
    if hasattr(v, "isoformat"):
        return v.isoformat(sep=" ") if hasattr(v, "hour") else v.isoformat()
    return v


def _row_to_dict(r: Any) -> Dict[str, Any]:
    d = dict(r)
    for k in ("supports", "resistances", "extras"):
        d[k] = _as_dict(d.get(k))
    if d.get("score") is not None:
        d["score"] = float(d["score"])          # NUMERIC → Decimal，JSON 要数字
    if d.get("trade_date") is not None:
        d["trade_date"] = _iso(d["trade_date"])[:10]
    for k in ("facts_asof", "expires_at", "created_at", "updated_at"):
        if k in d:
            d[k] = _iso(d[k])
    return d


# ═══════════════════════════════════════════════════════════════════
# 写
# ═══════════════════════════════════════════════════════════════════

#: 本表所有时间列（`facts_asof` / `expires_at` / `created_at` / `updated_at`）统一语义 =
#: **市场本地时间（Asia/Shanghai）的 naive 值**，且**只有一个时钟来源 = `_now()`**。
#: ⚠️ 禁用 SQL `NOW()`：库会话 TimeZone=UTC，`NOW()` 比应用本地时钟早 8 小时；
#: 读路径（`api._is_expired` / `render.trading_age_days`）用的是 Python 本地时钟 ⇒
#: 两钟并存会让"已失效 / 未更新天数"在本地 00:00~08:00 内出现分歧。
def _now() -> datetime:
    """本表时间列的唯一时钟（市场本地）。"""
    return datetime.now()


_UPSERT = f"""
INSERT INTO {TABLE}
    (market, symbol, trade_date, source, grade, score, score_version,
     supports, resistances, extras, facts_asof, expires_at, created_at, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
ON CONFLICT (market, symbol, trade_date, source) DO UPDATE SET
    grade         = EXCLUDED.grade,
    score         = EXCLUDED.score,
    score_version = EXCLUDED.score_version,
    supports      = EXCLUDED.supports,
    resistances   = EXCLUDED.resistances,
    extras        = EXCLUDED.extras,
    facts_asof    = EXCLUDED.facts_asof,
    expires_at    = EXCLUDED.expires_at,
    updated_at    = EXCLUDED.updated_at
"""


def _upsert(cur, *, market: str, symbol: str, trade_date: str, source: str, grade: int,
            score: Optional[float], score_version: Optional[int],
            supports: Any, resistances: Any, extras: Any,
            facts_asof: Optional[datetime], expires_at: Optional[datetime]) -> None:
    ts = _now()                      # created_at / updated_at 同一次取值 ⇒ 同行同钟
    cur.execute(_UPSERT, (
        market, symbol, trade_date, source, grade, score, score_version,
        json.dumps(supports, ensure_ascii=False),
        json.dumps(resistances, ensure_ascii=False),
        json.dumps(extras, ensure_ascii=False),
        facts_asof, expires_at, ts, ts,
    ))


def upsert_system(batch: Sequence[Dict[str, Any]]) -> int:
    """批量写 system（grade=1）事实。**只触碰 source='system' 的行**（由 UNIQUE 键保证）。

    单票失败不阻断整批（方案 §4.3）。返回成功写入行数。
    """
    if not batch:
        return 0
    ok = 0
    with get_db_connection() as db:
        cur = db.cursor()
        for it in batch:
            try:
                _upsert(
                    cur,
                    market=it["market"], symbol=it["symbol"], trade_date=it["trade_date"],
                    source="system", grade=1,
                    score=it.get("score"), score_version=it.get("score_version"),
                    supports=it.get("supports") or [], resistances=it.get("resistances") or [],
                    extras=it.get("extras") or [], facts_asof=it.get("facts_asof"),
                    expires_at=it.get("expires_at"),
                )
                ok += 1
            except Exception as e:  # 单票不阻断整批
                db.rollback()
                logger.warning("[label] system upsert 失败 %s/%s: %s",
                               it.get("market"), it.get("symbol"), e)
        db.commit()
        cur.close()
    return ok


def upsert_submitted(*, source: str, market: str, symbol: str, trade_date: str, grade: int,
                     score: Optional[float], score_version: Optional[int],
                     supports: Any, resistances: Any, extras: Any,
                     facts_asof: Optional[datetime] = None,
                     expires_at: Optional[datetime] = None) -> None:
    """上级（agent/auto）唯一写路径。`grade` 由 model.GRADE_TABLE 映射后传入，不接受外部伪造。"""
    with get_db_connection() as db:
        cur = db.cursor()
        _upsert(cur, market=market, symbol=symbol, trade_date=trade_date, source=source,
                grade=grade, score=score, score_version=score_version,
                supports=supports, resistances=resistances, extras=extras,
                facts_asof=facts_asof, expires_at=expires_at)
        db.commit()
        cur.close()


# ═══════════════════════════════════════════════════════════════════
# 读
# ═══════════════════════════════════════════════════════════════════

def fetch_candidates(pairs: Sequence[Tuple[str, str]], asof: str) -> List[Dict[str, Any]]:
    """一次取出这些票的候选行（**每票每来源只取最新一行**），含已失效行。

    与方案 §3.3 示例 SQL 的两处差异（均已声明，理由记录在案）：
      1. `DISTINCT ON (market, symbol, source)` —— 否则表按日快照会**无界增长**
         （`ttl_days=None` 的每日提交让 1 票 1 年就上千行）；取每来源最新一行后，
         行数恒为 **票数 × 来源数(≤3)**，与历史长度无关，同时保留"同级取新"语义。
      2. 失效过滤**下沉到 Python** —— 行数既然只有 票数×来源，代价可忽略，
         且**免费获得 `stale` 诊断**（哪一级已失效、失效多久）。

    外层再按 `grade DESC, created_at DESC` 排序 ⇒ Python 侧取第一个未失效的即为
    「等级择高 + 同级取新」。
    """
    if not pairs:
        return []
    markets = [p[0] for p in pairs]
    symbols = [p[1] for p in pairs]
    sql = f"""
        SELECT * FROM (
            SELECT DISTINCT ON (market, symbol, source) {_SELECT_COLS}
            FROM {TABLE}
            WHERE (market, symbol) IN (SELECT * FROM UNNEST(%s::text[], %s::text[]))
              AND trade_date <= %s
            ORDER BY market, symbol, source, trade_date DESC, created_at DESC
        ) t
        ORDER BY market, symbol, grade DESC, created_at DESC
    """
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(sql, (markets, symbols, asof))
        rows = cur.fetchall() or []
        cur.close()
    return [_row_to_dict(r) for r in rows]


def fetch_expired_by_grade(asof: str, *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """已失效（`expires_at <= now`）的行 —— 供每日 job 的接管统计（§3.3 可观测性）。

    ⚠️ 过期比较**必须用 Python 传入的同一个时钟**（默认 `datetime.now()`），不能用 SQL `NOW()`：
    本表的时间列是 `timestamp without time zone`，而库会话 TimeZone=UTC，
    于是 `NOW()` 比应用本地时钟**早 8 小时**；读路径（`api._is_expired`）用的是本地 `datetime.now()`。
    两钟并存会让"已失效"在本地 00:00~08:00 内出现分歧 ⇒ 统一为**单一事实源 = Python 时钟**。
    """
    sql = f"""
        SELECT market, symbol, source, grade, trade_date, expires_at, updated_at
        FROM {TABLE}
        WHERE expires_at IS NOT NULL AND expires_at <= %s
          AND trade_date <= %s
        ORDER BY expires_at DESC
    """
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(sql, (now or _now(), asof))
        rows = cur.fetchall() or []
        cur.close()
    return [dict(r) for r in rows]


def fetch_higher_grade_latest(*, now: Optional[datetime] = None) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """每票当前**最高等级的未失效**行（供接管统计与年龄展示）。

    ⚠️ 同 `fetch_expired_by_grade`：过期比较走 Python 时钟，不用 SQL `NOW()`（否则差 8 小时）。
    """
    sql = f"""
        SELECT DISTINCT ON (market, symbol)
               market, symbol, source, grade, trade_date, expires_at, updated_at
        FROM {TABLE}
        WHERE source <> 'system' AND (expires_at IS NULL OR expires_at > %s)
        ORDER BY market, symbol, grade DESC, created_at DESC
    """
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(sql, (now or _now(),))
        rows = cur.fetchall() or []
        cur.close()
    return {(r["market"], r["symbol"]): dict(r) for r in rows}


# ═══════════════════════════════════════════════════════════════════
# 自选股范围（label 只通过 (market, symbol) 与 qd_watchlist 关联）
# ═══════════════════════════════════════════════════════════════════

def list_watchlist_symbols() -> List[Tuple[str, str]]:
    """自选股并集 `DISTINCT (market, symbol)`（所有用户，方案 §4.3）。**非全市场**。"""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute("SELECT DISTINCT market, symbol FROM qd_watchlist ORDER BY market, symbol")
        rows = cur.fetchall() or []
        cur.close()
    return [(r["market"], r["symbol"]) for r in rows]


def list_user_rows(user_id: int, group_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """关系：某用户（可选某分组）的自选行 —— 供读路径的第 1 步。"""
    sql = ("SELECT id, market, symbol, name, group_name, sort_order FROM qd_watchlist "
           "WHERE user_id = %s")
    params: List[Any] = [user_id]
    if group_name:
        sql += " AND group_name = %s"
        params.append(group_name)
    sql += " ORDER BY COALESCE(sort_order, 1000000) ASC, id DESC"
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() or []
        cur.close()
    return [dict(r) for r in rows]
