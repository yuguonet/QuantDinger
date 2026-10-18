# -*- coding: utf-8 -*-
"""工具面 —— 注册 / 分层 / 激活 / 预热 / 地图。

从 `qd_agent.py` 拆出（2026-10-13 P3 拆分）。这是执行核里最大的一块（原 ~450 行），
管的是「模型每轮**看得到**哪些工具」这件单一的事。

【分层的由来】全量 73 个工具 schema ≈ 12.8k tokens，而 mimo 每轮请求**整包下发**
（`DefaultAgent.get_model_query_kwargs` 返回 registry 全量定义）⇒ 多轮即 token 爆炸。
故切成两层：
  · 必注入层（core）：mimo 原生工具 + domain ∈ {common, meta}，每轮都下发；
  · 按需层（on_demand）：子域金融/知识工具，默认**不下发**，靠预选或
    search_tools→activate_tools 激活后才进本轮 schema。

【三条易错点，都踩过】
  1. 元工具（web_search / format_result）在 `tools/_base.py` 的 `_MUST_HAVE` 名单里
     ⇒ `scan_directory` 跳过 ⇒ `provider.get_functions()` 里**没有** web_search。
     迁移 mimo 后只取 get_functions() ⇒ 模型根本没有 web_search 可调，而 system
     prompt 却硬性要求"实时信息必须调用 web_search" ⇒ 天气类任务模型撞墙瞎答。
     ⇒ 元工具必须一并收进来并标 domain="meta"（见 `_collect_tool_functions`）。
  2. 层 fail-open：模型可能直接点名"已注册但本轮未激活"的按需工具（上一轮文档里
     见过名字/网关透传）。原版抛 ToolException(Unknown tool) 让它卡住重试；
     `execute_action` 里就地激活放行 —— 多花一步 schema，好过整轮死胡同。
  3. 工具地图必须**明确告诉模型"已激活的可以直接调"**。首版没写已激活段，地图里
     按需层一律标"需先 search_tools" ⇒ 模型照旧走三轮发现，预选白做
     （实测 stock_screen 发现轮一次没少、token 反而 +21%）。
"""
from __future__ import annotations

import logging
import os
import time as _time
from datetime import datetime
from typing import Any, Callable, List, Optional

from mimoagent.tools.mimocode import MimocodeToolRegistry
from .tools._fn_adapter import FnToolAdapter
from .preselect import capability_domain, expand_overrides, group_members
from .qd_config import (
    CORE_TOOL_DOMAINS,
    _activate_priority_enabled,
    tool_connect_overrides,
)

logger = logging.getLogger(__name__)


# ── F24 激活来源优先级（序数越大越晚淘汰）─────────────────────────────
#  【为什么】淘汰点全仓唯一（下方 `activate_tools`），原为纯 FIFO：`search_tools`
#  中途检索自动激活的 top-N（`tool_discovery`，source="auto"）会把模型**刚点名**
#  的工具从 `_activated` 挤掉 ⇒ 点名不生效 = 降智。
#  顺序：模型点名 > 技能卡片/声明补位 > 预选 > 域兜底 > 自动检索。
_ACTIVATION_SOURCE_RANK = {
    "auto": 0,       # search_tools 自动激活（栈底，最先被淘汰）
    "fallback": 1,   # 预选域兜底
    "preselect": 2,  # LLM 预选 / 确定性快路由
    "declared": 3,   # 技能卡片声明的 force 工具
    "named": 4,      # 模型显式点名（activate_tools / fail-open 直调）
}

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
        from .capabilities import register_capabilities
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
        from .tools._base import ToolProvider
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


