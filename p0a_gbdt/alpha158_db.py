# alpha158_db.py — 完整 Alpha158 因子在 DB 前复权 OHLCV 上的向量化实现
# 作用：复刻 qlib Alpha158（158 因子），供 p0a_gbdt 粗筛器使用；口径真源 = qlib/contrib/data/loader.py::Alpha158DL.get_feature_config。
# 因子构成：kbar 9 + price 4(OPEN0/HIGH0/LOW0/VWAP0) + rolling 29 算子 × 5 窗口(5/10/20/30/60) = 158。
# 易错点：
#   1. DB 无 amount 列 → $vwap 用 (H+L+C)/3 典型价近似，与 qlib 的 amount/volume 有口径差异，VWAP0 因子需标注；
#   2. qlib 的 Ref($x, d) 是「d 天前」，rolling 统计窗口含当日；pandas rolling(d) 语义一致；
#   3. RANK/IMAX/IMIN/IMXD 用 numpy 滑动窗口视图向量化，避免 rolling.apply 的 Python 循环爆炸；
#   4. BETA/RSQR/RESI 用滚动协方差解析式（x=时间序号为等差数列，Σx/Σx² 闭式），不用逐窗 regress；
#   5. 所有因子只用 t 及之前信息，无前视；输出与输入长表同索引（instrument, datetime）。

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from common import limit_up_threshold

ROLL_WINDOWS = [5, 10, 20, 30, 60]
EPS = 1e-12


def _wide(panel, col):
    return panel.pivot(index="datetime", columns="instrument", values=col).sort_index()


def _rank_pct(y, d):
    w = sliding_window_view(y, d)
    return np.r_[np.full(d - 1, np.nan), (w < w[:, -1:]).mean(axis=1)]


def _idx_max(y, d):
    w = sliding_window_view(y, d)
    return np.r_[np.full(d - 1, np.nan), (w.argmax(axis=1) + 1) / d]


def _idx_min(y, d):
    w = sliding_window_view(y, d)
    return np.r_[np.full(d - 1, np.nan), (w.argmin(axis=1) + 1) / d]


def _rolling_regression(C, d):
    """C: 宽表 DataFrame(date × instrument)。返回 slope/rsqr/resi 三个同形宽表。
    解析式：对每列 y，x=0..n-1 为等差数列，Σx/Σx² 闭式，滚动协方差用 rolling.sum 向量化。"""
    n, m = C.shape
    x = np.arange(n, dtype=float)
    sx = pd.Series(x).rolling(d).sum().to_numpy()[:, None]
    sxx = pd.Series(x * x).rolling(d).sum().to_numpy()[:, None]
    sy = C.rolling(d).sum().to_numpy()
    sxy = (C * x[:, None]).rolling(d).sum().to_numpy()
    syy = (C * C).rolling(d).sum().to_numpy()
    dx = d * sxx - sx * sx
    dy = d * syy - sy * sy
    cov = d * sxy - sx * sy
    slope = np.divide(cov, dx, out=np.full_like(cov, np.nan), where=np.abs(dx) > EPS)
    corr = np.divide(cov, np.sqrt(np.maximum(dx, EPS) * np.maximum(dy, EPS)),
                     out=np.full_like(cov, np.nan), where=(dx > EPS) & (dy > EPS))
    rsqr = corr * corr
    xbar = sx / d
    ybar = sy / d
    resi = C.to_numpy() - (ybar - slope * xbar + slope * x[:, None])
    idx, cols = C.index, C.columns
    return (pd.DataFrame(v, index=idx, columns=cols) for v in (slope, rsqr, resi))


