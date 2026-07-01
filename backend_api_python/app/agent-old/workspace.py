# -*- coding: utf-8 -*-
"""
Code Workspace — 每会话隔离的持久化文件存储。

每个 session 拥有独立的工作空间目录，脚本/数据文件/执行结果跨工具调用持久化。
支持：保存脚本复用、多文件分析管道、代码迭代、可复现分析报告。

环境变量：
  AGENT_WORKSPACE_ROOT         — 工作空间根目录（默认 ./workspaces）
  AGENT_WORKSPACE_MAX_FILES    — 每 session 最大文件数（默认 100）
  AGENT_WORKSPACE_MAX_AGE_HOURS — 自动清理年龄（默认 72 小时）

被调用方：
  tools/code_workspace_tools.py → 所有文件操作工具
  tools/code_workspace_tools.py → 工作区工具

公开接口：
  get_workspace(session_id, domain) → CodeWorkspace
  CodeWorkspace.path → Path
  CodeWorkspace.write_file(name, content) → Path
  CodeWorkspace.read_file(name) → str
  CodeWorkspace.list_files() → List[dict]
  CodeWorkspace.run_script(name, args) → dict
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────
WORKSPACE_ROOT = os.getenv(
    "AGENT_WORKSPACE_ROOT",
    os.path.join(os.path.dirname(__file__), "..", "..", "workspaces"),
)
MAX_FILE_SIZE = int(os.getenv("AGENT_WORKSPACE_MAX_FILE_SIZE", str(5 * 1024 * 1024)))  # 5MB
MAX_FILES_PER_SESSION = int(os.getenv("AGENT_WORKSPACE_MAX_FILES", "100"))
MAX_WORKSPACE_AGE_HOURS = int(os.getenv("AGENT_WORKSPACE_MAX_AGE_HOURS", "72"))
CLEANUP_INTERVAL = int(os.getenv("AGENT_WORKSPACE_CLEANUP_INTERVAL", "3600"))


class CodeWorkspace:
    """Manages a single session's code workspace."""

    def __init__(self, session_id: str, root: str = None):
        self.session_id = session_id
        self.root = Path(root or WORKSPACE_ROOT).resolve()
        self.session_dir = self.root / _safe_session_id(session_id)
        self.scripts_dir = self.session_dir / "scripts"
        self.data_dir = self.session_dir / "data"
        self.output_dir = self.session_dir / "output"
        self._write_lock = threading.Lock()
        self._ensure_dirs()

    def _ensure_dirs(self):
        """Create workspace directories if they don't exist."""
        for d in [self.scripts_dir, self.data_dir, self.output_dir]:
            d.mkdir(parents=True, exist_ok=True)

    # ── Script operations with versioning ──────────────────────

    def save_script(self, name: str, code: str, description: str = "") -> Dict[str, Any]:
        """Save a Python script with automatic versioning.

        Each save creates a new version (v1, v2, ...). The latest version
        is symlinked as the canonical name. Previous versions are kept.
        Thread-safe: uses per-workspace lock to prevent version race conditions.

        Args:
            name: Script filename (e.g., "backtest_ma.py")
            code: Python source code
            description: Optional description

        Returns:
            dict with path, size, saved_at, version
        """
        name = _sanitize_filename(name)
        if not name.endswith(".py"):
            name += ".py"

        if len(code.encode()) > MAX_FILE_SIZE:
            return {"error": f"Script too large (max {MAX_FILE_SIZE // 1024}KB)"}

        with self._write_lock:
            # Atomic version increment + write
            version = self._next_version(name)
            versioned_name = _versioned_filename(name, version)
            script_path = self.scripts_dir / versioned_name

            script_path.write_text(code, encoding="utf-8")

            # Update canonical copy
            canonical = self.scripts_dir / name
            canonical.write_text(code, encoding="utf-8")

            # Save version metadata
            self._save_version_meta(name, version, description, len(code), code.count("\n") + 1)

        logger.info("Script saved: %s v%d (%d bytes)", name, version, len(code))
        return {
            "path": str(canonical),
            "name": name,
            "size": len(code),
            "lines": code.count("\n") + 1,
            "description": description,
            "version": version,
        }

    def load_script(self, name: str, version: int = 0) -> Dict[str, Any]:
        """Load a script from the workspace.

        Args:
            name: Script filename
            version: Specific version to load (0 = latest)

        Returns:
            dict with name, code, metadata
        """
        name = _sanitize_filename(name)
        if not name.endswith(".py"):
            name += ".py"

        if version > 0:
            versioned_name = _versioned_filename(name, version)
            script_path = self.scripts_dir / versioned_name
        else:
            script_path = self.scripts_dir / name

        if not script_path.exists():
            return {"error": f"Script '{name}'" + (f" v{version}" if version else "") + " not found"}

        code = script_path.read_text(encoding="utf-8")
        meta = self._load_version_meta(name, version or self._latest_version(name))

        return {
            "name": name,
            "code": code,
            "size": len(code),
            "lines": code.count("\n") + 1,
            "description": meta.get("description", ""),
            "saved_at": meta.get("saved_at_human", ""),
            "version": meta.get("version", version),
        }

    def list_scripts(self) -> List[Dict[str, Any]]:
        """List all scripts in the workspace (latest versions only)."""
        scripts = []
        seen = set()
        for f in sorted(self.scripts_dir.glob("*.py")):
            base = _base_name(f.name)
            if base in seen:
                continue
            seen.add(base)
            version = self._latest_version(base)
            meta = self._load_version_meta(base, version)
            scripts.append({
                "name": base,
                "size": f.stat().st_size,
                "lines": _count_lines(f),
                "description": meta.get("description", ""),
                "saved_at": meta.get("saved_at_human", ""),
                "version": version,
                "versions": self._list_versions(base),
            })
        return scripts

    def list_script_versions(self, name: str) -> List[Dict[str, Any]]:
        """List all versions of a specific script."""
        name = _sanitize_filename(name)
        if not name.endswith(".py"):
            name += ".py"
        return self._list_versions(name)

    def diff_versions(self, name: str, v1: int, v2: int) -> Dict[str, Any]:
        """Get a diff between two script versions."""
        name = _sanitize_filename(name)
        if not name.endswith(".py"):
            name += ".py"

        code1 = self._read_version(name, v1)
        code2 = self._read_version(name, v2)
        if code1 is None:
            return {"error": f"Version v{v1} not found"}
        if code2 is None:
            return {"error": f"Version v{v2} not found"}

        import difflib
        diff = list(difflib.unified_diff(
            code1.splitlines(keepends=True),
            code2.splitlines(keepends=True),
            fromfile=f"{name} v{v1}",
            tofile=f"{name} v{v2}",
            lineterm="",
        ))
        return {"diff": "".join(diff), "name": name, "v1": v1, "v2": v2}

    def delete_script(self, name: str) -> Dict[str, Any]:
        """Delete a script and all its versions."""
        name = _sanitize_filename(name)
        if not name.endswith(".py"):
            name += ".py"

        deleted = []
        for f in self.scripts_dir.glob(f"{name}.*"):
            f.unlink()
            deleted.append(f.name)
        canonical = self.scripts_dir / name
        if canonical.exists():
            canonical.unlink()
            deleted.append(name)

        if not deleted:
            return {"error": f"Script '{name}' not found"}
        return {"deleted": deleted}

    def _next_version(self, name: str) -> int:
        """Get the next version number for a script."""
        existing = list(self.scripts_dir.glob(f"{name}.v*"))
        versions = []
        for f in existing:
            try:
                v = int(f.name.split(".v")[-1])
                versions.append(v)
            except (ValueError, IndexError):
                pass
        return max(versions, default=0) + 1

    def _latest_version(self, name: str) -> int:
        """Get the latest version number for a script."""
        existing = list(self.scripts_dir.glob(f"{name}.v*"))
        versions = []
        for f in existing:
            try:
                v = int(f.name.split(".v")[-1])
                versions.append(v)
            except (ValueError, IndexError):
                pass
        return max(versions, default=0)

    def _list_versions(self, name: str) -> List[Dict[str, Any]]:
        """List all versions of a script with metadata."""
        versions = []
        for f in sorted(self.scripts_dir.glob(f"{name}.v*")):
            try:
                v = int(f.name.split(".v")[-1])
            except (ValueError, IndexError):
                continue
            meta = self._load_version_meta(name, v)
            versions.append({
                "version": v,
                "size": f.stat().st_size,
                "lines": _count_lines(f),
                "saved_at": meta.get("saved_at_human", ""),
                "description": meta.get("description", ""),
            })
        return versions

    def _read_version(self, name: str, version: int) -> Optional[str]:
        """Read a specific version's code."""
        versioned_name = _versioned_filename(name, version)
        path = self.scripts_dir / versioned_name
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def _save_version_meta(self, name: str, version: int, description: str, size: int, lines: int):
        """Save version metadata."""
        meta_path = self.scripts_dir / f"{name}.versions.json"
        all_meta = {}
        if meta_path.exists():
            try:
                all_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        all_meta[f"v{version}"] = {
            "version": version,
            "description": description,
            "size": size,
            "lines": lines,
            "saved_at": time.time(),
            "saved_at_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        meta_path.write_text(json.dumps(all_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_version_meta(self, name: str, version: int) -> Dict[str, Any]:
        """Load version metadata."""
        meta_path = self.scripts_dir / f"{name}.versions.json"
        if meta_path.exists():
            try:
                all_meta = json.loads(meta_path.read_text(encoding="utf-8"))
                return all_meta.get(f"v{version}", {})
            except Exception:
                pass
        return {}

    # ── Data file operations ──────────────────────────────────

    def save_data(self, name: str, content: str, fmt: str = "json") -> Dict[str, Any]:
        """Save a data file (JSON, CSV, text)."""
        name = _sanitize_filename(name)
        ext = f".{fmt}" if not name.endswith(f".{fmt}") else ""
        if ext:
            name += ext

        path = self.data_dir / name
        path.write_text(content, encoding="utf-8")
        return {"path": str(path), "name": name, "size": len(content)}

    def load_data(self, name: str) -> Dict[str, Any]:
        """Load a data file."""
        name = _sanitize_filename(name)
        path = self.data_dir / name
        if not path.exists():
            return {"error": f"Data file '{name}' not found"}
        content = path.read_text(encoding="utf-8")
        return {"name": name, "content": content, "size": len(content)}

    def list_data(self) -> List[Dict[str, Any]]:
        """List all data files."""
        files = []
        for f in sorted(self.data_dir.iterdir()):
            if f.is_file():
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(f.stat().st_mtime)),
                })
        return files

    # ── Output operations ─────────────────────────────────────

    def save_output(self, name: str, content: str) -> Dict[str, Any]:
        """Save an output/result file."""
        name = _sanitize_filename(name)
        path = self.output_dir / name
        path.write_text(content, encoding="utf-8")
        return {"path": str(path), "name": name, "size": len(content)}

    def list_outputs(self) -> List[Dict[str, Any]]:
        """List all output files."""
        files = []
        for f in sorted(self.output_dir.iterdir()):
            if f.is_file():
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(f.stat().st_mtime)),
                })
        return files

    # ── Workspace info ────────────────────────────────────────

    def info(self) -> Dict[str, Any]:
        """Get workspace summary."""
        return {
            "session_id": self.session_id,
            "workspace_dir": str(self.session_dir),
            "scripts": self.list_scripts(),
            "data_files": self.list_data(),
            "output_files": self.list_outputs(),
            "total_files": (
                len(list(self.scripts_dir.glob("*")))
                + len(list(self.data_dir.glob("*")))
                + len(list(self.output_dir.glob("*")))
            ),
        }

    def get_context_summary(self) -> str:
        """Get a text summary of workspace state for injection into agent prompts."""
        scripts = self.list_scripts()
        data_files = self.list_data()
        outputs = self.list_outputs()

        parts = []
        if scripts:
            parts.append("已保存脚本:")
            for s in scripts:
                desc = f" — {s['description']}" if s.get("description") else ""
                ver = f" (v{s['version']})" if s.get("version") else ""
                parts.append(f"  - {s['name']} ({s['lines']}行){ver}{desc}")
        if data_files:
            parts.append("数据文件:")
            for d in data_files:
                parts.append(f"  - {d['name']} ({d['size']}B)")
        if outputs:
            parts.append("输出文件:")
            for o in outputs:
                parts.append(f"  - {o['name']} ({o['size']}B)")

        return "\n".join(parts) if parts else "工作区为空"


