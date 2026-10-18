# -*- coding: utf-8 -*-
"""预选「空的兜底BOSS」观测计数器 —— 先看概率，再决定要不要救。

背景（2026-10-01 用户裁定）：
    旧系统 `agents/task_agent.py` 的第三层兜底是「空域不许静默」：域选择为空时
    打 warning 并落 trace 标 `plan_domain_empty_degraded`。本系统迁移后这层没了，
    但我和用户都**不确定它实际发生的概率**——如果万分之一，加修复逻辑是过度设计；
    如果百分之几，那是每天几十次的静默降级，必须救。

    ⇒ 先装计数器跑一段，**用数据决定**，不猜。

如何使用（三步走）：
    Step 1（现在）：集成计数器，读日志 / 调 `report()` 看占比。
    Step 2（数据说话）：按下面的阈值表判断。
    Step 3（达标才动手）：按 `DECISION_NOTE` 里的处置方法改。

升级阈值（**达到任一**才值得动手，别提前优化）：
    * empty_rate > 2%          → 域词典 `DATA_DOMAIN_TOOLS` 缺口，补词（成本最低，先做这个）
    * empty_rate > 5%          → 闸门 `QD_PRESELECT_GATE` 关键词需放宽，把问法加到 hints
    * empty_rate > 10%         → 预选 prompt 或模型有问题，先看 Route A/B/C 哪条在漏
    * 单 domain 连续 N>=20 命中 → 该域该单独考察（可能是某个具体问法没覆盖）

注意：计数器是**进程内**的，重启归零。要看长期趋势得落库或进日志聚合，
近期以 warning 日志为准（`grep "preselect-empty-face"`）。

【O-7 追加（2026-10-17 F23）：观测快照落盘】
    每 run 收口 `snapshot()` 把当前累计观测写一行 JSONL
    （`QD_PRESELECT_STATS_FILE`，默认 agent/traces/preselect_stats.jsonl）；
    读侧 `load_snapshots()` + `aggregate_snapshots()` 按进程（`boot_ts`）分组取
    末条再求和，**跨重启不丢不重** ⇒ 拿到真正的长期趋势（批跑见
    `scripts/preselect_eval.py`）。仍是先攒数、不改任何在线行为。

【差距 K 追加（2026-10）：任务→实际调用工具 配对通道】
    `record_task_calls()` 在 run 收口处把「用户问题 + 本轮真正调用过的工具」
    追加进 JSONL（`QD_PRESELECT_PAIRS_FILE`，默认 agent/traces/preselect_pairs.jsonl），
    供周级离线算共现权重（见 `preselect_cooc.py`）。仍是先攒数、不改任何行为：
    配对文件只被离线脚本读，在线预选完全不依赖它。
"""

import json
import os
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence

# ★ A-04（2026-10-14）：用户输入落盘前统一脱敏，与 `utils/tracing.py` 共用
#   同一个入口（`utils.mask.mask_user_text`）——两个 sink 必须同一个开关，
#   否则会出现「traces 脱了、preselect 没脱」的半边脱敏。
from ..utils.mask import mask_user_text as _mask_user_text

logger = __import__("logging").getLogger(__name__)

# ★ 2026-10-13 P9：本模块已迁出 `tools/` 自发现目录（现 `app/agent/preselect/`），
#   不再需要 `_NOT_TOOLS` 名单——根本不会被扫进注册表。
#   此前名单只登记了 2 个名字，其余 5 个（`report` / `reset` / `record_selected` /
#   `record_empty_face` / `empty_rate`）**实际一直被注册成模型可调工具**——
#   等于把"清空观测计数器"（reset）开放给了模型。移出扫描目录后这洞自然消失。

# 最近样例保留条数（够看规律，又不占内存）
_SAMPLE_LIMIT = 50

# warning 的最小样本下限：样本不足时只 info（**别拿 1/1=100% 下结论**，
# 与本模块 `_verdict` 的"<50 样本继续观测"同一立场，取 20 兼顾响应速度）。
_WARN_MIN_SAMPLE = 20

