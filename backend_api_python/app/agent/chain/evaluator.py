# -*- coding: utf-8 -*-
"""
Evaluator — 回溯评估引擎（重写版）。

基于 qd_agent_traces 表 + qd_agent_weights。

核心流程（每日盘后自动运行）：
  evaluate_pending()      → 按 timeframe 取实际行情，写回 qd_agent_traces
  update_weights()        → 统一更新 skill + factor 权重（原 update_skill_weights / update_factor_weights 已合并）
  auto_evaluate()         → 自动闭环

核心指标：单位时间期望收益率（不是胜率）
  return_per_day = (win_rate × avg_win - loss_rate × avg_loss) / avg_hold_days

纯 SQL + 数学，0 token 消耗，不涉及 agent。
"""
from __future__ import annotations

import json
import os
from ..log import logger
import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from .schema import DIRECTION_THRESHOLD, Direction, classify_return, is_direction_correct
from . import store
from . import label_spec as _ls
# ═══════════════════════════════════════════════════════════════
# timeframe → 验证窗口映射
# ═══════════════════════════════════════════════════════════════
# ★ 2026-10-13（v5 P1-1 收口）：本文件**不再自持**任何 horizon→days 映射，
#   全部委派 `chain/label_spec.HOLDS`（唯一真源）。
#
#   此前 `_TIMEFRAME_DAYS`（1M=22）与 `HOLD_LABELS`（1M=20）两处各写一份且数值
#   不同 —— 改一处忘一处即**静默漂移**。当时判断"两者语义不同（自然月 vs 交易日），
#   不许合并"，**这个判断已查证推翻**：
#     · 消费端按**交易日**解释 —— `_maturity_days` 注释原文「hold_days 按交易日计」，
#       且 `_get_actual_return` 用 `base_idx + hold_days` 偏移 **K 线根数**；
#     · 22 与表内 `1Y=252` 不自洽（22×12=264 > 252，一年没那么多交易日）；
#     · 影响面实测为零：`qd_agent_traces` 根节点 timeframe **恒为空**（1256 条），
#       22/66/252 从未被消费。
#   ⇒ 已统一为 `HOLDS`（1M=21 / 3M=63，与 1Y=252 自洽）。详见 label_spec 模块头。

_TIMEFRAME_DAYS = _ls.HOLDS                  # 单位：**交易日**（唯一真源）
HOLD_LABELS = _ls.HOLD_LABELS                # 天数 → 标签（先到先得）
LABEL_HOLDS: Dict[str, int] = {v: k for k, v in HOLD_LABELS.items()}

_DEFAULT_HOLD_DAYS = _ls.DEFAULT_HOLD_DAYS   # 标签缺失/无法识别时的默认值


def _get_hold_days(timeframe: str) -> int:
    """timeframe → 验证用持有**交易日**数。

    与 claim 的 T+N **同一套口径**（`label_spec.HOLDS`），不再分两套。
    未知/缺失标签兜底 `_DEFAULT_HOLD_DAYS` —— 兜底点仅 `resolve_hold_days` 一处。
    """
    return _ls.resolve_hold_days(timeframe)


# 注：`HOLD_LABELS` / `LABEL_HOLDS` 已在上方随 `_TIMEFRAME_DAYS` 一并委派
#     到 `chain/label_spec`（v5 P1-1）。此处不再重复定义。


def _maturity_days(hold_days: int) -> int:
    """持有交易日数 → 可以定论所需的**自然日**下界（视界门，2026-09-27 修 E2）。

    hold_days 按交易日计（_TIMEFRAME_DAYS），而 exec_date / CURRENT_DATE 是自然日
    口径，两者必须换算：A 股一年约 244 交易日 / 365 自然日 ≈ 1.496，取 1.5；
    +2 天用于覆盖周末与节假日（周五做的 T+1 决策，要到下周一才有第 1 根 K 线）。
    """
    return int(hold_days * 1.5) + 2
# ═══════════════════════════════════════════════════════════════
# 实际行情获取
# ═══════════════════════════════════════════════════════════════