# ── Project Templates ──────────────────────────────────────────

TEMPLATES: Dict[str, Dict[str, str]] = {
    "multi_factor": {
        "name": "多因子选股模板",
        "description": "从全市场筛选多因子评分最高的股票",
        "scripts": {
            "multi_factor_screen.py": '''"""
多因子选股策略
从A股全市场筛选因子评分Top N的股票
"""
import pandas as pd
import numpy as np

# === 配置 ===
TOP_N = 20  # 精选数量
FACTORS = {
    "momentum_20d": {"weight": 0.3, "ascending": False},  # 20日动量
    "volume_ratio": {"weight": 0.2, "ascending": False},   # 量比
    "pe_ttm": {"weight": 0.2, "ascending": True},          # 市盈率（越低越好）
    "turnover_rate": {"weight": 0.15, "ascending": False},  # 换手率
    "amplitude": {"weight": 0.15, "ascending": False},      # 振幅
}

# === 获取数据 ===
# data 变量由工作区注入，或通过 DataSourceFactory 获取
# 这里使用 mock 数据演示结构
if isinstance(data, pd.DataFrame):
    df = data
else:
    # 从工作区读取之前保存的数据
    import os
    data_path = Path(DATA_DIR) / "stock_pool.csv"
    if data_path.exists():
        df = pd.read_csv(data_path)
    else:
        print("请先准备 stock_pool.csv 数据文件到 data/ 目录")
        df = pd.DataFrame()

if not df.empty:
    # === 因子标准化 + 打分 ===
    scores = pd.Series(0.0, index=df.index)
    for factor, cfg in FACTORS.items():
        if factor in df.columns:
            vals = df[factor].rank(ascending=cfg["ascending"], pct=True)
            scores += vals * cfg["weight"]

    df["factor_score"] = scores
    df = df.sort_values("factor_score", ascending=False)

    # === 输出结果 ===
    top = df.head(TOP_N)
    print(f"\\n=== 多因子选股 Top {TOP_N} ===")
    print(top[["code", "name", "factor_score"]].to_string(index=False))

    # 保存到输出目录
    top.to_csv(Path(OUTPUT_DIR) / "multi_factor_result.csv", index=False)
    print(f"\\n结果已保存到 output/multi_factor_result.csv")
    result = top.to_dict(orient="records")
''',
        },
    },
    "backtest_pipeline": {
        "name": "回测管道模板",
        "description": "策略回测 + 绩效分析 + 可视化",
        "scripts": {
            "backtest_strategy.py": '''"""
策略回测管道
支持自定义策略函数，输出完整绩效报告
"""
import pandas as pd
import numpy as np
import os

# === 策略参数 ===
INITIAL_CAPITAL = 100000
COMMISSION_RATE = 0.001  # 手续费
STOP_LOSS = -0.05        # 止损线
TAKE_PROFIT = 0.15       # 止盈线

# === 加载数据 ===
data_path = Path(DATA_DIR) / "klines.csv"
if data_path.exists():
    df = pd.read_csv(data_path, parse_dates=["date"])
else:
    print("请将K线数据保存为 data/klines.csv（列: date,open,high,low,close,volume）")
    df = pd.DataFrame()

if not df.empty:
    # === 策略信号 ===
    # 示例：双均线策略
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["signal"] = 0
    df.loc[df["ma5"] > df["ma20"], "signal"] = 1   # 买入
    df.loc[df["ma5"] < df["ma20"], "signal"] = -1  # 卖出

    # === 回测引擎 ===
    position = 0
    capital = INITIAL_CAPITAL
    trades = []
    equity_curve = []

    for i, row in df.iterrows():
        if row["signal"] == 1 and position == 0:
            shares = int(capital * 0.95 / row["close"])
            if shares > 0:
                cost = shares * row["close"] * (1 + COMMISSION_RATE)
                capital -= cost
                position = shares
                trades.append({"date": row["date"], "action": "BUY",
                               "price": row["close"], "shares": shares})

        elif row["signal"] == -1 and position > 0:
            revenue = position * row["close"] * (1 - COMMISSION_RATE)
            capital += revenue
            trades.append({"date": row["date"], "action": "SELL",
                           "price": row["close"], "shares": position})
            position = 0

        equity = capital + position * row["close"]
        equity_curve.append({"date": row["date"], "equity": equity})

    # === 绩效分析 ===
    eq = pd.DataFrame(equity_curve)
    eq["returns"] = eq["equity"].pct_change()
    total_return = (eq["equity"].iloc[-1] / INITIAL_CAPITAL - 1) * 100
    max_dd = ((eq["equity"].cummax() - eq["equity"]) / eq["equity"].cummax()).max() * 100
    sharpe = eq["returns"].mean() / eq["returns"].std() * np.sqrt(252) if eq["returns"].std() > 0 else 0
    win_trades = len([t for t in trades if t["action"] == "SELL"])
    buy_trades = len([t for t in trades if t["action"] == "BUY"])

    report = {
        "total_return_pct": round(total_return, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 2),
        "total_trades": len(trades),
        "buy_trades": buy_trades,
        "sell_trades": win_trades,
        "final_equity": round(eq["equity"].iloc[-1], 2),
    }

    print("\\n=== 回测绩效报告 ===")
    for k, v in report.items():
        print(f"  {k}: {v}")

    # 保存结果
    eq.to_csv(Path(OUTPUT_DIR) / "equity_curve.csv", index=False)
    pd.DataFrame(trades).to_csv(Path(OUTPUT_DIR) / "trades.csv", index=False)

    import json
    with open(Path(OUTPUT_DIR) / "backtest_report.json", "w") as f:
        json.dump(report, f, indent=2)

    print("\\n结果已保存到 output/ 目录")
    result = report
''',
        },
    },
    "data_pipeline": {
        "name": "数据清洗管道",
        "description": "下载 → 清洗 → 特征工程 → 输出",
        "scripts": {
            "data_pipeline.py": '''"""
数据清洗与特征工程管道
输入：原始K线CSV
输出：清洗后的特征矩阵
"""
import pandas as pd
import numpy as np
import os

# === 加载原始数据 ===
raw_path = Path(DATA_DIR) / "raw_klines.csv"
if not raw_path.exists():
    print(f"请将原始数据放到 {raw_path}")
    print("需要列: date, code, open, high, low, close, volume")
    df = pd.DataFrame()
else:
    df = pd.read_csv(raw_path)

if not df.empty:
    print(f"原始数据: {len(df)} 行, {df['code'].nunique()} 只股票")

    # === 清洗 ===
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    df = df[df["volume"] > 0]
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["code", "date"])

    print(f"清洗后: {len(df)} 行")

    # === 特征工程 ===
    for code, group in df.groupby("code"):
        idx = group.index
        close = group["close"]
        volume = group["volume"]

        # 收益率
        df.loc[idx, "return_1d"] = close.pct_change()
        df.loc[idx, "return_5d"] = close.pct_change(5)
        df.loc[idx, "return_20d"] = close.pct_change(20)

        # 波动率
        df.loc[idx, "volatility_20d"] = close.pct_change().rolling(20).std()

        # 量比
        df.loc[idx, "volume_ratio"] = volume / volume.rolling(20).mean()

        # 动量
        df.loc[idx, "momentum_5d"] = close / close.shift(5) - 1
        df.loc[idx, "momentum_20d"] = close / close.shift(20) - 1

        # 均线偏离
        df.loc[idx, "ma5_bias"] = (close - close.rolling(5).mean()) / close.rolling(5).mean()
        df.loc[idx, "ma20_bias"] = (close - close.rolling(20).mean()) / close.rolling(20).mean()

    # === 输出 ===
    output_path = Path(OUTPUT_DIR) / "features.csv"
    df.to_csv(output_path, index=False)
    print(f"\\n特征矩阵已保存: {output_path}")
    print(f"列: {list(df.columns)}")
    result = {"rows": len(df), "columns": list(df.columns), "output": output_path}
''',
        },
    },
}


