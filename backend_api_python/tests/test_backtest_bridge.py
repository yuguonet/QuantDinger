# -*- coding: utf-8 -*-
"""backtest_bridge 单测（backtest_bridge_plan.md Task 4，§5.7 桥核心）。全部 fake 子进程，不依赖 DB/网络。

覆盖改进点：A（--out 落盘+stats 原样搬运）、B（manifest/digest）、C（state/并发/GC/杀树）、
E（proposal 物化走 _run_proposal_cli 接缝）、H（失败诚实落盘）、I（cwd/env 契约）；
Task 7 守卫（2026-10-15，批次D）：AST 级源码守卫（改进 N，文本匹配会误伤
docstring——守卫只认语法树），桥不 import auto 写者/扫描/重建模块、工具文件零
import svc 引擎（services/backtest）；helper 本体在 tests/_ast_guard.py（批次D
质量审查上移为跨文件单一真源，本文件 `from tests._ast_guard import` 消费），
Task 8 守 svc CLI 复用同一 helper。

易错点：
  - 用例间共享 QD_BT_ROOT（autouse fixture 指向 tmp_path/bt），任何落盘断言都经 B._bridge_root() 取；
  - 预算/并发常量是桥模块 **import 时**从 env 绑定的（B.MAX_DAYS 等），测试用 monkeypatch
    直接改模块属性（fixture 里另钉 QD_BT_* env 只对 import 后才读 env 的路径有意义）；
  - _live_yaml_path 替身必须 key 敏感（lambda key: tmp_path / f"{key}.yaml"）：existing
    载体的 key 单一事实源是 carrier["key"]（批次B质量审查 C1），key 不敏感的替身测不出
    读错 key 的回归；
  - 守卫测试（2026-10-15 批次B质量审查 C3）：STRATEGY_DIR 一律 monkeypatch 到 tmp_path，
    绝不允许 QD_BT_ROOT 指向真实活策略树 —— 此前版本曾把 jail/ 目录真落在 strategies/ 下。
"""
import json
import os
import subprocess
import time
import hashlib
from pathlib import Path

import pytest

from app.agent.chain import backtest_bridge as B
from tests._ast_guard import _ast_violations


@pytest.fixture(autouse=True)
def _tmp_root(tmp_path, monkeypatch):
    monkeypatch.setenv("QD_BT_ROOT", str(tmp_path / "bt"))
    # 防开发机 shell 泄漏 overlay 钩子 env，致 svc 泳道「零注入」断言假红（批次E Minor 6）
    monkeypatch.delenv("QD_STRATEGY_YAML_OVERLAY_DIR", raising=False)
    # 钉住预算六件套：防开发机环境变量渗入（对 import 时已绑定的模块常量无效，
    # 那条路径在用例里用 monkeypatch.setattr(B, ...) 补钉）
    for name, val in (("QD_BT_MAX_DAYS", "365"), ("QD_BT_MAX_CODES", "50"),
                      ("QD_BT_TIMEOUT", "900"), ("QD_BT_MAX_CONCURRENT", "1"),
                      ("QD_BT_JOB_TTL_DAYS", "7"), ("QD_BT_RUNS_TTL_DAYS", "30"),
                      ("QD_BT_DEL_TTL_DAYS", "30")):
        monkeypatch.setenv(name, val)
    yield
    return


def _patch_strategy_tree(monkeypatch, tmp_path):
    """STRATEGY_DIR 指向 tmp_path（C3：守卫/路径解析测试零污染活策略树）。"""
    import app.market_cn.auto.core._paths as paths_mod
    fake_tree = tmp_path / "strategies"
    fake_tree.mkdir(exist_ok=True)
    monkeypatch.setattr(paths_mod, "STRATEGY_DIR", str(fake_tree))
    return fake_tree


# 键集与真实 backtest_cli._build_report 一致（C2 修正后含 meta 透传）
_REPORT = {"strategy": "break", "start_date": "2026-06-01", "end_date": "2026-08-01",
           "source_window_days": 60,
           "stats": {"n": 5, "winrate": 60.0, "pl_ratio": 1.7, "n_valid": 5},
           "codes_ok": 3, "elapsed_seconds": 1.2,
           "daily_activity": [], "trades": [],
           "meta": {"git_sha": "abc1234", "effective_params": {"a": 1}}}


class _FakeProc:
    def __init__(self, rc=0, stderr="", timeout=False):
        self.pid = 4242
        self._rc, self._stderr, self._timeout = rc, stderr, timeout

    def communicate(self, timeout=None):
        if self._timeout:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return "progress...", self._stderr

    @property
    def returncode(self):
        return self._rc


def _fake_popen(report=_REPORT, rc=0, stderr="", timeout=False):
    def factory(cmd, **kwargs):
        out = cmd[cmd.index("--out") + 1]
        Path(out).write_text(json.dumps(report), encoding="utf-8")
        assert kwargs.get("cwd") == str(B._BACKEND_ROOT)          # 改进 I
        assert B.ENV_OVERLAY_DIR in kwargs["env"]                 # 改进 I
        assert "--progress-every" in cmd and cmd[cmd.index("--progress-every") + 1] == "0"  # 改进 A
        return _FakeProc(rc=rc, stderr=stderr, timeout=timeout)
    return factory


