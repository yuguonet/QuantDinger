# -*- coding: utf-8 -*-
"""D-12：`_skill_seen` 的 LRU 容量上限守卫（2026-10-14）。

【为什么要有这个文件】
设计文档 `AGENT_DESIGN_v3.md` D-12 把 `_skill_seen` 记为「长跑内存增长点」：
它是 `{session_id: {已注入技能名}}`，只增不减 —— 服务不重启就永远涨。
O-11 小项打包把它列为待办。本文件把它**钉成可执行的事实**：
改坏了（退回无上限 dict、淘汰顺序写反、键口径不一致）就当场红。

【三条不可回退的口径（改代码前先读）】
1. **键必须是 `str(session_id)`**，与 `_get_agent` 同口径
   （设计 §易错点已明写：两者不一致 ⇒ 将来加清理逻辑时漏删）。
   所以传 int 进去，落进 dict 的键必须是 "123" 而不是 123。
2. **淘汰是 LRU（按访问刷新），不是 FIFO**：刚被访问的会话不能被淘汰，
   否则活跃会话每轮都重新灌一次正文（降 token 的目的直接作废）。
3. **容量走 env，不硬编码**（工程红线）：`QD_SKILL_SEEN_MAX`；脏值回落默认值，
   且**不允许出现 0/负数** —— 容量为 0 会让「刚插入就被淘汰」，
   去重表退化成永远空集（静默放大 token，看不出报错）。

【为什么淘汰是安全的】
`_skill_seen` 只影响「正文 vs 一行指针」。淘汰一个会话的条目 ⇒ 下次该会话
重新注入一次正文，**正确性不变**，只是多花一次 token。所以这里的 fail-open
方向是「宁可多注，不可误判为已注入」。
"""
from __future__ import annotations

import ast
import importlib
import os
from pathlib import Path

import pytest

