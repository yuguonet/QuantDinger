# -*- coding: utf-8 -*-
"""阶段 P3 拆分守卫：QDAgent 上帝类拆成职责模块后，对外契约不得变。

背景（用户拍板 2026-10-13）：
  `qd_agent.py` 1486 行 / 41 方法，是注册 / 预选 / 安全 / 循环 / 账本 / 收尾 /
  轨迹 七件事的补丁堆叠现场。拆分的**唯一目的**是让每一块有清晰的归属，
  **不是**改任何行为 —— 所以本文件守的是「契约不变」，不是「实现不变」。

三条必须守住的东西：
  ① 对外名字不变：`qd_service` / `message_queue` / `qd_smoke` / `tool_discovery`
     都从 `qd_agent` 取 `QDAgent` / `QDAgentConfig` / hooks 四件套，且运行时会
     调 `agent.activate_tools()` / `agent.trace_prefetch()` 等实例方法。
     ⇒ 拆成 mixin 后，这些名字必须**仍然能在 QDAgent 上 getattr 到**。
  ② MRO 不变：mixin 必须排在 `MimocodeAgent` **之前**，否则
     `ToolFaceMixin._build_tool_registry` 里的 `super()` 会落到 object 而不是
     mimo 原版实现 ⇒ 工具面直接空掉。
  ③ 命名空间守卫不失效：`test_agent_attr_namespace.py` 原先只静态扫
     `qd_agent.py` 一个文件；方法散到多个文件后它扫不到 `self.X = ...`
     ⇒ 2026-10-12 血案（`_run_tool_calls`）的网会破一个洞。故守卫一并扩到
     所有组成文件（见该文件）。
"""
from __future__ import annotations

import importlib
from pathlib import Path

# 【2026-10-13·P5】legacy 目录**不再进 sys.path**，包内一律相对导入，
# 因此测试也改用绝对导入 `app.agent.*`（与包外生产代码同款）。
from app.agent.qd_guards import GuardMixin
from app.agent.qd_preselect import PreselectMixin
from app.agent.qd_tool_face import ToolFaceMixin
from app.agent.qd_budgeting import BudgetMixin
from app.agent.qd_aftercare import AftercareMixin

_BACKEND = Path(__file__).resolve().parent.parent
_LEGACY_DIR = _BACKEND / "app" / "agent"

# ── 拆分后的组成模块 ──────────────────────────────────────────
# 命名一律 `qd_` 前缀：与 legacy 目录内其余模块同风格，`config`/`hooks`/`guards`
# 这种通用名极易与三方包或将来新增模块撞车（2026-10-13 定名）。
SPLIT_MODULES = (
    "qd_hooks",        # 会话钩子表（线程局部绑定）
    "qd_config",       # QDAgentConfig + 上下文窗口/压缩阈值口径
    "qd_guards",       # 危险面守卫 + 交易确认闸
    "qd_budgeting",    # 成本记账 + 硬熔断
    "qd_preselect",    # 工具预选（点名 + lint + 域兜底）
    "qd_tool_face",    # 工具分层 / 注册 / 预热 / 地图
    "qd_aftercare",    # 轨迹收尾 / 追责 / C3P / 案例记忆
    "qd_grounding",    # 数字溯源门
)

# QDAgent 对外必须仍然具备的成员（缺一个就是拆分时漏搬了）。
# 来源：qd_service.py:27 re-export + tool_discovery / qd_smoke 的运行时调用。
REQUIRED_MEMBERS = (
    # 循环主干
    "run", "_run_inner", "step", "after_step", "execute_action",
    # 工具面
    "activate_tools", "active_tool_names", "_sync_tool_tiers", "_tool_domain",
    "_build_tool_registry", "_collect_tool_functions",
    "_maybe_register_capabilities", "_tool_map_text", "_tool_availability_text",
    "get_model_query_kwargs", "_active_tool_definitions",
    # 预选
    "_maybe_preselect_tools", "_preselect_query", "_finish_preselect",
    "_empty_route",
    # 守卫
    "_trading_confirm_guard", "_danger_guard", "_deny",
    # 预算
    "_account_budget", "_last_step_tool_calls",
    # 收尾
    "_make_collector", "_plan_lint", "_finish_collector", "_c3p_pipeline",
    "_record_case", "_intake_accountability", "_trace_tool",
    # 溯源 + 事件
    "_observation_corpus", "_grounding_violation", "_emit_action_step",
    "trace_prefetch",
)


