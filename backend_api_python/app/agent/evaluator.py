# -*- coding: utf-8 -*-
"""
Evaluator — 盘后回溯验证 + 权重迭代。

纯 SQL + 数学，0 token 消耗。
替代旧 chain/evaluator.py，适配 nanobot。
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# timeframe → 持有天数映射
_TIMEFRAME_DAYS = {"T+1": 1, "T+3": 3, "T+5": 5, "1W": 5, "1M": 22, "3M": 66, "1Y": 252}


def _get_db():
    from app.utils.db import get_db_connection
    return get_db_connection()


# ═══════════════════════════════════════════════════════════════
# 1. 盘后验证：查找待验证记录，获取实际行情，写回盈亏
# ═══════════════════════════════════════════════════════════════

def evaluate_pending() -> Dict[str, int]:
    """盘后自动运行。查找 exit_date IS NULL 的 chain 层记录，验证并写回。

    Returns:
        {"checked": N, "verified": M, "errors": K}
    """
    stats = {"checked": 0, "verified": 0, "errors": 0}

    with _get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, stock_code, direction, timeframe, exec_date, score, action
            FROM qd_traces
            WHERE layer = 'chain' AND exit_date IS NULL
            ORDER BY id LIMIT 500
        """)
        rows = cur.fetchall()

        for row in rows:
            stats["checked"] += 1
            try:
                result = _verify_one(row)
                if result:
                    cur.execute("""
                        UPDATE qd_traces
                        SET exit_date = %s, pnl_pct = %s, hold_days = %s,
                            correct = %s, exit_reason = %s
                        WHERE id = %s
                    """, (
                        result["exit_date"], result["pnl_pct"],
                        result["hold_days"], result["correct"],
                        result["exit_reason"], row["id"],
                    ))
                    stats["verified"] += 1
            except Exception as e:
                logger.warning("[Evaluator] verify id=%s failed: %s", row["id"], e)
                stats["errors"] += 1

        conn.commit()
        cur.close()

    # 更新权重
    if stats["verified"] > 0:
        try:
            update_skill_weights()
            update_factor_weights()
        except Exception as e:
            logger.error("[Evaluator] weight update failed: %s", e)

    return stats


def _verify_one(row) -> Optional[Dict]:
    """验证单条记录。"""
    stock_code = row.get("stock_code")
    direction = row.get("direction", "")
    timeframe = row.get("timeframe", "T+3")
    exec_date = row.get("exec_date")

    if not stock_code or not direction or direction == "neutral":
        return None

    hold_days = _TIMEFRAME_DAYS.get(timeframe, 3)
    exit_date = _get_trade_day_offset(exec_date, hold_days)
    if not exit_date or exit_date > date.today():
        return None  # 还没到验证时间

    entry_price = _get_close_price(stock_code, exec_date)
    exit_price = _get_close_price(stock_code, exit_date)
    if not entry_price or not exit_price or entry_price <= 0:
        return None

    pnl_pct = (exit_price - entry_price) / entry_price * 100

    if direction == "bullish":
        correct = pnl_pct > 0
    elif direction == "bearish":
        correct = pnl_pct < 0
    else:
        correct = None

    return {
        "exit_date": exit_date.isoformat(),
        "pnl_pct": round(pnl_pct, 2),
        "hold_days": hold_days,
        "correct": correct,
        "exit_reason": "timeframe",
    }


def _get_close_price(stock_code: str, trade_date) -> Optional[float]:
    """获取指定日期收盘价。"""
    try:
        from app.data_sources.factory import DataSourceFactory
        ds = DataSourceFactory.get_source("CNStock")
        klines = ds.get_kline(stock_code, timeframe="1D", days=1, end_date=str(trade_date))
        if klines and len(klines) > 0:
            return float(klines[-1].get("close", 0))
    except Exception:
        pass
    return None


def _get_trade_day_offset(start_date, offset_days: int):
    """简单偏移（不考虑节假日精确性，后续可用交易日历优化）。"""
    if not start_date:
        return None
    if isinstance(start_date, str):
        from datetime import datetime
        start_date = datetime.strptime(start_date[:10], "%Y-%m-%d").date()
    return start_date + timedelta(days=int(offset_days * 1.5))  # 粗略按 1.5 倍自然日


