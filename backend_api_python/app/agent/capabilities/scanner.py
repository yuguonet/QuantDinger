# -*- coding: utf-8 -*-
"""capabilities/scanner.py — 能力发现扫描器（A 阶段, 2026-09-12）

用途:
  对"显式指定"的目标模块做公开函数盘点并分类，产出能力清单报告（JSON+Markdown
  到 tmp/），供人工过目后固化进 admission.json；运行期由 loader 读取准入清单
  注册为 agent 工具。auto/ 为第一个接入域。

设计要点:
  - 发现范围闸门（三层闸门之一）: 只扫 SCAN_TARGETS 里的显式模块;
  - 公开函数 = 非下划线开头且定义于本模块（re-export 由源模块负责盘点）;
    项目代码约定"内部函数下划线开头"，公开面即天然白名单;
  - 分类仅为建议: 命中 WRITE_PREFIXES（写操作）→ 排除; 其余 → 待审候选。
    硬性准入由 admission.json 决定，本扫描器绝不写准入配置;
  - 三态标注: 已准入(admitted) / 待审(pending) / 写排除(excluded)，
    与 admission.json 对照 → 新函数自动浮现为待审;
  - 纯静态盘点：只做 import+inspect，不触网络/DB。

易错点:
  - 不同模块可能有同名函数（如 hub.all_codes）→ 报告以 "module:name" 为唯一键；
    loader 注册时同名冲突先到先得并告警;
  - inspect.signature 对个别 callable 可能抛异常 → 逐函数兜底 "(?)"。

用法（命令行）:
  <python> app/agent/capabilities/scanner.py [--out-dir <dir>]
  默认输出到 <repo>/tmp/capability_scan_<ts>.{json,md}
"""
from __future__ import annotations

import importlib
import inspect
import json
import sys
from datetime import datetime
from pathlib import Path


def _bootstrap_path():
    """独立运行时把 backend_api_python 与 app/agent 挂上 sys.path（幂等）。"""
    base = Path(__file__).resolve().parents[3]   # backend_api_python
    agent_dir = Path(__file__).resolve().parents[1]  # app/agent
    for p in (str(base), str(agent_dir)):
        if p not in sys.path:
            sys.path.insert(0, p)


_bootstrap_path()

# ── 显式扫描范围（第一域: auto/ 数据与信号接口层）──
SCAN_TARGETS = [
    "app.market_cn.auto.data.hub",     # auto 唯一数据出口: 日线/分钟/快照/指数/龙虎榜…
    "app.market_cn.auto.store",        # 信号事实表查询与状态机（只读子集）
    "app.market_cn.auto.registry",     # 策略注册表元数据（key/label）
]

# 写操作前缀（硬性排除：扫描标 excluded，loader 注册时二次复核）
WRITE_PREFIXES = (
    "set_", "update_", "delete_", "remove_", "insert_", "upsert_", "create_",
    "drop_", "clear_", "reset_", "save_", "write_", "run_", "start_", "stop_",
    "trigger_", "schedule_", "sync_", "cleanup_", "ensure_", "reconcile_",
    "emit_", "commit_", "apply_", "patch_", "migrate_", "purge_",
)


def is_write_name(name: str) -> bool:
    return name.startswith(WRITE_PREFIXES)


def _tmp_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "tmp"


def _admission_keys():
    """读取 admission.json 的已准入键集合（缺失/损坏 → 空集）。"""
    try:
        p = Path(__file__).resolve().parent / "admission.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        return {
            f"{e.get('module')}:{e.get('name')}"
            for e in (data.get("entries") or [])
            if e.get("admitted")
        }
    except Exception:
        return set()


