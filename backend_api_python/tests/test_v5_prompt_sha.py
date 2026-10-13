"""v5 P1-4：`prompt_sha` 恒 missing（G10）守卫（TDD，2026-10-13）

**已查证的成因（勿重复查）**：`utils/tracing.py::_repro_meta` 曾**硬编码 5 个文件名**
求 sha，但其中 `plan_system.txt` / `code_agent.yaml` / `intent_classifier.txt`
**早已删除**（实测 `app/agent/prompts/` 只剩 qd_core_base.txt / qd_system.txt /
skill_brew.txt / skill_revise.txt）⇒ `prompt_sha` 恒为 **3/5 `"missing"`**。

为什么这条值得修：`_repro_meta` 存在的唯一理由是回答
「这单结论是哪套配置给出的」。恒 missing = **静默降级**，复盘时对不上版本，
而这类问题只在需要复盘时才暴露 —— 那时已经晚了。

修法（**根治，不是打补丁**）：改成**扫描目录**，删了/加了自动跟随，
结构上不可能再出现恒 missing。硬编码清单再精确也会随文件增删再次腐化。

易错处：
  · `missing` 语义**必须保留**：文件真读不了时仍标 missing（不是把它从结果里
    删掉 —— 那会让"有问题"变成"看起来没问题"）。
  · 扫不到任何文件时**告警**而不是静默返回空 dict。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.utils import tracing


def _prompts_dir() -> Path:
    return Path(tracing.__file__).resolve().parent.parent / "prompts"


def test_prompt_sha_has_no_missing_entries():
    """★ 核心断言：实际存在的 prompt 文件，sha 不得为 missing。"""
    meta = tracing._repro_meta()
    sha = meta.get("prompt_sha") or {}
    assert sha, "prompt_sha 为空 —— 扫不到文件时应告警，不是静默返回空"
    missing = sorted(k for k, v in sha.items() if v == "missing")
    assert not missing, f"仍有恒 missing 的 prompt（G10 复发）: {missing}"


def test_prompt_sha_covers_all_prompt_files():
    """扫描口径：prompts/ 下每个 prompt 文件都要被纳入（删了自动消失、加了自动纳入）。"""
    meta = tracing._repro_meta()
    sha = meta.get("prompt_sha") or {}
    on_disk = sorted(p.name for p in _prompts_dir().glob("*")
                     if p.is_file() and p.suffix.lower() in tracing._PROMPT_SUFFIXES)
    assert on_disk, "prompts 目录下应有 prompt 文件（否则本测试无意义）"
    assert sorted(sha) == on_disk, (
        f"覆盖不一致：扫到 {sorted(sha)}，盘上 {on_disk}")


def test_removed_prompts_are_gone():
    """记录事实：这 3 个已删文件**不应**再出现在结果里（防有人把硬编码加回来）。"""
    meta = tracing._repro_meta()
    sha = meta.get("prompt_sha") or {}
    for gone in ("plan_system.txt", "code_agent.yaml", "intent_classifier.txt"):
        assert gone not in sha, f"{gone} 已删除却仍被求 sha ⇒ 硬编码清单被加回来了"


def test_env_can_narrow_the_set(monkeypatch):
    """`QD_PROMPT_SHA_FILES` 可显式收窄（只关心部分文件时）。"""
    monkeypatch.setenv("QD_PROMPT_SHA_FILES", "qd_system.txt")
    sha = (tracing._repro_meta().get("prompt_sha") or {})
    assert list(sha) == ["qd_system.txt"]


def test_unreadable_file_still_marked_missing(monkeypatch):
    """★ 语义保留：读不了仍标 missing —— 不许把"有问题"抹成"看起来没问题"。"""
    monkeypatch.setenv("QD_PROMPT_SHA_FILES", "no_such_prompt_xyz.txt")
    sha = (tracing._repro_meta().get("prompt_sha") or {})
    assert sha.get("no_such_prompt_xyz.txt") == "missing"


def test_sha_is_stable():
    """同一状态下两次调用结果必须一致（可复现性本身的要求）。"""
    a = tracing._repro_meta()["prompt_sha"]
    b = tracing._repro_meta()["prompt_sha"]
    assert a == b
