# -*- coding: utf-8 -*-
"""
情绪周期 — 交易日情绪快照采集 + 历史查询。

数据源: stockapi.com.cn/v1/base/emotionalCycle
缓存策略: 单文件存储，保留最近 30 天，自动清理过期数据。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_SOURCE_URL = "https://www.stockapi.com.cn/v1/base/emotionalCycle"
_CACHE_FILE = os.path.join(os.getcwd(), "data", "market_cn_cache", "emotion.json")
_MAX_DAYS = 30

_snapshots: Dict[str, List[Dict[str, Any]]] = {}  # {"2026-06-07": [...], ...}
_lock = threading.Lock()
_loaded = False


def _load():
    global _snapshots, _loaded
    if _loaded:
        return
    if os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                _snapshots = json.load(f)
        except Exception as e:
            logger.warning("加载情绪缓存失败: %s", e)
            _snapshots = {}
    _loaded = True


def _save():
    os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
    tmp = f"{_CACHE_FILE}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_snapshots, f, ensure_ascii=False)
        os.replace(tmp, _CACHE_FILE)
    except Exception as e:
        logger.warning("写情绪缓存失败: %s", e)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _clean_old():
    """删除超过 30 天的日期。"""
    cutoff = (datetime.now() - timedelta(days=_MAX_DAYS)).strftime("%Y-%m-%d")
    keys_to_del = [k for k in _snapshots if k < cutoff]
    for k in keys_to_del:
        del _snapshots[k]


# ══════════════════════════════════════════════════════════════
#  对外接口
# ══════════════════════════════════════════════════════════════

def fetch_emotion_cycle(force: bool = False) -> Dict[str, Any]:
    """拉取最新情绪数据 + 追加快照到当天缓存。"""
    if not force and _rt_emotion_cycle is not None:  # 内存缓存
        return _rt_emotion_cycle
    try:                                      # ② 远端 fallback
        r = requests.get(_SOURCE_URL, headers={"User-Agent": _UA}, timeout=10)
        d = r.json()
        if d.get("code") != 20000:
            return {"code": 0, "msg": d.get("msg", "接口错误"), "data": {}}

        cols = d["data"]["colNameList"]
        rows = d["data"]["contentList"]
        latest = rows[-1] if rows else []
        row_dict = dict(zip(cols, latest)) if cols and latest else {}

        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        snapshot = {
            "ts": now.strftime("%H:%M:%S"),
            "ts_epoch": int(now.timestamp()),
            **row_dict,
        }

        with _lock:
            _load()
            day_list = _snapshots.setdefault(today, [])
            # 同一分钟内去重
            if day_list and day_list[-1].get("ts", "")[:5] == now.strftime("%H:%M"):
                day_list[-1] = snapshot
            else:
                day_list.append(snapshot)
            _clean_old()
            _save()

        return {
            "code": 1,
            "msg": "success",
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "data": {
                "emotion": row_dict,
                "history_days": len(rows),
            },
            "snapshots": list(day_list),
        }
    except Exception as e:
        logger.error("情绪数据拉取失败: %s", e)
        return {"code": 0, "msg": str(e), "data": {}}


def get_emotion_history(days: int = 1, hours: Optional[int] = None) -> Dict[str, Any]:
    """获取情绪快照历史。

    Args:
        days: 返回最近 N 天的数据，默认 1（当天）。
        hours: 在指定天数内，只返回最近 N 小时的快照。None 返回全天。

    Returns:
        {"code": 1, "count": N, "history": [...]}
    """
    with _lock:
        _load()
        cutoff_date = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        history = []
        for date_str in sorted(_snapshots.keys()):
            if date_str >= cutoff_date:
                history.extend(_snapshots[date_str])

    if hours is not None:
        cutoff_ts = int((datetime.now() - timedelta(hours=hours)).timestamp())
        history = [s for s in history if s.get("ts_epoch", 0) >= cutoff_ts]

    return {"code": 1, "count": len(history), "history": history}


def get_emotion_latest() -> Dict[str, Any]:
    """获取当天最新一条情绪快照。"""
    if _rt_emotion_cycle is not None:        # ① 内存缓存
        return {"code": 1, "emotion": _rt_emotion_cycle, "count": 1}
    today = datetime.now().strftime("%Y-%m-%d")  # ② 原有逻辑
    with _lock:
        _load()
        day_list = _snapshots.get(today, [])
        if day_list:
            return {"code": 1, "emotion": day_list[-1], "count": len(day_list)}
    return {"code": 0, "msg": "暂无数据", "emotion": {}, "count": 0}


# ═══ 内存缓存 + refresh（scheduler 调用）═══

_rt_emotion_cycle = None

def refresh_emotion_cycle():
    global _rt_emotion_cycle
    try:
        _rt_emotion_cycle = fetch_emotion_cycle(force=True)
    except Exception as e:
        logger.warning("[refresh] refresh_emotion_cycle 失败: %s", e)