# ═══════════════════════════════════════════════════════════════
#  ① 结构：拆分真的发生了
# ═══════════════════════════════════════════════════════════════

def test_qd_agent_source_is_slimmed() -> None:
    """qd_agent.py 必须是瘦执行核，不能又长回上帝类。

    1486 行 → 目标 ≤ 420 行。留余量是给文件头注释（那些注释是有价的：
    每条都记着一次实测事故），别为了凑行数删注释。
    """
    src = (_LEGACY_DIR / "qd_agent.py").read_text(encoding="utf-8")
    n = src.count("\n") + 1
    assert n <= 420, (
        f"qd_agent.py 又长到 {n} 行（拆分目标 ≤420）—— "
        f"说明有逻辑被塞回执行核了，请放回对应的 qd_* 职责模块")


def test_split_modules_exist_and_importable() -> None:
    """每个职责模块都存在、且能**独立**导入（不依赖 qd_agent 先被导入）。

    独立可导入是拆分的验收标准：如果某个模块只能通过 qd_agent 间接导入，
    说明它内部有循环依赖，拆分就是假的。
    """
    for name in SPLIT_MODULES:
        path = _LEGACY_DIR / f"{name}.py"
        assert path.is_file(), f"职责模块缺失：{path}"
        mod = importlib.import_module("app.agent." + name)
        assert mod.__doc__ and len(mod.__doc__) > 30, (
            f"{name}.py 缺少文件头注释（说明职责与易错点，见本仓注释规范）")


def test_no_module_exceeds_single_responsibility_budget() -> None:
    """单个职责模块也不许超过 600 行 —— 否则只是把上帝类换成了上帝模块。"""
    for name in SPLIT_MODULES:
        n = (_LEGACY_DIR / f"{name}.py").read_text(encoding="utf-8").count("\n") + 1
        assert n <= 600, f"{name}.py {n} 行 > 600：职责还要再切一刀"


# ═══════════════════════════════════════════════════════════════
#  ② 契约：对外名字一个都不能少
# ═══════════════════════════════════════════════════════════════

def test_qd_agent_exposes_required_members() -> None:
    """QDAgent 必须仍然具备全部对外成员（mixin 继承也算）。

    守的是运行时契约：`tools/tool_discovery.py` 调 `agent.activate_tools()`、
    `qd_service` 调 `agent.trace_prefetch()` —— getattr 不到就是当场崩。
    """
    from mimoagent.agents.mimocode.mimocode_agent import MimocodeAgent
    qd_agent = importlib.import_module("app.agent.qd_agent")
    agent_cls = qd_agent.QDAgent

    missing = [m for m in REQUIRED_MEMBERS if not hasattr(agent_cls, m)]
    assert not missing, (
        f"拆分后 QDAgent 丢失成员 {missing} —— 运行时调用方会 AttributeError")

    # 继承链必须仍然以 mimo 执行核为终点（不是把 MimocodeAgent 换掉了）
    assert issubclass(agent_cls, MimocodeAgent)


def test_module_level_names_still_reexported() -> None:
    """`qd_agent` 模块级名字不变：`qd_service` 从这里 re-export 给 message_queue。

    `_QD_COMPACT_NOTES` 单独点名：`tests/test_v5_compaction_guard.py:110` 引用它，
    且压缩摘要第 (6) 条（保住注入块）的指纹就在这个常量里 —— 漂移了那边会红。
    """
    qd_agent = importlib.import_module("app.agent.qd_agent")
    for name in ("QDAgent", "QDAgentConfig", "bind_run_session",
                 "register_run_hooks", "unregister_run_hooks", "_hooks_now",
                 "_QD_COMPACT_NOTES", "CORE_TOOL_DOMAINS", "warmup_tool_face",
                 "_ProbeInterrupt"):
        assert hasattr(qd_agent, name), (
            f"qd_agent.{name} 不见了 —— qd_service 的 re-export 会 ImportError")

    notes = qd_agent._QD_COMPACT_NOTES
    # 压缩摘要第 (6) 条的真实前缀（全角书名号），v5-实施计划里写的 [参考案例] 是笔误
    assert "【历史相似案例——仅作参考，不要照搬结构】" in notes
    assert "[参考资料]" in notes


