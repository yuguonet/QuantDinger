# -*- coding: utf-8 -*-
"""run 收尾旁路 —— 轨迹落库 / 计划清账 / 追责入库 / C3P 证据 / 案例记忆。

从 `qd_agent.py` 拆出（2026-10-13 P3 拆分）。这一整块的共同性质：
**全是旁路，全部 fail-open** —— 任何一环挂了都不能让用户收不到答复。

【四条链的产物与落点】
  ① 轨迹链  ：TraceCollector → qd_agent_traces（复盘用）
  ② 追责链  ：chain/intake → decisions / claims（T+N 回填真值）
  ③ C3P 链  ：chain/c3p → evidence（EV-1 证据向量，写侧）
  ④ 案例链  ：utils/case_memory → qd_agent_cases（下次同类任务注入参考）

【★ 顺序是契约，不能换】`_finish_collector` 里的调用次序：
  `_intake_accountability` → `_c3p_pipeline` → `_record_case`
  · C3P 必须排在 intake **之后**：它要拿 intake 产出的 decision_id / claim_ids /
    claims —— claim 的 `subject` 是算 `p_base` 的原料，且 Δp 必须按 claim 配对；
  · C3P 必须排在 case **之前**：产出的 evidence 要回传给案例记忆落进
    `outcome.evidence`（那是 P1-2 缺的写侧，没它库里一条真 EV-1 都没有）。

【★ 接线后 Δp 恒为 0 是预期行为】P3 校准未上线 ⇒ `coefs=None` ⇒ `p_final=p_base`。
本阶段在**攒数据**，不是在看效果。别为了"看到非零 Δp"去凭空编系数。

【用户维度（2026-10-15 用户裁定，长期有效）】追责链与案例链**不按用户区分**：
collector.session_id 只作弱关联（API 层带 `user:<uid>:` 前缀；exp_grind/cron 传
合成 sid；hooks 未设时 `_make_collector` 回落 "default"），decisions/cases 均无
user_id——有意设计，空/合成 session_id 与 "default" 都不是 bug，放行即可。
详见 `chain/intake.py` 模块头【用户维度】段。
"""
from __future__ import annotations

import logging
import time as _time
from typing import List, Optional

from .qd_hooks import current_session_id
from .upstream_compat import read_cache_tokens, read_token_stats
import os
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# ── 技能归属信号（P2-a / 2026-10-16）─────────────────────────────────
# `TraceCollector.begin_skill` 原设计由 planner_node 输出 current_skill 驱动，但本项目
# 无 planner_node：执行型技能经 `run_skill` 工具走**子进程**，其内部工具调用不回流父
# collector。故以「run_skill 调用点」作为 skill 层信号——每次 run_skill 开一个 skill
# 节点，其后的 on_tool_call（本次即 run_skill 自身）归入该节点，让「Chain→Skill→Tool」
# 三层树真正生效（plan_digest / 案例聚类的 step 维度不再恒为 _orphan）。
_SKILL_DISPATCH_TOOLS = ("run_skill",)


def _skill_dispatch_name(action: dict) -> str:
    """从工具调用里解析技能名：仅 `run_skill` 命中，取其 `name` 入参。永不抛。"""
    try:
        if str(action.get("tool", "")).strip() not in _SKILL_DISPATCH_TOOLS:
            return ""
        return str((action.get("params") or {}).get("name", "") or "").strip()
    except Exception:
        return ""

# 「异常收尾」状态集：只有这些状态下，计划残留在才是"跑偏/被截断"的真信号。
# 正常 Idle 收尾时模型常把 plan 当 TODO 用、不回写 completed ⇒ 残留是噪声。
# ── 收尾链异步（2026-10-16 / ① 结果先出）──────────────────────────────
# 【为什么】主轮出答案后还要跑 `_plan_lint → flush → intake → C3P → 案例`，其中
# C3P 证据提取在本网关实测需 80~155 秒（成功）/更久（重试）⇒ 用户干等。
# 移入后台后答案立刻返回；每 agent 一个**单线程** executor ⇒ 同会话收尾串行、
# 不重叠，但**不阻塞** run() 返回。`QD_TAIL_ASYNC=0` 可回退同步。
_TAIL_ASYNC_ON = (os.getenv("QD_TAIL_ASYNC") or "1").strip() not in ("0", "false", "False")
_TAIL_WAIT_S = float(os.getenv("QD_TAIL_WAIT_S", "300") or 300)


