# -*- coding: utf-8 -*-
"""app/watchlist/model.py — label 域的口径单一真值源（纯声明 + 纯函数，零 I/O）

承载（设计依据 `docs/自选股标签统一产出方案.md`）：
  1. **4 段结构契约**（§2.1/§2.2）与扩展段单元 schema（`fields` / `table`）校验
  2. **等级表** `GRADE_TABLE`（§3.1）与覆盖规则（§3.2）
  3. **system 评分口径 v1**（§7）：权重表 + 分段归一函数 + 空值再归一 + `SCORE_VERSION`
  4. 市场白名单与空值语义（§7.5）

纪律
  - 本文件**不 import** `app.agent.*` / `app.market_cn.auto.*`（§5.5 禁令 1）
  - 全部函数为**纯函数**：无随机、无时间、无状态、无未来函数（§7.1）
  - `SCORE_VERSION` **人工递增，禁止运行时自动计算**（§7.7 R1）；口径变更必须 +1 并留冻结快照
"""
from __future__ import annotations

import numbers
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ═══════════════════════════════════════════════════════════════════
# 1. 等级表与覆盖规则（§3.1 / §3.2）
# ═══════════════════════════════════════════════════════════════════

#: source → grade。**auto 最高**：掌握信号→入场→止损→持仓→出场全链交易证据（§3.1）
GRADE_TABLE: Dict[str, int] = {"system": 1, "agent": 2, "auto": 3}

#: 允许通过 `submit()` 写入的 source（system 由 write_system_facts 独占；无人工写、无 grade 4）
SUBMIT_SOURCES: Tuple[str, ...] = ("agent", "auto")

#: 唯一写通道的 grade 反查（防止外部伪造 grade）
GRADE_BY_SOURCE = {v: k for k, v in GRADE_TABLE.items()}

#: 空白的 grade 语义（不落行）
GRADE_BLANK = 0


# ═══════════════════════════════════════════════════════════════════
# 2. 4 段结构契约（§2.1 / §2.2）
# ═══════════════════════════════════════════════════════════════════

#: 4 段 key（渲染顺序即此序）
SECTION_KEYS: Tuple[str, ...] = ("supports", "resistances", "score", "extras")

SECTION_TITLES: Dict[str, str] = {
    "supports": "支撑位",
    "resistances": "压力位",
    "score": "评分",
    "extras": "扩展",
}

#: 前端通用渲染器需支持的 type（第 1~3 段是同一套原语的特化）
SECTION_TYPES: Dict[str, str] = {
    "supports": "levels",
    "resistances": "levels",
    "score": "score",
    "extras": "units",
}

#: 扩展段允许的单元类型（只有两种 ⇒ 前端只需一个通用渲染器）
UNIT_TYPES: Tuple[str, ...] = ("fields", "table")

#: 关键位（levels 项）必备键
LEVEL_REQUIRED_KEYS: Tuple[str, ...] = ("price",)

#: 市场白名单（§7.5：非 CN/HK **明确不产出**，不是落 NULL）
SUPPORTED_MARKETS: Tuple[str, ...] = ("CNStock", "HKStock")

#: 数据不足判定（§7.5）
MIN_KLINES = 60          # len(klines) < 60 ⇒ 整票不产出 ⇒ 空白
MIN_W_EFF = 0.60         # W_eff < 0.60 ⇒ 不产出 score


class LabelContractError(ValueError):
    """payload 不符 4 段契约 —— 唯一写通道的结构校验失败。"""


def _is_number(v: Any) -> bool:
    """数值判定。**必须接受 `Decimal`** —— 库侧 `score NUMERIC(5,2)` 读回来是 Decimal。"""
    return isinstance(v, numbers.Number) and not isinstance(v, bool)


def validate_levels(items: Any, *, field: str) -> List[Dict[str, Any]]:
    """校验并规范化关键位列表（1~3 项，每项必须带 `price`）。

    保留调用方给出的顺序（§7 P2/P3 约定 [0] = 最近项）。
    """
    if items is None:
        return []
    if not isinstance(items, (list, tuple)):
        raise LabelContractError(f"{field} 必须是数组")
    out: List[Dict[str, Any]] = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise LabelContractError(f"{field}[{i}] 必须是对象")
        for k in LEVEL_REQUIRED_KEYS:
            if k not in it:
                raise LabelContractError(f"{field}[{i}] 缺少必备键 {k}")
        if not _is_number(it.get("price")):
            raise LabelContractError(f"{field}[{i}].price 必须是数值")
        out.append(dict(it))
    if len(out) > 3:
        raise LabelContractError(f"{field} 最多 3 项（当前 {len(out)}）")
    return out


