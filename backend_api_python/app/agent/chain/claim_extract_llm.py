# -*- coding: utf-8 -*-
"""Claim LLM 抽取 —— 多标的回答的结构化解析通道。

【为什么单独成文件】
`chain/claims.py` 的文件级契约是「纯函数层：不碰 DB、不调 LLM」。本模块要发一次
LLM 调用，因此不能并入；规则抽取器保留为解析失败时的兜底，能力不删。

【闸门（零成本，先于任何 LLM 调用）】
复用 `claims.intake_gate`（空回答 / 过短 / 域禁用 / 无可验证词）与
`claims.guess_subject`（抽不出标的）。任一不过直接返回 `[]`，一次 LLM 都不发出去
——寒暄、ack、报错、纯陈述在这里被挡掉。

【防幻觉三道校验，任一不过即丢弃该条】
  ① `quote` 去空白后须为原文子串（另做一次剥 markdown 符号的宽松比对）
  ② `code` 须在原文字面出现
  ③ `p` 非空时该条 `quote` 必须含把握标记（「把握XX%」或「P(T+N涨)=」），否则清空 p

【时间基准（exec_date）】
`exec_date` 是**该条回答的生成日**，不是「现在」。它只用于两件事：把「明天/后天/
11月26日」换算成交易日差，以及给无周期线索的判断定到期日。
  · 实时链路——回答当场生成，传 `current_data_day()`
  · 历史回放——传该记录的**保存时间**，不能拿今天顶
  · 无时间词——基准即当前（实时＝当前数据日）；回放时即记录保存日
拿不到可靠基准就不判：`intake.record_decision` 会整条拒绝入库，宁可漏掉，不能错了。

【方向的两类来源（basis）】
  `labeled`  —— 原文有周期依据（自带 T+N，或多档表格单元格由列头决定）
  `inferred` —— 原文用自然周期词表达（短期/中期/中长线…），按词表映射到最近一档
  `default`  —— 原文没给任何周期线索，落域默认档；**不采信模型自行猜测的周期**
自然语言里的方向同样要抽：人类读「中期结构没有坏、短期在走弱」即得到中期多头+短期空头，
正则读不出。但下列三类**不是**方向预测，一律不抽：条件句（若…则…）、纯仓位动作
（仓位不动/分批减仓）、中性表述（震荡/无共振/反弹≠反转）。

【能力边界：只抽 direction 与 level】
幅度（magnitude）不抽。历史 564 条 magnitude 抽样 18 条全为噪音——净占比、当日涨幅、
筹码套牢比例、区间分位、炸板率都被规则误抽成「预测幅度」，每条回评必错。新通道不
复制该缺陷；规则抽取器仍保留该能力，需要时由调用方开关恢复。

【返回语义】
`[]`   —— 闸门判定本就不该抽（不是故障，调用方无需回退）
`None` —— 调用或解析失败（调用方回退规则抽取器）
`list` —— 抽取成功，可能为空
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from ..log import logger

EXTRACTOR_VER = "llm.v2"

_MAX_CLAIMS = 40
_MAX_TOKENS = 3000
_TIMEOUT_S = 90.0

_HZ_OK = ("T+1", "T+3", "T+5", "T+10", "T+20")
_DIR_OK = ("bullish", "bearish")
_LEVEL_KINDS = ("目标价", "目标位", "支撑", "支撑位", "压力", "压力位",
                "止损", "止损位", "跌破", "跌穿", "站稳", "突破", "站上")

# 自然周期词 → 最近一档（inferred 方向用；顺序即优先级）
_TERM_HZ = (
    (("短期", "短线", "今日", "今天", "明日", "明天", "次日", "日内", "盘口"), "T+1"),
    (("中期", "中线", "本周", "一周", "3日", "三个交易日", "数日"), "T+5"),
    (("长期", "中长线", "长线", "一个月", "月度", "月内"), "T+10"),
)
_AXIS_BULL = ("走强", "转强", "偏多", "看多", "看涨", "上攻", "修复", "强势",
              "向上", "回升", "没坏", "没有坏", "未走坏", "没走坏", "完好",
              "良性", "健康", "有支撑", "守住")
_AXIS_BEAR = ("走弱", "转弱", "偏空", "看空", "看跌", "下探", "破位", "空头",
              "弱势", "向下", "回踩", "承压", "失守")
# 精确相对日期词 → 自然日偏移（长词在前，否则「大后天」会被「后天」抢匹配）
_REL_DAYS = (("大后天", 3), ("后天", 2), ("明天", 1), ("明日", 1), ("次日", 1),
             ("今天", 0), ("今日", 0), ("当天", 0), ("日内", 0))
_ABS_DATE = re.compile(r"(?:(20\d{2})[-/年])?(\d{1,2})[-/月](\d{1,2})日?")
_COND_RE = re.compile(r"若|如果|一旦|假如|倘若|除非")
_ADVICE_RE = re.compile(r"加仓|减仓|仓位|持有|买入|卖出|止损|分批|观望|不建议|不要买|不宜")
_HZ_IN_QUOTE = re.compile(r"T\s*\+\s*(\d+)", re.I)

_PNOTE_ANY = re.compile(r"把握\s*[:：]?\s*\d{1,3}|P\s*\(\s*T\s*\+?\s*\d+\s*涨\s*\)", re.I)
_PNOTE_HEUR = re.compile(r"把握\s*[:：]?\s*\d{1,3}")
_PNOTE_CALIB = re.compile(r"P\s*\(\s*T\s*\+?\s*(\d+)\s*涨\s*\)\s*[=＝]\s*(0?\.\d+|[01](?:\.0+)?)")
_CODE_ANY = re.compile(r"(?<!\d)(\d{6})(?!\d)")

_PROMPT = """你是结构化抽取器。从下面这段投研回答中抽出可验证的判断。