def _tail_executor(agent):
    """取（或惰性创建）该 agent 的收尾执行器。

    单线程 ⇒ 同会话收尾**串行不重叠**（上一轮收尾没完，下一轮排队），
    但**不阻塞** `run()` 返回。
    """
    ex = getattr(agent, "_tail_exec", None)
    if ex is None:
        ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qd-tail")
        agent._tail_exec = ex
    return ex


# ── 进程退出兜底（2026-10-16）──────────────────────────────────────────
# 【为什么必须有】异步收尾是**后台线程**，而一次性进程（CLI / 脚本 / 服务退出）
# 会在打印答案后立刻返回解释器 ⇒ 线程被杀 ⇒ 本轮 `flush/intake/C3P/案例`
# **全部丢失且静默**。结构性实证：异步模式下 `[Intake] 入库`/`run_finish`/案例
# 三者皆无；同步模式（QD_TAIL_ASYNC=0）三者齐全。
# 等待只发生在**退出阶段**，不影响 `run()` 的返回时刻。
# 【局限·2026-10-16 实测】`os._exit()` **绕过** atexit ⇒ 一次性 CLI
# （`cli.py:_flush_and_exit`）走不到这里。故退出点另配显式 `wait_tail_grace()`；
# 本钩子保留给"库方式使用 / 正常解释器退出"的场景（无害的二次保险）。
_TAIL_PENDING: "list" = []
# 【2026-10-16 二次修正·竞态】原实现登记 **agent 对象**，而 CLI 主线程的
# `wait_tail_grace()` 可能早于 agent 线程里的 `_spawn_tail` 执行（那时表是空的
# ⇒ 直接返回 ⇒ 丢落库；实测异步 2 次里 1 次出现）。改为登记 **Event 本身**，
# 并让 `wait_tail_grace` 对"在飞集合"轮询 —— 不再依赖登记时刻与登记对象。
_TAIL_INFLIGHT: "list" = []          # [(event, done_future)]，线程安全（GIL 下 list 操作原子）


def _register_inflight(ev, fut) -> None:
    """登记"在飞收尾"（ev=落库阶段完成事件）。完成后自动摘除。"""
    try:
        _TAIL_INFLIGHT.append((ev, fut))
    except Exception:
        pass
    try:
        fut.add_done_callback(lambda _f: _drop_inflight(ev))
    except Exception:
        pass


def _drop_inflight(ev) -> None:
    try:
        _TAIL_INFLIGHT[:] = [x for x in _TAIL_INFLIGHT if x[0] is not ev]
    except Exception:
        pass


def _register_pending_tail(agent) -> None:
    """登记一个"有待收尾"的 agent（atexit 时逐个等）。"""
    try:
        if not any(a is agent for a in _TAIL_PENDING):
            _TAIL_PENDING.append(agent)

    except Exception:
        pass


def _wait_pending_tails() -> None:
    """atexit：等所有未完成收尾（每个最多 `QD_TAIL_WAIT_S` 秒）。永不抛。"""
    while _TAIL_PENDING:
        ag = _TAIL_PENDING.pop()
        fut = getattr(ag, "_pending_tail", None)
        if fut is None:
            continue
        try:
            fut.result(timeout=_TAIL_WAIT_S)
        except Exception:
            try:
                logger.warning("[tail] 退出前等待收尾超时/失败（%.0fs 上限），"
                               "本轮落库可能不完整", _TAIL_WAIT_S)
            except Exception:
                pass


try:
    import atexit as _atexit
    _atexit.register(_wait_pending_tails)
except Exception:
    pass


