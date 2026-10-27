# -*- coding: utf-8 -*-
"""mootdx 免扫描直连（L0）+ 保活 daemon（L1）单测（2026-10-25；2026-10-27 改扫描榜）。

背景：Quotes.factory(bestip=True) 无条件串行扫 146 hosts ≈ 74s/进程冷启，且
选出的"最快"可能是 bars 恒空的僵尸（2026-10-27 日志两轮全废）。
L0 候选顺序：「进程内 _last_server → ~/.mootdx/config.json BESTIP.HQ」免扫描
直连 → HQ 主机扫描榜（并发 TCP 探测）**逐台**过 bars 数据面验证（协议探活
不可信：实测 get_security_count 0.03s 通但 bars 恒空）；判死进 _bad_servers。
L1 由 scheduler.start() 起保活线程，TTL 80% 年龄提前重建。

锁定的行为契约（防回归）：
  A. 候选顺序：免扫描直连（last_server/config）→ HQ 扫描榜；直连命中不触发扫描
  B. 数据面验证失败 ⇒ 标记 bad + 试下一台；bad 不重复试
  C. 冷启（无任何直连候选）仍走 HQ 扫描榜找服务器，不能把冷启路径改丢
  D. 保活：env/参数开关、幂等、周期语义
"""
import os
import sys
import threading
import time
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.utils.mootdx_client as mc


@contextmanager
def _scoped_factory(ret=None):
    """Quotes.factory 假实现，只记录**本线程**调用（mock.call 形态，.kwargs 可用）。

    全量跑时其它测试遗留的 dispatch 工作线程可能并发建 mootdx 连接，与本
    文件共享 Quotes.factory 全局 patch，call_args_list 会被外来调用污染
    （2026-10-27 全量实测：assert 2 == 1 即此因）⇒ 按线程过滤后再断言。
    ret: 每次调用返回的客户端（None 时每次给新 MagicMock）。
    """
    tid = threading.get_ident()
    calls = []

    def _fake(*a, **k):
        if threading.get_ident() == tid:
            calls.append(mock.call(*a, **k))
        return ret if ret is not None else mock.MagicMock(name="cli")

    with mock.patch("mootdx.quotes.Quotes.factory", side_effect=_fake):
        yield calls


def _reset_state():
    mc._last_server = None
    mc._bad_servers = set()
    mc._client = None
    mc._client_ts = 0
    mc._hq_scan_cache = None


def test_l0_direct_connect_preferred_over_bestip():
    """A1: _last_server 数据面验证通过 ⇒ 只直连它，不触发 HQ 扫描。"""
    _reset_state()
    with mock.patch.object(mc, "_read_config_bestip", return_value=None), \
         mock.patch.object(mc, "_probe_data_plane", return_value=True), \
         mock.patch.object(mc, "_scan_hq_candidates", return_value=[("9.9.9.9", 7709)]) as scan, \
         _scoped_factory() as calls:
        mc._last_server = ("10.0.0.1", 7709)
        cli = mc._create_client()
        assert cli is not None
        assert len(calls) == 1
        assert calls[0].kwargs.get("server") == ("10.0.0.1", 7709)
        assert not any(c.kwargs.get("bestip") for c in calls)
        scan.assert_not_called()                     # 直连命中 ⇒ 扫描榜都不用跑


def test_l0_dead_plane_marks_bad_and_falls_through():
    """A2: 直连候选数据面死 ⇒ 标记 bad ⇒ 回落 HQ 扫描榜逐台试。"""
    _reset_state()
    with mock.patch.object(mc, "_read_config_bestip", return_value=("10.0.0.2", 7709)), \
         mock.patch.object(mc, "_probe_data_plane", return_value=False), \
         mock.patch.object(mc, "_scan_hq_candidates",
                           return_value=[("9.9.9.9", 7709)]), \
         _scoped_factory() as calls:
        mc._last_server = ("10.0.0.1", 7709)
        mc._create_client()
        tried = [c.kwargs.get("server") for c in calls if c.kwargs.get("server")]
        assert tried == [("10.0.0.1", 7709), ("10.0.0.2", 7709), ("9.9.9.9", 7709)]
        assert mc._bad_servers == {("10.0.0.1", 7709), ("10.0.0.2", 7709),
                                   ("9.9.9.9", 7709)}   # 扫描榜僵尸同标
        assert not any(c.kwargs.get("bestip") for c in calls)