def _get_actual_return(
    stock_code: str,
    from_date: date,
    hold_days: int,
    market: str = "CNStock",
    with_extremes: bool = False,
    extra_holds: Optional[List[int]] = None,
) -> Optional[Dict[str, Any]]:
    """获取股票实际涨跌数据。

    Args:
        stock_code: 股票代码
        from_date: 决策日期
        hold_days: 持有天数（**主窗口**）
        market: 市场类型
        with_extremes: 是否附带持有窗口内的最高/最低价（2026-10-01 追责系统 v1.1 加）。
            旧链路（T+N 收益验证）不需要极值，默认 False ⇒ **返回值与行为完全不变**；
            新链路的 `level` 型 claim（"触及目标价 / 不跌破支撑"）必须看窗口极值才能判，
            故在此**同一处**取，而不是在 resolver 里另写一份取数逻辑
            （两套取数口径必然漂移，是本项目的老教训）。
        extra_holds: **附加窗口**的持有天数（v5 D3 方案 D，2026-10-13）。
            例如主窗口 T+3 时传 `[1]` ⇒ 一次取数同时给出 T+1 结果，落在返回值的
            `windows` 里。**不传则行为与改动前完全一致**（旧链路零影响）。
            ★ 之所以要"一次取数算多窗口"：本函数每次调用都会重新 `get_kline`
              （网络 IO），若让调用方对同一标的调两次，成本真翻倍；而本函数一次
              就拉回整段 K 线，T+1 的 exit 就是 `base_idx + 1`，**数据本来就在**。

    Returns:
        {"pnl_pct": float, "hold_days": int, "direction": str,
         "exit_date": str, "truncated": bool, "hold_days_expected": int,
         "windows": {str(hold_days): <同构子结果>}} 或 None
        with_extremes=True 时额外含 {"high": float, "low": float}
        ★ `windows` 的键是**持有天数的十进制字符串**（"1"/"3"），
          由 `chain/resolver` 映射成 horizon 标签（"T+1"/"T+3"）——
          本函数只认天数，不认标签，避免把标签语义渗进通用取数层。
    """
    try:
        from app.data_sources.cn_stock import CNStockDataSource
        from datetime import datetime as _dt

        # 直接走底层数据源 cn_stock（agent_get_kline 工具即其封装 + 归一）。
        # 不再 import 工具包装层，避免破坏拔插式单一真相源。
        # ── 取数窗口必须覆盖 from_date（2026-10-13 修复，v5 P0-3）────────────
        # 原实现固定取 max(hold_days + 10, 1) 根（T+3 ⇒ 仅 13 根），且是「截至当前」取的。
        # 当 from_date 早于这 13 根时，下面「第一个 k_date >= from_date」**恒为第 0 根**
        # ⇒ base_idx = 0 ⇒ exit_date 变成「窗口内第 hold_days 根」，与 from_date 完全脱钩。
        # 又因「当前」每天都在变，同一历史日期在不同日子调用会得到不同收益 ——
        # 这是「稳定地算错 + 随调用时刻漂移」，不报错，只安静污染每一个 T+N 标签。
        # 实测：from_date=2026-08-29 / hold_days=3 ⇒ 期望 exit=2026-09-02，实际 2026-09-22。
        # 修法：按交易日历算出 from_date 距今的真实交易日跨度，据此取足够长的窗口。
        _need = max(hold_days + 10, 1)
        try:
            from app.utils import trading_calendar as _tc

            _span = int(_tc.trading_days_count(
                from_date.isoformat(), _dt.now().date().isoformat()) or 0)
            if _span > 0:
                _need = _span + hold_days + 10
        except Exception as _e:
            logger.debug("[Evaluator] 交易日历不可用，退回默认窗口: %s", _e)
        # 上限保护：防止 from_date 极早时取回海量 K 线（阈值可配，禁止硬编码语义）
        _cap = int(os.getenv("QD_EVAL_KLINE_MAX", "500"))
        if _need > _cap:
            _need = _cap
        raw = CNStockDataSource().get_kline(stock_code, "1D", _need)

        def _ts_to_date(ts):
            # ★ 必须带年份（%Y-%m-%d）。cn_stock 的 1D 行只有 time（无 date 字段），
            #   此前回退成 "%m-%d" → 下游 date.fromisoformat("09-28") 恒 ValueError
            #   → base_idx 永远 None → 本函数恒返回 None → 每条待验证记录都记一次
            #   失败，5 次后被 query_pending_verify 的毒丸机制置 'unverifiable' 永久
            #   出队。实测库里 874 条 root 已是 unverifiable、root 的 pnl_pct 与
            #   skill/tool 层的 correct **一条都没写进去**、qd_agent_weights 只有
            #   tool 层 58 行且 sample_count 全 0 —— 整条 T+N 闭环就是死在这里。
            try:
                return _dt.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
            except Exception:
                return str(ts)

        # 规整成 evaluator 下游依赖的 {t,o,h,l,c,v} 形态。
        # 注：键名与 agent_get_kline 一致，但 t **必须**是完整日期——工具侧 "%m-%d"
        # 只用于给模型展示，本函数的 t 要被 date.fromisoformat 回解析。
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

        # ── 单窗口计算（内部闭包）：主窗口与附加窗口**共用这一份逻辑** ──────
        # 抽出来是为了让"多窗口"与"单窗口"走**完全相同**的算数口径 ——
        # 若另写一份给 windows 用，两套口径必然漂移（老教训，不重犯）。
        def _window(hold: int) -> Optional[Dict[str, Any]]:
            """在**已取回的同一份** klines 上算单个 hold 的窗口结果。

            返回 None 表示该 hold 算不出来（数据不足/收盘价为 0），
            调用方决定是「整体失败」还是「仅该窗口缺席」。
            """
            if hold <= 0:
                return None
            ei = min(base_idx + hold, len(klines) - 1)
            if ei <= base_idx:
                return None
            ec = klines[ei].get("c", 0) if isinstance(klines[ei], dict) else 0
            if not ec:
                return None
            pct = round((ec - base_close) / base_close * 100, 2)
            w: Dict[str, Any] = {
                "pnl_pct": pct,
                "hold_days": ei - base_idx,
                "hold_days_expected": hold,
                # ★ 视界门②：实际根数 < 应有根数 ⇒ 窗口不完整，**不得定论**
                "truncated": bool(ei - base_idx < hold),
                "direction": classify_return(pct / 100),
                "exit_date": (klines[ei].get("t", "")[:10]
                              if isinstance(klines[ei], dict) else ""),
            }
            if with_extremes:
                # 窗口 = (base_idx, ei]，**不含**决策日那根（预测是收盘后做出的）
                seg = klines[base_idx + 1: ei + 1]
                highs = [float(k.get("h") or 0) for k in seg if isinstance(k, dict)]
                lows = [float(k.get("l") or 0) for k in seg if isinstance(k, dict)]
                w["high"] = round(max(highs), 2) if highs else round(float(ec), 2)
                w["low"] = round(min(lows), 2) if lows else round(float(ec), 2)
                w["base_close"] = round(float(base_close), 2)
                w["exit_close"] = round(float(ec), 2)
            return w

        out = _window(hold_days)
        if out is None:
            return None

        # ── 附加窗口（v5 D3 方案 D）：**不再取数**，只在已取回的 klines 上多算几次
        #    ⇒ 边际成本近零（省掉的是一整次 get_kline 网络往返）。
        #    主窗口恒在；某个附加窗口算不出来就**缺席**，不牵连主窗口。
        wins: Dict[str, Dict[str, Any]] = {}
        want = {hold_days}
        for _h in (extra_holds or []):
            try:
                _hi = int(_h)
            except (TypeError, ValueError):
                continue
            if _hi > 0:
                want.add(_hi)
        for _h in sorted(want):
            _w = _window(_h)
            if _w is not None:
                wins[str(_h)] = _w
        out["windows"] = wins
        return out

    except Exception as e:
        logger.warning("[Evaluator] 获取实际涨跌失败 %s: %s", stock_code, e)
        return None
