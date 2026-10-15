# -*- coding: utf-8 -*-
"""mimoagent 依赖引导 —— 所有入口（cli/flask/message_queue/cron）统一从这里解析执行核。

解析顺序：
  1. 已安装的 mimoagent 包（pip install，生产推荐）
  2. 环境变量 MIMOAGENT_SRC 指向的源码目录（含 mimoagent/ 包的 src/）
  3. 相对路径候选（../mimoagent/src、<仓库根>/mimoagent/src 等常见放置位置）
  4. 都没有 → 报错并给出确切安装命令（不再抛裸 ModuleNotFoundError）

mimoagent 取码注意：XiaomiMiMo/mimoagent 的 **mimo-oss 分支**才是完整框架
（main 是上游 miniswe-agent 基线）。

★ 本模块还负责一件「第三方朝 stdout 输出」的收口（契约：stdout = 结果通道）：
  mimoagent 日志 handler 收口 stderr（下方 ensure_console_on_stderr）——
  RichHandler 的 Console 回落 stdout。必须放在**所有入口的共享引导**才覆盖得住：
  放在 cli.py 时 `scripts.exp_grind` 这类入口实测复现过污染。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 2026-10-15：mimoagent 已重装为纯净上游版（patches/ 只打 Windows 补丁），其
# `models/openai_chat.py::_debug_dump_messages` / QD_LLM_DEBUG 调试转储
# （历史调试产物：裸 print 摘要 + CWD 落 llm_inputs.log）已从包内根除 ——
# 原「默认关」策略随之撤销，无需也不应再设该环境变量。

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

# ★ 第三方日志收口（2026-10-15）：mimoagent 的 `utils/log.py` 在 **import 时**
#   给 `"mimoagent"` logger 挂一个 RichHandler —— rich 的 Console 未显式指定
#   流时回落 `sys.stdout`，且该 logger `propagate=False`（记录上不到 root，
#   root 的 stderr handler 也接不住）⇒ 子 logger（environments.local /
#   tools.ripgrep 等）的 INFO 全打 stdout。实测 exp_grind 真跑 stdout 混入
#   三条「PATH 前置」INFO，管道消费方直接不可解析。
#   `app.agent.log` 的 `ensure_console_on_stderr()` 正是干这个的，但它只在自身
#   import 时扫一次已存在的 handler；mimoagent 的 handler 是**这里解析执行核
#   时才挂上的**，那次扫描永远看不到它 ⇒ 在共享引导层补一次（覆盖每一个入口，
#   不赌 import 顺序）。幂等，可安全重入。
from app.agent.log import ensure_console_on_stderr as _ensure_console_on_stderr

_ensure_console_on_stderr()
