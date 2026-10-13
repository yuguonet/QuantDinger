# -*- coding: utf-8 -*-
"""工具结果序列化 —— 一切以「最省 token 且 LLM 可读」为目标。

★ 来源：从 `tools/_base.py`（1025 行）拆出（2026-10-13，P4）。
  这里全是**纯函数**，零内部依赖（只 import json），是拆分的叶子模块。

★ 核心决策（别改，实测过）
  - 含「表」的部分一律压成 **TSV**（list[dict] / dict-of-records）：
    表头一次性给列名、每行只放值，无引号/括号/重复键名，token 黑洞主因就在这里。
  - 纯键值嵌套保留**紧凑 JSON**：实测那里 TSV 反而更膨胀。
  - LLM 对格式无感，只认「键名 → 值」；命中率由「所有非空叶子值都可通过键路径
    找回、绝不丢有效信息」保证（仅 null/空容器这种无信息量内容被剥离）。

★ 易错点
  - 单元格内的制表符/换行必须清洗成空格，否则 TSV 列错位、模型读串列。
  - `_strip_nulls` 同时承担「截断超长列表」职责（防病态万行 dump 撑爆上下文），
    不是纯粹的清洗函数——改名/挪走时别把它拆开。
"""
from __future__ import annotations

import json
from typing import Any


