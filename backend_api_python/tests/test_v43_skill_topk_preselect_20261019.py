# -*- coding: utf-8 -*-
"""阶段 3 守卫：技能 Top-K 渐进披露 + 可见性兜底 + 单一字符预算 + 受限探索位（2026-10-19）。

对应 `tmp/skill_system_rework_implementation_plan_20261018.md` §5/§14.1/§14.2/§14.4/§15.2：

  ① **Top-K 截断**：预选技能段不再随技能总数线性增长（7 技能 1678 字符、每技能 +94c，
     阶段 0 实测）。`build_skill_catalog_topk` 是单一入口：`QD_PRESELECT_SKILL_TOPK`
     默认 4 = 3 常规排序位 + 1 探索位；≤0 时**内部委托**旧 `build_skill_catalog`
     全量路径（逐位一致由构造保证，回退铁律）。
  ② **可见性兜底（§14.1/P14）**：截断后段内追加指针——尚有多少可用技能、经
     `search_resources(query, kind='skill')` 发现 / `read_skill()` 读取；不得暗示可编造未展示名称。
     未展示技能仍全量可查（不缩 `search_resources(query, kind='skill')` 全集）。
  ③ **单一字符预算（§14.4）**：`preselect_catalog_chars(tool_catalog, skill_section)`
     = 工具目录 + 技能段 + 固定模板骨架（system + user 空槽）。不新增技能字符平行
     阈值；`QD_PRESELECT_TIER_CHARS` 仍是唯一阈值 env；`schema_chars_of` 保持
     工具 schema 专用口径（F14 冻结），本函数是其"预选输入总预算"伴生度量。
     选中后才加载的技能正文**不进**计数。
  ④ **受限探索位（§14.2）**：探索候选 = eligible（active + 过 `QD_SKILL_MIN_SCORE`）
     且 TTL 内新发布（registry `recent_published_skill_ids`，DB 不可用 ⇒ 不探索，
     fail-closed）且未在曝光窗口内曝光（模块级曝光状态，有界轮换：同一技能不得
     因多次索引刷新重复获得无限探索优先级）。探索位只扩大候选可见性：不降低门槛、
     不强制 planner 的 `skill` 结果、不视为成功使用；零曝光/曝光未选中不是负向样本。
  ⑤ **空池不填充（§15.2）**：无达标技能时允许 null，不做最低填充；长尾经发现兜底可达。

接线：`qd_preselect._maybe_preselect_tools` 单一入口调 `build_skill_catalog_topk`
（k≤0 内部委托旧全量函数），report 记
`skill_catalog{mode/topk/total/shown/hidden/chars/funnel/explore}` 与
`preselect_chars`（影子对照字段，阶段 4 事件链消费）。

【v17j 既有守卫的 fixture 调整】`test_v17j_*` 三个声明工具补位用例的技能描述
原为薄夹具（"个股尽调"），在 Top-K 词面门下不过 `QD_SKILL_MIN_SCORE=2` ⇒ 技能段
空 ⇒ 补位契约无从验证。已把夹具描述补成与真实技能同型的措辞（真实 auto-agent
描述本就含"分析/复盘/调研"，标准问法"分析600519"可过门）。守卫意图（补位加法/
陈旧声明 fail-loud/常驻层双计）不变。

测试纪律：全部 monkeypatch（registry `_db` / 曝光状态 / env），不碰真库、不写真
技能目录、不联网。
"""
from __future__ import annotations

import ast
import json
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
QD_PRESELECT = BACKEND / "app" / "agent" / "qd_preselect.py"
TOOL_PRESELECT = BACKEND / "app" / "agent" / "preselect" / "tool_preselect.py"

from app.agent.chain import skill_registry as sr
from app.agent.preselect import candidate_index as ci

