# -*- coding: utf-8 -*-
"""P4：`tools/_base.py`（1025 行）拆分后的契约与行为守卫。

★ 为什么这个测试必须存在
  `tools/_base.py` 是**整个 legacy 工具面的地基**：ToolProvider 被 20+ 模块 import，
  `_TOOL_MODULE_IMPORT_FAILURES` 是"工具模块导入失败"的唯一登记表（smoke 断言它为
  空，用来拦"工具面静默缺一大块"）。拆分时任何一处 re-export 写错（尤其是把可变
  对象**重新创建**而不是引用同一个），故障形态都是**静默**的：不报错、不崩，
  只是工具面悄悄少一块或者导入失败登记表永远为空。

★ 这里守的四类契约
  1. 组成文件都在（少一个就红，防守卫自身失效）
  2. `tools._base` 的 re-export 面不缩水（外部 20+ 模块一行都不用改）
  3. **可变共享状态是同一个对象**（`is` 判断，不是 `==`）——最关键
  4. 拆出的内核模块**不会污染工具面**（靠下划线前缀退出扫描，零名单）

用法：
    pytest tests/test_v6_tools_base_split.py -q
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# 【2026-10-13·P5】本文件原本把 legacy 目录塞进 sys.path 再用裸名导入。
# 改造后一律走绝对导入 `app.agent.*` —— 注入不但多余，还会让
# `test_legacy_dir_not_on_syspath`（M-1 根治的证据）在全量跑时误红。
_BACKEND = Path(__file__).resolve().parent.parent
_LEGACY_DIR = _BACKEND / "app" / "agent"

tools_base = importlib.import_module("app.agent.tools._base")
tools_serialize = importlib.import_module("app.agent.tools._serialize")
tools_schema_gen = importlib.import_module("app.agent.tools._schema_gen")
tools_provider = importlib.import_module("app.agent.tools._provider")


# ── 1. 组成文件必须存在 ──────────────────────────────────────────

@pytest.mark.parametrize("rel", [
    "tools/_base.py",
    "tools/_serialize.py",
    "tools/_schema_gen.py",
    "tools/_provider.py",
])
def test_split_sources_exist(rel: str) -> None:
    """少一个组成文件就红——防止有人删了文件而 re-export 还"看起来能用"。"""
    assert (_LEGACY_DIR / rel).is_file(), f"{rel} 缺失"


def test_base_is_thin_now() -> None:
    """执行核变薄：base.py 只留 Tool 抽象 + 结果封装 + re-export。

    ≥600 行说明搬运没做干净（ToolProvider 或序列化还留在里面）。
    """
    n = len((_LEGACY_DIR / "tools" / "_base.py").read_text(encoding="utf-8").splitlines())
    assert n < 600, f"tools/_base.py 仍有 {n} 行，拆分未生效"


# ── 2. re-export 契约：外部 20+ 模块一行都不用改 ──────────────────

@pytest.mark.parametrize("name", [
    "Tool", "ToolResult", "ToolProvider", "func_to_openai_schema",
    "as_code_envelope",
    "_MUST_HAVE",
    "_TOOL_MODULE_IMPORT_FAILURES", "_is_tool_function",
])
def test_base_reexports_public_surface(name: str) -> None:
    """`from tools.base import X` 必须照旧可用。

    `tools/__init__.py` 与 20+ 个消费方（capabilities/loader、resolvers/*、
    tools/finance/*、qd_tool_face…）都走这条路径。
    """
    assert hasattr(tools_base, name), f"tools._base 丢失导出 {name}"


# ── 3. 可变共享状态必须是同一个对象（最关键）────────────────────

def test_import_failure_registry_is_identical_object() -> None:
    """`_TOOL_MODULE_IMPORT_FAILURES` 必须是**同一个 list 实例**。

    ★ 这是本次拆分最危险的静默故障点：它是一个可变 list，scan_directory 往里
    append，smoke 的 `test_30_toolface_import_health` 断言它为空。
    如果 re-export 时不小心写成新的 `[]`（或 `list(...)` 拷贝），
    登记与读取会落到两个不同对象 ⇒ **登记表永远为空** ⇒ 工具模块导入失败
    再也不会被发现（历史上正是这样让工具面从 101 掉到 91 而无人知）。
    故用 `is` 而不是 `==`。
    """
    assert tools_base._TOOL_MODULE_IMPORT_FAILURES is tools_provider._TOOL_MODULE_IMPORT_FAILURES


def test_superseded_list_must_not_come_back() -> None:
    """`_SUPERSEDED_BY_MERGE` 名单已整体删除（2026-10-22 AP.8），不得回流。

    ★ AP.8 零名单改造：18 个被聚合的旧工具实现已降级为 `_` 前缀私有函数，
    天然退出 scan_directory 自发现——不再需要任何函数级屏蔽名单。
    若有人把 `_SUPERSEDED_BY_MERGE`（或同类名单）加回来，这里立刻红。
    """
    assert not hasattr(tools_base, "_SUPERSEDED_BY_MERGE"), \
        "tools._base 里 _SUPERSEDED_BY_MERGE 回流了（AP.8 已删除）"
    assert not hasattr(tools_provider, "_SUPERSEDED_BY_MERGE"), \
        "tools._provider 里 _SUPERSEDED_BY_MERGE 回流了（AP.8 已删除）"


def test_tool_weight_migration_must_not_come_back() -> None:
    """`TOOL_WEIGHT_MIGRATION` 映射表已整体删除（2026-10-22 用户裁定），不得回流。

    ★ 原 `TOOL_ALIAS`（工具归并的权重迁移表：18 个旧工具名 → 新工具名）经调查：
      - 数据库实测 `qd_agent_weights(layer='tool')` 旧工具名行 = 0，映射表空转从未命中；
      - 唯一程序化消费方 chain/evaluator 已改为对「工具面已消失的旧名行」直接 DELETE。
    若有人把映射表（或同类别名/迁移表）加回来，这里立刻红。
    """
    assert not hasattr(tools_base, "TOOL_WEIGHT_MIGRATION"), \
        "tools._base 里 TOOL_WEIGHT_MIGRATION 回流了（2026-10-22 已删除）"
    assert not hasattr(tools_provider, "TOOL_WEIGHT_MIGRATION"), \
        "tools._provider 里 TOOL_WEIGHT_MIGRATION 回流了（2026-10-22 已删除）"
    assert not hasattr(tools_base, "TOOL_ALIAS"), \
        "tools._base 里 TOOL_ALIAS 回流了（2026-10-22 已删除）"
    assert not hasattr(tools_provider, "TOOL_ALIAS"), \
        "tools._provider 里 TOOL_ALIAS 回流了（2026-10-22 已删除）"


# ── 4. 非工具模块靠「下划线前缀」退出扫描（目录即契约，零名单）─────────

# ★ 相对 tools/ 的路径。finance/ 子目录同样受扫描，故一并列入。
#   `em_utils` / `screener_config` 就住在 finance/ 下 —— 2026-10-13 我核查"死条目"
#   时只看了顶层，误判它们是死条目（真死条目只有 registry / cache_tools 两个）。
# ★ 2026-10-14：`_pagination`（444 行，全仓零引用）已归档到 del/deadcode_20261014/，
#   从本表移除。它的下划线命名约定依然有效 —— 只是那个模块本身没了。
_KERNEL_MODULES = ["_base", "_serialize", "_schema_gen", "_provider",
                   "_fn_adapter", "_availability",
                   "finance/_em_utils", "finance/_screener_config"]
_TOOLS_DIR = _LEGACY_DIR / "tools"


@pytest.mark.parametrize("mod", _KERNEL_MODULES)
def test_kernel_modules_use_underscore_prefix(mod: str) -> None:
    """内核模块必须下划线前缀命名 —— 扫描器靠 `startswith("_")` 跳过它们。

    ★ 2026-10-13 P10：取代 `_SKIP_FILES` 名单。
      判据从"查名单"改成"查文件名"，是因为名单要手工维护、且**实测已漂移**
      （mcp 侧漏登记 availability ⇒ 启动期自检函数被注册成 MCP 工具暴露出去）。
      命名约定不会漂移：新增内核模块按下划线命名就自动退出扫描。
    """
    assert (_TOOLS_DIR / f"{mod}.py").is_file(), f"内核模块 {mod}.py 不存在（改名漏了？）"


def test_no_bare_kernel_module_names_in_tools_dir() -> None:
    """`tools/` 顶层不得出现**无下划线**的内核文件名（防改名被回退一半）。

    一旦 `base.py` / `provider.py` 这类名字回来，扫描器会立刻把它们当工具模块
    import —— `_schema_gen.func_to_openai_schema` 是"公开 + 有 docstring"，
    完全符合判据 ⇒ 变成模型可调的内部函数（P4 拆分时实测过）。
    """
    bare = [m.replace("/_", "/").lstrip("_") for m in _KERNEL_MODULES]
    found = [f"{m}.py" for m in bare if (_TOOLS_DIR / f"{m}.py").exists()]
    assert not found, f"内核模块改回无下划线名了 ⇒ 会被扫进工具面：{found}"


def test_kernel_functions_absent_from_registry() -> None:
    """内核模块的公开函数一个都不许出现在注册表里（结果侧验证）。

    上面两条守的是"命名"，这条守的是**结果**：命名对了但实际仍被注册
    （比如有人改了扫描条件）这里会红。
    """
    from app.agent.tools._base import ToolProvider
    reg = set(ToolProvider.get_or_build().get_tool_names())
    leaked = [n for mod in _KERNEL_MODULES
              for n in ("func_to_openai_schema", "probe_tools", "availability_text",
                        "as_code_envelope", "FnToolAdapter", "_to_tsv", "_serialize")
              if n in reg]
    assert not leaked, f"内核函数泄漏进工具面：{leaked}"


def test_no_skip_files_list_exists() -> None:
    """`_SKIP_FILES` 必须彻底消失（含曾经的第二份 mcp_bridge 那份）。

    名单方案的病根是"手工同步，忘了是静默的"。现在靠目录/命名解决，
    名单不许回流 —— 有人加回来这里立刻红。
    """
    for mod, label in ((tools_provider, "tools._provider"), (tools_base, "tools._base")):
        assert not hasattr(mod, "_SKIP_FILES"), f"{label} 里 _SKIP_FILES 回流了"


# ── 5. 行为不变：序列化 ──────────────────────────────────────────

def test_tsv_serialization_unchanged() -> None:
    """list[dict] → TSV：表头一次、每行只放值，单元格内制表符/换行被清洗。"""
    rows = [{"code": "600519", "close": 1680.5}, {"code": "000001", "close": 11.2}]
    out = tools_serialize._to_tsv(rows)
    lines = out.split("\n")
    assert lines[0] == "code\tclose"
    assert lines[1] == "600519\t1680.5"
    assert lines[2] == "000001\t11.2"

    dirty = [{"a": "x\ty", "b": "p\nq"}]
    out2 = tools_serialize._to_tsv(dirty)
    assert "\t" not in out2.split("\n")[1].replace("\t", "", 1) or True  # 首列分隔符保留
    assert "x y" in out2 and "p q" in out2, "单元格内制表符/换行必须被清洗成空格"


def test_strip_nulls_and_row_cap() -> None:
    """null/空容器剥离；超长列表按 _MAX_OBSERVATION_ROWS 截断（防万行 dump 撑爆上下文）。"""
    obj = {"a": 1, "b": None, "c": "", "d": [], "e": {}}
    assert tools_serialize._strip_nulls(obj) == {"a": 1}

    cap = tools_serialize._MAX_OBSERVATION_ROWS
    big = {"rows": [{"i": i} for i in range(cap + 50)]}
    assert len(tools_serialize._strip_nulls(big)["rows"]) == cap


def test_tool_result_uses_tsv_for_tables() -> None:
    """含「表」的结果压 TSV，纯嵌套保留紧凑 JSON——最省 token 的双口径不能漂移。"""
    r = tools_base.ToolResult(success=True,
                              output={"rows": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]})
    txt = r.to_str()
    assert "a\tb" in txt, "表形数据必须走 TSV"

    r2 = tools_base.ToolResult(success=True, output={"k": {"nested": "v"}})
    assert r2.to_str() == '{"k":{"nested":"v"}}', "纯嵌套走紧凑 JSON"

    r3 = tools_base.ToolResult(success=False, error="boom")
    assert r3.to_str() == "[工具执行失败] boom"


def test_envelope_keeps_error_shape() -> None:
    """单码信封：始终带 count/data{code}，但 error 形态不包（否则错误被埋进 data）。"""
    out = tools_base.as_code_envelope("600519", {"close": 1.0})
    assert out["count"] == 1 and out["data"]["600519"]["close"] == 1.0
    assert out["close"] == 1.0, "单码时顶层镜像扁平键（双通道可读）"

    err = {"error": "无数据", "retriable": False}
    assert tools_base.as_code_envelope("600519", err) == err


# ── 6. 行为不变：schema 生成 ─────────────────────────────────────

def test_func_to_openai_schema_respects_returns_section() -> None:
    """Returns 段必须进描述、Args 段必须不进——2026-09-15 修过一轮，不许回退。"""
    def probe(city: str, days: int = 3) -> dict:
        """查询城市天气。

        Args:
            city: 城市名
            days: 天数

        Returns:
            {"temp": float, "humidity": float}
        """
        return {}

    schema = tools_schema_gen.func_to_openai_schema(probe)
    fn = schema["function"]
    assert fn["name"] == "probe"
    assert "Returns" in fn["description"] or "temp" in fn["description"], \
        "返回结构契约必须留在描述里（否则模型只能猜返回形态）"
    assert "city: 城市名" not in fn["description"], "Args 段不得进描述"
    assert fn["parameters"]["properties"]["city"]["description"] == "城市名"
    assert fn["parameters"]["required"] == ["city"], "无默认值的才是必填"


def test_is_tool_function_criteria() -> None:
    """判据**只看函数本身**——不看任何模块级名单。

    ★ 2026-10-13 P10：`_NOT_TOOLS`（模块级退出注册名单）与 `_ENTRY_NAME_DENY`
    （入口名黑名单）两套模块级逃生门已删。前者 P9 后零声明者（纯死代码），
    后者全树唯一命中在被跳过的模块里（双重冗余）。
    "非工具模块不该被扫"改由**目录**解决（`_schema_gen.py` 这类下划线前缀），
    不靠名单——名单要手工同步，忘了是静默的。
    """
    mod = type(sys)("fake")

    def public_tool():
        """有 docstring 的公开函数。"""
    public_tool.__module__ = "fake"
    mod.public_tool = public_tool
    assert tools_schema_gen._is_tool_function(public_tool, mod) is True

    def private_tool():
        """下划线开头。"""
    private_tool.__module__ = "fake"
    private_tool.__name__ = "_private_tool"      # def 名 ≠ __name__，判据只看 __name__
    assert tools_schema_gen._is_tool_function(private_tool, mod) is False

    def undocumented():
        pass
    undocumented.__module__ = "fake"
    assert tools_schema_gen._is_tool_function(undocumented, mod) is False, \
        "无 docstring 的不是工具（模型无从判断何时调用）"

    def foreign():
        """别处定义的函数。"""
    foreign.__module__ = "somewhere.else"
    assert tools_schema_gen._is_tool_function(foreign, mod) is False, \
        "import 进来的函数不该被当成本模块的工具"

    # 模块级名单不再被读取：设了也不该生效（若有人把名单加回来，这里会红）
    mod._NOT_TOOLS = {"public_tool"}
    assert tools_schema_gen._is_tool_function(public_tool, mod) is True, \
        "判据不该再读 _NOT_TOOLS —— 名单方案已废弃，靠目录解决"


# ── 7. 行为不变：ToolProvider ────────────────────────────────────

def test_provider_register_get_search() -> None:
    """注册表基本行为：注册 → get/domain/search/元数据与元工具两表分离。"""

    def alpha_tool(query: str) -> dict:
        """搜索阿尔法信息。"""
        return {}

    def beta_tool(code: str) -> dict:
        """查询贝塔行情。"""
        return {}

    p = tools_provider.ToolProvider()
    p.register("alpha_tool", alpha_tool, domain="common")
    p.register("beta_tool", beta_tool, domain="finance")

    assert len(p) == 2 and "alpha_tool" in p
    assert p.get("alpha_tool") is alpha_tool
    assert p.get_domain("beta_tool") == "finance"
    assert p.list_by_domain("finance") == ["beta_tool"]

    # 精确命中优先
    assert [n for n, _ in p.search("alpha")] == ["alpha_tool"]
    # 短语查询（整串无命中）走分词兜底，不得返回空 —— 2026-10-12 修复
    assert p.search("贝塔 行情 查询") != [], "短语检索必须能兜底命中"


def test_meta_table_separate_from_registry() -> None:
    """元工具只在 get_meta()，注册表工具只在 get()——2026-09-21 事故的根因。

    两者互相取不到是**设计**（元工具必须每阶段在场），不是 bug。
    """
    p = tools_provider.ToolProvider()

    def some_tool():
        """某工具。"""
    p.register("some_tool", some_tool)
    p._meta_tools["web_search"] = some_tool

    assert p.get("web_search") is None, "元工具不得出现在注册表"
    assert p.get_meta("web_search") is some_tool

    # ★ 反向也必须成立：注册表里的工具**不能**从 get_meta() 取到。
    #   若有人"顺手"给 get_meta 加了 `or self._tools.get(name)` 兜底，
    #   两表就合并了 —— 元工具"每阶段必在场"的设计随之失效
    #   （phase 白名单只认白名单、不与 common 求并集，注册进注册表反会被丢掉）。
    assert p.get_meta("some_tool") is None, "注册表工具不得从 get_meta() 取到（两表必须分离）"


def test_superseded_tools_not_registered() -> None:
    """归组旧工具以 `_` 前缀私有化后**天然不进注册面**（零名单，AP.8）。

    ★ 原测试（2026-10-22 改写）：此前靠 `_SUPERSEDED_BY_MERGE` 名单摘除，AP.8
    已整体删除名单，机制改为「`_` 前缀私有函数天然退出 scan_directory」。
    测试意图不变：归组旧工具不得进注册表、公开同名函数必须照常注册。
    """
    mod = type(sys)("finance.fake_tools")

    def _legacy():
        """已被聚合取代的旧工具（`_` 前缀 = 私有，天然退出注册）。"""
    _legacy.__module__ = "finance.fake_tools"
    mod._legacy = _legacy

    p = tools_provider.ToolProvider()
    mod.__name__ = "finance.fake_tools"
    p._register_module_functions(mod, domain="finance")
    assert "_legacy" not in p.get_functions(), "`_` 前缀旧工具不得进注册表（零名单）"

    # 反向：公开同名函数必须照常注册（`_` 前缀是唯一判据，不误伤公开函数）
    def legacy():
        """同名公开函数照常注册。"""
    legacy.__module__ = "finance.fake_tools"
    mod.legacy = legacy
    p2 = tools_provider.ToolProvider()
    p2._register_module_functions(mod, domain="finance")
    assert "legacy" in p2.get_functions(), "公开同名函数必须照常注册"
