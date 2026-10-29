# -*- coding: utf-8 -*-
"""app/watchlist/entries.py — `qd_watchlist`（自选**列表**）的写入出口（agent 工具层使用）。

现状（2026-10-29 用户裁定回滚后）：HTTP 路由 `routes/market.py` 的 `/watchlist/add|remove`
**保留原实现、不调用本文件**。两套写者并存是已知状态，不是遗漏。

本文件存在的理由：agent 工具层没有 Flask 请求上下文（`g.user_id` / `request` 均不可用，
裸调路由报 `Working outside of request context`），需要一个与 HTTP 无关的写入口。

为什么单独成文件、不并进 store.py：
  store.py 的模块 docstring 已声明它是 `qd_watchlist_label` 的唯一读写出口，且有一条
  常驻 grep 闸门「字符串 qd_watchlist_label 只允许出现在本文件」。把 `qd_watchlist` 的
  写 SQL 塞进同一文件，会让两张表的"唯一出口"声明互相污染。读路径**不重复实现**——
  `list_entries` 直接转调 `store.list_user_rows`（既有实现），本文件只补"写"这一半。

职责边界：
  - 只写 `qd_watchlist`；label 的补算由调用方入库后经 `enqueue_ensure` 触发，不在本文件
  - 不做 HTTP 语义：不读 `g.user_id`、不返回 jsonify，`user_id` 由调用方给
  - 不 import `app.agent.*` / `app.market_cn.auto.*`（沿用 api.py 禁令 1，防反向依赖；
    qd_smoke 的 R2 红线也查这条）
  - **不是全仓唯一写者**（2026-10-29 现状）：另有三类写点并存，收口需单独评估——
    ① `market_cn/auto/store.sync_watchlist_group`（auto 活跃信号的单向投影，独立域，不动）；
    ② `routes/market.py` 的 add / remove / rename-group / remove-group（HTTP 侧全套原地保留）；
    ③ `routes/xuangu.py:324`、`services/indicator_review.py:701` 的直插——两者 INSERT 的
    列名（industry/concept/new_price/change_rate）与现表结构不符，疑似历史遗留死代码，
    未在本轮改动
  - 与 ② 的**已知行为差异**（刻意保留，勿“对齐”掉）：本文件的 `resolve_cn_target` 在
    `get_stock()` 按名称命中后会以库内 symbol 为准，② 沿用入参 ⇒ 传中文名时 ② 会把中文
    写进 symbol 列（实测产生过脏行 id=658）；本文件的 `remove_entry` 仅在有删到行时清孤儿
    标签，② 无条件全表扫

易错点：
  - ON CONFLICT 必须与既有唯一索引
    `qd_watchlist_user_id_market_symbol_group_name_key (user_id, market, symbol, group_name)`
    列序一致，否则 PG 报 "no unique or exclusion constraint matching the ON CONFLICT"
  - 移出自选后必须 `store.delete_orphan_labels()`：标签表无外键，孤儿行只能主动清；
    且它按"全表仍存在的 symbol"判存活 ⇒ **先删再清**，顺序不可反
  - 本模块**不擅自填默认分组**：`group_name` 由调用方决定（HTTP 路由给「默认自选」，
    agent 给 `AGENT_GROUP`），避免两套写者互相踩组
  - `EntryError` 是**可预期失败**（代码非法 / 名称有歧义），不是异常兜底：调用方必须
    显式捕获并转成用户可读提示，不要吞成 500
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.utils.db import get_db_connection
from app.utils.logger import get_logger
from app.watchlist import store

logger = get_logger(__name__)

#: agent 独占写入的分组：与「自动策略组」（引擎按活跃信号单向投影）分开，互不覆盖
AGENT_GROUP = "AGENT"

#: 前端默认分组名（与 routes/market.py 的 DEFAULT_GROUP_NAME 同值；两边独立，改一处需同步另一处）
DEFAULT_GROUP = "默认自选"

#: 与 routes/market.py:_valid_group_name 的 50 字符上限同源
_GROUP_MAXLEN = 50


class EntryError(ValueError):
    """自选写入的可预期失败。`code` 供调用方映射成不同的用户提示。

    code 取值：
      - ``invalid``   参数/代码非法（空值、纯数字但库内无此股）
      - ``ambiguous`` 模糊匹配到多只，需要调用方让用户挑（`candidates` 带候选）
      - ``not_found`` 模糊匹配零命中
    """

    def __init__(self, message: str, *, code: str = "invalid",
                 candidates: Optional[Sequence[Dict[str, Any]]] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.candidates = list(candidates or [])


def normalize_symbol(symbol: str) -> str:
    """代码规范化：去空白 + 转大写（与 routes/market.py:_normalize_symbol 同义）。"""
    return (symbol or "").strip().upper()


def validate_group(name: Optional[str]) -> str:
    """分组名校验（非空 + 不超长）。非法抛 `EntryError`。"""
    norm = (name or "").strip()
    if not norm:
        raise EntryError("分组名称不能为空")
    if len(norm) > _GROUP_MAXLEN:
        raise EntryError(f"分组名称不能超过{_GROUP_MAXLEN}个字符")
    return norm


def search_cn(keyword: str, limit: int = 20) -> List[Dict[str, Any]]:
    """A股模糊搜索薄壳（逻辑在 basicinfo_db.StockBasicDB.search_stocks）。"""
    from app.utils.basicinfo_db import get_stock_basic_db

    db = get_stock_basic_db()
    raw = db.search_stocks(keyword, limit) or []
    return [{
        "market": "CNStock",
        "symbol": s["symbol"],
        "name": s["name"],
        "market_cn": s.get("market_cn", ""),
    } for s in raw][:limit]


def resolve_cn_target(symbol: str, name: str = "") -> Tuple[str, str]:
    """CNStock 标的校验 + 名称补全 ⇒ ``(代码, 名称)``。

    规则以回滚前的 HTTP 路由为基准，逐条同义；唯一有意加固的一处在第一条：
      - basicinfo_db 命中 ⇒ 用库内名称（本地、快）；**代码以库内 symbol 为准**——
        `get_stock` 支持按名称精确匹配（"贵州茅台"），若沿用入参会把中文名写进
        symbol 列（2026-10-29 实测脏行实证，原路由同样有此缺陷）
      - 非纯数字且库内未命中 ⇒ 模糊搜索：唯一命中即采用；多候选抛 ambiguous；
        零命中抛 not_found
      - 纯数字但库内没有 ⇒ 抛 invalid（拒绝不存在的代码）
    """
    from app.utils.basicinfo_db import get_stock_basic_db

    code = normalize_symbol(symbol)
    name_in = (name or "").strip()
    stock = get_stock_basic_db().get_stock(code)
    if stock:
        real_code = normalize_symbol(stock.get("symbol") or "")
        return (real_code or code), name_in or stock["name"]
    if not code.isdigit():
        matches = search_cn(code, limit=5)
        if len(matches) == 1:
            return matches[0]["symbol"], name_in or matches[0]["name"]
        if len(matches) > 1:
            raise EntryError(f"匹配到 {len(matches)} 个，请用准确代码", code="ambiguous",
                             candidates=matches)
        raise EntryError(f"未找到股票: {code}", code="not_found")
    raise EntryError(f"无效股票代码: {code}")


def resolve_name(market: str, symbol: str, name: str = "") -> str:
    """非 CNStock 的名称解析：显式名 → 公开源 → 种子表 → 代码本身。"""
    name_in = (name or "").strip()
    if name_in:
        return name_in
    from app.data.market_symbols_seed import get_symbol_name as seed_get_symbol_name
    from app.services.symbol_name import resolve_symbol_name

    return (resolve_symbol_name(market, symbol)
            or seed_get_symbol_name(market, symbol)
            or symbol)


def upsert_entry(*, market: str, symbol: str, name: str = "",
                 group_name: Optional[str] = None,
                 user_id: int = 1) -> Dict[str, Any]:
    """新增/更新一条自选（同 `(user_id, market, symbol, group_name)` 幂等）。

    Returns:
        {"id", "user_id", "market", "symbol", "name", "group_name"}；
        CNStock 传名称/拼音时会解析成准确代码后再入库。

    Raises:
        EntryError: 参数非法、代码不存在、模糊匹配多候选/零命中。
    """
    mkt = (market or "").strip()
    code = normalize_symbol(symbol)
    if not mkt or not code:
        raise EntryError("market 与 symbol 不能为空")
    group = validate_group(group_name or DEFAULT_GROUP)

    if mkt == "CNStock":
        code, name_final = resolve_cn_target(code, name)
    else:
        name_final = resolve_name(mkt, code, name)

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO qd_watchlist
                (user_id, market, symbol, name, group_name, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT(user_id, market, symbol, group_name) DO UPDATE SET
                name = excluded.name,
                updated_at = NOW()
            RETURNING id
            """,
            (user_id, mkt, code, name_final, group),
        )
        row = cur.fetchone()
        db.commit()
        cur.close()

    return {
        "id": (row or {}).get("id") if isinstance(row, dict) else None,
        "user_id": user_id, "market": mkt, "symbol": code,
        "name": name_final, "group_name": group,
    }


