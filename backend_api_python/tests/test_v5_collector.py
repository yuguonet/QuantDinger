"""v5 D4：盘后采集编排守卫（TDD，2026-10-13）

**为什么这一批最重要**：D1（情境帧）与 D2（分层配额）只有函数、**没人调用**
⇒ 数据一天都不会攒。而 C 类情境**不可重建**（今天没记，事后补不回来）。
本文件守的是「D1/D2 的价值真的兑现了」。

守的四条性质：
  ① 两项都落盘；② 同日重跑**幂等**（覆盖不追加，补跑不重复计票）；
  ③ 分项隔离（一项挂不拖累另一项）；④ **不静默成功**（失败必进 errors 且 ok=False）。
  ⑤ 盘后 worker **真的接线了**（防日后接线被删而无人察觉）。

易错处：
  · 测试用 `tmp_path` 覆盖落盘目录 —— 必须 monkeypatch `context_frame.FRAMES_DIR`
    **模块属性**（它是模块级常量，setenv 已来不及）。
  · 假源要让 `build_frame` / `pick_daily_universe` 都能跑通，否则测试查的是
    "源坏了"而不是"编排坏了"。
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from app.agent.chain import collector, context_frame, evaluator


def _patch(monkeypatch, mod_short: str, attr: str, value):
    """同时 patch `chain.X` 与 `app.agent.chain.X`。

    ★ **已查证的项目事实（勿忘）**：这两条路径**不是同一个模块对象** ——
      `app/agent` 目录被单独加进 `sys.path`，故 `chain.collector` 与
      `app.agent.chain.collector` 各有一份（各自的模块级常量互相独立）。
      生产代码（`collector.py`）用的是**短路径** `from chain import ...`，
      而测试用**长路径** import ⇒ 只 patch 长路径**不会生效**，
      表现为「patch 了却像没 patch」，极难排查（曾让 4 条测试假失败）。
    """
    hit = 0
    for name in (f"chain.{mod_short}", f"app.agent.chain.{mod_short}"):
        try:
            m = importlib.import_module(name)
        except Exception:
            continue
        monkeypatch.setattr(m, attr, value, raising=True)
        hit += 1
    assert hit > 0, f"patch 未命中任何模块: {mod_short}.{attr}"


# ── 假源：让两侧都能跑通的最小形态 ────────────────────────────────────
def _fake_frame_sources():
    return {
        "index_kline": lambda code, days: [
            {"date": f"2026-10-{d:02d}", "close": 100.0 + d, "open": 99.0,
             "high": 101.0 + d, "low": 98.0, "volume": 1000}
            for d in range(1, 25)],
        "limit_pool": lambda: {"count": 46, "zt": {"stocks": []},
                               "broken": {"stocks": []}},
        "hot_sectors": lambda: {"count": 3, "data": [{"change_pct": 2.0}]},
    }


def _fake_sample_sources():
    return {
        "limit_pool": lambda: {"zt": {"stocks": [{"stock_code": f"60{i:04d}"}
                                                 for i in range(40)]},
                               "broken": {"stocks": []}},
        "dragon_tiger": lambda: [],
        "hot_rank": lambda n: [{"code": f"00{i:04d}"} for i in range(300)],
        "sector_stocks": lambda bc, limit=10: [],
        "all_codes": lambda: [f"70{i:04d}" for i in range(500)],
    }


@pytest.fixture()
def _tmpdirs(tmp_path, monkeypatch):
    """把帧与样本的落盘目录都指到 tmp_path。**改模块属性**，不是 setenv。"""
    cf, cu = tmp_path / "frames", tmp_path / "universe"
    _patch(monkeypatch, "context_frame", "FRAMES_DIR", str(cf))
    monkeypatch.setenv("QD_COLLECT_UNIVERSE_DIR", str(cu))
    return cf, cu


# ═══════════════════════════════════════════════════════════════
#  ① 两项都落盘
# ═══════════════════════════════════════════════════════════════

def test_collection_writes_both(_tmpdirs, monkeypatch):
    cf, cu = _tmpdirs
    _patch(monkeypatch, "context_frame", "_default_sources", _fake_frame_sources)
    _patch(monkeypatch, "sampler", "_default_sources", _fake_sample_sources)

    r = collector.run_daily_collection(as_of="2026-10-12", n=20)
    assert r["ok"], f"采集应成功，errors={r['errors']}"
    assert r["as_of"] == "2026-10-12"
    assert r["frame_path"] and Path(r["frame_path"]).exists(), "情境帧没落盘"
    assert r["universe_path"] and Path(r["universe_path"]).exists(), "样本没落盘"
    # 内容真写进去了（不是空文件）
    assert Path(r["frame_path"]).read_text(encoding="utf-8").strip()
    assert Path(r["universe_path"]).read_text(encoding="utf-8").strip()


def test_universe_record_is_readable(_tmpdirs, monkeypatch):
    cf, cu = _tmpdirs
    _patch(monkeypatch, "context_frame", "_default_sources", _fake_frame_sources)
    _patch(monkeypatch, "sampler", "_default_sources", _fake_sample_sources)

    collector.run_daily_collection(as_of="2026-10-12", n=20)
    rows = collector.load_universe("2026-10-12")
    assert len(rows) == 1
    rec = rows[0]
    assert rec["as_of"] == "2026-10-12"
    assert rec["universe"]["items"], "样本明细是空的（记了个空壳）"


# ═══════════════════════════════════════════════════════════════
#  ② 同日幂等
# ═══════════════════════════════════════════════════════════════

def test_same_day_rerun_is_idempotent(_tmpdirs, monkeypatch):
    cf, cu = _tmpdirs
    _patch(monkeypatch, "context_frame", "_default_sources", _fake_frame_sources)
    _patch(monkeypatch, "sampler", "_default_sources", _fake_sample_sources)

    collector.run_daily_collection(as_of="2026-10-12", n=20)
    collector.run_daily_collection(as_of="2026-10-12", n=20)
    assert len(collector.load_universe("2026-10-12")) == 1, "同日重跑重复追加了"
    frames = [p for p in cf.glob("*.jsonl")]
    assert frames, "帧文件不存在"
    n_rows = sum(1 for line in frames[0].read_text(encoding="utf-8").splitlines()
                 if line.strip())
    assert n_rows == 1, f"同日重跑帧重复了（{n_rows} 行）"


# ═══════════════════════════════════════════════════════════════
#  ③ 分项隔离 + ④ 不静默成功
# ═══════════════════════════════════════════════════════════════

def test_frame_failure_does_not_block_universe(_tmpdirs, monkeypatch):
    """情境帧挂了，样本**照样要记**（C 类不可重建，但样本能记多少记多少）。"""
    cf, cu = _tmpdirs

    def _boom(*a, **k):
        raise RuntimeError("frame source down")

    _patch(monkeypatch, "context_frame", "build_frame", _boom)
    _patch(monkeypatch, "sampler", "_default_sources", _fake_sample_sources)

    r = collector.run_daily_collection(as_of="2026-10-12", n=20)
    assert r["ok"] is False and r["errors"], "失败必须进 errors，不许静默成功"
    assert any("frame" in e for e in r["errors"])
    assert r["universe_path"], "帧挂了样本就不记了 ⇒ 白白丢一天数据"


def test_universe_failure_does_not_block_frame(_tmpdirs, monkeypatch):
    """样本挂了，情境帧**照样要记**（帧是最高优先、不可重建）。"""
    cf, cu = _tmpdirs
    _patch(monkeypatch, "context_frame", "_default_sources", _fake_frame_sources)

    def _boom(*a, **k):
        raise RuntimeError("sampler down")

    _patch(monkeypatch, "sampler", "pick_daily_universe", _boom)

    r = collector.run_daily_collection(as_of="2026-10-12", n=20)
    assert r["ok"] is False
    assert any("universe" in e for e in r["errors"])
    assert r["frame_path"] and Path(r["frame_path"]).exists(), "样本挂了帧也不记了"


def test_all_failed_reports_ok_false(_tmpdirs, monkeypatch):
    cf, cu = _tmpdirs

    def _boom(*a, **k):
        raise RuntimeError("all down")

    _patch(monkeypatch, "context_frame", "build_frame", _boom)
    _patch(monkeypatch, "sampler", "pick_daily_universe", _boom)

    r = collector.run_daily_collection(as_of="2026-10-12", n=20)
    assert r["ok"] is False
    assert len(r["errors"]) == 2, f"两项都挂应记 2 条，实际 {r['errors']}"


def test_collection_never_raises(_tmpdirs, monkeypatch):
    """★ 盘后任务不能因为采集挂掉而中断 T+N 验证 ⇒ 本函数**不抛异常**。"""
    cf, cu = _tmpdirs

    def _boom(*a, **k):
        raise RuntimeError("boom")

    _patch(monkeypatch, "context_frame", "build_frame", _boom)
    _patch(monkeypatch, "sampler", "pick_daily_universe", _boom)
    r = collector.run_daily_collection()          # 不指定 as_of，走默认路径
    assert isinstance(r, dict) and r["ok"] is False


# ═══════════════════════════════════════════════════════════════
#  ⑤ 盘后 worker 真的接线了
# ═══════════════════════════════════════════════════════════════

def test_evaluator_has_collection_hook():
    """evaluator 必须暴露盘后采集钩子，且它**真的调** collector。"""
    assert callable(getattr(evaluator, "run_v5_collection", None)), \
        "evaluator 缺少 run_v5_collection 钩子 ⇒ 采集没人调用"


def test_collection_hook_calls_collector(monkeypatch):
    calls = {"n": 0}

    def _fake(**k):
        calls["n"] += 1
        return {"ok": True, "as_of": "2026-10-12", "errors": []}

    _patch(monkeypatch, "collector", "run_daily_collection", _fake)
    evaluator.run_v5_collection()
    assert calls["n"] == 1, "钩子没调 collector ⇒ 接线是假的"


def test_worker_source_calls_the_hook():
    """★ 源码级守卫：worker 里必须出现对钩子的调用（防日后接线被删而无人察觉）。"""
    src = Path(evaluator.__file__).read_text(encoding="utf-8", errors="replace")
    # 定位 worker 函数体（从 def _worker 到下一个顶层 def）
    i = src.find("def _worker():")
    assert i > 0, "找不到 worker 函数"
    body = src[i: src.find("\ndef ", i + 1)] if "\ndef " in src[i + 1:] else src[i:]
    assert "run_v5_collection" in body, \
        "worker 函数体里没有 run_v5_collection ⇒ 每天不会自动跑"
