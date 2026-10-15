# agent × auto 回测桥 实施方案（backtest_bridge_plan.md）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通 agent → auto 折叠回测的数据流：agent 想出的策略假设（proposal 改参/加门，或全新 .py+.yaml）可直接送 `run_all` 官方口径回测，结果摘要回传，全程零污染生产链路。

**Architecture:** 独立桥（`chain/backtest_bridge.py`）= 作业目录物化（overlay）+ 子进程执行（`backtest_cli`）+ 归一化摘要。auto 侧只加 **env 默认关闭的读钩子**（生产进程行为逐字节不变）；agent 侧新增 1 桥模块 + 1 工具 + 1 技能，不动现有两条回测链。设计态全文见同目录 `AGENT_DESIGN_v3.md` §5.7（用户裁定三原则 ①独立桥 ②直传策略 ③不污染）。

**Tech Stack:** Python 3.12 / subprocess（子进程隔离）/ PyYAML / pytest。测试全部不依赖 DB 与外部服务（桥与钩子均为纯文件/进程逻辑）。

> ⚠ **本项目红线（覆盖技能模板默认流程）**
> 1. **禁止执行任何 git 指令**——本计划的「提交点」由**用户手工提交**，agent 只改工作区文件。
> 2. 共享文档（`AGENT_DESIGN_v3.md`）只**增量追加**，不覆盖他人内容；追加必须署名。
> 3. 临时脚本/调试产物一律放 `tmp/` 并署名；确需删除文件先移动到 `del/`（桥的 GC 同此约定 → `_del/`）。
> 4. pytest 现网基线存在历史失败（约 30F/44E，环境性），**验收标准 = 本计划新增测试文件逐个独立全绿**（`python -m pytest tests/<file> -v`），不与基线混跑判成败。
> 5. 测试解释器用 `d:\QuantDinger\.venv\Scripts\python.exe`，工作目录 `backend_api_python`（PowerShell 用 `;` 分隔，不用 `&&`）。
> 6. 方案落盘位置按 §5.7 指定（同目录 `backtest_bridge_plan.md`），覆盖 superpowers 默认的 `docs/superpowers/plans/`。

---

## 0 数据流缺口分析（2026-10-15 施工前终审，署名：Trae agent / backtest_bridge_plan）

### 0.1 已对账的代码事实（设计假设 → 证据锚点）

| # | 事实 | 锚点 |
|---|---|---|
| F1 | CLI 独立可跑：`python -m app.market_cn.auto.backtest_cli --strategy <key> --days N [--codes ...] [--exec-engine daily] [--out report.json]`；`--out` 落全量 JSON 报告（trades+daily_activity+stats），`--json` 才炸 stdout；`--progress-every 0` 可关进度噪声；失败走 `parser.error` = exit 2 + stderr 文本 | `backtest_cli.py:209-250` |
| F2 | CLI 自插 sys.path（parents[3]）但 **cwd 必须是 backend 根**（`load_env_first_found(cwd/.env)` + 模块解析） | `backtest_cli.py:24-26,113` |
| F3 | `run_all` 主路**零 DB 写、零 is_enabled 门控**：`fetch_klines_batch`（回落 `hub.daily`）→ `strat.backtest_stock`；返回 `{trades, stats, codes_ok, elapsed, meta{git_sha, effective_params, ...}}` | `core/backtest.py:58-143` |
| F4 | yaml 消费钩子点：`strategies/_yaml_doc`（mtime 缓存，`_YAML_DOC_CACHE` 以 **key** 为键）、`evaluate.load_strategy`（`_STRATEGY_DIR` 直拼路径）、`autodiscover`（**只扫包目录**，pkgutil） | `strategies/__init__.py:53-106`、`core/runtime/evaluate.py:151-153` |
| F5 | proposal CLI：`validate --proposal P [--strategy-dir D]`（退出码=失败项数）、`apply --proposal P [--apply] [--strategy-dir D]`（默认 dry-run diff；**`.history` 备份落在 `<strategy-dir>/.history/`**）；红线①路径白名单 ②选择集须附 stability+pool_check 证据 ③不删 qualify ④as-of 静态检查 ⑤before 值一致性 | `auto/tools/proposal.py:26-48` |
| F6 | run_all 单次只跑一个策略；`strat.params` 单一事实源 = yaml `params` 段（`params_override` → `_yaml_doc`） | `strategies/__init__.py:194-208` |
| F7 | 技能白名单：`scripts/skill_run.py::EXECUTABLE_SKILLS` = `{name: (模块路径, (fn,), 默认fn)}`，模块路径相对 `app/agent`；产物落 `backend_api_python/tmp/skill_output/`，预览上限 20000 字符 | `skill_run.py:28-43` |
| F8 | 工具自动注册：`tools/finance/` 下非 `_` 前缀 .py 自动成为 finance 域工具，无需中心清单；`WRITE_PREFIXES`（含 `run_`/`apply_`）只约束能力层扫描 | `tools/_provider.py:38-49,170-195`、`capabilities/scanner.py:77-86` |
| F9 | **SCAN_TARGETS 不含 `app.market_cn.auto.tools`** ⇒ `pool_check` 等证据函数不在 agent 能力面（红线②证据从哪来 = 真缺口，见 G-9） | `capabilities/scanner.py:52-74` |
| F10 | claim 入口 `chain/intake.record_decision` 由主请求 finalize 流程调用（过闸门才入库）；`StrategyBase` 是 dataclass（`key`/`scan_spec` 有默认值，最小子类可直接实例化） | `chain/intake.py:43-96`、`strategies/base.py:165-193` |

### 0.2 §5.7 修正稿之外的改进点（A~I，各标注原则归属）

| # | 缺口 | 改进 | 原则 |
|---|---|---|---|
| A | 结果回流无护栏：5k 票 report 含全量 trades（`--json` 会让 stdout 爆炸） | 桥固定 `--out <job>/report.json` + `--progress-every 0`；摘要只搬 `stats`（**桥不重算指标**，口径单源 `core.backtest._summary`，防两套口径）；逐笔明细留文件，agent 拿 `report_path` 自取（§5.3「明细留在文件里」） | ③ |
| B | 作业无凭证：§5.7 只有 `overlay_digest` 概念，无可复测的最小证据集 | 作业目录首物 `manifest.json`：job_id/ts/state/carrier/scope/days/codes/exec_engine/budget/`overlay_files{name:sha256[:16]}`/pid/session_id；`overlay_digest` = 文件清单的 sha（可复测 = 同 manifest 重放同 CLI） | ③ |
| C | 生命周期缺失：无 state、无 TTL、无并发约束执行者、Windows `Popen.kill` 不杀孙进程（F3 的 git 子进程/行情线程残留） | manifest `state`（running→done/failed/timeout）；`QD_BT_MAX_CONCURRENT`（默认 1）以 running 计数执行；`QD_BT_JOB_TTL_DAYS`（默认 7）GC **移动**到 `_del/`；超时 `taskkill /T /F` 杀整树 | ①③ |
| D | **钩子点列漏一个**：§5.7 只列 `_paths/evaluate/_yaml_doc` 三处，但③档新策略 .py 在作业目录，`autodiscover` 只扫包目录 ⇒ `get_strategy` 解析不到（F4） | 补第四钩子：`autodiscover()` 尾部扫 overlay 目录 `*.py`，`importlib` 按路径加载（模块名 `qd_overlay_<stem>` 防撞 sys.modules）；AST 静态预检 key 必须 `agentbt_` 前缀 | ①③ |
| E | `--apply` 禁令过粗：桥若自研「proposal JSON → yaml 编辑」就是第二份编辑逻辑（与 proposal 的文本级保注释引擎漂移） | **精确化**：禁止的是对**活树** `--apply`；`apply --strategy-dir <作业overlay> --apply` 是唯一物化通道（`.history` 备份随 `--strategy-dir` 落在作业目录内，F5），活 yaml 从未改过，DRY 复用 proposal 引擎 | ①③ |
| F | `exec_engine=intraday/auto` 走 1m 精修路径，其取数缓存行为未验证（§5.7 隔离清单也只对日线主路下结论） | 一期**钉死 `exec_engine="daily"`**（桥参数保留、工具不暴露）；intraday 精修列二期，验证 1m 取数只读后再放开 | ③ |
| G | 「可选记 claim」若在桥内手工调 `record_decision` = 绕闸门 + 双重入库（F10：主流程 finalize 已自动过闸抽 claim） | **桥不记 claim**：agent 把回测结论写进 answer，§5.1 intake 主流程自然接住；`conclusion_ref` 字段暂存 session_id | ③ |
| H | 失败语义：CLI 失败 = exit 2 + stderr 文本，吞掉 = 复现不了的「回测没跑出来」 | 失败诚实落盘：summary.ok=False + `error`/`stderr_tail`（尾部 2000 字符）进 summary.json 与 JSONL；state=failed/timeout 区分 | ③ |
| I | env/cwd 契约未落字：`cwd` 错 = .env 读不到 + 模块解析错（F2，误导性报错史） | 桥固定 `cwd=_BACKEND_ROOT`、`sys.executable -m`、env 透传 + 注入 `QD_STRATEGY_YAML_OVERLAY_DIR`（**仅子进程**有此 env，生产进程永不设 = 行为逐字节不变） | ①③ |

