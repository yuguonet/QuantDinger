# -*- coding: utf-8 -*-
"""
TraceCollector — Agent 执行追踪器（mimo 新系统的决策树写入侧，**现役**）。

在 agent 执行过程中自动收集信息，构建 EvalNode 树。
对 agent 透明，agent 不需要知道它的存在。

【2026-10-01 更正】文件头曾被打上 "DEPRECATED / 全后端零引用" 审计标记，
但 `qd_agent.py::_make_collector` 实际在引用本模块（mimo 迁移后唯一写入侧），
标记失准已撤；另注：`utils/tracing.py` 的 AgentTraceRecorder 是**旧 smolagents**
链路的写入侧，两条链并存，改任一侧都要回读另一侧。

【2026-10-01 实测修复：结构化预测字段假填充】
落库后 `qd_agent_traces`（layer='chain' 根节点 954 条）实测：
  - user_query 恒空（on_agent_finish 只塞进 input_params，没赋到节点字段）
  - stock_code 填充率仅 16.8%（只认 4 个工具入参键名，且从不看用户问题）
  - direction 69% 是默认兜底值 'neutral'、confidence 88% 是默认 0.5
    ⇒ 词表不含模型实际用词（"偏空/偏多"），T+N 闭环拿到的是常量而非预测。
本轮修复见 `_extract_direction_from_text` / `_extract_stock_code_from_query`。

树形结构（skill 是 tool 的容器，tool 不包含 skill）：
  chain (根)
  ├── skill: market_screener
  │   ├── tool: search_stocks
  │   └── tool: get_realtime_quote
  ├── skill: stock_analysis
  │   └── tool: get_kline
  └── tool: get_market_overview    ← 无 skill 归属，直接挂 chain

提取策略：JSON 优先，正则降级。
"""
from __future__ import annotations

from log import logger
import re
import time
from datetime import date
from typing import Any, List, Optional

from chain.schema import (
    EvalNode, Layer, Status,
)
from utils.json_parser import extract_decision


# ── 标的代码识别（2026-10-01）──────────────────────────────────
# 工具入参里可能承载股票代码的键名（按命中概率排序）。
_STOCK_CODE_KEYS = (
    "stock_code", "stockcode", "stock", "symbol", "code", "codes",
    "stock_codes", "symbols", "ts_code", "secid", "security", "ticker",
    "stock_list", "symbol_list",
)
# A 股代码：6 位数字。首位白名单排除掉日期/金额/百分比等纯数字噪音
# （0=深主板/中小, 3=创业板, 6=沪主板, 8=北交所, 4/9=科创/转板等）。
# 要求左右不为数字/小数点，避免从 "1234567.89" 里截出假代码。
_CODE_RE = re.compile(r"(?<![0-9.])([036894][0-9]{5})(?![0-9])")
# 代码前后的锚定词：说明这个 6 位数字**确实是标的**而不是金额/日期/编号。
# 除"股票/代码"等显式词外，也收常见任务动词（分析/走势/行情…）——
# 实测 "请分析一下603466当前的走势" 的锚定词在代码**之前**，只认显式词会漏。
_CODE_ANCHOR_RE = re.compile(
    r"(股票|个股|代码|标的|证券|stock|symbol|code|分析|走势|行情|资金|技术|"
    r"止损|买入|卖出|预测|看看|查一?下?|帮我)", re.I)


def _extract_stock_code_from_query(query: str) -> str:
    """从用户问题里兜底提取股票代码。

    为什么需要兜底：工具入参键名对不上时（批量接口、模型自定义参数名），
    stock_code 就永远为空，T+N 闭环拿不到标的 ⇒ 决策无法被行情验证。
    用户几乎总会把代码写在问题里（"分析603466"），故作为二级来源。

    降噪：命中多个时要求代码**前后 16 字内**有锚定词；只命中唯一一个则直接采信
    （单一 6 位数字出现在问句里，是标的的概率远高于是金额）。
    """
    if not query:
        return ""
    hits = list(_CODE_RE.finditer(query))
    if not hits:
        return ""
    if len(hits) == 1:
        return hits[0].group(1)
    for m in hits:
        seg = query[max(0, m.start() - 16): m.end() + 16]
        if _CODE_ANCHOR_RE.search(seg):
            return m.group(1)
    return ""