def validate_extras(units: Any) -> List[Dict[str, Any]]:
    """校验扩展段：有序单元列表，单元类型只能是 `fields` / `table`（§2.2）。"""
    if units is None:
        return []
    if not isinstance(units, (list, tuple)):
        raise LabelContractError("extras 必须是数组（有序单元列表）")
    out: List[Dict[str, Any]] = []
    for i, u in enumerate(units):
        if not isinstance(u, dict):
            raise LabelContractError(f"extras[{i}] 必须是对象")
        t = u.get("type")
        if t not in UNIT_TYPES:
            raise LabelContractError(f"extras[{i}].type 必须是 {UNIT_TYPES} 之一，得到 {t!r}")
        if t == "fields":
            rows = u.get("rows")
            if not isinstance(rows, (list, tuple)):
                raise LabelContractError(f"extras[{i}].rows 必须是数组")
            for j, r in enumerate(rows):
                if not isinstance(r, dict) or "label" not in r:
                    raise LabelContractError(f"extras[{i}].rows[{j}] 必须是 {{label, value}}")
        else:  # table
            cols = u.get("columns")
            if not isinstance(cols, (list, tuple)) or not cols:
                raise LabelContractError(f"extras[{i}].columns 必须是非空数组")
            keys = []
            for j, c in enumerate(cols):
                if not isinstance(c, dict) or "key" not in c or "label" not in c:
                    raise LabelContractError(f"extras[{i}].columns[{j}] 必须是 {{key, label}}")
                keys.append(c["key"])
            if len(set(keys)) != len(keys):
                raise LabelContractError(f"extras[{i}].columns 的 key 重复")
            rows = u.get("rows")
            if not isinstance(rows, (list, tuple)):
                raise LabelContractError(f"extras[{i}].rows 必须是数组")
            for j, r in enumerate(rows):
                if not isinstance(r, dict):
                    raise LabelContractError(f"extras[{i}].rows[{j}] 必须是对象")
        out.append(dict(u))
    return out


def validate_score(score: Any, *, field: str = "score") -> Optional[float]:
    """校验评分：None 或 0~100 数值（库侧另有 CHECK 兜底，此处提前拦）。"""
    if score is None:
        return None
    if not _is_number(score):
        raise LabelContractError(f"{field} 必须是数值或 None")
    if not (0 <= float(score) <= 100):
        raise LabelContractError(f"{field} 必须在 0~100 之间，得到 {score}")
    return round(float(score), 2)


def validate_payload(payload: Any, *, source: str) -> Dict[str, Any]:
    """按 4 段契约校验上级提交的 payload（`submit` 内唯一入口）。"""
    if not isinstance(payload, dict):
        raise LabelContractError("payload 必须是对象")
    extras = validate_extras(payload.get("extras"))
    sv = payload.get("score_version")
    if sv is not None and not isinstance(sv, int):
        raise LabelContractError("score_version 必须是整数（该 source 内的口径版本）")
    return {
        "supports": validate_levels(payload.get("supports"), field="supports"),
        "resistances": validate_levels(payload.get("resistances"), field="resistances"),
        "score": validate_score(payload.get("score")),
        "score_version": sv,
        "extras": extras,
    }


# ═══════════════════════════════════════════════════════════════════
# 3. system 评分口径 v1（§7）—— SCORE_VERSION = 1
# ═══════════════════════════════════════════════════════════════════

#: ⚠️ 人工递增，禁止运行时计算（§7.7 R1）。任何影响 score 数值的口径变化必须 +1（R2）。
SCORE_VERSION = 1

#: 分段线性插值点：(x, y) 必须按 x 严格升序；两端超出即 clamp 到端点 y。
#: 形态原则（§7.4）：全部分段线性（可手算无黑箱）；每项最小值 > 0（禁端点堆积）；
#: 超买/超卖/暴量都不给满分（追高不该得高分）。
SEG = {
    # 趋势
    "T2": ((-0.03, 0.0), (0.03, 1.0)),                      # z = close/ma20 - 1
    "T4": ((-0.02, 0.10), (0.00, 0.45), (0.03, 1.00)),      # s = ma20[-1]/ma20[-6] - 1
    # 动能
    "M1": ((20.0, 0.55), (45.0, 0.90), (65.0, 1.00), (80.0, 0.35)),   # RSI 梯形
    "M2": ((-10.0, 0.15), (0.0, 0.50), (8.0, 1.00), (20.0, 0.55)),    # ROC(10)
    "M3": ((0.6, 0.20), (1.0, 0.70), (2.0, 1.00), (4.0, 0.40)),       # 量能比
    # 位置
    "P1": ((0.30, 0.20), (0.60, 1.00), (0.90, 1.00), (1.00, 0.25)),   # 获利比例
    "P2": ((0.01, 1.00), (0.08, 0.20)),                               # 到最近支撑距离
    "P3": ((0.02, 0.20), (0.12, 1.00)),                               # 到最近压力空间
    # 风险（值越大越安全）
    "R1": ((0.10, 0.60), (0.25, 1.00), (0.70, 1.00), (0.95, 0.25)),   # 布林带宽分位
    "R2": ((0.05, 1.00), (0.20, 0.10)),                               # 20 日振幅
}

