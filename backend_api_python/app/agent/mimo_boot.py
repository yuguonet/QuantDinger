# -*- coding: utf-8 -*-
"""mimoagent 依赖引导 —— 所有入口（cli/flask/message_queue/cron）统一从这里解析执行核。

解析顺序：
  1. 已安装的 mimoagent 包（pip install，生产推荐）
  2. 环境变量 MIMOAGENT_SRC 指向的源码目录（含 mimoagent/ 包的 src/）
  3. 相对路径候选（../mimoagent/src、<仓库根>/mimoagent/src 等常见放置位置）
  4. 都没有 → 报错并给出确切安装命令（不再抛裸 ModuleNotFoundError）

mimoagent 取码注意：XiaomiMiMo/mimoagent 的 **mimo-oss 分支**才是完整框架
（main 是上游 miniswe-agent 基线）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_INSTALL_HINT = """\
未找到 mimoagent（QuantDinger agent 执行核）。请任选其一：

  A. 直接安装（推荐，无需 git；要求 Python 3.12.x）：
     pip install "mimoagent @ https://github.com/XiaomiMiMo/mimoagent/archive/refs/heads/mimo-oss.tar.gz"

  B. git 方式：
     pip install "mimoagent @ git+https://github.com/XiaomiMiMo/mimoagent.git@mimo-oss"

  C. 源码方式（不安装包）：克隆 mimo-oss 分支后把 src/ 路径给环境变量：
     set MIMOAGENT_SRC=D:\\path\\to\\mimoagent\\src      (Windows)
     export MIMOAGENT_SRC=/path/to/mimoagent/src        (Linux/macOS)

注意：分支是 mimo-oss（main 是上游基线，缺 QDAgent 所需的完整框架）。
"""


def ensure_mimoagent() -> None:
    try:
        import mimoagent  # noqa: F401
        return
    except ImportError:
        pass

    candidates: list[Path] = []
    env_src = os.getenv("MIMOAGENT_SRC", "").strip()
    if env_src:
        candidates.append(Path(env_src))
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidates.append(parent / "mimoagent" / "src")
        candidates.append(parent / "mimoagent-mimo-oss" / "src")

    for cand in candidates:
        if (cand / "mimoagent" / "__init__.py").is_file():
            sys.path.insert(0, str(cand))
            try:
                import mimoagent  # noqa: F401
                return
            except ImportError:
                sys.path.remove(str(cand))
                continue

    raise RuntimeError(_INSTALL_HINT)


ensure_mimoagent()
