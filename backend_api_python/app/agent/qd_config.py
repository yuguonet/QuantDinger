# -*- coding: utf-8 -*-
"""QDAgentConfig —— mimo 原版 MimocodeAgentConfig 之上只加增强项。

从 `qd_agent.py` 拆出（2026-10-13 P3 拆分）。本模块是**纯配置**：不 import 任何
执行核/工具面模块，只依赖 mimo config 与 os.getenv —— 这样配置能被单独测试，
也不会在配置期触发工具面冷启动。

【每个 env 旋钮为什么存在】都在对应字段的注释里（多数记着一次实测事故）。
改动前请先读注释，尤其是这几条踩过坑的：
  · `max_observation_length` —— 上游 mimo 显式设 0（关闭），但本项目 102 个
    金融工具走 FnToolAdapter 且**没有自我限流** ⇒ 继承 0 = 放弃唯一字符闸门。
  · `compaction_context_window` —— mimo 默认 1_000_000，而我们的窗口是十万级
    ⇒ 压缩提示永远不会出现。且**不能**拿 OPENAI_MAX_TOKENS 当窗口（量纲不同）。
  · `run_token_budget` / `run_step_cap` —— 光有 step_limit 不够：步数便宜、
    上下文贵（实测一轮 15 次调用烧 40 万 token 且零产出）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from mimoagent.agents.antihack import AntiHackConfig
from mimoagent.agents.mimocode.mimocode_agent import MimocodeAgentConfig

from .upstream_compat import core_tool_names

logger = logging.getLogger(__name__)


# ── T7 快速接入/断开：统一读口（2026-10-16；2026-10-17 接线 + fail-loud）──────
# 【设计口径】方案 §8.3 要的是"接入=加一行、断开=置位"的三态开关：
#   resident（常驻）/ deferred（注册但延迟暴露）/ disabled（不注册）。
#   ★ 2026-10-17 补第四态 capability（方案 §8.3 示例第 4 行 / §8.4"按域暴露"）：
#     注册、按需激活、目录里按组呈现——能力层的正式状态名（与 deferred 同路径）。
# 【落地取舍】**不新建平行体系** —— 本项目各域早已各有关闭/延迟开关
#   （provider：不注册 / 能力层：admission.json / 工具：on_demand_tools /
#    技能：skills_adapter 的 env）。故此处只做**统一读口**：
#     · `QD_TOOL_CONNECT` 一份覆盖表（`name=state`，用 ; 或 , 分隔）；
#     · 未覆盖的项由既有 env 派生（默认口径与现状一致）；
#     · 只回答"某单位处于什么状态"，不接管注册/执行（避免双写风险）。
# 【2026-10-17 G1 接线】读口自此被 `qd_tool_face._partition_tools` 消费
#   （经 `tool_connect_overrides()`）：改 `QD_TOOL_CONNECT` 下一 run 生效。
#   此前零调用方 ⇒ 开关是哑炮（审计报告 G1）。
# 【2026-10-17 G4 fail-loud】非法 state 值（拼错如 defered/disable）不再静默
#   兜底成 resident——那正是"禁止兜底"红线要修的行为：ERROR 日志（每条目一次）
#   + 登记进 `tool_connect_invalid()` 供 tool_face 快照审计，该条被忽略。
#   不 raise：这是请求级运行时读取，raise 会把配置笔误升级成 500；
#   "留痕可观测"即是修（与 grounding/T2 那轮 None 不回落 0 同一条纪律）。
_TOOL_CONNECT_STATES = ("resident", "deferred", "disabled", "capability")

# 非法 state 登记表（进程级，逐条一次）：供 tool_face_snapshot 的 connect_invalid
_TOOL_CONNECT_INVALID: set = set()


def _tool_connect_defaults() -> "list":
    """本文件 `on_demand_tools` 的代码默认值（读口与分层共用，避免两处漂移）。

    ★ `os.getenv(...) or 默认` 语义：env 未设**或为空串**都回落代码默认
    （9 个）。旧读口曾用 None-vs-"" 双语义（空串 ⇒ 空集），与
    `QDAgentConfig.default_factory`（走本函数）口径不一致 ⇒ 读口说 resident、
    真实分层是 deferred（2026-10-17 锤实的第二处漂移，随接线一并消灭）。

    【F11 加 bash（2026-10-17，E2/R2）】实测 `bash` 单工具 schema **4,280 字符
    = 核心面（11,281）的 38%**，而现场 trace 全程未调用 ⇒ 按「提智不降智、总
    token 越小越好」降级为按需。不是删能力：仍注册、仍可点名激活 / `search_tools`
    发现 / execute_action 直调 fail-open（三入口可见性 T1 已补），只是不再每步白挂。
    """
    return [w.strip() for w in
            (os.getenv("QD_ON_DEMAND_TOOLS")
             or "actor,run_skill,task,edit,read,write,glob,grep,bash").split(",") if w.strip()]


def _flag_tool_connect_invalid(item: str) -> None:
    """登记并 ERROR 报警一条非法 `QD_TOOL_CONNECT` 条目（每条目一次）。"""
    if item in _TOOL_CONNECT_INVALID:
        return
    _TOOL_CONNECT_INVALID.add(item)
    logger.error(
        "[tool_connect] 非法 state 值已忽略：%r（合法值 %s）——"
        "该条不生效，请修 env；本条已登记进 tool_face 快照 connect_invalid",
        item, "/".join(_TOOL_CONNECT_STATES))


def tool_connect_overrides() -> Dict[str, str]:
    """解析 `QD_TOOL_CONNECT` 覆盖表 ⇒ {name/group: state}（非法条忽略+fail-loud）。

    ★ 解析器唯一事实源：`tool_connect()` 读口与 `qd_tool_face` 分层共用，
    避免"读口一套解析、消费方另一套"的第三处漂移。组级条目（quote/coding/…）
    原样返回，由消费方按成员展开（`preselect/tool_groups.expand_overrides`）。
    """
    out: Dict[str, str] = {}
    raw = (os.getenv("QD_TOOL_CONNECT") or "").strip()
    if not raw:
        return out
    for item in raw.replace(",", ";").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        k, v = item.split("=", 1)
        key, val = k.strip().lower(), v.strip().lower()
        if not key:
            continue
        if val not in _TOOL_CONNECT_STATES:
            _flag_tool_connect_invalid(f"{key}={val}")
            continue
        out[key] = val
    return out


def tool_connect_invalid() -> tuple:
    """进程内已登记的全部非法 `QD_TOOL_CONNECT` 条目（供快照/审计）。"""
    return tuple(sorted(_TOOL_CONNECT_INVALID))


def tool_connect(name: str) -> str:
    """查某工具/域/能力/技能单位的暴露状态（env-only 读口，回答"默认配置下"）。

    `QD_TOOL_CONNECT="coding=deferred;ability=disabled"` 可覆盖（也接受逗号分隔；
    大小写/空格容差）。未覆盖时按既有开关派生，保证**默认行为与改造前逐位一致**：
      · 在 `QD_ON_DEMAND_TOOLS` 名单里 ⇒ deferred
      · 能力层且 `QD_CAPABILITIES=0` ⇒ disabled
      · 其余 ⇒ resident
    ★ 与 live 面的关系：真实分层（`_partition_tools`）以 **config.on_demand_tools
    为基座**、本覆盖表赢之；本函数是无 config 时的回答者（测试/探针/smoke 用）。
    显式 config 覆盖（如 on_demand_tools=[]）在 live 面优先——这是刻意优先级：
    显式配置 > env 默认。
    """
    key = str(name or "").strip().lower()
    if not key:
        return "resident"
    ov = tool_connect_overrides()
    if key in ov:
        return ov[key]
    if key in set(_tool_connect_defaults()):
        return "deferred"
    if key in ("capability", "capabilities", "ability", "abilities"):
        if (os.getenv("QD_CAPABILITIES") or "1").strip() in ("0", "false", "False"):
            return "disabled"
    return "resident"

def _load_system_template() -> str:
    """精简 Agent 行为基座 + 金融域覆盖层（后置，指令晚者优先）。

    2026-10-11 token 瘦身：原先直接复用 mimoagent 的 `_CORE_PROMPT`
    （mimocode_core.txt，9948 字符 ≈ 4960 tokens/轮），其大半是"写代码/重构/
    开 dev server"等**软件工程指令**，与 A 股研究助手无关，却是每轮必发的固定死重。
    改用本项目自有精简基座 `prompts/qd_core_base.txt`（≈900 字符），只保留真正
    影响工具循环的行为（上下文有限/注入防御/原生工具清单/优先专用工具/
    工具结果当数据/{{cwd}}）；金融纪律仍由 qd_system.txt 后置覆盖。
    """
    here = Path(__file__).resolve().parent / "prompts"
    base = (here / "qd_core_base.txt").read_text(encoding="utf-8").strip()
    layer = (here / "qd_system.txt").read_text(encoding="utf-8").strip()
    return base + "\n\n---\n\n" + layer


# 压缩摘要金融化定制（提智点 D）：compact 默认摘要偏通用，金融细节易丢。
# 要求摘要必保：数字+来源、标的代码、已有结论、未完事项、用户偏好。
_QD_COMPACT_NOTES = (
    "Compressed summary must preserve, in Chinese where natural: "
    "(1) every number cited so far with its source tool name; "
    "(2) stock/board codes and names discussed; "
    "(3) conclusions already established and their evidence; "
    "(4) open threads / pending checks; (5) user preferences stated; "
    "(6) any 【历史相似案例——仅作参考，不要照搬结构】 or [参考资料] injection blocks "
    "present in the conversation, verbatim and in full — never truncate or "
    "paraphrase them. "
    "Drop tool boilerplate, retries and failed attempts first."
)
# ── 第 (6) 条为什么必须写（v5 P1-C3，2026-10-13）──────────────────────────
#   mimo 的压缩 rebuild 是 `[system…, boundary, anchor, summary]`：
#   anchor（surgery.py:47-65）把**首轮 user 消息**原样包进 <original_user_issue>，
#   而 qd_service.py:447 的金例/资料注入块正是拼在首轮 user 里的
#   ⇒ **注入块由 anchor 保护，理论上不会丢**。
#   但 summary 是**模型自己写的**：若摘要指令没点名这些块，模型可能
#   ① 在摘要里误以为已涵盖而省略；② 后续轮次模型看不到完整块时无从察觉。
#   本条是**纵深防御**：让摘要侧也明确不许吞掉它们。
#   ⚠️ 块名必须是上面那两个**真实前缀** —— v5-实施计划 C3 原文写的 `[参考案例]`
#      是笔误（实际金例前缀是全角的「【历史相似案例——仅作参考，不要照搬结构】」）。
#      指纹一旦漂移，tests/test_v5_compaction_guard.py 会当场红。


# ── 工具分层口径（2026-10-01）──────────────────────────────────
# 必注入层 = mimo 原生工具 + 这两个域（tools/ 顶层 common + 元工具 meta）。
# 其余子域（finance / knowledge …）= 按需层，默认**不下发 schema**。
# 依据：全量 73 个工具 schema ≈ 12.8k tokens，而 mimo 每轮请求整包下发
# （DefaultAgent.get_model_query_kwargs 返回 registry 全量定义）⇒ 多轮即 token 爆炸。
CORE_TOOL_DOMAINS = ("common", "meta")


# ── 瞬态控制消息（跨轮防泄漏口径，2026-10-14）──────────────────────────
# 【实测事故·002866】会话内上一轮「现在买什么股?」烧穿 per-run token 预算
# （run_token:202346/200000），`qd_budgeting._account_budget` 抛
# `LimitsExceeded(budget_wrapup_hint(...))`；mimo `BaseAgent.run` 捕获后
# `add_message('user', str(e))` 并返回 `('LimitsExceeded', str(e))`。于是这条
# **控制指令**（"已停止继续探索…不要再展开新的取数"）同时留在：
#   ① 会话复用同一 QDAgent 的 `self.messages`（role=user，永久污染下一轮）；
#   ② `qd_service._run_sync` 把返回文本当 assistant 落进记忆库。
# 下一轮「分析002866」（预选已正确点名 get_realtime_quote 等 10 个工具）被这条
# 旧指令带偏 ⇒ **零工具调用**、直接把全部字段写成「未获取」
# （trace: status=Idle, steps=1, children=0）。
#
# 本表是这几类**系统生成、非用户输入**的控制语的唯一指纹口径，供两处消费：
#   · `qd_agent._run_inner` 开轮前从 `self.messages` 清除（防跨轮泄漏）；
#   · `qd_service._should_store_assistant` 跳过 assistant 记忆写入（防入库污染）。
# ⚠ 只认**固定前缀/整体**：宁可漏判（当普通文本）也不误判用户问题（会静默丢输入）。
TRANSIENT_CONTROL_MARKERS = (
    "（本轮已触及成本上限：",       # utils/budget.budget_wrapup_hint（成本熔断）
    "（本轮达到步数上限被截断",     # qd_agent._run_inner 步数耗尽兜底文案
    "（模型未返回文字内容",         # qd_agent._run_inner 空回复兜底文案
    "请用文字直接给出本轮结论",     # qd_agent._run_inner 空回复轻推 prompt
    "【数字溯源拦截】",             # qd_agent._run_inner 溯源重写 prompt
    "Empty assistant response",     # mimo BaseAgent.query 空响应字面量
)


def is_transient_control(text: Any) -> bool:
    """一段文本是否是**内部瞬态控制消息**（不得跨轮/入库）。

    只看开头是否命中 `TRANSIENT_CONTROL_MARKERS` —— 这几个文案全部由系统生成，
    不会以它们开头出现在真实用户提问或正常答复里。
    """
    s = str(text or "").strip()
    if not s:
        return False
    return s.startswith(TRANSIENT_CONTROL_MARKERS)


# 成本熔断软收尾提示的固定前缀（= TRANSIENT_CONTROL_MARKERS 首项）。
# 单独一个口径供 `qd_agent._run_inner` 判定"本轮是超预算被中断"——
# 命中它就必须走一次纯文字收尾（把已取数据变成部分结论），不能原样透传。
_BUDGET_WRAPUP_PREFIX = TRANSIENT_CONTROL_MARKERS[0]


def is_budget_wrapup(text: Any) -> bool:
    """该文本是否是**成本熔断软收尾提示**（`utils/budget.budget_wrapup_hint`）。

    与 `is_transient_control` 的区别：后者是一大类瞬态控制消息的总闸（用于清污染），
    本函数只认"超预算熔断"这一种，因为它触发的是**另一条处理路径**（冻结预算 +
    禁用工具的纯文字收尾），而不是简单丢弃。
    """
    return str(text or "").strip().startswith(_BUDGET_WRAPUP_PREFIX)


def _context_window() -> int:
    """模型上下文窗口（token）——compaction 与 wrap_up 的**唯一**口径。

    取值优先级：`QD_CONTEXT_WINDOW` > `QD_COMPACT_WINDOW`（旧名兼容）> 65536。

    【为什么 OPENAI_MAX_TOKENS 不能当真值】
    它在 `app/agent/agent.py:44` 是被当 **max_tokens（单次输出上限）** 消费的，
    与输入窗口是两个量纲。实测反证：6 步的选股任务单轮 input 已到约 45k 仍请求
    成功 ⇒ 真实输入窗口必然 > 45k；若窗口真是 32k，那次请求早就 400 了。

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


