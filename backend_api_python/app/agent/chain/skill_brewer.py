# -*- coding: utf-8 -*-
"""chain/skill_brewer.py — 技能酿造器（2026-09-18，用户方案）

思路：高频且回测验证效果好的节点树（qd_traces），周期性由 LLM 编译成
SKILL.md（auto_ 前缀目录），下次同类问题 planner 优先技能调用——
"用数据库代替 Hermes 的 Skill 固化，回测 = 给编排 Skill 打分"。

与 path_cache/模板短路路线的取舍：本路线复用已接线的 skill 全链
（planner 语义选择 → SKILL.md body 注入 → skill_tools 白名单 →
qd_agent_weights layer='skill' 打分），无需键匹配与短路逻辑。

提示词：prompts/skill_brew.txt（领域无关——技能机制是通用机制，酿造器
不假设轨迹所属领域；签名清单/低权重规避经占位符注入）。

易错点：
  - 生成的 SKILL.md 必须符合 market_screener 样例的 frontmatter 契约
    （name/version/description/tags/tools），否则 _scan_markdown 解析失败静默丢弃；
  - tools 白名单真实性：LLM 从日志推断工具名可能编造——写盘前与 provider
    注册表求交集剔除（不存在的名字 = 执行期必踩坑）；
  - 幂等：chain 已有 auto_ skill 则跳过（避免重复酿造）；人工编辑过的 auto_ 技能
    （文件含 "human-edited" 标记）永不覆盖；
  - LLM 输出可能裹 markdown 代码围栏，需剥离后再写盘；
  - QDLLM.generate 只收 ChatMessage 对象（dict 无 to_dict）；事件循环内调用
    asyncio.run 会抛——本模块仅同步上下文使用。
"""
from __future__ import annotations

import logging
import re
import shutil
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "skill_brew.txt"
_AUTO_PREFIX = "auto_"
_HUMAN_EDITED_MARK = "human-edited"
_BREW_TEMPLATE: Optional[str] = None


def _load_brew_template() -> str:
    """读取酿造提示词模板（模块级缓存）。缺失时抛错——模板是酿造的必要输入。"""
    global _BREW_TEMPLATE
    if _BREW_TEMPLATE is None:
        _BREW_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")
    return _BREW_TEMPLATE


def _strip_code_fence(text: str) -> str:
    """剥离 LLM 输出可能包裹的 markdown 代码围栏。"""
    m = re.search(r"```(?:markdown|md|yaml)?\s*\n(.*?)\n```\s*$", text.strip(), re.S)
    return m.group(1) if m else text.strip()


def _extract_frontmatter_tools(skmd: str) -> list:
    """从 SKILL.md frontmatter 提取 tools 列表（用于真实性校验）。"""
    m = re.search(r"^tools:\s*\[([^\]]*)\]", skmd, re.M)
    if not m:
        return []
    return [t.strip().strip("'\"") for t in m.group(1).split(",") if t.strip()]


# 元工具/沙箱内置：始终在场，写进 skill tools 白名单只是噪音（LLM 从日志推断时
# 常把 list_tools/search_tools 混进来——它们是"查工具的工具"，不是数据能力）。
_META_TOOLS = {"list_tools", "search_tools", "format_result", "final_answer",
               "python_interpreter", "web_search"}


def _validate_tools(skmd: str, provider) -> str:
    """tools 白名单真实性校验：剔除编造名 + 元工具，并告警。

    LLM 从 Execution logs 推断工具名可能编造；写死不存在的名字 = 执行期必踩坑。
    """
    if provider is None:
        return skmd
    valid = set(provider.get_tool_names()) - _META_TOOLS
    tools = _extract_frontmatter_tools(skmd)
    bad = [t for t in tools if t and (t not in valid or t in _META_TOOLS)]
    if not bad:
        return skmd
    kept = [t for t in tools if t not in bad]
    fixed = re.sub(
        r"^(tools:\s*)\[[^\]]*\]$",
        lambda m: m.group(1) + "[" + ", ".join(kept) + "]",
        skmd, flags=re.M,
    )
    logger.warning("[Brewer] 剔除编造的工具名（不在 provider 注册表）: %s", bad)
    return fixed


def _skill_dir_exists_for(chain_name: str) -> Optional[str]:
    """幂等检查：该 chain 已有 auto_ skill（含人工编辑标记识别）→ 返回目录名。"""
    if not _SKILLS_DIR.exists():
        return None
    for d in sorted(_SKILLS_DIR.glob(f"{_AUTO_PREFIX}*")):
        md = d / "SKILL.md"
        if not md.exists():
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        if chain_name in text:
            logger.info("[Brewer] chain=%s 已有 %s（人工编辑=%s），跳过",
                        chain_name, d.name, _HUMAN_EDITED_MARK in text)
            return d.name
    return None


