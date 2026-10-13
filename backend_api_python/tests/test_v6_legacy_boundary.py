# -*- coding: utf-8 -*-
"""阶段 K 边界守卫：旧执行系统迁到 `app/agent/` 之后不能悄悄退化。

2026-10-13：旧 `app/agent/` 整体改名为 `app/agent/`，给新版 `app/agent/` 让位。
改名当时踩到的真实失败（本文件就是它们的回归网）：

  1. tests 里 12 处 `app.agent.*` **点号导入**没跟着改 → ImportError
  2. `test_skill_run_exits.py` / `test_agent_attr_namespace.py` 用 **Path 拼接**
     （`ROOT / "app" / "agent" / ...`）硬编码路径 → 上面的点号 sed 扫不到 →
     FileNotFoundError。**这是最隐蔽的一类，改名类改动必查 Path 拼接。**
  3. 注释里的 `app/agent/...` 路径引用会误导后来人

守卫范围（本文件只管「旧系统还活着且路径正确」；
新版隔离红线——零 sys.path 注入 / 零裸名导入 / 零 agent 引用——见
`tests/test_v6_isolation.py`，骨架落地时一并建立）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
_APP_DIR = _BACKEND / "app"
_LEGACY_DIR = _APP_DIR / "agent"

# 旧版标志性文件：出现在新版目录里就说明"改名被回退"或"旧系统被复制过去了"。
# 取名要够独特 —— 用 qd_agent.py / flask_app.py 这种旧版独有的名字，
# 不能用 tools/ 这种新旧都有的目录名（会误伤新版自己的工具层）。
_LEGACY_MARKERS = (
    "qd_agent.py", "qd_service.py", "flask_app.py",
    "mimo_boot.py", "agent.py", "cli.py", "trace_collector.py",
)


def test_legacy_directory_exists() -> None:
    """旧系统目录必须在 app 包内、名字必须是 agent。

    ★ 2026-10-13 追加：新版骨架曾建在 `app/agent/`，两者**并存**，
      所以"app/agent 不得存在"这条旧断言已过时。改为守另一件事：
      新版目录里**不许出现旧版标志性文件** —— 一旦有人把旧系统复制回
      app/agent（或者改名被回退一半），这里立刻红。

    ★ 2026-10-13 晚·P6 定稿：名字已归位。旧版现代化后**收回** `app/agent`，
      新版骨架迁到中立层 `app/agentcore/`。所以"新版目录"= agentcore。
    """
    assert _LEGACY_DIR.is_dir(), f"旧执行系统目录缺失：{_LEGACY_DIR}"

    # ★ P6 改名后的关键修正：新版骨架已迁到 app/agentcore/。
    #   机械替换会把 `new_dir` 也写成 app/agent —— 与 _LEGACY_DIR 同一个目录，
    #   于是"新版目录里出现旧版标志性文件"变成**恒真**，这条守卫从"防回退"
    #   退化成"每次必红"的噪音。新版目录必须显式指 agentcore。
    new_dir = _APP_DIR / "agentcore"
    if not new_dir.is_dir():
        # ★ 2026-10-13·P16：新版骨架已退役，目录归档到 del/agentcore_retired_20261013/。
        #   原写 `if not new_dir.is_dir(): return` ⇒ 目录一删就**静默 return**，
        #   变成"看起来在跑、其实什么都没守"的假守卫（比没有更危险：给虚假安全感）。
        #   改为正向确认终态：目录应**确实不存在**；若哪天新版目录重建，
        #   本文件需同步恢复上面那段"新旧不得混目录"的守卫。
        assert not new_dir.exists(), (
            f"app/agentcore 已于 P16 退役归档，不应再存在；若是有意重建新版骨架，"
            f"请同步恢复本函数中『新版目录里不得出现旧版标志性文件』的守卫")
        return
    found = [m for m in _LEGACY_MARKERS if (new_dir / m).exists()]
    assert not found, (
        f"app/agentcore/ 里出现了旧版标志性文件 {found} —— "
        f"旧系统住在 app/agent/、新版住在 app/agentcore/，新旧不得混在同一目录")


def test_app_init_does_not_inject_legacy_into_syspath() -> None:
    """【2026-10-13·P5 改语义】`app/__init__.py` **不得**把 legacy 目录塞进 sys.path。

    改之前守的是反向命题（`_agent_dir` 必须指向 agent）—— 那时 legacy
    包内全用裸名导入，不注入就整体瘫痪。P5 之后包内改为相对导入、包外改为绝对
    导入，注入不但不必要，还是事故 M-1（同一模块裸名/全名两份实例）的成因。
    所以现在守的是「不许再注入」。
    """
    src = (_APP_DIR / "__init__.py").read_text(encoding="utf-8")
    body = src.split('"""', 2)[-1]
    assert "_agent_dir" not in body, (
        "`app/__init__.py` 又在往 sys.path 里塞 legacy 目录 —— "
        "包内已改为相对导入，注入只会重新引入裸名/全名双实例（事故 M-1）")
    assert '"agent"' not in body, (
        "`app/__init__.py` 里出现了 `\"agent\"` 字符串常量 —— "
        "legacy 目录不应该再出现在路径注入代码里")

    # 直接扫源码文本而不是读模块属性：这类"漏改一处就全站瘫痪"的常量，
    # 只有文本扫描能在 import 之前就发现问题。


