# -*- coding: utf-8 -*-
"""常驻断言：自选股标签扩展层（`app/watchlist/`）的结构性禁令。

方案依据：`docs/自选股标签统一产出方案.md` §10「常驻断言」。
全部为**纯静态**检查（AST + 文本扫描），不连库、不依赖运行环境。

1. **import 闭包**（禁令 1）：`app/watchlist/**` 不得 import `app.agent.*` / `app.market_cn.auto.*`
   ⇒ label 是基座扩展层，与上级**零代码关系**（上级只经 `submit()` 单向写）。
2. **唯一写通道**（禁令 4）：表名字面只允许出现在 `app/watchlist/store.py`。
   判据是**字面零容忍** —— 连注释/docstring 里的提及都不许 ⇒ 文档中提到该表一律写「label 表」。
   建表入口 `migrations/qd_watchlist_label.sql` 是 DDL，不属读/写通道，豁免。
3. **单向边**（反向）：`app/agent/**`、`app/market_cn.auto/**` 内表名出现 **0** 次
   ⇒ 上级不会回读 label。
4. **禁用依赖**：label 不得用 `core.data.hub`，也不得用 agent 的筹码实现
   ⇒ 取数走 `services/kline`，筹码走 `services/chip_service`（唯一数学内核）。
5. **前端零感知**：前端只认 4 段结构与 `grade`/`source` 标注，不得出现表名。
6. **弹层样式作用域**：弹层内容经 `v-html` 注入（+ popover teleport 到 body）
   ⇒ 样式**必须**写在非 scoped 的 `<style>` 块里，否则 `[data-v-*]` 全部匹配不到、**静默失效**。

运行：
    python -m pytest tests/test_watchlist_label_gates.py -v
"""
from __future__ import annotations

import ast
from pathlib import Path

#: 表名以隐式拼接书写，避免本文件自身成为 repo 级 grep 的噪声命中
#: （保持「全仓搜索该表名只命中 store.py 与建表 SQL」这一可审计性质）。
TABLE = "qd_watchlist_" "label"

REPO = Path(__file__).resolve().parents[1]          # backend_api_python/
APP = REPO / "app"
WATCHLIST = APP / "watchlist"
VUE_SRC = REPO.parent / "QuantDinger-Vue" / "src"

BANNED_IMPORT_ROOTS = ("app.agent", "app.market_cn.auto")


def _py_files(root: Path):
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore")


def test_package_exists():
    """包必须存在且八个模块齐备（防被误删/误搬）。"""
    assert WATCHLIST.is_dir(), f"missing package: {WATCHLIST}"
    expected = {
        "__init__.py", "model.py", "compute.py", "store.py",
        "overlay.py", "render.py", "api.py", "job.py",
    }
    present = {p.name for p in WATCHLIST.glob("*.py")}
    missing = expected - present
    assert not missing, f"app/watchlist/ 缺模块: {sorted(missing)}"


