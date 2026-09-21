"""core/_paths.py — 项目根锚点（唯一），取代散落的 `__file__` 层级回退。

**为什么必须有这个文件**：此前 `data/frames.py`、`data/hub.py`（缓存放 `backend_api_python/data/`）、
`backtest.py`、`present.py`（读 `backend_api_python/.env`）都用"从本文件回退 N 级"定位项目根。
这种写法的致命处是**文件一挪位置就指错目录且不报错** —— 缓存会写到别处、
`.env` 读不到（于是连不上库，报的还是"没密码"这种误导性错）。目录按架构 §10 重组后
本层整体深了一级，正是会踩中的时点，故把根锚点收敛到本模块：**层级变化只改这一处**。

锚定方式：从本文件向上找到**同时含 `app/` 与 `run.py` 的目录**（= `backend_api_python`）。
这是结构化判据，不是数层级，因此对目录深度、包重排都免疫。

易错：不要退回"回退 N 级"写法；不要在 core 之外的模块里自己再算路径。
"""

from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_AUTO = os.path.dirname(_HERE)                      # .../app/market_cn/auto


def _find_project_root() -> str:
    """向上找 `backend_api_python`（含 `app/` 且含 `run.py`）。找不到 → 回退固定层级。"""
    d = _HERE
    for _ in range(8):
        if os.path.isdir(os.path.join(d, "app")) and os.path.isfile(os.path.join(d, "run.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    # 兜底（源码树被裁剪时）：core/ → auto → market_cn → app → backend_api_python
    return os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))


#: `backend_api_python` 目录（`.env`、`data/market_cn_cache` 等都挂它下面）
PROJECT_ROOT: str = _find_project_root()
#: `auto/` 目录
AUTO_DIR: str = os.path.normpath(_AUTO)
#: 策略 YAML 目录（L2，与冻结的 .py 插件同目录）
STRATEGY_DIR: str = os.path.join(AUTO_DIR, "strategies")
#: 市场适配目录（adapters/markets/*.yaml）
MARKETS_DIR: str = os.path.join(AUTO_DIR, "adapters", "markets")
#: 后端 `.env`
ENV_FILE: str = os.path.join(PROJECT_ROOT, ".env")
#: 缓存根（不进源码树）
CACHE_ROOT: str = os.path.join(PROJECT_ROOT, "data", "market_cn_cache")


def load_env_first_found(*extra: str) -> None:
    """按序找第一个存在的 .env 并加载（幂等；应用内运行已由 app 初始化加载）。

    extra: 额外候选路径（如 os.path.join(os.getcwd(), ".env")）。找不到就静默跳过 ——
    手动跑诊断脚本时缺 .env 不是致命错误（报错留给真正的连接处）。
    """
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    for p in (ENV_FILE, *extra):
        if p and os.path.isfile(p):
            load_dotenv(p, override=False)
            return
