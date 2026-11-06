# -*- coding: utf-8 -*-
"""
market-screener/common.py

通用基础设施：数据加载、技术指标计算、涨停/跌停/炸板池、龙回头检测。
三个策略（盘中/尾盘/盘后）共享此模块。
"""

from __future__ import annotations

from ...log import logger
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor

# ═══════════════════════════════════════════════════════════════
#  Skill 自包含数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class FactorItem:
    """单个因子的评分结果。"""
    name: str
    value: str = ""
    score: Optional[float] = None
    weight: float = 1.0
    status: str = "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "value": self.value,
            "score": self.score, "weight": self.weight, "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FactorItem":
        return cls(
            name=d.get("name", ""),
            value=str(d.get("value", "")),
            score=d.get("score"),
            weight=d.get("weight", 1.0),
            status=d.get("status", "ok"),
        )


@dataclass
class SkillReport:
    """Skill 标准化输出。"""
    skill_name: str
    score: float = 50.0
    confidence: float = 0.0
    direction: str = "neutral"
    signal: str = ""
    factors: List[FactorItem] = field(default_factory=list)
    analysis: str = ""
    output_data: Dict[str, Any] = field(default_factory=dict)
    tools_called: List[str] = field(default_factory=list)
    missing_data: List[str] = field(default_factory=list)
    status: str = "ok"
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "score": self.score, "confidence": self.confidence,
            "direction": self.direction, "signal": self.signal,
            "factors": [f.to_dict() for f in self.factors],
            "analysis": self.analysis, "output_data": self.output_data,
            "tools_called": self.tools_called, "missing_data": self.missing_data,
            "status": self.status, "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SkillReport":
        return cls(
            skill_name=d.get("skill_name", ""),
            score=d.get("score", 50.0),
            confidence=d.get("confidence", 0.0),
            direction=d.get("direction", "neutral"),
            signal=d.get("signal", ""),
            factors=[FactorItem.from_dict(f) for f in d.get("factors", [])],
            analysis=d.get("analysis", ""),
            output_data=d.get("output_data", {}),
            tools_called=d.get("tools_called", []),
            missing_data=d.get("missing_data", []),
            status=d.get("status", "ok"),
            error=d.get("error", ""),
        )

# ═══════════════════════════════════════════════════════════════
#  Markdown 渲染
# ═══════════════════════════════════════════════════════════════

_SRC_SHORT = {
    "连板": "连板", "龙回头": "龙头", "尾盘封板": "封板", "尾盘强势": "尾强",
    "盘后筛选": "筛选", "条件搜索": "搜索", "4IN1(近期涨停)": "4IN1",
}

_DIR_CN = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}


def _dir_cn(d: str) -> str:
    """英文方向 → 中文（给 LLM 与终端用户看的文案统一用中文）。"""
    return _DIR_CN.get(str(d or ""), str(d or "-"))


def _fmt(v, dec=2):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.{dec}f}"
    return str(v)


def _fmt_pct(v):
    if v is None or v == 0:
        return "-"
    return f"{v:+.1f}"