### 0.3 二期积压（本方案不做，明确记录）

- `admission.json` 增补 `auto/tools` 只读证据函数（`pool_check` 等，F9）——实施时先跑 scanner 盘点验证 import 无副作用，再人工准入；一期红线②证据缺失时由 `proposal.validate` 诚实拒绝（agent 依据失败原因自行取证后重提）。
- `exec_engine=intraday/auto` 放开（F 的验证后）。
- 技能产物接入 §5.4 exp_grind 式批量假设刷选（同一桥，多 job 排队）。

---

## 1 文件结构（谁建谁改）

```
新建 app/agent/chain/backtest_bridge.py        # 桥核心：作业目录/物化/子进程/归一化/JSONL/GC
新建 app/agent/tools/finance/auto_bt_tools.py  # 个股/小集合工具（不塞 backtest_tools.py）
新建 app/agent/skills/auto-backtest/SKILL.md   # 全盘量级技能（§5.3 子进程+产物落盘）
新建 app/agent/skills/auto-backtest/run.py     # 技能入口（白名单 fn=run）
改   app/agent/scripts/skill_run.py            # EXECUTABLE_SKILLS +1 行
改   app/market_cn/auto/strategies/__init__.py # overlay 读钩子 ×2（_yaml_doc / autodiscover）
改   app/market_cn/auto/core/runtime/evaluate.py # overlay 读钩子 ×1（load_strategy）
新建 tests/test_auto_overlay_hooks.py          # 任务 1-3 的测试
新建 tests/test_backtest_bridge.py             # 任务 4+7 的测试
新建 tests/test_auto_bt_tools.py               # 任务 5-6 的测试
```

分工边界：`chain/` 只放**编排与落盘**（无指标计算）；auto 侧钩子只改**路径解析**（不改判定）；写域白名单 = `data/backtest_bridge/**` + `tmp/skill_output/**`。

## 2 环境变量契约（全部可选；未设 = 现网行为逐字节不变）

| 变量 | 默认 | 语义 |
|---|---|---|
| `QD_STRATEGY_YAML_OVERLAY_DIR` | （不设） | **仅桥子进程**设置；三处读钩子的开关。生产进程永不设 |
| `QD_BT_MAX_DAYS` | 365 | days 上限（桥侧钳制，CLI 自身不设限） |
| `QD_BT_MAX_CODES` | 50 | scope=codes 的代码数上限；全盘走技能 scope=all 不受限 |
| `QD_BT_TIMEOUT` | 900 | 单作业墙钟超时（秒），超时杀进程树 |
| `QD_BT_MAX_CONCURRENT` | 1 | 桥作业并发上限（running manifest 计数） |
| `QD_BT_JOB_TTL_DAYS` | 7 | 作业目录 TTL，GC 移入 `_del/` |
| `QD_BT_ROOT` | `backend_api_python/data/backtest_bridge` | 桥产物根（测试/部署可改） |

## 3 任务分解（TDD；每个任务独立可验收）

### Task 1: auto overlay 钩子之一 —— `_yaml_doc` 按 overlay 优先

**Files:**
- Modify: `app/market_cn/auto/strategies/__init__.py`（`_yaml_doc` 区域，L78-106）
- Test: `tests/test_auto_overlay_hooks.py`

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""auto overlay 读钩子单测（§5.7 桥，2026-10-15）。

红线：env 未设 ⇒ 行为与今日逐字节相同（回归用例钉住）。
注意：_YAML_DOC_CACHE 是模块级缓存，fixture 每用例前后清空，防跨用例串缓存。
"""
import textwrap

import pytest

from app.market_cn.auto import strategies as S

_OVERLAY_ENV = "QD_STRATEGY_YAML_OVERLAY_DIR"


@pytest.fixture(autouse=True)
def _clean_yaml_cache():
    S._YAML_DOC_CACHE.clear()
    yield
    S._YAML_DOC_CACHE.clear()


@pytest.fixture(autouse=True)
def _registry_cleanup():
    """overlay 测试会往模块级 _REGISTRY 塞探测策略，用例后清掉防泄漏。"""
    added = set(S._REGISTRY)
    yield
    for k in set(S._REGISTRY) - added:
        S._REGISTRY.pop(k, None)


def test_yaml_doc_overlay_wins(tmp_path, monkeypatch):
    (tmp_path / "agentbt_t1.yaml").write_text(
        "meta: {enabled: false}\nparams: {marker: 42}\n", encoding="utf-8")
    monkeypatch.setenv(_OVERLAY_ENV, str(tmp_path))
    doc, mt = S._yaml_doc("agentbt_t1")
    assert doc["params"]["marker"] == 42
    assert mt is not None


def test_yaml_doc_env_unset_never_reads_overlay(tmp_path, monkeypatch):
    monkeypatch.delenv(_OVERLAY_ENV, raising=False)
    (tmp_path / "agentbt_t1.yaml").write_text("params: {marker: 42}\n", encoding="utf-8")
    assert S._yaml_doc("agentbt_t1") == (None, None)   # 只看活树（无此策略）


def test_yaml_doc_cache_keyed_by_path(tmp_path, monkeypatch):
    """同 key 先 overlay 后回退，缓存不得串槽（改进 D 的配套语义）。"""
    (tmp_path / "agentbt_t1.yaml").write_text("params: {marker: 1}\n", encoding="utf-8")
    monkeypatch.setenv(_OVERLAY_ENV, str(tmp_path))
    doc1, _ = S._yaml_doc("agentbt_t1")
    monkeypatch.delenv(_OVERLAY_ENV, raising=False)
    doc2, _ = S._yaml_doc("agentbt_t1")
    assert doc1["params"]["marker"] == 1
    assert doc2 is None
```

- [ ] **Step 2: 跑测试确认失败**

```
d:\QuantDinger\.venv\Scripts\python.exe -m pytest tests/test_auto_overlay_hooks.py -v
```
预期：`test_yaml_doc_overlay_wins` FAIL（`doc` 为 None —— overlay 未被读取）。

- [ ] **Step 3: 最小实现**

在 `strategies/__init__.py` 的 `_STRATEGY_DIR = ...` 行（L78）之后插入：

```python
#: 子进程 overlay 目录（§5.7 回测桥专用，2026-10-15）：env 仅由 chain/backtest_bridge
#: 派生的子进程设置；生产进程（scan/monitor/常规 run_all）永不设 ⇒ 下面的解析函数
#: 全部走原路径，行为与今日逐字节相同。
OVERLAY_DIR_ENV = "QD_STRATEGY_YAML_OVERLAY_DIR"


def _resolve_yaml_path(key):
    """解析 `<key>.yaml` 实际路径：overlay 目录优先（仅子进程），回退活策略树。

    易错点：返回值同时用作 _YAML_DOC_CACHE 的缓存键 —— 活树与 overlay 同 key 必须是
    两个缓存槽位，否则同进程先读 overlay 再回退活树会串缓存（测试
    test_yaml_doc_cache_keyed_by_path 钉住）。
    """
    overlay = os.environ.get(OVERLAY_DIR_ENV)
    if overlay:
        p = os.path.join(overlay, f"{key}.yaml")
        if os.path.isfile(p):
            return p
    return os.path.join(_STRATEGY_DIR, f"{key}.yaml")
```

把 `_yaml_doc` 改为按**路径**缓存（只动 4 行，注释同步更新）：

```python
def _yaml_doc(key):
    """读策略宏 `<key>.yaml` 的解析结果（mtime 缓存，缓存键=解析后路径）；无 yaml / 解析失败 → None。

    带 mtime 缓存: 改 yaml 后无需重启后端; 解析失败返回 None (交调用方回退)。
    §5.7 桥钩子: 子进程 env QD_STRATEGY_YAML_OVERLAY_DIR 指向作业目录时, overlay 的
    `<key>.yaml` 优先 —— 活策略树只读, 生产进程不设此 env, 行为不变。
    """
    path = _resolve_yaml_path(key)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None, None
    hit = _YAML_DOC_CACHE.get(path)
    if hit is not None and hit[0] == mt:
        return hit[1], mt
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        if not isinstance(doc, dict):
            doc = None
    except Exception as e:
        logger.error("[strategies] %s 解析失败, 回退 config: %s", path, e)
        doc = None
    _YAML_DOC_CACHE[path] = (mt, doc)
    return doc, mt
```

（同步把 L79 的缓存注释改为 `key -> (mtime, doc)` → `path -> (mtime, doc)`。）

- [ ] **Step 4: 跑测试确认通过**

```
d:\QuantDinger\.venv\Scripts\python.exe -m pytest tests/test_auto_overlay_hooks.py -v
```
预期：3 个用例 PASS。

- [ ] **Step 5: 回归证明（env 未设行为不变）**

```
d:\QuantDinger\.venv\Scripts\python.exe -m pytest tests/test_auto_overlay_hooks.py -v
```
用例 `test_yaml_doc_env_unset_never_reads_overlay` 即回归钉子；另跑一次语法自检：

```
d:\QuantDinger\.venv\Scripts\python.exe -m compileall -q app/market_cn/auto/strategies/__init__.py
```
预期：无输出（EXIT 0）。

- [ ] **Step 6: 提交点（用户手工提交）**

改动：`strategies/__init__.py` + `tests/test_auto_overlay_hooks.py`。

---

### Task 2: auto overlay 钩子之二 —— `autodiscover` 注册③档新策略（改进 D）

**Files:**
- Modify: `app/market_cn/auto/strategies/__init__.py`（`autodiscover` L53-66）
- Test: `tests/test_auto_overlay_hooks.py`（追加）

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_auto_overlay_hooks.py`）

```python
_MIN_STRATEGY = textwrap.dedent("""
    from app.market_cn.auto.strategies import register
    from app.market_cn.auto.strategies.base import StrategyBase

    @register
    class OverlayProbe(StrategyBase):
        key = "agentbt_t2_probe"