硬规则：
1. 每只股票（6 位代码）单独成组，不要合并成一只。
   只输出**被正面判断**的票；作为对比、旁证、举例出现的其他代码（原文说「与某票不同」
   「不像某某」「参考里那只」），不要输出它们的判断。
2. direction 的周期按两种方式取：
   a) 原文标了 T+1/T+3/T+5/T+10/T+20 → horizon 照抄该标注。
   b) 原文用自然周期词 → 按此映射填 horizon：
      短期/短线/今日/明日/次日/日内 → T+1
      中期/中线/本周/一周内/数日 → T+5
      长期/中长线/月内 → T+10
   同一只票不同周期可给不同方向（如「中期没坏、短期走弱」= T+5 bullish + T+1 bearish）。
3. dir 只能取 bullish（看多/看涨/偏多/走强/修复/强势）或 bearish（看空/看跌/偏空/走弱/破位）。
4. 下列三类**不是**方向预测，不要输出 direction：
   - 条件句：「若/如果/一旦…就/则…」（条件未成立不算预测）
   - 纯仓位动作：只说买卖/持仓动作而不含趋势描述（如「仓位不动」「分批减仓」）
   - 中性表述：「震荡/横盘/无共振/反弹≠反转/方向中性/多空不明」
   注意：**含趋势或结构描述的句子，即使写在建议语境里也要输出方向**。
   如「中期结构没有坏，短期在走弱」→ 必须输出 T+5 bullish 与 T+1 bearish 两条。
5. level：原文明确给出止损位、支撑、压力、目标价时输出一条，price 为数字。
   kind 只能取：目标价/目标位/支撑/支撑位/压力/压力位/止损/止损位/跌破/跌穿/站稳/突破/站上。
6. 不要输出涨跌幅、资金净占比、筹码比例、区间位置等任何幅度类数字。
7. p 是「把握度」：仅当原文明确写了「(把握XX%)」或「P(T+N涨)=0.xx」时才填
   （把握度给 0~100 的整数），否则一律 null。严禁自己估计或编造。
