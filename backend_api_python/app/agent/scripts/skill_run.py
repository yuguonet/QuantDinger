# -*- coding: utf-8 -*-
"""skill_run.py —— 执行型技能的统一子进程入口（差距 B，2026-10-02）。

供 run_skill 工具 / task 子 agent / 人工跑批共用。子进程形态的意义：
  1. 超时可击杀（长流水线跑飞不拖死主 agent）；
  2. 崩溃隔离（技能代码异常不污染主进程）；
  3. 产物天然落文件（大结果不必挤进上下文）。

用法：
    python scripts/skill_run.py market_screener
    python scripts/skill_run.py strategy_debug '{"fn": "debug_strategy", "kwargs": {"strategy": "break", "code": "600519"}}'

输出（stdout，单行 JSON）：
    {"name":..., "ok":bool, "result":<截断>,
     "full_path":"<backend_api_python>/tmp/skill_output/xxx.json", "elapsed_s":...}
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import time
from pathlib import Path

# 技能执行注册表：name → (模块路径, 允许调用的函数, 默认函数)
# 白名单制——run_skill 不执行任意代码，只跑这里登记过的入口。
EXECUTABLE_SKILLS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "market_screener": ("skills.market_screener.run", ("run",), "run"),
    "strategy_debug": (
        "skills.strategy_debug.run",
        ("list_strategies", "why_strategy", "debug_strategy", "doctor_auto"),
        "debug_strategy",
    ),
}

# ★ 2026-10-14 迁址：原为 `parents[1]`（= app/agent）⇒ 产物落在 `app/agent/tmp/`
#   skill_output/。随 `app/agent/tmp/` 整体移除，改到**项目级** `backend_api_python/tmp/`
#   skill_output/ —— 与项目约定「临时文件/结果/日志一律放 tmp/」一致，也让 agent 包
#   目录里不再夹带运行时产物（`app/agent` 只放代码）。
#   parents: 0=scripts 1=agent 2=app 3=backend_api_python
_OUT_DIR = Path(__file__).resolve().parents[3] / "tmp" / "skill_output"
_PREVIEW_LIMIT = 20000   # 回传主 agent 的预览上限（字符）；全量落盘不受限


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in EXECUTABLE_SKILLS:
        print(json.dumps({"ok": False, "error":
                          f"未知技能。可执行: {sorted(EXECUTABLE_SKILLS)}"}, ensure_ascii=False))
        return 2
    name = sys.argv[1]
    spec = {}
    if len(sys.argv) > 2 and sys.argv[2].strip():
        try:
            spec = json.loads(sys.argv[2])
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"arguments 不是合法 JSON: {e}"}, ensure_ascii=False))
            return 2

    mod_path, allowed, default_fn = EXECUTABLE_SKILLS[name]
    fn_name = str(spec.get("fn") or default_fn)
    if fn_name not in allowed:
        print(json.dumps({"ok": False, "error":
                          f"fn 不在白名单: {fn_name}（允许: {list(allowed)}）"}, ensure_ascii=False))
        return 2
    kwargs = spec.get("kwargs") or {}
    if not isinstance(kwargs, dict):
        print(json.dumps({"ok": False, "error": "kwargs 必须是对象"}, ensure_ascii=False))
        return 2

    t0 = time.time()
    try:
        # 只挂 backend_api_python（保证 `import app.*` 成立）。
        # 【2026-10-13·P5】原本还挂 app/agent 让 `skills.*` 裸名成立 ——
        # 改造后包内相对导入，EXECUTABLE_SKILLS 里的裸名由 modpath.resolve 补前缀，
        # 不再需要把包目录塞进 sys.path（那正是 M-1 双实例的成因）。
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        import importlib
        from app.agent.modpath import import_module
        fn = getattr(import_module(mod_path), fn_name)
        sig = inspect.signature(fn)
        unknown = set(kwargs) - set(sig.parameters)
        if unknown:
            print(json.dumps({"ok": False, "error": f"未知参数: {sorted(unknown)}"}, ensure_ascii=False))
            return 2
        result = fn(**kwargs)
    except Exception as e:
        print(json.dumps({"ok": False, "name": name,
                          "error": f"{type(e).__name__}: {e}"[:2000]}, ensure_ascii=False))
        return 1

    # 产物落盘 + 预览回传（差距 B 第 3 条：主上下文只拿结论，不搬明细）
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    full_path = _OUT_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}.json"
    try:
        full_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                             encoding="utf-8")
    except Exception:
        full_path = None

    preview = json.dumps(result, ensure_ascii=False, default=str)
    if len(preview) > _PREVIEW_LIMIT:
        preview = preview[:_PREVIEW_LIMIT] + f"\n……（结果共 {len(preview)} 字符，全量见 full_path）"
    print(json.dumps({
        "ok": True, "name": name, "fn": fn_name,
        "result": preview, "full_path": str(full_path) if full_path else "",
        "elapsed_s": round(time.time() - t0, 2),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    _rc = main()
    # 本进程是**一次性**的：结果已写盘、已回传 stdout，之后没有任何东西需要优雅收尾。
    # 走正常解释器退出会被第三方常驻线程拖死 —— 实测（2026-10-12，tmp/_screener_hang.py）：
    # market_screener 的 run() 只跑 51~58s，随后进程 >10min 不退出（TDX 心跳线程是
    # 非 daemon，threading._shutdown() 死等它），于是 run_skill 恒定表现为
    # 「超时（>600s）已中止」。流水线本身并不慢，是退出卡住制造了慢的假象。
    # 因此显式 flush 后直接 os._exit，把控制权立刻交还父进程。
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(_rc)
