# -*- coding: utf-8 -*-
"""app/watchlist/render.py — **唯一渲染层**（方案 §9.1）

后端只有这一处拼装展示结构（4 段）；前端只需一个通用渲染器，支持 3 种 type：
`levels` / `score` / `units(fields|table)`。

- 一律请求时渲染 ⇒ **展示口径不进 schema**（不落配色/行序/HTML）
- 每段都带 `asof` / `grade` / `source` / `age_days`（来源、接管、年龄必须可见）
- **空段不产出**：某段没内容就整段不出现在 `sections` 里（原「空白也返回 4 段空壳」的约定在
  09-23「简单明了」瘦身时废弃 —— 前端「无」占位是纯噪音，见 §10.1）
- **单元瘦身**（`_tidy_units`）：空字段行、**整列皆空**的列、瘦身后为空的单元一律不产出。
  列清单是**契约**（不因当日缺数据而变），裁列是**显示** ⇒ 显示决策归本层，前端保持傻瓜渲染器
- **过程明细不上屏**（`_extras_units`）：system 的 extras（技术指标 / 评分明细 / 评分口径）是
  **评分过程的可解释性**，面向审计与 §7.8 回归，不是看盘信息 ⇒ 整段不进弹层。数据仍完整留库

纪律：本文件**不 import** `app.agent.*` / `app.market_cn.auto.*`。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.utils import trading_calendar as tcal
from app.watchlist import model


def to_dt(v: Any) -> Optional[datetime]:
    """容错解析时间：`datetime` 原样返回，字符串走 `fromisoformat`（store 边界已转 ISO）。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except Exception:
        return None


