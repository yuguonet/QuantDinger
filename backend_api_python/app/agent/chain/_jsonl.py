# -*- coding: utf-8 -*-
"""_jsonl — chain 三处 JSONL 读取的统一实现（2026-10-22 冗余收敛）。

## 为什么抽这个

`aggregator._read` / `collector._read` / `evidence._read` 三份实现逐字同型
（10-19 审计 4 组 curated 重复之一）：JSONL 是 v5 采集/证据/配对的落盘格式，
「读整个文件、坏行跳过、文件不存在当空」是共同契约。三处各写一遍 ⇒ 修契约
（如换 errors 策略）要改三处，正是「同一语义两套记账」的温床。

## 契约（改这里必须三处同收益，且不得 silently 改变行为）

- 文件不存在 ⇒ `[]`（调用方按「今天没采」处理，不是错误）；
- 空行/空白行跳过；
- 单行 JSON 解析失败 ⇒ **跳过该行**，不抛（坏行不毁掉整个文件——
  C 类数据不可重建，能读多少读多少）；
- 返回 `List[dict]`；非 dict 的行（如 JSON 标量）也会被 json.loads 接受，
  由调用方按需过滤（历史行为一致，不在此加严）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """读 JSONL 文件为 dict 列表（契约见模块 docstring）。"""
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            out.append(json.loads(s))
        except (ValueError, TypeError):
            continue
    return out
