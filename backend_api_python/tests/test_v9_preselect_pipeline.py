# -*- coding: utf-8 -*-
"""预选管线的防降智测试（2026-10-13 P9）。

背景：`app/agent/tools/tool_preselect.py` 是全仓**补丁痕迹密度最高**的模块
（21 处/830 行），是多版改版的典型沉积。整理它最大的风险不是写错，而是
**悄悄改了输出却不自知**——lint 多裁一个、补位少补一个，线上表现为"某类
问题工具面变窄"，极难归因。

故本测试的第一道防线是**基线逐键对拍**（`tests/fixtures/p9_tool_preselect_baseline.json`，
改造前采集），判据是精确相等而非容差：返回值里有中文告警文案进 trace 供人肉
归因，容差守不住"文案被改掉"。

第二道防线是**结构判据**：预选管线必须住在 `app/agent/preselect/`（不是
`tools/` 自发现目录），且公开函数一个都不许进注册表。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from tests._p9_preselect_baseline import collect, diff, load_baseline  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PRESELECT_DIR = ROOT / "app" / "agent" / "preselect"
TOOLS_DIR = ROOT / "app" / "agent" / "tools"
TP = PRESELECT_DIR / "tool_preselect.py"

# 本次**有意**的行为变化（其余一律必须与基线逐键相等）
# ① 迁移：三个模块移出 tools/ 扫描目录 ⇒ `_NOT_TOOLS` 名单整体作废
# ② 补齐 parse_selection 容错链最后一环：结构化 JSON 无工具字段时回退逐字扫描
# ③ 【2026-10-14 G2】lint R4 **中间分支**（remain<MIN_FACE 但未达 R4b 门槛）补留痕：
#    原先不裁、不记、不告警 ⇒ trace 里 `pruned: []` 被读成"没发现零相关"，
#    实际是"发现了但决策被丢弃"。现补一条 warning（**行为仍是不裁**）。
#    ⚠ 基线连 `dirty` 一起变：保护名单场景原先 warnings 为空 ⇒ dirty=False，
#      现在多了留痕 ⇒ dirty=True。两者同源，一并登记。
EXPECTED_CHANGES = {
    "module",                                   # 新增探针：模块全名（迁移本身）
    "not_tools",                                # 名单作废 → 空
    "parse_selection.dict_names_in_goal",       # 容错链补齐 → 召回工具名
    "parse_plan.dict_names_in_goal.tools",      # 同上
    "lint.normal.trace.warnings",               # G2 留痕：多一条 R4 中间分支告警
    "lint.protected.trace.warnings",            # 同上（保护名单场景原先为空）
    "lint.protected.dirty",                     # 同源：warnings 非空 ⇒ dirty False→True
    # ④ 【2026-10-16 补登记】`check_domain_dict` 收录的可用工具名单发生变换：
    #    finance 域内工具合并（`run_backtest`→`agent_backtest_strategy`、
    #    `build_keyword_from_filters`→`agent_backtest_svc`、`daily` 摘除等）后
    #    基线未同步。核对方式：同一采集面在本轮改动前后对拍，落差条数恒为 8，
    #   仅此键未登记 ⇒ 与工具面改造（T1/T2/T4）无关，属前序工具合并的滞后项。
    "check_domain_dict",
    # ⑤ 【2026-10-17 P1-b】能力层目录**组面折叠**（`build_catalog_grouped`
    #    能力层段：40 行全列 ⇒ "capability（N 个）…例：代表6个+检索指针"）。
    #    动机：O2 残留（能力层 O(N)，300 工具爆掉）；P14 不变量保住：代表名
    #    仍可见 + list_tools(domain='capability') 指针可行动；env
    #    `QD_PRESELECT_CAP_FOLD=0` 回退全列（守卫 tests/test_v17i_*）。
    #    `catalog_grouped_kept`（合法点名集）**不变**——只缩呈现不缩合法集。
    "catalog_grouped",
    # ⑥ 【2026-10-20 提示词精简】preselect system 去掉诱导长推理的措辞：
    #    原「先想清楚这个任务要交付什么信息，再据此声明最小工具面」→
    #    现「直接声明最小工具面，不要展开推理」。动机：该措辞主动诱导长 CoT，
    #    实测预选单轮输出 544~3360 token（6 倍方差）、耗时 20.87~99.52s，
    #    隐藏推理占输出绝大多数。实质约束（最小工具面 / 关键链路不能断 /
    #    工具名逐字来自候选目录 / 已常驻无需再选）**一条未删**，只删引导展开
    #    推理的那半句。
    #    system 是 messages[0] ⇒ **所有含 system 的消息采集场景**都随之变化
    #    （8 个 _QUERIES 场景 + messages_no_hist 无历史分支，共 10 键）。
    #    故用**前缀豁免**而非逐个键登记（逐个登记不可维护：新增 _QUERIES
    #    场景就要回来补一条）。前缀口径不会误豁免真实的未登记变更——
    #    `messages` / `messages_no_hist` 这两个顶层键的**唯一**内容就是
    #    preselect 消息，不含 catalog / lint / parse_* 等其它采集面。
    # ⑦ 【2026-10-23 附录 AQ】schedule 域新建：5 个定时/告警工具从 common
    #    （必注入层）移入 tools/schedule/ 子目录走按需预选（用户裁定：窄场景
    #    工具不该每轮白占 ~3.8k 字符 schema）。`_PREFERRED`（域→首选工具）
    #    因此多出 `schedule: create_cron_job` 一条，其余键不变（工具总量 58
    #    不变、只换域）。必注入 schema 7,349→3,535 由设计机检 B8 对账。
    "preferred.schedule",
}

# 前缀豁免（见 ⑥）：preselect system 变更波及的所有消息场景键。
EXPECTED_PREFIXES = ("messages.", "messages_no_hist")


def _is_expected_change(key: str) -> bool:
    """键是否属登记在案的变更（精确集合 ∪ 前缀豁免，见 ⑥）。"""
    return key in EXPECTED_CHANGES or key.startswith(EXPECTED_PREFIXES)


def test_baseline_parity_except_expected():
    """改造前后逐键对拍：除登记在案的 12 处有意变更，其余必须一字不差。"""
    bad = [d for d in diff(collect(), load_baseline())
           if not _is_expected_change(d.split(":")[0].strip())]
    assert not bad, "预选管线输出发生未登记的变化：\n  " + "\n  ".join(bad[:30])


def test_parse_falls_back_to_name_scan_when_no_tools_field():
    """结构化 JSON 里**没有**工具字段时，必须回退"目录逐字扫描"。

    旧实现只在这一分支（parsed 既非 list 也非 dict）才扫描；模型把工具名写进
    goal/deliverables 却省略 tools 字段时直接返回空 ⇒ 白丢一次召回。
    """
    from app.agent.preselect import parse_selection

    names = ["get_realtime_quote", "technical_analysis", "get_fund_flow"]
    raw = '{"goal":"用 get_realtime_quote 查现价和走势","deliverables":["现价"]}'
    assert parse_selection(raw, names, 5) == ["get_realtime_quote"]


def test_parse_respects_explicit_empty_tools():
    """模型**显式**声明 `"tools": []` 时必须尊重，不得从正文捞名字硬塞。

    这条是上一条的边界：宁可不补，不自作主张（预选设计红线 6"误杀比漏杀烦人"
    的另一面 —— 也不能反过来硬塞）。
    """
    from app.agent.preselect import parse_selection

    names = ["get_realtime_quote", "technical_analysis"]
    raw = '{"goal":"纯聊天，提到 get_realtime_quote 但不需要","tools":[]}'
    assert parse_selection(raw, names, 5) == []


def test_pipeline_modules_outside_scan_dir():
    """预选管线必须在 `app/agent/preselect/`，**不在** `tools/` 自发现目录下。

    ★ 这是 P9 的根治点：此前它们住在 tools/ 下，靠 `_NOT_TOOLS` 名单"声明自己
    不该被注册"。名单要手工同步，忘了是**静默**的 —— `preselect_stats` 只登记了
    2 个名字，于是 `reset` / `report` 等 5 个计数器函数实际一直是模型可调工具
    （等于把"清空观测计数器"开放给模型）。移出扫描目录 ⇒ 这洞不存在。
    """
    from app.agent.tools._base import ToolProvider

    registered = set(ToolProvider.get_or_build().get_tool_names())
    for mod_name in ("tool_preselect", "preselect_cooc", "preselect_stats"):
        assert (PRESELECT_DIR / f"{mod_name}.py").is_file(), f"{mod_name} 不在 preselect 包"
        assert not (TOOLS_DIR / f"{mod_name}.py").exists(), f"{mod_name} 仍在 tools/ 下"
        mod = __import__(f"app.agent.preselect.{mod_name}", fromlist=["x"])
        assert mod.__name__ == f"app.agent.preselect.{mod_name}"
        pub = {n for n, o in vars(mod).items()
               if inspect.isfunction(o) and getattr(o, "__module__", "") == mod.__name__
               and not n.startswith("_")}
        assert pub, f"{mod_name} 没扫到公开函数"
        assert not (registered & pub), \
            f"{mod_name} 的公开函数被注册成了工具：{sorted(registered & pub)}"


def test_no_dangling_relative_import_in_package():
    """`preselect/` 包内不得有**指向不存在模块**的相对导入。

    ★ 2026-10-13 真实断裂（P9 搬迁遗漏）
      `preselect_cooc.py` 里那句 `from .base import ToolProvider` 是从 `tools/`
      搬出来时**没跟着改**的残留 —— preselect 包里根本没有 base.py。
      ImportError 被外层 `except Exception` 吞掉 ⇒ common 域与元工具永远进不了
      core 集合 ⇒ 召回统计口径偏大（注释写着"拼不齐就当空集 = 不剔除"）。
      **不报错、不崩、测试全绿**，只有对拍/静态扫描能抓。

    level 语义：level=1 是当前包，每 +1 上移一层。故目标目录 = 文件所在目录
    上移 (level-1) 层。
    """
    broken: list[str] = []
    for f in sorted(PRESELECT_DIR.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            base = f.parent
            for _ in range(node.level - 1):
                base = base.parent
            target = base if not node.module else base.joinpath(*node.module.split("."))
            if (target.with_suffix(".py")).is_file() or (target / "__init__.py").is_file():
                continue
            broken.append(f"{f.name}:{node.lineno} → {'.' * node.level}{node.module or ''}")

    assert not broken, (
        "preselect 包里有指向不存在模块的相对导入（ImportError 会被 except 吞掉 "
        "⇒ 口径静默失真）：\n  " + "\n  ".join(broken))


def test_no_not_tools_declaration_left():
    """三个模块都不该再**赋值** `_NOT_TOOLS`（不被扫描 ⇒ 名单是死代码）。

    只看 AST 赋值而不是字符串：注释里解释"为什么不再需要名单"是必要的，
    那正是防止后人把手写名单加回来的说明。
    """
    for mod_name in ("tool_preselect", "preselect_cooc", "preselect_stats"):
        tree = ast.parse((PRESELECT_DIR / f"{mod_name}.py").read_text(encoding="utf-8-sig"))
        declared = [n for n in ast.walk(tree)
                    if isinstance(n, (ast.Assign, ast.AnnAssign))
                    and any(getattr(t, "id", None) == "_NOT_TOOLS"
                            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
        assert not declared, f"{mod_name} 仍声明 _NOT_TOOLS（已不被扫描，是死代码）"


def test_index_cache_validates_identity_not_just_id():
    """索引缓存必须校验**对象身份**，不能只认 `id()`。

    `id()` 是内存地址：旧 defs 被回收后，新 defs 完全可能拿到同一地址 ⇒ 命中
    上一份工具面的索引。症状极隐蔽 —— 新激活的工具不在索引里 ⇒ lint 判它"零
    相关" ⇒ 被裁掉。

    ★ 为什么用"预置幽灵条目"而不是真的制造 id 复用：后者依赖 CPython 的
    内存分配时机，时灵时不灵（变异检验里会出现"改了却没红"的假阴性）。
    预置一条「id 相同但对象不同」的缓存条目，把场景**确定地**构造出来。
    """
    from app.agent.preselect import tool_preselect as T

    def _defs(name: str) -> dict:
        return {name: {"function": {"name": name, "description": "唯一工具 " + name,
                                    "parameters": {"properties": {}}}}}

    # 同一对象 ⇒ 命中缓存（不重复建索引）
    stable = _defs("stable_tool")
    assert T.get_tool_index(stable) is T.get_tool_index(stable)

    target = _defs("beta_tool")
    ghost = _defs("alpha_tool")            # 活着的"旧工具面"，只为占住一个对象
    T._INDEX_CACHE[id(target)] = (ghost, {"tok": {"alpha_tool"}})   # 模拟 id 撞车
    try:
        idx = T.get_tool_index(target)
    finally:
        T._INDEX_CACHE.pop(id(target), None)
    names = {n for s in idx.values() for n in s}
    assert "beta_tool" in names, "新工具面的索引没建出来"
    assert "alpha_tool" not in names, "命中了 id 撞车的旧索引（只认 id 的经典症状）"


def test_capability_import_has_single_entry():
    """能力层符号导入必须只有**一处**，不得再退化成逐字重复的 try/except。

    2026-10-13 P9 前：`capability_domain` 与 `_near_dup` 各写了一份**完全相同的
    双层 try/except**（第二层永远不会给出不同结果，纯复制残留）。
    """
    src = TP.read_text(encoding="utf-8-sig")
    hits = [n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.ImportFrom)
            and (n.module or "").split(".")[0] == "capabilities"]
    assert len(hits) == 1, f"capabilities 导入点应为 1 处，实际 {len(hits)} 处"
    assert "_capability_symbol" in src


def test_preselect_package_reexports_call_sites():
    """`app/agent/preselect/__init__` 必须覆盖 `qd_preselect` 用到的全部名字。

    迁移后调用方写成 `from .preselect import X`；若 __init__ 漏导出，ImportError
    会在运行到预选时才炸（fail-open 会把它吞成"预选没生效"，极难察觉）。
    """
    import app.agent.preselect as pkg

    src = (ROOT / "app" / "agent" / "qd_preselect.py").read_text(encoding="utf-8-sig")
    used: set = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module == "preselect":
            used |= {a.name for a in node.names}
    assert used, "没解析到 qd_preselect 的预选导入"
    missing = sorted(used - set(pkg.__all__))
    assert not missing, f"preselect 包未导出：{missing}"


@pytest.mark.parametrize("query,expected", [
    ("帮我看看 600519 的行情", ["get_realtime_quote"]),   # 域命中 ⇒ R1 补位
    ("今天北京天气怎么样", []),                            # 无域命中 ⇒ 宁可不补
    ("", []),
])
def test_lint_never_raises(query, expected):
    """lint 的纯 fail-open 底线：任何输入都不许抛（抛了就丢工具面）。

    空工具面也不代表空返回：命中数据域时 R1 会**补位**（宁多不可断），
    这是设计而非缺陷 ⇒ 只验"不抛 + 返回名单"，不验"必须为空"。
    """
    from app.agent.preselect import lint_selection, apply_lint

    rep = lint_selection(query, [], available_names=["get_realtime_quote"],
                         index={}, plan_text="", parsed=False)
    assert apply_lint([], rep) == expected
