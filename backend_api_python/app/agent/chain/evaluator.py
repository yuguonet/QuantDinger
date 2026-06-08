# -*- coding: utf-8 -*-
"""
Chain Evaluator — 决策闭环评估器。

职责：验证 Chain 决策的准确性，更新因子/工具权重，驱动系统自迭代。

核心流程（每日盘后自动运行）：
  evaluate_pending()        → 获取 T+1/3/5 实际涨跌，写入 qd_agent_decision_results
  update_factor_weights()   → 带时间衰减聚合因子准确率，写入 qd_agent_factor_weights
  update_tool_eval()        → 统计工具调用成功率/有用率，写入 qd_agent_tool_eval
  get_weight_adjustments()  → 生成权重调整建议（供人工审核或自动应用）

自动触发：
  start_chain_eval_worker() → 后台线程，每4小时运行 auto_evaluate_and_update()
  与 FastAnalysis 的 reflection worker 并列，但服务 Chain/Agent 决策系统。

门控支撑：
  get_chain_eval_stats()    → 返回已评估决策数/准确率/最小因子样本
                              供 executor 判断是否达到决策门槛（≥10条）

时间衰减：
  update_factor_weights(decay_half_life_days=30)
  近期样本权重更高：weight = 0.5 ^ (days_ago / half_life_days)
  30天前×0.5, 60天前×0.25, 90天前×0.125

方向判定阈值：
  _classify_return(threshold=0.003)
  收益率 > 0.3% → bullish, < -0.3% → bearish, 其余 → neutral
  旧阈值 0.5% 在震荡市丢弃过多样本，0.3% 更合理

中性结果处理（P0-2）：
  旧逻辑：actual == neutral → 不计分（丢弃样本）
  新逻辑：actual == neutral 仍参与计分（predicted neutral → True, 否则 → False）
  避免震荡市评估样本大量流失

数据库依赖（decision_evaluation.sql）：
  qd_agent_decisions          ← 决策记录（executor 写入）
  qd_agent_decision_steps     ← 步骤详情（executor 写入）
  qd_agent_decision_results   ← T+N 验证（本模块写入）
  qd_agent_factor_weights     ← 因子权重（本模块写入，executor 读取）
  qd_agent_tool_eval          ← 工具评估（本模块写入）

公开接口：
  evaluate_pending(days_old, market) → Dict
  update_factor_weights(days, decay_half_life_days) → Dict
  update_tool_eval(days) → Dict
  get_weight_adjustments(chain_id, days) → Dict
  walk_forward_validate(chain_id, train_days, test_days) → Dict
  get_eval_report(chain_id, days) → Dict
  auto_evaluate_and_update(days_old, market, decay_half_life_days, weight_adjust_days) → Dict
  get_chain_eval_stats(chain_id) → Dict
  start_chain_eval_worker() → None
  stop_chain_eval_worker() → None
  get_worker_health() → Dict（worker 健康状态：last_run_at/consecutive_failures/...）
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 实际涨跌数据获取
# ═══════════════════════════════════════════════════════════════

def _get_actual_returns(
    stock_code: str,
    from_date: date,
    market: str = "CNStock",
) -> Dict[str, Any]:
    """获取股票实际涨跌数据。"""
    try:
        from app.data_sources.factory import DataSourceFactory

        ds = DataSourceFactory.get(market)
        klines = ds.get_kline(stock_code, timeframe="1D", days=10)
        if not klines or len(klines) < 2:
            return {}

        from_str = from_date.strftime("%Y-%m-%d")
        base_idx = None
        for i, k in enumerate(klines):
            k_date = k.get("t", "")[:10]
            if k_date >= from_str:
                base_idx = i
                break

        if base_idx is None:
            return {}

        base_close = klines[base_idx]["c"]
        result = {}

        if base_idx + 1 < len(klines):
            close_1d = klines[base_idx + 1]["c"]
            ret_1d = (close_1d - base_close) / base_close
            result["return_1d"] = round(ret_1d, 4)
            result["direction_1d"] = _classify_return(ret_1d)

        if base_idx + 3 < len(klines):
            close_3d = klines[base_idx + 3]["c"]
            ret_3d = (close_3d - base_close) / base_close
            result["return_3d"] = round(ret_3d, 4)
            result["direction_3d"] = _classify_return(ret_3d)

        if base_idx + 5 < len(klines):
            close_5d = klines[base_idx + 5]["c"]
            ret_5d = (close_5d - base_close) / base_close
            result["return_5d"] = round(ret_5d, 4)
            result["direction_5d"] = _classify_return(ret_5d)

        return result

    except Exception as e:
        logger.warning("[Evaluator] 获取实际涨跌失败 %s: %s", stock_code, e)
        return {}


def _classify_return(ret: float, threshold: float = 0.003) -> str:
    """根据收益率判断方向。

    Args:
        ret: 收益率（如 0.03 = +3%）
        threshold: 方向判定阈值（默认 0.3%）
            - 0.5% 太高，震荡市大量样本被判为 neutral 导致丢弃
            - 0.3% 更合理：A股日均波动 2-3%，0.3% 是有效信号的下限

    Returns:
        "bullish" / "bearish" / "neutral"
    """
    if ret > threshold:
        return "bullish"
    elif ret < -threshold:
        return "bearish"
    return "neutral"


def _is_correct(predicted: str, actual: str) -> Optional[bool]:
    """判断预测是否正确。

    规则：
      - predicted == actual → True（方向一致）
      - predicted != actual 且 actual 非 neutral → False（方向错误）
      - actual == "neutral" 且 predicted == "neutral" → True（正确判断中性）
      - actual == "neutral" 且 predicted 非 neutral → False（误判方向）
      - 缺失任一 → None（不计分）

    设计变更（P0-2）：
      旧逻辑：actual == neutral → return None（丢弃样本）
      新逻辑：actual == neutral 仍参与计分，避免震荡市样本大量流失
    """
    if not predicted or not actual:
        return None
    return predicted == actual


# ═══════════════════════════════════════════════════════════════
# 评估执行
# ═══════════════════════════════════════════════════════════════

def evaluate_pending(days_old: int = 1, market: str = "CNStock") -> Dict[str, Any]:
    """评估所有待验证的决策记录。

    查找 qd_agent_decisions 中没有对应 qd_agent_decision_results 的记录，
    获取 T+1/3/5 实际涨跌，判断决策是否正确，写入 qd_agent_decision_results。

    评估完成后自动触发 update_factor_weights() 和 update_tool_eval()。

    Args:
        days_old: 只评估至少 N 天前的决策（确保有后续行情数据）
        market: 市场类型（用于获取实际涨跌）

    Returns:
        {"evaluated": int, "errors": int, "details": list}
    """
    from app.utils.db import get_db_connection

    stats = {"evaluated": 0, "errors": 0, "details": []}
    cutoff_date = date.today() - timedelta(days=days_old)

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 查找未评估的决策（没有对应 qd_agent_decision_results 记录的）
            cur.execute("""
                SELECT d.id, d.exec_date, d.stock_code, d.stock_name, d.chain_id, d.action
                FROM qd_agent_decisions d
                LEFT JOIN qd_agent_decision_results r ON r.decision_id = d.id
                WHERE d.exec_date <= %s AND r.id IS NULL
                ORDER BY d.exec_date ASC
                LIMIT 100
            """, (cutoff_date,))

            rows = cur.fetchall()

            for row in rows:
                decision_id, exec_date, stock_code, stock_name, chain_id, action = row
                try:
                    _evaluate_single(conn, decision_id, exec_date, stock_code, action, market)
                    stats["evaluated"] += 1
                    stats["details"].append({
                        "decision_id": decision_id,
                        "stock": stock_code,
                        "status": "ok",
                    })
                except Exception as e:
                    stats["errors"] += 1
                    stats["details"].append({
                        "decision_id": decision_id,
                        "stock": stock_code,
                        "status": "error",
                        "error": str(e),
                    })
                    logger.error("[Evaluator] 评估 decision %d 失败: %s", decision_id, e)

            conn.commit()

    except Exception as e:
        logger.error("[Evaluator] 评估任务失败: %s", e)

    logger.info("[Evaluator] 评估完成: %d 条已评估, %d 条失败",
                stats["evaluated"], stats["errors"])

    if stats["evaluated"] > 0:
        try:
            update_factor_weights()
            update_tool_eval()
        except Exception as e:
            logger.warning("[Evaluator] 更新权重/工具评估失败: %s", e)

    return stats


def _evaluate_single(
    conn,
    decision_id: int,
    exec_date: date,
    stock_code: str,
    action: str,
    market: str,
):
    """评估单条决策记录。"""
    cur = conn.cursor()

    # 获取实际涨跌
    actuals = _get_actual_returns(stock_code, exec_date, market)
    if not actuals:
        # 没有后续数据，跳过（不插入 results 记录，下次重试）
        return

    # 判断决策是否正确
    action_to_direction = {"buy": "bullish", "sell": "bearish", "hold": "neutral", "skip": "neutral"}
    predicted_dir = action_to_direction.get(action, "neutral")

    correct_1d = _is_correct(predicted_dir, actuals.get("direction_1d"))
    correct_3d = _is_correct(predicted_dir, actuals.get("direction_3d"))
    correct_5d = _is_correct(predicted_dir, actuals.get("direction_5d"))

    cur.execute("""
        INSERT INTO qd_agent_decision_results
            (decision_id, actual_return_1d, actual_return_3d, actual_return_5d,
             actual_direction_1d, actual_direction_3d, actual_direction_5d,
             correct_1d, correct_3d, correct_5d)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        decision_id,
        actuals.get("return_1d"), actuals.get("return_3d"), actuals.get("return_5d"),
        actuals.get("direction_1d"), actuals.get("direction_3d"), actuals.get("direction_5d"),
        correct_1d, correct_3d, correct_5d,
    ))

    # 子项级验证：每个步骤独立跟实际方向对比，写回数据库
    # 这是分项记分制的核心——每个子项的正确率远比决策整体正确率重要
    cur.execute("""
        SELECT id, step_name, direction, status, score
        FROM qd_agent_decision_steps
        WHERE decision_id = %s
    """, (decision_id,))

    for step_id, step_name, step_dir, step_status, step_score in cur.fetchall():
        # 只验证有效步骤（status=ok 且有方向）
        if step_status != "ok" or not step_dir:
            continue

        # 用 3 日方向作为主要验证基准（中短线交易周期）
        actual_dir = actuals.get("direction_3d")
        step_correct = _is_correct(step_dir, actual_dir)

        # 校准因子：score 越偏离50（越自信），奖惩幅度越大
        # confidence = |score - 50| / 50  → 0~1
        # multiplier = 1 + confidence × 0.05  → 1.00~1.05
        # 作用：微调权重更新速度，不是主驱动力
        calibration_factor = 1.0
        if step_score is not None:
            confidence = abs(step_score - 50) / 50.0  # 0~1
            calibration_factor = 1.0 + confidence * 0.05  # 1.00~1.05

        cur.execute("""
            UPDATE qd_agent_decision_steps
            SET actual_direction = %s, step_correct = %s, calibration_factor = %s
            WHERE id = %s
        """, (actual_dir, step_correct, round(calibration_factor, 4), step_id))


