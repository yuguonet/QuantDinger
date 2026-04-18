# 后端改动清单

需要修改 3 个已有文件 + 新增 1 个文件。

---

## 1. 新增文件

`backend/app/emotion_scheduler.py` ← 已生成在 workspace

---

## 2. 修改 `app/interfaces/cache_file.py`

在 `TABLE_PRIMARY_KEYS` 字典里加一条：

```python
"cnd_emotion_history":      ["timestamp"],
```

在 `TABLE_DTYPES` 字典里加一条：

```python
"cnd_emotion_history": {
    "timestamp": "string",
    "trade_date": "string",
    "emotion": "Int64",
    "up_count": "Int64",
    "down_count": "Int64",
    "limit_up": "Int64",
    "limit_down": "Int64",
    "north_net_flow": "float64",
},
```

同时把 `"cnd_emotion_history"` 加入 `ALLOWED_TABLES`——不过 `ALLOWED_TABLES` 是从 `TABLE_PRIMARY_KEYS` 的 key 自动生成的（`set(TABLE_PRIMARY_KEYS.keys())`），所以只要加了上面的 key 就自动生效。

---

## 3. 修改 `app/routes/shichang.py`

在文件顶部 import 区域加：

```python
from app.emotion_scheduler import EmotionScheduler, query_emotion_history
```

在文件末尾加两个路由 + 启动函数：

```python
# ============================================================
#  情绪历史（前端图表用）
# ============================================================

_emotion_scheduler = None


def start_emotion_collector():
    """启动情绪采集调度器（由 app factory 调用）"""
    global _emotion_scheduler
    if _emotion_scheduler is None:
        _emotion_scheduler = EmotionScheduler(_hub, _hub.db)
    _emotion_scheduler.start()


@shichang_bp.route('/emotion/history')
def emotion_history():
    from flask import request
    date = request.args.get('date')
    hours = request.args.get('hours', type=int)
    data = query_emotion_history(_hub.db, date=date, hours=hours)
    return _make_resp({"history": data})
```

---

## 4. 修改 `app/__init__.py`

在 `create_app()` 函数里，找到这行：

```python
    #将新项目嵌入到该位置,作为后端API启动
```

在这行**下面**加：

```python
    # 启动情绪采集调度器
    try:
        from app.routes.shichang import start_emotion_collector
        start_emotion_collector()
    except Exception as e:
        logger.error(f"Failed to start emotion collector: {e}")
```

---

## 5. 环境变量（.env）

```bash
# 情绪采集总开关（默认关闭）
EMOTION_COLLECTOR_ENABLED=true

# 采集间隔秒数（默认 60，即每分钟）
EMOTION_COLLECTOR_INTERVAL=60

# 保留天数（默认 30）
EMOTION_COLLECTOR_RETENTION_DAYS=30
```

---

## 6. 前端 API

查询接口：`GET /api/shichang/emotion/history`

参数：
- `date=2026-04-19` — 查询指定日期
- `hours=4` — 查询最近 N 小时（优先级高于 date）
- 都不传 → 默认当天

返回格式：
```json
{
  "history": [
    {"time": "09:31", "value": 62},
    {"time": "09:32", "value": 58},
    ...
  ]
}
```

前端对接时，`fetchSentiment()` 里加一个调用即可：
```js
const res = await fetch('/api/shichang/emotion/history?hours=4')
const { history } = await res.json()
// 用 history 渲染图表
```