def trading_age_days(updated_at: Any, ref: Optional[str] = None) -> Optional[int]:
    """标签年龄 = `updated_at` 之后经过的**交易日数**（当日 = 0）。

    按交易日而非日历日：否则长假一回来所有标签的"年龄"集体虚高。
    """
    dt = to_dt(updated_at)
    if dt is None:
        return None
    d0 = dt.date()
    ref = ref or datetime.now().strftime("%Y-%m-%d")
    if d0.strftime("%Y-%m-%d") >= ref:
        return 0
    nxt = (d0 + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        return max(0, tcal.trading_days_count(nxt, ref))
    except Exception:
        return None


def _meta(row: Dict[str, Any], quote: Optional[Dict[str, Any]], *, taken_over: bool,
          stale: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    facts_asof = row.get("facts_asof")
    return {
        "grade": row.get("grade"),
        "source": row.get("source"),
        "taken_over": taken_over,
        "stale": stale or {},
        "age_days": trading_age_days(row.get("updated_at")),
        "updated_at": row.get("updated_at"),
        "trade_date": row.get("trade_date"),
        "score_version": row.get("score_version"),
        "expires_at": row.get("expires_at"),
        "asof": {
            "facts": facts_asof,
            "quote": (quote or {}).get("quote_asof"),
        },
        "price": (quote or {}).get("price"),
        "change_pct": (quote or {}).get("change_pct"),
    }


def section_empty(key: str, row: Optional[Dict[str, Any]]) -> bool:
    """该行的某一段是否"没答"（未答 ⇒ 允许由次高等级回填，而不是把别人的答案抹掉）。"""
    if not row:
        return True
    if key in ("supports", "resistances"):
        return not (row.get(key) or [])
    if key == "score":
        return row.get("score") is None
    if key == "extras":
        return not (row.get("extras") or [])
    return True


#: 视为"没答"的文本占位（上级 payload 有时用它们填位）—— 一律不渲染
_PLACEHOLDER_TEXT = {"", "无", "-", "—", "none", "null", "nan"}


def _is_blank_value(v: Any) -> bool:
    """值是否为空/占位。`0` 与 `False` 是**有效值**，不算空。"""
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip().lower() in _PLACEHOLDER_TEXT
    return False


def _tidy_units(units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """扩展段瘦身（§10.1「简单明了」）。

    - `fields`：丢掉值为空的**行**（如上级填的"预判 无"）
    - `table` ：丢掉**整列皆空**的列，并把行重映射到保留下来的列
    - 瘦身后为空的单元整块不产出

    ⚠️ 这是**显示**决策，不是数据决策：原始 payload 一字不改地留在库里，
    这里只决定"这一屏要不要出现"。列清单（`LABEL_TABLE_COLUMNS` 等）仍是不变的契约。
    """
    out: List[Dict[str, Any]] = []
    for u in units or []:
        if not isinstance(u, dict):
            continue
        u_type = u.get("type")
        if u_type == "fields":
            rows = [r for r in (u.get("rows") or []) if not _is_blank_value(r.get("value"))]
            if rows:
                out.append({**u, "rows": rows})
            continue
        if u_type == "table":
            rows = list(u.get("rows") or [])
            cols = list(u.get("columns") or [])
            keep = [c for c in cols
                    if any(not _is_blank_value(r.get(c.get("key"))) for r in rows)]
            if not keep:
                continue
            out.append({**u, "columns": keep,
                        "rows": [{c.get("key"): r.get(c.get("key")) for c in keep}
                                 for r in rows]})
            continue
        # 未知单元类型：原样透传（前端通用渲染器自己决定）
        out.append(u)
    return out


def _section_has_content(sec: Dict[str, Any]) -> bool:
    """段内是否真有东西可看（没有就整段不出现在出口里）。"""
    t = sec.get("type")
    if t == "levels":
        return bool(sec.get("items"))
    if t == "score":
        return sec.get("value") is not None
    if t == "units":
        return bool(sec.get("units"))
    return True


#: 哪些**来源**的 extras 属于「过程明细」⇒ 整段不进弹层（§10.3）。
#:
#: system 的 **技术指标 / 评分明细 / 评分口径** 就是评分过程本身，回答的是"分数怎么来的"。
#: 但 **「次日预测」** 是行动信息（P涨/操作提示），不是过程 ⇒ **白名单放行**。
#:
#: ⚠️ 纯**显示**决策：数据一字不差地留在库里。
_EXTRAS_HIDDEN_SOURCES = frozenset({"system"})

#: system 来源下仍上屏的 extras 单元标题（行动信息，非过程明细）
_EXTRAS_VISIBLE_TITLES = frozenset({"次日预测"})


def _extras_units(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """extras 段的展示口径：过程明细来源整段不产出（行动信息白名单放行），其余照 `_tidy_units` 瘦身。"""
    units = list(row.get("extras") or [])
    if (row.get("source") or "") in _EXTRAS_HIDDEN_SOURCES:
        units = [u for u in units
                 if isinstance(u, dict) and u.get("title") in _EXTRAS_VISIBLE_TITLES]
    return _tidy_units(units)


def render(row: Optional[Dict[str, Any]], quote: Optional[Dict[str, Any]] = None, *,
           parts: Optional[Dict[str, Dict[str, Any]]] = None,
           taken_over: bool = False, stale: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把「一行 label 事实 + 实时快照」渲染成 4 段统一出口。

    `row is None` ⇒ 空白（等级 0，4 段空壳）。

    ⚠️ `parts` = **段级回填**（每段自己的来源行）。默认（不传）为严格整行口径（方案 §3.2）。
    传入时：某段在 `row` 里为空（"上级没答这一段"）而更次等级答了，则该段用**答了的那一行**，
    并**带自己的 `grade`/`source`**（来源标注必须诚实）。这样"等级择高"仍是答案的选择规则，
    但**不会因为上级没覆盖某段就把已有的答案抹掉**。
    """
    parts = parts or {}
    if not row:
        base = _meta({}, quote, taken_over=False, stale=stale)
        base["grade"] = model.GRADE_BLANK
        return {
            "sections": [],                    # 空白 ⇒ 没有可看的段（不再产出 4 段空壳）
            "meta": base,
            "blank": True,
        }

    price = (quote or {}).get("price")

    from app.watchlist.overlay import overlay_levels

    def _row_of(key: str) -> Dict[str, Any]:
        return parts.get(key) or row

    def _levels_of(key: str, is_support: bool) -> List[Dict[str, Any]]:
        items = list(_row_of(key).get(key) or [])
        if price:                              # 薄覆盖：只重算距离，位置本身不动
            items = overlay_levels(items, price, is_support=is_support)
        return items

    score_row = _row_of("score")
    extras_row = _row_of("extras")

    sections: List[Dict[str, Any]] = [
        {"key": "supports", "title": "支撑位", "type": "levels",
         "items": _levels_of("supports", True),
         **_meta(_row_of("supports"), quote, taken_over=taken_over, stale=stale)},
        {"key": "resistances", "title": "压力位", "type": "levels",
         "items": _levels_of("resistances", False),
         **_meta(_row_of("resistances"), quote, taken_over=taken_over, stale=stale)},
        {"key": "score", "title": "评分", "type": "score",
         "value": score_row.get("score"), "scale": [0, 100],
         "score_version": score_row.get("score_version"),
         **_meta(score_row, quote, taken_over=taken_over, stale=stale)},
        {"key": "extras", "title": "扩展", "type": "units",
         "units": _extras_units(extras_row),
         **_meta(extras_row, quote, taken_over=taken_over, stale=stale)},
    ]
    return {"sections": [s for s in sections if _section_has_content(s)],
            "meta": _meta(row, quote, taken_over=taken_over, stale=stale),
            "blank": False}


def render_one(row: Dict[str, Any], relation: Dict[str, Any],
               quote: Optional[Dict[str, Any]] = None, *,
               parts: Optional[Dict[str, Dict[str, Any]]] = None,
               taken_over: bool = False, stale: Optional[Dict[str, Any]] = None
               ) -> Dict[str, Any]:
    """读路径单行输出 = 关系字段 + 渲染结果（方案 §9.2 的返回形状）。"""
    r = render(row, quote, parts=parts, taken_over=taken_over, stale=stale)
    meta = r["meta"]
    return {
        "id": relation.get("id"),
        "market": relation.get("market"),
        "symbol": relation.get("symbol"),
        "name": relation.get("name"),
        "group_name": relation.get("group_name"),
        "sort_order": relation.get("sort_order"),
        "grade": meta.get("grade"),
        "source": meta.get("source"),
        "taken_over": meta.get("taken_over"),
        "stale": meta.get("stale"),
        "age_days": meta.get("age_days"),
        "trade_date": meta.get("trade_date"),
        "expires_at": meta.get("expires_at"),
        "price": meta.get("price"),
        "change_pct": meta.get("change_pct"),
        "blank": r["blank"],
        "sections": r["sections"],
        "asof": meta.get("asof"),
    }
