# -*- coding: utf-8 -*-
"""
东财数据中心共享工具。

提供 _em_datacenter() 统一查询接口，供 signal_tools / capital_tools 等复用。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_EM_MIN_INTERVAL = float(os.getenv("EM_MIN_INTERVAL", "1.0"))
_last_call: float = 0.0


def em_datacenter(report_name: str, columns: str = "ALL",
                  filter_str: str = "", page_size: int = 50,
                  sort_columns: str = "", sort_types: str = "-1") -> list:
    """东财数据中心统一查询（带限流）。

    Args:
        report_name: 报表名，如 RPT_LIFT_STAGE
        columns: 列名，ALL 或逗号分隔
        filter_str: 过滤条件
        page_size: 每页条数
        sort_columns: 排序列
        sort_types: 排序方向 (-1 降序)
    """
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < _EM_MIN_INTERVAL:
        time.sleep(_EM_MIN_INTERVAL - elapsed)

    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    try:
        r = requests.get(_DATACENTER_URL, params=params,
                         headers={"User-Agent": _UA}, timeout=15)
        _last_call = time.time()
        d = r.json()
        if d.get("result") and d["result"].get("data"):
            return d["result"]["data"]
        return []
    except Exception as e:
        logger.warning("em_datacenter(%s) failed: %s", report_name, e)
        return []
