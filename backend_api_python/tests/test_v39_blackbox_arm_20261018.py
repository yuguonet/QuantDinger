# -*- coding: utf-8 -*-
"""F21 标准对照臂守卫（2026-10-18，tests/test_v39_blackbox_arm_20261018.py）。

覆盖方案 §7.1 四条验收 + 两条加强：
  ① mock 后端 + mock 主链臂 ⇒ 报告 schema 与 qd_dispatch_ab 同构
     （ts/model/step_limit/records/verdict/thresholds；records 双臂槽位）；
  ② `_verdict` / `score_answer` 是**同一个函数对象**（复用断言，防另写判定/评分）；
  ③ 缺后端 skip 而非 fail（exit 2 + [SKIP] 明示；未知后端同理）；
  ④ 主链路无 import 变化（AST/文本断言：app/ 下无 blackbox_runner 引用，
     qd_dispatch_ab/qd_bench 源码不含 blackbox，本工具不自定义 _verdict/score_answer）；
  ⑤ preds 输出可被 qd_bench.score_batch 直接消费（同 schema 的端到端证明）；
  ⑥ --rejudge 只复判双臂报告；命令模板 env 覆写与缺 {query} 占位 fail-loud。

**跑法注意**：本文件延迟导入 blackbox_runner（其模块链会加载 qd_dispatch_ab ⇒
模块级 load_dotenv + sys.path 引导，R3 入口脚本豁免行为），收集期不导入是为了
不去污染别的文件的进程全局判据——与 test_v32 同款纪律。
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture(scope="module")
def br():
    """blackbox_runner 延迟导入（理由见模块 docstring）。"""
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import blackbox_runner
    return blackbox_runner


def _mock_arms(monkeypatch, br, *, blackbox_answer="结论：短线偏多，建议持有观望。"
                  "依据数据显示现价1329.00元、日涨幅1.80%、成交量12.5万手，"
                  "技术面站稳年线，上方压力1365元，下方支撑1300元。"
                  "风险提示：仅供参考，不构成投资建议，注意波动风险。",
                  qd_answer="主链答案：偏多，现价1329.00元，压力1365元，支撑1300元，注意风险。",
                  blackbox_error=None, qd_error=None):
    """把两个臂都换成假人：_run_backend 与 qd_dispatch_ab 的 _build_service/_run_arm。"""
    monkeypatch.setattr(br, "_build_service", lambda arm, step_limit: object())

    def _fake_backend(backend, query, timeout_s):
        if blackbox_error:
            raise RuntimeError(blackbox_error)
        return blackbox_answer

    monkeypatch.setattr(br, "_run_backend", _fake_backend)

    def _fake_run_arm(svc, query, session):
        if qd_error:
            raise RuntimeError(qd_error)
        return {"tokens": 4200, "steps": 6, "elapsed_s": 12.3,
                "answer_len": len(qd_answer), "answer": qd_answer,
                "corpus": "工具输出拼接"}

    monkeypatch.setattr(br, "_run_arm", _fake_run_arm)


# ── ① schema 同构 ───────────────────────────────────────────────

def test_mock_backend_report_schema_isomorphic(br, monkeypatch):
    """mock 双臂 ⇒ 报告与 qd_dispatch_ab 同构，records 双臂槽位齐全、字段不丢。"""
    _mock_arms(monkeypatch, br)
    rep = br.run_experiment("opencode", [("screen_01", "帮我做一次全市场短线强势股筛选")])

    # 顶层 schema：与 qd_dispatch_ab.run_experiment 返回同族（多 backend/kind 两个本工具键）
    for k in ("ts", "model", "step_limit", "records", "verdict", "thresholds"):
        assert k in rep, f"报告缺 schema 键 {k}"
    assert rep["backend"] == "opencode" and rep["kind"] == "blackbox_vs_qd"
    assert rep["thresholds"]["cost_judged"] is False, "CLI 臂成本维必须明示不判"

    (row,) = rep["records"]
    assert row["task_id"] == "screen_01" and row["query"]
    d, i = row["dispatch"], row["inline"]
    # dispatch 槽 = 被测 blackbox：无 tokens 字段（CLI 无计量），有质量/耗时/答案
    assert "tokens" not in d, "blackbox 臂不得伪造 token 计数"
    assert d["quality"] > 0 and d["answer_len"] > 0 and "elapsed_s" in d
    # inline 槽 = 主链 QD：token delta 口径同 qd_dispatch_ab
    assert i["tokens"] == 4200 and i["steps"] == 6 and i["quality"] > 0
    # 判定由 _verdict 产出：per_task 五字段 + pass/failures
    v = rep["verdict"]
    assert set(v) >= {"pass", "failures", "per_task", "cost_max", "quality_tol"}
    (pt,) = v["per_task"]
    assert set(pt) == {"task_id", "ratio_tokens", "quality_delta",
                       "pass_cost", "pass_quality"}
    # 质量Δ = blackbox - qd（同一把尺打出来的分才可减）
    assert pt["quality_delta"] == round(d["quality"] - i["quality"], 2)
    assert pt["ratio_tokens"] == 0.0, "CLI 臂无 token ⇒ _verdict 机械记 0（cost_judged=false）"
    # preds：qd_bench preds schema
    (p,) = rep["preds"]
    assert set(p) >= {"task_id", "answer", "meta"} and p["meta"]["arm"] == "blackbox"


def test_arm_error_recorded_as_failure(br, monkeypatch):
    """臂失败（后端超时/LLM 重试耗尽）记 error ⇒ _verdict 该题双维判负（不静默）。"""
    _mock_arms(monkeypatch, br, blackbox_error="后端超时 300s")
    rep = br.run_experiment("codex", [("screen_01", "q")])
    (row,) = rep["records"]
    assert "error" in row["dispatch"]
    v = br._verdict(rep["records"])
    (pt,) = v["per_task"]
    assert pt["pass_cost"] is False and pt["pass_quality"] is False
    assert any("臂失败" in f for f in v["failures"])


# ── ② 复用断言（同一函数对象） ──────────────────────────────────

def test_verdict_and_scorer_are_same_objects(br):
    """_verdict / score_answer 必须是 qd_dispatch_ab / qd_bench 的原对象（复用，非重写）。"""
    from app.agent.scripts import qd_bench, qd_dispatch_ab
    assert br._verdict is qd_dispatch_ab._verdict
    assert br.score_answer is qd_bench.score_answer
    assert br.load_tasks is qd_bench.load_tasks
    assert br._build_service is qd_dispatch_ab._build_service
    assert br._run_arm is qd_dispatch_ab._run_arm


def test_runner_does_not_redefine_verdict_or_scorer(br):
    """源码级：本工具不得自定义 _verdict / score_answer / _run_arm（防悄悄另写口径）。"""
    import inspect
    src = inspect.getsource(br)
    for name in ("def _verdict", "def score_answer", "def _run_arm",
                 "def _build_service"):
        assert name not in src, f"blackbox_runner 不得自定义 {name}（应复用 qd_dispatch_ab）"


# ── ③ 缺后端 skip 而非 fail ────────────────────────────────────

def test_missing_backend_skips_not_fails(br, monkeypatch, capsys):
    """PATH 无任何后端 ⇒ [SKIP] + exit 2（环境不可用，不是实验失败）。"""
    monkeypatch.setattr(br, "detect_backends", lambda: [])
    rc = br.main([])
    out = capsys.readouterr().out
    assert rc == 2, "缺后端必须 exit 2（skip），不是 1（fail）"
    assert "[SKIP]" in out and "opencode" in out and "不进 CI" in out


def test_named_backend_unavailable_skips(br, monkeypatch, capsys):
    """点名了但 PATH 没有的后端 ⇒ [SKIP] 明示哪个可执行缺失 + exit 2。"""
    monkeypatch.setattr(br, "detect_backends", lambda: ["claude_code"])
    rc = br.main(["--backend", "opencode"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "[SKIP]" in out and "opencode" in out


def test_unknown_backend_env_error(br, capsys):
    """不在名单里的后端名 ⇒ [ENV] + exit 2（不猜、不静默改名）。"""
    assert br.main(["--backend", "gpt4all"]) == 2
    assert "[ENV]" in capsys.readouterr().out


# ── ④ 主链路无 import 变化 ─────────────────────────────────────

def test_main_chain_has_no_blackbox_reference():
    """app/ 下任何文件不得引用 blackbox_runner；qd_dispatch_ab/qd_bench 源码不含 blackbox。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for py in (root / "app").rglob("*.py"):
        if "blackbox" in py.read_text(encoding="utf-8", errors="ignore"):
            raise AssertionError(f"主链路文件出现 blackbox 引用：{py}")
    for name in ("app/agent/scripts/qd_dispatch_ab.py", "app/agent/scripts/qd_bench.py"):
        assert "blackbox" not in (root / name).read_text(encoding="utf-8"), (
            f"{name} 被 4f 改动了吗？F21 要求不动主链路")