# ═══════════════════════════════════════════════════════════════
# 因子级衰减半衰期
# ═══════════════════════════════════════════════════════════════

# 因子名关键词 → 半衰期（天）映射
# 匹配规则：因子名包含任一关键词即命中，取第一个匹配的半衰期
_FACTOR_HALF_LIFE_RULES = [
    # 政策/消息类 — 信息快速消化，7天半衰期
    (["政策", "policy", "监管", "新规", "法规"], 7),
    (["新闻", "news", "公告", "消息", "舆情"], 7),
    (["解禁", "lockup", "减持", "增持", "质押"], 7),

    # 游资/资金类 — 资金流向变化快，14天半衰期
    (["游资", "hot_money", "龙虎榜", "主力", "席位"], 14),
    (["资金", "fund_flow", "北向", "融资", "融券"], 14),

    # 概念/板块类 — 题材生命周期约3-4周，21天半衰期
    (["概念", "concept", "题材", "板块", "sector", "热点"], 21),

    # 技术指标类 — 市场风格切换周期，30天半衰期
    (["MACD", "macd", "DIF", "DEA", "金叉", "死叉"], 30),
    (["RSI", "rsi", "超买", "超卖"], 30),
    (["KDJ", "kdj", "J值"], 30),
    (["BOLL", "boll", "布林", "通道"], 30),
    (["均线", "MA", "ma", "MA5", "MA10", "MA20", "MA60", "多头排列", "空头排列"], 30),
    (["形态", "pattern", "突破", "反转", "整理", "K线"], 30),

    # 动量类 — 趋势持续性，30天半衰期
    (["动量", "momentum", "趋势", "trend", "强度"], 30),

    # 量价关系类 — 较稳定的统计规律，60天半衰期
    (["量", "volume", "量比", "换手", "放量", "缩量"], 60),

    # 筹码类 — 中长期分布，45天半衰期
    (["筹码", "chip", "持仓", "成本"], 45),
]