def wait_tail_grace(grace_s: float = None) -> bool:
    """退出点显式等收尾（有界）。返回 True=没有未完成收尾。

    【为什么不用 atexit】`os._exit()` 绕过 atexit，而一次性 CLI 正是走它
    （`cli.py:_flush_and_exit`，为躲数据源线程池的 join 卡死而有意为之）。
    故退出点必须**显式**等一次，并给硬性上限：`QD_TAIL_EXIT_GRACE_S`（默认 30 秒）。
    """
    # 先摘掉已完成的（顺手清理）
    for ev, fut in list(_TAIL_INFLIGHT):
        if ev.is_set() or (fut is not None and fut.done()):
            _drop_inflight(ev)
    evs = [ev for ev, _f in list(_TAIL_INFLIGHT)]
    # 只等**落库阶段**（flush+intake+案例，实测 ≤1s），不等 C3P（80~155s）。
    g = float(grace_s if grace_s is not None else
              (os.getenv("QD_TAIL_EXIT_GRACE_S") or 30))
    deadline = _time.monotonic() + max(0.0, g)
    # 轮询：即使此刻 `_spawn_tail` 还没执行（集合暂空），也给它一段宽限窗口，
    # 因为收尾通常在本函数进入后毫秒级内被提交（run 的异步边界）。
    while True:
        pend = [ev for ev, _f in list(_TAIL_INFLIGHT) if not ev.is_set()]
        if not pend:
            if evs or _time.monotonic() >= deadline:
                logger.info("[tail] 退出前已等完落库阶段（C3P 仍可在后台继续）")
                return True
            if _time.monotonic() >= (deadline - max(0.0, g) + 2.0):
                # 宽限 2s 仍未见登记 ⇒ 本轮没有收尾（如未走 agent 路径）
                logger.info("[tail] 退出前无待收尾任务")
                return True
        elif _time.monotonic() >= deadline:
            logger.warning("[tail] 退出前等待**落库阶段**超时（%.0fs 上限）；"
                           "C3P 证据提取属增益层，未完成不影响主数据", g)
            return False
        _time.sleep(0.05)
_ABNORMAL_RUN_STATUS = ("truncated", "LimitsExceeded", "grounding_failed",
                        "empty", "budget_wrapped")


