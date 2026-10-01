# -*- coding: utf-8 -*-
"""追责体系复位工具（S2 数据重来 / S4 权重复位）。

背景（2026-10-01 用户裁定）：
    历史追责数据错误较多（`direction` 默认 neutral、`confidence` 默认 0.5 这类
    常量污染会**把权重训练歪**），与其回填旧数据不如**清空重来**：新链路的
    intake 闸门已经就位，重新累积的每一行都是可信样本。

    ⚠️ 因此这里提供的是**破坏性复位**，执行前必须先 dry_run 看清影响面。

设计要点：
    * **只碰追责 4 表 + 权重表**，不碰 `qd_agent_traces`（那是审计链，另回事）。
    * claims / resolutions 对 decisions 是外键 **ON DELETE CASCADE**（已核验），
      所以清 decisions 即可级联清空，但仍显式 DELETE 三者，防某天改了级联规则。
    * `reset_weights` 是**归位到工厂初值**，不是删行：
      权重行由 registry 自动同步产生，删了下次还会重建且带上旧 sample_count。
    * 全部 fail-open 到调用方判断，内部不 sys.exit。
"""

from typing import Any, Dict, List

from app.utils.db import get_db_connection

logger = __import__("logging").getLogger(__name__)

# 追责三表（顺序有意义：先子后父，即便没级联也不会留下外键冲突）
_ACCOUNT_TABLES: List[str] = ["qd_agent_resolutions", "qd_agent_claims",
                              "qd_agent_decisions"]

# 权重复位目标值 —— 与 evaluator.update_weights 新注册行的初值口径一致
_WEIGHT_INITIAL = 1.0
_SAMPLE_INITIAL = 0


def snapshot(limit_days: int = 0) -> Dict[str, Any]:
    """复位前先看清楚要毁掉什么（**幂等只读**）。

    Args:
        limit_days: 0 = 不限时间。

    Returns:
        {"tables": {...}, "total_decisions": int, "weight_rows": int, ...}
    """
    out: Dict[str, Any] = {"tables": {}, "weight_rows": 0, "weight_dirty": 0}
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            for t in _ACCOUNT_TABLES:
                if limit_days and t == "qd_agent_decisions":
                    cur.execute("SELECT count(*) AS n FROM qd_agent_decisions"
                                " WHERE created_at > NOW() - (%s || ' days')::interval",
                                (int(limit_days),))
                else:
                    cur.execute(f"SELECT count(*) AS n FROM {t}")
                out["tables"][t] = int((cur.fetchone() or {}).get("n") or 0)
            # 权重的“脏”= 被真实样本写过（区别于从未更新过的工厂行）
            cur.execute("SELECT count(*) AS n, count(*) FILTER "
                        "(WHERE sample_count > 0 OR abs(weight - %s) > 0.0001) AS dirty"
                        " FROM qd_agent_weights", (_WEIGHT_INITIAL,))
            r = cur.fetchone() or {}
            out["weight_rows"] = int(r.get("n") or 0)
            out["weight_dirty"] = int(r.get("dirty") or 0)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        logger.warning("[Reset] 快照失败: %s", e)
    out["total_decisions"] = out["tables"].get("qd_agent_decisions", 0)
    return out


def reset_accountability(limit_days: int = 0, dry_run: bool = True) -> Dict[str, Any]:
    """清空追责三表（S2：**重来**，不是回填）。

    Args:
        limit_days: >0 只清最近 N 天；0 = 全清。
        dry_run: True 只报数不动手（默认 True，防手滑）。

    Returns:
        {"ok": bool, "dry_run": bool, "deleted": {...}, "before": {...}}
    """
    before = snapshot(limit_days)
    out: Dict[str, Any] = {"ok": False, "dry_run": bool(dry_run),
                           "deleted": {}, "before": before}
    if before.get("error"):
        out["error"] = before["error"]
        return out
    if dry_run:
        out["ok"] = True
        out["note"] = "dry_run：未做任何删除，确认后 dry_run=False"
        return out
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            for t in _ACCOUNT_TABLES:
                if limit_days and t == "qd_agent_decisions":
                    cur.execute("DELETE FROM qd_agent_decisions"
                                " WHERE created_at > NOW() - (%s || ' days')::interval",
                                (int(limit_days),))
                else:
                    cur.execute(f"DELETE FROM {t}")
                out["deleted"][t] = int(cur.rowcount or 0)
            conn.commit()
        out["ok"] = True
        logger.info("[Reset] 追责三表已清空: %s", out["deleted"])
    except Exception as e:
        logger.error("[Reset] 清空失败: %s: %s", type(e).__name__, e)
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def reset_weights(dry_run: bool = True) -> Dict[str, Any]:
    """权重复位到工厂初值（S4：**从零开始慢调**）。

    为什么是 UPDATE 不是 DELETE：权重行由 `update_weights` 依据 registry
    自动增删改，删了下次跑盘后任务会重建——但那是 INSERT 工厂值，
    语义反而绕。直接归位更直白，也保留 layer/name 维度的注册表。

    Args:
        dry_run: True 只报数。

    Returns:
        {"ok": bool, "dry_run": bool, "rows": int, ...}
    """
    out: Dict[str, Any] = {"ok": False, "dry_run": bool(dry_run), "rows": 0}
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            if dry_run:
                cur.execute("SELECT count(*) AS n, count(*) FILTER "
                            "(WHERE sample_count > 0 OR abs(weight - %s) > 0.0001) AS dirty"
                            " FROM qd_agent_weights", (_WEIGHT_INITIAL,))
                r = cur.fetchone() or {}
                out["rows"] = int(r.get("n") or 0)
                out["dirty"] = int(r.get("dirty") or 0)
                out["ok"] = True
                out["note"] = "dry_run：未更新，确认后 dry_run=False"
                return out
            cur.execute("""
                UPDATE qd_agent_weights
                   SET weight = %s,
                       sample_count = %s,
                       win_rate = NULL,
                       avg_pnl_pct = NULL,
                       avg_hold_days = NULL,
                       return_per_day = NULL,
                       decay_half_life = NULL,
                       last_updated = NOW()
            """, (_WEIGHT_INITIAL, _SAMPLE_INITIAL))
            out["rows"] = int(cur.rowcount or 0)
            conn.commit()
        out["ok"] = True
        logger.info("[Reset] 权重已复位 %d 行 → weight=%.1f sample_count=%d",
                    out["rows"], _WEIGHT_INITIAL, _SAMPLE_INITIAL)
    except Exception as e:
        logger.error("[Reset] 权重复位失败: %s: %s", type(e).__name__, e)
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def reset_all(dry_run: bool = True) -> Dict[str, Any]:
    """S2 + S4 一次性复位。**默认 dry_run**。"""
    return {
        "dry_run": bool(dry_run),
        "accountability": reset_accountability(0, dry_run=dry_run),
        "weights": reset_weights(dry_run=dry_run),
    }


__all__ = ["snapshot", "reset_accountability", "reset_weights", "reset_all"]