def test_budget_reject(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "MAX_CONCURRENT", 1)
    with pytest.raises(B.BridgeError):
        B.run_backtest_job(carrier={"kind": "existing", "key": "break"},
                           codes=["000001"], days=10 ** 6)
    with pytest.raises(B.BridgeError):
        B.run_backtest_job(carrier={"kind": "existing", "key": "break"},
                           scope="codes", codes=[str(i) for i in range(999)])
    with pytest.raises(B.BridgeError):
        # I1：scope=codes + 空 codes = 静默全盘，绕过 MAX_CODES —— 必须拒
        B.run_backtest_job(carrier={"kind": "existing", "key": "break"},
                           scope="codes", codes=[])
    with pytest.raises(B.BridgeBusy):
        pre_running = B._bridge_root() / "jobs" / "x"
        pre_running.mkdir(parents=True)
        (pre_running / "manifest.json").write_text(
            json.dumps({"job_id": "x", "state": "running"}), encoding="utf-8")
        B.run_backtest_job(carrier={"kind": "existing", "key": "break"},
                           codes=["000001"])


def test_existing_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "Popen", _fake_popen())
    fake_tree = _patch_strategy_tree(monkeypatch, tmp_path)
    (fake_tree / "break.yaml").write_text("meta: {enabled: true}\n", encoding="utf-8")
    s = B.run_backtest_job(carrier={"kind": "existing", "key": "break"},
                           scope="codes", codes=["000001", "600000"], days=60,
                           session_id="sess-1")
    assert s["ok"] is True
    assert s["strategy_key"] == "break"                          # C1：key 读自 carrier
    assert s["stats"]["n"] == 5 and s["git_sha"] == "abc1234"   # 改进 A/C2：桥不重算
    assert Path(s["report_path"]).is_file()
    assert list((B._bridge_root() / "runs").glob("*.jsonl"))     # 改进 B/H 审计行已落盘
    m = json.loads((B._bridge_root() / "jobs" / s["job_id"] / "manifest.json")
                   .read_text(encoding="utf-8"))
    assert m["state"] == "done" and m["overlay_digest"]


def test_proposal_materialize_via_proposal_cli(tmp_path, monkeypatch):
    """改进 E：物化走 proposal CLI 接缝；活 yaml 全程只读。"""
    calls = []

    def fake_cli(args, timeout_s=None):        # ④（P1）：接缝带作业级 timeout_s
        calls.append(args)
        return ""

    monkeypatch.setattr(B, "_run_proposal_cli", fake_cli)
    monkeypatch.setattr(B, "Popen", _fake_popen())
    fake_tree = _patch_strategy_tree(monkeypatch, tmp_path)
    live = fake_tree / "break.yaml"
    live.write_text("params: {x: 1}\n", encoding="utf-8")
    s = B.run_backtest_job(
        carrier={"kind": "proposal",
                 "proposal": {"strategy": "break", "edits": []}},
        scope="codes", codes=["000001"], days=30)
    assert s["ok"] is True
    assert calls[0][0] == "validate" and calls[1][0] == "apply"
    assert "--apply" in calls[1]                                  # I4：物化唯一通道回归钉
    job_overlay = Path(s["report_path"]).parent / "overlay"
    assert any(str(job_overlay) in str(a) for a in calls[1])      # apply 只指作业目录
    assert live.read_text(encoding="utf-8") == "params: {x: 1}\n"  # 活树未动


def test_failure_is_honest(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "Popen", _fake_popen(rc=2, stderr="boom\nValueError: 坏策略"))
    fake_tree = _patch_strategy_tree(monkeypatch, tmp_path)
    (fake_tree / "break.yaml").write_text("meta: {}\n", encoding="utf-8")
    s = B.run_backtest_job(carrier={"kind": "existing", "key": "break"},
                           scope="codes", codes=["000001"], days=10)
    assert s["ok"] is False and "ValueError" in s["error"]        # 改进 H
    m = json.loads((B._bridge_root() / "jobs" / s["job_id"] / "manifest.json")
                   .read_text(encoding="utf-8"))
    assert m["state"] == "failed"


def test_timeout_kills_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "Popen", _fake_popen(timeout=True))
    killed = []
    monkeypatch.setattr(B, "_kill_tree", lambda proc: killed.append(proc.pid))
    fake_tree = _patch_strategy_tree(monkeypatch, tmp_path)
    (fake_tree / "break.yaml").write_text("meta: {}\n", encoding="utf-8")
    s = B.run_backtest_job(carrier={"kind": "existing", "key": "break"},
                           scope="codes", codes=["000001"], days=10,
                           timeout_s=1)
    assert s["ok"] is False and "超时" in s["error"]
    assert killed == [4242]                                       # 改进 C


def test_new_strategy_key_prefix(tmp_path):
    with pytest.raises(B.BridgeError):
        B.run_backtest_job(carrier={"kind": "new", "py": "x", "yaml": {}},
                           strategy_key="not_prefixed", scope="codes", codes=["000001"])


