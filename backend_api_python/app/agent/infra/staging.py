# -*- coding: utf-8 -*-
"""阶段中转数据暂存区（2026-09-12）。

背景：执行沙箱不支持 import os / 直接写文件，跨阶段重数据只能靠 ≤2000 字摘要，
明细在阶段间丢失（实测：深入分析阶段的全量结果无法完整传给成稿阶段）。
本模块提供受控暂存区：按 run（scope）分区，phase 间中转大块数据。

安全边界：
  - 目录限定 <项目>/tmp/agent_staging/<scope>/（scope 由任务书下发并做白名单清洗）
  - name 任意描述性字符串（中文亦可，仅限长度）；scope 限 ASCII 白名单；内容 ≤ 2MB（仅字符串路径）
  - 无任意路径访问；scope/name 非法直接拒绝
易错点：
  - scope 必须在一次 run 内保持稳定（chat_node 用 session_id+start_time 派生），
    否则跨阶段读不到
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger(__name__)

# <项目>/tmp/agent_staging/（tools/staging.py → parents[3] = backend_api_python）
_ROOT = Path(__file__).resolve().parents[3] / "tmp" / "agent_staging"

# 注：文件名白名单已废弃（全局变量区用任意字符串键；仅磁盘模式经 _disk_name 做 sanitize）
_SCOPE_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}")
_MAX_CHARS = 2 * 1024 * 1024  # 2MB

# ═══════════════════════════════════════════════════════════════
#  存储后端：内存（默认） / 磁盘（STAGING_BACKEND=disk）
# ═══════════════════════════════════════════════════════════════
# 2026-09-14：用户指出——落盘的代价是**磁盘 I/O，不是 token**（模型可在同一代码块内
# 完成"调用工具 → 读回 → 处理"，不额外消耗 LLM 轮次）。既然如此，默认后端改为内存：
# 零 I/O、更快，且 run 结束随 scope 淘汰自动清空，不留垃圾文件。
#
# 前提：单进程。gunicorn_config.py 默认 GUNICORN_WORKERS=1 + gthread，
# 且注释已声明"agent 子系统多处为进程内单例，扩 worker 前须先做进程安全改造"。
# 多线程故需加锁；需要跨进程/持久化时用 STAGING_BACKEND=disk（行为与旧版一致）。
_MEM: dict = {}                       # scope -> {name: content}
_MEM_LOCK = threading.Lock()
_MEM_MAX_SCOPES = int(os.getenv("STAGING_MAX_SCOPES", "50"))

# ── 活对象区（2026-09-14）：自动暂存/F3 用，存**原对象**而非字符串 ──
# 与 _MEM（stage_write 字符串路径）分离、互不干扰。agent 经 stage_get_obj 直接拿回
# 活对象，跳过 str(repr)→json.loads 往返（此前连撞 TypeError/IndexError/AttributeError 的根）。
# 内存常驻，靠 stage_clear(scope)（run 结束）、单 scope 上限、全局总上限三重防泄漏。
_OBJ: dict = {}                       # scope -> {name: obj}
_OBJ_LOCK = threading.Lock()
_MISSING = object()                   # stage_get_obj 未命中哨兵
_OBJ_MAX_PER_SCOPE = int(os.getenv("STAGING_OBJ_MAX_PER_SCOPE", "200"))
_OBJ_MAX_TOTAL = int(os.getenv("STAGING_OBJ_MAX_TOTAL", "1000"))


def _obj_total_locked() -> int:
    return sum(len(d) for d in _OBJ.values())


def _evict_obj_locked() -> None:
    """单 scope 超上限则淘汰最旧；全局超上限则整 scope 淘汰最旧。防止长跑内存无限增长。"""
    for d in list(_OBJ.values()):
        while len(d) > _OBJ_MAX_PER_SCOPE:
            d.pop(next(iter(d)), None)
    while _obj_total_locked() > _OBJ_MAX_TOTAL and _OBJ:
        _OBJ.pop(next(iter(_OBJ)), None)


def _use_memory() -> bool:
    return os.getenv("STAGING_BACKEND", "memory").lower() != "disk"


def _evict_locked() -> None:
    """淘汰最旧的 scope（dict 保序），避免长跑进程内存无限增长。"""
    while len(_MEM) > _MEM_MAX_SCOPES:
        _MEM.pop(next(iter(_MEM)), None)


def _dir(scope: str) -> Path:
    if not _SCOPE_RE.fullmatch(scope or ""):
        raise ValueError(f"scope 非法: {scope!r}")
    return _ROOT / scope


def _safe_name(name: str) -> str:
    """内存键（_MEM/_OBJ 的 dict 键）：任意字符串均可，仅做非空+长度限制。
    文件名白名单 [A-Za-z0-9_.-] 是落盘时代的残留——全局变量区本就不需要它。"""
    s = name if isinstance(name, str) else str(name)
    if not s:
        raise ValueError("name 不能为空")
    if len(s) > 120:
        s = s[:120]
    return s


# 磁盘文件名（仅 STAGING_BACKEND=disk 用）：把文件系统不安全字符统一替换，保证 write/read 往返一致
_FS_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

def _disk_name(name: str) -> str:
    s = name if isinstance(name, str) else str(name)
    s = _FS_UNSAFE.sub("_", s).strip(". ").replace("..", "_")
    if not s:
        s = "unnamed"
    if len(s) > 120:
        s = s[:120]
    return s


def stage_write(scope: str, name: str, content) -> dict:
    """写入阶段中转数据（全局变量模式：非字符串内容直接存活对象，stage_read 直接取回、无需解析）。

    name 任意描述性字符串（中文亦可），仅限长度；字符串内容走文本路径（≤2MB）。
    """
    d = _dir(scope)                       # scope 校验（ASCII 白名单）
    mem_key = _safe_name(name)           # 现已放宽：任意字符串
    try:
        # 活对象：直接存内存，不受 2MB 文本限制（全局变量区，靠 caps 防泄漏）
        if not isinstance(content, str):
            try:
                with _OBJ_LOCK:
                    _OBJ.setdefault(scope, {})[mem_key] = content
                    _evict_obj_locked()
            except Exception:
                pass
            logger.info("[staging] write obj %s/%s", scope, mem_key)
            return {"ok": True, "name": mem_key, "chars": len(str(content))}
        # 字符串：走文本路径（内存/磁盘），受 2MB 限制
        text = content or ""
        if len(text) > _MAX_CHARS:
            return {"ok": False, "error": f"内容超限（{len(text)} > {_MAX_CHARS} 字符），请先提炼"}
        if _use_memory():
            with _MEM_LOCK:
                _MEM.setdefault(scope, {})[mem_key] = text
                _evict_locked()
        else:
            fname = _disk_name(name)
            d.mkdir(parents=True, exist_ok=True)
            (d / fname).write_text(text, encoding="utf-8")
        logger.info("[staging] write %s/%s (%d chars)", scope, mem_key, len(text))
        return {"ok": True, "name": mem_key, "chars": len(text)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def stage_read(scope: str, name: str):
    """读取阶段中转数据（全局变量模式：命中活对象直接返回原对象；否则返回文本，>60000 截断）。"""
    try:
        mem_key = _safe_name(name)
        # 优先返回活对象（全局变量区）；不在则回退下方字符串路径
        with _OBJ_LOCK:
            _od = _OBJ.get(scope)
            _obj = _od.get(mem_key) if _od is not None else None
        if _obj is not None:
            return _obj
        d = _dir(scope)
        if _use_memory():
            text = _MEM.get(scope, {}).get(mem_key)
            if text is None:
                avail = sorted(_MEM.get(scope, {}))
                return json.dumps({"ok": False, "error": f"数据不存在: {name}", "可用": avail},
                                  ensure_ascii=False)
        else:
            f = d / _disk_name(name)
            if not f.is_file():
                avail = [x.name for x in d.glob("*") if x.is_file()] if d.is_dir() else []
                return json.dumps({"ok": False, "error": f"数据不存在: {name}", "可用": avail},
                                  ensure_ascii=False)
            text = f.read_text(encoding="utf-8", errors="replace")
        if len(text) > 60000:
            text = text[:60000] + f"\n…(截断，共 {len(text)} 字符)"
        return text
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)[:200]}, ensure_ascii=False)


def stage_list(scope: str) -> list:
    """列出暂存区现有文件（name/chars/mtime）。"""
    try:
        d = _dir(scope)
        if _use_memory():
            return [{"name": n, "chars": len(c), "mtime": "-"}
                    for n, c in sorted(_MEM.get(scope, {}).items())]
        if not d.is_dir():
            return []
        out = []
        for x in sorted(d.glob("*")):
            if x.is_file():
                out.append({"name": x.name, "chars": x.stat().st_size,
                            "mtime": time.strftime("%H:%M:%S", time.localtime(x.stat().st_mtime))})
        return out
    except Exception as e:
        return [{"error": str(e)[:200]}]


# ═══════════════════════════════════════════════════════════════
#  活对象存取（2026-09-14）：自动暂存用，存原对象、取回即对象
# ═══════════════════════════════════════════════════════════════
def stage_put_obj(scope: str, name: str, obj) -> bool:
    """存**活对象**（自动暂存/F3 用）：跳过 str(repr)→json.loads 往返，agent 经
    stage_get_obj 直接拿回原对象。与 stage_write(字符串) 分离，互不干扰。
    内存常驻，依赖 stage_clear(scope) 或上限淘汰防泄漏。"""
    if not _SCOPE_RE.fullmatch(scope or ""):
        return False
    fname = _safe_name(name)
    try:
        with _OBJ_LOCK:
            _OBJ.setdefault(scope, {})[fname] = obj
            _evict_obj_locked()
        return True
    except Exception:
        return False


def stage_get_obj(scope: str, name: str):
    """取回 stage_put_obj 存入的活对象（非字符串，无需 json.loads）。
    返回对象本身；不存在/非法时返回 {"ok": False, "error": ...} 便于 agent 判错恢复。"""
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


def stage_clear(scope: str) -> None:
    """清空某 scope 的暂存（字符串区 _MEM + 活对象区 _OBJ），用于一次 run 结束防泄漏。"""
    with _MEM_LOCK, _OBJ_LOCK:
        _MEM.pop(scope, None)
        _OBJ.pop(scope, None)
