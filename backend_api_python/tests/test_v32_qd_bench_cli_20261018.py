# -*- coding: utf-8 -*-
"""qd_bench CLI 扩展回归（F19，2026-10-18）。

覆盖方案 6.2 的五条验收：
  ① `selftest` 退出码 0 且含 PASS（内置 fixture 全过，零网络零外部依赖）
  ② selftest 判据失败时退出码非 0（证明不是空壳——把打分打平成常量即红）
  ③ `--num-rollouts` 聚合字段齐全（mean±std/min/max/n：总体+分项+逐任务），
     且 cmd_run 的 rollout 接线正确（逐轮 run_id 后缀 / preds 按轮次落盘 /
     聚合记录另存 runs/rollouts/）
  ④ N 超 QD_BENCH_MAX_ROLLOUTS 上限被拒（fail-loud，exit 2，触服务前）
  ⑤ 既有子命令 `--help` 不回归（含新增 selftest / run --num-rollouts）

背景：F19 前 qd_bench 无 `--selftest`、无 `--num-rollouts`（grep 零命中），
评分机制自身没有回归闸门——改坏 score_answer 只能靠跑分肉眼发现。
"""
import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def qb():
    """qd_bench 延迟到用例内导入（module 级 import 会在收集期把 app/agent 插进
    sys.path——R3 入口脚本豁免下的模块级路径引导，会污染后续文件的进程全局判据，
    如 test_v30 壳的 test_29；本文件自己不要当污染源）。
    """
    from app.agent.scripts import qd_bench
    return qd_bench


# ── ① selftest 全过 ─────────────────────────────────────────────

def test_selftest_passes_exit_zero(capsys, qb):
    """内置 fixture 自洽校验：六项判据全过 ⇒ exit 0 且输出含 [PASS]。"""
    rc = qb.main(["selftest"])
    out = capsys.readouterr().out
    assert rc == 0, f"selftest 应全过，实际 rc={rc}\n{out}"
    assert "[PASS] selftest 全部通过（6/6）" in out
    # 六项判据逐一在案（缺一项即说明判据被删）
    for name in ("评分可分辨", "打分确定性", "must 逐条可判",
                 "agreement 自比 100%", "agreement 容差内外可判",
                 "agreement 无交集 n=0"):
        assert f"[PASS] {name}" in out, f"判据缺失：{name}"


# ── ② selftest 失败可分辨 ───────────────────────────────────────

def test_selftest_failure_exit_nonzero(capsys, monkeypatch, qb):
    """把 score_answer 打平成常量 ⇒ 可分辨性判据必须翻红、rc 非 0。

    证明 selftest 不是"恒绿空壳"：判据与真实打分函数联动。
    """
    monkeypatch.setattr(qb, "score_answer",
                        lambda *a, **k: {"dims": {}, "total": 50.0, "must": {}})
    rc = qb.main(["selftest"])
    out = capsys.readouterr().out
    assert rc != 0, "打平打分后 selftest 必须翻红"
    assert "[FAIL] 评分可分辨" in out
    assert "selftest 1/6 项失败" in out


# ── ③ 聚合字段齐全 + rollout 接线 ───────────────────────────────

def _sum(total: float) -> dict:
    """合成一份单轮 summary（总分=分项=逐任务，便于手算均值/标准差）。"""
    return {"total": total, "dims": {"coverage": total},
            "tasks": [{"task_id": "t1", "total": total}],
            "n_scored": 1, "n_total": 1}


def test_aggregate_rollouts_fields_and_math(qb):
    """`_aggregate_rollouts`：总体/分项/逐任务三层 mean±std 字段齐全且算对。"""
    agg = qb._aggregate_rollouts([_sum(70.0), _sum(80.0), _sum(90.0)])
    assert agg["n_rollouts"] == 3
    # mean=80；总体 std=sqrt((100+0+100)/3)=8.165→8.2
    assert agg["total"] == {"mean": 80.0, "std": 8.2, "min": 70.0,
                            "max": 90.0, "n": 3}
    assert agg["dims"]["coverage"]["mean"] == 80.0
    assert agg["dims"]["coverage"]["std"] == 8.2
    assert agg["tasks"]["t1"]["std"] == 8.2
    # 空输入不造数（全 0 + n=0）
    empty = qb._aggregate_rollouts([])
    assert empty["n_rollouts"] == 0
    assert empty["total"] == {"mean": 0.0, "std": 0.0, "min": 0.0,
                              "max": 0.0, "n": 0}