def test_mro_places_mixins_before_mimo_core() -> None:
    """MRO 红线：所有 mixin 必须排在 MimocodeAgent 前面。

    ★ 为什么这条要单独钉：`ToolFaceMixin._build_tool_registry` 里有
    `super()._build_tool_registry()`。若 mixin 排在 MimocodeAgent 之后，
    这个 super() 就再也碰不到 mimo 原版实现 ⇒ 工具注册表里一个工具都没有，
    而且**不报错**、只是模型说什么都做不了 —— 属于最难受的那类静默故障。
    """
    from mimoagent.agents.mimocode.mimocode_agent import MimocodeAgent
    qd_agent = importlib.import_module("app.agent.qd_agent")
    mro = qd_agent.QDAgent.__mro__
    names = [c.__name__ for c in mro]

    assert "MimocodeAgent" in names
    core_at = names.index("MimocodeAgent")
    mixins = [n for n in names[:core_at] if n.endswith("Mixin")]
    assert mixins, (
        f"QDAgent 的 MRO 里 MimocodeAgent 之前没有任何 Mixin（MRO={names}）"
        f"—— 说明拆分被回退成了单类")
    # 执行核本体（QDAgent 自己）排在最前，mixin 紧随其后，最后是 mimo
    assert names[0] == "QDAgent"


# ═══════════════════════════════════════════════════════════════
#  ③ 行为：搬过去的纯逻辑仍然正确（不看实现看结果）
# ═══════════════════════════════════════════════════════════════

def test_empty_route_classification() -> None:
    """`_empty_route` 把「为什么空手」归成稳定标签（供 counters 分组归因）。

    标签必须**稳定**：改文案不能断统计，所以只认关键字、认不出归 unknown。
    """
    qd_agent = importlib.import_module("app.agent.qd_agent")
    f = qd_agent.QDAgent._empty_route
    assert f({"reason": "无领域特征，跳过预选(闸门)"}) == "gate"
    assert f({"reason": "模型未点名(可能是纯问答)"}) == "no_pick"
    assert f({"reason": "lint 判定整体误判，已作废"}) == "lint_void"
    assert f({"reason": "预选调用失败(fail-open): Timeout"}) == "call_error"
    assert f({}) == "unknown"
    assert f({"reason": "某种没见过的新文案"}) == "unknown"


def test_deny_output_shape() -> None:
    """`_deny` 产出必须是 mimo ToolOutput 字典：success=False + danger_denied 标记。

    下游（trace / after_step）按 `success` 判错误、按 metadata 判被拒原因。
    """
    qd_agent = importlib.import_module("app.agent.qd_agent")
    out = qd_agent.QDAgent._deny("bash", "危险命令拒绝执行: rm -rf /")
    assert out["success"] is False
    assert "安全策略拒绝" in out["output"]
    assert out["metadata"]["tool"] == "bash"
    assert out["metadata"]["danger_denied"] is True


class _FakeSelf(GuardMixin, PreselectMixin):
    """替身：**继承**被测 mixin，只补它们真正读到的那几个属性。

    用替身而不是真造 QDAgent（真造要 model + env + 工具面冷启动 3s），
    因为这几个方法是**纯逻辑**：不碰 self 以外的任何东西。
    继承而不是手搓属性，是因为 `_danger_guard` 里调了 `self._deny`、
    `_preselect_query` 里读了类常量 `_USER_QUERY_MARK` —— 这两个本来就该由
    mixin 自己提供，手搓等于把被测代码复制一遍（那测试就失去意义了）。
    """

    def __init__(self, **kw):
        self.last_user_query = ""
        for k, v in kw.items():
            setattr(self, k, v)