# ═══════════════════════════════════════════════════════════════
# 评估执行
# ═══════════════════════════════════════════════════════════════

def _bump_eval_failure(root_id: int):
    """累计单条记录的评估失败次数（写 qd_agent_traces.error，'eval_failed:N' 前缀）。

    毒丸治理配套（审计 P0-1 断点C）：取不到行情的记录（退市/停牌/代码错误）此前
    会被静默跳过并永久占用评估队列。失败次数 >= 5 时由 store 侧置 unverifiable。
    """
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE qd_agent_traces
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

    查找 qd_agent_traces 中 exit_date IS NULL 的根节点，
    按 timeframe 取实际行情，写回验证结果。

    Args:
        days_old: 只评估至少 N 天前的决策
        market: 市场类型

    Returns:
        {"evaluated": int, "errors": int, "details": list,
         "not_mature": int, "truncated": int}
        not_mature = 持有期未走完而跳过；truncated = 数据缺口导致窗口被截断而跳过。
    """
    stats = {"evaluated": 0, "errors": 0, "details": [],
             "not_mature": 0, "truncated": 0}
    today = date.today()

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

            # ── 视界门 ①：持有期未走完不得定论（2026-09-27 修 E2）──
            # 旧行为：query_pending_verify 统一 days_old=1（只够 T+1），而
            # _get_actual_return 里 exit_idx = min(base_idx+hold_days, len-1) 会把窗口
            # 静默截断到"现有最后一根 K 线"，于是 T+5/1M/3M/1Y 在次日就被 1 天的窗口
            # 定了论；exit_date 一写入该记录即永久出队（WHERE exit_date IS NULL），
            # 之后永不再验 → 权重/酿造信号/win_rate 全被错误标签污染。
            # hold_days 是**交易日**数，换算自然日下界（1 交易日 ≈ 1.5 自然日，
            # +2 覆盖周末/节假日）。未到视界只跳过，**不计失败**——否则连跳 5 次
            # 会被 query_pending_verify 误判 unverifiable 永久出队。
            if exec_date + timedelta(days=_maturity_days(hold_days)) > today:
                stats["not_mature"] += 1
                continue

            actual = _get_actual_return(stock_code, exec_date, hold_days, market)
            if not actual:
                # 失败计数累计到 error 列（'eval_failed:N' 前缀），供
                # store.query_pending_verify 在 >=5 次后将记录置 unverifiable 出队。
                # 不写 exit_date（未来 K 线补齐后仍可回补验证）。
                _bump_eval_failure(root_id)
                continue

            # ── 视界门 ②：窗口被截断不得定论（2026-09-27 修 E2）──
            # 时间已到但 K 线仍不足（长期停牌/退市/数据缺口）时，_get_actual_return
            # 依旧会用"最后一根"交卷。实际持有根数 < 应有根数即判定被截断：不写
            # exit_date（留待数据补齐后回补），计入失败次数走毒丸治理（连续 5 次出队）。
            if int(actual.get("hold_days") or 0) < hold_days:
                _bump_eval_failure(root_id)
                stats["truncated"] += 1
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

    # 防御纵深（2026-09-27 修 E1）：pnl_pct 缺失的行一律不入统计。
    # 旧实现直接 sum(t["pnl_pct"])，遇 None 抛 TypeError 并炸穿整个权重主链
    # （根因是取错行，已改为取根行；此处防止历史 NULL 行再次炸链）。
    def _has_pnl(t: Dict) -> bool:
        return isinstance(t.get("pnl_pct"), (int, float))

    correct_trades = [t for t in trades if t.get("correct") is True and _has_pnl(t)]
    wrong_trades = [t for t in trades if t.get("correct") is False and _has_pnl(t)]
    total = len(correct_trades) + len(wrong_trades)

    if total == 0:
        return {"weight": 1.0, "win_rate": 0, "avg_pnl_pct": 0,
                "avg_hold_days": 1, "return_per_day": 0, "sample_count": 0}

    win_rate = len(correct_trades) / total
    avg_win = (sum(t["pnl_pct"] for t in correct_trades) / len(correct_trades)) if correct_trades else 0
    avg_loss = abs(sum(t["pnl_pct"] for t in wrong_trades) / len(wrong_trades)) if wrong_trades else 0
    # `t.get("hold_days", 3)` 对"键存在但值为 None"仍返回 None → sum 同样会炸，
    # 故用 `or 3` 兜底（与 pnl 同一道防线）。
    avg_hold = sum((t.get("hold_days") or 3) for t in trades) / len(trades)
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

    一次扫描 qd_agent_traces，同时产出：
      1. skill 层权重（按单位时间收益率）
      2. factor 层权重（带时间衰减的准确率）
      3. tool 层权重（按工具参与链路的 correct 率，含失败样本）
      4. chain 层权重（链路 correct 率，2026-09-24 P3/TODO-1 补全三层追责）

    自动同步 registry（双向）：
      - 新 Skill/新工具 → INSERT 工厂默认值（权重 1.0，sample_count=0）；
      - 已删除的 Skill/工具 → DELETE 对应行（不再人工介入）。
    """
    from app.utils.db import get_db_connection

    stats = {"synced": 0, "skill_updated": 0, "factor_updated": 0, "factor_cleaned": 0,
             "tool_updated": 0, "tool_synced": 0, "tool_cleaned": 0, "skill_cleaned": 0,
             "tool_migrated": 0, "chain_updated": 0}
    since = date.today() - timedelta(days=days)
    today = date.today()

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # ① 同步 semantics：新 Skill 自动 INSERT 工厂默认值
            cur.execute("SELECT name FROM qd_agent_weights WHERE layer = 'skill'")
            existing_skills = {row['name'] for row in cur.fetchall()}

            from ..skills_adapter import QDSkillAdapter
            adapter = QDSkillAdapter()
            for skill_info in adapter.list_skills():
                name = skill_info["name"]
                if name not in existing_skills:
                    info = adapter.get(name)
                    # SkillInfo 无 default_weight 字段（2026-09-25 B1）：新技能默认 1.0，与下方 tool 行 INSERT 口径一致。
                    default_w = 1.0
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
                from ..tools._base import ToolProvider as _TP
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
                    # 工具归并的权重迁移（2026-09-27）：旧名→新名，样本量加权并入后再删旧行，
                    # 避免 EvalNode 历史学习被冷启动清零。多旧名并入同新名时逐个累加。
                    from ..tools._base import TOOL_ALIAS
                    target = TOOL_ALIAS.get(name)
                    if target and target in live_tools:
                        cur.execute("""
                            SELECT weight, win_rate, sample_count FROM qd_agent_weights
                            WHERE layer = 'tool' AND name = %s
                        """, (name,))
                        old = cur.fetchone()
                        cur.execute("""
                            SELECT weight, win_rate, sample_count FROM qd_agent_weights
                            WHERE layer = 'tool' AND name = %s
                        """, (target,))
                        new = cur.fetchone()
                        if old and new:
                            n_old = int(old.get("sample_count") or 0)
                            n_new = int(new.get("sample_count") or 0)
                            n_all = n_old + n_new
                            if n_old > 0 and n_all > 0:
                                w = ((old.get("weight") or 1.0) * n_old
                                     + (new.get("weight") or 1.0) * n_new) / n_all
                                wr_old, wr_new = old.get("win_rate"), new.get("win_rate")
                                wr = (wr_old * n_old + wr_new * n_new) / n_all \
                                    if wr_old is not None and wr_new is not None else None
                                cur.execute("""
                                    UPDATE qd_agent_weights
                                    SET weight = %s, win_rate = COALESCE(%s, win_rate),
                                        sample_count = %s, last_updated = NOW()
                                    WHERE layer = 'tool' AND name = %s
                                """, (w, wr, n_all, target))
                                stats["tool_migrated"] += 1
                                logger.info("[Evaluator] 工具权重迁移: %s → %s（样本 %d+%d）",
                                            name, target, n_old, n_new)
                    cur.execute("DELETE FROM qd_agent_weights WHERE layer = 'tool' AND name = %s", (name,))
                    stats["tool_cleaned"] += 1
                    logger.info("[Evaluator] 已删除工具，权重行清理: %s", name)

            # ② 一次扫描 qd_agent_traces，同时聚合 skill 和 factor 数据
            # ⚠️ pnl_pct / hold_days 必须取**根节点** r（2026-09-27 修 E1）：
            # store.update_verify_results 的 UPDATE 带 `AND parent_id IS NULL`，只写根行；
            # skill 行的 pnl_pct 恒为 NULL。旧实现取 t.pnl_pct → _calc_weight 里
            # sum(None) 抛 TypeError → 被外层 except 吞掉 → commit 永远到不了，
            # skill/factor/tool/chain 四层权重全部冻结。correct 仍是 skill 行口径
            # （update_skill_verify 写子节点），保持 t.correct。
            cur.execute("""
                SELECT t.name as skill_name, t.factors, r.pnl_pct,
                       r.hold_days, t.correct, r.exec_date
                FROM qd_agent_traces t
                JOIN qd_agent_traces r ON r.id = t.root_id
                WHERE t.layer = 'skill'
                  AND t.status = 'ok'
                  AND t.correct IS NOT NULL
                  AND r.pnl_pct IS NOT NULL
                  AND r.exec_date >= %s
            """, (since,))

            # 护栏 2 真实现（2026-09-24 提智 0.3）：修订过的技能只统计**新版本**产出的 run
            # （exec_date ≥ 修订时刻）——设计 §3.17“新版本重新积累”此前只写了权重行 reset，
            # 本轮 update_weights 又用 90 日窗全量重算，重置恒被覆盖（护栏 2 名存实亡的一半）。
            from .store import get_skill_revisions as _gsrs
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
                    -- qd_agent_weights 无 id 列：必须自带 RETURNING，否则游标兼容层
                    -- （app.utils.db_postgres.PostgresCursor.execute 对"简单 INSERT…VALUES"
                    --   自动追加 RETURNING id）会让本语句变 UndefinedColumn、整段回滚
                    RETURNING name
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
                    -- 同 skill 层：无 id 列，必须自带 RETURNING 抑制兼容层自动追加
                    RETURNING name
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
                FROM qd_agent_traces child
                JOIN qd_agent_traces root ON child.root_id = root.id
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
                    -- 同 skill 层：无 id 列，必须自带 RETURNING 抑制兼容层自动追加
                    RETURNING name
                """, (tname, weight, wr, n))
                stats["tool_updated"] += 1

            # ⑦ chain 层权重（同表 layer='chain'，2026-09-24 提智 P3/TODO-1）：
            # 三层追责（Layer.CHAIN/SKILL/TOOL）此前只记账不加权（已知局限 G1）——
            # 本段把链路（root 层 name=domain+verb+noun）的 correct 率折成权重，
            # planner 侧消费点 = task_agent._plan 的【链路权重提示】（与工具权重同款）。
            # 同步口径：只算有定论（correct IS NOT NULL）的链；含失败链路（链坏了
            # 也是权重信号，与 tool 层同款）；低样本不改权重（防误判）。
            # name 含 'unknown' 的不可归类链不产权重行（与酿造候选同口径排除）。
            from .store import _sql_chain_norm
            _nk = _sql_chain_norm("t.name")
            cur.execute("""
                SELECT %s AS chain_name,
                       COUNT(*) AS n,
                       AVG(CASE WHEN t.correct THEN 1.0 ELSE 0.0 END) AS win_rate
                FROM qd_agent_traces t
                WHERE t.layer = 'chain'
                  AND t.correct IS NOT NULL
                  AND t.exec_date >= %%s
                  AND position('unknown' in t.name) = 0
                GROUP BY %s
            """ % (_nk, _nk), (since,))
            for row in cur.fetchall():
                cname, n, wr = row["chain_name"], int(row["n"]), float(row["win_rate"])
                weight = 1.0 if n < 10 else round(max(0.5, min(2.0, 1.0 + (wr - 0.5) * 2.0)), 4)
                cur.execute("""
                    INSERT INTO qd_agent_weights
                        (layer, name, skill_name, weight, win_rate, sample_count, last_updated)
                    VALUES ('chain', %s, NULL, %s, %s, %s, NOW())
                    ON CONFLICT (layer, name, COALESCE(skill_name, ''))
                    DO UPDATE SET
                        weight = EXCLUDED.weight,
                        win_rate = EXCLUDED.win_rate,
                        sample_count = EXCLUDED.sample_count,
                        last_updated = NOW()
                    -- 同 skill 层：无 id 列，必须自带 RETURNING 抑制兼容层自动追加
                    RETURNING name
                """, (cname, weight, wr, n))
                stats["chain_updated"] = stats.get("chain_updated", 0) + 1

            conn.commit()

    except Exception as e:
        logger.error("[Evaluator] 更新权重失败: %s", e)

    logger.info("[Evaluator] 权重更新: skill 同步%d/更新%d/清理%d, tool 同步%d/更新%d/清理%d, "
                "factor %d/清理 %d, chain %d",
                stats["synced"], stats["skill_updated"], stats["skill_cleaned"],
                stats["tool_synced"], stats["tool_updated"], stats["tool_cleaned"],
                stats["factor_updated"], stats["factor_cleaned"],
                stats.get("chain_updated", 0))
    # 迭代旁支调度（重设计 §2.6，2026-09-19）：链式门控 · 第 2 级——仅当权重确有更新
    # （skill/factor/tool 任一 *_updated>0）才把低于阈值的 auto_ 技能交给修订器。
    # 护栏/队列全在 skill_brewer 侧；本处只一行调度（模块边界 §11.1）。
    _updated = stats["skill_updated"] + stats["factor_updated"] + stats["tool_updated"]
    if _updated > 0:
        try:
            from .skill_brewer import maybe_revise
            from .store import get_skill_weight_rows as _gswr
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
                FROM qd_agent_traces
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
                FROM qd_agent_traces t
                JOIN qd_agent_traces r ON r.id = t.root_id
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

    # ── v4 追责判定（claims → resolutions）──────────────────────────────────
    # 【转接背景】2026-10-01：旧系统 `agent_smolagents/chain/evaluator.py` 的
    # worker 行 _worker_health"等待 post_market_done → auto_evaluate"，这一步已
    # 随 evaluator 迁移过来；**缺的是新追责链没接线** —— intaker 写的 claims
    # 一直停在 pending，只有手工调 resolver 才会判，闭环是断的。
    #
    # 【为什么放在 evaluate_pending 之后】两者同源（都是 T+N 真值回溯），但
    # 各自失败互不影响：旧的写 qd_agent_traces，新的写 qd_agent_resolutions，
    # 任一步炸了另一边照跑（下面的 try 保证这点）。
    #   ⚠ 旧系统 evaluate_pending 读的是 direction neutral / confidence 0.5 的
    #   污染数据，S2 已清空重来；此后有权重的样本全部来自本步产出的 resolutions。
    try:
        from .resolver import resolve_due_claims
        acc = resolve_due_claims(limit=200)
        result["accountability"] = {k: v for k, v in acc.items() if k != "details"}
        _worker_health["last_accountability"] = result["accountability"]
        logger.info(
            "[AutoEval] 追责判定: due=%s resolved=%s undecidable=%s judged=%s errors=%s",
            acc.get("due"), acc.get("resolved"), acc.get("undecidable"),
            acc.get("judged"), acc.get("errors"))
    except Exception as e:
        logger.error("[AutoEval] 追责判定失败(不影响旧链路): %s", e)
        result["accountability"] = {"error": str(e)}

    # ── v4 追责 → 权重（闭环补头，2026-10-01）─────────────────────────────
    # 上面判定出的 resolutions 此前**没有任何代码喂回权重表**，追责只是审计账本。
    # 本步按 domain 聚合命中率慢调写 qd_agent_weights(layer='domain')，由
    # chain/weight_hints 在工具预选 / 技能选择处消费 ⇒ 判定结果真正影响下一次选择。
    # 独立 try：喂数失败不影响报告产出。
    try:
        from .weight_feed import feed as _feed_weights
        wf = _feed_weights()
        result["weight_feed"] = wf
        _worker_health["last_weight_feed"] = wf
        if wf.get("updated"):
            logger.info("[AutoEval] 追责→权重: %s", wf["updated"])
    except Exception as e:
        logger.error("[AutoEval] 追责→权重失败(不影响报告): %s", e)
        result["weight_feed"] = {"error": str(e)}

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
    # v4 追责判定上一次结果（None = 一次都没跑成功过，用来区分"没数据"和"没接线"）
    "last_accountability": None,
    # 追责→权重 上一次结果（同理：None = 没跑成功过）
    "last_weight_feed": None,
}

def get_worker_health() -> Dict[str, Any]:
    h = dict(_worker_health)
    h["is_alive"] = _eval_thread is not None and _eval_thread.is_alive()
    h["next_run_in_seconds"] = _seconds_until_post_market() if h["is_alive"] else None
    h["schedule"] = "每天 15:30（盘后）"
    # ── C：合并只读探针（2026-10-18）────────────────────────────────
    # in-process 字段在"宿主根本没起"时全是 None/假象（没有进程就没有内存状态）
    # ⇒ 探针读**落盘**真相补进来：stalled/health_status/health_detail。
    probe = probe_worker_health()
    h["stalled"] = probe["stalled"]
    h["health_status"] = probe["status"]
    h["health_detail"] = probe["detail"]
    h["health_file"] = probe["health_file"]
    h["last_persisted_success_at"] = probe["last_success_at"]
    return h


# ═══════════════════════════════════════════════════════════════════
#  停摆自愈（B）+ 健康探针（C）—— 2026-10-18 用户裁定 B+C
#
#  背景：盘后闭环 = scheduler._post_market_batch（15:30，backfill 1m/1d
#  → post_market_done.set()）→ EvalWorker（auto_evaluate + v5 采集 + 酿造）。
#
#  B) 停摆不自健：flask 在 15:30 之后才起（宿主机几天没开 / 重启）时，
#     scheduler 主循环会在 ~10s 内补跑当日 post_market_batch（**启动补跑
#     路径刻意跳过它、主循环不跳过**，scheduler.py:833-881），但本线程
#     按 _seconds_until_post_market() 睡到次日 15:30 ⇒ 错过事件、当日
#     闭环静默顺延，v5 情境帧（C 类不可重建）永久丢失。
#     ⇒ 线程启动时判定：已过盘后时刻 + 批次今日未完成 ⇒ 等事件而非睡到
#        明日；批次已完成而评估今日未成功（进程在两者之间死过）⇒ 立即补评。
#
#  B') 消费即清：post_market_done 历史上从不 clear ⇒ 长跑进程从第 2 天起
#     "等待盘后数据同步"恒跳过，15:30 评估与 backfill 赛跑（靠 resolver
#       视界门② + 重试治理兜底，但违背设计意图）。清在**消费侧**
#       （_wait_post_market_done）而非 batch 起始侧——后者与 15:30 准点
#       wake 存在竞态（wake 可能先于 clear 读到昨日的置位）。
#
#  C) 停摆不可见：_worker_health 只在进程内 ⇒ 宿主不起时外部无从得知。
#     ⇒ 健康快照落盘 JSONL（对齐 trace 机制：AGENT_*_FILE 相对路径锚定
#       app/agent/）+ 只读探针 + CLI --health（stalled ⇒ exit 1）。
# ═══════════════════════════════════════════════════════════════════

# 盘后批次触发时刻 —— 与 scheduler.TASKS 的 post_market_batch(15:30) **同口径**；
# 改时刻必须两处同改（本常量是 EvalWorker 侧的自愈判据，不是新事实源）。
_POST_MARKET_TRIGGER = (15, 30)


def _wait_post_market_secs() -> float:
    """等盘后批次完成事件的上限秒数（QD_EVAL_WAIT_POST_MARKET_SECONDS，默认 3600）。

    默认值即旧代码内联的 3600（批次含 10min 未到位重试，~50min 量级）；
    提成 env 旋钮：阈值口径走配置层，不新造硬编码。
    """
    try:
        return max(60.0, float(os.getenv("QD_EVAL_WAIT_POST_MARKET_SECONDS", "3600") or 3600))
    except (TypeError, ValueError):
        return 3600.0


def _stall_grace_hours() -> float:
    """探针判定停摆的宽限小时数（QD_EVAL_STALL_GRACE_HOURS，默认 2）。"""
    try:
        return max(0.5, float(os.getenv("QD_EVAL_STALL_GRACE_HOURS", "2") or 2))
    except (TypeError, ValueError):
        return 2.0


def _health_file_path():
    """健康落盘路径：AGENT_EVAL_HEALTH_FILE 相对路径锚定 app/agent/（对齐 trace 机制）。"""
    from app.agent.utils.tracing import anchored_path
    return anchored_path("AGENT_EVAL_HEALTH_FILE", "traces/eval_worker_health.jsonl")


def _persist_worker_health(extra: Optional[Dict[str, Any]] = None) -> None:
    """把 _worker_health 快照追加进健康 JSONL（C）。**fail-soft**：观测不许成为新故障源。

    一行一事件（worker_start / run）；字段含 ts/pid + _worker_health 全量 +
    extra（event/catchup）。写失败只记 warning，绝不影响 worker 主线。
    """
    try:
        snap: Dict[str, Any] = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "pid": os.getpid()}
        snap.update(_worker_health)
        if extra:
            snap.update(extra)
        p = _health_file_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning("[EvalWorker] 健康落盘失败(不影响 worker): %s: %s",
                       type(e).__name__, e)


def _read_last_health_line() -> Optional[Dict[str, Any]]:
    """读健康落盘最后一条**可解析**行（容忍尾撕裂/半写）；没有 ⇒ None。"""
    try:
        p = _health_file_path()
        if not p.is_file():
            return None
        last = None
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)
                except Exception:
                    continue    # 撕裂行跳过，用上一条好行
        return last
    except Exception:
        return None


def _post_market_catchup_plan(now: Optional[datetime] = None) -> Dict[str, Any]:
    """停摆自愈判定（B）。返回 {"mode": "wait"|"run_now"|"none", "reason": str}。

    - wait:    今日已过盘后触发时刻、批次今日未完成 ⇒ 批次会由 scheduler 主循环
               补跑（单一触发点不破，EvalWorker **不自己跑批次**）⇒ 本线程应等
               事件，而非睡到次日 15:30。
    - run_now: 批次今日已完成、但评估今日未成功跑过（健康落盘有历史成功证据）
               ⇒ 数据已就绪，立即补评（补的是当日闭环，不是重跑批次）。
    - none:    未到时刻 / 非交易日 / 今日闭环已大概率完成 / 无证据（首次部署）。

    ★ run_now 的证据要求：健康落盘必须存在且历史 total_successes>0。没有证据时
      宁可漏一次自愈，也不冒"评估其实已跑过"的风险双算（weight_feed 是 EMA 慢调，
      同数据双跑会双步进）。漏诊由 C 探针暴露，不错算。
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    if (now.hour, now.minute) < _POST_MARKET_TRIGGER:
        return {"mode": "none",
                "reason": f"未到盘后时刻({now.strftime('%H:%M')} < 15:30)"}
    from app.utils.trading_calendar import is_trading_day
    if not is_trading_day(today):
        return {"mode": "none", "reason": "非交易日"}
    from app.market_cn.scheduler import scheduler_task_done
    if not scheduler_task_done("post_market_batch", today):
        return {"mode": "wait",
                "reason": f"今日({today})盘后批次未完成 ⇒ 等 scheduler 主循环补跑后 set 的事件"}
    last = _read_last_health_line()
    if last and str(last.get("last_success_at") or "").startswith(today):
        return {"mode": "none", "reason": "今日盘后闭环已成功完成"}
    if last and int(last.get("total_successes") or 0) > 0:
        return {"mode": "run_now",
                "reason": "批次已完成但评估今日未成功跑过(有历史成功证据) ⇒ 数据就绪，立即补评"}
    return {"mode": "none",
            "reason": "批次已完成、评估无历史成功证据(首次部署/从未跑过) ⇒ 不敢双算，回归正常节拍"}