# 【为什么用「分析600519」】① 含 6 位代码 ⇒ 过域特征闸门；② 无域词命中 ⇒
# 不走确定性快路由（快路由 2026-10-16 上线、早于 skill 进预选，命中时 LLM
# 预选整段不跑 ⇒ 技能段无从验证——这是既有取舍，不在阶段 3 范围）；③ 词面
# token {600519,分,析,分析} 与 `_eligible_desc` 高重叠 ⇒ 可过最低相关度门。
_QUERY = "分析600519"


# ── 夹具 ─────────────────────────────────────────────────────────────

def _skill(name: str, desc: str, tools=()) -> dict:
    return {"name": name, "type": "markdown", "description": desc,
            "tools": list(tools)}


def _eligible_desc(tag: str = "个股") -> str:
    """与 `_QUERY` 词面高相关的描述（≥2 命中：分/析/分析/资金/筹码/分布…）。"""
    return f"{tag}资金面与筹码分布分析：K线走势复盘"


class _FakeSkills:
    """假技能适配器：list_skills/get/load_body 三件（形态同 skills_adapter）。"""

    def __init__(self, items):
        self._items = [dict(it) for it in items]

    def list_skills(self):
        return [dict(it) for it in self._items]

    def get(self, name):
        for it in self._items:
            if it.get("name") == name:
                return SimpleNamespace(name=name)
        return None

    def load_body(self, name):
        return "## 步骤\n1. 用工具取数\n2. 出结论\n"


