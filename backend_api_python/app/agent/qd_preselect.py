# -*- coding: utf-8 -*-
"""工具预选 —— run 前用 1 次轻量 LLM 点名本轮要用的工具，直接激活进首轮。

从 `qd_agent.py` 拆出（2026-10-13 P3 拆分）。

【解决什么】分层之后，模型要靠 `search_tools`→`list_tools`→`activate_tools` 三轮
才能摸到一个按需工具（实测一条选股任务 21 次调用里 **13 次是发现轮**，62%）。
预选把这三轮压成 run 前的**一次纯文本 LLM 调用** ⇒ 21 次 → 1+3~4 次。

【为什么不是静态白名单 `QD_EXTRA_CORE_TOOLS`】
静态白名单换领域就失效：把金融高频工具写进白名单，遇到"写个跑马灯"这种任务
纯属白挂（8.7k tokens/轮照付），而预选是**每次按问题**定的，跨领域自适应。
（静态白名单仍保留，用于"无论如何都要常驻"的极少数工具。）

【三个必须坚持的性质】
  1. **纯 fail-open**：任何异常/空选都退回 search_tools 老路，**绝不丢工具**；
  2. **只做加法**：不清空已激活项（多轮会话里上一轮的工具还可能用得上）；
  3. **确定性校正优先**：LLM 只做"词典查不出的语义匹配"，能被词典/索引查出来的
     交给 `lint_selection` —— 没有这一层，预选质量全靠模型运气（首版实测：
     选对省 25%，选错费 136%，三次同任务 +21%/−25%/+31%）。

【★ 输入必须剥掉 prefetch 注入块】见 `_preselect_query`：用加工后的文本做预选，
模型会被历史记忆带偏（问天气却点名 7 个股票工具 ⇒ token +136%）。
"""
from __future__ import annotations

import logging
from typing import List

from .preselect import build_catalog_grouped, build_preselect_messages, parse_plan, lint_selection, apply_lint, get_tool_index, has_domain_hint, capability_domain, fallback_domain_tools, fast_route_tools, detect_domains, shrink_catalog, _preselect_catalog_limit, single_target_default_tools
# 2026-10-17 skill 进预选：技能目录 + 两个 env 旋钮（QD_PRESELECT_SKILL /
# QD_PRESELECT_SKILL_TOOLS_MAX），关停时整段不出现（见 _preselect_skill_on）
# 2026-10-19 阶段3：Top-K 渐进披露为单一入口（QD_PRESELECT_SKILL_TOPK ≤0 时
# 内部委托 build_skill_catalog 旧全量路径）+ 单一预选目录字符预算（§14.4）
from .preselect import (build_skill_catalog, build_skill_catalog_topk,
                        _preselect_skill_on, _preselect_skill_tools_max,
                        preselect_catalog_chars)
# 2026-10-17 F14（E3）：O-14 分级目录的触发条件改判 schema 总字符数
from .preselect import tier_catalog_enabled

logger = logging.getLogger(__name__)

# ── F25 run 阶段机（显式时序合同，取代隐式 bool `_preselect_prerun`）──
#  idle → primed（service 预跑成功）→ running（`_run_inner` 已消费）→ idle（收尾/兜底）
#  真源 `_qd_run_phase_value`；`_preselect_prerun` 退化为派生只读视图 ⇒ 调用点零改。
_PHASE_IDLE = "idle"
_PHASE_PRIMED = "primed"
_PHASE_RUNNING = "running"

# 【2026-10-19 阶段3 职责切分】`PRESELECT_DOMAIN_CN` / `preselect_additions` 已迁至
# `preselect` 包（预选域词汇表属于预选管线知识，不属于 mixin 编排；qd_preselect
# 因此守住了 ≤600 行单职责预算，见 tests/test_v6_qd_agent_split）。消费方
# （qd_tool_face / tests/test_v17c）改为从 `app.agent.preselect` 导入。