def remove_entry(*, symbol: str, market: str = "", group_name: str = "",
                 user_id: int = 1) -> Dict[str, Any]:
    """移出自选；随后清孤儿标签。

    Args:
        market: 空=不限市场（兼容旧调用）；建议显式传，防跨市场同名误删
        group_name: 空=删除该标的所有分组；显式传=只删该分组（agent 只删自己的组）

    Returns:
        {"deleted": 删除行数, "labels_cleaned": 顺带清掉的孤儿标签数, ...}
    """
    code = normalize_symbol(symbol)
    if not code:
        raise EntryError("symbol 不能为空")

    where = ["user_id = %s", "symbol = %s"]
    params: List[Any] = [user_id, code]
    if (market or "").strip():
        where.append("market = %s")
        params.append(market.strip())
    if (group_name or "").strip():
        where.append("group_name = %s")
        params.append(validate_group(group_name))

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(f"DELETE FROM qd_watchlist WHERE {' AND '.join(where)}", tuple(params))
        deleted = cur.rowcount or 0
        db.commit()
        cur.close()

    cleaned = store.delete_orphan_labels() if deleted else 0
    if cleaned:
        logger.info("[entries] 移出自选 %s 后清理孤儿标签 %d 条", code, cleaned)
    return {"deleted": deleted, "labels_cleaned": cleaned,
            "user_id": user_id, "symbol": code}


def list_entries(user_id: int = 1,
                 group_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """某用户（可选某分组）的自选行 —— 转调 store 既有实现，不重复写 SQL。"""
    return store.list_user_rows(user_id, group_name)
