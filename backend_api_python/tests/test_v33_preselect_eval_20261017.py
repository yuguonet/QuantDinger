# -*- coding: utf-8 -*-
"""F23（O-7 + D-16）测试：观测计数器落盘 + heldout 召回批跑闭环。

覆盖：
  ① stats_path 默认/ env 覆盖（镜像 pairs_path）；
  ② snapshot 追加一行 JSONL（跨进程可读）；
  ③ snapshot 坏路径 fail-open 返回 None（不抛）；
  ④ load_snapshots 跳坏行；
  ⑤ aggregate_snapshots 跨重启正确（按 boot 分组取末条再求和）；
  ⑥ eval_heldout 委托 heldout_recall（口径单一真源）；
  ⑦ scripts/preselect_eval.main 输出合并 JSON + 退出码守 pass_95；
  ⑧ 公开面导出。

设计约束：全部进程内、无 DB/无网络；tmp_path + monkeypatch 隔离落盘。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.agent.preselect import preselect_stats as PS
from app.agent.preselect import preselect_cooc as PC

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load_script():
    """按文件路径加载批跑脚本（不依赖 scripts 是否为包）。"""
    spec = importlib.util.spec_from_file_location(
        "preselect_eval", _SCRIPTS / "preselect_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── ① 路径 ──────────────────────────────────────────────────────────────
def test_stats_path_default_under_agent_traces():
    PS.reset()
    p = PS.stats_path()
    assert p.name == "preselect_stats.jsonl"
    assert p.parent.name == "traces"
    assert "app" in p.parts and "agent" in p.parts


def test_stats_path_env_override(tmp_path, monkeypatch):
    target = tmp_path / "custom_stats.jsonl"
    monkeypatch.setenv("QD_PRESELECT_STATS_FILE", str(target))
    assert PS.stats_path() == target


# ── ② snapshot 写行 ─────────────────────────────────────────────────────
def test_snapshot_appends_jsonl_line(tmp_path, monkeypatch):
    target = tmp_path / "stats.jsonl"
    monkeypatch.setenv("QD_PRESELECT_STATS_FILE", str(target))
    PS.reset()
    PS.record_selected("ok")
    PS.record_empty_face("q", "empty", 2)
    out = PS.snapshot()
    assert out and out["file"] == str(target)
    assert target.exists()
    lines = [ln for ln in target.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    # record_empty_face 也是「一次预选」（计入 total 分母），故 total=2 / empty=1
    assert rec["total"] == 2 and rec["empty"] == 1
    assert "boot_ts" in rec and "pid" in rec
    assert rec.get("recent_samples") is None      # 快照剔除大字段
    PS.snapshot()
    lines2 = [ln for ln in target.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines2) == 2                        # 追加，不覆盖


# ── ③ fail-open ─────────────────────────────────────────────────────────
def test_snapshot_fail_open_on_bad_path(monkeypatch):
    PS.reset()
    # 把落盘路径指向一个无法创建的父路径（Windows：非法字符）
    monkeypatch.setenv("QD_PRESELECT_STATS_FILE", "Z:\\no_such_drive\\x\\s.jsonl")
    assert PS.snapshot() is None                   # 不抛，返回 None


# ── ④ 读回跳坏行 ────────────────────────────────────────────────────────
def test_load_snapshots_skips_bad_lines(tmp_path, monkeypatch):
    target = tmp_path / "stats.jsonl"
    monkeypatch.setenv("QD_PRESELECT_STATS_FILE", str(target))
    target.write_text(
        json.dumps({"boot_ts": 1.0, "total": 3}) + "\n"
        + "{ this is not json }\n"
        + "\n"
        + json.dumps({"boot_ts": 1.0, "total": 5}) + "\n",
        encoding="utf-8")
    recs = PS.load_snapshots()
    assert [r["total"] for r in recs] == [3, 5]


def test_load_snapshots_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("QD_PRESELECT_STATS_FILE", str(tmp_path / "none.jsonl"))
    assert PS.load_snapshots() == []


# ── ⑤ 跨重启聚合 ────────────────────────────────────────────────────────
def test_aggregate_snapshots_cross_restart():
    recs = [
        {"boot_ts": 1.0, "total": 2, "empty": 1, "gate_skip": 0, "gate_miss": 0},
        {"boot_ts": 1.0, "total": 4, "empty": 1, "gate_skip": 1, "gate_miss": 2},  # 同进程末条
        {"boot_ts": 2.0, "total": 3, "empty": 0, "gate_skip": 0, "gate_miss": 1},
        {"boot_ts": 2.0, "total": 6, "empty": 2, "gate_skip": 0, "gate_miss": 1},  # 重启后末条
    ]
    agg = PS.aggregate_snapshots(recs)
    assert agg["boots"] == 2
    # 末条求和：boot1(total4,empty1,gate1,gm2) + boot2(total6,empty2,gate0,gm1)
    assert agg["total"] == 10 and agg["empty"] == 3
    assert agg["gate_skip"] == 1 and agg["gate_miss"] == 3
    assert agg["empty_rate"] == round(3 / 10, 4)
    assert agg["gate_miss_rate"] == round(3 / (10 + 3), 4)


def test_aggregate_snapshots_empty():
    agg = PS.aggregate_snapshots([])
    assert agg["boots"] == 0 and agg["total"] == 0 and agg["empty_rate"] == 0.0


# ── ⑥ eval_heldout 委托 ─────────────────────────────────────────────────
def test_eval_heldout_delegates_to_heldout_recall(monkeypatch):
    monkeypatch.setattr(PC, "_core_names", lambda: {"update_plan"})
    pairs = [
        {"query": "打板", "tools": ["get_limit_pool", "update_plan"]},
        {"query": "资金流", "tools": ["get_fund_flow"]},
    ]
    # 固定面：两个按需工具都在面内 ⇒ 召回 100%
    monkeypatch.setattr(PC, "_domain_face", lambda q, k: ["get_limit_pool", "get_fund_flow"])
    out = PC.eval_heldout(pairs, k=8)
    assert out["n"] == 2
    assert out["skipped_core_only"] == 1                 # update_plan 被剔除
    assert out["heuristic"]["recall"] == 1.0
    assert out["heuristic"]["pass_95"] is True
    assert out["weighted"] is None                       # 未传 weights


def test_eval_heldout_skips_core_only_pairs(monkeypatch):
    monkeypatch.setattr(PC, "_core_names", lambda: {"update_plan"})
    pairs = [{"query": "闲聊", "tools": ["update_plan"]}]
    out = PC.eval_heldout(pairs, k=8)
    assert out["n"] == 0 and out["skipped_core_only"] == 1
    assert out["heuristic"] is None


# ── ⑦ 批跑脚本 ─────────────────────────────────────────────────────────
def _write_pairs(path: Path, n: int) -> None:
    with path.open("w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"query": f"q{i}", "tools": ["get_tool_x"]}) + "\n")


def test_preselect_eval_main_reports_json(tmp_path, monkeypatch, capsys):
    mod = _load_script()
    pairs_file = tmp_path / "pairs.jsonl"
    _write_pairs(pairs_file, 12)
    monkeypatch.setattr(mod, "eval_heldout",
                        lambda *a, **k: {"n": 2, "skipped_core_only": 0,
                                         "heuristic": {"recall": 1.0, "pass_95": True, "calls": 2},
                                         "weighted": None})
    rc = mod.main(["--pairs", str(pairs_file), "--stats", str(tmp_path / "s.jsonl"),
                   "--k", "8"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["pairs_total"] == 12
    assert payload["recall"]["heuristic"]["pass_95"] is True
    assert "stats" in payload and "gate" in payload


def test_preselect_eval_main_exit1_below_95(tmp_path, monkeypatch, capsys):
    mod = _load_script()
    pairs_file = tmp_path / "pairs.jsonl"
    _write_pairs(pairs_file, 12)
    monkeypatch.setattr(mod, "eval_heldout",
                        lambda *a, **k: {"n": 2, "skipped_core_only": 0,
                                         "heuristic": {"recall": 0.5, "pass_95": False, "calls": 2},
                                         "weighted": None})
    rc = mod.main(["--pairs", str(pairs_file), "--stats", str(tmp_path / "s.jsonl")])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1 and payload["gate"] == "below95"


def test_preselect_eval_main_exit2_no_data(tmp_path, monkeypatch, capsys):
    mod = _load_script()
    pairs_file = tmp_path / "pairs.jsonl"
    _write_pairs(pairs_file, 12)
    monkeypatch.setattr(mod, "eval_heldout",
                        lambda *a, **k: {"n": 0, "skipped_core_only": 0,
                                         "heuristic": None, "weighted": None})
    rc = mod.main(["--pairs", str(pairs_file), "--stats", str(tmp_path / "s.jsonl")])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2 and payload["gate"] == "no_data"


# ── ⑧ 公开面导出 ────────────────────────────────────────────────────────
def test_public_api_exports():
    from app.agent import preselect as P
    for name in ("stats_path", "snapshot", "load_snapshots", "aggregate_snapshots"):
        assert hasattr(P, name), name
        assert name in P.__all__, name
    assert hasattr(P, "eval_heldout") and "eval_heldout" in P.__all__


def test_new_public_fns_not_registered_as_tool():
    """新增公开函数不得被扫成模型工具（与 test_not_tools_coverage 同源判据）。"""
    from app.agent.tools._base import ToolProvider
    registered = set(ToolProvider.get_or_build().get_tool_names())
    for name in ("stats_path", "snapshot", "load_snapshots",
                 "aggregate_snapshots", "eval_heldout"):
        assert name not in registered, name
