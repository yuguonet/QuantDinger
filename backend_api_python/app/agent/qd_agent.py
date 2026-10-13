# -*- coding: utf-8 -*-
"""QDAgent —— QuantDinger × mimoagent 执行核（run 生命周期主干）。

设计定调（2026-09-30 用户拍板）：
  **mimo 原版为主、增强为辅，不做强行嫁接。**
  - Agent 循环 / 工具调度 / 并行 / 上下文 / 容错 / 轨迹 → mimoagent DefaultAgent 原版
  - 规划 → mimo 原版方式（模型在循环内自规划，system template 承载纪律），
    不引入外部 Planner/phases/StateGraph——在原版基础上增强的是"金融域纪律"
    （数字溯源、交易确认、输出格式），不是替换它的规划机制
  - 保留：tools/（FnToolAdapter 零改动挂载）、skills/、三个闭环
    ① skill 酿造（chain/，与执行核解耦）② 数字溯源+审计（qd_grounding 门
    + audit/trace_adapter.py）③ 交易人工确认（qd_guards + 函数内 confirm 硬闸）

╔══ 2026-10-13 P3 拆分：本文件只留 **run 主干** ══════════════════════════╗
║ 原先 1486 行 / 41 方法是注册+预选+安全+循环+账本+收尾+轨迹七件事的补丁堆叠 ║
║ 现场。职责已按下面这张表拆到 `qd_*.py`，本文件只剩循环骨架与编排：      ║
║                                                                        ║
║   qd_hooks.py      会话钩子表（线程局部绑定）                           ║
║   qd_config.py     QDAgentConfig + 上下文窗口/压缩阈值单一口径           ║
║   qd_guards.py     交易确认闸 + 危险面守卫（action interceptor）        ║
║   qd_budgeting.py  成本记账 + 硬熔断                                    ║
║   qd_preselect.py  工具预选（点名 + lint + 域兜底）                     ║
║   qd_tool_face.py  工具注册 / 分层 / 激活 / 预热 / 地图                 ║
║   qd_aftercare.py  收尾旁路（轨迹 / 追责 / C3P / 案例记忆）              ║
║   qd_grounding.py  数字溯源门                                          ║
║                                                                        ║
║ ★ 组装方式是 **mixin**：`QDAgent(ToolFace, Preselect, Guard, Budget,    ║
║   Aftercare, Grounding, MimocodeAgent)`。MRO 里所有 mixin 必须排在       ║
║   MimocodeAgent **之前**，否则 `ToolFaceMixin._build_tool_registry` 的   ║
║   `super()` 碰不到 mimo 原版 ⇒ 工具面空掉且**不报错**。                 ║
║   守这条的是 tests/test_v6_qd_agent_split.py::test_mro_places_mixins_…  ║
║                                                                        ║
║ ★ 本文件对外 re-export 的名字（`qd_service` 从这里转给 `message_queue`） ║
║   不能动：QDAgent / QDAgentConfig / hooks 四件套 / _QD_COMPACT_NOTES。   ║
╚════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime
from typing import Any, Callable

# ⚠ 本模块**没有**沿用项目 `from log import logger` 的写法：cli 直跑与单元测试两种
# 入口下 `log` 不一定在 sys.path，一旦缺失就是 NameError（2026-10-01 实测：新增的
# 上下文窗口校验分支在 smoke test 7 里以 NameError("name 'logger' is not defined")
# 静默炸掉，而该分支本该是 fail-open 的）。标准 logging 在任何入口都成立。
logger = logging.getLogger(__name__)

from . import mimo_boot

from mimoagent.agents.mimocode.mimocode_agent import MimocodeAgent
from .audit.trace_adapter import TraceAdapter

# ── 职责模块（详见文件头拆分表）──────────────────────────────────
from .qd_config import QDAgentConfig, CORE_TOOL_DOMAINS, _QD_COMPACT_NOTES, _compaction_threshold
from .qd_hooks import _ProbeInterrupt, _hooks_now, bind_run_session, register_run_hooks, unregister_run_hooks
from .qd_tool_face import warmup_tool_face, ToolFaceMixin
from .qd_preselect import PreselectMixin
from .qd_guards import GuardMixin
from .qd_budgeting import BudgetMixin
from .qd_aftercare import AftercareMixin
from .qd_grounding import GroundingMixin


class QDAgent(ToolFaceMixin, PreselectMixin, GuardMixin, BudgetMixin,
              AftercareMixin, GroundingMixin, MimocodeAgent):
    """QuantDinger 执行核：mimoagent MimocodeAgent（MiMo Desktop 引擎）+ 金融域增强。

    ★ MRO 顺序是契约：mixin 必须整体排在 `MimocodeAgent` 之前（理由见文件头）。
    """

    def __init__(self, model, env, *, config_class: Callable = QDAgentConfig, **kwargs):
        super().__init__(model, env, config_class=config_class, **kwargs)
        self.interrupt_switch = False  # 旧 interrupt_switch 语义兼容（message_queue 探针读取）
        # ── 成本累计量（2026-10-12 P0）──
        # 按「请求」计：_run_inner 开头归零，重试轮**不**归零（见 _account_budget）。
        #
        # ⚠️ 命名红线（2026-10-12 血案）：`_run_tool_calls` **不能用** ——
        # mimoagent `DefaultAgent._run_tool_calls(tool_calls)` 是**方法**
        # （default.py:245；mimocode_agent.py:161 调用它）。这里若设成 int，
        # 实例属性会覆盖祖先方法 ⇒ 模型第一次调任何工具即
        # `TypeError: 'int' object is not callable`，整轮崩溃。
        # ⇒ 本项目自加的实例属性一律 `qd_` 前缀，与框架命名空间隔离。
        self._qd_run_tokens = 0
        self._qd_run_steps_total = 0
        self._qd_run_tool_calls = 0
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
        # 累计步数上限兜底：0 → step_limit + grace（重试轮要留几步，否则一进来就死）
        if not getattr(self.config, "run_step_cap", 0):
            self.config.run_step_cap = (
                max(1, int(getattr(self.config, "step_limit", 0) or 0))
                + max(0, int(getattr(self.config, "run_step_grace", 3) or 0)))
        self._sync_tool_tiers()
        if self.config.trading_confirm:
            self.add_action_interceptor(self._trading_confirm_guard)
        if self.config.danger_guard:
            self.add_action_interceptor(self._danger_guard)

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

    def after_step(self) -> None:
        super().after_step()
        # 成本熔断必须在埋点**之前**：超预算就直接终止，不再往下走。
        self._account_budget()
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
        # 成本累计量按**请求**归零（2026-10-12 P0）。下面两处 `_steps_taken = 0`
        # （空回复轻推轮 / 溯源重写轮）**故意不归零**这三个计数器 —— 详见
        # _account_budget 的说明：重试轮归零成本 = 无限续杯。
        self._qd_run_tokens = 0
        self._qd_run_steps_total = 0
        self._qd_run_tool_calls = 0
        self.tool_call_errors = []
        self.current_plan = None       # 计划是 per-run 的（与步数同生命周期）
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
            # 只归零**步数**：轻推轮给独立预算，不跟主轮抢。
            # 成本计数器（_qd_run_tokens/_qd_run_steps_total/_qd_run_tool_calls）**不动** ——
            # 累计步数仍受 run_step_cap 约束，成本仍受 AGENT_RUN_TOKEN_BUDGET 约束。
            self._steps_taken = 0
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
                    # 同上：只归零步数（否则主轮耗尽后重写轮一进来就死），
                    # 成本与累计步数继续累计，由 run_step_cap / token 预算兜底。
                    self._steps_taken = 0
                    status, message = super().run(
                        f"【数字溯源拦截】{guide}\n"
                        "请只依据本轮工具输出中的数字重写结论；无法溯源的数字改为"
                        "\"未获取\"并说明缺失口径。"
                    )
                    guide = self._grounding_violation(message, corpus)
                if guide is not None:
                    status = "grounding_failed"

        self._plan_lint()
        self._finish_collector(message, status)
        if self.trace is not None:
            try:
                self.trace.run_finish(self, message, status)
            except Exception:
                pass
        return status, message

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
    # 实现归档在 del/agent_formatters_20261001/；恢复前先读 `_run_inner` 里那个
    #   TODO(结果格式化) 标记的三条前置条件（超时 / 轻量模型 / 可选触发）。
