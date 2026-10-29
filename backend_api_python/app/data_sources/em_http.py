# -*- coding: utf-8 -*-
"""东财 push2 通用 GET（五族中立层，不属于任何 provider 族）。

东财 CDN 封杀 urllib/requests 的 JA3 指纹 ⇒ 必须 curl_cffi edge101，Referer 带
quote.eastmoney.com；连续请求会触发 RST 突发窗（分钟级），靠单 host 退避重试跨窗。
"""
from __future__ import annotations

import time
from typing import Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


def em_get(url: str, timeout: float = 15, retries: int = 3) -> Optional[str]:
    """返回响应文本，失败 None（由调用方翻译成各自的失败语义）。"""
    try:
        from curl_cffi import requests as _cr
    except ImportError:
        logger.debug("[em_http] curl_cffi 未安装 (pip install curl_cffi)")
        return None

    headers = {"Referer": "https://quote.eastmoney.com/"}
    last_err = None
    for i in range(retries):
        try:
            s = _cr.Session(impersonate="edge101")
            r = s.get(url, timeout=timeout, headers=headers)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            if i < retries - 1:
                time.sleep(3.0 * (i + 1))

    err_str = str(last_err)[:120]
    if "Connection closed" in err_str or "curl: (56)" in err_str or "RST" in err_str:
        logger.debug("[em_http] em_get 重试 %d 次失败(限频窗口): %s", retries, err_str)
    else:
        logger.warning("[em_http] em_get 重试 %d 次失败: %s", retries, err_str)
    return None
