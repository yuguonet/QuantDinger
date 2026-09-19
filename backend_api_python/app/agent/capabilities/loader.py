# -*- coding: utf-8 -*-
"""capabilities/loader.py — 准入能力加载 / 护栏包装 / 注册（A 阶段, 2026-09-12）

职责: 读 admission.json（人工过目固化的准入清单）-> 导入函数 -> 护栏包装 ->
      注册进 ToolProvider（来源层标记 domain=CAPABILITY_DOMAIN）。

来源层 vs 工具域（2026-09-13 修正）:
  本模块注册的是"数据能力层"——底层取数函数，按需点名注入，**不是** planner 用
  selected_domain 选择的工具集域。此前用 domain="quant" 注册，与真实域
  tools/finance（domain="finance"）并列且互斥（_build_code_agent 只加载
  common+单域），planner 无法判断该选哪个，选任一都丢掉另一半工具。
  现统一打 CAPABILITY_DOMAIN 标记，并从可选域清单（ToolProvider.get_domains）中
  天然排除；能力的唯一注入途径是 stage 级 tools 白名单。

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

# 写操作前缀（注册时二次复核）——单一事实源在 scanner.WRITE_PREFIXES。
# 2026-09-13 去重：原先此处复制了一份相同元组，靠注释"与 scanner 保持一致"手工同步；
# 一旦漂移，扫描器标 excluded 的函数可能在注册层被放行（或反之），且无告警。
from .scanner import WRITE_PREFIXES as _WRITE_PREFIXES  # noqa: E402


# 来源层标记（不是可选域）：capabilities 是"数据能力层"，不是 planner 用
# selected_domain 能选的工具集域。它只用于两处：planner 提示里"数据能力"分段的
# 过滤（task_agent），以及可选域清单（ToolProvider.get_domains）的天然排除。
# 2026-09-13：原为 domain="quant"，与真实域 tools/finance（domain="finance"）
# 并列且互斥，是"把工具来源当领域"的典型误用。
CAPABILITY_DOMAIN = "capability"


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


def register_capabilities(provider, admission_path=None,
                          domain: str = CAPABILITY_DOMAIN) -> int:
    """把准入清单中的函数注册进 provider（来源层标记，见 CAPABILITY_DOMAIN）。

    Returns:
        实际注册数（admission.json 缺失/损坏时为 0，不抛错）。
    """
    entries = load_admitted(admission_path)
    if not entries:
        return 0
    registered = 0
    failures = []
    conflicts = []
    for mod_path, name, timeout_s, max_chars in entries:
        if _is_hard_denied(name):
            failures.append(f"{mod_path}:{name}(写操作前缀,拒绝)")
            continue
        if name in provider:
            conflicts.append(f"{mod_path}:{name}")
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
            name, _wrap_guards(fn, timeout_s, max_chars, name), domain=domain
        )
        registered += 1
    # 日志分级（2026-09-18）：同名让位 = 预期内的替补待命（既有包装工具优先生效，
    # admission 有意保留这些条目，删除包装后自动补位）→ INFO，避免每次启动都拉
    # WARNING 造成警报疲劳；failures（写拒绝/导入失败）= 真问题 → 保持 WARNING。
    if conflicts:
        logger.info("[capabilities] %d 项同名让位（既有包装工具优先生效，能力函数待命，"
                    "删除包装后自动补位）: %s", len(conflicts), "；".join(conflicts))
    if failures:
        logger.warning("[capabilities] %d 项未注册: %s", len(failures), "；".join(failures))
    return registered
