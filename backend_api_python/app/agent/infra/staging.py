# -*- coding: utf-8 -*-
"""跨阶段会话级变量存储（2026-09-15 两级统一后的唯一用途）。

背景：沙箱是单进程内的多次 executor 实例（每个阶段/批次新建），Python 变量无法靠
smolagents 原生 state 跨实例存活。本模块在进程内用 `_OBJ`（scope -> {name: 原对象}）
承接被促升的会话级变量，供新建 executor 时投影回其 state，实现跨阶段续承。

机制（与 GuidedCPythonExecutor._promote_model_vars、task_agent 投影段配合）：
  · 写入：阶段结果注册 / 模型变量促升时调 stage_put_obj，存**原对象**（跳过序列化往返）
  · 读出：新建 executor 前用 stage_scope_vars(scope) 取出全部变量，send_variables 装回 state
  · 清理：一次 run 结束由 finalize 调 stage_clear(scope)，防长跑内存增长

为什么不像早期版本那样暴露 stage_write / stage_read 这类读写 API：
  两级已统一为"executor.state 里的 Python 变量"——模型用赋值即续承，无需读写调用，
  也不丢 Python 类型；早期的字符串/文件读写暂存 API 已被变量形态取代并彻底移除。
"""
from __future__ import annotations

import os
import re
import threading
import time

from app.utils.logger import get_logger

logger = get_logger(__name__)

# scope 限 ASCII 白名单（须在单次 run 内保持稳定，由 nodes 用 start_time 派生）
_SCOPE_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}")

# ── 活对象区（跨阶段变量存储） ──
# 存**原对象**而非字符串：跳 str(repr)→json.loads 往返，避免连撞 TypeError/IndexError。
# 内存常驻，靠 stage_clear(scope)（run 结束）、单 scope 上限、全局总上限三重防泄漏。
_OBJ: dict = {}                       # scope -> {name: obj}
_OBJ_LOCK = threading.Lock()
_MISSING = object()                   # stage_get_obj 未命中哨兵
_OBJ_MAX_PER_SCOPE = int(os.getenv("STAGING_OBJ_MAX_PER_SCOPE", "200"))
_OBJ_MAX_TOTAL = int(os.getenv("STAGING_OBJ_MAX_TOTAL", "1000"))

# ── TTL 过期清理（防中途退出泄漏） ──
_SCOPE_TS: dict = {}                  # scope -> last_write_timestamp
_SCOPE_TTL = int(os.getenv("STAGING_SCOPE_TTL", "3600"))  # 默认 1 小时


def _obj_total_locked() -> int:
    return sum(len(d) for d in _OBJ.values())


def _evict_obj_locked() -> None:
    """单 scope 超上限则淘汰最旧；全局超上限则整 scope 淘汰最旧。防止长跑内存无限增长。"""
    for d in list(_OBJ.values()):
        while len(d) > _OBJ_MAX_PER_SCOPE:
            d.pop(next(iter(d)), None)
    while _obj_total_locked() > _OBJ_MAX_TOTAL and _OBJ:
        _OBJ.pop(next(iter(_OBJ)), None)


def _expire_stale_scopes() -> None:
    """清理超过 TTL 未写入的 scope（防中途退出泄漏）。"""
    now = time.time()
    expired = [s for s, ts in _SCOPE_TS.items() if now - ts > _SCOPE_TTL]
    for s in expired:
        _OBJ.pop(s, None)
        _SCOPE_TS.pop(s, None)
    if expired:
        logger.info("[Staging] 过期清理 %d 个 scope", len(expired))


def _safe_name(name: str) -> str:
    """dict 键：任意字符串均可，仅做非空 + 长度限制（全局变量区本就不需要文件名白名单）。"""
    s = name if isinstance(name, str) else str(name)
    if not s:
        raise ValueError("name 不能为空")
    if len(s) > 120:
        s = s[:120]
    return s


def stage_put_obj(scope: str, name: str, obj) -> bool:
    """存**活对象**（跨阶段变量续承用）：跳过序列化往返，下阶段经 stage_get_obj / 投影直接拿回原对象。
    返回是否成功；依赖 stage_clear(scope) 或上限淘汰防泄漏。"""
    if not _SCOPE_RE.fullmatch(scope or ""):
        return False
    fname = _safe_name(name)
    try:
        with _OBJ_LOCK:
            _OBJ.setdefault(scope, {})[fname] = obj
            _SCOPE_TS[scope] = time.time()
            _expire_stale_scopes()
            _evict_obj_locked()
        return True
    except Exception:
        return False


def stage_get_obj(scope: str, name: str):
    """取回 stage_put_obj 存入的活对象（非字符串，无需 json.loads）。
    返回对象本身；不存在/非法时返回 {"ok": False, "error": ...} 便于判错恢复。"""
    try:
        if not _SCOPE_RE.fullmatch(scope or ""):
            return {"ok": False, "error": f"scope 非法: {scope!r}"}
        fname = _safe_name(name)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    with _OBJ_LOCK:
        d = _OBJ.get(scope)
        if d is None:
            return {"ok": False, "error": f"scope 不存在: {scope}"}
        obj = d.get(fname, _MISSING)
    if obj is _MISSING:
        return {"ok": False, "error": f"对象不存在: {name}", "可用": sorted(d)}
    return obj


def stage_scope_vars(scope: str) -> dict:
    """返回某 scope 的全部会话级变量（**浅拷贝**），用于把它投影进新建 executor 的 state。
    `_OBJ` 承担**会话级**那份（跨阶段存活）；本函数只是把它读出来的入口。"""
    if not scope or not _SCOPE_RE.fullmatch(scope):
        return {}
    try:
        with _OBJ_LOCK:
            return dict(_OBJ.get(scope) or {})
    except Exception:
        return {}


def stage_clear(scope: str) -> None:
    """清空某 scope 的会话级变量存储，用于一次 run 结束防泄漏。"""
    with _OBJ_LOCK:
        _OBJ.pop(scope, None)
        _SCOPE_TS.pop(scope, None)