""")


def test_autodiscover_overlay_registers_new_strategy(tmp_path, monkeypatch):
    (tmp_path / "agentbt_t2_probe.py").write_text(_MIN_STRATEGY, encoding="utf-8")
    monkeypatch.setenv(_OVERLAY_ENV, str(tmp_path))
    S.autodiscover()
    assert S.get_strategy("agentbt_t2_probe") is not None


def test_autodiscover_overlay_rejects_non_agentbt_key(tmp_path, monkeypatch):
    """AST 静态预检：key 不带 agentbt_ 前缀的模块拒绝注册（防撞生产 key）。"""
    (tmp_path / "evil.py").write_text(
        _MIN_STRATEGY.replace("agentbt_t2_probe", "evil_key"), encoding="utf-8")
    monkeypatch.setenv(_OVERLAY_ENV, str(tmp_path))
    S.autodiscover()
    assert S.get_strategy("evil_key") is None


def test_autodiscover_overlay_per_module_fault_isolation(tmp_path, monkeypatch):
    """单模块损坏只跳过自身（与包内 autodiscover 同款 L3 隔离）。"""
    (tmp_path / "_broken.py").write_text("this is {{{ not python", encoding="utf-8")
    (tmp_path / "agentbt_t2_ok.py").write_text(
        _MIN_STRATEGY.replace("agentbt_t2_probe", "agentbt_t2_ok"), encoding="utf-8")
    monkeypatch.setenv(_OVERLAY_ENV, str(tmp_path))
    S.autodiscover()
    assert S.get_strategy("agentbt_t2_ok") is not None
```

- [ ] **Step 2: 跑测试确认失败**

```
d:\QuantDinger\.venv\Scripts\python.exe -m pytest tests/test_auto_overlay_hooks.py -v
```
预期：新增 3 用例 FAIL（`get_strategy` 返回 None）。

- [ ] **Step 3: 最小实现**

`strategies/__init__.py` 头部 import 补 `sys` 与 `ast`（`importlib.util` 就地引入）。
在 `autodiscover` 之后新增：

```python
def _import_overlay_modules():
    """§5.7 ③档通道：import 作业目录 overlay 里的新策略 .py（仅子进程；env 未设 = no-op）。

    红线：
      - 只注册 key 以 `agentbt_` 开头的模块 —— AST 静态预检（不 exec 就能拒），
        防止 overlay 模块撞生产 key（register 重复 key 会抛，但无碰撞的新 key 会静默注册）；
      - per-module 容错与包内 autodiscover 同款：单模块失败只 CRITICAL 不拖死；
      - 模块名走 `qd_overlay_<stem>` 命名空间，绝不与包内模块同名（sys.modules 防撞、幂等）。
    """
    import ast
    import importlib.util

    overlay = os.environ.get(OVERLAY_DIR_ENV)
    if not overlay or not os.path.isdir(overlay):
        return
    for fn in sorted(os.listdir(overlay)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        path = os.path.join(overlay, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=fn)
            declared = [
                n.value.value
                for n in ast.walk(tree)
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "key" for t in n.targets)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
            ]
            if not declared or not all(k.startswith("agentbt_") for k in declared):
                logger.critical("[strategies] overlay 模块 %s 未声明 agentbt_ 前缀 key, 拒绝注册", fn)
                continue
            stem = fn[:-3]
            mod_name = f"qd_overlay_{stem}"
            if mod_name in sys.modules:
                continue
            spec = importlib.util.spec_from_file_location(mod_name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
        except Exception as e:
            logger.critical("[strategies] overlay 模块 %s 加载失败, 已跳过: %s", fn, e)
```

`autodiscover()` 的包内扫描循环之后、`return sorted(_REGISTRY)` 之前插一行：

```python
    _import_overlay_modules()
```

- [ ] **Step 4: 跑测试确认通过**

```
d:\QuantDinger\.venv\Scripts\python.exe -m pytest tests/test_auto_overlay_hooks.py -v
```
预期：6 个用例全 PASS（含 `_registry_cleanup` 防注册表泄漏）。

- [ ] **Step 5: 提交点（用户手工提交）**

---

### Task 3: auto overlay 钩子之三 —— `evaluate.load_strategy`

**Files:**
- Modify: `app/market_cn/auto/core/runtime/evaluate.py`（L151-153）
- Test: `tests/test_auto_overlay_hooks.py`（追加）

- [ ] **Step 1: 写失败测试**（追加）

```python
def test_load_strategy_overlay_params(tmp_path, monkeypatch):
    key = "agentbt_t3"
    (tmp_path / f"{key}.yaml").write_text(textwrap.dedent("""
        meta: {market: A}
        params: {marker: 7}
        entry: {}
        signal: {}
        exit: {}
        gates: []
    """), encoding="utf-8")
    monkeypatch.setenv(_OVERLAY_ENV, str(tmp_path))
    from app.market_cn.auto.core.runtime import evaluate
    spec = evaluate.load_strategy(key)
    assert spec.params.get("marker") == 7
```

- [ ] **Step 2: 跑测试确认失败**

```
d:\QuantDinger\.venv\Scripts\python.exe -m pytest tests/test_auto_overlay_hooks.py -v
```
预期：FAIL（FileNotFoundError —— 仍读活树）。

- [ ] **Step 3: 最小实现**

`evaluate.py` 在 `load_strategy`（L151）之前加 helper（**刻意不 import strategies 复用 helper**——core→strategies 反向 import 违反依赖方向，`strategies/__init__.py` 尾部注入注释即前车之鉴；三行路径解析就地复制）：

```python
def _overlay_yaml_path(key: str) -> str:
    """§5.7 桥钩子：子进程 env QD_STRATEGY_YAML_OVERLAY_DIR 优先，回退活策略树。

    为什么不 import strategies 复用 _resolve_yaml_path：core→strategies 是反向依赖
    （依赖方向红线），三行路径解析就地复制并在此注明。
    """
    overlay = os.environ.get("QD_STRATEGY_YAML_OVERLAY_DIR")
    if overlay:
        p = os.path.join(overlay, f"{key}.yaml")
        if os.path.isfile(p):
            return p
    return os.path.join(_STRATEGY_DIR, f"{key}.yaml")
```

`load_strategy` 第一行改：

```python
    path = _overlay_yaml_path(key)
```

as-of 静态检查（L173 `_static_asof(spec)`）不动 —— overlay yaml 过同一道检查（原则③的口径一致性）。

- [ ] **Step 4: 跑测试确认通过 + 语法自检**

```
d:\QuantDinger\.venv\Scripts\python.exe -m pytest tests/test_auto_overlay_hooks.py -v ; d:\QuantDinger\.venv\Scripts\python.exe -m compileall -q app/market_cn/auto/
```
预期：7 用例 PASS；compileall 无输出。

- [ ] **Step 5: 提交点（用户手工提交）**

---

### Task 4: 桥核心 `chain/backtest_bridge.py`

**Files:**
- Create: `app/agent/chain/backtest_bridge.py`
- Test: `tests/test_backtest_bridge.py`

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""backtest_bridge 单测（§5.7 桥核心）。全部 fake 子进程，不依赖 DB/网络。

覆盖改进点：A（--out 落盘+stats 原样搬运）、B（manifest/digest）、C（state/并发/GC/杀树）、
E（proposal 物化走 _run_proposal_cli 接缝）、H（失败诚实落盘）、I（cwd/env 契约）。
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from app.agent.chain import backtest_bridge as B


@pytest.fixture(autouse=True)
def _tmp_root(tmp_path, monkeypatch):
    monkeypatch.setenv("QD_BT_ROOT", str(tmp_path / "bt"))
    yield
    return


_REPORT = {"stats": {"n": 5, "winrate": 60.0, "pl_ratio": 1.7, "n_valid": 5},
           "codes_ok": 3, "elapsed_seconds": 1.2,          # CLI 报告键名（backtest_cli._build_report）
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


def test_budget_reject(tmp_path):
    with pytest.raises(B.BridgeError):
        B.run_backtest_job(carrier={"kind": "existing", "key": "break"}, days=10 ** 6)
    with pytest.raises(B.BridgeError):
        B.run_backtest_job(carrier={"kind": "existing", "key": "break"},
                           scope="codes", codes=[str(i) for i in range(999)])
    with pytest.raises(B.BridgeBusy):
        monkey_run = B._bridge_root() / "jobs" / "x"
        monkey_run.mkdir(parents=True)
        (monkey_run / "manifest.json").write_text(
            json.dumps({"job_id": "x", "state": "running"}), encoding="utf-8")
        B.run_backtest_job(carrier={"kind": "existing", "key": "break"})


def test_existing_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "Popen", _fake_popen())
    live = tmp_path / "break.yaml"
    live.write_text("meta: {enabled: true}\n", encoding="utf-8")
    monkeypatch.setattr(B, "_live_yaml_path", lambda key: live)
    s = B.run_backtest_job(carrier={"kind": "existing", "key": "break"},
                           scope="codes", codes=["000001", "600000"], days=60,
                           session_id="sess-1")
    assert s["ok"] is True
    assert s["stats"]["n"] == 5 and s["git_sha"] == "abc1234"   # 改进 A：桥不重算
    assert Path(s["report_path"]).is_file()
    assert list((B._bridge_root() / "runs").glob("*.jsonl"))     # 改进 B/H 审计行已落盘
    m = json.loads((B._bridge_root() / "jobs" / s["job_id"] / "manifest.json")
                   .read_text(encoding="utf-8"))
    assert m["state"] == "done" and m["overlay_digest"]


def test_proposal_materialize_via_proposal_cli(tmp_path, monkeypatch):
    """改进 E：物化走 proposal CLI 接缝；活 yaml 全程只读。"""
    calls = []

    def fake_cli(args):
        calls.append(args)
        return ""

    monkeypatch.setattr(B, "_run_proposal_cli", fake_cli)
    monkeypatch.setattr(B, "Popen", _fake_popen())
    live = tmp_path / "break.yaml"
    live.write_text("params: {x: 1}\n", encoding="utf-8")
    monkeypatch.setattr(B, "_live_yaml_path", lambda key: live)
    s = B.run_backtest_job(
        carrier={"kind": "proposal",
                 "proposal": {"strategy": "break", "edits": []}},
        scope="codes", codes=["000001"], days=30)
    assert s["ok"] is True
    assert calls[0][0] == "validate" and calls[1][0] == "apply"
    job_overlay = Path(s["report_path"]).parent / "overlay"
    assert any(str(job_overlay) in str(a) for a in calls[1])      # apply 只指作业目录
    assert live.read_text(encoding="utf-8") == "params: {x: 1}\n"  # 活树未动


def test_failure_is_honest(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "Popen", _fake_popen(rc=2, stderr="boom\nValueError: 坏策略"))
    live = tmp_path / "break.yaml"
    live.write_text("meta: {}\n", encoding="utf-8")
    monkeypatch.setattr(B, "_live_yaml_path", lambda key: live)
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
    live = tmp_path / "break.yaml"
    live.write_text("meta: {}\n", encoding="utf-8")
    monkeypatch.setattr(B, "_live_yaml_path", lambda key: live)
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


def test_guard_job_dir_not_in_strategy_tree(tmp_path, monkeypatch):
    from app.market_cn.auto.core._paths import STRATEGY_DIR
    monkeypatch.setenv("QD_BT_ROOT", str(Path(STRATEGY_DIR) / "jail"))
    job = B._bridge_root() / "jobs" / "x"
    job.mkdir(parents=True, exist_ok=True)
    with pytest.raises(B.BridgeError):
        B._assert_guards(job)
```

- [ ] **Step 2: 跑测试确认失败**

```
d:\QuantDinger\.venv\Scripts\python.exe -m pytest tests/test_backtest_bridge.py -v
```
预期：ModuleNotFoundError（桥不存在）。

- [ ] **Step 3: 实现桥核心**

```python
# -*- coding: utf-8 -*-
"""chain/backtest_bridge.py — agent × auto 回测独立桥（AGENT_DESIGN_v3.md §5.7，2026-10-15）。

