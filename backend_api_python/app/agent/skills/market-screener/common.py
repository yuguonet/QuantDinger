# -*- coding: utf-8 -*-
"""
market-screener/common.py

通用基础设施：数据加载、技术指标计算、涨停/跌停/炸板池（市场事实）。

【2026-10-28】龙回头检测已随「选股去连板/龙回头」整段移除；近 N 日涨停池
仅保留给 inspect_stock 的涨停史客观事实，不再进候选池。
三个策略（盘中/尾盘/盘后）共享此模块。
"""

from __future__ import annotations

from ...log import logger
import json
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
    "热点题材": "热点", "条件搜索": "搜索",
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
            mood = market.get("mood", "")
            ms = market.get("mood_score", 0)
            zt = market.get("zt_count", 0)
            dt = market.get("dt_count", 0)
            fund = market.get("fund_flow", 0)
            brk = market.get("broken_rate", 0)
            m_line = f"{strategy} M:{mood}({ms}) ZT:{zt} DT:{dt}"
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
            parts.append(f"资金: 主力净额{flow.get('main_net_wan')}万 "
                         f"占成交{flow.get('net_pct_of_amount')}%")
        elif flow:
            parts.append(f"资金: 缺失（{flow.get('reason')}）")

        th = self.get("theme") or {}
        if any(th.values()):
            boards = ",".join(th.get("boards") or []) or "-"
            tags = ",".join(th.get("concept_tags") or []) or "-"
            parts.append(f"题材: 行业{th.get('industry') or '-'} | 板块{boards} | 概念{tags}")

        zt = self.get("zt_history") or []
        if zt:
            parts.append("涨停史: " + " ".join(
                f"{z.get('date')}({z.get('continuous_days')}板)" for z in zt[:6]))

        if self.get("fund_hist"):
            parts.append(f"近10日主力资金明细已附(fund_hist)")
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
                f"{strategy} M:{market.get('mood','')}({market.get('mood_score','')}) "
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
        from app.utils.db_market import get_market_db_manager
        pool = get_market_db_manager()._get_pool("CNStock")
        inner = pool._pool          # psycopg2 ThreadedConnectionPool
        if inner.minconn >= workers:
            return
        inner.minconn = workers     # 先抬阈值，防补充的连接被 _putconn 关掉

        # 串行持有 workers 条再统一归还：前几条弹空闲、其余锁内新建，全部
        # 同时在手 ⇒ 归还时见 minconn=workers 逐一保留为空闲连接。
        # 不用并发 ping wave：快查询的连接会被后续任务弹走复用，wave 结束
        # 后空闲数不确定（实测 8 任务只补到 6）。
        held = []
        try:
            for _ in range(workers):
                held.append(inner.getconn())
        finally:
            for c in held:
                inner.putconn(c)
        logger.info("[market-screener] CNStock 连接池预热完成: 空闲 %d 条"
                    "（对齐画像并发 %d）", len(inner._pool), workers)
    except Exception as e:
        logger.warning(
            "[market-screener] CNStock 连接池预热失败（按原池行为继续）: %s", e)
def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")
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
        # 惰性登记（2026-10-28 多源选股：人气榜/板块成分必须可调）
        try:
            if name == "get_hot_rank":
                from ...tools.finance.dragon_tools import get_hot_rank as _fn
            elif name == "get_sector_stocks":
                from ...tools.finance.sector_analysis_tools import get_sector_stocks as _fn
            else:
                return {"error": f"未知工具: {name}"}
            _TOOL_REGISTRY[name] = _fn
            fn = _fn
        except Exception as e:
            return {"error": f"未知工具: {name} ({type(e).__name__}: {e})"}
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
#  涨停池按日落盘缓存（2026-10-25 P1-②）
# ═══════════════════════════════════════════════════════════════
#
# 根因（主探针实锤）：fetch_recent_zt_pools(8) 每个进程冷启都对 7 个历史
# 日期串行拉 akshare（stock_zt_pool_em），限流器纯睡 27.68s
# （min=2.0 + jitter U(1.5,3.5) ≈ 3.5-5.5s/次）+ 实取 ~5s。历史日期的
# 涨停池当日不变 ⇒ 按日落盘，同日二次运行零网络、零限流等待。
# 口径：① 只缓存**历史日期**（< 今日）；当日实时池盘中动态，且
# get_zt_pool 当日本就走 _rt_zt_pool 内存缓存（零网络），无需落盘；
# ② TTL 24h——上游迟到的数据修正不会永久驻留，也不会每次运行重付；
# ③ 只缓存非空结果——akshare 失败时 _ak_zt_pool 静默返回 []
# （dragon_limit 的 fail-soft），把 [] 落盘会把瞬时故障冻结成假数据；
# ④ 原子写（tmp + os.replace）防多进程并发写半截文件；读坏按未命中回源。
# 缓存目录：backend_api_python/data/zt_pool/（与 adjustment_factors.json 同区的
# 跨进程缓存；本模块在 app/agent/skills/market-screener/ 下，上溯四层到 backend
# 根—— Adjustment 在 app/data_sources/provider/ 下只需三层，勿照抄层数）
_ZT_POOL_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "data", "zt_pool")
_ZT_POOL_CACHE_TTL_S = 24 * 3600


