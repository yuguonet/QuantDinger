# -*- coding: utf-8 -*-
"""
任务型 Agent — 统一多阶段

流程：
  user input
    -> 负面反馈检测
    -> plan: LLM 选择技能、划分阶段（不选具体工具）
    -> 统一多阶段循环：
       - skill 阶段：加载技能指令 + 工具 → CodeAgent 执行
       - execute 阶段：通用工具 → CodeAgent 执行
       - direct 阶段：LLM 直接回答
    -> 规则 eval（结果非空 → passed）
    -> response

设计要点：
  - ToolProvider 统一管理所有工具（本地扫描 + skill 动态注册）
  - _plan() 只看技能列表，工具在执行阶段通过 ToolProvider 注入
  - 必选工具（list_tools/search_tools/format_result/web_search）通过 smolagents tools=[] 注入
  - 统一多阶段：所有任务走同一套循环，无单阶段/多阶段分支
  - eval 用规则判断，不调 LLM，省 token
"""
from __future__ import annotations

import inspect
import logging
import os
import re
import time
from difflib import SequenceMatcher
from typing import Dict, List, Optional

import yaml

from agents.base import AgentBase, AgentResponse
from llm.base import ChatMessage, LLMBase
from memory.base import MemoryBase
from utils.prescan import prescan_skill_funcs, prescan_tools  # 预扫（2026-09-12 Q4）
from rag.retriever import Retriever
from smolagents import Tool as SmolToolBase
from smolagents.local_python_executor import FinalAnswerException
from utils.json_parser import safe_parse_json
from utils.tracing import AgentTraceRecorder, llm_response_to_dict

# ═══════════════════════════════════════════════════════════════
#  框架内部模块（infra/）——标准 import，非插件
# ═══════════════════════════════════════════════════════════════
from infra.resilient_parse import apply as _apply_resilient_parse, resilient_parse_code_blobs
from infra.breaker import ToolCircuitBreaker
from infra.guided_executor import GuidedCPythonExecutor
from infra.staging import stage_scope_vars

# 使用 app.agent logger（与 log.py 配置一致，确保日志写入文件）
try:
    from log import logger
except ImportError:
    logger = logging.getLogger(__name__)

# 熔断器单例（跨 agent 实例共享）
try:
    _global_breaker = ToolCircuitBreaker(threshold=2)
except Exception as e:
    logger.warning("[TaskAgent] ToolCircuitBreaker 实例化失败: %s（熔断降级）", e)
    _global_breaker = None

# 定时任务模块
from cron.cron_tools import create_cron_job

# Resilient parse 初始化（模块加载时应用一次）
try:
    _apply_resilient_parse()
except Exception as e:
    logger.warning("[TaskAgent] resilient_parse.apply() 失败: %s（功能降级）", e)



# Plan 提示词模板
_PLAN_TEMPLATE: str | None = None
_PLAN_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "prompts", "plan_system.txt"
)

# CodeAgent YAML 提示词模板（缓存）
_CODE_AGENT_YAML: dict | None = None
_CODE_AGENT_YAML_PATH = os.path.join(
    os.path.dirname(__file__), "..", "prompts", "code_agent.yaml"
)

# Phase 契约常量（2026-09-12 B 阶段接线；2026-09-15 批次化改造）：
# 外部 planner 产出 phases[] 契约，execute_node 按"批次"轮询执行——
# 连续非边界 phase 合并为一批，一次 CodeAgent 跑完（省去逐阶段重新初始化的 token/时间）；
# barrier 阶段独占一批；replan 阶段发重规划信号。route_after_execute 条件边形成循环，
# 全部阶段完成才进 finalize。
PLAN_MAX_PHASES = 5          # 单次 plan 的阶段数上限（超出截断）
PLAN_PHASE_MAX_RETRIES = 1   # 单阶段默认重试上限（phase.max_retries 可覆盖，钳制 [0,3]）
PLAN_PHASE_MAX_STEPS = 12    # 单阶段内部步数上限（phase.step_budget 钳制上界，2026-09-13）
# 注：PLAN_BATCH_MAX_STEPS（批次级步数上限）的唯一来源在 nodes.py——它是执行侧"批次合并"
# 的常量，不属于 planner 契约。曾在此双份定义（本处为死定义），改一处漏一处即静默漂移
# （与审计 L6 同型），2026-09-17 删除，勿再加回。

# planner 工具清单条数上限（2026-09-13 审计 L7）。原先固定 60 且按字母序截断：
# 工具总数接近/超过该值时被砍掉的是"字母序靠后"的工具，与需求无关，会整批丢掉
# 核心工具（且能力段不截断 → 误导 planner 优先选低阶能力）。现改为按与本次需求的
# 相关度排序后截断，并把被裁掉的数量如实告知 planner。
_PLAN_TOOL_LIST_LIMIT_ENV = os.getenv("PLAN_TOOL_LIST_LIMIT", "").strip()
PLAN_TOOL_LIST_LIMIT = (int(_PLAN_TOOL_LIST_LIMIT_ENV)
                        if _PLAN_TOOL_LIST_LIMIT_ENV.isdigit() else 60)
# plan 提示"数据能力"段的条数上限（2026-09-18）：能力层扩至 70 项后全量注入会让
# plan 输入膨胀 ~10k 字符；按 user_input 相关度排序截断（复用 prescan.rank_tool_names，
# 补 L7 遗留的"能力段不截断"缺口），被裁数量如实告知 planner。
_CAP_PLAN_LIST_LIMIT_ENV = os.getenv("CAP_PLAN_LIST_LIMIT", "").strip()
CAP_PLAN_LIST_LIMIT = (int(_CAP_PLAN_LIST_LIMIT_ENV)
                       if _CAP_PLAN_LIST_LIMIT_ENV.isdigit() else 30)


# 沙箱安全内置补全（2026-09-12）已随执行器更换整段删除（2026-09-20）：
# 原 `_SANDBOX_EXTRA_BUILTINS`（repr/format/hash/dir/super…21 个）是为补 smolagents
# BASE_PYTHON_TOOLS 只有 52 个内置而加的。真 CPython 执行器下这些内置**本就存在**，
# 补全表与 `_sandbox_help` 一并作废（help 的防 stdin 阻塞覆盖已内移到执行器）。

# ── 执行环境：真 CPython（2026-09-20 路线 C）────────────────────────────────
# 原 smolagents `LocalPythonExecutor` 是自实现的 AST 解释器，与 CPython 语义系统性偏差
# （vararg/kwonly/posonly 绑参错、仅 52 个内置、dunder 默认禁、错误行号指到调用点）。
# 其"安全职能"已全部作废：import 白名单 2026-09-14 删除，破坏性操作审批 2026-09-18 删除
# ⇒ 只剩误伤。现改用 `infra/guided_executor.GuidedCPythonExecutor`（真 exec，官方
# `executor=` 扩展点），全量真内置按 CPython 语义可用。
# 真隔离需求请走官方 `executor_type="e2b"/"docker"/"modal"/"blaxel"`，不要再自建解释器。


def _sandbox_instructions(tools: Any = None) -> str:
    """渲染执行环境说明段。

    2026-09-14 决策：已去掉 import 白名单，故本段**不再列举白/黑名单**
    （那段每步重发是上下文膨胀来源，实测多轮后 4 万 token）。
    2026-09-20：执行器换成真 CPython 后，原"break/continue 不能放进 try/except"
    提示已删除——那是旧解释器用 BreakException 实现循环控制的产物，CPython 下**是错的**
    （教错比不教更糟）。

    2026-09-17 修复（根因）：模型对业务工具的真实返回形态（dict 顶层键、list 在哪个
    二级键、单/多参数返回结构差异）一无所知，只能靠 print 探查类型 → REPL 式多步、
    200K token 烧在类型试探。故在此把本阶段工具的【返回结构速查】拼进 system_prompt，
    模型每一步都看得到，无需再探查。
    2026-09-20 改版：契约真源改为**工具 docstring 的 `Returns:` 段**，由
    tools/returns_contract.py 自动抽取（旧的集中式平行表已删——120 个注册工具只有 19 个
    登记，且表与代码必然漂移）。因此本函数现在应当收到**本阶段实际注入的工具表**
    （`{name: 函数/Tool实例}`，即 `tool_functions`），而不是 planner 声明的名字列表：
    只有拿到函数对象才读得到 docstring。未声明 `Returns:` 的工具走标准兜底口径。
    """
    base = (
        "【执行环境】真 CPython：Python 标准库与已安装的第三方库均可 import（无白名单限制），"
        "内置函数（print/len/dir/type/eval/…）与语言语义（含 break/continue、异常、闭包、装饰器）"
        "与本地 Python 完全一致。\n"
        "沙箱内只做演示：有限次循环 + print 输出要点；完整源码在最终答复里一次性给出，"
        "不要在沙箱内重复打印整份源码。\n"
        "【工具返回值】工具**直接返回数据本身**：赋给变量即可复用（如 k = agent_get_kline(...)），"
        "该变量在本阶段后续步骤始终可用，并会自动续承到下一阶段（跨阶段无需任何读写调用）。"
        "结果另有一份兜底登记在 `_r_<工具名>`（如 _r_agent_get_kline），没赋值时也能按名取回。"
        "**不要把裸调用 tool() 放在代码最后一行**——那样返回值会整份进观测、撑爆上下文；"
        "要查看就打印提炼后的关键字段。\n"
    )
    # 2026-09-17：返回结构速查（本阶段实际注入的工具）。这是压住 REPL 式类型探查的关键。
    contract_block = ""
    try:
        from app.agent.tools.returns_contract import _build_return_contract_block
        if tools:
            contract_block = _build_return_contract_block(tools)
    except Exception as _ce:
        logger.debug("[TaskAgent] 返回结构速查拼装跳过: %s", _ce)
    if contract_block:
        return base + "\n" + contract_block + "\n"
    return base


# ── 两级全局变量：**统一实现**（2026-09-15 用户定调）────────────────────────
# 两级共用**同一个投递方式**——`executor.state` 里的 Python 变量：
#   2 级（本阶段内跨 step）= 执行器原生 state。
#      `GuidedCPythonExecutor.__call__` 每次的 globals 就是同一个 self.state，
#      故 step N 赋的变量 step N+1 仍在；executor 随阶段重建而消亡，无需清理。
#   1 级（跨 phase / 整个 run）= 同样是 state 变量，只是**是否被促升**的区别：
#      · 促升：`GuidedCPythonExecutor._promote_model_vars()` 每步执行后把模型命名的
#        变量写入 `infra/staging.py` 的会话级存储（退化为框架内部实现，非模型 API）；
#      · 投影：新建 executor 时（本文件下方）把该 scope 的变量装回 state。
#
# 统一后模型无需任何读写调用，只需记住一条：
#   工具**直接返回数据本身** —— 想在本阶段后续步骤复用就赋值给自己的变量
#   （`k = agent_get_kline(...)`），该变量即跨阶段续承；工具结果另有一份兜底登记在
#   `_r_<工具名>`（模型没赋值时也能按名取回）。
#
# 为什么要把 per-step 数据从 1 级桶里挪出来（旧实现 _wrap_stage_guard 写 run 作用域 _OBJ）：
#   ① 与刻意交接的数据抢 _OBJ_MAX_PER_SCOPE=200 的 FIFO 名额，长跑会挤掉跨阶段数据；
#   ② 该作用域会被 auto-*.obj 噪声淹没，违背"点名即可引用"；
#   ③ 需要 json.loads / 取回调用，多一步且易踩类型坑。
#
# 实现"抄作业"：照 smolagents 自带的重结果范式（agents.py:1398-1399 存进 state、
# 观测只留 `Stored 'x' in memory.`），并**对所有结果统一生效**——不分大小，
# 避免模型去写 `if 'staged' in r` 之类的分支。
def _tool_result_var(tool_name: str) -> str:
    """工具结果变量的**确定性**名字：`_r_<工具名>`。

    2026-09-15 实测事故：早先用带序号的名字（`_r_xxx_1`），模型**事前无法知道名字**
    ⇒ 只能先调一次拿到名字、下一步才能引用，硬生生把"取数 + 取回"拆成两个 step
    （Step1 发现名字、Step2 才用得上）。名字可由工具名推导后，同一代码块内就能
    `get_xxx(...)` 紧接着 `data = _r_get_xxx`，合并回一步。
    同名重复调用会覆盖上一个结果——需要留旧值就自己赋值给别的名字。
    """
    base = re.sub(r"\W", "_", str(tool_name or ""))[:60]
    vname = "_r_%s" % base
    return vname if vname.isidentifier() else "_r_result"


def _wrap_stage_guard(fn, tool_name, executor=None):
    """把工具结果**顺带**登记进 executor.state（`_r_<工具名>`），并**原样返回数据本身**。

    2026-09-15 第二次修订：上一版返回"变量名提示字符串"，于是 `x = tool()` 拿到的是
    **通知**而不是数据 ⇒ 模型被迫多走一步去取回（实测 Step1 发现名字、Step2 才用）。
    改回原样返回后：
      · `x = tool()` 一步拿到真数据；且赋值不产生 code output ⇒ **不会进观测**；
      · 结果同时登记进 `_r_<工具名>`，模型万一没赋值，仍能按确定性名字取回。

    唯一回归风险：把**裸调用** `tool()` 写在代码块最后一行时，返回值会整份进观测
    （`_truncate_observations` 只截断 `keep_recent` 之前的旧步骤，当前步不截）。
    已用提示词约束该写法（见 _sandbox_instructions）。

    executor 为 None 时退化为原样返回（不登记）。
    """
    if executor is None:
        return fn
    vname = _tool_result_var(tool_name)

    def wrapper(*args, **kwargs):
        # 立即停止（2026-09-17）：工具调用是 step 内最密集的可中断点——
        # 每次**进入**工具前查一次中断回调，命中即中止（比等 step 边界快一个数量级：
        # 一个 step 可含多次工具调用 + 最长 120s 沙箱代码）。
        for _check in _INTERRUPT_CHECKS:
            _check()
        result = fn(*args, **kwargs)
        try:
            executor.state[vname] = result
        except Exception as e:
            logger.warning("[stage-guard] %s 结果登记进 state 失败（不影响返回值）: %s",
                           tool_name, e)
        return result

    return wrapper


def _load_plan_template() -> str:
    global _PLAN_TEMPLATE
    if _PLAN_TEMPLATE is None:
        with open(_PLAN_TEMPLATE_PATH, encoding="utf-8") as f:
            _PLAN_TEMPLATE = f.read()
    return _PLAN_TEMPLATE


def _load_code_agent_yaml() -> dict:
    global _CODE_AGENT_YAML
    if _CODE_AGENT_YAML is None:
        with open(_CODE_AGENT_YAML_PATH, encoding="utf-8") as f:
            _CODE_AGENT_YAML = yaml.safe_load(f)
    return _CODE_AGENT_YAML


# ── 立即停止（2026-09-17）──
# 中断检查回调注册表：每次工具调用进入前逐个调用，抛异常即中止当前 run。
# 由 message_queue worker 在执行任务期间注册（future 取消位 / session 停止位 /
# smolagents interrupt_switch），任务结束后恢复原状。与步边界检查互补：
#   - 工具调用边界：本注册表（step 内最密集检查点）
#   - 步边界：smolagents interrupt_switch + step_callbacks（既有）
#   - 沙箱内长代码：无法安全强杀线程，CODE_EXEC_TIMEOUT 兜底
_INTERRUPT_CHECKS: list = []

_VALID_ON_FAIL = {"retry", "replan", "abort"}


