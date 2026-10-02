# -*- coding: utf-8 -*-
"""QDAgentService —— 对外服务门面（一步到位对接层）。

职责：
  1. 会话管理：session_id → QDAgent（多轮上下文天然延续 mimoagent messages）
  2. 模型构建：mimoagent models 三协议原生（OpenAI 兼容端点走 chat 协议）
  3. 自动预取（v2 功能对账承诺）：run 前把记忆近史 + RAG 命中拼进任务上下文，
     与旧注入管线体验等价；模型还可经 search_knowledge 工具主动再检索
  4. 兼容收口：chat() 返回带 .content 的对象（message_queue/cli 的既有契约）

对外入口契约不变：flask_app（Web 路由/SSE）、cli、message_queue、cron
都从 agent.py 拿 `agent` 对象调 chat()/run_agent()。
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from typing import Any, Optional

import mimo_boot

from mimoagent.environments import get_environment

try:
    from qd_agent import (  # noqa: F401  兼容 re-export（message_queue 旧名）
        QDAgent, QDAgentConfig,
        bind_run_session, register_run_hooks, unregister_run_hooks, _hooks_now,
    )
except ImportError:
    from qd_agent import (  # noqa: F401
        QDAgent, QDAgentConfig,
        bind_run_session, register_run_hooks, unregister_run_hooks, _hooks_now,
    )

logger = logging.getLogger(__name__)


def _relevance(query: str, text: str) -> int:
    """粗相关度：query 的词/中文二元组在 text 中的命中数（预取防污染闸门）。"""
    q = (query or "").lower()
    t = (text or "").lower()
    if not q or not t:
        return 0
    keys = {q[i:i + 2] for i in range(max(0, len(q) - 1))} | {w for w in q.split() if len(w) >= 2}
    return sum(1 for k in keys if k in t)


# ── 技能选择（2026-10-01 重写：词典分 + 范围信号 + 历史权重）─────────────
# 【原实现的缺陷·实测取证】纯中文二元组词典匹配，阈值 2 ⇒ 5 条典型问法里只有
# 2 条命中："帮我分析一下贵州茅台" 只得 1 分（技能描述里没有"茅台"），
# "帮我查一下平安银行现在多少钱" 得 0 分 —— 最日常的问法反而注入不了技能，
# 而"今天买什么股票好"因问法与描述字面重合度高反而能命中。命中与否取决于
# 用户措辞，不由语义决定 ⇒ 技能自动注入近似随机。
#
# 【修法】加两条**确定性**信号，都不需要 LLM：
#   ① 范围信号：由 resolvers 解析出的**标的**决定——有标的 → 个股类技能加分；
#      无标的 → 全市场筛选类技能加分（这条正是"贵州茅台"类问法缺的那 2 分）。
#   ② 意图信号：query 里的动作词（分析/查询/策略）与技能描述里的同类词对应加分。
#   ③ 历史权重只做 **tie-break**（词典分打平时让验证过的技能胜出）——
#      它来自 qd_agent_weights（盘后产出），初始恒 1.0，无信号时不改变排序。
_SCOPE_SINGLE = ("单只", "个股", "目标股票")     # 个股类技能
_SCOPE_SCREEN = ("全市场", "筛选")              # 选股/全市场类
_SCOPE_STRAT = ("策略",)                        # 策略调试类
_INTENT_RULES = (
    # (query 动作词, 技能描述对应词, 加分)
    (re.compile(r"分析|诊断|研判|怎么样|怎么看|能不能买|走势"), re.compile(r"分析|诊断|研判"), 1),
    (re.compile(r"查询|查一下|查查|多少|现价|最新价|行情|报价|多少钱"), re.compile(r"查询|行情|最新|涨跌幅"), 1),
    (re.compile(r"策略|不出信号|为什么不出|卡在哪|参数|调参"), re.compile(r"策略|调试"), 1),
)
_SKILL_INJECT_MIN = 2   # 注入阈值（总分）

# 领域闸门（2026-10-01）：范围/意图加分**只在金融语境下生效**。
# 没有它会出现实测到的误伤：'写个跑马灯页面' 因"无标的 ⇒ 全市场筛选类 +2"
# 拿到 2 分，被注入 3000 字的选股技能正文 —— 正是本系统最怕的"文不对题"回归
# （此前跑马灯被记忆/行情带偏是同一类事故）。非金融问题只保留词典分。
_FINANCE_GATE = re.compile(
    r"股票|个股|A股|港股|美股|行情|板块|大盘|指数|涨跌|选股|买入|卖出|持仓|"
    r"基金|ETF|标的|走势|技术面|基本面|资金面|筹码|财报|业绩|策略|信号")


def _skill_score(desc_l: str, keys, msg_l: str, has_entity: bool) -> int:
    """技能匹配总分 = 词典分 + 范围信号 + 意图信号（全确定性，无 LLM）。"""
    base = sum(1 for k in keys if k in desc_l)
    # 非金融语境 ⇒ 不加分（防"跑马灯"类误注入，见 _FINANCE_GATE 注释）
    if not has_entity and not _FINANCE_GATE.search(msg_l):
        return base
    bonus = 0
    if has_entity:
        if any(w in desc_l for w in _SCOPE_SINGLE):
            bonus += 2
    elif any(w in desc_l for w in _SCOPE_SCREEN):
        bonus += 2
    for q_rx, d_rx, add in _INTENT_RULES:
        if q_rx.search(msg_l) and d_rx.search(desc_l):
            bonus += add
    return base + bonus


def _skill_weight(name: str) -> float:
    """技能历史权重（tie-break 用；无记录 = 1.0 中性）。"""
    try:
        from chain.weight_hints import weight_of
    except ImportError:
        try:
            from chain.weight_hints import weight_of
        except ImportError:
            return 1.0
    try:
        return float(weight_of("skill", name, 1.0))
    except Exception:
        return 1.0


def _build_default_model():
    """从 .env 构建 mimoagent 模型（OpenAI 兼容 chat 协议）。"""
    import os
    from mimoagent.models import get_model
    return get_model(config={
        "model_name": os.getenv("OPENAI_MODEL", "qwen-plus"),
        "protocol": "chat",
        "model_kwargs": {
            "base_url": os.getenv("OPENAI_BASE_URL") or None,
            "api_key": os.getenv("OPENAI_API_KEY", ""),
            "temperature": float(os.getenv("AGENT_LLM_TEMPERATURE", "0.1")),
            "max_tokens": int(os.getenv("OPENAI_MAX_TOKENS", "16384")),
        },
    })


class QDAgentService:
    """会话化 QDAgent 服务。"""

    def __init__(
        self,
        *,
        model: Any = None,
        memory: Any = None,
        retriever: Any = None,
        skills: Any = None,
        agent_config: Optional[dict] = None,
    ):
        self._model = model            # None → 首次 chat 时按 .env 构建
        self.memory = memory
        self.retriever = retriever
        self.skills = skills
        self.agent_config = dict(agent_config or {})
        self._env = get_environment({"environment_class": "local"})
        self._sessions: dict[str, QDAgent] = {}
        self._lock = threading.Lock()

    # ── 会话 ─────────────────────────────────────────────────
    def _get_agent(self, session_id: str) -> QDAgent:
        key = str(session_id)
        with self._lock:
            agent = self._sessions.get(key)
            if agent is None:
                if self._model is None:
                    self._model = _build_default_model()
                agent = QDAgent(self._model, self._env, **self.agent_config)
                self._sessions[key] = agent
            return agent

    # ── 自动预取（功能对账：RAG/记忆注入体验等价）──────────────
    def _prefetch(self, message: str, session_id: str,
                  resolve_info: Optional[dict] = None) -> tuple[str, dict]:
        """拼装上下文并返回 (注入后任务文本, 留痕报告)。

        留痕报告（2026-10-01）记录每个候选块：注入/丢弃 + 字符数 + 原因，
        由 QDAgent.trace_prefetch 落 trace —— "为什么它会这么说"不再靠猜。

        resolve_info（2026-10-01 新增）：`resolvers/bridge.resolve()` 的产出。
        用于 ①注入已识别标的/领域 ②给技能选择提供"有没有标的"这个范围信号。
        """
        import time as _time
        _t0 = _time.perf_counter()
        parts: list[str] = []
        blocks: list[dict] = []
        # 实体解析结果（2026-10-01）：标的/领域/时间口径进上下文。
        # 澄清（clarify）不在这里处理——它在 _run_sync 里**短路反问**，不进执行。
        if resolve_info and resolve_info.get("ran"):
            from resolvers.bridge import context_block
            _rb = context_block(resolve_info)
            if _rb:
                parts.append(_rb)
                blocks.append({"kind": "resolve", "injected": True, "chars": len(_rb),
                               "reason": "已识别实体/领域"})
        # 记忆近史（相关度闸门：无关历史不注入——2026-10-01 文不对题回归，
        # 问跑马灯被旧股票记忆带偏成行情跑马灯）
        if self.memory is not None:
            try:
                hist = asyncio.run(self.memory.get_history(session_id, limit=6))
                if hist:
                    lines = [f"- {getattr(m, 'role', 'user')}: {str(getattr(m, 'content', ''))[:200]}" for m in hist]
                    block = "\n".join(lines)
                    score = _relevance(message, block)
                    if score >= 2:
                        parts.append("[会话记忆近史]\n" + block)
                    blocks.append({"kind": "memory", "injected": score >= 2,
                                   "chars": len(block), "relevance": score,
                                   "reason": "相关度达标" if score >= 2 else f"相关度 {score} < 2 闸门丢弃"})
            except Exception as e:
                logger.debug("[prefetch] 记忆召回失败（fail-open）: %s", e)
                blocks.append({"kind": "memory", "injected": False, "chars": 0,
                               "reason": f"召回异常（fail-open）: {e}"})
        # 技能自动加载（提智点 A）：按任务关键词匹配技能，命中即注入方法论正文
        if self.skills is not None:
            try:
                cands = self.skills.list_skills() or []
                msg_l = message.lower()
                keys = {k.lower() for k in message.split() if len(k) >= 2}
                keys |= {msg_l[i:i + 2] for i in range(max(0, len(msg_l) - 1))}  # 中文二元组
                # 范围信号来自实体解析（无 resolver 信息时 has_entity=False，
                # 退化为"词典分 + 意图信号"，不会比原实现更差）
                has_entity = bool((resolve_info or {}).get("entity_code"))
                scored = []
                for c in cands:
                    if not isinstance(c, dict):
                        continue
                    hay = f"{c.get('name', '')} {c.get('description', '')}".lower()
                    nm = c.get("name") or ""
                    sc = _skill_score(hay, keys, msg_l, has_entity)
                    if sc > 0:
                        scored.append((-sc, -_skill_weight(nm), nm))
                scored.sort()
                best = scored[0][2] if scored else None
                best_score = -scored[0][0] if scored else 0
                hit = False
                if best and best_score >= _SKILL_INJECT_MIN:
                    body = self.skills.load_body(best) or ""
                    if body:
                        parts.append(f"[相关技能：{best}]\n{body[:3000]}")
                        hit = True
                blocks.append({"kind": "skill", "injected": hit,
                               "chars": 3000 if hit else 0, "relevance": best_score,
                               "name": best or "", "reason":
                                   (f"命中技能 {best}（得分 {best_score}"
                                    f"，有标的={has_entity}）" if hit
                                    else f"最佳候选 {best} 得分 {best_score} "
                                         f"< {_SKILL_INJECT_MIN}，未注入")})
            except Exception as e:
                logger.debug("[prefetch] 技能匹配失败（fail-open）: %s", e)
                blocks.append({"kind": "skill", "injected": False, "chars": 0,
                               "reason": f"匹配异常（fail-open）: {e}"})
        # RAG 命中（intent 信号沿用 RAG 层过滤决策，不替 RAG 做判断）
        if self.retriever is not None:
            try:
                docs = asyncio.run(self.retriever.retrieve(message, top_k=3, intent=""))
                if docs:
                    # 逐条相关度过滤：检索器的宽松路由（如聊天历史 FTS）会捞回
                    # 无关文档，命中数 <2 的丢弃，防止任务被旧话题带偏
                    lines, kept, dropped = [], 0, 0
                    for d in docs:
                        if not isinstance(d, dict):
                            continue
                        sc = _relevance(message, str(d.get('content', '')))
                        if sc >= 2:
                            lines.append(f"- {str(d.get('content', ''))[:300]}")
                            kept += 1
                        else:
                            dropped += 1
                    if lines:
                        parts.append("[参考资料]\n" + "\n".join(lines))
                    blocks.append({"kind": "rag", "injected": bool(lines),
                                   "chars": sum(len(x) for x in lines),
                                   "relevance": kept,
                                   "reason": f"召回 {len(docs)} 条，留 {kept} 条/弃 {dropped} 条（相关度<2）"})
            except Exception as e:
                logger.debug("[prefetch] RAG 检索失败（fail-open）: %s", e)
                blocks.append({"kind": "rag", "injected": False, "chars": 0,
                               "reason": f"检索异常（fail-open）: {e}"})
        report = {
            "blocks": blocks,
            "injected_kinds": [b["kind"] for b in blocks if b.get("injected")],
            "dropped_kinds": [b["kind"] for b in blocks if not b.get("injected")],
            "chars_total": sum(len(p) for p in parts),
            "elapsed_ms": round((_time.perf_counter() - _t0) * 1000, 2),
        }
        if not parts:
            return message, report
        return "\n\n".join(parts) + f"\n\n[用户问题]\n{message}", report

    # ── 同步执行（worker 线程内跑）────────────────────────────
    def _run_sync(self, message: str, session_id: str) -> str:
        bind_run_session(session_id)  # to_thread 换线程，重新绑定钩子表
        # ── 实体解析（2026-10-01 接线）：chat 阶段等价物，取数之前 ──
        # 澄清优先（base.ResolveResult 契约）：拿不到准确信息就**反问**，绝不猜着执行。
        # 代价不对称——猜错标的/猜错时间窗 ⇒ 整份结论作废甚至据此下单；反问只花一轮。
        resolve_info: Optional[dict] = None
        try:
            from resolvers.bridge import resolve as _resolve, clarify_enabled
        except ImportError:
            try:
                from resolvers.bridge import resolve as _resolve, clarify_enabled
            except ImportError:
                _resolve, clarify_enabled = None, None      # type: ignore
        if _resolve is not None:
            try:
                resolve_info = _resolve(message)
            except Exception as e:      # fail-open（bridge 内部已兜一层，这里是双保险）
                logger.debug("[resolver] 解析不可用（已忽略）: %s: %s", type(e).__name__, e)
                resolve_info = None
            if resolve_info and resolve_info.get("clarify") and clarify_enabled():
                logger.info("[resolver] 澄清反问（不进执行）: %s",
                            str(resolve_info["clarify"])[:80])
                try:
                    from resolvers.bridge import context_block
                    _cb = context_block(resolve_info)
                except Exception:
                    _cb = ""
                return (str(resolve_info["clarify"]) + (("\n\n" + _cb) if _cb else ""))
        agent = self._get_agent(session_id)
        task, prefetch_report = self._prefetch(message, session_id, resolve_info)
        try:
            agent.trace_prefetch(prefetch_report)   # 预取留痕（排查"为什么它会这么说"）
        except Exception as e:
            logger.debug("[prefetch] 留痕失败（不影响主流程）: %s", e)
        # ★ 把**用户原始问题**挂在 agent 上，供工具预选使用（2026-10-01）。
        #   预选绝不能用 prefetch 增强后的 task：那里面塞着记忆/RAG/技能块，
        #   实测把"今天北京天气"的预选 goal 带成了"个股综合分析"（历史记忆是
        #   个股分析）⇒ 点名 7 个股票工具 ⇒ 该任务 token +136%。这是"预选不稳定"
        #   的真正成因之一——不是模型随机，是**输入被上下文污染**。
        try:
            agent.last_user_query = message
            # 供收尾格式化（formatters）选 domain / 填实体，避免再解析一次
            agent.last_resolve_info = resolve_info or {}
        except Exception:
            pass
        status, text = agent.run(task)
        if status == "_ProbeInterrupt":
            return ""  # 用户停止：安静收尾（与旧请求停止语义一致）
        # 历史落记忆（best-effort）
        if self.memory is not None:
            try:
                asyncio.run(self.memory.add(session_id, "user", message))
                if text:
                    asyncio.run(self.memory.add(session_id, "assistant", text))
            except Exception as e:
                logger.debug("[memory] 历史写入失败（fail-open）: %s", e)
        return text or ""

    # ── 对外契约：agent.chat(message, session_id=...) ─────────
    async def chat(self, message: str, session_id: str = "default", **kwargs):
        content = await asyncio.to_thread(self._run_sync, message, session_id)
        return _AgentResponse(content)


class _AgentResponse:
    """兼容旧 AgentResponse 的最小契约（message_queue/cli 只读 .content）。"""
    def __init__(self, content: str, **extra):
        self.content = content
        self.__dict__.update(extra)

    def to_dict(self) -> dict:
        return {"content": self.content}
