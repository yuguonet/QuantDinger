# -*- coding: utf-8 -*-
"""QDAgent 试一版 smoke —— 不需要 API key，用脚本化假模型驱动全流程。

覆盖：
  1. happy path：FnToolAdapter 挂载 + 工具调用 + 正常收尾
  2. 闭环③ 交易确认闸：confirm 缺失 → requires_confirmation 短路，副作用不触发
  3. 危险面守卫：破坏性命令 / 凭据路径 → deny 短路，副作用不触发
  4. 闭环② 数字溯源门：编造数字 → 拦截重写；重写后放行
  5. 审计轨迹：qd_agent_runs.jsonl 事件齐全（run_start/tool_call/tool_result/run_finish）

运行（需 mimoagent 已安装，或 MIMOAGENT_SRC 指向其 src/）：
  python app/agent/scripts/qd_smoke.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
AGENT_DIR = HERE.parent.parent            # app/agent
BACKEND_ROOT = HERE.parent.parent.parent.parent  # backend_api_python

# 插两个目录：backend_api_python（让 `import app.*` 可用）+ app/agent（让裸名可用）。
#
# 【导入约定】agent 包内一律用**裸名**（`from chain.store import ...`），不用全名
# `app.agent.chain.store`。原因：全名把包名硬编码进 300+ 处 import，一旦移动/重命名
# 目录就得全量改；裸名只依赖这里一处 `__file__` 相对计算，移动时零改动。
# 代价是 app/agent 下的 23 个顶层名（chain/tools/utils/log/memory/rag/llm/...）
# 成为全局裸名 —— 因此 **AGENT_DIR 必须排在 BACKEND_ROOT/app 之前**，否则
# `import utils` 会命中 app/utils。已实测与 site-packages 无同名冲突。
for p in (str(BACKEND_ROOT), str(AGENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

_mimo_src = os.getenv("MIMOAGENT_SRC", "")
if _mimo_src and _mimo_src not in sys.path:
    sys.path.insert(0, _mimo_src)

import mimo_boot

from mimoagent.models import TokenStats  # noqa: E402
from mimoagent.environments import get_environment  # noqa: E402

from qd_agent import QDAgent  # noqa: E402


# ═══════════════════════════════════════════════════════════════
#  脚本化假模型（对齐 DeterministicModel 的鸭子类型）
# ═══════════════════════════════════════════════════════════════


def _preselect_marker() -> str:
    """预选请求的识别标记，取自 PRESELECT_SYSTEM 常量本身。

    【2026-10-01 教训】首版把判据写死成提示词里的一个词（"工具路由器"），
    后来预选从「LLM 路由器」重构为「外置 plan」、提示词改写 ⇒ 判据静默失配
    ⇒ ScriptedModel 不再短路预选 ⇒ 所有按"第 N 次调用 = 第 N 个动作"编排的
    用例错位一格。故判据必须与常量绑定，不写死文案。
    """
    from tools.tool_preselect import PRESELECT_SYSTEM as _ps
    return str(_ps).strip()[:16]


def _is_preselect_request(messages) -> bool:
    """识别 QDAgent 的工具预选请求（首条 system 以 PRESELECT_SYSTEM 开头）。

    比"没有 tools 参数"更可靠——压缩摘要同样是不带 tools 的纯文本回合，不能误伤。
    """
    try:
        first = (messages or [{}])[0] or {}
        return _preselect_marker() in str(first.get("content") or "")
    except Exception:
        return False


class ScriptedModel:
    """按脚本顺序返回响应的假模型。

    【2026-10-01 契约变更】QDAgent 现在在每次 run 前会先发一次**工具预选**
    请求（纯文本、不带 tools）。若这里照常消耗一条脚本输出，所有按"第 N 次
    调用 = 第 N 个工具动作"编排的用例会整体错位一格（实测 test 6 表现为
    `cat tmp/demo.txt` 报 FileNotFoundError —— 因为 write 那条被预选吃掉了）。
    故预选请求在**此处短路**返回空选，不消耗脚本；预选本身另有 test 覆盖。
    """
    def __init__(self, outputs: list[dict]):
        self.outputs = outputs
        self.i = -1
        self.n_calls = 0
        self.preselect_calls = 0
        self.token_stats = TokenStats()

    def query(self, messages, **kwargs):
        self.n_calls += 1
        if kwargs.get("tools") is None and _is_preselect_request(messages):
            self.preselect_calls += 1
            return {"content": "[]"}
        self.i += 1
        self.token_stats.input_tokens += 50
        self.token_stats.output_tokens += 25
        return dict(self.outputs[self.i])

    def get_template_vars(self):
        return {"model_name": "scripted", "n_model_calls": self.n_calls}


# ═══════════════════════════════════════════════════════════════
#  假金融工具（模拟 tools/finance 约定：裸函数 + docstring Returns）
# ═══════════════════════════════════════════════════════════════

SIDE_EFFECTS = {"order_placed": False, "shell_ran": False}


def get_demo_quotes() -> dict:
    """演示行情：返回两只股票的价格。

    Returns:
        {"quotes": [{code, name, price}, ...]} —— dict，列表在二级键 quotes。
    """
    return {"quotes": [
        {"code": "600519", "name": "贵州茅台", "price": 1688.0},
        {"code": "000858", "name": "五粮液", "price": 142.5},
    ]}


def place_demo_order(symbol: str, amount: int, confirm: bool = False) -> dict:
    """演示下单（真实交易动作，需 confirm=true 才执行）。

    Args:
        symbol: 股票代码
        amount: 数量（股）
        confirm: 人工确认标记

    Returns:
        {"status": "placed"|"requires_confirmation", ...}
    """
    if not confirm:
        return {"status": "requires_confirmation", "symbol": symbol}
    SIDE_EFFECTS["order_placed"] = True
    return {"status": "placed", "symbol": symbol, "amount": amount}


def run_shell(command: str) -> dict:
    """演示 shell 执行。

    Args:
        command: shell 命令

    Returns:
        {"ran": bool, "command": str}
    """
    SIDE_EFFECTS["shell_ran"] = True
    return {"ran": True, "command": command}


def _tool_call(name: str, args: dict, cid: str = "c1") -> dict:
    return {"id": cid, "function": {"name": name, "arguments": args}}


def _build_agent(outputs: list[dict]) -> QDAgent:
    model = ScriptedModel(outputs)
    env = get_environment({"environment_class": "local"})
    return QDAgent(
        model,
        env,
        tools=[],  # smoke 不挂 bash 等原版工具（生产默认保留 mimo 原版全集）
        tool_functions=[get_demo_quotes, place_demo_order, run_shell],
    )


def _observations(agent) -> str:
    return "\n".join(
        m["content"] for m in agent.messages
        if m.get("role") == "tool" and isinstance(m.get("content"), str)
    )


PASS = []


def check(name: str, cond: bool, detail: str = ""):
    PASS.append(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


# ═══════════════════════════════════════════════════════════════
#  用例
# ═══════════════════════════════════════════════════════════════

def test_1_happy_path():
    print("test 1: happy path（工具挂载 + 调用 + 收尾）")
    agent = _build_agent([
        {"content": "", "tool_calls": [_tool_call("get_demo_quotes", {})]},
        {"content": "结论：贵州茅台现价 1688.0 元（来源：get_demo_quotes）"},
    ])
    status, result = agent.run("看看茅台现在多少钱")
    check("返回结论", "1688.0" in result)
    check("工具被调用", agent.model.n_calls == 2)
    check("观察值进上下文", "贵州茅台" in _observations(agent))
    check("数字溯源放行", agent._steps_taken == 2)


def test_2_trading_confirm_gate():
    print("test 2: 闭环③ 交易确认闸（未确认 → 短路，不触发副作用）")
    agent = _build_agent([
        {"content": "", "tool_calls": [_tool_call("place_demo_order", {"symbol": "600519", "amount": 100})]},
        {"content": "该操作需要您确认后才能执行。"},
    ])
    agent.run("帮我买 100 股茅台")
    obs = _observations(agent)
    check("返回 requires_confirmation", "requires_confirmation" in obs)
    check("副作用未触发", SIDE_EFFECTS["order_placed"] is False)


def test_3_danger_guard():
    print("test 3: 危险面守卫（破坏性命令 / 凭据路径 → deny）")
    agent = _build_agent([
        {"content": "", "tool_calls": [
            _tool_call("run_shell", {"command": "rm -rf /"}, "d1"),
            _tool_call("run_shell", {"command": "cat /root/.ssh/id_rsa"}, "d2"),
        ]},
        {"content": "命令被安全策略拒绝。"},
    ])
    agent.run("帮我清理一下系统")
    obs = _observations(agent)
    check("破坏性命令被拒", obs.count("安全策略拒绝") == 2, obs[:200])
    check("副作用未触发", SIDE_EFFECTS["shell_ran"] is False)


def test_4_grounding_gate():
    print("test 4: 闭环② 数字溯源门（编造数字 → 拦截重写）")
    agent = _build_agent([
        {"content": "", "tool_calls": [_tool_call("get_demo_quotes", {})]},
        # 编造 4+ 个数值（check_grounding 口径：数值 ≥4 且溯源率 <30% 才拒收）
        {"content": "结论：贵州茅台现价 1999.0 元，涨幅 99.99%，成交 8888 万股，主力净流入 77.7 亿元。"},
        {"content": "结论：贵州茅台现价 1688.0 元（来源：get_demo_quotes）"},   # 重写
    ])
    status, result = agent.run("茅台现在多少钱")
    check("编造数字被拦截重写", agent.model.n_calls == 3)
    check("最终结论用真实数字", "1688.0" in result and "1999.0" not in result)
    check("拦截话术进上下文", any(
        isinstance(m.get("content"), str) and "数字溯源拦截" in m["content"]
        for m in agent.messages
    ))


def test_5_trace_events():
    print("test 5: 审计轨迹（闭环①配套事件落盘）")
    agent = _build_agent([
        {"content": "", "tool_calls": [_tool_call("get_demo_quotes", {})]},
        {"content": "结论：贵州茅台现价 1688.0 元（来源：get_demo_quotes）"},
    ])
    agent.run("看看茅台")
    path = agent.trace.path
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    events = [__import__("json").loads(x) for x in lines]
    mine = [e for e in events if e.get("run_id") == agent.trace.run_id]
    kinds = {e["event"] for e in mine}
    check("事件齐全", {"run_start", "tool_call", "tool_result", "run_finish"} <= kinds, str(kinds))
    repro = next((e.get("repro") for e in mine if e["event"] == "run_start"), {})
    check("repro 字段", {"prompt_hash", "model", "tool_list_hash"} <= set(repro), str(repro))


def test_6_native_mimo_capabilities():
    print("test 6: mimo 全盘能力（原版 lowercase 目录 + compact）")
    import tempfile
    tmp = tempfile.mkdtemp()
    model = ScriptedModel([
        {"content": "", "tool_calls": [_tool_call("write", {"file_path": f"{tmp}/demo.txt", "content": "hello qd"}, "w1")]},
        {"content": "", "tool_calls": [_tool_call("bash", {"command": f"cat {tmp}/demo.txt", "description": "查看刚写入的文件"}, "b1")]},
        {"content": "文件内容确认：hello qd"},
    ])
    env = get_environment({"environment_class": "local", "cwd": tmp})
    agent = QDAgent(model, env, tool_functions=[get_demo_quotes])  # 原版工具全家桶默认开启
    names = agent.tool_registry.list_tools()
    check("原版工具挂载齐全", {"bash", "read", "write", "edit", "grep", "glob", "task", "compact", "actor"} <= set(names), str(names))
    check("AntiHackGuard 已启用", getattr(getattr(agent, "antihack", None), "config", None) is not None and agent.antihack.config.enabled is True)
    status, result = agent.run("帮我建个文件并看看内容")
    check("原版 write 工具生效", (Path(tmp) / "demo.txt").read_text() == "hello qd")
    check("原版 bash 工具生效", "hello qd" in _observations(agent))
    check("领域工具与原版共存", "get_demo_quotes" in names)
    check("update_plan 计划工具已挂", "update_plan" in names)


def test_7_service_facade():
    print("test 7: 服务门面（chat 契约 + 会话复用 + 记忆落史 + 预取注入）")
    import asyncio
    from types import SimpleNamespace
    from qd_service import QDAgentService

    class _Mem:
        def __init__(self):
            self.items = [("user", "之前聊过茅台的行情走势"), ("assistant", "好的")]

        async def add(self, sid, role, content):
            self.items.append((role, content))
            return True

        async def get_history(self, sid, limit=10):
            return [SimpleNamespace(role=r, content=c) for r, c in self.items[-limit:]]

    model = ScriptedModel([
        {"content": "", "tool_calls": [_tool_call("get_demo_quotes", {})]},
        {"content": "结论：贵州茅台现价 1688.0 元（来源：get_demo_quotes）"},
    ])
    mem = _Mem()
    svc = QDAgentService(
        model=model, memory=mem,
        agent_config={"tools": [], "tool_functions": [get_demo_quotes]},
    )
    resp = asyncio.run(svc.chat("看看茅台的行情多少钱", session_id="s1"))
    check("chat 契约 .content", "1688.0" in resp.content)
    check("会话复用同一 agent", svc._get_agent("s1") is svc._get_agent("s1"))
    check("记忆落史", any("贵州茅台现价" in c for _, c in mem.items))
    agent = svc._get_agent("s1")
    user_msg = next(m["content"] for m in agent.messages if m.get("role") == "user")
    check("预取注入记忆近史", "会话记忆近史" in user_msg and "之前聊过茅台" in user_msg)
    check("预取保留用户问题", "[用户问题]" in user_msg)

    # 技能自动加载（提智点 A）
    class _Skills:
        def list_skills(self):
            return [{"name": "market_screener", "description": "选股与市场筛选"}]

        def load_body(self, name):
            return "# 分析框架\n先看资金面再看技术面。" if name == "market_screener" else None

    model2 = ScriptedModel([{"content": "选股完成。"}])
    svc2 = QDAgentService(model=model2, skills=_Skills(), agent_config={"tools": [], "tool_functions": [get_demo_quotes]})
    asyncio.run(svc2.chat("帮我做个选股筛选", "s9"))
    agent2 = svc2._get_agent("s9")
    user_msg2 = next(m["content"] for m in agent2.messages if m.get("role") == "user")
    check("技能自动注入", "相关技能：market_screener" in user_msg2 and "先看资金面" in user_msg2)


def test_8_context_awareness():
    print("test 8: 上下文感知（多轮连贯 + 时间注入 + wrap-up 预算感知）")
    import asyncio
    from qd_service import QDAgentService

    model = ScriptedModel([
        {"content": "第一轮回答完成。"},
        {"content": "第二轮：沿用上文即可。"},
    ])
    svc = QDAgentService(model=model, agent_config={"tool_functions": [get_demo_quotes]})
    asyncio.run(svc.chat("我的自选股是茅台", "s8"))
    asyncio.run(svc.chat("我刚才说的自选是什么？", "s8"))
    agent = svc._get_agent("s8")
    user_texts = [m["content"] for m in agent.messages if m.get("role") == "user"]
    check("两轮对话同上下文", any("茅台" in t for t in user_texts) and any("自选是什么" in t for t in user_texts))
    sys_msg = next(m["content"] for m in agent.messages if m.get("role") == "system")
    idx = sys_msg.find("当前时间")
    val = sys_msg[idx + 4:idx + 12].lstrip(":：;； ") if idx >= 0 else ""
    check("系统模板注入当前时间", idx >= 0 and val[:1].isdigit(), sys_msg[:120])
    check("原版 context_usage 预算感知开启", agent.config.show_context_usage is True)
    check("compact 上下文压缩可用", "compact" in agent.tool_registry.list_tools())


def test_9_step_budget_per_run():
    print("test 9: 步数预算按请求归零（回归：带上下文会话空结果退出）")
    import asyncio
    from qd_service import QDAgentService

    model = ScriptedModel([
        {"content": "", "tool_calls": [_tool_call("get_demo_quotes", {})]},   # 轮1: 步1
        {"content": "第一轮完成。"},                                          # 轮1: 步2（耗尽 step_limit=2）
        {"content": "第二轮正常回答。"},                                      # 轮2: 若不归零会瞬死
    ])
    svc = QDAgentService(model=model, agent_config={
        "tools": [], "tool_functions": [get_demo_quotes], "step_limit": 2})
    r1 = asyncio.run(svc.chat("看行情", "s10"))
    r2 = asyncio.run(svc.chat("继续", "s10"))
    check("轮1 有结果", "第一轮完成" in r1.content)
    check("轮2 不被累积预算误杀", "第二轮正常回答" in r2.content)
    check("空结果不再发生", r2.content.strip() != "")


def test_10_empty_reply_fallback():
    print("test 10: 空回复兜底（reasoning-only → 轻推补结论）")
    import asyncio
    from qd_service import QDAgentService

    model = ScriptedModel([
        {"content": "", "reasoning_content": "模型思考中…"},   # 空正文
        {"content": "补充结论：牛市。"},                        # 轻推后的回答
    ])
    svc = QDAgentService(model=model, agent_config={"tools": [], "tool_functions": [get_demo_quotes]})
    resp = asyncio.run(svc.chat("现在什么行情", "s11"))
    check("空回复被轻推补齐", "牛市" in resp.content)
    agent = svc._get_agent("s11")
    check("轻推话术进上下文", any(
        isinstance(m.get("content"), str) and "不要再调用工具" in m["content"]
        for m in agent.messages))


def test_11_prefetch_no_pollution():
    print("test 11: 预取防污染（无关记忆/检索不注入，回归：问跑马灯变行情跑马灯）")
    import asyncio
    from types import SimpleNamespace
    from qd_service import QDAgentService

    class _Mem:
        async def add(self, sid, role, content):
            return True

        async def get_history(self, sid, limit=10):
            return [SimpleNamespace(role="user", content="昭衍新药和康辰药业的财务分析结论"),
                    SimpleNamespace(role="assistant", content="医药主线强势延续")]

    class _Retr:
        async def retrieve(self, query, top_k=None, filter=None, intent=""):
            return [{"content": "创新药板块今日领涨，CXO 概念资金净流入居前"}]

    model = ScriptedModel([{"content": "跑马灯代码已完成。"}])
    svc = QDAgentService(model=model, memory=_Mem(), retriever=_Retr(),
                         agent_config={"tools": [], "tool_functions": [get_demo_quotes]})
    resp = asyncio.run(svc.chat("写一个跑马灯的代码并运行", "s12"))
    agent = svc._get_agent("s12")
    user_msg = next(m["content"] for m in agent.messages if m.get("role") == "user")
    check("无关记忆被拦截", "会话记忆近史" not in user_msg and "昭衍新药" not in user_msg)
    check("无关检索被拦截", "参考资料" not in user_msg and "创新药" not in user_msg)
    check("任务本体保留", "跑马灯" in user_msg)


def test_12_retry_independent_budget():
    print("test 12: 溯源重试独立预算（回归：主轮耗尽后重试瞬死）")
    import asyncio
    from qd_service import QDAgentService

    model = ScriptedModel([
        {"content": "", "tool_calls": [_tool_call("get_demo_quotes", {})]},
        {"content": "结论：贵州茅台现价 1999.0 元，涨 99.99%，成交 8888 万股，主力净流入 77.7 亿元。"},  # 编造→拦截
        {"content": "结论：贵州茅台现价 1688.0 元（来源：get_demo_quotes）"},   # 重写（需独立预算）
    ])
    svc = QDAgentService(model=model, agent_config={
        "tools": [], "tool_functions": [get_demo_quotes], "step_limit": 2})  # 主轮刚好耗尽
    resp = asyncio.run(svc.chat("茅台现在多少钱", "s13"))
    check("重写轮拿到独立预算", "1688.0" in resp.content and "1999.0" not in resp.content)
    check("重写不被截断文案顶替", "被截断" not in resp.content)


def test_13_empty_response_nudge():
    print("test 13: Empty assistant response 走轻推（回归：误判为截断）")
    import asyncio
    from qd_service import QDAgentService

    model = ScriptedModel([
        {"content": "", "tool_calls": []},   # 全空→基类抛 LimitsExceeded('Empty assistant response')
        {"content": "补充结论：牛市。"},
    ])
    svc = QDAgentService(model=model, agent_config={"tools": [], "tool_functions": [get_demo_quotes]})
    resp = asyncio.run(svc.chat("现在什么行情", "s14"))
    check("空响应经轻推补齐", "牛市" in resp.content)
    check("不误报截断", "被截断" not in resp.content)


def test_15_realtime_info_must_search():
    print("test 15: 实时信息必须检索（回归：天气被导流去天气网）")
    from pathlib import Path as _P
    txt = (_P(__file__).resolve().parent.parent / "prompts" / "qd_system.txt").read_text(encoding="utf-8")
    check("实时信息强制检索", "必须调用" in txt and "web_search" in txt)
    check("禁止导流用户自查", "严禁回复" in txt and "自己去网上查" in txt)
    check("金融限制不波及通用工具", "受限的只是金融工具" in txt)


def test_14_demo_task_discipline():
    print("test 14: 演示任务纪律（回归：跑马灯不带股票）")
    from pathlib import Path as _P
    txt = (_P(__file__).resolve().parent.parent / "prompts" / "qd_system.txt").read_text(encoding="utf-8")
    check("演示任务禁金融工具", "禁止调用金融工具取真实数据填充" in txt)
    check("无市场任务不碰金融域", "一律不调用金融工具域" in txt)
    check("数字纪律范围界定", "虚构数字不受限" in txt)
    check("任务范围纪律置顶", txt.find("任务范围纪律") < txt.find("数字纪律"))


def test_16_meta_tools_mounted():
    print("test 16: 元工具必须挂进工具面（回归：web_search 只在元工具表，模型根本调不到）")
    import asyncio
    from qd_service import QDAgentService
    from tools.base import ToolProvider

    svc = QDAgentService(model=ScriptedModel([{"content": "ok"}]),
                         agent_config={"tools": []})
    asyncio.run(svc.chat("随便问一句", session_id="s16"))
    names = svc._get_agent("s16").tool_registry.list_tools()
    prov = ToolProvider.get_or_build()
    meta_names = set(prov.get_meta_functions())
    check("元工具已装配（非空）", bool(meta_names), str(meta_names))
    check("web_search 在工具面", "web_search" in names, str(names))
    check("format_result 在工具面", "format_result" in names, str(names))
    check("发现元工具已挂", {"list_tools", "search_tools", "activate_tools"} <= set(names),
          str(names))


def test_17_tool_tiering():
    print("test 17: 工具分层（按需域默认不下发，激活后才下发）")
    import asyncio
    from qd_service import QDAgentService

    svc = QDAgentService(model=ScriptedModel([{"content": "ok"}]),
                         agent_config={"tools": []})
    asyncio.run(svc.chat("hi", session_id="s17"))
    agent = svc._get_agent("s17")
    core = set(agent._core_names)
    on_demand = set(agent._on_demand_names)
    check("有按需层工具", bool(on_demand), str(sorted(on_demand))[:200])
    check("按需工具默认不下发", not (on_demand & set(agent.active_tool_names())),
          str(sorted(on_demand & set(agent.active_tool_names()))))
    check("web_search 属必注入层", "web_search" in core)
    kw = agent.get_model_query_kwargs()
    check("tools 走分层通道", len(kw.get("tools", [])) == len(agent.active_tool_names()))
    # 激活后进入下发清单
    target = sorted(on_demand)[0]
    res = agent.activate_tools([target])
    check("激活成功", res["activated"] == [target], str(res))
    check("激活后进入工具清单", target in agent.active_tool_names(), str(agent.active_tool_names()))
    # 容量上限 FIFO
    cap = agent.config.max_active_domain_tools
    agent.activate_tools(sorted(on_demand)[: cap + 5])
    check("激活数受上限约束", len(agent._activated) <= cap, f"{len(agent._activated)}>{cap}")


def test_18_activate_and_auto_activate():
    print("test 18: 激活通道（activate_tools 点名 + 未激活直调 fail-open）")
    import asyncio
    from qd_service import QDAgentService
    from tools.tool_discovery import search_tools, activate_tools

    svc = QDAgentService(model=ScriptedModel([{"content": "ok"}]),
                         agent_config={"tools": []})
    asyncio.run(svc.chat("hi", session_id="s18"))
    agent = svc._get_agent("s18")
    ctx = {"agent": agent}
    # 点名激活（用真实存在的按需工具名）
    target = sorted(agent._on_demand_names)[0]
    text = activate_tools(target, _context=ctx)
    check("点名激活生效", target in agent._activated and "新激活 1" in text, text[:200])
    # 未激活直接调用 → fail-open 就地激活放行（不抛 Unknown tool）
    before = set(agent._activated)
    agent2 = svc._get_agent("s18")
    other = sorted(set(agent2._on_demand_names) - before)
    if other:
        try:
            agent2.execute_action({"tool": other[0], "params": {}})
        except Exception:
            pass   # 工具本身可能报参数错，只要不是 Unknown tool 即可
        check("未激活直调被 fail-open 激活", other[0] in agent2._activated,
              f"{other[0]} not in {agent2._activated}")
    else:
        check("未激活直调被 fail-open 激活", True)
    # search_tools 能检索（无 agent 上下文时退化不激活）
    out = search_tools("资金流", count=3, _context=ctx)
    check("search_tools 有输出", isinstance(out, str) and len(out) > 10, str(out)[:120])


def test_19_availability_probe():
    print("test 19: 工具可用性探测（结果进系统提示）")
    import asyncio
    from qd_service import QDAgentService
    from tools.availability import probe_tools

    info = probe_tools()
    check("探测返回结构化结果", isinstance(info, dict) and "web_search" in info, str(info)[:200])
    ws = info["web_search"]
    check("web_search 探测含可用标记与原因",
          isinstance(ws.get("available"), bool) and bool(ws.get("reason")), str(ws)[:200])

    svc = QDAgentService(model=ScriptedModel([{"content": "ok"}]),
                         agent_config={"tools": [], "service_availability": {"search_knowledge": False}})
    asyncio.run(svc.chat("hi", session_id="s19"))
    agent = svc._get_agent("s19")
    text = agent._tool_availability_text()
    check("可用性写进系统提示变量", "web_search" in text and "search_knowledge" in text, text[:200])
    sys_msg = next(m["content"] for m in agent.messages if m.get("role") == "system")
    check("系统提示含可用性小节", "工具可用性" in sys_msg and "不可用" in sys_msg)


def test_20_prefetch_trace():
    print("test 20: 预取留痕（注入/丢弃都落 trace 事件）")
    import asyncio
    from types import SimpleNamespace
    from qd_service import QDAgentService

    class _Mem:
        async def add(self, sid, role, content):
            return True

        # 相关度闸门阈值=2（中文二元组命中数），这里要**够 2 个**才会被注入：
        # "茅台"+"现在" 命中 ⇒ injected；否则本用例测的就是丢弃路径。
        async def get_history(self, sid, limit=10):
            return [SimpleNamespace(role="user", content="贵州茅台现在的价位怎么看"),
                    SimpleNamespace(role="assistant", content="茅台现在维持震荡")]

    svc = QDAgentService(model=ScriptedModel([{"content": "ok"}]), memory=_Mem(),
                         agent_config={"tools": [], "tool_functions": [get_demo_quotes]})
    asyncio.run(svc.chat("茅台现在什么价", session_id="s20"))
    agent = svc._get_agent("s20")
    rep = getattr(agent, "last_prefetch_report", {}) or {}
    check("留痕报告含 blocks", bool(rep.get("blocks")), str(rep)[:200])
    check("记忆块被标记注入", "memory" in (rep.get("injected_kinds") or []), str(rep)[:200])
    check("报告含字符数", isinstance(rep.get("chars_total"), int) and rep["chars_total"] > 0)
    events = [__import__("json").loads(x)
              for x in agent.trace.path.read_text(encoding="utf-8").splitlines() if x.strip()]
    mine = [e for e in events if e.get("run_id") == agent.trace.run_id]
    check("prefetch 事件已落盘", any(e.get("event") == "prefetch" for e in mine),
          str({e.get("event") for e in mine}))


def test_21_tool_preselect():
    """工具预选：1 次 LLM 点名替代 search_tools→list_tools→activate_tools 三轮。

    覆盖三条红线：幻觉名必须丢弃、只做加法、fail-open（开关关掉即不预选）。
    """
    print("test 21: 工具预选（跨领域自适应，替代静态白名单）")

    class _SelModel:
        def __init__(self):
            self.seen = []
        def query(self, messages, **kwargs):
            self.seen.append(messages)
            return {"content": '["get_fund_flow", "不存在的幻觉工具"]'}
        def get_template_vars(self):
            return {}

    env = get_environment({"environment_class": "local"})
    model = _SelModel()
    agent = QDAgent(model, env, tool_functions=[get_demo_quotes])
    # 人为造一个按需层（真实场景由 _sync_tool_tiers 从 finance/knowledge 子域切出）
    agent._on_demand_names = ["get_fund_flow", "get_sector_fund_flow"]
    for n, desc in (("get_fund_flow", "获取个股资金流"),
                    ("get_sector_fund_flow", "获取板块资金流")):
        agent._defs_by_name[n] = {"function": {
            "name": n, "description": desc,
            "parameters": {"properties": {"stock_code": {}}}}}

    rep = agent._maybe_preselect_tools("帮我看看茅台的资金流")
    check("预选已启用", rep["enabled"] is True, str(rep))
    check("目录规模=按需层大小", rep["catalog_size"] == 2, str(rep))
    check("幻觉工具名被丢弃", rep["selected"] == ["get_fund_flow"], str(rep))
    check("点名工具已进首轮 schema", "get_fund_flow" in agent.active_tool_names(),
          str(agent.active_tool_names()))
    check("预选请求不带 tools（防递归调工具）",
          len(model.seen) == 1 and all(_preselect_marker() in str(m[0].get("content", ""))
                                       for m in model.seen))

    # 域特征闸门：天气/跑马灯这类问题不得触发预选（否则会被硬塞股票工具，实测 +136%）
    check("闸门拦住无关问题（天气）",
          agent._maybe_preselect_tools("今天北京天气怎么样")["reason"].find("闸门") >= 0,
          str(agent.last_preselect_report)[:160])
    check("闸门放行金融问题",
          agent._maybe_preselect_tools("分析603466的资金流")["reason"] == "ok")

    # fail-open：模型报错不应炸掉主流程（闸门会先拦，故关掉闸门再测）
    agent.config.preselect_gate = False

    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("网关 500")
        def get_template_vars(self):
            return {}
    agent.model = _Boom()
    rep2 = agent._maybe_preselect_tools("再看一个")
    check("预选异常 fail-open 不抛错", rep2["selected"] == [] and "fail-open" in rep2["reason"],
          str(rep2)[:200])

    # 开关关闭
    agent.config.tool_preselect = False
    rep3 = agent._maybe_preselect_tools("再看一个")
    check("开关关闭后不预选", rep3["selected"] == [] and "已关闭" in rep3["reason"], str(rep3)[:160])


def test_22_context_window_single_source():
    """上下文窗口：compaction 与 wrap_up 必须同源，且阈值要为压缩留出余量。

    回归 2026-10-01 首版修复的坑：两个窗口写成两个 env 名（QD_COMPACT_WINDOW /
    QD_CONTEXT_WINDOW），只改一个会打架，页脚分母和催收阈值对不上。
    """
    print("test 22: 上下文窗口单一口径 + 阈值留余量")
    from qd_agent import QDAgentConfig, _context_window
    cfg = QDAgentConfig()
    win = cfg.compaction_context_window
    thr = cfg.compaction_threshold_tokens
    check("窗口>0", isinstance(win, int) and win > 0, str(win))
    check("compaction 与 wrap_up 同源", win == cfg.wrap_up_hint_context_window,
          f"{win} vs {cfg.wrap_up_hint_context_window}")
    check("阈值已显式计算（不是 mimo 的 None）", isinstance(thr, int) and thr > 0, str(thr))
    check("阈值明显低于窗口（压缩要留余量）", thr < win * 0.95, f"{thr}/{win}")
    check("_context_window 与配置一致", _context_window() == win, f"{_context_window()} vs {win}")


def test_23_preselect_lint():
    """预选的确定性校正层（对齐旧系统 plan_linter 的 R1/R4）。

    LLM 选得对不对**不由 LLM 判**：能被词典/索引查出来的不劳 LLM。
    旧系统真正的稳定性来源就在这里，首版缺失它 ⇒ 预选方差大于效应。

    三态必验：
      R1 补位——任务确有某域需求但工具面没覆盖 → 补首选工具（宁多不可断）
      R4 裁剪——与 plan 自述目标零相关 → 裁（保留面 >= MIN_FACE）
      R4b 作废——几乎全零相关且词典也无需求 → 整体作废（天气 +136% 的治法）
    """
    from tools.tool_preselect import (
        lint_selection, apply_lint, detect_domains, check_domain_dict,
        get_tool_index, DATA_DOMAINS, MIN_FACE,
    )
    from tools.base import ToolProvider

    provider = ToolProvider.get_or_build()
    names = list(provider.get_functions())
    defs = {}
    for n in names:
        fn = provider.get(n)
        defs[n] = {"function": {
            "name": n, "description": ((getattr(fn, "__doc__", "") or n)[:120]),
            "parameters": {"properties": {}}}}
    idx = get_tool_index(defs)

    # ── 词典自检：词典引用的工具名必须逐字存在于注册表 ──
    # 旧系统教训：词典与注册表两处漂移 ⇒ 词典失效无人报警（plan_linter 头部易错点）
    stale = check_domain_dict(names)
    check("词典工具名 ⊆ 注册表（防词典陈旧）", stale == [], str(stale))
    check("词典非空且每域有首选", len(DATA_DOMAINS) >= 10
          and all(len(t) >= 1 for _d, _k, t in DATA_DOMAINS), str(len(DATA_DOMAINS)))

    def run(q, sel, plan_text, parsed=True):
        rep = lint_selection(q, sel, available_names=names, index=idx,
                             plan_text=plan_text, parsed=parsed)
        return rep, apply_lint(sel, rep)

    # R4b：天气任务被硬塞 8 个股票工具（首版实测 30k→70.9k，+136% 的成因）
    bad8 = ["get_realtime_quote", "get_fund_flow", "resolve_stock", "technical_analysis",
            "search_stocks", "get_market_overview", "analyze_trend", "get_chip_distribution"]
    rep, out = run("今天北京天气怎么样", bad8, "查询北京今日天气 温度 风力 空气质量")
    check("R4b 误判作废：天气被塞股票工具 → 清空", rep.misselected and out == [],
          f"misselected={rep.misselected} out={out}")

    # R1：资金流任务只选了行情工具 → 补 get_fund_flow（宁多不可断）
    rep, out = run("帮我看看茅台的资金流", ["get_realtime_quote"],
                   "获取贵州茅台主力资金净流入 近5日资金流")
    check("R1 覆盖补位：缺资金流工具 → 补上", "get_fund_flow" in out,
          f"additions={rep.additions} out={out}")

    # R4：选股任务正常，不得误裁
    rep, out = run("帮我选几只强势股", ["search_stocks", "get_hot_stocks_with_reason"],
                   "筛选当前强势个股 代码 名称 涨幅 入选理由")
    check("R4 不误杀：正常选股工具保留",
          all(t in out for t in ("search_stocks", "get_hot_stocks_with_reason"))
          and not rep.misselected, f"out={out} pruned={rep.pruned}")

    # 保守原则：plan 未解析（模型只回裸数组）→ 无语义基准，不得裁
    rep, out = run("帮我看看茅台的资金流", ["get_realtime_quote"], "", parsed=False)
    check("无语义基准时保守不裁", rep.pruned == [] and not rep.misselected,
          f"pruned={rep.pruned} misselected={rep.misselected}")

    # 域识别本身的合理性
    check("域识别：资金流→fundflow", "fundflow" in detect_domains("看看资金流"),
          str(detect_domains("看看资金流")))
    check("域识别：无关问题→空", detect_domains("今天天气怎么样") == [],
          str(detect_domains("今天天气怎么样")))
    check("MIN_FACE 保底已设置", isinstance(MIN_FACE, int) and MIN_FACE >= 1, str(MIN_FACE))


def test_24_tool_grading():
    """工具分级（2026-10-01 用户裁定口径）——三级，且**域内优先于能力层**。

      一级 必选：tools/ 顶层（common）+ mimo 原生 + 元工具 → 每轮全量下发
      二级 域内：tools/<子目录>（finance/knowledge…）→ 按任务筛选后激活
      三级 能力层：capabilities/ 准入函数 → 域内无同功能工具时才用

    本用例专治两个真问题：
      ① 管线函数被当工具——tool_preselect 的公开函数是给代码 import 的，
         不是给模型调的；此前它们全部进了必选层（27 个里占 13 个）。
      ② 能力层"有实现没接线"——register_capabilities 无调用点，且 admission
         缺失 ⇒ 能力层永不参与筛选。此处用**合成能力**验证让位与排序，
         免得接线后才发现规则是死的。
    """
    from tools.base import ToolProvider
    from tools.tool_preselect import (
        build_catalog_grouped, capability_domain, lint_selection, apply_lint,
        CAP_SECTION_NOTE, _NOT_TOOLS,
    )

    provider = ToolProvider.get_or_build()
    common = set(provider.list_by_domain("common"))

    # ① 必选层里不得出现管线函数（它们是代码内部调用面，不是工具面）
    #    ★ 判据用「模块实际公开函数」而不是 _NOT_TOOLS 名单：后者是人写的，
    #      新增函数忘了登记时它会一起漏——那就测不出来了（自己验自己）。
    import inspect
    from tools import tool_preselect as _tp
    pub = {n for n, o in vars(_tp).items()
           if inspect.isfunction(o) and getattr(o, "__module__", "") == _tp.__name__
           and not n.startswith("_")}
    leaked = sorted(common & pub)
    check("必选层不含预选/lint 管线函数", not leaked, f"leaked={leaked}")
    check("管线模块公开面已全部登记 _NOT_TOOLS", (pub - set(_NOT_TOOLS)) == set(),
          f"未登记={sorted(pub - set(_NOT_TOOLS))}")
    check("必选层仍保留真工具（发现/记忆/文件）",
          {"search_tools", "list_tools", "activate_tools", "read_text", "recall"} <= common,
          str(sorted(common)))
    check("能力层来源域名一致", capability_domain() == "capability", capability_domain())

    # ② 能力层为空 ⇒ 目录不生成第二段（零 token、零幻觉空间）
    dom_names = sorted(provider.list_by_domain("finance"))[:5]
    defs = {n: {"function": {"name": n, "description": f"{n} 说明",
                             "parameters": {"properties": {}}}} for n in dom_names}
    txt, kept = build_catalog_grouped(defs, dom_names, [])
    check("能力层为空时不分第二段",
          CAP_SECTION_NOTE not in txt and kept == dom_names, txt[:80])

    # ③ 合成能力层：与域内工具近重名 ⇒ R0 让位（域内优先）
    cap = "get_fund_flow_daily"        # 与域内 get_fund_flow 同功能
    defs[cap] = {"function": {"name": cap, "description": "资金流日报（能力层）",
                              "parameters": {"properties": {}}}}
    dom_all = dom_names + ["get_fund_flow"]
    defs["get_fund_flow"] = {"function": {
        "name": "get_fund_flow", "description": "获取个股资金流",
        "parameters": {"properties": {}}}}
    txt2, kept2 = build_catalog_grouped(defs, dom_all, [cap])
    check("有能力层时目录分两段且域内在前",
          CAP_SECTION_NOTE in txt2 and txt2.index("get_fund_flow") < txt2.index(cap),
          txt2[:120])

    sel = ["get_realtime_quote", cap]
    rep = lint_selection("看看茅台的资金流", sel, available_names=list(defs),
                         plan_text="获取贵州茅台主力资金净流入",
                         capability_names=[cap], domain_names=dom_all)
    out = apply_lint(sel, rep, capability_names=[cap])
    check("R0 能力层让位：域内有同功能工具 → 裁掉能力层",
          cap not in out and any(x["capability"] == cap for x in rep.capability_yield),
          f"yield={rep.capability_yield} out={out}")

    # ④ 排序：能力层**未被让位**时（域内确无同功能工具），仍排域内之后
    cap2 = "northbound_flow_daily"       # 域内无同功能工具 ⇒ 保留，但排最后
    defs[cap2] = {"function": {"name": cap2, "description": "北向资金日报（能力层）",
                               "parameters": {"properties": {}}}}
    sel2 = [cap2, "get_fund_flow"]
    rep2 = lint_selection("看看茅台的资金流", sel2, available_names=list(defs),
                          plan_text="获取贵州茅台主力资金净流入",
                          capability_names=[cap, cap2], domain_names=dom_all)
    out2 = apply_lint(sel2, rep2, capability_names=[cap, cap2])
    check("工具面排序：域内工具在能力层之前（能力层保留但垫底）",
          out2 == ["get_fund_flow", cap2], f"out={out2}")

    # ⑤ 能力层**真的接上了线**（2026-10-01 复制 admission.json 后补）
    #    此前 register_capabilities 有实现无调用方 + admission.json 不在仓库
    #    ⇒ 能力层双重死代码。这里锁死：有准入就必须能注册，且不得进必选层。
    from qd_agent import CORE_TOOL_DOMAINS
    from capabilities.loader import load_admitted_meta
    from capabilities import register_capabilities
    check("能力层不在必选层（三级而非一级）",
          "capability" not in CORE_TOOL_DOMAINS, str(CORE_TOOL_DOMAINS))
    admitted = load_admitted_meta()
    if admitted:
        register_capabilities(provider)
        caps_now = provider.list_by_domain("capability")
        check("有准入 ⇒ 能力层已注册进 provider", len(caps_now) > 0,
              f"admitted={len(admitted)} registered={len(caps_now)}")
        check("域内优先：同名/近重名的能力已让位（未满额注册）",
              len(caps_now) < len(admitted),
              f"admitted={len(admitted)} registered={len(caps_now)}")
    else:
        check("无准入清单 ⇒ 能力层为空且不报错（fail-open）",
              provider.list_by_domain("capability") == [],
              str(provider.list_by_domain("capability")))


def test_25_toolface_preload():
    """工具面预加载三改（2026-10-01，对齐旧系统「启动时筛一次 + 绝不裸工具面」）。

    旧系统两处设计在迁移中被丢掉，本用例逐条锁回：
      ① **启动期预热**：旧 `agent.py` 模块级 `NodeContext(llm).init_tools()`。
         迁移后变懒加载 ⇒ 实测 3.1s 冷路径压在首条消息上。现由 `warmup_tool_face()`
         在装配期跑掉，且幂等（重复调用不重复扫描）。
      ② **能力层注册幂等**：`_maybe_register_capabilities` 挂在 agent 构造路径上，
         每个会话都重跑一遍注册 + 刷一遍 29 项让位日志。现按 provider 只做一次。
      ③ **域兜底（防裸工具面）**：旧 `task_agent.py` P0 断言——域选择失效绝不能降成
         "只有通用工具"的裸沙箱（09-19 故障机制）。预选空手时按域词典补核心子集，
         纯字符串匹配、零 LLM；**命中不了就一个不补**（天气问题不许被硬塞股票工具）。
    """
    from tools.base import ToolProvider
    from tools.tool_preselect import fallback_domain_tools
    from qd_agent import warmup_tool_face, _ensure_capabilities, QDAgent

    # ① 预热：done + 工具数 > 0，且重复调用不重扫（elapsed 沿用首次）
    w1 = warmup_tool_face()
    check("工具面预热完成", bool(w1.get("done")) and int(w1.get("tools", 0)) > 0, str(w1))
    w2 = warmup_tool_face()
    check("预热幂等（重复调用不重复扫描）",
          w2.get("tools") == w1.get("tools") and w2.get("elapsed") == w1.get("elapsed"),
          f"{w1.get('elapsed')} → {w2.get('elapsed')}")

    provider = ToolProvider.get_or_build()

    # ② 能力层按 provider 幂等：第二次直接返回缓存数且不新增工具
    n1 = _ensure_capabilities(provider)
    before = len(provider.list_by_domain("capability"))
    n2 = _ensure_capabilities(provider)
    after = len(provider.list_by_domain("capability"))
    check("能力层注册幂等（第二次不重复注册）",
          n1 == n2 and before == after and getattr(provider, "_qd_caps_registered", False),
          f"n1={n1} n2={n2} caps {before}→{after}")

    # ③ 域兜底：金融问法补、非金融问法一个不补
    dom_names = [n for n in provider.list_by_domain("finance")]
    check("域内候选非空（否则兜底无从补起）", len(dom_names) > 0, str(len(dom_names)))
    fb_mkt, doms_mkt = fallback_domain_tools("帮我看看大盘现在怎么样", dom_names)
    check("域兜底：大盘问法命中 market 域并补到工具",
          doms_mkt and doms_mkt[0] == "market" and fb_mkt
          and set(fb_mkt) <= set(dom_names), f"{doms_mkt} → {fb_mkt}")
    fb_ff, doms_ff = fallback_domain_tools("查一下这只股票的资金流向", dom_names)
    check("域兜底：资金流问法命中并补到工具",
          "fundflow" in doms_ff and fb_ff and set(fb_ff) <= set(dom_names),
          f"{doms_ff} → {fb_ff}")
    for q in ("今天北京天气怎么样", "写个跑马灯页面"):
        fb_q, doms_q = fallback_domain_tools(q, dom_names)
        check(f"域兜底克制：'{q}' 一个不补", not fb_q and not doms_q, f"{doms_q} → {fb_q}")

    # ④ 端到端：闸门强制拦截时（hints 置空）仍不许落回裸工具面
    class _Stub:
        def query(self, *a, **k):
            raise RuntimeError("预选不应在本用例中触发 LLM")
    ag = QDAgent(_Stub(), None)
    ag.config.preselect_gate = True
    ag.config.preselect_hints = []          # 强制闸门拦截（不发 LLM）
    rep = ag._maybe_preselect_tools("帮我看看大盘现在怎么样")
    check("闸门拦截后仍由域兜底补出工具面（不落回裸必选层）",
          bool(rep.get("selected")) and "fallback" in rep, str(rep.get("reason", "")))
    ag2 = QDAgent(_Stub(), None)
    ag2.config.preselect_gate = True
    ag2.config.preselect_hints = []
    rep2 = ag2._maybe_preselect_tools("今天北京天气怎么样")
    check("闸门拦截 + 非领域问题 ⇒ 不补、不激活（零膨胀）",
          not rep2.get("selected") and not ag2._activated, str(rep2.get("reason", "")))


def test_26_accountability_v11():
    """追责系统 v1.1（2026-10-01）：闸门 / Claim 提取 / 判定锚点 / 多域留位。

    用户三条修正逐条锁死：
      ① **不追责的不入库**——闸门在写库之前（天气/纯查询 ⇒ tracked=False，
         一行都不写 decisions），而不是"写进去再标 claim_count=0"。
      ② **慢调、容忍少量误判**——代码锚点放宽（方向对即 hit 0.75，不因幅度差
         判 miss）；confidence 抽不到一律 **None**（绝不填 0.5，旧表校准曲线
         就是被这个常量毒化的）。
      ③ **多域通用留位**——域开关来自**配置**，把 finance 关掉后同样的问题
         产出 0 条 claim ⇒ 证明调用方没有 `if domain == 'finance'` 硬编码。
    """
    from chain import claims as C
    from chain.resolver import compute_deviation, verdict_by_rule

    # ① 闸门：金融放行，天气/纯查询拦下
    q_fin = "帮我看看贵州茅台600519接下来三天怎么走"
    a_fin = ("结论：偏空。当前不具备强势上行基础，向上突破难度较大，"
             "预计回调 3% 左右，支撑 1400 元。")
    check("闸门放行金融预测", C.intake_gate(q_fin, a_fin, "finance")[0], "")
    ok_weather, why_weather = C.intake_gate(
        "今天北京天气怎么样", "北京今天晴，气温 22 度，适宜出行，空气质量良。")
    check("闸门拦下天气（不入库）", not ok_weather, why_weather)
    ok_data, why_data = C.intake_gate(
        "查一下茅台现在多少钱", "当前价格 1680 元，今日成交额约 30 亿元，换手 0.8%。")
    check("闸门拦下纯查询（不入库）", not ok_data, why_data)
    check("闸门拦下空答复", not C.intake_gate(q_fin, "", "finance")[0], "")

    # ② 提取：方向必须**正确**（否定句回归）
    cs = C.extract_claims(q_fin, a_fin, "finance")
    dirs = [c for c in cs if c["claim_type"] == "direction"]
    check("抽到 direction claim 且方向为 bearish（否定句不误判）",
          dirs and dirs[0]["predicted"]["dir"] == "bearish",
          str([c["predicted"] for c in cs]))
    check("confidence 抽不到 ⇒ None（不填 0.5）",
          all(c["confidence"] is None for c in cs),
          str([c["confidence"] for c in cs]))
    check("大盘走 subject 而非空（只换 subject 不换字段）",
          any(c["subject"] == "000001.SH" and c["subject_kind"] == "index"
              for c in C.extract_claims("帮我看看大盘",
                                        "综合判断：偏多震荡，上证指数有望突破 3200 点。",
                                        "finance")),
          "")
    check("抽不出标的 ⇒ 不产 claim（宁漏不脏）",
          C.extract_claims("帮我分析一下", "结论：偏多。该标的量能配合，可看高一线。",
                           "finance") == [], "")

    # ③ 判定锚点放宽（修正②）：方向对即 hit，幅度差只记偏差不降级
    act = {"pct": -1.35, "dir": "bearish", "high": 1285.53, "low": 1254.1,
           "close": 1258.0, "as_of": None, "source": "test"}
    c_dir = {"claim_type": "direction", "predicted": {"dir": "bearish"}}
    dev = compute_deviation(c_dir, act)
    v = verdict_by_rule(c_dir, act, dev)
    check("方向对 ⇒ hit（不因幅度差判 miss）",
          v["verdict"] == "hit" and v["verdict_score"] >= 0.7, str(v))
    c_mag = {"claim_type": "magnitude", "predicted": {"pct": -3.0}}
    dev_m = compute_deviation(c_mag, act)
    v_m = verdict_by_rule(c_mag, act, dev_m)
    check("幅度误差 0.55 在容差内 ⇒ hit（慢调放宽）",
          v_m["verdict"] == "hit" and dev_m["magnitude"]["err_rel"] == 0.55,
          f"{v_m} dev={dev_m}")
    wrong = {"claim_type": "direction", "predicted": {"dir": "bullish"}}
    check("方向反 ⇒ miss", verdict_by_rule(wrong, act, compute_deviation(wrong, act))
          ["verdict"] == "miss", "")

    # ④ 多域留位（修正③）：关掉 finance ⇒ 立刻 0 条（证明走配置而非硬编码）
    pol_off = {"finance": {"enabled": False, "horizon_default": "T+3",
                           "judge_enabled": True}}
    check("域开关来自配置：关闭 finance ⇒ 不产 claim",
          C.extract_claims(q_fin, a_fin, "finance", policy=pol_off) == []
          and not C.intake_gate(q_fin, a_fin, "finance", pol_off)[0], "")
    check("内置策略只有 finance 启用（v1 范围）",
          [d for d, c in C.DOMAIN_POLICY.items() if c.get("enabled")] == ["finance"],
          str(list(C.DOMAIN_POLICY)))

    # ⑤ 端到端入库（有 DB 才验；无 DB 不影响主结论）
    try:
        from chain.intake import record_decision
        r_fin = record_decision(user_query=q_fin, answer=a_fin, session_id="smoke26")
        r_weather = record_decision(user_query="今天北京天气怎么样",
                                    answer="北京今天晴，气温 22 度，适宜出行。",
                                    session_id="smoke26")
        if r_fin.get("reason", "").startswith("error"):
            print("  [SKIP] DB 不可用，跳过入库端到端断言")
        else:
            # ★ 别写 `a and b`：b 是 list 时整个表达式是 list，`sum(PASS)` 会炸
            check("金融决策已入库并带 claim",
                  bool(r_fin.get("tracked")) and bool(r_fin.get("claim_ids")),
                  str(r_fin))
            check("不追责的**一行都没写库**",
                  not r_weather.get("tracked") and r_weather.get("decision_id") is None,
                  str(r_weather))
            _cleanup_accountability_rows()
    except Exception as e:
        print(f"  [SKIP] 入库端到端不可用: {type(e).__name__}")


def _cleanup_accountability_rows() -> None:
    """★ smoke 端到端写的是**真实库**，跑完必须自清。

    遗留修复（2026-10-01）：test_26 的 record_decision 直连生产库写入后会一直
    留在表里（实测残留 id=8,9 两条 `smoke26`），把 health 度指标（decisions /
    claim_rate）算成虚高，且没人看得出那是测试数据。CJK：清理按 session_id
    定向删，claims/resolutions 走外键级联。
    """
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM qd_agent_decisions WHERE session_id = %s",
                        ("smoke26",))
            n = cur.rowcount
            conn.commit()
            if n:
                print(f"  [CLEANUP] 已清理 smoke 写入的 {n} 条测试决策")
    except Exception as e:
        print(f"  [CLEANUP] 跳过: {type(e).__name__}: {str(e)[:60]}")


def test_27_reset_wiring_observation():
    """S2/S4 复位入口 + 盘后追责接线 + 两个观测计数器（2026-10-01）。

    为什么要有这个用例：
      * 追责链此前**没接进盘后 worker** ⇒ intaker 写的 claims 永远停在 pending，
        闭环是断的（2026-10-01 补全）。这里锁"接线存在"，防再次断链。
      * 复位是**破坏性**操作，必须保证默认 dry_run，否则一次误触就清空全表。
      * 观测计数器（`preselect_stats` / `judge_stats`）是"先量化再决定要不要救"
        的载体，锁住它的归类与阈值逻辑不退化。
    """
    # ① 复位入口：默认必须是 dry_run（防破坏）
    try:
        from chain import reset as R
        d = R.reset_accountability(dry_run=True)
        check("追责复位默认 dry_run（不真删）",
              d.get("dry_run") is True and not d.get("deleted"), str(d)[:80])
        w = R.reset_weights(dry_run=True)
        check("权重复位默认 dry_run", w.get("dry_run") is True and w.get("rows", 0) >= 0,
              str(w)[:80])
        snap = R.snapshot()
        check("快照可读且不炸", isinstance(snap.get("tables"), dict), str(snap)[:80])
    except Exception as e:
        check("复位模块可导入", False, f"{type(e).__name__}: {e}")
        return

    # ② 盘后接线：auto_evaluate 必须调用 v4 追责（否则闭环断）
    #    用源码 + 返回值双保险：源码锁"写了"，返回键锁"跑到了"
    import inspect
    from chain import evaluator as EV
    src = inspect.getsource(EV.auto_evaluate)
    check("盘后 auto_evaluate 已接 v4 追责（防断链回归）",
          "resolve_due_claims" in src, "auto_evaluate 源码无 resolve_due_claims")
    check("追责失败不影响旧链路（独立 try/except）",
          "不影响旧链路" in src, "缺兜底 try")
    health = EV.get_worker_health()
    check("worker 健康视图含追责字段", "last_accountability" in health, str(list(health)))

    # ③ 空域观测：归类稳定 + 比例计算正确
    from tools import preselect_stats as PS
    PS.reset()
    for _ in range(9):
        PS.record_selected("ok")
    r = PS.record_empty_face("帮我看看大盘", "无领域特征，跳过预选(闸门)", 100)
    check("空域占比计算正确(1/10)", abs(r["rate"] - 0.1) < 1e-9, str(r["rate"]))
    rep = PS.report()
    check("空域样本不足时不下结论（提示继续观测）",
          "样本不足" in rep["verdict"], rep["verdict"])
    PS.record_selected("ok")             # 凑到 >=50 之外仍应提示样本不足(<50)
    for _ in range(60):
        PS.record_selected("ok")
    check("样本足够后按阈值给结论", "健康" in PS.report()["verdict"], PS.report()["verdict"])

    # ④ 空手原因归类稳定（改文案不能断统计）
    from qd_agent import QDAgent
    check("_empty_route 归类：闸门",
          QDAgent._empty_route({"reason": "无领域特征，跳过预选(闸门)"}) == "gate", "")
    check("_empty_route 归类：模型未点名",
          QDAgent._empty_route({"reason": "模型未点名(可能是纯问答)"}) == "no_pick", "")
    check("_empty_route 归类：lint 作废",
          QDAgent._empty_route({"reason": "lint 判定整体误判，已作废"}) == "lint_void", "")
    check("_empty_route 认不出时不猜（unknown）",
          QDAgent._empty_route({"reason": "某种全新文案"}) == "unknown", "")

    # ⑤ judge 观测：默认未启用时应明确提示，且一致率口径正确
    from chain import judge_stats as JS
    JS.reset()
    j0 = JS.report()
    check("judge 未启用时给出明确结论（而非静默 0）",
          "未启用" in j0["verdict"], j0["verdict"])
    for i in range(10):
        JS.record_call(); JS.record_result(ok=True, agreed=(i < 8), claim_type="level")
    j1 = JS.report()
    check("judge 分歧率口径正确(2/10)", abs(j1["disagree_rate"] - 0.2) < 1e-9,
          str(j1["disagree_rate"]))
    check("judge 分歧按类型分组", j1["disagree_by_type"].get("level") == 2,
          str(j1["disagree_by_type"]))
    JS.reset()
    print("  [info] 处置备忘: " + PS.DECISION_NOTE.strip().splitlines()[1].strip()[:40])


def _cleanup_trace_rows(base_id: int = 0) -> None:
    """★ smoke 端到端写的是**真实库**，跑完应自清。

    ⚠️ 2026-10-01 安全修正：**绝不能再按 `session_id=''` 删**。
    旧假设写在这段注释里——"真实会话恒有 session_id，不会被误删"——**是错的**：
    `EvalNode` 根节点一直没下传 session_id（`TraceCollector._finish` 漏传），
    所以真实会话写进 `qd_agent_traces` 的 session_id **同样是空**。实测拿到
    22:03~22:13 的真实提问（"分析300497"/"节后开盘买什么股?"）session_id 为空，
    照旧判据删 ⇒ **会把用户的真实提问记录一并删掉**。

    现在的判据改为**显式测试标记**：只删 session_id = `__smoke__` 的行。
    smoke 尚未给会话打该标记 ⇒ 本次不会删任何东西（宁可留脏数据，不可删真实数据）。
    彻底方案是让 smoke 跑专用会话标记（或隔离到测试库），见 TODO。
    """
    _SMOKE_TAG = "__smoke__"
    # ★ base_id=0 是合法值（表刚清空过），不能写成 `if not base_id: return`
    if base_id is None or base_id < 0:
        return
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM qd_agent_traces WHERE id > %s AND session_id = %s",
                (base_id, _SMOKE_TAG))
            n = cur.rowcount
            conn.commit()
            if n:
                print(f"  [CLEANUP] 已清理 smoke 写入的 {n} 行 qd_agent_traces")
            else:
                cur.execute(
                    "SELECT count(*) AS n FROM qd_agent_traces WHERE id > %s", (base_id,))
                left = (cur.fetchone() or {"n": 0})["n"]
                if left:
                    print(f"  [CLEANUP] 本轮写入 {left} 行未清理：未打 __smoke__ 标记，"
                          f"按安全策略保留（避免误删真实会话）")
    except Exception as e:
        print(f"  [CLEANUP] 跳过: {type(e).__name__}: {str(e)[:60]}")


def _trace_baseline() -> int:
    """smoke 启动前的 traces 最大 id（自清基线）。失败返回 -1（=不自清，不误删）。"""
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            # ★ 必须起别名：游标行是 RealDictRow，`COALESCE(...)` 无别名时列名是
            #   "coalesce"，按 ["c"] 取会 KeyError ⇒ 基线取不到 ⇒ 自清被跳过。
            cur.execute("SELECT COALESCE(MAX(id), 0) AS c FROM qd_agent_traces")
            v = int((cur.fetchone() or {"c": 0})["c"] or 0)
            cur.close()
            return v
    except Exception:
        return -1


def test_28_closed_loop_consumers():
    """闭环消费端 + resolvers 接线 + formatters 补回（2026-10-01）。

    锁三件事，都是"写了但没接线"这一类事故的高发区：
      ① 权重表必须**有消费方**（此前 resolutions 无人喂、权重无人读 ⇒ 断头路）；
      ② resolvers/ 必须真的跑起来（此前是死代码，标的消歧从未生效）；
      ③ formatters/ 的注册表必须真的注册上（旧系统曾因漏 import 恒返回兜底）。
    """
    # ① 权重消费端：快照可读、低权重判定、提示段可生成
    try:
        from chain import weight_hints as WH
        WH.reset()
        snap = WH.snapshot(force=True)
        check("权重快照含四层(skill/tool/chain/domain)",
              all(k in snap for k in ("skill", "tool", "chain", "domain")), str(list(snap)))
        rep = WH.report()
        check("权重自检报告可读（含阈值与低权重名单）",
              "threshold" in rep and "low" in rep, str(rep)[:80])
        # 低权重判定：造一个假快照验证阈值生效（不写库）
        WH._cache["tool"] = {"good_tool": 0.95, "bad_tool": 0.30}
        low = WH.low_weight("tool")
        check("低权重名单按阈值筛出（升序，最差在前）",
              low == ["bad_tool"], str(low))
        check("权重提示只提示不过滤（低权重仍在候选里）",
              "bad_tool" in (WH.hint_text(["good_tool", "bad_tool"]) or ""), "")
    except Exception as e:
        check("权重消费端可导入", False, f"{type(e).__name__}: {e}")

    # ①b 追责→权重喂数：dry_run 必须不写库
    try:
        from chain import weight_feed as WF
        r = WF.feed(dry_run=True)
        check("追责→权重 dry_run 不写库",
              r.get("updated") == {} and "would_update" in r, str(r)[:80])
        check("喂数最小样本阈值存在（慢调纪律）", WF.MIN_SAMPLES >= 1,
              str(WF.MIN_SAMPLES))
    except Exception as e:
        check("追责→权重模块可导入", False, f"{type(e).__name__}: {e}")

    # ② resolvers 接线：标的解析 + 歧义反问（防错的核心）
    try:
        from resolvers.bridge import resolve, context_block
        i1 = resolve("帮我分析一下贵州茅台")
        check("resolvers 已接线：口语问法能解析出标的",
              i1.get("ran") and i1.get("entity_code") == "600519",
              f"{i1.get('entity_code')} / ran={i1.get('ran')}")
        i2 = resolve("写个跑马灯页面")
        check("非金融问题不误解析出标的",
              not i2.get("entity_code"), str(i2.get("entity_code")))
        check("实体块可渲染（有解析结果时非空）",
              "贵州茅台" in context_block(i1), context_block(i1)[:60])
        check("无实体时上下文块为空（不占位）", context_block(i2) == "", "")
    except Exception as e:
        check("resolvers 桥接可导入", False, f"{type(e).__name__}: {e}")

    # ③ 【2026-10-01 摘除】formatters 已从主链路下線，改为**延迟护栏**
    # 背景：formatter 每次答复多发一整轮 LLM（实测稳态 11.7s / 首次 21.9s），
    #   而它只是"整理排版"，失败可用原文 ⇒ 用户裁定去掉，实现归档在
    #   del/agent_formatters_20261001/。这里断言它**没被悄悄接回来**。
    try:
        import os as _os
        fmt_dir = _os.path.join(_os.path.dirname(_os.path.dirname(
            _os.path.abspath(__file__))), "formatters")
        check("formatters 不在生产包内（已归档到 del/）",
              not _os.path.isdir(fmt_dir), fmt_dir)
        src = open(_os.path.join(_os.path.dirname(_os.path.dirname(
            _os.path.abspath(__file__))), "qd_agent.py"), encoding="utf-8").read()
        # 只算带 cusumr 调用的那种出现（注释里会提到旧名，不算接线）
        wired = [l for l in src.splitlines()
                 if "_maybe_format_result(" in l and not l.strip().startswith("#")]
        check("主链路已无 formatter 调用（防延迟回归）", not wired, str(wired)[:120])
        # 归档在**项目根** del/（约定：删除一律 shutil.move 到根 del/，不硬删）
        _d = _os.path.abspath(__file__)
        for _ in range(5):      # scripts → agent → app → backend_api_python → 项目根
            _d = _os.path.dirname(_d)
        archived = _os.path.isdir(_os.path.join(_d, "del", "agent_formatters_20261001"))
        check("实现已归档（可回滚，不是硬删）", archived, "")
    except Exception as e:
        check("formatters 摘除护栏可用", False, f"{type(e).__name__}: {e}")

    # ④ 技能选择：金融问法命中 / 跑马灯不得注入（文不对题回归防线）
    try:
        from qd_service import _skill_score, _FINANCE_GATE
        keys_f = {w for w in "帮我分析一下贵州茅台"
                  if False} | {"分析", "茅台", "贵州"}
        desc_ana = "对单只a股标的开展多周期技术面、资金面、基本面综合诊断".lower()
        sc_entity = _skill_score(desc_ana, keys_f, "帮我分析一下贵州茅台", True)
        check("有标的时个股类技能拿到范围加分", sc_entity >= 3, str(sc_entity))
        desc_scr = "从a股全市场筛选短线标的".lower()
        sc_noent = _skill_score(desc_scr, set(), "写个跑马灯页面", False)
        check("非金融问题不给范围加分（防误注入）", sc_noent == 0, str(sc_noent))
        check("金融闸门命中金融问法", bool(_FINANCE_GATE.search("今天买什么股票好")), "")
    except Exception as e:
        check("技能打分函数可用", False, f"{type(e).__name__}: {e}")


def test_29_module_single_instance():
    """M-1 防复发护栏：agent 包子模块不得被加载成两份。

    事故回放：`app/__init__.py` / `app/agent/__init__.py` 把 `app/agent` 插进
    sys.path，但代码里裸名与全名混用 ⇒ 同一份源码被加载成 `agent` 与
    `app.agent.agent` 两个 module 对象 —— 模块级单例（QDAgentService / 会话表 /
    TraceCollector 收集器 / weight_hints 缓存 / capabilities 缓存）全部双份且
    状态互不可见。

    现行约定：**统一裸名**（`from chain.store import ...`）。理由是全名把包名硬编码
    进 300+ 处 import，移动/重命名目录时移植成本极高；裸名只依赖 `__file__` 相对
    计算的一处 bootstrap。因此判据与长名方案**相反**，共五条：
      ① sys.path 必须含 app/agent（裸名可解析的前提）
      ② sys.modules 不得出现 `app.agent.*` 子模块长名键（出现=混用=双份）
      ③ 裸名必须真的解析到 app/agent 下（防 `utils` 命中 app/utils）
      ④ 同一文件不得对应多个 module 名
      ⑤ 重复 import 拿到同一 module 对象
    """
    print("\n[test_29] 模块单实例（M-1 防复发·裸名方案）")
    _agent_dir = os.path.normcase(os.path.abspath(str(AGENT_DIR)))

    _on_path = [p for p in sys.path
                if p and os.path.normcase(os.path.abspath(str(p))) == _agent_dir]
    check("app/agent 目录在 sys.path（裸名导入的前提）", bool(_on_path), str(sys.path[:3]))

    # `app.agent` 包本身允许存在（bootstrap 会 import 它，其 __init__ 无状态），
    # 但任何 `app.agent.<子模块>` 长名键都意味着两种写法混用 ⇒ 必然双份。
    _long_mod = [n for n in sys.modules
                 if n.startswith("app.agent.") and n != "app.agent"]
    check("sys.modules 无 app.agent.* 子模块长名键（防裸名/全名双份）",
          not _long_mod, ",".join(sorted(_long_mod)[:5]))

    _wrong = []
    for _n in ("chain", "tools", "utils", "log", "memory", "rag", "llm",
               "qd_agent", "qd_service", "agent", "trace_collector"):
        _m = sys.modules.get(_n)
        _f = getattr(_m, "__file__", None) if _m is not None else None
        if not _f:
            continue
        # 包（chain）的 __file__ 是 .../chain/__init__.py，故用 startswith 而非等于
        if not os.path.normcase(os.path.abspath(_f)).startswith(_agent_dir + os.sep):
            _wrong.append(f"{_n}->{_f}")
    check("裸名模块解析到 app/agent 下（防 utils 命中 app/utils）",
          not _wrong, ";".join(_wrong[:3]))

    _byfile = {}
    for _name, _m in list(sys.modules.items()):
        _f = getattr(_m, "__file__", None)
        if not _f:
            continue
        # multiprocessing 会把入口脚本再以 __mp_main__ 载入一次，属正常现象，不算双份
        if _name in ("__main__", "__mp_main__"):
            continue
        _f = os.path.normcase(os.path.abspath(_f))
        if "backend_api_python" in _f:
            _byfile.setdefault(_f, []).append(_name)
    _dups = {f: v for f, v in _byfile.items() if len(v) > 1}
    check("同一文件未被加载成多份 module", not _dups,
          "; ".join(f"{os.path.basename(f)}->{v}" for f, v in list(_dups.items())[:3]))

    try:
        import importlib
        _m1 = sys.modules.get("qd_agent")
        _m2 = importlib.import_module("qd_agent")
        check("重复 import 拿到同一 module 对象",
              _m1 is not None and _m1 is _m2, str(_m1))
    except Exception as e:
        check("重复 import 拿到同一 module 对象", False, f"{type(e).__name__}: {e}")


def test_30_toolface_import_health():
    """工具面无静默降级。

    事故回放（2026-10-02）：`skills/market_screener/common.py` 把 **`app/`**（误当
    backend 根）insert 到 sys.path[0]，使 `app/` 反超 `app/agent/` ⇒ 裸名
    `import utils` 命中 `app/utils` ⇒ 9 个 finance 工具模块的
    `from utils.md_format import ...` 全部 ModuleNotFoundError ⇒ 工具面 101→91。
    而 ToolProvider 对导入失败只 warning + continue，能力层随即补位把缺口盖住，
    所以 CLI 一切"正常"，缺陷完全隐身。

    判据：① 工具模块导入失败登记表为空 ② 工具面总量不低于基线（防别的静默削减）。
    """
    print("\n[test_30] 工具面导入健康（防静默降级）")
    try:
        from tools.base import ToolProvider, _TOOL_MODULE_IMPORT_FAILURES
    except Exception as e:
        check("可导入 ToolProvider", False, f"{type(e).__name__}: {e}")
        return

    check("无工具模块导入失败（防 sys.path 错序等静默降级）",
          not _TOOL_MODULE_IMPORT_FAILURES,
          "; ".join(_TOOL_MODULE_IMPORT_FAILURES[:4]))

    try:
        _n = len(ToolProvider.get_or_build().get_functions())
        # 基线 101（含能力层 40）；下调阈值留出正常增删余量，但能挡住成片掉模块
        check(f"工具面数量充足（{_n} ≥ 95）", _n >= 95, str(_n))
    except Exception as e:
        check("工具面数量充足", False, f"{type(e).__name__}: {e}")

    # 顺序判据：app/agent 必须压在 app/ 之前，且唯一（否则裸名解析随时可能错位）
    _agent_dir = os.path.normcase(os.path.abspath(str(AGENT_DIR)))
    _app_dir = os.path.dirname(_agent_dir)
    _i_agent = [i for i, p in enumerate(sys.path)
                if p and os.path.normcase(os.path.abspath(str(p))) == _agent_dir]
    _i_app = [i for i, p in enumerate(sys.path)
              if p and os.path.normcase(os.path.abspath(str(p))) == _app_dir]
    check("app/agent 在 sys.path 中唯一（避免重复条目）", len(_i_agent) == 1, str(_i_agent))
    check("app/agent 严格排在 app/ 之前（防 import utils 命中 app/utils）",
          bool(_i_agent) and (not _i_app or _i_agent[0] < _i_app[0]),
          f"agent={_i_agent} app={_i_app}")


def test_31_startup_bare_imports():
    """启动期（`app/__init__.py`）的裸名导入必须**可解析**。

    事故回放 C-1：`app/__init__.py` 写 `from cron_worker import start_cron_worker`，
    但真身是 `app/agent/cron/cron_worker.py`（顶包名是 `cron`，不是 `cron_worker`）
    ⇒ 每次启动都 ImportError，被外层 `try/except` 吞成一行 warning ⇒
    **定时任务从未启动过**，而启动日志一切正常。

    这类「启动期裸名写错」没有任何运行时症状（不崩、不报、功能直接消失），
    只能静态查。判据：`app/__init__.py` 里所有非标准库、非 `app.*` 的顶层模块名，
    都必须能被 `importlib.util.find_spec` 解析到（否则就是写错了路径）。
    """
    print("\n[test_31] 启动期裸名导入可解析（C-1 防复发）")
    import ast as _ast
    import importlib.util as _ilu

    _init_py = os.path.join(os.path.dirname(str(AGENT_DIR)), "__init__.py")
    try:
        _src = open(_init_py, encoding="utf-8-sig").read()
        _tree = _ast.parse(_src)
    except Exception as e:
        check("可解析 app/__init__.py", False, f"{type(e).__name__}: {e}")
        return

    _tops = set()
    for _n in _ast.walk(_tree):
        if isinstance(_n, _ast.Import):
            for _a in _n.names:
                _tops.add(_a.name.split(".")[0])
        elif isinstance(_n, _ast.ImportFrom) and _n.module:
            _tops.add(_n.module.split(".")[0])
    _tops = {t for t in _tops
             if t and not t.startswith("app.") and t != "app"
             and t not in sys.stdlib_module_names}

    _bad = []
    for _t in sorted(_tops):
        try:
            if _ilu.find_spec(_t) is None:
                _bad.append(_t)
        except Exception as _e:
            _bad.append(f"{_t}({type(_e).__name__})")
    check(f"app/__init__.py 的 {len(_tops)} 个裸名导入全部可解析（防 cron_worker 式写错）",
          not _bad, "不可解析: " + ",".join(_bad))


def test_32_skill_injection_progressive():
    """技能注入：渐进式加载 + 会话级去重 + 路标真实性（2026-10-02 防复发）。

    缺陷回放（审查 `tmp/qd_service.py` 时**实测**发现，非推测）：
      F1 `resource=` 路标无条件给出，但现网 5 个技能里 **4 个 `list_resources()`
         为空** ⇒ 诱导模型做一次必然失败的调用（返回 `{"error":"无资源"}`）。
      F2 指针文案写"（见上方历史）"，而上下文压缩（阈值 `_compaction_threshold`，
         默认 65536×0.55≈36k）会改写历史、`_skill_seen` 却对此不知情 ⇒
         压缩后正文永久只剩指针。修法：指针改给 `read_skill(name=...)`
         不带参数 = 全文（`skill_tools.py` 实测走 `load_body`），模型可自取，
         不再依赖历史是否还在。

    判据：① 超阈值技能注入块必须带 [技能目录] + [按需读取]
          ② resource 路标必须与 `list_resources()` 实况一致（防指向空气）
          ③ 指针必须给出"完整正文"自取方式（防压缩后正文消失）
          ④ 同会话二次命中→指针；换 session→重新注入正文（不串味）
    """
    print("\n[test_32] 技能注入渐进式加载（F1/F2 防复发）")
    try:
        from qd_service import (QDAgentService as _Svc,
                                _skill_block as _blk,
                                _SKILL_BODY_CAP as _CAP)
        from llm.qd_skills import QDSkillAdapter
    except Exception as e:
        check("可导入 qd_service / qd_skills", False, f"{type(e).__name__}: {e}")
        return

    _sk = QDSkillAdapter()
    _big = []
    for _s in (_sk.list_skills() or []):
        _n = _s.get("name") if isinstance(_s, dict) else str(_s)
        try:
            _b = _sk.load_body(_n) or ""
        except Exception:
            _b = ""
        if len(_b) > _CAP:
            _big.append((_n, _b))
    if not _big:
        # 技能被删/都很小时不误报：本项判据依赖"存在超阈值技能"这一前提
        check("存在超阈值技能（无则跳过本项判据）", True,
              f"当前无 >{_CAP} 字符的技能，①②③ 跳过")
        return

    _no_ol = [_n for _n, _b in _big if "[技能目录]" not in _blk(_n, _b, _sk)]
    check(f"超阈值技能注入块含 [技能目录]（{len(_big)} 个）", not _no_ol, ",".join(_no_ol))
    _no_rd = [_n for _n, _b in _big if "[按需读取]" not in _blk(_n, _b, _sk)]
    check("超阈值技能注入块含 [按需读取] 路标", not _no_rd, ",".join(_no_rd))

    _mm = []
    for _n, _b in _big:
        try:
            _has = bool(_sk.list_resources(_n))
        except Exception:
            _has = False
        if ("resource=" in _blk(_n, _b, _sk)) != _has:
            _mm.append(f"{_n}(有资源={_has})")
    check("resource 路标与 list_resources 实况一致（防指向空气·F1）",
          not _mm, ",".join(_mm))

    _svc = _Svc(skills=_sk, agent_config={})
    _q = "帮我做个选股筛选"
    try:
        _t1, _ = _svc._prefetch(_q, "__t32a__")
        _t2, _ = _svc._prefetch(_q, "__t32a__")   # 同 session 同问 ⇒ 应去重
        _t3, _ = _svc._prefetch(_q, "__t32b__")   # 换 session ⇒ 应重注正文
    except Exception as e:
        check("_prefetch 可执行", False, f"{type(e).__name__}: {e}")
        return

    _ptr = "技能指针" in _t2
    check("同会话二次命中同一技能 → 指针（去重生效）", _ptr, _t2[:120])
    if _ptr:
        check("指针给出完整正文自取方式（不依赖历史·F2）",
              "要完整正文" in _t2, _t2[:160])
        check("指针不再写'见上方历史'（压缩后会失效）", "见上方历史" not in _t2)
    check("换 session 重新注入正文（不串味）", "相关技能" in _t3, _t3[:120])


def test_33_skill_dispatch_and_plan_lint():
    """执行型技能分发（差距 B）+ 计划残留轻校验（差距 A）防复发护栏（2026-10-02）。

    差距 B 回放：执行型技能（market_screener / strategy_debug）原先只能靠主 agent
    逐步取数来"手工执行"，重活全压上下文。现走 `run_skill` → 子进程 `skill_run.py`：
    白名单注册表 + 超时可击杀 + 产物落盘 + 预览回传。风险点是**白名单被绕过**
    （变成任意代码执行）与**链路静默不通**（工具注册了但子进程跑不起来）。

    判据：① run_skill 在工具面 ② 未登记技能被白名单拦住（不落到执行）
          ③ 真实链路通：ok=True 且 full_path 文件确实落盘
          ④ run_skill 在**按需层**（不在必注入层）：它只服务批量流水线，
             常驻每轮白挂 ~0.3k tokens 不划算；降级后靠 fail-open + 预选 +
             system 点名三条兜底拿到（见 QDAgentConfig.on_demand_tools 注释）
          ⑤ 未激活直调被 fail-open 就地激活（降级后模型仍调得到）
    差距 A 判据：⑥ 计划留 in_progress/pending → 落 plan_lint 事件
                ⑦ 全部 completed → 不落事件（不刷噪声）
                ⑧ current_plan 每轮归零（是 per-run 而非 per-session）
    """
    print("\n[test_33] 执行型技能分发 + 计划残留校验（差距 A/B）")

    # ── B① run_skill 进了工具面 ──
    try:
        from tools.base import ToolProvider
        _faces = ToolProvider.get_or_build().get_functions()
        check("run_skill 已进工具面（主 agent 才派得出去）", "run_skill" in _faces,
              ",".join(sorted(_faces)[:5]))
    except Exception as e:
        check("可导入 ToolProvider / 取工具面", False, f"{type(e).__name__}: {e}")
        return

    try:
        from tools import skill_tools as _st
    except Exception as e:
        check("可导入 tools.skill_tools", False, f"{type(e).__name__}: {e}")
        return

    # ── B② 白名单拦截（未登记技能不得执行）──
    _bad = _st.run_skill("__not_a_skill__")
    check("未登记技能被白名单拦住（防任意代码执行）",
          _bad.get("ok") is False and "未知技能" in str(_bad.get("error", "")),
          str(_bad)[:200])
    _badfn = _st.run_skill("strategy_debug", '{"fn":"__nope__"}')
    check("未登记入口函数被白名单拦住",
          _badfn.get("ok") is False and "白名单" in str(_badfn.get("error", "")),
          str(_badfn)[:200])

    # ── B③ 真实链路：子进程执行 + 产物落盘 + 预览回传 ──
    _ok = _st.run_skill("strategy_debug", '{"fn":"list_strategies"}')
    _fp = _ok.get("full_path") or ""
    check("执行型技能真实跑通（子进程→落盘→回传）",
          _ok.get("ok") is True and bool(_fp) and os.path.exists(_fp),
          str(_ok)[:200])
    if _ok.get("ok") is True:
        check("回传体含截断预览与耗时（主上下文只拿结论）",
              isinstance(_ok.get("result"), str) and "elapsed_s" in _ok,
              str(sorted(_ok))[:120])

    # ── B④/B⑤ 分层归属 + 降级后仍拿得到 ──
    try:
        import asyncio
        from qd_service import QDAgentService
        _svc33 = QDAgentService(model=ScriptedModel([{"content": "ok"}]),
                                agent_config={"tools": []})
        asyncio.run(_svc33.chat("hi", session_id="__t33__"))
        _ag33 = _svc33._get_agent("__t33__")
        check("run_skill 在按需层、不在必注入层（省 ~0.3k tokens/轮）",
              "run_skill" not in (_ag33._core_names or [])
              and "run_skill" in (_ag33._on_demand_names or []),
              f"core含={('run_skill' in (_ag33._core_names or []))}")
        try:
            _ag33.execute_action({"tool": "run_skill", "params": {"name": "__nope__"}})
        except Exception:
            pass   # 工具本身返回 error 字典无妨；只关心有没有被激活
        check("未激活直调被 fail-open 就地激活（降级后仍拿得到）",
              "run_skill" in (_ag33._activated or []), str(_ag33._activated)[:120])
    except Exception as e:
        check("可装配 agent 做分层判定", False, f"{type(e).__name__}: {e}")

    # ── A⑥/A⑦ 计划残留 → 落 trace 事件 ──
    try:
        from qd_agent import QDAgent
    except Exception as e:
        check("可导入 qd_agent", False, f"{type(e).__name__}: {e}")
        return

    class _T:
        def __init__(self):
            self.events = []

        def emit(self, event, **fields):
            self.events.append((event, fields))

    class _S:
        pass

    def _mk(plan):
        s = _S()
        s.trace = _T()
        s.current_plan = plan
        return s

    _dirty = _mk([{"step": "看大盘", "status": "completed"},
                  {"step": "看技术面", "status": "in_progress"},
                  {"step": "看资金面", "status": "pending"}])
    QDAgent._plan_lint(_dirty)
    check("计划留残账 → 落 plan_lint 事件（收尾未清账可事后对账）",
          _dirty.trace.events and _dirty.trace.events[0][0] == "plan_lint"
          and _dirty.trace.events[0][1]["residue"] == ["看技术面", "看资金面"],
          str(_dirty.trace.events)[:200])

    _clean = _mk([{"step": "a", "status": "completed"}])
    QDAgent._plan_lint(_clean)
    check("计划已勾完 → 不落事件（不刷噪声）", not _clean.trace.events,
          str(_clean.trace.events)[:120])

    # ── A⑥ current_plan 是 per-run 生命周期 ──
    try:
        import inspect as _inspect
        _src = _inspect.getsource(QDAgent._run_inner)
        check("current_plan 每轮归零（per-run，非跨请求残留）",
              "self.current_plan = None" in _src, "")
    except Exception as e:
        check("可读 _run_inner 源码", False, f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    _base = _trace_baseline()     # 自清基线：只删 smoke 自己写的行
    for t in (test_1_happy_path, test_2_trading_confirm_gate,
              test_3_danger_guard, test_4_grounding_gate, test_5_trace_events,
              test_6_native_mimo_capabilities, test_7_service_facade,
              test_8_context_awareness, test_9_step_budget_per_run,
              test_10_empty_reply_fallback, test_11_prefetch_no_pollution,
              test_12_retry_independent_budget, test_13_empty_response_nudge,
              test_14_demo_task_discipline, test_15_realtime_info_must_search,
              test_16_meta_tools_mounted, test_17_tool_tiering,
              test_18_activate_and_auto_activate, test_19_availability_probe,
              test_20_prefetch_trace, test_21_tool_preselect,
              test_22_context_window_single_source, test_23_preselect_lint,
              test_24_tool_grading, test_25_toolface_preload,
              test_26_accountability_v11, test_27_reset_wiring_observation,
              test_28_closed_loop_consumers,
              test_29_module_single_instance,
              test_30_toolface_import_health,
              test_31_startup_bare_imports,
              test_32_skill_injection_progressive,
              test_33_skill_dispatch_and_plan_lint):
        try:
            t()
        except Exception as e:
            PASS.append(False)
            print(f"  [FAIL] 异常: {e!r}")
    _cleanup_trace_rows(_base)
    ok = sum(PASS)
    print(f"\n== {ok}/{len(PASS)} passed ==")
    sys.exit(0 if ok == len(PASS) else 1)