def _turn_compact_threshold() -> int:
    """轮界主动压缩阈值（F12，C2）。

    优先级：QD_TURN_COMPACT_THRESHOLD > QD_TURN_COMPACT_RATIO*窗口 > 默认 0.30。
    QD_TURN_COMPACT=0 时返回 0（关闭）。
    """
    # 一键回退
    if str((os.getenv("QD_TURN_COMPACT") or "").strip()).lower() == "0":
        return 0
    raw_thr = (os.getenv("QD_TURN_COMPACT_THRESHOLD") or "").strip()
    if raw_thr:
        try:
            v = int(raw_thr)
            return max(0, v)
        except ValueError:
            logger.warning("[ctx] QD_TURN_COMPACT_THRESHOLD 非整数，忽略: %r", raw_thr)
    try:
        ratio = float(os.getenv("QD_TURN_COMPACT_RATIO", "0.30"))
    except ValueError:
        ratio = 0.30
    ratio = min(max(ratio, 0.05), 0.95)
    return max(0, int(_context_window() * ratio))


def _activate_priority_enabled() -> bool:
    """F24 激活优先级栈开关（默认开）。

    `QD_ACTIVATE_PRIORITY=0/false/off` ⇒ 退回纯 FIFO 淘汰（旧行为）。
    为什么默认开：FIFO 只按插入序淘汰，会让 `search_tools` 中途检索自动激活的
    工具把模型**刚点名**的那几个挤掉（点名不生效 = 降智）。旋钮只为出问题时一键回退。
    """
    return str(os.getenv("QD_ACTIVATE_PRIORITY", "1")).strip().lower() not in ("0", "false", "off")