class AftercareMixin:
    """run 收尾：以 mixin 形式并进 QDAgent（必须排在 MimocodeAgent 之前）。"""

    # ── qd_traces DB 链（闭环②：TraceCollector → chain.store）────
    def _make_collector(self, task):
        try:
            from .trace_collector import TraceCollector
            sid = current_session_id() or "default"
            query = task if isinstance(task, str) else str(task)
            # 2026-10-01：task 是 **prefetch 加工后**的文本（记忆/技能/RAG 块 + 用户问题），
            # 直接落库会让 qd_agent_traces.user_query 变成一坨注入块，复盘时看不到
            # 用户到底问了什么（实测首条修复记录就是 "[相关技能：auto-stock-analyze]…"）。
            # 这里剥掉注入前缀，只留 [用户问题] 之后的原文。
            marker = "\n[用户问题]\n"
            if marker in query:
                query = query.split(marker, 1)[1]
            return TraceCollector(session_id=sid, user_query=query[:2000])
        except Exception:
            return None

    def _plan_lint(self, status: str = "") -> None:
        """计划收尾轻校验（差距 A，2026-10-02）：只告警不拦截。

        update_plan 的清单若留着 in_progress/pending 收不了口，**只有在 run 非正常
        收尾时**才是"任务跑偏/被截断"的信号（预算熔断 / 步数耗尽 / 溯源失败 / 空回复）。
        正常 Idle 收尾时模型常把 plan 当 TODO 用、不回写 completed —— 此时残留是纯噪声
        （实测「分析600519」正常收尾也报 5 项残留）。故：异常收尾才 WARNING，
        正常收尾降为 debug；两种情况下都照旧落 trace 事件供事后对账。
        """
        plan = getattr(self, "current_plan", None)
        if not plan:
            return
        try:
            residue = [p.get("step", "") for p in plan
                       if isinstance(p, dict) and p.get("status") in ("in_progress", "pending")]
            if residue:
                if str(status or "") in _ABNORMAL_RUN_STATUS:
                    logger.warning(
                        "[plan-lint] run 异常收尾(status=%s)且计划未清账（%d 项）: %s",
                        status, len(residue), residue[:5])
                else:
                    logger.debug(
                        "[plan-lint] 正常收尾仍有计划残留（%d 项，非异常·不告警）", len(residue))
                if self.trace is not None:
                    self.trace.emit("plan_lint", residue=residue[:10],
                                    status=str(status or ""),
                                    plan_explanation=getattr(self, "plan_explanation", ""))
        except Exception:
            pass

    # ── ① 结果先出：收尾链异步（2026-10-16）────────────────────────
    def _spawn_tail(self, message: str, status: str, trace=None) -> None:
        """把收尾链（计划校正 / 落库 / 追责 / C3P / 案例 / run_finish）丢到后台。

        【快照纪律】后台要用的一切**在 spawn 时捕获**：`collector` 引用 + message +
        status + stats + trace。线程起来后不再读 agent 的易变状态（agent 可能已被
        下一轮复用）—— `self._collector` 也在此处摘除并交给 `_run_tail` 收尾。
        【不阻塞】同 agent 单线程串行 ⇒ 上一轮收尾未完成时，新一轮的收尾排队，但
        `run()` 本身**立刻返回**。
        """
        collector = getattr(self, "_collector", None)
        self._collector = None
        # ★ 快照纪律（2026-10-16 补）：agent 实例按 session 缓存、跨 run 复用
        #   （qd_service._get_agent）⇒ 后台线程**不能**读 self.* 的易变字段，
        #   否则第 N+1 轮会把 _steps_taken 归零 / 改写，run_finish 就会把
        #   下一轮的 steps/tokens/model 记到本轮账上。此处一次性取值。
        stats = read_token_stats(self.model)
        _snap = {
            "steps": int(getattr(self, "_steps_taken", 0) or 0),
            "err": [e for e in (getattr(self, "tool_call_errors", None) or []) if e],
            "tokens": int(getattr(stats, "total_tokens", 0) or 0),
            # F10：网关回填的前缀缓存 input token（含在 tokens 内）。
            # 缺省 0=网关未上报，写库侧据此落 NULL，不与"真实 0"混淆。
            # F15b：改经 upstream_compat —— 上游改名时告警而非静默恒 NULL。
            "cached": read_cache_tokens(self.model) or 0,
            "model": str(getattr(getattr(self.model, "config", None),
                                "model_name", "") or ""),
        }

        import threading as _threading
        _db_ev = _threading.Event()          # 落库阶段完成事件（creator 先于 _collect 定义）

        def _collect():
            """后台：落库 + 追责（原 `_finish_collector` 的前半，顺序是契约）。"""
            if collector is None:
                return None
            try:
                from .preselect import record_task_calls
                record_task_calls(str(getattr(collector, "user_query", "") or ""),
                                  list(getattr(collector, "_all_tools_called", None) or []))
            except Exception:
                pass
            # O-7（F23）：每 run 收口落一行观测快照（跨重启聚合看长期趋势）
            try:
                from .preselect import snapshot
                snapshot()
            except Exception:
                pass
            root_id = None
            try:
                collector.on_agent_finish(
                    final_answer=message or "",
                    total_steps=_snap["steps"],
                    total_tokens=_snap["tokens"],
                    model=_snap["model"],
                    cached_tokens=_snap["cached"],
                )
                root_id = collector.flush()
            except Exception:
                pass
            self._intake_accountability(collector, message or "", root_id, stats)
            # ★ 落库阶段到此为止（traces=flush / decisions+claims=intake）。
            #   先放行退出点，再跑增益层（C3P 实测 80~155s、案例依赖其 evidence）。
            if trace is not None:
                try:
                    class _SnapAgent:               # run_finish 只读这两个字段
                        pass
                    _sa = _SnapAgent()
                    _sa._steps_taken = _snap["steps"]
                    _sa.tool_call_errors = _snap["err"]
                    trace.run_finish(_sa, message or "", status)
                except Exception:
                    pass
            try:
                _db_ev.set()
            except Exception:
                pass
            self._c3p_pipeline(collector, message or "")
            self._record_case(collector, message or "", status, root_id, stats)
            return root_id

        if not _TAIL_ASYNC_ON:
            self._plan_lint(status)
            _collect()
            if trace is not None:
                try:
                    trace.run_finish(self, message, status)
                except Exception:
                    pass
            return

        def _job():
            try:
                self._plan_lint(status)
                _collect()                     # flush → intake → run_finish → C3P → 案例
                logger.info("[tail] 收尾完成（后台）：status=%s", status)
            except Exception as e:              # 收尾是旁路：绝不冒泡
                logger.warning("[tail] 收尾异常(fail-open): %s: %s", type(e).__name__, e)

        self._tail_db_done = _db_ev
        self._pending_tail = _tail_executor(self).submit(_job)
        _register_inflight(self._tail_db_done, self._pending_tail)
        _register_pending_tail(self)          # atexit 兜底：进程退出前等它（见模块级注释）
        logger.info("[tail] 收尾已转入后台（status=%s，QD_TAIL_ASYNC=0 可回退同步）", status)

    def wait_tail(self, timeout: Optional[float] = None) -> bool:
        """等本轮后台收尾完成（测试/脚本用）。返回 True=已完成或本就无任务。"""
        fut = getattr(self, "_pending_tail", None)
        if fut is None:
            return True
        try:
            fut.result(timeout=float(timeout if timeout is not None else _TAIL_WAIT_S))
            return True
        except Exception:
            return False

    def _finish_collector(self, message: str, status: str) -> None:
        """四条旁路的总入口；调用次序是契约（见本模块文件头）。"""
        collector = getattr(self, "_collector", None)
        self._collector = None
        if collector is None:
            return
        # ── 差距 K：攒「任务→实际调用工具」对（JSONL，周级喂共现权重）────
        # 只写文件不改行为；空问题/空工具链不记；任何异常静默（旁路 fail-open）。
        try:
            from .preselect import record_task_calls
            record_task_calls(str(getattr(collector, "user_query", "") or ""),
                              list(getattr(collector, "_all_tools_called", None) or []))
        except Exception:
            pass
        # O-7（F23）：每 run 收口落一行观测快照（与上面配对同一收口点）
        try:
            from .preselect import snapshot
            snapshot()
        except Exception:
            pass
        root_id = None
        stats = None
        try:
            stats = read_token_stats(self.model)
            collector.on_agent_finish(
                final_answer=message or "",
                total_steps=self._steps_taken,
                total_tokens=int(getattr(stats, "total_tokens", 0) or 0),
                model=str(getattr(getattr(self.model, "config", None), "model_name", "") or ""),
                cached_tokens=read_cache_tokens(self.model) or 0,
            )
            root_id = collector.flush()
        except Exception:
            pass
        self._intake_accountability(collector, message or "", root_id, stats)
        self._c3p_pipeline(collector, message or "")
        self._record_case(collector, message or "", status, root_id, stats)

    def _c3p_pipeline(self, collector, answer: str) -> None:
        """C3P 接线（v5 P2，2026-10-13）：一次推理 → 证据向量（EV-1）+ 配对双跑。

        【为什么必须 fail-open】这是增益层，且会打一次 LLM —— 挂了绝不能让用户
        收不到答复。整段包 try/except，`QD_C3P_ENABLED=0` 可整体关闭。
        """
        self.last_c3p: dict = {}
        acc = getattr(self, "last_accountability", None) or {}
        try:
            from .chain import c3p
            self.last_c3p = c3p.run_for_decision(
                user_query=str(getattr(collector, "user_query", "") or ""),
                answer=answer,
                tracked=bool(acc.get("tracked")),
                decision_id=acc.get("decision_id"),
                claim_ids=list(acc.get("claim_ids") or []),
                claims=list(acc.get("claims") or []),
            )
        except Exception as e:
            logger.debug("[c3p] 流水线跳过(fail-open): %s: %s", type(e).__name__, e)

    def _record_case(self, collector, message: str, status: str,
                     root_id: Optional[int], stats) -> None:
        """案例记忆写入侧（2026-10-10 接线）：run 收尾落一条案例（label=pending）。

        【为什么挂 _finish_collector 后置】这是唯一同时拿得到四样原料的位置：
        用户原话（collector.user_query，已剥掉 prefetch 注入块）、工具链
        （collector.plan_digest()）、最终答复/置信度、以及成本（steps/tokens/耗时）。
        【为什么 label 恒为 pending】防错误自我复制：correct/incorrect 由 T+N 真值
        经 chain/store 的 backfill_by_root 延迟回填，入库时谁也不知道对错。
        【fail-open】案例记忆是增益层，任何异常只 debug 日志，不影响回复主链。
        """
        try:
            from .utils import case_memory
            if not case_memory.case_memory_enabled() or collector is None:
                return
            query = str(getattr(collector, "user_query", "") or "").strip()
            if not query:
                return
            root = getattr(collector, "_root", None)
            # 失败坑（只记客观事实，不写“怎么修”——注入侧也只给坑不给修复路径）
            failures: List[str] = []
            if status == "grounding_failed":
                failures.append("数字溯源未通过（结论含不可溯源数字）")
            if status in ("truncated", "LimitsExceeded", "empty", "budget_wrapped"):
                failures.append("run 未正常收尾（status=%s）" % status)
            try:
                residue = [p.get("step", "") for p in (getattr(self, "current_plan", None) or [])
                           if isinstance(p, dict)
                           and p.get("status") in ("in_progress", "pending")]
                if residue:
                    failures.append("计划未清账：" + "、".join(str(s) for s in residue[:3]))
            except Exception:
                pass
            errs = [str(e)[:80] for e in (getattr(self, "tool_call_errors", None) or [])
                    if e][:3]
            if errs:
                failures.append("工具调用错误：" + "；".join(errs))
            cost = {
                "steps": int(getattr(self, "_steps_taken", 0) or 0),
                "tokens": int(getattr(stats, "total_tokens", 0) or 0),
                # F10：命中前缀缓存 input token（含在 tokens 内）——经验库据此
                # 对账「同题复跑的上下文复用率」，验证缓存是否真省了钱。
                "cached": read_cache_tokens(self.model) or 0,
                "latency_ms": int(getattr(root, "elapsed_ms", 0) or 0),
                "model": str(getattr(getattr(self.model, "config", None),
                                     "model_name", "") or ""),
                "status": str(status or ""),
            }
            # ★ evidence 来自紧邻的 `_c3p_pipeline`（P1-2 缺的写侧在此补上）。
            #   没有它，EV-1 契约只有读侧，库里一条真证据都不会有。
            _ev = (getattr(self, "last_c3p", None) or {}).get("evidence")
            case_memory.record_case(
                task_summary=query,
                root_id=root_id,
                level="root",
                tags=case_memory.classify_case_tags(query, message),
                plan_digest=collector.plan_digest() if hasattr(collector, "plan_digest") else [],
                failure_modes=failures,
                cost=cost,
                confidence=getattr(root, "confidence", None),
                evidence=_ev if isinstance(_ev, dict) else None,
            )
        except Exception as e:
            logger.debug("[case-memory] 案例落库跳过(fail-open): %s: %s",
                         type(e).__name__, e)

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
            from .chain.intake import record_decision
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

    # ── 轨迹采集（工具级，闭环②的写侧）──────────────────────────
    def _trace_tool(self, action: dict, result, elapsed_ms: float, error: str = None) -> None:
        """工具调用落 qd_traces（闭环② DB 链，TraceCollector）。"""
        collector = getattr(self, "_collector", None)
        if collector is None:
            return
        # 技能归属（P2-a，2026-10-16）：run_skill 分发时开一个 skill 节点，其 on_tool_call
        # 归入该节点（详见模块头 `_skill_dispatch_name`）。无论成败都收口（finally）。
        skill = _skill_dispatch_name(action)
        if skill:
            try:
                collector.begin_skill(skill)
            except Exception:
                skill = ""
        try:
            out = result.get("output") if isinstance(result, dict) else result
            err = error or (None if not isinstance(result, dict) or result.get("success", True)
                            else str(out)[:200])
            collector.on_tool_call(action.get("tool", ""), action.get("params") or {},
                                   out, elapsed_ms, error=err)
        except Exception:
            pass
        finally:
            if skill:
                try:
                    collector.end_skill()
                except Exception:
                    pass