def _normalize_phases(raw, available_names: set) -> list:
    """把 _plan 输出的 phases[] 规格化为契约结构（防御性解析，2026-09-12 B 阶段）。

    设计点：
      - id 重排为 1..n；总数为 PLAN_MAX_PHASES 截断
      - tools 只保留 provider 中真实存在的名字（LLM 幻觉名丢弃并记入 tools_dropped）
      - on_fail ∈ {retry,replan,abort}（默认 retry）；max_retries 钳制 [0,3]
      - step_budget 钳制 [0,PLAN_PHASE_MAX_STEPS]，0 = 未指定（执行侧回退全局 step_budget）
      - internal_plan ∈ {True,False,None}；None = 未指定（执行侧按阶段预算判复杂度兜底）
      - goal 为空的条目跳过；非 list / 全空 → 返回 []（调用方回退单段执行旧路径）
    易错点：
      - 纯函数（不 import provider），名称集合由调用方传入，便于单测
      - 每个 phase 输出必带 tools 键（可能为空 list）+ tools_declared 声明位：执行侧
        按三态处理 —— tools 非空 = 严格白名单；tools 空且 tools_declared=True = 显式
        "本阶段不用数据工具"（只留元工具 + 技能工具）；tools_declared=False = 未声明，
        回退 domain 逻辑
    """
    if not isinstance(raw, list):
        return []
    out = []
    for p in raw[:PLAN_MAX_PHASES]:
        if not isinstance(p, dict):
            continue
        goal = str(p.get("goal") or "").strip()
        if not goal:
            continue
        tools_raw = p.get("tools")
        # 2026-09-17（设计文档 §8.2）：区分"未声明"与"显式空"。二者此前都归一成 tools: []，
        # 执行侧无从分辨 ⇒ planner 明确写 "tools": []（本阶段不用数据工具）时，会被静默
        # 放大成整域几十个工具。声明位只认 list/str 形态；其余类型（dict/int）视为未声明、
        # 回退域基调（安全侧：避免"声明了却一个工具都没有"的反向事故）。
        tools_declared = isinstance(tools_raw, (list, str))
        if isinstance(tools_raw, str):
            tools_raw = [tools_raw]
        tools, dropped = [], []
        if isinstance(tools_raw, list):
            for t in tools_raw:
                t = str(t).strip()
                if not t:
                    continue
                if t in available_names:
                    if t not in tools:
                        tools.append(t)
                else:
                    dropped.append(t)
        on_fail = str(p.get("on_fail") or "retry").strip().lower()
        if on_fail not in _VALID_ON_FAIL:
            on_fail = "retry"
        try:
            mr = int(p.get("max_retries", PLAN_PHASE_MAX_RETRIES))
        except (TypeError, ValueError):
            mr = PLAN_PHASE_MAX_RETRIES
        mr = max(0, min(3, mr))
        acc = p.get("acceptance") or []
        if isinstance(acc, str):
            acc = [acc]
        if not isinstance(acc, list):
            acc = [str(acc)]
        # 阶段级步数预算（2026-09-13 契约补全）：plan_system 早已承诺"多阶段按每阶段 3~7 步
        # 分别给"，但契约里一直没这个字段，执行侧只能用全局 step_budget → 阶段间预算无法
        # 区分（审计 G3）。0 = 未指定，执行侧回退全局；上界比顶层（20）更紧，阶段粒度更细。
        try:
            pbudget = int(p.get("step_budget") or 0)
        except (TypeError, ValueError):
            pbudget = 0
        pbudget = max(0, min(PLAN_PHASE_MAX_STEPS, pbudget))
        # 内部 planner 开关（2026-09-13）：由外部 planner 显式声明本阶段是否需要执行器
        # 内部细化规划。旧实现用 selected_skill 判断（技能层清空后恒真 → 内部 planner 常开），
        # 职责错位（审计 G2）。None = 未声明，执行侧按阶段预算判复杂度兜底。
        _ip = p.get("internal_plan", None)
        internal_plan = bool(_ip) if isinstance(_ip, (bool, int)) else None
        # 批次边界（2026-09-15 批次化）：barrier = 目标已知但依赖上游运行结果 → 独占一批；
        # replan = 目标本身未知（需看结果才能定）→ 触发重规划。二者默认 False（普通顺序阶段，
        # 与后续非边界阶段合并成批）。契约层只负责规格化，切片决策在 _run_phase_step。
        _bar = p.get("barrier", False)
        barrier = bool(_bar) if isinstance(_bar, (bool, int)) else False
        _rp = p.get("replan", False)
        replan = bool(_rp) if isinstance(_rp, (bool, int)) else False
        out.append({
            "id": len(out) + 1,
            "name": (str(p.get("name") or "").strip() or f"阶段{len(out) + 1}")[:40],
            "goal": goal[:800],
            "tools": tools,
            "tools_declared": tools_declared,
            "tools_dropped": dropped,
            "deliverable": p.get("deliverable") or "",
            "step_budget": pbudget,
            "internal_plan": internal_plan,
            "barrier": barrier,
            "replan": replan,
            "acceptance": [str(a)[:300] for a in acc][:8],
            "on_fail": on_fail,
            "max_retries": mr,
        })
    return out


def _normalize_plan_tools(raw, available_names: set) -> tuple:
    """把 _plan 输出的**顶层** tools 规格化为"附加点名"名单（2026-09-13）。

    为什么需要这条通道（能力层断链根因）：
      capabilities 层（CAPABILITY_DOMAIN）刻意不属于任何可选域，其唯一注入途径曾是
      `phases[].tools` —— 而 phases 是编排契约，简单任务本来就不拆阶段，于是"没有阶段
      就没有点名通道"，能力层对这些任务永久不可见（prompt 里已如实写明"无 phases 的
      单段任务用不了能力"）。把"工具可见性"耦合到"编排结构"上是设计错位：
      本函数为单段任务补一条与阶段解耦的点名通道。

    与 phases[].tools 的语义差异（关键，勿混用）：
      - phases[].tools = **独占白名单**：只注入点名的工具（阶段内工具面刻意收窄）；
      - 顶层 tools     = **附加点名**：在 selected_domain 的基调（域+通用）之上做并集，
                        不会挤掉域工具。典型用途：点名取数能力（all_codes/daily/
                        list_signals…），也可点名其他域的单个工具。
    两者同时出现时以 phases 为准（单段通道不生效），避免静默放宽每个阶段的工具面。

    Returns:
        (tools, dropped)：tools 去重且只保留 available_names 内的名字；
        dropped 为不存在的名字（调用方负责告警——点名落空必须可见，否则重演
        "planner 以为能用 / 执行时 Forbidden"的静默断链）。
    """
    if isinstance(raw, str):
        # LLM 偶尔把数组写成逗号串（"daily,all_codes"），按逗号拆开以免整串落空
        raw = [x for x in raw.split(",")]
    if not isinstance(raw, list):
        return [], []
    tools, dropped = [], []
    for t in raw:
        t = str(t).strip()
        if not t:
            continue
        if t in available_names:
            if t not in tools:
                tools.append(t)
        else:
            dropped.append(t)
    return tools, dropped


def _salvage_tools_from_text(text: str, available_names: set) -> list:
    """从 planner 的 task 正文回收其点名的真实工具名（2026-09-20，退化兜底）。

    为什么需要（实证 ts=1789835139377，模型 deepseek-ai/DeepSeek-V4-Flash）：
      结构化字段（selected_domain / tools / phases）是规划器的"点名通道"，但模型退化时
      可能只吐 `task` 一个字段，把工具名写进正文菜单却不填机器可读的 tools → 执行层看到
      的是"任务书承诺了工具、沙箱里没有"，调用即 Forbidden，被误报成"幻觉调用" → 空转
      至 max steps。设计红线「planner 必须点名工具」在模型退化时缺兜底：点名只认结构化
      字段，不认 planner 自己写进 task 的菜单。

    保守性：只恢复 **provider 真实注册** 的名字（available_names 内），幻觉名不回收，
    因此不会引入不可调用的名字；纯推理任务的正文不含数据工具名（如跑马灯 code 任务），
    扫描为空 → 行为不变。词边界扫描避免 `get_fund_flow` 命中 `get_fund_flow_daily`。
    """
    if not text or not available_names:
        return []
    import re as _re
    hits = []
    for _n in sorted(available_names):
        if _re.search(r"(?<![A-Za-z0-9_])" + _re.escape(_n) + r"(?![A-Za-z0-9_])", text):
            hits.append(_n)
    return hits


def _capability_names(provider) -> list:
    """列出 provider 中数据能力层（CAPABILITY_DOMAIN）的工具名（2026-09-13）。

    能力视图注入（_plan 的 planner 提示）与"能力是否可达"的观测信号共用本函数，
    避免两处各写一遍来源层过滤而漂移（来源层标记的单一事实源见 capabilities/loader）。
    """
    if not provider:
        return []
    try:
        from capabilities.loader import CAPABILITY_DOMAIN
    except Exception:
        return []
    try:
        return sorted(n for n in provider.get_tool_names()
                      if provider.get_domain(n) == CAPABILITY_DOMAIN)
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════
#  smolagents 适配层
# ═══════════════════════════════════════════════════════════════

class _LLMAdapter:
    """把 LLMBase 包装为 smolagents Model 接口。

    直接使用同步 OpenAI client 调用 LLM，避免 asyncio.run() 的
    事件循环开销和 nest_asyncio 依赖。
    """

    def __init__(self, llm: LLMBase):
        self._llm = llm
        self.model_id = getattr(llm, "model", "unknown")
        self._sync_client = None
        # 期望的同步客户端超时（2026-09-12）：_set_llm_timeout 在客户端未创建时
        # 记录于此，惰性创建时补挂——修复"复用 Agent 重试时 180s 超时丢失"。
        self._desired_timeout = None

    def close(self):
        """关闭同步 OpenAI 客户端，释放 httpx 连接池。

        每次构建 CodeAgent 都会新建 _LLMAdapter（随之新建同步客户端），
        不关闭会在长运行进程中持续泄漏连接（审计 P2）。在 execute_node 收尾调用。
        """
        if self._sync_client is not None:
            try:
                self._sync_client.close()
            except Exception:
                pass
            self._sync_client = None

    def _get_sync_client(self):
        """惰性创建同步 OpenAI client（复用 _llm 的连接配置）。"""
        if self._sync_client is not None:
            return self._sync_client
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai 未安装，请运行: pip install openai")
        # 远端网关（如 g2claw）偶发 5xx 时 SDK 内置重试只有 1 次，快速抖动期不够用；
        # 本地 llama 稳定无需多试。允许用 LLM_MAX_RETRIES 按部署环境调整。
        import os as _os
        try:
            max_retries = int(_os.getenv("LLM_MAX_RETRIES", str(self._llm.max_retries)))
        except ValueError:
            max_retries = self._llm.max_retries
        client_kwargs = {
            "api_key": self._llm.api_key,
            "timeout": self._llm.timeout,
            "max_retries": max_retries,
        }
        base_url = getattr(self._llm, "base_url", None)
        if base_url:
            client_kwargs["base_url"] = base_url
        self._sync_client = OpenAI(**client_kwargs)
        # 补挂期望超时（2026-09-12）：跳过则执行期沿用工厂默认超时
        if getattr(self, "_desired_timeout", None):
            try:
                self._sync_client.timeout = self._desired_timeout
            except Exception:
                pass
        return self._sync_client

    @staticmethod
    def _normalize_code_blocks(content: str) -> str:
        """将 markdown 代码块转为 smolagents <code>...</code> 格式。"""
        import re
        # ```python ... ``` → <code>...</code>
        content = re.sub(r'```python\s*\n(.*?)```', r'<code>\n\1</code>', content, flags=re.DOTALL)
        # ``` ... ``` → <code>...</code>（排除已处理的）
        content = re.sub(r'```\s*\n(.*?)```', r'<code>\n\1</code>', content, flags=re.DOTALL)
        # 裸 final_answer() 没有 <code> 包裹 → 包裹它
        if 'final_answer(' in content and '<code>' not in content:
            content = f'<code>\n{content}\n</code>'
        return content

    def generate(
        self,
        messages: list,
        stop_sequences: list | None = None,
        response_format: dict | None = None,
        tools_to_call_from: list | None = None,
        **kwargs,
    ):
        from smolagents import ChatMessage as SmolChatMessage

        # 非标 role 转换（与 smolagents 官方 get_clean_message_list 的
        # tool_role_conversions 完全一致）：CodeAgent memory 里的 ActionStep 会产出
        # role="tool-call"/"tool-response"，OpenAI 协议只认 user/assistant/system/tool，
        # 直接透传会被远端网关拒 500（本地 llama.cpp 宽容解析所以不报错——
        # 这就是"本地正常、远端 500"的根因，2026-09-10 实证定位）。
        _ROLE_CONVERSIONS = {
            "tool-call": "assistant",
            "tool-response": "user",
        }

        chat_messages = []
        for m in messages:
            if isinstance(m, dict):
                role, content = m.get("role", "user"), m.get("content", "")
            else:
                role = getattr(m, "role", "user")
                content = getattr(m, "content", "")
            # role 归一化：smolagents 的 role 是 MessageRole 枚举（str 子类），但 str() 产出
            # "MessageRole.TOOL_CALL" 而非 "tool-call"——必须先取 .value 再查表，
            # 否则转换永远 miss，非标 role 原样透传（2026-09-10 二次定位）。
            if not isinstance(role, str):
                role = getattr(role, "value", str(role))
            role = _ROLE_CONVERSIONS.get(role, role)
            chat_messages.append(ChatMessage(role=role, content=content))

        # 5xx/连接错误退避重试（有界循环，不递归）：
        # 旧实现 except 内递归调用 self.generate()，网关持续 5xx 时每次重试失败
        # 都会再次进入同一 except → 无限递归（2026-09-10 22:09 日志实证）。
        # 现改为固定次数的退避循环：SDK 内置重试（max_retries，默认 1）负责秒级抖动，
        # 本循环负责"网关重启中"的分钟级窗口，总尝试 = (LLM_MAX_RETRIES+1) × LLM_RETRY_ROUNDS。
        import time as _time
        import os as _os

        def _is_retriable(err: Exception) -> bool:
            t = str(err).lower()
            return ("500" in t or "502" in t or "503" in t or "429" in t
                    or "connection" in t or "timeout" in t)

        try:
            client = self._get_sync_client()
            formatted_messages = [m.to_dict() for m in chat_messages]
            response = None
            for attempt in range(int(_os.getenv("LLM_RETRY_ROUNDS", "3"))):
                try:
                    # Output budget: CodeAgent full-file generation exceeds the
                    # 2048 default -> truncated mid-string. Env-tunable.
                    _exec_max_tokens = int(_os.getenv("CODE_AGENT_MAX_TOKENS", "4096"))
                    _call_kwargs = {
                        "model": self._llm.model,
                        "messages": formatted_messages,
                        "temperature": self._llm.temperature,
                        "max_tokens": max(self._llm.max_tokens or 0, _exec_max_tokens),
                        "top_p": self._llm.top_p,
                    }
                    # seed 透传（2026-09-15）：同 seed 同输入消除采样抖动；None=不传。
                    if getattr(self._llm, "seed", None) is not None:
                        _call_kwargs["seed"] = self._llm.seed
                    response = client.chat.completions.create(**_call_kwargs)
                    break  # 成功即退出重试循环
                except KeyboardInterrupt:
                    raise
                except Exception as retry_err:
                    rounds_left = int(_os.getenv("LLM_RETRY_ROUNDS", "3")) - attempt - 1
                    if rounds_left <= 0 or not _is_retriable(retry_err):
                        raise
                    # 指数退避：2s → 4s → 8s（封顶 15s）
                    delay = min(2.0 * (2 ** attempt), 15.0)
                    logger.warning(
                        "[TaskAgent] LLM 网关抖动（%s），%.0fs 后第 %d 次重试（剩 %d 轮）",
                        str(retry_err)[:120], delay, attempt + 1, rounds_left,
                    )
                    _time.sleep(delay)
            if response is None:
                raise RuntimeError("LLM 调用失败：重试轮次耗尽且无响应")
            choice = response.choices[0]
            content = choice.message.content or ""
            finish_reason = choice.finish_reason or "stop"
            tool_calls = []
            if choice.message.tool_calls:
                for tc in choice.message.tool_calls:
                    tool_calls.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    })
                finish_reason = "tool_calls"
            usage = response.usage
            prompt_tokens = usage.prompt_tokens if usage else 0
            completion_tokens = usage.completion_tokens if usage else 0
            from llm.base import LLMResponse
            resp = LLMResponse(
                content=content,
                tool_calls=tool_calls,
                model=self._llm.model,
                finish_reason=finish_reason,
                tokens_used=prompt_tokens + completion_tokens,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        except KeyboardInterrupt:
            logger.warning("[LLMAdapter] LLM 调用被中断")
            raise
        except Exception as e:
            logger.error("[TaskAgent] LLM 调用失败（重试已耗尽）: %s", e)
            raise

        if resp.finish_reason == "error":
            raise RuntimeError(f"LLM 调用失败: {resp.content}")

        # 规范化：markdown 代码块 → <code>...</code>
        # ???????2026-09-12??length=?????????????????
        # ?????????????????????????????????
        if getattr(resp, "finish_reason", "") == "length":
            raise RuntimeError(
                "????????????????????????????????????"
                "????????????????? CODE_AGENT_MAX_TOKENS ?????"
            )
        raw_content = resp.content or ""
        normalized = self._normalize_code_blocks(raw_content)
        if normalized != raw_content:
            logger.debug("[LLMAdapter] 代码块格式已规范化")

        smol_msg = SmolChatMessage(role="assistant", content=normalized)

        class _TokenUsage:
            def __init__(self, r):
                self.input_tokens = r.prompt_tokens if r else 0
                self.output_tokens = r.completion_tokens if r else 0
                self.total_tokens = r.tokens_used if r else 0

        smol_msg.token_usage = _TokenUsage(resp)
        return smol_msg


class _SkillSectionTool(SmolToolBase):
    """让 CodeAgent 按需加载 SKILL.md 的段落。"""
    skip_forward_signature_validation = True

    def __init__(self, loader, skill_name: str):
        super().__init__()
        self._loader = loader
        self._skill_name = skill_name
        self.name = f"read_skill_section"
        headings = loader.get_section_headings(skill_name)
        headings_text = ", ".join(headings) if headings else "(无段落)"
        self.description = (
            f"读取 skill '{skill_name}' 的 SKILL.md 指令段落。"
            f"可用段落: {headings_text}\n"
            f"参数: heading（段落标题关键词）"
        )
        self.output_type = "string"
        self.inputs = {
            "heading": {
                "type": "string",
                "description": f"段落标题关键词，可用: {headings_text}",
            }
        }

    def forward(self, heading: str = "", **kwargs) -> str:
        kw = heading or kwargs.get("section", "")
        if not kw:
            headings = self._loader.get_section_headings(self._skill_name)
            return f"[错误] 未指定段落。可用段落: {headings}"
        content = self._loader.load_section(self._skill_name, kw)
        if content is None:
            headings = self._loader.get_section_headings(self._skill_name)
            return f"[错误] 未找到段落 '{kw}'。可用段落: {headings}"
        return content

    def __call__(self, *args, **kwargs):
        pnames = list(self.inputs.keys())
        for i, arg in enumerate(args):
            if i < len(pnames):
                kwargs[pnames[i]] = arg
        return self.forward(**kwargs)


class _SkillResourceTool(SmolToolBase):
    """让 CodeAgent 按需加载 skill 目录下的资源文件。"""
    skip_forward_signature_validation = True

    def __init__(self, loader, skill_name: str):
        super().__init__()
        self._loader = loader
        self._skill_name = skill_name
        self.name = f"read_skill_resource"
        resources = loader.list_resources(skill_name)
        if resources:
            res_list = ", ".join(resources)
            desc_suffix = f"\n可用资源: {res_list}"
        else:
            desc_suffix = "\n该 skill 无额外资源文件。"
        self.description = (
            f"读取 skill '{skill_name}' 的资源文件。"
            f"参数: relative_path（相对路径）"
            f"{desc_suffix}"
        )
        self.output_type = "string"
        self.inputs = {
            "relative_path": {
                "type": "string",
                "description": f"资源文件路径。{desc_suffix}",
            }
        }

    def forward(self, relative_path: str = "", **kwargs) -> str:
        path = relative_path or kwargs.get("file_path", "")
        if not path:
            _avail = self._loader.list_resources(self._skill_name)
            return (f"[错误] 未指定资源路径。用法: read_skill_resource(relative_path='references/xxx.md')；"
                    f"可用资源: {_avail}")
        content = self._loader.load_resource(self._skill_name, path)
        if content is None:
            available = self._loader.list_resources(self._skill_name)
            return f"[错误] 资源不存在: {path}\n可用资源: {available}"
        return content

    def __call__(self, *args, **kwargs):
        pnames = list(self.inputs.keys())
        for i, arg in enumerate(args):
            if i < len(pnames):
                kwargs[pnames[i]] = arg
        return self.forward(**kwargs)


class _TempSkillSectionTool(SmolToolBase):
    """从内存 SKILL.md body 按需加载段落（临时技能用）。

    与 _SkillSectionTool 的区别：
      - _SkillSectionTool：从 QDSkillAdapter 加载（文件系统）
      - _TempSkillSectionTool：从内存字符串加载（临时生成）
    接口一致，CodeAgent 无感知，统一用 read_skill_section 调用。

    兼容性：
      - 段落切分逻辑与 QDSkillAdapter._split_sections 一致（按 ## 标题）
      - 支持 # ~ #### 四级标题
      - Anthropic SKILL.md 标准兼容

    扩展点：
      - 持久化时，body 直接写入 skills/auto_xxx/SKILL.md
      - 持久化后可改用 _SkillSectionTool 加载（从文件系统）
    """
    skip_forward_signature_validation = True

    def __init__(self, body: str, skill_name: str):
        super().__init__()
        self._body = body
        self._skill_name = skill_name
        self.name = "read_skill_section"

        # 按 ## 标题切分段落（re.finditer 方式，避免 split 的分组问题）
        self._sections = {}
        heading_pattern = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)
        matches = list(heading_pattern.finditer(body))
        if matches:
            # 前言（第一个标题之前的内容）
            preamble = body[:matches[0].start()].strip()
            if preamble:
                self._sections["(前言)"] = preamble
            # 各段落
            for i, m in enumerate(matches):
                heading = m.group(2).strip()
                start = m.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
                content = body[start:end].strip()
                if content:
                    self._sections[heading] = content
        else:
            self._sections["(全文)"] = body

        headings = list(self._sections.keys())
        headings_text = ", ".join(headings) if headings else "(无段落)"
        self.description = (
            f"读取技能 '{skill_name}' 的指令段落。"
            f"可用段落: {headings_text}\n"
            f"参数: heading（段落标题关键词）"
        )
        self.output_type = "string"
        self.inputs = {
            "heading": {
                "type": "string",
                "description": f"段落标题关键词，可用: {headings_text}",
            }
        }

    def forward(self, heading: str = "", **kwargs) -> str:
        kw = heading or kwargs.get("section", "")
        if not kw:
            return f"[错误] 未指定段落。可用段落: {list(self._sections.keys())}"
        kw_lower = kw.lower()
        for h, content in self._sections.items():
            if kw_lower in h.lower():
                return content
        return f"[错误] 未找到段落 '{kw}'。可用段落: {list(self._sections.keys())}"

    def __call__(self, *args, **kwargs):
        pnames = list(self.inputs.keys())
        for i, arg in enumerate(args):
            if i < len(pnames):
                kwargs[pnames[i]] = arg
        return self.forward(**kwargs)