def scan(targets=None):
    """盘点目标模块公开函数。返回 manifest dict（不落盘）。"""
    targets = list(targets or SCAN_TARGETS)
    admitted = _admission_keys()
    functions = []
    for mod_path in targets:
        try:
            mod = importlib.import_module(mod_path)
        except Exception as e:
            functions.append({"module": mod_path, "error": f"import failed: {e}"})
            continue
        for name, obj in inspect.getmembers(mod, inspect.isfunction):
            if name.startswith("_"):
                continue
            if getattr(obj, "__module__", "") != mod.__name__:
                continue  # re-export 由源模块盘点
            key = f"{mod_path}:{name}"
            try:
                sig = str(inspect.signature(obj))
            except Exception:
                sig = "(?)"
            doc = inspect.getdoc(obj) or ""
            functions.append({
                "module": mod_path,
                "name": name,
                "key": key,
                "signature": sig,
                "doc_first": (doc.splitlines()[0] if doc else "").strip()[:110],
                "has_doc": bool(doc),
                "category": "excluded" if is_write_name(name) else "candidate",
                "status": ("admitted" if key in admitted
                           else "excluded" if is_write_name(name)
                           else "pending"),
            })
    functions.sort(key=lambda r: (r.get("module", ""), r.get("name", "")))
    counts = {
        "candidates": sum(1 for f in functions if f.get("category") == "candidate"),
        "excluded": sum(1 for f in functions if f.get("category") == "excluded"),
        "admitted": sum(1 for f in functions if f.get("status") == "admitted"),
        "pending": sum(1 for f in functions if f.get("status") == "pending"),
    }
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "targets": targets,
        "counts": counts,
        "functions": functions,
    }


def render_markdown(m):
    lines = [
        f"# 能力扫描报告（{m.get('generated_at', '')}）",
        "",
        f"- 目标模块 {len(m['targets'])} 个 · 函数 {len(m['functions'])} 个",
        f"- 待审候选 {m['counts']['pending']} · 已准入 {m['counts']['admitted']}"
        f" · 写排除 {m['counts']['excluded']}",
        "",
        "## 待审候选（只读建议，人工过目后进 admission.json）",
        "",
        "| module | function | 签名 | 文档首行 |",
        "|---|---|---|---|",
    ]
    for f in m["functions"]:
        if f.get("status") != "pending":
            continue
        lines.append(
            f"| {f['module'].split('.')[-1]} | {f['name']} "
            f"| `{f['signature']}` | {f['doc_first']} |"
        )
    lines += ["", "## 写操作排除（硬性不接受）", "",
              "| module | function | 原因 |", "|---|---|---|"]
    for f in m["functions"]:
        if f.get("category") != "excluded":
            continue
        lines.append(f"| {f['module'].split('.')[-1]} | {f['name']} | 写操作前缀 |")
    if any(f.get("status") == "admitted" for f in m["functions"]):
        lines += ["", "## 已准入", ""]
        for f in m["functions"]:
            if f.get("status") == "admitted":
                lines.append(f"- {f['key']}")
    lines.append("")
    return "\n".join(lines)


def main():
    import argparse

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # dotenv 可选加载（个别模块导入期读 env）
    try:
        from dotenv import load_dotenv
        load_dotenv(str(Path(__file__).resolve().parents[3] / ".env"), override=False)
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="能力发现扫描器")
    ap.add_argument("--out-dir", default=str(_tmp_dir()))
    args = ap.parse_args()

    m = scan()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    jp = out_dir / f"capability_scan_{ts}.json"
    mp = out_dir / f"capability_scan_{ts}.md"
    jp.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    mp.write_text(render_markdown(m), encoding="utf-8")

    print(f"[scan] targets={len(m['targets'])} functions={len(m['functions'])} "
          f"pending={m['counts']['pending']} admitted={m['counts']['admitted']} "
          f"excluded={m['counts']['excluded']}")
    for f in m["functions"]:
        if "error" in f:
            print(f"  [ERR] {f['module']}: {f['error']}")
    print(f"[scan] json -> {jp}")
    print(f"[scan] md   -> {mp}")


if __name__ == "__main__":
    main()
