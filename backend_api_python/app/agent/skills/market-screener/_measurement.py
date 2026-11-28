# -*- coding: utf-8 -*-
"""market-screener/_measurement.py — 闭环测量：记录选股 → 结算 T+N 收益 → 汇总单位时间收益。

选股主流程只旁路记录，不参与任何取舍。账本落盘于
backend_api_python/data/market_screener/（picks.jsonl / settlements.jsonl，append-only）。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from .common import _backend_root, fetch_kline
from ...log import logger

_LEDGER_DIR = os.path.join(_backend_root, "data", "market_screener")
_PICKS_PATH = os.path.join(_LEDGER_DIR, "picks.jsonl")
_SETTLEMENTS_PATH = os.path.join(_LEDGER_DIR, "settlements.jsonl")
_DEFAULT_HORIZONS = (1, 3, 5)
_DEFAULT_KLINE_DAYS = 60
_SETTLE_WORKERS = 8

_lock = threading.Lock()


def _ensure_dir() -> None:
    try:
        os.makedirs(_LEDGER_DIR, exist_ok=True)
    except Exception as e:
        logger.warning("[measure] 账本目录创建失败 %s: %s", _LEDGER_DIR, e)


def _append_jsonl(path: str, obj: Dict) -> bool:
    """线程安全追加一行 JSON。失败只 warning 不抛——测量是旁路。"""
    try:
        _ensure_dir()
        line = json.dumps(obj, ensure_ascii=False, default=str)
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except Exception as e:
        logger.warning("[measure] 账本写入失败 %s: %s", path, e)
        return False

def _read_jsonl(path: str) -> List[Dict]:
    out: List[Dict] = []
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    out.append(json.loads(s))
                except Exception:
                    continue
    except Exception as e:
        logger.warning("[measure] 账本读取失败 %s: %s", path, e)
    return out


def record_run(picks: List[Dict], strategy: str, elapsed_s: float,
               date: Optional[str] = None, funnel: Optional[Dict] = None,
               meta: Optional[Dict] = None) -> Optional[str]:
    """记录一次 run() 的选股与耗时，返回 run_id（失败返回 None）。"""
    try:
        now = datetime.now()
        run_id = f"{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        rec = {
            "run_id": run_id,
            "ts": now.isoformat(timespec="seconds"),
            "date": date or now.strftime("%Y-%m-%d"),
            "strategy": strategy,
            "elapsed_s": round(float(elapsed_s or 0.0), 3),
            "n_picks": len(picks or []),
            "picks": [
                {
                    "code": str(p.get("code") or ""),
                    "name": p.get("name") or "",
                    "source": p.get("source") or "",
                    "is_baseline": bool(p.get("is_baseline")),
                }
                for p in (picks or [])
                if p.get("code")
            ],
            "funnel": funnel or {},
            "meta": meta or {},
        }
        _append_jsonl(_PICKS_PATH, rec)
        return run_id
    except Exception as e:
        logger.warning("[measure] record_run 失败: %s", e)
        return None

def _entry_and_exits(bars, entry_date, horizons):
    """按选股日收盘价入场，T+N 交易日收盘价出场。bars 按日期升序。"""
    if not bars:
        return None
    bs = sorted(bars, key=lambda b: str(b.get("time") or ""))
    entry_idx = None
    for i, b in enumerate(bs):
        if str(b.get("time") or "") >= entry_date:
            entry_idx = i
            break
    if entry_idx is None:
        return None
    entry_close = bs[entry_idx].get("close")
    if not entry_close:
        return None
    out = {"entry_date": bs[entry_idx].get("time"), "entry_close": entry_close,
           "horizons": {}}
    for h in horizons:
        j = entry_idx + int(h)
        if j < len(bs):
            close = bs[j].get("close")
            if close:
                out["horizons"][str(h)] = {
                    "date": bs[j].get("time"), "close": close,
                    "ret_pct": round((close / entry_close - 1.0) * 100.0, 4),
                }
            else:
                out["horizons"][str(h)] = None
        else:
            out["horizons"][str(h)] = None
    return out


def _normalize_codes(codes):
    if not codes:
        return None
    if isinstance(codes, str):
        codes = codes.split(",")
    return {str(c).strip() for c in codes if str(c).strip()}

def settle_picks(codes=None, lookback_days: int = _DEFAULT_KLINE_DAYS,
                 horizons=(1, 3, 5), limit: Optional[int] = None) -> Dict:
    """结算已成熟 pick 的 T+N 收益并写入 settlements 账本。

    入场价 = 选股日收盘价；出场价 = T+N 交易日收盘价。只结算选股日早于今天的票，
    缺失的 horizon 留 None，下次调用补齐（与已有值合并，不回退已结算值）。
    """
    horizons = tuple(int(h) for h in (horizons or _DEFAULT_HORIZONS))
    want = _normalize_codes(codes)
    picks = _read_jsonl(_PICKS_PATH)
    settlements = _read_jsonl(_SETTLEMENTS_PATH)

    latest: Dict[Tuple[str, str], Dict] = {}
    for ev in settlements:
        key = (str(ev.get("run_id") or ""), str(ev.get("code") or ""))
        if key[0] and key[1]:
            latest[key] = ev.get("horizons") or {}

    today = datetime.now().strftime("%Y-%m-%d")
    targets = []
    for rec in picks:
        run_id = str(rec.get("run_id") or "")
        rdate = str(rec.get("date") or "")
        for p in rec.get("picks") or []:
            code = str(p.get("code") or "")
            if not code or (want and code not in want):
                continue
            if rdate >= today:
                continue  # 选股日未过完，T+N 尚不可得
            key = (run_id, code)
            cur = latest.get(key, {})
            if all(cur.get(str(h)) is not None for h in horizons):
                continue  # 全部 horizon 已结算（缺失值 None 视为未结算，可补）
            targets.append((run_id, rdate, code, p.get("name") or "",
                            p.get("source") or "", rec.get("strategy") or "",
                            cur))

    if limit:
        targets = targets[: int(limit)]

    codes_needed = sorted({t[2] for t in targets})
    bars_map: Dict[str, List[Dict]] = {}
    ex = None
    try:
        ex = ThreadPoolExecutor(max_workers=_SETTLE_WORKERS)
        futures = {ex.submit(fetch_kline, c, int(lookback_days)): c
                   for c in codes_needed}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                bars_map[c] = fut.result()
            except Exception as e:
                logger.warning("[measure] 结算取K线失败 %s: %s", c, e)
                bars_map[c] = []
    finally:
        if ex is not None:
            ex.shutdown(wait=False, cancel_futures=True)

    settled = 0
    skipped = 0
    for run_id, rdate, code, name, source, strategy, cur in targets:
        bars = bars_map.get(code) or []
        res = _entry_and_exits(bars, rdate, horizons)
        if res is None:
            skipped += 1
            continue
        merged = dict(cur)
        for h in horizons:
            new = res["horizons"].get(str(h))
            if new is not None:
                merged[str(h)] = new
            elif str(h) not in merged:
                merged[str(h)] = None
        ev = {
            "run_id": run_id, "code": code, "name": name, "source": source,
            "strategy": strategy, "pick_date": rdate,
            "settled_at": datetime.now().isoformat(timespec="seconds"),
            "entry_date": res["entry_date"], "entry_close": res["entry_close"],
            "horizons": merged,
        }
        _append_jsonl(_SETTLEMENTS_PATH, ev)
        latest[(run_id, code)] = merged
        settled += 1

    return {"targets": len(targets), "settled": settled, "skipped": skipped,
            "horizons": list(horizons), "codes_needed": len(codes_needed)}

def measurement_summary(horizons=(1, 3, 5)) -> Dict:
    """汇总闭环测量：各 horizon 收益分布 + 单位时间收益。

    单位时间收益 = 已结算 pick 的合计收益(bp) ÷ 全部 run 累计耗时(秒) × 60。
    分母含未结算 run 的耗时（时间照花），口径在返回中显式标注。
    """
    horizons = tuple(int(h) for h in (horizons or _DEFAULT_HORIZONS))
    picks = _read_jsonl(_PICKS_PATH)
    settlements = _read_jsonl(_SETTLEMENTS_PATH)

    latest: Dict[Tuple[str, str], Dict] = {}
    for ev in settlements:
        key = (str(ev.get("run_id") or ""), str(ev.get("code") or ""))
        if key[0] and key[1]:
            latest[key] = ev.get("horizons") or {}

    n_runs = len(picks)
    total_elapsed_s = sum(float(r.get("elapsed_s") or 0.0) for r in picks)
    total_picks = sum(len(r.get("picks") or []) for r in picks)

    summary = {
        "n_runs": n_runs,
        "total_elapsed_s": round(total_elapsed_s, 3),
        "total_elapsed_min": round(total_elapsed_s / 60.0, 3),
        "total_picks": total_picks,
        "settled_picks": len(latest),
        "horizons": list(horizons),
        "unit_time_basis": "total_ret_bp / total_elapsed_s * 60（分母含全部 run 耗时）",
        "metrics": {},
    }

    for h in horizons:
        rets = []
        for ev in latest.values():
            v = ev.get(str(h)) or {}
            if v.get("ret_pct") is not None:
                rets.append(float(v["ret_pct"]))
        total_bp = sum(rets) * 100.0
        if rets:
            n = len(rets)
            wins = sum(1 for r in rets if r > 0)
            avg = sum(rets) / n
            unit_bp_per_min = (total_bp / total_elapsed_s * 60.0
                               if total_elapsed_s > 0 else 0.0)
            summary["metrics"][str(h)] = {
                "settled": n,
                "win_rate_pct": round(wins / n * 100, 2),
                "avg_ret_pct": round(avg, 4),
                "total_ret_pct": round(sum(rets), 4),
                "total_ret_bp": round(total_bp, 2),
                "return_per_run_bp": round(total_bp / n_runs, 2) if n_runs else 0.0,
                "unit_time_bp_per_min": round(unit_bp_per_min, 4),
                "unit_time_bp_per_s": (round(total_bp / total_elapsed_s, 4)
                                       if total_elapsed_s > 0 else 0.0),
            }
        else:
            summary["metrics"][str(h)] = {
                "settled": 0, "win_rate_pct": 0.0, "avg_ret_pct": 0.0,
                "total_ret_pct": 0.0, "total_ret_bp": 0.0,
                "return_per_run_bp": 0.0, "unit_time_bp_per_min": 0.0,
                "unit_time_bp_per_s": 0.0,
            }
    return summary