def _wait_post_market_done(timeout: float) -> bool:
    """等盘后批次完成事件；**等到即消费（clear）**，返回是否等到（B/B'）。

    ★ 为什么消费即清：post_market_done 历史上从不 clear ⇒ 长跑进程从第 2 天起
      `is_set()` 恒真，"等待盘后数据同步完成"永远跳过，15:30 评估与 backfill
      赛跑（靠视界门② + 重试治理兜底，但违背设计意图）。
    ★ 导入失败（market_cn 不可用）⇒ 返回 True 按旧语义继续执行，不炸 worker
      （C-1 事故教训：启动期导入失败必须显式记一笔，不能静默）。
    """
    try:
        from app.market_cn.scheduler import post_market_done
    except Exception as e:
        logger.warning("[EvalWorker] 盘后事件不可用(%s: %s)，按旧语义继续执行",
                       type(e).__name__, e)
        return True
    if not post_market_done.is_set():
        logger.info("[EvalWorker] 等待盘后数据同步完成...")
        if not post_market_done.wait(timeout=timeout):
            return False
        logger.info("[EvalWorker] 盘后数据同步完成，开始回溯验证")
    post_market_done.clear()   # 消费即清：次日等待才有效
    return True


def probe_worker_health(now: Optional[datetime] = None) -> Dict[str, Any]:
    """只读健康探针（C）：不依赖进程内状态/DB，宿主down了也能从外部调。

    staleness 判据：
      · 有成功记录 ⇒ 预期下一完成时刻 = 下一交易日 15:30 + 宽限
        （QD_EVAL_STALL_GRACE_HOURS，默认 2h）；now 超过它仍无新成功 ⇒ stalled。
      · 从未成功 ⇒ 以最后一条线的日期为参照：当天 15:30+宽限前算 pending
        （首跑还没到/当天还会重试），之后 stalled（闭环从未完成）。
      · 文件缺失/空 ⇒ no_data（无证据 ≠ 健康，只是判不了）；无可解析行 ⇒ unreadable。

    返回 {"status": ok|pending|stalled|no_data|unreadable, "stalled": bool,
          "health_file", "last_run_at", "last_success_at",
          "consecutive_failures", "total_runs", "detail"}
    """
    now = now or datetime.now()
    out: Dict[str, Any] = {
        "stalled": False, "status": "no_data",
        "health_file": str(_health_file_path()),
        "last_run_at": None, "last_success_at": None,
        "consecutive_failures": 0, "total_runs": 0, "detail": "",
    }
    try:
        p = _health_file_path()
        if not p.is_file():
            out["detail"] = "健康文件不存在（worker 从未在本机起过或未落盘）"
            return out
        rows = []
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue    # 容忍尾撕裂/半写行
        if not rows:
            out["status"] = "unreadable"
            out["detail"] = "健康文件无任何可解析行（格式损坏？）"
            return out
        last = rows[-1]
        out["last_run_at"] = last.get("last_run_at")
        out["last_success_at"] = last.get("last_success_at")
        out["consecutive_failures"] = int(last.get("consecutive_failures") or 0)
        out["total_runs"] = int(last.get("total_runs") or 0)
        succ = str(last.get("last_success_at") or "")
        if succ:
            ref = succ[:10]
            from app.utils.trading_calendar import next_trading_day
            ref = next_trading_day(ref)
        else:
            ref = str(last.get("ts") or "")[:10]
            if not ref:
                out["status"] = "unreadable"
                out["detail"] = "最后一行无 ts（无法定位时间参照）"
                return out
        expected = datetime.strptime(ref, "%Y-%m-%d").replace(hour=15, minute=30) \
            + timedelta(hours=_stall_grace_hours())
        if now > expected:
            out["stalled"] = True
            out["status"] = "stalled"
            out["detail"] = (f"超过预期完成时刻 {expected:%Y-%m-%d %H:%M} 仍无成功记录"
                             + (f"（连续失败 {out['consecutive_failures']} 次）"
                                if out["consecutive_failures"] else ""))
        else:
            out["status"] = "ok" if succ else "pending"
            out["detail"] = ("正常" if succ else
                             f"今日尚未首跑（等待 15:30 节拍；连续失败 {out['consecutive_failures']} 次）")
        return out
    except Exception as e:
        out["status"] = "unreadable"
        out["detail"] = f"{type(e).__name__}: {e}"
        return out


