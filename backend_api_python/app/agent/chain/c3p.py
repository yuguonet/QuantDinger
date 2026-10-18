"""C3P 编排：一次推理 → 证据向量（EV-1）+ 配对双跑（v5 **P2 接线**，2026-10-13）

## 这个模块解决什么问题

v3 附录 F 的偏差核对结论：C3P 的第 ②③ 段**没接线** ——
`evidence.build_evidence()` / `aggregator.aggregate()` / `aggregator.record_pair()`
三个已建好的模块**生产代码零调用方**，`case_memory.record_case(..., evidence=)`
参数就绪却没人传。⇒ **`p_final` 从未被真实产出、Δp 一条都没落过**。

本模块就是那条线：**claim 级**地把它们串起来。

## ★ 为什么是 claim 级，不是 run 级

1. `aggregator.record_pair()` 的**第一个参数就是 `claim_id`** —— 契约本就按 claim 设计；
2. Δp 的真值 `y` 来自该 claim 的 `resolution` ⇒ **必须按 claim 配对**，按 run 配对会对不上；
3. run 级可能根本没有单一标的（"帮我看看大盘"），而 claim 一定有 `subject`
   （`claims.guess_subject` 抽不出标的就不产 claim）。

## ★ Δp 为 0 仍然可能是**设计内的预期行为**（P3 已接线，但仍在冷启动期）

2026-10-14 **P3 已接线**：`coefs` 不再恒为 `None`，改由
`chain/calib_feed.coefs_for(ck)` 供给（pairs × resolutions → 条件格 → 独立簇折算
→ 层级池化 → β₀）。但**样本不足时它仍然返回 None** ——
独立簇 < `min_clusters` 的格只出先验（P3-3 禁进判定）⇒ `Δlogit=0`
⇒ `p_final == p_base`。
依据 v3 §15.1：「无经验时（新格子/冷启动）β₀=0, βⱼ=0 ⇒ p_final=p_base，fail-open
天然成立。」⇒ 攒够之前 Δp 恒 0 是**对的**，
**不要为了"看到非零 Δp"去凭空编系数。**

## 两个方向相反的 fail-open（最容易写反，写反就产生假数据）

| 情形 | 处置 | 为什么 |
|---|---|---|
| evidence 取不到（LLM 挂了 / 无 falsifiers） | **仍记 pair**，标 `degraded=True` | G20 的监控需要「降级比例」这个分母 —— 不记就永远分不清「没经验」还是「没产出」 |
| `p_base` 取不到 | **不记 pair** | 对照臂缺失的 Δp 是**假数据**，不是"降级数据" |

## 红线

**R1**：LLM 自报概率一律丢弃 —— 由 `evidence._strip_llm_probability` 与
       `aggregator._scan_r1` 拦，**本模块不重复实现**。
**R2**：`p_base` 路径禁止接入 `qd_agent_weights` / `exp_*` 反馈信号 ⇒ 本模块源码里
       不得出现这些表名（`tests/test_v5_c3p.py` 有扫描守卫）。

## 易错处

· ★ `chain.X` 与 `app.agent.chain.X` **不是同一模块对象**（`app/agent` 被单独加进 sys.path）。
  ⇒ 单测一律 patch **本模块对象上的属性**（`c3p._kf` / `c3p.p_base_of` / `c3p.extract_evidence`），
  不要去 patch `app.agent.chain.evidence.*` —— 打不到，且会**误触真源网络**。
· 情境键由 `evidence.condition_key()` **确定性**算出，取不到情境标 `unknown`
  （**不猜** —— 猜出来的键会把样本分进错误的格子，且事后无法识别）。
· 证据提取是**一次额外 LLM 调用**，必须带超时 + fail-open，不能因为它挂了让用户收不到答复。
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from time import perf_counter
from typing import Any, Dict, List, Optional, Sequence

from ..log import logger

from . import aggregator as _agg
from . import calib_feed as _cfd
from . import context_frame as _cf
from . import evidence as _evidence
from . import kline_feed as _kf

SCHEMA_VER = "c3p1"

_DEFAULT_MAX_CLAIMS = 2          # 一次最多处理几条 claim（控成本）
_DEFAULT_BARS = 120              # 取数根数（远大于模型下限）
_DEFAULT_LLM_TIMEOUT = 180.0     # 秒
# 【2026-10-16 实测校正 20 → 180】本网关（api.g2claw.com / DeepSeek-V4.1-Flash）
#   处理本任务需 80~155 秒：同一 payload 实测成功耗时 83.8 / 96.4 / 154.1 秒。
#   旧默认 20s 配上 mimo 通道的 tenacity 5 次重试 ⇒ 5 次全超时、墙钟 124.9s
#   后降级（白烧且拿不到证据）。改走独立通道（`llm.complete_direct`，attempts=2）
#   后，本值即"单次读取超时"，最坏墙钟 ≈ 2×180+6s，且只影响后台旁路。
#   可用 `QD_C3P_LLM_TIMEOUT` 覆盖（换更快的网关/小模型时可下调）。
_MIN_BARS = 60                   # 与 app.watchlist.predict.MIN_BARS 对齐（有守卫测试）

_CODE_RE = re.compile(r"(\d{6})")

# ── 证据提取 prompt（v3 §13 EV-1；★ 不许让模型报概率 —— 见红线 R1）────────
# 2026-10-14 外置 prompts/evidence_extract.md（禁止概率字段的纪律在文件内）
from ..prompts import load_prompt as _load_prompt
_EV_SYSTEM = _load_prompt("evidence_extract")


# ═══════════════════════════════════════════════════════════════
#  阈值（禁止硬编码，一律可从 env 改；脏值回落默认，不崩）
# ═══════════════════════════════════════════════════════════════

def _env_int(name: str, default: int) -> int:
    try:
        v = int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _env_float(name: str, default: float) -> float:
    try:
        v = float(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def enabled() -> bool:
    """总开关 `QD_C3P_ENABLED`（默认开）。"""
    return str(os.getenv("QD_C3P_ENABLED", "1")).strip().lower() not in (
        "0", "false", "no", "off", "")


def max_claims() -> int:
    return _env_int("QD_C3P_MAX_CLAIMS", _DEFAULT_MAX_CLAIMS)


def bars_n() -> int:
    """取数根数。**下限就是模型下限**（低于它模型直接返回 None，等于白取一次网络）。"""
    return max(_env_int("QD_C3P_BARS", _DEFAULT_BARS), _MIN_BARS)


def llm_model() -> str:
    return str(os.getenv("QD_C3P_LLM_MODEL", "") or "").strip()


def llm_timeout() -> float:
    return _env_float("QD_C3P_LLM_TIMEOUT", _DEFAULT_LLM_TIMEOUT)


# ═══════════════════════════════════════════════════════════════
#  路段：情境帧 / 标的 / 基线
# ═══════════════════════════════════════════════════════════════

def current_frame(as_of: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """当日情境帧（C 类特征）。取不到返回 `None` ⇒ 条件键标 `unknown`，**不猜**。"""
    try:
        frames = _cf.load_frames() or []
        if not frames:
            return None
        if as_of:
            hit = [f for f in frames if str(f.get("as_of") or "")[:10] == str(as_of)[:10]]
            if hit:
                return hit[-1]
        return frames[-1]
    except Exception as e:
        logger.debug("[C3P] 情境帧读取失败(标 unknown): %s: %s", type(e).__name__, e)
        return None


def resolve_subject(claim: Optional[Dict[str, Any]]) -> Optional[str]:
    """claim → 个股代码（6 位）。非个股（指数/板块/空）返回 `None`。

    ★ 为什么只认 `subject_kind == "stock"`：`predict_next_day` 是个股模型，
      喂指数/板块代码会算出一条**看起来合理但毫无意义**的概率 —— 那正是"假数据"。
    """
    if not isinstance(claim, dict):
        return None
    if str(claim.get("subject_kind") or "").strip() != "stock":
        return None
    m = _CODE_RE.search(str(claim.get("subject") or ""))
    return m.group(1) if m else None


def _predict_next_day(stock_klines: Sequence[Dict[str, Any]],
                      market_klines: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """冻结基线的**唯一**入口（惰性 import，方便单测替换）。

    ★ 只调用 `app.watchlist.predict.predict_next_day`，**不改**它（红线 R2：
      基线的系数必须保持冻结，否则 Δp 的分母会自己漂移）。
    """
    from app.watchlist.predict import predict_next_day
    return predict_next_day(list(stock_klines or []), list(market_klines or []))


def p_base_of(code: str) -> Optional[float]:
    """冻结基线给出的 P(涨)。取不到返回 `None` —— **不许兜底成 0.5**。

    兜底成 0.5 的危害：事后分不清「模型真给了 0.5」与「这次根本没算出来」，
    而这两者在 Δp 统计里必须是不同的处理方式（后者要丢弃，不是记 0）。
    """
    try:
        bars = _kf.daily_bars(code, bars_n())
        if not _kf.enough(bars, _MIN_BARS):
            logger.debug("[C3P] %s 日线不足 %d 根 ⇒ p_base 取不到（不兜底）", code, _MIN_BARS)
            return None
        pred = _predict_next_day(bars, _kf.market_bars(bars_n()))
        if not isinstance(pred, dict):
            return None
        p = pred.get("p_up")
        return float(p) if isinstance(p, (int, float)) else None
    except Exception as e:
        logger.debug("[C3P] p_base 取不到(fail-open): %s: %s", type(e).__name__, e)
        return None


# ═══════════════════════════════════════════════════════════════
#  路段：证据提取（一次轻量 LLM）
# ═══════════════════════════════════════════════════════════════

def _call_llm(system: str, user: str, timeout: float) -> str:
    """一次轻量 LLM 调用，带超时。失败抛异常（由调用方 fail-open）。

    【2026-10-13 P1】旧实现把同步/异步两种上下文都搓了一遍：无事件循环时
    `asyncio.run`，有循环时塞 ThreadPoolExecutor，外面再套 `wait_for`。三层嵌套
    只为完成"一次调用"，而且 wait_for 取消不了线程里已经发出去的 HTTP 请求 ——
    超时只是"不再等"，请求仍在跑（旧的 LLM 出口没有 timeout 参数可传）。
    【2026-10-16 二次改】原走上层 `complete()`（mimo 通道）⇒ 其 `_query` 上的
    tenacity **硬编码 5 次**重试改不掉，一次调用的墙钟 = 5 ×(timeout+退避)，
    实测 `timeout=5` 花 52.9s、`timeout=20` 花 124.9s 且**必然失败**
    （该网关对本任务本身就慢：成功调用 15.4/77.5/81.3/189.7 秒）。
    现改走 `app.agent.llm.complete_direct()` —— 独立 requests 通道，
    尝试次数/读取超时/输出上限全部由本模块控制，并回传**精确 usage**。
    主链路与其它调用点仍用 `complete()`，两个出口并存是有意为之。

    `QD_C3P_LLM_MODEL` 优先于全局模型：C3P 的证据提取是轻量活，允许单独指定小模型。
    """
    from app.agent.llm import complete_direct

    # 2026-10-16 输入压 token：输出上限默认 1200→800（抽取 JSON 远小于此），
    # 尝试次数默认 2→1（`QD_C3P_LLM_ATTEMPTS`；C3P 已转后台，少一次重试即少一份
    # 重复输入 token）。两值均可 env 覆盖，不硬编码。
    resp = complete_direct(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=llm_model() or None,
        timeout=timeout,
        max_tokens=_env_int("QD_C3P_MAX_OUTPUT_TOKENS", 800),
        attempts=_env_int("QD_C3P_LLM_ATTEMPTS", 1),
    )
    return str(resp.content or "")


def _json_obj(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 输出里抠出第一个 JSON 对象（复用 `utils.json_parser`，不另写一份）。"""
    try:
        from ..utils.json_parser import extract_json
        obj = extract_json(text, default=None)
    except Exception:
        obj = None
    if isinstance(obj, dict):
        return obj
    m = re.search(r"\{.*\}", str(text or ""), re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def _json_safe(obj: Any) -> Any:
    """把 `date` / `datetime` 递归转成 ISO 串 —— **evidence 的契约是可 JSON 序列化**。

    ★ 为什么必须有这一步（2026-10-14 实测 cli 抓到）：
      `claims.due_date_of()` 有意返回 **`date` 对象**（§9：不改列类型、不改扫描 SQL）。
      当 LLM 没产出 claims 时，`extract_evidence` 会**直接拿原始 claim 兜底**
      ⇒ `date` 混进 `evidence.claims[*].due_date` ⇒ 下游 `case_memory.record_case`
      的 `json.dumps(outcome)` 抛 `TypeError: Object of type date is not JSON
      serializable` ⇒ **案例一条都进不了库**（fail-open，只有一条 warn，静默）。

    ⚠ 更阴的地方：`evidence.append_evidence()` 落盘用的是 `default=str`，
      所以 JSONL 里看到的是规规矩矩的 `"2026-10-20"` —— **落盘看不出问题**，
      只有走 `record_case` 那条路才炸。排查时别被 JSONL 骗了。
    """
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def extract_evidence(*, user_query: str = "", answer: str = "",
                     subject: Optional[str] = None,
                     frame: Optional[Dict[str, Any]] = None,
                     claim: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """一次轻量 LLM → EV-1 证据向量（并落 JSONL）。失败返回 `None`（fail-open）。

    ★ 为什么是"额外一次调用"而不是让主答复里带 JSON 块：
      ① 不动主 prompt ⇒ 不动 smoke/bench 基线；
      ② 不需要从最终答复里清洗 JSON；
      ③ 模型合规率可控（不产出就降级，且能被 G20 的降级比例看见）。
    """
    try:
        # 2026-10-16 输入压 token：query/answer 截断长度改 env 可配且收紧默认
        # （600→400 / 2000→1500）。这两段是 payload 的绝对大头；claim 自带
        # `source_quote`，故缩短 answer 仍保留判断依据。
        payload = {
            "subject": subject,
            "user_query": str(user_query or "")[:_env_int("QD_C3P_MAX_QUERY_CHARS", 400)],
            "answer": str(answer or "")[:_env_int("QD_C3P_MAX_ANSWER_CHARS", 1500)],
            "claim": {k: claim.get(k) for k in
                      ("claim_type", "subject", "horizon", "predicted", "source_quote")
                      if isinstance(claim, dict) and claim.get(k) is not None},
        }
        text = _call_llm(_EV_SYSTEM, json.dumps(payload, ensure_ascii=False), llm_timeout())
        obj = _json_obj(text)
        if not obj:
            logger.debug("[C3P] 证据提取未产出合法 JSON ⇒ 降级")
            return None

        ev = _evidence.build_evidence(
            subject=subject,
            as_of=(frame or {}).get("as_of"),
            # ★ 兜底用原始 claim 时必须先过 `_json_safe`：claim 里的 `due_date`
            #   是 `date` 对象（见 `_json_safe` 的注释）。
            claims=obj.get("claims") or ([_json_safe(claim)] if claim else []),
            discriminators=obj.get("discriminators") or [],
            falsifiers=obj.get("falsifiers") or [],
            frame=frame,
            extra=_json_safe({k: v for k, v in obj.items()
                              if k not in ("claims", "discriminators",
                                           "falsifiers")}),
        )
        try:
            _evidence.append_evidence(ev)
        except Exception as e:                       # 落盘失败不影响主链
            logger.debug("[C3P] 证据落盘跳过: %s", e)
        return ev
    except Exception as e:
        logger.debug("[C3P] 证据提取失败(fail-open): %s: %s", type(e).__name__, e)
        return None


# ═══════════════════════════════════════════════════════════════
#  编排
# ═══════════════════════════════════════════════════════════════

def run_for_decision(*, user_query: str = "", answer: str = "",
                     tracked: bool = True,
                     decision_id: Optional[int] = None,
                     claim_ids: Optional[Sequence[Any]] = None,
                     claims: Optional[Sequence[Dict[str, Any]]] = None,
                     as_of: Optional[str] = None) -> Dict[str, Any]:
    """一次推理 → 证据 + 配对。**永不抛异常**（挂在收尾钩子上，必须 fail-open）。

    Returns:
        {"ran","reason","as_of","decision_id","n_claims","n_pairs","condition_key",
         "evidence","degraded","pairs","schema_ver","elapsed_ms"}
        ★ `evidence` 供调用方回传给 `case_memory.record_case`（落 `outcome.evidence`）。
    """
    t0 = perf_counter()
    out: Dict[str, Any] = {
        "ran": False, "reason": "", "as_of": str(as_of or "")[:10],
        "decision_id": decision_id, "n_claims": 0, "n_pairs": 0,
        "condition_key": "", "evidence": None, "degraded": False,
        "pairs": [], "calib": None, "schema_ver": SCHEMA_VER, "elapsed_ms": 0.0,
    }
    try:
        if not enabled():
            out["reason"] = "disabled"
            return out
        if not tracked:
            out["reason"] = "not_tracked"
            return out

        ids = list(claim_ids or [])
        cl = list(claims or [])
        if not ids or not cl:
            out["reason"] = "no_claim"
            return out
        out["n_claims"] = len(ids)

        # claim_id 与 claim 按下标对齐（intake 的落库顺序即抽取顺序）
        works: List[tuple] = []
        for cid, c in list(zip(ids, cl))[:max_claims()]:
            if cid is None:
                # 该 claim 未落库（save_claims 的失败位）⇒ 没有可回写的 id。
                # 必须**跳过**而不是沿用下一个 id：那会把 Δp 记到别人的账上。
                continue
            code = resolve_subject(c)
            if code:
                works.append((cid, c, code))
        if not works:
            out["reason"] = "no_subject"
            return out

        frame = current_frame(as_of or None)
        ev: Optional[Dict[str, Any]] = None
        try:
            ev = extract_evidence(user_query=user_query, answer=answer,
                                  subject=works[0][2], frame=frame,
                                  claim=works[0][1])
        except Exception as e:                       # ★ 证据挂在结尾，绝不能带崩主链
            logger.debug("[C3P] 证据提取异常(降级): %s: %s", type(e).__name__, e)
            ev = None
        out["evidence"] = ev
        # v3 §19.1：没有证伪条件 ⇒ 本次降级（有对照臂，但不给经验加成）
        degraded = not bool((ev or {}).get("falsifiers"))
        out["degraded"] = degraded

        for cid, c, code in works:
            ck = str((ev or {}).get("condition_key")
                     or _evidence.condition_key(code, frame) or "")
            out["condition_key"] = ck
            pb = p_base_of(code)
            if pb is None:
                continue                            # ★ 没有对照臂 ⇒ 不记（记了是假数据）
            # ★ P3 接线（2026-10-14）：系数来自「攒下来的 pair × 判定」的条件校准。
            #   样本不足 ⇒ calib_feed 返回 None ⇒ aggregate 走 fail-open（p_final=p_base），
            #   与接线前**完全一致** —— 不给"估估看"的系数（那等于拿噪声当信号）。
            coefs = None
            try:
                coefs = _cfd.coefs_for(ck)
            except Exception as e:                  # 校准是旁路：挂了不许带崩推理
                logger.debug("[C3P] 取校准系数失败(降级为无系数): %s: %s",
                             type(e).__name__, e)
                coefs = None
            _calib_meta = (coefs or {}).get("source_level") or (
                "" if coefs else "none")
            if degraded:
                # ★ v3 §19.1 降级门（2026-10-14 补，修 S-3）：缺证伪条件 ⇒ 本次降级。
                #   ① 根本**不进经验环节**（不是"算了再压回去"）——aggregate 的
                #      Δlogit 一律作废 ⇒ p_final = p_base（不给经验加成）。
                #      原实现里 degraded 只是 extra 里的 flag，aggregate 照常跑 ⇒
                #      ev=None + coefs={"b0":0.25} 时 p_final≈0.61 ≠ p_base。
                #      当前 coefs=None 让它无症状（fail-open 本就等于 p_base）；
                #      P3 一接线，这些"没证伪条件"的样本就会带着假 Δp 进拟合。
                #   ② **留痕必须跟着变**：`calib_source` 写 "degraded"，且
                #      `calib.used` 记 False —— 否则降级样本会被记成
                #      「source=cell（用了本格系数）但 Δp=0」，事后按来源层分组
                #      统计时出现自相矛盾的行（可审计性）。
                #   ③ **pair 照记**：G20 的「降级比例」需要这个分母。
                r = {"p_final": pb, "clipped": False, "used_coefs": False}
                pf = pb
                _calib_meta = "degraded"
            else:
                r = _agg.aggregate(pb, ev, ck, coefs=coefs)
                pf = r.get("p_final")
                pf = float(pf) if isinstance(pf, (int, float)) else pb
            try:
                _agg.record_pair(
                    cid, pb, pf, condition_key=ck, as_of=(as_of or None),
                    extra={"degraded": degraded,
                           "degrade_reason": ("" if degraded else ""),
                           "subject": code,
                           "horizon": str(c.get("horizon") or ""),
                           "ev_ver": (ev or {}).get("ev_ver", ""),
                           "schema_ver": SCHEMA_VER,
                           "decision_id": decision_id,
                           "clipped": bool(r.get("clipped")),
                           # ★ 校准来源层：事后能一眼看出这条 Δp 是"本格自己的"
                           #   还是"借来的"（把借来的当成自己的 = 静默放大）
                           "calib_source": _calib_meta,
                           "calib_n": int((coefs or {}).get("n_clusters") or 0),
                           "calib_pooled_n": int(
                               (coefs or {}).get("pooled_n_clusters") or 0)})
            except Exception as e:
                logger.debug("[C3P] 配对落盘跳过: %s", e)
                continue
            out["pairs"].append({"claim_id": cid, "p_base": pb, "p_final": pf,
                                 "condition_key": ck, "degraded": degraded})
            if out.get("calib") is None:
                # ★ 降级 ⇒ 系数拿来了也没用（`used=False`），留痕才对得上
                out["calib"] = {"used": bool(coefs) and not degraded,
                                "source": _calib_meta,
                                "n_clusters": int(
                                    (coefs or {}).get("n_clusters") or 0),
                                "pooled_n_clusters": int(
                                    (coefs or {}).get("pooled_n_clusters") or 0)}
            out["n_pairs"] += 1

        out["ran"] = True
        if out["n_pairs"] == 0:
            out["reason"] = "no_p_base"
    except Exception as e:                            # 整条旁路永不阻断
        out["reason"] = f"error:{type(e).__name__}"
        logger.debug("[C3P] 流水线异常(fail-open): %s: %s", type(e).__name__, e)
    finally:
        out["elapsed_ms"] = round((perf_counter() - t0) * 1000, 2)
    return out


def load_recent_pairs(as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    """读回配对记录（薄封装，避免调用方各自拼 `aggregator` 路径）。"""
    try:
        return _agg.load_pairs(as_of)
    except Exception:
        return []
