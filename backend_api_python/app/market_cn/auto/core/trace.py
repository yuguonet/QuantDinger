"""core/trace.py — 门原因通道 TraceSink（改进方案 §2.4，P0 落地）。

定位（Excel 比喻的「追踪引用单元格」）：调试信息的**收集端归内核**。
门聚合器本来就在返回 `(ok, reason)`（判定内容自带理由），策略只把它**一行**交给本
sink —— 策略文件中不出现任何调试逻辑（无 stage 排名、无 sample 组装）。

契约（与 contract.py 的约定一致）：
  - 注入方式: `DayInput.ctx["_trace"]`（可选键；无 sink 时零开销、零行为差异）；
  - 策略义务: 门聚合器返回 (ok, reason)，落选/命中时一行 `sink.gate(ok, reason, ...)`；
  - 本 sink 职责: 收集、JSONL 导出、（后续 P2）渲染 —— 策略与视图都不碰存储细节。

易错点:
  - **不要在 sink 里做判定**：它只记录；任何"顺手算一下"都会变成第二份门逻辑。
  - `gate()` 返回入参 `ok`（透传），允许 `if not sink.gate(ok, r): ...` 链式写法，
    但**不鼓励**——保持"记录与控制流无关"。
  - JSONL 行格式与 probe 时代的字段命名解耦（probe 兼容导出是 P2 的适配器职责，
    不在本模块）；本模块只保证自身格式稳定（一行一记录，`ensure_ascii=False`）。

契约约定（改进方案 §2.4/§2.6d；因内核体量门禁（test_kernel_size）登记于本模块，
contract.py 仅留指针）：
  - 门**判定**（门表 GateEvaluator 一层）返回 `(ok, reason)`；叶子谓词保持
    bool 不强求。reason 是策略内部语义串，只作审计，不是展示档位。
  - **payload 保留键名表**（新策略照此写，旧策略由 replay/trade_map 兑）：
    交易载荷（stage=exec/exit）: entry_date / entry_price / exit_date / exit_price /
    exit_day / exit_reason（或 reason）/ return_pct（或 exit_ret）/ peak_return_pct /
    exec_basis；信号载荷（stage=ready）: score / price / label / extra。
    载荷命名统一（策略侧改名）不在改进方案内；新代码以本表为准。
"""

from __future__ import annotations

import json
import os


class TraceSink:
    """一次回放/一轮判定的门原因收集器。

    记录格式（一行一条）:
        {"t": <date>, "ok": bool, "reason": str|None, ...meta}

    - `t` 由 `begin_day(date)` 设定（fold 每日调一次；缺省 None）；
    - `meta` 开放字典：候选标识（cand）、gap 等策略自定字段，原样保留（展示层不解释）。
    """

    def __init__(self, code: str = "", strategy: str = ""):
        self.code = code
        self.strategy = strategy
        self.records: list[dict] = []
        self._day: str | None = None

    # ---- 记录面（策略唯一接触的三个方法） ----
    def begin_day(self, date: str) -> None:
        """fold 推进到某日时调用（可选；不调则记录的 t 为 None）。"""
        self._day = str(date)[:10]

    def gate(self, ok: bool, reason: str | None, **meta) -> bool:
        """记录一次门判定（一行胶水的唯一入口）。返回入参 ok（透传）。"""
        rec: dict = {"t": self._day, "ok": bool(ok), "reason": reason}
        if self.code:
            rec.setdefault("code", self.code)
        if meta:
            rec.update(meta)
        self.records.append(rec)
        return ok

    def note(self, kind: str, **meta) -> None:
        """非门类标注（如 no_signal 汇总、data_gap）。kind=事件种类，meta 自定。"""
        rec: dict = {"t": self._day, "ok": True, "reason": None, "kind": kind}
        if self.code:
            rec.setdefault("code", self.code)
        if meta:
            rec.update(meta)
        self.records.append(rec)

    # ---- 导出面（内核/视图用） ----
    def to_jsonl(self) -> str:
        """一行一记录的 JSONL 文本（空收集 = 空串）。"""
        return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self.records)

    def dump_jsonl(self, path: str) -> int:
        """追加写到文件（同文件可多次 dump，天然分段）。返回写出行数。"""
        txt = self.to_jsonl()
        if not txt:
            return 0
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(txt)
        return len(self.records)

    def clear(self) -> None:
        """清空已收集（保留 code/strategy 身份）。"""
        self.records.clear()