class TraceCollector:
    """Agent 执行过程中的自动追踪器。

    职责：
    1. 通过 begin_skill() 标记当前 skill（由 planner 输出驱动）
    2. 拦截 tool_call，自动归属到当前 skill 或作为 orphan 挂 chain
    3. Agent 结束时，组装完整 EvalNode 树（纯内存）
    4. flush() 由调用方在确认成功后写入 SQL
    """

    def __init__(self, session_id: str, user_query: str):
        self.session_id = session_id
        self.user_query = user_query
        self._stock_code = ""
        self._stock_name = ""
        self.start_time = time.time()
        self.intent_verb = ""
        self.intent_noun = ""
        self.domain = ""
        self._root: Optional[EvalNode] = None

        # ── 统一树结构 ──
        self._current_skill_node: Optional[EvalNode] = None
        self._skill_nodes: List[EvalNode] = []      # 所有 skill 节点
        self._orphan_tools: List[EvalNode] = []     # 无 skill 归属的 tool 节点
        self._all_tools_called: List[str] = []      # 聚合所有工具名（去重保序）

    # ── stock_code / stock_name 属性，自动规范化 dict → str ──

    @property
    def stock_code(self) -> str:
        return self._stock_code

    @stock_code.setter
    def stock_code(self, value):
        if isinstance(value, dict):
            results = value.get("results", [])
            self._stock_code = results[0].get("code", "") if results else ""
        elif value is not None:
            self._stock_code = str(value).strip()
        else:
            self._stock_code = ""

    @property
    def stock_name(self) -> str:
        return self._stock_name

    @stock_name.setter
    def stock_name(self, value):
        if isinstance(value, dict):
            results = value.get("results", [])
            self._stock_name = results[0].get("name", "") if results else ""
        elif value is not None:
            self._stock_name = str(value).strip()
        else:
            self._stock_name = ""

    # ── Skill 生命周期（由 planner 输出驱动）──────────────────

    def begin_skill(self, skill_name: str, tools: List[str] = None):
        """标记当前开始执行的 skill。

        由 planner_node 输出 current_skill 后，在 agent_node 创建 agent 前调用。
        后续所有 tool_call 自动归属到此 skill，直到 begin_skill 再次调用或
        agent 结束。
        """
        node = EvalNode(
            layer=Layer.SKILL.value,
            name=skill_name,
        )
        self._current_skill_node = node
        self._skill_nodes.append(node)
        logger.debug("[Trace] begin_skill: %s, tools=%s", skill_name, tools)

    def end_skill(self):
        """标记当前 skill 执行结束。"""
        self._current_skill_node = None

    # ── Tool 调用回调（由 TracedTool 自动触发）─────────────────

    @staticmethod
    def _summarize_for_storage(data: Any, max_items: int = 10) -> dict:
        """将工具返回数据压缩为摘要。"""
        if data is None:
            return {}
        if isinstance(data, dict):
            summary = {}
            for k, v in data.items():
                if isinstance(v, list) and len(v) > max_items:
                    summary[k] = v[:max_items]
                    summary[f"{k}_total"] = len(v)
                else:
                    summary[k] = v
            return summary
        if isinstance(data, list):
            return {"items": data[:max_items], "total": len(data)}
        return {"raw": str(data)[:1000]}

    def on_tool_call(self, tool_name: str, arguments: dict, result: Any,
                     elapsed_ms: float, error: str = None):
        """工具调用回调。自动归属到当前 skill 或作为 orphan。"""
        node = EvalNode(
            layer=Layer.TOOL.value,
            name=tool_name,
            input_params=arguments,
            output_data=self._summarize_for_storage(result),
            elapsed_ms=elapsed_ms,
            status=Status.FAILED.value if error else Status.OK.value,
            error=error or "",
        )

        # 自动提取 stock_code
        # 2026-10-01：键名表扩充。实测金融工具用的入参名五花八门
        # （codes/stock_codes/symbols/ts_code/security/ticker/stock_list…），
        # 旧表只有 4 个键 ⇒ 大量决策树的 stock_code 落空（填充率仅 16.8%）。
        # 值可能是 list（批量接口）⇒ 取第一个非空元素，多标的仍只记首个。
        if not self._stock_code:
            for key in _STOCK_CODE_KEYS:
                val = arguments.get(key) if isinstance(arguments, dict) else None
                if not val:
                    continue
                if isinstance(val, (list, tuple)):
                    val = next((v for v in val if v), None)
                if val:
                    self._stock_code = str(val)[:32]
                    break

        # 归属到当前 skill 或 orphan
        if self._current_skill_node:
            self._current_skill_node.add_child(node)
            if tool_name not in self._current_skill_node.tools_called:
                self._current_skill_node.tools_called.append(tool_name)
        else:
            self._orphan_tools.append(node)

        # 聚合工具名
        if tool_name not in self._all_tools_called:
            self._all_tools_called.append(tool_name)

    # ── Agent 结束，组装树并存库 ─────────────────────────────

    def on_agent_finish(self, final_answer: str, total_steps: int,
                        total_tokens: int, model: str) -> EvalNode:
        """Agent 结束，构建完整 EvalNode 树并存库。"""
        # 防空
        if not final_answer:
            final_answer = ""

        # 只解析一次 JSON
        extracted = self._extract_from_json(final_answer)

        # 从 extracted 取值，缺失则正则 fallback
        score = extracted.get("score")
        if score is not None:
            score = max(0, min(100, float(score)))
        else:
            score = self._extract_score_from_regex(final_answer)

        direction = extracted.get("direction", "")
        if not direction:
            direction = self._extract_direction_from_text(final_answer)

        action = extracted.get("action", "")
        if not action:
            action = self._extract_action_from_text(final_answer)

        signal = extracted.get("signal", "")
        if not signal:
            signal = self._extract_signal_from_regex(final_answer)

        confidence = extracted.get("confidence")
        if isinstance(confidence, (int, float)):
            confidence = max(0.0, min(1.0, float(confidence)))
        elif isinstance(confidence, str):
            confidence = {"high": 0.8, "medium": 0.5, "low": 0.3}.get(confidence, 0.5)
        elif confidence is None:
            confidence = self._extract_confidence_from_text(final_answer)

        # 标的代码三级兜底：工具入参 → 模型 JSON → 用户问题原文
        # （2026-10-01：前两级常常都空，导致 T+N 拿不到标的无法验证）
        stock_code = (self._stock_code or extracted.get("stock_code", "")
                      or _extract_stock_code_from_query(self.user_query))
        # 2026-10-01：user_query 此前**从未**落到 qd_agent_traces.user_query 列
        # （只塞进了 input_params JSON）⇒ 复盘时看不到当初问了什么。补上。
        root = EvalNode(
            layer=Layer.CHAIN.value,
            name=f"{self.domain}+{self.intent_verb}+{self.intent_noun}" if self.intent_verb else "agent",
            exec_date=date.today(),
            # ★ session_id 此前**从未下传** ⇒ qd_agent_traces.session_id 恒为空，
            #   后果有二：① 无法按会话回溯（复盘看不到同一对话的其他层）
            #            ② 更危险：**测试清理脚本靠 session_id 识别测试数据**，
            #               真实会话也是空 ⇒ 会被当成测试数据删掉。2026-10-01 修。
            session_id=str(self.session_id or ""),
            user_query=(self.user_query or "")[:2000],
            stock_code=str(stock_code)[:32],
            stock_name=self._stock_name or extracted.get("stock_name", ""),
            input_params={"user_query": self.user_query},
            analysis=extracted.get("analysis", final_answer[:2000]),
            score=score,
            direction=direction,
            action=action,
            signal=signal,
            confidence=confidence,
            timeframe=extracted.get("timeframe", ""),
            elapsed_ms=(time.time() - self.start_time) * 1000,
        )

        # 挂载 skill 节点（每个 skill 已包含其 tool 子节点）
        for skill_node in self._skill_nodes:
            root.add_child(skill_node)

        # 挂载无 skill 归属的 tool 节点
        for tool_node in self._orphan_tools:
            root.add_child(tool_node)

        # 聚合 tools_called（缓存查询依赖此字段）
        root.tools_called = list(self._all_tools_called)

        self._root = root
        return root

    def flush(self) -> Optional[int]:
        """将组装好的 EvalNode 树写入 SQL。调用方在确认成功后调用。"""
        if not self._root:
            return None
        from app.agent.chain import store
        execution_id = store.save_tree(self._root)
        self._root.id = execution_id
        return execution_id

    # ── JSON 提取（主路径）────────────────────────────────────

    @staticmethod
    def _extract_from_json(answer: str) -> dict:
        """从 Agent 输出的 JSON 中提取字段。返回空 dict 表示未命中。"""
        result = extract_decision(answer)
        return result if result else {}

    # ── 正则 fallback（降级路径）───────────────────────────────

    @staticmethod
    def _extract_score_from_regex(answer: str) -> Optional[float]:
        m = re.search(r'(?:评分|score)[：:\s]*(\d+(?:\.\d+)?)', answer, re.I)
        if m:
            return max(0, min(100, float(m.group(1))))
        return None

    # ── 方向/动作弱提取（2026-10-01 重写）──────────────────────
    # 旧实现的两个硬伤（实测 954 条根节点里 69% 落到默认 neutral）：
    #   ① 词表不含模型实际用词 —— 模型写"偏空/偏多"，旧表只有"看空/看多"；
    #   ② 全文中"支持上涨 / 压制下跌"这类**多空论据列表**必然同时命中两侧，
    #      靠"先判多后判空"的顺序硬选 ⇒ 恒等于列表顺序，与结论无关。
    # 改法：**结论段优先**（prompt 要求结论先行，模型第一段就是结论），
    # 结论段内命中即定；否则全文加权计分（结论段权重 3，论据段权重 1）。

    _BEAR_WORDS = ("看空", "偏空", "看跌", "做空", "走弱", "空头", "偏弱", "下行",
                   "卖出", "减持", "离场", "回避", "谨慎", "bearish", "sell", "跌破")
    _BULL_WORDS = ("看多", "偏多", "看涨", "做多", "走强", "多头", "偏强", "上行",
                   "买入", "增持", "建仓", "加仓", "bullish", "buy", "突破")
    # 强判断词：出现即代表模型**下了结论**，优先级高于计分。
    # （"综合方向：偏空""结论：看多"这类显式表述，不该被后面风险段里的
    #   "若跌破…卖出" 稀释掉 —— 实测看多用例就被稀释成了 neutral。）
    _STRONG_BEAR = ("看空", "偏空", "看跌", "做空", "bearish")
    _STRONG_BULL = ("看多", "偏多", "看涨", "做多", "bullish")

    @staticmethod
    def _conclusion_text(answer: str, span: int = 600) -> str:
        """取结论段：优先「结论/一句话判断/核心判断」之后的一段，否则取开头。"""
        if not answer:
            return ""
        m = re.search(r"(?:结论|一句话判断|核心判断|综合判断|判断[:：])", answer)
        if m:
            return answer[m.start(): m.start() + span]
        return answer[:span]

    # 否定词：中文的否定可以在词前（"不具备…上行"）也可以在词后
    # （"向上突破难度较大"），故取**前后各 5 字**的窗口检测（2026-10-01 实测：
    # 茅台回答"当前不具备强势上行基础 / 向上突破难度较大"里 bull 词被否定，
    # 只做前置检测会把它误判成 bullish，而真实结论是"偏空"）。
    _NEG_RE = re.compile(r"(不|未|无|难|缺乏|不够|尚未|没有|避免|别)")
    _NEG_WINDOW = 5
    # 判定所需的最小净优势：|score| < 2 视为"两边都在说"（多空论据列表的常态），
    # 宁可记 neutral 也不猜 —— 错标方向比空标更危险（会训练出反向权重）。
    _MIN_MARGIN = 1

    # 句读分隔符：否定检测**不得跨小句**（2026-10-01 修，见 _negated 的说明）
    _CLAUSE_SEPS = ("。", "！", "？", "；", "\n", "，", ",", ". ")

    @classmethod
    def _negated(cls, low: str, start: int, end: int) -> bool:
        """该词在其**所在小句**内是否被否定。

        ★★ 根因修复（2026-10-01）：旧实现取"词前后各 5 字"做窗口，**不切断句读**，
        于是"结论：偏空。当前不具备强势上行基础"里，句号后那句的"不"落进了"偏空"
        的窗口 ⇒ "偏空"被判为被否定 ⇒ 反向记成 bull ⇒ 最终 direction=bullish
        （实测：strong_vote=+1、score_direction=-1，两个口径给出相反结论，而
        `_extract_direction_from_text` 优先信 strong_vote，于是采信了错的那个）。
        追责链路复用本函数（chain/claims），错的标签会训练出反向权重，必须修到根上。

        修法：窗口在句号/分号/逗号/换行处截断——否定只在同一个小句里成立。
        """
        left = low[max(0, start - cls._NEG_WINDOW): start]
        right = low[end: end + cls._NEG_WINDOW]
        for sep in cls._CLAUSE_SEPS:
            i = left.rfind(sep)
            if i >= 0:
                left = left[i + len(sep):]
            j = right.find(sep)
            if j >= 0:
                right = right[:j]
        return bool(cls._NEG_RE.search(left + right))

    @classmethod
    def _strong_vote(cls, text: str) -> int:
        """强判断词净优势（带否定检测）：>0 偏多，<0 偏空，0 无显式结论。"""
        low = (text or "").lower()
        bull = bear = 0
        for w in cls._STRONG_BULL:
            for m in re.finditer(re.escape(w), low):
                neg = cls._negated(low, m.start(), m.end())
                bear += 1 if neg else 0
                bull += 0 if neg else 1
        for w in cls._STRONG_BEAR:
            for m in re.finditer(re.escape(w), low):
                neg = cls._negated(low, m.start(), m.end())
                bull += 1 if neg else 0
                bear += 0 if neg else 1
        return bull - bear

    @classmethod
    def _score_direction(cls, text: str) -> int:
        low = (text or "").lower()
        bull = bear = 0
        for words, add_bull, add_bear in ((cls._BULL_WORDS, "bull", "bear"),
                                          (cls._BEAR_WORDS, "bear", "bull")):
            for w in words:
                for m in re.finditer(re.escape(w), low):
                    negated = cls._negated(low, m.start(), m.end())
                    hit = add_bear if negated else add_bull
                    if hit == "bull":
                        bull += 1
                    else:
                        bear += 1
        return bull - bear

    @classmethod
    def _extract_direction_from_text(cls, answer: str) -> str:
        concl = cls._conclusion_text(answer)
        # ① 结论段里的强判断词优先（"偏空/看多"这类显式结论）
        v_concl = cls._strong_vote(concl)
        if v_concl:
            return "bullish" if v_concl > 0 else "bearish"
        v_all = cls._strong_vote(answer)
        if v_all:
            return "bullish" if v_all > 0 else "bearish"
        # ② 无显式结论 ⇒ 退化为加权计分（弱证据，需过最小净优势才采信）
        s_concl = cls._score_direction(concl)
        if abs(s_concl) >= cls._MIN_MARGIN:
            return "bullish" if s_concl > 0 else "bearish"
        s_all = cls._score_direction(answer)
        if abs(s_all) >= cls._MIN_MARGIN:
            return "bullish" if s_all > 0 else "bearish"
        return "neutral"

    # 条件从句前缀：这些句子里的"卖出/买入"是**预案**而非建议，
    # 直接按关键词扫会把"若跌破支撑则卖出"误判成 sell 建议（实测踩到）。
    _COND_RE = re.compile(r"(若[^。；\n]{0,60}|如果[^。；\n]{0,60}|一旦[^。；\n]{0,60}|"
                          r"跌破[^。；\n]{0,40}|止损[^。；\n]{0,40})")

    @classmethod
    def _extract_action_from_text(cls, answer: str) -> str:
        concl = cls._conclusion_text(answer).lower()
        # 先在结论段里剥掉条件从句再判；剥完仍无结论再看全文（同样剥）
        scope = cls._COND_RE.sub("", concl)
        if any(kw in scope for kw in ["买入", "buy", "建议买", "建仓", "加仓", "可考虑买"]):
            return "buy"
        if any(kw in scope for kw in ["卖出", "sell", "建议卖", "离场", "清仓", "减仓"]):
            return "sell"
        scope_all = cls._COND_RE.sub("", (answer or "").lower())
        if any(kw in scope_all for kw in ["建议买入", "建议买", "可以买入", "可买入"]):
            return "buy"
        if any(kw in scope_all for kw in ["建议卖出", "建议卖", "可以卖出", "清仓", "离场"]):
            return "sell"
        if any(kw in (concl + scope_all) for kw in ["跳过", "skip", "回避", "观望"]):
            return "skip"
        return "hold"

    @staticmethod
    def _extract_signal_from_regex(answer: str) -> str:
        m = re.search(r'(?:signal|信号)[：:\s]*(.+?)(?:\n|$)', answer, re.I)
        if m:
            return m.group(1).strip()[:200]
        return ""

    @staticmethod
    def _extract_confidence_from_text(answer: str) -> float:
        answer_lower = answer.lower()
        if any(kw in answer_lower for kw in ["高度确信", "非常确定", "high confidence"]):
            return 0.8
        if any(kw in answer_lower for kw in ["不太确定", "有风险", "low confidence"]):
            return 0.3
        return 0.5
