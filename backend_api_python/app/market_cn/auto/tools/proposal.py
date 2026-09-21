#!/usr/bin/env python3
"""auto/tools/proposal.py — LLM 建议产物 + 红线校验 + 应用 + 回滚 (M6, 2026-09-20)

定位 (架构 §8「LLM 协作」+ §12-M6「建议可审计、可回滚」):
  LLM 读 tools/explain.py 的报告后, **只产出对 `<key>.yaml` 的最小改动** (一行门 / 一个参数值),
  产物即本工具的 proposal JSON —— 天然可审计(逐条 rationale+evidence)、可回滚(改前备份)。

proposal schema (JSON):
  {
    "strategy": "dragon_callback",
    "base_sha": "<可选: 提案时 <key>.yaml 的 sha256, 用于检测过期>",
    "created": "2026-09-20T19:51",
    "rationale": "整体意图 (人读)",
    "edits": [
      {"kind": "param",        "target": "max_last_chg", "before": 3.0, "after": 2.5,
       "rationale": "...", "evidence": {"stability": {...}, "pool_check": {...}}},
      {"kind": "gate_expr",    "target": "g4", "before": "close() >= lu_close()",
       "after": "close() >= lu_close() * 1.02", "rationale": "...", "evidence": {...}},
      {"kind": "gate_enabled", "target": "g2", "before": 1, "after": 0, ...},
      {"kind": "gate_add",     "target": "g5", "after": {"name": "...", "role": "required",
                                  "needs_d0": 0, "expr": "..."}, ...},
      {"kind": "gate_del",     "target": "g3", ...}
    ]
  }

子命令:
  validate --proposal P.json [--strategy-dir DIR]   # 只校验, 不改文件; 退出码=失败项数
  apply    --proposal P.json [--apply] [--strategy-dir DIR]
                                                   # 默认 dry-run 出 diff; --apply 才落盘+备份
  revert   --strategy <key> [--to <ts>] [--strategy-dir DIR]   # 用最近(或指定)备份还原

红线三闸 (validate 硬校验, apply 前必过) —— 架构 §8 红线:
  ① 不碰 core: 改动只落在 `strategies/<key>.yaml` (路径白名单; 任何其它文件一律拒);
  ② 不绕两段稳定性: 凡改变选择集 (param/gate_expr/gate_add/gate_del/gate_enabled) 的编辑,
     必须附 evidence.stability.both_positive=true 且 evidence.pool_check.verdict=="MIGRATE";
  ③ 不删合规门: gate_del / gate_enabled(→0) 命中 role=qualify 一律拒;
  ④ (附加) as-of: gate_expr/gate_add 的新表达式过 static_asof_check, 禁正偏移(未来函数)。
  另: before 值须与当前 YAML 一致 (防过期提案误用)。

落盘机制 (刻意**文本级**改 YAML, 不用 YAML round-trip):
  单文件 YAML 的核心价值之一是"能写注释"; round-trip 会丢注释与格式。故 apply 做定点文本替换
  (按 gate id 的行跨度 / 参数行), 保注释。备份写 strategies/.history/<key>.<ts>.yaml。

易错点:
  - 本工具**不改 core**、不改 `.py` 插件、不改 config.json; 只改 strategies/<key>.yaml;
  - `--apply` 才写盘; 默认 dry-run 只打印 unified diff, 供人先审;
  - gate 为 flow 风格 (`- {id: x, ...}`, 可能跨行): 行跨度 = 含 `- {` 到首次出现 `}`;
  - 若同一参数名在文件多处出现, param 替换限定在 `params:` 段内。
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import time

from app.market_cn.auto.core._paths import STRATEGY_DIR as _DEFAULT_STRATEGY_DIR

_EDIT_KINDS = {"param", "gate_expr", "gate_enabled", "gate_add", "gate_del"}
# 改变选择集的 kind → 红线② 要求稳定性 + 信号级复验证据
_SELECTION_KINDS = {"param", "gate_expr", "gate_add", "gate_del", "gate_enabled"}


# ================================================================
# YAML 文本工具 (保注释的定点编辑)
# ================================================================

def _yaml_path(strategy, strategy_dir):
    return os.path.join(strategy_dir, f"{strategy}.yaml")


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _find_gate_span(lines, gid):
    """定位 gate `gid` 的行跨度 [start, end) —— 含 `- {` 到含 `}` 的行。

    返回 (start, end, text) 或 None。text = 该 gate 的原始文本 (含换行)。
    """
    id_re = re.compile(r"\bid\s*:\s*[\"']?" + re.escape(str(gid)) + r"[\"']?")
    for i, ln in enumerate(lines):
        if id_re.search(ln):
            # 向上找该 gate 的起始行 (含 "- {" 或 "- {...")
            start = i
            for j in range(i, -1, -1):
                if re.match(r"\s*-\s*\{", lines[j]) or re.match(r"\s*-\s*\S", lines[j]):
                    start = j
                    break
            # 向下找结束行 (首个含 "}")
            end = i + 1
            for j in range(i, len(lines)):
                if "}" in lines[j]:
                    end = j + 1
                    break
            return start, end, "".join(lines[start:end])
    return None


def _parse_gates(yaml_text):
    """解析出 gates 列表 (用 PyYAML, 只读不改文件)。"""
    import yaml
    doc = yaml.safe_load(yaml_text) or {}
    return doc, (doc.get("gates") or [])


def _current_gate(doc, gid):
    for g in (doc.get("gates") or []):
        if str(g.get("id")) == str(gid):
            return g
    return None


def _set_scalar_in_span(span_text, key, new_value):
    """在 gate 的文本块里把 `key: <val>` 的标量替换为 new_value (保持引号风格)。"""
    # 匹配 key: "..." / key: '...' / key: 123
    pat = re.compile(r"(\b" + re.escape(key) + r"\s*:\s*)(\"[^\"]*\"|'[^']*'|[^,}\s][^,}]*)")
    def _rep(m):
        old = m.group(2)
        if old.startswith('"') or old.startswith("'"):
            return f'{m.group(1)}"{new_value}"'
        return f"{m.group(1)}{new_value}"
    new_text, n = pat.subn(_rep, span_text, count=1)
    return new_text, n


def _set_param(yaml_text, name, new_value):
    """在 `params:` 段内把 `name: <val>` 的值替换 (保留行尾注释)。返回 (text, n)。"""
    lines = yaml_text.splitlines(keepends=True)
    in_params = False
    for i, ln in enumerate(lines):
        if re.match(r"^params\s*:", ln):
            in_params = True
            continue
        if in_params and re.match(r"^\S", ln):      # 顶格 → 离开 params 段
            break
        if in_params:
            m = re.match(r"^(\s*" + re.escape(str(name)) + r"\s*:\s*)([^#\n]*?)(\s*#.*)?(\n?)$", ln)
            if m:
                tail = m.group(3) or ""
                lines[i] = f"{m.group(1)}{new_value}{tail}{m.group(4) or ''}"
                return "".join(lines), 1
    return yaml_text, 0


def _apply_edits_text(yaml_text, edits):
    """按 edits 做定点文本替换。返回 (new_text, applied[], errors[])。

    不做任何结构推断之外的猜测: 每处替换都要求命中一次, 否则记 error (不静默)。
    """
    lines = yaml_text.splitlines(keepends=True)
    applied, errors = [], []
    # 先处理删除/新增对行号的影响最小: 排序为 param → gate_* ; 删除放最后
    ordered = sorted(edits, key=lambda e: {"param": 0, "gate_expr": 1, "gate_enabled": 2,
                                           "gate_add": 3, "gate_del": 4}.get(e["kind"], 9))
    text = "".join(lines)
    for e in ordered:
        k, tgt = e["kind"], e["target"]
        if k == "param":
            text, n = _set_param(text, tgt, e["after"])
            (applied if n else errors).append(e if n else f"param {tgt}: 未在 params 段命中")
        elif k in ("gate_expr", "gate_enabled"):
            cur = text.splitlines(keepends=True)
            sp = _find_gate_span(cur, tgt)
            if sp is None:
                errors.append(f"{k} {tgt}: 未找到 gate")
                continue
            s, en, block = sp
            key = "expr" if k == "gate_expr" else "enabled"
            nb, n = _set_scalar_in_span(block, key, e["after"])
            if n:
                cur[s:en] = nb.splitlines(keepends=True)
                text = "".join(cur)
                applied.append(e)
            else:
                errors.append(f"{k} {tgt}: 未命中 {key}")
        elif k == "gate_add":
            cur = text.splitlines(keepends=True)
            after = e["after"]
            gate_line = ("  - {id: %s, name: %s, role: %s, needs_d0: %s, enabled: %s, expr: %s}\n"
                         % (after.get("id", tgt), after.get("name", tgt),
                            after.get("role", "required"), int(after.get("needs_d0", 0)),
                            int(after.get("enabled", 1)), json.dumps(str(after.get("expr", "")),
                                                                    ensure_ascii=False)))
            # 插到最后一道 gate 之后 (找最后一个含 '}' 的 `- {...}` 行)
            last_end = None
            for i, ln in enumerate(cur):
                if re.match(r"\s*-\s*\{.*\}", ln):
                    last_end = i + 1
            if last_end is None:
                errors.append(f"gate_add {tgt}: 找不到 gates 列表锚点")
            else:
                cur.insert(last_end, gate_line)
                text = "".join(cur)
                applied.append(e)
        elif k == "gate_del":
            cur = text.splitlines(keepends=True)
            sp = _find_gate_span(cur, tgt)
            if sp is None:
                errors.append(f"gate_del {tgt}: 未找到 gate")
            else:
                s, en, _ = sp
                del cur[s:en]
                text = "".join(cur)
                applied.append(e)
        else:
            errors.append(f"未知 kind {k}")
    return text, applied, errors


# ================================================================
# 红线校验
# ================================================================

def _evidence_ok(e):
    """红线② 证据完整性: stability 两段全正 + pool_check MIGRATE。"""
    ev = e.get("evidence") or {}
    st = ev.get("stability") or {}
    pc = ev.get("pool_check") or {}
    return bool(st.get("both_positive") is True and str(pc.get("verdict", "")).upper() == "MIGRATE")


def validate(prop, strategy, yaml_text, strict_stale=True):
    """→ errors[] (空 = 全过)。把红线三闸 + before 一致性 + as-of 全查一遍。"""
    errors = []
    doc, gates = _parse_gates(yaml_text)

    # 红线① 不碰 core: strategy 必须对应 strategies/<key>.yaml, 且 kind 合法
    if str(prop.get("strategy", "")) != strategy:
        errors.append(f"① proposal.strategy={prop.get('strategy')!r} 与 --strategy {strategy!r} 不符")
    # base_sha 过期检测 (可选)
    if strict_stale and prop.get("base_sha"):
        cur = _sha256(yaml_text)
        if str(prop["base_sha"]) != cur:
            errors.append(f"⓪ base_sha 过期 (提案={prop['base_sha']} 现状={cur}); 请基于最新 YAML 重出提案")

    for e in (prop.get("edits") or []):
        k, tgt = e.get("kind"), e.get("target")
        if k not in _EDIT_KINDS:
            errors.append(f"① 未知 kind={k!r}")
            continue
        # ① 路径白名单: target 不得含路径分隔符/.. (只能改本策略 YAML 内的门/参数)
        if "/" in str(tgt) or "\\" in str(tgt) or ".." in str(tgt):
            errors.append(f"① target={tgt!r} 含路径字符 — 只允许改本策略 YAML 内的门/参数")
            continue

        cur_gate = _current_gate(doc, tgt)
        # ④ as-of
        if k in ("gate_expr", "gate_add"):
            expr = e.get("after") if k == "gate_expr" else (e.get("after") or {}).get("expr")
            if not expr:
                errors.append(f"④ {k} {tgt}: 缺 after.expr")
            else:
                errors += _check_asof(f"④ {k} {tgt}", str(expr))
        # before 一致性 (防过期/误用)
        if k == "param":
            cur = (doc.get("params") or {}).get(tgt)
            if tgt not in (doc.get("params") or {}):
                errors.append(f"⓪ param {tgt} 不在当前 params 中")
            elif strict_stale and cur != e.get("before"):
                errors.append(f"⓪ param {tgt} before={e.get('before')!r} ≠ 现状 {cur!r}")
        elif k == "gate_expr":
            if cur_gate is None:
                errors.append(f"⓪ gate {tgt} 不存在")
            elif strict_stale and cur_gate.get("expr") != e.get("before"):
                errors.append(f"⓪ gate {tgt} expr before 与现状不符")
        elif k == "gate_enabled":
            if cur_gate is None:
                errors.append(f"⓪ gate {tgt} 不存在")
            else:
                if strict_stale and int(cur_gate.get("enabled", 1)) != int(e.get("before", 1)):
                    errors.append(f"⓪ gate {tgt} enabled before 与现状不符")
                # 红线③ 不删/不停用 qualify 合规门
                if int(e.get("after", 1)) == 0 and str(cur_gate.get("role")) == "qualify":
                    errors.append(f"③ gate_enabled {tgt}: role=qualify 的合规门不得停用")
        elif k == "gate_del":
            if cur_gate is None:
                errors.append(f"⓪ gate {tgt} 不存在")
            elif str(cur_gate.get("role")) == "qualify":
                errors.append(f"③ gate_del {tgt}: role=qualify 的合规门不得删除")
        elif k == "gate_add":
            if cur_gate is not None:
                errors.append(f"⓪ gate_add {tgt}: 门 id 已存在")

        # 红线② 不绕两段稳定性 (改变选择集者须附证据)
        if k in _SELECTION_KINDS:
            # 停用/删除 role=audit 门不改变选择集 → 豁免
            exempt = (k in ("gate_enabled", "gate_del") and cur_gate is not None
                      and str(cur_gate.get("role")) == "audit")
            if not exempt and not _evidence_ok(e):
                errors.append(f"② {k} {tgt}: 缺两段稳定+信号级复验证据 "
                              f"(需 evidence.stability.both_positive=true 且 "
                              f"evidence.pool_check.verdict=MIGRATE)")
    return errors


def _check_asof(label, expr):
    try:
        from app.market_cn.auto.core.runtime.expr import static_asof_check
        from app.market_cn.auto.core.runtime.functions import offset_funcs
        bad = static_asof_check(expr, offset_funcs())
        return [f"{label}: {f}({o}) 正偏移=未来函数" for (f, o) in bad]
    except Exception as ex:
        return [f"{label}: as-of 校验异常 {type(ex).__name__}: {ex}"]


# ================================================================
# 子命令
# ================================================================

def _load_prop(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cmd_validate(args):
    prop = _load_prop(args.proposal)
    strategy = args.strategy or prop.get("strategy")
    p = _yaml_path(strategy, args.strategy_dir)
    if not os.path.isfile(p):
        print(f"策略 YAML 不存在: {p}", file=sys.stderr)
        return 99
    yaml_text = open(p, encoding="utf-8").read()
    errors = validate(prop, strategy, yaml_text, strict_stale=not args.allow_stale)
    if errors:
        print(f"❌ validate 失败 ({len(errors)} 项):")
        for e in errors:
            print(f"  - {e}")
        return len(errors)
    print(f"✅ validate 通过: {strategy} / {len(prop.get('edits') or [])} 条编辑 "
          f"(红线三闸 + before 一致性 + as-of 全过)")
    return 0


def cmd_apply(args):
    prop = _load_prop(args.proposal)
    strategy = args.strategy or prop.get("strategy")
    p = _yaml_path(strategy, args.strategy_dir)
    if not os.path.isfile(p):
        print(f"策略 YAML 不存在: {p}", file=sys.stderr)
        return 99
    yaml_text = open(p, encoding="utf-8").read()
    errors = validate(prop, strategy, yaml_text, strict_stale=not args.allow_stale)
    if errors:
        print(f"❌ 拒绝应用 (红线未过, {len(errors)} 项):")
        for e in errors:
            print(f"  - {e}")
        return len(errors)
    new_text, applied, aerr = _apply_edits_text(yaml_text, prop.get("edits") or [])
    if aerr:
        print("❌ 文本替换失败 (未改动任何文件):")
        for e in aerr:
            print(f"  - {e}")
        return len(aerr)
    # 应用后必须仍是合法 YAML 且门表可通过加载校验
    ok, msg = _post_check(new_text)
    if not ok:
        print(f"❌ 应用后校验失败 (未改动任何文件): {msg}")
        return 1

    diff = "".join(difflib.unified_diff(
        yaml_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile=f"a/{strategy}.yaml", tofile=f"b/{strategy}.yaml"))
    if not args.apply:
        print(f"—— dry-run (未落盘; 加 --apply 生效) —— {len(applied)} 条编辑")
        print(diff or "(无差异)")
        return 0

    hist = os.path.join(args.strategy_dir, ".history")
    os.makedirs(hist, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup = os.path.join(hist, f"{strategy}.{ts}.yaml")
    shutil.copy2(p, backup)
    with open(p, "w", encoding="utf-8") as f:
        f.write(new_text)
    print(f"✅ 已应用 {len(applied)} 条编辑 → {p}")
    print(f"   备份: {backup}")
    print(f"   回滚: python -m app.market_cn.auto.tools.proposal revert "
          f"--strategy {strategy} [--to {ts}]")
    return 0


def _post_check(new_text):
    """应用后: YAML 可解析 且 门表能 load_strategy (静态 as-of 等一次过)。"""
    try:
        import yaml
        doc = yaml.safe_load(new_text)
    except Exception as e:
        return False, f"YAML 解析失败: {e}"
    # 门表合法性: 复用加载期校验 (expr 解析 + 静态 as-of)
    try:
        from app.market_cn.auto.core.runtime.expr import parse
        for g in (doc.get("gates") or []):
            parse(str(g["expr"]))
    except Exception as e:
        return False, f"门表达式非法: {e}"
    return True, "ok"


def cmd_revert(args):
    hist = os.path.join(args.strategy_dir, ".history")
    if not os.path.isdir(hist):
        print("无备份目录 (.history)", file=sys.stderr)
        return 1
    cands = sorted(f for f in os.listdir(hist)
                   if f.startswith(args.strategy + ".") and f.endswith(".yaml"))
    if args.to:
        cands = [c for c in cands if f".{args.to}.yaml" in c]
    if not cands:
        print(f"未找到 {args.strategy} 的备份" + (f" (to={args.to})" if args.to else ""),
              file=sys.stderr)
        return 1
    latest = cands[-1]
    src = os.path.join(hist, latest)
    dst = _yaml_path(args.strategy, args.strategy_dir)
    shutil.copy2(src, dst)
    print(f"✅ 已回滚 {args.strategy}.yaml ← {src}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="LLM 建议产物: 校验/应用/回滚 (红线三闸)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sd = dict(default="", help="策略 YAML 目录 (默认 core/_paths.STRATEGY_DIR; 测试可覆盖)")

    pv = sub.add_parser("validate", help="只校验, 不改文件 (退出码=失败项数)")
    pv.add_argument("--proposal", required=True)
    pv.add_argument("--strategy", default="")
    pv.add_argument("--strategy-dir", **sd)
    pv.add_argument("--allow-stale", action="store_true", help="跳过 base_sha/before 过期检测")
    pv.set_defaults(func=cmd_validate)

    pa = sub.add_parser("apply", help="应用 (默认 dry-run; --apply 才落盘+备份)")
    pa.add_argument("--proposal", required=True)
    pa.add_argument("--strategy", default="")
    pa.add_argument("--strategy-dir", **sd)
    pa.add_argument("--apply", action="store_true", help="真正写盘 (默认只出 diff)")
    pa.add_argument("--allow-stale", action="store_true")
    pa.set_defaults(func=cmd_apply)

    pr = sub.add_parser("revert", help="用备份还原")
    pr.add_argument("--strategy", required=True)
    pr.add_argument("--to", default="", help="指定备份时间戳 (默认最近一个)")
    pr.add_argument("--strategy-dir", **sd)
    pr.set_defaults(func=cmd_revert)

    args = ap.parse_args()
    args.strategy_dir = args.strategy_dir or _DEFAULT_STRATEGY_DIR
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