_DEFAULT_HALF_LIFE = 30  # 未匹配因子的默认半衰期


def _get_factor_half_life(factor_name: str, default: int = _DEFAULT_HALF_LIFE) -> int:
    """根据因子名推断衰减半衰期。

    匹配规则：因子名包含任一关键词即命中。
    未匹配时返回 default 参数。

    Args:
        factor_name: 因子名（如 "MACD金叉"、"政策利好"）
        default: 未匹配时的默认值

    Returns:
        半衰期（天）
    """
    name_lower = factor_name.lower()
    for keywords, half_life in _FACTOR_HALF_LIFE_RULES:
        for kw in keywords:
            if kw.lower() in name_lower:
                return half_life
    return default


# ═══════════════════════════════════════════════════════════════
# 因子权重更新
# ═══════════════════════════════════════════════════════════════

def update_factor_weights(days: int = 60, decay_half_life_days: int = 30) -> Dict[str, Any]:
    """更新因子权重表（带因子级时间衰减）。

    遍历所有已评估的决策，按 (chain_id, factor_name) 聚合准确率。
    每个因子使用独立的衰减半衰期，基于因子类型自动匹配。

    因子类型 → 半衰期映射：
      政策/消息类（policy/news/lockup）    → 7天（信息快速消化）
      游资/资金类（hot_money/fund_flow）   → 14天（资金流向变化快）
      技术指标类（MACD/RSI/KDJ/BOLL/MA）  → 30天（市场风格切换周期）
      量价关系类（volume/量比/换手）       → 60天（较稳定统计规律）
      概念/板块类（concept/sector）        → 21天（题材生命周期约3-4周）
      动量类（momentum/趋势）              → 30天
      未知/其他                            → 30天（默认）

    衰减公式: weight = 0.5 ^ (days_ago / factor_half_life)

    写入 qd_agent_factor_weights 表（含 decay_half_life 列），供 executor 读取。

    Args:
        days: 回溯天数
        decay_half_life_days: 全局默认半衰期（被因子级配置覆盖）

    Returns:
        {"updated": int}  更新的因子数量
    """
    from app.utils.db import get_db_connection
    import math

    stats = {"updated": 0}
    since = date.today() - timedelta(days=days)
    today = date.today()

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 读取已有因子的半衰期配置（如果数据库有 decay_half_life 列）
            existing_half_lives: Dict[Tuple[str, str], int] = {}
            try:
                cur.execute("SELECT chain_id, factor_name, decay_half_life FROM qd_agent_factor_weights WHERE decay_half_life IS NOT NULL")
                for row in cur.fetchall():
                    existing_half_lives[(row[0], row[1])] = row[2]
            except Exception:
                pass  # 列不存在时忽略（首次运行或未迁移）

            # 获取所有已评估的决策及其步骤因子（包含决策日期用于衰减计算）
            # 核心改动：用子项级正确率（ds.step_correct）替代决策级正确率（r.correct_3d）
            # 分项记分制下，每个子项独立跟实际方向对比，谁对谁加分，谁错谁扣分
            cur.execute("""
                SELECT d.chain_id, ds.step_name, ds.factors, ds.direction,
                       ds.step_correct, ds.calibration_factor, d.exec_date
                FROM qd_agent_decisions d
                JOIN qd_agent_decision_steps ds ON ds.decision_id = d.id
                WHERE d.exec_date >= %s
                  AND ds.status = 'ok'
                  AND ds.step_correct IS NOT NULL
            """, (since,))

            # 聚合: (chain_id, factor_name) → {weighted_correct, weighted_total}
            factor_stats: Dict[Tuple[str, str], Dict[str, float]] = {}

            for chain_id, step_name, factors_json, direction, step_correct, cal_factor, exec_date in cur.fetchall():
                try:
                    factors = json.loads(factors_json) if factors_json else []
                except (json.JSONDecodeError, TypeError):
                    continue

                days_ago = (today - exec_date).days
                cal = cal_factor or 1.0  # 校准因子：1.00~1.05

                for factor in factors:
                    fname = factor.get("name", "")
                    if not fname:
                        continue

                    key = (chain_id, fname)

                    # 确定该因子的半衰期：已有配置 > 因子类型推断 > 全局默认
                    if key in existing_half_lives:
                        hl = existing_half_lives[key]
                    else:
                        hl = _get_factor_half_life(fname, decay_half_life_days)

                    decay_weight = math.pow(0.5, days_ago / max(hl, 1))

                    if key not in factor_stats:
                        factor_stats[key] = {
                            "weighted_correct": 0.0,
                            "weighted_total": 0.0,
                            "raw_total": 0, "half_life": hl,
                        }

                    # 校准因子调节衰减权重：自信+对了/不自信+错了 → 微调幅度 ±5%
                    effective_weight = decay_weight * cal
                    factor_stats[key]["weighted_total"] += effective_weight
                    factor_stats[key]["raw_total"] += 1
                    if step_correct:
                        factor_stats[key]["weighted_correct"] += effective_weight

            # UPSERT
            for (chain_id, fname), s in factor_stats.items():
                total = s["weighted_total"]
                raw_total = s["raw_total"]
                if total < 0.5:  # 衰减后有效样本太少，跳过
                    continue

                # 子项级正确率：基于每个步骤独立验证的结果
                step_accuracy = round(s["weighted_correct"] / total, 4)

                cur.execute("""
                    INSERT INTO qd_agent_factor_weights
                        (chain_id, factor_name, weight, accuracy_1d, accuracy_3d, accuracy_5d,
                         sample_count, decay_half_life, last_updated)
                    VALUES (%s, %s, 1.0, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (chain_id, factor_name)
                    DO UPDATE SET
                        accuracy_1d = EXCLUDED.accuracy_1d,
                        accuracy_3d = EXCLUDED.accuracy_3d,
                        accuracy_5d = EXCLUDED.accuracy_5d,
                        sample_count = EXCLUDED.sample_count,
                        decay_half_life = EXCLUDED.decay_half_life,
                        last_updated = NOW()
                """, (
                    chain_id, fname,
                    step_accuracy,   # 1d 暂用子项级（后续可拆分）
                    step_accuracy,   # 3d — 主验证基准
                    step_accuracy,   # 5d 暂用子项级
                    raw_total,
                    s["half_life"],
                ))
                stats["updated"] += 1

            conn.commit()

    except Exception as e:
        logger.error("[Evaluator] 更新因子权重失败: %s", e)

    logger.info("[Evaluator] 因子权重已更新: %d 个因子 (half_life=%dd)", stats["updated"], decay_half_life_days)
    return stats


