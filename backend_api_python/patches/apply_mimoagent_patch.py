# -*- coding: utf-8 -*-
"""mimoagent Windows 兼容补丁 —— 标准 patch 流程的重放工具。

补丁文件：patches/mimoagent_windows.patch（diff -u 生成，-p0 直达 site-packages）
基线来源：patches/mimo_orig/（mimoagent 0.1.0 原始四文件：
          paths.py / local.py / ripgrep.py / prompts.py）
          ★ 2026-10-14：随 `app/agent/tmp/` 移除，基线快照迁到 patches/ 下
          （它是补丁的"改前样本"，与补丁文件同住才可重新生成）。

用法（在仓库任意位置运行，路径自动解析）：
  python patches/apply_mimoagent_patch.py            # 等同 --check
  python patches/apply_mimoagent_patch.py --check    # 校验补丁是否已在位
  python patches/apply_mimoagent_patch.py --apply    # 应用补丁（幂等：已在位则跳过）

判定逻辑（patch --dry-run，不落盘先探路）：
  正向 dry-run 成功        → 补丁未打，可应用（--apply 时真正执行）
  反向 dry-run 成功(-R)    → 补丁已在位（幂等跳过）
  两者都失败               → 基线漂移（mimoagent 被升级/改动），报错并提示重新生成

依赖：patch 命令（Git for Windows 的 usr/bin/patch.exe，随 PATH 可得）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
PATCH_FILE = os.path.join(_HERE, "mimoagent_windows.patch")


def _site_packages() -> str:
    """site-packages 根：优先从已安装的 mimoagent 反查，兜底当前 venv。"""
    try:
        import mimoagent
        return os.path.dirname(os.path.dirname(os.path.abspath(mimoagent.__file__)))
    except Exception:
        import sysconfig
        return sysconfig.get_paths()["purelib"]


def _patch_exe() -> str:
    exe = shutil.which("patch")
    if not exe:
        raise SystemExit(
            "找不到 patch 命令。请确认已安装 Git for Windows 且其 usr/bin 在 PATH。")
    return exe


def _run(patch_exe: str, args: list, data: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [patch_exe, *args], input=data, capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=_site_packages(),
    )


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"
    if mode not in ("--check", "--apply"):
        raise SystemExit(__doc__)
    with open(PATCH_FILE, encoding="utf-8") as f:
        data = f.read()
    patch_exe = _patch_exe()
    sp = _site_packages()

    fwd = _run(patch_exe, ["-p0", "--dry-run", "-f"], data)   # -f：忽略已应用残留
    rev = _run(patch_exe, ["-p0", "--dry-run", "-R"], data)

    if fwd.returncode == 0:
        if mode == "--check":
            print("NOT APPLIED：补丁未打在 %s（可运行 --apply）" % sp)
            return 2
        out = _run(patch_exe, ["-p0"], data)
        if out.returncode != 0:
            print(out.stdout); print(out.stderr)
            return 1
        print("APPLIED：补丁已应用到 %s" % sp)
        print(out.stdout.strip())
        return 0

    if rev.returncode == 0:
        print("IN PLACE：补丁已在位（%s）" % sp)
        return 0

    print("CONFLICT：正/反向 dry-run 均失败 —— mimoagent 基线已漂移（升级/改动？）")
    print("--- forward dry-run ---"); print(fwd.stdout); print(fwd.stderr)
    print("--- reverse dry-run ---"); print(rev.stdout); print(rev.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
