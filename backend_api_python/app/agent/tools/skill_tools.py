# -*- coding: utf-8 -*-
"""list_skills / read_skill / run_skill —— 技能读取与执行型技能分发工具。

旧核心用 _SkillSectionTool/_SkillResourceTool/_SkillFuncTool（smolagents 载体）把技能
喂给模型；mimoagent 范式下技能走统一函数工具通道，语义不变：
  目录 → list_skills；正文/小节/资源 → read_skill；流水线执行 → run_skill。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict

from ..exec.job_runner import JobStalled, run_job
from ..log import logger


def _get_skills():
    from ..agent import skills as _s
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
#  执行型技能分发（差距 B，2026-10-02；F26-D2 作业目录/manifest，2026-10-18）
#  重活（全市场扫描/回测等流水线）在**子进程**里跑：超时可击杀、崩溃隔离、
#  产物落 **项目级** <backend_api_python>/tmp/skill_output/jobs/<job_id>/：
#    · 2026-10-14 随 `app/agent/tmp/` 移除从 app/agent/tmp/ 迁到项目级 tmp/；
#    · 2026-10-18 F26-D2 改**作业目录 + manifest**（对齐 backtest_bridge 范式），
#      执行出口统一走 `exec/job_runner.run_job` —— 杀树（此前 subprocess.run
#      超时只杀直接子进程、孙进程残留）+ 停滞看门狗 + 墙钟三件事。
#  主上下文只拿截断预览 + 文件路径。
# ═══════════════════════════════════════════════════════════

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
         "job_dir": ...} 或 {"error": ..., "job_dir": ...}
        （job_dir = 作业目录，manifest.json 在内，终态可观测）
    """
    # 名称归一化（2026-10-12）：SKILL.md frontmatter 的 name 用连字符
    # （market-screener / strategy-debug），而 skill_run.py 的执行注册表键是**目录名**
    # 下划线（market-screener / strategy-debug），且那边是精确匹配、不做归一化。
    # 模型从 list_skills() / [相关技能：xxx] 拿到的是**显示名**，不经归一化直达子进程
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
