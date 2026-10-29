# -*- coding: utf-8 -*-
"""Watchlist tools — 用户自选股的读取与增删（写入域固定为 AGENT 分组）。

增删读全部转调 `app.watchlist.entries`，工具层不重复实现校验与 SQL。

注意（2026-10-29 用户裁定）：`routes/market.py` 的 `/watchlist/add|remove` 已回滚为
原实现，**不再**与这里共用出口。两套实现并存是已知状态，差异见
`app/watchlist/entries.py` 模块 docstring 的「同类写点」小节。
"""
from __future__ import annotations

from typing import Any, Dict, List

from ...log import logger


def _entries():
    from app.watchlist import entries
    return entries


def _clean_group(group_name: str):
    g = (group_name or "").strip()
    return g or None


def _merge_labels(rows: List[Dict[str, Any]], group_name, user_id: int) -> List[Dict[str, Any]]:
    from app.watchlist import api as wl_api

    labels = wl_api.get_labels(user_id, group_name=group_name, with_overlay=False) or []
    by_key = {(x.get("market"), x.get("symbol")): x for x in labels}
    out: List[Dict[str, Any]] = []
    for r in rows:
        lab = by_key.get((r.get("market"), r.get("symbol"))) or {}
        merged = {**r, "label": lab or None}
        out.append(merged)
    return out


def _split_symbols(raw: str) -> List[str]:
    """把 "600519,000001" 拆成去重后的代码列表（英文/中文逗号均可，顺序保留）。"""
    entries = _entries()
    out: List[str] = []
    for chunk in (raw or "").replace("，", ",").split(","):
        code = entries.normalize_symbol(chunk)
        if code and code not in out:
            out.append(code)
    return out


def get_watchlist(group_name: str = "", with_labels: bool = False,
                  user_id: int = 1) -> Dict[str, Any]:
    """读取用户自选股列表（可按分组过滤，可附带系统标签）。

    Args:
        group_name: 只看某个分组（"默认自选" / "自动策略组" / "AGENT" 等）；空 = 全部
        with_labels: True 时附带每只的系统标签（等级/来源/评分与支撑压力 4 段/有效期）。
                     标签来自本地标签表，不吃实时行情；需要现价请另调行情工具
        user_id: 用户 ID。当前 agent 为单用户部署，默认 1（与其它工具同口径）

    Returns:
        {"count": N, "groups": [分组名...], "items": [{
            market, symbol, name, group_name, id, sort_order,
            label: {grade, source, price, change_pct, expires_at, sections, blank, ...} | null
        }]}；失败 → {"error": ...}
    """
    try:
        entries = _entries()
        group = _clean_group(group_name)
        rows = entries.list_entries(user_id, group)
        groups = sorted({r.get("group_name") for r in rows if r.get("group_name")})
        items = _merge_labels(rows, group, user_id) if with_labels and rows else rows
        return {"count": len(items), "groups": groups, "items": items}
    except Exception as e:
        logger.warning("get_watchlist failed: %s", e)
        return {"error": str(e)}


def add_watchlist(symbol: str, name: str = "", group_name: str = "AGENT",
                  market: str = "CNStock", user_id: int = 1) -> Dict[str, Any]:
    """把一只或多只股票加入用户自选股（默认写进 agent 独占的 "AGENT" 分组）。

    Args:
        symbol: 单只，或用逗号分隔的多只，如 "600519,000001,300750"（中文逗号同样识别）；
                重复代码自动去重并保持顺序。CNStock 支持名称/拼音模糊解析
        name: 名称，可省。**仅单只时生效**；多只时逐只从本地股票库补全
        group_name: 目标分组，默认 "AGENT"（agent 专用，与用户手工分组物理隔离）
        market: CNStock / USStock / Crypto / Forex / Futures，默认 CNStock；多只时共用
        user_id: 用户 ID（单用户部署，默认 1）

    Returns:
        {"added": 成功数, "failed": 失败数, "results": [
            {"ok": true, "id", "user_id", "market", "symbol", "name", "group_name"} |
            {"ok": false, "symbol", "error", "code", "candidates"}
        ]}
        逐只独立处理：某只不合法/不存在只记入该只的 failed，不影响其余标的上车。
        `symbol` 归一后与入参不同时（如传名称解析成代码），results 里给实际入库代码。
    """
    ents = _entries()
    codes = _split_symbols(symbol)
    if not codes:
        return {"added": 0, "failed": 0, "results": [], "error": "symbol 不能为空"}

    multi = len(codes) > 1
    results: List[Dict[str, Any]] = []
    pending: List[tuple] = []

    for code in codes:
        try:
            row = ents.upsert_entry(market=market, symbol=code,
                                    name="" if multi else name,
                                    group_name=group_name or ents.AGENT_GROUP,
                                    user_id=user_id)
        except ents.EntryError as ee:
            results.append({"ok": False, "symbol": code, "error": ee.message,
                            "code": ee.code, "candidates": ee.candidates})
            continue
        except Exception as e:
            logger.warning("add_watchlist failed %s: %s", code, e)
            results.append({"ok": False, "symbol": code, "error": str(e)})
            continue
        results.append({"ok": True, **row})
        pending.append((row["market"], row["symbol"]))

    if pending:
        try:
            from app.watchlist import enqueue_ensure
            enqueue_ensure(pending)
        except Exception as e:
            logger.warning("add_watchlist label enqueue failed %s: %s", pending, e)

    added = sum(1 for r in results if r.get("ok"))
    return {"added": added, "failed": len(results) - added, "results": results}


def remove_watchlist(symbol: str, market: str = "CNStock",
                     user_id: int = 1) -> Dict[str, Any]:
    """把一只或多只股票从 **AGENT 分组**移出自选（不会动用户手工维护的分组）。

    Args:
        symbol: 单只准确代码，或用逗号分隔的多只，如 "600519,000001"（中文逗号同样识别）；
                重复代码自动去重。**不做模糊解析**，必须是准确代码
        market: 市场，默认 CNStock；多只时共用
        user_id: 用户 ID（单用户部署，默认 1）

    Returns:
        {"removed": 删除总行数, "labels_cleaned": 孤儿标签清理总数, "results": [
            {"ok": true, "symbol", "removed", "labels_cleaned"} |
            {"ok": false, "symbol", "error", "code"}
        ]}
        removed=0 表示该分组里本来就没有这只票（不是报错）。逐只独立处理，
        某只失败不影响其余只。
    """
    ents = _entries()
    codes = _split_symbols(symbol)
    if not codes:
        return {"removed": 0, "labels_cleaned": 0, "results": [],
                "error": "symbol 不能为空"}

    results: List[Dict[str, Any]] = []
    total_removed = 0
    total_cleaned = 0

    for code in codes:
        try:
            res = ents.remove_entry(symbol=code, market=market,
                                    group_name=ents.AGENT_GROUP, user_id=user_id)
        except ents.EntryError as ee:
            results.append({"ok": False, "symbol": code, "error": ee.message,
                            "code": ee.code})
            continue
        except Exception as e:
            logger.warning("remove_watchlist failed %s: %s", code, e)
            results.append({"ok": False, "symbol": code, "error": str(e)})
            continue
        removed = res.get("deleted", 0)
        cleaned = res.get("labels_cleaned", 0)
        total_removed += removed
        total_cleaned += cleaned
        results.append({"ok": True, "symbol": res.get("symbol"),
                        "removed": removed, "labels_cleaned": cleaned})

    return {"removed": total_removed, "labels_cleaned": total_cleaned,
            "results": results}