def test_gc_moves_stale_jobs_to_del(tmp_path, monkeypatch):
    d = B._bridge_root() / "jobs" / "stale"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"job_id": "stale", "state": "running"}),
                                     encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(d, (old, old))
    moved = B.gc_jobs()
    assert moved == ["stale"]
    assert not d.exists() and (B._bridge_root() / "_del" / "stale").exists()  # 项目规则：移到 del
    m = json.loads((B._bridge_root() / "_del" / "stale" / "manifest.json")
                   .read_text(encoding="utf-8"))
    assert m["state"] == "failed"                                 # 悬挂 running 不许留


def test_parse_carrier_rejects_fullwidth_codes():
    """⑪（P2）：str.isdigit() 对全角数字返回 True ⇒ 会在取数层才炸。
    parse_carrier 是载体规整单源，全角码必须在这里就拒（ASCII 数字才收）。"""
    with pytest.raises(B.BridgeError):
        B.parse_carrier(strategy_key="break", codes="０００００１")
    with pytest.raises(B.BridgeError):
        B.parse_carrier(strategy_key="break", codes="000001,１２")


def test_gc_two_stage_ttl_cleans_runs_and_del(tmp_path, monkeypatch):
    """⑭（P2）：GC 二段回收——jobs/ 移 _del/ 之后，runs/ 审计行与 _del/ 归档区
    各按自身 TTL 真删（此前两处从不清理 = 无限增长）。TTL 均 env 可调。"""
    monkeypatch.setattr(B, "RUNS_TTL_DAYS", 7)
    monkeypatch.setattr(B, "DEL_TTL_DAYS", 7)
    root = B._bridge_root()
    runs = root / "runs"
    runs.mkdir(parents=True)
    (runs / "20260101_old.jsonl").write_text("{}\n", encoding="utf-8")
    (runs / "20261015_new.jsonl").write_text("{}\n", encoding="utf-8")
    old = time.time() - 10 * 86400
    os.utime(runs / "20260101_old.jsonl", (old, old))
    dele = root / "_del"
    (dele / "oldjob").mkdir(parents=True)
    (dele / "newjob").mkdir(parents=True)
    os.utime(dele / "oldjob", (old, old))
    B.gc_jobs()
    assert not (runs / "20260101_old.jsonl").exists()      # 超 TTL 审计行删
    assert (runs / "20261015_new.jsonl").exists()          # 未超 TTL 保留
    assert not (dele / "oldjob").exists()                  # 归档超 DEL_TTL 真删
    assert (dele / "newjob").exists()                      # 未超 TTL 保留


def test_gc_del_ttl_counts_from_archive_time(tmp_path, monkeypatch):
    """⑭ 配套：_del 条目 TTL 从**归档时刻**起算——move 时打 mtime=now。
    若沿用作业创建时的旧 mtime，刚归档的作业会被下次 GC 当超龄立即误删。"""
    monkeypatch.setattr(B, "DEL_TTL_DAYS", 7)
    d = B._bridge_root() / "jobs" / "stale2"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"job_id": "stale2", "state": "done"}),
                                     encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(d, (old, old))
    assert B.gc_jobs() == ["stale2"]
    archived = B._bridge_root() / "_del" / "stale2"
    assert archived.is_dir()
    assert B.gc_jobs() == []          # 二次 GC：归档时刻起算未超 TTL ⇒ 不得删
    assert archived.is_dir()


def test_guard_job_dir_not_in_strategy_tree(tmp_path, monkeypatch):
    """C3 修正：STRATEGY_DIR 指向 tmp_path，守卫失败必须零落盘（不再碰活策略树）。"""
    fake_tree = _patch_strategy_tree(monkeypatch, tmp_path)
    monkeypatch.setenv("QD_BT_ROOT", str(fake_tree / "jail"))
    job = B._bridge_root() / "jobs" / "x"
    with pytest.raises(B.BridgeError):
        B._assert_guards(job)          # 守卫先于任何 mkdir 被调用
    assert not (fake_tree / "jail").exists()   # 钉：违规配置不得在策略树内留痕


def test_guard_sibling_dir_allowed(tmp_path, monkeypatch):
    """守卫正向回归：兄弟目录（strategies_backup 场景）不得被裸 startswith 误拒。"""
    fake_tree = _patch_strategy_tree(monkeypatch, tmp_path)
    sibling = tmp_path / "strategies_backup"
    sibling.mkdir()
    monkeypatch.setenv("QD_BT_ROOT", str(sibling / "jobs_root"))
    B._assert_guards(B._bridge_root() / "jobs" / "x")   # 不抛 = 通过