# ═══════════════════════════════════════════════════════════════
# 工具评估更新
# ═══════════════════════════════════════════════════════════════

def update_tool_eval(days: int = 60) -> Dict[str, Any]:
    """更新工具评估表。"""
    from app.utils.db import get_db_connection

    stats = {"updated": 0}
    since = date.today() - timedelta(days=days)

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 获取所有已评估的决策及其工具调用
            # 用子项级正确率（ds.step_correct）替代决策级正确率
            cur.execute("""
                SELECT d.chain_id, ds.tools_called, ds.elapsed_ms,
                       ds.step_correct
                FROM qd_agent_decisions d
                JOIN qd_agent_decision_steps ds ON ds.decision_id = d.id
                WHERE d.exec_date >= %s
                  AND ds.status = 'ok'
                  AND ds.step_correct IS NOT NULL
            """, (since,))

            # 聚合: (chain_id, tool_name) → {calls, successes, useful_count, total_latency}
            tool_stats: Dict[Tuple[str, str], Dict[str, float]] = {}

            for chain_id, tools_json, elapsed_ms, step_correct in cur.fetchall():
                try:
                    tools = json.loads(tools_json) if tools_json else []
                except (json.JSONDecodeError, TypeError):
                    continue

                for tool_name in tools:
                    if not tool_name:
                        continue
                    key = (chain_id, tool_name)
                    if key not in tool_stats:
                        tool_stats[key] = {"calls": 0, "successes": 0, "useful": 0, "latency": 0}

                    tool_stats[key]["calls"] += 1
                    tool_stats[key]["successes"] += 1  # tools_called 中的都是成功的
                    tool_stats[key]["latency"] += elapsed_ms or 0
                    if step_correct:
                        tool_stats[key]["useful"] += 1

            # UPSERT
            for (chain_id, tool_name), s in tool_stats.items():
                calls = int(s["calls"])
                if calls == 0:
                    continue

                cur.execute("""
                    INSERT INTO qd_agent_tool_eval
                        (tool_name, chain_id, calls, successes, useful_count,
                         avg_latency_ms, last_updated)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (tool_name, chain_id)
                    DO UPDATE SET
                        calls = EXCLUDED.calls,
                        successes = EXCLUDED.successes,
                        useful_count = EXCLUDED.useful_count,
                        avg_latency_ms = EXCLUDED.avg_latency_ms,
                        last_updated = NOW()
                """, (
                    tool_name, chain_id, calls,
                    int(s["successes"]),
                    int(s["useful"]),
                    round(s["latency"] / calls, 1),
                ))
                stats["updated"] += 1

            conn.commit()

    except Exception as e:
        logger.error("[Evaluator] 更新工具评估失败: %s", e)

    logger.info("[Evaluator] 工具评估已更新: %d 个工具", stats["updated"])
    return stats