8. quote 必须从原文逐字连续复制一小段（能定位到该判断），不得改写、不得拼接。
   方向类 quote 要带上周期词或方向词本身，让人能看出依据。
9. 原文没写的判断不要补。宁可少抽，不可编造。

示例一（持仓建议文里夹着方向描述 → 要抽，code 填该票代码）：
原文：「结论：可以留，但不宜长期等待。中期结构没有坏，今天是缩量回踩，短期在走弱。」
输出：{"claims":[{"type":"direction","code":"300236","horizon":"T+5","dir":"bullish","p":null,"quote":"中期结构没有坏"},{"type":"direction","code":"300236","horizon":"T+1","dir":"bearish","p":null,"quote":"短期在走弱"}]}

示例二（条件句 + 仓位动作 → 一条都不抽）：
原文：「若跌破 14.44，则空头延续，明确不要加仓。已持仓者：仓位不动，以 14.44 作为止损参考位。」
输出：{"claims":[]}

只输出 JSON，不要任何解释文字：
{"claims":[{"type":"direction","code":"601975","horizon":"T+1","dir":"bearish","p":null,"quote":"短期在走弱"},
 {"type":"level","code":"601975","kind":"跌破","price":3.41,"quote":"跌破 3.41"}]}"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _json_of(raw: str) -> Optional[Dict[str, Any]]:
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        data = json.loads(s[i:j + 1])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _prob_of(item: Dict[str, Any], quote: str) -> Optional[Dict[str, Any]]:
    raw = item.get("p")
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= val <= 100.0):
        return None
    p = val / 100.0 if val > 1.0 else val
    if not (0.0 <= p <= 1.0):
        return None
    m = _PNOTE_CALIB.search(quote)
    if m and abs(float(m.group(2)) - p) < 0.005:
        return {"p": round(p, 4), "p_source": "calibrated",
                "p_target": "up", "p_horizon": "T+%s" % m.group(1)}
    if _PNOTE_HEUR.search(quote):
        return {"p": round(p, 4), "p_source": "heuristic_confidence"}
    return None


def _bucket(k: int) -> str:
    """交易日差 → 最近档位（k=0 即当日判断，按最小档 T+1 结算）。"""
    if k <= 1:
        return "T+1"
    if k <= 3:
        return "T+3"
    if k <= 7:
        return "T+5"
    if k <= 15:
        return "T+10"
    return "T+20"