def test_import_closure_has_no_upstream():
    """禁令 1：label 的 import 闭包不得触及 agent / auto。"""
    offenders = []
    for p in _py_files(WATCHLIST):
        tree = ast.parse(_read(p), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(BANNED_IMPORT_ROOTS):
                        offenders.append(f"{p.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.startswith(BANNED_IMPORT_ROOTS):
                    offenders.append(f"{p.name}: from {mod}")
    assert not offenders, "label 不得 import 上级: " + "; ".join(offenders)


def test_no_banned_dependencies():
    """禁令：不得用 hub 取数、不得用 agent 的筹码实现（须走 services/）。"""
    banned = ("core.data.hub", "core/data/hub", "chip_distribution")
    offenders = []
    for p in _py_files(WATCHLIST):
        src = _read(p)
        for token in banned:
            if token in src:
                offenders.append(f"{p.name}: {token}")
    assert not offenders, "label 禁用依赖: " + "; ".join(offenders)


def test_table_name_only_in_store():
    """禁令 4：app/ 树内表名字面只允许出现在 app/watchlist/store.py。"""
    hits = []
    for p in _py_files(APP):
        if TABLE in _read(p):
            hits.append(p.relative_to(REPO).as_posix())
    assert hits == ["app/watchlist/store.py"], (
        "表名字面必须唯一出现在 app/watchlist/store.py，实际: " + repr(hits)
    )


def test_upstream_never_reads_label():
    """禁令：agent / auto 内表名出现 0 次（单向边，否则双向依赖）。"""
    offenders = []
    for sub in ("app/agent", "app/market_cn/auto"):
        root = REPO / sub
        if not root.is_dir():
            continue
        for p in _py_files(root):
            n = _read(p).count(TABLE)
            if n:
                offenders.append(f"{p.relative_to(REPO).as_posix()} x{n}")
    assert not offenders, "上级不得回读 label 表: " + "; ".join(offenders)


def test_frontend_is_table_agnostic():
    """前端只认 4 段结构，不感知表名。"""
    if not VUE_SRC.is_dir():
        return  # 前端不在本工作区时跳过（后端独立部署场景）
    offenders = []
    for p in VUE_SRC.rglob("*"):
        if not p.is_file() or p.suffix not in (".vue", ".js", ".ts"):
            continue
        if TABLE in _read(p):
            offenders.append(p.relative_to(REPO.parent).as_posix())
    assert not offenders, "前端不得出现表名: " + "; ".join(offenders)


_WL_VUE = VUE_SRC / "components" / "WatchlistPanel" / "index.vue"


def _style_blocks(src: str):
    """产出 `(是否 scoped, 块内容)` —— 用于判断某段样式落在哪种作用域里。"""
    import re
    return [("scoped" in m.group(1), m.group(2))
            for m in re.finditer(r"<style([^>]*)>(.*?)</style>", src, re.S)]


def test_label_popover_styles_are_unscoped():
    """弹层样式必须写在**非 scoped** 块（v-html + popover teleport ⇒ scoped 匹配不到）。

    这是一个**真实踩过的坑**（09-23 三轮微调）：样式放在 scoped 块里不会报错、不会警告，
    只会静默失效 —— 表现为横排退化成纯文本流（价格数粘连成 `33.1332.3231.10`、
    标题独占一行）。因此用断言把它钉住，而不是靠人记住。
    """
    if not _WL_VUE.is_file():
        return  # 前端不在本工作区时跳过
    blocks = _style_blocks(_read(_WL_VUE))
    assert blocks, f"未解析到 <style> 块，结构变了？{_WL_VUE}"

    wrongly_scoped = [i for i, (sc, body) in enumerate(blocks)
                      if sc and ".wl-strategy-pop" in body]
    assert not wrongly_scoped, (
        "label 弹层样式不得放在 scoped <style> 块：内容经 v-html 注入，"
        "scoped 编译出的 [data-v-*] 匹配不到 ⇒ 样式静默失效"
    )

    unscoped = "".join(body for sc, body in blocks if not sc)
    assert ".wl-strategy-pop" in unscoped, "label 弹层样式必须存在于非 scoped 块"
    for cls in (".wl-lv-row {", ".wl-sec-inline {", ".wl-lv {"):
        assert cls in unscoped, f"弹层样式缺 {cls}（是否又被挪回 scoped 块？）"


def test_levels_renderer_stays_horizontal():
    """关键位必须**水平排列**（`.wl-lv-row`），不得回退成一档一行的表格。"""
    if not _WL_VUE.is_file():
        return
    src = _read(_WL_VUE)
    beg = src.find("renderLevelsSection (")
    end = src.find("renderScoreSection (")
    assert 0 < beg < end, "未定位到 renderLevelsSection / renderScoreSection"
    seg = src[beg:end]
    assert "wl-lv-row" in seg, "关键位必须用 .wl-lv-row 水平铺开"
    assert "wl-dtable" not in seg, "关键位不得回退成 <table>（一档一行）"


# ═══════════════════════════════════════════════════════════════════
# 关键位上图（分时图叠加支撑/压力线）
# ═══════════════════════════════════════════════════════════════════

_KLINE_VUE = VUE_SRC / "views" / "indicator-analysis" / "components" / "KlineChart.vue"
_IDE_VUE = VUE_SRC / "views" / "indicator-ide" / "index.vue"


def _segment(src: str, start: str, end: str) -> str:
    """截取 `[start, end)` 片段，用于把断言限定在某个函数体内（避免全文件误命中）。"""
    i = src.find(start)
    assert i >= 0, f"未找到起始标记: {start!r}"
    j = src.find(end, i + len(start))
    assert j > i, f"未找到结束标记: {end!r}"
    return src[i:j]


def test_level_lines_clip_out_of_range():
    """「超出当前 Y 轴范围的关键位不画」是功能契约，判据必须保持**严格**。

    分时 Y 轴被锁定为「昨收 ± 当日最大偏离」，筹码关键位常在范围外。**实测**（32 只自选、
    162 档关键位）：39.5% 落在视野内，31/32 只票至少可见 1 档 —— 即"过滤"是常态而非边缘情况。

    若把比较放宽成贴边显示，图右边缘会挤上几条分不清归属的线，与 label「简单明了」的取向相反。
    要做"视野外贴边指示"应当是新增一个显式档位，而不是把这里的比较悄悄放宽。
    """
    if not _KLINE_VUE.is_file():
        return
    src = _read(_KLINE_VUE)
    seg = _segment(src, "const pickVisibleLevels = (", "\n\n    /**")
    assert "const pickVisibleLevels" in seg
    assert "if (y < 0 || y > height) continue" in seg, (
        "关键位可见性判据被放宽了：必须严格剔除 y<0 / y>height，"
        "不得改成留像素余量的贴边显示"
    )


def test_key_level_indicator_is_minute_only():
    """关键位线指标只在分时挂载，且退出分时时被移除（否则会残留到日K上）。"""
    if not _KLINE_VUE.is_file():
        return
    src = _read(_KLINE_VUE)

    # ① 挂载入口必须带分时守卫：函数体开头即 `if (!chart || !isMinuteLine.value) return`
    seg = _segment(src, "const ensureMinuteSrLinesIndicator = (", "\n    /** 已应用的锚点区间")
    assert "isMinuteLine.value" in seg, "关键位线必须只在分时模式挂载"
    assert "createIndicator(MINUTE_SR_LINES_IND, true," in seg, (
        "必须以 isStack=true 追加：false 会执行 paneInstances=[] 清空同 pane 的均价线与 0 轴线"
    )

    # ② 清理入口：退出分时（clearMinutePrevCloseAxis）必须移除该指标
    clear = _segment(src, "const clearMinutePrevCloseAxis = ()", "const ensureMinuteIndicators = (")
    assert "removeIndicator('candle_pane', MINUTE_SR_LINES_IND)" in clear, (
        "退出分时必须移除关键位线指标，否则切回日K后线仍在"
    )


def test_klinechart_takes_levels_via_prop():
    """分层：图表组件**只负责画**，数据源归父组件（`levelLines` prop）。

    KlineChart 不得自己去拉 watchlist / label —— 那会让图表组件承担自选股概念，
    并且绕开「label 读路径唯一出口」。父组件用的也是**已加载**的自选列表
    （`/watchlist/get` 已 attach 4 段）⇒ 零新增接口、零新增请求。
    """
    if not _KLINE_VUE.is_file() or not _IDE_VUE.is_file():
        return

    chart_src = _read(_KLINE_VUE)
    for banned in ("getWatchlist", "@/api/market"):
        assert banned not in chart_src, f"图表组件不得依赖 {banned}：关键位应由父组件经 prop 注入"
    assert "levelLines: {" in chart_src, "图表组件须声明 levelLines prop"

    ide_src = _read(_IDE_VUE)
    assert ':level-lines="chartLevelLines"' in ide_src, "父组件须把关键位传给图表"
    seg = _segment(ide_src, "chartLevelLines () {", "\n    chartTabOptions")
    for need in ("'supports'", "'resistances'", "sec.items"):
        assert need in seg, f"chartLevelLines 须遍历 levels 段（缺 {need}）"
    assert "score" not in seg, "chartLevelLines 只取支撑/压力，不得把评分段当价格"
