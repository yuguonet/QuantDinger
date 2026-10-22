# -*- coding: utf-8 -*-
"""执行层工具 —— activate_tools / read_skill / run_skill / call_capability。

由三模块合并而来（2026-10-22，AP.7）：
  - tool_discovery.py 的 activate_tools（list_tools/search_tools 随统一发现层
    并进 resource_catalog 作为内部接口，不再注册为工具面动词）；
  - skill_tools.py 的 read_skill / run_skill（技能读取 + 执行型技能子进程分发）；
  - capability_tools.py 的 call_capability（能力层低优先级派发，三闸门）。

合并只做文件归并，函数体逐字保真：本模块公开函数恰好 4 个执行动词，
随 tools/scan_directory 自动注册进工具面；辅助函数一律 `_` 前缀退出注册。

依赖方向：resource_catalog（统一发现口）→ 本模块（无反向 import，防环）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from ..exec.job_runner import JobStalled, run_job
from ..log import logger


# ═══════════════════════════════════════════════════════════
#  activate_tools（原 tool_discovery.py）
# ═══════════════════════════════════════════════════════════

def _agent_of(_context: Any):
    return (_context or {}).get("agent") if isinstance(_context, dict) else None


def activate_tools(names: str, _context: Any = None) -> str:
    """点名激活按需层工具（激活后其完整 schema 才进入后续请求的工具清单）。

    Args:
        names: 工具名，逗号/空格分隔，如 "get_realtime_quote,get_fund_flow"。

    Returns:
        激活结果：已激活 / 本就激活 / 未找到。
    """
    import re
    raw = re.split(r"[,\s]+", (names or "").strip())
    want = [n for n in raw if n]
    if not want:
        return "请给出要激活的工具名（逗号或空格分隔）。"
    agent = _agent_of(_context)
    if agent is None or not hasattr(agent, "activate_tools"):
        return f"当前无活动会话，无法激活：{', '.join(want)}（请通过 agent 调用本工具）。"
    res = agent.activate_tools(want)
    activated = res.get("activated", [])
    already = res.get("already", [])
    unknown = res.get("unknown", [])
    off = res.get("disabled", [])
    lines = [f"激活结果：新激活 {len(activated)}，已激活 {len(already)}，未找到 {len(unknown)}"
             + (f"，已停用 {len(off)}" if off else "") + "。"]
    if activated:
        lines.append("新激活: " + ", ".join(activated))
    if unknown:
        lines.append("未找到: " + ", ".join(unknown) + "（用 search_resources 查准确名字）")
    if off:
        # ★ T7（2026-10-17）：disabled 不是"未找到"——静默归零会让模型反复重试。
        #   明示停用状态；直调仍可回退执行（execute_action fail-open），但不会
        #   进工具面，别把任务押在它上面。
        lines.append("已停用: " + ", ".join(off)
                     + "（当前被 tool_connect 禁用——不会进工具清单；如确需调用请直呼其名走回退通道）")
    lines.append(f"当前已激活按需工具 {res.get('active_total', 0)}/{res.get('cap', 0)} 个。")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  read_skill / run_skill（原 skill_tools.py）
# ═══════════════════════════════════════════════════════════

def _get_skills():
    from ..agent import skills as _s
    return _s


def read_skill(name: str, heading: str = "", resource: str = "") -> Dict[str, Any]:
    """读取技能内容。三选一：全文（都不传）/ 按小节标题关键词 / 按资源相对路径。

    **读方法论用本工具；要真跑流水线用 run_skill。**

    Args:
        name: 技能名（list_resources 返回的 name）
        heading: 小节标题关键词（如 "分析框架"），优先级高于 resource
        resource: 资源相对路径（如 "references/analysis-framework.md"）

    Returns:
        {"name": ..., "content": ...} 或 {"error": ...}
    """
    sk = _get_skills()
    try:
        if heading:
            text = sk.load_section(name, heading)
            if text is None:
                return {"error": f"技能 {name} 无匹配小节: {heading}",
                        "headings": sk.get_section_headings(name)}
        elif resource:
            text = sk.load_resource(name, resource)
            if text is None:
                return {"error": f"技能 {name} 无资源: {resource}",
                        "resources": sk.list_resources(name)}
        else:
            text = sk.load_body(name)
        if text is None:
            return {"error": f"技能不存在: {name}"}
        return {"name": name, "content": text[:20000]}
    except Exception as e:
        return {"error": str(e)}


#: 流水线墙钟上限（秒）。env `QD_SKILL_RUN_TIMEOUT` 可调；0/负/非法回落本缺省。
_SKILL_RUN_TIMEOUT = 600


def _skill_run_timeout() -> int:
    """墙钟上限：env 优先（阈值不走硬编码），回落模块缺省。"""
    raw = os.environ.get("QD_SKILL_RUN_TIMEOUT", "") or ""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _SKILL_RUN_TIMEOUT
    return v if v > 0 else _SKILL_RUN_TIMEOUT


def _jobs_root() -> Path:
    """技能作业根：<backend_api_python>/tmp/skill_output/jobs（与旧扁平输出同根）。"""
    return Path(__file__).resolve().parents[3] / "tmp" / "skill_output" / "jobs"


def _new_job_dir(name: str) -> Path:
    """建作业目录（幂等 mkdir；job_id 含时间戳+随机后缀防同秒撞名）。"""
    job_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{name}_{uuid.uuid4().hex[:6]}"
    job = _jobs_root() / job_id
    job.mkdir(parents=True, exist_ok=True)
    return job


def _write_manifest(job: Path, **fields) -> None:
    """manifest 合并写：ts 只记**起始**时刻（终态写不改起始），state 由调用方给。

    写失败只 warning 不判死 —— manifest 是观测件，运行结果已由子进程 stdout 决定；
    但**不许静默**（观测件丢了要在日志里看得见）。
    """
    mp = job / "manifest.json"
    m: Dict[str, Any] = {}
    if mp.is_file():
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            m = {}
    m.setdefault("job_id", job.name)
    m.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
    m.update(fields)
    try:
        mp.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("[run_skill] manifest 写入失败（%s）：%s", job, e)


def run_skill(name: str, arguments: str = "") -> Dict[str, Any]:
    """执行执行型技能流水线（market-screener / strategy-debug），返回结论预览。
    批量任务（全市场扫描、策略诊断等）用本工具，不要在主对话里逐步跑取数；
    结果很大时引用返回的 full_path，不要把明细全量贴进对话。

    Args:
        name: 技能名（market-screener / strategy-debug）
        arguments: JSON 字符串，可含 fn（调用哪个入口，默认 run/debug_strategy）
                   与 kwargs（传给入口的参数）；无参可传空

    Returns:
        {"ok": True, "result": <预览>, "full_path": ..., "elapsed_s": ...,
         "job_dir": ...} 或 {"error": ..., "job_dir": ...}（job_dir=作业目录）。
        预览按**整条目**裁剪（永远是合法 JSON）；被裁时多带 preview_note
        说明裁了几条、全量在 full_path —— 看到它就别把预览当全量下结论。
    """
    # 名称归一化（2026-10-12）：SKILL.md frontmatter 的 name 用连字符
    # （market-screener / strategy-debug），而 skill_run.py 的执行注册表键是**目录名**
    # 下划线（market-screener / strategy-debug），且那边是精确匹配、不做归一化。
    # 模型从 list_resources() / [相关技能：xxx] 拿到的是**显示名**，不经归一化直达子进程
    # 会恒定返回「未知技能。可执行: ['market-screener', 'strategy-debug']」。
    # 注册表是唯一事实源（QDSkillAdapter.get 已有 display_name → 目录名映射），
    # 这里复用它，不再另写一份字符串替换。
    try:
        _info = _get_skills().get(name)
        if _info is not None and getattr(_info, "name", ""):
            name = _info.name
    except Exception:
        pass

    runner = Path(__file__).resolve().parent.parent / "scripts" / "skill_run.py"
    backend_root = Path(__file__).resolve().parents[3]
    timeout_s = _skill_run_timeout()
    job = _new_job_dir(name)
    _write_manifest(job, name=name, state="running", timeout_s=timeout_s,
                    arguments=arguments or "")
    try:
        # 子进程 stdout 是 UTF-8（ensure_ascii=False 的中文 JSON）；不显式指定编码时
        # Windows 父进程按 GBK 解码 → 读线程 UnicodeDecodeError，stdout 整个丢掉
        #（该口径由 run_job 的 text/encoding/errors 参数保证）。
        # check_rc=False：skill_run 的**结构化错误通道**——rc≠0 时 stdout 仍是给模型的
        # ok=False JSON（"未知参数"还带 hint/report_guide 重试指引），必须照常解析；
        # 由 run_job 统一抛 RuntimeError 会把指引吞成一句"启动失败"（冒烟 B② 实锤）。
        out, err, rc = run_job(
            [sys.executable, str(runner), name, arguments or ""],
            job_dir=job, timeout_s=timeout_s, check_rc=False,
            env={**os.environ, "PYTHONIOENCODING": "utf-8",
                 "QD_SKILL_JOB_DIR": str(job)},
            cwd=str(backend_root),
        )
    except subprocess.TimeoutExpired:
        _write_manifest(job, name=name, state="timeout", timeout_s=timeout_s,
                        error=f"墙钟 {timeout_s}s 到，已杀树")
        return {"error": f"skill_run 超时（>{timeout_s}s）已中止: {name}",
                "job_dir": str(job)}
    except JobStalled as e:
        _write_manifest(job, name=name, state="stalled", timeout_s=timeout_s,
                        error=str(e))
        return {"error": f"skill_run 停滞判死: {e}", "job_dir": str(job)}
    except Exception as e:
        _write_manifest(job, name=name, state="failed", timeout_s=timeout_s,
                        error=f"{type(e).__name__}: {e}")
        return {"error": f"skill_run 启动失败: {e}", "job_dir": str(job)}

    lines = (out or "").strip().splitlines()
    payload = lines[-1] if lines else ""
    if not payload:
        _write_manifest(job, name=name, state="failed", timeout_s=timeout_s,
                        error=f"rc={rc}，stderr 尾: {(err or '')[-300:]}")
        return {"error": f"skill_run 无输出（rc={rc}）: {(err or '')[-500:]}",
                "job_dir": str(job)}
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        _write_manifest(job, name=name, state="failed", timeout_s=timeout_s,
                        error=f"rc={rc}，输出非 JSON: {payload[:200]}")
        return {"error": f"skill_run 输出非 JSON（rc={rc}）: {payload[:500]}",
                "job_dir": str(job)}
    if not isinstance(data, dict):
        _write_manifest(job, name=name, state="failed", timeout_s=timeout_s,
                        error=f"rc={rc}，输出不是对象: {payload[:200]}")
        return {"error": f"skill_run 输出不是 JSON 对象（rc={rc}）: {payload[:500]}",
                "job_dir": str(job)}
    if rc != 0 and data.get("ok") is not False:
        # 与 F26-D2 之前逐字一致：rc≠0 但 stdout 没给明确 ok=False 时补 rc 归因
        data.setdefault("error", f"rc={rc}")
    _write_manifest(job, name=name, timeout_s=timeout_s,
                    state="done" if data.get("ok") else "failed",
                    ok=bool(data.get("ok")),
                    elapsed_s=data.get("elapsed_s"))
    data.setdefault("job_dir", str(job))
    return data


# ═══════════════════════════════════════════════════════════
#  call_capability（原 capability_tools.py）
# ═══════════════════════════════════════════════════════════

def _load_loader():
    from ..capabilities import loader as _l
    return _l


def _admitted_meta() -> List[dict]:
    try:
        return _load_loader().load_admitted_meta() or []
    except Exception:
        return []


def _active_tool_face():
    """当前 run 的活跃 ToolFace（QDAgent）；无 → None（能力层可脱离 agent 直连调用）。

    经线程局部钩子表取 `active_agent`（`qd_agent.run()` 在 run 开始写入、`finally` 清）。
    取不到不是异常：脚本/测试直连能力层时本就不该有面上 agent。
    """
    try:
        from ..qd_hooks import _hooks_now
        return _hooks_now().get("active_agent")
    except Exception:
        return None


def _call_tool_layer(provider, name: str, arguments: dict) -> Dict[str, Any]:
    """F4（2026-10-17）：同名遮蔽 —— **直接激活并执行工具层实现**，不再让模型白跑一轮。

    「工具层 > 能力层」的语义就是"用工具层那份"：既然同名工具存在，派发层就把它
    执行掉、把结果原样交给模型（与工具面执行的是同一实现），而不是回一句错误让
    模型再发一次 tool call（run10 实测：模型据此绕了 3 轮空转）。
    顺带把该工具激活进本轮工具面（`activate_tools`），使后续该工具**直调**也可用；
    无活跃 agent（脚本直连）时跳过激活，仅执行。
    执行失败/取不到实现才退为可执行指引（fail-open，不阻断）。
    """
    agent = _active_tool_face()
    if agent is not None:
        try:
            agent.activate_tools([name])      # 让同名工具进本轮工具面（后续直调）
        except Exception:
            pass
    getter = getattr(provider, "get", None)
    fn = getter(name) if getter is not None else None
    if fn is None:
        return {"error": f"工具层同名工具 {name} 不可通过派发层执行，"
                         f"请用 search_resources 激活后直接调用"}
    try:
        result = fn(**(arguments or {}))
    except Exception as e:
        return {"error": f"工具层同名工具 {name} 执行失败: {e}（请直接调用该工具）"}
    if isinstance(result, dict):
        # ★ 与 G7② 同口径：标注键 name 在后，不被工具返回里的 name 覆盖（留痕不失真）
        return {**result, "name": name}
    return {"name": name, "result": result}


def call_capability(name: str, arguments: dict = None) -> Dict[str, Any]:
    """调用一个准入能力函数（经护栏：写前缀复核/超时/结果体积）。低优先级派发。

    Args:
        name: 能力函数名（list_resources/search_resources 返回的 name）
        arguments: 传给函数的关键字参数

    Returns:
        {"name": ..., "result": ...} 或 {"error": ...}
    """
    import importlib
    from ..modpath import import_module

    loader = _load_loader()
    # 工具层 > 能力层：同名工具存在时**直接执行工具层实现**（F4，2026-10-17）。
    # 原实现回一句错误让模型改用工具层 ⇒ 模型需多花一轮重发 tool call（run10 实证绕 3 轮）。
    from ._base import ToolProvider
    provider = ToolProvider.get_default()
    if provider is not None and name in provider:
        return _call_tool_layer(provider, name, arguments)

    # ★ 2026-10-14 G1：上面**只判同名**，与另两个执行点不同口径 ——
    #   注册期 `capabilities/loader.py::register_capabilities` 用「同名 + 近重名」
    #   （`_shadows_domain_tool`）、预选期 `lint_selection` R0 用 `near_dup_tool_names`。
    #   后果：名字不同但功能近重的能力（如 `get_fund_flow_daily` 对上域内
    #   `get_fund_flow`）在注册期被判让位（根本不注册），派发期却能照常执行 ——
    #   让位裁定被旁路，工具层 > 能力层形同虚设。此处补上近重名判据，三处同口径。
    if provider is not None:
        shadow = loader._shadows_domain_tool(name, provider)
        if shadow:
            # ⚠ `(generic)` 是 `_shadows_domain_tool` 对通用名（daily/quote…）的返回值，
            #   不是工具名 —— 判 truthiness 即可，别去工具表里比对它。
            where = "" if shadow == "(generic)" else f" {shadow}"
            return {"error": f"能力 {name} 已让位给工具层{where}（同名/近重名，"
                             f"工具层优先），请改用工具层工具"}

    entry = None
    for mod_path, fn_name, timeout_s, max_chars in (loader.load_admitted() or []):
        if fn_name == name:
            entry = (mod_path, fn_name, timeout_s, max_chars)
            break
    if entry is None:
        return {"error": f"能力不存在或未准入: {name}", "available": [m.get("name") for m in _admitted_meta()][:20]}

    mod_path, fn_name, timeout_s, max_chars = entry
    try:
        fn = getattr(import_module(mod_path), fn_name)
    except Exception as e:
        return {"error": f"能力导入失败: {e}"}
    wrapped = loader._wrap_guards(fn, timeout_s, max_chars, fn_name)
    try:
        result = wrapped(**(arguments or {}))
    except Exception as e:
        return {"error": f"能力执行失败: {e}"}
    if isinstance(result, dict):
        # ★ 2026-10-14 G7②：展开顺序必须是 **result 在前、标注键在后**。
        #   原写法 `{"name": fn_name, **result}` 让能力返回值里的 `name` **赢**，
        #   trace/追责里记到的就成了能力内部自报的名字（留痕失真）。
        #   其余字段仍原样透传，契约形状不变。
        return {**result, "name": fn_name}
    return {"name": fn_name, "result": result}