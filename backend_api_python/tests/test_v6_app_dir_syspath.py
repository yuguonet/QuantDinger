# -*- coding: utf-8 -*-
"""D-14 红线守卫：`app/` 目录**不得**进 sys.path（2026-10-14）。

起因：跑 `python -m app.agent.cli` 做实测时顺手监控 sys.path，发现两处历史包袱。
  ① `app/agent/cli.py` 把 `app/agent` 插进 sys.path[0]（注释还写着 P5 之前的旧口径
     "agent 包内一律用裸名"）。实证：执行后裸名 `tools` 与 `app.agent.tools` 是
     **两个不同的 module 对象**（同一份文件加载两遍）= 事故 M-1 的活跃敞口。已删。
  ② `app/__init__.py` 把 `app/` 插进 sys.path[0]，而其正上方的注释却写
     "本目录也不必再进 sys.path" —— 注释与代码自相矛盾。已删（本文件的守卫对象）。

为什么 ② 删得掉（`tmp/probe_app_dir_syspath_20261014.py` 实测）：
  · 原始动机「让 `app/nanobot/` 遮蔽 pip 的 nanobot-ai」已失效：目录不存在、
    `find_spec('nanobot')` 为 None；
  · 全仓扫「裸名 import 且指向 app/* 顶层名」只有三处命中，且**删前删后都失败**：
    `app/market_store/plugin_api.py`（`app/market_store/__init__.py` 是空文件，
    `_HAS_DEPS` 早就是 False）、`scripts/calibrate_scores.py`（`utils.calibration`
    不存在，脚本本就断链）、原 `app/agent/tmp/*.bak.py`（备份文件，
    该目录已于 2026-10-14 移入 `del/agent_tmp_20261014/`）；
  · 动态加载不依赖它：cron 走 `resolve_function_path()` 补包前缀，
    `capabilities/scanner.py` 挂的是 backend 根。

★ 为什么用**子进程**而不是在 pytest 进程里断言 sys.path：
  pytest 的 rootdir/conftest 注入、以及其它测试的 import 副作用都会改 sys.path，
  在进程内断言会随用例顺序漂移（假红/假绿）。子进程是干净解释器，结果确定。
  ⚠ 判"某目录不在 sys.path"前先确认是干净进程 —— 这是本仓第三次同类教训
  （前两次：P17-b 相对 import 漏扫、P18-b 只扫包内漏掉包外消费方）。

★ 为什么源码级用 **AST** 而不是 `in src` 全文匹配：
  本文件自己的注释里就写着 `sys.path.insert` 字样，全文匹配会**恒红**（假守卫）。
  同理 `app/__init__.py` 的新注释里也写了这些字 —— 必须只匹配真实 `ast.Call`。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_APP_INIT = _BACKEND / "app" / "__init__.py"
_CLI = _BACKEND / "app" / "agent" / "cli.py"
_PY = sys.executable

# `app/` 下的顶层目录名（曾被 sys.path 注入暴露成全进程裸名的那批）
_TOP_NAMES = (
    "agent", "config", "data", "data_providers", "data_sources",
    "market_cn", "market_store", "routes", "services", "utils", "watchlist",
)


# ────────────────────────────────────────────────────────────── 工 具


def _syspath_mutations(py: Path) -> list:
    """返回源码里所有**真实**的 `sys.path.insert/append` 调用：[(行号, [参数的源码文本])]。

    只认 `ast.Call`，杜绝注释/docstring 里的同名字样（本仓已踩过一次：
    `test_no_spec_from_file_location_in_qd_service` 因全文匹配而恒红）。
    """
    tree = ast.parse(py.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute) or fn.attr not in {"insert", "append"}:
            continue
        owner = fn.value
        if not (isinstance(owner, ast.Attribute) and owner.attr == "path"):
            continue
        if not (isinstance(owner.value, ast.Name) and owner.value.id in {"sys", "_sys"}):
            continue
        found.append((node.lineno, [ast.unparse(a) for a in node.args]))
    return found


def _run_snippet(code: str, timeout: int = 240) -> dict:
    """在**干净子进程**里跑一段代码，取回它 `__JSON__` 打印的字典。"""
    proc = subprocess.run(
        [_PY, "-c", code],
        cwd=str(_BACKEND),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    marker = "__JSON__"
    line = ""
    for ln in proc.stdout.splitlines():
        if ln.startswith(marker):
            line = ln[len(marker):]
            break
    assert line, (
        f"子进程没吐出 {marker} 标记 —— 多半是 `import app` 直接崩了。\n"
        f"rc={proc.returncode}\nstdout(tail)={proc.stdout[-1500:]}\n"
        f"stderr(tail)={proc.stderr[-1500:]}"
    )
    return json.loads(line)


_NORM = (
    "def _norm(p):\n"
    "    import os\n"
    "    try:\n"
    "        return os.path.normcase(os.path.abspath(p))\n"
    "    except Exception:\n"
    "        return str(p)\n"
)

_SNIPPET_APP = (
    "import json, os, sys, importlib\n"
    + _NORM
    + "import app\n"
    "APP = os.path.dirname(os.path.abspath(app.__file__))\n"
    "out = {\n"
    "    'ok': True,\n"
    "    'app_dir': APP,\n"
    "    'in_syspath': any(_norm(p) == _norm(APP) for p in sys.path),\n"
    "    'polluted': [],\n"
    "    'errors': {},\n"
    "}\n"
    "for _n in " + repr(list(_TOP_NAMES)) + ":\n"
    "    try:\n"
    "        _m = importlib.import_module(_n)\n"
    "    except Exception as _e:\n"
    "        out['errors'][_n] = type(_e).__name__\n"
    "        continue\n"
    "    _f = getattr(_m, '__file__', None)\n"
    "    if _f and _norm(os.path.dirname(_f)) == _norm(os.path.join(APP, _n)):\n"
    "        out['polluted'].append([_n, _f])\n"
    "print('__JSON__' + json.dumps(out, ensure_ascii=False))\n"
)

_SNIPPET_CLI = (
    "import json, os, sys\n"
    + _NORM
    + "import app.agent.cli\n"
    "APP = os.path.dirname(os.path.abspath(app.__file__))\n"
    "AGENT = os.path.join(APP, 'agent')\n"
    "out = {\n"
    "    'ok': True,\n"
    "    'agent_in_syspath': any(_norm(p) == _norm(AGENT) for p in sys.path),\n"
    "    'cli_mod': 'app.agent.cli' in sys.modules,\n"
    "    'bare': [n for n in ('cli', 'tools', 'chain', 'memory', 'rag', 'cron')\n"
    "             if n in sys.modules],\n"
    "}\n"
    "print('__JSON__' + json.dumps(out, ensure_ascii=False))\n"
)


@pytest.fixture(scope="module")
def app_probe() -> dict:
    """子进程跑一次 `import app`，结果在模块内共享（避免每个用例都起解释器）。"""
    return _run_snippet(_SNIPPET_APP)


@pytest.fixture(scope="module")
def cli_probe() -> dict:
    return _run_snippet(_SNIPPET_CLI)


# ────────────────────────────────────────────────────────── 用 例


def test_app_init_has_no_syspath_mutation() -> None:
    """`app/__init__.py` 里不许出现任何 `sys.path.insert/append`（源码级，AST）。

    ★ 2026-10-14 前这里真的有一句 `_sys.path.insert(0, _app_dir)`，
      而其正上方注释写着"本目录也不必再进 sys.path"——注释与代码打架，
      代码赢，于是 `app/` 一直在 sys.path 里、11 个顶层名一直是裸名。
    """
    bad = _syspath_mutations(_APP_INIT)
    assert not bad, (
        f"`app/__init__.py` 又在改 sys.path（行号+参数）：{bad}\n"
        f"`app/` 一旦进 sys.path，`app/*` 下 11 个顶层名就成了全进程裸名，"
        f"`app.agent` 与裸名 `agent`、`app/agent/utils` 与 `app/utils` 会各加载成"
        f"两份 module 对象（事故 M-1 / D-13）")


def test_app_dir_not_in_syspath_after_import_app(app_probe: dict) -> None:
    """行为级：`import app` 之后 `app/` 不得出现在 sys.path 里。"""
    assert app_probe["ok"]
    assert not app_probe["in_syspath"], (
        f"`import app` 之后 `app/` 仍在 sys.path：{app_probe['app_dir']} —— "
        f"说明有人（很可能又是 `app/__init__.py`）把它塞回去了，M-1 风险复活")


def test_no_bare_name_resolves_into_app_dir(app_probe: dict) -> None:
    """行为级：`app/*` 的 11 个顶层目录名不得被裸名解析到 `app/` 下。

    注意：断言的不是"裸名 import 必须失败"（`utils`、`data` 可能在 site-packages
    或 backend 根有同名包，那是别人的地盘），而是**不能命中 `app/<name>`**。
    """
    assert not app_probe["polluted"], (
        f"以下裸名被解析进了 `app/` 目录（应立即定位是谁在往 sys.path 塞 `app/`）：\n"
        + "\n".join(f"  {n} -> {f}" for n, f in app_probe["polluted"]))


def test_cli_only_inserts_project_root() -> None:
    """`app/agent/cli.py` 只允许挂 `backend_api_python` 根，**不许挂 `app/agent`**。

    ★ 2026-10-14 前它把 `app/agent` 插到 sys.path[0]（注释还留着 P5 之前的旧口径）。
      实证后果：裸名 `tools` 与 `app.agent.tools` 是两个 module 对象 = M-1。
    """
    calls = _syspath_mutations(_CLI)
    for lineno, args in calls:
        # `insert` 的第一个参数是下标（`0`），`append` 没有 —— 逐一放行数字字面量，
        # 其余**必须**只有 `_project_root`。
        offenders = [a for a in args if a != "_project_root" and not a.lstrip("-").isdigit()]
        assert not offenders, (
            f"`app/agent/cli.py:{lineno}` 的 sys.path 变更参数里有 {offenders}（完整：{args}）—— "
            f"除下标外只允许 `_project_root`（backend_api_python 根）。"
            f"挂 `app/agent` 会让全包出现裸名/全名两份 module 实例（事故 M-1）")


def test_importing_cli_creates_no_bare_module_copies(cli_probe: dict) -> None:
    """行为级：`import app.agent.cli` 之后不得出现裸名副本，且 `app/agent` 不在 sys.path。"""
    assert cli_probe["ok"]
    assert cli_probe["cli_mod"], "`app.agent.cli` 没被正常导入"
    assert not cli_probe["agent_in_syspath"], (
        "`import app.agent.cli` 之后 `app/agent` 出现在 sys.path —— "
        "M-1 双实例敞口又开了")
    assert not cli_probe["bare"], (
        f"出现了裸名模块副本 {cli_probe['bare']} —— 同一份源码被以两个 module 名加载，"
        f"模块级单例双份且状态互不可见")


def test_syspath_mutation_detector_is_not_trivially_empty() -> None:
    """自检：AST 探测器必须**能**检出阳性，否则上面几条全是恒绿的假守卫。

    ★ 反直觉教训（本仓第二次）：写了守卫不等于守得住。探测器本身一旦失灵
    （比如 `ast.Call` 判断写错、`sys`/`_sys` 漏了一个别名），所有断言都会静默变绿，
    比没有守卫更危险 —— 它给的是虚假安全感。
    """
    positive = ast.parse("import sys as _sys\nsys.path.insert(0, x)\n_sys.path.append(y)\n")
    hits = []
    tmp = Path(_APP_INIT)  # 复用同一份判定逻辑：直接对 AST 走一遍
    _collect_from_ast(positive, hits)
    assert len(hits) == 2, f"AST 探测器失灵：阳性样本只检出 {hits}（应为 2 条）"

    # 阴性样本：注释里写着 sys.path.insert 不算
    negative = ast.parse("# sys.path.insert(0, 'x')\nfoo = 1\n")
    neg_hits: list = []
    _collect_from_ast(negative, neg_hits)
    assert not neg_hits, f"AST 探测器把注释当成了调用：{neg_hits}"
    assert tmp.exists()


def _collect_from_ast(tree: ast.AST, out: list) -> None:
    """`_syspath_mutations` 的 AST 判定内核（抽出来给自检用例复用，保持单一真源）。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute) or fn.attr not in {"insert", "append"}:
            continue
        owner = fn.value
        if not (isinstance(owner, ast.Attribute) and owner.attr == "path"):
            continue
        if not (isinstance(owner.value, ast.Name) and owner.value.id in {"sys", "_sys"}):
            continue
        out.append((node.lineno, [ast.unparse(a) for a in node.args]))
