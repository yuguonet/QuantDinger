# -*- coding: utf-8 -*-
"""chain/backtest_bridge.py — agent × 双回测链独立桥（AGENT_DESIGN_v3.md §5.7，2026-10-15）。

定位：agent 想出的策略假设 → 归一化摘要回传。两条泳道共享作业目录/manifest/JSONL/GC/
预算基建（manifest 以 lane 字段区分），载体重材料互不套管线：
  - auto 泳道（run_backtest_job）：auto 官方回测口径（run_all），proposal 改参/加门或
    全新 .py+.yaml 直传；
  - svc 泳道（run_svc_backtest_job，Task 8）：指标/脚本策略链（BacktestService），
    indicator DSL / script 代码字符串直构 snapshot，子进程纯计算
    （scripts/backtest_svc_cli.py），零落库（persist_run/ensure_storage_schema 绝不调用）。
    svc 结论按「探索性参考」表述进 answer（svc_code 是 agent 自由代码，带前视/理想化
    风险），不与 auto 泳道（有生产锚点）结论同权重进 T+N 问责闭环。
三原则（用户裁定）：
  ① 独立桥：对 auto 只加 env 默认关闭的读钩子（strategies/_yaml_doc、autodiscover、
     evaluate.load_strategy 三处）；对 agent 只新增本模块 + auto_bt_tools.py +
     skills/auto-backtest，不动现有回测链。
  ② 直传策略：proposal JSON（①改参/②加门，②是①的子集）或新策略 .py+.yaml（③）
     由 agent 直接传入，物化到**作业目录**，永不写活 strategies/。
  ③ 不污染：子进程隔离（_REGISTRY/_YAML_DOC_CACHE/配置缓存全在子进程私有）；零写共享表
     （qd_backtest_runs/qd_dragon_signals/qd_watchlist 都不碰）；磁盘写域 =
     data/backtest_bridge/**；回滚 = 移除作业目录。

数据流：
  run_backtest_job(carrier)
    → jobs/<job_id>/manifest.json(running) + overlay/<key>.yaml[.py]
    → (①/②) 子进程 proposal validate + apply --strategy-dir <job>/overlay --apply
      （E 修正：--apply 禁令精确化——禁的是活树；作业目录内 apply 是唯一物化通道，
       .history 备份随 --strategy-dir 落在作业目录内）
    → 子进程 backtest_cli --out <job>/report.json --progress-every 0
      （cwd=backend 根 + env QD_STRATEGY_YAML_OVERLAY_DIR 仅子进程）
    → report.json 解析：stats 原样搬运，桥绝不重算指标（口径单源 core.backtest._summary）
    → jobs/<job_id>/summary.json + runs/<date>_<job>.jsonl（一行审计，含失败）

易错点：
  - 子进程必须 cwd=backend_api_python（CLI 的 .env 与 `python -m` 解析都依赖它）；
  - Windows Popen.kill() 不杀孙进程（git rev-parse / 行情拉取线程会残留）⇒ 超时走
    taskkill /T /F 杀整树；
  - 桥进程绝不 import auto 的策略注册表/写者（守卫测试 test_guard_* 钉住）；
  - 并发约束用 running manifest 计数而非文件锁（单机、低频，可读性好）；
  - claim 闭环不在桥内做：agent 把结论写进 answer，§5.1 intake 主流程自动过闸抽 claim；
  - 错误分两段（v3 施工修正）：调用方输入形状错误（载体/预算/并发/key 前缀）在 Phase 1
    落盘前直接抛 BridgeError；落盘后的 IO/执行失败一律诚实落盘 ok=False 不抛 ——
    输入形状校验若放 try 内会被 except 吞成失败摘要，测试 test_new_strategy_key_prefix
    与 docstring 契约都会落空；
  - 前移校验含两处（2026-10-15 规格审查补记）：new 载体的 strategy_key 前缀校验，
    以及 proposal 载体的 proposal.strategy 键名合法性校验 —— 二者同构（纯输入形状、
    无需 IO），同在 Phase 1 直接抛，_materialize_* 内保留同款校验作公共物化契约；
  - 守卫时序（2026-10-15 规格审查修正）：_assert_guards 必须在作业目录 mkdir **之前**
    调用 —— 否则 QD_BT_ROOT 误配进活策略树时，守卫抛错前已在树内留下作业目录；
    路径包含判断用 os.sep 锚定（strategies_backup 等兄弟目录不得误拒），
    相等（作业根=策略树本身）也算违规；
  - existing 载体的 key 从 carrier["key"] 读（2026-10-15 批次B质量审查 C1 修正：
    此前实现误用 strategy_key 参数默认 ""，existing 作业生产必败；参数仅作回退）；
  - summary 的 git_sha/effective_params 依赖 backtest_cli._build_report 透传
    run_all 的 meta 键（批次B质量审查 C2：CLI 侧补一行纯字段透传，桥不改判定）；
  - 悬挂判死（批次B质量审查 I2）：_running_jobs 对 running 项用 manifest 自带
    ts+timeout_s 判超期 —— 超期仍 running 只可能是桥中途崩溃残留，标 failed 不计数，
    堵「崩溃残留 manifest 锁桥」。
  - 载体规整单源（批次C质量审查 I4，2026-10-15）：parse_carrier 是工具面
    （tools/finance/auto_bt_tools.py）与技能面（skills/auto-backtest/run.py）共用的
    载体分发与入参规整唯一实现 —— 此前两面各写一份且技能面是降级副本：半③档被
    静默路由成 existing、畸形 JSON / days 非整数以裸异常逃逸。调用面只保留场景化
    提示（空 codes 指路技能）与错误归一（BridgeBusy 前置，BridgeBusy 是子类须先接）。
  - session_id/user_id 不注入（2026-10-15 用户裁定：「回测不用区分 session_id/
    user_id，谁回测都一样」）：三个入口（工具/技能/svc 工具）均不传 session_id
    ⇒ summary/manifest/JSONL 的 session_id 与审计行 conclusion_ref 恒空 ——
    改进 G 的弱关联按裁定放弃，问责主链走 §5.1 intake 过闸抽 claim，可复现证据
    = overlay_digest/git_sha/effective_params/job_id（全部 session 无关）。
    参数与字段保留为 reserved（禁止降智，不删）；将来若要会话归因，设计路径
    仍是覆写 get_tool_context() 补 session_id（与旧 M4 同族，按裁定搁置）。
    svc 的 user_id 仅对照组 resolver 的策略归属过滤用：默认 1 = 单用户部署
    假设，LLM 可经工具显式传（auto_bt_tools.agent_backtest_svc 已暴露）。
  - GC 二段回收（2026-10-15 P2-⑭）：jobs/ 超龄移 _del/（JOB_TTL_DAYS，只搬不删）
    → _del/ 归档按 DEL_TTL_DAYS 真删、runs/ 审计行按 RUNS_TTL_DAYS 真删；
    _del 条目 move 时打 mtime=now ⇒ TTL 从归档时刻起算（沿用作业创建时的旧
    mtime 会让刚归档的作业被下次 GC 立即误删）。三个 TTL 均 env 可调。
    ⚠ 前提（2026-10-16 P1-⑦）：QD_BT_TIMEOUT < QD_BT_JOB_TTL_DAYS×86400——
    GC（mtime+TTL）与判死（ts+timeout_s）两套时钟，详见 gc_jobs docstring。
  - 股票代码只收 ASCII 数字（P2-⑪）：str.isdigit() 对全角数字（"１２３"）也返回
    True，全角码会穿透到 CLI 取数层才炸——parse_carrier 单源处即拒。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from ..exec.job_runner import kill_tree
from ..log import logger

#: backend 根（backtest_cli 要求 cwd=backend_api_python；parents[3]=chain←agent←app←backend）
_BACKEND_ROOT = Path(__file__).resolve().parents[3]

ENV_OVERLAY_DIR = "QD_STRATEGY_YAML_OVERLAY_DIR"
CLI_MODULE = "app.market_cn.auto.backtest_cli"

#: 模块级别名（测试 monkeypatch B.Popen 的接缝；直接调 subprocess.Popen 会绕过替身）
Popen = subprocess.Popen


def _bridge_root() -> Path:
    """桥产物根（env 可改：测试/部署）；缺省 = backend_api_python/data/backtest_bridge。"""
    return Path(os.getenv("QD_BT_ROOT") or (_BACKEND_ROOT / "data" / "backtest_bridge"))


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, "") or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


#: 预算钳制（§5.7 ②）：env 可调，缺省保证「工具路径分钟级、技能路径十分钟级」
MAX_DAYS = _env_int("QD_BT_MAX_DAYS", 365)
MAX_CODES = _env_int("QD_BT_MAX_CODES", 50)
TIMEOUT_S = _env_int("QD_BT_TIMEOUT", 900)
MAX_CONCURRENT = _env_int("QD_BT_MAX_CONCURRENT", 1)
JOB_TTL_DAYS = _env_int("QD_BT_JOB_TTL_DAYS", 7)
#: 二段回收 TTL（2026-10-15 P2-⑭）：runs/ 审计行与 _del/ 归档区各自的保留期。
#: 禁硬编码——与 JOB_TTL_DAYS 同款 env 可调（GC 只删桥自己产物根下的数据）。
RUNS_TTL_DAYS = _env_int("QD_BT_RUNS_TTL_DAYS", 30)
DEL_TTL_DAYS = _env_int("QD_BT_DEL_TTL_DAYS", 30)


class BridgeError(ValueError):
    """调用方错误（预算超额 / 载体非法 / 守卫失败）。"""


class BridgeBusy(BridgeError):
    """并发预算已满（QD_BT_MAX_CONCURRENT）。"""


# ================================================================
# 预算 / 并发 / 守卫
# ================================================================

def _check_budget(days: int, codes_n: int) -> int:
    """days 上限 MAX_DAYS；scope=codes 时代码数上限 MAX_CODES（全盘走技能 scope=all）。"""
    if days < 1 or days > MAX_DAYS:
        raise BridgeError(f"days={days} 超出预算 [1, {MAX_DAYS}]（QD_BT_MAX_DAYS）")
    if codes_n > MAX_CODES:
        raise BridgeError(
            f"codes_n={codes_n} 超出预算 {MAX_CODES}（QD_BT_MAX_CODES）；"
            "全盘量级请走 auto-backtest 技能（scope=all）")
    return days


def _to_positive_int(value, name: str) -> int:
    """入参强转正整数；非整数/非正值 = 调用方错误（BridgeError），不做静默钳制。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise BridgeError(f"{name} 必须是整数: {value!r}") from None
    if v < 1:
        raise BridgeError(f"{name} 必须为正整数: {value!r}")
    return v


