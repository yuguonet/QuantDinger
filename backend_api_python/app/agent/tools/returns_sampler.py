# -*- coding: utf-8 -*-
"""returns_sampler.py — 工具返回结构**采样器**（生产版，2026-09-20）

用途（方案 D）：
  启动时对**只读**工具用样本参数真实调用一次，把观测到的返回结构固化成 JSON 缓存；
  运行期按当前阶段白名单把缓存渲染成「返回结构速查」注入 system_prompt，让模型
  取数后直接按键访问，而不是「取数 → print → 看类型 → 下一步再引用」的 REPL 式试探。

  与 `tools/returns_contract.py` 的分工：
    · returns_contract.py —— **读 docstring 的 `Returns:` 段**（静态、零成本、覆盖率 ~64%）；
    · returns_sampler.py  —— **采样真实返回**（启动跑一次、覆盖率 ~91%、结构更准）。
    两者**同形**（都产出一行/工具的「返回结构」），读取侧按「采样优先、docstring 兜底」
    合并，见 `_build_return_contract_block(..., sampled=...)`。

接入点（两处，见 nodes.py / task_agent.py 注释）：
  · 写侧：`nodes.py::Context.init_tools()` 末尾 → `start_background_sampling(provider)`
    （守护线程后台跑，进程级一次；不阻塞启动）。
  · 读侧：`agents/task_agent.py::_sandbox_instructions()` →
    `get_sampled_structures(names)` 作为 `sampled=` 传进契约渲染。

安全性（绝不触碰写操作）：
  · 复用 `capabilities/scanner.WRITE_PREFIXES` 单一事实源，命中前缀即跳过；
  · 参数不可合成（需真实 DB id / 自然语言过滤器等）→ 跳过；
  · 单工具硬超时 → 跳过（只读调用无副作用，超时线程自然结束）；
  · 任何异常都被吞掉并降级为「不注入」，绝不影响主流程。

增量策略（工具没变就不重扫）：
  每个工具记录其所在**模块源文件的 stat 指纹**（mtime_ns:size）。重扫时
  指纹未变且工具仍在 → 复用旧条目（零成本）；指纹变了/新增 → 重采；已消失 → 删。

易错点（改本文件前先读）：
  1. 本模块放在 `tools/` 根目录，会被 `ToolProvider.scan_directory` **import**
     （不是注册）。故**所有本模块自身函数一律下划线开头**——公开命名会被
     `_register_module_functions` 注册成模型可调工具（`returns_contract.py` 已踩过此坑）。
  2. 采样是「真实调用外部接口」，会暴露数据源真实状况（东财反爬熔断、DB 未配密码等）。
     这些是**环境日志噪音**，不是本模块缺陷；不要试图在采样器里"修"数据源。
  3. 并发越高覆盖率反而可能**略降**（并发放大对单一数据源的压力 → 触发反爬/超时）。
     默认 12 是实测平台期附近的折中；`RETURNS_SAMPLE_WORKERS` 可覆盖（0/1=串行）。
  4. 读取侧 `get_sampled_structures()` 必须**内存缓存**（按文件 mtime 失效）：
     它处在「每步重发」的渲染路径上，绝不能每步读盘。
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FTimeout, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping

logger = logging.getLogger(__name__)

# ── 路径 ───────────────────────────────────────────────────────
# 本文件：<backend_api_python>/app/agent/tools/returns_sampler.py → parents[3] = backend_api_python
_BACKEND = Path(__file__).resolve().parents[3]
_CACHE_PATH = _BACKEND / "tmp" / "qclaw" / "tool_returns_cache.json"

# ── 参数合成 / 安全 ────────────────────────────────────────────
# 单工具硬超时（秒）。2026-09-20 由 8s 提到 30s：8s 会把大盘概览/个股信息/资金摘要等
# 核心工具误杀成 timeout（采样侧压力），30s 足以让真实接口跑完。可用环境变量覆盖。
TIMEOUT_S = int(os.getenv("RETURNS_SAMPLE_TIMEOUT_S", "30") or "30")
MAX_DEPTH = 3          # 采样时推断 schema 的最大嵌套深度
MAX_KEYS = 24          # 每个对象最多记录多少键
CACHE_VERSION = 1

# 并行度（启动建缓存时多线程）。0/1 = 串行退路。
# 2026-09-20 定档 12：再高会放大反爬/DB 压力致覆盖率下降，20 已是平台期。
SAMPLE_WORKERS = int(os.getenv("RETURNS_SAMPLE_WORKERS", "12") or "12")

# 样本参数（按参数名套用；不在表中的必需参数 → 判为不可采样，跳过）
_NAME_SAMPLES: Dict[str, Any] = {
    "codes": "000001", "code": "000001", "stock_code": "000001", "symbol": "000001",
    "keyword": "平安银行", "query": "平安银行", "board_name": "银行",
    "start_date": "2026-01-01", "end_date": "2026-09-18",
}
# 需真实业务 id / 自然语言过滤器，无法合成 → 直接跳过
_UNSAMPLEABLE = {"strategy_id", "indicator_id", "filters"}
# provider 扫描时排除的入口名（与 tools/base._ENTRY_NAME_DENY 同义，双保险）
_ENTRY_DENY = {"main", "cli", "serve", "run_server", "server", "app"}


def _write_prefixes() -> tuple:
    """写操作前缀（单一事实源 = capabilities/scanner.WRITE_PREFIXES）。

    取不到时返回 ()**并置 `_SAFE_OK=False`** ⇒ 采样整体停用（安全优先：无法证明只读就不调）。
    """
    global _SAFE_OK
    try:
        from capabilities.scanner import WRITE_PREFIXES  # noqa: WPS433
        return tuple(WRITE_PREFIXES)
    except Exception as e:  # pragma: no cover
        logger.warning("[returns-sampler] 写操作前缀单一事实源不可用，采样停用: %s", e)
        _SAFE_OK = False
        return ()


_SAFE_OK = True
_WRITE_PREFIXES = _write_prefixes()


def _is_write(name: str) -> bool:
    return name.startswith(_WRITE_PREFIXES) if _WRITE_PREFIXES else True


# ── 指纹 / 参数合成 / schema 推断 ──────────────────────────────
def _module_fingerprint(fn: Callable) -> str:
    """工具所在模块源文件的 stat 指纹；取不到返回空串（则该工具总重采）。"""
    try:
        f = inspect.getsourcefile(fn) or inspect.getfile(fn)
        st = Path(f).stat()
        return "%d:%d" % (st.st_mtime_ns, st.st_size)
    except Exception:
        return ""


def _infer_schema(v: Any, depth: int = 0) -> dict:
    if depth > MAX_DEPTH:
        return {"type": type(v).__name__}
    if isinstance(v, dict):
        keys = list(v.keys())[:MAX_KEYS]
        return {"type": "object", "keys": keys,
                "properties": {k: _infer_schema(v[k], depth + 1) for k in keys}}
    if isinstance(v, (list, tuple)):
        mkeys, mprops = [], {}
        for el in list(v)[:5]:
            if isinstance(el, dict):
                for k, val in list(el.items())[:MAX_KEYS]:
                    if k not in mprops:
                        mkeys.append(k)
                        mprops[k] = _infer_schema(val, depth + 1)
        if mprops:
            return {"type": "array", "len": len(v),
                    "items": {"type": "object", "keys": mkeys, "properties": mprops}}
        if v:
            return {"type": "array", "len": len(v), "items": _infer_schema(v[0], depth + 1)}
        return {"type": "array", "len": 0, "items": None}
    return {"type": {bool: "boolean", int: "integer", float: "number",
                     str: "string", type(None): "null"}.get(type(v), type(v).__name__)}


def _build_sample_args(fn: Callable):
    """按参数名合成样本实参；有任一必需参数无法合成 → 返回 None（跳过）。"""
    try:
        sig = inspect.signature(fn, eval_str=True)
    except Exception:
        return None
    args, kwargs = [], {}
    for p in sig.parameters.values():
        if p.default is not inspect.Parameter.empty:
            continue
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if p.name in _UNSAMPLEABLE or p.name not in _NAME_SAMPLES:
            return None
        val = _NAME_SAMPLES[p.name]
        if p.kind == p.KEYWORD_ONLY:
            kwargs[p.name] = val
        else:
            args.append(val)
    return args, kwargs


def _samplable(name: str, fn: Any):
    """返回 (可采样?, 跳过原因)。非函数 / 下划线 / 入口名 / 写操作 / 参数不可合成 → 跳过。"""
    if not callable(fn):
        return False, "not-callable"
    if not inspect.isfunction(fn):
        # Tool 实例（技能工具）等：不采样（其 forward 语义与 provider 工具不同）
        return False, "not-function"
    if name.startswith("_"):
        return False, "private"
    if name in _ENTRY_DENY:
        return False, "entry-name"
    if not _SAFE_OK or _is_write(name):
        return False, "write-op"
    if _build_sample_args(fn) is None:
        return False, "unsampleable-args"
    return True, ""


def _sample_one(fn: Callable) -> dict:
    sa = _build_sample_args(fn)
    if sa is None:
        return {"skipped": "unsampleable-args"}
    args, kwargs = sa
    t0 = time.time()
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(fn, *args, **kwargs)
        try:
            r = fut.result(timeout=TIMEOUT_S)
        except _FTimeout:
            return {"skipped": "timeout", "elapsed": round(time.time() - t0, 2)}
        except Exception as e:
            return {"error": "%s: %s" % (type(e).__name__, str(e)[:120]),
                    "elapsed": round(time.time() - t0, 2)}
    finally:
        ex.shutdown(wait=False)
    return {"schema": _infer_schema(r), "elapsed": round(time.time() - t0, 2),
            "arg_sample": {"args": args, "kwargs": kwargs},
            "is_error_payload": isinstance(r, dict) and "error" in r}


# ── 采样（写侧）───────────────────────────────────────────────
def _sample_tools(provider, cache_path: Path = _CACHE_PATH, log=logger.info,
                 max_workers: int | None = None) -> dict:
    """采集工具返回结构并写缓存。

    并行策略：先按模块指纹分拣「可复用」项（串行、零成本）；其余提交线程池并发；
    结果按工具名排序后统一打印，避免多线程日志交错。
    """
    fns = provider.get_functions()
    old: dict = {}
    if cache_path.exists():
        try:
            old = json.loads(cache_path.read_text(encoding="utf-8")).get("tools", {})
        except Exception:
            old = {}

    out = {"version": CACHE_VERSION, "sampled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "tool_count": len(fns), "tools": {}}
    t0 = time.time()

    todo = []           # [(name, fn, fp)]  需重采
    for name, fn in sorted(fns.items()):
        fp = _module_fingerprint(fn)
        prev = old.get(name)
        if prev and fp and prev.get("_fp") == fp:
            out["tools"][name] = prev          # 指纹未变 → 复用
        else:
            todo.append((name, fn, fp))
    n_reuse = len(fns) - len(todo)

    workers = SAMPLE_WORKERS if max_workers is None else max_workers
    results: dict = {}
    if todo:
        if workers and workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_sample_one, fn): (name, fp) for name, fn, fp in todo}
                for fut in as_completed(futs):
                    name, fp = futs[fut]
                    try:
                        res = fut.result()
                    except Exception as e:
                        res = {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}
                    res["_fp"] = fp
                    results[name] = res
        else:
            for name, fn, fp in todo:
                res = _sample_one(fn)
                res["_fp"] = fp
                results[name] = res

    for name, _fn, _fp in todo:
        res = results.get(name, {"error": "missing"})
        out["tools"][name] = res
        log("  %-34s %s" % (name,
            ("OK %ss" % res["elapsed"]) if "schema" in res else
            ("SKIP " + res.get("skipped", "")) if "skipped" in res else
            ("ERR " + res.get("error", ""))))

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log("[returns-sampler] 缓存写入失败: %s" % e)
    ok = sum(1 for v in out["tools"].values() if "schema" in v)
    log("[returns-sampler] 工具 %d | 有结构 %d | 重采 %d | 复用 %d | workers=%d | %.2fs"
        % (len(fns), ok, len(todo), n_reuse, (workers if todo else 0), time.time() - t0))
    return out


# ── 读取侧：渲染（内存缓存，按 mtime 失效）─────────────────────
_TYPE_MAP = {"string": "str", "integer": "int", "number": "num",
             "boolean": "bool", "null": "null"}

# 错误载荷的嫌疑键：若采样对象**只有**这些键，说明那是采样当次的失败返回，不是真结构
_SUSPECT_KEYS = {"error", "retriable", "msg", "message", "code", "delisted"}


def _quality(ent: Any) -> str:
    """采样条目质量三档：good / suspect / empty。

    good    —— 拿到非空真实结构（对象有键 / 数组）。
    suspect —— 只拿到错误载荷（键全在 _SUSPECT_KEYS 内），如 `obj{error:str, retriable:bool}`。
    empty   —— 空对象 / null / 无 schema（样本参数下无数据），如 `obj{}`。

    设计决策（2026-09-20）：suspect / empty **一律不注入**读取侧，回退 docstring。
    原因是「注入一个错误的返回形状」比「不注入」危害更大——模型会把 `obj{error:str}`
    当成该工具的真实结构去按键访问，反而制造幻觉。**工具本身不因此被屏蔽**（它仍可被调用）。
    """
    if not isinstance(ent, dict):
        return "empty"
    if ent.get("skipped") or ent.get("error"):
        return "empty"
    sc = ent.get("schema")
    if not isinstance(sc, dict):
        return "empty"
    t = sc.get("type")
    if t == "object":
        keys = set(sc.get("keys") or [])
        if not keys:
            return "empty"
        if keys <= _SUSPECT_KEYS:
            return "suspect"
        return "good"
    if t == "array":
        return "good"
    if t == "null":
        return "empty"
    return "good"


def _fold(schema: Any, depth: int = 0) -> str:
    """把采样 schema 折叠成紧凑单行。

    depth=0：顶层对象的键 + 类型；嵌套对象/数组收敛为 `obj`/`arr`（只保结构骨架）。
    这是实测的最省 token 渲染档（56 工具 ≈ 3.7K 字符），且信息量已足够——模型只需知道
    「顶层键叫什么、哪个键是列表」，元素细节可泛化处理。
    """
    if not isinstance(schema, dict):
        return "?"
    t = schema.get("type")
    if t == "object":
        ps = schema.get("properties") or {}
        if depth >= 1:
            return "obj"
        return "obj{%s}" % ", ".join("%s:%s" % (k, _fold(v, 1)) for k, v in ps.items())
    if t == "array":
        it = schema.get("items")
        if depth >= 1:
            return "arr"
        return "arr[%s]" % (_fold(it, 1) if it else "any")
    return _TYPE_MAP.get(t, str(t))


_CACHE_MEMO: dict = {"mtime": None, "tools": {}}
_CACHE_LOCK = threading.Lock()


def _load_cache(cache_path: Path = _CACHE_PATH) -> dict:
    """读缓存（内存 memo，按文件 mtime 失效）。任何异常 → 空表。"""
    try:
        mt = cache_path.stat().st_mtime_ns
    except Exception:
        return {}
    with _CACHE_LOCK:
        if _CACHE_MEMO["mtime"] == mt:
            return _CACHE_MEMO["tools"]
    try:
        tools = json.loads(cache_path.read_text(encoding="utf-8")).get("tools", {}) or {}
    except Exception:
        tools = {}
    with _CACHE_LOCK:
        _CACHE_MEMO["mtime"] = mt
        _CACHE_MEMO["tools"] = tools
    return tools


def _get_sampled_structures(names: Iterable[str],
                           cache_path: Path = _CACHE_PATH) -> Dict[str, str]:
    """按工具名集合返回 {name: 折叠后的返回结构单行}；未采到的工具不出现在结果里。

    供 `_build_return_contract_block(..., sampled=...)` 使用：命中的工具用采样结构，
    未命中的自动回退 docstring 契约（见该函数）。
    """
    cache = _load_cache(cache_path)
    if not cache:
        return {}
    out: Dict[str, str] = {}
    for n in names:
        ent = cache.get(n)
        if ent and _quality(ent) == "good":      # suspect/empty 不注入（见 _quality 注释）
            out[n] = _fold(ent["schema"])
    return out


def _cache_summary(cache_path: Path = _CACHE_PATH) -> dict:
    """缓存概况（诊断用）。"""
    tools = _load_cache(cache_path)
    ok = [n for n, v in tools.items() if isinstance(v, dict) and v.get("schema")]
    return {"path": str(cache_path), "total": len(tools), "with_schema": len(ok)}


# ── 启动接入（写侧）：非阻塞守护线程 ──────────────────────────
_BG_THREAD: threading.Thread | None = None
_BG_LOCK = threading.Lock()


def _start_background_sampling(provider, cache_path: Path = _CACHE_PATH,
                              log=logger.info, max_workers: int | None = None) -> threading.Thread:
    """在**守护线程**里跑采样——立即返回（微秒级），不阻塞主线程启动。

    缓存未就绪期间，读取侧 `get_sampled_structures()` 返回空 → 契约渲染自动回退
    docstring（零回归）。幂等：已在跑则不重复启动。
    """
    global _BG_THREAD
    with _BG_LOCK:
        if _BG_THREAD is not None and _BG_THREAD.is_alive():
            log("[returns-sampler] 后台采样已在运行，跳过重复启动")
            return _BG_THREAD

        def _run():
            try:
                _sample_tools(provider, cache_path=cache_path, log=log, max_workers=max_workers)
            except Exception as e:
                log("[returns-sampler] 后台采样失败（不影响主流程）: %s" % e)

        t = threading.Thread(target=_run, name="returns-sampler", daemon=True)
        _BG_THREAD = t
        t.start()
        log("[returns-sampler] 后台采样线程已启动（daemon，不阻塞启动）")
        return t


if __name__ == "__main__":  # 手动冷启/重扫：python -m app.agent.tools.returns_sampler
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from app.agent.tools.base import ToolProvider
    _prov = ToolProvider()
    _tdir = Path(__file__).resolve().parent
    _prov.scan_directory(_tdir, domain="common", package_prefix="tools")
    _prov.scan_subdirectories(_tdir, package_prefix="tools")
    _sample_tools(_prov, log=logger.info, max_workers=SAMPLE_WORKERS)