class SkillResult(dict):
    """dict 子类，代码按 dict 用，转 str 出 markdown 给 LLM。"""

    def __str__(self):
        # 错误
        if self.get("error"):
            return f"ERR: {self['error']}"

        # profile_candidates() 格式（证据卡）—— 必须排在 prescreen 之前：
        # profile bundle 自身也带 market 键，判序反了会被 _prescreen_md 吃掉（2026-09-29 实测）
        if "profiles" in self:
            return self._profile_md()

        # inspect_stock() 格式（单票深挖）
        if "tech" in self and "risks" in self:
            return self._inspect_md()

        # pre_screen() 格式
        if "candidates" in self or "market" in self:
            return self._prescreen_md()

        # deep_analyze() 格式
        if "analyzed" in self:
            return self._deep_analyze_md()

        # raw dict fallback
        return _compact_json(self)

    def __repr__(self):
        return self.__str__()

    def _prescreen_md(self):
        parts = []

        # 策略 + market 一行
        strategy = self.get("strategy", "")
        market = self.get("market", {})
        if market:
            zt = market.get("zt_count", 0)
            dt = market.get("dt_count", 0)
            fund = market.get("fund_flow", 0)
            brk = market.get("broken_rate", 0)
            m_line = (
                f"{strategy} 大盘M:{market.get('market_mood','')}({market.get('market_mood_score','')})"
                f" 个股M:{market.get('stock_mood','')}({market.get('stock_mood_score','')})"
                f" ZT:{zt} DT:{dt}"
            )
            if fund:
                m_line += f" F{fund/1e8:.1f}e"
            if brk:
                m_line += f" 炸{brk}%"
            parts.append(m_line)

        # main_themes
        themes = self.get("main_themes", [])
        if themes:
            parts.append("题材:" + ",".join(f"{t}({c})" for t, c in themes[:5]))

        # candidates
        candidates = self.get("candidates", [])
        if candidates:
            parts.append(f"候选{len(candidates)}只:")
            rows = ["C#   Name    Src  Chg%   Trn%   Price  Vol"]
            rows.append("---- ------ ---- ----- ------ ----- ---")
            for c in candidates:
                src = _SRC_SHORT.get(c.get("source", ""), c.get("source", "")[:3])
                price = c.get("price") or c.get("close") or ""
                price_s = f"{price:.2f}" if isinstance(price, (int, float)) else str(price)
                rows.append(
                    f"{c.get('code',''):6} {c.get('name',''):6} {src:4} "
                    f"{_fmt_pct(c.get('change_pct')):5} "
                    f"{_fmt_pct(c.get('turnover_pct')):4} "
                    f"{price_s:6} {_fmt(c.get('vol_ratio'),1):3}"
                )
            parts.append("\n".join(rows))

        return "\n".join(parts)

    def _inspect_md(self):
        """单票深挖结果渲染（inspect_stock）：按「结论 → 证据 → 缺口」顺序。"""
        parts = []

        def _n(v, suffix=""):
            """None → '-'，数值保持原样（避免渲染出 None 字面量误导 LLM）。"""
            return "-" if v is None else f"{v}{suffix}"

        tech = self.get("tech") or {}
        name = self.get("name") or ""
        parts.append(f"**{self.get('code','')} {name}** [{self.get('strategy','')}]")

        pred = self.get("prediction") or {}
        if pred.get("ok"):
            parts.append(f"P(T+1涨)={pred.get('p_up')}  建议方向:{_dir_cn(pred.get('suggest_dir'))}  "
                         f"预期收益:{_n(pred.get('exp_ret_bp'), 'bp')}")
        else:
            parts.append(f"P(T+1涨)=缺失（{pred.get('reason','未知')}）")

        q = self.get("quote") or {}
        if any(v is not None for v in q.values()):
            parts.append(f"报价: 价{_n(q.get('price'))} 涨{_n(q.get('change_pct'), '%')} "
                         f"换手{_n(q.get('turnover_pct'), '%')} 额{_n(q.get('amount'))}")

        if tech:
            ma_txt = ""
            if tech.get("ma20"):
                ma_txt = (f"MA5/10/20={tech.get('ma5')}/{tech.get('ma10')}/{tech.get('ma20')} "
                          f"距MA20 {tech.get('dist_ma20_pct')}%"
                          f"{'(多头排列)' if tech.get('ma_bull_stack') else ''}")
            parts.append("技术: " + " | ".join(x for x in [
                f"收{tech.get('close')} 涨{tech.get('change_pct')}%",
                f"振幅{tech.get('amplitude_pct')}% 收盘位{tech.get('close_pos_in_day')}%",
                ma_txt,
                f"RSI{tech.get('rsi14')} 量比{tech.get('vol_ratio')}",
                f"20日位置{tech.get('pos_in_20d_pct')}%"
                f"{'(20日新高)' if tech.get('is_20d_high') else ''}",
                f"5日{tech.get('ret_5d_pct')}% 20日{tech.get('ret_20d_pct')}%",
                f"连板{tech.get('zt_streak')}/前{tech.get('prev_zt_streak')}",
                "当日涨停" if tech.get("limit_up") else "",
            ] if x))

        flow = self.get("flow") or {}
        if flow.get("ok"):
            parts.append(f"资金: 量价净额{flow.get('net_wan')}万 "
                         f"占成交{flow.get('net_pct_of_amount')}%（派生口径，非主力）")
        elif flow:
            parts.append(f"资金: 缺失（{flow.get('reason')}）")

        th = self.get("theme") or {}
        if any(th.values()):
            boards = ",".join(th.get("boards") or []) or "-"
            tags = ",".join(th.get("concept_tags") or []) or "-"
            parts.append(f"题材: 行业{th.get('industry') or '-'} | 板块{boards} | 概念{tags}")

        if tech.get("regime") or tech.get("rs_d0") is not None:
            seg = []
            if tech.get("regime"):
                seg.append(f"形态[{tech['regime']}]")
            if tech.get("rs_d0") is not None:
                seg.append(f"当日相对上证{tech['rs_d0']:+}%")
            if tech.get("rs_5d") is not None:
                seg.append(f"5日超额{tech['rs_5d']:+}%")
            if tech.get("vr_rel") is not None:
                seg.append(f"量比相对大盘{tech['vr_rel']}x")
            if (self.get("flow") or {}).get("flow_rel") is not None:
                seg.append(f"资金流相对大盘{(self.get('flow') or {})['flow_rel']:+}pp")
            parts.append("相对强度: " + " ".join(seg)
                         + "（三轴：大盘弱个股强=真强；大盘强个股弱=真弱）")

        st = self.get("same_t")
        if isinstance(st, dict) and st.get("ok"):
            seg = [f"截至{st.get('t')} 量能={_n(st.get('vr_same_t'), 'x前日均值')}",
                   f"个股昨收→今{st.get('t')}涨{_n(st.get('ret_same_t'), '%')}"]
            if st.get("idx_ret_same_t") is not None:
                seg.append(f"上证同期{_n(st.get('idx_ret_same_t'), '%')}")
            if st.get("rs_same_t") is not None:
                seg.append(f"同时刻超额{_n(st.get('rs_same_t'), '%')}")
            if st.get("vr_rel_same_t") is not None:
                seg.append(f"量比相对大盘={_n(st.get('vr_rel_same_t'), 'x')}")
            if st.get("fp_same_t") is not None:
                seg.append(f"同刻净占比={_n(st.get('fp_same_t'), '%')}")
            if st.get("flow_rel_same_t") is not None:
                seg.append(f"资金流相对大盘={_n(st.get('flow_rel_same_t'), 'pp')}")
            if st.get("mkt_vr") is not None:
                seg.append(f"大盘量比={_n(st.get('mkt_vr'), 'x')}")
            if st.get("mkt_fp_pct") is not None:
                seg.append(f"大盘净占比={_n(st.get('mkt_fp_pct'), '%')}")
            parts.append("同时刻比对(" + str(st.get("days_used")) + "日前推): " + " | ".join(seg))

        zt = self.get("zt_history") or []
        if zt:
            parts.append("涨停史: " + " ".join(
                f"{z.get('date')}({z.get('continuous_days')}板)" for z in zt[:6]))

        if self.get("fund_hist"):
            parts.append("近10日逐日净额明细已附(fund_hist)")
        if self.get("dragon"):
            parts.append(f"龙虎榜席位已附(dragon, detail=True)")
        if self.get("boards"):
            parts.append(f"板块排名已附(boards, top30)")

        risks = self.get("risks") or []
        parts.append("风险: " + ("；".join(risks) if risks else "无客观风险标记"))

        missing = self.get("missing") or []
        if missing:
            parts.append("数据缺口: " + "；".join(str(m) for m in missing[:6]))
        return "\n".join(parts)

    def _profile_md(self):
        """候选证据卡渲染（profile_candidates）：一行一票，列=决策要用的事实。"""
        parts = []
        strategy = self.get("strategy", "")
        market = self.get("market", {}) or {}
        if market:
            parts.append(
                f"{strategy} 大盘M:{market.get('market_mood','')}({market.get('market_mood_score','')}) "
                f"个股M:{market.get('stock_mood','')}({market.get('stock_mood_score','')}) "
                f"ZT:{market.get('zt_count',0)} DT:{market.get('dt_count',0)} "
                f"炸{market.get('broken_rate',0)}% F{_fmt(market.get('fund_flow',0)/1e8,1)}e"
            )
        themes = self.get("themes") or []
        if themes:
            parts.append("主线题材: " + ",".join(themes))

        hints = self.get("hints") or {}
        if hints:
            parts.append(
                f"参考门槛(非硬约束): p_up≥{hints.get('p_up_floor_suggest')} "
                f"建议≤{hints.get('max_picks_suggest')}只 情绪桶:{self.get('mood_regime','')}"
            )

        profiles = self.get("profiles") or []
        if not profiles:
            parts.append("无候选画像")
        else:
            rows = ["CODE   NAME     SRC      CHG%   TRN%   额万      P↑     位置%   DISP%  RSI   量比  连板  主线命中/障碍"]
            rows.append("------ -------- -------- ------ ------ -------- ------- ------ ------ ----- ----- ---- -------------")
            for p in profiles:
                t = p.get("tech") or {}
                th = p.get("theme") or {}
                hard = ",".join(p.get("hard") or []) or "-"
                warn = p.get("warnings") or []
                warn_s = (" | 软提示:" + ",".join(warn)) if warn else ""
                hit = ("√" + ",".join(th.get("tags") or [])) if th.get("hit") else "-"
                rows.append(
                    f"{p.get('code',''):6} {str(p.get('name',''))[:7]:8} {str(p.get('source',''))[:7]:8} "
                    f"{_fmt_pct(p.get('change_pct')):>6} {_fmt_pct(p.get('turnover_pct')):>6} "
                    f"{_fmt(p.get('amount_wan'),0):>8} "
                    f"{str(p.get('p_up') if p.get('p_up') is not None else '-'):>7} "
                    f"{_fmt(t.get('pos_in_20d_pct'),1):>6} {_fmt(t.get('dist_ma20_pct'),1):>6} "
                    f"{_fmt(t.get('rsi14'),0):>5} {_fmt(t.get('vol_ratio'),1):>5} "
                    f"{t.get('zt_streak','-')!s:>4}  {hit} | 硬:{hard}{warn_s}"
                )
            rows.append("(P↑=P(次日涨)；列仅为事实排序参考，取舍由你决定；missing 见每行末尾)")
            parts.append("\n".join(rows))
            miss_rows = [(p.get("code"), p.get("missing")) for p in profiles if p.get("missing")]
            if miss_rows:
                parts.append("数据缺口: " + "；".join(
                    f"{c}:{','.join(str(x) for x in m[:2])}" for c, m in miss_rows[:8]))

        trimmed = self.get("trimmed") or 0
        if trimmed:
            parts.append(f"注: 因 limit 被裁掉 {trimmed} 只（pool={self.get('pool_size')}）")
        if self.get("missing"):
            parts.append("全局缺口: " + "；".join(str(m) for m in self["missing"][:4]))
        if self.get("errors"):
            parts.append("错误: " + "；".join(
                f"{e.get('code')}:{e.get('error')}" for e in self["errors"][:4]))
        return "\n".join(parts)

    def _deep_analyze_md(self):
        parts = []
        s = self.get("score", 0)
        d = self.get("direction", "")
        cnf = self.get("confidence", 0)
        sig = self.get("signal", "")
        strategy = self.get("strategy", "")
        parts.append(f"**{strategy}**")
        parts.append(f"  综合评分: {s:.1f}/100")
        parts.append(f"  方向: {d}")
        parts.append("")

        analyzed = self.get("analyzed", [])
        if analyzed:
            parts.append("股票代码\t股票名称\t评分\t方向\t置信度\t信号")
            for a in analyzed:
                code = a.get("code", "")
                name = a.get("name", "")
                score = a.get("score", 0)
                direction = a.get("direction", "")
                confidence = a.get("confidence", "")
                signal = (a.get("signal", "") or "")
                parts.append(f"{code}\t{name}\t{score}\t{direction}\t{confidence}\t{signal}")

        return "\n".join(parts)


