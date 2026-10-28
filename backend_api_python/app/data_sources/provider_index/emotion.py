# -*- coding: utf-8 -*-
"""provider_index / emotion —— 指数族情绪周期源（emotion_cycle）。

接口: https://www.stockapi.com.cn/v1/base/emotionalCycle
响应: {"code": 20000, "data": {"colNameList": [...], "contentList": [[...], ...]}}
  colNameList 是列名（随上游改版变化，**必须**按列名映射，不能按位置硬取），
  contentList 是**按时间升序**的整表（最后一行 = 最新）。

本层只负责"取表 + 按列名转 records"，快照落盘/去重/历史查询留在
`market_cn/emotion.py`（那是缓存与业务语义，不是取数）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.utils.logger import get_logger

logger = get_logger(__name__)

_URL = "https://www.stockapi.com.cn/v1/base/emotionalCycle"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
}


def fetch_emotion_cycle() -> Dict[str, Any]:
    """情绪周期整表（按时间升序）。

    返回 {"rows": [{列名: 值, ...}, ...], "total": 行数}；
    上游 code != 20000 或表为空 ⇒ raise（单源 kind，让 dispatch 如实报失败，
    不伪造空表 —— 空表会被当成"今天没有情绪数据"误导下游）。
    """
    import requests

    try:
        resp = requests.get(_URL, headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        logger.warning("[emotion] 情绪数据拉取失败: %s", e)
        raise

    if payload.get("code") != 20000:
        raise RuntimeError("情绪接口返回 code=%r msg=%r"
                           % (payload.get("code"), payload.get("msg")))

    node = payload.get("data") or {}
    cols: List[Any] = node.get("colNameList") or []
    content: List[Any] = node.get("contentList") or []
    if not cols or not content:
        raise RuntimeError("情绪接口返回空表")

    rows = [dict(zip(cols, item)) for item in content if isinstance(item, list)]
    return {"rows": rows, "total": len(rows)}


from app.data_sources.provider_index import register  # noqa: E402


@register
class EmotionIndexProvider:
    name = "stockapi_emotion"
    priority = 60
    markets = {"CNStock"}
    capabilities = {
        "emotion_cycle": fetch_emotion_cycle,
    }

    def fetch(self, kind: str, **params) -> Dict[str, Any]:
        fn = self.capabilities.get(kind)
        if fn is None:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        return fn(**params)
