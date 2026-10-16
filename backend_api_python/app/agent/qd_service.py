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
import os
import re
import threading
from collections import OrderedDict
from typing import Any, Optional

from . import mimo_boot

from mimoagent.environments import get_environment

try:
    from .qd_agent import QDAgent, QDAgentConfig, bind_run_session, register_run_hooks, unregister_run_hooks, _hooks_now
except ImportError:
    from .qd_agent import QDAgent, QDAgentConfig, bind_run_session, register_run_hooks, unregister_run_hooks, _hooks_now
from .qd_config import is_transient_control
from .prompts import load_prompt as _load_prompt

logger = logging.getLogger(__name__)


def _relevance(query: str, text: str) -> int:
    """粗相关度：query 的词/中文二元组在 text 中的命中数（预取防污染闸门）。"""
    q = (query or "").lower()
    t = (text or "").lower()
    if not q or not t:
        return 0
    keys = {q[i:i + 2] for i in range(max(0, len(q) - 1))} | {w for w in q.split() if len(w) >= 2}
    return sum(1 for k in keys if k in t)


def _should_store_assistant(text: str) -> bool:
    """本轮回复是否入会话记忆（best-effort 写侧闸门）。

    2026-10-14：预算熔断/步数耗尽/空回复/溯源拦截等**瞬态控制文案**会被
    `agent.run()` 当作 message 原样返回；若照旧落 `assistant` 记忆，下一轮预取
    又把它注回上下文 ⇒ 跨轮污染（实测 002866 全字段「未获取」的成因之一）。
    故只有**非空且非瞬态控制**的文本才写库。
    """
    return bool((text or "").strip()) and not is_transient_control(text)


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
#   ③ 历史权重是**排序分乘子**（G，2026-10-10：score × (0.5+0.5×weight)，见
#      _rank_skills）——它来自 qd_agent_weights（盘后产出），初始恒 1.0（乘子 1.0，
#      冷启动与纯词典序一致）；注入阈值仍看原始词典分（权重只调序不过滤）。
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

# 超大技能渐进式加载（2026-10-02）：正文 > 此预算时，注入改为
# 「目录 + 首段正文 + read_skill 按需读指针」——方法论尾部章节（执行流程/注意事项）
# 不再被 body[:3000] 硬切丢失；同会话已注入过的技能只发一行指针，不再重复注入。
_SKILL_BODY_CAP = 2200

# ── 会话级技能去重表的容量上限（D-12，2026-10-14）────────────────
# 【为什么必须有上限】`_skill_seen` 是 {session_id: {已注入技能名}}，原实现只增
# 不减 ⇒ 服务不重启就一直涨（长跑内存增长点，设计文档 D-12）。
# 【为什么是 LRU 不是 FIFO】这张表的唯一作用是「同会话不重复灌正文」（降 token）。
# 若按 FIFO 淘汰，一个长期活跃的老会话会被先踢 ⇒ 它下一轮重新灌一次正文，
# 恰恰是这张表要避免的事。所以按**访问**刷新热度。
# 【为什么淘汰是安全的】它只影响「正文 vs 一行指针」：淘汰 ⇒ 下次重注一次正文，
# 正确性不变，只多花一次 token ⇒ fail-open 方向天然安全。
# 【为什么走 env】工程红线：阈值不硬编码。脏值回落默认且夹紧到 >=1 ——
# 容量 0 会让「刚插入就被自己挤掉」，去重表蜕化成永空集，静默放大 token 看不出报错。
_SKILL_SEEN_MAX_DEFAULT = 512


def _skill_seen_max() -> int:
    """`_skill_seen` 的会话容量上限（QD_SKILL_SEEN_MAX，默认 512）。

    约定：解析失败 / 非正值一律夹紧到 >=1（或回落默认），**不抛**——
    这张表挂了最坏也只是多注一次正文，不该让整轮对话失败。
    """
    raw = os.getenv("QD_SKILL_SEEN_MAX")
    if raw is None or not str(raw).strip():
        return _SKILL_SEEN_MAX_DEFAULT
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.debug("[skill_seen] QD_SKILL_SEEN_MAX=%r 非整数，回落默认 %s",
                     raw, _SKILL_SEEN_MAX_DEFAULT)
        return _SKILL_SEEN_MAX_DEFAULT
    if v < 1:
        # 0/负数 ⇒ 去重表会变永空集（每轮重复灌正文），按 1 处理至少保住当前会话
        return 1
    return v


