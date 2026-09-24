# -*- coding: utf-8 -*-
"""数据自检（提智方案 二波 B3）——validate_df + SelfCheckError。

设计（2026-09-24）：
  · **异常式为主**：硬失败抛 `SelfCheckError(code, detail)`，由执行器捕获并路由
    （可见、可归因、可统计），而不是静默返回 False 让模型忽略。
  · **软/硬路由**：
      软失败（`soft=True`）→ 只登记检查结果（level="soft"），不抛；由调用方把缺口
        写入 missing_data 并降置信度——"缺口显式声明比硬凑数字值钱"。
      硬失败（`soft=False`）→ 抛 SelfCheckError，不许绕过（换口径重算）。
  · **统计面并入 _qd_stats**：本模块与 failure_memory 一样用 drain 模式，
    执行器每步取回写进 `_qd_stats.data_checks`（给 verify / 评测 / 计数器用，零重复采集）。
  · **sanity 参数从工具域元数据取，不硬编码**（项目红线）：调用方传 `sanity={列: (lo, hi)}`。

红线：本模块是数据自检的**单一事实源**；调用方不得各自实现阈值/规则。
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence

__all__ = ["SelfCheckError", "validate_df", "drain_checks", "CHECK_CODES"]

# 定死检查码（评测 L3 / 面板按此统计；新增须评审）
CHECK_CODES = (
    "null_frame", "empty_frame", "missing_cols", "non_numeric",
    "out_of_range", "nan_ratio", "shape_mismatch", "type_mismatch",
)

_MAX_LOG = 50
_CHECK_LOG: List[Dict[str, Any]] = []


class SelfCheckError(Exception):
    """数据自检硬失败。code ∈ CHECK_CODES；detail 为人类可读说明。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = str(code or "unknown")
        self.detail = str(detail or "")
        super().__init__(f"SelfCheckError: code={self.code} detail={self.detail}")


def _log(code: str, detail: str, level: str) -> None:
    _CHECK_LOG.append({"code": code, "detail": str(detail)[:120], "level": level})
    if len(_CHECK_LOG) > _MAX_LOG:
        del _CHECK_LOG[:-_MAX_LOG]


def drain_checks() -> List[Dict[str, Any]]:
    """取回并清空本 step 的数据自检记录（供执行器写入 _qd_stats）。"""
    out = list(_CHECK_LOG)
    del _CHECK_LOG[:]
    return out


def _len_of(df: Any) -> Optional[int]:
    try:
        return len(df)
    except Exception:
        return None


def _has_cols(df: Any, cols: Sequence[str]) -> List[str]:
    """返回 df 中缺失的列名。"""
    try:
        if hasattr(df, "columns"):
            have = {str(c) for c in list(df.columns)}
        elif isinstance(df, dict):
            have = {str(k) for k in df.keys()}
        else:
            return list(cols)
        return [c for c in cols if str(c) not in have]
    except Exception:
        return list(cols)


def _col_values(df: Any, col: str) -> Optional[Iterable]:
    try:
        if hasattr(df, "columns") and col in list(df.columns):
            return list(df[col])
        if isinstance(df, dict) and col in df:
            v = df[col]
            return list(v) if hasattr(v, "__iter__") and not isinstance(v, (str, bytes)) else [v]
        if isinstance(df, (list, tuple)):
            return [row.get(col) if isinstance(row, dict) else None for row in df]
    except Exception:
        return None
    return None


def validate_df(
    df: Any,
    *,
    kind: str = "frame",
    required_cols: Optional[Sequence[str]] = None,
    allow_empty: bool = False,
    numeric_cols: Optional[Sequence[str]] = None,
    sanity: Optional[Dict[str, tuple]] = None,
    nan_ratio_max: float = 0.5,
    soft: bool = False,
) -> Any:
    """校验数据表/映射；返回原对象（便于链式 `data = validate_df(data, ...)`）。

    失败处置：
      · soft=False（默认，硬失败）→ 登记 + 抛 SelfCheckError（不许绕过）；
      · soft=True（软失败）→ 只登记（level="soft"），由调用方记 missing_data 并降置信度。
    """
    def _fail(code: str, detail: str) -> Any:
        _log(code, detail, "soft" if soft else "hard")
        if soft:
            return df
        raise SelfCheckError(code, detail)

    if df is None:
        return _fail("null_frame", f"{kind} 为 None")

    n = _len_of(df)
    if n is None:
        return _fail("type_mismatch", f"{kind} 非表结构（{type(df).__name__}）")
    if n == 0 and not allow_empty:
        return _fail("empty_frame", f"{kind} 为空（0 行）")

    if required_cols:
        miss = _has_cols(df, required_cols)
        if miss:
            return _fail("missing_cols", f"{kind} 缺列: {','.join(str(m) for m in miss)}")

    if numeric_cols:
        for c in numeric_cols:
            vals = _col_values(df, c)
            if vals is None:
                continue
            for v in vals:
                if v is None:
                    continue
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    return _fail("non_numeric", f"{kind}.{c} 含非数值: {type(v).__name__}")

    if sanity:
        for col, lim in sanity.items():
            vals = _col_values(df, col)
            if vals is None:
                continue
            try:
                lo, hi = lim
            except Exception:
                continue
            for v in vals:
                if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                if math.isnan(v) if isinstance(v, float) else False:
                    continue
                if lo is not None and v < lo or hi is not None and v > hi:
                    return _fail("out_of_range", f"{kind}.{col}={v} 越界[{lo},{hi}]")

    # NaN 比例（仅 pandas 风格）
    try:
        import pandas as _pd  # type: ignore
        if isinstance(df, _pd.DataFrame) and len(df) > 0:
            ratio = float(df.isna().sum().sum()) / float(max(df.size, 1))
            if ratio > nan_ratio_max:
                return _fail("nan_ratio", f"{kind} NaN 占比 {ratio:.2f}>{nan_ratio_max}")
    except ImportError:
        pass
    except Exception:
        pass

    _log("ok", f"{kind} 通过（{n} 行）", "ok")
    return df