def _compact_json(d, max_depth=2, _depth=0):
    if _depth >= max_depth or not isinstance(d, dict):
        return str(d)
    items = []
    for k, v in d.items():
        if isinstance(v, (list, dict)) and len(str(v)) > 80 and _depth > 0:
            items.append(f"{k}: [{type(v).__name__}({len(v)})]")
        else:
            items.append(f"{k}: {_compact_json(v, max_depth, _depth+1)}")
    return "{" + ", ".join(items) + "}"


# ═══════════════════════════════════════════════════════════════
#  路径与环境
# ═══════════════════════════════════════════════════════════════

# ⚠ 层数必须数对：本文件在 app/agent/skills/market-screener/ 下，
# 往上是 skills → agent → app → backend_api_python，即 **4 层** 才到 backend 根。
# （2026-10-02 事故：曾写成 3 层 = 插成了 `app/`，于是 `import utils` 命中
#  **app/utils** 而不是 app/agent/utils ⇒ 9 个 finance 工具模块的
#  `from utils.md_format import ...` 全部 ModuleNotFoundError，被 ToolProvider
#  **静默跳过**，工具面从 101 掉到 91、能力层从 40 虚增到 62 补位掩盖缺口。）
_backend_root = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..")
)
# 【2026-10-13·P5】**不再**把 backend 根插进 sys.path。
# 本文件是技能模块，由 QDSkillAdapter 以 `app.agent.skills.*` 加载，
# 包内一律相对导入；历史上那次"插 app/ 保序"的补丁正是 2026-10-02 事故的成因，
# 现在既不必要也不允许（红线 R3 会拦）。`_backend_root` 仅保留给下面 _load_env
# 定位 .env 用（纯路径计算，不动 sys.path）。


def _load_env():
    try:
        from dotenv import load_dotenv
        for p in [
            os.path.join(_backend_root, ".env"),
            os.path.join(os.path.dirname(_backend_root), ".env"),
        ]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass
_writer_cache = None
_basic_db_cache = None
def _get_writer():
    global _writer_cache
    if _writer_cache is not None:
        return _writer_cache
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    _writer_cache = get_market_kline_writer()
    return _writer_cache
def _get_basic_db():
    global _basic_db_cache
    if _basic_db_cache is not None:
        return _basic_db_cache
    _load_env()
    from app.utils.basicinfo_db import get_stock_basic_db
    _basic_db_cache = get_stock_basic_db()
    return _basic_db_cache
# ═══════════════════════════════════════════════════════════════
#  CNStock 行情库连接池预热（2026-10-25 P0）
# ═══════════════════════════════════════════════════════════════
#
# 根因（2026-10-24 探针实锤 tmp/probe_sql_concurrency_split_20261024.py）：
# psycopg2 ThreadedConnectionPool._putconn 只保留 minconn(=2) 个空闲连接
# （psycopg2/pool.py:105，超出即 conn.close()），且 getconn 持全局锁执行
# _connect（TCP + PG 后端 fork + 认证）。画像期 _FETCH_WORKERS=8 并发取
# K 线时每波 6 建 6 关、创建锁内串行 ⇒ 单次 read_db 0.30s（同 SQL 串行
# 1.4ms；PG 侧 EXPLAIN 仅 0.386ms、索引无恙——劣化全在池层）。
# 本 skill 画像期固定 8 线程并发单股取数 ⇒ 入口把 CNStock 池空闲连接
# 预热到同值，消除连接抖动。限定本 skill 生效：不动 db_multi 的 POOL_MIN
# 缺省与 env（全局池配置影响所有调用方，不在本技能授权内）。
_FETCH_POOL_MIN = 8   # 与 _helpers._FETCH_WORKERS 对齐；改动需同步两侧


def warm_market_pool(workers: int = _FETCH_POOL_MIN) -> None:
    """把 CNStock 行情库连接池的空闲连接预热到 workers 条（P0）。

    先抬 minconn 再补连接：_putconn 见空闲已满 minconn 会把多出的连接
    直接 close，顺序反了预热白做。失败不抛（预热是优化不是关键路径），
    记 warning 后按原 2 连接池行为继续。
    """
    try:
        _load_env()
        # 连接池内部细节属数据层：预热逻辑下沉到 screen_query（skill 层零直连）
        from app.data_sources import screen_query as _sq
        _sq.warm_pool(workers)
    except Exception as e:
        logger.warning(
            "[market-screener] CNStock 连接池预热失败（按原池行为继续）: %s", e)