定位：agent 想出的策略假设 → auto 官方回测口径（run_all）→ 归一化摘要回传。
三原则（用户裁定）：
  ① 独立桥：对 auto 只加 env 默认关闭的读钩子（strategies/_yaml_doc、autodiscover、
     evaluate.load_strategy 三处，见 2026-10-15 复核补的第四钩子=autodiscover）；
     对 agent 只新增本模块 + auto_bt_tools.py + skills/auto-backtest，不动现有回测链。
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
  - claim 闭环不在桥内做：agent 把结论写进 answer，§5.1 intake 主流程自动过闸抽 claim。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import yaml

#: 桥产物根（env 可改：测试/部署）；缺省 = backend_api_python/data/backtest_bridge
def _bridge_root() -> Path:
    return Path(os.getenv("QD_BT_ROOT") or (
        Path(__file__).resolve().parents[3] / "data" / "backtest_bridge"))


#: backend 根（backtest_cli 的 docstring 要求 cwd=backend_api_python）
_BACKEND_ROOT = Path(__file__).resolve().parents[3]

ENV_OVERLAY_DIR = "QD_STRATEGY_YAML_OVERLAY_DIR"
CLI_MODULE = "app.market_cn.auto.backtest_cli"

#: 模块级别名（测试 monkeypatch B.Popen 的接缝；直接调 subprocess.Popen 会绕过替身）
Popen = subprocess.Popen


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


