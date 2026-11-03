# -*- coding: utf-8 -*-
"""stock-analyzer/run.py — 单只 A 股 T+1~T+20 全周期诊断（执行型技能）。

定位：只做**个股**诊断，不做选股（选股归 market-screener）。两者触发词互斥，不抢单。

自包含原则：本技能不 import 任何其他 skill，也不被其他 skill import。取证、技术面
指标、形态识别、A 股涨跌停规则全部在 `_inner.py` 内独立实现；数据只经工具层
（`app.agent.tools.finance.*`）与数据源接口层取得。与 market-screener 算法同形是
刻意保留 —— 两者口径各自演化，共享会让改一边悄悄改另一边。

为什么必须是执行型（有 run.py）：被它取代的三个分析技能是纯 SKILL.md 提示词型，
输出无 schema、无置信度字段，肯定度只能靠措辞拿捏，又被「不给出高置信度方向 /
不输出确定性涨跌承诺」这类条款压成「证据不足」——用户拿不到可执行结论，只能二次分析。
本技能把置信度做成**可计算字段**，让结论可校验、可回归、可按证据推翻。

两个置信度字段的区别（勿合并）：
    evidence    证据完整度 = 拿到维度数 / 计入维度数（客观计数，与把握无关）
    confidence  该周期方向把握度 = 档位上限 × evidence × 信号强度。
                只有 T+1 的 `p_up` 是标定过的数字；T+3 及以后**只给方向，不给概率**

纪律：禁止「可能 / 建议关注 / 谨慎乐观」这类无信息量措辞。每周期必须落到四要素：
方向 / 触发条件 / 证伪条件 / 关键价位。数据不足写「不适用」，不编造概率。

易错点：
  - `HOLDS` 里 T+5 与 1W 同为 5 天、T+20 与 1M 同为 21 天；本文件按**档位名**取天数，
    不按天数反查，避免撞上「先到先得」的规范名规则
  - 条件值为 None（该维度缺数）时**视为不成立**，不猜测、不跳过 —— 缺数自然走向
    中性或低置信度，而不是伪造方向
  - `predict_next_day` 要求 bars 的 `time` 是 int 时间戳；`_inner.load_bars` 已保证，
    不要在这里再传日期串
"""
from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from ...log import logger
from . import _inner

#: 全周期档位（顺序即输出顺序）；天数不在此硬编码，取 `label_spec.HOLDS`
_HORIZONS: Tuple[str, ...] = ("T+1", "T+3", "T+5", "T+10", "T+20")

#: 各档置信度上限（T+1 有标定故最高；越远越不确定）。可被 env 覆盖
_CONF_CAP_DEFAULT: Dict[str, float] = {
    "T+1": 0.90, "T+3": 0.72, "T+5": 0.68, "T+10": 0.58, "T+20": 0.50,
}

_BARS_DAYS_DEFAULT = 120

#: 并发取数总等待上限：外部源偶发挂起，不能拖死整轮
_DEADLINE_S = 15.0