class _SkillFuncTool(SmolToolBase):
    """把 skill 的 Python 函数暴露为 CodeAgent 工具。"""
    skip_forward_signature_validation = True

    def __init__(self, func, module_path: str):
        super().__init__()
        self._func = func
        self._module_path = module_path
        self.name = func.__name__
        self.output_type = "string"

        doc = func.__doc__ or ""
        doc_lines = doc.strip().split("\n")
        func_desc = doc_lines[0][:200] if doc_lines[0] else f"调用 {func.__name__}()"

        param_docs = {}
        for line in doc_lines:
            line = line.strip()
            for sep in [":", " – "]:
                if sep in line:
                    key, val = line.split(sep, 1)
                    key = key.strip()
                    if key and not key.startswith(" ") and " " not in key:
                        param_docs[key] = val.strip()[:100]
                    break

        self.inputs = {}
        param_parts = []
        try:
            sig = inspect.signature(func)
            for pname, param in sig.parameters.items():
                type_map = {dict: "object", list: "array", str: "string", int: "integer", float: "number", bool: "boolean"}
                ptype = "string"
                if param.annotation != inspect.Parameter.empty:
                    ptype = type_map.get(param.annotation, "string")

                desc = param_docs.get(pname, "")
                has_default = param.default != inspect.Parameter.empty
                entry = {"type": ptype, "description": desc, "nullable": has_default}

                if has_default:
                    param_parts.append(f"{pname}={param.default}")
                else:
                    param_parts.append(pname)

                self.inputs[pname] = entry
        except Exception:
            pass

        params_str = ", ".join(param_parts)
        _no_arg_hint = "（无参数，直接调用）" if not param_parts else ""
        self.description = f"{func.__name__}({params_str}) — {func_desc}{_no_arg_hint}"

    def forward(self, **kwargs):
        try:
            return self._func(**kwargs)
        except Exception as e:
            return {"error": f"{self.name} 执行失败: {e}"}

    def __call__(self, *args, **kwargs):
        pnames = list(self.inputs.keys())
        for i, arg in enumerate(args):
            if i < len(pnames):
                kwargs[pnames[i]] = arg
        return self.forward(**kwargs)


def _load_skill_functions(skill_name: str, skill_adapter=None) -> list:
    """加载 skill 的 run.py 中的公开函数，包装为 CodeAgent 工具。

    约定：skill 目录名使用下划线（如 stock_evaluation），不用连字符。
    注意：导入失败必须留痕（2026-09-19）——旧实现静默 `return []`，
    导致 skill 工具全部不注入沙箱、模型调用被幻觉拦截却无人知晓
    （market_screener/common.py 的过时 import 曾因此长期失效）。
    """
    import importlib
    module_name = skill_name.replace("-", "_")
    try:
        mod = importlib.import_module(f"skills.{module_name}.run")
    except Exception as e:
        logger.warning("[TaskAgent] 加载技能函数失败 skill=%s module=skills.%s.run: %s",
                       skill_name, module_name, e)
        return []

    tools = []
    for attr_name in dir(mod):
        if attr_name.startswith("_"):
            continue
        func = getattr(mod, attr_name)
        if not callable(func) or inspect.isclass(func):
            continue
        if getattr(func, "__module__", "") != mod.__name__:
            continue
        try:
            tools.append(_SkillFuncTool(func, f"skills.{module_name}.run"))
        except Exception as e:
            logger.warning("[TaskAgent] 包装技能函数失败 %s.%s: %s", module_name, attr_name, e)
    return tools


def _list_skill_func_names(skill_name: str) -> set:
    """列出技能 run.py 的公开函数名集合（发现规则与 _load_skill_functions 一致）。

    2026-09-12 阶段清单升级：_normalize_phases 的合法工具名集合要并入这些名字，
    planner 在 phases[].tools 里点名技能函数（如 pre_screen / deep_analyze）时不被丢弃。
    """
    import importlib
    module_name = skill_name.replace("-", "_")
    try:
        mod = importlib.import_module(f"skills.{module_name}.run")
    except Exception as e:
        logger.warning("[TaskAgent] 列出技能函数名失败 skill=%s: %s", skill_name, e)
        return set()
    out = set()
    for attr_name in dir(mod):
        if attr_name.startswith("_"):
            continue
        obj = getattr(mod, attr_name)
        if not callable(obj) or inspect.isclass(obj):
            continue
        if getattr(obj, "__module__", "") != mod.__name__:
            continue
        out.add(attr_name)
    return out


def _load_skill_stages(skill_name: str, skill_adapter=None) -> list:
    """解析 SKILL.md 中可选的「## stages」段（阶段清单，2026-09-12）。

    约定：`## stages` 小节内放置 YAML 列表，每项字段 name / goal / tools /
    deliverable / acceptance（均容错，缺省即空）。解析失败或未定义 → []
    （向后兼容：旧技能不写该段则行为完全不变）。
    """
    if not skill_adapter:
        return []
    try:
        body = skill_adapter.load_body(skill_name)
    except Exception:
        return []
    if not body:
        return []
    m = re.search(r"^#{1,3}\s*stages\s*$", body, re.MULTILINE | re.IGNORECASE)
    if not m:
        return []
    rest = body[m.end():]
    m2 = re.search(r"^#{1,2}\s+", rest, re.MULTILINE)
    section = rest[:m2.start()] if m2 else rest
    section = re.sub(r"^\s*```[a-zA-Z]*\s*$", "", section, flags=re.MULTILINE).strip()
    if not section:
        return []
    try:
        data = yaml.safe_load(section)
    except Exception as e:
        logger.warning("[Plan] 技能 %s 的 stages 段解析失败: %s", skill_name, e)
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("name"):
            out.append(item)
    return out


# ═══════════════════════════════════════════════════════════════
#  TaskAgent
# ═══════════════════════════════════════════════════════════════