def _running_jobs() -> list:
    out = []
    for mp in _bridge_root().joinpath("jobs").glob("*/manifest.json"):
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if m.get("state") == "running":
            out.append(m.get("job_id") or mp.parent.name)
    return out


def _assert_guards(job: Path) -> None:
    """原则③守卫：作业目录绝不允许落在活策略树内（写域白名单的数据面闸门）。"""
    from app.market_cn.auto.core._paths import STRATEGY_DIR
    if str(job.resolve()).startswith(str(Path(STRATEGY_DIR).resolve())):
        raise BridgeError("作业目录不得位于活策略树内（QD_BT_ROOT 配置错误）")


# ================================================================
# 载体物化（原则②；活树全程只读）
# ================================================================

def _live_yaml_path(key: str) -> Path:
    """活策略树 yaml 路径（只读引用；测试唯一接缝——monkeypatch 这里）。"""
    from app.market_cn.auto.core._paths import STRATEGY_DIR
    return Path(STRATEGY_DIR) / f"{key}.yaml"


def _run_proposal_cli(args: list) -> str:
    """子进程跑 auto/tools/proposal.py（validate 红线 / apply 物化）；非 0 退出 = 失败。"""
    cmd = [sys.executable, "-m", "app.market_cn.auto.tools.proposal", *args]
    proc = subprocess.run(cmd, cwd=str(_BACKEND_ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=TIMEOUT_S)
    if proc.returncode != 0:
        raise BridgeError(f"proposal {args[0]} 失败(exit={proc.returncode})，尾部输出:\n"
                          f"{(proc.stderr or proc.stdout or '')[-2000:]}")
    return proc.stdout or ""


def _materialize_proposal(job: Path, proposal: dict) -> str:
    """①/②档：拷活 yaml → 作业 overlay，proposal validate+apply 只落作业目录。

    validate 的 before 值一致性检查针对 overlay 内副本（与活文件逐字节相同，检查语义不变）；
    apply 的 .history 备份随 --strategy-dir 落在作业目录内（proposal.py 落盘机制）。
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
                       "--strategy-dir", str(overlay)])
    _run_proposal_cli(["apply", "--proposal", str(job / "proposal.json"),
                       "--strategy-dir", str(overlay), "--apply"])
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


def _overlay_digest(overlay: Path) -> tuple:
    """overlay 文件清单 {相对名: sha256[:16]} 与总 digest（可复测的最小证据集，改进 B）。"""
    import hashlib

    def _sha16(b: bytes) -> str:
        return hashlib.sha256(b).hexdigest()[:16]

    files = {str(p.relative_to(overlay)): _sha16(p.read_bytes())
             for p in sorted(overlay.rglob("*")) if p.is_file()}
    return files, _sha16(json.dumps(files, sort_keys=True).encode("utf-8"))


# ================================================================
# 子进程执行（原则①③；改进 C/H/I）
# ================================================================

def _kill_tree(proc: subprocess.Popen) -> None:
    """杀整棵进程树。Windows: taskkill /T /F；posix: killpg（孙进程残留史见模块头）。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


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
    proc = Popen(cmd, cwd=str(_BACKEND_ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        _out, err = proc.communicate(timeout=timeout_s)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        _out, err = proc.communicate()
        raise subprocess.TimeoutExpired(
            cmd=cmd, timeout=timeout_s, stderr=(err or _out or "")[-2000:])
    if rc != 0:
        raise RuntimeError(f"backtest_cli 退出码 {rc}，尾部输出:\n"
                           f"{(err or _out or '')[-2000:]}")
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
    （诚实失败语义：失败必须可见可追溯，B2 教训）。
    """
    # ---- Phase 1: 调用方校验（不落盘，错误直接抛）----
    kind = str((carrier or {}).get("kind") or "")
    if kind not in ("proposal", "new", "existing"):
        raise BridgeError(f"carrier.kind 必须是 proposal|new|existing: {kind!r}")
    if scope not in ("codes", "all"):
        raise BridgeError(f"scope 必须是 codes|all: {scope!r}")
    if kind == "new" and not strategy_key:
        raise BridgeError("kind=new 必须传 strategy_key（agentbt_ 前缀）")
    codes = [str(c) for c in (codes or [])]
    days = _check_budget(days, 0 if scope == "all" else len(codes))
    timeout_s = int(timeout_s or TIMEOUT_S)
    running = _running_jobs()
    if len(running) >= MAX_CONCURRENT:
        raise BridgeBusy(f"已有 {len(running)} 个作业在跑 "
                         f"≥ QD_BT_MAX_CONCURRENT={MAX_CONCURRENT}: {running}")

    # ---- Phase 2: 作业目录 + manifest（此后失败一律诚实落盘，不抛）----
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    job_id = f"{time.strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}"
    job = _bridge_root() / "jobs" / job_id
    (job / "overlay").mkdir(parents=True, exist_ok=True)
    _assert_guards(job)
    manifest = {"job_id": job_id, "ts": ts, "state": "running",
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
            key = _materialize_proposal(job, carrier["proposal"])
        elif kind == "new":
            key = _materialize_new(job, strategy_key, carrier["py"], carrier["yaml"])
        else:
            if not _live_yaml_path(strategy_key).is_file():
                raise BridgeError(f"策略 {strategy_key!r} 的活 yaml 不存在")
            key = strategy_key
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
        state, summary, error_tail = "timeout", None, \
            f"回测超时 >{timeout_s}s（进程树已杀）{(getattr(e, 'stderr', '') or '')[-2000:]}"
    except Exception as e:                                 # noqa: BLE001
        state, summary, error_tail = "failed", None, f"{type(e).__name__}: {e}"

    if state != "done":
        summary = {"ok": False, "job_id": job_id, "ts": ts, "session_id": session_id,
                   "strategy_key": strategy_key, "carrier": kind, "scope": scope,
                   "days": days, "codes_n": len(codes), "exec_engine": exec_engine,
                   "stats": None, "error": error_tail,
                   "summary_path": str(job / "summary.json")}
    manifest["state"] = state
    if state != "done":
        manifest["error"] = error_tail
    _write_manifest(job, manifest)
    (job / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    _append_jsonl(summary)
    return summary


def _write_manifest(job: Path, manifest: dict) -> None:
    (job / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")


def _append_jsonl(summary: dict) -> None:
    """§5.7 一行审计（runs/<date>_<job>.jsonl）；失败作业 stats=None + error 字段（改进 H）。"""
    runs = _bridge_root() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    date_tag = (summary.get("ts") or time.strftime("%Y%m%d"))[:10].replace("-", "")
    row = {"ts": summary.get("ts"), "session_id": summary.get("session_id", ""),
           "job_id": summary.get("job_id"), "strategy_key": summary.get("strategy_key"),
           "carrier": summary.get("carrier"), "overlay_digest": summary.get("overlay_digest"),
           "scope": summary.get("scope"), "days": summary.get("days"),
           "stats": summary.get("stats"),
           "meta": {"git_sha": summary.get("git_sha"),
                    "effective_params": summary.get("effective_params")},
           "pid": os.getpid(),
           "conclusion_ref": summary.get("session_id", ""),   # 改进 G：不记 claim，只留关联
           "error": summary.get("error")}
    path = runs / f"{date_tag}_{summary['job_id']}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def gc_jobs(now: Optional[float] = None) -> list:
    """TTL 归档：超龄作业目录**移动**到 _del/（项目规则：删除先移 del，不直接删）。

    running 态超龄作业先标 failed —— 进程多半已死，悬挂状态不许留（可观测性）。
    """
    now = now or time.time()
    cutoff = now - JOB_TTL_DAYS * 86400
    moved = []
    del_root = _bridge_root() / "_del"
    for d in sorted(_bridge_root().joinpath("jobs").glob("*")):
        if not d.is_dir() or d.stat().st_mtime > cutoff:
            continue
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
        moved.append(d.name)
    return moved
```

- [ ] **Step 4: 跑测试确认通过**

```
d:\QuantDinger\.venv\Scripts\python.exe -m pytest tests/test_backtest_bridge.py -v
```
预期：8 个用例 PASS。注意 `test_budget_reject` 里第三个分支（BridgeBusy）写在同一个用例内的 manifest 预置依赖前两个分支未落盘 —— 若顺序脆弱，拆成三个独立用例。

- [ ] **Step 5: 提交点（用户手工提交）**

---

### Task 5: 工具面 `tools/finance/auto_bt_tools.py`

**Files:**
- Create: `app/agent/tools/finance/auto_bt_tools.py`
- Test: `tests/test_auto_bt_tools.py`

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""auto_bt_tools 单测：命名红线（WRITE_PREFIXES）+ 载体分发 + 预算前置校验。"""
import json

import pytest

from app.agent.capabilities.scanner import is_write_name
from app.agent.tools.finance import auto_bt_tools as T


def test_tool_name_avoids_write_prefixes():
    assert not is_write_name("agent_backtest_strategy")   # §5.6 坑表：run_ 前缀硬拒


def test_empty_codes_rejected(monkeypatch):
    captured = {}

    def fake_run(**kw):
        captured.update(kw)
        return {"ok": True}

    monkeypatch.setattr(T, "run_backtest_job", fake_run)
    out = T.agent_backtest_strategy(strategy_key="break", codes="")
    assert out["ok"] is False and "技能" in out["error"]
    assert not captured


def test_bad_codes_rejected():
    out = T.agent_backtest_strategy(strategy_key="break", codes="000001,ABCD")
    assert out["ok"] is False and "数字" in out["error"]


def test_proposal_carrier_dispatch(monkeypatch):
    captured = {}

    def fake_run(**kw):
        captured.update(kw)
        return {"ok": True}

    monkeypatch.setattr(T, "run_backtest_job", fake_run)
    proposal = {"strategy": "break", "edits": [{"kind": "param",
                                                "target": "x", "before": 1, "after": 2}]}
    out = T.agent_backtest_strategy(proposal_json=json.dumps(proposal),
                                    codes="000001 600000", days=30)
    assert out["ok"] is True
    assert captured["carrier"]["kind"] == "proposal"
    assert captured["codes"] == ["000001", "600000"]


def test_new_carrier_requires_key():
    out = T.agent_backtest_strategy(strategy_py="x", strategy_yaml={"a": 1},
                                    codes="000001")
    assert out["ok"] is False and "strategy_key" in out["error"]


def test_existing_carrier_via_key_only(monkeypatch):
    """只传 strategy_key = existing 对照组（零改动回测现有策略）。"""
    captured = {}

    def fake_run(**kw):
        captured.update(kw)
        return {"ok": True}

    monkeypatch.setattr(T, "run_backtest_job", fake_run)
    out = T.agent_backtest_strategy(strategy_key="break", codes="000001")
    assert out["ok"] is True
    assert captured["carrier"] == {"kind": "existing", "key": "break"}


def test_missing_carrier_rejected():
    out = T.agent_backtest_strategy(codes="000001")
    assert out["ok"] is False and "载体缺失" in out["error"]
```

- [ ] **Step 2: 跑测试确认失败**

```
d:\QuantDinger\.venv\Scripts\python.exe -m pytest tests/test_auto_bt_tools.py -v
```
预期：ModuleNotFoundError。

- [ ] **Step 3: 实现**

```python
# -*- coding: utf-8 -*-
"""tools/finance/auto_bt_tools.py — agent × auto 回测桥的工具面（§5.7，2026-10-15）。

只放「个股/小集合」桥工具；全盘量级走技能 skills/auto-backtest（能力层 60s 扛不住，
走 §5.3 子进程+产物落盘）。**不塞进 backtest_tools.py** —— 那是指标/脚本策略链
（BacktestService → qd_backtest_runs），与本桥（内存 trades、零写共享表）两套口径，
混文件必把工具预选送错链（§5.7 与现网两条回测链的关系表）。

命名红线：公开函数名避开 capabilities/scanner.WRITE_PREFIXES（run_/apply_ 等），
"agent_backtest_strategy" 由此得名（§5.6 坑表「run_ 前缀硬拒」）。

易错点：本工具是 agent 上下文内的同步调用（分钟级）——预算钳制（QD_BT_*）保证
不超时；scope=all 永远不要从这里走。
"""
from __future__ import annotations

import json

from app.agent.chain.backtest_bridge import (
    BridgeBusy,
    BridgeError,
    run_backtest_job,
)


def agent_backtest_strategy(
    strategy_key: str = "",
    proposal_json: str = "",
    strategy_py: str = "",
    strategy_yaml: str = "",
    codes: str = "",
    days: int = 120,
) -> dict:
    """对 auto 折叠策略跑一次回测（独立桥：生产树只读、零写共享表、删作业目录即回滚）。

    载体三选一（§5.7 档位）：
      ①/② proposal_json —— 对现有策略 <strategy>.yaml 的最小改动提案（proposal schema；
        改选择集须附 evidence.stability/pool_check，红线由 auto proposal.validate 强制）；
      ③ strategy_py + strategy_yaml —— 全新策略（strategy_key 必填，agentbt_ 前缀）；
      existing —— 只传 strategy_key 且不传前两者 = 回测现有策略零改动（对照组）。
    全盘回测（scope=all）不在此工具：请走 auto-backtest 技能。

    Args:
        strategy_key: 现有策略 key（如 break/g56/dragon_callback）或新策略 key
        proposal_json: proposal JSON 字符串（①/②档）
        strategy_py: 新策略 .py 全文（③档，@register + 折叠契约）
        strategy_yaml: 新策略 yaml 的 JSON 字符串（③档）
        codes: 逗号/空格分隔的股票代码（必填，≤ QD_BT_MAX_CODES 个）
        days: 回看自然日数（默认 120，上限 QD_BT_MAX_DAYS）

    Returns:
        {"ok": bool, "job_id": str, "stats": {n,winrate,pl_ratio,...}|None,
         "report_path": str（全量逐笔，agent 自取）, "elapsed_seconds": ...,
         "error": str（失败时，含 stderr 尾部）}
    """
    if not codes.strip():
        return {"ok": False, "error": "codes 不能为空；全盘回测请走 auto-backtest 技能"}
    code_list = [c for c in codes.replace(",", " ").split() if c]
    bad = [c for c in code_list if not c.isdigit()]
    if bad:
        return {"ok": False, "error": f"股票代码应为数字: {', '.join(bad)}"}

    try:
        if proposal_json:
            proposal = json.loads(proposal_json)
            if not isinstance(proposal, dict):
                return {"ok": False, "error": "proposal_json 必须是 JSON object"}
            carrier = {"kind": "proposal", "proposal": proposal}
        elif strategy_py or strategy_yaml:
            if not (strategy_py and strategy_yaml):
                return {"ok": False, "error": "③档必须同时提供 strategy_py 与 strategy_yaml"}
            if not strategy_key:
                return {"ok": False, "error": "③档必须提供 strategy_key（agentbt_ 前缀）"}
            try:
                yaml_doc = json.loads(strategy_yaml)
            except (TypeError, ValueError) as e:
                return {"ok": False, "error": f"strategy_yaml 必须是 JSON 字符串: {e}"}
            if not isinstance(yaml_doc, dict):
                return {"ok": False, "error": "strategy_yaml 必须是 JSON object"}
            carrier = {"kind": "new", "py": strategy_py, "yaml": yaml_doc}
        else:
            if not strategy_key:
                return {"ok": False, "error":
                        "载体缺失：传 proposal_json（①/②档）或 strategy_py+strategy_yaml（③档）"}
            carrier = {"kind": "existing", "key": strategy_key}
        return run_backtest_job(carrier=carrier, scope="codes", codes=code_list,
                                days=int(days), strategy_key=strategy_key)
    except BridgeBusy as e:
        return {"ok": False, "error": f"回测桥忙: {e}"}
    except BridgeError as e:
        return {"ok": False, "error": str(e)}
```

⚠ 注：`strategy_yaml` 工具入参为 **JSON 字符串**（LLM 传参规范统一为字符串，与 `proposal_json` 一致），工具内 `json.loads` 成 dict 再进桥。

- [ ] **Step 4: 跑测试确认通过**

```
d:\QuantDinger\.venv\Scripts\python.exe -m pytest tests/test_auto_bt_tools.py -v
```
预期：7 用例 PASS。

- [ ] **Step 5: 提交点（用户手工提交）**

---

### Task 6: 技能面 `skills/auto-backtest/`（全盘量级）

**Files:**
- Create: `app/agent/skills/auto-backtest/SKILL.md`
- Create: `app/agent/skills/auto-backtest/run.py`
- Modify: `app/agent/scripts/skill_run.py:28-35`（EXECUTABLE_SKILLS +1）
- Test: `tests/test_auto_bt_tools.py`（追加）

- [ ] **Step 1: 写失败测试之一（白名单登记）**（追加到 `tests/test_auto_bt_tools.py`）

```python
def test_skill_registered_in_whitelist():
    from app.agent.scripts.skill_run import EXECUTABLE_SKILLS
    assert EXECUTABLE_SKILLS.get("auto-backtest") == \
        ("skills.auto-backtest.run", ("run",), "run")
```

- [ ] **Step 1.5: 核对技能模块的执行形态（2 分钟，防连字符目录踩坑）**

`skills/auto-backtest/` 目录名带连字符（非合法包名），skill_run.py 的 `modpath.resolve` 机制（F7）按字符串路径解析 —— 现网先例 `skills/market-screener/run.py` 同为连字符目录。实现前先读 `skill_run.py` L60-L115 的 `modpath.resolve` 与 `getattr(mod, fn)` **调用形态**，确定技能函数收 `run(spec)` 还是 `run(**spec)`，并把结论回填到本步骤：

- 结论（实施时填写）：`run` 函数签名 = `run(args: dict) -> dict`（与 market-screener 同款；若核对结果不同，以核对为准并同步本文件与本任务测试）。

- [ ] **Step 2: 写失败测试之二（技能行为）**

⚠ **连字符目录的 import 技巧**：`skills/auto-backtest/` 目录名带连字符，测试里不能直接 `import`。照 `market-screener` 的既有做法（它同样带连字符且被 skill_run.py 成功执行）：测试用 `importlib.util.spec_from_file_location("auto_bt_skill_run", r"<path>/skills/auto-backtest/run.py")` 按路径加载后调用：

```python
import importlib.util
from pathlib import Path

_SKILL_RUN = Path(__file__).resolve().parents[1] / "app/agent/skills/auto-backtest/run.py"


def _load_skill_run():
    spec = importlib.util.spec_from_file_location("auto_bt_skill_run", _SKILL_RUN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_skill_run_dispatches_scope_all(monkeypatch):
    captured = {}

    def fake_run(**kw):
        captured.update(kw)
        return {"ok": True, "job_id": "j1", "stats": {"n": 120},
                "report_path": "/tmp/r.json", "summary_path": "/tmp/s.json",
                "elapsed_seconds": 12.0}

    monkeypatch.setattr("app.agent.chain.backtest_bridge.run_backtest_job", fake_run)
    mod = _load_skill_run()
    out = mod.run({"scope": "all", "strategy_key": "break", "days": 180})
    assert out["ok"] is True
    assert captured["scope"] == "all" and captured["days"] == 180
    assert "preview" in out and len(out["preview"]) <= 20000     # F7 预览上限口径


def test_skill_run_rejects_missing_carrier():
    mod = _load_skill_run()
    out = mod.run({"scope": "all"})
    assert out["ok"] is False
```

- [ ] **Step 3: 跑测试确认失败 → 实现**

```
d:\QuantDinger\.venv\Scripts\python.exe -m pytest tests/test_auto_bt_tools.py -v
```
预期：新用例 FAIL（文件不存在）。然后写 `app/agent/skills/auto-backtest/run.py`：

```python
# -*- coding: utf-8 -*-
"""skills/auto-backtest/run.py — 全盘量级回测技能入口（§5.3/§5.7，2026-10-15）。

与工具面的分界（§5.6）：工具 = 一次确定性调用（个股/小集合，分钟级）；技能 = 多步
编排 + 大预算（scope=all 全市场约 5k 票，十分钟级），走 skill_run.py 子进程白名单，
产物落盘 + 预览回传，主上下文 O(摘要)。

易错点：本模块由 skill_run.py 按路径执行（目录名带连字符，不可包导入）；函数签名
必须与 skill_run.py 的调用形态一致（实施时按 writing-plans Task 6 Step 1.5 核对）。
"""
from __future__ import annotations

import json

from app.agent.chain.backtest_bridge import BridgeBusy, BridgeError, gc_jobs, run_backtest_job


def _preview(summary: dict) -> str:
    """O(摘要) 预览：主 agent 只读这一段；明细在 report_path/summary_path。"""
    if not summary.get("ok"):
        return f"回测失败: {summary.get('error', '')[:500]}"
    s = summary.get("stats") or {}
    return (f"策略 {summary.get('strategy_key')} | {summary.get('scope')} "
            f"days={summary.get('days')} | 样本 {s.get('n', 0)} "
            f"(有效 {s.get('n_valid', '?')}) | 胜率 {s.get('winrate', '?')}% "
            f"| 盈亏比 {s.get('pl_ratio', '?')} | 耗时 {summary.get('elapsed_seconds')}s\n"
            f"全量报告: {summary.get('report_path')}")


def run(args: dict) -> dict:
    """skill_run.py 白名单入口（fn=run）。args 为 run_skill 传入的 spec dict。"""
    gc_jobs()                                  # 顺手 TTL 归档（幂等、廉价）
    scope = str((args or {}).get("scope") or "all")
    days = int((args or {}).get("days") or 365)
    strategy_key = str((args or {}).get("strategy_key") or "")
    try:
        if (args or {}).get("proposal_json"):
            carrier = {"kind": "proposal",
                       "proposal": json.loads(args["proposal_json"])}
        elif (args or {}).get("strategy_py") and (args or {}).get("strategy_yaml"):
            yaml = args["strategy_yaml"]
            carrier = {"kind": "new", "py": args["strategy_py"],
                       "yaml": json.loads(yaml) if isinstance(yaml, str) else yaml}
        elif strategy_key:
            carrier = {"kind": "existing", "key": strategy_key}
        else:
            return {"ok": False,
                    "error": "载体缺失：proposal_json / strategy_py+strategy_yaml / strategy_key 三选一"}
        summary = run_backtest_job(
            carrier=carrier, scope=scope,
            codes=[c for c in str(args.get("codes") or "").replace(",", " ").split() if c],
            days=days, strategy_key=strategy_key)
        return {**summary, "preview": _preview(summary)}
    except BridgeBusy as e:
        return {"ok": False, "error": f"回测桥忙: {e}"}
    except BridgeError as e:
        return {"ok": False, "error": str(e)}
```

`SKILL.md`（结构照 `skills/market-screener/SKILL.md`，frontmatter 必填 name/description）：

```markdown
---
name: auto-backtest
description: 全盘量级回测技能：把策略假设（proposal 改参/加门、或全新 agentbt_ 策略、或现有策略对照）送 auto 折叠回测官方口径（run_all），十分钟级，产物落盘回传预览。个股/小集合（分钟级）不要用本技能，用工具 agent_backtest_strategy。
---

# auto-backtest（agent × auto 回测桥 · 全盘档）

## 何时用我
- 需要全市场（scope=all）跑一个策略假设的胜率/盈亏比；
- 个股/小集合验证请改用工具 `agent_backtest_strategy`（分钟级）。

## 参数（spec dict）
- scope: "all"（全盘）| "codes"（配 codes 逗号分隔）
- strategy_key: 现有策略 key（break/g56/dragon_callback）或 agentbt_ 前缀新策略
- proposal_json: 对现有策略 yaml 的最小改动提案（改选择集须附 evidence.stability/pool_check）
- strategy_py / strategy_yaml: 全新策略成对载体（key 必须 agentbt_ 前缀）
- days: 回看自然日数（默认 365，上限 QD_BT_MAX_DAYS）

## 产物与红线
- 摘要回传 ≤20000 字符；全量逐笔在 report_path（agent 引用路径，不搬进上下文）
- 生产树只读、零写共享表；回滚 = 删作业目录 data/backtest_bridge/jobs/<job_id>/
- 结论解读后写进 answer —— T+N 问责由 intake 主流程自动接住（桥不记 claim）
```

`skill_run.py` 注册表追加一行：

```python
    "auto-backtest": ("skills.auto-backtest.run", ("run",), "run"),
```

- [ ] **Step 4: 跑测试确认通过**

```
d:\QuantDinger\.venv\Scripts\python.exe -m pytest tests/test_auto_bt_tools.py -v
```
预期：10 用例 PASS（Task 5 的 7 个 + 本任务 3 个）。

- [ ] **Step 5: 真连冒烟（可选但推荐；需要 DB 与行情库）**

```
cd backend_api_python ; d:\QuantDinger\.venv\Scripts\python.exe -m app.agent.scripts.skill_run auto-backtest "{\"scope\":\"codes\",\"strategy_key\":\"break\",\"codes\":\"000001\",\"days\":30}"
```
预期：stdout 输出 JSON（ok=true，或带真实失败原因的 ok=false——两种都可接受，不允许的是 traceback 崩溃）。

- [ ] **Step 6: 提交点（用户手工提交）**

---

### Task 7: 守卫与集成（桥自身的不变量测试）

**Files:**
- Test: `tests/test_backtest_bridge.py`（追加）

- [ ] **Step 1: 追加守卫测试**

```python
def test_bridge_never_imports_auto_writers():
    """原则③静态守卫：桥源码禁止 import auto 的写者/扫描/重建模块。"""
    src = B.__file__
    text = Path(src).read_text(encoding="utf-8")
    for bad in ("auto.scan", "auto.rebuild", "auto.monitor", "auto.startup",
                "auto.sched", "run_scan(", "load_windows", "rebuild("):
        assert bad not in text, f"桥源码出现禁止符号: {bad}"


def test_tool_file_separate_from_backtest_tools():
    """§5.7：桥工具不得并入指标/脚本策略链文件。"""
    from app.agent.tools.finance import auto_bt_tools
    assert "BacktestService" not in Path(auto_bt_tools.__file__).read_text(encoding="utf-8")
```

- [ ] **Step 2: 跑测试**

```
d:\QuantDinger\.venv\Scripts\python.exe -m pytest tests/test_backtest_bridge.py -v
```
预期：10 用例 PASS。

- [ ] **Step 3: 提交点（用户手工提交）**

---

### Task 8: 设计文档回填（增量追加，署名）

**Files:**
- Modify: `app/agent/AGENT_DESIGN_v3.md`（§5.7 末尾 + 文末变更史）

- [ ] **Step 1: §5.7「实现落点见同目录…」段之后追加一个引用块**

```markdown
> **复核二（2026-10-15，施工前终审，署名：Trae agent / backtest_bridge_plan）**：
> 对照代码逐条对账后新增 9 项数据流改进 + 2 项修正（证据锚点逐条见同目录
> `backtest_bridge_plan.md` §0）：
> A 结果回流护栏（`--out` 落盘 + stats 原样搬运，桥绝不重算指标，口径单源 `core.backtest._summary`）；
> B `manifest.json`（state/budget/overlay_files+digest = 可复测最小证据集）；
> C 生命周期（state 三态 / `QD_BT_MAX_CONCURRENT` running 计数 / TTL GC 移入 `_del/` / Windows `taskkill /T /F` 杀树）；
> D **第四钩子 = `autodiscover`**（③档 .py 在作业目录，包内扫描看不见——本表原三处钩子漏它）；
> E `--apply` 禁令精确化：禁的是**活树**；`apply --strategy-dir <作业overlay> --apply` 是唯一物化通道（`.history` 备份随 `--strategy-dir` 落在作业目录内）；
> F 一期钉死 `exec_engine=daily`（1m 精修路径取数缓存行为未验证，二期放开）；
> G claim 闭环不在桥内手工记（intake 主流程自动过闸抽 claim，桥内记 = 绕闸门 + 双重入库）；
> H 失败诚实落盘（ok=False + error/stderr_tail 进 summary 与 JSONL，state=failed/timeout）；
> I env/cwd 契约（cwd=backend 根 + overlay env 仅子进程 + `QD_BT_*` 预算六件套）。
> 状态：方案已立（`backtest_bridge_plan.md`），未施工。
```

- [ ] **Step 2: 文末变更史追加一行**（沿用 L2017 格式）

```markdown
- 2026-10-15 **§5.7 回测桥施工前终审**：新增 9 项数据流改进 + 2 项修正（第四钩子 autodiscover；--apply 语义精确化为作业目录内物化通道），一期钉死 exec_engine=daily、桥不记 claim。施工清单：`backtest_bridge_plan.md`（TDD 8 任务）。
```

- [ ] **Step 3: 核对追加未破坏既有内容**

```
d:\QuantDinger\.venv\Scripts\python.exe -c "from pathlib import Path; t=Path(r'app/agent/AGENT_DESIGN_v3.md').read_text(encoding='utf-8'); print('复核二' in t, t.count('2026-10-15'))"
```
预期：True，计数 ≥ 之前值（只增不减）。

- [ ] **Step 4: 提交点（用户手工提交）**

---

## 4 验收清单（全部满足才算完成）

1. `python -m pytest tests/test_auto_overlay_hooks.py tests/test_backtest_bridge.py tests/test_auto_bt_tools.py -v` 全绿（在 backend_api_python 下，venv 解释器）。
2. `python -m compileall -q app/agent/ app/market_cn/auto/` 无输出。
3. 生产进程不设任何 `QD_*_OVERLAY` / `QD_BT_*` env 时，`strategies`/`evaluate` 行为与改造前一致（回归钉子用例 PASS 即证明）。
4. 桥产物只出现在 `data/backtest_bridge/**` 与 `tmp/skill_output/**`；共享表零写（守卫测试 + 隔离清单）。
5. `AGENT_DESIGN_v3.md` 只有追加、无删改他人内容；追加块署名。

## 5 明确不做（防过度工程）

- 不做异步 job 队列/HTTP 状态轮询（同步 + 预算钳制已覆盖工具与技能两条路径的时延窗口）；
- 不做指标二次计算/再聚合（stats 原样搬运）；
- 不把 `pool_check` 等证据函数接进能力层（二期，见 §0.3）；
- 不支持多策略一次 CLI 调用（F6：单次一策略，多假设 = 多 job 排队）。

---

*署名：Trae agent（数据流缺口分析 + 实施方案），2026-10-15。设计态依据：`AGENT_DESIGN_v3.md` §5.7（2026-10-15 复核稿）。*