def brew_skills(llm=None, min_runs: int = 5, limit: int = 3, trigger: str = "auto") -> list:
    """酿造主入口：筛候选 → 逐链 LLM 编译 → 写 skills/auto_*/SKILL.md。

    触发分派（重设计 §2.5，2026-09-19）：
      trigger="auto"   → 混合三通道：信号就绪（query_brew_ready）立即酿；
                         兜底（brew_state 距上次尝试 ≥7 天且 runs 达标）；
                         冷却（连续失败 ≥3 → 静默）
      trigger="manual" → 原 query_brew_candidates 全量门槛（手动入口语义不变）

    Args:
        llm: LLMBase 实例；None 时自动创建。仅同步上下文调用。
        min_runs: 酿造候选的最小 run 数。
        limit: 单轮最多酿造几个技能。
        trigger: auto=混合信号触发；manual=手动全量门槛。

    Returns:
        酿造结果列表 [{"chain_name", "status", ...}]，失败项带 error。
    """
    from chain.store import (query_brew_candidates, query_brew_ready,
                             get_run_tree_digest, get_tool_weights,
                             get_brew_states, set_brew_state)

    _today = date.today()
    states = {s["chain_name"]: s for s in get_brew_states()} if trigger == "auto" else {}

    if trigger == "auto":
        # 通道 1：信号就绪（无视 last_brew_date——信号达标即酿）
        ready = query_brew_ready(limit=limit)
        # 通道 2：兜底（距上次尝试 ≥ BREW_FALLBACK_DAYS）；通道 3：冷却过滤
        fallback = query_brew_candidates(min_runs=min_runs, limit=limit)
        candidates, seen = [], set()
        for c in ready:
            name = c["chain_name"]
            s = states.get(name, {})
            if s.get("fail_streak", 0) >= 3:
                logger.info("[Brewer] %s 冷却中（连续失败 %d），跳过", name, s["fail_streak"])
                continue
            if name not in seen:
                c["_channel"] = "signal"
                candidates.append(c)
                seen.add(name)
        for c in fallback:
            name = c["chain_name"]
            if name in seen:
                continue
            s = states.get(name, {})
            if s.get("fail_streak", 0) >= 3:
                continue
            last = s.get("last_brew_date")
            if last and (_today - last).days < 7:
                continue
            c["_channel"] = "fallback"
            candidates.append(c)
            seen.add(name)
        candidates = candidates[:limit]
    else:
        candidates = query_brew_candidates(min_runs=min_runs, limit=limit)

    if not candidates:
        logger.info("[Brewer] 无酿造候选（trigger=%s）", trigger)
        return []

    if llm is None:
        # 优先复用主 agent 的 LLM 实例（provider/model 与 .env 一致）；create_llm()
        # 的 auto 模式解析链不同（曾出现 fallback 到不存在的模型 → 400）。
        try:
            import agent as agent_mod
            llm = agent_mod.llm
        except Exception:
            from llm.factory import create_llm
            llm = create_llm()

    # 工具质量上下文（领域无关：签名清单=全 provider；低权重名单=回测统计）
    provider = None
    signatures = "（不可用）"
    low_tools = "（无数据）"
    try:
        from tools.base import ToolProvider
        provider = ToolProvider.get_default()
        if provider is None:
            # 手动酿造入口没经过 plan_node 的 init_tools——自建一份用于校验/签名
            from pathlib import Path as _P
            provider = ToolProvider()
            _td = _P(__file__).resolve().parent.parent / "tools"
            provider.scan_directory(_td, domain="common", package_prefix="tools")
            provider.scan_subdirectories(_td, package_prefix="tools")
        if provider:
            from utils.prescan import prescan_tools
            signatures = prescan_tools(provider, limit=40, query="")
        from chain.store import get_tool_weights as _gtw
        weights = _gtw()
        # [AUDIT-MASK:B3/E1|2026-09-19] 阈值 0.7 与 task_agent.py plan 提示处硬编码重复；
        # 「工具差」三源口径不同（熔断=连续失败/权重=回测胜率/此处=低胜率名单）。
        # 统一清理阶段抽公共 helper + 阈值常量化。
        low = sorted(n for n, w in weights.items() if w < 0.7)
        low_tools = ", ".join(low[:15]) if low else "（暂无低权重工具）"
    except Exception as e:
        logger.warning("[Brewer] 工具质量上下文获取失败（继续酿造）: %s", e)

    template = _load_brew_template()
    results = []
    for cand in candidates:
        chain_name = cand["chain_name"]
        existing = _skill_dir_exists_for(chain_name)
        if existing:
            results.append({"chain_name": chain_name, "status": "exists", "skill_dir": existing})
            continue

        digest = None
        try:
            digest = get_run_tree_digest(cand["sample_root_id"])
        except Exception as e:
            logger.warning("[Brewer] 轨迹摘要失败 chain=%s: %s", chain_name, e)
        if not digest:
            results.append({"chain_name": chain_name, "status": "no_digest"})
            continue

        skill_name = _AUTO_PREFIX + re.sub(r"[^a-z0-9]+", "-", chain_name.lower()).strip("-")
        steps_txt = "\n".join(
            f"  - [{s['status']}] {s['step']}: {s['log']}" for s in digest.get("steps", []))
        prompt = template.format(
            chain_name=chain_name,
            user_query=digest.get("user_query", ""),
            plan=digest.get("plan", "") or "（无）",
            signatures=signatures,
            low_weight_tools=low_tools,
            steps=steps_txt or "（无步骤记录）",
            skill_name=skill_name,
        )
        try:
            import asyncio
            from llm.base import ChatMessage
            # 仅同步上下文调用（eval worker 线程 / 手动入口）；事件循环内调用会抛
            # RuntimeError → 由 except 捕获记 llm_error，不影响主流程。
            resp = asyncio.run(llm.generate(messages=[
                ChatMessage(role="system", content="你是技能工程师，只输出 SKILL.md 文件内容。"),
                ChatMessage(role="user", content=prompt),
            ]))
            skmd = _strip_code_fence(resp.content or "")
        except Exception as e:
            logger.warning("[Brewer] LLM 编译失败 chain=%s: %s", chain_name, e)
            results.append({"chain_name": chain_name, "status": "llm_error", "error": str(e)})
            continue

        if "name:" not in skmd or "description:" not in skmd:
            results.append({"chain_name": chain_name, "status": "bad_format"})
            continue

        # 目录名由代码决定（不信任 LLM 输出的 name——它可能改前缀/连字符风格）；
        # frontmatter 里的 name 仅作展示，_scan_markdown 的 display 映射会兜住。
        final_name = skill_name
        skmd = _validate_tools(skmd, provider)
        skill_dir = _SKILLS_DIR / final_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        header = (f"<!-- auto-brewed {date.today().isoformat()} from root_id="
                  f"{cand['sample_root_id']} chain={chain_name} | "
                  f"{_HUMAN_EDITED_MARK} 后请去除 auto_ 前缀接管 -->\n")
        (skill_dir / "SKILL.md").write_text(header + skmd + "\n", encoding="utf-8")
        logger.info("[Brewer] 酿成技能: %s (chain=%s)", final_name, chain_name)
        results.append({"chain_name": chain_name, "status": "brewed", "skill_dir": final_name})
        set_brew_state(chain_name, last_brew_date=_today, fail_streak=0)

    # 失败/跳过链也记尝试日期（兜底节奏由 brew_state 驱动；失败连续计数驱动冷却）
    for r in results:
        if r["status"] not in ("brewed", "exists"):
            set_brew_state(r["chain_name"], last_brew_date=_today,
                           fail_streak=(states.get(r["chain_name"], {}).get("fail_streak", 0) + 1))
    return results


