"""test_today_str_tz.py — L-13 回归: today_str / cleanup_cutoff 必须返回北京日。

L-13 根因 (2026-10-10 收口): 原 `today_str` 用裸 `datetime.now()` (宿主/容器本地时区),
仅在 TZ=Asia/Shanghai 时恰好等于北京日; UTC 运行时 (裸启 `python run.py` / CI /
误配容器) 拿前一天 → 15:01 确认闸门误判 (停摆或误触发)。修复后显式绑定 TZ_CN,
与 realtime_snapshot.time 写入端同口径。

本测试在「宿主本地时钟 = UTC」假设下, 断言两个日期函数仍返回正确北京日,
防止日后回退成裸 `datetime.now()` (那是 L-13 复发的直接原因)。
"""
from datetime import datetime, timezone, timedelta

from app.market_cn.auto import store


def _fake_datetime_host_is_utc(utc_wall: datetime):
    """构造一个 FakeDateTime: 其无参 now() 返回 UTC 墙钟 (模拟宿主 TZ=UTC)。"""
    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return utc_wall  # 宿主本地 (无 tz) 只能拿到 UTC 墙钟
            # 有 tz: 当作 UTC 瞬时再换算到目标时区 (复刻 datetime.now 真实语义)
            return utc_wall.replace(tzinfo=timezone.utc).astimezone(tz)
    return _FakeDateTime


def test_today_str_independent_of_host_tz(monkeypatch):
    # 此刻真实北京 = 2026-01-02 02:00, 但 host-local now 只能拿到 18:00 UTC (前一日)
    utc_wall = datetime(2026, 1, 1, 18, 0, 0)
    monkeypatch.setattr(store, "datetime", _fake_datetime_host_is_utc(utc_wall))
    # 若回退成裸 datetime.now() (无 tz), 会得到 "2026-01-01" —— 这正是 L-13 错日
    assert store.today_str() == "2026-01-02"


def test_cleanup_cutoff_uses_beijing(monkeypatch):
    utc_wall = datetime(2026, 1, 1, 18, 0, 0)  # 北京当日 = 2026-01-02
    monkeypatch.setattr(store, "datetime", _fake_datetime_host_is_utc(utc_wall))
    # cleanup_cutoff(15) = 北京今日 - 24 日历日 (15 × 1.6)
    assert store.cleanup_cutoff(15) == "2025-12-09"