@dataclass
class QDAgentConfig(MimocodeAgentConfig):
    """mimo 原版 MimocodeAgentConfig 之上只加增强项。"""
    system_template: str = field(default_factory=_load_system_template)
    instance_template: str = "Your task: {{task}}"
    # 原版核心 7 件 + compact（模型自主上下文压缩，长程任务关键）
    # + actor（并行子代理，多线索分析；并发受 actor_max_concurrency 约束）
    # F15b：核心工具名经 upstream_compat 取（上游改名 → 告警，不再静默空表）
    tools: List[Any] = field(
        default_factory=lambda: [{"tool": n} for n in core_tool_names()]
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
    # ── observation 字符闸门（2026-10-12 P0 修复）──────────────────────────
    # 上游 MimocodeAgentConfig 把 max_observation_length 显式设为 **0（关闭）**，
    # 理由是 MiMo 自带工具目录自我限流（bash 2000 行/50KB、read 2000 行/50KB、
    # grep/glob 100 条），再叠一层字符截断会把"工具自称未截断"的中段悄悄砍掉。
    # 但本项目 **102 个金融工具走 FnToolAdapter，输出没有任何自我限流**
    # （tools/_base.py 全程无 truncate）⇒ 继承这个 0 = 放弃了唯一的字符闸门。
    # 生效路径已核实：DefaultAgent._format_observation → truncate_middle(out, 本值)，
    # MimocodeAgent 是 super() 调用后再拼 <context_usage> 页脚，故本值有效。
    # 设 QD_MAX_OBSERVATION_CHARS=0 可退回上游行为。
    max_observation_length: int = field(
        default_factory=lambda: int(os.getenv("QD_MAX_OBSERVATION_CHARS", "30000")))
    # ── 成本硬预算（2026-10-12 P0 修复）────────────────────────────────────
    # 光有 step_limit 一个刹车不够：**步数便宜、上下文贵**。实测「明天买什么股」
    # 一轮 15 次调用、累计 ≈40 万 token、上下文每轮 ~29k tokens（其中工具 schema
    # 就占 ~22k），且**零产出**——21 步的步数预算对 token 毫无约束力。
    # 阈值沿用 utils/budget.py 既有 env（该模块此前零消费方），不另起一套口径。
    run_token_budget: int = field(
        default_factory=lambda: int(os.getenv("AGENT_RUN_TOKEN_BUDGET", "200000")))
    run_tool_call_budget: int = field(
        default_factory=lambda: int(os.getenv("AGENT_RUN_TOOL_CALL_BUDGET", "60")))
    # 整个请求（含空回复轻推轮 + 溯源重写轮）的**累计步数**上限。
    # 0 = 自动取 step_limit + run_step_grace（给重试轮留几步，不让它们瞬死）。
    run_step_cap: int = field(default_factory=lambda: int(os.getenv("QD_RUN_STEP_CAP", "0")))
    run_step_grace: int = field(default_factory=lambda: int(os.getenv("QD_RUN_STEP_GRACE", "3")))
    # 闭环③：交易人工确认闸（interceptor；函数内 confirm 硬闸保留）
    trading_confirm: bool = True
    # 危险面守卫
    danger_guard: bool = True
    # ── 工具分层 ──
    # tiered（默认）：只下发必注入层 + 已激活的按需工具；
    # all：退回"全量下发"（排查/对照用；QD_TOOL_TIER_MODE=all）
    tool_tier_mode: str = field(
        default_factory=lambda: (os.getenv("QD_TOOL_TIER_MODE", "tiered").strip().lower() or "tiered"))
    # 单次会话最多同时激活多少个按需工具（F24 起按**优先级栈**淘汰，见 qd_tool_face）。
    # 太大 ⇒ 分了等于没分；太小 ⇒ 多线索任务来回换手。默认 20。
    # 【F6 抬闸（2026-10-17）】原值 12 = 「点名上限 8 + 技能补位 4」的数值巧合；
    #   但 F6 裁定「技能声明工具是方法论合同，补位不截断」⇒ 技能声明可达 15 个
    #   （auto-stock-analyze），12 会在 FIFO 淘汰里**顶掉模型刚点名的工具**。
    #   抬到 20 让「点名(≤8) + 技能声明(≤15)」在常见情形下不触发淘汰；真正的
    #   容量治理与优先级（点名 > 声明补位 > 域兜底）已由 F24 优先级栈实现
    #   （qd_tool_face._ACTIVATION_SOURCE_RANK，回退旋钮 QD_ACTIVATE_PRIORITY=0）。
    max_active_domain_tools: int = field(
        default_factory=lambda: int(os.getenv("QD_MAX_ACTIVE_TOOLS", "20")))
    # 额外强制进必注入层的工具名（例：高频金融工具，避免每次都要激活一轮）
    extra_core_tools: List[str] = field(default_factory=lambda: [
        n.strip() for n in os.getenv("QD_EXTRA_CORE_TOOLS", "").split(",") if n.strip()])
    # 反向旋钮：把本来必注入的工具**降级**为按需（token 再压缩用）。
    # 默认降级 actor + run_skill：
    #   · actor（≈0.8k tokens/轮）是并行子代理，单线任务用不到；mimo 上游
    #     MIMOCODE_CORE_TOOL_NAMES 本就**不含** actor/compact，它们是我们自己补挂的。
    #     compact 是长上下文压缩手段，必须常驻。
    #   · run_skill（≈0.3k/轮，2026-10-02 新增）只服务"全市场扫描/策略诊断"这类批量
    #     流水线，命中率极低 ⇒ 每轮白挂不划算。降级后仍拿得到，三条兜底：
    #     ① 分层 fail-open（execute_action）：未激活直调就地激活放行；
    #     ② 工具预选目录由 _on_demand_names 构建 ⇒ 相关任务首轮就能被点名激活；
    #     ③ system_template 的"重活分发"小节直接点名 run_skill，模型知道名字。
    # 【2026-10-16 T2】默认再追加 7 个**原生编码工具**（task/edit/read/write/glob/grep）：
    #   现场 trace 的 30 次工具调用里，这 6 类**一次都没出现**（业务走域工具 + 技能
    #   流水线 run_skill）；8 个 SKILL.md 对它们零引用，market-screener/run.py 甚至
    #   明写"禁止在技能内二次调用"。21 个必注入工具实测 12,486 tokens/轮，原生
    #   agentic 家族占 9,673（77.5%）⇒ 移出后每轮 ≈5.6k。
    #   【不是删能力】工具仍在注册表、仍由执行器执行；地图（tool_map）用中文列出
    #   "coding（N 个）… 组内名单"，点名即 activate；直调走 execute_action 的
    #   fail-open 就地激活。三入口（地图/list_tools/search_tools）均已补可见性（T1）。
    #   【F11：bash 也降级（2026-10-17）】原此处置「bash 是"能跑命令"的兜底通路
    #   （技能脚本调试），单独一步待定」。实测 bash schema 4,280 字符 = 核心面
    #   38%（单工具最贵）且现场零调用 ⇒ 已并入本名册按需化。技能脚本调试若真需要，
    #   走三入口（地图/list_tools/search_tools）点名或 execute_action 直调 fail-open。
    #   compact 与会话收尾相关，常驻（不降级）。
    #   【怎么回退】QD_ON_DEMAND_TOOLS="actor,run_skill" 即恢复 T2 前的行为；
    #   要单独把 bash 放回必注入层，用不含 bash 的名册覆盖即可（非 FnToolAdapter
    #   在 resident 分支恒必注入，见 qd_tool_face._partition_tools）。
    on_demand_tools: List[str] = field(default_factory=_tool_connect_defaults)
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