from app.agent import qd_service as qs


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例前清掉开关，防止用例间互相污染（也防止本机 .env 把默认值改掉）。"""
    monkeypatch.delenv("QD_SKILL_SEEN_MAX", raising=False)
    yield


def _svc() -> "qs.QDAgentService":
    """构造一个不依赖模型/库的服务实例（只用到会话去重表，不需要跑链路）。"""
    return qs.QDAgentService()


# ── 1. 容量上限确实生效 ────────────────────────────────────────
def test_skill_seen_is_bounded_by_cap(monkeypatch):
    monkeypatch.setenv("QD_SKILL_SEEN_MAX", "3")
    s = _svc()
    for i in range(6):
        s._skill_seen_for(f"s{i}")
    assert len(s._skill_seen) == 3, (
        f"容量 3 时应只剩 3 个会话，实际 {len(s._skill_seen)}：{list(s._skill_seen)}")


def test_skill_seen_evicts_oldest_not_newest(monkeypatch):
    """淘汰必须丢最旧的 —— 丢最新的会让「刚注入过」的记忆立刻失效。"""
    monkeypatch.setenv("QD_SKILL_SEEN_MAX", "3")
    s = _svc()
    for i in range(4):
        s._skill_seen_for(f"s{i}").add("x")
    assert "s0" not in s._skill_seen, "s0 是最旧的，应被淘汰"
    assert {"s1", "s2", "s3"} == set(s._skill_seen), f"保留集合不对：{list(s._skill_seen)}"


def test_skill_seen_touch_refreshes_recency(monkeypatch):
    """LRU ≠ FIFO：回访过的会话必须被保住。"""
    monkeypatch.setenv("QD_SKILL_SEEN_MAX", "3")
    s = _svc()
    for k in ("s1", "s2", "s3"):
        s._skill_seen_for(k)
    s._skill_seen_for("s1")          # 回访 ⇒ s1 变最新，s2 变最旧
    s._skill_seen_for("s4")          # 溢出 ⇒ 应淘汰 s2
    assert "s2" not in s._skill_seen, "回访过 s1 后最旧的是 s2，淘汰对象写反了（退化成 FIFO）"
    assert {"s1", "s3", "s4"} == set(s._skill_seen), f"保留集合不对：{list(s._skill_seen)}"


# ── 2. 键口径（设计 §易错点：必须与 _get_agent 一致）─────────────
def test_skill_seen_key_is_str(monkeypatch):
    monkeypatch.setenv("QD_SKILL_SEEN_MAX", "5")
    s = _svc()
    s._skill_seen_for(123)           # 故意传 int
    assert "123" in s._skill_seen, (
        "键必须 str(session_id)；传 int 落进去的键是 '123' 而不是 123，"
        "否则与 _get_agent 不同口径，将来加清理会漏删")
    assert 123 not in s._skill_seen


def test_skill_seen_same_session_reuses_set(monkeypatch):
    monkeypatch.setenv("QD_SKILL_SEEN_MAX", "5")
    s = _svc()
    a = s._skill_seen_for("x")
    a.add("skill_a")
    b = s._skill_seen_for("x")
    assert b is a and "skill_a" in b, "同会话两次取必须是同一个 set（否则去重永远不生效）"


# ── 3. 容量走 env，不硬编码 ────────────────────────────────────
def test_cap_is_read_from_env(monkeypatch):
    monkeypatch.setenv("QD_SKILL_SEEN_MAX", "2")
    s = _svc()
    for i in range(5):
        s._skill_seen_for(f"s{i}")
    assert len(s._skill_seen) == 2


def test_default_cap_is_finite():
    """默认容量必须是有限正整数（无限 = 没有上限 = D-12 没做）。"""
    cap = qs._skill_seen_max()
    assert isinstance(cap, int) and cap > 0, f"默认容量必须是正整数，实际 {cap!r}"


@pytest.mark.parametrize("dirty", ["abc", "", "-5", "0", "1.5", "  "])
def test_dirty_cap_falls_back_or_clamps(monkeypatch, dirty):
    """脏值 / 非正值不能把去重表弄成永空集，也不能崩。"""
    monkeypatch.setenv("QD_SKILL_SEEN_MAX", dirty)
    cap = qs._skill_seen_max()
    assert isinstance(cap, int) and cap >= 1, f"脏值 {dirty!r} 应回落/夹紧到 >=1，实际 {cap!r}"
    s = _svc()
    seen = s._skill_seen_for("only")     # 容量为 1 时也必须留住刚访问的那一个
    seen.add("k")
    assert "only" in s._skill_seen, "容量为 1 时刚插入的会话不应被自己挤掉"
    assert "k" in s._skill_seen["only"], "去重集合必须可用（否则每轮重复灌正文）"


# ── 4. 接线守卫：正文注入路径必须走 LRU 助手 ────────────────────
def test_prefetch_does_not_use_bare_setdefault():
    """AST 守卫：`_skill_seen` 的写入只能经 `_skill_seen_for`。

    为什么用 AST 而不是跑一遍 _prefetch：_prefetch 需要 skills/memory 桩，
    桩出来的用例很脆；而"绕过 LRU 助手直接 setdefault"正是最容易回归的写法
    （改的人只想要一个 set，顺手就写回去了）。AST 直接钉住这个动作。
    """
    src = Path(qs.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "setdefault"):
            continue
        owner = fn.value
        # 只拦 _skill_seen.setdefault(...)（self._skill_seen 或别名）
        name = None
        if isinstance(owner, ast.Name):
            name = owner.id
        elif isinstance(owner, ast.Attribute):
            name = owner.attr
        if name == "_skill_seen":
            bad.append(node.lineno)
    assert not bad, (
        f"qd_service.py 第 {bad} 行仍在直接 `_skill_seen.setdefault(...)` —— "
        "这会绕过 LRU 容量上限（D-12 回归）。必须改走 self._skill_seen_for(session_id)")


def test_evicted_session_gets_fresh_set(monkeypatch):
    """被淘汰后再次访问 ⇒ 拿到空集合，fail-open 重新注正文（不报 KeyError）。"""
    monkeypatch.setenv("QD_SKILL_SEEN_MAX", "1")
    s = _svc()
    s._skill_seen_for("a").add("x")
    s._skill_seen_for("b")            # a 被淘汰
    again = s._skill_seen_for("a")
    assert again == set(), "被淘汰的会话应拿到全新空集（重注正文），而不是残留旧状态"