def test_stale_running_manifest_does_not_block(tmp_path, monkeypatch):
    """I2：超期 running（ts+timeout_s 已过）= 崩溃残留，判死标 failed 且不再占并发闸。

    全程 fake 子进程 + STRATEGY_DIR 指 tmp_path（本文件头部契约：不碰真实回测/DB）。
    """
    monkeypatch.setattr(B, "Popen", _fake_popen())
    monkeypatch.setattr(B, "MAX_CONCURRENT", 1)
    fake_tree = _patch_strategy_tree(monkeypatch, tmp_path)
    (fake_tree / "break.yaml").write_text("meta: {}\n", encoding="utf-8")
    d = B._bridge_root() / "jobs" / "dead"
    d.mkdir(parents=True)
    old_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 3600))
    (d / "manifest.json").write_text(
        json.dumps({"job_id": "dead", "state": "running", "ts": old_ts,
                    "timeout_s": 900}), encoding="utf-8")
    B.run_backtest_job(carrier={"kind": "existing", "key": "break"},
                       codes=["000001"], days=10)
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    assert m["state"] == "failed" and "stale" in m["error"]


# ---------------------------------------------------------------------------
# Task 7 守卫（backtest_bridge_plan.md，2026-10-15）：桥自身的不变量测试。
# 守卫必须 AST 级（改进 N）：文本 `in src` 匹配会被 docstring 里的解释文字误伤恒红。
# `_ast_violations` 在 tests/_ast_guard.py（跨文件单一真源，批次D 质量审查上移；
# 三种静态 import 形态闭环 + 边界匹配 + 相对导入 package 解析，详见其头注释），
# 同时供 Task 8（svc 泳道）复用，守 backtest_svc_cli 只准调 run_strategy_snapshot、
# 绝不 persist_run/ensure_storage_schema。
# ---------------------------------------------------------------------------


def test_bridge_never_imports_auto_writers():
    """原则③静态守卫：桥源码禁止 import auto 的写者/扫描/重建模块（AST 级，改进 N）。"""
    text = Path(B.__file__).read_text(encoding="utf-8")
    assert not _ast_violations(
        text,
        import_prefixes=("app.market_cn.auto.scan", "app.market_cn.auto.rebuild",
                         "app.market_cn.auto.monitor", "app.market_cn.auto.startup",
                         "app.market_cn.auto.sched"),
        call_names=("run_scan", "load_windows", "rebuild"),
        package="app.agent.chain",
    )


def test_tool_file_separate_from_backtest_tools():
    """§5.7：桥工具不得并入指标/脚本策略链文件（AST import 级）。

    双泳道隔离核心不变量：主进程（agent 工具面）零 import svc 回测引擎
    （services/backtest.py 的 BacktestService 等）——svc 引擎只允许在
    backtest_svc_cli 子进程内被消费（Task 8 守住 CLI 侧同一不变量）。
    """
    from app.agent.tools.finance import auto_bt_tools
    text = Path(auto_bt_tools.__file__).read_text(encoding="utf-8")
    assert not _ast_violations(text, import_prefixes=("app.services.backtest",),
                               package="app.agent.tools.finance")


def test_ast_helper_catches_submodule_import_via_parent():
    """盲区修正回归钉（2026-10-15 规格审查）：三种静态 import 形态闭环。

    ① 绝对（module 前缀直中）：`from app.services.backtest import BacktestService`；
    ② 经父包导入子模块：`from app.market_cn.auto import scan` —— module 不含
       子模块名，须把 names[].name（原始名，非 asname）拼上 module 再匹配；
    ③ 相对 import：`from ...services import backtest` —— level=3 从
       package="app.agent.chain" 解析回 app.services（桥真实所在的包层级，
       `...` 恰好回到 app.），不解析 = 绕过守卫。
    """
    src = ("from app.market_cn.auto import scan\n"
           "from app.services.backtest import BacktestService\n"
           "from app.services import backtest\n"
           "from ...services import backtest\n"
           "from ....services import backtest\n"
           "from .....services import backtest\n")
    got = _ast_violations(
        src,
        import_prefixes=("app.services.backtest", "app.market_cn.auto.scan",
                         "app.market_cn.auto.rebuild", "app.market_cn.auto.monitor",
                         "app.market_cn.auto.startup", "app.market_cn.auto.sched"),
        package="app.agent.chain",
    )
    assert "from app.market_cn.auto import scan" in got
    # module 前缀直中形态的报文是省略号格式（不逐名展开）
    assert "from app.services.backtest import ..." in got
    # 相对形态报文用解析后的真实模块名（`...` → app.），与绝对形态同文各一条
    assert got.count("from app.services import backtest") == 2
    # fail-closed：level=4（越顶）与 level=5（负切片静默误解析路径）都必报违规，
    # package=app.agent.chain 仅容 level≤3（level=3 恰好回到 app.）
    assert any(v.startswith("relative import level=4 无法解析") for v in got)
    assert any(v.startswith("relative import level=5 无法解析") for v in got)


# ---------------------------------------------------------------------------
# Task 8: svc 泳道（backtest_bridge_plan.md）：指标/脚本策略链桥。
# 载体/守卫/摘要三段与 auto 泳道全不同（snapshot 直构 vs overlay 物化；窗口钳制
# vs codes 预算；标量摘要 vs stats 搬运），只共享 helper 层。测试契约同文件头：
# 全部 fake 子进程，不依赖 DB/网络；svc CLI 守卫走 _ast_guard（改进 N，AST 调用级）。
# ---------------------------------------------------------------------------