def test_danger_guard_denies_credentials_and_destructive_cmds() -> None:
    """危险面守卫：凭据路径 deny + 破坏性命令 deny（沿用修订三②口径）。

    ★ `2>/dev/null` 必须**放行** —— 它是无害重定向，`which x 2>/dev/null`
    被误拒过一次。这个正则里的 `(?!null\b)` 就是那次事故留下的。
    """
    qd_agent = importlib.import_module("app.agent.qd_agent")
    guard = qd_agent.QDAgent._danger_guard

    denied = guard(_FakeSelf(), {"tool": "read",
                                 "params": {"path": "/home/u/.ssh/id_rsa"}})
    assert denied is not None and "安全策略拒绝" in denied["output"]

    denied = guard(_FakeSelf(), {"tool": "bash",
                                 "params": {"command": "rm -rf /data"}})
    assert denied is not None

    denied = guard(_FakeSelf(), {"tool": "read",
                                 "params": {"path": "/app/.env"}})
    assert denied is not None

    # ★ 已知边界（**本次不改**，如实记录）：`cat .env` 这种**相对**路径不拦 ——
    #   `_DENY_PATH_RE` 的 `.env` 分支要求 `(^|/)` 前缀，"cat .env" 里前面是空格。
    #   这是既有口径（不是拆分引入的），收紧它需要同步评估误伤（业务里有
    #   `.env.example` 之类文件名）。留在这里是为了让后来人知道这是**已知**不是**漏测**。
    assert guard(_FakeSelf(), {"tool": "bash",
                               "params": {"command": "cat .env"}}) is None

    # 无害重定向必须放行
    assert guard(_FakeSelf(), {"tool": "bash",
                               "params": {"command": "which python 2>/dev/null"}}) is None
    assert guard(_FakeSelf(), {"tool": "read",
                               "params": {"path": "src/main.py"}}) is None


def test_preselect_query_strips_injection_blocks() -> None:
    """预选必须看**用户原始问题**，不是 prefetch 加工后的文本。

    2026-10-01 实测：用加工后文本做预选，模型被历史记忆带偏，
    问天气却点名 7 个股票工具 ⇒ token +136%。三级来源优先级如下。
    """
    qd_agent = importlib.import_module("app.agent.qd_agent")
    f = qd_agent.QDAgent._preselect_query

    # ① service 显式挂的原始问题最准
    assert f(_FakeSelf(last_user_query="明天买什么股"),
             "记忆块\n\n[用户问题]\n今天天气") == "明天买什么股"
    # ② 从 prefetch 拼装结构里截
    assert f(_FakeSelf(), "记忆块\n\n[用户问题]\n今天天气") == "今天天气"
    # ③ 无注入：task 即原始消息
    assert f(_FakeSelf(), "帮我看看大盘") == "帮我看看大盘"
    # last_user_query 为空串时不能顶掉 ②
    assert f(_FakeSelf(last_user_query="  "),
             "块\n\n[用户问题]\n真问题") == "真问题"


def _tier_self(tools: dict, **cfg):
    """造一个能跑 `_sync_tool_tiers` / `_tool_map_text` 的替身。

    用**真的** `FnToolAdapter` 而不是假的：`_sync_tool_tiers` 靠
    `isinstance(tool, FnToolAdapter)` 分流，替身过不了这道判定，测试就假了。
    """
    from types import SimpleNamespace

    class _Reg:
        def __init__(self, t):
            self._t = t

        def get_function_definitions(self):
            return [{"function": {"name": n}} for n in self._t]

        def list_tools(self):
            return list(self._t)

        def get(self, n):
            return self._t[n]

    class _S(ToolFaceMixin):
        pass

    s = _S()
    s.tool_registry = _Reg(tools)
    s.config = SimpleNamespace(on_demand_tools=[], extra_core_tools=[], **cfg)
    return s