def test_rollout_suffix_rules(qb):
    """preds 文件按轮次加后缀；无 tag 原样返回（单轮行为逐字不变）。"""
    assert qb._rollout_suffix("p.jsonl", "r0") == "p_r0.jsonl"
    # with_name 走 pathlib ⇒ 分隔符随平台（Windows 为 \），期望值同源派生
    assert qb._rollout_suffix("dir/p.jsonl", "r2") == str(
        Path("dir") / "p_r2.jsonl")
    assert qb._rollout_suffix("p.jsonl", "") == "p.jsonl"


class _Args:
    """cmd_run 的最小 args 桩（只走 rollout 接线用到的字段）。"""

    def __init__(self, **kw):
        self.only = ""
        self.model = ""
        self.step_limit = ""
        self.save_preds = ""
        self.annotations = ""
        self.note = ""
        self.to_db = False
        self.num_rollouts = 1
        self.__dict__.update(kw)


def test_cmd_run_rollouts_wiring(monkeypatch, tmp_path, capsys, qb):
    """cmd_run --num-rollouts 3：逐轮各落一个 run（_r{i} 后缀）+ 一份聚合记录。

    假执行核经 sys.modules 预置注入（不触真实服务/网络）；_rollout_preds
    返回罐头 preds；只验 rollouts 接线本身。
    """
    monkeypatch.setenv("QD_BENCH_MAX_ROLLOUTS", "5")
    fake_mod = types.ModuleType("app.agent.qd_service")

    class _FakeSvc:
        def __init__(self, **kw):
            pass

    fake_mod.QDAgentService = _FakeSvc
    monkeypatch.setitem(sys.modules, "app.agent.qd_service", fake_mod)

    calls = []

    def _fake_preds(svc, tasks, model, tag):
        calls.append(tag)
        return [{"task_id": t["id"], "answer": qb._SELFTEST_GOOD,
                 "corpus": "", "meta": {"model": model or "default"}}
                for t in tasks]

    monkeypatch.setattr(qb, "_rollout_preds", _fake_preds)
    saved_runs, saved_aggs = [], []
    monkeypatch.setattr(qb, "_save_run",
                        lambda s, m: saved_runs.append(dict(m)) or m.get("run_id", ""))
    monkeypatch.setattr(qb, "_save_rollouts",
                        lambda a, m: saved_aggs.append((a, dict(m)))
                        or (tmp_path / "agg.json"))
    monkeypatch.setattr(qb, "_print_rollouts", lambda a, ids: None)
    monkeypatch.setattr(qb, "_print_summary", lambda rid, s: None)

    preds_path = tmp_path / "p.jsonl"
    args = _Args(num_rollouts=3, save_preds=str(preds_path))
    rc = qb.cmd_run(args)
    assert rc == 0

    # 逐轮 tag 与 run 记录：3 轮各一条，run_id 同基不同后缀
    assert calls == ["r0", "r1", "r2"]
    assert len(saved_runs) == 3
    ids = [m["run_id"] for m in saved_runs]
    assert len(set(ids)) == 3, "逐轮 run_id 必须互斥"
    assert all(i.endswith(f"_r{k}") for k, i in enumerate(ids))
    assert len({i.rsplit("_r", 1)[0] for i in ids}) == 1, "逐轮应共用同一 base"

    # preds 按轮次落盘（p.jsonl 本身不写——避免单轮/多轮口径混淆）
    for k in range(3):
        p = tmp_path / f"p_r{k}.jsonl"
        assert p.is_file(), f"缺轮次 preds 文件：{p.name}"
        rows = [json.loads(l) for l in
                p.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert rows and all(r["answer"] for r in rows)
    assert not preds_path.exists()

    # 聚合记录：字段齐全 + run_ids 回指逐轮
    assert len(saved_aggs) == 1
    agg, agg_meta = saved_aggs[0]
    assert agg["n_rollouts"] == 3
    assert set(agg["total"]) == {"mean", "std", "min", "max", "n"}
    assert agg["dims"], "分项聚合不应为空"
    assert agg["tasks"], "逐任务聚合不应为空"
    assert agg_meta["run_ids"] == ids
    assert agg_meta["run_id"] == ids[0].rsplit("_r", 1)[0]


def test_cmd_run_single_rollout_unchanged_shape(monkeypatch, qb):
    """--num-rollouts 1（缺省）不聚合不打印 rollouts 块：单轮路径逐字保留。"""
    monkeypatch.setenv("QD_BENCH_MAX_ROLLOUTS", "5")
    fake_mod = types.ModuleType("app.agent.qd_service")

    class _FakeSvc:
        def __init__(self, **kw):
            pass

    fake_mod.QDAgentService = _FakeSvc
    monkeypatch.setitem(sys.modules, "app.agent.qd_service", fake_mod)
    monkeypatch.setattr(qb, "_rollout_preds",
                        lambda svc, tasks, model, tag: [
                            {"task_id": t["id"], "answer": qb._SELFTEST_GOOD,
                             "corpus": "", "meta": {"model": model}}
                            for t in tasks])
    saved_runs, printed = [], []
    monkeypatch.setattr(qb, "_save_run",
                        lambda s, m: saved_runs.append(dict(m)) or "rid")
    monkeypatch.setattr(qb, "_print_summary",
                        lambda rid, s: printed.append(rid))
    monkeypatch.setattr(qb, "_save_rollouts",
                        lambda a, m: pytest.fail("单轮不应写聚合记录"))
    rc = qb.cmd_run(_Args())
    assert rc == 0
    assert len(saved_runs) == 1
    assert printed == ["rid"], "单轮应照旧打印 run 摘要"
    assert saved_runs[0]["run_id"] == "", "单轮 run_id 仍由 _save_run 自动生成"


# ── ④ 上限 fail-loud ────────────────────────────────────────────

def test_run_over_max_rollouts_refused(monkeypatch, capsys, qb):
    """N 超上限 ⇒ 触服务前拒绝（exit 2 + [REFUSE]），默认上限 5。"""
    monkeypatch.delenv("QD_BENCH_MAX_ROLLOUTS", raising=False)
    rc = qb.cmd_run(_Args(num_rollouts=6))
    out = capsys.readouterr().out
    assert rc == 2, "超限必须拒跑（非 0 非 1：拒绝执行而非失败）"
    assert "[REFUSE]" in out and "超过上限 5" in out
    # 边界：等于上限放行（不会在此返回 2；后续因假环境另说，但不可能是 REFUSE）
    monkeypatch.setattr(qb, "_max_rollouts", lambda: 5)
    assert qb._max_rollouts() == 5


def test_run_over_env_cap_refused(monkeypatch, capsys, qb):
    """env 可调上限：QD_BENCH_MAX_ROLLOUTS=3 时 N=4 同样被拒。"""
    monkeypatch.setenv("QD_BENCH_MAX_ROLLOUTS", "3")
    rc = qb.cmd_run(_Args(num_rollouts=4))
    out = capsys.readouterr().out
    assert rc == 2
    assert "超过上限 3" in out


@pytest.mark.parametrize("raw", ["", "0", "-2", "abc"])
def test_max_rollouts_bad_env_falls_back_with_warning(monkeypatch, capsys, raw, qb):
    """上限 env 未设/非正/非法 ⇒ 回落缺省 5 且明示告警（不静默）。"""
    monkeypatch.setenv("QD_BENCH_MAX_ROLLOUTS", raw)
    assert qb._max_rollouts() == qb.DEFAULT_MAX_ROLLOUTS == 5
    if raw:
        assert "[WARN]" in capsys.readouterr().out, f"{raw!r} 应告警"


# ── ⑤ 既有子命令 --help 不回归 ──────────────────────────────────

def test_subcommand_help_no_regression(capsys, qb):
    """八个子命令 --help 全部 exit 0；新增 selftest 与 run --num-rollouts 在案。"""
    for cmd in ("validate", "score", "run", "trend", "diff", "agreement",
                "selftest", "import-traces"):
        with pytest.raises(SystemExit) as ei:
            qb.main([cmd, "--help"])
        assert ei.value.code == 0, f"{cmd} --help 回归"
        capsys.readouterr()
    with pytest.raises(SystemExit):
        qb.main(["run", "--help"])
    run_help = capsys.readouterr().out
    assert "--num-rollouts" in run_help
    with pytest.raises(SystemExit):
        qb.main(["selftest", "--help"])
    assert "selftest" in capsys.readouterr().out