def apply_template(session_id: str, template_name: str) -> Dict[str, Any]:
    """Apply a project template to a workspace.

    Creates all template scripts and data directories.

    Args:
        session_id: Session ID
        template_name: Template key (e.g., "multi_factor")

    Returns:
        dict with created files
    """
    if template_name not in TEMPLATES:
        return {"error": f"Unknown template: {template_name}", "available": list(TEMPLATES.keys())}

    template = TEMPLATES[template_name]
    ws = get_workspace(session_id)
    created = []

    for script_name, code in template.get("scripts", {}).items():
        result = ws.save_script(script_name, code, description=template["name"])
        if not result.get("error"):
            created.append(script_name)

    return {
        "template": template_name,
        "name": template["name"],
        "description": template["description"],
        "created_scripts": created,
        "workspace": str(ws.session_dir),
    }


def list_templates() -> List[Dict[str, str]]:
    """List available project templates."""
    return [
        {"key": k, "name": v["name"], "description": v["description"],
         "scripts": list(v.get("scripts", {}).keys())}
        for k, v in TEMPLATES.items()
    ]


# ── Cleanup ────────────────────────────────────────────────────

_cleanup_timer: Optional[threading.Timer] = None
_cleanup_lock = threading.Lock()


def cleanup_expired_workspaces(root: str = None, max_age_hours: int = None):
    """Remove workspaces older than max_age_hours."""
    root = Path(root or WORKSPACE_ROOT).resolve()
    max_age = (max_age_hours or MAX_WORKSPACE_AGE_HOURS) * 3600
    now = time.time()
    cleaned = 0
    if not root.exists():
        return 0
    for d in root.iterdir():
        if d.is_dir():
            try:
                age = now - d.stat().st_mtime
                if age > max_age:
                    shutil.rmtree(d)
                    cleaned += 1
            except Exception:
                pass
    if cleaned:
        logger.info("Cleaned up %d expired workspaces", cleaned)
    return cleaned