# `_LATENCY`：total / empty 是**空域率**的分子分母（只统计"本应给出工具面却没给"）；
# gate 是"闸门有意跳过"的独立计数 —— 闲聊/代码类任务**本就不需要工具**，
# 把它算进空域率会污染退化指标（本次事故：打招呼即 1/1=100% 告警）。
# gate_miss（F13 2026-10-17）：闸门**漏判总次数**（含被域兜底救回的），与 gate 分开：
#   gate      只在「跳过闸门 **且** 域兜底也空手」时 +1（空域率视角）；
#   gate_miss 每次漏判都 +1 ⇒ 直接观测漏判率（R2/D-8 长期闭环的缺失口）。
_LATENCY = {"total": 0, "empty": 0, "gate": 0, "gate_miss": 0}
_ROUTES: Counter = Counter()          # 触发原因 → 次数
_DOMAINS: Counter = Counter()         # 兜底也没命中的 #domain → 次数
_SAMPLES: Deque[Dict[str, str]] = deque(maxlen=_SAMPLE_LIMIT)
_PAIRS = {"n": 0}                     # 成功落盘的配对数（进程内，重启归零）
_STARTED = time.time()


def pairs_path() -> Path:
    """「任务→实际调用工具」配对 JSONL 的落盘路径。

    env `QD_PRESELECT_PAIRS_FILE` 覆盖；相对路径按**本文件位置**解析
    （tools/ → agent/），与 cwd 无关——周级脚本在哪个目录跑都能读到同一份。
    """
    raw = os.getenv("QD_PRESELECT_PAIRS_FILE") or "traces/preselect_pairs.jsonl"
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    return p


def stats_path() -> Path:
    """观测快照 JSONL 的落盘路径（O-7）。

    env `QD_PRESELECT_STATS_FILE` 覆盖；相对路径按**本文件位置**解析（与
    `pairs_path` 同款），与 cwd 无关——批跑脚本在哪个目录跑都能读到同一份。
    """
    raw = os.getenv("QD_PRESELECT_STATS_FILE") or "traces/preselect_stats.jsonl"
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    return p


def snapshot() -> Optional[Dict[str, Any]]:
    """把当前累计观测写一行 JSONL（O-7：进程内计数器落盘，求长期趋势）。

    【为什么每 run 写一行】计数器是进程内累计的，进程被强杀时拿不到退出事件，
    所以每 run 收口写一份**当前累计值**（末条即该进程终值）。跨进程重启由
    `aggregate_snapshots` 按 `boot_ts` 分组去重，不会重复计数。

    Returns:
        快照 dict（含 report 字段 + `pid`/`boot_ts`/`file`）；写盘失败 None +
        warning（fail-open，绝不影响回复主链）。
    """
    try:
        path = stats_path()
        rec = {k: v for k, v in report().items()
               if k not in ("recent_samples", "uptime_sec")}
        rec["pid"] = os.getpid()
        rec["boot_ts"] = _STARTED          # 进程启动时间戳 = 分组键
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("[preselect-stats] 快照写盘失败(fail-open): %s: %s",
                       type(e).__name__, e)
        return None
    rec["file"] = str(path)
    return rec


