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
import os
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


_DUP_SUFFIXES = (
    "_daily", "_live", "_history", "_from_ticks", "_realtime",
    "_detail", "_all", "_snapshot",
)
_GENERIC_NAMES = frozenset({
    "daily", "quote", "lhb", "index", "minute", "snapshot",
    "close", "open", "high", "low", "volume", "kline",
})


def _tool_stem(name: str) -> str:
    x = name
    for s in _DUP_SUFFIXES:
        if x.endswith(s):
            x = x[: -len(s)]
    return x


def near_dup_tool_names(a: str, b: str) -> bool:
    """语义近重名（工具层 vs 能力层让位判定）。

    规则（收紧，避免误伤 get_northbound_daily 这类独立能力）：
      1. 同名；2. 一方 token 集是另一方子集；3. 去后缀同干；4. 前两段相同。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = set(a.split("_")), set(b.split("_"))
    # token 子集：只当「多出来的词只是模式后缀」时算等价
    # （daily≈daily_live）；否则 get_northbound_daily 会被单字 daily 误杀。
    _mode_tokens = {"live", "daily", "history", "realtime", "detail",
                    "all", "snapshot", "from", "ticks", "minute"}
    if ta <= tb or tb <= ta:
        extra = (tb - ta) if ta <= tb else (ta - tb)
        if extra <= _mode_tokens:
            return True
    if _tool_stem(a) == _tool_stem(b):
        return True
    pa, pb = a.split("_"), b.split("_")
    if len(pa) >= 2 and len(pb) >= 2 and pa[0] == pb[0] and pa[1] == pb[1]:
        return True
    return False


def _shadows_domain_tool(name: str, provider) -> str:
    """若能力名与既有工具层（common/可选域）同名或近重名，返回遮蔽它的工具名。

    优先级原则（2026-09-25 用户裁定）：**工具层 > 能力层**。
    同名原本已让位；近重名此前漏判，双轨并存导致 planner 选错/幻象调用。
    """
    if name in _GENERIC_NAMES:
        return "(generic)"
    try:
        existing = set(provider.list_by_domain("common"))
        for d in provider.get_domains() or []:
            existing |= set(provider.list_by_domain(d))
    except Exception:
        return ""
    for f in sorted(existing):
        if f != name and near_dup_tool_names(name, f):
            return f
    return ""


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


def load_admitted_meta(admission_path=None) -> list:
    """准入条目全量元数据（含 superseded_by，供同功能筛选）。"""
    path = Path(admission_path) if admission_path else _ADMISSION_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    defaults = data.get("defaults") or {}
    out = []
    for ent in data.get("entries") or []:
        if not ent.get("admitted"):
            continue
        mod, name = ent.get("module"), ent.get("name")
        if not mod or not name:
            continue
        out.append({
            "module": mod,
            "name": name,
            "timeout_s": int(ent.get("timeout_s") or defaults.get("timeout_s") or 60),
            "max_chars": int(ent.get("max_chars") or defaults.get("max_chars") or 8000),
            "doc": ent.get("doc") or "",
            "superseded_by": ent.get("superseded_by") or "",
        })
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
            except Exception as e:
                p = None
                logger.warning("[capability:%s] 落盘失败: %s", tool_name, e)
            # 2026-09-25 智力下降修复：旧形态只有 {note,file,preview}，模型按
            # 业务工具习惯写 r["data"][code]/r[0] → KeyError，整段分析被拖垮。
            # 统一补可下标 data + count + truncated，与 tools.base 信封对齐。
            data_obj = result if isinstance(result, (list, dict)) else None
            if isinstance(result, list):
                data_obj = result[: max(1, min(50, max_chars // 80))]
            elif isinstance(result, dict):
                # 只镜像键，不深拷贝大结构；列表字段截断
                data_obj = {}
                for k, v in list(result.items())[:30]:
                    if isinstance(v, list):
                        data_obj[k] = v[:20]
                    else:
                        data_obj[k] = v
            out = {
                "count": (len(result) if isinstance(result, list)
                          else (len(result) if isinstance(result, dict) else 1)),
                "data": data_obj,
                "truncated": True,
                "note": f"[capability:{tool_name}] 结果 {len(text)} 字符超上限"
                        f"({max_chars})，已截断；完整数据见 file",
            }
            if p is not None:
                out["file"] = str(p)
            out["preview"] = text[:800]
            return out
        return result

    return wrapper


def register_capabilities(provider, admission_path=None,
                          domain: str = CAPABILITY_DOMAIN) -> int:
    """把准入清单中的函数注册进 provider（来源层标记，见 CAPABILITY_DOMAIN）。

    Returns:
        实际注册数（admission.json 缺失/损坏时为 0，不抛错）。
    """
    metas = load_admitted_meta(admission_path)
    if not metas:
        return 0
    # 启动期同功能筛选（缓存 + 可选 LLM）：工具层 > 能力层，判据是功能等价而非只看名字
    try:
        from . import func_overlap
        use_llm = os.getenv("FUNC_OVERLAP_LLM", "1").lower() not in ("0", "false", "no", "off")
        overlap = func_overlap.load_or_build(provider, metas, use_llm=use_llm)
    except Exception as e:
        logger.warning("[capabilities] 同功能筛选失败，退回近重名启发式: %s", e)
        overlap = {}

    entries = load_admitted(admission_path)
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
        meta = overlap.get(name) or {}
        if meta.get("decision") == "shadowed":
            conflicts.append(
                f"{mod_path}:{name}(同功能让位→{meta.get('superseded_by') or '?'};"
                f"{meta.get('reason')})"
            )
            continue
        # 报告缺失时的兜底：近重名仍让位（缓存未建/筛选失败）
        if not meta:
            _shadows = _shadows_domain_tool(name, provider)
            if _shadows:
                conflicts.append(f"{mod_path}:{name}(近重名让位→{_shadows})")
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
        logger.info("[capabilities] %d 项工具层让位（同名/近重名：域工具优先生效，能力待命，"
                    "删除包装后自动补位）: %s", len(conflicts), "；".join(conflicts))
    if failures:
        logger.warning("[capabilities] %d 项未注册: %s", len(failures), "；".join(failures))
    return registered
