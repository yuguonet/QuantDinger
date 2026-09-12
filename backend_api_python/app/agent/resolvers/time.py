# -*- coding: utf-8 -*-
"""时间实体解析器 v3（2026-09-12）：域特化 + 语义异常澄清。

域契约（用户裁定）：
  - domain="finance"：昨天/前天/前一天/明天/后天等相对日词一律按【交易日】口径。
  - domain="general"：自然日口径，不引入任何交易日语义。

新增（2026-09-12 用户裁定）：语义异常打回。
  周六说"今天行情/明天开盘"——行情语境 + 相对日词指向非交易日 = 语义异常，
  不应静默标注，应【打回用户澄清】。resolve() 返回的 ResolveResult 增加通过
  entities 携带的标记：{"type": "time_clarify", "question": "..."}；
  chat_node 检测到该标记时走 direct_answer 通道直接询问用户。
  用户在追问中明确日期后，新消息重新解析（那时相对日词消失或指向交易日）。

异常判定（仅 finance 域，且消息含行情/交易语境词）：
  - "今天/当日" + 今天非交易日
  - "明天/后天/下周X" + 所指日期非交易日
  - （"昨天/前天"在非交易日说，上一交易日口径无歧义，不打回）
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from .base import EntityResolver, ResolveResult

logger = logging.getLogger(__name__)

_WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

# 行情/交易语境词（出现才考虑异常打回；纯闲聊不拦）
_MARKET_WORDS = re.compile(
    r"行情|开盘|收盘|涨停|跌停|大盘|指数|个股|股票|资金|龙虎榜|板块|题材|"
    r"买入|卖出|建仓|减仓|仓位|止损|止盈|K线|均线|MACD|RSI|KDJ|涨跌|做多|做空|看多|看空"
)

_PATTERNS = [
    (re.compile(r"[近这](\d+)\s*个?交易日内?(?:的)?(?:行情|走势|数据|资金|表现)?"), "recent_trade_days"),
    (re.compile(r"[近这](\d+)\s*天内?(?:的)?(?:行情|走势|数据|资金|表现)?"), "recent_days"),
    (re.compile(r"(\d+)\s*天前"), "days_ago"),
    (re.compile(r"(\d+)\s*天[之]?后"), "days_after"),
    (re.compile(r"([上本下])个?(?:星期|周)([一二三四五六日天])"), "weekday_rel"),
    (re.compile(r"(今天|今日|当天)"), "today"),
    (re.compile(r"(前天)"), "dbd2"),
    (re.compile(r"(前一日|前一天)"), "prev_day"),
    (re.compile(r"(昨天|昨日)"), "yesterday"),
    (re.compile(r"(明天|明日)"), "tomorrow"),
    (re.compile(r"(后天)"), "after_tomorrow"),
    (re.compile(r"(?:星期|周)([一二三四五六日天])"), "weekday_abs"),
    (re.compile(r"(上周|本周|这周|下周)"), "week_rel"),
    (re.compile(r"(上个月|上月|本月|这个月|下个月|下月)"), "month_rel"),
    (re.compile(r"(最近|近期)"), "recent"),
    (re.compile(r"(上一?个?交易日|前一?个?交易日)"), "prev_trade_day"),
    (re.compile(r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)"), "explicit_date"),
]


def _cal():
    from app.utils import trading_calendar
    return trading_calendar


def _shift_trade(base: str, n: int, future: bool = False) -> str:
    cal = _cal()
    try:
        return cal.next_trading_day(base, n) if future else cal.prev_trading_day(base, n)
    except Exception:
        dt = datetime.strptime(base, "%Y-%m-%d")
        step = timedelta(days=max(1, round(n * 1.4)))
        return (dt + step if future else dt - step).strftime("%Y-%m-%d")


def _next_weekday_after(base: str, wd: int) -> str:
    dt = datetime.strptime(base, "%Y-%m-%d")
    delta = (wd - dt.weekday()) % 7 or 7
    return (dt + timedelta(days=delta)).strftime("%Y-%m-%d")


class TimeResolver(EntityResolver):
    """时间实体解析器（域特化 + 语义异常打回）。"""

    def __init__(self, domain: str = "general", now: Optional[datetime] = None):
        self.domain = domain
        self._now = now

    def _today(self) -> str:
        return (self._now or datetime.now()).strftime("%Y-%m-%d")

    def _finish(self, today: str) -> str:
        try:
            ref = (self._now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
            return _cal().last_finish_trading_day(ref)
        except Exception:
            return today

    # ── 语义异常检测（仅 finance 域）──
    def _detect_anomaly(self, user_input: str, today: str, finish: str):
        """返回澄清问题字符串；无异常返回 None。

        判定（需消息含行情/交易语境词）：
          - "今天/当日" + 今天非交易日 → 打回（用户可能是想问最近收盘日，或想在
            非交易日做盘后规划——两种意图都成立，必须问）
        """
        if not _MARKET_WORDS.search(user_input):
            return None
        if re.search(r"(今天|今日|当天)", user_input):
            try:
                if not _cal().is_trading_day(today):
                    prev1 = _shift_trade(today, 1)
                    next1 = _shift_trade(today, 1, future=True)
                    return (f"今天（{today}）不是交易日。您是想查询最近已收盘交易日（{prev1}）的行情，"
                            f"还是做面向下一交易日（{next1}）的开盘规划？")
            except Exception:
                return None
        # 明天/后天/下周X 指向非交易日 → 打回
        m = re.search(r"(明天|明日|后天|下周)([一二三四五六日天])?", user_input)
        if m:
            try:
                if m.group(1) in ("明天", "明日"):
                    d = _shift_trade(today, 1, future=True)
                elif m.group(1) == "后天":
                    d = _shift_trade(today, 2, future=True)
                else:
                    wd_char = m.group(2) or "一"
                    wd = _WEEKDAY_CN.get(wd_char, 0)
                    next_mon = datetime.strptime(today, "%Y-%m-%d") - timedelta(
                        days=datetime.strptime(today, "%Y-%m-%d").weekday()) + timedelta(days=7)
                    d = _next_weekday_after((next_mon - timedelta(days=1)).strftime("%Y-%m-%d"), wd)
                if not _cal().is_trading_day(d):
                    prev1 = _shift_trade(today, 1)
                    next1 = _shift_trade(today, 1, future=True)
                    return (f"您提到的「{m.group(0)}」对应 {d}，不是交易日。请确认：是查询最近已收盘"
                            f"交易日（{prev1}）的数据，还是面向下一交易日（{next1}）做规划？")
            except Exception:
                return None
        return None

    def resolve(self, user_input: str) -> Optional[ResolveResult]:
        finance = self.domain == "finance"
        today = self._today()
        finish = self._finish(today) if finance else today

        # ── 语义异常 → 打回澄清（优先于常规标注）──
        if finance:
            question = self._detect_anomaly(user_input, today, finish)
            if question:
                logger.info("[TimeResolver] 语义异常打回: %s", question[:80])
                return ResolveResult(
                    entities=[{"type": "time_clarify", "question": question}],
                    entity_code="", entity_name="",
                    entity_type="time_clarify",
                    effective_input=f"{user_input} 【时间待澄清】{question}",
                )

        lines = []
        if finance:
            lines.append(f"今天={today}；最近已收盘交易日={finish}")

        seen = set()
        for pattern, kind in _PATTERNS:
            m = pattern.search(user_input)
            if not m or kind in seen:
                continue
            seen.add(kind)
            try:
                desc = self._resolve_one(kind, m, today, finish, finance)
            except Exception as e:
                logger.debug("[TimeResolver] %s 解析失败: %s", kind, e)
                continue
            if desc:
                lines.append(desc)

        if len(lines) <= (1 if finance else 0):
            return None

        time_note = "；".join(lines)
        tail = "（以交易日历为准，请在规划与取数时使用上述标定日期）" if finance \
            else "（自然日口径）"
        expanded = f"{user_input} 【时间】{time_note}{tail}"
        return ResolveResult(entities=[], entity_code="", entity_name="",
                             entity_type="time", effective_input=expanded)

    def _resolve_one(self, kind: str, m: re.Match, today: str, finish: str,
                     finance: bool) -> Optional[str]:
        cal = _cal()
        g = m.group(1) if m.groups() else ""
        dt_today = datetime.strptime(today, "%Y-%m-%d")

        if kind == "yesterday" or kind == "prev_day":
            if finance:
                word = "昨天" if kind == "yesterday" else "前一天"
                return f"{word}={_shift_trade(today, 1)}（上一交易日）"
            return f"{'昨天' if kind == 'yesterday' else '前一天'}={(dt_today - timedelta(days=1)).strftime('%Y-%m-%d')}"

        if kind == "dbd2":
            if finance:
                return f"前天={_shift_trade(today, 2)}（前两个交易日）"
            return f"前天={(dt_today - timedelta(days=2)).strftime('%Y-%m-%d')}"

        if kind == "tomorrow":
            if finance:
                return f"明天={_shift_trade(today, 1, future=True)}（下一交易日）"
            return f"明天={(dt_today + timedelta(days=1)).strftime('%Y-%m-%d')}"

        if kind == "after_tomorrow":
            if finance:
                return f"后天={_shift_trade(today, 2, future=True)}（下两个交易日）"
            return f"后天={(dt_today + timedelta(days=2)).strftime('%Y-%m-%d')}"

        if kind == "today":
            if finance:
                if cal.is_trading_day(today):
                    return f"今天={today}（当前交易日）"
                return f"今天={today}（非交易日），最近已收盘交易日={finish}"
            return f"今天={today}"

        if kind == "prev_trade_day":
            return f"上一交易日={finish}"

        if kind == "recent":
            if finance:
                d5 = _shift_trade(finish, 4)
                return f"最近=默认近 5 个交易日 {d5}~{finish}（右端为最近已收盘交易日，可按需调整）"
            d7 = (dt_today - timedelta(days=7)).strftime("%Y-%m-%d")
            return f"最近=约 {d7}~{today}"

        if kind == "recent_trade_days":
            n = max(1, min(60, int(g)))
            if finance:
                d_start = _shift_trade(finish, n - 1)
                return f"近{n}个交易日={d_start}~{finish}"
            d_start = (dt_today - timedelta(days=max(1, round(n * 7 / 5)) - 1)).strftime("%Y-%m-%d")
            return f"近{n}个交易日≈{d_start}~{today}（按自然日估算）"

        if kind == "recent_days":
            n = max(1, min(90, int(g)))
            d_start = (dt_today - timedelta(days=n - 1)).strftime("%Y-%m-%d")
            if finance:
                return f"近{n}天（自然日）={d_start}~{today}；交易日口径≈{_shift_trade(today, max(1, round(n * 7 / 5)) - 1)}~{today}"
            return f"近{n}天（自然日）={d_start}~{today}"

        if kind == "days_ago":
            n = max(1, int(g))
            if finance:
                return f"{n}天前={_shift_trade(today, n)}（{n} 个交易日前）"
            return f"{n}天前={(dt_today - timedelta(days=n)).strftime('%Y-%m-%d')}"

        if kind == "days_after":
            n = max(1, int(g))
            if finance:
                return f"{n}天后={_shift_trade(today, n, future=True)}（{n} 个交易日后）"
            return f"{n}天后={(dt_today + timedelta(days=n)).strftime('%Y-%m-%d')}"

        if kind == "weekday_rel":
            rel, wd_char = m.group(1), m.group(2)
            wd = _WEEKDAY_CN.get(wd_char, 0)
            if rel == "上":
                last_mon = dt_today - timedelta(days=dt_today.weekday() + 7)
                d = _next_weekday_after((last_mon - timedelta(days=1)).strftime("%Y-%m-%d"), wd)
                return f"上周{wd_char}={d}"
            if rel == "下":
                next_mon = dt_today - timedelta(days=dt_today.weekday()) + timedelta(days=7)
                d = _next_weekday_after((next_mon - timedelta(days=1)).strftime("%Y-%m-%d"), wd)
                if finance:
                    tag = "（交易日）" if cal.is_trading_day(d) else "（非交易日）"
                    return f"下周{wd_char}={d}{tag}"
                return f"下周{wd_char}={d}"
            d = (dt_today + timedelta(days=(wd - dt_today.weekday()) % 7)).strftime("%Y-%m-%d")
            return f"本周{wd_char}={d}"

        if kind == "weekday_abs":
            wd = _WEEKDAY_CN.get(g, 0)
            d = _next_weekday_after(today, wd)
            if finance:
                tag = "交易日" if cal.is_trading_day(d) else "非交易日"
                return f"星期{g}={d}（未来最近一个，{tag}）"
            return f"星期{g}={d}（未来最近一个）"

        if kind == "week_rel":
            s = g
            monday = dt_today - timedelta(days=dt_today.weekday())
            if s.startswith("上"):
                ws = (monday - timedelta(days=7)).strftime("%Y-%m-%d")
                we = (monday - timedelta(days=1)).strftime("%Y-%m-%d")
            elif s.startswith("下"):
                ws = (monday + timedelta(days=7)).strftime("%Y-%m-%d")
                we = (monday + timedelta(days=13)).strftime("%Y-%m-%d")
            else:
                ws = monday.strftime("%Y-%m-%d")
                we = (monday + timedelta(days=6)).strftime("%Y-%m-%d")
            if finance:
                return f"{s}={ws}~{we}（交易日 {cal.trade_date_range(ws, we)}）"
            return f"{s}={ws}~{we}"

        if kind == "month_rel":
            s = g
            first = dt_today.replace(day=1)
            if "上" in s:
                last_prev = first - timedelta(days=1)
                first = last_prev.replace(day=1)
                end = last_prev.strftime("%Y-%m-%d")
            elif "下" in s:
                nm = (first + timedelta(days=32)).replace(day=1)
                first = nm
                end = ((nm + timedelta(days=32)).replace(day=1) - timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                end = today
            return f"{s}={first.strftime('%Y-%m-%d')}~{end}"

        if kind == "explicit_date":
            s = re.sub(r"[年月]", "-", g).replace("日", "").replace("/", "-")
            try:
                datetime.strptime(s, "%Y-%m-%d")
            except ValueError:
                return None
            if finance:
                tag = "交易日" if cal.is_trading_day(s) else "非交易日"
                return f"{g}={s}（{tag}）"
            return f"{g}={s}"
        return None