class ToolFaceMixin:
    """工具面：以 mixin 形式并进 QDAgent（必须排在 MimocodeAgent 之前）。

    ★ MRO 红线：本 mixin 的 `_build_tool_registry` 里调 `super()._build_tool_registry()`，
    若排到 MimocodeAgent 之后就再也碰不到 mimo 原版实现 ⇒ 工具面空掉且**不报错**。
    `tests/test_v6_qd_agent_split.py::test_mro_places_mixins_before_mimo_core` 守这条。
    """

    # ── 工具分层 ────────────────────────────────────────────────
    def _partition_tools(self) -> tuple:
        """按「config 基座 + T7 覆盖」派生 (core, on_demand, disabled, states)。

        ★ T7 接线（2026-10-17，审计 G1）：分层不再裸读 config.on_demand_tools，
        而是经 `tool_connect_overrides()` 叠加 `QD_TOOL_CONNECT` 覆盖——
        此前读口零消费方，改 env 对工具面零效果（哑炮，`tmp/t7_improvement_gaps_
        20261017.md` G1）。推导链（单一推导；states 供 tool_face 快照复用）：
          base     = name ∈ config.on_demand_tools ? deferred : resident
                     （显式 config 口径优先于 env 默认——刻意优先级）
          override = QD_TOOL_CONNECT 覆盖（**组级条目先经 tool_groups
                     expand_overrides 展开到成员**，单项优先、组间冲突
                     disabled 最严格胜出；再赢过 base）
          cap_off  = domain==capability 且 QD_CAPABILITIES=0 ⇒ disabled
          之后     = resident 走既有规则（extra_core→core / 非 FnToolAdapter→core /
                     domain∈CORE_TOOL_DOMAINS→core / 其余 on_demand）
        ★ 默认 env 下与改造前逐位一致——v6 分层测试 + p9 基线是判据。
        """
        forced_on_demand = set(self.config.on_demand_tools or ())
        overrides = expand_overrides(tool_connect_overrides(),
                                     group_members(self.tool_registry))
        cap_off = (os.getenv("QD_CAPABILITIES") or "1").strip() in ("0", "false", "False")
        cap_dom = capability_domain()
        core: List[str] = []
        on_demand: List[str] = []
        disabled: List[str] = []
        states: dict = {}
        for name in self.tool_registry.list_tools():
            try:
                tool = self.tool_registry.get(name)
            except Exception:
                tool = None
            domain = getattr(tool, "domain", "") or ""
            state = overrides.get(name.lower()) or (
                "deferred" if name in forced_on_demand else "resident")
            if state == "resident" and cap_off and domain == cap_dom:
                state = "disabled"      # 能力层总闸：QD_CAPABILITIES=0 整域下线
            states[name] = state
            if state == "disabled":
                disabled.append(name)   # 注册表与 defs 仍在 ⇒ 回退可救（§8.3）
                continue
            if state in ("deferred", "capability"):
                on_demand.append(name)
                continue
            # resident：既有规则不动（2026-09-19 故障的直接机制就是原生工具掉出必注入）
            if name in (self.config.extra_core_tools or []):
                core.append(name)
            elif not isinstance(tool, FnToolAdapter):
                core.append(name)          # mimo 原生 / update_plan
            elif domain in CORE_TOOL_DOMAINS:
                core.append(name)
            else:
                on_demand.append(name)
        return core, on_demand, disabled, states

    def _sync_tool_tiers(self) -> None:
        """快照工具定义并按域切分必注入层 / 按需层 / 禁用层。

        【必注入层的判定】非 FnToolAdapter（mimo 原生 + update_plan）恒必注入；
        FnToolAdapter 看 domain：common（tools/ 顶层）与 meta（元工具：web_search /
        format_result）必注入，子域（finance/knowledge…）按需。
        【三分组来源】`_apply_tool_connect()`（T7 覆盖在其内叠加）。
        """
        defs = {}
        for d in self.tool_registry.get_function_definitions():
            name = (d.get("function") or {}).get("name")
            if name:
                defs[name] = d
        self._defs_by_name = defs
        self._all_defs = [defs[n] for n in self.tool_registry.list_tools() if n in defs]
        self._apply_tool_connect()

    def _apply_tool_connect(self) -> None:
        """（重）算 T7 三分组：core / on_demand / disabled + 生效态字典。

        【热生效载体】改 `QD_TOOL_CONNECT` 后**下一 run** 工具面即变，
        无需重启进程（方案 §8.6「无重启」验收项）。挂在 `reset_run_activation()`
        （`_run_inner` 每 run 已调用）⇒ 不新增 qd_agent 行数（420 行护栏）。
        【为什么不重建 defs】`_defs_by_name` 是全量 schema 快照，与 T7 状态
        无关；本方法只做 O(工具数) 的字符串/集合比较，零网络零 schema 序列化。
        """
        core, on_demand, disabled, states = self._partition_tools()
        self._core_names = core
        self._on_demand_names = on_demand
        self._disabled_names = disabled
        self._connect_states = states

    def reset_run_activation(self) -> None:
        """每 run 开头清空按需层激活 + 刷新 T7 三分组（P2-c + T7 热生效）。

        【为什么按 run 清空】`_activated` 原为**会话级**累积（2026-10-14 G4 旧注
        「多轮里上轮激活的工具还可能用得上」），实测每会话多漏 ≈2,280 tok schema
        （审计文档 §三）。会话级在新架构下已无必要：按需层本就是"用什么激活什么"，
        上轮激活过、本轮又被点名的工具由 `execute_action` 的 fail-open 就地再激活。
        【必须在预选之前】`qd_agent._run_inner` 在 `_maybe_preselect_tools` 之前调用，
        否则残留项会挤占 FIFO 名额、顶掉本轮预选成果。
        【T7 热生效（2026-10-17）】同点刷新三分组：`_activated` 清空后重算
        core/on_demand/disabled，保证"改配置 ⇒ 下一 run 生效"且不带着上轮的
        禁用/延迟残留。顺序在清空**之后**：先丢激活态，再按新配置分组。
        """
        self._activated = []
        self._activated_source = {}
        self._apply_tool_connect()

    def _src_map(self) -> dict:
        """惰性取/建「工具 → 来源优先级序数」表（F24）。

        【为什么惰性】ToolFaceMixin 的测试 stand 只设 `_activated`（不跑 `__init__`），
        故不能假定 `_activated_source` 已建；这里按需补建，兼容真实 agent 与 stand。
        """
        m = getattr(self, "_activated_source", None)
        if m is None:
            m = self._activated_source = {}
        return m

    def activate_tools(self, names: List[str], *, source: str = "named") -> dict:
        """激活按需层工具（模型经 activate_tools / search_tools 调用）。

        返回 {"activated","already","unknown","disabled","active_total","cap"}。
        超容量时按 **来源优先级** 淘汰（F24）：序数小者先淘汰，同序数退回 FIFO（早插先淘汰）。
        `source` ∈ {"named","declared","preselect","fallback","auto"}；非法值直接
        `ValueError`（fail-loud，不静默归 named —— 避免误用悄悄降保护级）。
        `QD_ACTIVATE_PRIORITY=0` ⇒ 一键退回纯 FIFO 淘汰。

        ★ T7 disabled（2026-10-17）：`QD_TOOL_CONNECT` 置 disabled 的工具
        **不接受激活**（进 `disabled` 桶，调用方可见）——否则 trace 的
        active_tool_names 会虚报"已激活"而面里根本没有（审计对不上账）。
        但**直调回退仍放行**：`execute_action` 的 fail-open 不依赖本函数的
        返回值，registry 持有实例 ⇒ 模型直接点名照样执行（§8.3「回退可救」）。
        """
        if source not in _ACTIVATION_SOURCE_RANK:
            raise ValueError(
                f"unknown activation source {source!r}; "
                f"expect one of {sorted(_ACTIVATION_SOURCE_RANK)}")
        rank = _ACTIVATION_SOURCE_RANK[source]
        cap = max(1, int(self.config.max_active_domain_tools))
        activated, already, unknown, disabled = [], [], [], []
        src = self._src_map()
        off = set(getattr(self, "_disabled_names", None) or [])
        for n in (names or []):
            n = str(n).strip()
            if not n:
                continue
            if n in off:
                disabled.append(n)
            elif n not in self._defs_by_name:
                unknown.append(n)
            elif n in self._core_names or n in self._activated:
                already.append(n)
                # 更强的来源再次点名既激活项 ⇒ 升级其保护级（如 auto 激活后被模型点名）
                if n in self._activated and rank > src.get(n, -1):
                    src[n] = rank
            else:
                self._activated.append(n)
                src[n] = rank
                activated.append(n)
        while len(self._activated) > cap:
            if _activate_priority_enabled():
                idx = min(range(len(self._activated)),
                          key=lambda k: (src.get(self._activated[k], 0), k))
            else:
                idx = 0                       # 回退：纯 FIFO（先激活的先淘汰）
            src.pop(self._activated[idx], None)
            self._activated.pop(idx)
        return {
            "activated": activated, "already": already, "unknown": unknown,
            "disabled": disabled,
            "active_total": len(self._activated), "cap": cap,
        }

    # ── 能力目录（T1：让被延迟的工具可被模型看见）────────────────────
    #  `_sync_tool_tiers` 的 on_demand_tools 会把工具移出必注入层；移出后若地图
    #  只给"域 + 数量"，模型就**不知道这些能力存在** ⇒ 静默消失。
    #  这里按"组名 + 适配场景 + 组内名单"补齐。
    #
    #  ★ 口径对齐 OpenAI tool search：延迟的工具仍暴露**名字与一句描述**，
    #    只是不暴露参数 schema —— 所以这里极便宜（名字远小于 schema）。
    #  ★ 组描述是检索面：模型主要靠它决定"要不要加载这一组" ⇒ 每组的
    #    "适配场景"必须写清用途，而不是罗列工具名。
    #  ★ 名单在加载时刻生成，不硬编码任何文件名/命令。
    #  ⚠ 名单长度：原生 coding 组仅 8 个，全列；大域（finance 37 / capability 40）
    #    沿用"例：…"口径省 token，精确检索交给 search_tools。
    _DEFERRED_NATIVE_LABELS = {
        "task": "派子 agent 执行独立子任务",
        "bash": "执行 shell 命令（装包/跑脚本/调试/批量处理）",
        "edit": "精确字符串替换修改文件",
        "write": "整文件写入/覆盖",
        "read": "读取文件内容",
        "glob": "按文件名模式查找",
        "grep": "按内容正则检索",
        "compact": "压缩上下文",
    }
    _DEFERRED_DOMAIN_HINTS = {
        "finance": "A股行情/财务/资金面/板块等取数与分析",
        "knowledge": "本地知识库检索",
        "common": "通用技能执行入口",
        "capability": "外部能力包",
        "other": "其他未分域工具",
        "native": "agent 原生能力（文件/shell/子任务）",
    }

    def _deferred_names(self) -> List[str]:
        """延迟层（注册表有、但不在必注入层）—— 正确性来自"注册表 − core"差集。

        ★ 2026-10-17 T7：再减 `_disabled_names`——disabled 工具从目录消失
        （§8.3「目录条目同时消失」），否则地图/目录仍会把它报给模型。
        默认配置（无 T7 覆盖）下 disabled 恒为空 ⇒ 本改动零副作用。
        """
        try:
            core = set(self._core_names or [])
            disabled = set(getattr(self, "_disabled_names", None) or [])
            return [n for n in self.tool_registry.list_tools()
                    if n not in core and n not in disabled]
        except Exception:
            return []

    def _deferred_group_desc(self, group: str) -> str:
        """组描述（检索面）：coding 用贴合量化场景的自述，其余走域/原生提示表。"""
        if group == "coding":
            return ("读写改文件 / 跑 shell 命令 / 派子任务，"
                    "用于写分析脚本、调试回测、查代码")
        if group.startswith("native"):
            return "agent 原生能力（文件/shell/子任务）"
        return self._DEFERRED_DOMAIN_HINTS.get(
            group, "%s 域工具" % group if group and group != "other" else "其他未分域工具")

    def _tool_domain(self, name: str) -> str:
        """工具的来源域（common / finance / knowledge / capability …）。"""
        try:
            return getattr(self.tool_registry.get(name), "domain", "") or ""
        except Exception:
            return ""

    def get_model_query_kwargs(self, *, no_tools: bool = False) -> dict:
        """每轮只下发「必注入层 + 已激活」的工具定义（原版是全量下发）。

        `no_tools=True`：**纯文字收尾轮**用（预算熔断后，见
        `qd_agent._budget_summary_turn`）——彻底清空工具面，逼模型只据已有工具输出
        总结，不能再取数；不带 tools/tool_choice，走模型原生纯文本回合。
        """
        if no_tools:
            return {}
        return {"tools": self._active_tool_definitions(), "tool_choice": "auto"}

    def _active_tool_definitions(self) -> list:
        if self.config.tool_tier_mode != "tiered":
            return self._all_defs
        names = list(self._core_names)
        names += [n for n in (getattr(self, "_activated", None) or [])
                  if n not in self._core_names]
        # ★ T7 disabled：本 run 中途被禁用的工具（如上轮激活残留）立即从面里
        # 消失；execute_action 的 fail-open 仍放行直调（回退可救，§8.3）。
        disabled = set(getattr(self, "_disabled_names", None) or [])
        return [self._defs_by_name[n] for n in names
                if n in self._defs_by_name and n not in disabled]

    def active_tool_names(self) -> list:
        return [(d.get("function") or {}).get("name") for d in self._active_tool_definitions()]

    # ── 工具注册：mimo 原版 lowercase 目录 + FnToolAdapter 函数工具 ─────
    def _build_tool_registry(self) -> MimocodeToolRegistry:
        registry = super()._build_tool_registry()  # bash/read/write/edit/grep/glob/task/compact

        # 注：Windows 兼容性由 **mimoagent 源码补丁**统一解决
        # （patches/mimoagent_windows.patch：paths/local/ripgrep 三件），qd_agent 不再
        # 挂任何执行层 shim；此处不替换任何工具实例 —— 保持 mimoagent 原版工具
        # 逐字节生效，避免重写工具契约（上游升级即漂移）。
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

        【易错点·2026-10-01 事故】web_search / format_result 在 tools/_base.py 的
        `_MUST_HAVE` 名单里 ⇒ scan_directory **跳过它们** ⇒ provider.get_functions()
        里**没有** web_search。旧系统靠 provider.get_meta() 单独取用；迁移到 mimo 后
        只取了 get_functions() ⇒ **模型根本没有 web_search 可调**，而 system prompt
        却硬性要求"实时信息必须调用 web_search" ⇒ 天气/新闻类任务模型撞墙后只能瞎答。
        故元工具必须一并收进来，并标 domain="meta"（必注入层）。
        """
        explicit = getattr(self.config, "tool_functions", None)
        if explicit:
            return [(fn, "common") for fn in explicit]
        from .tools._base import ToolProvider
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

    # ── 每轮动态上下文（注入本轮 task，不进 system）──────────────
    def _dynamic_context_text(self, task):
        """F9：把 per-turn 动态上下文（当前时间 + 工具地图）前置到本轮 task。

        【为什么必须离开 system_template】mimo `agents/base.py:213` 只在会话
        **第一条消息**时 seed system（`if first_turn: add_message("system", ...)`），
        之后 system 永久冻结。而 `now`（秒级）与 `tool_map`（含
        `[已激活·本轮…]`，依赖每轮 `_activated`）都是 per-turn 量 ⇒ 若留在
        system 里，自 turn2 起即陈旧（实测 turn2 的 system 仍是 turn1 的
        时间戳）。而 task 每轮都新进 messages ⇒ 是唯一可靠的 per-turn 通道。

        【tool_availability 为何留下】它是启动期实测的进程内不变量
        （`probe_tools`），会话内不变 ⇒ 仍在 system 里渲染，且**只算一次**
        （首个 run、`self.messages` 尚空时）—— 避免每轮白算。system 冻结对
        prompt cache 友好（跨轮前缀逐字节稳定），故不逐轮重渲 system。

        【token 代价】工具地图每轮随 task 进一次（约 k 级 tok）；换取"模型
        每轮看到真实工具面/时间"。旧实现在 turn2+ 既不刷新、又每轮白算，两头亏。
        """
        if not getattr(self, "messages", None):
            try:
                self.extra_template_vars["tool_availability"] = self._tool_availability_text()
            except Exception:
                pass
        if not isinstance(task, str):
            return task
        try:
            head = "[当前时间] " + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tmap = self._tool_map_text()
            if tmap:
                head += "\n[工具地图]\n" + tmap
            return head + "\n\n" + task
        except Exception:
            return task

    # ── 工具地图（作 [工具地图] 块注入本轮 task，省发现轮）──────
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
                # ★ 2026-10-16 改口径：`_activated` 现为**per-run**（`qd_agent._run_inner`
                #   每轮开头清空）——原为会话级累积（2026-10-14 G4），实测每会话多漏
                #   ≈2,280 tok schema，故改按 run 重置。措辞恢复"本轮"（名单即本轮预选/
                #   激活成果）。上轮激活过、本轮又被点名的工具由 execute_action 的
                #   fail-open 就地再激活。
                # ★ 2026-10-17 审计 T3：预选 lint 覆盖补位加进来的工具带**原因**
                #   标注（"任务涉及估值/基本面"）——旧版只列名字，模型不知道为何
                #   多出一个没点过的工具，实测把能补 PE/PB 的 batch_valuation_compare
                #   白白放着没用，报告留了本可避免的"未获取"缺口。
                #   来源：qd_agent 在预选后、渲染前写好 last_preselect_report；
                #   此处现算（不落新属性）以守住 qd_agent ≤420 行拆分红线。
                from .qd_preselect import preselect_additions
                additions = preselect_additions(
                    getattr(self, "last_preselect_report", {}) or {})
                lines += ["[已激活·本轮可直接调用，**不要再** "
                          "search_tools/activate_tools]",
                          "  " + ", ".join(
                              n + (f"（预选补充：任务涉及{additions[n]}）"
                                   if n in additions else "")
                              for n in activated)]
            lines.append("[延迟·需先 activate_tools 点名激活；不确定名字用 search_tools 检索]")
            if groups:
                cap_domain = capability_domain()
                coding_names = sorted(n for n in self._deferred_names()
                                      if n in self._DEFERRED_NATIVE_LABELS)
                if coding_names:
                    lines.append(f"  - coding（{len(coding_names)} 个）"
                                 f"{self._deferred_group_desc('coding')}："
                                 f"{', '.join(coding_names)}")
                for dom, names in sorted(groups.items()):
                    if dom == "native":
                        continue                 # 原生工具已并入 coding 组
                    if dom == "other":
                        # 已并入 coding 组的原生工具不再在 other 里重复出现
                        names = [n for n in names if n not in self._DEFERRED_NATIVE_LABELS]
                        if not names:
                            continue
                    desc = self._deferred_group_desc(dom)
                    low = "，低优先级" if dom == cap_domain else ""
                    lines.append(f"  - {dom}（{len(names)} 个{low}）{desc} —— 例："
                                 f"{', '.join(sorted(names)[:6])}")
                other_native = sorted(n for n in self._deferred_names()
                                      if n not in self._DEFERRED_NATIVE_LABELS
                                      and self._tool_domain(n) in ("", "native"))
                if other_native:
                    lines.append("  - native-其他: " + ", ".join(other_native))
            else:
                lines.append("（本会话无按需层工具）")
            return "\n".join(lines)
        except Exception:
            return ""

    def _tool_availability_text(self) -> str:
        """工具可用性（启动期探测 + 服务级事实）——写进系统提示，防模型被放鸽子。"""
        try:
            from .tools._availability import probe_tools
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
