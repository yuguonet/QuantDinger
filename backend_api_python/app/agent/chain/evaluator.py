# -*- coding: utf-8 -*-
"""
Evaluator — 回溯评估引擎（重写版）。

基于 qd_traces 表 + qd_agent_weights。

核心流程（每日盘后自动运行）：
  evaluate_pending()      → 按 timeframe 取实际行情，写回 qd_traces
  update_weights()        → 统一更新 skill + factor 权重（原 update_skill_weights / update_factor_weights 已合并）
  auto_evaluate()         → 自动闭环

核心指标：单位时间期望收益率（不是胜率）
  return_per_day = (win_rate × avg_win - loss_rate × avg_loss) / avg_hold_days

纯 SQL + 数学，0 token 消耗，不涉及 agent。
"""
from __future__ import annotations

import json
from log import logger
import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from chain.schema import (
    DIRECTION_THRESHOLD, Direction, classify_return, is_direction_correct,
)
from chain import store
# ═══════════════════════════════════════════════════════════════
# timeframe → 验证窗口映射
# ═══════════════════════════════════════════════════════════════

_TIMEFRAME_DAYS = {
    "T+1": 1,
    "T+3": 3,
    "T+5": 5,
    "1W": 5,
    "1M": 22,
    "3M": 66,
    "1Y": 252,
}

_DEFAULT_HOLD_DAYS = 3  # timeframe 缺失时的默认值
def _get_hold_days(timeframe: str) -> int:
    """timeframe → 验证用持有天数。"""
    return _TIMEFRAME_DAYS.get(timeframe, _DEFAULT_HOLD_DAYS)
# ═══════════════════════════════════════════════════════════════
# 实际行情获取
# ═══════════════════════════════════════════════════════════════

def _get_actual_return(
    stock_code: str,
    from_date: date,
    hold_days: int,
    market: str = "CNStock",
) -> Optional[Dict[str, Any]]:
    """获取股票实际涨跌数据。

    Args:
        stock_code: 股票代码
        from_date: 决策日期
        hold_days: 持有天数
        market: 市场类型

    Returns:
        {"pnl_pct": float, "hold_days": int, "direction": str} 或 None
    """
    try:
        from app.data_sources.cn_stock import CNStockDataSource
        from datetime import datetime as _dt

        # 直接走底层数据源 cn_stock（agent_get_kline 工具即其封装 + 归一）。
        # 不再 import 工具包装层，避免破坏拔插式单一真相源。
        raw = CNStockDataSource().get_kline(stock_code, "1D", max(hold_days + 10, 1))

        def _ts_to_date(ts):
            try:
                return _dt.fromtimestamp(int(ts)).strftime("%m-%d")
            except Exception:
                return str(ts)

        # 规整成 evaluator 下游依赖的 {t,o,h,l,c,v} 形态（与 agent_get_kline 一致）
        klines = [{
            "t": (k.get("date") or _ts_to_date(k.get("time", 0)))[:10],
            "o": round(k.get("open", 0), 2),
            "h": round(k.get("high", 0), 2),
            "l": round(k.get("low", 0), 2),
            "c": round(k.get("close", 0), 2),
            "v": k.get("volume", 0),
        } for k in (raw or []) if isinstance(k, dict)]

        # 校验：需要可遍历的列表
        if not isinstance(klines, list) or len(klines) < 2:
            return None

        base_idx = None
        for i, k in enumerate(klines):
            if not isinstance(k, dict):
                continue
            raw_date = str(k.get("t", ""))[:10]
            try:
                normalized = raw_date.replace("/", "-")
                if len(normalized) == 8 and normalized.isdigit():
                    normalized = f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:8]}"
                k_date = date.fromisoformat(normalized)
            except (ValueError, TypeError):
                continue
            if k_date >= from_date:
                base_idx = i
                break

        if base_idx is None:
            return None

        base_close = klines[base_idx].get("c", 0)
        if not base_close:
            return None
        exit_idx = min(base_idx + hold_days, len(klines) - 1)
        if exit_idx <= base_idx:
            return None

        exit_close = klines[exit_idx].get("c", 0)
        if not exit_close:
            return None
        pnl_pct = round((exit_close - base_close) / base_close * 100, 2)
        actual_hold = exit_idx - base_idx
        direction = classify_return(pnl_pct / 100)

        return {
            "pnl_pct": pnl_pct,
            "hold_days": actual_hold,
            "direction": direction,
            "exit_date": klines[exit_idx].get("t", "")[:10] if isinstance(klines[exit_idx], dict) else "",
        }

    except Exception as e:
        logger.warning("[Evaluator] 获取实际涨跌失败 %s: %s", stock_code, e)
        return None