_SVC_REPORT = {"totalReturn": 12.5, "annualReturn": 30.0, "maxDrawdown": 8.1,
               "sharpeRatio": 1.4, "winRate": 55.0, "profitFactor": 1.8,
               "totalTrades": 24, "totalProfit": 12500.0,
               "trades": [{"time": "2025-01-06", "profit": 1.0}] * 30,
               "equityCurve": [1.0] * 300}


def _fake_svc_popen(report=_SVC_REPORT, rc=0, stderr="", timeout=False):
    def factory(cmd, **kwargs):
        out = cmd[cmd.index("--out") + 1]
        Path(out).write_text(json.dumps(report), encoding="utf-8")
        assert kwargs.get("cwd") == str(B._BACKEND_ROOT)
        assert "QD_STRATEGY_YAML_OVERLAY_DIR" not in kwargs["env"]   # svc 泳道零注入
        return _FakeProc(rc=rc, stderr=stderr, timeout=timeout)
    return factory


def test_svc_code_carrier_happy_path(tmp_path, monkeypatch):
    """改进 M：执行假设完整透传 + cost_model 口径标注。"""
    monkeypatch.setattr(B, "Popen", _fake_svc_popen())
    s = B.run_svc_backtest_job(
        carrier={"kind": "svc_code", "run_type": "strategy_script",
                 "code": "def on_bar(ctx): pass"},
        symbol="600519", commission=0.0003, slippage=0.001,
        strategy_config={"ashare_rules": {"t_plus_1": True, "limit_pct": 0.10}},
        start_date="2025-01-01", end_date="2025-03-31",
        session_id="sess-2")
    assert s["ok"] is True and s["lane"] == "svc"
    assert s["summary_metrics"]["totalReturn"] == 12.5          # 改进 A：标量原样搬运
    assert "trades" not in s["summary_metrics"]                 # 改进 L：大件不进摘要
    assert s["cost_model"] == {"market": "CNStock", "commission": 0.0003,
                               "slippage": 0.001, "trade_direction": "long",
                               "strategy_config_provided": True,
                               "source": "bridge_params"}       # 改进 M：口径诚实标注
    spec = json.loads((B._bridge_root() / "jobs" / s["job_id"] / "snapshot.json")
                      .read_text(encoding="utf-8"))
    assert spec["snapshot"]["strategy_config"]["ashare_rules"]["t_plus_1"] is True
    assert spec["snapshot"]["commission"] == 0.0003
    m = json.loads((B._bridge_root() / "jobs" / s["job_id"] / "manifest.json")
                   .read_text(encoding="utf-8"))
    assert m["lane"] == "svc" and m["snapshot_digest"] and m["cost_model"]["market"] == "CNStock"
    row_files = list((B._bridge_root() / "runs").glob(f"*_{s['job_id']}.jsonl"))
    assert row_files and json.loads(row_files[0].read_text(encoding="utf-8"))["lane"] == "svc"


def test_svc_strategy_carrier_dispatch(tmp_path, monkeypatch):
    """对照组：strategy_id 只透传 snapshot.json，resolver 在子进程内执行（桥不碰 DB）；
    cost_model.source=resolver_snapshot —— 口径由子进程内 resolver 决定（F15）。"""
    monkeypatch.setattr(B, "Popen", _fake_svc_popen())
    s = B.run_svc_backtest_job(carrier={"kind": "svc_strategy", "strategy_id": 7},
                               symbol="600519", start_date="2025-01-01",
                               end_date="2025-02-01")
    assert s["ok"] is True and s["carrier"] == "svc_strategy"
    assert s["cost_model"]["source"] == "resolver_snapshot"
    spec = json.loads((B._bridge_root() / "jobs" / s["job_id"] / "snapshot.json")
                      .read_text(encoding="utf-8"))
    assert spec["kind"] == "svc_strategy" and spec["strategy_id"] == 7
    assert "snapshot" not in spec                                # 对照组不直构


def test_svc_carrier_validation():
    kw = dict(symbol="600519", start_date="2025-01-01", end_date="2025-02-01")
    with pytest.raises(B.BridgeError):
        B.run_svc_backtest_job(carrier={"kind": "new", "py": "x"}, **kw)      # 载体非法
    with pytest.raises(B.BridgeError):
        B.run_svc_backtest_job(carrier={"kind": "svc_code", "code": "  "}, **kw)  # 缺 code
    with pytest.raises(B.BridgeError):
        B.run_svc_backtest_job(carrier={"kind": "svc_code", "code": "x",
                                        "run_type": "evil"}, **kw)            # run_type 非法
    with pytest.raises(B.BridgeError):
        B.run_svc_backtest_job(carrier={"kind": "svc_strategy", "strategy_id": True},
                               **kw)                                          # bool 冒充 int
    with pytest.raises(B.BridgeError):
        B.run_svc_backtest_job(carrier={"kind": "svc_code", "code": "x"}, market="Mars",
                               **kw)                                          # market 非枚举（改进 M）
    with pytest.raises(B.BridgeError):
        B.run_svc_backtest_job(carrier={"kind": "svc_code", "code": "x"},
                               commission=-0.1, **kw)                         # 费率负数
    with pytest.raises(B.BridgeError):
        B.run_svc_backtest_job(carrier={"kind": "svc_code", "code": "x"},
                               commission="abc", **kw)                        # 费率非数字（I1）
    with pytest.raises(B.BridgeError):
        B.run_svc_backtest_job(carrier={"kind": "svc_code", "code": "x"},
                               timeout_s=0, **kw)                # I1：timeout_s 走 _to_positive_int
    with pytest.raises(B.BridgeError):
        B.run_svc_backtest_job(carrier={"kind": "svc_code", "code": "x"},
                               strategy_config="not-a-dict", **kw)            # strategy_config 类型
    with pytest.raises(B.BridgeError):
        B.run_svc_backtest_job(carrier={"kind": "svc_code", "code": "x"},
                               symbol="600519", start_date="2025-01-01",
                               end_date="2025-01-01")                          # 窗口非法
    with pytest.raises(B.BridgeError):
        B.run_svc_backtest_job(carrier={"kind": "svc_code", "code": "x"},
                               **{**kw, "end_date": "2025-99-01"})             # 日期格式


