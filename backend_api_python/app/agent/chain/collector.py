# -*- coding: utf-8 -*-
"""collector — v5 盘后采集编排（v5 阶段 D4，2026-10-13）

## ★ 为什么必须有这个文件

D1（`context_frame`，情境帧）与 D2（`sampler`，分层配额）只提供了**函数**，
**没有任何地方调用它们** ⇒ 数据一天都不会攒。
而 C 类情境**不可重建** —— 今天没记，事后补不回来。
⇒ 本模块是 D1/D2 的**价值兑现点**：挂进盘后 worker，每日自动跑。

## 设计要点（改本文件时先读）

1. **分项隔离**：情境帧失败**不拖累**样本采集，反之亦然。
   一项挂了另一项照样落盘 —— C 类不可重建，能记多少记多少。
2. **不静默成功**：任一项失败都会出现在 `errors` 里且 `ok=False`。
   "看起来跑完了但什么都没记"是本项目最贵的一类失败。
3. **同日幂等**：同 `as_of` 重跑是**覆盖**不是追加（补跑不会重复计票）。
4. **不把异常抛给 worker**：盘后任务不能因为采集挂掉而中断 T+N 验证。
   全部失败也只返回 `ok=False` + `errors`。
5. 落盘目录走 env，默认 `backend_api_python/data/` 下，**不放 tmp/**
   （tmp 会被清理，而 C 类不可重建 ⇒ 放会被清的地方等于白记）。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..log import logger

from . import context_frame, sampler

SCHEMA_VER = "col1"

# 每日采样数（分层配额的 n）。与 v5 §19.2 的统计功效约束一致：
# 150 只/日 ≈ 4.5 独立观察/日（DEFF_day=33.6），年 361 独立簇（T+3 口径）。
DEFAULT_N = 150


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _collect_n() -> int:
    return _env_int("QD_COLLECT_N", DEFAULT_N)


def _universe_dir() -> str:
    """样本落盘目录。**每次读 env**（便于测试与运维覆盖），不缓存成模块常量。"""
    return os.getenv("QD_COLLECT_UNIVERSE_DIR") or str(
        Path(__file__).resolve().parents[3] / "data" / "daily_universe")


def universe_path(as_of: str) -> Path:
    """按月分文件（与 context_frame 同款切法）：`daily_universe_YYYY-MM.jsonl`。"""
    return Path(_universe_dir()) / f"daily_universe_{str(as_of)[:7]}.jsonl"


def _read(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            out.append(json.loads(s))
        except (ValueError, TypeError):
            continue                        # 坏行跳过，不因一行毁掉整个文件
    return out


def _append_universe(rec: Dict[str, Any]) -> Path:
    """落盘一日样本。**同 as_of 覆盖**（幂等，补跑不重复）。"""
    key = str(rec.get("as_of") or "")[:10]
    path = universe_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [r for r in _read(path) if str(r.get("as_of") or "")[:10] != key]
    rows.append(rec)
    rows.sort(key=lambda r: str(r.get("as_of") or ""))
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)
        + ("\n" if rows else ""),
        encoding="utf-8")
    return path


def load_universe(as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    """回读样本。指定 `as_of` 时只回该月文件再过滤（不扫全目录）。"""
    if as_of:
        rows = _read(universe_path(str(as_of)[:10]))
        return [r for r in rows if str(r.get("as_of") or "")[:10] == str(as_of)[:10]]
    d = Path(_universe_dir())
    if not d.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in sorted(d.glob("daily_universe_*.jsonl")):
        out.extend(_read(p))
    return out


def run_daily_collection(as_of: Optional[str] = None,
                         n: Optional[int] = None) -> Dict[str, Any]:
    """盘后采集：① C 类情境帧 ② 分层配额样本。

    Args:
        as_of: 指定交易日（默认取**最近已收盘交易日**）。
        n: 采样数（默认 `QD_COLLECT_N`，150）。

    Returns:
        {"as_of", "schema_ver", "ok", "frame", "frame_path",
         "universe", "universe_path", "errors", "finished_at"}
        ★ `ok=False` 时 `errors` **必非空** —— 不允许"静默成功"。
    """
    errors: List[str] = []
    _n = n if n is not None else _collect_n()

    # ── ① 情境帧（C 类，不可重建，最高优先）────────────────────────────
    frame: Optional[Dict[str, Any]] = None
    frame_path_s: Optional[str] = None
    try:
        _d: Optional[date] = None
        if as_of:
            try:
                _d = date.fromisoformat(str(as_of)[:10])
            except (ValueError, TypeError):
                _d = None
        frame = context_frame.build_frame(_d)
        frame_path_s = str(context_frame.append_frame(frame))
    except Exception as e:
        logger.warning("[Collector] 情境帧采集中止: %s", e)
        errors.append(f"frame: {type(e).__name__}: {e}")

    # as_of 以**帧**为准（帧自带"最近已收盘交易日"的正确回退逻辑），
    # 帧失败时才退回调用方传入值 —— 保证两项记的是**同一天**。
    _as_of = str((frame or {}).get("as_of") or as_of
                 or context_frame.last_closed_trading_day())[:10]

    # ── ② 分层配额样本 ────────────────────────────────────────────────
    uni: Optional[Dict[str, Any]] = None
    uni_path_s: Optional[str] = None
    try:
        uni = sampler.pick_daily_universe(_as_of, _n)
        rec = {
            "as_of": _as_of,
            "schema_ver": SCHEMA_VER,
            "n": _n,
            "universe": uni,
            "collected_at": datetime.now().isoformat(timespec="seconds"),
        }
        uni_path_s = str(_append_universe(rec))
    except Exception as e:
        logger.warning("[Collector] 样本采集中止: %s", e)
        errors.append(f"universe: {type(e).__name__}: {e}")

    ok = not errors
    if not ok:
        logger.warning("[Collector] 盘后采集部分失败 as_of=%s errors=%s", _as_of, errors)
    return {
        "as_of": _as_of,
        "schema_ver": SCHEMA_VER,
        "ok": ok,
        "frame": frame,
        "frame_path": frame_path_s,
        "universe": uni,
        "universe_path": uni_path_s,
        "errors": errors,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