def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _data_day() -> str:
    """本次分析的**目标交易日**（三池取数日期唯一来源）。

    语义（见下方实现注释）：盘中/盘后 → 今日；开盘前/非交易日 → 上一个交易日。

    ★ 2026-10-31 实锤：三池（涨停/跌停/炸板）的取数不能用 `_today_str()`。
      实测 `get_zt_pool("2026-10-30")` = 47 只（含 open_count / seal_amount），
      `get_zt_pool("2026-10-31")` = 0（周六）——而整份分析（日线、资金流、
      画像）用的都是 10-30 的数据。日期口径错位 ⇒ 三个后果：
        · 封板结构（open_count / seal_amount）恒 unknown；
        · `market.zt_count` 恒 0，模型据此写「涨停 0」，与它自己列出的
          涨停票自相矛盾；
        · `zt` / `eod_zt` 两个候选源恒空，候选池只剩 search + dragon。
      周末/节假日是**最常用**的提问场景（"明天买什么"多在盘后问），故必须对齐。

    日历不可用（`CalendarUnavailable`）时按「回落到今天 + 记 warning」降级：
    宁可用一个可能取不到池的日期，也不要把整条链路静默塌成空池。
    """
    today = _today_str()
    try:
        # 语义整体下沉到 trading_calendar.current_data_day（该文件内的统一实现，
        # 此处只保留「日历不可用」的降级）：盘中/午休/盘后 → 今日；开盘前/非交易日
        # → 上一交易日。勿在本文件拼 is_in_quote_session + last_finish_trading_day：
        # 前者午休不算（会把午休退回昨天），后者盘中不算（会把盘中退回昨天）。
        from app.utils.trading_calendar import current_data_day
        return current_data_day() or today
    except Exception as e:      # 日历不可用：由本调用方显式降级，不静默吞
        logger.warning("[MktScreen] 交易日历不可用，目标交易日回落到今天 %s: %s",
                       today, e)
        return today
# ═══════════════════════════════════════════════════════════════
#  工具分发
# ═══════════════════════════════════════════════════════════════

# 2026-09-19：修正过时的 import 路径——工具已迁至 tools/finance/ 子目录，
# 旧路径 app.agent.tools.screener_tools 不存在，导致整个 run.py 导入失败
# （_load_skill_functions 静默返回 []，skill 工具全部不注入沙箱 → 模型调用被幻觉拦截）。
from ...tools.finance.screener_tools import search_stocks
from ...tools.finance.analysis_tools import _get_indicator_snapshot
from app.market_cn.tape import get_fund_flow_realtime

_TOOL_REGISTRY = {
    "get_fund_flow_realtime": get_fund_flow_realtime,
    "get_indicator_snapshot": _get_indicator_snapshot,
    "search_stocks": search_stocks,
}
def call_tool(name: str, **kwargs) -> Any:
    """按名称分发工具调用。"""
    fn = _TOOL_REGISTRY.get(name)
    if fn is None:
        return {"error": f"未知工具: {name}"}
    try:
        return fn(**kwargs)
    except Exception as e:
        logger.warning("[market-screener] 工具 %s 调用失败: %s", name, e)
        return {"error": str(e)}
# ═══════════════════════════════════════════════════════════════
#  通用数据采集
# ═══════════════════════════════════════════════════════════════

def fetch_kline(code: str, days: int = 60) -> List[Dict]:
    """个股日线（前复权），统一走 `CNStockDataSource.get_kline` 门面。

    2026-10-18 F23 收口：原实现直读 db_market writer（不复权原始价）+ 自调
    `unadj_to_qfq`，是 agent 内部最后一处绕开门面的 K 线取数。现改为经门面，
    与追责链（evaluator）/ C3P（kline_feed）/ 工具面（agent_get_kline）同源：
    DB 优先 + snapshot lastbar（盘中）+ 远端补满（DB 不够新时）+ 出口 qfq。
    - **契约不变**：`days` = 回溯根数（_facts.multitimeframe docstring「日线
      回溯根数」）⇒ 直传 `limit=days`。旧实现用 `days*1.5` 日历窗近似，实测
      给 days-1 根；limit 语义更准且只多不少。
    - **返回形状不变**：`time` 为 `YYYY-MM-DD` 日期串（下游 `_norm_bars` 按此
      转 int 时间戳给 predict）。门面 1D 出口 `time` 是 int 时间戳 ⇒ 在此转串，
      转换不外溢给调用方。时区显式取北京时间（对齐门面 `_read_db` 的
      `ZoneInfo("Asia/Shanghai")`，不依赖部署机本地时区）。
    - **失败语义不变**：取不到返回 []（不抛）。
    对拍基线：tmp/fetch_kline_baseline_20261018.json（改造前输出），
    逐键比对脚本 tmp/fetch_kline_compare_20261018.py，值零差异。
    """
    from zoneinfo import ZoneInfo
    try:
        from app.data_sources.cn_stock import CNStockDataSource
        raw = CNStockDataSource().get_kline(code, "1D", int(days)) or []
    except Exception:
        return []
    _bj = ZoneInfo("Asia/Shanghai")
    bars: List[Dict] = []
    for k in raw:
        if not isinstance(k, dict):
            continue
        t = k.get("time")
        if isinstance(t, (int, float)) and not isinstance(t, bool):
            # 门面 1D 行 time = int epoch（_read_db 按北京时区生成）
            try:
                ts = datetime.fromtimestamp(int(t), tz=_bj).strftime("%Y-%m-%d")
            except (ValueError, OSError, OverflowError):
                continue
        else:
            ts = str(t or "")[:10]     # 容错：门面若直出日期串则原样取前 10 位
        if not ts:
            continue
        bars.append({
            "time": ts, "open": k.get("open"), "high": k.get("high"),
            "low": k.get("low"), "close": k.get("close"), "volume": k.get("volume"),
        })
    return bars
def get_limit_pct(code: str, name: str = "") -> float:
    """涨跌停幅度（%）。返回 0.0 = 无涨跌幅限制。

    2026-10-15 实测重写（见 references/limit-rules.md）：
    · ST/风险警示**跟随所在板**（主板 ST 亦 ±10%，创业/科创 ST ±20%），不再单独 ±5%
    · 新股 N=上市首日 / C=注册制第 2~5 日：无涨跌幅（返回 0）
    · 仅作 rt 缺失时的近似；有 rt.limit_*_price 时以 rt 为准
    """
    code = (code or "").strip()
    name = (name or "").strip()
    # 新股标记（N联亚药业 / C粤芯半导体-U）；A 股简称 N/C 后为中文
    if len(name) >= 2 and name[0] in ("N", "C"):
        return 0.0
    if code.startswith(("300", "301")):
        return 20.0
    if code.startswith(("688", "689")):
        return 20.0
    if code.startswith(("8", "4", "92")):
        return 30.0
    return 10.0


def is_limit_locked(code: str, name: str, close: float, prev_close: float) -> bool:
    """判断是否涨停封板（买不进去）。"""
    if prev_close <= 0 or close <= 0:
        return False
    limit_pct = get_limit_pct(code, name)
    if limit_pct <= 0:
        return False  # 无涨跌幅（新股），不存在「封板买不进」
    change_pct = (close - prev_close) / prev_close * 100
    return change_pct >= limit_pct - 0.5