def compute_alpha158(panel):
    """panel: 长表 DataFrame，含 instrument/datetime/open/high/low/close/volume（已前复权）。
    返回：158 因子宽表，index=MultiIndex(datetime, instrument)，列名与 qlib Alpha158 一致。
    调用方用 panel.set_index(['datetime','instrument']).join(feat) 对齐。"""
    need = {"instrument", "datetime", "open", "high", "low", "close", "volume"}
    assert need.issubset(panel.columns), f"missing cols: {need - set(panel.columns)}"
    O = _wide(panel, "open")
    H = _wide(panel, "high")
    L = _wide(panel, "low")
    C = _wide(panel, "close")
    V = _wide(panel, "volume")
    VW = (H + L + C) / 3.0
    idx = C.index
    cols = C.columns
    out = {}

    def put(name, mat):
        out[name] = pd.DataFrame(mat, index=idx, columns=cols).stack()

    # ---- kbar 9 ----
    denom = H - L + EPS
    put("KMID", (C - O) / O)
    put("KLEN", (H - L) / O)
    put("KMID2", (C - O) / denom)
    put("KUP", (H - np.maximum(O, C)) / O)
    put("KUP2", (H - np.maximum(O, C)) / denom)
    put("KLOW", (np.minimum(O, C) - L) / O)
    put("KLOW2", (np.minimum(O, C) - L) / denom)
    put("KSFT", (2 * C - H - L) / O)
    put("KSFT2", (2 * C - H - L) / denom)

    # ---- price 4 (windows=[0]) ----
    for nm, src in (("OPEN0", O), ("HIGH0", H), ("LOW0", L), ("VWAP0", VW)):
        put(nm, src / C)

    # ---- rolling 29 算子 × 5 窗口 ----
    dC = C.diff()
    logV = np.log(V + 1)
    for d in ROLL_WINDOWS:
        put(f"ROC{d}", C.shift(d) / C)
        put(f"MA{d}", C.rolling(d).mean() / C)
        put(f"STD{d}", C.rolling(d).std() / C)
        sl, rsq, resi = _rolling_regression(C, d)
        put(f"BETA{d}", sl)
        put(f"RSQR{d}", rsq)
        put(f"RESI{d}", resi / C)
        put(f"MAX{d}", H.rolling(d).max() / C)
        put(f"MIN{d}", L.rolling(d).min() / C)
        put(f"QTLU{d}", C.rolling(d).quantile(0.8) / C)
        put(f"QTLD{d}", C.rolling(d).quantile(0.2) / C)
        put(f"RANK{d}", pd.DataFrame(
            np.column_stack([_rank_pct(C[c].to_numpy(), d) for c in cols]), index=idx, columns=cols))
        put(f"RSV{d}", (C - L.rolling(d).min()) / (H.rolling(d).max() - L.rolling(d).min() + EPS))
        imax_w = pd.DataFrame(
            np.column_stack([_idx_max(H[c].to_numpy(), d) for c in cols]), index=idx, columns=cols)
        imin_w = pd.DataFrame(
            np.column_stack([_idx_min(L[c].to_numpy(), d) for c in cols]), index=idx, columns=cols)
        put(f"IMAX{d}", imax_w)
        put(f"IMIN{d}", imin_w)
        put(f"IMXD{d}", imax_w - imin_w)
        put(f"CORR{d}", C.rolling(d).corr(logV))
        put(f"CORD{d}", ((C / C.shift(1)).rolling(d).corr(
            np.log(V / V.shift(1) + 1))))
        up = dC.clip(lower=0)
        dn = (-dC).clip(lower=0)
        absd = dC.abs()
        cntp_w = (dC > 0).rolling(d).mean()
        cntn_w = (dC < 0).rolling(d).mean()
        put(f"CNTP{d}", cntp_w)
        put(f"CNTN{d}", cntn_w)
        put(f"CNTD{d}", cntp_w - cntn_w)
        sump_w = up.rolling(d).sum() / (absd.rolling(d).sum() + EPS)
        sumn_w = dn.rolling(d).sum() / (absd.rolling(d).sum() + EPS)
        put(f"SUMP{d}", sump_w)
        put(f"SUMN{d}", sumn_w)
        put(f"SUMD{d}", sump_w - sumn_w)
        put(f"VMA{d}", V.rolling(d).mean() / V)
        put(f"VSTD{d}", V.rolling(d).std() / V)
        ret_abs = (C / C.shift(1) - 1).abs()
        wvma_src = ret_abs * V
        put(f"WVMA{d}", wvma_src.rolling(d).std() / (wvma_src.rolling(d).mean() + EPS))
        dv = V.diff()
        dvup = dv.clip(lower=0)
        dvdn = (-dv).clip(lower=0)
        dvabs = dv.abs()
        vsump_w = dvup.rolling(d).sum() / (dvabs.rolling(d).sum() + EPS)
        vsumn_w = dvdn.rolling(d).sum() / (dvabs.rolling(d).sum() + EPS)
        put(f"VSUMP{d}", vsump_w)
        put(f"VSUMN{d}", vsumn_w)
        put(f"VSUMD{d}", vsump_w - vsumn_w)

    feat = pd.DataFrame(out)
    assert feat.shape[1] == 158, f"expected 158 factors, got {feat.shape[1]}"
    return feat


FACTOR_INDEX = ["datetime", "instrument"]


ALPHA158_COLS = (
    ["KMID", "KLEN", "KMID2", "KUP", "KUP2", "KLOW", "KLOW2", "KSFT", "KSFT2",
     "OPEN0", "HIGH0", "LOW0", "VWAP0"]
    + [f"{op}{d}" for d in ROLL_WINDOWS for op in (
        "ROC", "MA", "STD", "BETA", "RSQR", "RESI", "MAX", "MIN", "QTLU", "QTLD",
        "RANK", "RSV", "IMAX", "IMIN", "IMXD", "CORR", "CORD", "CNTP", "CNTN", "CNTD",
        "SUMP", "SUMN", "SUMD", "VMA", "VSTD", "WVMA", "VSUMP", "VSUMN", "VSUMD")]
)

TAIL_COLS = ["GAP_OPEN", "DIST_LIMIT", "AMPLITUDE", "LIMIT_HIT", "ZT_STREAK"]


def compute_tail_features(panel):
    o = _wide(panel, "open")
    h = _wide(panel, "high")
    low = _wide(panel, "low")
    c = _wide(panel, "close")
    prev = c.shift(1)
    thr = pd.Series({col: limit_up_threshold(str(col)) for col in c.columns})
    limit_px = prev.mul(1.0 + thr, axis=1)
    ret = c / prev - 1.0
    hit = ret.sub(thr - 1e-6, axis=1) >= 0
    cs = hit.cumsum(axis=0)
    values = {
        "GAP_OPEN": o / prev - 1.0,
        "DIST_LIMIT": (limit_px - c) / c,
        "AMPLITUDE": (h - low) / prev,
        "LIMIT_HIT": hit.astype(float),
        "ZT_STREAK": cs - cs.where(~hit).ffill().fillna(0.0),
    }
    feat = pd.DataFrame({name: mat.stack() for name, mat in values.items()})
    return feat[TAIL_COLS]
