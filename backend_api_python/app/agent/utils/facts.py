# -*- coding: utf-8 -*-
"""utils/facts.py — 事实权威层（AGENT_DESIGN §8.1，2026-09-30 提智）。

问题：复盘输入取 completed_phases_text 里的日期断言，模型可改写时间事实且被
复盘继承——金融分析里日期错 = 结论全错。

方案（§8.1）：
  - state["facts"]（结构化、只读）：{ref_date, weekday, is_trading_day, ...}
  - 任务书固定区块【时间口径·权威（不得改写）】，渲染自 facts
  - 收尾时若最终答案含与 facts 冲突的断言 → 记 fact_conflict 到 trace
    （只记账，不硬阻断）

设计边界：本模块零重依赖（交易日历懒加载、fail-open）；判定只做**确定性**
日期断言比对，不做语义猜测。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Dict, List

logger = logging.getLogger(__name__)

_WEEK_CN = "一二三四五六日"


def build_facts(ref: date = None) -> Dict[str, object]:
    """构建事实快照（确定性；日历缺失 fail-open，is_trading_day=None）。"""
    today = ref or date.today()
    is_td = None
    try:
        from app.utils import trading_calendar
        try:
            is_td = bool(trading_calendar.is_trading_day(today.isoformat()))
        except Exception:
            is_td = bool(trading_calendar.is_trading_day(today))
    except Exception:
        pass
    return {
        "ref_date": today.isoformat(),
        "weekday": "周" + _WEEK_CN[today.weekday()],
        "is_trading_day": is_td,
        # 时间窗由任务语义决定，解析器产出后可回填；无则留空不臆造
        "window_start": "",
        "window_end": "",
        "basis": "K线默认前复权(qfq)；实时行情为盘中动态数据",
        "source": "系统时钟(Asia/Shanghai)+交易日历",
    }


def render_facts_block(facts: Dict[str, object]) -> str:
    """渲染任务书固定区块（不得改写声明 + 系统事实）。"""
    td = facts.get("is_trading_day")
    td_str = "是" if td is True else ("否" if td is False else "未知")
    lines = [
        "【时间口径·权威（不得改写）】",
        f"- 参考日期：{facts.get('ref_date')}（{facts.get('weekday')}），是否交易日：{td_str}",
        f"- 数据口径：{facts.get('basis')}",
        "- 本区块为系统事实：所有分析、结论与复盘以此为准；任何步骤不得改写以上"
        "日期/口径，时间窗换算必须基于参考日期；与用户表述冲突时以本区块为准并注明。",
    ]
    ws, we = facts.get("window_start"), facts.get("window_end")
    if ws and we:
        lines.insert(2, f"- 时间窗：{ws} ~ {we}")
    return "\n".join(lines)


# 「今天/今日/现在/当前 + 日期」断言：命中即与 facts 对账。刻意不收「截至/目前」
# ——“截至 09-28 收盘”是合法的时间窗边界，不是对今天的断言（只记账也怕噪音）。
_DATE_ASSERT_RE = re.compile(
    r"((?:今天|今日|现在|当前)(?:日期)?\s*(?:是|为|[：:])?\s*)"
    r"(20\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2}日?|\d{1,2}月\d{1,2}日)")


def _norm_date(token: str, ref_year: int) -> str:
    """断言日期归一到 YYYY-MM-DD；解析失败返回空串。"""
    t = token.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-").replace(".", "-")
    try:
        if re.fullmatch(r"\d{1,2}-\d{1,2}", t):
            t = f"{ref_year}-{t}"
        return datetime.strptime(t, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def detect_fact_conflict(answer: str, facts: Dict[str, object]) -> List[str]:
    """确定性事实冲突检测（只记账）：日期断言与 ref_date 不符 → 冲突描述列表。"""
    out: List[str] = []
    ref = str(facts.get("ref_date") or "")
    if not ref or not answer:
        return out
    try:
        ref_year = int(ref[:4])
    except ValueError:
        return out
    for m in _DATE_ASSERT_RE.finditer(str(answer)):
        got = _norm_date(m.group(2), ref_year)
        if got and got != ref:
            out.append(f"「{m.group(1)}{m.group(2)}」与参考日期 {ref} 冲突")
    return out
