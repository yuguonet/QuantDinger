# -*- coding: utf-8 -*-
"""预选共现权重（差距 K：工具预选从启发式到可学习）。

## 解决什么

现预选排序是纯启发式（域词典 + token 倒排索引，见 `tool_preselect.py`）：
同义问法 / 新黑话（“打板”“埋伏”）词典命中不了 ⇒ 该用的工具不在预选面
（选错工具面 = 该用的工具不在场），召回上不去。

## 三段管线（数据流单向，任何一段缺失都退化）

    在线攒对    preselect_stats.record_task_calls() → JSONL（每 run 一对）
    周级离线    build_weights(pairs) → 共现权重 JSON（token × 工具 计数）
    在线排序    rank_candidates(names, query, weights) —— 分数 = 启发式 × 权重

## 退化规则（永不更差 · 冷启动与现状 bit 级一致）

以下任一成立 ⇒ `rank_candidates` **原序返回**（候选集合、顺序都与加权前相同）：
  1. 权重文件不存在 / 损坏 / 版本不匹配（冷启动常态）；
  2. 权重样本不足（total_pairs < min_pairs，默认 50，env `QD_PRESELECT_MIN_PAIRS`）；
  3. 查询的 token 与权重表零覆盖（这次问法没学到过）;
  4. 覆盖到 token 但候选集里没有任何工具共现（max_cooc == 0）；
  5. 任何异常（调用方 qd_agent 还有一层 fail-open try/except 兜底）。

## 易错点（坑）

- **只排序，不改集合**：候选目录本来就是全量按需层，权重只影响呈现顺序
  （影响 LLM 点名的先后偏好），工具数量不变 ⇒ token 不回升。
- 分数 = (1 + 启发式命中数) × (1 + 归一化共现)：启发式命中给了 1 的下限，
  否则“黑话问法”（启发式命中=0）永远乘 0、学了也白学；共现项归一到 (0,1]
  保证权重最多把分数翻倍，不会淹没启发式。
- 本模块**不住在** tools/ 自发现目录下（2026-10-13 P9 迁至 `app/agent/preselect/`）
  ⇒ 不会被扫成工具，无需 `_NOT_TOOLS` 名单。
- 裸名/全名双导入与 qd_agent 同款 try/except，防止同一模块双实例。
- 配对 JSONL 是追加写、**旧→新**；DB 源读出来也统一成旧→新，
  held-out 一律切“最后一段”（时间序，避免未来数据泄漏进训练段）。

## 用法

    # 周级（backend_api_python 下）：
    python -m app.agent.preselect.preselect_cooc --db      # 读 qd_agent_traces（真实历史）
    python -m app.agent.preselect.preselect_cooc           # 读配对 JSONL
    # 真实预选面（LLM 点名）的召回 ≥95% 验收需线上/跑批数据；
    # 本脚本输出的是**代理指标**（词典面 vs 加权面），只用于校验逻辑与趋势。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

# 【2026-10-13·P5】原本是「裸名优先 + `app.agent.tools.*` 全名兜底」的双导入。
# 改造后两种写法都不成立也不该成立：本目录不再进 sys.path（裸名无效），
# 而 `app.agent` 已改名指向新版骨架、那里没有这两个模块（全名恒 ImportError，
# 而且是被 except 吞掉的静默失效）。相对导入是唯一正解，不需要兜底。
from .tool_preselect import _tokens, detect_domains, DATA_DOMAIN_TOOLS
from .preselect_stats import pairs_path

logger = logging.getLogger(__name__)

# ★ 2026-10-13 P9：本模块已迁出 `tools/` 自发现目录（现 `app/agent/preselect/`），
#   不再需要 `_NOT_TOOLS` 名单——根本不会被扫进注册表。
#   此前名单要求"新增公开函数必须同步登记"，这条约定必被遗忘且忘了是静默的。

__all__ = [
    "record_pairs_path", "weights_path", "load_pairs", "load_pairs_from_db",
    "build_weights", "save_weights", "load_weights", "get_weights",
    "rank_candidates", "heldout_recall", "main",
]

WEIGHTS_VERSION = 1
MIN_PAIRS_DEFAULT = 50          # 样本低于此数不产出权重（与 preselect_stats 的 <50 不下结论同口径）


def _min_pairs() -> int:
    """最小配对样本数（env `QD_PRESELECT_MIN_PAIRS` 覆盖）。"""
    try:
        return max(1, int(os.getenv("QD_PRESELECT_MIN_PAIRS", "") or MIN_PAIRS_DEFAULT))
    except ValueError:
        return MIN_PAIRS_DEFAULT


def record_pairs_path() -> Path:
    """在线攒对的 JSONL 路径（委托 preselect_stats，单一事实源）。"""
    return pairs_path()


def weights_path() -> Path:
    """离线权重文件路径。

    env `QD_PRESELECT_COOC_FILE` 覆盖；相对路径按本文件位置解析（与 cwd 无关）。
    """
    raw = os.getenv("QD_PRESELECT_COOC_FILE") or "traces/preselect_cooc.json"
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    return p


# ═══════════════════════════════════════════════════════════════════════════
#  一、配对读取（JSONL / qd_agent_traces 两个来源，都统一成 旧→新）
# ═══════════════════════════════════════════════════════════════════════════
def load_pairs(path: Optional[Path] = None) -> List[dict]:
    """读配对 JSONL → [{"query": str, "tools": [str]}]（旧→新）。

    文件不存在返回 []；坏行跳过（攒数通道宁缺勿断，不让一行脏数据毁整份）。
    """
    p = Path(path) if path else record_pairs_path()
    if not p.exists():
        return []
    out: List[dict] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                q = str(rec.get("query") or "").strip()
                tools = [str(t) for t in (rec.get("tools") or []) if str(t).strip()]
                if q and tools:
                    out.append({"query": q, "tools": tools})
    except Exception as e:
        logger.warning("[preselect-cooc] 配对读取失败(按空处理): %s: %s",
                       type(e).__name__, e)
        return []
    return out


def load_pairs_from_db(limit: int = 5000) -> List[dict]:
    """读 qd_agent_traces 的 (user_query, tools_called) → 配对（旧→新）。

    【为什么可以直接读】现表已有这两列（chain 层根节点），**不新开表**；
    这是 JSONL 通道上线前就积累的真实调用数据。status='ok' 才计（失败 run
    的工具链不完整，会把召回评估往坏里带偏）。
    """
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT user_query, tools_called FROM qd_agent_traces "
                "WHERE layer='chain' AND status='ok' "
                "  AND user_query IS NOT NULL AND tools_called IS NOT NULL "
                "  AND array_length(tools_called, 1) > 0 "
                "ORDER BY id DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
    except Exception as e:
        logger.warning("[preselect-cooc] DB 取配对失败(fail-open→空): %s: %s",
                       type(e).__name__, e)
        return []
    out: List[dict] = []
    for row in reversed(rows or []):        # DESC → 统一成旧→新
        # ⚠ get_db_connection 的游标是字典行（RealDictCursor 风格）；
        # 按元组解包会把**列名**当成数据（首次跑批踩过：query 全变 "user_query"）。
        if isinstance(row, dict):
            q, tools = row.get("user_query"), row.get("tools_called")
        else:
            q, tools = row[0], row[1]
        q = str(q or "").strip()
        ts = [str(t) for t in (tools or []) if str(t).strip()]
        if q and ts:
            out.append({"query": q, "tools": ts})
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  二、离线权重（周级）
# ═══════════════════════════════════════════════════════════════════════════
def build_weights(pairs: Sequence[dict], min_pairs: Optional[int] = None) -> Optional[dict]:
    """共现计数：token × 工具（周级离线跑；样本不足返回 None ⇒ 一切退化为启发式）。

    分词与倒排索引同源（`tool_preselect._tokens`：ASCII 词 + 中文 2-gram），
    保证“训练怎么切、线上查询怎么切”，不同分词器学了也对不上。

    Returns:
        {"version", "total_pairs", "min_pairs", "token_tool", "tool_n",
         "generated_at"}；可直接 `save_weights`。样本不足 → None。
    """
    m = _min_pairs() if min_pairs is None else int(min_pairs)
    usable = [p for p in (pairs or [])
              if str(p.get("query") or "").strip() and (p.get("tools") or [])]
    if len(usable) < m:
        return None
    token_tool: Dict[str, Dict[str, int]] = {}
    tool_n: Dict[str, int] = {}
    for p in usable:
        tools = []
        for t in p["tools"]:
            t = str(t).strip()
            if t and t not in tools:
                tools.append(t)
        for t in tools:
            tool_n[t] = tool_n.get(t, 0) + 1
        for tok in set(_tokens(p["query"])):
            d = token_tool.setdefault(tok, {})
            for t in tools:
                d[t] = d.get(t, 0) + 1
    return {
        "version": WEIGHTS_VERSION,
        "total_pairs": len(usable),
        "min_pairs": m,
        "token_tool": token_tool,
        "tool_n": tool_n,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_weights(weights: Optional[dict], path: Optional[Path] = None) -> bool:
    """落权重 JSON（失败 warning + False；调用方保留旧文件即可）。"""
    if not weights:
        return False
    p = Path(path) if path else weights_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(weights, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(p)                         # 原子换，防周级跑到一半被在线读到半份
        return True
    except Exception as e:
        logger.warning("[preselect-cooc] 权重写盘失败(保留旧文件): %s: %s",
                       type(e).__name__, e)
        return False


def load_weights(path: Optional[Path] = None) -> Optional[dict]:
    """读权重；不存在/损坏/版本不符/样本不足 → None（= 退化为纯启发式）。"""
    p = Path(path) if path else weights_path()
    if not p.exists():
        return None
    try:
        w = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[preselect-cooc] 权重文件损坏(退化为启发式): %s: %s",
                       type(e).__name__, e)
        return None
    if not isinstance(w, dict) or w.get("version") != WEIGHTS_VERSION:
        return None
    if int(w.get("total_pairs") or 0) < max(int(w.get("min_pairs") or 0), _min_pairs()):
        return None                            # 建权时够、后来门槛抬高了 → 也退化
    if not w.get("token_tool"):
        return None
    return w


# 权重缓存：按 (路径, mtime) 失效——每 run 至多 stat 一次，换权重文件立即生效
_WEIGHTS_CACHE: Dict[str, object] = {"key": None, "w": None}


def get_weights(path: Optional[Path] = None) -> Optional[dict]:
    """取（并缓存）权重；任何异常一律 None（fail-open）。"""
    try:
        p = Path(path) if path else weights_path()
        if not p.exists():
            return None
        key = (str(p), p.stat().st_mtime_ns)
        if _WEIGHTS_CACHE["key"] != key:
            _WEIGHTS_CACHE["key"] = key
            _WEIGHTS_CACHE["w"] = load_weights(p)
        return _WEIGHTS_CACHE["w"]  # type: ignore[return-value]
    except Exception as e:
        logger.debug("[preselect-cooc] get_weights 跳过(fail-open): %s: %s",
                     type(e).__name__, e)
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  三、在线排序（预选打分 = 启发式 × 权重）
# ═══════════════════════════════════════════════════════════════════════════
def rank_candidates(names: Sequence[str], query: str,
                    weights: Optional[dict],
                    index: Optional[Dict[str, set]] = None) -> List[str]:
    """按「启发式 × 共现权重」给候选重排；数据不足/零覆盖 ⇒ **原序返回**。

    Args:
        names: 候选工具名（现状目录顺序 = 注册表顺序，即“现启发式”的序）。
        query: 用户问题（与攒对时的 query 同源，分词器一致）。
        weights: build_weights 产物（None = 冷启动，直接原序）。
        index: 工具倒排索引（tool_preselect.get_tool_index），提供启发式命中数；
            None 时命中数按 0（只按共现排）。

    分数 = (1 + 索引命中数) × (1 + cooc/max_cooc)；同分保持原序（稳定排序）。
    """
    out = list(names or ())
    if not out or not weights:
        return out
    if weights.get("version") != WEIGHTS_VERSION:
        return out
    try:
        if int(weights.get("total_pairs") or 0) < int(weights.get("min_pairs") or 0):
            return out                          # 样本不足 → 退化（双保险：load 已判过）
        tt = weights.get("token_tool") or {}
        toks = [t for t in set(_tokens(query)) if t in tt]
        if not toks:
            return out                          # 零覆盖问法 → 原序（规则 3）
        cooc = {n: sum(int(tt[t].get(n, 0) or 0) for t in toks) for n in out}
        max_cooc = max(cooc.values()) if cooc else 0
        if max_cooc <= 0:
            return out                          # 候选里没人共现 → 原序（规则 4）
        hit: Dict[str, int] = {}
        if index:
            for t in set(_tokens(query)):
                for n in index.get(t, ()):
                    if n in cooc:               # 只关心候选内的命中
                        hit[n] = hit.get(n, 0) + 1
        scored = []
        for i, n in enumerate(out):
            score = (1 + hit.get(n, 0)) * (1.0 + cooc.get(n, 0) / max_cooc)
            scored.append((-score, i, n))       # 负号降序；i 保证同分原序
        scored.sort()
        return [n for _, _, n in scored]
    except Exception as e:
        logger.debug("[preselect-cooc] rank 跳过(fail-open→原序): %s: %s",
                     type(e).__name__, e)
        return out


# ═══════════════════════════════════════════════════════════════════════════
#  四、held-out 召回率（验收口径的计算逻辑）
# ═══════════════════════════════════════════════════════════════════════════
def heldout_recall(pairs: Sequence[dict],
                   face_fn: Callable[[str], Sequence[str]]) -> dict:
    """held-out 任务上「实际调用工具 ∈ 预选面」的召回率。

    验收口径（差距文档原文）：held-out 召回率 ≥95%。这里算的是**按调用计**
    的命中率：hit = (任务, 实际调用工具) 对中工具落在预选面内的比例；
    另附 task_all_rate（该任务全部实际工具都在面内的任务占比），供参考。

    Args:
        pairs: held-out 段 [{"query", "tools"}]。
        face_fn: query → 预选面工具名列表（评测方注入：真实 LLM 预选面、
            代理面、或任何可复现的面构造逻辑）。
    """
    tasks = calls = hits = all_in = 0
    misses: List[dict] = []
    for p in pairs or ():
        tools = [str(t) for t in (p.get("tools") or []) if str(t).strip()]
        if not tools:
            continue
        q = str(p.get("query") or "")
        face = set(face_fn(q) or ())
        tasks += 1
        calls += len(tools)
        hit_n = 0
        for t in tools:
            if t in face:
                hit_n += 1
            else:
                misses.append({"query": q[:60], "tool": t})
        hits += hit_n
        if hit_n == len(tools):
            all_in += 1
    recall = (hits / calls) if calls else 0.0
    return {
        "recall": round(recall, 4),
        "pass_95": bool(calls) and recall >= 0.95,
        "tasks": tasks,
        "calls": calls,
        "hits": hits,
        "task_all_rate": round(all_in / tasks, 4) if tasks else 0.0,
        "misses": misses[:20],
    }


# ═══════════════════════════════════════════════════════════════════════════
#  五、周级入口（CLI）
# ═══════════════════════════════════════════════════════════════════════════
def _core_names() -> set:
    """必选层近似集合（离线拿不到 QDAgent._core_names，按同一分级口径拼）。

    验收口径里「实际调用工具 ∈ 预选面」只针对**按需层**：必选层（common 域 +
    元工具 + mimo 原生）每轮本来就在场，算 miss 会把召回冤枉地拉低。
    拼不齐就当空集（= 不剔除，退回未过滤口径），不阻断。
    """
    core: set = set()
    try:
        from ..tools._base import ToolProvider
        p = ToolProvider.get_or_build()
        core |= set(p.list_by_domain("common"))
        core |= set(p.get_meta_functions())
    except Exception as e:
        logger.debug("[preselect-cooc] core(common/meta) 取失败: %s: %s",
                     type(e).__name__, e)
    try:
        from mimoagent.tools.mimocode import MIMOCODE_CORE_TOOL_NAMES
        core |= set(MIMOCODE_CORE_TOOL_NAMES)
    except Exception:
        pass
    core.add("update_plan")      # qd_agent 跨目录注册的计划工具，同样每轮在场
    return core


def _domain_face(query: str, k: int) -> List[str]:
    """启发式预选面代理：域词典候选（现行 lint R1 / 域兜底同源），截断 k。"""
    out: List[str] = []
    for dom in detect_domains(query):
        for t in DATA_DOMAIN_TOOLS.get(dom, ()):
            if t not in out:
                out.append(t)
            if len(out) >= k:
                return out[:k]
    return out[:k]


def _weighted_face(query: str, weights: dict, k: int) -> List[str]:
    """加权预选面代理：域候选 ∪ 该问法共现过的工具，按加权分取前 k。"""
    base = _domain_face(query, k)
    pool = list(base)
    tt = weights.get("token_tool") or {}
    for tok in set(_tokens(query)):
        for t, c in (tt.get(tok) or {}).items():
            if c > 0 and t not in pool:
                pool.append(t)
    return rank_candidates(pool, query, weights)[:k]


def main(argv: Optional[Sequence[str]] = None) -> int:
    """周级离线：读配对 → 算共现权重 → 落盘 → held-out 代理召回报告。

    python -m app.agent.preselect.preselect_cooc [--db] [--holdout 20] [--k 8]

    注意：输出的召回是**代理指标**（面 = 词典/共现构造，不是 LLM 真实点名），
    用于校验数据与逻辑通不通；验收 ≥95% 要用真实预选面跑批后复核。
    """
    ap = argparse.ArgumentParser(description="预选共现权重周级计算")
    ap.add_argument("--db", action="store_true",
                    help="从 qd_agent_traces 读配对（默认读 JSONL）")
    ap.add_argument("--pairs", default="", help="配对 JSONL 路径（默认走通道）")
    ap.add_argument("--holdout", type=int, default=20,
                    help="时间序切最后一段做 held-out（默认 20 对）")
    ap.add_argument("--k", type=int, default=8,
                    help="预选面大小（默认 8 = QD_PRESELECT_MAX 默认值）")
    ap.add_argument("--out", default="", help="权重输出路径（默认 QD_PRESELECT_COOC_FILE）")
    ap.add_argument("--min-pairs", type=int, default=0,
                    help="覆盖 QD_PRESELECT_MIN_PAIRS（默认 0=用全局）")
    args = ap.parse_args(list(argv) if argv is not None else None)

    # 独立跑周级脚本时没人替我们加载 .env（run.py 才有）——与 run.py 同款兜底，
    # 仅 CLI 路径执行；dotenv 不在则靠环境变量，缺了 DB 源自然 fail-open 成空。
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)
    except Exception:
        pass

    if args.db:
        pairs = load_pairs_from_db()
    elif args.pairs:
        pairs = load_pairs(Path(args.pairs))
    else:
        pairs = load_pairs()
    # 时间序切尾做 held-out，且最多占 20%——样本本来就少时优先保训练段
    n_hold = max(0, min(args.holdout, len(pairs) // 5))
    train = pairs[:-n_hold] if n_hold else pairs
    test = pairs[-n_hold:] if n_hold else []
    min_pairs = args.min_pairs or None
    weights = build_weights(train, min_pairs=min_pairs)

    report: dict = {
        "pairs_total": len(pairs),
        "train": len(train),
        "heldout": len(test),
        "min_pairs": min_pairs or _min_pairs(),
        "weights_built": bool(weights),
        "source": "db" if args.db else (args.pairs or str(record_pairs_path())),
    }
    if weights:
        out = Path(args.out) if args.out else None
        report["saved"] = save_weights(weights, out)
        report["weights_file"] = str(out or weights_path())
        report["tools_learned"] = len(weights.get("tool_n") or {})
        report["tokens_learned"] = len(weights.get("token_tool") or {})
        if test:
            # 召回只考核**按需层**调用（必选层/元工具每轮在场，不归预选管）
            core = _core_names()
            ev: List[dict] = []
            skipped = 0
            for p in test:
                od = [t for t in p["tools"] if t not in core]
                skipped += len(p["tools"]) - len(od)
                if od:
                    ev.append({"query": p["query"], "tools": od})
            report["eval_note"] = (
                "held-out 按需层口径（已剔除必选层/元工具调用 %d 次）；"
                "代理面 ≠ LLM 真实预选面，达标性以跑批为准" % skipped)
            report["heldout_heuristic"] = heldout_recall(
                ev, lambda q: _domain_face(q, args.k))
            report["heldout_weighted"] = heldout_recall(
                ev, lambda q: _weighted_face(q, weights, args.k))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if weights else 1


if __name__ == "__main__":
    raise SystemExit(main())