def test_l0_bad_server_not_retried():
    """A3: _bad_servers 里的 server 不再直连（进程内判死一次就够）。"""
    _reset_state()
    with mock.patch.object(mc, "_read_config_bestip", return_value=("10.0.0.2", 7709)), \
         mock.patch.object(mc, "_probe_data_plane", return_value=False), \
         mock.patch.object(mc, "_scan_hq_candidates",
                           return_value=[("10.0.0.2", 7709)]), \
         _scoped_factory() as calls:
        mc._last_server = None
        mc._bad_servers = {("10.0.0.2", 7709)}
        mc._create_client()
        tried = [c.kwargs.get("server") for c in calls if c.kwargs.get("server")]
        assert tried == []


def test_l0_cold_start_uses_hq_scan():
    """A4: 无任何直连候选（冷启第一台）必须走 HQ 扫描榜找服务器，不许丢冷启路径。"""
    _reset_state()
    with mock.patch.object(mc, "_read_config_bestip", return_value=None), \
         mock.patch.object(mc, "_probe_data_plane", return_value=True), \
         mock.patch.object(mc, "_scan_hq_candidates",
                           return_value=[("9.9.9.9", 7709)]) as scan, \
         _scoped_factory() as calls:
        cli = mc._create_client()
        assert cli is not None
        scan.assert_called()
        assert calls[0].kwargs.get("server") == ("9.9.9.9", 7709)
        assert not any(c.kwargs.get("bestip") for c in calls)


def test_l0_config_bestip_parsing():
    """config.json BESTIP.HQ 解析：合法/缺键/损坏都不炸。"""
    with mock.patch("os.path.isfile", return_value=True), \
         mock.patch("builtins.open", mock.mock_open(read_data='{"BESTIP": {"HQ": ["1.2.3.4", 7709]}}')), \
         mock.patch("json.load", return_value={"BESTIP": {"HQ": ["1.2.3.4", 7709]}}):
        assert mc._read_config_bestip() == ("1.2.3.4", 7709)
    with mock.patch("os.path.isfile", return_value=False):
        assert mc._read_config_bestip() is None
    with mock.patch("os.path.isfile", side_effect=OSError("boom")):
        assert mc._read_config_bestip() is None


def test_l1_keepalive_switch_and_idempotent():
    """D: interval≤0 关闭；正常启动 True；重复启动幂等。

    起真 daemon 线程但周期 999999s —— 循环体不会执行，pytest 退出即杀，
    零副作用；不 mock 全局 threading（会波及其他测试的线程创建）。
    """
    assert mc.start_mootdx_keepalive(interval_sec=0) is False
    assert mc.start_mootdx_keepalive(interval_sec=-1) is False
    mc._KEEPALIVE_STARTED = False
    assert mc.start_mootdx_keepalive(interval_sec=999999) is True
    assert mc.start_mootdx_keepalive(interval_sec=999999) is True   # 幂等


def test_l1_keepalive_env_off():
    """D: env QD_MOOTDX_KEEPALIVE_SEC=0 显式关闭；合法值启动。"""
    mc._KEEPALIVE_STARTED = False
    with mock.patch.dict(os.environ, {"QD_MOOTDX_KEEPALIVE_SEC": "0"}):
        assert mc.start_mootdx_keepalive() is False
    mc._KEEPALIVE_STARTED = False
    with mock.patch.dict(os.environ, {"QD_MOOTDX_KEEPALIVE_SEC": "999999"}):
        assert mc.start_mootdx_keepalive() is True
    # 非法值回落缺省并启动
    mc._KEEPALIVE_STARTED = False
    with mock.patch.dict(os.environ, {"QD_MOOTDX_KEEPALIVE_SEC": "abc"}):
        assert mc.start_mootdx_keepalive() is True


