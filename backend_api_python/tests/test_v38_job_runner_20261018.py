# -*- coding: utf-8 -*-
"""F26-D1/D2：exec/job_runner 统一执行器 + skill_run 作业目录/manifest（2026-10-18）。

覆盖总账 F26 批 4d 的验收清单（tmp/批4_剩余项实施总方案_20261017.md §5）：
  ① `_kill_tree` 提为公共（bridge 移交，原行为不回归）；
  ② 停滞看门狗超时判死（有进度戳）；
  ③ 有进度戳不误杀；
  ④ `skill_run` 经 runner 起的子进程超时被杀（真进程，含孙进程finally不执行证据）；
  ⑤ 缺 `last_progress` 退回墙钟且**明示**（不静默）；
  ⑥ bridge 目录/manifest/审计结构未被改动；
  + D2：run_skill 建作业目录+manifest（running→终态）、child 写 result.json/last_progress、
        无 job env 时回落旧扁平输出（人工直跑兼容）、非 0 退出抛错、stall env 三态。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
AGENT = BACKEND / "app" / "agent"
SKILL_RUN = AGENT / "scripts" / "skill_run.py"


# ─────────────────────────────────────────────────── 替身与夹具

class _FakeProc:
    """最小 Popen 替身：communicate 前 succeed_after 次抛 TimeoutExpired。"""

    def __init__(self, *, succeed_after=None, tail=("out", "err"), returncode=0):
        self.pid = 4242
        self.returncode = returncode
        self._calls = 0
        self._succeed_after = succeed_after
        self._tail = tail
        self.killed = False

    def communicate(self, timeout=None):
        self._calls += 1
        if self._succeed_after is not None and self._calls > self._succeed_after:
            return self._tail
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)

    def poll(self):
        return None

    def kill(self):
        self.killed = True
        self.returncode = -9


def _patch_popen(monkeypatch, proc):
    import app.agent.exec.job_runner as jr
    monkeypatch.setattr(jr, "Popen", lambda *a, **kw: proc)
    return jr


def _patch_kill(monkeypatch):
    import app.agent.exec.job_runner as jr
    killed = []
    monkeypatch.setattr(jr, "kill_tree", lambda p: killed.append(p.pid))
    return jr, killed


def _touch(path: Path, age_s: float = 0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    if age_s:
        old = time.time() - age_s
        import os as _os
        _os.utime(path, (old, old))


# ─────────────────────────────────────────────────── ① kill_tree 公共化

def test_bridge_kill_tree_delegates_to_shared(monkeypatch):
    """bridge 的 `_kill_tree` 只剩委托 seam（测试 monkeypatch B._kill_tree 的接须不变）。"""
    import app.agent.chain.backtest_bridge as B
    calls = []
    monkeypatch.setattr(B, "kill_tree", lambda proc: calls.append(proc.pid))
    B._kill_tree(_FakeProc())
    assert calls == [4242]


def test_bridge_no_longer_implements_kill_tree():
    """AST 级：bridge `_kill_tree` 函数体不得再含 taskkill/killpg 实现（已移交）。"""
    src = (AGENT / "chain" / "backtest_bridge.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_kill_tree"), None)
    assert fn is not None, "bridge 仍应有 _kill_tree 委托 seam"
    body = ast.dump(ast.Module(body=fn.body, type_ignores=[]))
    assert "taskkill" not in body and "killpg" not in body, "实现未移交 exec/job_runner"
    jr_src = (AGENT / "exec" / "job_runner.py").read_text(encoding="utf-8")
    assert "taskkill" in jr_src and "killpg" in jr_src, "公共实现必须在 job_runner"


# ─────────────────────────────────────────────────── ②③ 停滞看门狗

def test_stall_watchdog_kills_when_progress_stale(monkeypatch, tmp_path):
    job = tmp_path / "job"
    _touch(job / "last_progress", age_s=999)          # 进度戳 999s 前
    proc = _FakeProc()                                  # 永不退出
    jr, killed = _patch_kill(monkeypatch)
    _patch_popen(monkeypatch, proc)
    t0 = time.monotonic()
    with pytest.raises(jr.JobStalled, match="停滞"):
        jr.run_job(["fake"], job_dir=job, timeout_s=30, stall_s=5, poll_s=0.01)
    assert time.monotonic() - t0 < 5, "判死必须远早于墙钟"
    assert killed == [4242]


def test_fresh_progress_no_false_kill(monkeypatch, tmp_path):
    job = tmp_path / "job"
    _touch(job / "last_progress")                       # 进度戳是新的
    proc = _FakeProc(succeed_after=3, tail=("OUT", "ERR"))
    jr, killed = _patch_kill(monkeypatch)
    _patch_popen(monkeypatch, proc)
    out, err, rc = jr.run_job(["fake"], job_dir=job, timeout_s=30, stall_s=5,
                              poll_s=0.01)
    assert (out, err, rc) == ("OUT", "ERR", 0)
    assert killed == [], "进度新鲜不得误杀"


# ─────────────────────────────────────────────────── ④ 真子进程超时杀树

def test_real_subprocess_timeout_kills_tree(tmp_path):
    """真进程：timeout_s=1 必须秒级杀树；孙进程级 finally 不执行 = 真被杀（非自然退出）。"""
    import app.agent.exec.job_runner as jr
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    child = (
        "import sys, time\n"
        "open(sys.argv[1], 'w').write('1')\n"
        "try:\n"
        "    time.sleep(60)\n"
        "finally:\n"
        "    open(sys.argv[2], 'w').write('1')\n"
    )
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        jr.run_job([sys.executable, "-c", child, str(started), str(finished)],
                   timeout_s=1)
    assert time.monotonic() - t0 < 15, "杀树必须及时，不得等 sleep(60)"
    assert started.is_file(), "子进程应已启动"
    time.sleep(2)                                        # 给可能的自然退出留窗口
    assert not finished.is_file(), "finally 执行了 = 进程自然退出而非被杀树"


# ─────────────────────────────────────────────────── ⑤ 无进度信号明示退回墙钟

def test_no_progress_signal_falls_back_to_wall_clock_explicitly(monkeypatch, tmp_path, caplog):
    """空作业目录（执行方什么都没写）= 无进度信号 ⇒ 明示退回墙钟。

    ★ 语义口径（计划原文「进度戳/产物 mtime」）：目录内**任何**文件 mtime 都算信号——
      manifest.json 也是。所以"无信号"仅指真空目录；有文件但过期 = 停滞判死（上一个用例）。
      这正是真实 skill 流的形状：父进程先写 manifest ⇒ 有信号；child touch last_progress
      ⇒ 信号刷新；两者之间 aging 到 stall_s 才判死。默认 stall=timeout ⇒ 永不提前杀。
    """
    job = tmp_path / "job"
    job.mkdir(parents=True)
    proc = _FakeProc()                                            # 永不退出
    jr, killed = _patch_kill(monkeypatch)
    _patch_popen(monkeypatch, proc)
    with caplog.at_level("INFO", logger="app.agent.exec.job_runner"):
        t0 = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            jr.run_job(["fake"], job_dir=job, timeout_s=1, stall_s=0.2, poll_s=0.05)
    assert time.monotonic() - t0 < 5
    assert killed == [4242], "无进度信号必须走墙钟杀树（不是停滞判死）"
    assert any("无进度信号" in r.message or "退回墙钟" in r.message
               for r in caplog.records), "退回墙钟必须明示（日志），不得静默"


# ─────────────────────────────────────────────────── ⑥ bridge 结构未动

def test_bridge_job_structure_untouched():
    import inspect
    import app.agent.chain.backtest_bridge as B
    src = (AGENT / "chain" / "backtest_bridge.py").read_text(encoding="utf-8")
    for token in ("manifest.json", "overlay", "_manifest_is_stale", "gc_jobs",
                  "_assert_guards", "_running_jobs"):
        assert token in src, f"bridge 结构件被误动: {token}"
    sig = inspect.signature(B.run_backtest_job)
    for p in ("carrier", "scope", "timeout_s", "strategy_key"):
        assert p in sig.parameters, f"run_backtest_job 签名被改: {p}"


# ─────────────────────────────────────────────────── run_job 其余契约

def test_nonzero_exit_raises_with_tail(monkeypatch):
    proc = _FakeProc(succeed_after=1, tail=("", "boom-tail"), returncode=3)
    jr, killed = _patch_kill(monkeypatch)
    _patch_popen(monkeypatch, proc)
    with pytest.raises(RuntimeError, match="boom-tail"):
        jr.run_job(["fake"], timeout_s=5, poll_s=0.01)


def test_check_rc_false_returns_returncode(monkeypatch):
    """check_rc=False：rc≠0 不抛、把 rc 交还调用方（结构化错误通道型子进程用）。"""
    proc = _FakeProc(succeed_after=1, tail=('{"ok": false, "error": "未知技能"}', ""),
                     returncode=2)
    jr, killed = _patch_kill(monkeypatch)
    _patch_popen(monkeypatch, proc)
    out, err, rc = jr.run_job(["fake"], timeout_s=5, poll_s=0.01, check_rc=False)
    assert rc == 2 and "未知技能" in out
    assert killed == []


def test_stall_env_three_states(monkeypatch):
    import app.agent.exec.job_runner as jr
    monkeypatch.delenv("QD_JOB_STALL_SECONDS", raising=False)
    assert jr._stall_default() is None                    # 未设 → 沿用调用方 timeout
    monkeypatch.setenv("QD_JOB_STALL_SECONDS", "42")
    assert jr._stall_default() == 42
    monkeypatch.setenv("QD_JOB_STALL_SECONDS", "0")
    assert jr._stall_default() is None                    # 0/负 = 不启用提前杀
    monkeypatch.setenv("QD_JOB_STALL_SECONDS", "abc")
    assert jr._stall_default() is None                    # 非法值不静默生效


# ─────────────────────────────────────────────────── D2：skill_run 作业目录

def _load_skill_run():
    spec = importlib.util.spec_from_file_location("v38_skill_run", SKILL_RUN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_skill_module():
    import types
    return types.SimpleNamespace(debug_strategy=lambda **kw: {"ok": True, "n": 1})


def test_run_skill_creates_job_dir_and_manifest(monkeypatch, tmp_path):
    from app.agent.tools import skill_tools
    captured = {}

    def fake_run_job(cmd, **kw):
        captured.update(kw)
        captured["cmd"] = cmd
        return (json.dumps({"ok": True, "name": "market-screener",
                            "result": "预览", "full_path": "", "elapsed_s": 1.0}), "", 0)

    monkeypatch.setattr(skill_tools, "run_job", fake_run_job)
    monkeypatch.setattr(skill_tools, "_jobs_root", lambda: tmp_path / "jobs")
    out = skill_tools.run_skill("market-screener")
    assert out["ok"] is True
    job_dir = Path(captured["env"]["QD_SKILL_JOB_DIR"])
    m = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    assert m["state"] == "done" and m["name"] == "market-screener"
    assert m["timeout_s"] > 0 and m["ts"]
    assert captured["cmd"][1].endswith("skill_run.py")
    assert captured["cwd"] == str(BACKEND)
    assert "PYTHONIOENCODING" in captured["env"]
    assert captured["check_rc"] is False, "skill_run 的结构化错误通道必须透传"


def test_run_skill_passes_structured_error_through(monkeypatch, tmp_path):
    """★ 冒烟 B② 实锤的回归防护：子进程 rc≠0 + stdout ok=False（带重试指引）
    必须**原样透传**给模型；被 run_job 的 RuntimeError 吞掉 = 降智
    （"未知参数"的 hint/report_guide 是模型自救的唯一指引）。"""
    from app.agent.tools import skill_tools
    child_payload = {
        "ok": False, "error": "未知参数: ['market_cap']",
        "hint": "请只用: ['strategy', 'code']",
        "report_guide": {"STEPS": "按 hint 重试一次", "priority": "支撑≫涨跌幅"},
    }

    def fake_run_job(cmd, **kw):
        return (json.dumps(child_payload, ensure_ascii=False), "", 2)

    monkeypatch.setattr(skill_tools, "run_job", fake_run_job)
    monkeypatch.setattr(skill_tools, "_jobs_root", lambda: tmp_path / "jobs")
    out = skill_tools.run_skill("strategy-debug", '{"fn": "debug_strategy"}')
    assert out.get("ok") is False, "结构化错误的 ok=False 必须保留"
    assert out.get("hint") == child_payload["hint"], "重试指引不得被吞"
    assert out.get("report_guide") == child_payload["report_guide"]
    assert "market_cap" in out.get("error", "")
    m = json.loads((Path(out["job_dir"]) / "manifest.json").read_text(encoding="utf-8"))
    assert m["state"] == "failed"


def test_run_skill_timeout_and_stall_write_terminal_manifest(monkeypatch, tmp_path):
    from app.agent.tools import skill_tools
    from app.agent.exec.job_runner import JobStalled

    def fake_timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout_s", 0))

    def fake_stall(cmd, **kw):
        raise JobStalled("停滞 999s 未更新")

    monkeypatch.setattr(skill_tools, "_jobs_root", lambda: tmp_path / "jobs")

    monkeypatch.setattr(skill_tools, "run_job", fake_timeout)
    out = skill_tools.run_skill("market-screener")
    assert "超时" in out["error"]
    m = json.loads((Path(out["job_dir"]) / "manifest.json").read_text(encoding="utf-8"))
    assert m["state"] == "timeout" and "error" in m

    monkeypatch.setattr(skill_tools, "run_job", fake_stall)
    out = skill_tools.run_skill("market-screener")
    assert "停滞" in out["error"]
    m = json.loads((Path(out["job_dir"]) / "manifest.json").read_text(encoding="utf-8"))
    assert m["state"] == "stalled"


def test_child_writes_result_into_job_dir(monkeypatch, tmp_path, capsys):
    mod = _load_skill_run()
    monkeypatch.setattr("app.agent.modpath.import_module", lambda p: _fake_skill_module())
    job = tmp_path / "job"
    job.mkdir()
    monkeypatch.setenv("QD_SKILL_JOB_DIR", str(job))
    monkeypatch.setattr(sys, "argv",
                        ["skill_run.py", "strategy-debug", '{"fn": "debug_strategy"}'])
    rc = mod.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    result = json.loads((job / "result.json").read_text(encoding="utf-8"))
    assert result["n"] == 1, "全量结果必须落作业目录（预览截断不影响全量）"
    assert (job / "last_progress").is_file(), "child 必须留进度戳供看门狗"
    assert str(job) in payload["full_path"]


def test_child_fallback_flat_output_without_job_env(monkeypatch, tmp_path, capsys):
    """人工直跑（无 QD_SKILL_JOB_DIR）保持旧扁平输出 —— 不是兜底，是文档化的直跑用法。"""
    mod = _load_skill_run()
    monkeypatch.setattr("app.agent.modpath.import_module", lambda p: _fake_skill_module())
    monkeypatch.delenv("QD_SKILL_JOB_DIR", raising=False)
    monkeypatch.setattr(mod, "_OUT_DIR", tmp_path / "flat")
    monkeypatch.setattr(sys, "argv",
                        ["skill_run.py", "strategy-debug", '{"fn": "debug_strategy"}'])
    rc = mod.main()
    assert rc == 0
    assert len(list((tmp_path / "flat").glob("*.json"))) == 1
    assert capsys.readouterr().out.strip()