_EXECUTABLE_SKILLS_CACHE: Optional[dict] = None


def _executable_skills() -> dict:
    """执行型技能白名单（单一真源 = app.agent.scripts.skill_run.EXECUTABLE_SKILLS）。

    【加载方式·2026-10-14 D-11 收敛】此前用 `spec_from_file_location` 按文件路径
    加载，理由是「`import scripts.*` 在 sys.path 错序时会解析到
    backend_api_python/scripts」（本仓吃过 `utils` 命中 app/utils 的亏，见 test_30）。
    两个前提现在都不成立了：
      · P5 起 `app/agent` 不进 sys.path、全包统一 `app.agent.*`；
      · 用**包内相对导入**（`from .scripts.skill_run import ...`）根本不经过
        sys.path 查找，不存在被顶层 scripts/ 抢解析的可能。
    而按路径加载会造出**第二个模块实例**（不进 sys.modules 的影子模块）——
    与 P5 根治的"双模块实例"是同一类隐患：将来谁正常 import 一次，两份
    EXECUTABLE_SKILLS 就分叉。故改为包内相对导入（守卫见
    tests/test_v6_d11_whitelist_knobs.py）。
    fail-open：取不到返回 {}（退化为注正文，行为等同改前，不会更坏）。
    """
    global _EXECUTABLE_SKILLS_CACHE
    if _EXECUTABLE_SKILLS_CACHE is not None:
        return _EXECUTABLE_SKILLS_CACHE
    try:
        from .scripts.skill_run import EXECUTABLE_SKILLS as _WL
        _EXECUTABLE_SKILLS_CACHE = dict(_WL or {})
    except Exception as e:
        logger.debug("[skill_block] 执行型白名单加载失败（fail-open 退化为注正文）: %s", e)
        _EXECUTABLE_SKILLS_CACHE = {}
    return _EXECUTABLE_SKILLS_CACHE


def _canonical_skill_name(skills, name: str) -> str:
    """显示名 → 目录名（2026-10-14）。

    `list_skills()` 返回的是 frontmatter **显示名**，与 `EXECUTABLE_SKILLS` 的键
    （**目录名**）同口径（2026-10-14 kebab 化后两者均为连字符，如 market-screener）。
    历史遗留里显示名（连字符）与目录名（下划线）不同口径，不归一化会让
    `_executable_skills().get(显示名)` 恒为 None ⇒ 执行型技能永远进不了
    分发卡片分支、退化成灌方法论正文（实测注 2529 字正文而非 407 字卡片）。
    复用 adapter 既有的 display→name 映射（`skills.get()`，与 `skill_tools.run_skill`
    同口径，见其 2026-10-12 注释），不另写字符串替换。取不到时原样返回（fail-open）。
    """
    try:
        info = skills.get(name)
        if info is not None and getattr(info, "name", ""):
            return info.name
    except Exception:
        pass
    return name


# 分发卡片模板（2026-10-16 外置）：卡片正文规则在 prompts/dispatch_card.md，
# 本文件只保留参数计算与 format 填充——改卡片措辞无需动代码。模板是模块
# import 时加载的常量（prompts/ 加载器约定），改 .md 后需重启进程生效。
# 【逐字保文】模板输出必须与 2026-10-10 起的 f-string 版本逐字节一致：
# qd_smoke.test_38 对卡片有内容断言（run_skill 签名/入口/结果读法/空结果
# 纪律/非空纪律），改措辞前先 dump 基线做字节级比对（tmp/dispatch_card_verify.py）。
_DISPATCH_CARD_TPL = _load_prompt("dispatch_card")


def _dispatch_card(name: str, spec) -> str:
    """执行型技能 → 分发卡片（2026-10-10）：不注方法论正文，只注
    「何时派 / 怎么派 / 入口 / 结果怎么读」。

    【为什么执行型不注正文】正文是分步方法论，注进主上下文有两个害处：
    ① 正确姿势是 run_skill 一行派活、只回预览摘要，正文全量注入违背 B 的
    「上下文 O(摘要)」；② 模型照着分步方法论在主对话硬跑的概率上升——与
    system_template §重活分发**抢方向盘**。正文并未删除：read_skill 按需读
    兜底仍在，模型真要方法论仍拿得到。

    【外置·2026-10-16】卡片正文外置到 prompts/dispatch_card.md（占位符
    {name}/{fn_note}/{default_fn}；字面花括号写 {{ }}），本函数只算三个实参。
    `default_fn or "run"` 的落空逻辑保留在此处算完再传 str 进模板——模板里
    不放表达式，避免措辞之外的行为也散进 .md。
    """
    try:
        _mod_path, entries, default_fn = list(spec)[:3]
    except Exception:
        entries, default_fn = (), "run"
    entries = list(entries or ())
    fn_note = "、".join(entries) if entries else str(default_fn or "run")
    return _DISPATCH_CARD_TPL.format(
        name=name, fn_note=fn_note, default_fn=str(default_fn or "run"))


