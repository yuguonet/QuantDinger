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


# 句子/行边界字符（按优先级从高到低）：换行 > 句末 > 分句 > 逗号
_BOUNDARY_CHARS = "\n。！？；"


def _clip(text: str, limit: int) -> str:
    """按 limit 截断，但**落在句子/行边界**上，不盲切。

    为什么改（2026-10-17 审计 I2a）：近史/参考资料原先用裸 `[:200]`/`[:300]`
    硬切，实测三条近史全部断在句子中间（"不建议在分歧转一"、"行情快照
    （2026-10-1"、"## 一、"）——断句残片进上下文，模型读着费 token 还容易
    把半句话当事实引用。切点优先取 ≤limit 的最后一个边界字符；前半段内
    一个边界都没有（罕见：无标点长串）才退化为硬切。
    """
    s = str(text or "")
    if len(s) <= limit:
        return s
    window = s[:limit]
    for ch in _BOUNDARY_CHARS:
        i = window.rfind(ch)
        if i >= limit // 2:          # 边界太靠前（前半段都没有）就换下一个字符
            return window[:i + 1]
    return window                    # 无可用边界：硬切（保持原上限语义）


def _first_paragraph(text: str, limit: int = 600) -> str:
    """取首个空行之前的首段（行内空白折叠），作为无 ## 结构技能的降级引导段。

    只在技能正文**没有任何 ## 标题**时才用——有结构时一律走 `_skill_block` 的
    首个 ## 段抽取（更贴近"使用场景"）。本函数是最后保底，**绝不盲截整篇**。
    """
    s = (text or "").strip()
    if not s:
        return ""
    para = re.split(r"\n\s*\n", s, maxsplit=1)[0].strip()
    para = re.sub(r"\s+", " ", para)
    return para[:limit]


def _history_headline(text: str, limit: int = 120) -> str:
    """把旧 assistant 答案**降级成一行标题**（记忆近史专用，2026-10-21）。

    【为什么降级而不是原样注入】记忆近史里的 assistant 段是**上一轮的完整
    诊断**（Markdown 标题 + 结论表格 + 关键位）。原样注入有两个实测问题：

      ① **过时数字混入**：实测捕获（tmp/llm_capture_step2_20261021.jsonl 步 2
         user 第 125 行）注入的是半截表格
         `| **今日表现** | 跌停收盘（-9.96%），光头光脚大阴线 |`——
         而本轮实际是 **+2.83%**。system 自己的数字纪律明写「价格、前高/前低、
         均线、涨跌停等关键锚点**必须本轮复核**」，输入里却摆着上次的涨跌停。
      ② **身份不可辨**：旧答案与模型自己刚产出的最终答案**格式完全同构**
         （`## 标题` + `| 表格 |`）⇒ 模型无法快速判断这段是"历史"还是"本轮事实"。

    【保留什么、丢弃什么】**留标题行（含标的/周期/时点）、丢正文**：
      · 标题行承载了"上次分析过什么标的、什么周期"——这是续接（同一标的追问、
        周期变化）真正需要的上下文；
      · **时点**额外抽一次（`MemoryMessage` 只有 role/content、**无时间戳字段**，
        只能从旧答案正文的「分析时点/诊断日期」里正则捞）——模型据此判断这条
        历史有多旧，是否还值得参考；
      · 正文里的价格/涨跌幅/关键位**全部是旧时点的**，留着比不留更危险
        （模型会当锚点用），故一律不进上下文。
      · 2026-10-21 用户裁定：**只做降级，不加警示头尾**（保持注入块形态不变）。

    【无标题时怎么办】取首个非空行（并按 limit 截断）——旧答案没有 `##`
    标题时也不至于退化成空串（空串会让 `- assistant:` 成为无信息的空行）。
    """
    s = str(text or "").strip()
    if not s:
        return ""
    title = ""
    for line in s.split("\n"):
        ln = line.strip()
        if not ln:
            continue
        if ln.startswith("#"):
            ln = ln.lstrip("#").strip()
            if ln:
                title = _clip(ln, limit)
                break
        # 非标题行：首个非空行就是标题（模型答案通常第一行就是 ## 标题）
        title = _clip(ln, limit)
        break
    if not title:
        return ""
    # 时点抽取：优先「分析时点 / 诊断日期 / 时点」后的日期时间，其次任意日期
    stamp = ""
    m = re.search(r"(?:分析时点|诊断日期|时点)\s*[:：]?\s*"
                  r"(\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}:\d{2})?)", s)
    if not m:
        m = re.search(r"(\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}:\d{2})?)", s)
    if m:
        stamp = m.group(1)
    if stamp and stamp not in title:
        title = f"{title}（{stamp}）"
    return title