#: 参与证据完整度统计的维度，及它在 missing 项里用的名字（前缀）
_DIMS: Tuple[str, ...] = ("quote", "tech", "prediction", "patterns", "flow", "theme", "zt_history")
_DIM_MISSING_KEY: Dict[str, str] = {"zt_history": "zt"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _conf_cap() -> Dict[str, float]:
    raw = os.environ.get("QD_STOCK_ANALYZER_CONF_CAP") or ""
    cap = dict(_CONF_CAP_DEFAULT)
    if raw:
        try:
            cap.update({k: float(v) for k, v in json.loads(raw).items() if k in cap})
        except (ValueError, TypeError) as e:
            logger.warning("[stock-analyzer] CONF_CAP 解析失败，回落默认: %s", e)
    return cap


def _bars_days() -> int:
    return _env_int("QD_STOCK_ANALYZER_BARS_DAYS", _BARS_DAYS_DEFAULT)


def _days(h: str) -> int:
    """档位 → 交易日天数（真源 `label_spec.HOLDS`，封顶 T+20≈1M）。"""
    from ...chain.label_spec import HOLDS

    return int(HOLDS.get(h) or 0)


_MKT_CACHE: Dict[str, Any] = {"data": None}
_MKT_LOCK = threading.Lock()


def _market_bars() -> List[Dict[str, Any]]:
    """大盘日线（int 时间戳形状，predict_next_day 直接吃）。取不到返回 []，预测退化不崩。"""
    if _MKT_CACHE["data"] is None:
        with _MKT_LOCK:
            if _MKT_CACHE["data"] is None:
                try:
                    from app.watchlist.predict import load_market_klines
                    _MKT_CACHE["data"] = load_market_klines(120) or []
                except Exception as e:
                    logger.warning("[stock-analyzer] 大盘日线取数失败: %s", e)
                    _MKT_CACHE["data"] = []
    return _MKT_CACHE["data"]


def _regime(overview: Dict[str, Any]) -> str:
    """情绪分桶 strong / neutral / weak / unknown（只作上下文标签，不参与判定）。"""
    try:
        e = float((overview or {}).get("emotion"))
    except (TypeError, ValueError):
        return "unknown"
    if e < 40:
        return "weak"
    if e >= 70:
        return "strong"
    return "neutral"


def _predict(bars: List[Dict], regime: str) -> Dict[str, Any]:
    """T+1 的 P(涨)。这是全部周期里唯一经标定的数字，其余档不许外推。"""
    if not bars:
        return {"ok": False, "reason": "日线为空"}
    try:
        from app.watchlist.predict import predict_next_day
    except Exception as e:
        return {"ok": False, "reason": f"预测模块不可用: {e}"}
    try:
        pred = predict_next_day(bars, _market_bars())
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    if not pred or pred.get("score") is None:
        return {"ok": False, "reason": "模型未给出分数"}
    p_up = float(pred.get("p_up") or 0.5)
    return {
        "ok": True,
        "p_up": round(p_up, 4),
        "exp_ret_bp": pred.get("exp_ret_bp"),
        "model": {
            "beta": (pred.get("beta") or {}).get("beta"),
            "regime": (pred.get("beta") or {}).get("regime"),
            "zhuang": (pred.get("zhuang") or {}).get("score"),
            "market_p_up": (pred.get("market") or {}).get("p_up"),
        },
        "mood_regime": regime,
        "suggest_dir": "bullish" if p_up >= 0.55 else ("bearish" if p_up <= 0.45 else "neutral"),
    }


def _quote_of(raw: Any, code: str) -> Dict[str, Any]:
    """实时行情归一。真实键是 last/changePercent/volume，其余为源字段漂移的兜底读法。"""
    data = (raw.get("data") if isinstance(raw, dict) else None) or {}
    if isinstance(data, dict):
        one = data.get(code) or (list(data.values())[0] if data else {})
    elif isinstance(data, list) and data:
        one = data[0] if isinstance(data[0], dict) else {}
    else:
        one = {}
    return one if isinstance(one, dict) else {}


def _flow_of(raw: Any, code: str) -> Dict[str, Any]:
    """个股资金流（万元）。`total_main_net` 单位是元，必须除 1e4。"""
    if not isinstance(raw, dict) or raw.get("error"):
        return {"ok": False, "reason": (raw or {}).get("error", "无返回") if isinstance(raw, dict) else "无返回"}
    one = (raw.get("data") or {}).get(code) or {}
    if not isinstance(one, dict) or not one:
        return {"ok": False, "reason": "该票无资金流数据"}
    net = float(one.get("total_main_net") or 0) / 1e4
    return {"ok": True, "main_net_wan": round(net, 1), "net_pct_of_amount": one.get("main_pct")}


def _theme_of(raw: Any, code: str) -> Dict[str, Any]:
    data = (raw.get("data") if isinstance(raw, dict) else None) or {}
    one = (data.get(code) if isinstance(data, dict) else None) or {}
    return {
        "industry": one.get("industry"),
        "boards": [b.get("name") for b in (one.get("boards") or [])][:6],
        "concept_tags": (one.get("concept_tags") or [])[:8],
    }


def _risks(tech: Dict[str, Any], flow: Dict[str, Any]) -> List[str]:
    """客观风险标记：只罗列事实，不构成买卖建议。"""
    out: List[str] = []
    if tech.get("is_20d_high"):
        out.append("处于20日最高位（追高风险）")
    if (tech.get("rsi14") or 0) >= 75:
        out.append(f"RSI{tech['rsi14']} 超买")
    if (tech.get("dist_ma20_pct") or 0) >= 15:
        out.append(f"离MA20已有 {tech['dist_ma20_pct']}%（乖离偏大）")
    if (tech.get("zt_streak") or 0) >= 2:
        out.append(f"{tech['zt_streak']}连板（次日分歧概率高）")
    if (tech.get("vol_ratio") or 0) >= 4:
        out.append(f"量比{tech['vol_ratio']}异常放大（注意出货）")
    if isinstance(flow, dict) and flow.get("ok") and (flow.get("main_net_wan") or 0) < 0:
        out.append(f"主力净流出{abs(flow['main_net_wan'])}万")
    if tech.get("limit_up"):
        out.append("当日涨停封板（次日竞价溢价已 price in，需防冲高回落）")
    return out


def _collect(code: str, deep: bool = False) -> Dict[str, Any]:
    """把一只票的证据一次拉齐。任一源失败/超时都进 `missing` 留痕，不吞成「没有」。"""
    missing: List[str] = []
    out: Dict[str, Any] = {"code": code, "missing": missing}

    def _task(name: str, fn):
        try:
            return name, fn()
        except Exception as e:
            missing.append(f"{name}: {type(e).__name__}: {e}")
            return name, None

    def _quote():
        from ...tools.finance.data_tools import get_realtime_quote
        return get_realtime_quote(code)

    def _flow():
        from ...tools.finance.fund_flow_tools import get_fund_flow
        return get_fund_flow(scope="stock", codes=code)

    def _theme():
        from ...tools.finance.sector_analysis_tools import get_stock_sector_info
        return get_stock_sector_info(code)

    def _market():
        from ...tools.finance.data_tools import get_market_overview
        return get_market_overview()

    def _dragon():
        from ...tools.finance.dragon_tools import get_dragon_tiger
        return get_dragon_tiger(codes=code, detail=True)

    tasks = [_quote, _flow, _theme, _market]
    if deep:
        tasks.append(_dragon)

    results: Dict[str, Any] = {}
    futs: Dict[Any, str] = {}
    ex = ThreadPoolExecutor(max_workers=max(1, len(tasks)))
    try:
        futs = {ex.submit(_task, fn.__name__.lstrip("_"), fn): fn.__name__.lstrip("_")
                for fn in tasks}
        for fut in as_completed(futs, timeout=_DEADLINE_S):
            name, val = fut.result()
            results[name] = val
    except Exception as e:
        missing.append(f"并发取数: {type(e).__name__}: {e}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    for fut, name in futs.items():
        if name not in results:
            missing.append(f"{name}: 超时>{_DEADLINE_S}s 未返回")

    qdata = _quote_of(results.get("quote"), code)
    out["name"] = qdata.get("name") or ""
    out["quote"] = {
        "price": round(float(qdata["last"]), 2) if qdata.get("last") else None,
        "change_pct": qdata.get("changePercent"),
        "turnover_pct": qdata.get("turnover_rate") or qdata.get("turnover_pct"),
        "amount": qdata.get("amount"),
        "volume": qdata.get("volume"),
        "volume_ratio": qdata.get("volume_ratio"),
    }
    if not out["quote"]["price"]:
        missing.append("quote: 实时行情缺失")

    out["flow"] = _flow_of(results.get("flow"), code)
    out["theme"] = _theme_of(results.get("theme"), code)
    if deep and results.get("dragon"):
        out["dragon"] = results.get("dragon")

    bars = _inner.load_bars(code, _bars_days())
    if not bars:
        missing.append("bars: 日线为空")

    ts = _inner.tech(bars, code, out["name"])
    if ts.get("ok"):
        out["tech"] = {k: v for k, v in ts.items() if k != "ok"}
    else:
        out["tech"] = {}
        missing.append(f"tech: {ts.get('reason')}")

    out["patterns"] = _inner.detect(bars) if bars else []
    out["zt_history"] = _inner.zt_days(bars, code, out["name"], 8) if bars else []

    regime = _regime(results.get("market") or {})
    out["regime"] = regime
    pred = _predict(bars, regime)
    out["prediction"] = pred
    if not pred.get("ok"):
        missing.append(f"prediction: {pred.get('reason')}")

    out["risks"] = _risks(out["tech"], out["flow"])
    out["as_of"] = bars[-1]["date"] if bars else None
    out["_bars"] = bars
    return out


def _levels(bars: List[Dict], tech: Dict[str, Any]) -> Dict[str, Any]:
    """关键价位：贴住的均线支撑、MA20、近 20 日高低点（四要素里的「价位」）。"""
    win = min(20, len(bars or []))
    if not win:
        return {}
    highs = [b.get("high") for b in bars[-win:] if b.get("high") is not None]
    lows = [b.get("low") for b in bars[-win:] if b.get("low") is not None]
    sup = (tech or {}).get("support") or {}
    name = sup.get("nearest")
    return {
        "support_name": name,
        "support_price": sup.get(name) if name else None,
        "ma5": sup.get("ma5"),
        "ma10": sup.get("ma10"),
        "ma20": sup.get("ma20") or (tech or {}).get("ma20"),
        "high20": round(max(highs), 2) if highs else None,
        "low20": round(min(lows), 2) if lows else None,
    }


def _anchor(h: str, levels: Dict[str, Any]) -> Tuple[Any, Any]:
    """按周期挑对应级别的价位：越长的周期看越粗的均线（T+20 不该用 MA5 当锚）。"""
    sup_name, sup_price = levels.get("support_name"), levels.get("support_price")
    if h in ("T+1", "T+3"):
        return sup_name, sup_price
    order = ("ma10", "ma20", "ma5") if h == "T+5" else ("ma20", "ma10", "ma5")
    for k in order:
        if levels.get(k) is not None:
            return k, levels[k]
    return sup_name, sup_price


def _anchor_txt(anchor: Tuple[Any, Any]) -> str:
    name, price = anchor
    return f"{name}({price})" if price is not None else "支撑"


def _cmp(a, b, op: str) -> Optional[bool]:
    """三态比较：任一操作数为 None ⇒ 返回 None（该条件不可判）。"""
    if a is None or b is None:
        return None
    if op == "ge":
        return a >= b
    if op == "le":
        return a <= b
    if op == "lt":
        return a < b
    if op == "gt":
        return a > b
    return None


def _neg(v: Optional[bool]) -> Optional[bool]:
    return None if v is None else (not v)


def _conds_for(h: str, tech: Dict[str, Any], net: Optional[float]) -> Tuple[List[tuple], List[tuple]]:
    """该档位的（看多条件, 看空条件）；每条 (名称, True/False/None)。"""
    sup = (tech or {}).get("support") or {}
    near = sup.get("near_support")
    stack = tech.get("ma_bull_stack")
    pos20 = tech.get("pos_in_20d_pct")
    dist20 = tech.get("dist_ma20_pct")
    rsi = tech.get("rsi14")
    vr = tech.get("vol_ratio")

    if h == "T+3":
        bull = [("贴支撑", near), ("均线多头", stack),
                ("未超买", None if rsi is None else rsi < 75),
                ("量能未异常放大", None if vr is None else vr < 4)]
        bear = [("已离开支撑", _neg(near)), ("MA20下方超3%", _cmp(dist20, -3, "lt")),
                ("主力净流出", None if net is None else net < 0)]
    elif h == "T+5":
        bull = [("均线多头", stack), ("20日位置过半", _cmp(pos20, 50, "ge")),
                ("乖离未过大", _cmp(dist20, 10, "le"))]
        bear = [("均线非多头", _neg(stack)), ("20日位置≤40", _cmp(pos20, 40, "le")),
                ("价格在MA20下方", _cmp(dist20, 0, "lt"))]
    elif h == "T+10":
        bull = [("均线多头", stack), ("20日位置≥60", _cmp(pos20, 60, "ge")),
                ("主力净流入", None if net is None else net > 0)]
        bear = [("均线非多头", _neg(stack)), ("20日位置≤30", _cmp(pos20, 30, "le"))]
    else:
        bull = [("均线多头", stack), ("20日位置≥60", _cmp(pos20, 60, "ge")),
                ("乖离在0~15%区间", None if dist20 is None else 0 <= dist20 <= 15)]
        bear = [("均线非多头", _neg(stack)), ("MA20下方超8%", _cmp(dist20, -8, "le"))]
    return bull, bear


def _evidence(base: Dict[str, Any], patterns: List[Dict],
              bars: List[Dict]) -> Tuple[float, List[str], List[str]]:
    """证据完整度 = 拿到维度数 / 计入维度数。

    判「拿到」不看值是否为空，而是看该维度**有没有真的取到**：
      - `missing` 项的前缀命中该维度 ⇒ 没拿到（超时/异常都在此留痕）；
        不这么判的话，某个源超时会让 evidence 虚高成 1.0，与并列的缺口说明自相矛盾
      - `zt_history` / `patterns` 依赖日线；日线为空时算没拿到，不算「确实没有」
    """
    miss_keys = set()
    for m in (base.get("missing") or []):
        key = str(m).split(":")[0].strip()
        if key:
            miss_keys.add(key)

    have: List[str] = []
    for d in _DIMS:
        if _DIM_MISSING_KEY.get(d, d) in miss_keys:
            continue
        if d == "prediction":
            ok = bool((base.get("prediction") or {}).get("ok"))
        elif d in ("patterns", "zt_history"):
            ok = bool(bars)
        else:
            ok = bool(base.get(d))
        if ok:
            have.append(d)
    return (round(len(have) / len(_DIMS), 2) if _DIMS else 0.0), have, list(_DIMS)


def _horizon_view(h: str, tech: Dict[str, Any], flow: Dict[str, Any],
                  pred: Dict[str, Any], levels: Dict[str, Any],
                  bars_n: int, cap: float, evidence: float) -> Dict[str, Any]:
    """单档位视图：方向 + 置信度 + 触发/证伪 + 依据（不给概率，T+1 除外）。"""
    need = _days(h)
    view: Dict[str, Any] = {"h": h, "days": need, "dir": "不适用",
                            "p_up": None, "confidence": 0.0,
                            "basis": [], "trigger": "", "invalidate": ""}

    if not tech:
        view["reason"] = "技术面缺数，无法判定"
        return view
    if bars_n < need:
        view["reason"] = f"日线仅 {bars_n} 根，不足 {need} 根"
        return view

    net: Optional[float] = None
    if isinstance(flow, dict) and flow.get("ok"):
        net = flow.get("main_net_wan")

    anchor_txt = _anchor_txt(_anchor(h, levels))

    if h == "T+1":
        if not (isinstance(pred, dict) and pred.get("ok")):
            view["reason"] = f"标定分缺失：{pred.get('reason') if isinstance(pred, dict) else '无'}"
            return view
        p = float(pred.get("p_up") or 0.5)
        view["p_up"] = p
        view["dir"] = {"bullish": "看多", "bearish": "看空"}.get(pred.get("suggest_dir"), "中性")
        view["basis"] = [f"P(T+1涨)={p}（唯一标定数字）"]
        strength = min(1.0, abs(p - 0.5) / 0.15)
    else:
        bull, bear = _conds_for(h, tech, net)
        hit_b = sum(1 for _, v in bull if v is True)
        hit_r = sum(1 for _, v in bear if v is True)
        if hit_b == len(bull):
            view["dir"], hits, total, side = "看多", hit_b, len(bull), bull
        elif hit_r == len(bear):
            view["dir"], hits, total, side = "看空", hit_r, len(bear), bear
        else:
            side = bull if hit_b >= hit_r else bear
            view["dir"], hits, total = "中性", max(hit_b, hit_r), len(side)
        view["basis"] = [f"{n}={v}" for n, v in side]
        strength = (hits / total) if total else 0.0

    ma20 = levels.get("ma20")
    long_h = h not in ("T+1", "T+3")
    down_ref = f"MA20({ma20})" if (long_h and ma20 is not None) else str(levels.get("low20"))
    up_ref = f"MA20({ma20})" if (long_h and ma20 is not None) else str(levels.get("high20"))

    if view["dir"] == "看多":
        view["trigger"] = f"回踩 {anchor_txt} 不破且量能转强"
        view["invalidate"] = f"跌破 {down_ref}"
    elif view["dir"] == "看空":
        view["trigger"] = f"反弹不过 {anchor_txt} 且量能不济"
        view["invalidate"] = f"站上 {up_ref}"
    else:
        view["trigger"] = f"站上 {up_ref} 转多"
        view["invalidate"] = f"跌破 {down_ref} 转空"

    view["confidence"] = round(cap * evidence * strength, 2)
    return view


def _render(out: Dict[str, Any]) -> str:
    """给 LLM / 用户的诊断摘要：结论先行，四要素齐全，不给模糊措辞。"""
    if out.get("error"):
        return f"ERR: {out['error']}"
    head = f"{out.get('code','')} {out.get('name','')}｜市场情绪 {out.get('regime','')}｜证据完整度 {out.get('evidence')}"
    lines = [head]
    if out.get("missing"):
        lines.append("数据缺口：" + "；".join(out["missing"][:4]))
    lines.append("")
    lines.append("| 周期 | 方向 | 置信度 | 触发条件 | 证伪条件 |")
    lines.append("|---|---|---|---|---|")
    for v in out.get("horizons") or []:
        p = f"P={v['p_up']}" if v.get("p_up") is not None else ""
        d = v["dir"] + (f"（{p}）" if p else "")
        note = v.get("reason") or ""
        lines.append(f"| {v['h']} | {d} | {v['confidence']} | {v['trigger'] or note} | {v['invalidate'] or '—'} |")
    if out.get("basis_note"):
        lines.append("")
        lines.append(out["basis_note"])
    pats = [p.get("pattern") for p in (out.get("patterns") or []) if isinstance(p, dict)]
    if pats:
        lines.append("形态：" + "、".join(str(x) for x in pats[:5]))
    if out.get("risks"):
        lines.append("风险：" + "；".join(out["risks"][:5]))
    return "\n".join(lines)


class AnalyzerResult(dict):
    """dict 子类：代码按 dict 用，转 str 出诊断摘要。"""

    def __str__(self) -> str:
        return _render(self)

    __repr__ = __str__


def analyze(code: str, deep: bool = False) -> dict:
    """对单只 A 股做 T+1~T+20 全周期诊断，输出分周期方向、置信度与关键价位。

    Args:
        code: 6 位股票代码
        deep: 是否追加慢速源（龙虎榜席位），默认 False

    Returns:
        code/name/as_of/regime；quote/tech/prediction/patterns/flow/theme/zt_history/risks；
        levels（支撑与近 20 日高低）；horizons 五档，每档含
        {h, days, dir, p_up(仅T+1), confidence, basis, trigger, invalidate}；
        evidence（证据完整度）与 missing（数据缺口）。
        方向取值：看多 / 看空 / 中性 / 不适用。
    """
    code = str(code or "").strip()
    if not code:
        return AnalyzerResult({"error": "code 不能为空"})

    cap_map = _conf_cap()
    base = _collect(code, deep=bool(deep))
    if base.get("error"):
        return AnalyzerResult({"code": code, "error": base["error"]})

    bars = base.pop("_bars", []) or []
    patterns = base.get("patterns") or []
    tech = base.get("tech") or {}
    levels = _levels(bars, tech)

    evidence, have, counted = _evidence(base, patterns, bars)

    out = dict(base)
    out.update({
        "levels": levels,
        "horizons": [_horizon_view(h, tech, base.get("flow") or {},
                                   base.get("prediction") or {}, levels, len(bars),
                                   cap_map.get(h, 0.5), evidence)
                     for h in _HORIZONS],
        "evidence": evidence,
        "evidence_dims": {"have": have, "counted": counted},
        "basis_note": "confidence = 档位上限 × 证据完整度 × 信号强度；仅 T+1 的 p_up 经标定，其余档位只给方向不给概率。",
    })
    return AnalyzerResult(out)

def deep_cards(codes: str, limit: int = 8, with_flow: bool = True,
               with_patterns: bool = True, with_mtf: bool = False,
               budget: int = None) -> dict:
    """deep 层地基：批量装配全部证据维度（rank 行 + 满卡），按 p_up 降序返回。

    与 `analyze` 的分工：`analyze` 是**单票结论生成器**（给方向/触发/证伪/价位）；
    本函数是它下面的**证据地基**——只取事实、不做判断、不给方向，供上层自行加权。
    逻辑逐字来自 market-screener 的 deep 层（见 `_deep.py` 的复制说明）。

    Args:
        codes: 逗号分隔代码，如 "600519,000858"
        limit: 最多几只（默认 8）
        with_flow: 是否取资金流（口径=量价方向净额，非主力）
        with_patterns: 是否识别 K 线形态
        with_mtf: 是否取多周期结构（需分钟源，更慢）
        budget: 整份返回值的字符预算；缺省 QD_SCREENER_DEEP_BUDGET

    Returns:
        dict: analyzed_rank[]（全部票的横向可比字段，不截断）/
            analyzed[]（满卡，按预算截断）/ report_guide / strategy / missing / errors。
            两档同序（p_up 降序、无预测沉底），故满卡即证据最强的 N 只。
    """
    from ._deep import deep_analyze as _deep_analyze
    return _deep_analyze(codes, limit=limit, with_flow=with_flow,
                         with_patterns=with_patterns, with_mtf=with_mtf,
                         budget=budget)