# ═══════════════════════════════════════════════════════════════
# 权重调整建议
# ═══════════════════════════════════════════════════════════════

def get_step_weights(chain_id: str, days: int = 30) -> Dict[str, float]:
    """获取链路各步骤的历史准确率权重。"""
    from app.utils.db import get_db_connection

    since = date.today() - timedelta(days=days)
    weights = {}

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    ds.step_name,
                    AVG(CASE WHEN r.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    COUNT(*) as cnt
                FROM qd_agent_decision_steps ds
                JOIN qd_agent_decisions d ON ds.decision_id = d.id
                JOIN qd_agent_decision_results r ON r.decision_id = d.id
                WHERE d.chain_id = %s AND d.exec_date >= %s
                  AND r.correct_3d IS NOT NULL
                  AND ds.status = 'ok'
                GROUP BY ds.step_name
                HAVING COUNT(*) >= 3
            """, (chain_id, since))

            for row in cur.fetchall():
                weights[row[0]] = round(row[1], 3) if row[1] else 0.5

    except Exception as e:
        logger.warning("[Evaluator] 获取步骤权重失败: %s", e)

    return weights


def get_factor_weights_for_chain(chain_id: str) -> Dict[str, float]:
    """获取链路各步骤的因子级权重。

    从 qd_agent_factor_weights 表读取该链路下所有因子的 accuracy_3d，
    按 step_name（通过因子名前缀推断）聚合为步骤级权重。
    返回 {step_name: avg_accuracy_3d}，仅包含 sample_count >= 5 的因子。
    """
    from app.utils.db import get_db_connection

    weights = {}

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            # 直接从 qd_agent_factor_weights 取因子准确率
            cur.execute("""
                SELECT factor_name, accuracy_3d, sample_count
                FROM qd_agent_factor_weights
                WHERE chain_id = %s AND sample_count >= 5
            """, (chain_id,))

            # 因子名 → 步骤名的映射（基于 chain 定义中的步骤名前缀）
            _STEP_PREFIXES = {
                "policy": "policy", "hot_money": "hot_money", "lockup": "lockup",
                "concept": "concept", "momentum": "momentum", "intelligence": "intelligence",
                "technical": "technical", "indicator": "indicator", "screening": "screening",
                "fund_flow": "fund_flow", "backtest": "backtest",
                "bull": "bull_bear_debate", "bear": "bear_rebuttal",
                "market": "market_overview", "hotspot": "hotspots",
            }

            # 按步骤聚合
            step_accs: Dict[str, List[float]] = {}
            for factor_name, acc_3d, sample_count in cur.fetchall():
                if acc_3d is None:
                    continue
                # 推断步骤名
                step_name = None
                for prefix, sname in _STEP_PREFIXES.items():
                    if factor_name.lower().startswith(prefix):
                        step_name = sname
                        break
                if not step_name:
                    # 无法推断的因子归入 technical（通用技术因子）
                    step_name = "technical"

                if step_name not in step_accs:
                    step_accs[step_name] = []
                step_accs[step_name].append(acc_3d)

            # 取每个步骤下因子准确率的均值
            for step_name, accs in step_accs.items():
                if accs:
                    weights[step_name] = round(sum(accs) / len(accs), 3)

    except Exception as e:
        logger.warning("[Evaluator] 获取因子权重失败: %s", e)

    return weights


def get_weight_adjustments(chain_id: str, days: int = 30) -> Dict[str, Any]:
    """输出权重调整建议（供 executor 自动应用）。"""
    from app.utils.db import get_db_connection

    since = date.today() - timedelta(days=days)
    result = {
        "chain_id": chain_id,
        "adjustments": [],      # [{factor_name, current_weight, suggested_weight, reason}]
        "tool_issues": [],      # [{tool_name, useful_rate, issue}]
    }

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 因子权重调整
            cur.execute("""
                SELECT factor_name, weight, accuracy_3d, accuracy_5d, sample_count
                FROM qd_agent_factor_weights
                WHERE chain_id = %s AND sample_count >= 5
                ORDER BY accuracy_3d DESC
            """, (chain_id,))

            for fname, weight, acc_3d, acc_5d, count in cur.fetchall():
                if acc_3d >= 0.65:
                    suggested = 1.3
                    reason = f"3日准确率 {acc_3d:.0%}，建议加权"
                elif acc_3d <= 0.35:
                    suggested = 0.5
                    reason = f"3日准确率仅 {acc_3d:.0%}，建议降权"
                else:
                    suggested = 1.0
                    reason = f"3日准确率 {acc_3d:.0%}，维持默认"

                if abs(suggested - weight) > 0.1:
                    result["adjustments"].append({
                        "factor_name": fname,
                        "current_weight": weight,
                        "suggested_weight": suggested,
                        "reason": reason,
                    })

            # 工具问题
            cur.execute("""
                SELECT tool_name, calls, successes, useful_count, avg_latency_ms
                FROM qd_agent_tool_eval
                WHERE chain_id = %s AND calls >= 5
            """, (chain_id,))

            for tool_name, calls, successes, useful, latency in cur.fetchall():
                success_rate = successes / calls if calls > 0 else 0
                useful_rate = useful / calls if calls > 0 else 0

                if success_rate < 0.7:
                    result["tool_issues"].append({
                        "tool_name": tool_name,
                        "useful_rate": round(useful_rate, 3),
                        "issue": f"成功率仅 {success_rate:.0%}（{successes}/{calls}），工具不稳定",
                    })
                elif useful_rate < 0.3:
                    result["tool_issues"].append({
                        "tool_name": tool_name,
                        "useful_rate": round(useful_rate, 3),
                        "issue": f"有用率仅 {useful_rate:.0%}，工具返回数据对判断几乎无帮助",
                    })

    except Exception as e:
        logger.error("[Evaluator] 获取权重调整建议失败: %s", e)

    return result


# ═══════════════════════════════════════════════════════════════
# Walk-forward 验证
# ═══════════════════════════════════════════════════════════════

def walk_forward_validate(
    chain_id: str,
    train_days: int = 60,
    test_days: int = 10,
) -> Dict[str, Any]:
    """Walk-forward 验证。

    用 train_days 的数据训练权重，用 test_days 的数据验证效果。
    """
    from app.utils.db import get_db_connection

    result = {
        "chain_id": chain_id,
        "train_days": train_days,
        "test_days": test_days,
        "train_accuracy": {},
        "test_accuracy": {},
        "improvement": {},
    }

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            train_start = date.today() - timedelta(days=train_days + test_days)
            train_end = date.today() - timedelta(days=test_days)
            test_start = train_end

            # 训练集准确率
            cur.execute("""
                SELECT
                    AVG(CASE WHEN r.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                    AVG(CASE WHEN r.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    AVG(CASE WHEN r.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d,
                    COUNT(*) as cnt
                FROM qd_agent_decisions d
                JOIN qd_agent_decision_results r ON r.decision_id = d.id
                WHERE d.chain_id = %s
                  AND d.exec_date >= %s AND d.exec_date < %s
                  AND r.correct_1d IS NOT NULL
            """, (chain_id, train_start, train_end))

            row = cur.fetchone()
            if row and row[3] > 0:
                result["train_accuracy"] = {
                    "1d": round(row[0], 3) if row[0] else 0,
                    "3d": round(row[1], 3) if row[1] else 0,
                    "5d": round(row[2], 3) if row[2] else 0,
                    "count": row[3],
                }

            # 测试集准确率
            cur.execute("""
                SELECT
                    AVG(CASE WHEN r.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                    AVG(CASE WHEN r.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    AVG(CASE WHEN r.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d,
                    COUNT(*) as cnt
                FROM qd_agent_decisions d
                JOIN qd_agent_decision_results r ON r.decision_id = d.id
                WHERE d.chain_id = %s
                  AND d.exec_date >= %s
                  AND r.correct_1d IS NOT NULL
            """, (chain_id, test_start))

            row = cur.fetchone()
            if row and row[3] > 0:
                result["test_accuracy"] = {
                    "1d": round(row[0], 3) if row[0] else 0,
                    "3d": round(row[1], 3) if row[1] else 0,
                    "5d": round(row[2], 3) if row[2] else 0,
                    "count": row[3],
                }

            # 计算改进
            for period in ("1d", "3d", "5d"):
                train_acc = result["train_accuracy"].get(period, 0)
                test_acc = result["test_accuracy"].get(period, 0)
                result["improvement"][period] = round(test_acc - train_acc, 3)

    except Exception as e:
        logger.error("[Evaluator] Walk-forward 验证失败: %s", e)

    return result


# ═══════════════════════════════════════════════════════════════
# 综合评估报告
# ═══════════════════════════════════════════════════════════════

def get_eval_report(chain_id: str = None, days: int = 30) -> Dict[str, Any]:
    """获取评估报告。"""
    from app.utils.db import get_db_connection

    since = date.today() - timedelta(days=days)
    result = {"overall": {}, "steps": [], "factors": []}

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            chain_filter = "AND d.chain_id = %s" if chain_id else ""
            params_base = [since] + ([chain_id] if chain_id else [])

            # 总体准确率
            cur.execute(f"""
                SELECT
                    d.chain_id,
                    COUNT(*) as total,
                    AVG(CASE WHEN r.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                    AVG(CASE WHEN r.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    AVG(CASE WHEN r.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d
                FROM qd_agent_decisions d
                JOIN qd_agent_decision_results r ON r.decision_id = d.id
                WHERE d.exec_date >= %s {chain_filter}
                  AND r.correct_1d IS NOT NULL
                GROUP BY d.chain_id
            """, params_base)

            row = cur.fetchone()
            if row:
                result["overall"] = {
                    "chain_id": row[0],
                    "total": row[1],
                    "accuracy": {
                        "1d": round(row[2], 3) if row[2] else 0,
                        "3d": round(row[3], 3) if row[3] else 0,
                        "5d": round(row[4], 3) if row[4] else 0,
                    },
                }

            # 各步骤准确率
            cur.execute(f"""
                SELECT
                    ds.step_name,
                    COUNT(*) as cnt,
                    AVG(CASE WHEN r.correct_1d THEN 1.0 ELSE 0.0 END) as acc_1d,
                    AVG(CASE WHEN r.correct_3d THEN 1.0 ELSE 0.0 END) as acc_3d,
                    AVG(CASE WHEN r.correct_5d THEN 1.0 ELSE 0.0 END) as acc_5d
                FROM qd_agent_decision_steps ds
                JOIN qd_agent_decisions d ON ds.decision_id = d.id
                JOIN qd_agent_decision_results r ON r.decision_id = d.id
                WHERE d.exec_date >= %s {chain_filter}
                  AND r.correct_1d IS NOT NULL AND ds.status = 'ok'
                GROUP BY ds.step_name
                ORDER BY acc_3d DESC
            """, params_base)

            result["steps"] = [
                {
                    "step_name": row[0],
                    "count": row[1],
                    "accuracy": {
                        "1d": round(row[2], 3) if row[2] else 0,
                        "3d": round(row[3], 3) if row[3] else 0,
                        "5d": round(row[4], 3) if row[4] else 0,
                    },
                }
                for row in cur.fetchall()
            ]

            # 因子准确率
            cur.execute(f"""
                SELECT
                    fw.factor_name,
                    fw.accuracy_1d,
                    fw.accuracy_3d,
                    fw.accuracy_5d,
                    fw.sample_count
                FROM qd_agent_factor_weights fw
                WHERE fw.chain_id = %s AND fw.sample_count >= 3
                ORDER BY fw.accuracy_3d DESC
            """, [chain_id] if chain_id else ["evaluate+stock"])

            result["factors"] = [
                {
                    "factor_name": row[0],
                    "accuracy_1d": round(row[1], 3) if row[1] else 0,
                    "accuracy_3d": round(row[2], 3) if row[2] else 0,
                    "accuracy_5d": round(row[3], 3) if row[3] else 0,
                    "sample_count": row[4],
                }
                for row in cur.fetchall()
            ]

    except Exception as e:
        logger.error("[Evaluator] 获取评估报告失败: %s", e)

    return result


# ═══════════════════════════════════════════════════════════════
# 自动评估入口（供定时任务/后台 worker 调用）
# ═══════════════════════════════════════════════════════════════

def auto_evaluate_and_update(
    days_old: int = 1,
    market: str = "CNStock",
    decay_half_life_days: int = 30,
    weight_adjust_days: int = 30,
) -> Dict[str, Any]:
    """自动评估闭环：评估待验证决策 → 更新因子权重 → 更新工具评估 → 生成调整建议。

    这是 Chain 决策系统的"最后一公里"，应由定时任务每日盘后调用一次。

    Args:
        days_old: 只评估至少 N 天前的决策（确保有 T+N 数据）
        market: 市场类型
        decay_half_life_days: 因子权重时间衰减半衰期
        weight_adjust_days: 权重调整建议的回溯天数

    Returns:
        {
            "evaluation": {...},
            "factor_weights": {...},
            "tool_eval": {...},
            "adjustments": {...},
        }
    """
    result = {}

    # Step 1: 评估待验证决策（获取 T+1/3/5 实际涨跌）
    try:
        eval_stats = evaluate_pending(days_old=days_old, market=market)
        result["evaluation"] = eval_stats
        logger.info("[AutoEval] 评估完成: %d 条已评估, %d 条失败",
                     eval_stats.get("evaluated", 0), eval_stats.get("errors", 0))
    except Exception as e:
        logger.error("[AutoEval] 评估步骤失败: %s", e)
        result["evaluation"] = {"evaluated": 0, "errors": 1, "error": str(e)}

    # Step 2: 更新因子权重（带时间衰减）
    try:
        fw_stats = update_factor_weights(days=60, decay_half_life_days=decay_half_life_days)
        result["factor_weights"] = fw_stats
    except Exception as e:
        logger.error("[AutoEval] 因子权重更新失败: %s", e)
        result["factor_weights"] = {"updated": 0, "error": str(e)}

    # Step 3: 更新工具评估
    try:
        te_stats = update_tool_eval(days=60)
        result["tool_eval"] = te_stats
    except Exception as e:
        logger.error("[AutoEval] 工具评估更新失败: %s", e)
        result["tool_eval"] = {"updated": 0, "error": str(e)}

    # Step 4: 生成权重调整建议（仅日志记录，不自动应用）
    try:
        # 遍历所有链路生成建议
        from app.agent.chain.chains import list_chains
        all_adjustments = {}
        for chain_def in list_chains():
            adj = get_weight_adjustments(chain_def.chain_id, days=weight_adjust_days)
            if adj.get("adjustments") or adj.get("tool_issues"):
                all_adjustments[chain_def.chain_id] = adj
        result["adjustments"] = all_adjustments
        if all_adjustments:
            logger.info("[AutoEval] 权重调整建议: %d 条链路有待调整", len(all_adjustments))
    except Exception as e:
        logger.error("[AutoEval] 生成调整建议失败: %s", e)
        result["adjustments"] = {"error": str(e)}

    return result


def get_chain_eval_stats(chain_id: str = None) -> Dict[str, Any]:
    """获取链路评估统计（供门控逻辑使用）。

    Returns:
        {
            "total_decisions": int,         # 总决策数
            "evaluated_decisions": int,     # 已评估数
            "overall_accuracy_3d": float,   # 3日准确率
            "min_factor_sample": int,       # 最小因子样本数
            "ready_for_decision": bool,     # 是否达到决策门槛
        }
    """
    from app.utils.db import get_db_connection

    result = {
        "total_decisions": 0,
        "evaluated_decisions": 0,
        "overall_accuracy_3d": 0.0,
        "min_factor_sample": 0,
        "ready_for_decision": False,
    }

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            chain_filter = "AND d.chain_id = %s" if chain_id else ""
            params = [chain_id] if chain_id else []

            # 总决策数和已评估数
            cur.execute(f"""
                SELECT
                    COUNT(*) as total,
                    COUNT(r.id) as evaluated
                FROM qd_agent_decisions d
                LEFT JOIN qd_agent_decision_results r ON r.decision_id = d.id
                WHERE 1=1 {chain_filter}
            """, params)

            row = cur.fetchone()
            if row:
                result["total_decisions"] = row[0]
                result["evaluated_decisions"] = row[1]

            # 3日准确率
            if result["evaluated_decisions"] > 0:
                cur.execute(f"""
                    SELECT AVG(CASE WHEN r.correct_3d THEN 1.0 ELSE 0.0 END)
                    FROM qd_agent_decisions d
                    JOIN qd_agent_decision_results r ON r.decision_id = d.id
                    WHERE r.correct_3d IS NOT NULL {chain_filter}
                """, params)
                acc = cur.fetchone()
                if acc and acc[0] is not None:
                    result["overall_accuracy_3d"] = round(float(acc[0]), 3)

            # 最小因子样本数
            if chain_id:
                cur.execute("""
                    SELECT COALESCE(MIN(sample_count), 0)
                    FROM qd_agent_factor_weights
                    WHERE chain_id = %s AND sample_count > 0
                """, (chain_id,))
                min_sample = cur.fetchone()
                if min_sample:
                    result["min_factor_sample"] = min_sample[0]

            # 门控判断：至少 10 条已评估决策才允许 BUY/SELL
            result["ready_for_decision"] = result["evaluated_decisions"] >= 10

    except Exception as e:
        logger.warning("[Evaluator] 获取评估统计失败: %s", e)

    return result


# ═══════════════════════════════════════════════════════════════
# 后台 Worker（定时评估 + 健康监控）
# ═══════════════════════════════════════════════════════════════

_chain_eval_thread = None
_chain_eval_stop = None

# Worker 健康状态（内存，进程重启重置）
_worker_health = {
    "last_run_at": None,            # 上次运行时间
    "last_success_at": None,        # 上次成功时间
    "last_error": None,             # 上次错误信息
    "consecutive_failures": 0,      # 连续失败次数
    "total_runs": 0,                # 总运行次数
    "total_successes": 0,           # 总成功次数
    "total_failures": 0,            # 总失败次数
}

_BASE_INTERVAL = 4 * 3600          # 基础间隔 4 小时
_MAX_INTERVAL = 24 * 3600          # 最大间隔 24 小时
_MAX_CONSECUTIVE_FAILURES = 5      # 连续失败 5 次后只打 ERROR 日志


def get_worker_health() -> Dict[str, Any]:
    """获取 worker 健康状态（供 API 查询）。"""
    h = dict(_worker_health)
    h["is_alive"] = _chain_eval_thread is not None and _chain_eval_thread.is_alive()
    h["current_interval"] = _calc_interval()
    return h


def _calc_interval() -> int:
    """计算当前等待间隔（指数退避）。"""
    failures = _worker_health["consecutive_failures"]
    if failures <= 0:
        return _BASE_INTERVAL
    # 指数退避：4h → 8h → 16h → 24h（封顶）
    interval = _BASE_INTERVAL * (2 ** min(failures, 3))
    return min(interval, _MAX_INTERVAL)


def start_chain_eval_worker():
    """启动 Chain 决策评估后台 worker。

    功能：
      - 每 4 小时运行一次 auto_evaluate_and_update()
      - 连续失败时指数退避（4h → 8h → 16h → 24h）
      - 成功后重置为 4h
      - 连续失败 >= 5 次打 ERROR 级别日志
      - 健康状态可通过 get_worker_health() 查询

    与 FastAnalysis 的 reflection worker 并列，但服务 Chain/Agent 决策系统。
    """
    global _chain_eval_thread, _chain_eval_stop

    import threading
    import time as _time

    if _chain_eval_thread is not None and _chain_eval_thread.is_alive():
        logger.info("[ChainEvalWorker] 已在运行，跳过")
        return

    _chain_eval_stop = threading.Event()

    def _worker():
        _time.sleep(60)  # 启动延迟，让服务就绪

        while not _chain_eval_stop.is_set():
            _worker_health["total_runs"] += 1
            _worker_health["last_run_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")

            try:
                logger.info("[ChainEvalWorker] 开始自动评估...")
                result = auto_evaluate_and_update(days_old=1)
                eval_stats = result.get("evaluation", {})
                evaluated = eval_stats.get("evaluated", 0)
                errors = eval_stats.get("errors", 0)

                # 判定成功：至少有评估结果或无待评估记录
                if errors == 0:
                    _worker_health["consecutive_failures"] = 0
                    _worker_health["total_successes"] += 1
                    _worker_health["last_success_at"] = _worker_health["last_run_at"]
                    _worker_health["last_error"] = None
                    logger.info(
                        "[ChainEvalWorker] 完成: evaluated=%d errors=%d",
                        evaluated, errors,
                    )
                else:
                    raise RuntimeError(f"evaluate_pending 返回 {errors} 个错误")

            except Exception as e:
                _worker_health["consecutive_failures"] += 1
                _worker_health["total_failures"] += 1
                _worker_health["last_error"] = str(e)

                if _worker_health["consecutive_failures"] >= _MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        "[ChainEvalWorker] 连续失败 %d 次！上次错误: %s",
                        _worker_health["consecutive_failures"], e, exc_info=True,
                    )
                else:
                    logger.warning("[ChainEvalWorker] 评估失败: %s", e)

            # 等待下次运行（指数退避）
            interval = _calc_interval()
            if interval > _BASE_INTERVAL:
                logger.info(
                    "[ChainEvalWorker] 退避: 下次运行在 %d 秒后（连续失败 %d 次）",
                    interval, _worker_health["consecutive_failures"],
                )
            _chain_eval_stop.wait(timeout=interval)

    _chain_eval_thread = threading.Thread(
        target=_worker, daemon=True, name="chain-eval-worker",
    )
    _chain_eval_thread.start()
    logger.info("[ChainEvalWorker] 后台评估 worker 已启动（基础间隔 %d 秒）", _BASE_INTERVAL)


def stop_chain_eval_worker():
    """停止后台评估 worker。"""
    global _chain_eval_stop
    if _chain_eval_stop:
        _chain_eval_stop.set()