def test_legacy_absolute_imports_resolve() -> None:
    """【2026-10-13·P5 改语义】包外对 legacy 的**绝对导入**必须全部解析得到。

    改之前守的是「裸名导入仍然成立」（靠 sys.path 注入）；P5 之后包外统一改成
    `app.agent.*` 绝对导入，注入已移除。这些是分布在 `app/routes/*`、
    `app/__init__.py`、`app/market_cn/auto/*` 的生产依赖，任一断裂都是大病。
    """
    from app.agent.flask_app import agent_v2_bp                 # /api/agent/* 全站路由
    from app.agent import cron as cron_pkg     # 包本体可达
    import app.agent.cron.cron_worker                            # 定时任务调度器
    from app.agent.tools.finance import screener_tools           # 前端选股 API
    from app.agent.chain.evaluator import start_eval_worker      # 盘后评估 worker
    # 【2026-10-13 P1】原来是 `from llm import create_llm`。旧自建 llm/ 包已退役，
    # agent 侧与 market_cn 侧统一走 `app.agent.llm`（P1 建的统一出口）。
    from app.agent.skills_adapter import QDSkillAdapter

    assert agent_v2_bp.name == "agent_v2"
    assert callable(start_eval_worker)
    assert callable(QDSkillAdapter)
    assert hasattr(screener_tools, "search_stocks")
    assert hasattr(app.agent.cron.cron_worker, "start_cron_worker")
    assert cron_pkg is not None


def test_legacy_reachable_by_full_package_name() -> None:
    """全名导入路径 `app.agent.*` 必须通（tests/ 全走这条路）。

    ★ 2026-10-13·P5 加强：改造**前**裸名 `chain.c3p` 与全名
    `app.agent.chain.c3p` 是两个 module 对象（事故 M-1，单例双份）。
    改造后裸名根本不应存在，所以这里现在断言「不存在裸名副本」——
    这是 M-1 根治的直接证据，不再是"已知且已记录的设计后果"。
    """
    mod = __import__("app.agent.chain", fromlist=["c3p"])
    assert mod is not None
    assert mod.__name__ == "app.agent.chain", mod.__name__
    # 裸名副本一旦出现即说明 legacy 目录又被塞回了 sys.path
    assert "chain" not in sys.modules, \
        "出现裸名 `chain` 模块副本 ⇒ sys.path 注入回潮 ⇒ M-1 双实例风险"