def test_tool_tier_classification() -> None:
    """分层口径：非 FnToolAdapter 恒必注入；FnToolAdapter 按 domain 分。

    ★ 两条都踩过坑：
      · mimo 原生工具（bash/read…）若被分到按需层 ⇒ 工具面裸掉、
        连文件都读不了（2026-09-19 故障的直接机制）；
      · `meta` 域（web_search / format_result）若掉出必注入层 ⇒ 模型想查实时信息
        却拿不到 web_search（2026-10-01：它们在 tools/_base.py 的 `_MUST_HAVE` 里，
        本来就被 scan 跳过，再掉一层就彻底没了）。
    """
    from types import SimpleNamespace
    from app.agent.tools._fn_adapter import FnToolAdapter
    from app.agent import qd_config

    assert qd_config.CORE_TOOL_DOMAINS == ("common", "meta"), (
        "CORE_TOOL_DOMAINS 变了 —— meta 域（元工具）必须留在必注入层")

    def _f() -> str:
        """[test] 占位工具。"""
        return ""

    s = _tier_self({
        "bash": object(),                                        # mimo 原生
        "common_tool": FnToolAdapter(_f, domain="common"),
        "meta_tool": FnToolAdapter(_f, domain="meta"),
        "fin_tool": FnToolAdapter(_f, domain="finance"),
    })
    s._sync_tool_tiers()
    assert set(s._core_names) == {"bash", "common_tool", "meta_tool"}, s._core_names
    assert s._on_demand_names == ["fin_tool"], s._on_demand_names


def test_tool_map_marks_activated_tools() -> None:
    """工具地图必须含 [已激活] 段，否则模型照旧走 search_tools 三轮发现。

    2026-10-01 实测：预选激活了工具但地图没写这一段 ⇒ 模型以为还得先检索
    ⇒ 发现轮一次没少、token 反而 +21%，预选白做。
    """
    from app.agent.tools._fn_adapter import FnToolAdapter

    def _f() -> str:
        """[test] 占位工具。"""
        return ""

    s = _tier_self({"fin_tool": FnToolAdapter(_f, domain="finance")})
    s._sync_tool_tiers()
    assert "[已激活" not in s._tool_map_text(), "未激活时不该有已激活段"
    s._activated = ["fin_tool"]
    text = s._tool_map_text()
    assert "[已激活" in text, text
    # 只判**这一段**的内容：这段的价值是告诉模型"可以直接调、不用再检索"
    seg = text.split("[已激活")[1].split("[按需")[0]
    assert "可直接调用" in seg and "fin_tool" in seg, seg
    assert "不要再" in seg, "必须明确制止模型再走 search_tools，否则发现轮一次不少"


def test_budget_accumulates_across_steps() -> None:
    """成本必须**累加**：不累加 ⇒ 熔断永不触发（P0 修的那个洞又开了）。

    2026-10-12 P0：一轮 15 次调用烧 40 万 token 且零产出，就是因为没人记账。
    """
    from types import SimpleNamespace

    class _S(BudgetMixin):
        pass

    s = _S()
    s._qd_run_tokens = 0
    s._qd_run_steps_total = 0
    s._qd_run_tool_calls = 0
    s.messages = []
    s.trace = None
    s.config = SimpleNamespace(run_step_cap=0)

    s._last_request_tokens = 1000
    s._account_budget()
    s._account_budget()
    assert s._qd_run_tokens == 2000, s._qd_run_tokens
    assert s._qd_run_steps_total == 2, s._qd_run_steps_total

    # 累计到超预算必须熔断（软收尾：LimitsExceeded，不硬崩）
    from mimoagent.agents.base import LimitsExceeded
    s._last_request_tokens = 10 ** 9
    try:
        s._account_budget()
        raise AssertionError("超预算没熔断 —— 成本记账失效")
    except LimitsExceeded:
        pass


def test_aftercare_call_order_is_contract() -> None:
    """收尾顺序 intake → c3p → case 是**契约**，不是实现细节。

    · C3P 在 intake 之后：它要吃 intake 产出的 claim（claim.subject 是 p_base 原料）；
    · C3P 在 case 之前：它产出的 evidence 要写进案例的 outcome.evidence
      （那正是 P1-2 缺的写侧，顺序反了库里一条真 EV-1 都不会有）。
    """
    from types import SimpleNamespace

    class _Collector:
        user_query = "问"
        session_id = "s"

        def on_agent_finish(self, **kw):
            pass

        def flush(self):
            return 1

    class _S(AftercareMixin):
        def __init__(self):
            self.calls = []
            self._collector = _Collector()
            self._steps_taken = 0
            self.model = SimpleNamespace(token_stats=None, config=None)
            self.trace = None

        def _intake_accountability(self, *a):
            self.calls.append("intake")

        def _c3p_pipeline(self, *a):
            self.calls.append("c3p")

        def _record_case(self, *a):
            self.calls.append("case")

    s = _S()
    s._finish_collector("答", "ok")
    assert s.calls == ["intake", "c3p", "case"], s.calls
    assert s._collector is None, "收尾后必须摘掉 collector（否则跨 run 串线）"


