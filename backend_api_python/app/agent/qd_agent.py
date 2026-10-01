# -*- coding: utf-8 -*-
"""QDAgent — QuantDinger × mimoagent 执行核组装（v2 大包大揽版·试一版）。

设计定调（2026-09-30 用户拍板）：
  **mimo 原版为主、增强为辅，不做强行嫁接。**
  - Agent 循环 / 工具调度 / 并行 / 上下文 / 容错 / 轨迹 → mimoagent DefaultAgent 原版
  - 规划 → mimo 原版方式（模型在循环内自规划，system template 承载纪律），
    不引入外部 Planner/phases/StateGraph——在原版基础上增强的是"金融域纪律"
    （数字溯源、交易确认、输出格式），不是替换它的规划机制
  - 保留：tools/（FnToolAdapter 零改动挂载）、skills/、三个闭环
    ① skill 酿造（chain/，与执行核解耦）② 数字溯源+审计（本文件 grounding 门
    + audit/trace_adapter.py）③ 交易人工确认（interceptor + 函数内 confirm 硬闸）

被本模块取代的旧件（P4 删除）：graph.py / nodes.py / agents/task_agent.py /
llm/* / infra/* / execution/*。对外入口（cli、flask 路由、message_queue、cron）
形态不变，调用点改为 QDAgent.run()。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

# ⚠ 本模块**没有**沿用项目 `from log import logger` 的写法：cli 直跑与单元测试两种
# 入口下 `log` 不一定在 sys.path，一旦缺失就是 NameError（2026-10-01 实测：新增的
# 上下文窗口校验分支在 smoke test 7 里以 NameError("name 'logger' is not defined")
# 静默炸掉，而该分支本该是 fail-open 的）。标准 logging 在任何入口都成立。
logger = logging.getLogger(__name__)

import mimo_boot  # noqa: F401  # 统一依赖引导：找不到 mimoagent 时给安装指引（入口首行）

from mimoagent.agents.antihack import AntiHackConfig
from mimoagent.agents.base import TerminatingException
# 执行核升级（2026-09-30）：DefaultAgent（基线循环）→ MimocodeAgent
# （MiMo Desktop 引擎：10KB 精调核心 prompt + task 子代理 + compact 上下文压缩
#  + <context_usage> 预算感知 + AntiHackGuard + 入参 schema 硬校验）
from mimoagent.agents.mimocode.mimocode_agent import (
    MimocodeAgent, MimocodeAgentConfig, _CORE_PROMPT,
)
from mimoagent.tools.mimocode import MIMOCODE_CORE_TOOL_NAMES, MimocodeToolRegistry
from mimoagent.tools.base import ToolOutput
from mimoagent.tools.registry import ToolRegistry

try:  # 生产：backend_api_python 为根
    from app.agent.tools.fn_adapter import FnToolAdapter
    from app.agent.audit.trace_adapter import TraceAdapter
    from app.agent.tools.tool_preselect import (
        build_catalog, build_catalog_grouped, build_preselect_messages,
        parse_selection, parse_plan, lint_selection, apply_lint,
        get_tool_index, has_domain_hint, capability_domain,
        fallback_domain_tools)
except ImportError:  # cli/测试：app/agent 在 sys.path
    from tools.fn_adapter import FnToolAdapter
    from audit.trace_adapter import TraceAdapter
    from tools.tool_preselect import (
        build_catalog, build_catalog_grouped, build_preselect_messages,
        parse_selection, parse_plan, lint_selection, apply_lint,
        get_tool_index, has_domain_hint, capability_domain,
        fallback_domain_tools)


# ═══════════════════════════════════════════════════════════════
#  Windows 路径兼容 shim（mimo 原生 read/write/edit/glob/grep/bash）
# ═══════════════════════════════════════════════════════════════
#
# 【根因】mimoagent 的 tools/mimocode/paths.py::resolve_path 只认 POSIX 绝对路径：
#     if path.startswith("/"): return normpath(path)
#     else: join(session_cwd, path)
# Windows 的 `C:\xxx` / `\\server\share` 不走第一支 ⇒ 被当成相对路径拼到 cwd 后面，
# 得到 "C:\Temp\tmpXXX/C:\Temp\tmpXXX/demo.txt" 这种鬼东西 ⇒ 原生 write/read 在
# Windows 上**只要传绝对路径就必炸 FileNotFoundError**（smoke test 6 即此，改前必现）。
# 而生产环境进程 cwd 是 backend_api_python，模型从 bash 的 pwd 拿到的一定是
# `D:\...` 绝对形式 ⇒ 这不是测试问题，是 Windows 上的实盘缺陷。
#
# 【为什么是 shim 而不是改包】mimoagent 是第三方依赖，改 site-packages 不可持续。
# 各消费模块是 `from ...paths import resolve_path`（**按值绑定**），只改 paths 模块
# 属性无效 ⇒ 需要在 paths 与 6 个消费模块上同时重绑。收敛在本函数，一处可回滚。
def _install_windows_path_shim() -> None:
    import ntpath
    import importlib

    try:
        from mimoagent.tools.mimocode import paths as _paths
    except Exception:
        return
    if getattr(_paths, "_qd_win_shim", False):
        return
    _orig = _paths.resolve_path

    def _resolve_path(path: str, context) -> str:
        p = str(path)
        # Windows 绝对形态：盘符（C:\ 或 C:/）与 UNC（\\host\share）→ 原样规范化
        if ntpath.isabs(p) or p.startswith("\\\\"):
            return ntpath.normpath(p)
        return _orig(p, context)

    _paths.resolve_path = _resolve_path
    for _mod in ("bash", "edit", "glob", "grep", "read", "write"):
        try:
            m = importlib.import_module(f"mimoagent.tools.mimocode.{_mod}")
            if hasattr(m, "resolve_path"):
                m.resolve_path = _resolve_path
        except Exception:
            continue
    _paths._qd_win_shim = True


_install_windows_path_shim()


# ═══════════════════════════════════════════════════════════════
#  增强版 system template（mimo 原版基座句 + 金融域纪律）
# ═══════════════════════════════════════════════════════════════

def _load_system_template() -> str:
    """mimo 原版 _CORE_PROMPT（行为基座）+ 金融域覆盖层（后置，指令晚者优先）。"""
    p = Path(__file__).resolve().parent / "prompts" / "qd_system.txt"
    return _CORE_PROMPT + "\n\n---\n\n" + p.read_text(encoding="utf-8").strip()


# ═══════════════════════════════════════════════════════════════
#  危险面守卫（沿用修订三②的口径：凭据路径 deny + 破坏性命令 deny）
# ═══════════════════════════════════════════════════════════════

# 会话钩子表（承接2026-09-24 修订④「线程局部绑定」设计）：
# 对外 API 名与旧 agents.task_agent 一致，message_queue 只需换 import 行。
# event_cb=过程事件（→SSE）；step_checks=每步开头检查（可抛错中止，异常原样上抛）；
# probes=工具派发前中断探针（命中转 _ProbeInterrupt 终止 run）。

class _ProbeInterrupt(TerminatingException):
    """探针命中（用户停止）→ 终止 run，状态名即类名。"""


_HOOKS: dict = {}          # session_id -> {event_cb, step_checks, probes, active_agent}
_HOOKS_LOCK = threading.Lock()
_HOOKS_LOCAL = threading.local()


def bind_run_session(session_id: str) -> None:
    _HOOKS_LOCAL.session_id = str(session_id)


def register_run_hooks(session_id: str, *, event_cb=None, step_checks=None, probes=None) -> None:
    with _HOOKS_LOCK:
        _HOOKS[str(session_id)] = {
            "event_cb": event_cb,
            "step_checks": list(step_checks or ()),
            "probes": list(probes or ()),
        }


def unregister_run_hooks(session_id: str) -> None:
    with _HOOKS_LOCK:
        _HOOKS.pop(str(session_id), None)


def _hooks_now() -> dict:
    sid = getattr(_HOOKS_LOCAL, "session_id", None)
    if sid is None:
        return {}
    with _HOOKS_LOCK:
        return _HOOKS.get(sid) or {}


_DENY_PATH_RE = re.compile(
    r"(~/?\.ssh|/etc/(passwd|shadow|sudoers)|\.git-credentials|\.aws/credentials"
    r"|\.kube/config|\.docker/config\.json|authorized_keys|id_(rsa|ed25519)"
    r"|openclaw\.json|(^|/)\.env($|/)|credentials|private[_-]?key)",
    re.IGNORECASE,
)

_DENY_CMD_RE = re.compile(
    r"(\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r|\bmkfs\b|\bdd\s+if=|\bshred\b"
    r"|\bwipefs\b|/dev/sd|>\s*/dev/|\bvisudo\b|\bcrontab\b|\b(useradd|usermod|passwd)\b"
    r"|chmod\s+.*authorized_keys|chown\s+.*authorized_keys"
    r"|\bsystemctl\s+(enable|disable)\b|curl[^|]*\|\s*(ba)?sh|wget[^|]*\|\s*(ba)?sh)",
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════════

# 压缩摘要金融化定制（提智点 D）：compact 默认摘要偏通用，金融细节易丢。
# 要求摘要必保：数字+来源、标的代码、已有结论、未完事项、用户偏好。
_QD_COMPACT_NOTES = (
    "Compressed summary must preserve, in Chinese where natural: "
    "(1) every number cited so far with its source tool name; "
    "(2) stock/board codes and names discussed; "
    "(3) conclusions already established and their evidence; "
    "(4) open threads / pending checks; (5) user preferences stated. "
    "Drop tool boilerplate, retries and failed attempts first."
)


# ── 工具分层口径（2026-10-01）──────────────────────────────────
# 必注入层 = mimo 原生工具 + 这两个域（tools/ 顶层 common + 元工具 meta）。
# 其余子域（finance / knowledge …）= 按需层，默认**不下发 schema**。
# 依据：全量 73 个工具 schema ≈ 12.8k tokens，而 mimo 每轮请求整包下发
# （DefaultAgent.get_model_query_kwargs 返回 registry 全量定义）⇒ 多轮即 token 爆炸。
CORE_TOOL_DOMAINS = ("common", "meta")


def _context_window() -> int:
    """模型上下文窗口（token）——compaction 与 wrap_up 的**唯一**口径。

    取值优先级：`QD_CONTEXT_WINDOW` > `QD_COMPACT_WINDOW`（旧名兼容）> `OPENAI_MAX_TOKENS` > 32768。

    【为什么 OPENAI_MAX_TOKENS 只能当兜底，不能当真值】
    它在 `app/agent/agent.py:44` 是被当 **max_tokens（单次输出上限）** 消费的，
    与输入窗口是两个量纲。实测反证：6 步的选股任务单轮 input 已到约 45k 仍请求
    成功 ⇒ 真实输入窗口必然 > 45k；若窗口真是 32k，那次请求早就 400 了。
    所以它只在"没有任何显式配置"时兜底（且取 32768 这个保守值）。

    ⚠ 想按 32768 做严格保护，显式设 `QD_CONTEXT_WINDOW=32768` 即可；
      但请先确认模型真实窗口，否则阈值过低会导致**频繁无谓压缩**（丢上下文）。
    """
    for key in ("QD_CONTEXT_WINDOW", "QD_COMPACT_WINDOW"):
        raw = (os.getenv(key) or "").strip()
        if raw:
            try:
                return max(4096, int(raw))
            except ValueError:
                logger.warning("[ctx] %s 非整数，忽略: %r", key, raw)
    # ★ 兜底**不再**取 OPENAI_MAX_TOKENS：两者量纲不同，拿输出上限当输入窗口会让
    #   阈值低到在正常任务上误触发压缩（continuation 任务单轮就 41k，若窗口按 32k
    #   算，阈值 18k，第二轮就会被压掉上下文）。65536 是实测折中：大于观测到的单轮
    #   峰值 41k，又显著小于 mimo 原版那个形同虚设的 1_000_000。
    return 65536


def _compaction_threshold() -> Optional[int]:
    """压缩触发阈值。显式配 `QD_COMPACT_THRESHOLD` 优先；否则按窗口比例。

    比例默认 **0.55**（不是 mimo 的 0.92，也不是首版的 0.7）：压缩提示必须
    明显早于"单轮装不下"出现——一轮里还要塞工具返回结果（单次可达数千 token）
    与本轮输出，等到 0.9 再提示已经压不动了。用 `QD_COMPACT_RATIO` 调。
    """
    raw = (os.getenv("QD_COMPACT_THRESHOLD") or "").strip()
    if raw:
        try:
            return max(2048, int(raw))
        except ValueError:
            logger.warning("[ctx] QD_COMPACT_THRESHOLD 非整数，忽略: %r", raw)
    try:
        ratio = float(os.getenv("QD_COMPACT_RATIO", "0.55"))
    except ValueError:
        ratio = 0.55
    return max(2048, int(_context_window() * min(max(ratio, 0.1), 0.95)))


@dataclass
class QDAgentConfig(MimocodeAgentConfig):
    """mimo 原版 MimocodeAgentConfig 之上只加增强项。"""
    system_template: str = field(default_factory=_load_system_template)
    instance_template: str = "Your task: {{task}}"
    # 原版核心 7 件 + compact（模型自主上下文压缩，长程任务关键）
    # + actor（并行子代理，多线索分析；并发受 actor_max_concurrency 约束）
    tools: List[Any] = field(
        default_factory=lambda: [{"tool": n} for n in MIMOCODE_CORE_TOOL_NAMES]
        + [{"tool": "compact"}, {"tool": "actor"}])
    # AntiHackGuard 默认开启（上线安全；误杀时可临时 enabled=False 回退）
    antihack: Any = field(default_factory=lambda: AntiHackConfig(enabled=True))
    # 提智点 D：压缩摘要金融化（保数字/来源/结论/未完事项）
    compaction_summary_prompt: Optional[str] = field(default_factory=lambda: _QD_COMPACT_NOTES)
    # FnToolAdapter 挂载源：显式函数列表（测试/单挂）；None → 扫描 ToolProvider（生产）
    tool_functions: Optional[List[Callable]] = None
    tool_whitelist: Optional[List[str]] = None      # 只挂名字命中的函数工具
    # 闭环②：数字溯源门
    grounding_gate: bool = True
    grounding_max_retries: int = 1
    # 闭环③：交易人工确认闸（interceptor；函数内 confirm 硬闸保留）
    trading_confirm: bool = True
    # 危险面守卫
    danger_guard: bool = True
    # ── 工具分层 ──
    # tiered（默认）：只下发必注入层 + 已激活的按需工具；
    # all：退回"全量下发"（排查/对照用；QD_TOOL_TIER_MODE=all）
    tool_tier_mode: str = field(
        default_factory=lambda: (os.getenv("QD_TOOL_TIER_MODE", "tiered").strip().lower() or "tiered"))
    # 单次会话最多同时激活多少个按需工具（FIFO 淘汰）。
    # 太大 ⇒ 分了等于没分；太小 ⇒ 多线索任务来回换手。默认 12。
    max_active_domain_tools: int = field(
        default_factory=lambda: int(os.getenv("QD_MAX_ACTIVE_TOOLS", "12")))
    # 额外强制进必注入层的工具名（例：高频金融工具，避免每次都要激活一轮）
    extra_core_tools: List[str] = field(default_factory=lambda: [
        n.strip() for n in os.getenv("QD_EXTRA_CORE_TOOLS", "").split(",") if n.strip()])
    # 反向旋钮：把本来必注入的工具**降级**为按需（token 再压缩用）。
    # 默认只降级 actor（≈0.8k tokens/轮）：mimo 上游 MIMOCODE_CORE_TOOL_NAMES
    # 本就**不含** actor/compact（它们是我们自己补挂的可选项），actor 是并行子代理，
    # 单线任务用不到 ⇒ 按需更划算；compact 是长上下文压缩手段，必须常驻。
    # 想再省：QD_ON_DEMAND_TOOLS=actor,task（task≈2.1k，子代理派发，纯问答用不到）。
    on_demand_tools: List[str] = field(default_factory=lambda: [
        n.strip() for n in os.getenv("QD_ON_DEMAND_TOOLS", "actor").split(",") if n.strip()])
    # ── 工具预选（2026-10-01）───────────────────────────────────
    # 分层后模型要靠 search_tools→list_tools→activate_tools 三轮才能摸到一个按需
    # 工具（实测 21 次调用里 13 次是发现轮）。这里在 run 前用 **1 次轻量 LLM** 把
    # "预计要用的工具"从极简目录里点名出来，直接激活进首轮 ⇒ 21 次 → 1+3~4 次。
    # 比静态白名单 QD_EXTRA_CORE_TOOLS 强的点：跨领域自适应（写跑马灯不会白挂
    # 金融工具），而静态白名单换领域就失效。
    # ★ 纯 fail-open：预选失败/选空一律退回 search_tools 老路，绝不丢工具。
    tool_preselect: bool = field(
        default_factory=lambda: os.getenv("QD_TOOL_PRESELECT", "1").strip().lower()
        not in ("0", "false", "no", "off"))
    # 单次预选最多点名几个工具（太大 ⇒ 分了等于没分；太小 ⇒ 还要补发现轮）
    preselect_max_tools: int = field(
        default_factory=lambda: int(os.getenv("QD_PRESELECT_MAX", "8")))
    # 域特征闸门：问题里没有领域特征（股票代码/金融词）就**根本不预选**。
    # 关掉它（QD_PRESELECT_GATE=0）= 每个问题都发一次 3k-token 目录，实测会把
    # 天气类任务拖到 +136%。
    preselect_gate: bool = field(
        default_factory=lambda: os.getenv("QD_PRESELECT_GATE", "1").strip().lower()
        not in ("0", "false", "no", "off"))
    # 闸门词表覆盖（默认见 tool_preselect._DEFAULT_HINTS）
    preselect_hints: Optional[List[str]] = field(default_factory=lambda: (
        [w.strip() for w in os.getenv("QD_PRESELECT_HINTS", "").split(",") if w.strip()]
        or None))
    # ── 上下文预算（2026-10-01 实测修复 / 同日二次修正）────────
    # mimo 的 `compaction_context_window` 默认 **1_000_000**，而我们的模型窗口是
    # 十万级 ⇒ `<context_usage>` 页脚的分母永远是 1M、`context_usage_warn_fraction=0.8`
    # 需要用到 79 万 token 才提示 ⇒ **压缩提示永远不会出现**，长会话只能靠模型
    # 偶然自主调用 compact。实测后果：6 步的选股任务 input 累计到 136k tokens。
    #
    # 【二次修正·两个坑】
    # ① 首版把 compaction 与 wrap_up_hint 的窗口写成**两个不同的 env 名**
    #    （QD_COMPACT_WINDOW / QD_CONTEXT_WINDOW）⇒ 只改一个时两者会打架，
    #    页脚分母和催收阈值对不上。现统一走 `_context_window()` 单一口径。
    # ② `.env` 里的 **OPENAI_MAX_TOKENS=32768 是「单次输出上限」不是上下文窗口**
    #    （agent.py:44 按 max_tokens 消费）。实测反证：选股任务 6 步里单轮 input
    #    已到约 45k 仍请求成功 ⇒ 真实输入窗口必然 > 45k。故此处**不**拿
    #    OPENAI_MAX_TOKENS 当窗口用，而是独立配置项 QD_CONTEXT_WINDOW，
    #    并按「窗口 − 输出预留 − 安全余量」算阈值（比 0.7×window 更靠谱：
    #    压缩提示必须早于"单轮放不下"出现，否则提示出来已经来不及压）。
    compaction_context_window: int = field(default_factory=_context_window)
    compaction_threshold_tokens: Optional[int] = field(
        default_factory=_compaction_threshold)
    # 服务级可用性事实（由启动装配层注入，如 {"search_knowledge": retriever is not None}）
    service_availability: dict = field(default_factory=dict)
    # 上下文感知：MimocodeAgent 原生 <context_usage> + Compact 已覆盖；
    # wrap_up 提示保留但默认关（避免双重催收）。
    wrap_up_hint: bool = False
    wrap_up_hint_fraction: float = 0.9
    wrap_up_hint_context_window: Optional[int] = field(default_factory=_context_window)
    # 闭环①配套：审计事件落盘
    trace_enabled: bool = True
    trace_dir: str = ""                              # 空 → app/agent/traces/
    # 追责入库总开关（v1.1，2026-10-01）：关掉后整条旁路不跑，
    # 主对话行为完全不变（闸门/提取/落库全在 intake 里，fail-open）。
    accountability: bool = field(
        default_factory=lambda: os.getenv("QD_ACCOUNTABILITY", "1").strip().lower()
        not in ("0", "false", "no", "off"))


# ═══════════════════════════════════════════════════════════════
#  工具面预加载（2026-10-01）—— 复刻旧系统「启动时筛一次，消息路径不扫」
# ═══════════════════════════════════════════════════════════════
# 【旧系统怎么做的】`agent_smolagents/agent.py` 模块级调用 `NodeContext(llm).init_tools()`：
#   扫 tools/（import 全部工具模块）+ 能力层注册 + 同功能筛选，结果写进进程内
#   `_SHARED_TOOL_PROVIDER`，注释里写明动机——"启动时筛一次、消息路径不扫"。
# 【迁移后丢了什么】该调用随 nodes.py 退役，改由 `QDAgent._build_tool_registry` 在
#   会话首建时一次性完成。进程内缓存还在，但**首条消息**要承担冷路径：实测
#   `ToolProvider.get_or_build()` 冷启动 3.11s（import 76 个工具模块）+ 能力层注册
#   0.11s。用户等的是这一条消息，不是启动日志。
# 【为什么能力层要幂等】provider 是进程单例，而 `_maybe_register_capabilities` 挂在
#   agent 构造路径上 ⇒ 每个会话都重跑一遍（并重复刷 29 项让位日志）。注册一次即可。
_WARM_STATE: dict = {"done": False, "tools": 0, "capabilities": 0,
                     "elapsed": 0.0, "error": ""}


def _ensure_capabilities(provider) -> int:
    """把 capabilities/ 准入函数注册进 provider（分级第三级），**按 provider 幂等**。

    幂等标记挂在 provider 实例上（`_qd_caps_registered`）而不是全局变量：provider
    本就是进程单例，换 provider（测试里新建的）会重新注册，不会漏注册；同一 provider
    重复构造 agent 则直接返回缓存的注册数。

    ★ 准入清单为空 ⇒ 注册 0 个，零副作用；`QD_CAPABILITIES=0` 可彻底关闭。
    """
    if os.getenv("QD_CAPABILITIES", "1").strip().lower() in ("0", "false", "no", "off"):
        return 0
    if getattr(provider, "_qd_caps_registered", None):
        return int(getattr(provider, "_qd_caps_count", 0) or 0)
    try:
        from app.agent.capabilities import register_capabilities
    except ImportError:
        try:
            from capabilities import register_capabilities
        except ImportError:
            return 0
    try:
        n = int(register_capabilities(provider) or 0)
    except Exception as e:
        # fail-open：能力层是兜底取数面，注册失败只降级不影响主链
        logger.warning("[capabilities] 注册失败(忽略): %s: %s", type(e).__name__, e)
        return 0
    try:
        setattr(provider, "_qd_caps_registered", True)
        setattr(provider, "_qd_caps_count", n)
    except Exception:
        pass
    if n:
        logger.info("[capabilities] 注册 %d 个能力层工具（三级·域内优先）", n)
    return n


def warmup_tool_face() -> dict:
    """启动期预热工具面：把冷启动成本从**首条消息**挪到**启动**。

    复刻旧系统 `agent.py` 的启动期预热（详见本段头部注释）。fail-open：预热失败
    不阻断启动，首条消息会走原懒加载路径（代价是多等几秒，行为不变）。

    Returns:
        {"done","tools","capabilities","elapsed","error"}
    """
    if _WARM_STATE["done"]:
        return dict(_WARM_STATE)
    t0 = _time.perf_counter()
    try:
        try:
            from app.agent.tools.base import ToolProvider
        except ImportError:
            from tools.base import ToolProvider
        provider = ToolProvider.get_or_build()
        n = _ensure_capabilities(provider)
        _WARM_STATE.update({
            "done": True,
            "tools": len(provider.get_functions()),
            "capabilities": n,
            "elapsed": round(_time.perf_counter() - t0, 3),
            "error": "",
        })
        logger.info("[启动] 工具面已预热：%d 个工具（含能力层 %d），耗时 %.2fs",
                    _WARM_STATE["tools"], _WARM_STATE["capabilities"],
                    _WARM_STATE["elapsed"])
    except Exception as e:
        _WARM_STATE.update({"done": False, "error": f"{type(e).__name__}: {e}"})
        logger.warning("[启动] 工具面预热失败（首条消息将懒加载）: %s", e)
    return dict(_WARM_STATE)


# ═══════════════════════════════════════════════════════════════
#  QDAgent
# ═══════════════════════════════════════════════════════════════

class QDAgent(MimocodeAgent):
    """QuantDinger 执行核：mimoagent MimocodeAgent（MiMo Desktop 引擎）+ 金融域增强。"""

    def __init__(self, model, env, *, config_class: Callable = QDAgentConfig, **kwargs):
        super().__init__(model, env, config_class=config_class, **kwargs)
        self.interrupt_switch = False  # 旧 interrupt_switch 语义兼容（message_queue 探针读取）
        self.trace = TraceAdapter(self.config.trace_dir or None) if self.config.trace_enabled else None
        # ── 工具分层台账（在 registry 建好后立刻快照）──
        # _defs_by_name：全量定义（按需激活时从此取）；_core_names：每轮必下发；
        # _activated：按需层"已点名"的工具（FIFO，受 max_active_domain_tools 约束）
        self._defs_by_name: dict = {}
        self._all_defs: list = []
        self._core_names: list = []
        self._activated: list = []
        self.last_preselect_report: dict = {}
        # 压缩阈值兜底：dataclass default_factory 已算好，仅在显式传 None 时补。
        if not getattr(self.config, "compaction_threshold_tokens", None):
            self.config.compaction_threshold_tokens = _compaction_threshold()
        self._sync_tool_tiers()
        if self.config.trading_confirm:
            self.add_action_interceptor(self._trading_confirm_guard)
        if self.config.danger_guard:
            self.add_action_interceptor(self._danger_guard)

    # ── 工具分层 ────────────────────────────────────────────────
    def _sync_tool_tiers(self) -> None:
        """快照工具定义并按域切分必注入层 / 按需层。

        【必注入层的判定】非 FnToolAdapter（mimo 原生 + update_plan）恒必注入；
        FnToolAdapter 看 domain：common（tools/ 顶层）与 meta（元工具：web_search /
        format_result）必注入，子域（finance/knowledge…）按需。
        """
        defs = {}
        for d in self.tool_registry.get_function_definitions():
            name = (d.get("function") or {}).get("name")
            if name:
                defs[name] = d
        self._defs_by_name = defs
        self._all_defs = [defs[n] for n in self.tool_registry.list_tools() if n in defs]
        core, on_demand = [], []
        forced_on_demand = set(self.config.on_demand_tools or ())
        for name in self.tool_registry.list_tools():
            if name in forced_on_demand:
                on_demand.append(name)
                continue
            if name in (self.config.extra_core_tools or []):
                core.append(name)
                continue
            try:
                tool = self.tool_registry.get(name)
            except Exception:
                core.append(name)
                continue
            if not isinstance(tool, FnToolAdapter):
                core.append(name)          # mimo 原生 / update_plan
            elif getattr(tool, "domain", "common") in CORE_TOOL_DOMAINS:
                core.append(name)
            else:
                on_demand.append(name)
        self._core_names = core
        self._on_demand_names = on_demand

    def activate_tools(self, names: List[str]) -> dict:
        """激活按需层工具（模型经 activate_tools / search_tools 调用）。

        返回 {"activated","already","unknown","active_total","cap"}。
        超容量时按 FIFO 淘汰最早的激活项。
        """
        cap = max(1, int(self.config.max_active_domain_tools))
        activated, already, unknown = [], [], []
        for n in (names or []):
            n = str(n).strip()
            if not n:
                continue
            if n not in self._defs_by_name:
                unknown.append(n)
            elif n in self._core_names or n in self._activated:
                already.append(n)
            else:
                self._activated.append(n)
                activated.append(n)
        while len(self._activated) > cap:
            self._activated.pop(0)         # FIFO：先激活的先淘汰
        return {
            "activated": activated, "already": already, "unknown": unknown,
            "active_total": len(self._activated), "cap": cap,
        }

    def _tool_domain(self, name: str) -> str:
        """工具的来源域（common / finance / knowledge / capability …）。"""
        try:
            return getattr(self.tool_registry.get(name), "domain", "") or ""
        except Exception:
            return ""

    def get_model_query_kwargs(self) -> dict:
        """每轮只下发「必注入层 + 已激活」的工具定义（原版是全量下发）。"""
        return {"tools": self._active_tool_definitions(), "tool_choice": "auto"}

    def _active_tool_definitions(self) -> list:
        if self.config.tool_tier_mode != "tiered":
            return self._all_defs
        names = list(self._core_names)
        names += [n for n in self._activated if n not in self._core_names]
        return [self._defs_by_name[n] for n in names if n in self._defs_by_name]

    def active_tool_names(self) -> list:
        return [(d.get("function") or {}).get("name") for d in self._active_tool_definitions()]

    # ── 工具预选（2026-10-01）───────────────────────────────────
    # prefetch 拼装格式：`块\n\n[用户问题]\n{原始消息}`（见 QDAgentService._prefetch）
    _USER_QUERY_MARK = "[用户问题]"

    def _preselect_query(self, task) -> str:
        """取预选应看的**用户原始问题**，剥掉 prefetch 注入的记忆/RAG/技能块。

        【为什么必须剥·2026-10-01 实测】
        直接用增强后的 task 做预选，模型会被注入的历史记忆带偏：测天气任务时，
        记忆里存着前几轮的个股分析，于是预选的 goal 写成了"在用户给出单只 A 股
        代码或名称后…形成综合诊断"，据此点名 7 个股票工具 ⇒ 该任务 token +136%。
        同一份代码换个上下文就换一批工具——看起来像"模型随机/不稳定"，
        实为**输入污染**。预选要判的是"用户这一轮要什么"，不是"历史上聊过什么"。

        三级来源：① service 挂在 agent 上的 `last_user_query`（最准）
                  ② task 里 `[用户问题]` 之后的部分（prefetch 有注入时）
                  ③ 整个 task（无注入时它本就是原始消息）
        """
        raw = task if isinstance(task, str) else str(task or "")
        # ① service 显式挂的原始问题
        orig = str(getattr(self, "last_user_query", "") or "").strip()
        if orig:
            return orig
        # ② 从 prefetch 拼装结构里截出用户问题段
        mark = self._USER_QUERY_MARK
        pos = raw.rfind(mark)
        if pos >= 0:
            return raw[pos + len(mark):].strip()
        # ③ 无注入：task 即原始消息
        return raw.strip()

    def _maybe_preselect_tools(self, task: str | dict) -> dict:
        """run 前用 **1 次轻量 LLM** 把「预计要用的工具」直接激活进首轮。

        替代模型自己 `search_tools`→`list_tools`→`activate_tools` 的三轮发现
        （实测一条选股任务 21 次调用里 13 次是发现轮，62%）。

        【为什么不是静态白名单 `QD_EXTRA_CORE_TOOLS`】
        静态白名单换领域就失效：把金融高频工具写进白名单，遇到"写个跑马灯"
        这种任务纯属白挂（8.7k tokens/轮照付），而预选是**每次按问题**定的，
        跨领域自适应。（静态白名单仍保留，用于"无论如何都要常驻"的极少数工具。）

        ★ 纯 fail-open：任何异常/空选都退回 search_tools 老路，**绝不丢工具**；
        ★ 只做加法：不清空已激活项（多轮会话里上一轮的工具还可能用得上）。

        Returns:
            {"enabled","selected","reason","catalog_size","unknown","raw"}
        """
        report = {"enabled": bool(getattr(self.config, "tool_preselect", True)),
                  "selected": [], "reason": "", "catalog_size": 0, "unknown": [], "raw": ""}
        if not report["enabled"]:
            report["reason"] = "已关闭(QD_TOOL_PRESELECT=0)"
            return report
        if self.config.tool_tier_mode != "tiered":
            report["reason"] = "非分层模式(全量已下发)"
            return report
        on_demand = list(getattr(self, "_on_demand_names", []) or [])
        if not on_demand:
            report["reason"] = "无按需工具"
            return report

        query = self._preselect_query(task)
        report["query"] = query[:200]

        # 三级分级：**域内优先于能力层**。
        # 能力层（capabilities/ 准入函数）是兜底：目录里排在域内之后并明确标注
        # "仅当域内无覆盖时才选"；即便模型仍选了它，lint 的 R0 也会让位裁掉。
        # ★ 提到闸门之前：闸门拦下时也要有候选集可供**域兜底**（纯名单计算，零成本）。
        cap_domain = capability_domain()
        cap_names = [n for n in on_demand if self._tool_domain(n) == cap_domain]
        dom_names = [n for n in on_demand if n not in set(cap_names)]
        report["capability_candidates"] = len(cap_names)

        # ★ 域特征闸门：纯字符串匹配，成本≈0。命中才值得发那次 3k-token 的目录。
        #   （没有它，天气类问题会被硬塞 8 个股票工具，token +136%。详见
        #    tool_preselect.has_domain_hint 的注释。）设 QD_PRESELECT_GATE=0 可关闭闸门。
        if self.config.preselect_gate and not has_domain_hint(query, self.config.preselect_hints):
            # 闸门只是「省不省那次 LLM」的成本闸，**不是**"这任务不需要工具"的判决：
            # 关键词没覆盖到的金融问法（如"帮我看看大盘"）同样需要工具面，交给收口兜底。
            report["reason"] = "无领域特征，跳过预选(闸门)"
            return self._finish_preselect(report, query, dom_names)

        catalog, names = build_catalog_grouped(
            self._defs_by_name, dom_names, cap_names)
        report["catalog_size"] = len(names)
        if not names:
            report["reason"] = "目录为空"
            return report

        max_tools = max(1, int(self.config.preselect_max_tools))
        # ── 闭环消费端（2026-10-01）：盘后产出的权重在这里影响"选哪些工具" ──
        # 权重表此前是断头路（无人读取）；本处 + 技能选择（qd_service）是它的两个
        # 消费点。提示失败一律 fail-open（空提示 = 与没有历史数据时行为一致）。
        hist_hint = ""
        try:
            try:
                from app.agent.chain.weight_hints import hint_text
            except ImportError:
                from chain.weight_hints import hint_text
            hist_hint = hint_text(names) or ""
        except Exception as e:
            logger.debug("[preselect] 权重提示跳过(fail-open): %s: %s",
                         type(e).__name__, e)
        report["weight_hint"] = hist_hint[:400]
        try:
            # 纯文本回合（不带 tools），与 mimo 压缩摘要同范式：
            # 防止模型在"选工具"这一步又去调工具，死循环。
            resp = self.model.query(build_preselect_messages(
                query, catalog, max_tools, already=self._core_names,
                history_hint=hist_hint))
            text = self._text_of(resp)
            report["raw"] = (text or "")[:500]
            plan = parse_plan(text, names, max_tools)
        except Exception as e:
            report["reason"] = f"预选调用失败(fail-open): {type(e).__name__}: {e}"
            logger.warning("[preselect] %s", report["reason"])
            return self._finish_preselect(report, query, names)

        report["goal"] = plan.get("goal", "")
        report["deliverables"] = plan.get("deliverables", [])
        selected = plan.get("tools") or []
        if not selected:
            report["reason"] = "模型未点名(可能是纯问答)"
            return self._finish_preselect(report, query, names)

        # ── 确定性校正（不调 LLM）：LLM 选得对不对由这一层判 ──────────
        # 旧系统 plan_linter 的立身之本：能被词典/索引查出来的不劳 LLM，
        # LLM 只做确定性查不了的语义匹配。没有这一层，预选质量全靠模型运气
        # （首版实测：选对省 25%，选错费 136%，三次同任务 +21%/−25%/+31%）。
        try:
            lint = lint_selection(
                query, selected,
                available_names=names,
                index=get_tool_index(self._defs_by_name),
                plan_text=" ".join([plan.get("goal", "")] + list(plan.get("deliverables") or [])),
                protected_names=(self.config.extra_core_tools or []),
                parsed=bool(plan.get("parsed")),
                capability_names=cap_names,
                domain_names=dom_names,
            )
            report["lint"] = lint.to_trace()
            report["llm_selected"] = list(selected)
            selected = apply_lint(selected, lint, capability_names=cap_names)
        except Exception as e:
            # lint 出错不阻断：原样放行（fail-open 一致性）
            logger.warning("[preselect] lint 异常(fail-open): %s", e)
            report["lint_error"] = f"{type(e).__name__}: {e}"

        if not selected:
            report["reason"] = "lint 判定整体误判，已作废(回退 search_tools)"
            return self._finish_preselect(report, query, names)
        act = self.activate_tools(selected)
        report["selected"] = list(act.get("activated", [])) + list(act.get("already", []))
        report["unknown"] = list(act.get("unknown", []))
        report["reason"] = "ok"
        return self._finish_preselect(report, query, names)

    @staticmethod
    def _empty_route(report: dict) -> str:
        """把「为什么走到空工具面」归类成稳定标签（供 counters 分组，便于事后归因）。

        标签稳定很重要：如果直接用 report["reason"] 原文，每次改文案统计就断了
        ⇒ 这里只认 reason 里的**关键字**，认不出一律归 "unknown"（不猜）。
        """
        reason = str(report.get("reason") or "")
        low = reason.lower()
        if "闸门" in reason or "gate" in low:
            return "gate"                    # 关键词没覆盖到 ⇒ 该放宽 hints
        if "未点名" in reason or "no_pick" in low:
            return "no_pick"                 # 模型觉得是纯问答
        if "作废" in reason or "void" in low:
            return "lint_void"               # 整体误判 ⇒ 看 lint 规则是否过严
        if "失败" in reason or "fail" in low:
            return "call_error"              # LLM 调用失败 ⇒ 基础设施问题，先修这个
        if "跳过" in reason or "skip" in low:
            return "skipped"
        return "unknown"

    def _finish_preselect(self, report: dict, query: str, names: List[str]) -> dict:
        """预选收口：**空手而归时不许落回裸必选层**，按域词典补核心子集。
        【旧系统 P0 教训】`agents/task_agent.py` 断言：域选择失效属退化，绝不能降成
        "只有通用工具"的裸沙箱（那是 2026-09-19 故障的直接机制），并配了三层兜底网。
        本系统预选空手时工具面只剩必选层，模型得靠 `search_tools` 自救一轮。

        【为什么不是整域注入】旧系统是 CodeAgent（注入函数名 + Returns 契约文本）；
        本系统 tool-calling 下整域 ≈ 15k tokens/轮，抄不起 ⇒ 只补词典核心子集。

        【克制】纯字符串匹配（零 LLM 成本）且与 lint 同源；命中不了就一个不补，
        宁可让模型走 search_tools，也不硬塞（"天气"不会命中任何域）。

        【第三层兜底 = 观测，2026-10-01】兜底也没补上时，旧系统的处置是
        `plan_domain_empty_degraded`（warning + trace）。本系统改成先**量化**：
        走这里 ≠ 一定是缺陷 —— 天气/写代码这类本来就不该给工具面，只有当
        空域占比真的抬头时才值得救。所以先计数 + 告警，判据与升级阈值写死在
        `tools/preselect_stats.py`（含 >2% 补词典 / >5% 放宽闸门 / >10% 查 prompt）。
        ★ 日志 keyword：`preselect-empty-face`（观察期请 grep 这个而不是肉眼盯日志）。
        """
        if report.get("selected"):
            try:
                from app.agent.tools.preselect_stats import record_selected
                record_selected("fallback" if report.get("fallback") else "ok")
            except Exception:
                pass
            return report
        if not names:
            # 可用候选本身就是空的 ⇒ 不是预选的锅，不计入分子也不计入分母
            return report
        try:
            fb, doms = fallback_domain_tools(query, names)
        except Exception as e:
            logger.warning("[preselect] 域兜底异常(fail-open): %s", e)
            try:
                from app.agent.tools.preselect_stats import record_empty_face
                record_empty_face(query, "fallback_error:%s" % type(e).__name__, len(names))
            except Exception:
                pass
            return report
        if not fb:
            # 第三层兜底：不静默 —— 计数 + 可查询（见 preselect_stats.report()）
            try:
                from app.agent.tools.preselect_stats import record_empty_face
                _pr = record_empty_face(query, self._empty_route(report), len(names))
                report["empty_face"] = {"rate": _pr["rate"], "sample": _pr["sample"]}
            except Exception:
                pass
            return report
        act = self.activate_tools(fb)
        got = list(act.get("activated", [])) + list(act.get("already", []))
        if not got:
            try:
                from app.agent.tools.preselect_stats import record_empty_face
                record_empty_face(query, "fallback_activate_miss", len(names))
            except Exception:
                pass
            return report
        report["fallback"] = {"domains": doms, "tools": got}
        report["selected"] = got
        report["reason"] = "%s → 域兜底补 %d 个（%s）" % (
            report.get("reason") or "预选为空", len(got), ",".join(doms))
        logger.info("[preselect] 预选空手，域兜底激活 %d 个（域=%s）: %s",
                    len(got), doms, got)
        return report

    # ── 工具注册：mimo 原版 lowercase 目录 + FnToolAdapter 函数工具 ─────
    def _build_tool_registry(self) -> MimocodeToolRegistry:
        registry = super()._build_tool_registry()  # bash/read/write/edit/grep/glob/task/compact
        for fn, domain in self._collect_tool_functions():
            tool = FnToolAdapter(fn, domain=domain)
            if self.config.tool_whitelist and tool.name not in self.config.tool_whitelist:
                continue
            if tool.name in registry.list_tools():
                continue                   # 后注册者不覆盖（同名让位：先注册者胜）
            registry.register(tool)
        # 提智点 B：update_plan 计划清单工具（codex 目录，跨目录注册）
        try:
            from mimoagent.tools.codex.update_plan import UpdatePlanTool
            if "update_plan" not in registry.list_tools():
                registry.register(UpdatePlanTool({}))
        except Exception:
            pass
        return registry

    def _collect_tool_functions(self) -> list:
        """收集 (func, domain) —— 显式挂靠优先，否则扫描 provider（含**元工具**）。

        【易错点·2026-10-01 事故】web_search / format_result 在 tools/base.py 的
        `_MUST_HAVE` 名单里 ⇒ scan_directory **跳过它们** ⇒ provider.get_functions()
        里**没有** web_search。旧系统靠 provider.get_meta() 单独取用；迁移到 mimo 后
        只取了 get_functions() ⇒ **模型根本没有 web_search 可调**，而 system prompt
        却硬性要求"实时信息必须调用 web_search" ⇒ 天气/新闻类任务模型撞墙后只能瞎答。
        故元工具必须一并收进来，并标 domain="meta"（必注入层）。
        """
        explicit = getattr(self.config, "tool_functions", None)
        if explicit:
            return [(fn, "common") for fn in explicit]
        try:
            from app.agent.tools.base import ToolProvider
        except ImportError:
            from tools.base import ToolProvider
        provider = ToolProvider.get_or_build()
        self._maybe_register_capabilities(provider)
        out = [(f, provider.get_domain(n)) for n, f in provider.get_functions().items()]
        out += [(f, "meta") for f in (provider.get_meta_functions() or {}).values()]
        return out

    def _maybe_register_capabilities(self, provider) -> int:
        """把 capabilities/ 准入函数注册进 provider（分级**第三级**，fail-open）。

        【为什么要补这个调用点·2026-10-01】
        `capabilities/loader.register_capabilities` 写好了却**没有任何调用方** ⇒
        能力层长期空转（叠加 admission.json 本就不在仓库里，等于双重死代码）。
        分级口径要求"域内工具 + 能力层工具"一起筛选，能力层必须先能注册：
        注册后 domain=capability ∉ CORE_TOOL_DOMAINS ⇒ 自动落在按需层，由预选
        或 search_tools 激活，且 lint R0 保证它让位于域内同功能工具。

        ★ 准入清单为空 ⇒ 注册 0 个，零副作用；`QD_CAPABILITIES=0` 可彻底关闭。

        【幂等·2026-10-01】本方法位于 agent 构造路径上，每个会话都会跑；实际注册
        动作收敛到模块级 `_ensure_capabilities`（按 provider 标记只做一次）。
        """
        return _ensure_capabilities(provider)

    # ── 闭环③：交易人工确认闸 ──────────────────────────────────
    def _trading_confirm_guard(self, action: dict) -> Optional[dict]:
        tool_name = action.get("tool", "")
        try:
            tool = self.tool_registry.get(tool_name)
        except Exception:
            return None
        if not isinstance(tool, FnToolAdapter) or not tool.requires_confirm:
            return None
        if (action.get("params") or {}).get("confirm") is True:
            return None  # 已确认，放行（函数内硬闸仍校验）
        return ToolOutput(
            output=(
                "requires_confirmation: 该操作涉及真实交易动作，未获用户确认。"
                "请先向用户复述标的/数量/方向并获得明确同意，"
                "再以 confirm=true 重新调用。"
            ),
            success=True,
            metadata={"tool": tool_name, "requires_confirmation": True},
        ).to_dict()

    # ── 危险面守卫 ─────────────────────────────────────────────
    def _danger_guard(self, action: dict) -> Optional[dict]:
        params = action.get("params") or {}
        tool_name = action.get("tool", "")
        # 路径类工具：凭据路径 deny
        for key in ("path", "file", "file_path", "relative_path", "target"):
            val = params.get(key)
            if isinstance(val, str) and _DENY_PATH_RE.search(val):
                return self._deny(tool_name, f"敏感路径拒绝访问: {val}")
        # bash/命令类：破坏性命令 + 凭据路径双重 deny
        for key in ("command", "cmd"):
            val = params.get(key)
            if isinstance(val, str) and (_DENY_CMD_RE.search(val) or _DENY_PATH_RE.search(val)):
                return self._deny(tool_name, f"危险命令拒绝执行: {val[:120]}")
        return None

    @staticmethod
    def _deny(tool_name: str, reason: str) -> dict:
        return ToolOutput(
            output=f"Error: 安全策略拒绝 —— {reason}",
            success=False,
            metadata={"tool": tool_name, "danger_denied": True},
        ).to_dict()

    # ── 事件钩子：审计轨迹（闭环②）────────────────────────────
    def execute_action(self, action: dict) -> dict:
        """拦截器之前先跑中断探针（旧 _interrupt_probe 语义）。"""
        for probe in _hooks_now().get("probes") or ():
            try:
                probe()
            except Exception as e:
                if type(e).__name__ == "_UserInterruptError":
                    raise _ProbeInterrupt(str(e) or "user interrupted") from e
                raise
        # 分层 fail-open（2026-10-01）：模型可能直接点名了"已注册但本轮未激活"的按需
        # 工具（上一轮文档里见过名字/网关透传）。原版会抛 ToolException(Unknown tool)
        # 让它卡住重试；这里就地激活放行——多花一步 schema，好过整轮死胡同。
        name = action.get("tool", "")
        if (self.config.tool_tier_mode == "tiered" and name in self._defs_by_name
                and name not in self._core_names and name not in self._activated):
            self.activate_tools([name])
        t0 = _time.perf_counter()
        try:
            result = super().execute_action(action)
        except Exception as e:
            self._trace_tool(action, None, (_time.perf_counter() - t0) * 1000, error=str(e))
            raise
        self._trace_tool(action, result, (_time.perf_counter() - t0) * 1000)
        return result

    def _tool_map_text(self) -> str:
        """提智点 C：分层工具地图（必注入层列名，按需层只给域+数量以省 token）。"""
        try:
            groups: dict = {}
            for name in getattr(self, "_on_demand_names", []):
                tool = self.tool_registry.get(name)
                domain = getattr(tool, "domain", "") or "other"
                groups.setdefault(domain, []).append(name)
            lines = ["[必注入·每轮可直接调用]",
                     "  " + ", ".join(self._core_names)]
            # ★ 已激活段（2026-10-01 补）：工具预选把按需工具提前激活进本轮 schema 后，
            #   必须**明确告诉模型"这些已可直接调用"**。首版没写这一段，地图里按需层
            #   仍一律标注"需先 search_tools 检索、activate_tools 激活" ⇒ 模型照旧走
            #   三轮发现，预选白做（实测 stock_screen 发现轮一次没少、token 反而 +21%）。
            activated = [n for n in getattr(self, "_activated", [])
                         if n not in self._core_names]
            if activated:
                lines += ["[已激活·本轮可直接调用，**不要再** search_tools/activate_tools]",
                          "  " + ", ".join(activated)]
            lines.append("[按需·需先 search_tools 检索、activate_tools 激活]")
            if groups:
                cap_domain = capability_domain()
                for dom, names in sorted(groups.items()):
                    # 域内工具优先于能力层：把优先级写进地图，别指望模型自己猜
                    note = ("（低优先级：域内工具无覆盖时才用）" if dom == cap_domain else "")
                    lines.append(f"  - {dom}: {len(names)} 个{note} —— 例："
                                 f"{', '.join(sorted(names)[:3])}")
            else:
                lines.append("  （本会话无按需层工具）")
            return "\n".join(lines)
        except Exception:
            return ""

    def _tool_availability_text(self) -> str:
        """工具可用性（启动期探测 + 服务级事实）——写进系统提示，防模型被放鸽子。"""
        try:
            try:
                from app.agent.tools.availability import probe_tools
            except ImportError:
                from tools.availability import probe_tools
            info = dict(probe_tools())
        except Exception:
            info = {}
        for k, v in (self.config.service_availability or {}).items():
            info[k] = {"available": bool(v),
                       "reason": "服务已装配" if v else "服务未装配（依赖缺失，调用必失败）"}
        if not info:
            return "（未探测）"
        return "\n".join(
            f"- {n}: {'可用' if d.get('available') else '不可用'} —— {d.get('reason', '')}"
            for n, d in sorted(info.items()))

    def _trace_tool(self, action: dict, result, elapsed_ms: float, error: str = None) -> None:
        """工具调用落 qd_traces（闭环② DB 链，TraceCollector）。"""
        collector = getattr(self, "_collector", None)
        if collector is None:
            return
        try:
            out = result.get("output") if isinstance(result, dict) else result
            err = error or (None if not isinstance(result, dict) or result.get("success", True)
                            else str(out)[:200])
            collector.on_tool_call(action.get("tool", ""), action.get("params") or {},
                                   out, elapsed_ms, error=err)
        except Exception:
            pass

    def after_step(self) -> None:
        super().after_step()
        if self.trace is not None:
            try:
                self.trace.on_step(self, self._steps_taken)
            except Exception:
                pass
        self._emit_action_step()

    def _emit_action_step(self) -> None:
        ev_cb = _hooks_now().get("event_cb")
        if ev_cb is None:
            return
        try:
            tools: list = []
            code_action = ""
            for msg in reversed(self.messages):
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls") or []:
                        fn = (tc or {}).get("function") or {}
                        if fn.get("name"):
                            tools.append(fn["name"])
                    code_action = str(msg.get("content") or "")[:200]
                    break
            err = "" if not (self.tool_call_errors and self.tool_call_errors[-1]) else "tool call format error"
            ev_cb({
                "kind": "action_step",
                "step_number": self._steps_taken,
                "tools": tools,
                "code_action": code_action,
                "error": err or None,
            })
        except Exception:
            pass

    def step(self) -> dict | None:
        # 协作取消检查（旧 _cancel_check 语义）：每步开头跑 step_checks，
        # 命中即抛错中止——异常类名 _UserInterruptError 由 message_queue 名字匹配收口。
        for check in _hooks_now().get("step_checks") or ():
            check(None)
        try:
            return super().step()
        except Exception as e:
            if self.trace is not None:
                try:
                    self.trace.on_error(self._steps_taken, e)
                except Exception:
                    pass
            raise

    # ── run：数字溯源门（闭环②）+ 轨迹 ─────────────────────────
    def run(self, task: str | dict, **kwargs) -> tuple[str, str]:
        hooks = _hooks_now()
        if hooks:
            hooks["active_agent"] = self
        try:
            return self._run_inner(task, **kwargs)
        finally:
            if hooks and hooks.get("active_agent") is self:
                hooks.pop("active_agent", None)

    def _run_inner(self, task: str | dict, **kwargs) -> tuple[str, str]:
        # 修复（2026-10-01 用户实测：带上下文的会话空结果静默退出）：
        # mimo 的 step_limit 是「单次 run 预算」，而我们会话复用同一 agent，
        # _steps_taken 跨请求累积 → 轮次多了新消息一进来就 LimitsExceeded()
        # 且 str(e)=="" → 空结果退出。每轮 run 前归零，预算语义改为按请求。
        self._steps_taken = 0
        self.tool_call_errors = []
        # 工具预选：必须在 tool_map / tool_availability 渲染**之前**跑，
        # 否则地图里看不到刚被点名激活的工具，模型会以为还得先去 search。
        self.last_preselect_report = self._maybe_preselect_tools(task)
        if self.trace is not None:
            try:
                self.trace.emit("tool_preselect", **self.last_preselect_report)
            except Exception:
                pass
        # 上下文感知：动态上下文注入 system_template（{{ now }} + {{ tool_map }}）
        self.extra_template_vars["now"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.extra_template_vars["tool_map"] = self._tool_map_text()
        self.extra_template_vars["tool_availability"] = self._tool_availability_text()
        self._collector = self._make_collector(task)
        if self.trace is not None:
            try:
                self.trace.run_start(self, task if isinstance(task, str) else str(task))
            except Exception:
                pass
        # mimoagent 原版契约：run() → (status, message)
        status, message = super().run(task, **kwargs)
        # 空结果兜底（2026-10-01）：LimitsExceeded 空文案（步数耗尽）/ reasoning-only
        # 空回复 / 网关截断，分别处理：截断给可读文案；空回复轻推一次补结论（带独立预算）。
        msg_text = (message or "").strip()
        if status == "LimitsExceeded" and msg_text == "":
            status, message = "truncated", (
                "（本轮达到步数上限被截断——请把问题拆小分步重试）")
        elif msg_text in ("", "Empty assistant response"):
            self._steps_taken = 0  # 轻推轮给独立预算，不跟主轮抢
            status, message = super().run("请用文字直接给出本轮结论（不要再调用工具）。")
            if not (message or "").strip():
                status, message = "empty", "（模型未返回文字内容，请重试或换个问法）"

        # ── TODO(结果格式化)·2026-10-01 **已摘除**，原因：延迟 ========
        # 这里原先挂 `formatters/`（FinanceFormatter 等）。实测它每次答复后**多发一整轮
        # LLM 生成**：稳态 11.7s、首次 21.9s（对比：resolver 95ms、weight_hints 命中缓存 0ms）
        # ⇒ 它一个人占了新增延迟的 99%。而它只是"整理排版"，失败可用原文，不值得用
        #   一轮生成去换。2026-10-01 用户裁定**去掉**（旧实现见 del/agent_formatters_20261001/）。
        # 将来若要捡回来，先满足这三条再接：
        #   ① 必须有超时（全局 llm 实例默认 timeout=180s，无兜底）；
        #   ② 必须用轻量模型 + max_tokens 上限，不能走主模型；
        #   ③ 必须是**可选的**（用户显式要报告时才格式化），不能默认每次都跑。
        # 搜索本标记可定位。
        # ── TODO(阶段验收)·2026-10-01 标记，暂不实现 ──────────────────────
        # 旧系统在 execute↔finalize 之间插 verify 节点做**阶段验收**（每阶段打分、
        # 部分达标保留、不达标回炉一次），随 nodes.py 退役消失。
        # 现状：grounding gate 只覆盖"数字溯源"这一条纪律，阶段粒度的是否达标无人判。
        # 用户裁定（2026-10-01）：**暂不实现**——当前未观察到偏差较大的结果，
        # 且阶段验收的价值主要在"多轮大循环的大项目"里（单轮短任务加了只增成本）。
        # 将来要做的落点就在这里（formatter 之后、grounding 之前），判据与埋点见
        # docs/AGENT_DESIGN_v2_20261001.md §阶段验收（暂缓）。搜索本标记即可定位。
        if self.config.grounding_gate:
            corpus = self._observation_corpus()
            if corpus.strip():
                guide = self._grounding_violation(message, corpus)
                retries = 0
                while guide is not None and retries < self.config.grounding_max_retries:
                    retries += 1
                    self._steps_taken = 0  # 溯源重写轮独立预算（否则主轮耗尽后重试瞬死）
                    status, message = super().run(
                        f"【数字溯源拦截】{guide}\n"
                        "请只依据本轮工具输出中的数字重写结论；无法溯源的数字改为"
                        "\"未获取\"并说明缺失口径。"
                    )
                    guide = self._grounding_violation(message, corpus)
                if guide is not None:
                    status = "grounding_failed"

        self._finish_collector(message, status)
        if self.trace is not None:
            try:
                self.trace.run_finish(self, message, status)
            except Exception:
                pass
        return status, message

    # ── qd_traces DB 链（闭环②：TraceCollector → chain.store）────
    def _make_collector(self, task):
        try:
            try:
                from app.agent.trace_collector import TraceCollector
            except ImportError:
                from trace_collector import TraceCollector
            sid = getattr(_HOOKS_LOCAL, "session_id", None) or "default"
            query = task if isinstance(task, str) else str(task)
            # 2026-10-01：task 是 **prefetch 加工后**的文本（记忆/技能/RAG 块 + 用户问题），
            # 直接落库会让 qd_agent_traces.user_query 变成一坨注入块，复盘时看不到
            # 用户到底问了什么（实测首条修复记录就是 "[相关技能：auto_stock-analyze]…"）。
            # 这里剥掉注入前缀，只留 [用户问题] 之后的原文。
            marker = "\n[用户问题]\n"
            if marker in query:
                query = query.split(marker, 1)[1]
            return TraceCollector(session_id=sid, user_query=query[:2000])
        except Exception:
            return None

    def _finish_collector(self, message: str, status: str) -> None:
        collector = getattr(self, "_collector", None)
        self._collector = None
        if collector is None:
            return
        root_id = None
        stats = None
        try:
            stats = getattr(self.model, "token_stats", None)
            collector.on_agent_finish(
                final_answer=message or "",
                total_steps=self._steps_taken,
                total_tokens=int(getattr(stats, "total_tokens", 0) or 0),
                model=str(getattr(getattr(self.model, "config", None), "model_name", "") or ""),
            )
            root_id = collector.flush()
        except Exception:
            pass
        self._intake_accountability(collector, message or "", root_id, stats)

    def _intake_accountability(self, collector, answer: str,
                               root_id: Optional[int], stats) -> None:
        """追责入库（旁路·v1.1）：闸门 → 抽 Claim → 落 decisions/claims。

        【为什么挂在这里】这是**唯一**同时拿得到三样东西的位置：用户原话
        （collector.user_query，已被剥掉 prefetch 注入块）、最终答复、以及
        `trace_root_id`（flush 返回的 qd_agent_traces.id）。

        【为什么必须 fail-open】追责是慢调旁路，绝不能因为它挂了让用户收不到回复：
        整段包 try/except，且 `QD_ACCOUNTABILITY=0` 可整体关闭。
        """
        if not getattr(self.config, "accountability", True):
            return
        self.last_accountability: dict = {}
        try:
            try:
                from app.agent.chain.intake import record_decision
            except ImportError:
                from chain.intake import record_decision
            self.last_accountability = record_decision(
                user_query=str(getattr(collector, "user_query", "") or ""),
                answer=answer,
                session_id=str(getattr(collector, "session_id", "") or ""),
                trace_root_id=root_id,
                run_id=str(getattr(getattr(self, "trace", None), "run_id", "") or ""),
                model=str(getattr(getattr(self.model, "config", None), "model_name", "") or ""),
                total_tokens=int(getattr(stats, "total_tokens", 0) or 0),
                latency_ms=int(getattr(getattr(collector, "_root", None),
                                       "elapsed_ms", 0) or 0),
            )
        except Exception as e:
            logger.debug("[accountability] 入库跳过(fail-open): %s: %s",
                         type(e).__name__, e)

    def trace_prefetch(self, report: dict) -> None:
        """预取留痕（2026-10-01）：把"本轮注入了哪些上下文块"落 trace。

        【解决什么】此前调"为什么它会这么说"只能靠猜 —— 记忆近史/技能正文/RAG 命中
        到底注没注、注了多少字、为什么被闸门拦掉，全都无痕。加这一条事件后，
        合谋类问题（被旧话题带偏、技能误命中）从小时级排查降到分钟级。
        """
        self.last_prefetch_report = report or {}
        if self.trace is None:
            return
        try:
            self.trace.emit("prefetch", **self.last_prefetch_report)
        except Exception:
            pass

    # ── _maybe_format_result（formatters/ 接线）已于 2026-10-01 摘除 ────
    # 原因：每次答复多发一整轮 LLM（实测稳态 11.7s），用户裁定去掉。
    # 实现归档在 del/agent_formatters_20261001/；恢复前先读 qd_agent.py 里那个
    #   TODO(结果格式化) 标记的三条前置条件（超时 / 轻量模型 / 可选触发）。
    def _observation_corpus(self) -> str:
        """闭环②语料：本轮全部工具观察值（取代旧 executor.state 语料源）。"""
        parts = []
        for msg in self.messages:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
        return "\n".join(parts)

    @staticmethod
    def _grounding_violation(text: str, corpus: str) -> Optional[str]:
        try:
            from app.agent.utils.grounding import check_grounding
        except ImportError:
            try:
                from utils.grounding import check_grounding
            except ImportError:
                return None
        try:
            ok, guide = check_grounding(text, corpus)
            return None if ok else guide
        except Exception:
            return None