# ═══════════════════════════════════════════════════════════════
#  auto_ 技能迭代升级（重设计 §2.6，2026-09-19）
#  原则（D6）：全链复用——用户消息 → planner 选 auto_skill → 执行入库 → T+N 回测
#  → 追责打分，整条链不改；本节只做"打分后旁支"：差技能 → 修订而非只降权。
# ═══════════════════════════════════════════════════════════════

_REVISE_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "skill_revise.txt"
LOW_WEIGHT_THRESHOLD = 0.7       # 与 planner 低权重告警同阈值（原 B3 硬编码收敛）
REVISE_MAX_STREAK = 2            # 连续修订 N 轮仍差 → 转淘汰（护栏 5）


def _load_revise_template() -> str:
    if not _REVISE_PROMPT_PATH.exists():
        # 修订模板缺失时退化为酿造模板（修订提示 = 原料里带旧文档 + 新轨迹）
        return _load_brew_template()
    return _REVISE_PROMPT_PATH.read_text(encoding="utf-8")


def _bump_version(version: str) -> str:
    """0.1.0 → 0.2.0（minor 递增；major 留给人工重构）。"""
    try:
        a, b, _c = (int(x) for x in version.split("."))
        return f"{a}.{b + 1}.0"
    except Exception:
        return "0.2.0"


