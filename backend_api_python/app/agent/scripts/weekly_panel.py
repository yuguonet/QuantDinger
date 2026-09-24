# -*- coding: utf-8 -*-
"""
周报计数器面板聚合（提智方案 阶段 0.9，原则 7：校验环必须自证在工作）。

读取 traces/panel.jsonl（每 run 一行 {trace_id, panel:{...}}），按时间窗口聚合，
输出各计数器的总量与「校验环流量是否为 0」的健康判定。

用法：
    cd backend_api_python
    python -m app.agent.scripts.weekly_panel                 # 默认近 7 天
    python -m app.agent.scripts.weekly_panel --days 1
    python -m app.agent.scripts.weekly_panel --file traces/panel.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
_AGENT_DIR = _HERE.parent.parent            # scripts/ → agent/
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from utils.budget import empty_panel, PANEL_KEYS, render_panel  # noqa: E402


def _panel_path(arg: str = "") -> Path:
    raw = arg or os.getenv("AGENT_PANEL_FILE", "traces/panel.jsonl")
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (_AGENT_DIR / p)


def load_records(path: Path, since_ms: int = 0) -> list:
    out = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if since_ms and int(rec.get("finished_at_ms") or 0) < since_ms:
                continue
            out.append(rec)
    return out


def aggregate(records: list) -> dict:
    total = empty_panel()
    total["runs"] = 0
    for rec in records:
        total["runs"] += 1
        pl = rec.get("panel") or {}
        for k in PANEL_KEYS:
            if k == "runs":
                continue
            total[k] = int(total.get(k, 0)) + int(pl.get(k, 0) or 0)
    return total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--file", default="")
    args = ap.parse_args(argv)

    path = _panel_path(args.file)
    since_ms = int((time.time() - args.days * 86400) * 1000) if args.days > 0 else 0
    recs = load_records(path, since_ms)
    panel = aggregate(recs)

    print("面板文件: %s" % path)
    print("窗口: 近 %.1f 天   记录数(run): %d" % (args.days, len(recs)))
    print(render_panel(panel))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
