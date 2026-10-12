"""run_skill 的子进程必须能**自行退出**（2026-10-12 血案守卫）。

现象：盘前 `run_skill(market_screener)` 恒定报「超时（>600s）已中止」，
看起来像「盘前全市场扫描比盘后慢 12 倍」。实测不是：
    tmp/_screener_hang.py → run() 本体只要 51~58s，随后**进程 10 分钟不退出**。
真因是退出被卡住，不是流水线慢：
    1. mootdx 取大盘日线时 `Quotes.factory(heartbeat=True)` 会起
       tdxpy.heartbeat.HeartBeatThread —— **非 daemon**，10s 周期
       stop_event.wait() 无限循环，只有 disconnect 才停；
    2. 主线程结束后 `threading._shutdown()` join 它 ⇒ 永不返回 ⇒
       subprocess.run(timeout=600) 必然超时；
    3. concurrent.futures 的池线程靠 `threading._register_atexit` 唤醒，
       而该回调排在 join **之后** ⇒ 连唤醒信号都收不到，一并卡死。

两道防线（本测试锁住，防止被后续改动悄悄还原）：
    ① mootdx_client._daemonize_heartbeat() 把心跳线程设为 daemon（根治，
       对 CLI 主进程同样有效 —— 否则主进程退出时也会挂住）；
    ② scripts/skill_run.py 末尾 os._exit —— 一次性子进程不等任何常驻线程（止血）。
"""
from __future__ import annotations

import ast
import importlib
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL_RUN = ROOT / "app" / "agent" / "scripts" / "skill_run.py"

_HEARTBEAT_CLASSES = (
    ("tdxpy.heartbeat", "HeartBeatThread"),       # mootdx 2.x
    ("pytdx.heartbeat", "HqHeartBeatThread"),     # mootdx 1.x
)


class _StubApi:
    """心跳线程构造所需的最小桩（只取 .client / .last_ack_time，不发任何网络包）。"""

    def __init__(self) -> None:
        self.client = None
        self.last_ack_time = time.time()


def test_heartbeat_threads_are_daemon():
    """① 心跳线程必须是 daemon —— 否则解释器退出时 join 死等。"""
    from app.utils import mootdx_client as mc

    mc._daemonize_heartbeat()

    found = []
    for mod_name, cls_name in _HEARTBEAT_CLASSES:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        cls = getattr(mod, cls_name, None)
        if cls is not None:
            found.append((mod_name, cls))

    if not found:
        pytest.skip("环境未安装 tdxpy/pytdx，无法验证心跳线程")

    for mod_name, cls in found:
        t = cls(_StubApi(), threading.Event())
        assert t.daemon, (
            f"{mod_name}.{cls.__name__} 仍是非 daemon 线程：进程退出时 "
            f"threading._shutdown() 会死等它，run_skill 将恒定 600s 超时。"
            f"（修法见 app/utils/mootdx_client.py::_daemonize_heartbeat）"
        )


def test_skill_run_exits_immediately_after_output():
    """② 子进程交还结果后必须 os._exit，不走会被常驻线程拖住的常规退出流程。"""
    tree = ast.parse(SKILL_RUN.read_text(encoding="utf-8"))
    main_blocks = [
        n for n in tree.body
        if isinstance(n, ast.If) and "__main__" in ast.dump(n.test)
    ]
    assert main_blocks, "skill_run.py 缺少 `if __name__ == '__main__':` 块"

    body_src = "\n".join(ast.unparse(stmt) for blk in main_blocks for stmt in blk.body)
    assert "os._exit(" in body_src, (
        "skill_run.py 的 __main__ 块必须显式 os._exit()：子进程是一次性的，"
        "走常规解释器退出会被 TDX 心跳等非 daemon 线程挂死，表现为 run_skill 600s 超时。"
    )