def _patch_registry(monkeypatch, *, states=None, recent=None):
    """注入 registry 三查询（states=None ⇒ get_lifecycle_states 回落真实 fail-soft）。"""
    if states is not None:
        monkeypatch.setattr(sr, "get_lifecycle_states",
                            lambda ids: {i: states.get(i, "active") for i in ids})
    monkeypatch.setattr(sr, "get_active_versions",
                        lambda ids: {i: "v1" for i in ids})
    monkeypatch.setattr(sr, "recent_published_skill_ids",
                        lambda days: (None if recent is None else set(recent)))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """干净 env + 全新曝光状态（探索位轮换用例依赖进程内状态隔离）。"""
    for var in ("QD_PRESELECT_SKILL", "QD_PRESELECT_SKILL_TOPK", "QD_SKILL_TOPK",
                "QD_SKILL_MIN_SCORE", "QD_SKILL_EXPLORE_TTL_DAYS",
                "QD_PRESELECT_SKILL_TOOLS_MAX", "QD_PRESELECT_CATALOG_MAX",
                "QD_PRESELECT_TIER_CHARS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ci, "_EXPOSURE", OrderedDict())
    yield


# ═══════════════════════════════════════════════════════════════════
# 一、Top-K 截断：技能段不再随总数线性增长（§5/§15.2）
# ═══════════════════════════════════════════════════════════════════

def test_topk_caps_section_growth():
    """3 → 12 个达标技能：展示数恒为 K-1 常规位，段长只差指针计数位数。"""
    from app.agent.preselect import build_skill_catalog_topk
    from app.agent.preselect.tool_preselect import _SKILL_POINTER

    few = _FakeSkills([_skill(f"auto-s{i}", _eligible_desc()) for i in range(3)])
    many = _FakeSkills([_skill(f"auto-s{i}", _eligible_desc()) for i in range(12)])
    sec_f, map_f, meta_f = build_skill_catalog_topk(few, _QUERY)
    sec_m, map_m, meta_m = build_skill_catalog_topk(many, _QUERY)
    assert len(map_f) == 3 and len(map_m) == 3, \
        "K=4 ⇒ 3 常规位（无探索资格时不拿低相关技能填满第 4 位）"
    assert meta_m["hidden"] == 9 and meta_f["hidden"] == 0
    lines_f = [l for l in sec_f.splitlines() if "声明工具:" in l]
    lines_m = [l for l in sec_m.splitlines() if "声明工具:" in l]
    assert len(lines_f) == len(lines_m) == 3, \
        "段内技能行数恒定：9 个新技能不得多出一行（治阶段 0 的 +94c/技能）"
    assert len(sec_m) - len(sec_f) < len(_SKILL_POINTER.format(n=999)) + 8, \
        "总增量只允许是指针句本身（+计数位数），不得含技能行"
    assert "search_resources(query, kind='skill')" not in sec_f \
    and "search_resources(query, kind='skill')" in sec_m


def test_topk_zero_env_uses_legacy_full_catalog(monkeypatch):
    """QD_PRESELECT_SKILL_TOPK=0 ⇒ 旧 build_skill_catalog 全量路径（回退铁律）。"""
    from app.agent.preselect import build_skill_catalog, build_skill_catalog_topk

    skills = _FakeSkills([_skill("auto-agent", _eligible_desc()),
                          _skill("auto-backtest", "策略回测与参数对照")])
    legacy, legacy_map = build_skill_catalog(skills)
    monkeypatch.setenv("QD_PRESELECT_SKILL_TOPK", "0")
    sec, tmap, meta = build_skill_catalog_topk(skills, _QUERY)
    assert meta["topk"] == 0 and meta["mode"] == "full"
    assert tmap == legacy_map, "0 = 全量映射（补位依据不缩水）"
    assert sec == legacy, "回退路径与旧函数输出逐位一致（同env 同 query）"
    assert "auto-backtest" in sec, "回退路径不做过门滤"


# ═══════════════════════════════════════════════════════════════════
# 二、可见性兜底（§14.1/P14）：指针 + 全集不缩 + 禁编造
# ═══════════════════════════════════════════════════════════════════

def test_visibility_pointer_keeps_longtail_discoverable():
    """长尾技能稳定排在 Top-K 外：指针给计数与发现路径，全集仍可查可读。"""
    from app.agent.preselect import build_skill_catalog_topk

    items = [_skill(f"auto-hot{i}", _eligible_desc()) for i in range(3)]
    items += [_skill("auto-tail-a", "可转债套利与期权对冲策略"),
              _skill("auto-tail-b", "龙虎榜营业部席位跟踪")]
    skills = _FakeSkills(items)
    sec, tmap, meta = build_skill_catalog_topk(skills, _QUERY)
    assert "auto-tail-a" not in sec and "auto-tail-b" not in sec, \
        "未展示技能名不得出现在段内（指针也不许列名 ⇒ 防模型照抄幻觉）"
    assert "另有 2 个可用技能" in sec, "指针必须说明尚有多少可用技能"
    assert "search_resources(query, kind='skill')" in sec and "read_skill()" in sec, "必须给发现路径"
    assert "不得编造" in sec, "必须明令禁止编造未展示名称（§14.1）"
    # P14：Top-K 限制的是预选展示集，不缩可用技能全集
    names = [i["name"] for i in skills.list_skills()]
    assert "auto-tail-a" in names and "auto-tail-b" in names
    assert skills.load_body("auto-tail-a"), "read_skill 路径仍可读正文"


def test_no_pointer_when_nothing_hidden():
    """全部达标技能都已展示 ⇒ 不出指针（不制造无关 token）。"""
    from app.agent.preselect import build_skill_catalog_topk

    skills = _FakeSkills([_skill("auto-only", _eligible_desc())])
    sec, _tmap, meta = build_skill_catalog_topk(skills, _QUERY)
    assert meta["hidden"] == 0 and "另有" not in sec


# ═══════════════════════════════════════════════════════════════════
# 三、单一预选目录字符预算（§14.4）
# ═══════════════════════════════════════════════════════════════════

def test_preselect_catalog_chars_single_budget():
    """预算 = 工具目录 + 技能段 + 固定模板骨架；确定性；签名只收文本（正文进不来）。"""
    import inspect

    from app.agent.preselect import preselect_catalog_chars
    from app.agent.preselect.tool_preselect import _PRESELECT_FIXED_CHARS

    params = list(inspect.signature(preselect_catalog_chars).parameters)
    assert params == ["tool_catalog", "skill_section"], \
        "签名只收目录/段文本 ⇒ 选中后才加载的技能正文结构上不可能进计数（§14.4）"
    cat, sec = "候选工具目录（每行：工具名 | 说明 | 参数）：\nget_a | 取数\n", "技能段A"
    base = preselect_catalog_chars(cat, sec)
    assert base == len(cat) + len(sec) + _PRESELECT_FIXED_CHARS
    assert preselect_catalog_chars(cat, sec) == base, "同输入同输出（确定性）"
    assert preselect_catalog_chars(cat, sec + "多加一行技能") > base, \
        "增减技能候选必须反映在统一计数中"
    assert preselect_catalog_chars(cat, "") == len(cat) + _PRESELECT_FIXED_CHARS


def test_topk_section_excludes_loaded_body():
    """目录段只有名|场景|声明工具，不含技能正文（正文选中后才加载）。"""
    from app.agent.preselect import build_skill_catalog_topk

    skills = _FakeSkills([_skill("auto-agent", _eligible_desc(),
                                 tools=["agent_get_kline"])])
    sec, tmap, _meta = build_skill_catalog_topk(skills, _QUERY)
    assert "## 步骤" not in sec and "用工具取数" not in sec
    assert tmap["auto-agent"] == ["agent_get_kline"], "声明工具映射 = 补位依据"


def test_single_threshold_env_no_parallel_skill_char_knob():
    """§14.4：不新增平行的技能字符阈值；QD_PRESELECT_TIER_CHARS 仍是唯一阈值 env。"""
    src = TOOL_PRESELECT.read_text(encoding="utf-8")
    assert "QD_PRESELECT_TIER_CHARS" in src
    for bad in ("QD_SKILL_CHARS", "SKILL_CATALOG_CHARS", "QD_SKILL_BUDGET"):
        assert bad not in src, f"不得引入平行技能字符阈值 {bad}"


# ═══════════════════════════════════════════════════════════════════
# 四、受限探索位（§14.2）：轮换 / 有界 / 守门 / 不强制选择
# ═══════════════════════════════════════════════════════════════════

def _explore_skills():
    """3 个常规热门 + 2 个 TTL 内新发布且达标（auto-new-a/b）。"""
    return _FakeSkills(
        [_skill(f"auto-hot{i}", _eligible_desc()) for i in (1, 2, 3)]
        + [_skill("auto-new-a", _eligible_desc()),
           _skill("auto-new-b", _eligible_desc())])


def test_explore_rotates_and_is_bounded(monkeypatch):
    """探索位轮换：a→b→空置（同一技能不在曝光窗口内重复获得无限优先级）。"""
    from app.agent.preselect import build_skill_catalog_topk

    _patch_registry(monkeypatch, states={}, recent={"auto-new-a", "auto-new-b"})
    skills = _explore_skills()
    _s1, _m1map, meta1 = build_skill_catalog_topk(skills, _QUERY)
    assert meta1["explore"]["picked"] in ("auto-new-a", "auto-new-b")
    assert meta1["explore"]["picked"] in meta1["shown"], "探索位进展示集"
    assert meta1["explore"]["recent_published_known"] is True
    _s2, _m2map, meta2 = build_skill_catalog_topk(skills, _QUERY)
    assert meta2["explore"]["picked"] not in ("", meta1["explore"]["picked"]), \
        "轮换：第二次必须换另一个合格新技能"
    _s3, _m3map, meta3 = build_skill_catalog_topk(skills, _QUERY)
    assert meta3["explore"]["picked"] == "" and meta3["explore"]["exhausted"] is True, \
        "窗口内都已曝光 ⇒ 空置（有界曝光，不无限重复）"
    assert len(meta3["shown"]) == 3, "探索位不合格时不拿低相关技能填满"


def test_explore_gated_by_min_score_and_lifecycle(monkeypatch):
    """低相关 / 非 active 技能永不因探索位进入候选（§14.1/§14.2 验收）。"""
    from app.agent.preselect import build_skill_catalog_topk

    _patch_registry(monkeypatch,
                    states={"auto-off": "disabled"},
                    recent={"auto-cold", "auto-off"})
    items = [_skill(f"auto-hot{i}", _eligible_desc()) for i in (1, 2, 3, 4)]
    items += [_skill("auto-cold", "完全无关：烘焙与咖啡拉花技巧"),
              _skill("auto-off", _eligible_desc())]      # 达标但被禁用
    skills = _FakeSkills(items)
    sec, tmap, meta = build_skill_catalog_topk(skills, _QUERY)
    assert "auto-cold" not in sec, "不过最低相关度门槛的技能不进探索位"
    assert "auto-off" not in sec and "auto-off" not in tmap, \
        "非 active 技能被硬约束剔除（生命周期优先于探索）"
    assert meta["explore"]["picked"] == "" and meta["explore"]["candidates"] == 0
    assert meta["total"] == 5, \
        "disabled（auto-off）不计入可用技能总数；低相关（auto-cold）仍算可用"
    assert meta["funnel"]["eligible"] == 4, "cold 不过门槛 ⇒ 不进 eligible"


def test_explore_fail_closed_when_registry_unknown(monkeypatch):
    """发布状态不可知（DB 不可用）⇒ 不探索（宁可不曝光，不放过期资格）。"""
    from app.agent.preselect import build_skill_catalog_topk

    _patch_registry(monkeypatch, states={}, recent=None)   # None = 不可用
    skills = _explore_skills()
    _sec, _tmap, meta = build_skill_catalog_topk(skills, _QUERY)
    assert meta["explore"]["picked"] == ""
    assert meta["explore"]["recent_published_known"] is False


def test_explore_does_not_force_selection(monkeypatch):
    """探索位只扩大候选可见性：planner 显式 null 时 skill 为空（不自动选中）。"""
    from app.agent.preselect import build_skill_catalog_topk, parse_plan

    _patch_registry(monkeypatch, states={}, recent={"auto-new-a"})
    skills = _explore_skills()
    _sec, tmap, meta = build_skill_catalog_topk(skills, _QUERY)
    assert meta["explore"]["picked"] != "", "前置条件：探索位确实曝光了新技能"
    # 显式 null ⇒ 解析层尊重（不因曝光过就塞 skill）
    out = parse_plan(_plan_skill_json(None), ["get_realtime_quote"], 8,
                     valid_skills=sorted(tmap))
    assert out["skill"] == "", "曝光 ≠ 强制选择：显式 null 必须被尊重"


def _plan_skill_json(skill):
    p = {"goal": "贵州茅台综合诊断", "deliverables": ["现价"],
         "tools": ["get_realtime_quote"], "skill": skill}
    return json.dumps(p, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════
# 五、空池不填充 + 展示集即合法点名集（§15.2/§14.1）
# ═══════════════════════════════════════════════════════════════════

def test_empty_pool_no_minimum_fill():
    """无达标技能 ⇒ 无技能行、不做最低填充；指针保留（长尾可发现）。"""
    from app.agent.preselect import build_skill_catalog_topk

    skills = _FakeSkills([_skill("auto-backtest", "策略回测与参数对照")])
    sec, tmap, meta = build_skill_catalog_topk(skills, _QUERY)
    assert tmap == {} and meta["shown"] == []
    assert "auto-backtest |" not in sec, "不得最低填充（无匹配不强配）"
    assert "另有 1 个可用技能" in sec, "空池也要给发现兜底（P14）"
    assert meta["funnel"]["eligible"] == 0


def test_min_score_boundary_is_inclusive():
    """门槛语义 = 原始分**至少** 2（与 _SKILL_INJECT_MIN 注入门槛一致）：恰好 2 命中算达标。

    词面分 = 查询 token 在技能文本中的命中数（去重）。本夹具描述只含"分""析"
    两个命中 token（不含"分析"二元组、不含代码）⇒ 恰好压线。
    """
    from app.agent.preselect import build_skill_catalog_topk

    skills = _FakeSkills([_skill("auto-edge", "分与析两个字的说明，别的都不涉及")])
    sec, tmap, meta = build_skill_catalog_topk(skills, _QUERY)
    assert meta["funnel"]["eligible"] == 1, "恰好 2 命中必须算达标（>= 而非 >）"
    assert "auto-edge" in sec and meta["shown"] == ["auto-edge"]


def test_empty_skills_no_section_at_all():
    """无技能 ⇒ 整段不出现（与旧 fail-open 同形状：不是空标题占位）。"""
    from app.agent.preselect import build_skill_catalog_topk

    sec, tmap, meta = build_skill_catalog_topk(None, _QUERY)
    assert (sec, tmap) == ("", {}) and meta["mode"] == "topk" and meta["total"] == 0
    sec2, tmap2, meta2 = build_skill_catalog_topk(_FakeSkills([]), _QUERY)
    assert (sec2, tmap2) == ("", {}) and meta2["total"] == 0


# ═══════════════════════════════════════════════════════════════════
# 六、registry：TTL 内新发布技能（探索位资格真源）
# ═══════════════════════════════════════════════════════════════════

class _FakeCur:
    def __init__(self, rows=None, one=None):
        self._rows, self._one = rows or [], one
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_recent_published_skill_ids_reads_registry(monkeypatch):
    """新发布资格真源 = qd_skill_registry.created_at（首次登记，非版本刷新）。"""
    cur = _FakeCur(rows=[{"skill_id": "auto-a"}, {"skill_id": "auto-b"}])
    monkeypatch.setattr(sr, "_db", lambda: _FakeConn(cur))
    out = sr.recent_published_skill_ids(14)
    assert out == {"auto-a", "auto-b"}
    sql, params = cur.executed[0]
    assert "qd_skill_registry" in sql, "必须按注册行创建时间（修订不刷新资格）"
    assert "qd_skill_versions" not in sql
    assert 14 in (params or ()), "天数必须参数化（不字符串拼接进 SQL）"
    # SQL 形状契约（防"恒真/恒假"语义变异：mock 不执行 SQL，只能钉形状）
    assert " ".join(sql.split()) == (
        "SELECT skill_id FROM qd_skill_registry "
        "WHERE created_at >= NOW() - make_interval(days => %s)"), sql


def test_recent_published_db_failure_returns_none(monkeypatch):
    """DB 不可用 ⇒ None（调用方 fail-closed 不探索，不抛）。"""
    def _boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(sr, "_db", _boom)
    assert sr.recent_published_skill_ids(14) is None


# ═══════════════════════════════════════════════════════════════════
# 七、接线不变量（AST）+ 影子对照字段
# ═══════════════════════════════════════════════════════════════════

def _func_node(src: str, name: str):
    tree = ast.parse(src)
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
        stack.extend(ast.iter_child_nodes(node))
    raise AssertionError(f"找不到函数 {name}")


def test_meta_candidates_snapshot_for_events():
    """meta["candidates"] = 阶段 4 事件链契约：eligible 全集 + 版本 + 分数 + 曝光标记 + 理由。

    事件链按它算 eligible_but_not_exposed / exposed_not_selected；版本在选择时
    冻结 ⇒ 快照必须带 version（None = registry 不可用，留空不猜）。
    """
    from app.agent.preselect import build_skill_catalog_topk

    skills = _FakeSkills([_skill(f"auto-hot{i}", _eligible_desc()) for i in (1, 2, 3, 4)]
                         + [_skill("auto-tail", "可转债套利与期权对冲策略")])
    _sec, _tmap, meta = build_skill_catalog_topk(skills, _QUERY)
    cands = meta["candidates"]
    assert [c["name"] for c in cands] == ["auto-hot1", "auto-hot2",
                                          "auto-hot3", "auto-hot4"], \
        "快照 = eligible 全集（含落选，不含未过门槛的 tail）"
    assert all(set(c) >= {"skill_id", "version", "base_score", "exposed", "reasons"}
               for c in cands)
    assert [c["exposed"] for c in cands] == [True, True, True, False]
    assert all(c["base_score"] >= 2 for c in cands), "快照内全部过门槛"


def test_wiring_uses_topk_and_budget_without_touching_f14():
    """`_maybe_preselect_tools` 必须接 Top-K + 单一预算；F14 门控原样保留。"""
    node = _func_node(QD_PRESELECT.read_text(encoding="utf-8"),
                      "_maybe_preselect_tools")
    body = ast.unparse(node)
    assert "build_skill_catalog_topk" in body, "Top-K 接线被摘 ⇒ 治理静默失效"
    assert "preselect_catalog_chars" in body, "单一字符预算必须留痕（影子对照）"
    assert "tier_catalog_enabled" in body, "F14 分级门控不得被本阶段改动"
    # k<=0 的回退委托在 Top-K 函数内部（单一入口，调用方无分叉）
    tp_src = TOOL_PRESELECT.read_text(encoding="utf-8")
    topk_fn = ast.get_source_segment(
        tp_src, _func_node(tp_src, "build_skill_catalog_topk")) or ""
    assert "build_skill_catalog(" in topk_fn, \
        "k<=0 必须委托旧全量函数（回退逐位一致由构造保证）"


def test_report_records_shadow_fields(monkeypatch):
    """report 记 mode/topk/total/shown/hidden/chars/funnel/explore + preselect_chars。"""
    _patch_registry(monkeypatch, states={}, recent=None)
    tools = _fn_tools("get_realtime_quote", "agent_get_kline",
                      "get_chip_distribution", "get_capital_summary")
    items = [_skill(f"auto-hot{i}", _eligible_desc()) for i in (1, 2, 3, 4)]
    items += [_skill("auto-tail", "可转债套利与期权对冲策略")]
    s = _stand(tools, skills=_FakeSkills(items),
               response=_plan_skill_json("auto-hot1"))
    rep = s._maybe_preselect_tools(_QUERY)
    sc = rep["skill_catalog"]
    assert sc["mode"] == "topk" and sc["topk"] == 4
    assert sc["shown"] and sc["hidden"] == 2
    assert sc["chars"] > 0 and "funnel" in sc and "explore" in sc
    assert rep["preselect_chars"] >= sc["chars"], \
        "总预算 ≥ 技能段（还含工具目录与固定模板）"
    assert rep["skill"] == "auto-hot1"


# ── stand（与 v17j 同款：PreselectMixin + ToolFaceMixin + 假模型）──

def _def(name: str) -> dict:
    return {"function": {"name": name, "description": f"[test] {name}",
                         "parameters": {"properties": {}, "type": "object"}}}


class _Reg:
    def __init__(self, tools: dict):
        self._t = tools

    def list_tools(self):
        return list(self._t)

    def get(self, n):
        return self._t.get(n)

    def get_function_definitions(self):
        return [_def(n) for n in self._t]


def _fn_tools(*names, domain: str = "finance"):
    from app.agent.tools._fn_adapter import FnToolAdapter

    def _f() -> str:
        """[test] 占位。"""
        return ""

    return {n: FnToolAdapter(_f, domain=domain) for n in names}


class _FakeModel:
    def __init__(self, text: str):
        self.text = text
        self.calls: list = []

    def query(self, messages, **kw):
        self.calls.append(messages)
        return self.text


def _stand(tools: dict, *, skills=None, response: str = "") -> object:
    from app.agent.qd_preselect import PreselectMixin
    from app.agent.qd_tool_face import ToolFaceMixin

    class _S(PreselectMixin, ToolFaceMixin):
        def _text_of(self, resp):
            return str(resp)

    s = _S()
    s.tool_registry = _Reg(tools)
    s.config = SimpleNamespace(
        tool_preselect=True, tool_tier_mode="tiered",
        preselect_gate=True, preselect_hints=None,
        preselect_max_tools=8, extra_core_tools=[],
        on_demand_tools=[], max_active_domain_tools=20)
    s._activated = []
    s._defs_by_name = {}
    s._all_defs = []
    s.skills = skills
    s.model = _FakeModel(response)
    s._sync_tool_tiers()
    return s
