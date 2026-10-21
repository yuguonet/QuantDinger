# -*- coding: utf-8 -*-
"""skill_run.py —— 执行型技能的统一子进程入口（差距 B，2026-10-02）。

供 run_skill 工具 / task 子 agent / 人工跑批共用。子进程形态的意义：
  1. 超时可击杀（长流水线跑飞不拖死主 agent）；
  2. 崩溃隔离（技能代码异常不污染主进程）；
  3. 产物天然落文件（大结果不必挤进上下文）。

用法：
    python scripts/skill_run.py market-screener
    python scripts/skill_run.py strategy-debug '{"fn": "debug_strategy", "kwargs": {"strategy": "break", "code": "600519"}}'

输出（stdout，单行 JSON）：
    {"name":..., "ok":bool, "result":<预览，恒为合法 JSON>,
     "preview_note":<裁剪事实，仅被裁时出现>, "full_path":"<全量结果路径>",
     "elapsed_s":...}
    预览按**整条目**裁剪（2026-10-21）：条目要么完整、要么整条不在，永不盲切
    字符 —— 盲切会把证据卡拦腰斩断，主 agent 拿到半张卡的坏 JSON。裁剪事实放
    外层信封 preview_note（传输层元信息不混进技能载荷）。

产物落点（F26-D2，2026-10-18）：
  · 工具路径（run_skill 派活）：父进程建好作业目录经 env `QD_SKILL_JOB_DIR` 传入，
    全量结果写 `<job_dir>/result.json`，并在**调用前后** touch `last_progress`
    （停滞看门狗的观测源，见 app/agent/exec/job_runner.py）；
  · 人工直跑（无该 env）：回落旧扁平输出 `<backend>/tmp/skill_output/<ts>_<name>.json`
    —— 这是文档化的直跑用法，不是兜底。
"""
from __future__ import annotations

import inspect
import json
import math
import os
import sys
import time
from pathlib import Path

# 技能执行注册表：name → (模块路径, 允许调用的函数, 默认函数)
# 白名单制——run_skill 不执行任意代码，只跑这里登记过的入口。
EXECUTABLE_SKILLS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "market-screener": ("skills.market-screener.run", ("run",), "run"),
    "strategy-debug": (
        "skills.strategy-debug.run",
        ("list_strategies", "why_strategy", "debug_strategy", "doctor_auto"),
        "debug_strategy",
    ),
    "auto-backtest": ("skills.auto-backtest.run", ("run",), "run"),
}

# ★ 2026-10-14 迁址：原为 `parents[1]`（= app/agent）⇒ 产物落在 `app/agent/tmp/`
#   skill_output/。随 `app/agent/tmp/` 整体移除，改到**项目级** `backend_api_python/tmp/`
#   skill_output/ —— 与项目约定「临时文件/结果/日志一律放 tmp/」一致，也让 agent 包
#   目录里不再夹带运行时产物（`app/agent` 只放代码）。
#   parents: 0=scripts 1=agent 2=app 3=backend_api_python
_OUT_DIR = Path(__file__).resolve().parents[3] / "tmp" / "skill_output"
# ── 预览裁剪（2026-10-21，结构化裁剪替代字符盲切）──────────────────────
# 预算推导（test_skill_run_preview.py 锁定）：market-screener 漏斗扩容后 run()
# 契约 = 12 张 deep_analyze 证据卡 ≈ 24K 字符 + report_guide/funnel/market 等
# 顶层信封 ≈ 2K ⇒ 32000 让 12 卡常态**完整送达**，再留 ~25% 余量防卡片膨胀。
# 改小前先过 ① 号测试；env `QD_SKILL_PREVIEW_LIMIT` 可调（≤0/非法回落缺省）。
_PREVIEW_LIMIT = 32000
_STR_CUT_MARK = "……【字符串过长已截断，全量见 full_path】"   # 截断处可见标记
_STR_CUT_MIN = 128   # 短于此的字符串不参与截断：截了救不回预算，还白毁语义
_LIST_ITEM_SEP = 2   # json.dumps 缺省分隔符 ", " 的长度（批量估算条目占用用）


def _preview_limit() -> int:
    """预览预算：env `QD_SKILL_PREVIEW_LIMIT` 优先，≤0/非法回落模块缺省。"""
    raw = os.environ.get("QD_SKILL_PREVIEW_LIMIT", "") or ""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _PREVIEW_LIMIT
    return v if v > 0 else _PREVIEW_LIMIT