# ── 四源取数（2026-10-21 fail-fast 收口）──
# 旧版在此吞异常返回 [] / {"error":...}，调用方（candidate_pool / market_state）
# 无法区分「源故障」与「真空无候选」⇒ 数据不可用被交付成「今日无候选」。
# 现一律向上抛，由调用方记 missing / source_errors（_facts.py 同构处理）。
# ═══════════════════════════════════════════════════════════════
#  三池（涨停/跌停/炸板）取数（2026-10-31 改走 data_sources 链路）
# ═══════════════════════════════════════════════════════════════
#
# 2026-10-25 曾在此做过一份 JSON 落盘缓存（data/pool_cache/），2026-10-31 撤除：
# 三池落库改由 data_sources 承担（表 cnd_limit_pool_list，同一份数据只存一处），
# 读侧统一经 cn_stock_ext.limit_pool，写侧 cn_stock_putdb.sync_limit_pools 批量补齐。
# 保留此处注释是为了说明「不要再在技能侧加一层缓存」——那会重新制造双份真源。


def _limit_pool(kind: str, date: str) -> List[Dict]:
    """三池统一取数：库优先 → coord_pool 远端兜底 → 已完结日回填库。

    2026-10-31 收口（用户裁定：改走 data_sources 链路）：此前技能侧自己写 JSON
    落盘缓存，与库表 cnd_limit_pool_list 双份冗余、且只有本技能享受。现统一走
    `cn_stock_ext.limit_pool` —— 读库 1~2ms，未命中才回源，回填由同层负责；
    批量补齐另由 `cn_stock_putdb.sync_limit_pools`（按交易日）承担。
    fail-fast 不变：取不到数向上抛（含远端失败），由调用方记 missing/source_errors。
    """
    from app.data_sources.cn_stock_ext import limit_pool
    return limit_pool(kind, date)


def fetch_zt_pool(date: str) -> List[Dict]:
    """涨停池。fail-fast：失败向上抛（调用方负责记 missing，不静默吞成空池）。"""
    return _limit_pool("limit_up_pool", date)


def fetch_dt_pool(date: str) -> List[Dict]:
    """跌停池。fail-fast：同 fetch_zt_pool。"""
    return _limit_pool("limit_down_pool", date)


def fetch_broken_board(date: str) -> List[Dict]:
    """炸板池。fail-fast：同 fetch_zt_pool。"""
    return _limit_pool("broken_board_pool", date)
def fetch_hot_stocks_with_reason(date: str) -> Dict:
    """同花顺强势股+题材。fail-fast：HTTP 错误码与网络异常一律抛错。

    旧版返回 {"error":...} 而 candidate_pool 不检查该键 ⇒ 源故障静默塌缩成
    「热点真空」（2026-10-21 缺陷），故错误码改为抛错，与 zt/dt/broken 同构。
    """
    import requests as _req
    url = (
        f"http://zx.10jqka.com.cn/event/api/getharden/"
        f"date/{date}/orderby/date/orderway/desc/charset/GBK/"
    )
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0"}
    r = _req.get(url, headers=headers, timeout=10)
    data = r.json()
    if data.get("errocode", 0) != 0:
        raise RuntimeError(f"同花顺错误: {data.get('errormsg', '')}")
    rows = data.get("data") or []
    stocks = [{
        "code": row.get("code", ""), "name": row.get("name", ""),
        "reason": row.get("reason", ""),
        "change_pct": float(row.get("zhangfu", 0) or 0),
        "turnover_pct": float(row.get("huanshou", 0) or 0),
        "amount": float(row.get("chengjiaoe", 0) or 0),
    } for row in rows]
    tag_counter: Counter = Counter()
    for s in stocks:
        if s["reason"]:
            tags = [t.strip() for t in s["reason"].replace("，", "+").replace(",", "+").split("+") if t.strip()]
            tag_counter.update(tags)
    return {"stocks": stocks, "hot_tags": tag_counter.most_common(20)}
def fetch_hot_sectors() -> Dict:
    try:
        from app.market_cn.china_market import get_hot_sectors
        return get_hot_sectors(industry_limit=15, concept_limit=15)
    except Exception as e:
        logger.warning("[MktScreen] 热门板块获取失败: %s", e)
        return {"error": str(e)}
# ═══════════════════════════════════════════════════════════════
#  技术指标计算
# ═══════════════════════════════════════════════════════════════

def compute_ma(closes: List[float], period: int) -> List[Optional[float]]:
    n = len(closes)
    ma = [None] * n
    for i in range(period - 1, n):
        ma[i] = sum(closes[i - period + 1:i + 1]) / period
    return ma
def compute_ema(values: List[float], period: int) -> List[float]:
    n = len(values)
    if n == 0:
        return []
    ema = [values[0]]
    k = 2.0 / (period + 1)
    for i in range(1, n):
        ema.append(values[i] * k + ema[-1] * (1 - k))
    return ema
def compute_macd(closes: List[float]) -> Dict[str, List[float]]:
    ema12 = compute_ema(closes, 12)
    ema26 = compute_ema(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = compute_ema(dif, 9)
    macd_bar = [2 * (d - e) for d, e in zip(dif, dea)]
    return {"dif": dif, "dea": dea, "macd": macd_bar}
def compute_rsi(closes: List[float], period: int = 14) -> List[float]:
    n = len(closes)
    if n < period + 1:
        return [50.0] * n
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains) / period
    al = sum(losses) / period
    rsi = [50.0] * (period + 1)
    rsi[period] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    alpha = 1.0 / period
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        ag = alpha * max(d, 0.0) + (1 - alpha) * ag
        al = alpha * max(-d, 0.0) + (1 - alpha) * al
        rsi.append(100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al))
    return rsi
def compute_volume_ratio(volumes: List[float], window: int = 5) -> List[float]:
    n = len(volumes)
    vr = [0.0] * n
    for i in range(window, n):
        avg = sum(volumes[i - window:i]) / window
        if avg > 0:
            vr[i] = volumes[i] / avg
    return vr
def compute_kdj(bars: List[Dict], period: int = 9) -> Dict[str, List[float]]:
    n = len(bars)
    k_vals = [50.0] * n
    d_vals = [50.0] * n
    j_vals = [50.0] * n
    for i in range(period - 1, n):
        high_max = max(b["high"] for b in bars[i - period + 1:i + 1])
        low_min = min(b["low"] for b in bars[i - period + 1:i + 1])
        if high_max == low_min:
            rsv = 50.0
        else:
            rsv = (bars[i]["close"] - low_min) / (high_max - low_min) * 100
        k_vals[i] = 2 / 3 * k_vals[i - 1] + 1 / 3 * rsv
        d_vals[i] = 2 / 3 * d_vals[i - 1] + 1 / 3 * k_vals[i]
        j_vals[i] = 3 * k_vals[i] - 2 * d_vals[i]
    return {"k": k_vals, "d": d_vals, "j": j_vals}