def load_snapshots(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """读回快照 JSONL（跳过坏行/空行；文件不存在/读失败返回 []，fail-open）。"""
    p = Path(path) if path else stats_path()
    out: List[Dict[str, Any]] = []
    try:
        if not p.exists():
            return out
        with p.open("r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except Exception as e:
        logger.warning("[preselect-stats] 快照读回失败(fail-open→空): %s: %s",
                       type(e).__name__, e)
    return out


def aggregate_snapshots(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """跨进程重启聚合观测（O-7 长期趋势）。

    【为什么要分组】计数器是进程内累计的，重启即归零。把所有快照直接相加会
    重复计数；正确做法是**按进程（`boot_ts`）分组、每组只取末条**（该进程终值）
    再求和——重启前后的观测都不丢、也不重。

    Returns:
        {"boots","total","empty","gate_skip","gate_miss","empty_rate","gate_miss_rate"}
    """
    last: Dict[Any, Dict[str, Any]] = {}
    for rec in records or ():
        if not isinstance(rec, dict):
            continue
        key = rec.get("boot_ts")
        if key is None:
            key = rec.get("pid")
        last[key] = rec                    # 文件有序 ⇒ 后写覆盖 = 取末条
    total = sum(int(r.get("total") or 0) for r in last.values())
    empty = sum(int(r.get("empty") or 0) for r in last.values())
    gate = sum(int(r.get("gate_skip") or 0) for r in last.values())
    gm = sum(int(r.get("gate_miss") or 0) for r in last.values())
    return {
        "boots": len(last),
        "total": total,
        "empty": empty,
        "gate_skip": gate,
        "gate_miss": gm,
        "empty_rate": round(empty / total, 4) if total else 0.0,
        "gate_miss_rate": round(gm / (total + gm), 4) if (total + gm) else 0.0,
    }


def record_task_calls(query: str, tools: Sequence[str]) -> Optional[Dict[str, Any]]:
    """攒一对「任务 → 实际调用工具」，追加进 JSONL（差距 K 的在线攒数口）。

    【定位】离线共现权重（`preselect_cooc.build_weights`）的原料。只写文件、
    不读不判——在线行为零变化；读侧（周级脚本）拿不到/读不懂就退化，见
    preselect_cooc 的退化规则。

    Args:
        query: 用户原始问题（qd_agent 传 collector.user_query，已剥 prefetch 注入块）。
        tools: 本轮实际调用过的工具名（collector._all_tools_called，去重保序）。

    Returns:
        {"pairs": 累计条数, "file": 路径}；空输入返回 None（不记也不算错误）；
        写盘失败返回 None 并 warning（fail-open，绝不影响回复主链）。
    """
    q = str(query or "").strip()
    seen: List[str] = []
    for t in tools or ():
        t = str(t or "").strip()
        if t and t not in seen:
            seen.append(t)
    if not q or not seen:
        return None
    path = pairs_path()
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
           # 脱敏后再截断（先截断会把 `sk-xxx…` 切坏、抹不干净）
           "query": _mask_user_text(q)[:2000], "tools": seen}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("[preselect-pairs] 配对写盘失败(fail-open): %s: %s",
                       type(e).__name__, e)
        return None
    _PAIRS["n"] += 1
    return {"pairs": _PAIRS["n"], "file": str(path)}


def record_selected(route: str = "ok") -> None:
    """记一次「预选有结果」（分母）。

    Args:
        route: 来源标记，便于事后看是哪条路径产出的（ok / lint_add / fallback）。
    """
    _LATENCY["total"] += 1
    _ROUTES[route or "ok"] += 1


def record_empty_face(query: str = "", route: str = "", domain_count: int = 0) -> Dict[str, Any]:
    """记一次「工具面跑空」——预选空手且域兜底也没补上。

    这里是第三层兜底的替身：**不静默**。当前只报警+计数，不自动救，
    因为还没证明值不值得救（见模块 docstring 的阈值表）。

    Args:
        query: 触发的用户问句（截断入库，够定位问法就行）。
        route: 走到空手的原因（gate / no_pick / lint_void / fallback_miss …）。
        domain_count: 当时可用候选工具数（0 说明分级本身就是空的，另回事）。

    Returns:
        本次统计摘要（同时写 warning 日志）。
    """
    sample = {"ts": time.strftime("%H:%M:%S"), "route": route or "unknown",
              "q": _mask_user_text(query or "")[:60]}
    _SAMPLES.append(sample)

    # 闸门跳过（gate）是**有意为之**（闲聊/代码类本就无需工具），不是空域失败：
    # 单列计数、不进 empty_rate 分母、**永不打 warning**。
    if (route or "") == "gate":
        _LATENCY["gate"] += 1
        _ROUTES["gate"] += 1
        logger.info("[preselect-gate-skip] 闸门有意跳过预选（不计空域） q=%s", sample["q"])
        return {"gate": _LATENCY["gate"], "total": _LATENCY["total"],
                "rate": empty_rate(), "sample": sample}

    _LATENCY["total"] += 1
    _LATENCY["empty"] += 1
    _ROUTES[route or "unknown"] += 1

    rate = empty_rate()
    # 只在**样本足够 且**比例抬头过阈值时刷 warning，避免 1/1 噪声把日志淹了
    level = "warning" if (_LATENCY["total"] >= _WARN_MIN_SAMPLE and rate > 0.02) else "info"
    getattr(logger, level)(
        "[preselect-empty-face] 工具面跑空 route=%s rate=%.2f%% (%d/%d) q=%s",
        route, rate * 100, _LATENCY["empty"], _LATENCY["total"], sample["q"])
    return {"empty": _LATENCY["empty"], "total": _LATENCY["total"],
            "rate": rate, "sample": sample}


def record_gate_miss(query: str = "") -> Dict[str, Any]:
    """记一次「闸门漏判」——域特征闸门没认出领域、于是跳过了那次 LLM plan。

    【F13 2026-10-17】与 `record_empty_face(route="gate")` 的分工：
      · 后者只在「跳过闸门 **且** 域兜底也空手」时计数（空域率视角，告警噪声低）；
      · 本函数**每次漏判都计**（含被确定性快路由/域兜底救回的），给出漏判总量
        与占比 ⇒ R2「长期漏判率」可直接观测（原实现漏判既不报警、计数器也拿不到）。
    不计入 empty_rate 分母（漏判 ≠ 空域：兜底可能已救回）。
    """
    _LATENCY["gate_miss"] += 1
    _ROUTES["gate_miss"] += 1
    rate = gate_miss_rate()
    q = _mask_user_text(query or "")[:60]
    level = "warning" if (_LATENCY["gate_miss"] >= _WARN_MIN_SAMPLE and rate > 0.05) else "debug"
    getattr(logger, level)("[preselect-gate-miss] 闸门漏判 rate=%.2f%% (%d) q=%s",
                           rate * 100, _LATENCY["gate_miss"], q)
    return {"gate_miss": _LATENCY["gate_miss"], "total": _LATENCY["total"], "rate": rate}


def gate_miss_rate() -> float:
    """漏判占「本应预选」总量的比例 0~1（分母 = 有结果 + 漏判）。"""
    denom = _LATENCY["total"] + _LATENCY["gate_miss"]
    return (_LATENCY["gate_miss"] / denom) if denom else 0.0


def empty_rate() -> float:
    """空域占比 0~1。"""
    return (_LATENCY["empty"] / _LATENCY["total"]) if _LATENCY["total"] else 0.0


def report() -> Dict[str, Any]:
    """一次性健康视图：占比 / 分原因 / 最近样例 / 当前该不该动手。"""
    rate = empty_rate()
    return {
        "total": _LATENCY["total"],
        "empty": _LATENCY["empty"],
        "gate_skip": _LATENCY["gate"],
        "gate_miss": _LATENCY["gate_miss"],
        "gate_miss_rate": round(gate_miss_rate(), 4),
        "empty_rate": round(rate, 4),
        "by_route": dict(_ROUTES.most_common()),
        "by_domain_miss": dict(_DOMAINS.most_common()),
        "recent_samples": list(_SAMPLES)[-10:],
        "pairs_written": _PAIRS["n"],
        "uptime_sec": int(time.time() - _STARTED),
        "verdict": _verdict(rate),
    }


def _verdict(rate: float) -> str:
    """按阈值表给结论（**只解释，不改变行为**）。"""
    if _LATENCY["total"] < 50:
        return "样本不足(<%d)：继续观测，别下结论" % 50
    if rate > 0.10:
        return "严重(>10%)：查预选 prompt / 模型调用本身"
    if rate > 0.05:
        return "偏高(>5%)：放宽 QD_PRESELECT_GATE 关键词"
    if rate > 0.02:
        return "轻微(>2%)：补 DATA_DOMAIN_TOOLS 词典"
    return "健康(<=2%)：保持现状，无需处置"


DECISION_NOTE = """
【达到阈值后怎么改】（按性价比从高到低）
  1. 补词典 `tool_preselect.DATA_DOMAIN_TOOLS`：零成本，先补这里。命中少了自然会漏。
  2. 放宽闸门 `QD_PRESELECT_GATE` hints：闸门是省钱的成本闸，不是"不需要工具"的判决，
     关键词覆盖不到会造成"本该给工具面却没给"。
  3. 给该域加 fallback 条目：`fallback_domain_tools` 只补核心 4 个，够用即可，别贪。
  4. 真要恢复旧系统的"整域注入"——**不要**。本系统 tool-calling 下整域 ≈15k tok/轮。
【禁做的事】为了让 rate 好看而放宽 intake_gate（那是 v1 的另一回事，混进来会污染样本）。
"""


def reset() -> None:
    """清零（仅供测试）。"""
    _LATENCY.update(total=0, empty=0, gate=0, gate_miss=0)
    _ROUTES.clear()
    _DOMAINS.clear()
    _SAMPLES.clear()
    _PAIRS["n"] = 0


__all__ = ["record_selected", "record_empty_face", "record_gate_miss",
           "empty_rate", "gate_miss_rate", "report",
           "reset", "DECISION_NOTE", "record_task_calls", "pairs_path",
           # O-7（2026-10-17 F23）：观测快照落盘 + 跨重启聚合
           "stats_path", "snapshot", "load_snapshots", "aggregate_snapshots"]
