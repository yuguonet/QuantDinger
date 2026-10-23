# -*- coding: utf-8 -*-
"""alert_eval.py — 告警条件求值器（`qd_agent_cron_jobs.mode='alert'` 的判定层）。

────────────────────────────────────────────────────────────
★ 为什么必须有这一层（2026-10-23）

cron 只解决 **when**（何时检查），解决不了 **what**（检查什么）。用户说
"到 1650 提醒我"，这里的 1650 是**参数**——没有求值层的话，唯一实现就是
每 5 分钟唤醒 LLM 问一句"到了吗"。成本上这是最差解：LLM 耗时 ≈ 输出 token
÷ ~120 tok/s，**与输入长度/前缀缓存命中率无关**（2026-10-20 实测：缓存命中
9.1%→43.3% 翻 4.7 倍，总耗时 176.3s→179.6s 不动）。盘中 48 拍 × 每拍一次
LLM = 纯浪费，且噪声大（LLM 每次判"没到"也要输出一串字）。

所以判定分两层，本模块是 **L1 确定性层**：

    L1 确定性条件（本模块·纯代码·零 LLM 成本）
        │ 未命中 → 直接返回，不唤醒 LLM、不推送
        │ 命中
        ▼
    L2 语义条件（可选·交给 LLM 复核，如"回踩确认"这类无法结构化的判断）

L1 能判的直接判掉，只有 L1 通过才可能付出 LLM 成本 —— 这是把"参数识别"
从 LLM 手里拿回来的关键。

★ 字段字典的边界（有意为之，不是偷懒）

第一版只开放 **realtime_snapshot 当日最后一拍**的行情列。理由：
  · 零计算成本：快照每 60s 全市场采一次，取最后一拍是单行主键查询
  · 单位可靠：volume 单位 bug（tencent 科创板 ×100）已于 2026-10-23 修复
技术指标（MA/RSI/MACD…）**暂不开放**——它们要现场算 K 线序列，单次成本
远高于取快照，且口径易漂移。等字段字典有明确需求再按同协议扩展，不要
为了"看着全面"先把成本埋进去。

★ 判定纪律：**不可判定 ⇒ 不命中**（本模块自身的设计选择，不是外推）

告警的失败模式是**静默**的：退化成"当它满足"会无意义轰炸用户，退化成
"当它不满足"则告警永不触发——两者都比不告警更糟，且都不会报错。
因此字段取不到、值为 None、条件结构非法 → 一律返回 `hit=False` + 显式
`error` 说明，绝不猜。

★ 条件结构：**单子句**（2026-10-23 重构，去掉 all/any 复合）

   {"symbol": "600519", "field": "last", "op": "<=", "value": 1650}

工具面 `create_alert_job` 只暴露 field/op/value 单条，复合结构没有任何
调用方。若未来需要多条件（AND/OR），再按同一协议扩展——不为"看着全面"
预留成本。
────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# ── 字段字典：可引用参数白名单（同时是 SQL 列名白名单，防注入）──
#   与 realtime_snapshot 表列名一致（"last"/"previousClose" 是 PG 保留字）
FIELDS: Dict[str, str] = {
    "last": "现价",
    "open": "今开",
    "high": "最高",
    "low": "最低",
    "previousClose": "昨收",
    "changePercent": "涨跌幅(%)",
    "volume": "累计成交量(股)",
}

_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _fetch_snapshot(symbol: str) -> Optional[Dict[str, Any]]:
    """取指定标的当日最后一拍快照。

    Returns:
        字段字典；无数据/查询失败一律 None（由上层转成显式 error，不猜）。
    """
    if not symbol:
        return None
    cols = ", ".join(f'"{c}"' for c in FIELDS)
    try:
        from app.utils.db_market import get_market_db_manager

        pool = get_market_db_manager()._get_pool("CNStock")
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'SELECT {cols}, time FROM "realtime_snapshot" '
                    "WHERE symbol = %s ORDER BY time DESC LIMIT 1",
                    (symbol,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        # psycopg2 游标返回 tuple —— 必须按位置映射回字段名
        out: Dict[str, Any] = {}
        for i, name in enumerate(FIELDS):
            out[name] = row[i]
        out["time"] = row[len(FIELDS)]
        return out
    except Exception:
        # 不吞：交由上层统一转成 error（此处不 log 是因为调用方会记账）
        raise


def _eval_one(field: str, op: str, raw_val: Any,
              actual: Dict[str, Any]) -> Dict[str, Any]:
    """求值单条子句。非法/缺值 → ok=False 且带 reason，绝不默认通过。"""
    field = str(field or "").strip()
    op = str(op or "").strip()

    if field not in FIELDS:
        return {"ok": False, "field": field, "op": op, "value": raw_val,
                "actual": None, "reason": f"未知字段（可用: {sorted(FIELDS)}）"}
    if op not in _OPS:
        return {"ok": False, "field": field, "op": op, "value": raw_val,
                "actual": None, "reason": f"未知运算符（可用: {sorted(_OPS)}）"}
    if raw_val is None:
        return {"ok": False, "field": field, "op": op, "value": raw_val,
                "actual": None, "reason": "缺少阈值 value"}

    cur_val = actual.get(field)
    if cur_val is None:
        return {"ok": False, "field": field, "op": op, "value": raw_val,
                "actual": None, "reason": f"快照无该字段值（{field} 为 NULL）"}
    try:
        cur_f = float(cur_val)
        thr_f = float(raw_val)
    except (TypeError, ValueError):
        return {"ok": False, "field": field, "op": op, "value": raw_val,
                "actual": cur_val, "reason": "值无法转为数值比较"}

    return {"ok": bool(_OPS[op](cur_f, thr_f)), "field": field, "op": op,
            "value": thr_f, "actual": cur_f, "reason": None}


def evaluate(condition: Dict[str, Any]) -> Dict[str, Any]:
    """对一条告警条件求值。

    Args:
        condition: 单子句结构
            {"symbol": "600519", "field": "last", "op": "<=", "value": 1650}
            · symbol：标的代码（必填）
            · field ：监测字段（alert_eval.FIELDS 白名单）
            · op    ：比较运算符（> >= < <= == !=）
            · value ：阈值（数值）
            缺任一 ⇒ 非法（无法判定，返回 error 而非 hit=True）。

    Returns:
        {"hit": bool, "symbol": str, "check": {...}, "actual": {...},
         "snapshot_time": str, "error": str|None}
        出错时 hit 恒为 False 且 error 非空——**不猜**。
    """
    if not isinstance(condition, dict):
        return {"hit": False, "symbol": "", "check": {}, "actual": {},
                "error": "condition 必须是对象"}

    symbol = str(condition.get("symbol") or "").strip()
    field = condition.get("field")
    op = condition.get("op")
    value = condition.get("value")

    if not symbol:
        return {"hit": False, "symbol": "", "check": {}, "actual": {},
                "error": "缺少 symbol"}
    if not field or not op:
        return {"hit": False, "symbol": symbol, "check": {}, "actual": {},
                "error": "缺少 field/op（condition = {symbol, field, op, value}）"}

    try:
        snap = _fetch_snapshot(symbol)
    except Exception as e:                      # noqa: BLE001 — 如实转 error
        return {"hit": False, "symbol": symbol, "check": {}, "actual": {},
                "error": f"取快照失败: {type(e).__name__}: {e}"}
    if snap is None:
        return {"hit": False, "symbol": symbol, "check": {}, "actual": {},
                "error": f"realtime_snapshot 无 {symbol} 的快照数据"}

    check = _eval_one(field, op, value, snap)
    if check.get("reason"):
        # 子句非法 ⇒ 条件不可信，判不命中（不猜）
        return {"hit": False, "symbol": symbol, "check": check,
                "actual": {k: snap.get(k) for k in FIELDS},
                "error": "条件含无法判定的子句: " + str(check.get("reason"))}

    return {
        "hit": bool(check.get("ok")),
        "symbol": symbol,
        "check": check,
        "actual": {k: snap.get(k) for k in FIELDS},
        "snapshot_time": str(snap.get("time") or ""),
        "error": None,
    }