def _to_positive_float(value, name: str) -> float:
    """入参强转正浮点数；非数字/非正值 = 调用方错误（BridgeError），不做静默钳制。

    ⑰-svc（P1）：initial_capital 的归一（与 _to_positive_int 同族语义）。
    NaN/inf 一并拒（float("nan") 比较全 False，漏检会以 NaN 本金进引擎）。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise BridgeError(f"{name} 必须是数字: {value!r}") from None
    if not math.isfinite(v) or v <= 0:
        raise BridgeError(f"{name} 必须是正数: {value!r}")
    return v


# ================================================================
# 载体规整单源（工具面/技能面共用，批次C质量审查 I4）
# ================================================================

def parse_carrier(*, proposal_json: str = "", strategy_py: str = "",
                  strategy_yaml: str = "", strategy_key: str = "",
                  codes: str = "", days: int = 120) -> tuple:
    """工具面/技能面共用的载体规整单源（批次C质量审查 I4 根修）。

    把 LLM 传来的散装入参规整成桥的载体会话三元组。此前分发逻辑在工具面与技能面
    各写一份、技能面是降级副本，四个边界穿透：半③档静默降级 existing（C1）、
    畸形 proposal_json 以 JSONDecodeError 逃逸（I1）、非 object JSON 穿透（I3）、
    days='abc' 以裸 ValueError 逃逸（I2）——全部收敛到这里统一 raise BridgeError。

    Args（全部 keyword-only，对应 LLM 工具/技能入参的字符串形态）:
        proposal_json: proposal JSON 字符串（①/②档）
        strategy_py / strategy_yaml: ③档成对载体；**任一非空即按③档处理**，缺另一半
            必须报错（严禁静默降级为 existing——那会把新策略假设回测成对照组结论）；
            strategy_yaml 必须是 JSON **字符串**（内部 loads 成 dict 再进桥）
        strategy_key: existing 对照组 key，或③档新策略 key（agentbt_ 前缀由桥校验）
        codes: 逗号/空格分隔代码串；空串 → 空列表（是否允许空由调用方 scope 决定，
            桥对 scope=codes+空 codes 已有拒绝）；非数字代码 = BridgeError
        days: 回看自然日数（复用 _to_positive_int，非正整数 = BridgeError）

    Returns:
        (carrier, codes_list, days_int)

    Raises:
        BridgeError: 畸形/非 object JSON、③档不成对、缺 key、代码非数字、days 非正整数

    易错点：错误措辞用「不是合法 JSON」（参数类型没错、内容错，勿写成"必须是
    JSON 字符串"误导类型问题）；调用面只保留场景化提示（空 codes 指路技能，两面
    消息不同故留调用方）与 BridgeBusy 前置的错误归一。
    """
    code_list = [c for c in str(codes or "").replace(",", " ").split() if c]
    # ⑪（P2）：str.isdigit() 对全角数字（"１２３"）也返回 True，全角码会一路穿透到
    # CLI 取数层才炸——单源规整处就要求 ASCII 数字（isascii() Python 3.7+）。
    bad = [c for c in code_list if not (c.isascii() and c.isdigit())]
    if bad:
        raise BridgeError(f"股票代码应为数字: {', '.join(bad)}")
    days_int = _to_positive_int(days, "days")
    if proposal_json:
        try:
            proposal = json.loads(proposal_json)
        except (TypeError, ValueError) as e:
            raise BridgeError(f"proposal_json 不是合法 JSON: {e}") from None
        if not isinstance(proposal, dict):
            raise BridgeError("proposal_json 必须是 JSON object")
        return {"kind": "proposal", "proposal": proposal}, code_list, days_int
    if strategy_py or strategy_yaml:
        if not (strategy_py and strategy_yaml):
            raise BridgeError("③档必须同时提供 strategy_py 与 strategy_yaml")
        if not strategy_key:
            raise BridgeError("③档必须提供 strategy_key（agentbt_ 前缀）")
        if not isinstance(strategy_yaml, str):
            raise BridgeError("strategy_yaml 必须是 JSON 字符串")
        try:
            yaml_doc = json.loads(strategy_yaml)
        except (TypeError, ValueError) as e:
            raise BridgeError(f"strategy_yaml 不是合法 JSON: {e}") from None
        if not isinstance(yaml_doc, dict):
            raise BridgeError("strategy_yaml 必须是 JSON object")
        return {"kind": "new", "py": strategy_py, "yaml": yaml_doc}, code_list, days_int
    if strategy_key:
        return {"kind": "existing", "key": strategy_key}, code_list, days_int
    raise BridgeError("载体缺失：传 proposal_json（①/②档）或 strategy_py+strategy_yaml（③档）")


def _manifest_is_stale(m: dict) -> bool:
    """running manifest 是否已超期（ts + timeout_s + 60s 时序余量）。

    超期仍 running 只可能是持有者中途崩溃（执行器自身会按 timeout_s 杀树并落终态）；
    缺 ts/timeout_s（旧格式/手写 manifest）无法判死 → 保守按活计数（宁误忙勿漏并行）。
    """
    try:
        start = time.mktime(time.strptime(str(m.get("ts")), "%Y-%m-%dT%H:%M:%S"))
        timeout_s = int(m.get("timeout_s") or TIMEOUT_S)
    except (TypeError, ValueError):
        return False
    return (time.time() - start) > timeout_s + 60


def _running_jobs() -> list:
    """running manifest 计数（并发闸）；悬挂项判死后标 failed 不计数（I2，见模块头）。"""
    out = []
    for mp in _bridge_root().joinpath("jobs").glob("*/manifest.json"):
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if m.get("state") != "running":
            continue
        if _manifest_is_stale(m):
            m["state"] = "failed"
            m["error"] = "stale: 超过 timeout_s 仍 running，判定持有进程已死（崩溃残留）"
            try:
                mp.write_text(json.dumps(m, ensure_ascii=False, indent=2),
                              encoding="utf-8")
            except Exception:
                pass
            continue
        out.append(m.get("job_id") or mp.parent.name)
    return out


def _assert_guards(job: Path) -> None:
    """原则③守卫：作业目录绝不允许落在活策略树内（写域白名单的数据面闸门）。

    易错：startswith 必须以 os.sep 锚定 —— 裸前缀会把 strategies_backup 这类
    兄弟目录误判为树内；作业根恰好等于策略树本身也属违规（相等单独判）。
    """
    from app.market_cn.auto.core._paths import STRATEGY_DIR
    tree = str(Path(STRATEGY_DIR).resolve())
    loc = str(job.resolve())
    if loc == tree or loc.startswith(tree + os.sep):
        raise BridgeError("作业目录不得位于活策略树内（QD_BT_ROOT 配置错误）")


# ================================================================
# 载体物化（原则②；活树全程只读）
# ================================================================

def _live_yaml_path(key: str) -> Path:
    """活策略树 yaml 路径（只读引用；测试唯一接缝——monkeypatch 这里）。"""
    from app.market_cn.auto.core._paths import STRATEGY_DIR
    return Path(STRATEGY_DIR) / f"{key}.yaml"


def _run_proposal_cli(args: list, timeout_s: int = TIMEOUT_S) -> str:
    """子进程跑 auto/tools/proposal.py（validate 红线 / apply 物化）；非 0 退出 = 失败。

    ④（P1）：timeout_s 由作业级透传（validate/apply 是秒级 yaml 操作，全局
    TIMEOUT_S 作缺省）——此前此处用全局常量，per-job 超时契约在 proposal 档被
    静默忽视；主回测执行路径（_run_backtest_cli→_spawn_and_wait）早已接。
    """
    cmd = [sys.executable, "-m", "app.market_cn.auto.tools.proposal", *args]
    proc = subprocess.run(cmd, cwd=str(_BACKEND_ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout_s)
    if proc.returncode != 0:
        raise BridgeError(f"proposal {args[0]} 失败(exit={proc.returncode})，尾部输出:\n"
                          f"{(proc.stderr or proc.stdout or '')[-2000:]}")
    return proc.stdout or ""


def _materialize_proposal(job: Path, proposal: dict,
                          timeout_s: int = TIMEOUT_S) -> str:
    """①/②档：拷活 yaml → 作业 overlay，proposal validate+apply 只落作业目录。

    validate 的 before 值一致性检查针对 overlay 内副本（与活文件逐字节相同，检查语义不变）；
    apply 的 .history 备份随 --strategy-dir 落在作业目录内（proposal.py 落盘机制；
    该备份不进 digest 证据集——_overlay_digest 排除点目录，⑧）。
    """
    key = str(proposal.get("strategy") or "")
    if not key or any(s in key for s in ("/", "\\", "..")):
        raise BridgeError(f"proposal.strategy 非法: {key!r}")
    src = _live_yaml_path(key)
    if not src.is_file():
        raise BridgeError(f"策略 {key!r} 的活 yaml 不存在: {src}")
    overlay = job / "overlay"
    overlay.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, overlay / f"{key}.yaml")
    (job / "proposal.json").write_text(
        json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
    _run_proposal_cli(["validate", "--proposal", str(job / "proposal.json"),
                       "--strategy-dir", str(overlay)], timeout_s=timeout_s)
    _run_proposal_cli(["apply", "--proposal", str(job / "proposal.json"),
                       "--strategy-dir", str(overlay), "--apply"],
                      timeout_s=timeout_s)
    return key


def _materialize_new(job: Path, strategy_key: str, strategy_py: str,
                     strategy_yaml: dict) -> str:
    """③档：新策略 .py+.yaml 写进作业 overlay（生产 autodiscover 永远看不见）。"""
    if not strategy_key.startswith("agentbt_"):
        raise BridgeError(f"新策略 key 必须 agentbt_ 前缀（防撞生产 key）: {strategy_key!r}")
    if any(s in strategy_key for s in ("/", "\\", "..")):
        raise BridgeError(f"strategy_key 非法: {strategy_key!r}")
    overlay = job / "overlay"
    overlay.mkdir(parents=True, exist_ok=True)
    (overlay / f"{strategy_key}.py").write_text(strategy_py, encoding="utf-8")
    (overlay / f"{strategy_key}.yaml").write_text(
        yaml.safe_dump(strategy_yaml, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return strategy_key


def _sha16(b: bytes) -> str:
    """sha256 前 16 位（证据集统一短摘要口径）。"""
    return hashlib.sha256(b).hexdigest()[:16]


def _overlay_digest(overlay: Path) -> tuple:
    """overlay 文件清单 {相对名: sha256[:16]} 与总 digest（可复测的最小证据集，改进 B）。

    ⑧（P1）：**排除点目录/点文件**——proposal apply 的 .history 备份随
    `--strategy-dir` 落在 overlay 内（proposal.py L375 `hist = strategy_dir/.history`），
    rglob 全收会让每个 proposal 档作业的 digest 都被时间戳备份文件污染（系统性、
    非条件性），可复现证据集失真。编辑器/工具产生的点目录一律不进证据。
    """
    files = {str(p.relative_to(overlay)): _sha16(p.read_bytes())
             for p in sorted(overlay.rglob("*"))
             if p.is_file()
             and not any(part.startswith(".") for part in p.relative_to(overlay).parts)}
    return files, _sha16(json.dumps(files, sort_keys=True).encode("utf-8"))


# ================================================================
# 子进程执行（原则①③；改进 C/H/I）
# ================================================================

def _kill_tree(proc: subprocess.Popen) -> None:
    """杀整棵进程树。

    2026-10-18 F26-D1：实现已抽至 `app/agent/exec/job_runner.kill_tree`（单一事实源，
    skill_run 路径同出口）。此处**只留委托 seam**——`tests/test_backtest_bridge.py`
    monkeypatch `B._kill_tree` 的接缝不变；删掉本函数会让那个替身静默失效。
    """
    kill_tree(proc)


def _spawn_and_wait(cmd: list, *, env: dict, timeout_s: int) -> tuple:
    """子进程执行 → (stdout, stderr)；非 0 退出抛 RuntimeError，超时杀树后抛 TimeoutExpired。

    auto/svc 两泳道共用的进程段统一接口；env 由调用方构造
    （auto 注入 QD_STRATEGY_YAML_OVERLAY_DIR，svc 零注入）。
    posix 子进程自成进程组长（start_new_session）：killpg 杀树才不会误伤桥自己；
    Windows 走 CREATE_NEW_PROCESS_GROUP + taskkill /T（批次B质量审查 I3）。
    """
    posix_kwargs = {"start_new_session": True} if os.name != "nt" else {}
    proc = Popen(cmd, cwd=str(_BACKEND_ROOT), env=env,
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 text=True, encoding="utf-8", errors="replace",
                 creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                 **posix_kwargs)
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        out, err = proc.communicate()
        raise subprocess.TimeoutExpired(
            cmd=cmd, timeout=timeout_s, stderr=(err or out or "")[-2000:])
    if proc.returncode != 0:
        raise RuntimeError(f"子进程退出码 {proc.returncode}，尾部输出:\n"
                           f"{(err or out or '')[-2000:]}")
    return out, err


def _run_backtest_cli(job: Path, *, key: str, days: int, codes: Optional[list],
                      exec_engine: str, timeout_s: int) -> dict:
    """子进程跑官方 CLI。--out 落盘全量报告（stdout 只有退出路径行），桥只解析 report.json。"""
    cmd = [sys.executable, "-m", CLI_MODULE,
           "--strategy", key, "--days", str(days),
           "--exec-engine", exec_engine,          # 一期钉死 daily（改进 F 由调用方保证）
           "--progress-every", "0",
           "--out", str(job / "report.json")]
    if codes:
        cmd += ["--codes", ",".join(codes)]
    env = {**os.environ, ENV_OVERLAY_DIR: str(job / "overlay")}
    _spawn_and_wait(cmd, env=env, timeout_s=timeout_s)
    report_path = job / "report.json"
    if not report_path.is_file():
        raise RuntimeError("backtest_cli 退出码 0 但 report.json 未生成")
    return json.loads(report_path.read_text(encoding="utf-8"))


# ================================================================
# 主入口 / 审计 / GC
# ================================================================

def run_backtest_job(*, carrier: dict, scope: str = "codes",
                     codes: Optional[list] = None, days: int = 120,
                     strategy_key: str = "", exec_engine: str = "daily",
                     session_id: str = "",
                     timeout_s: Optional[int] = None) -> dict:
    """同步执行一次桥作业，返回 summary dict（JSON 安全）。

    载体：{"kind": "proposal", "proposal": {...}} | {"kind": "new", "py": "...", "yaml": {...}}
          | {"kind": "existing", "key": "<生产key>"}（对照组：零改动回测现有策略）。
    调用方错误（预算/载体/并发）抛 BridgeError；执行失败不抛 —— ok=False + error
    （诚实失败语义：失败必须可见可追溯）。
    """
    # ---- Phase 1: 调用方输入形状校验（不落盘，错误直接抛）----
    kind = str((carrier or {}).get("kind") or "")
    if kind not in ("proposal", "new", "existing"):
        raise BridgeError(f"carrier.kind 必须是 proposal|new|existing: {kind!r}")
    if scope not in ("codes", "all"):
        raise BridgeError(f"scope 必须是 codes|all: {scope!r}")
    if kind == "new":
        if not strategy_key:
            raise BridgeError("kind=new 必须传 strategy_key（agentbt_ 前缀）")
        if any(s in strategy_key for s in ("/", "\\", "..")):
            raise BridgeError(f"strategy_key 非法: {strategy_key!r}")
        if not strategy_key.startswith("agentbt_"):
            raise BridgeError(f"新策略 key 必须 agentbt_ 前缀（防撞生产 key）: {strategy_key!r}")
        if not isinstance(carrier.get("py"), str) or not carrier["py"]:
            raise BridgeError("kind=new 必须传非空 py 源码字符串（carrier['py']）")
        if not isinstance(carrier.get("yaml"), dict):
            raise BridgeError("kind=new 必须传 yaml dict（carrier['yaml']）")
    if kind == "proposal":
        pkey = str((carrier.get("proposal") or {}).get("strategy") or "")
        if not pkey or any(s in pkey for s in ("/", "\\", "..")):
            raise BridgeError(f"proposal.strategy 非法: {pkey!r}")
    if kind == "existing":
        # C1 修正（2026-10-15 批次B质量审查）：key 的单一事实源是 carrier["key"]，
        # strategy_key 参数仅作回退——此前误用参数默认 ""，existing 作业生产必败。
        ekey = str(carrier.get("key") or strategy_key or "")
        if not ekey or any(s in ekey for s in ("/", "\\", "..")):
            raise BridgeError(f"existing.key 非法（非空且不含路径字符）: {ekey!r}")
        strategy_key = ekey
    codes = [str(c) for c in (codes or [])]
    if scope == "codes" and not codes:
        # I1：空 codes 会让 CLI 静默跑全池 = 绕过 MAX_CODES 预算；全盘必须显式 scope=all
        raise BridgeError("scope=codes 必须给非空 codes；全盘量级请走 scope=all（技能路径）")
    days = _check_budget(_to_positive_int(days, "days"),
                         0 if scope == "all" else len(codes))
    timeout_s = (TIMEOUT_S if timeout_s is None
                 else _to_positive_int(timeout_s, "timeout_s"))
    running = _running_jobs()
    if len(running) >= MAX_CONCURRENT:
        raise BridgeBusy(f"已有 {len(running)} 个作业在跑 "
                         f"≥ QD_BT_MAX_CONCURRENT={MAX_CONCURRENT}: {running}")

    # ---- Phase 2: 作业目录 + manifest（此后失败一律诚实落盘，不抛）----
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    job_id = f"{time.strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}"
    job = _bridge_root() / "jobs" / job_id
    _assert_guards(job)                    # 先守卫后落盘：误配时不得在策略树内留作业目录
    (job / "overlay").mkdir(parents=True, exist_ok=True)
    manifest = {"job_id": job_id, "ts": ts, "state": "running", "lane": "auto",
                "carrier": kind, "scope": scope, "days": days,
                "codes_n": len(codes), "codes": codes,
                "exec_engine": exec_engine, "strategy_key": strategy_key,
                "session_id": session_id, "pid": os.getpid(),
                "timeout_s": timeout_s,
                "budget": {"max_days": MAX_DAYS, "max_codes": MAX_CODES,
                           "max_concurrent": MAX_CONCURRENT}}
    _write_manifest(job, manifest)
    try:
        if kind == "proposal":
            key = _materialize_proposal(job, carrier["proposal"], timeout_s=timeout_s)
        elif kind == "new":
            key = _materialize_new(job, strategy_key, carrier["py"], carrier["yaml"])
        else:
            live_yaml = _live_yaml_path(strategy_key)
            if not live_yaml.is_file():
                raise BridgeError(f"策略 {strategy_key!r} 的活 yaml 不存在")
            key = strategy_key
            # ⑧（P1）：existing 档 overlay 恒空（活 yaml 不落 overlay，活树全程只读）
            # ⇒ overlay_digest 对它是 sha256("{}") 常数、无证据力。活 yaml 内容 sha
            # 直接入 manifest = 不落 overlay 也有物证（改进 B 最小证据集对齐双档）。
            manifest["live_yaml_digest"] = _sha16(live_yaml.read_bytes())
        files, digest = _overlay_digest(job / "overlay")
        manifest.update(overlay_files=files, overlay_digest=digest, strategy_key=key)
        _write_manifest(job, manifest)

        report = _run_backtest_cli(job, key=key, days=days,
                                   codes=codes if scope == "codes" else None,
                                   exec_engine=exec_engine, timeout_s=timeout_s)
        meta = report.get("meta") or {}
        summary = {"ok": True, "job_id": job_id, "ts": ts, "session_id": session_id,
                   "strategy_key": key, "carrier": kind, "scope": scope,
                   "days": days, "codes_n": len(codes), "exec_engine": exec_engine,
                   "stats": report.get("stats"),          # 口径单源，桥不重算（改进 A）
                   "codes_ok": report.get("codes_ok"),
                   "elapsed_seconds": report.get("elapsed_seconds"),
                   "overlay_digest": digest,
                   "git_sha": meta.get("git_sha"),
                   "effective_params": meta.get("effective_params"),
                   "report_path": str(job / "report.json"),
                   "summary_path": str(job / "summary.json")}
        state, error_tail = "done", ""
    except subprocess.TimeoutExpired as e:
        state, summary, error_tail = "timeout", None, (
            f"回测超时 >{timeout_s}s（进程树已杀）"
            f"{(getattr(e, 'stderr', '') or '')[-2000:]}")
    except Exception as e:                                 # noqa: BLE001
        state, summary, error_tail = "failed", None, f"{type(e).__name__}: {e}"

    if state != "done":
        summary = {"ok": False, "job_id": job_id, "ts": ts, "session_id": session_id,
                   "strategy_key": strategy_key, "carrier": kind, "scope": scope,
                   "days": days, "codes_n": len(codes), "exec_engine": exec_engine,
                   "stats": None, "error": error_tail,
                   "summary_path": str(job / "summary.json")}
    try:
        manifest["state"] = state
        if state != "done":
            manifest["error"] = error_tail
        _write_manifest(job, manifest)
        (job / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        _append_jsonl(summary)
    except OSError as e:
        # ①（P1）：终态落盘失败（磁盘满/权限/路径过长）不逃逸——降级诚实 failed
        # 摘要返回（ok=False+error；stats 保留为已算出的事实，不粉饰）。manifest
        # 可能停留 running：由 _manifest_is_stale（ts+timeout_s+60s）判死兜底，
        # 不存在静默——返回值自带 error。
        summary = {**summary, "ok": False,
                   "error": f"终态落盘失败: {type(e).__name__}: {e}"}
    return summary


def _write_manifest(job: Path, manifest: dict) -> None:
    (job / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")


def _append_jsonl(summary: dict) -> None:
    """§5.7 一行审计（runs/<date>_<job>.jsonl）；失败作业 stats=None + error 字段（改进 H）。

    ⑤（P1）：**按 lane 取字段**——svc 行的标识字段（symbol/market/timeframe/
    cost_model/窗口/summary_metrics/snapshot_digest）此前被整体丢弃，审计行连
    「回测了哪只票、什么窗口」都答不出，取证必须 join manifest 才完整。
    auto 行不加这些键（schema 零漂移，既有审计消费方解析不中断）。
    """
    runs = _bridge_root() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    date_tag = (summary.get("ts") or time.strftime("%Y%m%d"))[:10].replace("-", "")
    # lane：双泳道共享 runs/ 审计（auto summary 无 lane 键 → 缺省 "auto"，零改动回传）
    row = {"lane": summary.get("lane", "auto"),
           "ts": summary.get("ts"), "session_id": summary.get("session_id", ""),
           "job_id": summary.get("job_id"), "strategy_key": summary.get("strategy_key"),
           "carrier": summary.get("carrier"), "overlay_digest": summary.get("overlay_digest"),
           "scope": summary.get("scope"), "days": summary.get("days"),
           "stats": summary.get("stats"),
           "meta": {"git_sha": summary.get("git_sha"),
                    "effective_params": summary.get("effective_params")},
           "pid": os.getpid(),
           "conclusion_ref": summary.get("session_id", ""),   # 改进 G：不记 claim，只留关联
           "error": summary.get("error")}
    if row["lane"] == "svc":
        row.update({"symbol": summary.get("symbol"),
                    "market": summary.get("market"),
                    "timeframe": summary.get("timeframe"),
                    "cost_model": summary.get("cost_model"),
                    "start_date": summary.get("start_date"),
                    "end_date": summary.get("end_date"),
                    "summary_metrics": summary.get("summary_metrics"),
                    "snapshot_digest": summary.get("snapshot_digest")})
    path = runs / f"{date_tag}_{summary['job_id']}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def gc_jobs(now: Optional[float] = None) -> list:
    """TTL 归档：超龄作业目录**移动**到 _del/（项目规则：删除先移 del，不直接删）。

    running 态超龄作业先标 failed —— 进程多半已死，悬挂状态不许留（可观测性）。

    二段回收（2026-10-15 P2-⑭，审计发现：_del/ 与 runs/ 从不清理 = 无限增长）：
      · jobs/ 超龄 → 移 _del/（JOB_TTL_DAYS，一段，只搬不删）；
      · _del/ 归档条目按 DEL_TTL_DAYS 真删（二段；TTL 从**归档时刻**起算——
        move 后打 mtime=now，若沿用作业创建时的旧 mtime，刚归档就会被下次 GC
        当超龄立即误删）；
      · runs/*.jsonl 审计行按 RUNS_TTL_DAYS 真删（一行一封、作业终态即写毕，
        mtime 可信）。两个 TTL 均 env 可调；单条目失败不中断整扫（下轮 GC 重试）。

    ⚠ 前提条件（⑦，2026-10-16 P1 文档化）：**QD_BT_TIMEOUT 必须 <
    QD_BT_JOB_TTL_DAYS × 86400**。GC 判据是 mtime+JOB_TTL_DAYS，与判死判据
    （_manifest_is_stale：ts+timeout_s+60s）是两套时钟：若超时配得比作业
    TTL 还长，一个**合法在跑**的长作业会先被 GC 当僵尸标 failed 并归档，而它
    自己的 timeout_s 还没到（子进程随后写终态会落进 _del/ 目录）。默认
    TIMEOUT_S=900s ≪ 7d 安全；两套判据的统一属结构性重构，列二期
    （backtest_bridge_plan.md §0.3），此处只钉前提。
    """
    now = now or time.time()
    cutoff = now - JOB_TTL_DAYS * 86400
    moved = []
    del_root = _bridge_root() / "_del"
    for d in sorted(_bridge_root().joinpath("jobs").glob("*")):
        if not d.is_dir() or d.stat().st_mtime > cutoff:
            continue
        try:
            mp = d / "manifest.json"
            if mp.is_file():
                try:
                    m = json.loads(mp.read_text(encoding="utf-8"))
                    if m.get("state") == "running":
                        m["state"] = "failed"
                        m["error"] = "gc: TTL 到期仍 running，标记 failed 后归档"
                        mp.write_text(json.dumps(m, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
                except Exception:
                    pass
            del_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(del_root / d.name))
            os.utime(del_root / d.name, None)   # TTL 从归档时刻起算（见 docstring）
        except Exception:
            continue          # 单个作业 move 失败（占用/权限）不中断整扫，下轮 GC 重试
        moved.append(d.name)
    _gc_runs(now)
    _gc_del(del_root, now)
    return moved


def _gc_runs(now: float) -> list:
    """runs/ 审计 JSONL 的 TTL 回收（P2-⑭ 二段）。返回被删文件名列表。"""
    removed = []
    runs = _bridge_root() / "runs"
    if not runs.is_dir():
        return removed
    cutoff = now - RUNS_TTL_DAYS * 86400
    for f in sorted(runs.glob("*.jsonl")):
        try:
            if f.stat().st_mtime > cutoff:
                continue
            f.unlink()
        except OSError:
            continue          # 占用/权限：不中断整扫，下轮 GC 重试
        removed.append(f.name)
    if removed:
        logger.info("[backtest-bridge] GC 清理 runs/ 审计行 %d 个（TTL %d 天）",
                    len(removed), RUNS_TTL_DAYS)
    return removed


def _gc_del(del_root: Path, now: float) -> list:
    """_del/ 归档区的二段回收（P2-⑭）：条目 mtime（= 归档时刻，move 时已打）超
    DEL_TTL_DAYS 才真删。为什么不在 jobs 循环里顺手删：_del/ 是「删除先移 del」
    的归档段（项目规则），真删必须晚一步且从归档时刻起算。返回被删条目名列表。"""
    removed = []
    if not del_root.is_dir():
        return removed
    cutoff = now - DEL_TTL_DAYS * 86400
    for p in sorted(del_root.glob("*")):
        try:
            if p.stat().st_mtime > cutoff:
                continue
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        except OSError:
            continue
        removed.append(p.name)
    if removed:
        logger.info("[backtest-bridge] GC 清理 _del/ 归档 %d 项（TTL %d 天）",
                    len(removed), DEL_TTL_DAYS)
    return removed


# ================================================================
# svc 泳道（Task 8，改进 J/K/L/M）：指标/脚本策略链桥
# ================================================================

SVC_CLI_MODULE = "app.agent.scripts.backtest_svc_cli"


def _svc_markets() -> frozenset:
    """market 枚举（改进 M）：与数据源工厂注册表对齐，不硬编码第二份清单。

    单一事实源 = app/data_sources/factory.py::SUPPORTED_MARKETS（批次E质量审查
    Minor 4 根修：公开常量，取代此前跨模块读私有 _MARKET_ALIASES）。工厂新增
    市场自动跟进。工厂 import 放函数内（惰性，桥模块 import 零副作用，
    与 _assert_guards 同风格）。
    """
    from app.data_sources.factory import SUPPORTED_MARKETS
    return SUPPORTED_MARKETS


def _check_window(start_dt: datetime, end_dt: datetime) -> None:
    """svc 泳道窗口钳制。不复制 backtest_tools 的 tf_limits（那是既有工具面的语义）；
    svc lane 预算统一走 QD_BT_MAX_DAYS。"""
    if end_dt <= start_dt:
        raise BridgeError("end_date 必须晚于 start_date")
    if (end_dt - start_dt).days > MAX_DAYS:
        raise BridgeError(f"回测窗口 {(end_dt - start_dt).days} 天超出预算 "
                          f"{MAX_DAYS}（QD_BT_MAX_DAYS）")


def run_svc_backtest_job(*, carrier: dict, symbol: str, start_date: str,
                         end_date: str, market: str = "CNStock",
                         timeframe: str = "1D", commission: Optional[float] = None,
                         slippage: Optional[float] = None, leverage: Optional[int] = None,
                         trade_direction: Optional[str] = None,
                         strategy_config: Optional[dict] = None,
                         initial_capital: Optional[float] = None,
                         user_id: int = 1, session_id: str = "",
                         timeout_s: Optional[int] = None) -> dict:
    """svc 泳道（指标/脚本策略链）：子进程 run_strategy_snapshot → 归一化标量摘要。

    载体：{"kind": "svc_code", "run_type": "strategy_indicator"|"strategy_script", "code": "..."}
          （agent 直想出的策略，直构 snapshot dict，无需 DB 注册——改进 J）
          | {"kind": "svc_strategy", "strategy_id": N}（对照组：DB 已注册策略，
          子进程内走 resolver，与既有工具同语义——F13）。
    执行假设（改进 M）：market/commission/slippage/leverage/trade_direction/strategy_config
      由调用方显式传入并透传进 snapshot——**None/缺省 = 引擎口径**（commission/slippage
      缺省 0 = 零费率毛收益，strategy_config 缺省无 ashare_rules = T+0，F15）；费率是业务
      决策，桥不硬编码"像样的默认值"，只在 cost_model 里诚实标注实际口径。
    initial_capital（⑰-svc，P1）：初始本金，仅 svc_code 直构 snapshot 生效
      （None = 不写键 = 引擎默认 10000；读侧 services/backtest.py L1582 现成）。
      svc_strategy 对照组传它 = BridgeError——resolver 只覆写 symbol/timeframe
      （backtest_svc_cli L72-76），本金由 DB 策略自有配置决定，桥不透写也不静默丢弃。
    摘要只取标量键（指标随引擎口径走，trades/equityCurve 留 report.json——改进 A/L）。
    问责边界：svc 结论按「探索性参考」表述进 answer（svc_code 是 agent 自由代码），
    不与 auto 泳道结论同权重进 T+N 问责闭环。
    调用方错误抛 BridgeError；执行失败诚实落盘不抛（ok=False + error，改进 H）。
    """
    # ---- Phase 1: 调用方校验（不落盘，错误直接抛）----
    kind = str((carrier or {}).get("kind") or "")
    if kind not in ("svc_code", "svc_strategy"):
        raise BridgeError(f"carrier.kind 必须是 svc_code|svc_strategy: {kind!r}")
    symbol = str(symbol or "").strip()
    if not symbol:
        raise BridgeError("symbol 必填（如 600519 或 BTC/USDT）")
    markets = _svc_markets()
    market = str(market or "").strip()
    if market not in markets:
        raise BridgeError(f"market 必须是 {sorted(markets)} 之一: {market!r}（改进 M）")
    # 数值归一（批次E质量审查 I1）：Phase 1 的强转失败一律 BridgeError，
    # 不许裸 ValueError 逃逸（docstring 契约「调用方错误抛 BridgeError」）。
    try:
        commission = None if commission is None else float(commission)
    except (TypeError, ValueError):
        raise BridgeError(f"commission 必须是数字: {commission!r}") from None
    try:
        slippage = None if slippage is None else float(slippage)
    except (TypeError, ValueError):
        raise BridgeError(f"slippage 必须是数字: {slippage!r}") from None
    if commission is not None and commission < 0:
        raise BridgeError(f"commission 不能为负: {commission!r}")
    if slippage is not None and slippage < 0:
        raise BridgeError(f"slippage 不能为负: {slippage!r}")
    if leverage is not None:
        try:
            leverage = int(leverage)
        except (TypeError, ValueError):
            raise BridgeError(f"leverage 必须是整数: {leverage!r}") from None
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        raise BridgeError(f"user_id 必须是整数: {user_id!r}") from None
    if strategy_config is not None and not isinstance(strategy_config, dict):
        raise BridgeError(f"strategy_config 必须是 dict: {type(strategy_config).__name__}")
    # ⑰-svc（P1）：initial_capital 归一（None=不写键=引擎默认）；svc_strategy 载体
    # 的 resolver 不透写本金（CLI override 只含 symbol/timeframe）⇒ 诚实拒绝。
    if initial_capital is not None:
        initial_capital = _to_positive_float(initial_capital, "initial_capital")
        if kind == "svc_strategy":
            raise BridgeError("initial_capital 仅 svc_code 载体可用：svc_strategy "
                              "对照组的本金由 DB 策略配置决定（resolver 不透写）")
    try:
        start_dt = datetime.strptime(str(start_date), "%Y-%m-%d")
        end_dt = datetime.strptime(str(end_date), "%Y-%m-%d")
    except ValueError as e:
        raise BridgeError(f"日期格式错误，请使用 YYYY-MM-DD: {e}") from e
    _check_window(start_dt, end_dt)
    if kind == "svc_code":
        code = str(carrier.get("code") or "").strip()
        run_type = str(carrier.get("run_type") or "strategy_indicator")
        if run_type not in ("strategy_indicator", "strategy_script"):
            raise BridgeError(f"run_type 必须是 strategy_indicator|strategy_script: {run_type!r}")
        if not code:
            raise BridgeError("svc_code 载体必须提供 code")
        snap = {"code": code, "run_type": run_type, "market": market,
                "symbol": symbol, "timeframe": timeframe, "user_id": int(user_id)}
        if commission is not None:
            snap["commission"] = float(commission)
        if slippage is not None:
            snap["slippage"] = float(slippage)
        if leverage is not None:
            snap["leverage"] = int(leverage)
        if trade_direction:
            snap["trade_direction"] = str(trade_direction)
        if strategy_config:
            snap["strategy_config"] = strategy_config
        if initial_capital is not None:
            snap["initial_capital"] = initial_capital   # ⑰-svc：读侧现成（L1582）
        spec = {"kind": "svc_code", "snapshot": snap}
    else:
        strategy_id = carrier.get("strategy_id")
        if not isinstance(strategy_id, int) or isinstance(strategy_id, bool):
            raise BridgeError("svc_strategy 载体必须提供整数 strategy_id")
        spec = {"kind": "svc_strategy", "strategy_id": strategy_id,
                "symbol": symbol, "timeframe": timeframe, "user_id": int(user_id)}
    # cost_model：口径诚实标注（改进 M）。svc_code = 桥侧实传值；对照组口径在子进程
    # resolver 后的 snapshot 里，桥侧只声明来源，不猜值。
    cost_model = {"market": market, "commission": commission, "slippage": slippage,
                  "trade_direction": str(trade_direction or "long"),
                  "strategy_config_provided": bool(strategy_config),
                  "source": "bridge_params" if kind == "svc_code" else "resolver_snapshot"}
    # I1：与 auto 泳道同款——0/负数/非数字串经 _to_positive_int 归一为 BridgeError
    timeout_s = (TIMEOUT_S if timeout_s is None
                 else _to_positive_int(timeout_s, "timeout_s"))
    running = _running_jobs()
    if len(running) >= MAX_CONCURRENT:
        raise BridgeBusy(f"已有 {len(running)} 个作业在跑 "
                         f"≥ QD_BT_MAX_CONCURRENT={MAX_CONCURRENT}: {running}")

    # ---- Phase 2: 作业目录 + snapshot.json（此后失败一律诚实落盘，不抛）----
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    job_id = f"{time.strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}"
    job = _bridge_root() / "jobs" / job_id
    _assert_guards(job)                    # 先守卫后落盘（批次E Minor 2：与 auto 泳道对称，
                                           # 写域白名单原则对 svc 同样成立）
    job.mkdir(parents=True, exist_ok=True)
    (job / "snapshot.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    snapshot_digest = hashlib.sha256(
        (job / "snapshot.json").read_bytes()).hexdigest()[:16]     # 改进 K：可复测证据
    manifest = {"job_id": job_id, "ts": ts, "state": "running", "lane": "svc",
                "carrier": kind, "symbol": symbol, "market": market,
                "timeframe": timeframe, "cost_model": cost_model,
                "start_date": str(start_dt.date()), "end_date": str(end_dt.date()),
                "snapshot_digest": snapshot_digest, "session_id": session_id,
                "pid": os.getpid(), "timeout_s": timeout_s,
                "budget": {"max_days": MAX_DAYS, "max_concurrent": MAX_CONCURRENT}}
    _write_manifest(job, manifest)
    try:
        report = _run_svc_cli(job, start_date=str(start_dt.date()),
                              end_date=str(end_dt.date()), timeout_s=timeout_s)
        scalars = {k: v for k, v in report.items()
                   if isinstance(v, (int, float, str, bool))}      # 零维护口径：标量即摘要
        summary = {"ok": True, "lane": "svc", "job_id": job_id, "ts": ts,
                   "session_id": session_id, "carrier": kind, "symbol": symbol,
                   "market": market, "timeframe": timeframe, "cost_model": cost_model,
                   "start_date": str(start_dt.date()), "end_date": str(end_dt.date()),
                   "summary_metrics": scalars,
                   "snapshot_digest": snapshot_digest,
                   "report_path": str(job / "report.json"),
                   "summary_path": str(job / "summary.json")}
        state, error_tail = "done", ""
    except subprocess.TimeoutExpired as e:
        state, summary, error_tail = "timeout", None, \
            f"回测超时 >{timeout_s}s（进程树已杀）{(getattr(e, 'stderr', '') or '')[-2000:]}"
    except Exception as e:                                 # noqa: BLE001
        state, summary, error_tail = "failed", None, f"{type(e).__name__}: {e}"

    if state != "done":
        summary = {"ok": False, "lane": "svc", "job_id": job_id, "ts": ts,
                   "session_id": session_id, "carrier": kind, "symbol": symbol,
                   "market": market, "timeframe": timeframe, "cost_model": cost_model,
                   "summary_metrics": None,
                   "error": error_tail, "summary_path": str(job / "summary.json")}
    try:
        manifest["state"] = state
        if state != "done":
            manifest["error"] = error_tail
        _write_manifest(job, manifest)
        (job / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        _append_jsonl(summary)
    except OSError as e:
        # ①（P1）：同 auto 泳道——终态落盘失败降级诚实 failed 摘要返回，
        # summary_metrics 保留为已算出的事实；manifest 停留 running 由判死兜底。
        summary = {**summary, "ok": False,
                   "error": f"终态落盘失败: {type(e).__name__}: {e}"}
    return summary


def _run_svc_cli(job: Path, *, start_date: str, end_date: str, timeout_s: int) -> dict:
    """子进程跑 svc CLI。零 env 注入（不需要 overlay 钩子）；--out 落全量 report.json。"""
    cmd = [sys.executable, "-m", SVC_CLI_MODULE,
           "--snapshot-file", str(job / "snapshot.json"),
           "--start-date", start_date, "--end-date", end_date,
           "--out", str(job / "report.json")]
    _spawn_and_wait(cmd, env={**os.environ}, timeout_s=timeout_s)
    report_path = job / "report.json"
    if not report_path.is_file():
        raise RuntimeError("backtest_svc_cli 退出码 0 但 report.json 未生成")
    return json.loads(report_path.read_text(encoding="utf-8"))