def _zt_pool_cache_path(date_str: str) -> str:
    return os.path.join(_ZT_POOL_CACHE_DIR, f"zt_pool_{date_str}.json")


def _zt_pool_cache_get(date_str: str) -> Optional[List[Dict]]:
    """读按日涨停池缓存；未命中/过期/损坏返回 None（调用方回源拉取）。"""
    path = _zt_pool_cache_path(date_str)
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict) or not isinstance(payload.get("pool"), list):
            return None
        if time.time() - float(payload.get("fetched_at") or 0) > _ZT_POOL_CACHE_TTL_S:
            return None
        return payload["pool"]
    except Exception as e:
        logger.warning("[MktScreen] 涨停池缓存读取失败 %s: %s", date_str, e)
        return None


def _zt_pool_cache_put(date_str: str, pool: List[Dict]) -> None:
    """原子写按日涨停池缓存（tmp + replace）。失败仅记 warning 不抛。"""
    path = _zt_pool_cache_path(date_str)
    try:
        os.makedirs(_ZT_POOL_CACHE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": date_str, "fetched_at": time.time(), "pool": pool},
                      f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("[MktScreen] 涨停池缓存写入失败 %s: %s", date_str, e)


def fetch_zt_pool(date: str) -> List[Dict]:
    """涨停池。fail-fast：失败向上抛（调用方负责记 missing，不静默吞成空池）。

    2026-10-25 P1-②：历史日期（< 今日）按日落盘缓存（TTL 24h、只存非空），
    同日重复运行免 akshare 往返与限流等待；当日实时池不缓存（盘中动态）。
    """
    from app.market_cn.dragon_limit import get_zt_pool
    _day = (date or "").strip()
    if _day and _day != datetime.now().strftime("%Y-%m-%d"):
        cached = _zt_pool_cache_get(_day)
        if cached is not None:
            return cached
        pool = get_zt_pool(_day)
        if pool:                      # 不落空：akshare 失败是静默 []（见上）
            _zt_pool_cache_put(_day, pool)
        return pool
    return get_zt_pool(_day)
def fetch_dt_pool(date: str) -> List[Dict]:
    """跌停池。fail-fast：同 fetch_zt_pool。"""
    from app.market_cn.dragon_limit import get_dt_pool
    return get_dt_pool(date)
def fetch_broken_board(date: str) -> List[Dict]:
    """炸板池。fail-fast：同 fetch_zt_pool。"""
    from app.market_cn.dragon_limit import get_broken_board
    return get_broken_board(date)
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
#  近 N 日涨停池（inspect_stock 涨停史事实；不进候选池）
# ═══════════════════════════════════════════════════════════════

def fetch_recent_zt_pools(days: int = 8) -> Dict[str, List[Dict]]:
    """近 N 日涨停池（仅供 inspect_stock 的涨停史客观事实）。

    容错口径（2026-10-21）：单日失败跳过并留痕；**全部日期都失败**时必须抛出，
    否则源故障会塌缩成「近 N 日无涨停」假事实。
    """
    pools = {}
    today = datetime.now()
    last_err: Optional[Exception] = None
    for i in range(days):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
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