def compute_atr(bars: List[Dict], period: int = 14) -> List[float]:
    n = len(bars)
    trs = [0.0] * n
    for i in range(1, n):
        hl = bars[i]["high"] - bars[i]["low"]
        hc = abs(bars[i]["high"] - bars[i - 1]["close"])
        lc = abs(bars[i]["low"] - bars[i - 1]["close"])
        trs[i] = max(hl, hc, lc)
    atrs = [0.0] * n
    if n > period:
        atrs[period] = sum(trs[1:period + 1]) / period
        for i in range(period + 1, n):
            atrs[i] = (atrs[i - 1] * (period - 1) + trs[i]) / period
    return atrs
# ═══════════════════════════════════════════════════════════════
#  龙回头弱转强检测（盘中 + 盘后共享）
# ═══════════════════════════════════════════════════════════════

def _recent_trading_days(n: int) -> List[str]:
    """近 n 个交易日（含目标交易日），倒序。日历不可用 → 自然日回退并告警。"""
    try:
        from app.utils.trading_calendar import current_data_day, prev_trading_day
        day = current_data_day()
        out: List[str] = []
        for _ in range(n):
            out.append(day)
            day = prev_trading_day(day)
        return out
    except Exception as e:
        logger.warning("[MktScreen] 交易日历不可用，近N日涨停池回退自然日回溯: %s", e)
        now = datetime.now()
        return [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def fetch_recent_zt_pools(days: int = 8) -> Dict[str, List[Dict]]:
    """近 N 个**交易日**涨停池（龙回头输入）。

    容错口径（2026-10-21，配合四源 fail-fast）：单日失败跳过并留痕（历史某日
    缺数据不致命）；但**全部日期都失败**时必须抛出——否则源故障会塌缩成
    「近 8 日无涨停」，龙回头源随之静默空手而归（源失败显性化同一纪律）。

    ★ 2026-10-31：回溯单位由「自然日」改为「交易日」。旧实现按自然日取 8 天，
      其中 2~3 天是周末/节假日，每天仍要真拉一次远端等 4~5s 拿空 —— 实测
      `fetch_recent_zt_pools(8)` 8.4~11.1s，几乎全耗在非交易日的空拉上。
      改为交易日回溯后，历史日一律命中按日落盘缓存（1~2ms/日）。
    """
    pools = {}
    last_err: Optional[Exception] = None
    for d in _recent_trading_days(days):
        try:
            pool = fetch_zt_pool(d)
        except Exception as e:
            last_err = e
            logger.warning("[MktScreen] 近N日涨停池 %s 获取失败: %s", d, e)
            continue
        if pool:
            pools[d] = pool
    if not pools and last_err is not None:
        raise last_err
    return pools
def scan_lhb(date: str = None, days: int = 10) -> List[Dict]:
    """近 N 个交易日龙虎榜上榜股（按上榜次数 + 累计净买入聚合；供 candidate_pool 的 lhb 源）。

    Args:
        date: 截止交易日（含）；空 = 库内最新交易日。
        days: 回看交易日数，默认 10。

    Returns:
        [{code,name,source="龙虎榜",reason,count(上榜行数),net(累计净买入元),
          last_date,change_pct,turnover_pct}, ...]，按 (count, net) 降序。
    """
    from app.data_sources.dragon_tiger_local import query_dragon_tiger_last_days
    rows = query_dragon_tiger_last_days(trade_date=(date or ""), days=days) or []
    agg: Dict[str, Dict] = {}
    for r in rows:
        code = str(r.get("stock_code") or "")
        if len(code) != 6:
            continue
        a = agg.get(code)
        if a is None:
            a = agg[code] = {"code": code, "name": r.get("stock_name") or "",
                             "source": "龙虎榜", "reason": "", "count": 0, "net": 0.0,
                             "last_date": "", "change_pct": None, "turnover_pct": None}
        a["count"] += 1
        a["net"] += float(r.get("net_amount") or 0)
        td = str(r.get("trade_date") or "")
        if td > a["last_date"]:
            a["last_date"] = td
            a["reason"] = r.get("reason") or a["reason"]
            a["change_pct"] = r.get("change_percent")
            a["turnover_pct"] = r.get("turnover_rate")
    out = list(agg.values())
    out.sort(key=lambda x: (x["count"], x["net"]), reverse=True)
    return out


def scan_dragon_pullback(date: str) -> List[Dict]:
    recent_pools = fetch_recent_zt_pools(8)
    code_history: Dict[str, List[Dict]] = {}

    for pool_date, pool in recent_pools.items():
        for s in pool:
            code = s.get("stock_code", "")
            if not code:
                continue
            if code not in code_history:
                code_history[code] = []
            code_history[code].append({
                "date": pool_date,
                "continuous_days": int(s.get("continuous_zt_days", 1) or 1),
                "reason": s.get("reason", ""),
                "name": s.get("stock_name", ""),
            })

    dragon_codes = {}
    for code, records in code_history.items():
        max_days = max(r["continuous_days"] for r in records)
        if max_days >= 2:
            dragon_codes[code] = {
                "name": records[0]["name"],
                "max_continuous_days": max_days,
                "zt_dates": [r["date"] for r in records],
                "last_zt_date": max(records[0]["date"], date),
                "reason": records[0]["reason"],
            }

    if not dragon_codes:
        return []

    candidates = []
    for code, info in dragon_codes.items():
        bars = fetch_kline(code, days=30)
        if len(bars) < 10:
            continue

        closes = [b["close"] for b in bars]
        volumes = [b["volume"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        n = len(bars)
        i = n - 1

        lookback_start = max(0, n - 15)
        peak_idx = lookback_start
        for j in range(lookback_start, n):
            if highs[j] > highs[peak_idx]:
                peak_idx = j

        if peak_idx >= i:
            continue

        peak_price = highs[peak_idx]
        current_close = closes[i]
        trough_price = min(lows[peak_idx + 1:i + 1]) if peak_idx + 1 <= i else current_close
        pullback_pct = (peak_price - trough_price) / peak_price * 100

        if pullback_pct < 8 or pullback_pct > 35:
            continue

        signals = []
        strength_score = 0

        pullback_volumes = volumes[peak_idx + 1:i]
        avg_pullback_vol = sum(pullback_volumes) / len(pullback_volumes) if pullback_volumes else 1
        vol_ratio_today = volumes[i] / avg_pullback_vol if avg_pullback_vol > 0 else 1
        if vol_ratio_today > 1.5:
            signals.append(f"放量{vol_ratio_today:.1f}倍")
            strength_score += 15
        elif vol_ratio_today > 1.2:
            signals.append(f"温和放量{vol_ratio_today:.1f}倍")
            strength_score += 8

        if closes[i] > bars[i]["open"]:
            signals.append("收阳")
            strength_score += 5

        ma5 = compute_ma(closes, 5)
        if ma5[i] is not None and closes[i] > ma5[i]:
            signals.append("站上MA5")
            strength_score += 8

        if ma5[i] is not None and ma5[i - 1] is not None:
            ma5_slope_today = (ma5[i] - ma5[i - 1]) / ma5[i - 1] * 100 if ma5[i - 1] > 0 else 0
            if ma5_slope_today > 0:
                signals.append("MA5拐头")
                strength_score += 5

        rsi = compute_rsi(closes)
        if rsi[i] > 40 and rsi[i - 1] < 40:
            signals.append(f"RSI低位回升{rsi[i]:.0f}")
            strength_score += 10
        elif 40 <= rsi[i] <= 60:
            signals.append(f"RSI{rsi[i]:.0f}中性")
            strength_score += 3

        if len(pullback_volumes) >= 2:
            vol_declining = all(
                pullback_volumes[j] <= pullback_volumes[j - 1] * 1.1
                for j in range(1, len(pullback_volumes))
            )
            if vol_declining:
                signals.append("回调缩量(卖盘衰竭)")
                strength_score += 10

        if ma5[i] is not None and lows[i] <= ma5[i] * 1.01 and closes[i] > ma5[i]:
            signals.append("均线支撑")
            strength_score += 8

        if len(signals) < 2:
            continue

        candidates.append({
            "code": code, "name": info["name"], "source": "龙回头",
            "max_continuous_days": info["max_continuous_days"],
            "zt_dates": info["zt_dates"], "reason": info["reason"],
            "pullback_pct": round(pullback_pct, 1),
            "peak_price": round(peak_price, 3),
            "trough_price": round(trough_price, 3),
            "close": round(closes[i], 3),
            "vol_ratio_today": round(vol_ratio_today, 2),
            "rsi": round(rsi[i], 2),
            "signals": signals, "strength_score": strength_score,
            "evaluation": {
                "score": strength_score,
                "highlights": signals,
                "warnings": [] if strength_score >= 40 else ["强度偏低，谨慎"],
            },
        })

    candidates.sort(key=lambda x: -x["strength_score"])
    return candidates




def _eod_start_qfq(code: str, items: List) -> List:
    """[(日期, 价格|None), ...] → 前复权价格列表（None 原样透传）。

    kline_1m/kline_1D 库表均存**不复权原始价**（cn_stock._get_kline_intraday 注
    同一口径）；「未复权不能直接用」（2026-11-02 用户裁定）：与 facade 出口同款
    `unadj_to_qfq` 收口。prev_close 记前一交易日、当日价记扫描日 —— 除权日两侧
    因子不同，日涨幅/涨停价由此还原成行情显示口径；同一日内的价同因子，比值不变。
    """
    from app.data_sources.provider.adjustment import unadj_to_qfq
    idx = [i for i, it in enumerate(items) if it[0] and it[1] is not None]
    bars = [{"time": items[i][0], "open": items[i][1], "high": items[i][1],
             "low": items[i][1], "close": items[i][1], "volume": 0} for i in idx]
    conv = unadj_to_qfq(bars, code)
    out: List = [None] * len(items)
    for k, i in enumerate(idx):
        out[i] = conv[k]["close"]
    return out


def _eod_start_facts(date: str) -> List[Dict]:
    """尾盘启动扫描的分钟事实（每票一行）：现价/最高/上午量/尾盘量/尾盘净流/末拍。

    双路（_helpers._snap_same_t 同源纪律 + 2026-11-02 用户裁定「尾盘用 realtime」）：
    目标日=当日 → realtime_snapshot **优先**（尾盘 14:30-15:00 kline_1m 尚未盘后
    回填、且 ETL 15:01~15:30 半截窗口不可用，当日事实只能走实时），snapshot 无
    数据才落 1m；目标日=历史 → kline_1m 优先（盘后回填的完整日），snapshot 只兜底。
    当日分子分母同源，无跨源量偏差。
    1m 路的 prev_close = 上一交易日 1m 末拍（与行情 previousClose 互证为交易所收盘
    真值），出口经 _eod_start_qfq 转前复权后返回；snapshot 路的 previousClose 本就
    是交易所除权后昨收、当日价=复权锚点，无需再转。**不用 kline_1D 取前收**：实测
    2026-11-02 全市场 827 只 1D 前收与行情/1m 不一致，成因是 1D 行残写（非复权差 ——
    争议日复权因子恒等、300640/000702 无除权、偏差双向），1m+snapshot+行情三方互证
    为真源；300640 案例 1D 前收 5.16 → 涨幅算成 4.65%，真实 5.02 → 7.57%。
    snapshot 路自带 previousClose / name。量=股；pm_flow=Σ±股×价格方向（量价方向
    口径，与 fund_flow 本地派生同构，非东财主力口径）。
    """
    from app.data_sources import screen_query as _sq

    def _from_snap() -> Dict[str, Dict]:
        out: Dict[str, Dict] = {}
        raw = _sq.eod_start_raw(date, which="snap").get("snap") or {}
        for code, r in raw.items():
            out[code] = {
                "code": code, "name": str(r.get("name") or ""),
                "prev_close": r.get("prev_close"), "last": r.get("last"),
                "day_high": r.get("day_high"), "v_am": float(r.get("v_am") or 0),
                "v_pm": float(r.get("v_pm") or 0), "pm_flow": float(r.get("pm_flow") or 0),
                "last_t": r.get("last_t"),
            }
        return out

    def _from_1m() -> Dict[str, Dict]:
        out: Dict[str, Dict] = {}
        raw = _sq.eod_start_raw(date, which="1m")
        rows = raw.get("m1") or {}
        if not rows:
            return out
        prev: Dict[str, Any] = raw.get("prev") or {}
        for code, r in rows.items():
            pclose, pt_ = prev.get(code, (None, None))
            prev_date = str(pt_)[:10] if pt_ else None
            q_prev, q_last, q_high = _eod_start_qfq(
                code, [(prev_date, pclose), (date, r.get("last")),
                       (date, r.get("day_high"))])
            out[code] = {
                "code": code, "name": "",
                "prev_close": q_prev, "last": q_last,
                "day_high": q_high, "v_am": float(r.get("v_am") or 0),
                "v_pm": float(r.get("v_pm") or 0), "pm_flow": float(r.get("pm_flow") or 0),
                "last_t": r.get("last_t"),
            }
        return out

    facts = (_from_snap() or _from_1m()) if date == _data_day() \
        else (_from_1m() or _from_snap())
    return list(facts.values())


def _eod_start_prior8(code: str, date: str) -> Optional[float]:
    """前 8 个交易日（不含当日）累计涨跌%（qfq）；前史不足 9 根返回 None。

    不足 9 根同时是 N/C 新股（上市 ≤5 日）的天然排除 —— 不依赖名称识别新股。
    """
    bars = fetch_kline(code, days=60)
    prior = [b for b in bars if (b.get("time") or "") < date and b.get("close")]
    if len(prior) < 9:
        return None
    c0, c1 = float(prior[-9]["close"]), float(prior[-1]["close"])
    if c0 <= 0:
        return None
    return (c1 / c0 - 1) * 100


def scan_eod_start(date: str = None) -> List[Dict]:
    """尾盘启动扫描：行情刚启动 + 尾盘资金流入 + 未涨停（eod 候选源 eod_start）。

    语义（2026-11-02 用户拍板，参照 605333@2026-10-23 / 002487@2026-09-10）：
    找尾盘明显资金流入、当日涨幅适中但未涨停、启动前处于横盘/缓跌的票 —— 买在
    启动日尾盘吃次日延续（参照股 3 日 +26.5% / +17.6%）。

    核心 5 条判据（阈值 env 可调 QD_EOD_START_*）：
      1. 涨幅带：当日涨幅 ∈ [RET_MIN, RET_MAX]%（默认 2~8，现价口径）
      2. 未触板：当日最高价 < 涨停价（触过板不算「未涨停」）
      3. 刚启动：前 8 个交易日（不含当日）累计 ∈ [PRIOR8_MIN, PRIOR8_MAX]%
         （默认 -15~+6，横盘/缓跌底部区；前史不足自动跳过，兼排 N/C 新股）
      4. 尾盘放量：尾盘(≥14:30)每分钟量 ÷ 上午每分钟量 ≥ PM_RATE_MIN(1.05)
         且 ≥ PM_MED_MULT(1.15) × 当日截面中位强度。满日口径「尾盘量占比 ≥13%
         且 ≥1.15×截面中位」即此强度比（share13% ⇔ 强度 1.05，30/210 时长归一）；
         用强度比是为了盘中任意扫描时点可评 —— 14:45 时 1.15× 强度的票 share
         仅 ~7.6%，固定 share 阈值在 14:30-15:00 窗口内永远不触发
      5. 尾盘净流：尾盘量价方向净流 > PM_FLOW_MIN(0)

    Returns:
        list[dict]: [{code,name,source,reason,change_pct,pm_vol_share_pct,
            pm_rate_ratio,pm_flow,prior8d_ret,...}]，无命中 []。取数失败向上抛
            （fail-fast，调用方记 missing，不把源故障当「无候选」）。
    """
    def _f(key: str, dflt: float) -> float:
        try:
            return float(os.getenv("QD_EOD_START_" + key, "") or dflt)
        except ValueError:
            return dflt

    ret_min, ret_max = _f("RET_MIN", 2.0), _f("RET_MAX", 8.0)
    p8_min, p8_max = _f("PRIOR8_MIN", -15.0), _f("PRIOR8_MAX", 6.0)
    rate_min, med_mult = _f("PM_RATE_MIN", 1.05), _f("PM_MED_MULT", 1.15)
    flow_min = _f("PM_FLOW_MIN", 0.0)

    date = date or _data_day()
    facts = _eod_start_facts(date)
    if not facts:
        return []

    day_1430 = datetime.strptime(date, "%Y-%m-%d").replace(hour=14, minute=30)
    horizon = max((f["last_t"] for f in facts if f.get("last_t")), default=day_1430)
    t_pm = max(1.0, (horizon - day_1430).total_seconds() / 60.0)
    t_am = 210.0
    for f in facts:
        f["pm_rate"] = ((f["v_pm"] / t_pm) / (f["v_am"] / t_am)
                        if f["v_am"] > 0 and f["v_pm"] >= 0 else None)
    rates = sorted(f["pm_rate"] for f in facts
                   if f.get("pm_rate") is not None and f["v_am"] + f["v_pm"] > 0)
    median_rate = rates[len(rates) // 2] if rates else None

    out: List[Dict] = []
    for f in facts:
        prev_close, last = f.get("prev_close"), f.get("last")
        if not prev_close or not last:
            continue
        day_ret = (float(last) / float(prev_close) - 1) * 100
        if not (ret_min <= day_ret <= ret_max):
            continue
        lim_pct = get_limit_pct(f["code"], f.get("name") or "")
        if lim_pct <= 0:
            continue
        limit_px = round(float(prev_close) * (1 + lim_pct / 100), 2)
        if not f.get("day_high") or float(f["day_high"]) >= limit_px:
            continue
        rate = f.get("pm_rate")
        if rate is None or median_rate is None:
            continue
        if rate < rate_min or rate < med_mult * median_rate:
            continue
        if f["pm_flow"] <= flow_min:
            continue
        p8 = _eod_start_prior8(f["code"], date)
        if p8 is None or not (p8_min <= p8 <= p8_max):
            continue
        v_day = f["v_am"] + f["v_pm"]
        out.append({
            "code": f["code"], "name": f.get("name") or "", "source": "尾盘启动",
            "reason": ("尾盘启动 涨%.1f%% 尾盘量强%.2f倍 净流%.0f万 启动前8日%+.1f%%"
                       % (day_ret, rate, f["pm_flow"] / 1e4, p8)),
            "change_pct": round(day_ret, 2), "turnover_pct": None,
            "pm_vol_share_pct": round(f["v_pm"] / v_day * 100, 1) if v_day > 0 else None,
            "pm_rate_ratio": round(rate, 2),
            "pm_flow": round(f["pm_flow"], 1),
            "prior8d_ret": round(p8, 2),
        })
    out.sort(key=lambda x: -(x["pm_rate_ratio"] or 0))
    return out


# ═══════════════════════════════════════════════════════════════
#  实时行情分块获取（2026-10-20 漏斗）
# ═══════════════════════════════════════════════════════════════

def batch_realtime_quotes(codes, workers=4):
    """分块并发获取实时行情——绕过 get_realtime_quote 内部 20 只硬截断（2026-10-20）。

    data_tools.get_realtime_quote 固定只处理前 20 只（code_list[:20]），宽池回填
    会被静默截断。本函数按 20 只/块切分、线程池并发拉取后合并，块级失败只丢该块。

    Args:
        codes: 股票代码列表
        workers: 并发块数（每块固定 20 只）

    Returns:
        (quotes, failed_chunks): quotes={code: 行情dict} 只含成功项（无 error 字段）；
            failed_chunks=[[code, ...], ...] 整块失败（异常/格式错）的代码块。

    Gotcha:
        函数内局部 import get_realtime_quote——测试通过替换
        data_tools.get_realtime_quote 模块属性注入假数据，模块级 import 会绕过补丁。
    """
    from ...tools.finance.data_tools import get_realtime_quote

    codes = [c for c in (codes or []) if c]
    if not codes:
        return {}, []
    chunks = [codes[i:i + 20] for i in range(0, len(codes), 20)]
    quotes = {}
    failed_chunks = []

    def _fetch(chunk):
        try:
            raw = get_realtime_quote(",".join(chunk)) or {}
        except Exception as e:
            logger.warning("[MktScreen] 行情分块获取异常 %s只: %s", len(chunk), e)
            return chunk, None
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, dict):
            if isinstance(raw, dict) and raw.get("stock_code"):
                data = {raw["stock_code"]: raw}
            else:
                return chunk, None
        return chunk, data

    ex = None
    try:
        ex = ThreadPoolExecutor(max_workers=max(1, min(int(workers or 4), len(chunks))))
        results = list(ex.map(_fetch, chunks))
    finally:
        if ex is not None:
            ex.shutdown(wait=False, cancel_futures=True)

    for chunk, data in results:
        if data is None:
            failed_chunks.append(list(chunk))
            continue
        for k, v in data.items():
            if isinstance(v, dict) and not v.get("error"):
                quotes[str(k)] = v
    return quotes, failed_chunks
