# -*- coding: utf-8 -*-
"""tests/test_wiring.py — agent「接线契约」回归测试（2026-09-15 重建版）

由来：
  旧版（2026-09-14，21 项）覆盖的是"暂存区文件读写三件套 + import 白名单"时代的契约。
  2026-09-15 两级统一后 stage_write/read/list 已删除（交接 = executor.state 里的
  Python 变量），import 白名单/沙箱拦截也已作废（additional_authorized_imports=["*"]），
  旧断言约半数测的是已不存在的东西，且收集期即 ImportError（from infra.staging import
  stage_read），用户裁定删除。本文件按**当前架构**重建回归网。

覆盖的契约（每项对应一次真实事故或一次架构决策）：
  W1  能力层是来源层，不进可选域（L1 病灶：quant vs finance 互斥二选一）
  W2  阶段契约规范化：白名单过滤 / on_fail 收敛 / 预算钳制 / internal_plan 透传
  W3  规划提示必须保留单段点名通道示例（L9：示例/规则/实现三者不得互斥）
  W4  工具注入面：白名单独占 / 附加点名并集 / 无点名回退域基调 / 名字归一化
  W5  install_tools 单入口：custom_tools / state / static_tools 三处同步，
      每次 __call__ 前重装（阶段重试清空沙箱后工具必须仍在）
  W6  工具结果变量续承：_wrap_stage_guard 原样返回数据 + 登记 `_r_<工具名>`
  W7  会话级变量：stage_put_obj / stage_scope_vars / stage_clear 闭环 + TTL 清理
  W8  _auto_stage_phase_result：阶段结果注册为会话级变量（变量名合法标识符）
  W9  final_answer_checks 已接线：空答案 / 半成品 <code> 拒收
  W10 时间实体解析：交易日口径 + 内联标注 + 澄清契约（L2/F2 的执行侧）
  W11 实体解析契约：注入对象必须满足 EntityResolver 协议（L8）
  W12 formatter 目录自动发现（L3）
  W13 沙箱已作废的文案一致性：_sandbox_instructions 不得再宣称白名单限制
  W14 F3 代码意图门控：_CODE_INTENT_RE 在位且误伤可控
  W15 阶段路由：失败推进而非 abort / 中断收尾 / replan 回环
  W16 真实 provider 生产链路：扫描 → 注册 → 能力层默认屏蔽（集成冒烟）

设计取舍：
  - 合成 provider 决定论验证契约；W16 单独走生产扫描路径。
  - CodeAgent 用桩 model 构造（构造期不发起 LLM 调用）。
  - 交易日历用桩替换，测试不依赖真实日历/DB/网络。

本地回归命令（CI 只跑 compileall，不跑测试）：
  cd backend_api_python; python -m pytest tests/test_wiring.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# ── 路径：agent 内部用裸包名互相 import，app/agent 必须进 sys.path ──
_BACKEND = Path(__file__).resolve().parents[1]
_AGENT_DIR = _BACKEND / "app" / "agent"
for _p in (str(_BACKEND), str(_AGENT_DIR)):
    if _p not in sys.path:
        sys.path.append(_p)

from agents.task_agent import (  # noqa: E402
    PLAN_PHASE_MAX_STEPS,
    TaskAgent,
    _capability_names,
    _load_plan_template,
    _normalize_phases,
    _normalize_plan_tools,
    _sandbox_instructions,
    _tool_result_var,
    _wrap_stage_guard,
)
from capabilities.loader import CAPABILITY_DOMAIN, load_admitted  # noqa: E402
from infra import staging  # noqa: E402
from nodes import (  # noqa: E402
    PLAN_BATCH_MAX_STEPS,
    _auto_stage_phase_result,
    _CODE_INTENT_RE,
    _decide_phase_transition,
    route_after_execute,
)
from resolvers.base import EntityResolver, ResolveResult  # noqa: E402
from resolvers.composite import CompositeResolver  # noqa: E402
from resolvers.time import TimeResolver  # noqa: E402
from tools.base import ToolProvider  # noqa: E402


# ═══════════════════════════════════════════════════════════════
#  工具桩
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def provider() -> ToolProvider:
    """合成 provider：common / finance / capability 三个来源各备一个工具。"""
    p = ToolProvider()
    p._selectable_domains.add("finance")   # 等价于 scan_subdirectories 见到 tools/finance

    def common_tool(x: str = "") -> dict:
        """通用工具（测试桩）。"""
        return {"x": x}

    def finance_tool(code: str = "") -> dict:
        """金融域工具（测试桩）。"""
        return {"code": code}

    def daily(code: str = "") -> dict:
        """数据能力：日线（测试桩，模拟 admission.json 准入函数）。"""
        return {"code": code}

    p.register("common_tool", common_tool, domain="common")
    p.register("finance_tool", finance_tool, domain="finance")
    p.register("daily", daily, domain=CAPABILITY_DOMAIN)
    return p


class _StubModel:
    """CodeAgent 桩 model：构造期只读 `model_id`，不发起 LLM 调用。"""
    model_id = "wiring-test-stub"


def _build(provider: ToolProvider, **kwargs):
    """调用 `_build_code_agent` 而不跑 TaskAgent.__init__（后者需要 llm/memory/retriever）。"""
    agent = TaskAgent.__new__(TaskAgent)
    agent.max_tool_rounds = 5
    return TaskAgent._build_code_agent(
        agent, model=_StubModel(), provider=provider, skill_tools=[], **kwargs
    )


def _tools(agent) -> dict:
    """沙箱内实际可调用的函数表（executor.custom_tools）。"""
    return dict(agent.python_executor.custom_tools)


# ═══════════════════════════════════════════════════════════════
#  W1：能力层 = 来源层，不进可选域
# ═══════════════════════════════════════════════════════════════

def test_capability_is_a_source_layer_not_a_selectable_domain(provider):
    """能力层不得作为"可选工具域"暴露给 planner（L1：quant vs finance 互斥二选一）。"""
    domains = provider.get_domains()
    assert "finance" in domains
    assert CAPABILITY_DOMAIN not in domains
    assert provider.get_domain("daily") == CAPABILITY_DOMAIN


def test_capability_view_filters_by_source_layer(provider):
    """能力视图（planner 提示用）只列来源层工具，不混入域/通用工具。"""
    assert _capability_names(provider) == ["daily"]
    assert _capability_names(None) == []


# ═══════════════════════════════════════════════════════════════
#  W2：阶段契约规范化
# ═══════════════════════════════════════════════════════════════

def test_normalize_plan_tools_keeps_order_dedups_and_reports_ghosts():
    names = {"daily", "all_codes"}
    assert _normalize_plan_tools(["daily", "daily", "ghost"], names) == (["daily"], ["ghost"])
    assert _normalize_plan_tools("daily, all_codes", names) == (["daily", "all_codes"], [])
    assert _normalize_plan_tools(["", "  "], names) == ([], [])
    # 非数组不炸（LLM 偶尔给 dict/None）
    assert _normalize_plan_tools(None, names) == ([], [])
    assert _normalize_plan_tools({"daily": 1}, names) == ([], [])


def test_normalize_phases_contract():
    """阶段契约规范化：白名单过滤 / on_fail 收敛 / 预算钳制 / internal_plan 透传。"""
    names = {"t1", "t2"}
    out = _normalize_phases([
        {"name": "取数", "goal": "取近5日数据", "tools": ["t1", "幻觉名", "t1"],
         "on_fail": "重试", "max_retries": 99, "step_budget": 999,
         "internal_plan": True, "acceptance": "快照非空"},
        {"name": "空目标", "goal": "   "},            # 无 goal → 跳过
        {"name": "成稿", "goal": "成稿"},             # tools 缺失 → []（回退域逻辑）
    ], names)
    assert [p["id"] for p in out] == [1, 2]
    p0, p1 = out
    assert p0["tools"] == ["t1"] and p0["tools_dropped"] == ["幻觉名"]
    assert p0["on_fail"] == "retry"                 # 非法值回退
    assert p0["max_retries"] == 3                   # 钳制 [0,3]
    assert p0["step_budget"] == PLAN_PHASE_MAX_STEPS
    assert p0["internal_plan"] is True
    assert p0["acceptance"] == ["快照非空"]          # str → [str]
    assert p1["tools"] == [] and p1["internal_plan"] is None and p1["step_budget"] == 0
    assert _normalize_phases("not-a-list", names) == []


def test_plan_prompt_exposes_single_segment_naming_channel():
    """L9 病灶是"示例/规则/实现三者互斥"：提示必须同时覆盖单段与多段两条点名通道。

    现行形态（2026-09-15）：JSON 结构里 tools 字段说明 + 数据能力边界规则行
    （"无 phases（单段任务）→ 顶层 tools 点名"）承担单段示例；断言按当前形态验证
    两条通道都有描述，防止提示改回"能力只认 phases[].tools"的互斥旧态。
    """
    text = _load_plan_template()
    assert "必须点名才会注入" in text
    # 单段通道：顶层 tools 的说明必须在场且注明"仅无 phases 时使用"
    assert "仅无 phases 时使用" in text, "顶层 tools 单段点名通道无说明（L9 互斥复发）"
    # 能力名清单与"单段 → 顶层 tools"规则行必须在场
    naming_lines = [ln for ln in text.splitlines() if "list_signals" in ln]
    assert naming_lines, "能力名单里没有 list_signals（能力视图与提示脱节）"
    assert any("顶层 `tools`" in ln or "顶层 tools" in ln for ln in naming_lines + text.splitlines()), \
        "单段点名规则行缺失（无 phases → 顶层 tools）"


# ═══════════════════════════════════════════════════════════════
#  W4：工具注入面（真实构建 CodeAgent 后断言注入结果）
# ═══════════════════════════════════════════════════════════════

def test_single_segment_extra_tools_union_with_domain(provider):
    """单段任务的附加点名是**并集**：既拿到点名能力，也不挤掉域工具（L9）。"""
    tools = _tools(_build(provider, phase_id=0, domain="finance", extra_tools=["daily"]))
    assert {"common_tool", "finance_tool", "daily"} <= set(tools)


def test_single_segment_extra_tools_without_domain(provider):
    """无域（纯通用）时点名的能力同样可达——能力不属于任何可选域。"""
    tools = _tools(_build(provider, phase_id=0, domain="", extra_tools=["daily"]))
    assert set(tools) == {"common_tool", "daily"}


def test_phase_whitelist_wins_over_extra_tools(provider):
    """phases[].tools 是独占白名单：附加点名不生效、域基调不进（避免静默放宽工具面）。"""
    tools = _tools(_build(provider, phase_id=1, domain="finance",
                          tools=["finance_tool"], extra_tools=["daily"]))
    assert set(tools) == {"finance_tool"}


def test_no_naming_falls_back_to_domain_basis(provider):
    """未点名时回退域基调：域工具 + 通用工具（暂存区三件套已删除，不再是常驻工具）。"""
    tools = _tools(_build(provider, phase_id=2, domain="finance"))
    assert set(tools) == {"common_tool", "finance_tool"}


def test_whitelist_tool_names_are_normalized(provider):
    """白名单名字归一化：planner 常把工具写成调用形态（如 finance_tool()），
    剥掉括号/空白后必须命中——差一个字符就被静默丢弃的断链不应复发。"""
    tools = _tools(_build(provider, phase_id=21, domain="finance",
                          tools=[" finance_tool() "]))
    assert "finance_tool" in tools


# ═══════════════════════════════════════════════════════════════
#  W5：install_tools 单入口 + 每次执行前重装
# ═══════════════════════════════════════════════════════════════

def test_install_tools_syncs_all_three_paths(provider):
    """install_tools 必须把权威表同步到 custom_tools / state，并在 send_tools
    （run 启动会调，重设 static_tools）之后仍能经 _ensure_tools_available 并入。

    2026-09-15 合并后（_reinstall_tools + _sync_tools_into_static →
    _ensure_tools_available），调用方只调一次 install_tools——
    static_tools 由 smolagents send_tools 在 run() 时才创建，构造期为 None 是正常的；
    因此断言分两段：①构造后 custom_tools/state 即同步；②send_tools 模拟 run 启动后，
    static_tools 也要被补上。
    """
    agent = _build(provider, phase_id=5, domain="finance", tools=["finance_tool"])
    ex = agent.python_executor
    assert "finance_tool" in ex.custom_tools
    assert "finance_tool" in ex.state
    ex.send_tools({})          # 模拟 run 启动：static_tools 被重设（仅含 BASE_PYTHON_TOOLS 等）
    out = ex("finance_tool(code='600519')")   # __call__ 前置 _ensure_tools_available 触发并入
    assert "finance_tool" in (ex.static_tools or {}), \
        "send_tools 重设后 static_tools 未被 _ensure_tools_available 并入"
    assert "600519" in (out.logs or str(out) or "") or out.output is not None


def test_tools_reinstalled_after_retry_clears_sandbox(provider):
    """阶段**重试**时沙箱工具表会被清空——实证 `state=4 custom_tools=0`。

    修法是 executor 持有权威工具表并在每次执行前重装（_ensure_tools_available），
    而非只注入一次。本条模拟重试形态（state/custom_tools 被清），验证工具仍在。
    """
    agent = _build(provider, phase_id=15, domain="finance", tools=["finance_tool"])
    ex = agent.python_executor
    ex.send_tools({})
    for k in list(ex.state):
        if k != "__name__":
            ex.state.pop(k, None)
    ex.custom_tools.clear()
    out = ex("print(finance_tool(code='600519'))")
    assert "600519" in (out.logs or ""), "重试后工具丢失 → _ensure_tools_available 未生效"


def test_custom_tools_callable_after_send_tools(provider):
    """send_tools（官方注入点，会重设 static_tools）之后业务工具必须仍可调用。"""
    agent = _build(provider, phase_id=12, domain="finance", tools=["finance_tool"])
    ex = agent.python_executor
    ex.send_tools({})
    out = ex("print(finance_tool(code='600519'))")
    assert "600519" in (out.logs or ""), \
        "业务工具调不到：send_tools 重设 static_tools 后丢失（L16 复发）"


def test_list_tools_gives_full_signature(provider):
    """参数签名必须由 list_tools() 直接给出，不能逼模型去 `import inspect`。"""
    agent = _build(provider, phase_id=14, domain="finance", tools=["finance_tool"])
    lt = agent.tools["list_tools"]
    out = lt(domain="")
    assert "finance_tool(code: str = '')" in out, f"list_tools 未给出完整签名：{out}"


# ═══════════════════════════════════════════════════════════════
#  W6：工具结果变量续承（两级统一的模型侧形态）
# ═══════════════════════════════════════════════════════════════

def test_tool_result_var_is_deterministic():
    """`_r_<工具名>` 必须可由工具名事前推导——带序号的名字模型无法预知，
    会把"取数 + 取回"硬拆成两步（2026-09-15 实测）。"""
    assert _tool_result_var("get_daily") == "_r_get_daily"
    assert _tool_result_var("weird name!").isidentifier()
    assert _tool_result_var("").isidentifier()   # 非法名回退 _r_result


def test_tool_result_auto_registered_and_returned(provider):
    """_wrap_stage_guard 契约：①原样返回数据本身（不是通知字符串）；
    ②结果登记进 executor.state['_r_<工具名>']。

    2026-09-15 二次修订实证：返回"变量名提示"会让 `x = tool()` 拿到通知而非数据，
    模型被迫多走一步取回。
    """
    agent = _build(provider, phase_id=17, domain="finance")
    ex = agent.python_executor
    big = {"indices": [{"name": "x" * 120} for _ in range(80)]}
    wrapped = _wrap_stage_guard(lambda: big, "fake_tool", ex)
    out = wrapped()
    assert out is big, "工具结果必须原样返回，不得包装成通知"
    assert ex.state["_r_fake_tool"] is big, "结果未登记进 state['_r_<工具名>']"

    # executor 为 None → 退化为原函数（不登记、不包装）
    fn = lambda: 1  # noqa: E731
    assert _wrap_stage_guard(fn, "naked_tool", None) is fn


# ═══════════════════════════════════════════════════════════════
#  W7：会话级变量存储（infra.staging 新 API 闭环）
# ═══════════════════════════════════════════════════════════════

def test_staging_obj_roundtrip_and_clear():
    """stage_put_obj / stage_get_obj / stage_scope_vars / stage_clear 闭环。

    存**原对象**（跳过序列化往返），非法 scope/缺失名返回可判错结构而非抛异常。
    """
    scope = "wiring_obj_scope"
    obj = {"quotes": [1, 2, 3]}
    try:
        assert staging.stage_put_obj(scope, "result_a", obj) is True
        assert staging.stage_get_obj(scope, "result_a") is obj      # 原对象，非副本
        assert staging.stage_scope_vars(scope) == {"result_a": obj}
        # 非法 scope 拒绝且不抛
        assert staging.stage_put_obj("bad scope!", "x", 1) is False
        r = staging.stage_get_obj(scope, "missing")
        assert isinstance(r, dict) and r.get("ok") is False
        staging.stage_clear(scope)
        assert staging.stage_scope_vars(scope) == {}
    finally:
        staging.stage_clear(scope)


def test_staging_scope_ttl_expires_stale():
    """TTL 清理：超过 STAGING_SCOPE_TTL 未写入的 scope 必须被回收
    （M5：run 中途异常退出不走 finalize 时，_OBJ 不能无限驻留）。"""
    scope_old, scope_new = "wiring_ttl_old", "wiring_ttl_new"
    try:
        assert staging.stage_put_obj(scope_old, "a", 1) is True
        assert staging.stage_put_obj(scope_new, "b", 2) is True
        # 把旧 scope 的时间戳拨回 TTL+10s 之前
        staging._SCOPE_TS[scope_old] = staging._SCOPE_TS[scope_old] - (staging._SCOPE_TTL + 10)
        staging.stage_put_obj(scope_new, "c", 3)   # 任意一次写入触发惰性清理
        assert staging.stage_scope_vars(scope_old) == {}, "超 TTL scope 未被清理"
        assert staging.stage_get_obj(scope_new, "b") == 2
    finally:
        staging.stage_clear(scope_old)
        staging.stage_clear(scope_new)


def test_framework_auto_staging_registers_session_var():
    """_auto_stage_phase_result：阶段完整结果必须注册为会话级变量（L10 的当前形态：
    此前 `from tools.staging` ImportError 被静默吞掉，强制续承一次都没生效）。"""
    scope = "wiring_auto_scope"
    payload = "阶段完整结果" * 400          # 远超 min_chars=800
    try:
        name = _auto_stage_phase_result(scope, {"id": 7, "name": "回测"}, payload)
        assert name, "阶段结果自动续承未执行（infra.staging 接线断开复发）"
        assert name.isidentifier(), "变量名必须是合法 Python 标识符（下阶段要裸名引用）"
        got = staging.stage_get_obj(scope, name)
        assert got == payload
        # 低于阈值不注册（避免噪音）
        assert _auto_stage_phase_result(scope, {"id": 8, "name": "短"}, "短") == ""
    finally:
        staging.stage_clear(scope)


# ═══════════════════════════════════════════════════════════════
#  W9：final_answer_checks 已接线（M10）
# ═══════════════════════════════════════════════════════════════

def test_final_answer_check_installed_and_rejects_degenerate(provider):
    """M10：_build_code_agent 必须把 _check_final_answer 挂进 final_answer_checks，
    且该检查拒收空答案与半成品（仍含裸 <code> 标签的文本）。

    _check_final_answer 是 _build_code_agent 的嵌套函数，从挂载点取出验证行为。
    """
    agent = _build(provider, phase_id=18, domain="finance")
    checks = getattr(agent, "final_answer_checks", None) or []
    assert checks, "final_answer_checks 未接线（M10 断链）"
    check = checks[0]

    assert check(None, None, None) is False
    assert check("   ", None, None) is False
    assert check("```python\n<code>\nprint(1)\n```", None, None) is False, \
        "含裸 <code> 且无 final_answer 痕迹 = 半成品，必须拒收"
    assert check("正常分析结论", None, None) is True


# ═══════════════════════════════════════════════════════════════
#  W10：时间实体解析（交易日口径 + 内联标注 + 澄清）
# ═══════════════════════════════════════════════════════════════

class _FakeCalendar:
    """交易日历桩：周末非交易日，其余为交易日（与真实日历/DB 解耦）。"""

    @staticmethod
    def is_trading_day(day: str) -> bool:
        return datetime.strptime(day, "%Y-%m-%d").weekday() < 5

    @classmethod
    def prev_trading_day(cls, day: str, n: int) -> str:
        d = datetime.strptime(day, "%Y-%m-%d")
        while n > 0:
            d -= timedelta(days=1)
            if cls.is_trading_day(d.strftime("%Y-%m-%d")):
                n -= 1
        return d.strftime("%Y-%m-%d")

    @classmethod
    def next_trading_day(cls, day: str, n: int) -> str:
        d = datetime.strptime(day, "%Y-%m-%d")
        while n > 0:
            d += timedelta(days=1)
            if cls.is_trading_day(d.strftime("%Y-%m-%d")):
                n -= 1
        return d.strftime("%Y-%m-%d")

    @classmethod
    def last_finish_trading_day(cls, ref: str) -> str:
        day = ref[:10]
        return day if cls.is_trading_day(day) else cls.prev_trading_day(day, 1)

    @staticmethod
    def trade_date_range(start: str, end: str) -> str:
        return f"{start}~{end}"


@pytest.fixture
def fake_calendar(monkeypatch):
    import resolvers.time as time_mod
    cal = _FakeCalendar()
    monkeypatch.setattr(time_mod, "_cal", lambda: cal)
    return cal


def test_time_resolver_infers_domain_and_inlines_dates(fake_calendar):
    """L2：领域三级倒推必须生效；F2：日期内联钉在原文时间词上。

    2026-09-15 用户定格式（简洁不喧宾夺主）：所有时间事实一律就地内联为
    `词(日期)`，区间为 `词(起~止)`，`现在` 带时分秒；尾部【时间】说明块废除。
    """
    assert TimeResolver(entity_type="stock").domain == "finance"
    assert TimeResolver().domain == ""

    saturday = datetime(2026, 9, 12, 10, 0)
    # 金融域 + 非交易日说"今天行情" → 澄清反问（不猜口径）
    r = TimeResolver(entity_type="stock", now=saturday).resolve("今天的行情怎么样")
    assert r is not None and r.needs_clarify and "不是交易日" in r.clarify_question

    monday = datetime(2026, 9, 14, 10, 0)
    r = TimeResolver(entity_type="stock", now=monday).resolve("今天的行情怎么样")
    assert r is not None and not r.needs_clarify
    assert "今天(2026-09-14)" in r.effective_input
    assert TimeResolver(entity_type="stock", now=monday).resolve(
        "今日涨停概率最大的股票").effective_input.startswith("今日(2026-09-14)涨停概率最大的股票")
    # 交易日口径：周一说"昨日" → 上一交易日 09-11（周五），不是自然日 09-13
    assert TimeResolver(entity_type="stock", now=monday).resolve(
        "昨日涨停的股票").effective_input.startswith("昨日(2026-09-11)涨停的股票")

    # 盘中语义（用户实测"无所适从"修正）：交易日盘中问"现在买什么股"，
    # 内联的是 `现在(日期 时刻)` —— 不得出现指向昨日的"最近已收盘交易日"锚点
    tuesday_intraday = datetime(2026, 9, 15, 13, 50)
    r = TimeResolver(entity_type="stock", now=tuesday_intraday).resolve("现在买什么股?不要涨停了的")
    assert r is not None and r.effective_input.startswith("现在(2026-09-15 "), \
        f"盘中'现在'必须内联带时刻的日期：{r.effective_input if r else None}"
    assert "最近已收盘" not in r.effective_input, "盘中注入'最近已收盘=昨天'误导 agent"

    # 金融域常驻锚点：原文无时间词时补一个最小的 `今天(日期)`；无尾部说明块
    plain = TimeResolver(entity_type="stock", now=monday).resolve("涨停概率最大的股票")
    assert plain is not None and plain.effective_input.endswith("今天(2026-09-14)")
    assert "【时间】" not in plain.effective_input, "尾部说明块应已废除"

    # 区间型同样内联：近5个交易日(起~止)
    win = TimeResolver(entity_type="stock", now=monday).resolve("近5个交易日的行情")
    assert win is not None and "近5个交易日(" in win.effective_input and "~" in win.effective_input

    # 时间窗不明（域无关）→ 反问
    vague = TimeResolver(entity_type="stock", now=monday).resolve("最近的行情怎么样")
    assert vague is not None and vague.needs_clarify


def test_injected_resolver_satisfies_entity_resolver_contract():
    """L8：nodes.py 调 `.resolve()`，注入的复合解析器必须满足该契约（不是裸函数）。"""

    class _Clarify(EntityResolver):
        def resolve(self, user_input: str):
            return ResolveResult(clarify_question="请确认标的", entity_type="time_clarify")

    class _Stockish(EntityResolver):
        def resolve(self, user_input: str):
            return ResolveResult(entity_code="600519", entity_name="贵州茅台",
                                 entity_type="stock", effective_input=user_input)

    class _Broken(EntityResolver):
        def resolve(self, user_input: str):
            raise RuntimeError("子解析器故障")

    composed = CompositeResolver([_Stockish()])
    assert isinstance(composed, EntityResolver)
    result = composed.resolve("分析600519")
    assert isinstance(result, ResolveResult)
    assert result.entity_code == "600519" and result.effective_input == "分析600519"

    factory = CompositeResolver([lambda ctx: _Stockish()])
    assert factory.resolve("分析600519").entity_code == "600519"

    # 澄清优先：任一子解析器要求澄清即短路，与顺序无关
    assert CompositeResolver([_Stockish(), lambda ctx: _Clarify()]).resolve("分析600519").needs_clarify
    assert CompositeResolver([lambda ctx: _Clarify(), _Stockish()]).resolve("分析600519").needs_clarify

    # 单个子解析器故障不拖垮整体（降级为 warning + 跳过）
    assert CompositeResolver([_Broken(), _Stockish()]).resolve("分析600519").entity_code == "600519"


# ═══════════════════════════════════════════════════════════════
#  W12：formatter 目录自动发现
# ═══════════════════════════════════════════════════════════════

def test_formatters_autoload_and_domain_lookup():
    """注册表不得为空（L3：自动发现取代了被注释掉的 `from . import finance`）。"""
    import formatters
    from formatters.base import get_formatter, list_formatters
    from formatters.default import DefaultFormatter

    registry = list_formatters()
    assert registry, "formatter 注册表为空 —— 目录自动发现断链（L3 复发）"
    assert "finance" in registry
    assert type(get_formatter(domain="finance")).__name__ == registry["finance"]
    # key 语义：注册方用领域名，调用方传实体类型不得误命中（L3 的另一半）
    assert isinstance(get_formatter(entity_type="stock"), DefaultFormatter)
    assert isinstance(get_formatter(domain=""), DefaultFormatter)


# ═══════════════════════════════════════════════════════════════
#  W13：沙箱作废后的文案一致性
# ═══════════════════════════════════════════════════════════════

def test_sandbox_instructions_match_open_sandbox(provider):
    """import 全放行后，执行环境文案不得再宣称白名单限制（否则模型被误导）。"""
    text = _sandbox_instructions()
    for stale in ("不在沙箱白名单", "禁止 import", "仅限清单内模块"):
        assert stale not in text, f"文案仍宣称限制，与全开状态矛盾：{stale}"
    # 实测有效的防坑提示不应被删
    assert "不要在沙箱内重复打印整份源码" in text
    assert "_r_" in text, "工具结果兜底登记的取回说明（_r_<工具名>）不应缺失"

    agent = _build(provider, phase_id=16, domain="finance")
    assert "*" in set(agent.python_executor.authorized_imports), \
        "executor 未全开 import（沙箱作废决策被回退）"


# ═══════════════════════════════════════════════════════════════
#  W14：F3 代码意图门控
# ═══════════════════════════════════════════════════════════════

def test_code_intent_gate_blocks_rag_entity_injection():
    """F3：代码/通用意图不得被 RAG 历史标的污染（实证：跑马灯任务被注入 600928）。"""
    for q in ("写一个跑马灯的代码并运行", "用 python 写个排序", "写一段递归的示例"):
        assert _CODE_INTENT_RE.search(q), f"代码意图未命中门控：{q}"
    for q in ("分析一下这只票", "茅台还能拿吗", "今天涨停概率最大的股票"):
        assert not _CODE_INTENT_RE.search(q), f"金融意图被误判成代码：{q}"


# ═══════════════════════════════════════════════════════════════
#  W15：阶段路由与推进决策
# ═══════════════════════════════════════════════════════════════

def test_route_after_execute_advances_past_failed_phase():
    """单阶段验收失败且重试耗尽，必须推进下一阶段而非 abort 整个管道。"""
    phases = [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]

    s1 = {"phases": phases, "phase_index": 0, "phase_retry": 0,
          "phase_results": [{"id": 1, "status": "fail"}],
          "_phase_abort": False, "_phase_replan_request": False}
    assert route_after_execute(s1) == "execute"

    s2 = {"phases": phases, "phase_index": 1, "phase_retry": 0,
          "phase_results": [{"id": 1, "status": "fail"}],
          "_phase_abort": False, "_phase_replan_request": False}
    assert route_after_execute(s2) == "execute", \
        "单阶段验收失败不应 abort 整个多阶段管道"

    s3 = {"phases": phases, "phase_index": 4, "phase_retry": 0,
          "phase_results": [{"id": i, "status": "pass"} for i in range(1, 5)],
          "_phase_abort": False, "_phase_replan_request": False}
    assert route_after_execute(s3) == "finalize"

    s4 = {"phases": phases, "phase_index": 1, "phase_retry": 0,
          "phase_results": [], "_phase_abort": True, "_phase_replan_request": False}
    assert route_after_execute(s4) == "finalize"

    s5 = {"phases": phases, "phase_index": 0, "phase_retry": 0,
          "phase_results": [], "_phase_abort": False, "_phase_replan_request": True}
    assert route_after_execute(s5) == "plan"


def test_decide_phase_transition_tool_data_fault_soft_passes():
    """工具/数据不可用导致的验收失败必须软通过推进（不重试、不放弃已产出工作）。"""
    kind, new_retry, do_replan, soft = _decide_phase_transition(
        passed=False, reason="tool_data_fault", on_fail="retry",
        retry=0, max_retries=1, replan_count=0)
    assert kind == "advance" and soft is True and new_retry == 0 and do_replan is False

    kind, new_retry, do_replan, soft = _decide_phase_transition(
        passed=False, reason="agent_fault", on_fail="retry",
        retry=0, max_retries=2, replan_count=0)
    assert kind == "retry" and new_retry == 1 and soft is False

    kind, new_retry, do_replan, soft = _decide_phase_transition(
        passed=False, reason="agent_fault", on_fail="retry",
        retry=2, max_retries=1, replan_count=0)
    assert kind == "advance" and soft is False

    kind, new_retry, do_replan, soft = _decide_phase_transition(
        passed=True, reason="pass", on_fail="retry",
        retry=0, max_retries=1, replan_count=0)
    assert kind == "advance" and soft is False


def test_batch_budget_cap():
    """批次化（2026-09-15）：合并阶段的预算之和必须被 PLAN_BATCH_MAX_STEPS 封顶。"""
    assert PLAN_BATCH_MAX_STEPS >= PLAN_PHASE_MAX_STEPS
    assert PLAN_BATCH_MAX_STEPS == 30


# ═══════════════════════════════════════════════════════════════
#  W16：真实 provider 生产链路（集成冒烟）
# ═══════════════════════════════════════════════════════════════

def test_real_provider_wiring(monkeypatch):
    """走生产入口（NodeContext.init_tools）断言注册链路。

    能力层自 2026-09-14 起默认屏蔽（19 项准入全无审核留痕）；本条测"注册链路"
    这条契约，故显式打开；"生产默认关闭"是独立决策，不在此断言。
    """
    monkeypatch.setenv("CAPABILITIES_ENABLED", "1")
    from nodes import NodeContext

    ctx = NodeContext(llm=None)      # init_tools 只用 llm 造 adapter，不发起调用
    ctx.init_tools()
    p = ctx.tool_provider
    domains = p.get_domains()
    assert "finance" in domains
    assert CAPABILITY_DOMAIN not in domains

    cap = set(p.list_by_domain(CAPABILITY_DOMAIN))
    assert cap == set(_capability_names(p))            # 两处视图同源，不各自漂移
    assert not (cap & set(domains))

    admitted = {name for _mod, name, _t, _c in load_admitted()}
    assert admitted, "admission.json 未解析出任何准入项"
    assert admitted & cap, "admission.json 的准入项一个都没注册进 provider"

    # 端到端：真实 provider 下单段任务同样能拿到点名能力
    tools = _tools(_build(p, phase_id=9, domain="finance", extra_tools=["daily"]))
    assert "daily" in tools


# ═══════════════════════════════════════════════════════════════