def test_svc_window_over_budget(monkeypatch):
    """窗口超 QD_BT_MAX_DAYS 预算；MAX_DAYS 补钉防开发机 env 渗入（本文件头约定）。"""
    monkeypatch.setattr(B, "MAX_DAYS", 365)
    with pytest.raises(B.BridgeError):
        B.run_svc_backtest_job(carrier={"kind": "svc_code", "code": "x"},
                               symbol="600519", start_date="2020-01-01",
                               end_date="2030-12-31")     # > QD_BT_MAX_DAYS


def test_svc_failure_is_honest(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "Popen", _fake_svc_popen(rc=2, stderr="ValueError: No candle data"))
    s = B.run_svc_backtest_job(carrier={"kind": "svc_code", "code": "x"},
                               symbol="600519", start_date="2025-01-01",
                               end_date="2025-02-01")
    assert s["ok"] is False and "No candle data" in s["error"]     # 改进 H


def test_svc_cli_source_never_persists():
    """改进 L 守卫：svc CLI 禁止调用落库方法（AST 调用级，改进 N）。"""
    import app.agent.scripts.backtest_svc_cli as C
    text = Path(C.__file__).read_text(encoding="utf-8")
    assert not _ast_violations(
        text, call_names=("persist_run", "ensure_storage_schema"))


def test_svc_cli_only_talks_to_services():
    """svc CLI 运行时只依赖 app.services.*（不 import 桥/能力层/工具面，AST import 级）。"""
    import app.agent.scripts.backtest_svc_cli as C
    text = Path(C.__file__).read_text(encoding="utf-8")
    assert not _ast_violations(
        text, import_prefixes=("app.agent.chain", "app.agent.capabilities",
                               "app.agent.tools"))


def test_svc_cli_build_snapshot_direct():
    import app.agent.scripts.backtest_svc_cli as C
    snap = C._build_snapshot({"kind": "svc_code",
                              "snapshot": {"code": "def on_bar(ctx): pass",
                                           "run_type": "strategy_script",
                                           "market": "CNStock", "symbol": "600519",
                                           "timeframe": "1D"}})
    assert snap["symbol"] == "600519" and snap["run_type"] == "strategy_script"


def test_svc_cli_build_snapshot_resolver(monkeypatch):
    """svc_strategy：替身引擎验证分发语义（不连 DB）；重依赖在 _load_engines 内惰性 import。"""
    import app.agent.scripts.backtest_svc_cli as C
    captured = {}

    class _FakeResolver:
        def __init__(self, user_id=1):
            captured["user_id"] = user_id

        def resolve(self, strategy, override):
            captured.update(override)
            return {"code": "SNAP", "symbol": override.get("symbol", ""),
                    "timeframe": override.get("timeframe", "1D")}

    class _FakeStrategySvc:
        def __init__(self, user_id=1):
            pass

        def get_strategy(self, strategy_id, user_id=1):
            captured["strategy_id"] = strategy_id
            return {"id": strategy_id}

    monkeypatch.setattr(C, "_load_engines",
                        lambda: (object(), _FakeStrategySvc, _FakeResolver))
    snap = C._build_snapshot({"kind": "svc_strategy", "strategy_id": 7,
                              "symbol": "600519", "timeframe": "1D", "user_id": 3})
    assert captured["strategy_id"] == 7 and captured["user_id"] == 3
    assert snap["code"] == "SNAP" and snap["symbol"] == "600519"


def test_bridge_never_imports_backtest_service():
    """改进 K 守卫：桥主进程禁止 import svc 引擎（svc 引擎只在子进程活，AST 级）。"""
    text = Path(B.__file__).read_text(encoding="utf-8")
    assert not _ast_violations(
        text, import_prefixes=("app.services.backtest",),
        call_names=("persist_run", "ensure_storage_schema"),
        package="app.agent.chain")


# ---------------------------------------------------------------------------
# P1 修复（2026-10-16，回测桥审计 §9；backtest_bridge_plan.md v7）。
# 每项先红后绿：测试钉住契约，实现最小改动。
# ---------------------------------------------------------------------------