# ═══════════════════════════════════════════════════════════════
# 2. Skill 权重迭代
# ═══════════════════════════════════════════════════════════════

def update_skill_weights():
    """从 qd_traces 聚合每个 Skill 的历史表现，更新 qd_skill_weights。

    核心公式:
        return_per_day = (win_rate × avg_win - loss_rate × avg_loss) / avg_hold_days
        weight = clamp(1.0 + return_per_day × 20, 0.5, 2.0)
    """
    with _get_db() as conn:
        cur = conn.cursor()

        # 获取已验证的 skill 层记录
        cur.execute("""
            SELECT name AS skill_name,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE correct = true) AS wins,
                   AVG(pnl_pct) FILTER (WHERE correct = true) AS avg_win,
                   AVG(ABS(pnl_pct)) FILTER (WHERE correct = false) AS avg_loss,
                   AVG(hold_days) AS avg_hold
            FROM qd_traces
            WHERE layer = 'skill' AND correct IS NOT NULL
            GROUP BY name
            HAVING COUNT(*) >= 3
        """)
        rows = cur.fetchall()

        for r in rows:
            skill = r["skill_name"]
            total = r["total"]
            wins = r["wins"] or 0
            avg_win = r["avg_win"] or 0
            avg_loss = r["avg_loss"] or 0
            avg_hold = max(r["avg_hold"] or 1, 1)

            win_rate = wins / total if total > 0 else 0
            expected_return = win_rate * avg_win - (1 - win_rate) * avg_loss
            return_per_day = expected_return / avg_hold
            weight = max(0.5, min(2.0, 1.0 + return_per_day * 20))
            profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else avg_win

            cur.execute("""
                INSERT INTO qd_skill_weights
                    (skill_name, weight, win_rate, avg_pnl_pct, avg_hold_days,
                     return_per_day, profit_loss_ratio, sample_count, last_updated)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (skill_name) DO UPDATE SET
                    weight = EXCLUDED.weight,
                    win_rate = EXCLUDED.win_rate,
                    avg_pnl_pct = EXCLUDED.avg_pnl_pct,
                    avg_hold_days = EXCLUDED.avg_hold_days,
                    return_per_day = EXCLUDED.return_per_day,
                    profit_loss_ratio = EXCLUDED.profit_loss_ratio,
                    sample_count = EXCLUDED.sample_count,
                    last_updated = NOW()
            """, (skill, round(weight, 3), round(win_rate, 3),
                  round(expected_return, 2), round(avg_hold, 1),
                  round(return_per_day, 4), round(profit_loss_ratio, 2), total))

        # 自动注册新 Skill（registry 中有但表中无的）
        try:
            from app.agent.skills.indicator_skills import get_all_skill_names
            cur.execute("SELECT skill_name FROM qd_skill_weights")
            existing = {r["skill_name"] for r in cur.fetchall()}
            for name in get_all_skill_names():
                if name not in existing:
                    cur.execute("""
                        INSERT INTO qd_skill_weights (skill_name, weight, sample_count)
                        VALUES (%s, 1.0, 0) ON CONFLICT DO NOTHING
                    """, (name,))
        except Exception:
            pass

        conn.commit()
        cur.close()


# ═══════════════════════════════════════════════════════════════
# 3. 因子权重迭代
# ═══════════════════════════════════════════════════════════════

