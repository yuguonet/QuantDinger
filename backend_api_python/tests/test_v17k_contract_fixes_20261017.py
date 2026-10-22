# -*- coding: utf-8 -*-
"""契约/口径根因修复守卫（2026-10-17 A 类 6 项）。

背景：`tmp/llm_io_content_analysis_run8_20261017.md` §5 整改清单第 1/5/7/8/10/11
项——都是"描述/声明/口径与实现不一致"的**根因**型问题（不是表现层症状），一律小改动：

  #1  margin 只给万元键 ⇒ 模型自己换算、换算对了却贴错单位名（「164.51 万万元」）
  #5  数字纪律 vs「续接不要重复取数」没写优先级 ⇒ 模型只能猜
  #7  get_realtime_quote 描述承诺换手率/量比/PE/PB/总市值，实际一个都不返回
  #8  系统提示硬编码「原生工具清单」把 7 个**按需层**工具说成常驻（T2 之后的陈旧事实）
  #10 技能 frontmatter 把**子进程导出函数**当工具声明（strategy-debug 三个死名）
  #11 领域命中率提示无视本轮问什么，把所有低命中域倒给每一轮

本文件锁死"不得再漂回去"，外加 #7/#1 的字段级断言（对着实现验，不是对着文案验）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
QD_SYSTEM = BACKEND / "app" / "agent" / "prompts" / "qd_system.txt"
QD_CORE_BASE = BACKEND / "app" / "agent" / "prompts" / "qd_core_base.txt"
DATA_TOOLS = BACKEND / "app" / "agent" / "tools" / "finance" / "data_tools.py"


@pytest.fixture(scope="module")
def _registry_names() -> set:
    """"能被调到的工具名"全集 = agent 注册表 ∪ provider ∪ **能力层准入清单**。

    ★ 为什么要三个来源（试过两种都不可靠）：
      ① agent 注册表 —— mimo 原生工具（bash/compact/format_result…）只在这里；
         但它**依赖进程状态**：全量套件里前序用例动过能力层时，能力层工具
         （`all_codes` 等）就不在里面 ⇒ 单跑绿、全量红（本次真实踩到两次）。
      ② provider.list_tools() —— 域工具齐全（880 项），但不含原生工具。
      ③ `capabilities.load_admitted()` —— 能力层**准入清单**（JSON 真源），
         与注册时序无关，是"能力层有哪些名字"的权威。
      只有"三个来源都没有"的名字才算死名 ⇒ 守卫不会因执行顺序误报。
    """
    from mimoagent.environments import get_environment
    from app.agent.qd_agent import QDAgent
    from app.agent.tools._base import ToolProvider

    a = QDAgent(object(), get_environment({"environment_class": "local"}),
                trace_enabled=False)
    names = set(a._defs_by_name) | set(ToolProvider.get_or_build().list_tools())
    from app.agent.capabilities import load_admitted
    names |= {str(name) for (_mod, name, *_rest) in (load_admitted() or [])}
    return names


# ── #10 技能声明名必须落在注册表里（执行型技能的例外只有 run_skill）────────

def test_skill_declared_tools_exist_in_registry(_registry_names):
    """技能 frontmatter `tools:` ⊆ 注册表 ∪ {run_skill}。

    为什么放行 `run_skill`：执行型技能（`EXECUTABLE_SKILLS`：market-screener /
    strategy-debug / auto-backtest）的函数是**子进程导出**，agent 侧唯一入口是
    `run_skill` 派活。反之，若声明的是 run.py 里的函数名（如 why_strategy），
    预选目录（2026-10-17 起真的进 LLM 输入）就会教模型直调不存在的工具 ⇒ 幻觉。
    """
    from app.agent.skills_adapter import QDSkillAdapter

    dead = {}
    for info in QDSkillAdapter().list_skills():
        miss = [t for t in (info.get("tools") or []) if t not in _registry_names]
        if miss:
            dead[info["name"]] = miss
    assert not dead, ("技能声明了注册表里不存在的工具（预选目录会教模型直调幻觉）："
                      + str(dead))


# ── #7 描述不得承诺实现不返回的字段 ───────────────────────────────────────

def test_realtime_quote_schema_promises_only_real_fields():
    """对着**发给模型的 schema 描述**验（不是对着源码注释）——描述才是契约。"""
    from app.agent.tools.finance.data_tools import get_realtime_quote
    from app.agent.tools._base import func_to_openai_schema

    desc = (func_to_openai_schema(get_realtime_quote).get("function") or {}).get("description") or ""
    head = desc.split("⚠")[0]          # ⚠ 之后是"实情说明"，允许提到那些词
    for phantom in ("换手率", "量比", "PE", "PB", "总市值"):
        assert phantom not in head, (
            f"描述首段仍承诺「{phantom}」，但实现不返回（旧承诺 2026-10-17 已移除）")
    assert "get_stock_info" in desc, "撤掉承诺必须同时给出路（要这口径去哪个工具）"
    # 指路必须指对：量比在 technical_analysis（量价维聚合），不在 get_order_book
    # （修 A 类时一度指错——指错=制造新幻觉，性质与原 bug 同级）
    assert "technical_analysis" in desc and "get_order_book（量比）" not in desc


def test_order_book_schema_promises_only_real_fields():
    """`get_order_book` 同型过承诺（首行承诺换手率/PE/市值，返回只有 bid/ask）。"""
    from app.agent.tools.finance.data_tools import get_order_book
    from app.agent.tools._base import func_to_openai_schema

    desc = (func_to_openai_schema(get_order_book).get("function") or {}).get("description") or ""
    head = desc.split("⚠")[0]
    for phantom in ("换手率", "PE", "市值"):
        assert phantom not in head, f"get_order_book 描述首段仍承诺「{phantom}」"
    assert "get_stock_info" in desc


def test_realtime_quote_docstring_lists_returned_fields():
    """Returns 段列出的字段必须与实现的出口字段一致（防再次漂回模糊描述）。"""
    doc = DATA_TOOLS.read_text(encoding="utf-8")
    seg = doc.split("def get_realtime_quote(")[1].split("def agent_get_kline(")[0]
    for real in ("last", "changePercent", "volume", "volume_unit",
                 "limit_up", "limit_down"):
        assert real in seg, f"Returns 段漏了实际返回字段 {real}"


# ── #1 margin 双单位（键名即口径，亿元键给模型照抄）────────────────────────

def test_margin_returns_both_units(monkeypatch):
    """融资/融券余额同时给万元与亿元键——换算由工具做，模型只贴键名。"""
    from app.agent.tools.finance import _em_utils
    from app.agent.tools.finance.data_tools import _get_margin_trading

    rows = [{"RZYE": 16_450_780_103.0, "RQYE": 274_000_000.0} for _ in range(8)]
    monkeypatch.setattr(_em_utils, "em_datacenter", lambda *a, **k: rows)
    got = _get_margin_trading("600519")
    assert got["rz_balance_wan"] == 1_645_078.0
    assert got["rz_balance_yi"] == 164.51, "亿元键 = 模型直接引用的口径（run8 幻觉根）"
    assert got["rq_balance_yi"] == pytest.approx(2.74, abs=0.01)
    # 键名自带单位 ⇒ 不需要再挂 unit 键（仓内约定：_wan/_yi 后缀即口径）


# ── #8 系统提示不得硬编码工具名单（分层部署下必然漂）───────────────────────

def test_core_base_has_no_hardcoded_native_tool_list():
    """`qd_core_base.txt` 不得再列"原生工具清单"。

    T2 把 read/write/edit/glob/grep/task/actor 全部下沉按需层后，这份清单
    仍把它们当常驻讲 ⇒ 与同文件的「工具地图（分层）」自相矛盾（run8 §1.2，
    且当时只抓到 actor 一处，实际 read/write/edit/glob/grep/task 同样已陈旧）。
    权威应是本轮实际暴露的工具面 + 工具地图，故此处要求指向而非列举。
    """
    txt = QD_CORE_BASE.read_text(encoding="utf-8")
    assert "不要凭这份记忆列工具名单" in txt, "工具清单节必须改成指向权威来源"
    for stale in ("（原生小写工具）", "可选委派：`actor`（仅同步"):
        assert stale not in txt, f"工具清单仍在硬编码名单：{stale}"
    # 只剩 bash/compact/format_result 属常驻；其余原生工具都在按需层
    assert "工具地图" in txt, "必须指向分层说明（工具地图）而不是自带名单"


# ── #5 数字纪律与「续接不要重复取数」必须写清优先级 ───────────────────────

def test_numeric_discipline_declares_precedence():
    """两条规则争同一件事（能否用历史数字）⇒ 必须显式定优先级，否则模型只能猜。

    注意断言方向：**不得**给历史数字开口子（那正是 run8「前高 1363.35 用会话
    记忆未复核」的病根），正确口径是"可引用但须标来源与时点 + 关键锚点本轮复核"。
    """
    txt = QD_SYSTEM.read_text(encoding="utf-8")
    seg = txt.split("## 数字纪律")[1].split("##")[0]
    assert "优先级" in seg, "数字纪律节必须写明与『续接不要重复取数』的优先级"
    assert "关键锚点" in seg and "复核" in seg, "价格/前高前低等锚点必须要求本轮复核"


# ── #11 领域命中率提示必须按本轮 query 过滤 ───────────────────────────────

def test_hint_filters_domains_by_query(monkeypatch):
    """给了 query 时只提与本轮相关的域；不给 query 保持旧行为（老调用方零改动）。

    run8 §2.6 实测：一轮普通个股分析也收到「finance 命中率偏低」——与本轮
    无关，白占 token 还暗示"你的结论不可靠"。
    """
    from app.agent.chain import weight_hints as wh

    snap = {"domain": {"screen": 0.30, "fundflow": 0.85}, "skill": {},
            "tool": {}, "chain": {}, "ts": 0}
    monkeypatch.setattr(wh, "snapshot", lambda *a, **k: dict(snap))
    monkeypatch.setattr(wh, "low_weight", lambda *a, **k: [])

    # 本轮问选股（detect_domains → screen）⇒ 只应看到 screen，且看不到无关的 fundflow
    scoped = wh.hint_text([], query="帮我选几只强势涨停的股票")
    assert "screen" in scoped, "本轮相关域必须出现"
    assert "fundflow" not in scoped, "与本轮无关的域不得出现（run8 §2.6 的病根）"
    # 本轮问资金流/行情 ⇒ 看得到 fundflow（高命中），看不到 screen
    fund = wh.hint_text([], query="分析600519 的资金流和行情")
    assert "fundflow" in fund and "screen" not in fund
    # 本轮一个域都不命中（天气）⇒ 域提示整段不出现
    assert "命中率" not in wh.hint_text([], query="今天北京天气怎么样")
    # 老调用形态（不传 query）⇒ 全列旧行为
    legacy = wh.hint_text([])
    assert "screen" in legacy and "fundflow" in legacy, \
        "不传 query 时必须保持全列（兼容 + fail-open）"


# ── #F9 system 每会话只 seed 一次（冻结）⇒ now/tool_map 必须离开 system_template ──
#  mimo agents/base.py:213 只在「会话第一条消息」seed system，之后跨轮复用；
#  now（秒级）与 tool_map（含 [已激活·本轮…]，依赖每轮 _activated）都是 per-turn，
#  留在 system 里就会自 turn2 起永久陈旧。改为前置到本轮 task（[当前时间]/[工具地图]）。
#  tool_availability 是进程内不变量，仍留 system（仅首轮算一次）。

def test_system_prompt_has_no_per_turn_vars():
    txt = QD_SYSTEM.read_text(encoding="utf-8")
    assert "{{ now }}" not in txt, "now 是 per-turn，必须离开（会话冻结）的 system_template"
    assert "{{ tool_map }}" not in txt, "tool_map 依赖每轮激活面，必须离开 system_template"
    assert "{{ tool_availability }}" in txt, "进程内不变量仍留 system（只渲染一次）"
    assert "[当前时间]" in txt and "[工具地图]" in txt, "必须留指向行，引导模型看 task 头部"


def _make_face():
    from mimoagent.environments import get_environment
    from app.agent.qd_agent import QDAgent
    return QDAgent(object(), get_environment({"environment_class": "local"}),
                   trace_enabled=False)


def test_dynamic_context_prefixes_task(monkeypatch):
    """【契约变更·2026-10-19 登记】动态块**默认拼在任务末尾**，不再前置。

    意图不变：① 时间行与工具地图**每轮都进本轮 task**（不回退到 system 冻结那套）；
    ② 原 task 内容原样保留。变的只是**拼接位置**——秒级时间戳 + 每轮重建的工具
    地图放在开头会把整条 prompt 前缀逐轮打断，前缀缓存命中率实测仅 1~3%
    （详见 `qd_tool_face._dynamic_context_text` 的 docstring）。
    故本用例改为「默认末尾 + `QD_DYNAMIC_CTX_TAIL=0` 时仍前置」双向断言，
    保证回退旋钮**真的能回去**，不是只留个 env 名字。
    """
    face = _make_face()
    monkeypatch.setattr(type(face), "_tool_map_text", lambda self: "MAP")

    # 默认：动态块在末尾 ⇒ task 原文在前（前缀跨轮稳定，缓存可命中）
    out = face._dynamic_context_text("分析600519")
    assert out.startswith("分析600519"), "默认: 原 task 必须在最前（动态块让位到末尾）"
    assert "[工具地图]\nMAP" in out, "工具地图块必须进本轮 task（每轮重建）"
    assert "[当前时间] " in out, "时间行必须进本轮 task"

    monkeypatch.setattr(type(face), "_tool_map_text", lambda self: "")
    out2 = face._dynamic_context_text("hi")
    assert out2.startswith("hi") and "[工具地图]" not in out2 and "[当前时间] " in out2

    # 回退旋钮：QD_DYNAMIC_CTX_TAIL=0 ⇒ 逐位回到改造前（动态块前置）
    monkeypatch.setenv("QD_DYNAMIC_CTX_TAIL", "0")
    monkeypatch.setattr(type(face), "_tool_map_text", lambda self: "MAP")
    out3 = face._dynamic_context_text("分析600519")
    assert out3.startswith("[当前时间] "), "QD_DYNAMIC_CTX_TAIL=0: 时间行必须前置到 task 开头"
    assert "[工具地图]\nMAP" in out3, "回退态: 工具地图块仍必须进本轮 task"
    assert out3.endswith("分析600519"), "回退态: 原 task 内容必须原样保留在尾部"


def test_dynamic_context_computes_availability_only_first_run(monkeypatch):
    face = _make_face()
    calls = []
    monkeypatch.setattr(type(face), "_tool_availability_text",
                        lambda self: calls.append(1) or "AV")
    monkeypatch.setattr(type(face), "_tool_map_text", lambda self: "")
    face.messages = []
    face._dynamic_context_text("a")
    assert face.extra_template_vars.get("tool_availability") == "AV"
    assert len(calls) == 1, "首轮必须算一次（system 此刻才 seed）"
    face.messages = [{"role": "user", "content": "x"}]
    face._dynamic_context_text("b")
    assert len(calls) == 1, "非首轮不得重算（system 已冻结，算了也不生效）"


def test_dynamic_context_passthrough_non_str_task():
    face = _make_face()
    d = {"task": "x"}
    assert face._dynamic_context_text(d) is d, "非字符串 task 必须原样透传（不破坏结构化输入）"


def test_run_inner_wires_dynamic_context():
    src = (BACKEND / "app" / "agent" / "qd_agent.py").read_text(encoding="utf-8")
    assert "_dynamic_context_text(task)" in src, "_run_inner 必须调用 dynamic context 前置"
    assert 'extra_template_vars["now"]' not in src, "now 不得再写进 system 模板变量"
    assert 'extra_template_vars["tool_map"]' not in src, "tool_map 不得再写进 system 模板变量"
