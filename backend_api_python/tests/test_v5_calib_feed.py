"""v5 P3 **接线**：条件校准的喂数层（TDD，2026-10-14）

## 本文件测的是什么

不是测 `calibration.fit_cell()` 的数学（那是 `test_v5_calibration.py`），
而是测**接线**：`chain/calibration.py` 这个孤岛现在有没有真被生产链路喂上数。

接线前：`c3p._c3p_pipeline → aggregate(..., coefs=None)` ⇒ **Δp 恒为 0**，
攒再多的 pair 也出不来 β（附录 F A-6）。
接线后：`calib_feed` 负责 `pairs × resolutions → cells → β₀ → coefs`。

## 三条最容易写错的纪律（本文件逐个守）

| 纪律 | 写错的后果 |
|---|---|
| 一 claim 一票（resolution 是留痕，重判会新增行） | **重复计数** ⇒ n 虚涨 ⇒ 门槛虚过 ⇒ 拿噪声当信号 |
| 独立簇 ≠ 名义样本（同日相关） | **高估功效** ⇒ 8 个样本就敢出 β₀ |
| 只收与 p_base 同口径的 horizon | A 尺子量 B ⇒ β₀ 是口径错配的产物，不是错配的修正 |

## 易错处

· 一律 patch **`calib_feed` 本模块属性**（`_load_pairs` / `_load_resolutions`），
  不要去 patch `aggregator.load_pairs` —— 会误触真源（读真实 JSONL / 连真库）。
· 每个用例前**必须清缓存**（`reset_cache` + `QD_CALIB_DIR` 指 tmp），
  否则会读到上一个用例（或真实环境）落盘的系数 ⇒ 假绿。
"""
from __future__ import annotations

import pytest

from app.agent.chain import calib_feed as cf
from app.agent.chain import calibration as cal
from app.agent.chain import schema as sch
from app.agent.chain import weight_feed as wf


# ═══════════════════════════════════════════════════════════════
#  fixture：落盘指 tmp + 清缓存（防假绿）
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("QD_CALIB_DIR", str(tmp_path / "calib"))
    monkeypatch.setenv("QD_CALIB_ENABLED", "1")
    monkeypatch.setenv("QD_CALIB_HORIZON", "T+1")
    cf.reset_cache()


@pytest.fixture()
def sources(monkeypatch):
    """把两个数据源换成可控 fixture，并计数（用于验证缓存真的生效）。"""
    state = {"pairs": [], "res": [], "n_read": 0}

    def _pairs():
        state["n_read"] += 1
        return list(state["pairs"])

    def _res():
        return list(state["res"])

    monkeypatch.setattr(cf, "_load_pairs", _pairs)
    monkeypatch.setattr(cf, "_load_resolutions", _res)
    return state


def _pair(cid, ck="down|low", day="2026-10-01", pb=0.5, hz="T+1"):
    return {"as_of": day, "claim_id": cid, "p_base": pb, "p_final": pb,
            "condition_key": ck,
            "extra": {"horizon": hz, "subject": "000001", "degraded": False}}


def _res(cid, rid, verdict, score=None):
    return {"claim_id": cid, "res_id": rid, "verdict": verdict,
            "verdict_score": score}


def _fill(state, n_days=40, per_day=1, win=0.8, pb=0.5, ck="down|low"):
    """造 n_days × per_day 条样本（每天不同 as_of，保证独立簇 = 天数）。"""
    cid = 1
    for d in range(n_days):
        for k in range(per_day):
            state["pairs"].append(
                _pair(cid, ck=ck, day=f"2026-09-{d % 28 + 1:02d}", pb=pb))
            hit = (k + d) % 5 < round(win * 5)
            state["res"].append(
                _res(cid, rid=cid * 10, verdict=("hit" if hit else "miss")))
            cid += 1
    return state


# ═══════════════════════════════════════════════════════════════
#  口径：只收与 p_base 同口径的 horizon
# ═══════════════════════════════════════════════════════════════

def test_horizon_mismatch_is_dropped():
    """T+3 的判定**不许**去校准 T+1 的 p_base（A 尺子量 B）。"""
    s = cf.build_samples(
        [_pair(1, hz="T+3"), _pair(2, hz="T+1")],
        [_res(1, 10, "hit"), _res(2, 20, "hit")])
    assert [x["claim_id"] for x in s] == [2], f"T+3 样本不该进: {s}"


def test_missing_horizon_is_dropped_not_guessed():
    """horizon 缺失 ⇒ 丢弃，**不猜**（猜出来的配对事后无法识别）。"""
    s = cf.build_samples([_pair(1, hz="")], [_res(1, 10, "hit")])
    assert s == []


def test_undecidable_never_enters_calibration():
    """undecidable / data_missing 是常量污染，**不是半票** ⇒ 不进样本。"""
    s = cf.build_samples([_pair(1)], [_res(1, 10, "undecidable")])
    assert s == []
    s = cf.build_samples([_pair(1)], [_res(1, 10, "delisted")])
    assert len(s) == 1 and s[0]["y"] == 0.0, "delisted 是确定的最差结果，要计入"