def _skill_block(name: str, body: str, skills) -> str:
    """技能注入块：小技能全文；超大技能 = 前段 + 目录 + 按需读路标。"""
    if len(body) <= _SKILL_BODY_CAP:
        return body
    try:
        headings = skills.get_section_headings(name) or []
    except Exception as _he:
        # 不能静默：拿不到目录时下文的 outline 会退化成"（无）"，与"该技能
        # 本来就没有小节"在输出上完全无法区分，事后排查必然被误导。
        headings = []
        logger.debug("[skill_block] %s 取小节目录失败（fail-open）: %s: %s",
                     name, type(_he).__name__, _he)
    outline = "；".join(str(h).strip("# ").strip() for h in headings[:20]) or "（无）"
    # resource 路标只在技能**真的有**引用文件时才给：实测 5 个技能里 4 个
    # list_resources() 为空，无条件给会诱导模型做一次必然失败的工具调用
    # （返回 {"error": "无资源", "resources": []}），白花一轮。
    _res_hint = ""
    try:
        if skills.list_resources(name):
            _res_hint = (f'；参考资料用 read_skill(name="{name}", '
                         f'resource="references/<文件>")')
    except Exception:
        _res_hint = ""
    return (
        f"{body[:_SKILL_BODY_CAP]}\n……（正文共 {len(body)} 字符，已截断）\n"
        f"[技能目录] {outline}\n"
        f"[按需读取] 要完整正文用 read_skill(name=\"{name}\")；"
        f"只看某小节用 read_skill(name=\"{name}\", heading=\"<小节关键词>\"){_res_hint}。"
    )

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
    """技能历史权重（无记录 = 1.0 中性）。"""
    try:
        from .chain.weight_hints import weight_of
    except ImportError:
        return 1.0
    try:
        return float(weight_of("skill", name, 1.0))
    except Exception:
        return 1.0


