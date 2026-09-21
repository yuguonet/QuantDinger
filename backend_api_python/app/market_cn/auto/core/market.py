#!/usr/bin/env python3
"""core/market.py — 市场无关原语 + MarketSpec 接口（市场规则由 adapters 注入）。

**分层纪律（架构 §9.3）**：core 内**不出现任何市场专属常量**。本模块只做两件事：
  1. 定义 `MarketSpec` **接口**（架构 §9.1 的正交维度）——"市场规则长什么样"；
  2. 提供**与具体市场无关**的原语：按 spec 查档位做涨跌停 / 分板判定。
具体取值（A 股 9.8%/19.8%、±0.2% 容差、代码前缀分板规则……）住在
`adapters/markets/*.yaml`，由加载器注入为**默认市场**。

**为什么涨跌停判定要收在这一层**：此前 `is_limit_up` 把 `0.098/0.198` 与 `*0.98` 写死在
代码里，"接港股/美股"必然要改 core。现在阈值只是 spec 的一个字段 —— 新市场 = 新增一份
YAML，本文件一行不动。

**默认市场**：冻结的 `.py` 插件仍以 3 参数形式调用 `is_limit_up(close, prev, board)`。
为让这些调用点**零改动**，`spec=None` 时走**进程默认市场**：由 adapters 在 import 时注入
（`set_default_market`），未注入时**惰性加载 A**（冻结插件裸调用不会崩）。

**性能**（⚠️ 这一层是**热点**，改这里必跑 `tmp/_m5_islowup_bench.py` 同进程交错 A/B）：

`is_limit_up` 在全市场回测里是百万级调用。**已实测的代价**（`tmp/_hotpath_ab4.out`，1e6 次/组 × 7 轮
交错轮转取最优 —— 顺序跑的 A/B 会被温漂污染，同一份代码自己就能漂 ±10%，**别用顺序跑下结论**）：

| 原语 | 旧（硬编码） | 新（spec 驱动） | 差 |
|---|---|---|---|
| `is_limit_up`（3 参数/裸调用） | 333.5 ns | 483.6 ns | **+45%** |
| `is_limit_up`（显式 spec，引擎实际形态） | 333.5 ns | 472.6 ns | **+42%** |
| `get_board_type` | 519.1 ns | 637.6 ns | **+23%** |

→ **这是 spec 驱动的固有代价**，不是漏优化：旧实现是 `0.098 if bt == "main" else 0.198`
（字符串比较，无字典查找），新实现必然多一次 `dict.get` + 一次档位回退。已做的减负：
  - 阈值的 `up_pct * 0.98` 在**加载期**折算进元组（旧实现**每次调用**都乘）；
  - spec 解析**内联**读 `_DEFAULT` 模块全局（不再多一层函数调用）；
  - 缺省档位在 `__post_init__` 预先摊平为 `_default_band`（miss 时不再做第二次 `dict.get`）。
按全市场量级估算 ≈ **+0.8s / 次 20 策略全跑**（相对 ~32s 总量，约 2.5%）。
若将来此项成为瓶颈，应改的是"减少调用次数"（如 `find_limit_ups` 按 (bars, board) 跨策略记忆化），
**不是**把档位回退内联到各原语造出第二份语义。

⚠️ **不变量**：`bands` / `band_default` **构造后不得再改**（`_default_band` 是算好的快照）。
要改请重建 `MarketSpec`（`adapters/markets/registry.py` 就是这么做的）。

易错点：bars 元素为 dict (time/open/high/low/close/volume)，volume 单位是股。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 板块类型缺省键（单板块市场 / 未声明板块时用）
DEFAULT_BOARD = "default"


# ================================================================
# MarketSpec —— 市场规则的正交维度（架构 §9.1 字段表）
# ================================================================
@dataclass
class MarketSpec:
    """一个市场的全部规则差异（正交维度，见架构 §9.1）。

    只承载**市场级**规则；账户级约束（PDT / GFV）明确不入此结构（架构 §9.0）。

    bands：板块 → `(up_eff, dn_factor, dn_tol)` **摊平**档位（加载期算好，热路径零计算）：
      up_eff    涨停判别有效阈值 —— 涨停 ⟺ close/prev - 1 >= up_eff
      dn_factor 跌停价系数 —— 跌停价 = prev_close * dn_factor
      dn_tol    跌停判定相对容差（一字跌停 / 顺延判定用）
    无涨跌停市场（HK / US）→ bands 为空 + band_kind='none'。
    """

    key: str = "A"
    name: str = "A股"
    # --- 交易限制 / 结算 ---
    intraday_t0: bool = False              # 当日新开仓可否当日平（A=false）
    settlement_days: int = 1               # 交割周期（仅影响资金可用）
    settlement_basis: str = "calendar"     # calendar | event
    # --- 方向 / 做空 ---
    direction: str = "long_only"           # long_only | long_short
    short_rule: str = "none"
    # --- 价格边界 ---
    band_kind: str = "pct"                 # pct | none | bounded
    bands: Dict[str, Tuple[float, float, float]] = field(default_factory=dict)
    # 名义幅度（未乘容差）—— 供"要的是名义涨跌停幅度"的场景（如 tail 的 limit_pct 判定）。
    # 与 bands 同源同簿，只是不折叠容差：`{board: {"up_pct":…, "dn_pct":…}}`。
    nominal: Dict[str, Dict[str, float]] = field(default_factory=dict)
    band_default: Optional[str] = None     # 板块未命中时取哪个板块的档位（None = 无涨跌停）
    price_bound: Optional[Tuple[float, float]] = None   # bounded[lo, hi]（Polymarket $0~$1）
    # --- 交易单位 / 币种 ---
    lot_size: int = 100
    tick_size: float = 0.01
    currency: str = "CNY"
    tz: str = "CST"
    fee_model: str = "commission+stamp"
    # --- 分板规则（有序：先匹配先生效）---
    board_rules: List[Tuple[str, str]] = field(default_factory=list)   # (代码前缀, 板块键)
    board_default: str = DEFAULT_BOARD
    board_names: List[Tuple[str, str]] = field(default_factory=list)   # (代码前缀, 中文名)
    board_name_default: str = "未知"
    # --- 数据源 ---
    source: str = ""                       # 数据源适配器键（'' = 未接）
    runnable: bool = False                 # 该市场当前是否可实际运行
    note: str = ""

    #: 缺省档位快照（`__post_init__` 算出；`bands`/`band_default` 构造后不可变，见模块头）
    _default_band: Optional[Tuple[float, float, float]] = field(
        default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        bd = self.band_default
        self._default_band = self.bands.get(bd) if bd is not None else None

    # ---- 档位查询（热路径：一次 dict.get；缺省档位是 `__post_init__` 算好的快照）----
    def _band(self, board_type: str) -> Optional[Tuple[float, float, float]]:
        """板块档位；未命中 → `band_default` 档。**这条回退规则只此一处**。"""
        b = self.bands.get(board_type)
        if b is not None:
            return b
        return self._default_band

    def nominal_up_pct(self, board_type: str) -> float:
        """该板块的**名义**涨停幅度（不乘容差）；无涨跌停市场 → 0.0。"""
        cfg = self.nominal.get(board_type)
        if cfg is None and self.band_default is not None:
            cfg = self.nominal.get(self.band_default)
        return float(cfg.get("up_pct", 0.0)) if cfg else 0.0

    def board_of(self, code: Any) -> str:
        c = str(code)
        for prefix, board in self.board_rules:
            if c.startswith(prefix):
                return board
        return self.board_default

    def board_name(self, code: Any) -> str:
        c = str(code)
        for prefix, nm in self.board_names:
            if c.startswith(prefix):
                return nm
        return self.board_name_default


# ================================================================
# 默认市场注入（core 不 import adapters —— 由 adapters 配置 core）
# ================================================================
_DEFAULT: Optional[MarketSpec] = None
_LAZY_TRIED = False


def set_default_market(spec: MarketSpec) -> None:
    """注入进程默认市场（由 `adapters/markets` 加载器调用）。"""
    global _DEFAULT, _LAZY_TRIED
    _DEFAULT = spec
    _LAZY_TRIED = True


def default_market() -> MarketSpec:
    """当前默认市场。未注入时惰性加载 A（保证冻结插件的裸调用不崩）。"""
    global _DEFAULT, _LAZY_TRIED
    if _DEFAULT is None and not _LAZY_TRIED:
        _LAZY_TRIED = True
        try:
            from app.market_cn.auto.adapters.markets.registry import load_market
            _DEFAULT = load_market("A")
        except Exception:
            _DEFAULT = None
    return _DEFAULT if _DEFAULT is not None else MarketSpec()


# ================================================================
# 原语（全部 spec 驱动；spec=None → 默认市场）
#
# ⚠️ 热路径纪律：`is_limit_up` / `get_board_type` 在回测里是百万级调用。
# 故 spec 解析一律写成 `spec if spec is not None else (_DEFAULT or default_market())`
# **内联**（少一层 `default_market()` 函数调用，且已注入默认市场时零额外开销）；
# 语义 = `_DEFAULT if _DEFAULT is not None else default_market()` —— 未注入时才惰性加载。
#
# 档位回退规则（板块未命中 → 取 band_default 档）**只此一处**：`MarketSpec._band()`。
# 不要为省一次方法调用把它内联到各原语里 —— 那会造出第二份回退语义（漂移温床）。
# ================================================================
def get_board_type(code: Any, spec: Optional[MarketSpec] = None) -> str:
    """代码 → 板块键（规则来自 spec.board_rules，不再硬编码 30/68 前缀）。"""
    return (spec if spec is not None else (_DEFAULT or default_market())).board_of(code)


def get_board_name(code: Any, spec: Optional[MarketSpec] = None) -> str:
    """代码 → 板块中文名（规则来自 spec.board_names）。"""
    return (spec if spec is not None else (_DEFAULT or default_market())).board_name(code)


def is_limit_up(close: float, prev_close: float, board_type: str,
                spec: Optional[MarketSpec] = None) -> bool:
    """第 i 日是否涨停（口径 = spec 的板块档位；无涨跌停市场恒 False）。

    刻意保留 3 参数签名（第 4 个可选）= 冻结的 `.py` 策略调用点零改动。
    """
    if prev_close <= 0:
        return False
    b = (spec if spec is not None else (_DEFAULT or default_market()))._band(board_type)
    if b is None:
        return False
    return (close / prev_close - 1) >= b[0]


def find_limit_ups(bars: List[Dict[str, Any]], board_type: str,
                   spec: Optional[MarketSpec] = None) -> List[int]:
    """找到所有涨停日索引（口径同 is_limit_up）。

    ⚠️ 有意**不内联**涨跌停比较式：口径必须只有一份（内联出第二份 = 漂移温床，
    与 `core/exec.py` 反复强调的"成交语义唯一实现"同理）。这里只在循环外把
    spec 解析一次，逐根 bar 调 `is_limit_up(..., s)`。
    """
    s = spec if spec is not None else (_DEFAULT or default_market())
    result: List[int] = []
    ap = result.append
    for i in range(1, len(bars)):
        if is_limit_up(bars[i]["close"], bars[i - 1]["close"], board_type, s):
            ap(i)
    return result


def limit_dn_price(prev_close: float, board_type: str,
                   spec: Optional[MarketSpec] = None) -> float:
    """跌停价 = 昨收 × 系数（系数来自 spec；无涨跌停市场返回 0.0 = 不判定）。"""
    s = spec if spec is not None else (_DEFAULT or default_market())
    b = s._band(board_type)
    if b is None:
        return 0.0
    return prev_close * b[1]


def limit_dn_tol(spec: Optional[MarketSpec] = None) -> float:
    """跌停判定相对容差（一字跌停 / 顺延判定用；取默认板块档位）。"""
    s = spec if spec is not None else (_DEFAULT or default_market())
    b = s._band(s.board_default)
    return b[2] if b else 0.0


# ================================================================
# YAML → optbands 构造（语义集中一处：名义幅度 + 容差 → 摊平元组）
# ================================================================
def build_nominal(raw: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """`{board: {up_pct, dn_pct, ...}}` → 名义幅度簿（未折叠容差，与 build_bands 同源）。"""
    out: Dict[str, Dict[str, float]] = {}
    for board, cfg in (raw or {}).items():
        out[str(board)] = {
            "up_pct": float(cfg.get("up_pct", 0.0)),
            "dn_pct": float(cfg.get("dn_pct", 0.0)),
        }
    return out


def build_bands(raw: Dict[str, Dict[str, Any]]) -> Dict[str, Tuple[float, float, float]]:
    """`{board: {up_pct, dn_pct, up_tol, dn_tol}}` → 摊平档位元组。

    涨停有效阈值 = up_pct × up_tol（原实现 `threshold * 0.98` 的等价物，只是**提前算一次**）；
    跌停价系数 = 1 - dn_pct；dn_tol 原样保留（作相对容差）。
    """
    out: Dict[str, Tuple[float, float, float]] = {}
    for board, cfg in (raw or {}).items():
        up = float(cfg.get("up_pct", 0.0)) * float(cfg.get("up_tol", 1.0))
        dn = 1.0 - float(cfg.get("dn_pct", 0.0))
        tol = float(cfg.get("dn_tol", 0.0))
        out[str(board)] = (up, dn, tol)
    return out