# ── ⑤ preds 可被 qd_bench 消费 ─────────────────────────────────

def test_preds_consumable_by_qd_bench(br, monkeypatch, tmp_path):
    """blackbox preds 文件 ⇒ qd_bench.score_batch 直接产出 run 摘要（同 schema 端到端）。"""
    from app.agent.scripts import qd_bench
    _mock_arms(monkeypatch, br)
    rep = br.run_experiment("opencode", [("stock_01", "帮我分析一下贵州茅台600519，未来三天怎么看")])
    out = tmp_path / "bb.jsonl"
    br.save_preds(rep, str(out))
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rows and rows[0]["task_id"] == "stock_01"
    summary = qd_bench.score_batch(qd_bench.load_tasks(), rows)
    assert summary["n_scored"] >= 1 and summary["total"] > 0
    assert any(r["task_id"] == "stock_01" and r["status"] == "ok"
               for r in summary["tasks"]), "blackbox 答案应被能力标尺正常计分"


# ── ⑥ rejudge 与命令模板 ───────────────────────────────────────

def test_rejudge_only_accepts_two_arm_report(br, tmp_path, capsys):
    """--rejudge 只复判双臂报告；非双臂（缺 dispatch/inline 槽）⇒ exit 2 明示。"""
    bad = tmp_path / "single.json"
    bad.write_text(json.dumps({"records": [{"task_id": "t1"}]}), encoding="utf-8")
    assert br.main(["--rejudge", str(bad)]) == 2
    assert "无从复判" in capsys.readouterr().out

    good = tmp_path / "two_arm.json"
    good.write_text(json.dumps({"records": [
        {"task_id": "t1", "dispatch": {"quality": 70.0, "tokens": None},
         "inline": {"quality": 80.0, "tokens": 1000}}]}), encoding="utf-8")
    rc = br.main(["--rejudge", str(good), "--quality-tol", "5"])
    out = capsys.readouterr().out
    # 质量Δ = 70-80 = -10 < -5 ⇒ 不通过 ⇒ exit 1（复判真的换了阈值口径）
    assert rc == 1
    assert "FAIL" in out
    rc2 = br.main(["--rejudge", str(good), "--quality-tol", "12"])
    assert rc2 == 0, "-10 ≥ -12 ⇒ 放行"


