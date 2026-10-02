# -*- coding: utf-8 -*-
"""list_skills / read_skill —— 技能读取工具（skills/ 域保留件的模型侧入口）。

旧核心用 _SkillSectionTool/_SkillResourceTool/_SkillFuncTool（smolagents 载体）把技能
喂给模型；mimoagent 范式下技能走统一函数工具通道，语义不变：
  目录 → list_skills；正文/小节/资源 → read_skill。
技能的 run.py 类执行入口（market_screener 等）由任务方按需直接调用，暂不入工具面。
"""
from __future__ import annotations

import json
from typing import Any, Dict


def _get_skills():
    from agent import skills as _s
    return _s


def list_skills() -> Dict[str, Any]:
    """列出全部可用技能（名称 + 简介）。

    Returns:
        {"count": N, "skills": [{name, description}, ...]}
    """
    try:
        return {"count": len(_get_skills()), "skills": _get_skills().list_skills()}
    except Exception as e:
        return {"error": str(e)}


def read_skill(name: str, heading: str = "", resource: str = "") -> Dict[str, Any]:
    """读取技能内容。三选一：全文（都不传）/ 按小节标题关键词 / 按资源相对路径。

    **读方法论用本工具；要真跑流水线用 run_skill。**

    Args:
        name: 技能名（list_skills 返回的 name）
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


# ═══════════════════════════════════════════════════════════
#  执行型技能分发（差距 B，2026-10-02）
#  重活（全市场扫描/回测等流水线）在**子进程**里跑：超时可击杀、崩溃隔离、
#  产物落 tmp/skill_output/，主上下文只拿截断预览 + 文件路径。
# ═══════════════════════════════════════════════════════════

_SKILL_RUN_TIMEOUT = 600   # 秒；流水线上限，超时击杀子进程


def run_skill(name: str, arguments: str = "") -> Dict[str, Any]:
    """执行执行型技能流水线（market_screener / strategy_debug），返回结论预览。

    批量任务（全市场扫描、策略诊断等）用本工具，不要在主对话里逐步跑取数；
    结果很大时引用返回的 full_path，不要把明细全量贴进对话。

    Args:
        name: 技能名（market_screener / strategy_debug）
        arguments: JSON 字符串，可含 fn（调用哪个入口，默认 run/debug_strategy）
                   与 kwargs（传给入口的参数）；无参可传空

    Returns:
        {"ok": True, "result": <预览>, "full_path": ..., "elapsed_s": ...} 或 {"error": ...}
    """
    import subprocess
    import sys
    from pathlib import Path

    runner = Path(__file__).resolve().parent.parent / "scripts" / "skill_run.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(runner), name, arguments or ""],
            capture_output=True, text=True, timeout=_SKILL_RUN_TIMEOUT,
            cwd=str(Path(__file__).resolve().parents[3]),   # backend_api_python
        )
    except subprocess.TimeoutExpired:
        return {"error": f"skill_run 超时（>{_SKILL_RUN_TIMEOUT}s）已中止: {name}"}
    except Exception as e:
        return {"error": f"skill_run 启动失败: {e}"}

    out = (proc.stdout or "").strip().splitlines()
    payload = out[-1] if out else ""
    if not payload:
        return {"error": f"skill_run 无输出（rc={proc.returncode}）: {(proc.stderr or '')[-500:]}"}
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return {"error": f"skill_run 输出非 JSON（rc={proc.returncode}）: {payload[:500]}"}
    if proc.returncode != 0 and data.get("ok") is not False:
        data.setdefault("error", f"rc={proc.returncode}")
    return data