def main(argv: Optional[List[str]] = None) -> int:
    """只读 CLI（C）：--health 探针 / --catchup-plan 自愈判定预演。零副作用。"""
    import argparse
    ap = argparse.ArgumentParser(
        prog="app.agent.chain.evaluator",
        description="EvalWorker 只读观测（不触跑任何闭环；宿主 down 时也可用）")
    ap.add_argument("--health", action="store_true",
                    help="打印健康探针 JSON；stalled/unreadable 时 exit 1（供外部监控）")
    ap.add_argument("--catchup-plan", action="store_true",
                    help="打印停摆自愈判定（wait/run_now/none）")
    args = ap.parse_args(argv)
    if args.catchup_plan:
        print(json.dumps(_post_market_catchup_plan(), ensure_ascii=False,
                         indent=2, default=str))
        return 0
    r = probe_worker_health()   # 默认即 --health
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    return 1 if (r.get("stalled") or r.get("status") == "unreadable") else 0


if __name__ == "__main__":
    raise SystemExit(main())
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
def run_v5_collection() -> Dict[str, Any]:
    """盘后采集钩子（v5 阶段 D4，2026-10-13）。

    ★ 存在的理由：D1 情境帧与 D2 分层配额只有函数、**没人调用** ⇒ 数据一天都不攒。
      而 C 类情境**不可重建**（今天没记，事后补不回来）。故在此挂进盘后 worker。

    设计：**采集失败不影响 T+N 验证** —— 本函数吞掉一切异常只记日志，
    盘后主线（验证 + 酿造）必须照跑。
    """
    try:
        from .collector import run_daily_collection
        r = run_daily_collection()
        if r.get("ok"):
            logger.info("[EvalWorker] v5 采集完成 as_of=%s", r.get("as_of"))
        else:
            logger.warning("[EvalWorker] v5 采集部分失败 as_of=%s errors=%s",
                           r.get("as_of"), r.get("errors"))
        return r
    except Exception as e:
        logger.warning("[EvalWorker] v5 采集中止（不影响验证）: %s", e)
        return {"ok": False, "errors": [f"collector: {type(e).__name__}: {e}"]}