#: 峰后急降档（§7.4 明写：给"极端值"单独的更低调，不是简单 clamp）
OVERRIDE = {
    "M1": (80.0, 0.15),   # RSI ≥ 80 ⇒ 0.15（超买）
    "M2": (20.0, 0.35),   # ROC ≥ 20 ⇒ 0.35（短期过涨）
    "M3": (4.0, 0.30),    # 量比 ≥ 4 ⇒ 0.30（暴量）
}

#: 权重表 = 4 维度 12 子项，**合计 1.00**（§7.3）。维度权重 = 其子项权之和。
SCORE_ITEMS: Tuple[Tuple[str, str, str, float], ...] = (
    # (key, 展示名, 维度, 权重)
    ("T1", "均线排列", "trend", 0.12),
    ("T2", "价格 vs MA20", "trend", 0.10),
    ("T3", "MACD 状态", "trend", 0.08),
    ("T4", "MA20 斜率", "trend", 0.05),
    ("M1", "RSI(14)", "momentum", 0.10),
    ("M2", "ROC(10)", "momentum", 0.05),
    ("M3", "量能比", "momentum", 0.05),
    ("P1", "获利比例", "position", 0.12),
    ("P2", "距最近支撑", "position", 0.10),
    ("P3", "至最近压力空间", "position", 0.08),
    ("R1", "布林带宽分位", "risk", 0.07),
    ("R2", "20 日振幅", "risk", 0.08),
)

DIM_WEIGHTS: Dict[str, float] = {
    d: round(sum(w for _, _, dd, w in SCORE_ITEMS if dd == d), 10)
    for d in ("trend", "momentum", "position", "risk")
}

assert abs(sum(w for *_, w in SCORE_ITEMS) - 1.0) < 1e-9, "子项权重之和必须为 1.00"


def piecewise(x: float, pts: Sequence[Tuple[float, float]]) -> float:
    """分段线性归一 `L(x; ...)`：两端 clamp 到端点值（§7.4 统一记法）。"""
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def _norm_seg(key: str, x: float) -> float:
    """带峰后急降档的分段归一（`OVERRIDE` 优先，见 §7.4 各子项末档）。"""
    hi = OVERRIDE.get(key)
    if hi is not None and x >= hi[0]:
        return hi[1]
    return piecewise(x, SEG[key])


def norm_T1(ma5: float, ma10: float, ma20: float) -> float:
    """均线排列（离散档，§7.4）。"""
    if ma5 > ma10 > ma20:
        return 1.00
    if ma5 > ma20:
        return 0.65
    if ma10 > ma20:
        return 0.35
    return 0.10


def norm_T3(hist: Sequence[float]) -> float:
    """MACD 状态（离散档，§7.4）：hist 为 MACD 柱序列（末根最新）。"""
    h = hist[-1]
    if h > 0:
        return 1.00 if (len(hist) >= 2 and h > hist[-2]) else 0.80
    if len(hist) >= 2 and abs(h) < abs(hist[-2]):
        return 0.45
    return 0.15


def normalize(key: str, x: float) -> float:
    """子项归一入口：T1/T3 由调用方先行离散化（`norm_T1` / `norm_T3`），此处只处理连续档。"""
    if key in ("T1", "T3"):
        raise KeyError(f"{key} 是离散档，请直接调用 norm_T1 / norm_T3")
    return _norm_seg(key, x)


def score_from_features(feats: Dict[str, float]) -> Dict[str, Any]:
    """§7 评分**纯函数**：特征 dict → score / breakdown / W_eff。

    Args:
        feats: 子项 key → **已归一**的 x∈[0,1]；缺键（或值为 None）⇒ 剔除该子项并再归一。
               离散档由调用方用 `norm_T1` / `norm_T3` 先行算好（同为 0~1）。

    Returns:
        {"score": float|None, "w_eff": float, "breakdown": {key: {norm, weight, contrib}},
         "dropped": [key,...]}
        `w_eff < MIN_W_EFF` ⇒ `score = None`（§7.5，该票不产出 ⇒ 空白）。
    """
    used, dropped = {}, []
    for key, _label, _dim, w in SCORE_ITEMS:
        x = feats.get(key)
        if x is None:
            dropped.append(key)
            continue
        used[key] = max(0.0, min(1.0, float(x)))

    w_eff = sum(w for key, _l, _d, w in SCORE_ITEMS if key in used)
    if w_eff < MIN_W_EFF or not used:
        return {"score": None, "w_eff": round(w_eff, 6), "breakdown": {}, "dropped": dropped}

    breakdown: Dict[str, Dict[str, float]] = {}
    acc = 0.0
    for key, label, dim, w in SCORE_ITEMS:
        if key not in used:
            continue
        contrib = 100.0 * w * used[key] / w_eff
        acc += contrib
        breakdown[key] = {
            "label": label, "dim": dim, "norm": round(used[key], 6),
            "weight": w, "contrib": round(contrib, 4),
        }
    return {
        "score": round(acc, 2),
        "w_eff": round(w_eff, 6),
        "breakdown": breakdown,
        "dropped": dropped,
    }
