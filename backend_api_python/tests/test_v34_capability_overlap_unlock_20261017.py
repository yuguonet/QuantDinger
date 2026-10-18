# -*- coding: utf-8 -*-
"""F20：能力层 doc 解锁 func_overlap（G4）+ admission 口径核对（G1/G2）。

【病灶 · 实测 2026-10-17】
  · `admission.json`：entries=71 / admitted=68 / **doc 字段 0 条（全空）**，review_note=71 条。
  · `func_overlap.code_judge_pair` 的「文档共同词」分支要求 cap_doc 非空，
    cap doc 恒空 ⇒ 68 条能力该分支**永不命中**，全落 `different/无重合信号`
    （域内让位 R0 只能靠名字清单，不是语义）。
  · `fingerprint_inventory` 的能力条目哈希**不含 doc**
    ⇒ 即便补了 doc，`overlap_cache.json` 命中也会掩盖修复。

【修法】doc 由 `review_note` 派生（**单一事实源**，不复制文本，避免两份各自漂移）；
        指纹纳入 doc；补问责摘要（无描述计数 → 明示"对账结论不可信"）。
"""
import json

from app.agent.capabilities import func_overlap as fo
from app.agent.capabilities import loader


def _adm(entries, note=""):
    return {
        "version": 1,
        "note": note,
        "defaults": {"timeout_s": 60, "max_chars": 8000},
        "entries": entries,
    }


def _write(tmp_path, data):
    p = tmp_path / "admission.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


# ── ① doc 由 review_note 派生 ───────────────────────────────────
def test_doc_derived_from_review_note(tmp_path):
    p = _write(tmp_path, _adm([{
        "module": "app.x", "name": "f", "admitted": True,
        "review_note": "只读 DB；返回实时行情快照",
    }]))
    metas = loader.load_admitted_meta(p)
    assert len(metas) == 1
    assert metas[0]["doc"] == "只读 DB；返回实时行情快照"


# ── ② 显式 doc 优先（未来真填了就不被派生覆盖）─────────────────────
def test_explicit_doc_wins_over_review_note(tmp_path):
    p = _write(tmp_path, _adm([{
        "module": "app.x", "name": "f", "admitted": True,
        "doc": "显式说明", "review_note": "审核备注",
    }]))
    assert loader.load_admitted_meta(p)[0]["doc"] == "显式说明"


# ── ③ 派生 doc 截断到 160（与工具侧 _doc_first 同口径）─────────────
def test_doc_capped_at_160(tmp_path):
    p = _write(tmp_path, _adm([{
        "module": "app.x", "name": "f", "admitted": True,
        "review_note": "x" * 500,
    }]))
    assert len(loader.load_admitted_meta(p)[0]["doc"]) == 160


# ── ④ 真实清单解锁：68 条准入全部有非空 doc ────────────────────────
def test_real_admission_all_admitted_have_doc():
    metas = loader.load_admitted_meta()
    assert metas, "admission.json 应可读"
    missing = [m["name"] for m in metas if not (m.get("doc") or "").strip()]
    assert not missing, (
        f"仍有能力无描述（func_overlap 文档共同词对它们失效）: {missing[:10]}")


# ── ⑤ 解锁语义：doc 有 ≥2 共同词 → unsure（修复前永不发生）─────────
def test_code_judge_detects_overlap_when_doc_present():
    cap = {"name": "cap_x", "doc": "只读：资金流向与涨停板块"}
    tool = {"name": "tool_y", "doc": "获取资金流向与涨停板块数据"}
    verdict, reason = fo.code_judge_pair(cap, tool)
    assert verdict == "unsure", reason
    assert "共同词" in reason
    # 反证：doc 空时落 different —— 这正是修复前的行为
    v2, r2 = fo.code_judge_pair({"name": "cap_x", "doc": ""}, tool)
    assert v2 == "different" and r2 == "无重合信号"


# ── ⑥ 指纹纳入 cap doc（否则缓存掩盖修复）──────────────────────────
def test_fingerprint_includes_cap_doc():
    tools = [{"name": "t", "doc": "d"}]
    a = fo.fingerprint_inventory(tools, [{"module": "m", "name": "c", "doc": "x"}])
    b = fo.fingerprint_inventory(tools, [{"module": "m", "name": "c", "doc": "y"}])
    assert a != b, "cap doc 变化必须使指纹变化，否则缓存命中掩盖修复"


# ── ⑦ 问责摘要：无描述计数 + reason 分布 ──────────────────────────
def test_summarize_report_accountability():
    report = {
        "c1": {"decision": "keep", "reason": "无重合信号"},
        "c2": {"decision": "shadowed", "reason": "同名"},
        "c3": {"decision": "keep", "reason": "无重合信号"},
    }
    cap_meta = [
        {"name": "c1", "doc": "d"},
        {"name": "c2", "doc": ""},
        {"name": "c3", "doc": "d"},
    ]
    st = fo.summarize_report(report, cap_meta)
    assert st["caps"] == 3 and st["shadowed"] == 1
    assert st["doc_missing"] == 1
    assert st["by_reason"]["无重合信号"] == 2


# ── ⑧ admission 顶层 note 已登记真实计数（历史数字不擅改，只追加）──
def test_admission_note_registers_actual_counts():
    note = json.loads(loader._ADMISSION_PATH.read_text(encoding="utf-8"))["note"]
    assert "entries=71" in note and "admitted=68" in note, note[-200:]
