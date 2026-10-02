# -*- coding: utf-8 -*-
"""Claims — 可验证声明(Claim)提取 + 入库闸门（追责系统 v1.1，2026-10-01）。

设计文档：`docs/追责系统重设计方案_20261001.md`（§10 修正纪要 v1.1）。

【本模块的定位】
  纯函数层：**不碰 DB、不调 LLM**。落库由 `chain/astore` 侧的函数完成，判定由
  `chain/resolver` 完成。这样 smoke 可以零依赖直接测提取质量。

【用户三条修正的落点】
  ① **入库闸门 `intake_gate`（不追责的不入库）**——闸门在**写库之前**，不是"落进去
     再标 claim_count=0"。漏追责可接受（慢调），脏数据不行。判据全是零成本规则，
     判不出来就拦（宁漏不脏）。
  ② **慢调、容忍少量误判**——提取器是规则式（零 LLM 成本），宁可漏抽不可错抽
     （旧教训：错标方向会训练出反向权重）；`confidence` 抽不到一律 **None**，
     绝不填 0.5（那是旧表校准曲线退化成常数的根因）。
  ③ **多域通用留位**——域由 `DOMAIN_POLICY` 配置驱动，**禁止**在调用方写
     `if domain == 'finance'`；v1 只启用 finance，其余域翻开关即可（不加字段）。

【不变量】
  · `extract_claims` 返回的每条 claim 必须有可解析的 subject —— 抽不出标的的
    direction claim **不产出**（没有 subject 就取不到真实数据 ⇒ 必然 unresolvable）。
  · direction 判定**复用** `trace_collector` 的提取器（含否定检测 / 条件剥离），
    保证与旧 `qd_agent_traces.direction` 同口径，不出现两套相反结果。
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

GATE_VER = "v1.1"
EXTRACTOR_VER = "v1.1"

# ── 域判定：只分领域，不分类别（P1）─────────────────────────────────
# 粗粒度关键词；命中多域时按命中数计分，finance 与 data 冲突时 finance 优先
# （"查一下茅台现在多少钱"既是 data 又是 finance —— 有没有预测由 gate 判，不由域名判）
_DOMAIN_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "finance": ("股票", "个股", "A股", "大盘", "指数", "板块", "题材", "涨停", "跌停",
                "K线", "均线", "资金流", "估值", "市盈率", "市净率", "龙虎榜",
                "仓位", "持仓", "选股", "回测", "财报", "业绩", "筹码", "换手",
                "支撑", "压力", "目标价", "止损", "建仓", "清仓"),
    "code": ("写代码", "写个", "函数", "脚本", "跑马灯", "报错", "bug", "实现一下",
             "python", "javascript", "vue", "组件", "接口", "重构"),
    "data": ("查一下", "查询", "是多少", "多少钱", "获取数据", "最新价", "现在多少",
             "帮我看看", "显示"),
}

# ── 域策略（v1.1）：与 qd_domain_resolvers 表同源的**内置默认值** ──────
# 库里有配置以库为准（配置驱动）；库不可达时用这份兜底，保证 fail-open 行为一致。
# v1 只启用 finance；code/general/data 以 enabled=False 预留（v2 翻开关）。
DOMAIN_POLICY: Dict[str, Dict[str, Any]] = {
    "finance": {"enabled": True, "horizon_default": "T+3", "judge_enabled": True},
    "code":    {"enabled": False, "horizon_default": "n/a", "judge_enabled": False},
    "general": {"enabled": False, "horizon_default": "n/a", "judge_enabled": True},
    "data":    {"enabled": False, "horizon_default": "n/a", "judge_enabled": False},
}

# ── 可验证声明的候选特征（闸门用：命中任一即"可能有得追"）────────────
_VERIFIABLE_RE = re.compile(
    r"(看多|看涨|看空|看跌|偏多|偏空|上涨|下跌|走强|走弱|震荡|盘整|横盘|"
    r"反弹|回调|回落|突破|跌破|目标价|目标位|支撑|压力|"
    r"[+-]?\d+(?:\.\d+)?\s*%|涨\d|跌\d|"
    r"强于|弱于|优于|跑赢|跑输)", re.I)

# 幅度：±X% / 涨X% / 跌X% / 涨幅 X%
_MAGNITUDE_RE = re.compile(
    r"(?:涨|跌|上涨|下跌|涨幅|跌幅|振幅)?\s*[约至]?\s*([+-]?\d+(?:\.\d+)?)\s*%")
_MAG_SIGN_RE = re.compile(r"(跌|下跌|跌幅|回落|回调|下探|走弱|看空|看跌|偏空)")

# 点位：目标价/目标位/支撑/压力/不跌破/站稳 X(.XX)
_LEVEL_RE = re.compile(
    r"(目标价|目标位|支撑|支撑位|压力|压力位|不跌破|站稳|突破|站上)\s*"
    r"(?:在)?\s*(\d+(?:\.\d+)?)")

# 区间：X~Y / X-Y / X 到 Y
_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:~|-|—|到|至)\s*(\d+(?:\.\d+)?)")

# 视界
_HORIZON_RULES: Tuple[Tuple[str, str], ...] = (
    (r"(明日|明天|次日|下一个交易日|T\+?1|短线|短期)", "T+1"),
    (r"(T\+?3|三天|3天|本周|本周内|3个交易日)", "T+3"),
    (r"(T\+?5|一周|5天|下周|5个交易日|周内)", "T+5"),
    (r"(两周|半个月|10个?交易日|2周)", "2W"),
    (r"(一个月|1个月|月内|中期|1M)", "1M"),
)

# 置信度：抽不到 → None（★ 不填 0.5）
_CONF_HIGH = ("高度确信", "非常确定", "很有把握", "高置信", "confidence: high", "大概率")
_CONF_MID = ("较有把握", "中等置信", "confidence: medium", "偏乐观")
_CONF_LOW = ("不太确定", "不确定", "可能", "或许", "不好说", "难说", "存疑",
             "low confidence", "无法判断")
_CONF_PCT_RE = re.compile(r"(?:概率|把握|胜率)\s*[约]?\s*(\d{1,3})\s*%")

# 指数词 → 代码（kind=index）。★ 只用最广为认知的几个，不上不确定的映射
_INDEX_ALIAS: Dict[str, str] = {
    "上证": "000001.SH", "沪指": "000001.SH", "大盘": "000001.SH",
    "上证指数": "000001.SH", "沪深300": "000300.SH", "创业板": "399006.SZ",
    "深证": "399001.SZ", "深成指": "399001.SZ", "科创50": "000688.SH",
    "北证50": "899050.BJ",
}
_CODE_RE = re.compile(r"(?<![0-9])(\d{6})(?![0-9])")
_SECTOR_RE = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{2,8})\s*(?:板块|概念|题材)")

# 视界 → 到期日（自然日估算，与 evaluator._maturity_days 同口径：
# 1 交易日 ≈ 1.5 自然日 + 2 天周末/节假日缓冲）
_HORIZON_DAYS: Dict[str, int] = {"T+1": 1, "T+3": 3, "T+5": 5, "2W": 10, "1M": 20, "n/a": 0}


def _env_flag(name: str, default: bool) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v not in ("0", "false", "no", "off")


# ═══════════════════════════════════════════════════════════════
#  一、域判定
# ═══════════════════════════════════════════════════════════════
def classify_domain(query: str, answer: str = "") -> str:
    """粗粒度域判定：finance / code / data / general。

    ★ 只分领域，不分类别（P1）——大盘与个股的差异在 `subject`，不在这里。
    """
    text = f"{query or ''}\n{answer or ''}"
    if not text.strip():
        return "general"
    scores: Dict[str, int] = {}
    for dom, kws in _DOMAIN_KEYWORDS.items():
        scores[dom] = sum(1 for k in kws if k in text)
    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1] == 0:
        return "general"
    # finance 与 data 同分时 finance 优先（有没有预测由 gate 判，不由域名判）
    if best[0] == "data" and scores.get("finance", 0) >= best[1]:
        return "finance"
    return best[0]


def domain_enabled(domain: str, policy: Optional[Dict[str, Dict[str, Any]]] = None) -> bool:
    """该域是否允许入库（配置驱动；库里关掉的域不被内置默认值翻回来）。"""
    pol = policy or DOMAIN_POLICY
    cfg = pol.get(domain) or {}
    return bool(cfg.get("enabled", False))


# ═══════════════════════════════════════════════════════════════
#  二、入库闸门（修正①：不追责的不入库）
# ═══════════════════════════════════════════════════════════════
def intake_gate(query: str, answer: str, domain: Optional[str] = None,
                policy: Optional[Dict[str, Dict[str, Any]]] = None) -> Tuple[bool, str]:
    """是否需要追责 ⇒ 是否允许落 `qd_agent_decisions`。

    判据全是零成本规则（不调 LLM）：判不出来就**拦**（宁漏不脏）。

    Returns:
        (allow, reason_code)
        reason_code: ok / empty_answer / answer_too_short / domain_disabled
                     / no_verifiable_claim
    """
    if not (answer or "").strip():
        return False, "empty_answer"
    # 24 而不是 40：一行中文结论（"综合判断：偏多震荡，上证有望突破 3200"）就有 20+
    # 字，机械按 40 卡会把短结论整体拦掉。慢调容忍误判 ⇒ 闸门只挡"明显没内容"的
    # （寒暄/ack/报错），真正的取舍交给下面"有没有可验证声明"这道，而不是长度。
    if len(answer.strip()) < 24:
        return False, "answer_too_short"
    dom = domain or classify_domain(query, answer)
    if not domain_enabled(dom, policy):
        return False, "domain_disabled"
    if not _VERIFIABLE_RE.search(answer):
        return False, "no_verifiable_claim"
    return True, "ok"


# ═══════════════════════════════════════════════════════════════
#  三、Claim 提取（规则式、零 LLM）
# ═══════════════════════════════════════════════════════════════
def guess_subject(query: str, answer: str = "") -> Tuple[Optional[str], Optional[str]]:
    """猜标的：(subject, subject_kind)。

    顺序：用户问题里的 6 位代码 → 问题里的指数别名 / 板块 → 回答里的代码 → 指数别名。
    抽不到返回 (None, None) —— 调用方据此**不产出**需要标的的 claim。
    """
    q = query or ""
    a = answer or ""
    for text in (q, a):
        m = _CODE_RE.search(text)
        if m:
            return m.group(1), "stock"
    for text in (q, a):
        for alias, code in _INDEX_ALIAS.items():
            if alias in text:
                return code, "index"
    for text in (q, a):
        m = _SECTOR_RE.search(text)
        if m:
            return f"sector:{m.group(1)}", "sector"
    return None, None


def guess_horizon(text: str, default: str = "T+3") -> str:
    """从文本抽视界，抽不到用域默认（默认 T+3）。"""
    for pattern, horizon in _HORIZON_RULES:
        if re.search(pattern, text or ""):
            return horizon
    return default


def guess_confidence(text: str) -> Optional[float]:
    """抽置信度；**抽不到返回 None**（★ 绝不填 0.5 —— 旧表校准曲线就死在这）。"""
    t = (text or "").lower()
    m = _CONF_PCT_RE.search(t)
    if m:
        try:
            return max(0.0, min(1.0, int(m.group(1)) / 100.0))
        except (TypeError, ValueError):
            pass
    if any(k in t for k in _CONF_LOW):
        return None                      # 模糊 ⇒ 不填，判定时走 undecidable
    if any(k in t for k in _CONF_HIGH):
        return 0.8
    if any(k in t for k in _CONF_MID):
        return 0.6
    return None


def due_date_of(horizon: str, exec_date: Optional[date] = None) -> Optional[date]:
    """视界 → 到期日（自然日估算，与 evaluator 同口径）。n/a 返回 None。"""
    days = _HORIZON_DAYS.get(horizon)
    if not days:
        return None
    base = exec_date or date.today()
    return base + timedelta(days=int(days * 1.5) + 2)


def _conclusion_text(answer: str, span: int = 600) -> str:
    """结论段：优先「结论/综合判断」之后，否则取开头（与 trace_collector 同口径）。"""
    if not answer:
        return ""
    m = re.search(r"(?:结论|一句话判断|核心判断|综合判断|判断[:：])", answer)
    if m:
        return answer[m.start(): m.start() + span]
    return answer[:span]


def _quote_of(answer: str, pattern: str, span: int = 120) -> str:
    """命中的那句话原文（复盘看推理链用）。"""
    m = re.search(pattern, answer or "")
    if not m:
        return ""
    s = max(0, m.start() - 40)
    return (answer or "")[s: m.end() + span].strip()[:200]


def _extract_direction(answer: str) -> Optional[str]:
    """复用 trace_collector 的方向提取器（含否定检测/条件剥离），避免两套口径。"""
    try:
        from trace_collector import TraceCollector as _TC
        d = _TC._extract_direction_from_text(answer)
    except Exception:
        d = ""
    return d or None


def extract_claims(query: str, answer: str, domain: Optional[str] = None,
                   exec_date: Optional[date] = None,
                   policy: Optional[Dict[str, Dict[str, Any]]] = None,
                   max_claims: int = 4) -> List[Dict[str, Any]]:
    """从一条问答里抽可验证声明（规则式）。

    宁可漏抽不可错抽（修正②：慢调容忍误判，但**不容忍错的标签**——错标方向会
    训练出反向权重）。

    v1 只抽 finance 域的三类：direction / magnitude / level。
    range 暂不抽：`X~Y` 在中文回答里大量是数值区间罗列而非预测区间，误判率高。

    Returns:
        [{"claim_type","subject","subject_kind","horizon","predicted",
          "confidence","evidence","source_quote","due_date","seq"}, ...]
    """
    dom = domain or classify_domain(query, answer)
    if not domain_enabled(dom, policy):
        return []
    pol = (policy or DOMAIN_POLICY).get(dom) or {}
    horizon_default = str(pol.get("horizon_default") or "T+3")

    allow, _reason = intake_gate(query, answer, dom, policy)
    if not allow:
        return []

    subject, subject_kind = guess_subject(query, answer)
    if not subject:
        # 抽不出标的 ⇒ 取不到真实数据 ⇒ 必然 unresolvable，干脆不产 claim
        return []

    concl = _conclusion_text(answer)
    horizon = guess_horizon(f"{query}\n{concl}", horizon_default)
    conf = guess_confidence(concl)
    due = due_date_of(horizon, exec_date)
    out: List[Dict[str, Any]] = []

    def _add(claim_type: str, predicted: Dict[str, Any], quote: str) -> None:
        if len(out) >= max_claims:
            return
        out.append({
            "claim_type": claim_type,
            "subject": subject,
            "subject_kind": subject_kind,
            "horizon": horizon,
            "predicted": predicted,
            "confidence": conf,
            "evidence": concl[:300],
            "source_quote": quote,
            "due_date": due,
            "seq": len(out),
        })

    # ① 方向（主 claim，与旧表 direction 同口径）
    d = _extract_direction(answer)
    if d and d != "neutral":
        _add("direction", {"dir": d}, _conclusion_text(answer, span=200)[:200])

    # ② 幅度：只在结论段里抽，且必须有 % 与涨跌语义
    m = _MAGNITUDE_RE.search(concl)
    if m:
        try:
            pct = float(m.group(1))
        except (TypeError, ValueError):
            pct = None
        if pct is not None:
            if _MAG_SIGN_RE.search(concl[:m.end() + 6]):
                pct = -abs(pct)
            _add("magnitude", {"pct": round(pct, 2)},
                 _quote_of(concl, re.escape(m.group(0))))

    # ③ 点位：目标价/支撑/压力/跌破
    m2 = _LEVEL_RE.search(answer)
    if m2:
        try:
            lvl = float(m2.group(2))
        except (TypeError, ValueError):
            lvl = None
        if lvl is not None:
            kind = m2.group(1)
            _add("level", {"price": lvl, "kind": kind},
                 _quote_of(answer, re.escape(m2.group(0))))

    return out