def _dated_horizon(text: str, exec_date: Optional[date]) -> Optional[tuple]:
    """按生成日 + 交易日历把「明天/后天/11月20日」换算成档位，`("dated", horizon)`。

    比词表映射准：自然日偏移经日历转成**交易日差**，周末与休市自动跳过
    （周五说的「后天」落在周日，顺延为下周一＝T+2）。无 exec_date 或日历不可用时
    返回 None，由模糊词表或默认档兜底。
    """
    if not exec_date:
        return None
    t = text or ""
    target: Optional[date] = None
    for w, off in _REL_DAYS:
        if w in t:
            target = exec_date + timedelta(days=off)
            break
    if target is None:
        m = _ABS_DATE.search(t)
        if not m:
            return None
        try:
            target = date(int(m.group(1)) if m.group(1) else exec_date.year,
                          int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        if target < exec_date:
            return None
    try:
        from app.utils.trading_calendar import is_trading_day, trading_days_count
        k = trading_days_count(exec_date.isoformat(), target.isoformat()) - 1
        if not is_trading_day(target.isoformat()):
            k += 1                      # 落在休市区间 ⇒ 顺延到下一个交易日
    except Exception:
        return None
    return ("dated", _bucket(max(k, 0))) if k >= 0 else None


def _basis_of(quote: str, hz_llm: str, hz_default: str,
              exec_date: Optional[date] = None) -> Optional[tuple]:
    """判定这条方向的周期依据：`(basis, horizon)`，无任何依据返回 None。

    `labeled`  —— quote 自带 `T+N`，或来自多档表格单元格（周期在列头）
    `dated`    —— quote 含「明天/后天/11月20日」等，按回答生成日+交易日历换算
    `inferred` —— quote 含自然周期词（短期/中期/中长线…），按词表映射
    `default`  —— 原文没给任何周期线索，落域默认档（不采信模型自行猜测的周期）；
                  其到期日以回答生成日为基准，故无 exec_date 时不可用
    """
    m = _HZ_IN_QUOTE.search(quote or "")
    if m:
        h = "T+%s" % m.group(1)
        return ("labeled", h) if h in _HZ_OK else None
    dt = _dated_horizon(quote, exec_date)
    if dt:
        return dt
    for words, h in _TERM_HZ:
        if any(w in quote for w in words):
            return ("inferred", h)
    # 表格多档单元格（「看多 / 看多 / 中性」）或整行带竖线：周期由表头决定，采信模型给的档位
    if ("|" in (quote or "") or "/" in (quote or "")) and hz_llm in _HZ_OK:
        return ("labeled", hz_llm)
    return ("default", hz_default)


def _looks_directional(text: str) -> bool:
    """原文是否同时含周期词与方向词——用于「空结果但有明显方向语义」的定向重试。

    只做存在性判断，不判断极性（极性归 LLM）。周期词/方向词命中即视为「该抽」，
    避免模型偶发返回空数组把整篇方向丢掉。
    """
    t = text or ""
    has_term = bool(_HZ_IN_QUOTE.search(t)) or any(
        w in t for words, _h in _TERM_HZ for w in words)
    has_axis = any(w in t for w in _AXIS_BULL + _AXIS_BEAR)
    return has_term and has_axis


def extract_claims(query: str, answer: str, domain: Optional[str] = None,
                   exec_date: Optional[date] = None,
                   policy: Optional[Dict[str, Dict[str, Any]]] = None,
                   max_claims: int = _MAX_CLAIMS,
                   timeout: float = _TIMEOUT_S) -> Optional[List[Dict[str, Any]]]:
    """LLM 解析一条问答的可验证声明。

    闸门不通过返回 `[]`；调用或解析失败返回 `None`（调用方回退 `claims.extract_claims`）。
    """
    from . import claims as _c

    allow, _reason = _c.intake_gate(query, answer, domain, policy)
    if not allow:
        return []
    if not _c.guess_subject(query, answer)[0]:
        return []

    def _once_items() -> Optional[List[Any]]:
        from ..llm import complete
        resp = complete(
            [{"role": "system", "content": _PROMPT},
             {"role": "user", "content": answer or ""}],
            temperature=0.0, max_tokens=_MAX_TOKENS, timeout=timeout,
        )
        data = _json_of(getattr(resp, "content", "") or "")
        if not isinstance(data, dict):
            return None
        lst = data.get("claims")
        return lst if isinstance(lst, list) else None

    # 同输入同温度仍会偶发返回空数组或非 JSON（服务端不保证确定性），
    # 因此不可解析时重试一次；两次都不可解析才判为失败（回退规则抽取）。
    items: Optional[List[Any]] = None
    for attempt in (1, 2):
        try:
            items = _once_items()
        except Exception as exc:
            logger.warning("[ClaimLLM] 第%d次调用失败: %s: %s",
                           attempt, type(exc).__name__, exc)
            items = None
        if items is not None:
            break
        if attempt == 1:
            logger.warning("[ClaimLLM] 第1次输出不可解析，重试")
    if items is None:
        logger.warning("[ClaimLLM] 两次均不可解析，回退规则抽取")
        return None

    if not items and _looks_directional(answer):
        logger.info("[ClaimLLM] 空结果但原文含周期词+方向词，重试一次")
        try:
            retry = _once_items()
        except Exception as exc:
            logger.debug("[ClaimLLM] 重试失败(沿用空结果): %s", type(exc).__name__)
            retry = None
        if retry:
            items = retry

    na = _norm(answer)
    nb = _norm(re.sub(r"[*_`#>|]", "", answer or ""))
    codes = set(_CODE_ANY.findall(answer or ""))
    q_code = _CODE_ANY.search(query or "")
    only = {q_code.group(1)} if q_code else None
    # 主体票的代码常常只出现在问句里（回答正文写「现价 74.90」而不重现代码），
    # 因此合法标的是 answer ∪ query，否则单票分析会被整体拦死。
    known = set(codes) | (only or set())

    dom = domain or _c.classify_domain(query, answer)
    pol = (policy or _c.DOMAIN_POLICY).get(dom) or {}
    hz_default = str(pol.get("horizon_default") or "T+3")
    concl = _c._conclusion_text(answer)[:300]
    out: List[Dict[str, Any]] = []
    dropped = 0

    for item in items:
        if len(out) >= max_claims:
            break
        if not isinstance(item, dict):
            dropped += 1
            continue
        code = str(item.get("code") or "").strip()
        quote = str(item.get("quote") or "")
        kind = str(item.get("type") or "").strip().lower()
        # 问句指定唯一标的 ⇒ 一律归到该标的：回答里的对比票不是判断对象；正文通常
        # 只写简称/价格不重现代码，模型也常把主体代码错挂到对比票上或直接留空。
        if only and code != q_code.group(1):
            logger.debug("[ClaimLLM] 标的归一 %s→%s (kind=%s)",
                         code or "(空)", q_code.group(1), kind)
            code = q_code.group(1)
        if not re.fullmatch(r"\d{6}", code) or code not in known:
            dropped += 1
            continue
        nq = _norm(quote)
        if not nq or (nq not in na and nq not in nb):
            dropped += 1
            continue

        if kind == "direction":
            d = str(item.get("dir") or "").strip().lower()
            if d not in _DIR_OK:
                dropped += 1
                continue
            if _COND_RE.search(quote) or _ADVICE_RE.search(quote):
                dropped += 1
                continue
            axis = _AXIS_BULL if d == "bullish" else _AXIS_BEAR
            if not any(w in quote for w in axis):
                dropped += 1
                continue
            hz_llm = str(item.get("horizon") or "").strip().upper().replace(" ", "")
            basis = _basis_of(quote, hz_llm, hz_default, exec_date)
            if basis is None:
                dropped += 1
                continue
            b, hz = basis
            pred: Dict[str, Any] = {"dir": d, "basis": b}
            prob = _prob_of(item, quote)
            if prob:
                pred.update(prob)
            out.append({
                "claim_type": "direction", "subject": code, "subject_kind": "stock",
                "horizon": hz, "predicted": pred, "confidence": None,
                "evidence": concl, "source_quote": quote[:400],
                "due_date": _c.due_date_of(hz, exec_date), "seq": len(out),
            })
        elif kind == "level":
            lkind = str(item.get("kind") or "").strip()
            try:
                price = float(item.get("price"))
            except (TypeError, ValueError):
                dropped += 1
                continue
            if lkind not in _LEVEL_KINDS or price <= 0:
                dropped += 1
                continue
            hz = str(item.get("horizon") or "").strip().upper().replace(" ", "")
            hz = hz if hz in _HZ_OK else hz_default
            out.append({
                "claim_type": "level", "subject": code, "subject_kind": "stock",
                "horizon": hz, "predicted": {"price": price, "kind": lkind},
                "confidence": None, "evidence": concl, "source_quote": quote[:400],
                "due_date": _c.due_date_of(hz, exec_date), "seq": len(out),
            })
        else:
            dropped += 1

    logger.info("[ClaimLLM] domain=%s 抽出=%d 丢弃=%d", dom, len(out), dropped)
    return out