def _should_store_assistant(text: str) -> bool:
    """本轮回复是否入会话记忆（best-effort 写侧闸门）。

    2026-10-14：预算熔断/步数耗尽/空回复/溯源拦截等**瞬态控制文案**会被
    `agent.run()` 当作 message 原样返回；若照旧落 `assistant` 记忆，下一轮预取
    又把它注回上下文 ⇒ 跨轮污染（实测 002866 全字段「未获取」的成因之一）。
    故只有**非空且非瞬态控制**的文本才写库。
    """
    return bool((text or "").strip()) and not is_transient_control(text)


# ── 技能选择（2026-10-01 重写：词典分 + 范围信号 + 意图信号）───────────────
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
#   ③ 历史权重乘子（G，2026-10-10）**已摘除**（2026-10-19 阶段 5，§14.3）：
#      其数据源 `qd_agent_weights.layer='skill'` 实锤为死链（权重全 1.0 且
#      n=0、键名错配，阶段 0 基线）⇒ 乘子恒 1.0、白占一次快照查询。技能
#      效果反馈收敛到新链唯一读口 `skill_events.weight_of()`
#      （qd_skill_feedback，版本隔离 + 样本门槛 + 限幅 [0.9,1.1]），由预选
#      Top-K 池消费（`candidate_index.apply_feedback_rerank`，final = base ×
#      weight，只调序不过滤）——避免排序双账本。旧治理链（evaluator 写入 /
#      skill_brewer.maybe_revise / weight_hints 其余三层）保留不动。
#      本函数（关键词回落路径）回归纯确定性：注入阈值看本函数的原始分。
_SCOPE_SINGLE = ("单只", "个股", "目标股票")     # 个股类技能
_SCOPE_SCREEN = ("全市场", "筛选")              # 选股/全市场类
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
    """技能注入块：小技能全文；超大技能 = 使用场景（首个 ## 段）+ 小节清单 + 按需读路标。

    【2026-10-20 修正·用户裁定】超大技能**不再盲截正文**：
    旧实现 `_clip(body, _SKILL_BODY_CAP)` 把正文前 2200 字按句边界砍一段塞进
    上下文，常砍在"使用场景"段中部 ⇒ 模型拿到半句指令（比"明确告知已截断"更难
    识破）。改为给模型**决策所需的最小信息**：
      ① 使用场景 —— 首个 ## 段（技能里通常即"使用场景/何时用"引导段），告诉模型
         "该不该用、怎么用"；该段之前真有 H1 之外的实质前言也一并保留；
      ② 小节清单 —— 全部 ## 段标题，模型据此决定 read_skill(heading=...) 取哪节；
      ③ 按需读路标 —— read_skill(name) 全文 / read_skill(name, heading=) 单节 / 资源。
    read_skill(heading=...) 的小节选择机制（skills_adapter.load_section）早已存在，
    此前注入层没用它、一直盲截正文——本次改为由它收口。_clip 只作为**单段极端过长**
    的兜底（不截断整篇、不截断决策段）。
    """
    if len(body) <= _SKILL_BODY_CAP:
        return body
    sections = []
    try:
        sections = skills.load_sections(name) or []
    except Exception as _he:
        # 不能静默：拿不到段落时下文 outline 退化成"（无）"，与"该技能本来
        # 就没有小节"在输出上完全无法区分，事后排查必然被误导。
        logger.debug("[skill_block] %s 取段落失败（fail-open）: %s: %s",
                     name, type(_he).__name__, _he)
    # 真实正文段（排除 (前言) 占位与无结构的 (全文) 包）
    body_sections = [s for s in sections if s.heading not in ("(前言)", "(全文)")]
    # 真·前言：首个 ## 之前、剔除 H1 标题行后仍有实质内容才保留（罕见；避免把
    # "# 文档标题"这种与技能名重复的行塞进去）。
    preamble = ""
    for s in sections:
        if s.heading == "(前言)":
            _clean = re.sub(r"(?m)^#\s+.*$", "", s.content).strip()
            if _clean:
                preamble = _clean
            break
    if body_sections:
        # ① 使用场景：首个 ## 段（最贴近"该不该用/怎么用"的引导段）
        scenario = body_sections[0].content.strip()
        # 兜底长度保护：单段极端过长（个别技能把使用场景写成长文）才按句边界截断
        if len(scenario) > _SKILL_BODY_CAP:
            scenario = _clip(scenario, _SKILL_BODY_CAP)
        if preamble:
            scenario = preamble + "\n\n" + scenario
        # ② 小节清单：所有 ## 段标题
        headings = [s.heading for s in body_sections]
        outline = "；".join(str(h).strip("# ").strip() for h in headings[:20]) or "（无）"
    else:
        # 无 ## 结构（罕见）：取首段作为降级引导段，绝不盲截整篇
        scenario = _first_paragraph(body)
        headings = []
        outline = "（无）"
    # ③ 资源路标：只在技能**真的有**引用文件时才给（实测多数技能无资源，
    # 无条件给会诱导一次必然失败的工具调用，白花一轮）。
    _res_hint = ""
    try:
        if skills.list_resources(name):
            _res_hint = (f'；参考资料用 read_skill(name="{name}", '
                         f'resource="references/<文件>")')
    except Exception:
        _res_hint = ""
    return (
        f"{scenario}\n\n"
        f"[技能大纲] 正文共 {len(body)} 字符，分 {len(headings)} 节：{outline}\n"
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


def _rank_skills(cands: list, keys, msg_l: str, has_entity: bool) -> list:
    """候选技能排序（纯确定性：词典分 + 范围/意图信号，无 LLM、无权重乘子）。

    2026-10-19 阶段 5（§14.3）：历史权重乘子摘除——数据源
    `qd_agent_weights.layer='skill'` 是死链（权重全 1.0 且 n=0，阶段 0
    实锤），且技能候选排序的反馈来源必须收敛到新链唯一读口
    `skill_events.weight_of()`（由预选 Top-K 池 `apply_feedback_rerank`
    消费：final = base × weight，只调序不过滤、冷启动恒等、限幅 [0.9,1.1]）。
    本函数是**预选不可用时的关键词回落路径**，保持纯确定性基线：排序稳定、
    可复现，也是反馈重排出问题时的对照基准。旧治理链（evaluator 写入 /
    skill_brewer.maybe_revise / weight_hints 其余三层）保留不动。

    返回 [(分, name), ...] 按分降序、同名按 name；注入阈值
    （_SKILL_INJECT_MIN）消费本函数的原始分。
    """
    out = []
    for c in cands:
        if not isinstance(c, dict):
            continue
        hay = f"{c.get('name', '')} {c.get('description', '')}".lower()
        nm = c.get("name") or ""
        sc = _skill_score(hay, keys, msg_l, has_entity)
        if sc > 0:
            out.append((sc, nm))
    out.sort(key=lambda t: (-t[0], t[1]))
    return out


def _build_default_model():
    """从 .env 构建 mimoagent 模型（OpenAI 兼容 chat 协议）。

    【F5（2026-10-17）：收敛到 llm.py —— agent 链路唯一 LLM 出口】
    此前这里直调 `mimoagent.models.get_model(config={...})`，与 chain/、skill/
    evidence 全都经 `llm.get_model` 形成**第二条构造路径**：绕过 provider 解析、
    模型实例缓存，且不带超时（回落 mimo 默认 3600s，一次网络挂起吊死整条链）。
    现统一走 `llm.get_model` ⇒ 与其余调用点共用同一份「解析 + 缓存 + 超时」。

    保持不变的部分：`temperature` / `max_tokens` 仍经 `model_kwargs` 原样透传；
    base_url / api_key / model 仍只认 `OPENAI_*`（与改造前逐字同源，不随
    `LLM_PROVIDER` 漂移）。

    超时（用户裁定 A1，2026-10-17）：主链默认 **120s**（原 mimo 默认 3600s），
    `QD_LLM_TIMEOUT` 显式设置时覆盖 —— 与 chain/skill 同一旋钮、同一默认值档。

    流式（2026-10-20，用户裁定「不拦截 mimoagent、走生产 SDK」后的重做）：
    mimo `OpenAIChatModel._query` 第 2 行即
    `stream = bool(call_kwargs.pop("stream", False))`，`call_kwargs` 来自
    `self.config.model_kwargs | kwargs` ⇒ **在 model_kwargs 传 stream 即可让主循环
    流式化**，不覆盖 mimo 内部方法、不替换模型类，走的仍是 mimo 自己的
    `self.client`（openai SDK）。实测流式墙钟约省 1/3（网关流式生成更快；
    且 openai SDK 非流式 timeout 计整段生成，流式 timeout 按 chunk 间计，
    大上下文慢尾不再 120s 读超时+重发）。`QD_LLM_STREAM=0` 回落非流式。
    """
    import os
    from .llm import get_model
    try:
        timeout = float((os.getenv("QD_LLM_TIMEOUT") or "").strip() or 120)
    except ValueError:
        timeout = 120
    # 默认开流式：见 docstring。注意 stream 进 model_kwargs 会进缓存键（_freeze），
    # 开/关是两个 Model 实例——正确语义（协议不同不该复用连接）。
    stream = (os.getenv("QD_LLM_STREAM") or "1").strip() not in ("0", "false", "False")
    return get_model(
        base_url=os.getenv("OPENAI_BASE_URL") or None,
        api_key=os.getenv("OPENAI_API_KEY", ""),
        model=os.getenv("OPENAI_MODEL", "qwen-plus"),
        timeout=timeout,
        model_kwargs={
            "temperature": float(os.getenv("AGENT_LLM_TEMPERATURE", "0.1")),
            "max_tokens": int(os.getenv("OPENAI_MAX_TOKENS", "16384")),
            "stream": stream,
        },
    )


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
                  resolve_info: Optional[dict] = None,
                  skill_hint: str = "") -> tuple[str, dict]:
        """拼装上下文并返回 (注入后任务文本, 留痕报告)。

        留痕报告（2026-10-01）记录每个候选块：注入/丢弃 + 字符数 + 原因，
        由 QDAgent.trace_prefetch 落 trace —— "为什么它会这么说"不再靠猜。

        resolve_info（2026-10-01 新增）：`resolvers/bridge.resolve()` 的产出。
        用于 ①注入已识别标的/领域 ②给技能选择提供"有没有标的"这个范围信号
        ③F2 返工（2026-10-17）：其 effective_input 含相对日就地标注（如「后天(2026-10-19)」），
        作为**模型看到的用户问题**下发，禁止模型自行口算相对日 ⇒ 消除日期幻觉。

        skill_hint（2026-10-17 新增，skill 进预选）：预选器 LLM 从**技能目录**
        里点名的技能名，作强提示覆盖 `_rank_skills` 的关键词 top-1。空串 =
        没有提示 ⇒ 与改造前完全一致（关键词选择仍是唯一路径）。
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
        # 时间/实体增强后的用户问题（2026-10-17 F2 返工）：解析器（TimeResolver 等）把
        # 相对日词**就地标注**进 effective_input（如「后天(2026-10-19)」）。必须把它作为
        # 模型看到的用户问题下发——否则模型只拿原始问句自算相对日 ⇒ 幻觉（run10 H1）。
        # 原始 message 仅作各种匹配（技能/记忆相关度）与回落，避免被增强文本污染。
        _user_q = str((resolve_info or {}).get("effective_input") or "").strip() or message
        # 记忆近史（相关度闸门：无关历史不注入——2026-10-01 文不对题回归，
        # 问跑马灯被旧股票记忆带偏成行情跑马灯）
        # ★ 2026-10-21 用户裁定（只做降级，不加警示头尾）：assistant 侧旧答案
        #   降级成**一行标题**（_history_headline）——旧答案正文里的价格/涨跌停/
        #   关键位全是旧时点的（实测注入的是"跌停 -9.96%"，而本轮实际 +2.83%），
        #   与 system 数字纪律「关键锚点必须本轮复核」冲突。user 侧原样保留
        #   （那是用户原话，无过时数字问题，且是相关度判定的依据）。
        if self.memory is not None:
            try:
                hist = asyncio.run(self.memory.get_history(session_id, limit=6))
                if hist:
                    _lines = []
                    for m in hist:
                        _role = getattr(m, "role", "user")
                        _content = str(getattr(m, "content", ""))
                        if _role == "assistant":
                            _lines.append(f"- {_role}: {_history_headline(_content)}")
                        else:
                            _lines.append(f"- {_role}: {_clip(_content, 200)}")
                    block = "\n".join(_lines)
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
        # F1（2026-10-17）：实际注入的技能名 ⇒ 供 service 侧按**实际注入技能**补声明工具
        _skill_resolved = ""
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
                best = ranked[0][1] if ranked else None
                best_score = ranked[0][0] if ranked else 0   # 阈值看原始分（无乘子）
                # ★ skill 进预选（2026-10-17）：预选器 LLM 点名的技能作**强提示**
                #   覆盖关键词 top-1。两个设计判据：
                #   ① 为什么可信——7 个技能里有 3 个都声明适用"分析某只股票"
                #      （auto-agent / auto-finance-analysis-stock / auto-stock-analyze），
                #      关键词 top-1 在这种真歧义上只能靠词频硬挑；LLM 是看着
                #      「技能名 + 适用场景 + 声明工具」选的，证据强一档。
                #   ② 为什么绕过 _SKILL_INJECT_MIN——该阈值防的是**关键词弱证据**
                #      误注大正文（2,178c 技能预览）；显式点名不属于它要防的情形。
                #      非法名（幻觉/不在目录）⇒ 丢弃回落关键词（fail-open，与
                #      parse_selection 的"编造名丢弃"同哲学），不静默、不猜。
                hint_used = False
                if skill_hint:
                    _hint = str(skill_hint).strip()
                    if _hint in {c.get("name") for c in cands if isinstance(c, dict)}:
                        best, hint_used = _hint, True
                    else:
                        logger.debug("[prefetch] 预选技能名不在目录，回落关键词选择: %s",
                                     _hint)
                hit = False
                if best and (hint_used or best_score >= _SKILL_INJECT_MIN):
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
                        _route = ("" if _exec_spec is not None else
                                  "（按序执行：技能已给出⇒**先走技能流程**；"
                                  "取数工具已在工具面可**直调**，无需 search_resources）")
                        parts.append(
                            f"[技能指针：{best}] 正文本会话已注入过；"
                            f"要完整正文用 read_skill(name=\"{best}\")，"
                            f"只看某小节用 read_skill(name=\"{best}\", heading=\"…\")。{_ptr_tail}{_route}")
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
                                parts.append(
                                    f"[相关技能：{best}]\n{blk}\n"
                                    f"（按序执行：技能已给出⇒**先走技能流程**；"
                                    f"取数工具已在工具面可**直调**，无需 search_resources）")
                                seen.add(best)
                                hit = True
                _skill_resolved = best if hit else ""
                blocks.append({"kind": "skill", "injected": hit,
                               "chars": len(parts[-1]) if hit else 0, "relevance": best_score,
                               "name": best or "", "hint": str(skill_hint or ""),
                               "hint_used": hint_used, "reason":
                                   (f"预选指定技能 {best}（LLM 语义选择，绕过关键词阈值）"
                                    if (hit and hint_used)
                                    else (f"命中技能 {best}（得分 {best_score}"
                                          f"，有标的={has_entity}）" if hit
                                          else f"最佳候选 {best} 得分 {best_score} "
                                               f"< {_SKILL_INJECT_MIN}，未注入"))})
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
                            lines.append(f"- {_clip(str(d.get('content', '')), 300)}")
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
            # F1（2026-10-17）：实际注入正文/卡片的技能名（"" = 未注入）⇒ service 按它
            # 补声明工具（`supplement_skill_declared`），消除"注入正文却无工具"悬空。
            "skill_resolved": _skill_resolved,
        }
        if not parts:
            return _user_q, report
        return "\n\n".join(parts) + f"\n\n[用户问题]\n{_user_q}", report

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
        agent = None                 # 供 finally 清 `_preselect_prerun`（防跨 run 残留）
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
            # ★ 把**用户原始问题**挂在 agent 上，供工具预选使用（2026-10-01）。
            #   预选绝不能用 prefetch 增强后的 task：那里面塞着记忆/RAG/技能块，
            #   实测把"今天北京天气"的预选 goal 带成了"个股综合分析"（历史记忆是
            #   个股分析）⇒ 点名 7 个股票工具 ⇒ 该任务 token +136%。这是"预选不稳定"
            #   的真正成因之一——不是模型随机，是**输入被上下文污染**。
            try:
                agent.last_user_query = message
                # 供收尾格式化（formatters）选 domain / 填实体，避免再解析一次
                agent.last_resolve_info = resolve_info or {}
                # 技能对象挂给 agent 供预选构**技能目录**（2026-10-17 skill 进预选，
                # 同 last_user_query 范式）：技能体系在服务侧，预选在 agent 侧。
            except Exception:
                pass
            try:
                agent.skills = self.skills
            except Exception:
                pass
            # ★★ skill 进预选（2026-10-17）：**预选先于 prefetch**。
            #    预选这一次 LLM 调用同时产出「技能 + 工具」；技能要当强提示传进
            #    _prefetch 覆盖 _rank_skills 的关键词 top-1 ⇒ 必须早于它。
            #    reset+预选整体前移（`_run_inner` 经 `_preselect_prerun_done()`
            #    跳过重跑——它内部的 reset 会清掉预选刚激活的工具）。
            #    fail-open：任一步抛异常 ⇒ flag 不置位 ⇒ _run_inner 走原路径
            #    （reset+预选照常，只是没有技能提示）⇒ 与改造前一致。
            skill_hint = ""
            try:
                skill_hint = str((agent.preselect_prerun(message) or {}).get("skill") or "")
            except Exception as e:
                logger.debug("[preselect] run 前预选失败（fail-open，转 _run_inner 原路径）"
                             ": %s: %s", type(e).__name__, e)
            # ★ 阶段4（2026-10-19，影子）：技能选择事件链（fail-soft，不换排序）。
            #   候选快照 = 预选 report 的 skill_catalog.candidates（eligible 全集 +
            #   分数 + 曝光标记，漏斗 eligible_but_not_exposed 的判据源）；选中技能
            #   版本在**选择时刻冻结**（registry active_version），后续文件变更
            #   不改写历史事件（§6.2）。注入/执行状态由 prefetch 与收尾回填。
            agent._skill_event_id = None
            try:
                import uuid as _uuid
                from .chain import skill_events as _se
                from .chain import skill_registry as _sr
                _sk_rep = getattr(agent, "last_preselect_report", None) or {}
                _sk_cat = _sk_rep.get("skill_catalog") or {}
                _sel = str(_sk_rep.get("skill") or "")
                _sel_ver = ""
                if _sel:
                    _sel_id = _sr.resolve_skill_id(getattr(agent, "skills", None), _sel)
                    _sel_ver = _sr.get_active_version(_sel_id) or ""
                agent._skill_event_id = _se.record_selection(
                    request_id=_uuid.uuid4().hex,
                    session_id=session_id,
                    query=message,
                    candidates=_sk_cat.get("candidates") or [],
                    selected_skill_id=_sel, selected_version=_sel_ver)
            except Exception as e:
                logger.debug("[skill-events] 选择事件记录跳过(fail-open): %s: %s",
                             type(e).__name__, e)
            task, prefetch_report = self._prefetch(message, session_id, resolve_info,
                                                   skill_hint=skill_hint)
            # ★ 阶段4（续）：实际注入的技能（可能是关键词回落 top-1，非 planner
            #   点名）标记到事件——选中与注入是两个状态（§6.2 漏斗第③段）。
            try:
                _ev = getattr(agent, "_skill_event_id", None)
                _inj = str(prefetch_report.get("skill_resolved") or "")
                if _ev and _inj:
                    from .chain import skill_events as _se2
                    from .chain import skill_registry as _sr2
                    _inj_id = _sr2.resolve_skill_id(getattr(agent, "skills", None), _inj)
                    _se2.mark_injected(_ev, _inj_id,
                                       _sr2.get_active_version(_inj_id) or "")
            except Exception as e:
                logger.debug("[skill-events] 注入标记跳过(fail-open): %s: %s",
                             type(e).__name__, e)
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
                # ★ F1（2026-10-17）：按**实际注入的技能**补声明工具。
                #   preselect 的技能补位只认 planner 点名的 skill 字段；而此处注入的
                #   `skill_resolved` 可能是关键词回落 top-1（planner 未点名）⇒ 正文
                #   引用的工具不在面里（run10 D1）。复用 agent.supplement_skill_declared
                #   （同源 _merge_skill_declared + build_skill_catalog + 同一上限）。
                _resolved = str(prefetch_report.get("skill_resolved") or "")
                if _resolved:
                    _add = agent.supplement_skill_declared(
                        _resolved, extra_selected=agent.prefetch_force_tools)
                    if _add:
                        agent.prefetch_force_tools = list(
                            agent.prefetch_force_tools) + list(_add)
            except Exception as e:
                logger.debug("[prefetch] 技能声明工具补位失败（fail-open）: %s", e)
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
            # 清一次性标记（skill 进预选，2026-10-17）：run 中途异常/用户停止时
            # `_run_inner` 可能根本没跑到消费点 ⇒ 标记残留会让**下一次** run
            # 跳过 reset+预选（上轮激活残留 + 无技能提示），是跨 run 污染源。
            if agent is not None:
                try:
                    agent._preselect_prerun = False
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