def test_l1_keepalive_should_rebuild_pure():
    """D: 纯判定（now 注入，零并发敏感）——80% 阈值 + 从未建过视为 stale。"""
    now = 1000.0
    # 从未建过客户端（ts=0）⇒ 该建
    mc._client_ts = 0
    assert mc._keepalive_should_rebuild(now) is True
    # 79% 年龄 ⇒ 不动；80% ⇒ 重建；81% ⇒ 重建
    for ratio, expect in ((0.79, False), (0.80, True), (0.81, True)):
        mc._client_ts = now - mc.CLIENT_TTL * ratio
        assert mc._keepalive_should_rebuild(now) is expect, f"ratio={ratio}"
    # now=None 走 time.time()（真时钟：刚设的新 ts 不该重建）
    mc._client_ts = time.time()
    assert mc._keepalive_should_rebuild() is False
    _reset_state()


def test_l1_keepalive_loop_rebuilds_stale_singleton():
    """D: 循环体——stale 时置空并调 get_client 重建；新鲜时 continue。

    sleep_fn 注入假 sleep（第一轮返回、第二轮 KeyboardInterrupt 退出），
    不 patch 全局 time.sleep——后者会唤醒调度器主循环等无关线程
    （2026-10-25 全量实测：单跑绿/全量红的根因就是预热 daemon 线程
    并发改 _client_ts）。
    """
    _reset_state()
    rebuilt = {"n": 0}

    def _fake_get_client():
        rebuilt["n"] += 1
        return None

    def _sleep_two_rounds(_sec):
        if not _sleep_two_rounds.done:
            _sleep_two_rounds.done = True
            return
        raise KeyboardInterrupt
    _sleep_two_rounds.done = False

    with mock.patch.object(mc, "get_client", _fake_get_client):
        mc._client_ts = time.time() - mc.CLIENT_TTL * 0.9   # 90% ⇒ 重建
        try:
            mc._keepalive_loop(1, sleep_fn=_sleep_two_rounds)
        except KeyboardInterrupt:
            pass
        assert rebuilt["n"] == 1 and mc._client is None
        # 新鲜单例（10%）⇒ 不重建
        rebuilt["n"] = 0
        _sleep_two_rounds.done = False
        mc._client_ts = time.time() - mc.CLIENT_TTL * 0.1
        try:
            mc._keepalive_loop(1, sleep_fn=_sleep_two_rounds)
        except KeyboardInterrupt:
            pass
        assert rebuilt["n"] == 0
    _reset_state()


def test_l0_strategy3_bestip_also_verified():
    """A5: 扫描榜客户端逐台过数据面验证——僵尸不进单例、全死返 None。

    2026-10-25 全量实测：不验证时 TCP 通/bars 空的僵尸会进单例并把
    _client_ts 刷成 now（每次 bars 白等 4.4s）。2026-10-27：原 bestip=True
    单发（只验第一名）改为扫描榜逐台验证。
    """
    _reset_state()
    with mock.patch.object(mc, "_read_config_bestip", return_value=None), \
         mock.patch.object(mc, "_scan_hq_candidates",
                           return_value=[("9.9.9.9", 7709), ("8.8.8.8", 7709)]), \
         mock.patch.object(mc, "_probe_data_plane", side_effect=[False, True]), \
         _scoped_factory() as calls:
        cli = mc._create_client()
        assert cli is not None                     # 第二台（数据面活）入单例
        assert ("9.9.9.9", 7709) in mc._bad_servers   # 僵尸标 bad
        assert mc._last_server == ("8.8.8.8", 7709)
        tried = [c.kwargs.get("server") for c in calls if c.kwargs.get("server")]
        assert tried == [("9.9.9.9", 7709), ("8.8.8.8", 7709)]
        assert not any(c.kwargs.get("bestip") for c in calls)
    # 全部数据面死 ⇒ None，且都进 bad（不入单例）
    _reset_state()
    with mock.patch.object(mc, "_read_config_bestip", return_value=None), \
         mock.patch.object(mc, "_scan_hq_candidates",
                           return_value=[("9.9.9.9", 7709), ("8.8.8.8", 7709)]), \
         mock.patch.object(mc, "_probe_data_plane", return_value=False), \
         _scoped_factory():
        assert mc._create_client() is None
        assert ("9.9.9.9", 7709) in mc._bad_servers
        assert ("8.8.8.8", 7709) in mc._bad_servers
    _reset_state()
