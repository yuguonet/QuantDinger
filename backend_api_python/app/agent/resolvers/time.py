# -*- coding: utf-8 -*-
"""时间实体解析器 v3（2026-09-12）：域特化 + 语义异常澄清。

域契约（用户裁定）：
  - domain="finance"：昨天/前天/前一天/明天/后天等相对日词一律按【交易日】口径。
  - domain="general"：自然日口径，不引入任何交易日语义。

澄清契约（2026-09-12 起；2026-09-13 上升为通用设计）：
  **无法准确判断就应该反问，拿到准确信息才执行**。本解析器不猜——命中歧义时返回
  clarify_question（见 base.ResolveResult），chat_node 见非空即直接反问用户并
  **不进入执行**；用户答复作为新消息重新解析，那时歧义已消除，自然走常规流程。

  两类歧义：
  (a) 语义异常（仅 finance 域，且消息含行情/交易语境词）：相对日词落在非交易日。
      "今天/当日" + 今天非交易日；"明天/后天/下周X" + 所指日期非交易日。
      （"昨天/前天"在非交易日说，上一交易日口径无歧义，不问）
  (b) 时间窗不明（域无关）：只说"最近/近期"而不给具体窗口。
      原先静默按"近 5 个交易日"处理并标注"可按需调整"——它自己都知道在猜。
      现改为反问（交易日常见 5/10/20/60 个交易日；自然日口径 7/30/90 天）。
      带窗口的说法（最近一周 / 近 5 个交易日）不触发。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from .base import EntityResolver, ResolveResult

logger = logging.getLogger(__name__)

# ── 领域登记表（可扩展）────────────────────────────────────────
# 2026-09-13：原实现在 resolve() 内直接判定 `self.domain == "finance"`，领域一多就
# 无处扩展。现改为查登记表：
#   _ENTITY_DOMAIN           —— 实体类型 → 领域。chat 阶段先于 plan，拿不到
#                               selected_domain，只能倒推领域。
#   TRADING_CALENDAR_DOMAINS —— 使用【交易日】口径的领域。新增以交易日为一等
#                               公民的领域时在此登记；未登记的领域一律自然日口径。
#   _WORD_DOMAIN（见下）      —— 输入语汇 → 领域，用于"没有实体但语境明确"的兜底。
_ENTITY_DOMAIN = {"stock": "finance"}
TRADING_CALENDAR_DOMAINS = frozenset({"finance"})

_WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

# 行情/交易语境词（出现才考虑异常打回；纯闲聊不拦）
_MARKET_WORDS = re.compile(
    r"行情|开盘|收盘|涨停|跌停|大盘|指数|个股|股票|资金|龙虎榜|板块|题材|"
    r"买入|卖出|建仓|减仓|仓位|止损|止盈|K线|均线|MACD|RSI|KDJ|涨跌|做多|做空|看多|看空|"
    # 2026-09-14 补：这些词此前落不到 finance，导致"前天涨幅榜"被判 general 域、
    # 按自然日算出 2026-09-12（交易日口径应为 2026-09-10，差 2 天）——金融域漏判
    # 直接给错日期，是"统一加时间解析"的缺口。均为纯行情语汇，不误伤闲聊。
    r"涨幅|跌幅|振幅|换手|成交|量比|市盈率|市值|涨速|跌速|封板|炸板|打板"
)

# 语汇 → 领域（无实体时的兜底信号，按顺序匹配，可扩展）。
# "最近的行情怎么样"里没有股票名，但它显然属金融域——若落到 general，澄清会问出
# "自然日 7/30/90 天"这种错口径。新增领域在此登记（如 crypto 的 币/交易所 语汇）。
_WORD_DOMAIN = [(_MARKET_WORDS, "finance")]

_PATTERNS = [
    (re.compile(r"[近这](\d+)\s*个?交易日内?(?:的)?(?:行情|走势|数据|资金|表现)?"), "recent_trade_days"),
    (re.compile(r"[近这](\d+)\s*天内?(?:的)?(?:行情|走势|数据|资金|表现)?"), "recent_days"),
    (re.compile(r"(\d+)\s*天前"), "days_ago"),
    (re.compile(r"(\d+)\s*天[之]?后"), "days_after"),
    (re.compile(r"([上本下])个?(?:星期|周)([一二三四五六日天])"), "weekday_rel"),
    (re.compile(r"(现在|当前|此刻)"), "now"),
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

# 区间型词干：内联插入点 = 数字捕获组末尾 + 词干（保证不拆散「近5个交易日」这类词组）
_RANGE_STEMS = {
    "recent_trade_days": "个交易日",
    "recent_days": "天",
    "days_ago": "天前",
    "days_after": "天后",
}

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

    def __init__(self, domain: str = "", entity_type: str = "",
                 now: Optional[datetime] = None):
        """domain 显式给定优先；未给定则由 entity_type 推导（见 _ENTITY_DOMAIN）。

        chat 阶段（实体解析）先于 plan，拿不到 selected_domain，所以调用方通常
        只能传 entity_type；显式 domain 供已持有领域信息的调用方使用。
        """
        # 领域优先级：显式 domain > 实体类型倒推 > resolve() 内按输入语汇倒推。
        # 此处不再直接落到 "general"——否则无法区分"显式 general"与"未知"。
        self._domain_explicit = bool(domain)
        self.domain = domain or _ENTITY_DOMAIN.get(entity_type or "", "")
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

    def _clarify(self, user_input: str, question: str,
                 kind: str = "time_clarify") -> ResolveResult:
        """构造澄清结果（统一入口；entity_type 一律以 _clarify 结尾）。"""
        return ResolveResult(
            entities=[{"type": kind, "question": question}],
            entity_code="", entity_name="",
            entity_type=kind,
            effective_input=f"{user_input} 【时间待澄清】{question}",
            clarify_question=question,
        )

    def _vague_window_question(self, user_input: str, finance: bool) -> Optional[str]:
        """时间窗不明检测（域无关）：只说“最近/近期”而没给窗口 → 反问。

        带窗口的说法不算歧义：“最近一周”、“近 5 个交易日”、“最近 3 天”、“最近1个月”。
        """
        if not re.search(r"(最近|近期)", user_input):
            return None
        # 已给出量化窗口 → 可准确计算，不打扰用户
        if re.search(r"(最近|近期)\s*(?:[0-9一二三四五六七八九十]+|[这本个]?\s*(?:周|星期|月))",
                     user_input):
            return None
        if finance:
            return ("「最近」没有明确时间窗，请确认要看的区间：近 5 个交易日（约 1 周）/ "
                    "近 10 个交易日（约 2 周）/ 近 20 个交易日（约 1 个月）/ "
                    "近 60 个交易日（约 1 季度）？")
        return "「最近」没有明确时间窗，请确认要看多久：近 7 天 / 近 30 天 / 近 90 天？"

    def _infer_domain(self, user_input: str) -> str:
        """领域倒推（chat 先于 plan、拿不到 selected_domain）。

        优先级：显式 domain > 实体类型倒推（_ENTITY_DOMAIN）> 输入语汇倒推
        （_WORD_DOMAIN）> general。三者都是登记表——新增领域只加表、不改逻辑。
        """
        if self._domain_explicit or self.domain:
            return self.domain
        for _rx, _dom in _WORD_DOMAIN:
            if _rx.search(user_input):
                return _dom
        return "general"

    def resolve(self, user_input: str) -> Optional[ResolveResult]:
        # 领域先倒推，再查登记表判定是否走交易日口径（两者均可扩展）
        domain = self._infer_domain(user_input)
        finance = domain in TRADING_CALENDAR_DOMAINS
        today = self._today()
        finish = self._finish(today) if finance else today

        # ── 澄清优先于常规标注（通用契约：无法准确判断就反问，不猜）──
        if finance:
            # (a) 语义异常：相对日词落在非交易日（如周六问“今天行情”）
            _q = self._detect_anomaly(user_input, today, finish)
            if _q:
                logger.info("[TimeResolver] 语义异常反问: %s", _q[:80])
                return self._clarify(user_input, _q)
        # (b) 时间窗不明：默认窗口会实质改变结论，必须问清
        _q = self._vague_window_question(user_input, finance)
        if _q:
            logger.info("[TimeResolver] 时间窗不明反问: %s", _q[:80])
            return self._clarify(user_input, _q)

        edits: list = []      # (start, end, inline_text) —— 就地内联标注
        seen = set()
        for pattern, kind in _PATTERNS:
            m = pattern.search(user_input)
            if not m or kind in seen:
                continue
            seen.add(kind)
            try:
                out = self._resolve_one(kind, m, today, finish, finance)
            except Exception as e:
                logger.debug("[TimeResolver] %s 解析失败: %s", kind, e)
                continue
            if not out:
                continue
            inline = out                      # 2026-09-15：统一返回内联文本
            if inline:
                # 内联点钉在**时间词本身**的末尾：
                #   单日期型（今天/昨天/现在…）捕获组即整词 → m.end(1)；
                #   区间型捕获组是数字（近5…），须补上词干（个交易日/天/周/月）→ m.end(1)+词干长；
                #   二者都不含正则可选尾缀（如「的行情」）——否则日期被插到谓语后面拆散句意。
                _stem = _RANGE_STEMS.get(kind, "")
                _word_end = m.end(1) + (len(_stem) if _stem else 0) if m.groups() else m.end()
                edits.append((m.start(), _word_end, inline))

        # 就地内联（2026-09-15 用户定格式）：日期钉在**原文的时间词上**，
        # 形如 昨天(2026-09-14) / 现在(2026-09-15 13:50:00) / 近5个交易日(2026-09-08~2026-09-14)。
        # 不再追加任何尾部说明块——【时间】尾巴喧宾夺主，且盘中把 agent 引向昨日数据。
        annotated = user_input
        for start, end, inline in sorted(edits, key=lambda x: x[0], reverse=True):
            if 0 <= start < end <= len(annotated):
                annotated = f"{annotated[:end]}({inline}){annotated[end:]}"

        if finance and "today" not in seen and "now" not in seen:
            # 金融域常驻锚点（用户定格式 2026-09-15）：原文无任何时间词时补一个
            # `今天(日期)`，简洁不喧宾夺主。仅非交易日时附注最近已收盘交易日——
            # 盘中注入"最近已收盘=昨天"会误导 agent 弃实时取昨日（用户实测"无所适从"）。
            try:
                _is_td = _cal().is_trading_day(today)
            except Exception:
                _is_td = False
            if _is_td:
                annotated = f"{annotated} 今天({today})"
            else:
                annotated = f"{annotated} 今天({today}，非交易日；最近已收盘交易日={finish})"

        if annotated == user_input:
            return None

        expanded = annotated
        return ResolveResult(entities=[], entity_code="", entity_name="",
                             entity_type="time", effective_input=expanded)

    def _resolve_one(self, kind: str, m: re.Match, today: str, finish: str,
                     finance: bool) -> Optional[str]:
        """返回**内联标注文本**（2026-09-15 用户定格式）。

        格式契约：内联文本拼在原文时间词后面，形如——
          昨天 → `昨天(2026-09-14)`；前天 → `前天(2026-09-12)`
          现在 → `现在(2026-09-15 13:50:00)`（带时分秒）
          今天 → `今天(2026-09-15)`；交易日历史词注明上一交易日口径
          区间 → `近5个交易日(2026-09-08~2026-09-14)`
        解析失败返回 None（不标注）。旧版只内联单日期、区间走尾部【时间】块；
        尾部块喧宾夺主且盘中误导，已按用户裁定整体废除。
        """
        cal = _cal()
        g = m.group(1) if m.groups() else ""
        dt_today = datetime.strptime(today, "%Y-%m-%d")

        if kind == "now":
            ts = (self._now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
            return ts

        if kind == "yesterday" or kind == "prev_day":
            if finance:
                d = _shift_trade(today, 1)
                return d
            return (dt_today - timedelta(days=1)).strftime("%Y-%m-%d")

        if kind == "dbd2":
            if finance:
                return _shift_trade(today, 2)
            return (dt_today - timedelta(days=2)).strftime("%Y-%m-%d")

        if kind == "tomorrow":
            if finance:
                return _shift_trade(today, 1, future=True)
            return (dt_today + timedelta(days=1)).strftime("%Y-%m-%d")

        if kind == "after_tomorrow":
            if finance:
                return _shift_trade(today, 2, future=True)
            return (dt_today + timedelta(days=2)).strftime("%Y-%m-%d")

        if kind == "today":
            if finance and not cal.is_trading_day(today):
                return f"{today}，非交易日；最近已收盘交易日={finish}"
            return today

        if kind == "prev_trade_day":
            return finish

        # ── 区间型：内联为 起~止（2026-09-15 起同样内联）──
        if kind == "recent":
            if finance:
                d5 = _shift_trade(finish, 4)
                return f"{d5}~{finish}，近5个交易日"
            d7 = (dt_today - timedelta(days=7)).strftime("%Y-%m-%d")
            return f"{d7}~{today}，近7天"

        if kind == "recent_trade_days":
            n = max(1, min(60, int(g)))
            if finance:
                d_start = _shift_trade(finish, n - 1)
                return f"{d_start}~{finish}"
            d_start = (dt_today - timedelta(days=max(1, round(n * 7 / 5)) - 1)).strftime("%Y-%m-%d")
            return f"{d_start}~{today}，按自然日估算"

        if kind == "recent_days":
            n = max(1, min(90, int(g)))
            d_start = (dt_today - timedelta(days=n - 1)).strftime("%Y-%m-%d")
            return f"{d_start}~{today}"

        # ── 单一日期型 ──
        if kind == "days_ago":
            n = max(1, int(g))
            if finance:
                return _shift_trade(today, n)
            return (dt_today - timedelta(days=n)).strftime("%Y-%m-%d")

        if kind == "days_after":
            n = max(1, int(g))
            if finance:
                return _shift_trade(today, n, future=True)
            return (dt_today + timedelta(days=n)).strftime("%Y-%m-%d")

        if kind == "weekday_rel":
            rel, wd_char = m.group(1), m.group(2)
            wd = _WEEKDAY_CN.get(wd_char, 0)
            if rel == "上":
                last_mon = dt_today - timedelta(days=dt_today.weekday() + 7)
                return _next_weekday_after((last_mon - timedelta(days=1)).strftime("%Y-%m-%d"), wd)
            if rel == "下":
                next_mon = dt_today - timedelta(days=dt_today.weekday()) + timedelta(days=7)
                return _next_weekday_after((next_mon - timedelta(days=1)).strftime("%Y-%m-%d"), wd)
            return (dt_today + timedelta(days=(wd - dt_today.weekday()) % 7)).strftime("%Y-%m-%d")

        if kind == "weekday_abs":
            wd = _WEEKDAY_CN.get(g, 0)
            return _next_weekday_after(today, wd)

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
                return f"{ws}~{we}（交易日 {cal.trade_date_range(ws, we)}）"
            return f"{ws}~{we}"

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
            return f"{first.strftime('%Y-%m-%d')}~{end}"

        if kind == "explicit_date":
            s = re.sub(r"[年月]", "-", g).replace("日", "").replace("/", "-")
            try:
                datetime.strptime(s, "%Y-%m-%d")
            except ValueError:
                return None
            # 用户已写明确日期 → 原样即契约，不再重复内联同一日期
            return None if s in user_input else s
        return None