# ═══════════════════════════════════════════════════════════════
# 评估执行
# ═══════════════════════════════════════════════════════════════

def _bump_eval_failure(root_id: int):
    """累计单条记录的评估失败次数（写 qd_traces.error，'eval_failed:N' 前缀）。

    毒丸治理配套（审计 P0-1 断点C）：取不到行情的记录（退市/停牌/代码错误）此前
    会被静默跳过并永久占用评估队列。失败次数 >= 5 时由 store 侧置 unverifiable。
    """
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE qd_traces
                SET error = 'eval_failed:' || (
                    COALESCE(
                        (regexp_match(COALESCE(error, ''), '^eval_failed:(\\d+)'))[1]::int, 0
                    ) + 1
                )
                WHERE id = %s AND parent_id IS NULL AND exit_date IS NULL
            """, (root_id,))
            conn.commit()
    except Exception as e:
        logger.debug("[Evaluator] 失败计数写入失败 root_id=%s: %s", root_id, e)


def evaluate_pending(days_old: int = 1, market: str = "CNStock") -> Dict[str, Any]:
    """评估所有待验证的决策记录。

    查找 qd_traces 中 exit_date IS NULL 的根节点，
    按 timeframe 取实际行情，写回验证结果。

    Args:
        days_old: 只评估至少 N 天前的决策
        market: 市场类型

    Returns:
        {"evaluated": int, "errors": int, "details": list}
    """
    stats = {"evaluated": 0, "errors": 0, "details": []}

    pending = store.query_pending_verify(days_old=days_old, limit=100)

    for item in pending:
        root_id = item["id"]
        exec_date = item["exec_date"]
        stock_code = item["stock_code"]
        action = item["action"]
        timeframe = item.get("timeframe", "")
        if not (stock_code or "").strip():
            # 防御纵深：SQL 已滤空 code，这里兜底（永不可验证，计数让其出队）
            _bump_eval_failure(root_id)
            continue

        try:
            hold_days = _get_hold_days(timeframe)
            actual = _get_actual_return(stock_code, exec_date, hold_days, market)
            if not actual:
                # 失败计数累计到 error 列（'eval_failed:N' 前缀），供
                # store.query_pending_verify 在 >=5 次后将记录置 unverifiable 出队。
                # 不写 exit_date（未来 K 线补齐后仍可回补验证）。
                _bump_eval_failure(root_id)
                continue

            # 方向映射
            action_to_dir = {
                "buy": Direction.BULLISH.value,
                "sell": Direction.BEARISH.value,
                "hold": Direction.NEUTRAL.value,
                "skip": Direction.NEUTRAL.value,
            }
            predicted_dir = action_to_dir.get(action, Direction.NEUTRAL.value)
            actual_dir = actual["direction"]
            verdict = is_direction_correct(predicted_dir, actual_dir)

            if verdict == "neutral":
                correct = None
            else:
                correct = verdict == "correct"

            from datetime import datetime
            exit_date = None
            if actual.get("exit_date"):
                try:
                    exit_date = date.fromisoformat(actual["exit_date"])
                except (ValueError, TypeError):
                    pass

            store.update_verify_results(
                root_id=root_id,
                exit_date=exit_date or exec_date + timedelta(days=hold_days),
                exit_reason="max_hold",
                pnl_pct=actual["pnl_pct"],
                hold_days=actual["hold_days"],
                correct=correct,
            )

            # 写入 skill 子节点验证结果
            store.update_skill_verify(root_id, actual_dir)

            # 注：原此处调用 store.update_path_cache(root_id)，但 chain/store.py 中
            # 并不存在该函数（疑似旧版遗留接口），导致每条评估在收尾时抛 AttributeError，
            # evaluated 恒为 0、自动权重更新永不触发（审计 P0-1 断点A）。已移除，
            # 待真正实现编排路径缓存时再回加。
            stats["evaluated"] += 1
            stats["details"].append({
                "root_id": root_id, "stock": stock_code,
                "action": action, "timeframe": timeframe,
                "pnl_pct": actual["pnl_pct"], "correct": correct,
            })

        except Exception as e:
            stats["errors"] += 1
            stats["details"].append({
                "root_id": root_id, "stock": stock_code,
                "status": "error", "error": str(e),
            })
            logger.error("[Evaluator] 评估 root_id=%d 失败: %s", root_id, e)

    logger.info("[Evaluator] 评估完成: %d 条已评估, %d 条失败",
                stats["evaluated"], stats["errors"])

    # 评估后自动更新权重（链式门控 · 第 1 级：evaluated>0 才进入下一步）。
    # 末级 maybe_revise 已移入 update_weights() 末尾，按 "updated>0" 再门控一次
    # （重设计 §2.6 / 模块边界 §11.1，2026-09-19 调整）。
    if stats["evaluated"] > 0:
        try:
            update_weights()
        except Exception as e:
            logger.warning("[Evaluator] 自动更新权重失败: %s", e)

    return stats
# ═══════════════════════════════════════════════════════════════
# Skill 权重更新（按单位时间收益率）
# ═══════════════════════════════════════════════════════════════

def _calc_skill_weight_from_trades(trades: List[Dict]) -> Dict[str, float]:
    """从历史交易记录计算 Skill 权重。

    核心指标：单位时间期望收益率
      return_per_day = (win_rate × avg_win - loss_rate × avg_loss) / avg_hold_days

    Returns:
        {"weight": float, "win_rate": float, "avg_pnl_pct": float,
         "avg_hold_days": float, "return_per_day": float, "sample_count": int}
    """
    if not trades:
        return {"weight": 1.0, "win_rate": 0, "avg_pnl_pct": 0,
                "avg_hold_days": 1, "return_per_day": 0, "sample_count": 0}

    correct_trades = [t for t in trades if t.get("correct") is True]
    wrong_trades = [t for t in trades if t.get("correct") is False]
    total = len(correct_trades) + len(wrong_trades)

    if total == 0:
        return {"weight": 1.0, "win_rate": 0, "avg_pnl_pct": 0,
                "avg_hold_days": 1, "return_per_day": 0, "sample_count": 0}

    win_rate = len(correct_trades) / total
    avg_win = (sum(t["pnl_pct"] for t in correct_trades) / len(correct_trades)) if correct_trades else 0
    avg_loss = abs(sum(t["pnl_pct"] for t in wrong_trades) / len(wrong_trades)) if wrong_trades else 0
    avg_hold = sum(t.get("hold_days", 3) for t in trades) / len(trades)
    avg_hold = max(avg_hold, 1)

    expected_return = win_rate * avg_win - (1 - win_rate) * avg_loss
    return_per_day = expected_return / avg_hold

    # 映射到权重（0.5~2.0），带样本量置信度（2026-09-11，审计 P2）：
    # 旧实现 1 + rpd*20 的问题：+0.67%/日 -> 1.013（区分度≈0）；
    # -1%/日 与 -0.025%/日 同触 0.5 地板（噪声主导）。两步修正：
    # 1) 胜率先取 Wilson 下界（95% 置信），小样本高胜率不再直接顶格——
    #    n=5 全对时 win_rate=1.0 但下界仅 0.566；
    # 2) 用样本量缩放因子把权重拉向中性 1.0：n<15 信号不足，n>=30 全额生效。
    #    收益维度（expected_return）保持原始值进入线性段——它才是单位时间收益的分子。
    z = 1.96
    denom = 1 + z * z / total
    center = (win_rate + z * z / (2 * total)) / denom
    margin = z * math.sqrt(win_rate * (1 - win_rate) / total + z * z / (4 * total * total))
    win_rate_lb = max(0.0, center - margin)

    sample_scale = min(1.0, max(0.0, (total - 15) / 15.0))
    expected_adj = (win_rate_lb * avg_win - (1 - win_rate_lb) * avg_loss) / avg_hold
    weight_raw = 1.0 + expected_adj * 20
    weight = 1.0 + (weight_raw - 1.0) * sample_scale
    weight = max(0.5, min(2.0, weight))

    return {
        "weight": round(weight, 3),
        "win_rate": round(win_rate, 3),
        "avg_pnl_pct": round(expected_return, 2),
        "avg_hold_days": round(avg_hold, 1),
        "return_per_day": round(return_per_day, 4),
        "sample_count": total,
    }
def update_weights(days: int = 90) -> Dict[str, Any]:
    """更新 qd_agent_weights 表（统一 skill + factor + tool 三层，同表分层）。

    一次扫描 qd_traces，同时产出：
      1. skill 层权重（按单位时间收益率）
      2. factor 层权重（带时间衰减的准确率）
      3. tool 层权重（按工具参与链路的 correct 率，含失败样本）

    自动同步 registry（双向）：
      - 新 Skill/新工具 → INSERT 工厂默认值（权重 1.0，sample_count=0）；
      - 已删除的 Skill/工具 → DELETE 对应行（不再人工介入）。
    """
    from app.utils.db import get_db_connection

    stats = {"synced": 0, "skill_updated": 0, "factor_updated": 0, "factor_cleaned": 0,
             "tool_updated": 0, "tool_synced": 0, "tool_cleaned": 0, "skill_cleaned": 0}
    since = date.today() - timedelta(days=days)
    today = date.today()

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # ① 同步 semantics：新 Skill 自动 INSERT 工厂默认值
            cur.execute("SELECT name FROM qd_agent_weights WHERE layer = 'skill'")
            existing_skills = {row['name'] for row in cur.fetchall()}

            from llm.qd_skills import QDSkillAdapter
            adapter = QDSkillAdapter()
            for skill_info in adapter.list_skills():
                name = skill_info["name"]
                if name not in existing_skills:
                    info = adapter.get(name)
                    default_w = info.default_weight if info and info.default_weight else 1.0
                    cur.execute("""
                        INSERT INTO qd_agent_weights (layer, name, skill_name, weight, sample_count)
                        VALUES ('skill', %s, NULL, %s, 0)
                        ON CONFLICT (layer, name, COALESCE(skill_name, '')) DO NOTHING
                    """, (name, default_w))
                    stats["synced"] += 1
                    logger.info("[Evaluator] 新 Skill 注册: %s (weight=%.2f)", name, default_w)

            # ①b 同步 skills：已删除的 Skill → DELETE 权重行（不再人工介入）
            live_skills = {s["name"] for s in adapter.list_skills()}
            dead_skills = existing_skills - live_skills
            for name in sorted(dead_skills):
                cur.execute("DELETE FROM qd_agent_weights WHERE layer = 'skill' AND name = %s", (name,))
                stats["skill_cleaned"] += 1
                logger.info("[Evaluator] 已删除 Skill，权重行清理: %s", name)

            # ①c 同步 tools：新工具 → INSERT 默认；已删除工具 → DELETE（与 skill 同一张表，layer='tool'）
            # 注意 tool_provider 在 execute 阶段才惰性初始化；这里独立扫描工具目录（与 ToolProvider 同规则）。
            live_tools: set = set()
            try:
                from pathlib import Path as _Path
                from tools.base import ToolProvider as _TP
                _tp = _TP()
                _tools_dir = _Path(__file__).resolve().parent.parent / "tools"
                _tp.scan_directory(_tools_dir, domain="common", package_prefix="tools")
                _tp.scan_subdirectories(_tools_dir, package_prefix="tools")
                live_tools = set(_tp.get_tool_names())
            except Exception as e:
                logger.warning("[Evaluator] 工具目录扫描失败，跳过 tool 层同步: %s", e)
            if live_tools:
                cur.execute("SELECT name FROM qd_agent_weights WHERE layer = 'tool'")
                existing_tools = {row["name"] for row in cur.fetchall()}
                for name in sorted(live_tools - existing_tools):
                    cur.execute("""
                        INSERT INTO qd_agent_weights (layer, name, skill_name, weight, sample_count)
                        VALUES ('tool', %s, NULL, 1.0, 0)
                        ON CONFLICT (layer, name, COALESCE(skill_name, '')) DO NOTHING
                    """, (name,))
                    stats["tool_synced"] += 1
                for name in sorted(existing_tools - live_tools):
                    cur.execute("DELETE FROM qd_agent_weights WHERE layer = 'tool' AND name = %s", (name,))
                    stats["tool_cleaned"] += 1
                    logger.info("[Evaluator] 已删除工具，权重行清理: %s", name)

            # ② 一次扫描 qd_traces，同时聚合 skill 和 factor 数据
            cur.execute("""
                SELECT t.name as skill_name, t.factors, t.pnl_pct,
                       t.hold_days, t.correct, r.exec_date
                FROM qd_traces t
                JOIN qd_traces r ON r.id = t.root_id
                WHERE t.layer = 'skill'
                  AND t.status = 'ok'
                  AND t.correct IS NOT NULL
                  AND r.exec_date >= %s
            """, (since,))

            # 护栏 2 真实现（2026-09-24 提智 0.3）：修订过的技能只统计**新版本**产出的 run
            # （exec_date ≥ 修订时刻）——设计 §3.17“新版本重新积累”此前只写了权重行 reset，
            # 本轮 update_weights 又用 90 日窗全量重算，重置恒被覆盖（护栏 2 名存实亡的一半）。
            from chain.store import get_skill_revisions as _gsrs
            _revisions = _gsrs()

            skill_trades: Dict[str, List[Dict]] = {}
            factor_stats: Dict[tuple, Dict[str, float]] = {}

            for row in cur.fetchall():
                skill_name = row['skill_name']
                correct = row['correct']
                exec_date = row['exec_date']

                # 旧版本产出的 run 不进新版本评价（skill 与 factor 同口径，防偏）
                _rev = _revisions.get(skill_name)
                if _rev and _rev.get("revised_at") and exec_date < _rev["revised_at"]:
                    continue

                # 聚合 skill 交易数据
                if skill_name not in skill_trades:
                    skill_trades[skill_name] = []
                skill_trades[skill_name].append({
                    "pnl_pct": row['pnl_pct'],
                    "hold_days": row['hold_days'] or 3,
                    "correct": correct,
                    "exec_date": exec_date,
                })

                # 聚合 factor 数据（带时间衰减）
                factors_json = row['factors']
                try:
                    factors_raw = json.loads(factors_json) if isinstance(factors_json, str) else (factors_json or [])
                except (json.JSONDecodeError, TypeError):
                    factors_raw = []

                days_ago = (today - exec_date).days
                for factor in factors_raw:
                    if not isinstance(factor, dict):
                        continue
                    fname = factor.get("name", "")
                    if not fname:
                        continue

                    key = (skill_name, fname)
                    hl = _get_factor_half_life(fname)
                    decay_weight = math.pow(0.5, days_ago / max(hl, 1))

                    if key not in factor_stats:
                        factor_stats[key] = {
                            "weighted_correct": 0.0, "weighted_total": 0.0,
                            "raw_total": 0, "half_life": hl,
                        }
                    factor_stats[key]["weighted_total"] += decay_weight
                    factor_stats[key]["raw_total"] += 1
                    if correct:
                        factor_stats[key]["weighted_correct"] += decay_weight

            # ③ UPSERT skill 权重
            for skill_name, trades in skill_trades.items():
                result = _calc_skill_weight_from_trades(trades)
                cur.execute("""
                    INSERT INTO qd_agent_weights
                        (layer, name, skill_name, weight, win_rate,
                         avg_pnl_pct, avg_hold_days, return_per_day,
                         sample_count, last_updated)
                    VALUES ('skill', %s, NULL, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (layer, name, COALESCE(skill_name, ''))
                    DO UPDATE SET
                        weight = EXCLUDED.weight,
                        win_rate = EXCLUDED.win_rate,
                        avg_pnl_pct = EXCLUDED.avg_pnl_pct,
                        avg_hold_days = EXCLUDED.avg_hold_days,
                        return_per_day = EXCLUDED.return_per_day,
                        sample_count = EXCLUDED.sample_count,
                        last_updated = NOW()
                """, (
                    skill_name, result["weight"], result["win_rate"],
                    result["avg_pnl_pct"], result["avg_hold_days"],
                    result["return_per_day"], result["sample_count"],
                ))
                stats["skill_updated"] += 1

            # ④ UPSERT factor 权重
            active_factor_keys = set()
            for (skill_name, fname), s in factor_stats.items():
                total = s["weighted_total"]
                if total < 0.5:
                    continue

                active_factor_keys.add((skill_name, fname))
                accuracy = round(s["weighted_correct"] / total, 4)
                weight = max(0.5, min(2.0, accuracy * 2))

                cur.execute("""
                    INSERT INTO qd_agent_weights
                        (layer, name, skill_name, weight, win_rate,
                         sample_count, decay_half_life, last_updated)
                    VALUES ('factor', %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (layer, name, COALESCE(skill_name, ''))
                    DO UPDATE SET
                        weight = EXCLUDED.weight,
                        win_rate = EXCLUDED.win_rate,
                        sample_count = EXCLUDED.sample_count,
                        decay_half_life = EXCLUDED.decay_half_life,
                        last_updated = NOW()
                """, (fname, skill_name, weight, accuracy,
                      int(s["raw_total"]), s["half_life"]))
                stats["factor_updated"] += 1

            # ⑤ 清理过期因子
            if active_factor_keys:
                placeholders = []
                params = []
                for sname, fname in active_factor_keys:
                    placeholders.append("NOT (skill_name = %s AND name = %s)")
                    params.extend([sname, fname])
                cur.execute(f"""
                    DELETE FROM qd_agent_weights
                    WHERE layer = 'factor'
                      AND sample_count > 0
                      AND ({' AND '.join(placeholders)})
                """, params)
                stats["factor_cleaned"] = cur.rowcount

            # ⑥ tool 层权重（同表 layer='tool'）：按工具参与链路的 correct 率统计。
            # 样本含失败链路（工具坏了也是权重信号）；sample_count < 最小样本时不动（防误判）。
            cur.execute("""
                SELECT child.name AS tool_name,
                       COUNT(*) AS n,
                       AVG(CASE WHEN root.correct THEN 1.0 ELSE 0.0 END) AS win_rate
                FROM qd_traces child
                JOIN qd_traces root ON child.root_id = root.id
                WHERE child.layer = 'tool'
                  AND root.layer = 'chain'
                  AND root.correct IS NOT NULL
                  AND root.exec_date >= %s
                GROUP BY child.name
            """, (since,))
            for row in cur.fetchall():
                tname, n, wr = row["tool_name"], int(row["n"]), float(row["win_rate"])
                # 权重：0.5~2.0 夹紧（与 factor 层同幅度）；低样本不改权重
                weight = 1.0 if n < 10 else round(max(0.5, min(2.0, 1.0 + (wr - 0.5) * 2.0)), 4)
                cur.execute("""
                    INSERT INTO qd_agent_weights
                        (layer, name, skill_name, weight, win_rate, sample_count, last_updated)
                    VALUES ('tool', %s, NULL, %s, %s, %s, NOW())
                    ON CONFLICT (layer, name, COALESCE(skill_name, ''))
                    DO UPDATE SET
                        weight = EXCLUDED.weight,
                        win_rate = EXCLUDED.win_rate,
                        sample_count = EXCLUDED.sample_count,
                        last_updated = NOW()
                """, (tname, weight, wr, n))
                stats["tool_updated"] += 1

            conn.commit()

    except Exception as e:
        logger.error("[Evaluator] 更新权重失败: %s", e)

    logger.info("[Evaluator] 权重更新: skill 同步%d/更新%d/清理%d, tool 同步%d/更新%d/清理%d, factor %d/清理 %d",
                stats["synced"], stats["skill_updated"], stats["skill_cleaned"],
                stats["tool_synced"], stats["tool_updated"], stats["tool_cleaned"],
                stats["factor_updated"], stats["factor_cleaned"])
    # 迭代旁支调度（重设计 §2.6，2026-09-19）：链式门控 · 第 2 级——仅当权重确有更新
    # （skill/factor/tool 任一 *_updated>0）才把低于阈值的 auto_ 技能交给修订器。
    # 护栏/队列全在 skill_brewer 侧；本处只一行调度（模块边界 §11.1）。
    _updated = stats["skill_updated"] + stats["factor_updated"] + stats["tool_updated"]
    if _updated > 0:
        try:
            from chain.skill_brewer import maybe_revise
            from chain.store import get_skill_weight_rows as _gswr
            _rev_results = maybe_revise(_gswr())
            if any(r.get("status") == "revised" for r in _rev_results):
                stats["revised"] = sum(1 for r in _rev_results if r.get("status") == "revised")
        except Exception as _re:
            logger.warning("[Evaluator] 技能修订调度跳过: %s", _re)

    return stats

# ═══════════════════════════════════════════════════════════════
# 评估报告
# ═══════════════════════════════════════════════════════════════

def get_eval_report(days: int = 30) -> Dict[str, Any]:
    """获取评估报告。"""
    from app.utils.db import get_db_connection

    since = date.today() - timedelta(days=days)
    result = {"overall": {}, "skills": [], "factors": []}

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 总体准确率
            cur.execute("""
                SELECT COUNT(*) as total,
                       AVG(CASE WHEN correct THEN 1.0 ELSE 0.0 END) as acc
                FROM qd_traces
                WHERE parent_id IS NULL AND correct IS NOT NULL
                  AND exec_date >= %s
            """, (since,))

            row = cur.fetchone()
            if row and row['total']:
                result["overall"] = {
                    "total": row['total'],
                    "accuracy": round(row['acc'], 3) if row['acc'] else 0,
                }

            # 各 skill 准确率
            cur.execute("""
                SELECT t.name,
                       COUNT(*) as cnt,
                       AVG(CASE WHEN t.correct THEN 1.0 ELSE 0.0 END) as acc
                FROM qd_traces t
                JOIN qd_traces r ON r.id = t.root_id
                WHERE t.layer = 'skill' AND t.correct IS NOT NULL
                  AND r.exec_date >= %s
                GROUP BY t.name
                ORDER BY acc DESC
            """, (since,))

            result["skills"] = [
                {"name": row['name'], "count": row['cnt'],
                 "accuracy": round(row['acc'], 3) if row['acc'] else 0}
                for row in cur.fetchall()
            ]

            # 因子准确率
            cur.execute("""
                SELECT skill_name, name as factor_name, win_rate, weight, sample_count
                FROM qd_agent_weights
                WHERE layer = 'factor' AND sample_count >= 3
                ORDER BY win_rate DESC
                LIMIT 30
            """)

            result["factors"] = [
                {"skill": row['skill_name'], "factor": row['factor_name'],
                 "accuracy": round(row['win_rate'], 3) if row['win_rate'] else 0,
                 "weight": row['weight'], "samples": row['sample_count']}
                for row in cur.fetchall()
            ]

    except Exception as e:
        logger.error("[Evaluator] 获取评估报告失败: %s", e)

    return result
# ═══════════════════════════════════════════════════════════════
# 自动评估入口
# ═══════════════════════════════════════════════════════════════

def auto_evaluate(days_old: int = 1, market: str = "CNStock") -> Dict[str, Any]:
    """自动评估闭环：评估待验证决策 → 更新权重 → 生成报告。"""
    result = {}

    try:
        eval_stats = evaluate_pending(days_old=days_old, market=market)
        result["evaluation"] = eval_stats
    except Exception as e:
        logger.error("[AutoEval] 评估失败: %s", e)
        result["evaluation"] = {"evaluated": 0, "errors": 1, "error": str(e)}

    try:
        report = get_eval_report()
        result["report"] = report
    except Exception as e:
        logger.error("[AutoEval] 报告生成失败: %s", e)
        result["report"] = {"error": str(e)}

    return result
# ═══════════════════════════════════════════════════════════════
# 后台 Worker
# ═══════════════════════════════════════════════════════════════

_eval_thread = None
_eval_stop = None

_worker_health = {
    "last_run_at": None, "last_success_at": None,
    "last_error": None, "consecutive_failures": 0,
    "total_runs": 0, "total_successes": 0, "total_failures": 0,
}

def get_worker_health() -> Dict[str, Any]:
    h = dict(_worker_health)
    h["is_alive"] = _eval_thread is not None and _eval_thread.is_alive()
    h["next_run_in_seconds"] = _seconds_until_post_market() if h["is_alive"] else None
    h["schedule"] = "每天 15:30（盘后）"
    return h
def _seconds_until_post_market() -> float:
    """计算距离下一个盘后 15:30 的秒数。"""
    from datetime import datetime
    now = datetime.now()
    target = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now >= target:
        # 已过今天 15:30，算明天
        from datetime import timedelta
        target += timedelta(days=1)
    # 跳过周末
    while target.weekday() >= 5:
        from datetime import timedelta
        target += timedelta(days=1)
    return max(0, (target - now).total_seconds())
def start_eval_worker():
    """启动后台评估 worker（盘后 15:30 每天运行一次，T+N 验证）。"""
    global _eval_thread, _eval_stop

    import threading
    import time as _time

    if _eval_thread is not None and _eval_thread.is_alive():
        return

    _eval_stop = threading.Event()

    def _worker():
        while not _eval_stop.is_set():
            # 计算距离下一个盘后 15:30 的等待时间
            wait_secs = _seconds_until_post_market()
            logger.info("[EvalWorker] 下次盘后验证: %.0f 秒后 (%.1f 小时)",
                        wait_secs, wait_secs / 3600)
            if _eval_stop.wait(timeout=wait_secs):
                break  # 收到停止信号

            # 等待盘后批次完成（K线数据同步就绪）
            try:
                from app.market_cn.scheduler import post_market_done
                if not post_market_done.is_set():
                    logger.info("[EvalWorker] 等待盘后数据同步完成...")
                    post_market_done.wait(timeout=3600)  # 最多等1小时
                    logger.info("[EvalWorker] 盘后数据同步完成，开始回溯验证")
            except Exception as e:
                logger.warning("[EvalWorker] 等待盘后批次失败: %s，继续执行", e)

            # 盘后执行 T+N 验证
            _worker_health["total_runs"] += 1
            _worker_health["last_run_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                result = auto_evaluate(days_old=1)
                evaluated = result.get("evaluation", {}).get("evaluated", 0)
                _worker_health["consecutive_failures"] = 0
                _worker_health["total_successes"] += 1
                _worker_health["last_success_at"] = _worker_health["last_run_at"]
                _worker_health["last_error"] = None
                logger.info("[EvalWorker] 盘后验证完成: %d 条已评估", evaluated)
            except Exception as e:
                _worker_health["consecutive_failures"] += 1
                _worker_health["total_failures"] += 1
                _worker_health["last_error"] = str(e)
                logger.warning("[EvalWorker] 盘后验证失败: %s", e)

            # 技能酿造挂钩（2026-09-19 升级为触发策略 v2，重设计 §2.5）：
            # 每日调用一次 brew_skills(trigger="auto")，节拍由信号分内部决定
            # （信号就绪立即酿 / 7 日兜底 / 冷却），状态落 qd_agent_weights
            # （layer='brew_state'），重启不丢。worker 不再管理日期。
            try:
                from chain.skill_brewer import brew_skills, refresh_skill_adapter
                _brewed = brew_skills(trigger="auto")
                if any(r.get("status") == "brewed" for r in _brewed):
                    refresh_skill_adapter()
            except Exception as e:
                logger.warning("[EvalWorker] 技能酿造跳过: %s", e)

    _eval_thread = threading.Thread(target=_worker, daemon=True, name="eval-worker")
    _eval_thread.start()
    logger.info("[EvalWorker] 盘后回溯评估 worker 已启动")
def stop_eval_worker():
    global _eval_stop
    if _eval_stop:
        _eval_stop.set()