def start_cleanup_scheduler():
    """Start periodic cleanup of expired workspaces."""
    global _cleanup_timer
    with _cleanup_lock:
        if _cleanup_timer is not None:
            return  # Already running

        def _run():
            try:
                cleanup_expired_workspaces()
            except Exception as e:
                logger.error("Workspace cleanup error: %s", e)
            finally:
                _schedule_next()

        def _schedule_next():
            global _cleanup_timer
            with _cleanup_lock:
                _cleanup_timer = threading.Timer(CLEANUP_INTERVAL, _run)
                _cleanup_timer.daemon = True
                _cleanup_timer.start()

        _schedule_next()
        logger.info("Workspace cleanup scheduler started (interval=%ds, max_age=%dh)",
                     CLEANUP_INTERVAL, MAX_WORKSPACE_AGE_HOURS)


def stop_cleanup_scheduler():
    """Stop the cleanup scheduler."""
    global _cleanup_timer
    with _cleanup_lock:
        if _cleanup_timer is not None:
            _cleanup_timer.cancel()
            _cleanup_timer = None


# ── Helpers ────────────────────────────────────────────────────

def _safe_session_id(session_id: str) -> str:
    """Sanitize session ID for use as directory name."""
    safe = re.sub(r"[^a-zA-Z0-9_\-.]", "_", session_id)
    return safe[:64] or "default"


