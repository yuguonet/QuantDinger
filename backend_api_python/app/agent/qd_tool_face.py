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
from typing import Any, Callable, List, Optional

from mimoagent.tools.mimocode import MimocodeToolRegistry
from .tools._fn_adapter import FnToolAdapter
from .preselect import capability_domain
from .qd_config import CORE_TOOL_DOMAINS

logger = logging.getLogger(__name__)


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

    # ── 工具地图（写进 system template，省发现轮）──────────────
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
                # ★ 2026-10-14 G4（措辞订正）：`_activated` 是**会话级**累积（只做加法，
                #   多轮里上一轮激活的工具还可能用得上），这里原先写"本轮"与事实不符
                #   ⇒ 模型会以为名单里的都是本轮预选成果。措辞改为"本会话"。
                #   ⚠ 未修部分（结构性，待拍板）：FIFO 淘汰（cap=QD_MAX_ACTIVE_TOOLS）
                #   对本轮预选项**没有 pin**，预选 ≤8 + search 自动激活 ≤8 可达 16 > 12
                #   ⇒ 同一 run 内预选成果会被后到激活静默顶掉（`_activated.pop(0)`）。
                #   功能不坏（execute_action 有 fail-open 就地再激活），但会抖、留痕乱。
                lines += ["[已激活·本会话可直接调用（含历史轮次），**不要再** "
                          "search_tools/activate_tools]",
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
