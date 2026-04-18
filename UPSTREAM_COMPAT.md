# 上游兼容性与移植指南

> 上游: https://github.com/brokermr810/QuantDinger
> 分支: https://github.com/yuguonet/QuantDinger
> 最后更新: 2026-04-20

---

## 架构差异总览

| 层级 | brokermr810（上游） | yuguonet（分支） |
|------|---------------------|------------------|
| **前端** | 只有预编译 `frontend/dist/`，源码外部维护 | 完整 Vue 源码 `QuantDinger-Vue/` |
| **后端 routes** | 23 个路由文件 | 27 个（多 4 个） |
| **后端模块** | 标准模块 | + `market_store/`、`interfaces/` |
| **nginx** | `frontend/nginx.conf` | `QuantDinger-Vue/deploy/nginx-docker.conf` |

### 分支独有文件/目录

```
QuantDinger-Vue/                          # 整个前端源码
backend_api_python/app/routes/shichang.py # 市场看板后端
backend_api_python/app/routes/xuangu.py   # 选股器后端
backend_api_python/app/routes/agent_analysis.py
backend_api_python/app/routes/agent_blueprint.py
backend_api_python/app/routes/schemas/
backend_api_python/app/market_store/      # 国际市场监控模块
backend_api_python/app/interfaces/
```

### 共享文件（升级时可能冲突）

```
backend_api_python/app/routes/global_market.py  ⚠️ 重点关注
backend_api_python/app/routes/dashboard.py
backend_api_python/app/routes/market.py
backend_api_python/app/data_providers/*
backend_api_python/app/data_sources/*
docker-compose.yml
```

---

## 升级移植原则

### 规则 1：分支独有代码放心改

以下文件/目录是分支新增的，上游不存在，升级时不会冲突：

- `QuantDinger-Vue/src/views/shichang/` 全部
- `QuantDinger-Vue/src/views/xuangu/` 全部
- `backend_api_python/app/routes/shichang.py`
- `backend_api_python/app/routes/xuangu.py`
- `backend_api_python/app/routes/agent_*.py`
- `backend_api_python/app/routes/schemas/`
- `backend_api_python/app/market_store/`
- `backend_api_python/app/interfaces/`

**可以自由修改、新增功能，无需担心上游冲突。**

### 规则 2：global_market.py 不要直接改

这是最关键的共享文件。上游也在持续修改它。

- ❌ 不要在 `global_market.py` 里新增端点或修改逻辑
- ✅ 新功能放到 `shichang.py`（或其他分支独有路由文件）
- ✅ 升级时可以直接用上游版本覆盖 `global_market.py`
- ✅ 如果必须改，记录在下方的「已修改共享文件」清单中

### 规则 3：nginx 配置做差异层

- 保留上游 `frontend/nginx.conf` 不动
- 分支的代理规则维护在 `QuantDinger-Vue/deploy/nginx-docker.conf`
- docker-compose 引用分支的配置文件
- 上游 nginx 更新时，对比差异手动合并安全头等通用配置

### 规则 4：后端路由注册保持隔离

`backend_api_python/app/routes/__init__.py` 中，分支新增路由必须以 append 方式注册，不要修改已有的注册逻辑。升级时只需要把分支独有路由加回去。

示例：
```python
# 保持原有注册不变
from .global_market import global_market_bp
from .dashboard import dashboard_bp
# ... 上游的注册 ...

# 分支独有（追加在最后）
try:
    from .shichang import shichang_bp
    from .xuangu import xuangu_bp
    app.register_blueprint(shichang_bp)
    app.register_blueprint(xuangu_bp)
except ImportError:
    pass  # 分支独有模块不存在时静默跳过
```

### 规则 5：前端构建流程

上游说明原文：`Vue source is maintained separately; build there and sync dist into this repo.`

分支有自己的完整源码，构建后直接部署即可。不需要同步上游的 `frontend/dist/`。

---

## 已修改共享文件清单

记录所有直接修改过的共享文件，方便升级时逐项核对。

| 文件 | 修改日期 | 修改内容 | 升级处理方式 |
|------|---------|---------|-------------|
| （暂无） | | | |

> 每次修改共享文件时，更新此表。

---

## 升级操作流程

### 标准升级（无共享文件冲突）