def _sanitize_filename(name: str) -> str:
    """Sanitize filename to prevent path traversal."""
    name = os.path.basename(name)
    name = re.sub(r"[^a-zA-Z0-9_\-.]", "_", name)
    return name[:128] or "unnamed"


def _versioned_filename(name: str, version: int) -> str:
    """Generate a versioned filename."""
    return f"{name}.v{version}"


def _base_name(versioned: str) -> str:
    """Extract base name from a versioned filename."""
    m = re.match(r"^(.+?)\.v\d+$", versioned)
    return m.group(1) if m else versioned


def _count_lines(path: Path) -> int:
    """Count lines in a file."""
    try:
        return len(path.read_text(encoding="utf-8").split("\n"))
    except Exception:
        return 0


# ── Singleton access ───────────────────────────────────────────

_workspaces: Dict[str, CodeWorkspace] = {}
_lock = threading.Lock()


def get_workspace(session_id: str = "", user_id: int = 0, domain: str = "") -> CodeWorkspace:
    """Get or create a workspace with per-user, per-domain isolation.

    Composite key: {user_id}:{domain}:{session_id}
    - user_id: isolates different users' workspaces
    - domain: isolates different analysis domains (analysis, backtest, screening, etc.)
    - session_id: isolates different sessions within same user+domain

    If user_id/domain not provided, falls back to tool_context auto-detection.
    Directory structure: workspaces/{user_id}/{domain}/{session_id}/
    """
    # Auto-detect from context if not provided
    if not user_id or not domain:
        try:
            from app.agent.tool_context import get_user_id, get_tool_context
            if not user_id:
                user_id = get_user_id()
            if not domain:
                ctx = get_tool_context()
                domain = ctx.get("domain", "default")
        except Exception:
            pass

    user_id = user_id or 0
    domain = (domain or "default").replace("/", "_").replace("\\", "_")
    session_id = session_id or "default"

    # Composite key for in-memory cache
    cache_key = f"{user_id}:{domain}:{session_id}"

    with _lock:
        if cache_key not in _workspaces:
            # Directory structure: workspaces/{user_id}/{domain}/{session_id}/
            ws = CodeWorkspace(session_id)
            # Override session_dir to include user_id and domain
            ws.session_dir = ws.root / str(user_id) / domain / _safe_session_id(session_id)
            ws.scripts_dir = ws.session_dir / "scripts"
            ws.data_dir = ws.session_dir / "data"
            ws.output_dir = ws.session_dir / "output"
            ws._ensure_dirs()
            _workspaces[cache_key] = ws
        return _workspaces[cache_key]