def update_factor_weights():
    """从 qd_traces 的 factors JSON 聚合因子级表现，更新 qd_factor_weights。

    带时间衰减：半衰期由因子类型决定。
    自动清理过期因子。
    """
    _HALF_LIFE = {
        "新闻": 7, "情绪": 7, "政策": 14, "消息": 7,
        "游资": 14, "资金": 14, "龙虎榜": 7,
        "概念": 21, "板块": 21,
        "MACD": 30, "KDJ": 30, "RSI": 30, "BOLL": 30, "指标": 30,
        "趋势": 60, "量价": 60, "筹码": 60, "形态": 30,
    }

    def _get_half_life(factor_name: str) -> int:
        for key, days in _HALF_LIFE.items():
            if key in factor_name:
                return days
        return 30

    with _get_db() as conn:
        cur = conn.cursor()

        # 获取含 factors 的已验证 chain 层记录
        cur.execute("""
            SELECT factors, correct, pnl_pct, hold_days, exec_date
            FROM qd_traces
            WHERE layer = 'chain' AND correct IS NOT NULL AND factors IS NOT NULL
            ORDER BY exec_date DESC LIMIT 2000
        """)
        rows = cur.fetchall()

        # 聚合: (skill_name, factor_name) → trades
        factor_trades: Dict[tuple, List[Dict]] = {}
        today = date.today()

        for row in rows:
            factors = row["factors"]
            if isinstance(factors, str):
                import json
                try:
                    factors = json.loads(factors)
                except Exception:
                    continue
            if not isinstance(factors, list):
                continue

            for f in factors:
                if not isinstance(f, dict):
                    continue
                fname = f.get("name", "")
                if not fname:
                    continue

                # factors 中没有 skill_name 信息，用 "agent" 作为默认
                key = ("agent", fname)
                if key not in factor_trades:
                    factor_trades[key] = []

                age_days = (today - row["exec_date"]).days if row["exec_date"] else 0
                decay = 0.5 ** (age_days / max(_get_half_life(fname), 1))

                factor_trades[key].append({
                    "correct": row["correct"],
                    "pnl_pct": row["pnl_pct"] or 0,
                    "hold_days": row["hold_days"] or 3,
                    "decay": decay,
                })

        # 计算并写入
        active_keys = set()
        for (skill_name, factor_name), trades in factor_trades.items():
            if len(trades) < 2:
                continue
            active_keys.add((skill_name, factor_name))

            # 加权计算
            total_w = sum(t["decay"] for t in trades)
            if total_w <= 0:
                continue

            win_w = sum(t["decay"] for t in trades if t["correct"])
            win_rate = win_w / total_w

            wins = [t for t in trades if t["correct"]]
            losses = [t for t in trades if not t["correct"]]
            avg_win = sum(t["pnl_pct"] * t["decay"] for t in wins) / sum(t["decay"] for t in wins) if wins else 0
            avg_loss = abs(sum(t["pnl_pct"] * t["decay"] for t in losses) / sum(t["decay"] for t in losses)) if losses else 0
            avg_hold = sum(t["hold_days"] * t["decay"] for t in trades) / total_w

            expected_return = win_rate * avg_win - (1 - win_rate) * avg_loss
            return_per_day = expected_return / max(avg_hold, 1)
            weight = max(0.5, min(2.0, 1.0 + return_per_day * 20))

            cur.execute("""
                INSERT INTO qd_factor_weights
                    (skill_name, factor_name, weight, win_rate, avg_pnl_pct,
                     avg_hold_days, return_per_day, sample_count, decay_half_life, last_updated)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (skill_name, factor_name) DO UPDATE SET
                    weight = EXCLUDED.weight,
                    win_rate = EXCLUDED.win_rate,
                    avg_pnl_pct = EXCLUDED.avg_pnl_pct,
                    avg_hold_days = EXCLUDED.avg_hold_days,
                    return_per_day = EXCLUDED.return_per_day,
                    sample_count = EXCLUDED.sample_count,
                    last_updated = NOW()
            """, (skill_name, factor_name, round(weight, 3), round(win_rate, 3),
                  round(expected_return, 2), round(avg_hold, 1),
                  round(return_per_day, 4), len(trades), _get_half_life(factor_name)))

        # 清理过期因子（近 60 天内未出现的）
        if active_keys:
            placeholders = ",".join(["(%s,%s)"] * len(active_keys))
            params = []
            for s, f in active_keys:
                params.extend([s, f])
            cur.execute(f"""
                DELETE FROM qd_factor_weights
                WHERE (skill_name, factor_name) NOT IN ({placeholders})
                AND last_updated < NOW() - INTERVAL '60 days'
            """, params)

        conn.commit()
        cur.close()