def test_pair_without_resolution_is_dropped():
    """没判定的 pair 不能当样本（不知道 y ⇒ β₀ 无从谈起）。"""
    s = cf.build_samples([_pair(1)], [_res(2, 10, "hit")])
    assert s == []


def test_p_base_out_of_range_is_dropped():
    s = cf.build_samples([_pair(1, pb=1.0), _pair(2, pb=0.0)],
                         [_res(1, 10, "hit"), _res(2, 20, "hit")])
    assert s == [], "p_base 必须在 (0,1) 开区间，logit(0/1) 会炸"


# ═══════════════════════════════════════════════════════════════
#  一 claim 一票（G24）
# ═══════════════════════════════════════════════════════════════

def test_one_vote_per_claim_uses_latest_resolution():
    """同一 claim 两条留痕 ⇒ 只认 `res_id` 最大的那条。

    旧留痕 undecidable、新留痕 hit：若按 verdict 过滤来"凑"结果，
    就会变成"只数好看的那条"（与 G18 同类错误）。
    """
    s = cf.build_samples([_pair(1)],
                         [_res(1, 10, "undecidable"), _res(1, 20, "hit")])
    assert len(s) == 1 and s[0]["y"] == 1.0, f"应取最新一条(hit): {s}"


def test_repeated_resolution_does_not_inflate_sample_size():
    """★ 重复留痕 ⇒ **样本数不许虚涨**（n 虚涨会让门槛形同虚设）。"""
    one = cf.build_samples([_pair(1)], [_res(1, 10, "hit")])
    many = cf.build_samples([_pair(1)],
                            [_res(1, 10, "hit"), _res(1, 11, "hit"),
                             _res(1, 12, "hit")])
    assert len(many) == len(one) == 1


# ═══════════════════════════════════════════════════════════════
#  独立簇折算（不是名义样本）
# ═══════════════════════════════════════════════════════════════

def test_one_per_day_means_one_cluster_per_day():
    """每天 1 只 ⇒ 无同日相关 ⇒ 簇数 = 天数。"""
    cells = cf.build_cells([{"condition_key": "k", "as_of": f"d{i}",
                             "p_base": 0.5, "y": 1.0} for i in range(30)])
    assert cells["k"]["n_clusters"] == 30
    assert cells["k"]["n_obs"] == 30


def test_same_day_many_stocks_collapses_to_few_clusters():
    """★ 2 天 × 150 只 ≠ 300 个独立观察 —— 同日共享市场因子 ⇒ 折到个位数。

    用名义样本数当 n 会**严重高估功效**（这是本项目最贵的一类错误）。
    """
    cells = cf.build_cells([{"condition_key": "k", "as_of": f"d{i % 2}",
                             "p_base": 0.5, "y": 1.0} for i in range(300)])
    assert cells["k"]["n_obs"] == 300
    assert cells["k"]["n_clusters"] < 20, (
        f"300 条同一批日子 ⇒ 折算后应远小于 20 簇，实得 {cells['k']['n_clusters']}")


def test_rho_is_env_configurable(monkeypatch):
    """ρ 不许硬编码（它由实测决定，G19 后续会实测反推）。"""
    monkeypatch.setenv("QD_CALIB_RHO", "0.5")
    assert cf.rho() == pytest.approx(0.5)


# ═══════════════════════════════════════════════════════════════
#  系数：样本不足 ⇒ 不出（绝不"估估看"）
# ═══════════════════════════════════════════════════════════════

def test_no_coefs_when_clusters_below_threshold(sources, monkeypatch):
    """★ 冷启动（样本不足）⇒ `coefs_for` 返回 **None**，不是给个估估值。"""
    monkeypatch.setenv("QD_CALIB_MIN_CLUSTERS", "30")
    _fill(sources, n_days=3, per_day=1)
    assert cf.coefs_for("down|low") is None, (
        "3 簇就给 β₀ = 拿噪声当信号，且事后与真系数无法区分")


def test_coefs_sign_follows_actual_minus_predicted(sources, monkeypatch):
    """够样本 ⇒ 给 β₀，方向跟随「实际胜率 − p_base」。"""
    monkeypatch.setenv("QD_CALIB_MIN_CLUSTERS", "5")
    _fill(sources, n_days=10, per_day=1, win=0.8, pb=0.5)
    c = cf.coefs_for("down|low")
    assert c is not None and c["b0"] > 0, f"胜率 0.8 > p_base 0.5 ⇒ 应向上修正: {c}"
    assert c["source_level"] == "cell", f"本格够样本就不该池化: {c}"