def _scalar(v) -> str:
    """单值 → 字符串（float 去无意义尾零，bool 转 true/false）。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if v == int(v):
            return str(int(v))
        return repr(v)
    return str(v)


def _scalar_list(v: list) -> str:
    """标量列表 → 紧凑 JSON（体量小且明确）。"""
    return "[" + ", ".join(_scalar(x) for x in v) + "]"


def _is_leaf_dict(d: dict) -> bool:
    """dict 的值是否都不含 dict / list-of-dict（即可直接进 TSV 表头）。"""
    for vv in d.values():
        if isinstance(vv, dict):
            return False
        if isinstance(vv, list) and vv and isinstance(vv[0], dict):
            return False
    return True


def _is_direct_record(v) -> bool:
    """v 是否可在当前层直接展成 TSV：list[dict] / 扁平 dict-of-dicts / dict-of-list[dict]。"""
    if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
        return True
    if isinstance(v, dict) and v and all(isinstance(x, dict) for x in v.values()):
        return all(_is_leaf_dict(mv) for mv in v.values())
    if isinstance(v, dict) and v and all(
            isinstance(x, list) and x and all(isinstance(y, dict) for y in x)
            for x in v.values()):
        return True
    return False


def _to_tsv(items: list) -> str:
    """list[dict]（或已展平的带首列 list[dict]）→ TSV。

    表头一次性给列名，每行只放值，无引号/括号/重复键名，最省 token；
    单元格内的制表符/换行清洗掉，避免错位。
    """
    headers = list(dict.fromkeys(k for d in items for k in d))
    lines = ["\t".join(headers)]
    for item in items:
        cells = []
        for h in headers:
            cell = item.get(h, "")
            if isinstance(cell, (dict, list)):
                cell = json.dumps(cell, ensure_ascii=False, default=str)
            else:
                cell = _scalar(cell)
            cells.append(str(cell).replace("\t", " ").replace("\n", " ").replace("\r", " "))
        lines.append("\t".join(cells))
    return "\n".join(lines)


def _json(d) -> str:
    """纯键值嵌套（无表）→ 紧凑 JSON（实测比点路径扁平化更省 token）。"""
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"), default=str)


def _has_record(obj) -> bool:
    """结构里是否藏着「表」（list[dict] / 扁平 dict-of-dicts / dict-of-list[dict]）。"""
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
        return True
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _is_direct_record(v) or _has_record(v):
                return True
    elif isinstance(obj, list):
        for x in obj:
            if _has_record(x):
                return True
    return False


def _flatten_mixed(d: dict) -> str:
    """当前层无直接「表」但子树藏表 → 逐键展开：标量走 `- k: v`，嵌套 dict 递归
    （纯嵌套→JSON、含表→TSV），直接「表」就地展 TSV，保证命中率。
    """
    lines = []
    for k, v in d.items():
        if _is_direct_record(v):
            if isinstance(v, list):                      # list[dict]
                lines.append(f"# {k}")
                lines.append(_to_tsv(v))
            elif isinstance(v, dict) and all(isinstance(x, dict) for x in v.values()):
                lines.append(f"# {k}")
                rows = [{**{"_key": mk}, **mv} for mk, mv in v.items()]
                lines.append(_to_tsv(rows))
            else:                                        # dict-of-list[dict]
                for mk, lst in v.items():
                    lines.append(f"# {k}.{mk}")
                    lines.append(_to_tsv(lst))
        elif isinstance(v, dict) and v:
            lines.append(_serialize(v))
        elif isinstance(v, list):
            lines.append(f"- {k}: {_scalar_list(v)}")
        else:
            lines.append(f"- {k}: {_scalar(v)}")
    return "\n".join(lines)


def _serialize(obj) -> str:
    """递归把任意 JSON-able 结构压成 TSV / 键值文本 / 紧凑 JSON。

    决策：含「表」的部分一律 TSV（token 黑洞主因）；纯键值嵌套保留紧凑 JSON
    （实测 TSV 反而更膨胀）。LLM 对格式无感，命中率由「非空叶子值全部可找回」保证。
    """
    if isinstance(obj, dict):
        return _serialize_dict(obj)
    if isinstance(obj, list):
        return _serialize_list(obj)
    return _scalar(obj)


def _serialize_dict(d: dict) -> str:
    record_keys = [k for k, v in d.items() if _is_direct_record(v)]
    if record_keys:
        blocks = []
        meta = {k: v for k, v in d.items() if k not in record_keys}
        if meta:
            blocks.append(_serialize(meta))
        for k in record_keys:
            v = d[k]
            if isinstance(v, list):                      # list[dict]
                blocks.append(_to_tsv(v))
            elif isinstance(v, dict) and all(isinstance(x, dict) for x in v.values()):
                rows = [{**{"_key": mk}, **mv} for mk, mv in v.items()]  # 扁平 dict-of-dicts → 首列键
                blocks.append(_to_tsv(rows))
            else:                                        # dict-of-list[dict]
                for mk, lst in v.items():
                    blocks.append(f"# {mk}")
                    blocks.append(_to_tsv(lst))
        return "\n".join(blocks)
    # 当前层无直接「表」
    if _has_record(d):
        return _flatten_mixed(d)        # 表藏在更深 → 展开到表层
    return _json(d)                     # 纯嵌套、无表 → 紧凑 JSON（最省）


def _serialize_list(lst: list) -> str:
    if not lst:
        return "[]"
    if all(isinstance(x, dict) for x in lst):
        return _to_tsv(lst)
    if any(isinstance(x, dict) for x in lst):
        headers = list(dict.fromkeys(k for x in lst if isinstance(x, dict) for k in x))
        lines = ["\t".join(headers)]
        for i, x in enumerate(lst):
            if isinstance(x, dict):
                lines.append("\t".join(str(x.get(h, "")).replace("\t", " ").replace("\n", " ")
                                      for h in headers))
            else:
                lines.append(f"#[{i}] {_scalar(x)}")
        return "\n".join(lines)
    return _scalar_list(lst)


# 单列表最多保留条数，超出截断（防病态万行 dump 撑爆上下文）；正常数据（≤此值）不受影响。
_MAX_OBSERVATION_ROWS = 300


def _strip_nulls(obj: Any, _depth: int = 0) -> Any:
    """递归去除 null/空值（None、""、[]、{}），并截断超长列表。

    目标：削掉 fund_flow 等工具结果里占 60%+ 的 null 字段与无用空容器，
    同时把列表控制在 _MAX_OBSERVATION_ROWS 内，省 token 且不丢失有效信息。
    LLM 读取 JSON 不受缩进/空白影响，识别度不变。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            sv = _strip_nulls(v, _depth + 1)
            if sv is None or sv == "" or sv == [] or sv == {}:
                continue
            out[k] = sv
        return out
    if isinstance(obj, list):
        items = [_strip_nulls(x, _depth + 1) for x in obj]
        items = [x for x in items if not (x is None or x == "" or x == [] or x == {})]
        if len(items) > _MAX_OBSERVATION_ROWS:
            items = items[:_MAX_OBSERVATION_ROWS]
        return items
    return obj