class TaskAgent(AgentBase):
    """
    任务型 Agent — 统一多阶段

    1. plan: LLM 选择技能、划分阶段
    2. 统一多阶段循环：skill/execute/direct
    3. 规则 eval
    """

    def __init__(
        self,
        llm: LLMBase,
        memory: Optional[MemoryBase] = None,
        retriever: Optional[Retriever] = None,
        system_prompt: str = "你是一个智能助手，可以使用工具来完成任务。",
        memory_window_size: int = 10,
        max_tool_rounds: int = 10,
        skill_adapter=None,
        tool_provider=None,
    ):
        super().__init__(
            llm=llm,
            memory=memory,
            retriever=retriever,
            system_prompt=system_prompt,
            memory_window_size=memory_window_size,
        )
        self.max_tool_rounds = max_tool_rounds
        self.skill_adapter = skill_adapter
        self._tool_provider = tool_provider

    def _get_skill_loader(self):
        return self.skill_adapter

    # ── plan: 决定用哪些技能 ──────────────────────────────────

    async def _plan(
        self,
        user_input: str,
        llm: LLMBase,
        trace: AgentTraceRecorder,
        plan_ctx=None,
    ) -> dict:
        """plan_ctx: 本次请求的 NodeContext。

        plan 阶段的实体/RAG/历史上下文由 plan_node 写在 ctx 上（每请求独立，并发安全）。
        兼容旧调用方：plan_ctx=None 时回退读实例属性（单线程 CLI 场景）。
        """
        src = plan_ctx if plan_ctx is not None else self
        """Plan 节点：选择技能、划分执行阶段。

        设计决策：
          - _plan() 只看技能列表，不看全量工具（工具在执行阶段通过 ToolProvider 注入）
          - 输出统一格式：task + selected_skill + step_budget
          - selected_skill → 加载该技能的 SKILL.md + 工具
          - 无技能 → CodeAgent + 通用工具

        Returns:
            {
              "task": str,
              "selected_skill": str | None,
              "selected_domain": str,
              "step_budget": int,
              "planning_interval": int,
              "phases": list,       # [] = 单段执行旧路径
              "plan_tools": list,   # 顶层附加点名单（有 phases 时恒为 []，见 _normalize_plan_tools）
            }
        """
        # 构建技能描述（含权重 + SKILL.md 执行流程）
        skills_desc = []
        if self.skill_adapter:
            # 从数据库读取技能历史权重
            skill_weights = {}
            try:
                from chain.store import get_skill_weights
                skill_weights = get_skill_weights()
            except Exception as e:
                logger.debug("[Plan] 获取技能权重失败: %s", e)

            skills = self.skill_adapter.list_skills()
            # 按权重降序排列（无权重默认 0.5，排在已验证技能之后）
            skills.sort(key=lambda s: skill_weights.get(s['name'], 0.5), reverse=True)

            for s in skills:
                name = s['name']
                desc = s.get('description', '')[:150]
                weight = skill_weights.get(name)
                weight_tag = f" [权重:{weight:.2f}]" if weight is not None else ""
                skills_desc.append(f"- {name}{weight_tag}: {desc}")
                # 预扫（2026-09-12 Q4）：静态提取 run.py 函数签名+文档 → 规划器直接按
                # 真实接口编排（替代模型试错）；阶段流并入（v1 的 _load_skill_stages 已覆盖）
                _funcs = prescan_skill_funcs(name.replace("-", "_"))
                for _f in _funcs[:8]:
                    _line = "    · " + _f["sig"] + (" — " + _f["doc"] if _f["doc"] else "")
                    skills_desc.append(_line[:150])
        skills_text = "\n".join(skills_desc) if skills_desc else "(无可用技能)"

        # 注入可用域和工具名列表，让规划器知道 CodeAgent 能调什么
        tools_hint = ""
        if self._tool_provider:
            # 可用域 = 由 tools/<子目录> 推导的可选域（2026-09-13）。
            # 不再从 _domains 反推：那会把能力层等"来源层标记"也算成可选域，
            # 出现"finance / quant 两个都像金融域、选任一都丢掉另一半工具"的问题。
            domains = self._tool_provider.get_domains()
            if domains:
                tools_hint += f"\n\n可用工具域：{', '.join(domains)}"
                tools_hint += "\n（domain 为空时仅加载通用工具，指定域时加载域+通用工具）"
            # 上限 30→60（2026-09-12 B 阶段）：phase.tools 白名单要求 planner 看到足量
            # 工具名——截断会让清单外的真实工具被误判为不存在；名称短，token 开销可控。
            # 预扫（2026-09-12 Q4）：签名清单替代裸名列表——planner 直接写对参数
            _sig_text = prescan_tools(
                self._tool_provider,
                limit=PLAN_TOOL_LIST_LIMIT,
                query=user_input,
            )
            if _sig_text:
                tools_hint += "\n\n可用工具（CodeAgent 可直接调用，含参数签名）：\n" + _sig_text
            # 能力视图（2026-09-12 Q5）：准入数据能力单独成段——能力清单是外部 planner
            # 做细致分析的原料；段内排序稳定，planner 可直接点名（通道见 _normalize_plan_tools）。
            # 2026-09-13：改按来源层标记过滤（此前是虚构域 "quant"，与 tools/finance
            # 并列互斥，见 capabilities/loader.CAPABILITY_DOMAIN）；同时删除原先
            # 计算后从未使用的 _cap_text = prescan_tools(...)（每次 plan 白扫一遍全量工具）。
            # 2026-09-18：能力层扩至 70 项后按 user_input 相关度裁剪（补 L7 遗留的
            # "能力段不截断"缺口）——全量注入会让 plan 输入膨胀 ~10k 字符；被裁数量
            # 如实告知 planner（与 prescan 裁剪原则一致：不静默丢弃）。
            try:
                from utils.prescan import rank_tool_names
                from capabilities.loader import CAPABILITY_DOMAIN as _CAP_DOMAIN
                _cap_names, _cap_hidden = rank_tool_names(
                    self._tool_provider, user_input,
                    limit=CAP_PLAN_LIST_LIMIT, domain=_CAP_DOMAIN)
                if _cap_names:
                    _cap_lines = []
                    for _n in _cap_names:
                        _fn = self._tool_provider.get(_n)
                        _sig = ""
                        try:
                            import inspect as _ins
                            _ps = [p2.name for p2 in _ins.signature(_fn).parameters.values()
                                   if not p2.name.startswith("_")]
                            _sig = "%s(%s)" % (_n, ", ".join(_ps))
                        except Exception:
                            _sig = _n + "(…)"
                        _doc = (inspect.getdoc(_fn) or "").strip().split("\n")[0][:100]
                        _cap_lines.append("  %s — %s" % (_sig, _doc))
                    tools_hint += ("\n\n数据能力（底层取数函数，不属于任何工具域；必须点名才注入，"
                                   "未点名调用即失败。点名通道：有 phases → 写进该阶段 tools；"
                                   "无 phases（单段任务）→ 写进顶层 tools）：\n"
                                   + "\n".join(_cap_lines))
                    if _cap_hidden > 0:
                        tools_hint += (f"\n  …另有 {_cap_hidden} 项数据能力与本次需求相关度较低"
                                       f"未列出；如清单里没有你需要的取数通道，可点名 list_tools 查询。")
            except Exception as _e:
                logger.debug("[Plan] 能力视图注入跳过: %s", _e)

        # ── 编排缓存激活（2026-09-15，闭环④）：历史成功链路作为**参考**注入 plan 提示 ──
        # 设计要点（与旧 v5.0 缓存的本质区别）：不跳过 LLM 硬缓存（同一意图在不同市况下
        # 合理的工具组合不同），而是把「同 domain+意图+标的 的历史已验证工具链 + 胜率」
        # 作为参考注入，让 planner 在真 schema 地基上参考历史经验 —— 结合 2026-09-15
        # 的 seed 复现，输出既稳又可解释。查询失败/未命中 → 静默跳过（fail-open）。
        cached_chain_text = ""
        try:
            from chain.store import query_cached_tools
            # 2026-09-18 断链④修复：旧实现用 getattr(plan_ctx, "intent_verb")——NodeContext
            # 无此属性 → 恒空 → chain_name 永远对不上。现改取 plan_node 挂载的
            # _plan_task_type/_plan_entity_type（chat 意图分类的真实产出，见 nodes.py）。
            # 键格式与 qd_traces.name（domain+verb+noun）对齐，verb=noun=task_type 时
            # 语义即"同意图查询"。
            _iv = (getattr(plan_ctx, "_plan_task_type", "") if plan_ctx is not None else "") or "general"
            _in = (getattr(plan_ctx, "_plan_entity_type", "") if plan_ctx is not None else "") or "stock"
            _dom = ""
            if self._tool_provider:
                _dom = "finance"  # 唯一可选域；与 _infer_domain 的主路径一致
            _hit = query_cached_tools(_dom, _iv, _in)
            if _hit:
                cached_chain_text = (
                    "\n\n【历史成功链路（仅供参考）】同意图已验证的工具序列："
                    + ", ".join(_hit)
                    + "\n（可参考其取数/分析顺序；是否沿用由你根据本任务决定，非强制）"
                )
                trace.record("plan_cache_hit", {"tools": _hit, "domain": _dom, "verb": _iv, "noun": _in})
        except Exception as _e:
            logger.debug("[Plan] 编排缓存查询跳过: %s", _e)

        # ── 工具权重提示（2026-09-15）：低权重工具（历史链路胜率差）提醒 planner 谨慎点名 ──
        # 与 skill 权重同源同表（qd_agent_weights.layer='tool'），由 evaluator 盘后自动更新。
        try:
            from chain.store import get_tool_weights
            from chain.skill_brewer import LOW_WEIGHT_THRESHOLD  # S5：阈值单一事实源
            _tw = get_tool_weights()
            _low_tools = sorted(n for n, w in _tw.items() if w < LOW_WEIGHT_THRESHOLD)
            if _low_tools:
                tools_hint += ("\n\n【工具权重提示】以下工具近期参与链路胜率偏低（<0.7），"
                               "点名前请确认确有必要：" + ", ".join(_low_tools[:12]))
        except Exception as _e:
            logger.debug("[Plan] 工具权重提示跳过: %s", _e)

        template = _load_plan_template()
        # completed_phases_text: 已完成阶段的摘要（用于多轮规划），首次调用为空
        prompt = template.format(
            skills_text=skills_text,
            user_input=user_input,
            entity_info=getattr(src, '_plan_entity_info', '') or '',
            task_type_info=getattr(src, '_plan_task_type_info', '') or '',
            rag_context=getattr(src, '_plan_rag_context', '') or '',
            history_context=getattr(src, '_plan_history_context', '') or '',
            completed_phases_text=getattr(src, '_completed_phases_text', '') or '',
        ) + tools_hint + cached_chain_text

        messages = [
            ChatMessage(role="system", content="你是任务规划器。只输出 JSON。"),
            ChatMessage(role="user", content=prompt),
        ]

        trace.record("plan_request", {
            "model": getattr(llm, "model", ""),
            "skills_available": [s["name"] for s in self.skill_adapter.list_skills()] if self.skill_adapter else [],
        })

        plan_start = time.time()
        response = await llm.generate(messages=messages)
        trace.record("plan_response", {
            "elapsed_seconds": round(time.time() - plan_start, 3),
            **llm_response_to_dict(response),
        })

        # 2026-09-20 修复（解析截断，与"模型退化"同症状的第二种根因）：调用侧不再做
        # "首个 ``` 块"的非贪婪剥离。实测 plan_response 的 task 值内部自带 ```json 菜单块
        # （raw 含 4 个围栏），非贪婪 `(.*?)` 会截到内层围栏处（1458 → 257 字符、无闭合
        # 大括号）→ 解析失败 → plan={} → 域/工具/阶段三通道全空，与 planner 真退化
        # （只吐 task 字段）的表象完全一致，极易误判成模型问题。
        # 现保留原文交给 safe_parse_json（内部按括号平衡取顶层对象，跳过字符串内的围栏）。
        plan_raw = (response.content or "").strip()
        plan = safe_parse_json(plan_raw, default={})

        task = plan.get("task", "") or plan.get("expanded_query", "") or user_input
        # step_budget 钳制：LLM 输出不可信，范围 [1,20] + int 强转。
        # 旧实现仅 `or 10` 兜底：字符串 "10" 在 smolagents 步数比较时会炸；
        # 无上限时 LLM 可自定 50 步，AGENT_MAX_STEPS 环境变量形同虚设（审计 P1-6）。
        try:
            step_budget = int(plan.get("step_budget") or 10)
        except (TypeError, ValueError):
            step_budget = 10
        # 上限与 AGENT_MAX_STEPS 对齐（2026-09-23）：此前硬编码 20，.env 形同虚设（P1-6）
        try:
            _env_max = int(os.getenv("AGENT_MAX_STEPS", "20"))
        except ValueError:
            _env_max = 20
        step_budget = max(1, min(_env_max, step_budget))
        # 内部规划步距（2026-09-12 Q7+B 后修正）：旧公式 max(budget//2+1,6) 在预算小则
        # interval 小（2 保底），预算大时最多 6 步一复盘。
        # 2026-09-17 修正（根治 R1 串行 REPL）：step_budget<=4 表示 planner 已判定
        # “一段代码流跑完即可”，此时**关闭**内部重规划（interval=None）——否则每 2 步
        # 强制 replan 会把模型推回“拆步取数→验证”的 REPL 循环。大预算（多阶段/真要
        # 分阶段）仍保留每 N 步复盘（interval 3~6）。
        if step_budget <= 4:
            planning_interval = None
        else:
            planning_interval = max(3, min(step_budget // 2, 6))

        # 从 plan 结果中提取选中的技能名
        selected_skill = plan.get("selected_skill") or plan.get("skill") or None
        # 校验技能名是否真实存在
        if selected_skill and self.skill_adapter:
            if not self.skill_adapter.get(selected_skill):
                logger.warning("[TaskAgent] plan 选择了不存在的技能 '%s'，忽略", selected_skill)
                selected_skill = None

        # 从 plan 结果中提取选中的域
        selected_domain = ""
        if not selected_skill:  # 技能模式不加载域工具
            selected_domain = plan.get("selected_domain") or plan.get("domain") or ""
            # 校验域是否真实存在（可选域由 tools/<子目录> 推导，来源层标记不算域）
            if selected_domain and self._tool_provider:
                available_domains = set(self._tool_provider.get_domains())
                if selected_domain not in available_domains:
                    logger.warning("[TaskAgent] plan 选择了不存在的域 '%s'，忽略", selected_domain)
                    selected_domain = ""

        # ── Phase 契约（2026-09-12 B 阶段）：外部 planner 产出 phases[] 驱动单 phase 轮询 ──
        # 无 phases 输出（简单任务/旧模板）→ []，execute 走单段旧路径，行为兼容。
        available_names = set()
        if self._tool_provider:
            available_names = set(self._tool_provider.get_tool_names())
        if selected_skill:
            # 技能工具名加入合法集（2026-09-12 阶段清单升级）：phases[].tools 可直接
            # 点名技能函数（如 pre_screen / deep_analyze）而不被当作幻觉名丢弃
            available_names |= _list_skill_func_names(selected_skill)
            available_names |= {"read_skill_resource", "read_skill_section"}
        raw_phases = plan.get("phases")
        phases = _normalize_phases(raw_phases or [], available_names)
        if raw_phases and not phases:
            logger.warning("[TaskAgent] phases 输出未通过规格化（%r），回退单段执行",
                           str(raw_phases)[:120])
        if phases:
            logger.info("[TaskAgent] plan: %d 个阶段 %s", len(phases),
                        ", ".join(f"#{p['id']}{p['name']}[{len(p['tools'])}工具]" for p in phases))
            dropped_total = sum(len(p["tools_dropped"]) for p in phases)
            if dropped_total:
                logger.warning("[TaskAgent] phase 白名单丢弃不存在的工具名 %d 个: %s",
                               dropped_total, [t for p in phases for t in p["tools_dropped"]][:10])

        # 单段附加点名（2026-09-13，修"能力层断链"）：phases[].tools 只在有阶段时存在，
        # 而无阶段任务原本没有任何点名通道 → capabilities（刻意不属于任何可选域）永远
        # 注入不到，planner 提示里却展示着能力清单。顶层 tools 补上这条与阶段解耦的通道，
        # 语义为"在 selected_domain 基调之上做并集"（不是白名单，见 _normalize_plan_tools）。
        # 与 phases 同时出现时以 phases 为准：不把顶层名单静默并进每个阶段（会放宽阶段工具面）。
        raw_plan_tools = plan.get("tools")
        # ── planner 退化兜底（2026-09-20）：三通道全空时的工具面救援 ──
        # 仅当 selected_domain / phases / tools 三个结构化通道**全空**时才触发，从 planner
        # 的**原始输出**按 provider 真实注册名回收它自己写过的工具名（见 _salvage_tools_from_text）。
        # 命中即并进附加点名通道，避免"planner 任务书写了 11 个工具、沙箱只有 2 个通用工具"
        # 的裸沙箱（该形态下执行器按任务书调用全部被判幻觉，是 09-19 故障的直接机制）。
        # 扫描对象是 plan_raw（LLM 原文）而非 task：解析失败时 task 会退化为 plan_input
        # （用户输入+实体+意图），正文菜单只剩在原文里——扫 task 在本兜底的目标场景下必然落空
        # （2026-09-20 实测：第 1 轮 plan_tools=[] 即此因）。
        if not raw_plan_tools and not phases and not selected_domain:
            try:
                _salvaged = _salvage_tools_from_text(plan_raw, available_names)
                if _salvaged:
                    raw_plan_tools = _salvaged
                    logger.warning(
                        "[TaskAgent] planner 未产出结构化工具字段（域/阶段/tools 全空），"
                        "从 planner 原文回收点名工具 %d 个: %s", len(_salvaged), _salvaged[:12])
                    trace.record("plan_tools_salvaged", {"tools": _salvaged})
            except Exception as _e:
                logger.debug("[TaskAgent] planner 原文工具名回收跳过: %s", _e)

        # 域通道兜底（2026-09-20，与 tools 回收互斥的第二道网）：task 正文里一个 provider
        # 工具名都没提及（纯菜单蒸发）但意图是股票类任务（screen/analysis/compare/query）
        # 时，回退到唯一可选域 finance —— 这是历史上 460+ 次 run 的主路径（planner 正常时
        # 恒填 dom='finance'）。断言：域选择失效属退化，绝不能降成"只用 2 个通用工具"的
        # 裸沙箱。刻意不含 code/explain（跑马灯等纯推理任务不该拉金融工具，否则白烧 token）。
        if not raw_plan_tools and not phases and not selected_domain and not selected_skill:
            _fb_verb = (getattr(src, "_plan_task_type", "") or "").strip().lower()
            if _fb_verb in {"screen", "analysis", "compare", "query"} and available_names:
                _avail_domains = set(self._tool_provider.get_domains()) if self._tool_provider else set()
                if "finance" in _avail_domains:
                    selected_domain = "finance"
                    logger.warning(
                        "[TaskAgent] planner 域字段缺失（task_type=%s）→ 兜底回退 domain='finance'"
                        "（避免裸沙箱；若为误判请从 task 文本核对实体）", _fb_verb)
                    trace.record("plan_domain_fallback", {"domain": "finance", "reason": _fb_verb})
        plan_tools, _pt_dropped = _normalize_plan_tools(raw_plan_tools, available_names)
        if _pt_dropped:
            logger.warning("[TaskAgent] 顶层 tools 丢弃不存在的工具名 %d 个: %s",
                           len(_pt_dropped), _pt_dropped[:10])
        if plan_tools and phases:
            logger.info("[TaskAgent] 顶层 tools(%d 个) 因存在 phases 契约而不生效（按阶段白名单执行）",
                        len(plan_tools))
            plan_tools = []
        elif plan_tools:
            logger.info("[TaskAgent] 顶层 tools 附加点名 %d 个: %s",
                        len(plan_tools), plan_tools[:12])

        # 能力可达性观测（2026-09-13）：能力不属于任何可选域，只有被点名才注入。
        # 既无 phases 又无顶层 tools 的 run 必然用不到能力层——如实记录，避免
        # "提示里展示了能力、执行时无从调用"再次成为静默断链（本项目高发问题）。
        _cap_set = set(_capability_names(self._tool_provider))
        _cap_named = sorted((set(plan_tools) | {t for p in phases for t in p["tools"]}) & _cap_set)
        if _cap_set and not _cap_named:
            logger.info("[TaskAgent] 本次未点名数据能力（%d 个可用）→ 能力层不可达", len(_cap_set))

        logger.info("[TaskAgent] plan: task=%s..., skill=%s, domain=%s, step_budget=%d",
                     task[:80], selected_skill, selected_domain or "(通用)", step_budget)
        trace.record("plan_result", {
            "route": "plan",
            "task": task,
            "selected_skill": selected_skill,
            "selected_domain": selected_domain,
            "step_budget": step_budget,
            "planning_interval": planning_interval,
            "plan_tools": plan_tools,
            "capabilities_named": _cap_named,
        })
        if phases:
            # 契约全文入 trace（供晋升闭环聚合与 T+N 对账）
            trace.record("plan_phases", {"phases": phases})

        return {
            "task": task,
            "selected_skill": selected_skill,
            "selected_domain": selected_domain,
            "step_budget": step_budget,
            "planning_interval": planning_interval,
            "phases": phases,
            "plan_tools": plan_tools,
        }


    async def chat(
        self,
        user_input: str,
        session_id: str = "default",
        use_rag: bool = True,
    ) -> AgentResponse:
        # 反馈检测（负面对照 + 正面认可，2026-09-18 补接线：check_positive_feedback
        # 此前定义了但零调用——正面认可本应固化 correct=True 并让权重/编排缓存受益）
        try:
            from feedback import check_negative_feedback, check_positive_feedback
            check_negative_feedback(user_input, session_id=session_id)
            check_positive_feedback(user_input, session_id=session_id)
        except Exception:
            pass

        return await self._chat_plan_graph(user_input, session_id, use_rag, event_cb=getattr(self, "_current_event_cb", None))

    # ── 定时任务拦截 ──────────────────────────────────────

    @staticmethod
    def _try_intercept_cron(user_input: str, session_id: str) -> Optional[AgentResponse]:
        """检测调度意图，匹配到则直接创建定时任务并返回，跳过 agent 流程。

        匹配模式：
          - 开盘期间/交易时间/盘中,每N分钟<动作>
          - 每N分钟/小时/天<动作>
          - 每天/每周X/每月X号 HH:MM<动作>
          - HH:MM<动作> / 明天HH:MM<动作>
          - 提醒我<动作>

        Returns:
            AgentResponse if intercepted, None if not a cron request.
        """
        import re
        from datetime import datetime, timedelta, timezone

        text = user_input.strip()
        if not text or len(text) > 200:
            return None

        TZ_CN = timezone(timedelta(hours=8))

        # ── 模式匹配 ──────────────────────────────────────
        at = ""
        cron_expr = ""
        content = ""
        is_cron = False

        # 模式1: 开盘期间/交易时间/盘中,每N分钟<动作>
        m = re.match(r'(?:开盘期间|交易时间|盘中)\s*,?\s*每(\d+)分钟\s*(.+)', text)
        if m:
            step = m.group(1)
            content = m.group(2).strip()
            at = f"开盘期间,每{step}分钟"
            is_cron = True

        # 模式2: 每N分钟<动作>
        if not is_cron:
            m = re.match(r'每(\d+)分钟\s*(.+)', text)
            if m:
                step = m.group(1)
                content = m.group(2).strip()
                at = f"每天"
                # 用 cron_expr 而非 at
                at = ""
                is_cron = True
                cron_expr = f"*/{step} * * * *"

        # 模式3: 每N小时<动作>
        if not is_cron:
            m = re.match(r'每(\d+)小时\s*(.+)', text)
            if m:
                step = m.group(1)
                content = m.group(2).strip()
                cron_expr = f"0 */{step} * * *"
                at = ""
                is_cron = True

        # 模式4: 每天HH:MM<动作>
        if not is_cron:
            m = re.match(r'每天\s*(\d{1,2}):(\d{2})\s*(.+)', text)
            if m:
                h, mi = m.group(1), m.group(2)
                content = m.group(3).strip()
                at = f"每天 {h}:{mi}"
                is_cron = True

        # 模式5: 每周X HH:MM<动作>
        if not is_cron:
            m = re.match(r'每(?:周|星期)([一二三四五六日天])\s*(\d{1,2}):(\d{2})\s*(.+)', text)
            if m:
                content = m.group(4).strip()
                at = f"每{m.group(1)} {m.group(2)}:{m.group(3)}"
                is_cron = True

        # 模式6: 每月X号 HH:MM<动作>
        if not is_cron:
            m = re.match(r'每月(\d{1,2})[号日]\s*(\d{1,2}):(\d{2})\s*(.+)', text)
            if m:
                content = m.group(4).strip()
                at = f"每月{m.group(1)}号 {m.group(2)}:{m.group(3)}"
                is_cron = True

        # 模式7: HH:MM<动作>（一次性）
        if not is_cron:
            m = re.match(r'(\d{1,2}):(\d{2})\s*(.+)', text)
            if m:
                content = m.group(3).strip()
                at = f"{m.group(1)}:{m.group(2)}"
                is_cron = True

        # 模式8: 明天HH:MM<动作>（一次性）
        if not is_cron:
            m = re.match(r'明天\s*(\d{1,2}):(\d{2})\s*(.+)', text)
            if m:
                content = m.group(3).strip()
                at = f"tomorrow {m.group(1)}:{m.group(2)}"
                is_cron = True

        # 模式9: N分钟/小时/秒以后<动作>（一次性）
        if not is_cron:
            m = re.match(r'(\d+)\s*(?:分钟|min)\s*(?:以后|后|之后)\s*(.+)', text)
            if m:
                delay = int(m.group(1))
                content = m.group(2).strip()
                now = datetime.now(TZ_CN)
                target = now + timedelta(minutes=delay)
                at = f"{target.hour}:{target.minute:02d}"
                is_cron = True
        if not is_cron:
            m = re.match(r'(\d+)\s*(?:小时|hour)\s*(?:以后|后|之后)\s*(.+)', text)
            if m:
                delay = int(m.group(1))
                content = m.group(2).strip()
                now = datetime.now(TZ_CN)
                target = now + timedelta(hours=delay)
                at = f"{target.hour}:{target.minute:02d}"
                is_cron = True
        if not is_cron:
            m = re.match(r'(\d+)\s*(?:秒|sec)\s*(?:以后|后|之后)\s*(.+)', text)
            if m:
                delay = int(m.group(1))
                content = m.group(2).strip()
                now = datetime.now(TZ_CN)
                target = now + timedelta(seconds=delay)
                at = f"{target.hour}:{target.minute:02d}"
                is_cron = True

        # 模式10: 提醒我<动作>（默认1分钟后执行）
        if not is_cron:
            m = re.match(r'提醒我\s*(.+)', text)
            if m:
                content = f"提醒：{m.group(1).strip()}"
                now = datetime.now(TZ_CN)
                target = now + timedelta(minutes=1)
                at = f"{target.hour}:{target.minute:02d}"
                is_cron = True

        if not is_cron or not content:
            return None

        # ── 创建定时任务 ──────────────────────────────────
        try:
            # create_cron_job 已通过标准 import（cron.cron_tools）

            # 提取任务名（取前20字）
            name = content[:20]

            result = create_cron_job(
                name=name,
                prompt=content,
                cron_expr=cron_expr,
                at=at,
                one_shot=False,
            )

            if "error" in result:
                return None  # 创建失败，走正常流程

            job_id = result.get("job_id")
            next_run = result.get("next_run", "")
            one_shot = result.get("one_shot", False)

            if one_shot:
                reply = f" 已创建一次性任务 #{job_id}，将在 {next_run} 执行：{content}"
            else:
                cron = result.get("cron_expr", "")
                reply = f" 已创建定时任务 #{job_id}（{cron}），下次执行：{next_run}\n执行内容：{content}"

            return AgentResponse(
                content=reply,
                session_id=session_id,
                metadata={"intercepted": "cron", "job_id": job_id},
            )

        except Exception as e:
            logger.warning("[TaskAgent] Cron 拦截失败: %s", e)
            return None  # 失败走正常流程

    # ── 阶段执行与评估 ──────────────────────────────────────

    def _build_code_agent(
        self,
        model,
        provider,
        skill_tools: list,
        planning_interval: int | None = None,
        phase_id: int = 0,
        domain: str = "",
        tools: list | None = None,
        step_event_cb=None,
        run_scope: str | None = None,
        extra_tools: list | None = None,
    ):
        """构建 smolagents CodeAgent 实例。

        每个阶段独立构建，避免状态污染。
        planning_interval: None=不 replan，3~5=每 N 步 replan。
        phase_id: 阶段ID（trace 与日志标记）。
        domain: 领域名，用于过滤工具。
        tools: phase 工具白名单（2026-09-12 B 阶段）。三态（2026-09-17，设计文档 §8.2）：
            非空 list → 只注入白名单内的 provider 工具；
            空 list `[]`（planner 显式声明本阶段不用数据工具）→ 不注入任何 provider
            工具，沙箱内只剩元工具 + 技能工具；
            None（未声明）→ 回退 domain 逻辑（domain="" 时仅通用工具）。
        run_scope: 本次 run 的暂存区 scope（2026-09-12 F3）。**1 级（跨阶段）通道**
            由 nodes 侧驱动（任务书告知 scope、_auto_stage_phase_result 写阶段结果），
            本形参自 2026-09-15 起在本方法体内不再被消费（工具结果改走 2 级
            executor.state），保留仅为调用侧兼容。
        extra_tools: 单段路径的**附加点名**（2026-09-13，修能力层断链）。与上面三支的
            "基调"是并集而非替代——phases[].tools 才是独占白名单；tools 非空时本参数不生效。
            用途：无阶段的单段任务点名取数能力（capabilities 不属于任何可选域，不点名注入不到）。

        工具架构：
          - 必选工具（list_tools/search_tools/format_result/web_search）→ smolagents tools=[]
          - 领域工具 + 通用工具 → executor 权威工具表（通过 ToolProvider 注入）
          - 技能工具 → 同上（Tool 实例，真 exec 下按对象直接可调用）
          - phase 白名单（tools 非空）→ 只注入白名单内的 provider 工具
          - 附加点名（extra_tools，仅 tools 为空时）→ 并进 domain/common 基调
          - 全量工具 schema → planning YAML {{tool_list}}（供 smolagents 内部 planning 选工具）

        注入必须在 tool_functions **最终确定之后**（2026-09-14，L16）：
        `_wrap_stage_guard` 与 breaker 包装都会**重新绑定**这个名字（是重新赋值，不是
        原地改）。若在绑定前把旧 dict 交给 executor，executor 持有的就是旧表
        ⇒ 任务书写着这些工具、`list_tools()` 也列得出、日志也打印"已注入 N 个"，
        **但执行时一个都调不到**，调用即被误报成"幻觉调用"。日志现同时打印
        "实持 M 个"——N 与 M 不一致即注入错位。
        2026-09-20：执行器换真 CPython 后，"工具放哪个 dict 才生效"这类路径问题**整体消失**
        ——`install_tools()` 登记权威表，`__call__` 前无条件装进命名空间，不依赖注入时序。
        """
        from smolagents import CodeAgent as SmolCodeAgent
        from smolagents.memory import ActionStep, PlanningStep

        # ── 工具函数：phase 白名单 / domain 过滤 + 附加点名 + 技能工具 ──
        # phase 白名单（2026-09-12 B 阶段，审计自 qd_traces）：tools 非空时只注入白名单工具，
        # 补救通道 = smol_tools 的 search_tools/list_tools（只读探查，不产生调用能力）。
        # 附加点名（2026-09-13）：只在非白名单模式（单段路径）生效，与基调做并集，
        # 不改变"domain 决定基调"的既有语义（见 extra_tools 说明）。
        # 工具名归一化（防御性）：剥掉 planner 可能带上的 "()" / 空白，
        # 与 nodes.py 收集 union_tools 同一规则，避免白名单名字和 provider 注册名
        # 差一个括号就被静默丢弃
        def _norm_tool_name(_t):
            s = str(_t).strip()
            if s.endswith("()"):
                s = s[:-2].strip()
            return s

        _extra = {_norm_tool_name(t) for t in (extra_tools or [])} if tools is None else set()
        if tools is not None:
            allowed = {_norm_tool_name(t) for t in tools}
            _prov_fns = provider.get_functions()
            tool_functions = {n: f for n, f in _prov_fns.items() if n in allowed}
            # 落空告警（2026-09-15）：白名单名字在 provider 找不到 → 静默丢弃会让
            # “planner 以为能用 / 执行时 Forbidden”的断链重演；命中与落空都如实入日志
            _miss = sorted(allowed - set(_prov_fns))
            if _miss:
                logger.warning("[TaskAgent] phase 白名单 %d 个名字 provider 中不存在（已忽略）：%s",
                               len(_miss), _miss[:12])
            if allowed:
                logger.info("[TaskAgent] phase 白名单：加载 %d 个工具 %s", len(tool_functions),
                            sorted(tool_functions)[:12])
            else:
                # 显式声明 0 工具（§8.2）：不注入任何 provider 工具，沙箱内只剩
                # 元工具（list_tools / search_tools / format_result / web_search / final_answer）
                # 与技能工具——本阶段靠已续承变量与上下文完成。
                logger.info("[TaskAgent] phase 显式声明 0 工具：仅元工具 + 技能工具可用")
        elif domain:
            # 指定域：域工具 + 通用工具（+ 附加点名）
            allowed = set(provider.list_by_domain("common") + provider.list_by_domain(domain)) | _extra
            tool_functions = {n: f for n, f in provider.get_functions().items() if n in allowed}
            logger.info("[TaskAgent] domain='%s'，加载 %d 个工具（通用+%s%s）", domain, len(tool_functions),
                        domain, f"+附加点名{len(_extra)}" if _extra else "")
        else:
            # 无域：仅通用工具（+ 附加点名）
            allowed = set(provider.list_by_domain("common")) | _extra
            tool_functions = {n: f for n, f in provider.get_functions().items() if n in allowed}
            logger.info("[TaskAgent] 无域，加载 %d 个通用工具%s", len(tool_functions),
                        f"（含附加点名 {len(_extra)}）" if _extra else "")

        # 附加点名落空告警（2026-09-13）：点名了 provider 里不存在的名字 → 静默丢弃会让
        # "planner 以为能用 / 执行时 Forbidden"的静默断链重演。命中与落空都如实入日志。
        if _extra:
            _hit = sorted(n for n in _extra if n in tool_functions)
            _miss = sorted(n for n in _extra if n not in tool_functions)
            if _hit:
                logger.info("[TaskAgent] 附加点名生效 %d 个: %s", len(_hit), _hit[:12])
            if _miss:
                logger.warning("[TaskAgent] 附加点名落空 %d 个（provider 中不存在）: %s",
                               len(_miss), _miss[:12])

        # 技能工具（私有，不和 tools/ 通用）
        for st in skill_tools:
            sname = getattr(st, "name", "unknown")
            tool_functions[sname] = st

        # ── 2026-09-15：两级统一为同一个投递方式（executor.state 里的 Python 变量）──
        # 两级统一为同一个投递方式：executor.state 里的 **Python 变量**。
        #   2 级 = 工具结果与本阶段内变量（含自动生成的 `_r_*`）；
        #   1 级 = 模型起过名、被促升后跨阶段续承的变量。
        # 跨阶段续承不再靠任何读写调用，而靠两个框架动作：
        #   ① `GuidedCPythonExecutor._promote_model_vars()` —— 每步执行后促升到会话级；
        #   ② 下方投影 —— 新建 executor 时把会话级变量装回 state。
        # 存储层 `infra/staging.py` 仅作框架内部实现（会话级变量存储），不进模型工具面。

        # final_answer：必须抛 FinalAnswerException，smolagents 据此判定 is_final_answer=True
        def _final_answer(answer=None, **kwargs):
            result = answer if answer is not None else kwargs
            raise FinalAnswerException(result)

        # executor（2026-09-20 换真 CPython，官方 `executor=` 扩展点）
        # 幻觉调用纠正：执行器把 NameError/AttributeError 改写成"可用工具清单 + 处理指引"。
        # 工具名单延迟解析：__call__ 出错时从权威表/agent 工具动态收集（构造期 smol_tools
        # 尚未定义——run5 教训）。
        executor = GuidedCPythonExecutor(
            additional_functions={
            "final_answer": _final_answer,
        },
            # 打印上限（2026-09-17 收紧）：smolagents 默认 50k 字符/步。
            # 砍到 1200：当前步 print 超阈值自动替成占位（smolagents 原生 truncate），
            # 逼模型把数据留在变量里、用 final_answer 一次性汇总，而非分步 print 验证。
            # 注意：真正根治靠 prompt 让模型「一步写完」（详见 code_agent.yaml 规则 13），
            # 一步写完时 print 本就多余、自然消失；这里只是兜底防爆。
            max_print_outputs_length=int(os.getenv("CODE_MAX_PRINT_CHARS", "1200")),
            # 单步代码执行超时（2026-09-15）：smolagents 默认 30s，多标的批量取数
            # （几十只 × 串行 HTTP）必超。改为 env 可调，默认 120s；与外层
            # AGENT_RUN_WALL_TIMEOUT / wait_for 超时保持大于关系，保证外层先降级。
            timeout_seconds=int(os.getenv("CODE_EXEC_TIMEOUT", "120")),
        )
        # 过程事件钩子（2026-09-11 SSE 改造）：smolagents 在每个 step 结束时调用
        # callback(memory_step, agent=self)。把 ActionStep 的代码动作/工具产出转成
        # 轻量事件 dict 交给 cb（SSE 层入队）。cb 必须绝不抛异常、不阻塞执行——
        # 钩子内部捕获一切异常，cb 异常也吞掉（流式通道故障不能影响主任务）。
        def _make_step_event_hook(cb):
            if cb is None:
                return None

            def _hook(memory_step, agent):
                try:
                    if type(memory_step).__name__ != "ActionStep":
                        return
                    ev = {
                        "kind": "action_step",
                        "step_number": getattr(memory_step, "step_number", None),
                        "is_final": bool(getattr(memory_step, "is_final_answer", False)),
                    }
                    code_action = getattr(memory_step, "code_action", None)
                    if code_action:
                        ev["code_action"] = str(code_action)[:600]
                    obs = getattr(memory_step, "observations", None)
                    if obs:
                        ev["observations"] = str(obs)[:1200]
                    err = getattr(memory_step, "error", None)
                    if err:
                        ev["error"] = str(err)[:300]
                    # 从 CodeAgent 的工具调用日志提取工具名（若有）
                    tools = []
                    tc = getattr(memory_step, "tool_calls", None)
                    if tc:
                        try:
                            for t in tc:
                                name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
                                if name:
                                    tools.append(str(name))
                        except Exception:
                            pass
                    if tools:
                        ev["tools"] = tools
                    if ev.get("code_action") or ev.get("observations") or ev.get("error") or tools:
                        try:
                            cb(ev)
                        except Exception:
                            pass
                except Exception:
                    pass

            return _hook

        _evt_hook = _make_step_event_hook(step_event_cb)
        # 注意：_truncate_observations 定义在本函数后段（SmolCodeAgent 构造前），
        # 此处不能引用——回调列表在构造参数处内联组装。
        # 工具失败熔断（2026-09-12 + 2026-09-13 动态加载）：
        # 坏工具/坏数据源连续失败 ≥2 次 → 短路，防止执行器反复重试同一坑烧爆步数。
        # 熔断包装：跳过熔断包装，工具正常调用。
        if not hasattr(self, "_tool_breaker"):
            self._tool_breaker = _global_breaker
        # 2 级全局变量（2026-09-15）：provider 工具函数的结果统一存进 executor.state
        # （smolagents 原生，寿命=本阶段），返回变量名提示；不再按返回大小判定，
        # 也不再进 1 级 run 作用域全局区（那条通道仅供跨阶段交接）。
        # 技能工具是 Tool 实例而非纯函数，跳过包装（文档读取内容受控）。
        #
        # 【契约渲染必须在包装之前】(2026-09-20)：返回结构速查读的是工具**自身 docstring
        # 的 `Returns:` 段**，而下方两种包装（_wrap_stage_guard / breaker）返回的都是
        # 新闭包，**不保留 docstring** ⇒ 包装后再渲染会全部落进"未声明"兜底。故在此处
        # （tool_functions 仍是 provider 原始函数 + 技能 Tool 实例）先渲染好整段文本。
        _env_instructions = _sandbox_instructions(tool_functions)
        for _n in list(tool_functions.keys()):
            if not inspect.isfunction(tool_functions[_n]):
                continue
            tool_functions[_n] = _wrap_stage_guard(tool_functions[_n], _n, executor)
        if hasattr(self, "_tool_breaker"):
            tool_functions = {
                name: self._tool_breaker.wrap(name, fn)
                for name, fn in tool_functions.items()
            }

        # ── 注入工具（必须在 tool_functions **最终确定之后**）──
        # 上方 `_wrap_stage_guard` 与 breaker 包装都可能**重新绑定** tool_functions，
        # 若在绑定前把旧 dict 交给 executor，executor 持有的就是那张旧表 ⇒ 任务书写着
        # 这些工具、`list_tools()` 也列得出、日志也打印"已注入 N 个"，但执行时一个都调不到
        # （2026-09-14 L16 事故：phase 白名单 6 个工具 + 暂存区三件套全部调不动，
        #   模型只能按提示改用纯 Python 硬算）。旧注入点在 executor 创建处，已移到这里。
        # install_tools 登记权威表，executor 每次 __call__ 前无条件重装——阶段重试
        # （复用 CodeAgent）清空命名空间也不会复发。
        executor.install_tools(tool_functions)
        # 2026-09-22：把 provider 全量工具名登记给执行器（纠正话术用）——
        # 模型引用了"存在但未点名"的工具时（如 calculate_ma），纠正文案直接说明
        # "该工具存在但未列入本阶段白名单"，避免模型继续猜名字烧步数。
        try:
            if self._tool_provider is not None:
                executor.set_all_known_tools(self._tool_provider.get_functions().keys())
        except Exception:
            pass

        # 日志同时打印 executor **实际持有**的数量——只打印 len(tool_functions) 会在上述
        # 错位时给出误导数字（这是本次事故排查被带偏的直接原因）。
        logger.info("[TaskAgent] executor 已注入 %d 个工具函数（实持 custom=%d state=%d）",
                    len(tool_functions),
                    len(getattr(executor, "custom_tools", {}) or {}),
                    len(getattr(executor, "state", {}) or {}))

        # ── 会话级变量投影（2026-09-15 两级统一的核心一步）──
        # 新建 executor = 全新命名空间，上一阶段的变量**不在作用域里**；这里把该
        # run_scope 下已促升的变量投影回 state，模型下一阶段就能**直接用同名变量**。
        # 安全性：smolagents run() 开始时只做 send_variables（= state.update，**合并**
        # 而非替换），不会清掉此处注入的内容（agents.py:490-491 实证）。
        try:
            if run_scope:
                executor.session_vars_scope = run_scope
                prev = stage_scope_vars(run_scope)
                if prev:
                    executor.send_variables(prev)
                    logger.info("[TaskAgent] 已续承会话级变量 %d 个：%s",
                                len(prev), ", ".join(sorted(prev)[:20]))
        except Exception as e:
            logger.warning("[TaskAgent] 会话级变量投影失败（本阶段无续承）: %s", e)

        # ── 必选工具：注册为 smolagents Tool，放入 tools=[] ──
        # 注意：tools= 在本项目下**不会**"随 system prompt 下发工具描述"（原因与
        # 更正说明见下方 smol_tools 定义处）。它只决定"沙箱内可调用 + 以 BaseTool
        # 形态被调用"。LLM 认识这些工具名的通道是 prompts/code_agent.yaml 的正文/
        # 示例与 planning 段注入的 provider schema。

        class FinalAnswerTool(SmolToolBase):
            """把"调用 final_answer 即终止任务"这条契约以 Tool 形态表达一次。

            注意：它**不负责**让 LLM 知道 final_answer 的存在（本项目下 tools= 的
            描述不会进 system prompt，见下方 smol_tools 处的更正说明）；LLM 的认知
            来自 prompts/code_agent.yaml 正文与示例。

            forward 抛 FinalAnswerException（由 evaluate_python_code 捕获并设置
            is_final_answer=True），而非真正返回。这样 smolagents 才知道任务结束了。
            """
            skip_forward_signature_validation = True
            name = "final_answer"
            description = (
                "结束任务并返回最终答案。调用后任务立即结束——"
                "只在你确定已有足够信息来回答用户时才调用。"
            )
            output_type = "string"
            inputs = {
                "answer": {"type": "string", "description": "最终答案内容"},
            }

            def forward(self, answer: str = "", **kwargs):
                # 抛异常而非返回值——smolagents 据此判定 is_final_answer
                raise FinalAnswerException(answer)

        class _SearchToolsTool(SmolToolBase):
            skip_forward_signature_validation = True
            name = "search_tools"
            description = "按关键词搜索可用工具。返回匹配的工具名、参数和描述。用于不确定工具名时快速定位。"
            output_type = "string"
            inputs = {
                "query": {"type": "string", "description": "搜索关键词（如 资金流、K线、选股）"},
                "domain": {"type": "string", "description": "领域过滤（可选）", "nullable": True},
            }
            def forward(self, query: str = "", domain: str = "", **kwargs):
                return provider.search_tools(query, domain)

        def _sig(fn) -> str:
            """工具的完整签名（含默认值）。

            2026-09-14 实证：模型为确认 `get_market_indices` 有没有参数，写了
            `import inspect; inspect.signature(...)`，白烧一步（当时 import 受白名单
            限制，现已全放行，但"探签名"本身仍是无谓的一步）。签名在**宿主侧**
            算好随 list_tools 给出，模型就不必去探——比让它自己 import inspect 更省一步。
            """
            try:
                # eval_str=True：把 `from __future__ import annotations` 造成的字符串化
                # 注解（code: 'str' = ''）解析回可读形式（code: str = ''）。
                try:
                    return str(inspect.signature(fn, eval_str=True))
                except Exception:
                    return str(inspect.signature(fn))
            except Exception:
                return "(...)"

        class _ListToolsTool(SmolToolBase):
            skip_forward_signature_validation = True
            name = "list_tools"
            description = "列出所有可用工具（默认全部，可按领域过滤）。用于了解当前有哪些工具可用。"
            output_type = "string"
            inputs = {
                "domain": {"type": "string", "description": "领域名称（可选，空=全部）", "nullable": True},
            }
            def forward(self, domain: str = "", **kwargs):
                # phase 白名单模式（2026-09-12）：只展示本阶段可见工具
                # （E2E 实证：列出白名单外工具 → 执行器反复试探"搜到但调不动"）
                if tools is not None:
                    if not tools:
                        return "本阶段无数据工具（search_tools 仍可用）。"
                    lines = [f"本阶段可用工具 ({len(tools)})："]
                    for _n in tools:
                        _fn = provider.get(_n)
                        _desc = (getattr(_fn, "__doc__", "") or "").strip().split("\n")[0][:100]
                        lines.append(f"  - {_n}{_sig(_fn)} — {_desc}" if _desc
                                     else f"  - {_n}{_sig(_fn)}")
                    return "\n".join(lines)
                if not domain:
                    # 空 domain 在 provider 语义里=仅通用工具（E2E 实证误导）；默认列全部
                    domain = "all"
                return provider.list_tools(domain)

        def _meta_fn(name: str):
            """取元工具实现（web_search / format_result 等，供 tools=[] 通道的包装层用）。

            【易错点·2026-09-21】元工具走 `tools=[]` 通道，**不在 provider 注册表里**
            （base.py 的 `_MUST_HAVE` 让 scan_directory 跳过它们）⇒ 只能用
            provider.get_meta()。误用 provider.get() 会恒为 None —— 表现为
            web_search 整体不可用、format_result 静默退化成 str(result)[:2000]。

            缺装配时**响亮报错**、不静默降级：静默降级正是这个 bug 难发现的原因。
            这里只认**工具名**、不认模块名，模块改名/迁移只需改 base.py 的 _MUST_HAVE。
            """
            fn = provider.get_meta(name) if provider else None
            if fn is None:
                raise RuntimeError(
                    f"元工具 {name} 未装配（见 tools/base.py 的 _MUST_HAVE 与 _load_meta_tools）")
            return fn

        class _FormatResultTool(SmolToolBase):
            skip_forward_signature_validation = True
            name = "format_result"
            description = "把任意格式的数据转换为 LLM 容易理解的字符串。用于格式化工具返回的结果。"
            output_type = "string"
            inputs = {
                "result": {"type": "object", "description": "任意格式的数据"},
                "max_depth": {"type": "integer", "description": "最大递归深度", "nullable": True},
                "max_items": {"type": "integer", "description": "最多显示的项数", "nullable": True},
            }
            def forward(self, result=None, max_depth: int = 3, max_items: int = 20, **kwargs):
                return _meta_fn("format_result")(result, max_depth, max_items)

        class _WebSearchTool(SmolToolBase):
            skip_forward_signature_validation = True
            name = "web_search"
            description = "联网搜索最新信息。用于补充新闻面、政策面、市场情绪等实时数据。"
            output_type = "object"
            inputs = {
                "query": {"type": "string", "description": "搜索关键词"},
                "count": {"type": "integer", "description": "结果数量", "nullable": True},
                "freshness": {"type": "string", "description": "时效性过滤", "nullable": True},
            }
            def forward(self, query: str = "", count: int = 8, freshness: str = "", **kwargs):
                # 消毒在**真正的实现**里做（tools/web_search_tools.py 的 _sanitize_result），
                # 不在本包装层——否则两处都消毒会叠加成双重注释前缀。
                return _meta_fn("web_search")(query, count, freshness)

        # ── tools= 在本项目的真实作用（2026-09-14 更正误判）────────────────────
        # smolagents 里 tools= 有两个作用，本项目只吃到 ①：
        # ①【生效｜沙箱可调用】run 启动时随 send_tools 注入执行器
        #    （smolagents agents.py:492）⇒ 沙箱内可调用，且以 BaseTool 形态被调用：
        #    BaseTool.__call__ 会把"单个 dict 且键名匹配 inputs"的入参自动展开成
        #    kwargs（smolagents tools.py:231-246）⇒ 这正是技能工具从 custom_tools
        #    挪进 tools= 的真实收益（模型把参数打包成 dict 传参时不再直接
        #    TypeError 烧步数）。技能工具并入 smol_tools（2026-09-12）保留。
        # ②【不生效｜随提示下发描述】smolagents 默认模板里有
        #    `{% for tool in tools %}{{ tool.to_code_prompt() }}` 渲染块
        #    （.venv/.../smolagents/prompts/code_agent.yaml:132-137）；而本项目用
        #    prompts/code_agent.yaml **整体覆盖**了 system_prompt（见本函数下文
        #    agent.prompt_templates.update(custom_templates)），该模板里**没有**
        #    这个渲染块 ⇒ 放进 smol_tools 的工具描述不会进入 LLM 提示
        #    （2026-09-14 实测：用该模板 + 桩 tool 渲染，结果不含工具描述）。
        # ③【因此】LLM 能认识 search_tools / final_answer / 技能工具的名字，通道是
        #    prompts/code_agent.yaml 正文与示例 + planning 段注入的 provider schema
        #    （`{{tool_list}}` 只出现在 planning.*，且填的是 provider 工具，不含
        #    smol_tools）。**不要再假设"加进 smol_tools 模型就能看见"**；若确实想让
        #    工具描述进系统提示，必须改 system_prompt 模板——那是行为变更，需先评审。
        #
        # ── final_answer 接线（必要条件只有两条，均已满足，不要重复"再改一次"）──
        #   a. 执行器侧：additional_functions["final_answer"] = _final_answer，且
        #      _final_answer **抛** FinalAnswerException（定义见本函数上文）。
        #   b. 提示侧：prompts/code_agent.yaml 正文与示例已硬编码 `final_answer(...)`
        #      的用法与"最后用它返回"的纪律。
        # 下方的 FinalAnswerTool 在 CodeAgent 下**不是**必需条件（历史注释"不加就不
        # 结束"属误判）：它的 forward 抛同一个异常，作用是把该契约同时表达为 Tool
        # 形态，使 ToolCallingAgent 形态下也能正确终止。保留即可。
        smol_tools = [
            _SearchToolsTool(),
            _ListToolsTool(),
            _FormatResultTool(),
            _WebSearchTool(),
            FinalAnswerTool(),
        ]
        smol_tools += list(skill_tools or [])

        # 阶段内局部记忆（2026-09-15 激进压缩，用户定调；2026-09-17 收紧 keep_recent）：
        #   - 2026-09-17 keep_recent=1 仍线性膨胀：当前步生成 input 时上一步尚未
        #     finalize，obs 仍是全量 → 每步涨 ~上一步全量 obs。故进一步降到 0：
        #     历史步全部截到 400/200，当前步 obs 在其 finalize 后才被后续步看到
        #     （此时已是下一轮，本步已成历史被截）。数据变量在 executor.state
        #     跨步保留（见 _wrap_stage_guard），截断 observations 不丢数据。
        # smolagents 1.26 生命周期事实：write_memory_to_messages 每步全量重放
        # （无官方截断开关），社区方案 = step_callbacks 里原地改写 memory ——
        # 本函数即该方案（每步 finalize 后触发，截断对下一步的 prompt 生效）。
        keep_recent = 0
        keep_recent_mo = 1  # model_output 保留近 1 步 code 块（模型需看上一步代码续写），更早截断
        _CODE_BLOCK_RE = re.compile(r"<code>(.*?)</code>", re.S)

        def _truncate_field(step: ActionStep, field: str, cap: int = 200) -> None:
            val = getattr(step, field, None)
            if val is None:
                return
            if isinstance(val, str):
                if len(val) > cap:
                    # 保留首尾：头部含结构信息，尾部含 "Last output" 行（收尾语义）
                    tail_keep = min(80, cap // 4)
                    setattr(step, field, val[:cap - tail_keep] + "\n...[truncated, full data kept in variables]...\n" + val[-tail_keep:])
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict) and isinstance(item.get("text"), str) \
                            and len(item["text"]) > cap:
                        item["text"] = item["text"][:cap] + "...(truncated)"

        def _truncate_tool_call_args(step: ActionStep, cap: int = 300) -> None:
            # tool_calls 在 to_messages 里渲染为 str([tc.dict()])，arguments 中的大代码/
            # 参数字符串随步数累积重放（实测每步 8-12k 字符，是 O(n²) 主引擎之一）。
            # 只截展示用 arguments，不影响执行——执行读的是沙箱 state 里的代码与变量。
            for tc in (getattr(step, "tool_calls", None) or []):
                args = getattr(tc, "arguments", None)
                if isinstance(args, dict):
                    for k, v in list(args.items()):
                        if isinstance(v, str) and len(v) > cap:
                            args[k] = v[:cap] + "...[truncated]"
                elif isinstance(args, str) and len(args) > cap:
                    try:
                        tc.arguments = args[:cap] + "...[truncated]"
                    except Exception:
                        pass

        def _truncate_model_output(step: ActionStep, cap: int = 400) -> None:
            # model_output = Thought(思维链，往往几千字符) + <code>块。模型下一步续写真正
            # 需要的只是 code（变量在沙箱 state），思维链可丢。截断策略：保留 <code> 块完整
            # + 120 字符头部摘要；无 code 块时退回首尾截断。
            mo = getattr(step, "model_output", None)
            if not isinstance(mo, str) or len(mo) <= cap:
                return
            m = _CODE_BLOCK_RE.search(mo)
            if m and len(m.group(0)) <= max(cap, 2500):
                step.model_output = mo[:120].rstrip() + "\n...[thought truncated]...\n" + m.group(0)
            else:
                tail_keep = min(80, cap // 4)
                step.model_output = (mo[:cap - tail_keep] + "\n...[truncated]...\n"
                                     + mo[-tail_keep:])

        def _truncate_observations(memory_step: ActionStep, agent: SmolCodeAgent) -> None:
            # 三类累积重放大头（2026-09-18 cli 实测定量）：
            #   observations —— _wrap_stage_guard 后通常已是 0，维持全截兜底（裸调用洞）；
            #   tool_calls.arguments —— 实测每步 8-12k 字符，截展示串（执行不受影响）；
            #   model_output —— 模型思维链+完整代码，每步 2-11k 字符累积；保留近
            #     keep_recent_mo 步原文（模型需看上一步代码续写，全截实测令输出暴涨），
            #     更早截到 400。变量在 executor.state 跨步保留，截断不丢数据。
            for step in list(agent.memory.steps) + [memory_step]:
                if isinstance(step, PlanningStep):
                    # PlanningStep 重放渲染完整 plan（实测 ~9k 字符，planning 后 ASSISTANT
                    # 角色跳变的主因），压到 400 保留首尾。
                    _truncate_field(step, "plan", cap=400)
                    continue
                if not isinstance(step, ActionStep) or step.step_number is None:
                    continue
                if step is memory_step or step.step_number <= memory_step.step_number - keep_recent:
                    _truncate_field(step, "observations", cap=400)
                if step is not memory_step and step.step_number <= memory_step.step_number - keep_recent_mo:
                    _truncate_model_output(step, cap=400)
                _truncate_tool_call_args(step, cap=300)
                # error 字段（完整 traceback）渲染为 TOOL_RESPONSE，实测某步出错后
                # TOOL_RESPONSE 跳变 ~6k 字符，压到 400。
                _truncate_field(step, "error", cap=400)
                if hasattr(step, 'observations_images') and step.observations_images:
                    step.observations_images = None

        # 观察层「空输出」歧义修正（2026-09-14 CLI 实测查出的不收尾头号诱因）：
        # 本步代码只 print() 无 return 时，smolagents 把 observation 末行写成
        # `Last output from code snippet:` + `None`（agents.py:1752-1754 对
        # code_output.output 做 str(None)）。该 None 的真实语义只是「本步代码没有
        # return 值」，但模型会读成「上一步什么都没产出」→ 从头重写整份代码 →
        # 探索型死循环把步数烧光（实测「写跑马灯」：Step1 已跑出完整结果，仍重写
        # 3 次、全程 0 次 final_answer，token 单调膨胀 in 3884→7943 / out 7768→15888）。
        # 修法：只把该行补成语义明确的说明，不动任何行为契约——print 的日志本就在
        # 上方 Execution logs 中完整列出，模型可直接引用。
        def _clarify_empty_output(memory_step: ActionStep, agent: SmolCodeAgent) -> None:
            obs = getattr(memory_step, "observations", None)
            if not isinstance(obs, str):
                return  # PlanningStep / 异常路径不含该行，跳过
            marker = "Last output from code snippet:"
            idx = obs.rfind(marker)
            if idx == -1:
                return
            if obs[idx + len(marker):].strip() != "None":
                return
            memory_step.observations = (
                obs[:idx + len(marker)]
                + "\nNone ← 本行仅表示「本步代码没有 return 值」，不代表本步没有产出："
                  "print() 的输出已完整列在上方 Execution logs 中，可直接引用。"
            )

        # ── C 护栏层·确定性收尾（2026-09-14）──
        # A（`_clarify_empty_output`）治"把 None 误判成没产出"，B（交付物铁律）治
        # "不知道交付物该放哪个参数"。C 是两者都没拦住时的兜底：把"跑满 N 步 →
        # `_handle_max_steps_reached` 再额外调一次 LLM 强制收尾"压成"主动退出"。
        #
        # 判据为什么是「代码重复」而不是「没调 final_answer」：后者会把正常的多步任务
        # （取数 → 计算 → 交付）的中间步骤全部误判成"该收尾"，属于矫枉过正。
        # **原地重写同一份代码**才是真正的死循环信号——模型已经算出来了，却以为没算出来。
        #
        # 注入时机必须在**倒数第二步**：observations 要到下一步才会被渲染成
        # `Observation:` 消息（memory.py:126-137 / agents.py:768-769），在最后一步注入
        # 等于没人看得到。
        #
        # 时序前提（已核对 smolagents 源码，勿凭印象改）：`_finalize_step` → callbacks
        # 发生在 `action_step.is_final_answer = True` **之后**（agents.py:592 → 601），
        # 所以这里读到的 is_final_answer 是准确的。
        def _enforce_final_answer(memory_step: ActionStep, agent: SmolCodeAgent) -> None:
            if getattr(memory_step, "is_final_answer", False):
                return  # 本步已正常退出，无事可做
            obs = getattr(memory_step, "observations", None)
            code = getattr(memory_step, "code_action", None)
            if not isinstance(obs, str) or not obs:
                return
            if not isinstance(code, str) or not code.strip():
                return

            def _norm(src: str) -> str:
                # 去整行注释与全部空白：模型"重写"往往只改注释/缩进/换行，语义完全相同
                body = "".join(ln.split("#", 1)[0] for ln in src.splitlines())
                return "".join(body.split())

            # 只和最近 keep_recent 步比（更早的 code_action 已被 _truncate_observations
            # 截断，比较无意义，也正好省掉无谓开销）
            cur = _norm(code)
            dup_step: Optional[int] = None
            if cur and len(cur) <= 20000:
                lo = memory_step.step_number - keep_recent
                for prev in agent.memory.steps:
                    if not isinstance(prev, ActionStep) or prev.step_number is None:
                        continue
                    if not (lo <= prev.step_number < memory_step.step_number):
                        continue
                    # 上一步若是「失败重试」（报错 → 改写），重写是**必要的**：此时劝它
                    # "别重写"会直接阻断纠错——这是 C 最容易犯的错。用错误痕迹做门控排除。
                    # （2026-09-14 CLI 实测「写跑马灯」：Step1 整块中断，Step2 几乎是同一份
                    #   代码、只换了捕获方式 → 若误判成"原地重写"，就会劝它放弃修复。）
                    prev_obs = getattr(prev, "observations", "") or ""
                    # 2026-09-18：移除 "[import 拦截]" 匹配——该标记出自执行器的 import
                    # 越界改写分支，分支已随沙箱删除而移除（永不命中）。
                    if ("Traceback" in prev_obs
                            or "Error:" in prev_obs or "Exception:" in prev_obs):
                        continue
                    prev_code = getattr(prev, "code_action", None)
                    p = _norm(prev_code) if isinstance(prev_code, str) else ""
                    if p and SequenceMatcher(None, cur, p, autojunk=False).ratio() >= 0.9:
                        dup_step = prev.step_number
                        break

            if dup_step is not None:
                hint = (
                    f"\n[系统] 本步代码与第 {dup_step} 步几乎完全相同——你在原地重写，"
                    "并没有产生新信息。若任务已达成，**不要再重写代码**，直接把握有的"
                    "源码/结果/日志交给 `final_answer(...)` 收尾即可。"
                )
            elif agent.max_steps - memory_step.step_number <= 1:
                hint = (
                    "\n[系统] 只剩最后一步了。请立即用 `final_answer(...)` 送出交付物"
                    "（源码 / 结果 / 日志拼成字符串），不要再开始新的探索或重写。"
                )
            else:
                return  # 无重复迹象、且步数还有余量 → 不干预（避免误伤正常推进的任务）
            memory_step.observations = obs + hint

        def _check_final_answer(answer, memory, agent):
            """验证 final_answer 不为空且非半成品 + 数字溯源（2026-09-16）。

            数字溯源：报告中的数值必须能在之前的 Observation（工具返回）里找到——
            这是「结果必须来自工具输出」的引擎级保证，不依赖提示词遵守。
            保守触发：孤立数值 >= 4 个且 <=30% 可溯源时才拒收（少量数值可能是
            推理衍生值如百分比/评分，全部强拦会误伤）；拒收后 smolagents 要求模型重写。
            """
            if answer is None:
                return False
            text = str(answer).strip()
            if not text:
                return False
            # 半成品检测：仍含裸 <code> 标签且无 final_answer 调用痕迹
            if "<code>" in text and "final_answer" not in text:
                return False
            # 数字溯源（工具输出 grounding）
            try:
                import re as _re

                def _to_float(s):
                    try:
                        return float(s)
                    except Exception:
                        return None

                observations = []
                for step in getattr(getattr(agent, "memory", None), "steps", []) or []:
                    obs = getattr(step, "observations", None)
                    if obs:
                        observations.append(str(obs))
                obs_corpus = "\n".join(observations)
                if obs_corpus:
                    nums = _re.findall(r"\d+(?:\.\d+)?", text)
                    nums = [n for n in nums if len(n.lstrip("0.")) >= 2]  # 忽略 0/1/2 这类噪音
                    if len(nums) >= 4:
                        grounded = sum(1 for n in nums if n in obs_corpus)
                        # ① 总量级保守阈值：整体可溯源比例过低即拒收
                        if grounded / len(nums) < 0.3:
                            ungrounded = [n for n in nums if n not in obs_corpus][:10]
                            guide = (
                                f"以下数值在工具输出（Observation）中找不到，疑似编造：{ungrounded}。"
                                "修复方法（三选一）：① 删除这些数值，只保留可溯源的数据；"
                                "② 在数值后标注来源或'估算'，如'约12.5元（估算）'；"
                                "③ 改为引用前面步骤的变量而非写死数字。"
                                "重写 final_answer 时逐条核对每个数值。"
                            )
                            logger.warning(
                                "[FinalAnswer] 数字溯源失败：%d 个数值仅 %d 个可在 Observation 中溯源，拒收要求重写（已附修复指导）",
                                len(nums), grounded)
                            raise ValueError(f"数字溯源失败（{len(nums)} 个数值仅 {grounded} 个可溯源）。{guide}")
                        # ② 价格/金额/百分比类（含小数点且 >=1）几乎只可能来自工具数据：
                        #   模型若凭空在 final_answer 里写死这类数字（而非引用前面取到的变量 /
                        #   打印过的取值），就会大面积无法溯源 → 判定为编造并拒收重写。
                        #   30% 全局阈值会被计划文本/早期打印里的偶然整数、常见百分比「漏过」，
                        #   故对小数类数值单独加严（2026-09-19 修复 tmp/1.txt 选股报告幻觉）。
                        decimal_nums = [n for n in nums if "." in n and (_to_float(n) or 0) >= 1.0]
                        if len(decimal_nums) >= 4:
                            dec_grounded = sum(1 for n in decimal_nums if n in obs_corpus)
                            if dec_grounded / len(decimal_nums) < 0.5:
                                dec_un = [n for n in decimal_nums if n not in obs_corpus][:10]
                                guide = (
                                    f"以下价格/金额类小数在工具输出中找不到（疑似编造）：{dec_un}。"
                                    "修复方法：① 用之前步骤从工具取到的**变量**（如 latest['c']）代替写死的数字；"
                                    "② 无法变量化的，在数值后标注'（估算）'或'（工具未返回）'；"
                                    "③ 删除非必要数值。注意：print 过的数值也算可溯源，重写前可 print 复核。"
                                )
                                logger.warning(
                                    "[FinalAnswer] 数字溯源失败（价格/金额类）：%d 个小数数值仅 %d 个可溯源，"
                                    "疑似凭空编造，拒收要求重写（已附修复指导）",
                                    len(decimal_nums), dec_grounded)
                                raise ValueError(f"数字溯源失败（价格/金额类：{len(decimal_nums)} 个仅 {dec_grounded} 个可溯源）。{guide}")
            except Exception as e:
                logger.debug("[FinalAnswer] 数字溯源检查跳过: %s", e)
            return True

        agent = SmolCodeAgent(
            tools=smol_tools,
            model=model,
            max_steps=self.max_tool_rounds,
            executor=executor,
            planning_interval=planning_interval,
            step_callbacks=(
                [_truncate_observations, _clarify_empty_output, _enforce_final_answer]
                # 协作取消（2026-09-17 Ctrl+C 修复）：message_queue worker 在主线程
                # 收到 Ctrl+C 后置 future 取消位，这里每步开头检查 _user_step_callbacks，
                # 命中即抛错中止 run——worker 线程收不到 SIGINT，这是唯一可停点。
                + list(getattr(self, "_user_step_callbacks", None) or [])
                + ([_evt_hook] if _evt_hook is not None else [])
            ),
            final_answer_checks=[_check_final_answer],
            instructions=(
                _env_instructions
                + "\n【数据补充策略】\n"
                "- 当关键工具返回 error 或数据为空时，使用 web_search 搜索最新信息补充\n"
                "- web_search 搜索关键词示例：'{股票名称} {股票代码} 最新消息 分析'\n"
                "- 将 web_search 结果作为参考信息，结合已有数据分析\n"
                "- web_search 结果用于补充新闻面、政策面、市场情绪等实时信息"
            ),
        )

        # 立即停止（2026-09-17）：当前在跑的 CodeAgent 挂实例槽——
        # 外部（message_queue 探针 / Web stop 端点）可调 agent.interrupt()，
        # smolagents 原生 interrupt_switch 在下一个步边界抛 AgentError 中止。
        self._active_code_agent = agent

        # 2026-09-16：与实际执行环境对齐——executor 已是全放行（"*"），
        # 但 CodeAgent 默认 authorized_imports 会让 system_prompt 规则 9 渲染出
        # 受限模块清单（提示词与现实不符）。构造后直接设属性并重渲染 system_prompt，
        # 规则 9 变为 "You can import from any package you want."。
        try:
            agent.authorized_imports = ["*"]
            agent.prompt_templates["system_prompt"] = _load_code_agent_yaml()["system_prompt"]
            agent.memory.system_prompt = None  # 触发 initialize_system_prompt 惰性重渲染
        except Exception as _ae:
            # 2026-09-18：对齐失败会让规则 9 回退渲染成 smolagents 默认的**受限模块清单**，
            # 模型被教成"只能 import 这些"——与全放行的现实相反、且原 debug 级别无人可见。
            # 判据失效必须可见（同 nodes.py _detect_max_steps 的 warning 化处置）。
            logger.warning("[TaskAgent] authorized_imports 对齐失败，system_prompt 规则 9 "
                           "可能渲染出受限 import 清单（与全放行不符）: %s", _ae)

        # 覆盖 smolagents 默认 prompt_templates，使用自定义 YAML 模板
        try:
            custom_templates = _load_code_agent_yaml()
            import copy
            custom_templates = copy.deepcopy(custom_templates)

            # planning 段注入（2026-09-16 jinja 折衷说明）：
            #   smolagents._generate_planning_step 用 populate_template(..., variables={task, tools,
            #   managed_agents}) 渲染 initial_plan，变量集**写死**；26 个业务工具注册在 provider 侧、
            #   不在 self.tools（smolagents Tool 实例），所以官方 {% for tool in tools.values() %}
            #   循环对我们**只能看到 5 个内部工具**（_SearchToolsTool 等），业务 schema 一个都进不去。
            #   想让业务工具进 planning prompt，必须把它们的 schema 文本预渲染进模板。
            #   现状（字符串 .replace 占位符）与官方 jinja 完全等价——planning 其余占位符（task/
            #   managed_agents）由 smolagents 后续渲染。唯一代价是 planning 段不是"全文 jinja"，
            #   形式上稍欠纯，但避免重写 75 行 _generate_planning_step + 每次升级 diff 维护。
            #   system_prompt 段已完全贴官方 jinja（占位符 8 个全在 populate_template 变量集里）。
            planning = custom_templates.get("planning", {})
            if isinstance(planning, dict) and provider:
                if tools:
                    # 2026-09-15：stage_* 已不是工具；planning 可选范围就是本阶段白名单本身
                    allowed_names = set(str(t) for t in tools)
                elif domain:
                    allowed_names = set(provider.list_by_domain("common") + provider.list_by_domain(domain))
                else:
                    allowed_names = set(provider.list_by_domain("common"))
                tools_text = provider.get_schemas_text(names_filter=allowed_names)
                # 日志口径修正（2026-09-14）：原打印 len(provider)（全量 81），
                # 与"注入了几条 schema"无关——排查时极易误判成"全量 schema 进 prompt"。
                # 这里按 get_schemas_text 的同一过滤条件（base.py:409）数实际条数。
                _injected_n = sum(
                    1 for s in provider.get_schemas()
                    if s.get("function", {}).get("name", "") in allowed_names
                )
                for key in ("initial_plan", "update_plan_pre_messages", "update_plan_post_messages"):
                    val = planning.get(key, "")
                    if isinstance(val, str) and "{{tool_list}}" in val:
                        planning[key] = val.replace("{{tool_list}}", tools_text)
                        # len() 容错（2026-09-16）：provider 无 __len__ 时（测试桩/部分实现）
                        # 注入本身已成功，不该因日志取长度失败而整体回退到默认模板。
                        try:
                            _total = len(provider)
                        except TypeError:
                            _total = _injected_n
                        logger.info("[TaskAgent] YAML planning['%s'] 已注入 %d 个工具 schema"
                                    "（provider 共 %d）", key, _injected_n, _total)

            agent.prompt_templates.update(custom_templates)
            logger.info("[TaskAgent] 已加载自定义 prompt_templates (YAML)")
        except Exception as e:
            logger.warning("[TaskAgent] 自定义 prompt_templates 加载失败: %s，使用默认", e)

        return agent

    async def _execute_phase(
        self,
        task: str,
        agent,
        phase: dict,
        trace: AgentTraceRecorder,
    ) -> str:
        """执行单个阶段。

        phase 格式: {id, name, goal, tools}
        返回: 阶段执行结果字符串
        """
        phase_id = phase.get("id", 0)
        phase_name = phase.get("name", "执行")
        phase_goal = phase.get("goal", "")

        logger.info("[TaskAgent] 执行阶段 %d: %s — %s", phase_id, phase_name, phase_goal)

        react_start = time.time()

        try:
            result = agent.run(task)
        except KeyboardInterrupt:
            logger.warning("[TaskAgent] 阶段 %d 被用户中断", phase_id)
            return f"[中断] 阶段 {phase_name} 被用户中断", {}
        except Exception as e:
            logger.error("[TaskAgent] 阶段 %d 执行异常: %s", phase_id, e)
            trace.record("phase_error", {"phase_id": phase_id, "error": str(e)})
            return f"[错误] 阶段 {phase_name} 执行失败: {e}", {}

        react_elapsed = round(time.time() - react_start, 2)

        trace.record("phase_done", {
            "phase_id": phase_id,
            "elapsed_seconds": react_elapsed,
            "result_preview": str(result)[:200],
        })

        return str(result) if result else ""

    # ── 主对话入口（统一多阶段）──────────────────────────────
    async def _chat_plan_graph(
        self,
        user_input: str,
        session_id: str,
        use_rag: bool,
        event_cb=None,
    ) -> AgentResponse:
        """主对话流程：基于 StateGraph 的编排。

        用 graph.py 的 StateGraph 替代手搓 for 循环：
          - 状态持久化（Checkpointer）
          - 节点隔离（每个节点独立可测）
          - 条件路由（错误时走 fallback）
          - 流式输出（astream）
        """
        from graph import StateGraph, END
        END_SENTINEL = END  # astream 结束事件 node==END，事件回调跳过它
        from nodes import (
            AgentState, NodeContext,
            make_chat_node, make_plan_node,
            make_execute_node, make_finalize_node,
            route_after_chat, route_after_plan, route_after_execute,
        )

        start_time = time.time()
        trace = AgentTraceRecorder(
            agent_type=type(self).__name__,
            session_id=session_id,
            user_input=user_input,
            metadata={"mode": "graph", "use_rag": use_rag, "max_tool_rounds": self.max_tool_rounds},
        )

        try:
            # 创建运行时上下文
            # 实体解析改用 CompositeResolver（2026-09-13 接线修复）：NodeContext 的
            # 契约是 EntityResolver（nodes.py 调 .resolve()），此前注入的却是裸函数
            # _combined_resolver → 调用处 AttributeError 被 chat_node 的
            # `except Exception: logger.debug` 吞掉 ⇒ chat 阶段的实体解析与澄清反问
            # 在线上**从未执行**过（与 L2/L3 同源的"声明了没接线"）。组合逻辑现收敛到
            # resolvers/composite.CompositeResolver，注入即满足契约。
            #
            # 顺序即语义：先标的、后时间。时间解析必须拿到"标的反推的领域"
            # （chat 先于 plan、拿不到 selected_domain），故用工厂按前序 ctx 惰性创建。
            from resolvers.composite import CompositeResolver
            from resolvers.stock import StockResolver
            from resolvers.time import TimeResolver

            def _time_resolver(ctx):
                """由前序识别出的实体类型倒推领域（resolvers/time._ENTITY_DOMAIN），
                否则交易日口径与非交易日澄清整链不生效。"""
                _types = ctx.get("entity_types") or []
                return TimeResolver(entity_type=_types[0] if _types else "")

            ctx = NodeContext(
                llm=self.llm,
                memory=self.memory,
                retriever=self.retriever,
                skill_adapter=self.skill_adapter,
                system_prompt=self.system_prompt,
                memory_window_size=self.memory_window_size,
                max_tool_rounds=self.max_tool_rounds,
                entity_resolver=CompositeResolver([StockResolver(), _time_resolver]),
            )
            ctx.event_cb = event_cb  # SSE 过程事件回调（None=非流式路径零开销）
            ctx.agent = self  # 传递 TaskAgent 实例，供节点调用 _build_code_agent 等方法

            # 构建图
            graph = StateGraph(AgentState)
            graph.add_node("chat", make_chat_node(ctx))
            graph.add_node("plan", make_plan_node(ctx))
            graph.add_node("execute", make_execute_node(ctx))
            graph.add_node("finalize", make_finalize_node(ctx))

            graph.set_entry_point("chat")
            graph.add_conditional_edges("chat", route_after_chat, {
                "plan": "plan",
                "finalize": "finalize",
            })
            graph.add_conditional_edges("plan", route_after_plan, {
                "execute": "execute",
                "finalize": "finalize",
            })
            graph.add_conditional_edges("execute", route_after_execute, {
                "execute": "execute",
                "plan": "plan",
                "finalize": "finalize",
            })
            graph.add_edge("finalize", END)

            # 编译（暂不启用 checkpointer，需要数据库连接池）
            compiled = graph.compile()

            # 执行
            initial_state = {
                "user_input": user_input,
                "session_id": session_id,
                "use_rag": use_rag,
                "_start_time": start_time,
                "_trace": trace,
            }

            # 逐节点流式执行（2026-09-11 SSE 改造）：每个节点完成时经 event_cb 播报
            # 节点生命周期，执行节点产出 step/tool 事件由 _build_code_agent 的钩子上报。
            # node_start 播报：astream 仅按"节点完成"粒度回调，故入口节点先行播报、
            # 后续节点在上一节点完成且路由确定时播报（路由函数与图内共用、纯函数）。
            # event_cb 为 None 时行为与原 ainvoke 等价（只是换用 astream 驱动）。
            result = {}
            _node_cn = {"chat": "意图解析", "plan": "任务规划", "execute": "工具执行", "finalize": "结果整理"}
            _node_next = {"chat": route_after_chat, "plan": route_after_plan, "execute": route_after_execute}

            def _emit_node_start(_n):
                if event_cb is not None and _n in _node_cn:
                    try:
                        event_cb({"kind": "node_start", "node": _n, "label": _node_cn.get(_n, _n)})
                    except Exception:
                        pass

            _emit_node_start("chat")
            async for _evt in compiled.astream(initial_state):
                result = _evt.get("state", result)
                _node = _evt.get("node")
                if event_cb is None or _node == END_SENTINEL:
                    continue
                try:
                    if _evt.get("error"):
                        event_cb({"kind": "node_error", "node": _node,
                                  "error": str(_evt.get("error"))[:300]})
                        continue
                    event_cb({"kind": "node_done", "node": _node,
                              "label": _node_cn.get(_node, _node)})
                    if _node == "execute":
                        _obs = str(result.get("result_raw", "") or "")
                        if _obs:
                            event_cb({"kind": "step_content",
                                      "content": _obs[:1500], "stream": True})
                    elif _node == "plan":
                        _plan = result.get("_agent_plan", "")
                        if _plan:
                            event_cb({"kind": "progress",
                                      "message": "规划完成：" + str(_plan)[:200]})
                    _router = _node_next.get(_node)
                    if _router is not None:
                        _emit_node_start(_router(result))
                except Exception:
                    pass  # 事件通道故障不阻断主流程

            final_output = result.get("final_output", {})
            response = AgentResponse(
                content=result.get("result_raw", ""),
                sources=result.get("sources", []),
                session_id=session_id,
                elapsed_seconds=result.get("elapsed", 0),
                metadata={
                    "trace_id": trace.trace_id,
                    "phase_count": len(result.get("phases", [])),
                    # B 阶段（2026-09-12）：契约阶段无 "type" 键，退化为阶段名列表
                    # （旧实现取 p["type"] 会得到一串 None；保持字符串数组的对外契约）
                    "phase_types": [str(p.get("name") or "phase")
                                    for p in result.get("phases", []) if isinstance(p, dict)],
                    "final_output": final_output,
                },
            )
            trace.finish(response=response.to_dict())
            return response

        except Exception as e:
            trace.fail(e)
            raise

    @staticmethod
    def _infer_var_type(value) -> str:
        """推断变量类型摘要（用于注入 phase_task）。"""
        if isinstance(value, dict):
            keys = list(value.keys())[:5]
            return f"dict({', '.join(keys)}{'...' if len(value) > 5 else ''})"
        if isinstance(value, (list, tuple)):
            return f"list[{len(value)}]"
        if isinstance(value, str):
            return f"str[{len(value)}字符]" if len(value) > 50 else repr(value[:50])
        if isinstance(value, (int, float, bool)):
            return repr(value)
        return type(value).__name__

    @staticmethod
    def _is_serializable(value) -> bool:
        """判断变量是否可序列化（过滤模块、函数、类等）。"""
        import types
        if isinstance(value, (types.ModuleType, types.FunctionType, types.MethodType, type)):
            return False
        try:
            import json
            json.dumps(value, default=str)
            return True
        except (TypeError, ValueError):
            return False