def test_backend_cmd_template_env_override(br, monkeypatch):
    """命令模板走 env 覆写（不硬编码）；缺 {query} 占位 fail-loud。"""
    monkeypatch.setenv("QD_BLACKBOX_CMD_OPENCODE", "my-oc --json {query}")
    assert br._cmd_template("opencode") == "my-oc --json {query}"
    assert br._backend_exe("opencode") == "my-oc"
    monkeypatch.setenv("QD_BLACKBOX_CMD_OPENCODE", "my-oc --json")
    with pytest.raises(RuntimeError, match="query"):
        br._run_backend("opencode", "q", 5)


def test_backend_timeout_env_fallback(br, monkeypatch, capsys):
    """QD_BLACKBOX_TIMEOUT 未设/非法/非正 ⇒ 回落 300 且告警（不静默）。"""
    monkeypatch.delenv("QD_BLACKBOX_TIMEOUT", raising=False)
    assert br._backend_timeout() == 300
    monkeypatch.setenv("QD_BLACKBOX_TIMEOUT", "abc")
    assert br._backend_timeout() == 300
    assert "[WARN]" in capsys.readouterr().out
    monkeypatch.setenv("QD_BLACKBOX_TIMEOUT", "-1")
    assert br._backend_timeout() == 300


def test_run_backend_resolves_exe_via_which(br, monkeypatch):
    """argv[0] 必须走 shutil.which 解析后的全路径。

    2026-10-18 真实冒烟实锤：Windows 下 npm shim 是 `opencode.CMD`，shutil.which
    能解析到它，但 subprocess 列表形式不过 PATHEXT——裸名 "opencode" 直接
    WinError 2。本测试钉死「用 which() 返回值作 argv[0]」这个修法。
    """
    import subprocess as sp
    seen = {}

    monkeypatch.setattr(br.shutil, "which", lambda name: f"C:\\fake\\{name}.CMD")

    class _P:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _P()

    monkeypatch.setattr(br.subprocess, "run", _fake_run)
    assert br._run_backend("opencode", "q", 5) == "ok"
    assert seen["cmd"][0] == "C:\\fake\\opencode.CMD", "argv[0] 必须是 which 解析值"
    assert seen["cmd"][1:] == ["run", "q"]


def test_run_backend_failure_paths(br, monkeypatch):
    """超时 / 非 0 且无 stdout ⇒ RuntimeError（上层按臂失败记 verdict，不静默）。"""
    import subprocess as sp

    def _timeout(cmd, **kw):
        raise sp.TimeoutExpired(cmd, kw.get("timeout", 0))

    monkeypatch.setattr(br.subprocess, "run", _timeout)
    with pytest.raises(RuntimeError, match="超时"):
        br._run_backend("opencode", "q", 7)

    class _Bad:
        returncode = 3
        stdout = ""
        stderr = "boom detail"

    monkeypatch.setattr(br.subprocess, "run", lambda cmd, **kw: _Bad())
    with pytest.raises(RuntimeError, match="boom detail"):
        br._run_backend("opencode", "q", 7)
