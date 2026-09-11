# -*- coding: utf-8 -*-
"""capabilities/loader.py — 准入能力加载 / 护栏包装 / 注册（A 阶段, 2026-09-12）

职责: 读 admission.json（人工过目固化的准入清单）-> 导入函数 -> 护栏包装 ->
      注册进 ToolProvider（domain="quant"）。

三层闸门之二在本层落地:
  - 写入硬复核: 名称命中写操作前缀的条目拒绝注册（即便被误写进配置）;
  - 超时护栏: 线程池提交 + 限时等待; 超时放弃等待并返回错误标记（只读调用无副作用）;
  - 体积护栏: 结果序列化 > max_chars 时写入 tmp/capability_output/, 工具返回
    预览 + 文件路径（引用而非拷贝，大结果不占上下文）。

易错点:
  - 同名函数冲突: provider 已存在同名工具 → 跳过并汇总告警（先注册者胜出）;
  - 单项失败不阻断: import/getattr 失败逐项跳过, 汇总一条 warning;
  - admission.json 缺失/损坏 → 按空清单处理（注册 0 个, 不抛错）;
  - functools.wraps 保留原签名（inspect.signature 跟随 __wrapped__），LLM schema 不变形;
  - 超时线程不可杀: 放弃等待后线程自然结束（只读函数, 可接受）。
"""
from __future__ import annotations

import functools
import importlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_ADMISSION_PATH = Path(__file__).resolve().parent / "admission.json"

# 写操作前缀（与 scanner.WRITE_PREFIXES 保持一致；注册时二次复核）
_WRITE_PREFIXES = (
    "set_", "update_", "delete_", "remove_", "insert_", "upsert_", "create_",
    "drop_", "clear_", "reset_", "save_", "write_", "run_", "start_", "stop_",
    "trigger_", "schedule_", "sync_", "cleanup_", "ensure_", "reconcile_",
    "emit_", "commit_", "apply_", "patch_", "migrate_", "purge_",
)


def _is_hard_denied(name: str) -> bool:
    return name.startswith(_WRITE_PREFIXES)


def _output_dir() -> Path:
    # capabilities/loader.py -> parents[4] = 仓库根; 大结果产物统一进 tmp/
    return Path(__file__).resolve().parents[4] / "tmp" / "capability_output"


def load_admitted(admission_path=None):
    """读取准入清单 -> [(module, name, timeout_s, max_chars), ...]。"""
    path = Path(admission_path) if admission_path else _ADMISSION_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.info("[capabilities] 未发现准入清单 %s，按空清单处理", path.name)
        return []
    except Exception as e:
        logger.warning("[capabilities] 准入清单解析失败（按空清单）: %s", e)
        return []
    defaults = data.get("defaults") or {}
    out = []
    for ent in data.get("entries") or []:
        if not ent.get("admitted"):
            continue
        mod = ent.get("module")
        name = ent.get("name")
        if not mod or not name:
            continue
        out.append((
            mod, name,
            int(ent.get("timeout_s") or defaults.get("timeout_s") or 60),
            int(ent.get("max_chars") or defaults.get("max_chars") or 8000),
        ))
    return out


def _spill(name: str, text: str) -> Path:
    out_dir = _output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    p.write_text(text, encoding="utf-8")
    return p


def _wrap_guards(fn, timeout_s, max_chars, tool_name):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(fn, *args, **kwargs)
            try:
                result = fut.result(timeout=timeout_s)
            except FuturesTimeoutError:
                return {"error": f"[capability:{tool_name}] 超时(>{timeout_s}s)，"
                                 f"已放弃等待；建议缩小查询范围"}
            except Exception as e:
                return {"error": f"[capability:{tool_name}] 执行失败: "
                                 f"{type(e).__name__}: {e}"}
        finally:
            ex.shutdown(wait=False)
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            text = str(result)
        if len(text) > max_chars:
            try:
                p = _spill(tool_name, text)
                return {
                    "note": f"[capability:{tool_name}] 结果 {len(text)} 字符超上限"
                            f"({max_chars})，完整数据已写入文件",
                    "file": str(p),
                    "preview": text[:1200],
                }
            except Exception as e:
                return {"note": f"[capability:{tool_name}] 结果过大且落盘失败: {e}",
                        "preview": text[:1200]}
        return result

    return wrapper


def register_capabilities(provider, admission_path=None) -> int:
    """把准入清单中的函数注册进 provider（domain='quant'）。返回注册数。"""
    entries = load_admitted(admission_path)
    if not entries:
        return 0
    registered = 0
    failures = []
    for mod_path, name, timeout_s, max_chars in entries:
        if _is_hard_denied(name):
            failures.append(f"{mod_path}:{name}(写操作前缀,拒绝)")
            continue
        if name in provider:
            failures.append(f"{mod_path}:{name}(名称冲突)")
            continue
        try:
            mod = importlib.import_module(mod_path)
            fn = getattr(mod, name)
            if not callable(fn):
                raise TypeError("not callable")
        except Exception as e:
            failures.append(f"{mod_path}:{name}(导入失败:{e})")
            continue
        provider.register(
            name, _wrap_guards(fn, timeout_s, max_chars, name), domain="quant"
        )
        registered += 1
    if failures:
        logger.warning("[capabilities] %d 项未注册: %s", len(failures), "；".join(failures))
    return registered