def test_overlay_digest_excludes_dot_dirs(tmp_path):
    """⑧（P1）：proposal apply 的 .history 备份随 --strategy-dir 落在 overlay 内
    （proposal.py L375 hist = strategy_dir/.history），rglob 全收 ⇒ digest 随备份
    文件漂移 = 可复现证据集被污染（系统性：每个 proposal 档作业都中）。点目录必须排除。"""
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "break.yaml").write_text("params: {x: 1}\n", encoding="utf-8")
    files_1, digest_1 = B._overlay_digest(overlay)
    hist = overlay / ".history"
    hist.mkdir()
    (hist / "break.20261016000000.yaml").write_text("params: {x: 1}\n", encoding="utf-8")
    files_2, digest_2 = B._overlay_digest(overlay)
    assert files_2 == files_1 and digest_2 == digest_1     # 备份不得进证据集
    assert not any(".history" in k for k in files_2)


def test_existing_carrier_live_yaml_evidence(tmp_path, monkeypatch):
    """⑧ 配套：existing 档 overlay 恒空（活 yaml 不落 overlay、活树全程只读）⇒
    overlay_digest 是 sha256("{}") 常数、无证据力。活 yaml 内容 sha 必须直接入
    manifest——不落 overlay 也有物证（改进 B 最小证据集对齐 proposal/new 双档）。"""
    monkeypatch.setattr(B, "Popen", _fake_popen())
    fake_tree = _patch_strategy_tree(monkeypatch, tmp_path)
    # write_bytes：Windows 文本模式会把 \n 译成 \r\n，字节级 sha 断言必须绕开翻译
    (fake_tree / "break.yaml").write_bytes(b"params: {x: 1}\n")
    s = B.run_backtest_job(carrier={"kind": "existing", "key": "break"},
                           scope="codes", codes=["000001"], days=30)
    m = json.loads((B._bridge_root() / "jobs" / s["job_id"] / "manifest.json")
                   .read_text(encoding="utf-8"))
    expect = hashlib.sha256(b"params: {x: 1}\n").hexdigest()[:16]
    assert m["live_yaml_digest"] == expect


def _read_row(job_id: str) -> dict:
    """读该作业的审计行（一行一封、作业终态即写毕）。"""
    files = list((B._bridge_root() / "runs").glob(f"*_{job_id}.jsonl"))
    assert files
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_jsonl_row_svc_lane_fields(tmp_path, monkeypatch):
    """⑤（P1）：svc 审计行必须自洽——symbol/market/窗口/cost_model/summary_metrics/
    snapshot_digest 进 runs/*.jsonl。此前 _append_jsonl 把 svc summary 的这些字段
    整体丢弃，审计行连「回测了哪只票、什么窗口」都答不出，取证必须 join manifest。"""
    monkeypatch.setattr(B, "Popen", _fake_svc_popen())
    s = B.run_svc_backtest_job(
        carrier={"kind": "svc_code", "run_type": "strategy_indicator", "code": "x"},
        symbol="600519", start_date="2025-01-01", end_date="2025-03-31",
        commission=0.0003)
    row = _read_row(s["job_id"])
    assert row["lane"] == "svc"
    assert row["symbol"] == "600519" and row["market"] == "CNStock"
    assert row["timeframe"] == "1D"
    assert row["start_date"] == "2025-01-01" and row["end_date"] == "2025-03-31"
    assert row["cost_model"]["commission"] == 0.0003
    assert row["summary_metrics"]["totalReturn"] == 12.5
    assert row["snapshot_digest"]


def test_jsonl_row_auto_lane_schema_unchanged(tmp_path, monkeypatch):
    """⑤ 配套：auto 行 schema 零漂移——按 lane 取字段 ≠ 给 auto 行加 svc 键
    （既有审计消费方解析不中断）。"""
    monkeypatch.setattr(B, "Popen", _fake_popen())
    fake_tree = _patch_strategy_tree(monkeypatch, tmp_path)
    (fake_tree / "break.yaml").write_bytes(b"meta: {}\n")
    s = B.run_backtest_job(carrier={"kind": "existing", "key": "break"},
                           scope="codes", codes=["000001"], days=10)
    row = _read_row(s["job_id"])
    assert row["lane"] == "auto" and row["stats"]["n"] == 5
    for k in ("symbol", "market", "timeframe", "cost_model",
              "start_date", "end_date", "summary_metrics", "snapshot_digest"):
        assert k not in row