def test_context_window_and_threshold_single_source() -> None:
    """上下文窗口与压缩阈值必须走**同一口径**（`_context_window()`）。

    2026-10-01 二次修正的两个坑：
      · 首版写成两个 env 名（QD_COMPACT_WINDOW / QD_CONTEXT_WINDOW）⇒ 只改一个
        时页脚分母和催收阈值对不上；
      · 不能拿 OPENAI_MAX_TOKENS（单次输出上限）当输入窗口用。
    """
    import os
    qd_config = importlib.import_module("app.agent.qd_config")

    os.environ["QD_CONTEXT_WINDOW"] = "100000"
    try:
        assert qd_config._context_window() == 100000
        # 阈值按窗口比例（默认 0.55）
        assert qd_config._compaction_threshold() == 55000
    finally:
        os.environ.pop("QD_CONTEXT_WINDOW", None)

    # 非法值不能让配置静默变成 0（那会让压缩每轮触发）
    os.environ["QD_CONTEXT_WINDOW"] = "abc"
    try:
        assert qd_config._context_window() >= 4096
    finally:
        os.environ.pop("QD_CONTEXT_WINDOW", None)


def test_warmup_is_idempotent(monkeypatch) -> None:
    """工具面预热必须幂等：冷启动 3.11s 只能付一次。

    幂等靠模块级 `_WARM_STATE`，重复调用直接返回上次结果（不重扫 76 个工具模块）。
    """
    qd_tool_face = importlib.import_module("app.agent.qd_tool_face")

    class _P:
        def __init__(self):
            self.calls = 0
            self._qd_caps_registered = True
            self._qd_caps_count = 3

        def get_functions(self):
            self.calls += 1
            return {"a": lambda: 1, "b": lambda: 2}

    holder = {}

    class _TP:
        @staticmethod
        def get_or_build():
            if "p" not in holder:
                holder["p"] = _P()
            return holder["p"]

    import app.agent.tools._base as tb
    monkeypatch.setattr(tb, "ToolProvider", _TP, raising=True)
    qd_tool_face._WARM_STATE.update(
        {"done": False, "tools": 0, "capabilities": 0, "elapsed": 0.0, "error": ""})

    r1 = qd_tool_face.warmup_tool_face()
    r2 = qd_tool_face.warmup_tool_face()
    assert r1["done"] is True and r2["done"] is True
    assert r1["tools"] == 2 and r2["tools"] == 2
    assert holder["p"].calls == 1, "预热不幂等：provider 被构建/扫描了多次"
    assert set(r1) == {"done", "tools", "capabilities", "elapsed", "error"}


def test_grounding_violation_returns_none_when_module_missing(monkeypatch) -> None:
    """溯源门 fail-open：grounding 模块不可用时返回 None（绝不因此卡住回复）。"""
    qd_grounding = importlib.import_module("app.agent.qd_grounding")
    # 传明显不可溯源的文本 + 空语料，若模块可用应给出 guide；不可用返回 None。
    # 这里只断言「不抛异常且返回值是 str 或 None」—— 判定质量由 utils/grounding
    # 自己的测试守，本处只守接口契约。
    out = qd_grounding.GroundingMixin._grounding_violation("沪深300涨了 3.21%", "")
    assert out is None or isinstance(out, str)


def test_observation_corpus_collects_tool_messages() -> None:
    """闭环②语料 = 本轮全部 role=tool 的消息正文（取代旧 executor.state）。"""
    qd_grounding = importlib.import_module("app.agent.qd_grounding")
    self = _FakeSelf(messages=[
        {"role": "user", "content": "问题"},
        {"role": "tool", "content": "收盘价 10.5"},
        {"role": "assistant", "content": "分析"},
        {"role": "tool", "content": "成交量 1.2亿"},
    ])
    corpus = qd_grounding.GroundingMixin._observation_corpus(self)
    assert corpus == "收盘价 10.5\n成交量 1.2亿"
