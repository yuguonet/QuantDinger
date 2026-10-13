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

from .preselect import build_catalog_grouped, build_preselect_messages, parse_plan, lint_selection, apply_lint, get_tool_index, has_domain_hint, capability_domain, fallback_domain_tools

logger = logging.getLogger(__name__)


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
        if self.config.preselect_gate and not has_domain_hint(query, self.config.preselect_hints):
            # 闸门只是「省不省那次 LLM」的成本闸，**不是**"这任务不需要工具"的判决：
            # 关键词没覆盖到的金融问法（如"帮我看看大盘"）同样需要工具面，交给收口兜底。
            report["reason"] = "无领域特征，跳过预选(闸门)"
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
        catalog, names = build_catalog_grouped(
            self._defs_by_name, dom_names, cap_names, max_desc=60)
        report["catalog_size"] = len(names)
        if not names:
            report["reason"] = "目录为空"
            return report

        max_tools = max(1, int(self.config.preselect_max_tools))
        # ── 闭环消费端（2026-10-01）：盘后产出的权重在这里影响"选哪些工具" ──
        # 权重表此前是断头路（无人读取）；本处 + 技能选择（qd_service）是它的两个
        # 消费点。提示失败一律 fail-open（空提示 = 与没有历史数据时行为一致）。
        hist_hint = ""
        try:
            from .chain.weight_hints import hint_text
            hist_hint = hint_text(names) or ""
        except Exception as e:
            logger.debug("[preselect] 权重提示跳过(fail-open): %s: %s",
                         type(e).__name__, e)
        report["weight_hint"] = hist_hint[:400]
        try:
            # 纯文本回合（不带 tools），与 mimo 压缩摘要同范式：
            # 防止模型在"选工具"这一步又去调工具，死循环。
            resp = self.model.query(build_preselect_messages(
                query, catalog, max_tools, already=self._core_names,
                history_hint=hist_hint))
            text = self._text_of(resp)
            report["raw"] = (text or "")[:500]
            plan = parse_plan(text, names, max_tools)
        except Exception as e:
            report["reason"] = f"预选调用失败(fail-open): {type(e).__name__}: {e}"
            logger.warning("[preselect] %s", report["reason"])
            return self._finish_preselect(report, query, names)

        report["goal"] = plan.get("goal", "")
        report["deliverables"] = plan.get("deliverables", [])
        selected = plan.get("tools") or []
        if not selected:
            report["reason"] = "模型未点名(可能是纯问答)"
            return self._finish_preselect(report, query, names)

        # ── 确定性校正（不调 LLM）：LLM 选得对不对由这一层判 ──────────
        # 旧系统 plan_linter 的立身之本：能被词典/索引查出来的不劳 LLM，
        # LLM 只做确定性查不了的语义匹配。没有这一层，预选质量全靠模型运气
        # （首版实测：选对省 25%，选错费 136%，三次同任务 +21%/−25%/+31%）。
        try:
            lint = lint_selection(
                query, selected,
                available_names=names,
                index=get_tool_index(self._defs_by_name),
                plan_text=" ".join([plan.get("goal", "")] + list(plan.get("deliverables") or [])),
                protected_names=(self.config.extra_core_tools or []),
                parsed=bool(plan.get("parsed")),
                capability_names=cap_names,
                domain_names=dom_names,
            )
            report["lint"] = lint.to_trace()
            report["llm_selected"] = list(selected)
            selected = apply_lint(selected, lint, capability_names=cap_names)
        except Exception as e:
            # lint 出错不阻断：原样放行（fail-open 一致性）
            logger.warning("[preselect] lint 异常(fail-open): %s", e)
            report["lint_error"] = f"{type(e).__name__}: {e}"

        if not selected:
            report["reason"] = "lint 判定整体误判，已作废(回退 search_tools)"
            return self._finish_preselect(report, query, names)
        act = self.activate_tools(selected)
        report["selected"] = list(act.get("activated", [])) + list(act.get("already", []))
        report["unknown"] = list(act.get("unknown", []))
        report["reason"] = "ok"
        return self._finish_preselect(report, query, names)

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
        act = self.activate_tools(fb)
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