def revise_skill(chain_name: str, skill_dir: str, llm=None) -> dict:
    """修订单个 auto_ 技能（§2.6 旁支主体）。

    护栏（§2.6 五条）：
      2. 权重重置：修订完成后 weight=1.0、sample_count=0（新版本重新积累评价）
      3. .bak 单版本回滚：修订前把旧 SKILL.md 复制为 SKILL.md.bak
      4. human-edited（已去 auto_ 前缀）技能不在本函数射程（调用方过滤）
    原料（护栏 1）：旧 SKILL.md 全文 + origin root_id 之后的增量轨迹
    （correct=TRUE 教改进，FALSE 供坑）；修订是 LLM 对旧文档的增量更新，
    不是从零重编译。
    """
    from chain.store import get_delta_digest, get_skill_revision, set_skill_revision, set_brew_state

    skill_path = Path(skill_dir) / "SKILL.md"
    if not skill_path.exists():
        return {"chain_name": chain_name, "status": "no_skill"}

    old_doc = skill_path.read_text(encoding="utf-8", errors="replace")

    # origin root_id 从 header 注释解析（`from root_id=NNN`）
    m = re.search(r"from root_id=(\d+)", old_doc)
    since_id = int(m.group(1)) if m else None

    digest = get_delta_digest(chain_name, since_date=None)
    if not digest or (digest["n_correct"] + digest["n_falsified"]) == 0:
        return {"chain_name": chain_name, "status": "no_delta"}

    rev = get_skill_revision(chain_name)

    if llm is None:
        try:
            import agent as agent_mod
            llm = agent_mod.llm
        except Exception:
            from llm.factory import create_llm
            llm = create_llm()

    runs_txt = "\n".join(
        f"  - [{r['exec_date']}] {'✓正确' if r['correct'] is True else ('✗错误' if r['correct'] is False else '未验证')}"
        f" 任务:{r['user_query'][:60]} 摘要:{r['summary'][:120]}"
        for r in digest["runs"])
    prompt = (
        "你是技能工程师。以下是同一个 auto_ 技能的当前文档与本链路最新执行轨迹。"
        "请修订文档：从成功轨迹提炼步骤/参数改进，把失败轨迹中的坑写进「注意事项」；"
        "不要推翻文档结构，不要删除已有但未在轨迹中出现的条目；只更新有依据的部分。\n\n"
        f"## 当前文档\n{old_doc}\n\n"
        f"## 增量轨迹（本链 {digest['chain_name']}，"
        f"正确 {digest['n_correct']} 条 / 证伪 {digest['n_falsified']} 条）\n{runs_txt}\n\n"
        "只输出修订后的完整 SKILL.md（frontmatter + 正文），version 的 minor 位 +1。"
    )
    try:
        import asyncio
        from llm.base import ChatMessage
        resp = asyncio.run(llm.generate(messages=[
            ChatMessage(role="system", content="你是技能工程师，只输出修订后的 SKILL.md 文件内容。"),
            ChatMessage(role="user", content=prompt),
        ]))
        new_doc = _strip_code_fence(resp.content or "")
    except Exception as e:
        logger.warning("[Brewer] 修订 LLM 失败 chain=%s: %s", chain_name, e)
        return {"chain_name": chain_name, "status": "llm_error", "error": str(e)}

    if "name:" not in new_doc or "description:" not in new_doc:
        return {"chain_name": chain_name, "status": "bad_format"}

    # tools 白名单真实性校验（与首酿同规则）
    try:
        from tools.base import ToolProvider
        provider = ToolProvider.get_default()
        if provider is None:
            provider = ToolProvider()
            from pathlib import Path as _P
            _td = _P(__file__).resolve().parent.parent / "tools"
            provider.scan_directory(_td, domain="common", package_prefix="tools")
            provider.scan_subdirectories(_td, package_prefix="tools")
        new_doc = _validate_tools(new_doc, provider)
    except Exception as e:
        logger.debug("[Brewer] 修订版工具校验跳过: %s", e)

    # 版本递增（优先用 LLM 输出的 version；没给就本地 bump）
    m_old = re.search(r"^version:\s*([\d.]+)", old_doc, re.M)
    new_version = _bump_version(m_old.group(1)) if m_old else "0.2.0"
    new_doc = re.sub(r"^(version:\s*)[\d.]+$", lambda mm: "version: " + new_version,
                     new_doc, count=1, flags=re.M)

    # 护栏 3：.bak 单版本回滚
    shutil.copyfile(skill_path, skill_path.with_suffix(".md.bak"))

    # 修订头注释：保留溯源，追加 revision 序号
    m_rev = re.search(r"from root_id=(\d+)", old_doc)
    origin = m_rev.group(1) if m_rev else "?"
    header = (f"<!-- auto-brewed&revised {date.today().isoformat()} origin_root_id={origin} "
              f"chain={chain_name} | revision={rev['revision'] + 1} | "
              f"{_HUMAN_EDITED_MARK} 后请去除 auto_ 前缀接管 -->\n")
    skill_path.write_text(header + new_doc + "\n", encoding="utf-8")

    # 护栏 2：权重重置（新版本重新积累评价）
    set_brew_state(chain_name, last_brew_date=date.today(), fail_streak=0)
    set_skill_revision(chain_name, revision=rev["revision"] + 1, low_streak=0)

    logger.info("[Brewer] 技能已修订: %s → v%s (revision=%d)",
                skill_dir, new_version, rev["revision"] + 1)
    return {"chain_name": chain_name, "status": "revised",
            "skill_dir": skill_dir, "version": new_version}


