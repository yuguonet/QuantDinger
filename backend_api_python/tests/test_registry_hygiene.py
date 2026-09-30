# -*- coding: utf-8 -*-
"""登记表防烂尾测试（2026-09-30 提智批，词典防烂尾扩展）。

背景：_DATA_DOMAINS 曾整行失效而无人报警（intel_news 事件，domain_meta.py 头注释）。
W20a/b/c 只锁了数据域词典与 R2 依赖登记；本文件把其余共享分类学也上断言——
静默烂词典是"悄悄降智"的惯用通道。

纯确定性、零重依赖：domain_registry / tools/*/domain_meta 均为纯数据模块。
"""
import re
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1] / "app" / "agent"
sys.path.insert(0, str(AGENT_DIR))

from domain_registry import classify_verb, normalize_chain_name, specs  # noqa: E402

_CODE_RE = re.compile(r"(?<!\d)\d{4,6}(?!\d)")
_IDENT_RE = re.compile(r"[a-z_]+")


def test_specs_loaded():
    s = specs()
    assert "finance" in s, "finance 域分类学未装载（domain_meta 装载链断）"
    assert s["finance"].verb_nouns, "finance verb_nouns 为空"
    assert "knowledge" in s


def test_verb_nouns_generic():
    """verb/noun 词表不得含实体码或大写——链名归一与 skill 命名依赖该词表泛化。"""
    for name, spec in specs().items():
        for verb, noun in spec.verb_nouns.items():
            assert _IDENT_RE.fullmatch(verb), f"{name}.verb_nouns 键异常: {verb}"
            assert _IDENT_RE.fullmatch(noun), f"{name}.verb_nouns 值异常: {noun}"
            assert not _CODE_RE.search(verb + noun), f"{name} 词表含实体码: {verb}/{noun}"


def test_intent_verbs_registered():
    """意图动词必须登记 noun 兜底——否则链名落 unknown，酿造候选直接被排除。"""
    for name, spec in specs().items():
        missing = set(spec.intent_verbs or ()) - set(spec.verb_nouns)
        assert not missing, f"{name} 意图动词缺 noun 登记: {missing}"


def test_classify_verb_chain_name_digit_free():
    """classify_verb → 链名组合全程无实体码（与 tracing 写入层同构）。"""
    for verb in ("analysis", "query", "screen", "compare", "code", "explain"):
        cls = classify_verb(verb)
        if not cls:
            continue
        name = normalize_chain_name(
            f"{cls.get('domain', 'unknown')}+{verb}+{cls.get('noun', 'unknown')}")
        assert not _CODE_RE.search(name), f"verb={verb} 产出含码链名: {name}"