def test_pooling_provenance_is_carried_to_coefs(sources, monkeypatch):
    """★ 池化来的系数必须**如实标注**来源层（把借来的当成自己的 = 静默放大）。"""
    monkeypatch.setenv("QD_CALIB_MIN_CLUSTERS", "5")
    _fill(sources, n_days=2, per_day=1, ck="down|low")      # 本格只有 2 簇
    _fill(sources, n_days=10, per_day=1, ck="down|mid")     # 兄弟格 10 簇
    c = cf.coefs_for("down|low")
    assert c is not None, "本格不足、父层足 ⇒ 应池化出系数"
    assert c["source_level"] == "parent"
    assert c["pooled_from"] == "down|*"
    assert c["n_clusters"] == 2, "本格簇数**不许**被借来的簇数覆盖"
    assert c["pooled_n_clusters"] >= 10


def test_disabled_switch_returns_none(sources, monkeypatch):
    monkeypatch.setenv("QD_CALIB_ENABLED", "0")
    assert cf.coefs_for("down|low") is None


def test_source_failure_is_failed_open(sources, monkeypatch):
    """读源抛异常 ⇒ 返回 None（校准是旁路，不许带崩推理）。"""

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(cf, "_load_pairs", boom)
    assert cf.coefs_for("down|low") is None


# ═══════════════════════════════════════════════════════════════
#  缓存与 τ
# ═══════════════════════════════════════════════════════════════

def test_cache_avoids_rescanning_pairs(sources, monkeypatch):
    """缓存期内**不重扫**（每次推理都全量读 JSONL + 连库是不可接受的）。"""
    monkeypatch.setenv("QD_CALIB_CACHE_SEC", "600")
    _fill(sources, n_days=3, per_day=1)
    cf.coefs_for("down|low")
    n1 = sources["n_read"]
    cf.coefs_for("down|low")
    assert sources["n_read"] == n1, "缓存命中 ⇒ 不该再读源"


def test_refresh_bypasses_cache(sources, monkeypatch):
    monkeypatch.setenv("QD_CALIB_CACHE_SEC", "600")
    _fill(sources, n_days=3, per_day=1)
    cf.coefs_for("down|low")
    cf.coefs_for("down|low", refresh=True)
    assert sources["n_read"] >= 2


def test_disk_coefs_reused_within_tau(sources, monkeypatch, tmp_path):
    """★ 落盘系数在 τ 内被复用 ⇒ 跨进程重启也**不会**把 `fitted_at` 刷成 now。

    若每次冷启动都重拟合，`fitted_at` 恒为 now ⇒ τ（季度重估）形同虚设。
    """
    monkeypatch.setenv("QD_CALIB_MIN_CLUSTERS", "5")
    monkeypatch.setenv("QD_CALIB_CACHE_SEC", "0.01")
    _fill(sources, n_days=10, per_day=1, win=0.8, pb=0.5)
    c1 = cf.coefs_for("down|low")
    assert c1 is not None
    # 清进程内缓存（模拟重启），但落盘还在 τ 内 ⇒ 应直接复用
    cf.reset_cache()
    monkeypatch.setattr(cf, "_load_pairs", lambda: [])
    c2 = cf.coefs_for("down|low")
    assert c2 is not None and c2["b0"] == c1["b0"], (
        "τ 内应复用落盘系数，而不是拿空源重拟合")


# ═══════════════════════════════════════════════════════════════
#  verdict 计分的单一真源（防分叉）
# ═══════════════════════════════════════════════════════════════

def test_verdict_score_has_single_source():
    """★ `weight_feed` 与 `calib_feed` 必须读到**同一个对象**。

    2026-10-13 踩过：只改 `_VERDICT_SCORE` 漏掉 SQL 白名单 ⇒ delisted 进不了聚合。
    P3 接线后 verdict 计分有了第二个消费方，真源必须唯一。
    """
    assert wf._VERDICT_SCORE is sch.VERDICT_SCORE
    assert wf._COUNTED_VERDICTS is sch.COUNTED_VERDICTS
    assert set(sch.VERDICT_SCORE) == set(sch.COUNTED_VERDICTS)


def test_calib_feed_uses_schema_score_when_db_score_missing():
    """库里 `verdict_score` 为 NULL ⇒ 取 schema 真源映射（**不猜**）。"""
    s = cf.build_samples([_pair(1)], [_res(1, 10, "partial", score=None)])
    assert s and s[0]["y"] == pytest.approx(0.5)


# ═══════════════════════════════════════════════════════════════
#  报告（人工审计）
# ═══════════════════════════════════════════════════════════════

def test_report_lists_every_cell(sources, monkeypatch):
    monkeypatch.setenv("QD_CALIB_MIN_CLUSTERS", "5")
    _fill(sources, n_days=6, per_day=1, ck="down|low")
    _fill(sources, n_days=6, per_day=1, ck="up|high")
    rep = cf.build_report()
    assert set(rep["cells"]) == {"down|low", "up|high"}
    assert rep["n_samples"] == 12
    assert rep["feed_ver"] == cf.FEED_VER
    assert cal.min_clusters() == 5