def _rank_skills(cands: list, keys, msg_l: str, has_entity: bool) -> list:
    """候选技能排序（G，2026-10-10：权重从 tie-break 升级为排序分乘子）。

    返回 [(乘子化分, 原始词典分, name), ...] 按分降序。
    · 乘子 = 0.5 + 0.5×weight（weight∈[0.5,2.0]，evaluator 写侧口径）⇒ 乘子∈[0.75,1.5]；
      冷启动无记录 weight=1.0 ⇒ ×1.0，排序与纯词典分 bit 级一致（验收「不误伤」）。
    · 样本纪律在写侧（weight_feed n<MIN_SAMPLES 只记账、evaluator n<10 恒 1.0），
      读侧不重复校验；负向词面证据不足时 weight 仍为 1.0 ⇒ 自动回中性。
    · 注入阈值（_SKILL_INJECT_MIN）消费**原始词典分**——权重只调序不过滤，
      与 weight_hints.hint_text / case 同型标签一致的 fail-open 哲学。
    """
    out = []
    for c in cands:
        if not isinstance(c, dict):
            continue
        hay = f"{c.get('name', '')} {c.get('description', '')}".lower()
        nm = c.get("name") or ""
        sc = _skill_score(hay, keys, msg_l, has_entity)
        if sc > 0:
            w = _skill_weight(nm)
            out.append((sc * (0.5 + 0.5 * w), sc, nm))
    out.sort(key=lambda t: (-t[0], -t[1], t[2]))
    return out


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
        # 会话级技能注入去重（2026-10-02 降 token）：同会话已注入过的技能
        # 只发一行指针，不再每轮重复灌正文（5 轮 × 3000 字的重复实测存在）
        #
        # D-12（2026-10-14）：改为**带容量上限的 LRU**。OrderedDict 的键顺序即热度
        # （尾=最近访问）；写入与淘汰一律经 `_skill_seen_for()`，不要在这里直接
        # setdefault —— 那会绕过上限，把 D-12 改回没做（有 AST 守卫盯着，见
        # tests/test_v6_skill_seen_lru.py::test_prefetch_does_not_use_bare_setdefault）。
        self._skill_seen: "OrderedDict[str, set]" = OrderedDict()
        self._lock = threading.Lock()
        # A-01（2026-10-14，更正）：per-session 串行化锁，用 **threading.Lock** 且
        # 串行化点下沉到 `_run_sync`（to_thread 的 T2 工作线程）内。
        # 早期版本把 asyncio.Lock 放在 `chat`（协程层），但超时取消（`wait_for` 抛
        # CancelledError）会让 `async with` 释放锁，孤儿 run 的 T2 线程逃逸出串行化，
        # 与后续同会话 run 并发交错改同一 QDAgent 实例 —— 锁形同虚设。
        # threading.Lock 在 T2 内 acquire/release，不受 asyncio 取消影响，覆盖所有路径
        # （正常 / 超时孤儿 / 后续重入）；不同会话拿不同锁 ⇒ 并行不误伤吞吐。
        # 字典本身由 `self._lock` 保护（瞬态加锁，不跨 await 持有）。
        self._session_locks: dict[str, threading.Lock] = {}

    # ── 会话级串行化锁（A-01）──
    def _session_lock(self, session_id: str) -> threading.Lock:
        """取（并惰性创建）某会话的串行化锁 —— 本类**唯一**入口。

        返回 **threading.Lock**（不是 asyncio.Lock）：串行化点在 `_run_sync`（T2 工作
        线程）内 acquire/release，不受 asyncio 取消影响，覆盖超时孤儿 run。共享的
        `QDAgent` 实例不会被同会话的两个线程并发 `run()` 交错改写；`bind_run_session`
        的会话钩子表也不会被同一会话的并发 run 互相抹。不同会话拿不同锁 ⇒ 并行不误伤。
        """
        key = str(session_id)
        with self._lock:                      # threading.Lock 只保护字典增删（瞬态）
            lk = self._session_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._session_locks[key] = lk
            return lk

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

    def _skill_seen_for(self, session_id: Any) -> set:
        """取（并刷新）某会话的技能去重集合——`_skill_seen` 的**唯一**入口。

        · 键归一化为 `str(session_id)`：与 `_get_agent` 同口径（设计 §易错点），
          否则将来加清理逻辑时会漏删。
        · 访问即 `move_to_end`：实现 LRU（不是 FIFO）。
        · 超容量淘汰最久未访问的会话。被淘汰者下次拿到**空集** ⇒ 重注一次正文，
          正确性不变（fail-open）。
        """
        key = str(session_id)
        with self._lock:
            seen = self._skill_seen.get(key)
            if seen is None:
                seen = set()
                self._skill_seen[key] = seen
            self._skill_seen.move_to_end(key)
            cap = _skill_seen_max()
            while len(self._skill_seen) > cap:
                # last=False ⇒ 淘汰队首（最久未访问）
                self._skill_seen.popitem(last=False)
            return seen

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
        # 执行型技能注入时卡片点名的工具（2026-10-14 解耦修复）：卡片让模型用
        # `run_skill` 派活，但 `run_skill` 是**按需工具**（QD_ON_DEMAND_TOOLS 默认
        # `actor,run_skill`），而预选看的是剥掉注入后的**用户原始问题**、看不到技能
        # ⇒ 预选不会点它 ⇒ 模型要白花一轮 `search_tools` 才发现（实测 trace step1）。
        # 这里收集卡片点名的工具，经 report 传给 agent 在预选后就地激活。
        force_tools: list[str] = []
        # 实体解析结果（2026-10-01）：标的/领域/时间口径进上下文。
        # 澄清（clarify）不在这里处理——它在 _run_sync 里**短路反问**，不进执行。
        if resolve_info and resolve_info.get("ran"):
            from .resolvers.bridge import context_block
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
                ranked = _rank_skills(cands, keys, msg_l, has_entity)
                best = ranked[0][2] if ranked else None
                best_score = ranked[0][1] if ranked else 0   # 阈值看原始分（权重只调序）
                hit = False
                if best and best_score >= _SKILL_INJECT_MIN:
                    # 键口径 + LRU 容量上限都在 _skill_seen_for 里（D-12），
                    # 这里不要再直接动 self._skill_seen。
                    seen = self._skill_seen_for(session_id)
                    # 显示名（连字符）→ 目录名（下划线）后再查执行型白名单：
                    # 白名单键是目录名，显示名直查恒 None（见 _canonical_skill_name）。
                    _best_canon = _canonical_skill_name(self.skills, best)
                    if best in seen:
                        # 已在本会话注入过：一行指针即可。
                        # ⚠ 不写"见上方历史"：上下文压缩（阈值 _compaction_threshold）
                        # 会改写历史，而 _skill_seen 对此不知情 ⇒ 那时"回看"是假的。
                        # 给出 read_skill(name=...) 不带参数 = 全文（skill_tools 实测），
                        # 模型可一句话自取，不依赖历史是否还在。
                        _exec_spec = _executable_skills().get(_best_canon)
                        if _exec_spec is not None:
                            # 指针同样点名了 run_skill ⇒ 一并激活（否则又得 search_tools）
                            force_tools.append("run_skill")
                        _ptr_tail = ("（执行型技能：重活直接派 run_skill，别读正文硬跑）"
                                     if _exec_spec is not None else "")
                        parts.append(
                            f"[技能指针：{best}] 正文本会话已注入过；"
                            f"要完整正文用 read_skill(name=\"{best}\")，"
                            f"只看某小节用 read_skill(name=\"{best}\", heading=\"…\")。{_ptr_tail}")
                        hit = True
                    else:
                        # 2026-10-10 分叉：执行型技能不注方法论正文，只注分发卡片
                        # （正文会诱导主对话硬跑，与 §重活分发抢方向盘；见 _dispatch_card）
                        _spec_exec = _executable_skills().get(_best_canon)
                        if _spec_exec is not None:
                            blk = _dispatch_card(best, _spec_exec)
                            parts.append(f"[技能分发卡片：{best}]\n{blk}")
                            # 卡片第一行就让模型 run_skill 派活 ⇒ 必须让它在首轮工具面里，
                            # 否则模型要先 search_tools 发现（本次实测的额外一轮）。
                            force_tools.append("run_skill")
                            seen.add(best)
                            hit = True
                        else:
                            body = self.skills.load_body(best) or ""
                            if body:
                                blk = _skill_block(best, body, self.skills)
                                parts.append(f"[相关技能：{best}]\n{blk}")
                                seen.add(best)
                                hit = True
                blocks.append({"kind": "skill", "injected": hit,
                               "chars": len(parts[-1]) if hit else 0, "relevance": best_score,
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
        # few-shot 金例注入（2026-10-10 接线：case_memory 机制此前 0 调用方，
        # 「建好了电没通」）。正确性语义由 case_memory 保证：incorrect 硬排除 /
        # pending 降权 / 失败案例只给坑不给修复路径（见其模块头注释）。
        # 选例按任务类型标签加权（taxonomy 六类，只调序不过滤）。
        try:
            from .utils import case_memory as _cm
            if _cm.case_memory_enabled():
                _tags = _cm.classify_case_tags(message)
                _cases = _cm.retrieve_cases(message, top_k=_cm.CASE_INJECT_TOP_K,
                                            tags=_tags or None)
                _blk = _cm.format_case_injection(_cases)
                if _blk:
                    parts.append(_blk)
                blocks.append({"kind": "case", "injected": bool(_blk),
                               "chars": len(_blk), "relevance": len(_cases),
                               "reason": (f"命中 {len(_cases)} 条相似案例"
                                          f"（标签：{'/'.join(_tags) or '未分类'}）"
                                          if _blk else
                                          f"无达标案例（相似度<阈值，宁缺毋滥；"
                                          f"标签：{'/'.join(_tags) or '未分类'}）")})
            else:
                blocks.append({"kind": "case", "injected": False, "chars": 0,
                               "reason": "CASE_MEMORY_ENABLED=0 关闭"})
        except Exception as e:
            logger.debug("[prefetch] 案例注入失败（fail-open）: %s", e)
            blocks.append({"kind": "case", "injected": False, "chars": 0,
                           "reason": f"注入异常（fail-open）: {e}"})
        report = {
            "blocks": blocks,
            "injected_kinds": [b["kind"] for b in blocks if b.get("injected")],
            "dropped_kinds": [b["kind"] for b in blocks if not b.get("injected")],
            "chars_total": sum(len(p) for p in parts),
            "elapsed_ms": round((_time.perf_counter() - _t0) * 1000, 2),
            # 执行型技能卡片点名的工具（去重、保序）⇒ agent 在预选后激活（见 _run_inner）
            "skill_force_tools": list(dict.fromkeys(force_tools)),
        }
        if not parts:
            return message, report
        return "\n\n".join(parts) + f"\n\n[用户问题]\n{message}", report

    # ── 同步执行（worker 线程内跑）────────────────────────────
    def _run_sync(self, message: str, session_id: str) -> str:
        bind_run_session(session_id)  # to_thread 换线程，重新绑定钩子表
        # A-01（2026-10-14，更正）：同会话串行化在 **本线程（T2）** 内用 threading.Lock
        # 实现 —— 覆盖超时孤儿 run（asyncio.Lock 在 chat 层会在取消时提前释放）。
        # A-02 方向2（2026-10-14）：run 真正结束（含超时后的孤儿 run 自然结束）时
        # 释放锁 + 清理会话钩子表。超时取消时 message_queue 的 `_run_with_events`
        # 已跳过 unregister（保留钩子让 step 探针读到本 run 停止位协作自停），此处是
        # 最终清理点，避免钩子泄漏被后续线程误读（误停止）。try/finally 覆盖所有路径。
        lk = self._session_lock(session_id)
        lk.acquire()
        try:
            # ── 实体解析（2026-10-01 接线）：chat 阶段等价物，取数之前 ──
            # 澄清优先（base.ResolveResult 契约）：拿不到准确信息就**反问**，绝不猜着执行。
            # 代价不对称——猜错标的/猜错时间窗 ⇒ 整份结论作废甚至据此下单；反问只花一轮。
            resolve_info: Optional[dict] = None
            try:
                from .resolvers.bridge import resolve as _resolve, clarify_enabled
            except ImportError:
                try:
                    from .resolvers.bridge import resolve as _resolve, clarify_enabled
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
                        from .resolvers.bridge import context_block
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
            # ★ 技能卡片 ↔ 工具激活解耦（2026-10-14）：prefetch 注入执行型技能时卡片
            #   点名 run_skill，它却是按需工具 ⇒ 交给 agent 在预选后激活（见 qd_agent._run_inner）。
            #   挂在 agent 上（同 last_user_query 范式），run 后由 _run_inner 清空防跨轮残留。
            try:
                agent.prefetch_force_tools = list(
                    prefetch_report.get("skill_force_tools") or [])
            except Exception:
                pass
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
                    if _should_store_assistant(text):
                        asyncio.run(self.memory.add(session_id, "assistant", text))
                except Exception as e:
                    logger.debug("[memory] 历史写入失败（fail-open）: %s", e)
            return text or ""
        finally:
            # 最终清理：先释放同会话锁（让同会话后续 run 可进），再删全局 _HOOKS[session_id]。
            # 顺序重要：锁释放后同会话后续 run 的 `_run_with_events` 才 register，不会被本次
            # unregister 误删（A-02 方向2 已知边缘局限：若后续 run 已提前 register，本次
            # unregister 可能误删其钩子 ⇒ 该 run 暂时不可中断，但不破坏状态；残留钩子被
            # 误读才会「误停止」，故此处无脑清理优于泄漏）。
            try:
                lk.release()
            except Exception:
                pass
            try:
                unregister_run_hooks(session_id)
            except Exception:
                pass

    # ── 对外契约：agent.chat(message, session_id=...) ─────────
    async def chat(self, message: str, session_id: str = "default", **kwargs):
        # A-01（2026-10-14，更正）：同会话串行化下沉到 `_run_sync`（T2 工作线程）内
        # 用 threading.Lock 实现 —— 若把锁放在此处用 asyncio.Lock，超时取消会让
        # `async with` 释放锁，孤儿 run 的 T2 线程逃逸串行化，与后续同会话 run 并发
        # 交错改同一 QDAgent 实例。此处只负责派发，串行化交由 `_run_sync` 完成。
        content = await asyncio.to_thread(self._run_sync, message, session_id)
        return _AgentResponse(content)


class _AgentResponse:
    """兼容旧 AgentResponse 的最小契约（message_queue/cli 只读 .content）。"""
    def __init__(self, content: str, **extra):
        self.content = content
        self.__dict__.update(extra)

    def to_dict(self) -> dict:
        return {"content": self.content}