def test_svc_initial_capital_into_snapshot(tmp_path, monkeypatch):
    """⑰-svc（P1）：initial_capital 入 snapshot 直构键集——读侧现成
    （services/backtest.py L1582 snapshot.get('initial_capital')），桥只补写键。
    不传 = 不写键（引擎默认 10000，零行为变化）。"""
    monkeypatch.setattr(B, "Popen", _fake_svc_popen())
    s = B.run_svc_backtest_job(
        carrier={"kind": "svc_code", "run_type": "strategy_indicator", "code": "x"},
        symbol="600519", start_date="2025-01-01", end_date="2025-03-31",
        initial_capital=50000)
    spec = json.loads((B._bridge_root() / "jobs" / s["job_id"] / "snapshot.json")
                      .read_text(encoding="utf-8"))
    assert spec["snapshot"]["initial_capital"] == 50000.0
    s2 = B.run_svc_backtest_job(
        carrier={"kind": "svc_code", "run_type": "strategy_indicator", "code": "x"},
        symbol="600519", start_date="2025-01-01", end_date="2025-03-31")
    spec2 = json.loads((B._bridge_root() / "jobs" / s2["job_id"] / "snapshot.json")
                       .read_text(encoding="utf-8"))
    assert "initial_capital" not in spec2["snapshot"]


def test_svc_initial_capital_rejects_non_positive(tmp_path):
    """⑰-svc 配套：0/负数/非数字串 = 调用方错误（BridgeError），不做静默钳制
    （与 _to_positive_int 同族语义）。"""
    for bad in (0, -5, "abc"):
        with pytest.raises(B.BridgeError):
            B.run_svc_backtest_job(
                carrier={"kind": "svc_code", "run_type": "strategy_indicator",
                         "code": "x"},
                symbol="600519", start_date="2025-01-01", end_date="2025-03-31",
                initial_capital=bad)


def test_svc_initial_capital_rejected_for_strategy_carrier(tmp_path):
    """⑰-svc 边界：svc_strategy 对照组走 resolver（DB 策略自有本金配置），
    CLI 的 override 只透 symbol/timeframe（backtest_svc_cli L72-76）⇒ 桥侧诚实
    拒绝，不许「签名收了但静默丢弃」的假契约。"""
    with pytest.raises(B.BridgeError):
        B.run_svc_backtest_job(
            carrier={"kind": "svc_strategy", "strategy_id": 7},
            symbol="600519", start_date="2025-01-01", end_date="2025-03-31",
            initial_capital=50000)


def test_proposal_cli_honors_timeout_s(tmp_path, monkeypatch):
    """④（P1）：proposal materialize 的 validate/apply 子步骤必须接作业级
    timeout_s——此前 _run_proposal_cli 用全局 TIMEOUT_S，per-job 契约在 proposal
    档被静默忽视（主回测执行路径 _run_backtest_cli→_spawn_and_wait 早已接）。"""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        return _P()

    monkeypatch.setattr(B.subprocess, "run", fake_run)
    monkeypatch.setattr(B, "Popen", _fake_popen())
    fake_tree = _patch_strategy_tree(monkeypatch, tmp_path)
    (fake_tree / "break.yaml").write_bytes(b"params: {x: 1}\n")
    s = B.run_backtest_job(
        carrier={"kind": "proposal",
                 "proposal": {"strategy": "break", "edits": []}},
        scope="codes", codes=["000001"], days=30, timeout_s=123)
    assert s["ok"] is True
    assert seen["timeout"] == 123                                   # 作业级超时透传


def test_terminal_write_failure_degrades_honestly(tmp_path, monkeypatch):
    """①（P1）：终态落盘（manifest/summary/jsonl）失败不逃逸——降级诚实 failed
    摘要返回（ok=False+error；stats 保留为已算出的事实，不粉饰、不留漂移状态）。"""
    monkeypatch.setattr(B, "Popen", _fake_popen())
    fake_tree = _patch_strategy_tree(monkeypatch, tmp_path)
    (fake_tree / "break.yaml").write_bytes(b"meta: {}\n")

    real_write = B._write_manifest
    state = {"n": 0}

    def flaky_write(job, manifest):
        state["n"] += 1
        if state["n"] >= 3:          # 第 3 次 = 终态 manifest（1=running 2=digest 更新）
            raise OSError("disk full")
        real_write(job, manifest)

    monkeypatch.setattr(B, "_write_manifest", flaky_write)
    s = B.run_backtest_job(carrier={"kind": "existing", "key": "break"},
                           scope="codes", codes=["000001"], days=10)
    assert s["ok"] is False and "终态落盘失败" in s["error"]
    assert s["stats"]["n"] == 5      # 已算出的事实保留，不因落盘失败而丢弃


def test_svc_terminal_write_failure_degrades_honestly(tmp_path, monkeypatch):
    """① 配套：svc 泳道同款——终态 manifest 写失败时返回诚实 failed 摘要，
    已算出的 summary_metrics 保留。"""
    monkeypatch.setattr(B, "Popen", _fake_svc_popen())
    real_write = B._write_manifest
    state = {"n": 0}

    def flaky_write(job, manifest):
        state["n"] += 1
        if state["n"] >= 2:          # svc：1=running 2=终态
            raise OSError("disk full")
        real_write(job, manifest)

    monkeypatch.setattr(B, "_write_manifest", flaky_write)
    s = B.run_svc_backtest_job(
        carrier={"kind": "svc_code", "run_type": "strategy_indicator", "code": "x"},
        symbol="600519", start_date="2025-01-01", end_date="2025-03-31")
    assert s["ok"] is False and "终态落盘失败" in s["error"]
    assert s["summary_metrics"]["totalReturn"] == 12.5