class PreselectMixin:
    """工具预选：以 mixin 形式并进 QDAgent（必须排在 MimocodeAgent 之前）。"""

    # prefetch 拼装格式：`块\n\n[用户问题]\n{原始消息}`（见 QDAgentService._prefetch）
    _USER_QUERY_MARK = "[用户问题]"

    def _preselect_query(self, task) -> str:
        """取预选应看的**用户原始问题**，剥掉 prefetch 注入的记忆/RAG/技能块。

        【为什么必须剥·2026-10-01 实测】
        直接用增强后的 task 做预选，模型会被注入的历史记忆带偏：测天气任务时，
        记忆里存着前几轮的个股分析，于是预选的 goal 写成了"在用户给出单只 A 股
        代码或名称后…形成综合诊断"，据此点名 7 个股票工具 ⇒ 该任务 token +136%。
        同一份代码换个上下文就换一批工具——看起来像"模型随机/不稳定"，
        实为**输入污染**。预选要判的是"用户这一轮要什么"，不是"历史上聊过什么"。

        三级来源：① service 挂在 agent 上的 `last_user_query`（最准）
                  ② task 里 `[用户问题]` 之后的部分（prefetch 有注入时）
                  ③ 整个 task（无注入时它本就是原始消息）
        """
        raw = task if isinstance(task, str) else str(task or "")
        # ① service 显式挂的原始问题
        orig = str(getattr(self, "last_user_query", "") or "").strip()
        if orig:
            return orig
        # ② 从 prefetch 拼装结构里截出用户问题段
        mark = self._USER_QUERY_MARK
        pos = raw.rfind(mark)
        if pos >= 0:
            return raw[pos + len(mark):].strip()
        # ③ 无注入：task 即原始消息
        return raw.strip()

    def _merge_skill_declared(self, skill_name: str, selected: List[str],
                              valid: List[str], skill_tools_map: dict) -> tuple:
        """技能声明工具补位（2026-10-17 skill 进预选）。

        【为什么需要它】run8 实证：auto-agent 步骤 1 就要 `agent_get_kline`，
        而预选器盲于技能正文（只看用户问题）⇒ 没点它 ⇒ 该步骤被静默跳过。
        frontmatter 的 `tools:` 是**方法论合同**，不是建议——技能作者写下的
        取数依赖，不该由预选模型"碰巧想起来"。

        【三条规则】
          1. **加法**：不撤模型已点的任何工具（模型可能有合同外的辅助需求）；
          2. **只补按需层合法名**（`valid`）：常驻层已有、不必占激活名额；
             注册表里根本没有的进 `unknown`（声明陈旧，fail-loud 留痕）；
          3. **有上限，但声明优先占满**：room = `max(_preselect_skill_tools_max(),
             声明数)`（F6，2026-10-17）——技能声明是「方法论合同」，其声明的工具
             应优先占满 room，而不是被固定 4 个截断（auto-stock-analyze 声明 15 个）。
             但**整体仍受 `QD_MAX_ACTIVE_TOOLS` 硬闸**（`activate_tools` 容量）约束：
             room 只决定"补位器愿意补几个"，最终能否同时在场由硬闸裁决（硬闸不改）。

        Returns:
            (added, skipped, unknown)：补进去的 / 超出上限没补的 / 未注册的。
        """
        if not skill_name or not skill_tools_map:
            return [], [], []
        declared = list(skill_tools_map.get(skill_name) or [])
        if not declared:
            return [], [], []
        valid_set = set(valid or ())
        picked = set(selected or ())
        added, skipped, unknown = [], [], []
        room = max(_preselect_skill_tools_max(), len(declared))
        for t in declared:
            if t in picked or t in added or t in unknown:
                continue
            if t in valid_set:
                if len(added) < room:
                    added.append(t)
                else:
                    skipped.append(t)
            elif t not in (self._defs_by_name or {}):
                unknown.append(t)     # 注册表里没有 ⇒ 声明陈旧（改名/已下线）
            # 其余：常驻层已带（本轮工具面必有），静默跳过（不是异常）
        return added, skipped, unknown

    def supplement_skill_declared(self, skill_name: str, extra_selected=None) -> list:
        """正文注入技能 ↔ 工具面同源补位（F1，2026-10-17）。

        【为什么需要它】preselect 的技能补位只认 **planner 点名的 skill 字段**；
        而 prefetch 注入正文的 `best` 可能是**关键词回落** top-1（planner 未点名
        时）⇒ 正文引用的工具不在本轮工具面（run10 D1：注入 auto-stock-analyze
        正文，但 `get_chip_distribution` 未激活 ⇒ 模型 `call_capability` 绕 3 轮
        ≈60.5k tok / 50.5s）。⇒ 按**实际注入的技能**再补一次，与 preselect 补位
        **同源**（同一 `_merge_skill_declared` + 同一 `build_skill_catalog` +
        同一 `_preselect_skill_tools_max()` 上限），消除"注入正文却无工具"悬空。

        Args:
            skill_name: prefetch 实际注入的技能名（`report['skill_resolved']`）。
            extra_selected: 已确定要强激活的工具（`prefetch_force_tools`），与
                `self._activated` 一起作为去重基准 ⇒ 不重复补。

        Returns:
            本次补入的按需工具名（去重、受 `_preselect_skill_tools_max()` 上限）。
        """
        if not skill_name or getattr(self, "skills", None) is None:
            return []
        try:
            _catalog, skill_tools_map = build_skill_catalog(self.skills)
        except Exception:
            return []
        valid = list(getattr(self, "_on_demand_names", None) or [])
        selected = list(self._activated) + list(extra_selected or [])
        added, _skipped, _unknown = self._merge_skill_declared(
            skill_name, selected, valid, skill_tools_map or {})
        return added

    def preselect_prerun(self, task: str | dict) -> dict:
        """run 前预跑「reset + 预选」（2026-10-17 skill 进预选）。

        【为什么从 `_run_inner` 提到 service 侧】技能选择要当**强提示**传进
        `_prefetch`（覆盖 `_rank_skills` 关键词 top-1，见 qd_service）⇒ 预选
        必须先于 prefetch。而 `reset_run_activation()` 必须先于预选（否则上
        run 的残留激活挤占 FIFO 名额、顶掉预选成果）⇒ 两者一起前移。
        `_run_inner` 经 `_preselect_prerun_done()` 跳过重跑——它内部的 reset
        会**清掉**本轮预选刚激活的工具。

        【fail-open】任一环节抛异常 ⇒ flag 不置位 ⇒ `_run_inner` 走原路径
        （reset+预选照常发生，只是没有技能提示）⇒ 与改造前一致。
        """
        self.reset_run_activation()
        report = self._maybe_preselect_tools(task)
        self.last_preselect_report = report
        self._preselect_prerun = True
        return report

    # ── F25 run 阶段机（真源；`_preselect_prerun` 为派生只读视图）──────────
    #  【为什么】`_preselect_prerun`（一次性 bool）+ `reset_run_activation` +
    #  service finally 清标记构成隐式时序合同，跨 3 文件互相约定、无一处声明。
    #  升级为显式 phase ⇒ 调用点（`qd_preselect.py:193` 写 True /
    #  `qd_service.py:797` 写 False / `qd_agent.py:292` 读 done）零改动。
    @property
    def _qd_run_phase(self) -> str:
        return getattr(self, "_qd_run_phase_value", _PHASE_IDLE)

    @_qd_run_phase.setter
    def _qd_run_phase(self, value: str) -> None:
        if value not in (_PHASE_IDLE, _PHASE_PRIMED, _PHASE_RUNNING):
            raise ValueError(f"unknown run phase {value!r}")
        self._qd_run_phase_value = value

    @property
    def _preselect_prerun(self) -> bool:
        return self._qd_run_phase == _PHASE_PRIMED

    @_preselect_prerun.setter
    def _preselect_prerun(self, value: bool) -> None:
        self._qd_run_phase = _PHASE_PRIMED if value else _PHASE_IDLE

    def _preselect_prerun_done(self) -> bool:
        """消费 run 阶段机的一次性迁移：`primed → running`（F25）。

        service 预跑成功 ⇒ phase=primed（`preselect_prerun` 写 `_preselect_prerun=True`）；
        `_run_inner` 读到 True ⇒ 跳过自身 reset+预选。读即迁移到 running，再读返 False；
        已 running 时重复消费 ⇒ 告警不抛（fail-loud 记痕）。run 中途异常没读到时，
        由 service 的 finally 兜底（`_preselect_prerun=False` ⇒ phase 回 idle，防跨 run 残留）。
        """
        phase = getattr(self, "_qd_run_phase", _PHASE_IDLE)
        if phase == _PHASE_PRIMED:
            self._qd_run_phase = _PHASE_RUNNING
            return True
        if phase == _PHASE_RUNNING:
            logger.warning("[preselect] run 已 running，重复消费 `_preselect_prerun_done`（phase=%s）", phase)
        return False

    def _maybe_preselect_tools(self, task: str | dict) -> dict:
        """run 前用 **1 次轻量 LLM** 把「预计要用的工具」直接激活进首轮。

        Returns:
            {"enabled","selected","reason","catalog_size","unknown","raw"}
        """
        report = {"enabled": bool(getattr(self.config, "tool_preselect", True)),
                  "selected": [], "reason": "", "catalog_size": 0, "unknown": [], "raw": ""}
        if not report["enabled"]:
            report["reason"] = "已关闭(QD_TOOL_PRESELECT=0)"
            return report
        if self.config.tool_tier_mode != "tiered":
            report["reason"] = "非分层模式(全量已下发)"
            return report

        on_demand = list(getattr(self, "_on_demand_names", []) or [])
        if not on_demand:
            report["reason"] = "无按需工具"
            return report

        query = self._preselect_query(task)
        report["query"] = query[:200]

        # 三级分级：**域内优先于能力层**。
        # 能力层（capabilities/ 准入函数）是兜底：目录里排在域内之后并明确标注
        # "仅当域内无覆盖时才选"；即便模型仍选了它，lint 的 R0 也会让位裁掉。
        # ★ 提到闸门之前：闸门拦下时也要有候选集可供**域兜底**（纯名单计算，零成本）。
        cap_domain = capability_domain()
        cap_names = [n for n in on_demand if self._tool_domain(n) == cap_domain]
        dom_names = [n for n in on_demand if n not in set(cap_names)]
        report["capability_candidates"] = len(cap_names)

        # ★ 域特征闸门：纯字符串匹配，成本≈0。命中才值得发那次 3k-token 的目录。
        #   （没有它，天气类问题会被硬塞 8 个股票工具，token +136%。详见
        #    tool_preselect.has_domain_hint 的注释。）设 QD_PRESELECT_GATE=0 可关闭闸门。
        # 【F13 2026-10-17】闸门（成本闸）**移到快路由之后**：漏判时只跳 LLM plan，
        #   仍先走零成本确定性快路由；并把"漏判"落盘进 preselect_stats ⇒ 长期漏判率
        #   可测。原实现在此处直接 return，既跳过了确定性快路由、也不留痕（R2/D-8）。
        #   闸门只是「省不省那次 LLM」的判决，**不是**"这任务不需要工具"：
        #   关键词没覆盖到的金融问法（如"帮我看看大盘"）同样需要工具面，交给收口兜底。
        gate_miss = bool(self.config.preselect_gate) and not has_domain_hint(
            query, self.config.preselect_hints)

        # ★★ 确定性快路由（2026-10-16 T5）：单标的 + 单域意图 ⇒ 直接给工具组，
        #    跳过这次 LLM 与 ≈3k 目录。判据全部确定性（见 fast_route_tools），
        #    未命中一律 fail-open 落回原路径，语义与不接这一段完全一致。
        fast_names, fast_why = fast_route_tools(query, dom_names)
        report["fast_route"] = {"hit": bool(fast_names), "why": fast_why}
        if fast_names:
            act = self.activate_tools(fast_names, source="preselect")
            report["selected"] = list(act.get("activated", [])) + list(act.get("already", []))
            report["unknown"] = list(act.get("unknown", []))
            if report["selected"]:
                report["reason"] = "ok(快路由:%s)" % fast_why
                try:
                    from .preselect import record_selected
                    record_selected("fast_route")
                except Exception:
                    pass
                return report
            report["reason"] = "快路由候选激活未命中(unknown=%s)，回落 LLM 预选" % (
                report["unknown"] or "—")

        # 闸门漏判：快路由未兜住 ⇒ 只跳 LLM plan（省 ≈3k 目录），仍走域兜底。
        if gate_miss:
            report["gate_miss"] = True
            report["reason"] = "无领域特征，跳过 LLM 预选(闸门)，仅确定性兜底"
            try:
                from .preselect import record_gate_miss
                record_gate_miss(query)
            except Exception as e:
                logger.debug("[preselect] gate_miss 落盘跳过(fail-open): %s: %s",
                             type(e).__name__, e)
            return self._finish_preselect(report, query, dom_names)

        # ── 差距 K（共现权重排序）：分数 = 启发式 × 权重 ─────────────────
        # 周级离线攒「任务→实际调用工具」对算共现（preselect/preselect_cooc.py），
        # 这里只改**目录呈现顺序**（候选集合不变 ⇒ token 不回升）。
        # 权重缺失/样本不足/零覆盖/异常 → 原序（冷启动与现状 bit 级一致）。
        try:
            from .preselect import get_weights as _cooc_get_weights
            from .preselect import rank_candidates as _cooc_rank
            dom_names = _cooc_rank(
                dom_names, query, _cooc_get_weights(),
                index=get_tool_index(self._defs_by_name))
        except Exception as e:
            logger.debug("[preselect] 共现排序跳过(fail-open): %s: %s",
                         type(e).__name__, e)

        # ★ 设计 §3.2「域内优先」三处落实的**第①处**：目录分两段，能力层垫底。
        #   2026-10-11 曾为省 token 把第三参改成 []（能力层不进目录，只留一行计数
        #   指针），2026-10-13 P14 已回退——理由是它造成**功能性断裂**，不只是优先级
        #   退化：PRESELECT_SYSTEM 明写「目录里找不到对应工具就删掉该交付要素」，而
        #   指针行只说"另有 N 个"、不列名字 ⇒ 模型既无法按名调用、也会把只能由能力层
        #   支撑的交付要素删掉。能力层是**域内无覆盖时的唯一兜底**，不可见即不可用。
        #   代价实测：目录 4,144 → 6,946 字符（+2,802，+67.6%），仅发生在预选那一轮；
        #   相对全量 schema 49,507 字符仍是量级优势（§22.4）。
        #   —— 这是"省 token"与"能力可达"的取舍，本项目取后者：省下的 token 换不来
        #      一条被误删的交付要素。
        #
        # ★★ 2026-10-17 目录预过滤接线（用户点破"所有工具+描述都喂给 LLM，300 个爆掉"）：
        #   `shrink_catalog` 自 2026-10-16 写好后零调用，预选每次全量 87 行目录
        #   （实测 7197 字符）。现按 QD_PRESELECT_CATALOG_MAX（默认 24）收窄**域内段**：
        #   · 域词命中 ⇒ 命中域全收（细粒度域映射，DATA_DOMAIN_TOOLS 反转）；
        #   · 无域词 + 单标的（「分析600519」）⇒ 单股十域首选代表集；
        #   · 都不命中 ⇒ 原样返回（fail-open：空目录比冗余更致命）。
        #   能力层段**保持全量**（P14 裁定的不可见即不可用；其 O(N) 治理另行拍板）。
        #   ⚠ 分离原则：缩的只是**目录呈现**，`parse_plan`/`lint_selection`/域兜底
        #   的合法点名集仍是全量按需层（`valid`）——目录外的工具模型从正文/历史
        #   捞到名字照样能激活，lint R1 补位也全网可补。300 工具场景预选输入
        #   恒定 ≈2k 字符，与工具总数脱钩；未进目录的工具仍可 search_tools 发现。
        valid = list(dom_names) + list(cap_names)   # 合法点名集（全网）
        catalog_cap = _preselect_catalog_limit()
        # ★ 2026-10-17 F14（E3）：O-14「分级目录」的触发条件由「工具面破 120 个」
        #   改为 **schema 总字符数**（`QD_PRESELECT_TIER_CHARS`，默认 60000）。
        #   原因：真实成本变量是字符数——实测工具 92→101 仅 +10%，schema
        #   49.5k→75.3k +52%，按个数触发会漏掉「工具没涨、描述涨胖」。
        #   高于阈值 ⇒ 收窄域内段（省预选那一轮目录 token）；低于阈值 ⇒ 全量目录
        #   （面本就便宜，给足命中率）。0/负 ⇒ 恒分级（等价改造前行为）。
        tier_on = tier_catalog_enabled(self._defs_by_name)
        cat_dom_names = dom_names
        if catalog_cap > 0 and tier_on:
            defaults = single_target_default_tools(query, dom_names)
            cat_dom_names = shrink_catalog(
                query, dom_names, self._defs_by_name,
                limit=catalog_cap, defaults=defaults)
        report["catalog"] = {
            "cap": catalog_cap, "pre_size": len(dom_names),
            "post_size": len(cat_dom_names), "capability_size": len(cap_names),
            "tier": tier_on,
        }
        catalog, names = build_catalog_grouped(
            self._defs_by_name, cat_dom_names, cap_names, max_desc=60)
        report["catalog_size"] = len(names)
        if not names:
            report["reason"] = "目录为空"
            return report
        # ── skill 进预选（2026-10-17）：技能目录**排在工具目录之前** ──────
        # 走模板 `{skills}` 槽（贴在工具目录标题行行首）：空串时该行与改造前
        # 逐字一致 ⇒ v9 基线零登记、关停路径零痕迹。关 QD_PRESELECT_SKILL=0
        # ⇒ 整段不出现（守卫 tests/test_v17j_*）。
        # ★ 2026-10-19 阶段3 Top-K 渐进披露（§5/§14）：技能段不再随总数线性
        #   增长（阶段 0 实测每技能 +94c）。`build_skill_catalog_topk` 是单一
        #   入口：QD_PRESELECT_SKILL_TOPK 默认 4（3 常规 + 1 探索位），≤0 内部
        #   委托旧 build_skill_catalog 全量路径（回退逐位一致）。report 的
        #   skill_catalog 记 mode/topk/total/shown/hidden/chars/funnel/explore
        #   供影子对照（阶段 4 事件链消费同一口径）。
        skill_tools_map: dict = {}
        skill_section = ""
        skill_meta: dict = {"mode": "off", "size": 0}
        if _preselect_skill_on():
            skill_section, skill_tools_map, skill_meta = build_skill_catalog_topk(
                getattr(self, "skills", None), query)
        report["skill_catalog"] = skill_meta
        # §14.4 单一预选目录字符预算：工具目录 + 技能段 + 固定模板骨架
        # （不含选中后才加载的技能正文）。与 QD_PRESELECT_TIER_CHARS 同一阈值
        # 体系，不新增平行技能字符阈值；数值进 report 供影子观测。
        report["preselect_chars"] = preselect_catalog_chars(catalog, skill_section)

        max_tools = max(1, int(self.config.preselect_max_tools))
        # ── 闭环消费端（2026-10-01）：盘后产出的权重在这里影响"选哪些工具" ──
        # 权重表此前是断头路（无人读取）；本处 + 技能选择（qd_service）是它的两个
        # 消费点。提示失败一律 fail-open（空提示 = 与没有历史数据时行为一致）。
        # 领域命中率提示按本轮 query 过滤（2026-10-17，run8 §2.6）：此前无视
        # 问什么、把快照里所有低命中域倒给每一轮。
        hist_hint = ""
        try:
            from .chain.weight_hints import hint_text
            hist_hint = hint_text(names, query=query) or ""
        except Exception as e:
            logger.debug("[preselect] 权重提示跳过(fail-open): %s: %s",
                         type(e).__name__, e)
        report["weight_hint"] = hist_hint[:400]
        try:
            # 纯文本回合（不带 tools），与 mimo 压缩摘要同范式：
            # 防止模型在"选工具"这一步又去调工具，死循环。
            # T6（2026-10-16）：编码类工具已延迟暴露（T2）⇒ 目录里必须能被识别。
            # 仅在**本轮确实存在延迟的编码工具**时给出线索，其余情况为空串
            # （不改 system、不引入无关 token）。
            _coding = []
            try:
                _labels = getattr(self, "_DEFERRED_NATIVE_LABELS", None) or {}
                # ★ 2026-10-17：存在性判据用合法点名集（全网）而非目录集——编码
                # 工具被目录预过滤裁掉时，本提示里的名字仍可被 parse_plan 的正文
                # 逐字扫描捞到（hint 就在 user message 里），不受裁剪影响。
                _coding = [n for n in _labels if n in valid]
            except Exception:
                _coding = []
            _coding_hint = (
                "提示：如需读取/修改文件、运行脚本或调试代码，优先从目录里选择："
                + ", ".join(sorted(_coding))
            ) if _coding else ""
            # 【2026-10-16 用户裁定：不加预选超时预算】
            #   实测预选一次可达 152.7s（输出 1876 tokens 多为隐藏推理，独占整轮 85% 墙钟），
            #   曾尝试用独立实例 + `QD_PRESELECT_TIMEOUT`(25s) 限制；用户要求撤回 ⇒
            #   恢复"直接使用主模型、不做额外超时覆盖"。若日后要再优化预选耗时，
            #   应改**输入规模/候选集**或换模型，而不是给它加超时。
            resp = self.model.query(build_preselect_messages(
                query, catalog, max_tools, already=self._core_names,
                history_hint=hist_hint, coding_hint=_coding_hint,
                skills=skill_section))
            text = self._text_of(resp)
            report["raw"] = (text or "")[:500]
            # ★ 2026-10-17：valid_names 用**合法点名集（全网）**而非目录集——
            #   目录被预过滤后，模型从正文/历史/推理里逐字捞到的目录外工具名仍然
            #   合法（可激活、lint R1 可补位）。缩目录只缩"LLM 能看到什么"。
            plan = parse_plan(text, valid, max_tools,
                              valid_skills=sorted(skill_tools_map))
        except Exception as e:
            report["reason"] = f"预选调用失败(fail-open): {type(e).__name__}: {e}"
            logger.warning("[preselect] %s", report["reason"])
            return self._finish_preselect(report, query, valid)

        report["goal"] = plan.get("goal", "")
        report["deliverables"] = plan.get("deliverables") or []
        selected = plan.get("tools") or []
        # 【口径】`llm_selected` = **模型自己点的**（补位之前），与
        # `skill_declared_added`（方法论合同补的）分列 ⇒ trace 上能直接对账
        # "哪些是模型判断、哪些是技能要求"，混在一起会把补位算成模型的功劳。
        report["llm_selected"] = list(selected)
        # ★ 技能声明工具补位（2026-10-17）：排在"模型未点名"判空**之前**——
        #   声明工具是方法论合同，模型判了"纯问答"也不该断链路。
        report["skill"] = plan.get("skill") or ""
        declared_added, declared_skipped, declared_unknown = self._merge_skill_declared(
            report["skill"], selected, valid, skill_tools_map)
        if declared_added:
            report["skill_declared_added"] = declared_added
        if declared_skipped:
            report["skill_declared_skipped"] = declared_skipped
        if declared_unknown:
            # 【fail-loud】声明了注册表里根本没有的工具 = 技能 frontmatter 陈旧
            # （实测 auto-stock-analyze 仍写着已改名的 get_fund_flow_daily）。
            # 静默 pass 会让"技能在喊一个不存在的工具"永远不被发现 ⇒ 留痕。
            report["skill_declared_unknown"] = declared_unknown
            logger.warning("[preselect] 技能 %s 声明了未注册工具（frontmatter 陈旧）: %s",
                           report["skill"], ",".join(declared_unknown))
        selected = selected + [t for t in declared_added if t not in selected]
        if not selected:
            report["reason"] = "模型未点名(可能是纯问答)"
            return self._finish_preselect(report, query, valid)

        # ── 确定性校正（不调 LLM）：LLM 选得对不对由这一层判 ──────────
        # 旧系统 plan_linter 的立身之本：能被词典/索引查出来的不劳 LLM，
        # LLM 只做确定性查不了的语义匹配。没有这一层，预选质量全靠模型运气
        # （首版实测：选对省 25%，选错费 136%，三次同任务 +21%/−25%/+31%）。
        try:
            lint = lint_selection(
                query, selected,
                available_names=valid,
                index=get_tool_index(self._defs_by_name),
                plan_text=" ".join([plan.get("goal", "")] + list(plan.get("deliverables") or [])),
                # 【技能声明工具受保护】R4 零相关裁剪的判据是"plan 正文/域词典
                # 是否提到它"，而技能正文（模型没读到）才是提到它的地方 ⇒
                # 不护住就会被 R4 裁掉（补了等于没补）。护住≠免检：它仍参与
                # R0 能力层让位、R2/R3 等其他规则（见 lint_selection）。
                protected_names=list(self.config.extra_core_tools or [])
                + [t for t in declared_added if t in selected],
                parsed=bool(plan.get("parsed")),
                capability_names=cap_names,
                domain_names=dom_names,
            )
            report["lint"] = lint.to_trace()
            selected = apply_lint(selected, lint, capability_names=cap_names)
        except Exception as e:
            # lint 出错不阻断：原样放行（fail-open 一致性）
            logger.warning("[preselect] lint 异常(fail-open): %s", e)
            report["lint_error"] = f"{type(e).__name__}: {e}"

        if not selected:
            report["reason"] = "lint 判定整体误判，已作废(回退 search_tools)"
            return self._finish_preselect(report, query, valid)
        act = self.activate_tools(selected, source="preselect")
        report["selected"] = list(act.get("activated", [])) + list(act.get("already", []))
        report["unknown"] = list(act.get("unknown", []))
        report["reason"] = "ok"
        return self._finish_preselect(report, query, valid)

    @staticmethod
    def _empty_route(report: dict) -> str:
        """把「为什么走到空工具面」归类成稳定标签（供 counters 分组，便于事后归因）。

        标签稳定很重要：如果直接用 report["reason"] 原文，每次改文案统计就断了
        ⇒ 这里只认 reason 里的**关键字**，认不出一律归 "unknown"（不猜）。
        """
        reason = str(report.get("reason") or "")
        low = reason.lower()
        if "闸门" in reason or "gate" in low:
            return "gate"                    # 关键词没覆盖到 ⇒ 该放宽 hints
        if "未点名" in reason or "no_pick" in low:
            return "no_pick"                 # 模型觉得是纯问答
        if "作废" in reason or "void" in low:
            return "lint_void"               # 整体误判 ⇒ 看 lint 规则是否过严
        if "失败" in reason or "fail" in low:
            return "call_error"              # LLM 调用失败 ⇒ 基础设施问题，先修这个
        if "跳过" in reason or "skip" in low:
            return "skipped"
        return "unknown"

    def _finish_preselect(self, report: dict, query: str, names: List[str]) -> dict:
        """预选收口：**空手而归时不许落回裸必选层**，按域词典补核心子集。

        Args:
            names: **合法点名集（全网按需层）**——2026-10-17 目录预过滤后，调用方
                一律传 shrink 前的全集（`dom_names + cap_names`），兜底/计数都不受
                目录裁剪影响（缩的只是 LLM 能看到的目录呈现）。

        【旧系统 P0 教训】`agents/task_agent.py` 断言：域选择失效属退化，绝不能降成
        "只有通用工具"的裸沙箱（那是 2026-09-19 故障的直接机制），并配了三层兜底网。
        本系统预选空手时工具面只剩必选层，模型得靠 `search_tools` 自救一轮。

        【为什么不是整域注入】旧系统是 CodeAgent（注入函数名 + Returns 契约文本）；
        本系统 tool-calling 下整域 ≈ 15k tokens/轮，抄不起 ⇒ 只补词典核心子集。

        【克制】纯字符串匹配（零 LLM 成本）且与 lint 同源；命中不了就一个不补，
        宁可让模型走 search_tools，也不硬塞（"天气"不会命中任何域）。

        【第三层兜底 = 观测，2026-10-01】兜底也没补上时，旧系统的处置是
        `plan_domain_empty_degraded`（warning + trace）。本系统改成先**量化**：
        走这里 ≠ 一定是缺陷 —— 天气/写代码这类本来就不该给工具面，只有当
        空域占比真的抬头时才值得救。所以先计数 + 告警，判据与升级阈值写死在
        `preselect/preselect_stats.py`（含 >2% 补词典 / >5% 放宽闸门 / >10% 查 prompt）。
        ★ 日志 keyword：`preselect-empty-face`（观察期请 grep 这个而不是肉眼盯日志）。
        """
        if report.get("selected"):
            try:
                from .preselect import record_selected
                record_selected("fallback" if report.get("fallback") else "ok")
            except Exception:
                pass
            return report
        if not names:
            # 可用候选本身就是空的 ⇒ 不是预选的锅，不计入分子也不计入分母
            return report
        try:
            fb, doms = fallback_domain_tools(query, names)
        except Exception as e:
            logger.warning("[preselect] 域兜底异常(fail-open): %s", e)
            try:
                from .preselect import record_empty_face
                record_empty_face(query, "fallback_error:%s" % type(e).__name__, len(names))
            except Exception:
                pass
            return report
        if not fb:
            # 第三层兜底：不静默 —— 计数 + 可查询（见 preselect_stats.report()）
            try:
                from .preselect import record_empty_face
                _pr = record_empty_face(query, self._empty_route(report), len(names))
                report["empty_face"] = {"rate": _pr["rate"], "sample": _pr["sample"]}
            except Exception:
                pass
            return report
        act = self.activate_tools(fb, source="fallback")
        got = list(act.get("activated", [])) + list(act.get("already", []))
        if not got:
            try:
                from .preselect import record_empty_face
                record_empty_face(query, "fallback_activate_miss", len(names))
            except Exception:
                pass
            return report
        report["fallback"] = {"domains": doms, "tools": got}
        report["selected"] = got
        report["reason"] = "%s → 域兜底补 %d 个（%s）" % (
            report.get("reason") or "预选为空", len(got), ",".join(doms))
        logger.info("[preselect] 预选空手，域兜底激活 %d 个（域=%s）: %s",
                    len(got), doms, got)
        return report