```bash
# 1. 拉取上游最新代码
git remote add upstream https://github.com/brokermr810/QuantDinger.git 2>/dev/null
git fetch upstream

# 2. 查看上游变更
git log --oneline upstream/main...HEAD -- backend_api_python/

# 3. 检查共享文件是否有上游更新
git diff HEAD..upstream/main -- backend_api_python/app/routes/global_market.py
git diff HEAD..upstream/main -- backend_api_python/app/routes/dashboard.py
git diff HEAD..upstream/main -- docker-compose.yml

# 4. 如果共享文件有更新，逐个对比合并
#    - global_market.py: 用上游版本，检查是否有端点被删除
#    - dashboard.py: 对比合并
#    - docker-compose.yml: 对比合并

# 5. 分支独有文件不受影响，无需处理
```

### 大版本升级（可能有破坏性变更）

```bash
# 1. 在新分支上操作
git checkout -b upgrade-vX.Y upstream/main

# 2. 恢复分支独有文件
git checkout main -- QuantDinger-Vue/
git checkout main -- backend_api_python/app/routes/shichang.py
git checkout main -- backend_api_python/app/routes/xuangu.py
git checkout main -- backend_api_python/app/routes/agent_analysis.py
git checkout main -- backend_api_python/app/routes/agent_blueprint.py
git checkout main -- backend_api_python/app/routes/schemas/
git checkout main -- backend_api_python/app/market_store/
git checkout main -- backend_api_python/app/interfaces/

# 3. 恢复 nginx 配置
git checkout main -- QuantDinger-Vue/deploy/nginx-docker.conf

# 4. 修改 __init__.py 注册分支路由（见规则 4）

# 5. 构建测试
docker-compose build
docker-compose up -d

# 6. 检查日志
docker-compose logs backend | grep -i error
docker-compose logs frontend | grep -i error
```

---

## 建议的长期架构改进

### 1. 同步脚本

创建 `scripts/sync-upstream.sh`，自动化上游同步：

```bash
#!/bin/bash
# scripts/sync-upstream.sh
# 自动同步上游非分支独有文件，跳过分支新增内容

set -e

UPSTREAM_BRANCH="upstream/main"

# 同步的目录（排除分支独有）
SYNC_DIRS=(
    "backend_api_python/app/config"
    "backend_api_python/app/data"
    "backend_api_python/app/data_providers"
    "backend_api_python/app/data_sources"
    "backend_api_python/app/services"
    "backend_api_python/app/utils"
    "scripts"
    "docs"
)

# 不同步的文件（共享但可能被分支修改）
SKIP_FILES=(
    "backend_api_python/app/routes/__init__.py"
    "docker-compose.yml"
)

echo "=== 同步上游文件 ==="
for dir in "${SYNC_DIRS[@]}"; do
    if git diff --quiet HEAD..$UPSTREAM_BRANCH -- "$dir" 2>/dev/null; then
        echo "  ✓ $dir 无变化"
    else
        echo "  ⚠ $dir 有更新，同步中..."
        git checkout $UPSTREAM_BRANCH -- "$dir"
    fi
done

echo ""
echo "=== 需手动检查的共享文件 ==="
for f in "${SKIP_FILES[@]}"; do
    if ! git diff --quiet HEAD..$UPSTREAM_BRANCH -- "$f" 2>/dev/null; then
        echo "  ⚠ $f 上游有变更，需手动合并"
    fi
done

echo ""
echo "完成。请检查变更后提交。"
```

### 2. Feature Flag 架构

在后端加功能开关，方便未来合回上游：

```python
# backend_api_python/app/config/features.py
import os

SHICHANG_ENABLED = os.getenv("FEATURE_SHICHANG", "true") == "true"
XUANGU_ENABLED = os.getenv("FEATURE_XUANGU", "true") == "true"
AGENT_ENABLED = os.getenv("FEATURE_AGENT", "true") == "true"
```

路由注册时检查：
```python
from app.config.features import SHICHANG_ENABLED
if SHICHANG_ENABLED:
    from .shichang import shichang_bp
    app.register_blueprint(shichang_bp)
```

### 3. 测试覆盖

为分支独有功能写集成测试，确保升级后功能不被破坏：

```
tests/
├── test_shichang.py     # 市场看板 API 测试
├── test_xuangu.py       # 选股器测试
├── test_market_store.py # 国际市场监控测试
└── test_upstream_compat.py  # 上游兼容性检查
```

---

## 变更日志

| 日期 | 变更内容 |
|------|---------|
| 2026-04-20 | 初始文档。新增外围市场卡片、修复 nginx 代理、修复多个 Vue 2 兼容性问题。 |