def start_eval_worker():
    """启动后台评估 worker（盘后 15:30 每天运行一次，T+N 验证）。"""
    global _eval_thread, _eval_stop

    import threading
    import time as _time

    if _eval_thread is not None and _eval_thread.is_alive():
        return

    _eval_stop = threading.Event()

    def _worker():
        # ── 停摆自愈判定（B，2026-10-18）────────────────────────────
        # flask 15:30 之后才起 ⇒ scheduler 主循环 ~10s 内补跑当日 post_market_batch
        # （启动补跑路径刻意跳过它，主循环不跳过，scheduler.py:833-881），但本线程
        # 若按 _seconds_until_post_market() 睡到次日 15:30，事件被错过、当日闭环
        # 静默顺延、v5 情境帧（C 类不可重建）永久丢失 ⇒ 启动时改判一次。
        plan = _post_market_catchup_plan()
        if plan["mode"] != "none":
            logger.info("[EvalWorker] 停摆自愈(%s): %s", plan["mode"], plan["reason"])
        _persist_worker_health({"event": "worker_start", "catchup": plan["mode"]})

        while not _eval_stop.is_set():
            mode = plan["mode"]
            if mode == "wait":
                # 批次由 scheduler 主循环补跑（单一触发点不破）；本线程只负责等它
                if not _wait_post_market_done(_wait_post_market_secs()):
                    logger.warning(
                        "[EvalWorker] 停摆自愈等待超时(%ds)：盘后批次未完成，照常评估"
                        "（数据可能未齐，追责重试治理兜底）", _wait_post_market_secs())
            elif mode == "run_now":
                pass   # 批次已完成（数据就绪），无需等待，直接评估
            else:
                # 计算距离下一个盘后 15:30 的等待时间
                wait_secs = _seconds_until_post_market()
                logger.info("[EvalWorker] 下次盘后验证: %.0f 秒后 (%.1f 小时)",
                            wait_secs, wait_secs / 3600)
                if _eval_stop.wait(timeout=wait_secs):
                    break  # 收到停止信号

                # 等待盘后批次完成（K线数据同步就绪；B'：消费即清）
                if not _wait_post_market_done(_wait_post_market_secs()):
                    logger.warning("[EvalWorker] 等待盘后批次超时(%ds)，照常评估",
                                   _wait_post_market_secs())

            plan = {"mode": "none"}   # 自愈只生效一次，之后回归正常节拍
            if _eval_stop.is_set():
                break

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

            # ── v5 盘后采集（阶段 D4）：情境帧 + 分层配额样本 ──────────────
            # ★ 必须每天跑：C 类情境**不可重建**，今天没记事后补不回来。
            #   放在验证之后、酿造之前：验证是主线，采集失败不影响主线。
            run_v5_collection()

            # 技能酿造挂钩（2026-09-19 升级为触发策略 v2，重设计 §2.5）：
            # 每日调用一次 brew_skills(trigger="auto")，节拍由信号分内部决定
            # （信号就绪立即酿 / 7 日兜底 / 冷却），状态落 qd_agent_weights
            # （layer='brew_state'），重启不丢。worker 不再管理日期。
            try:
                from .skill_brewer import brew_skills, refresh_skill_adapter
                _brewed = brew_skills(trigger="auto")
                if any(r.get("status") == "brewed" for r in _brewed):
                    refresh_skill_adapter()
            except Exception as e:
                logger.warning("[EvalWorker] 技能酿造跳过: %s", e)

            # ── C：每轮跑完落一份健康快照（停摆可诊断、探针有数据可读）──────
            # 放在酿造之后：本行的 last_success_at 反映"整条闭环跑完"这一事实。
            # 进程在落盘前被杀 ⇒ 探针读到上一条旧线 ⇒ 判 stalled（正是目的）。
            _persist_worker_health({"event": "run", "catchup": mode})

    _eval_thread = threading.Thread(target=_worker, daemon=True, name="eval-worker")
    _eval_thread.start()
    logger.info("[EvalWorker] 盘后回溯评估 worker 已启动")
def stop_eval_worker():
    global _eval_stop
    if _eval_stop:
        _eval_stop.set()