def _dumps(obj) -> str:
    """预览统一序列化口径（与全量落盘一致：非 ASCII 直出、未知类型转 str）。"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def _normalize(result):
    """经 dumps/loads 往返把返回体拷成**纯 JSON 类型**副本。

    易错点：裁剪必须工作在副本上 —— main() 之后还要把**原样** result 写进
    full_path，且调用方（测试/技能）不得被预览改动污染；直接 deepcopy 会碰上
    技能产出里的任意对象（default=str 的存在暗示了这一点），JSON 往返最稳。
    """
    return json.loads(_dumps(result))


def _dict_list_paths(node, path: str):
    """yield (path, list)：非空且元素**全为 dict** 的列表（可整条裁剪的候选）。

    只裁 dict 列表：条目是自包含对象（证据卡/明细行），整条丢弃语义无损；
    标量列表（themes/missing/errors）语义是"清单"，丢一项就是丢事实，不动。
    path 以 "result" 为根（该对象在信封里就是 result 字段），列表层级记 "[]"。
    """
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _dict_list_paths(v, f"{path}.{k}")
    elif isinstance(node, list):
        if node and all(isinstance(it, dict) for it in node):
            yield path, node
        for it in node:
            yield from _dict_list_paths(it, f"{path}[]")


def _drop_tail_items(work, limit: int, preview: str):
    """第一层裁剪：按**整条目**丢。返回 ({裁剪路径: 丢的条数}, 新 preview)。

    每轮挑序列化占比最大的候选列表，按条目均摊体积从**尾部**批量丢整条
    （deep_analyze 按 p_up 降序，尾巴最弱）。丢完重序列化复核，不达标继续，
    直到装下或无候选 —— 条目严格递减保证终止，绝不产出半条。
    """
    dropped: dict[str, int] = {}
    while len(preview) > limit:
        candidates = list(_dict_list_paths(work, "result"))
        if not candidates:
            break
        path, lst = max(candidates, key=lambda c: len(_dumps(c[1])))
        sizes = [len(_dumps(it)) + _LIST_ITEM_SEP for it in lst]
        overshoot = len(preview) - limit
        avg = sum(sizes) / len(sizes)
        k = max(1, min(len(lst), math.ceil(overshoot / avg)))
        del lst[-k:]
        dropped[path] = dropped.get(path, 0) + k
        preview = _dumps(work)
    return dropped, preview


def _string_spots(node):
    """yield (container, key, value)：树里全部字符串值（container[key] 可回写），
    key 在 list 容器里是下标。供第二层超大字符串截断用。"""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                yield node, k, v
            else:
                yield from _string_spots(v)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str):
                yield node, i, v
            else:
                yield from _string_spots(v)


def _cut_long_strings(work, limit: int, preview: str):
    """第二层裁剪：超大字符串截短（唯一改语义的层级）。返回 (截断数, 新 preview)。

    每轮挑最长串按超出量截短，截断处补 _STR_CUT_MARK 可见标记。源串去掉 c 个
    字符至少省 c 个序列化字符（转义只多不少），故减去 len(MARK) 后仍保证
    截后 ≤ 预算；找不到足够长的串就停手，宁可超预算也不毁短串语义。
    """
    cut = 0
    while len(preview) > limit:
        longest = None
        for container, key, s in _string_spots(work):
            if len(s) > _STR_CUT_MIN and (longest is None or len(s) > longest[2]):
                longest = (container, key, s)
        if longest is None:
            break
        container, key, s = longest
        keep = max(0, len(s) - (len(preview) - limit) - len(_STR_CUT_MARK))
        container[key] = s[:keep] + _STR_CUT_MARK
        cut += 1
        preview = _dumps(work)
    return cut, preview


def _build_preview(result, limit=None) -> tuple:
    """把技能返回体裁成回传主 agent 的预览：恒为**合法 JSON**，永不盲切。

    两层语义（tests/test_skill_run_preview.py 为契约）：
      1. 整条目层：dict 列表从尾部丢整条，条目要么完整、要么整条不在；
      2. 字符串层：整条丢完仍超预算时才截超大字符串，截断处留可见标记。
    Returns:
        (preview, note)。note 无裁剪时为 {}；被裁时
        {"trimmed": True, "dropped_items": {路径: 条数}, "strings_cut": N}，
        由 main() 放进**外层信封** preview_note 字段 —— 传输层元信息不混进技能载荷。
    """
    if limit is None or limit <= 0:
        limit = _preview_limit()
    work = _normalize(result)
    preview = _dumps(work)
    if len(preview) <= limit:
        return preview, {}
    dropped, preview = _drop_tail_items(work, limit, preview)
    strings_cut = 0
    if len(preview) > limit:
        strings_cut, preview = _cut_long_strings(work, limit, preview)
    if not (dropped or strings_cut):
        # 结构上无可裁（无 dict 列表、无够长的串）——如实回全量，不伪造 note。
        return preview, {}
    note = {"trimmed": True}
    if dropped:
        note["dropped_items"] = dropped
    if strings_cut:
        note["strings_cut"] = strings_cut
    return preview, note


def _touch(job_dir: str) -> None:
    """进度戳（F26-D2）。延迟 import：本进程刚启动时 `app.*` 还不可导，
    须等 main() 里先把 backend 根挂上 sys.path。job_dir 为空 = 人工直跑，不戳。"""
    if not job_dir:
        return
    from app.agent.exec.job_runner import touch_progress
    touch_progress(job_dir)


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
    job_dir = os.environ.get("QD_SKILL_JOB_DIR", "") or ""
    try:
        # 只挂 backend_api_python（保证 `import app.*` 成立）。
        # 【2026-10-13·P5】原本还挂 app/agent 让 `skills.*` 裸名成立 ——
        # 改造后包内相对导入，EXECUTABLE_SKILLS 里的裸名由 modpath.resolve 补前缀，
        # 不再需要把包目录塞进 sys.path（那正是 M-1 双实例的成因）。
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        _touch(job_dir)   # F26-D2：作业启动即留进度戳（看门狗"已开工"证据）
        import importlib
        from app.agent.modpath import import_module
        fn = getattr(import_module(mod_path), fn_name)
        sig = inspect.signature(fn)
        unknown = set(kwargs) - set(sig.parameters)
        if unknown:
            # 2026-10-15：模型编造 kwargs（strategy/market_cap/with_mtf…）被拒后
            # 会 read_skill 查用法再重试 —— 直接把正确调法 + 结论规则回给它。
            allowed = sorted(sig.parameters)
            print(json.dumps({
                "ok": False,
                "error": f"未知参数: {sorted(unknown)}",
                "hint": f"请只用: {allowed}；一键入口 arguments='{{\"fn\":\"{fn_name}\"}}' 或 arguments=''（全默认）",
                "report_guide": {
                    "STEPS": "按 hint 重试 run_skill 一次即可；禁止 read_skill / search_tools / get_market_overview",
                    "priority": "支撑与弱转强/强转弱 ≫ 涨跌幅",
                },
            }, ensure_ascii=False))
            return 2
        result = fn(**kwargs)
    except Exception as e:
        print(json.dumps({"ok": False, "name": name,
                          "error": f"{type(e).__name__}: {e}"[:2000]}, ensure_ascii=False))
        return 1

    # 产物落盘 + 预览回传（差距 B 第 3 条：主上下文只拿结论，不搬明细）
    if job_dir:
        # F26-D2：作业目录模式 —— 全量落 <job>/result.json，与 bridge 的 report.json 同范式
        full_path = Path(job_dir) / "result.json"
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                                 encoding="utf-8")
            _touch(job_dir)   # 产物落定 = 最后一次有效进展
        except Exception:
            full_path = None
    else:
        # 人工直跑：旧扁平输出（文档化用法，见模块头）
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        full_path = _OUT_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}.json"
        try:
            full_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                                 encoding="utf-8")
        except Exception:
            full_path = None

    # 预览按整条目结构化裁剪（永不盲切字符）；裁剪事实进外层信封 preview_note，
    # 技能载荷 result 本身不被元信息污染（契约见 tests/test_skill_run_preview.py）。
    preview, preview_note = _build_preview(result)
    envelope = {
        "ok": True, "name": name, "fn": fn_name,
        "result": preview, "full_path": str(full_path) if full_path else "",
        "elapsed_s": round(time.time() - t0, 2),
    }
    if preview_note:
        envelope["preview_note"] = preview_note
    print(json.dumps(envelope, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    _rc = main()
    # 本进程是**一次性**的：结果已写盘、已回传 stdout，之后没有任何东西需要优雅收尾。
    # 走正常解释器退出会被第三方常驻线程拖死 —— 实测（2026-10-12，tmp/_screener_hang.py）：
    # market-screener 的 run() 只跑 51~58s，随后进程 >10min 不退出（TDX 心跳线程是
    # 非 daemon，threading._shutdown() 死等它），于是 run_skill 恒定表现为
    # 「超时（>600s）已中止」。流水线本身并不慢，是退出卡住制造了慢的假象。
    # 因此显式 flush 后直接 os._exit，把控制权立刻交还父进程。
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(_rc)