def maybe_revise(skill_weights: dict, skill_adapter=None, llm=None) -> list:
    """update_weights 收口调度入口（evaluator 只调本函数，一行）。

    筛选：layer='skill' 权重中 auto_ 前缀且 weight < LOW_WEIGHT_THRESHOLD；
    护栏 4：human-edited（无 auto_ 前缀）不修；护栏 5：low_streak ≥ REVISE_MAX_STREAK 跳过。
    """
    from chain.store import set_skill_revision as _ssr
    results = []
    low = sorted((n, w) for n, w in skill_weights.items()
                 if n.startswith(_AUTO_PREFIX) and w < LOW_WEIGHT_THRESHOLD)
    if not low:
        return results
    for name, w in low:
        chain_name = name[len(_AUTO_PREFIX):].replace("-", "+")
        # 反推 chain 原名不可靠（连字符双向），改由 header 注释读真实 chain
        skill_dir = _SKILLS_DIR / name
        if not skill_dir.exists():
            continue
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        m = re.search(r"chain=([^\s|]+)", text)
        real_chain = m.group(1) if m else None
        if not real_chain:
            continue
        from chain.store import get_skill_revision
        rev = get_skill_revision(real_chain)
        if rev["low_streak"] >= REVISE_MAX_STREAK:
            logger.info("[Brewer] %s 已连续 %d 轮修订仍低权重 → 转淘汰/人工接管",
                        name, rev["low_streak"])
            results.append({"chain_name": real_chain, "status": "give_up",
                            "skill_dir": name, "weight": w})
            continue
        r = revise_skill(real_chain, str(skill_dir), llm=llm)
        results.append(r)
        if r["status"] == "revised":
            _ssr(real_chain, revision=r.get("revision", 0), low_streak=0)
        elif r["status"] in ("llm_error", "bad_format", "no_delta"):
            _ssr(real_chain, revision=rev["revision"], low_streak=rev["low_streak"] + 1)
    return results


def refresh_skill_adapter() -> None:
    """酿造后刷新运行中的 skill_adapter 缓存（长驻进程生效；cli 下次启动自然生效）。"""
    try:
        from llm.qd_skills import QDSkillAdapter
        import agent as agent_mod          # 裸包名（app/agent 在 sys.path），全局单例
        adapter = getattr(agent_mod.agent, "skill_adapter", None)
        if isinstance(adapter, QDSkillAdapter):
            fresh = QDSkillAdapter(skills_dirs=list(adapter._default_dirs()))
            agent_mod.agent.skill_adapter = fresh
            logger.info("[Brewer] skill_adapter 已刷新: %d 个技能", len(fresh))
    except Exception as e:
        logger.warning("[Brewer] skill_adapter 刷新失败（下次启动生效）: %s", e)


if __name__ == "__main__":
    # 手动酿造入口（同步上下文）：
    #   <python> app/agent/chain/skill_brewer.py [min_runs]
    import sys
    _bp = Path(__file__).resolve().parents[3]
    sys.path.insert(0, _bp)
    sys.path.insert(0, os.path.join(_bp, "app", "agent"))
    from dotenv import load_dotenv
    load_dotenv(_bp / ".env", override=False)   # 否则 DATABASE_URL 缺失 → 候选查询空转
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    _mr = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 5
    _res = brew_skills(min_runs=_mr)
    for _r in _res:
        print(_r)
    refresh_skill_adapter()