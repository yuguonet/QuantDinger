#!/usr/bin/env python3
"""
QuantDinger IndicatorStrategy → YAML 反向转换器
================================================

将 IndicatorStrategy Python 代码反向解析为 YAML 策略描述文件。
与 yaml_to_indicator.py 形成双向转换对。

转换流程：
    .py 文件 → IndicatorParser 解析 @param/@strategy/指标逻辑
              → YAMLGenerator 生成 YAML 结构
              → 输出 .yaml 文件

核心能力：
    1. 提取 @param 参数声明（名称、类型、默认值、描述）
       支持类型：int/integer, float/double, bool/boolean, str/string
    2. 提取 @strategy 风控注解（支持可选冒号分隔）
       合法 key：stopLossPct, takeProfitPct, entryPct,
       trailingEnabled, trailingStopPct, trailingActivationPct, tradeDirection
    3. 识别 13 种指标（从源码 strategy_compiler.py / market_data_collector.py 对齐）
       ema, sma, rsi, macd, kdj, bollinger, atr, supertrend, vwap, volume, donchian, pivot
    4. 提取买卖信号的条件逻辑（df['buy'] / df['sell'] 赋值）
    5. 三层分类推断（从源码 YAML 策略定义对齐）
       第一层：策略名称语义检测 → framework / pattern
       第二层：买卖条件语义分析 → reversal / trend
       第三层：指标组合基础映射 → 兜底
    6. 生成结构化 instructions 文本

分类体系（4 类，与 backend_api_python/strategies/*.yaml 对齐）：
    trend      — 趋势跟随（EMA排列、MACD金叉、放量突破等）
    reversal   — 反转/均值回归（RSI超卖、KDJ+VWAP、布林带触及等）
    framework  — 框架类（缠论、箱体震荡、情绪周期、波浪理论）
    pattern    — 形态类（一阳夹三阴等 K 线形态）

依赖：
    Python >= 3.8, pyyaml

用法：
    # ── 转换单个 .py 文件为 YAML ──
    python3 indicator_to_yaml.py output_strategies/ema_rsi_pullback.py

    # ── 批量转换目录 ──
    python3 indicator_to_yaml.py --dir output_strategies/ --output yaml_output/

    # ── 输出 JSON 格式（不写文件）──
    python3 indicator_to_yaml.py output_strategies/ema_rsi_pullback.py --json

    # ── 运行测试套件 ──
    python3 test_robustness.py
    python3 test_roundtrip_real.py
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml


# ============================================================
# 1. 指标识别模式（与 strategy_compiler.py / market_data_collector.py 对齐）
# ============================================================

INDICATOR_PATTERNS: Dict[str, List[str]] = {
    'ema': [
        r"\bema\b\s*=",
        r"df\[['\"].*ema.*['\"]\]",
        r"\.ewm\s*\(\s*span\s*=",
        r"指数移动平均",
        r"EMA\d+",
    ],
    'sma': [
        r"\bma\b\s*=",
        r"df\[['\"].*ma[_]?['\"]\]",
        r"\.rolling\s*\(\s*window\s*=\s*\d+\s*\)\.mean\(\)",
        r"\.rolling\s*\(\s*\d+\s*(?:,\s*\w+\s*=\s*\w+)?\s*\)\.mean\(\)",
        r"均线",
        r"乖离率",
        r"\bsma\b",
    ],
    'rsi': [
        r"\brsi\b",
        r"100\s*-\s*\(?\s*100\s*/",
        r"gain.*loss",
        r"delta\.where",
        r"相对强弱",
        r"avg_gain.*avg_loss",
        r"\.clip\s*\(\s*lower\s*=\s*0\s*\)",
    ],
    'macd': [
        r"\bmacd\b",
        r"diff.*dea",
        r"histogram",
        r"ema_fast.*ema_slow",
        r"DIF.*DEA",
        r"\bdif\b.*\bdea\b",
        r"exp12.*exp26",
        r"span=12.*span=26",
    ],
    'kdj': [
        r"\bkdj\b",
        r"\brsv\b",
        r"3\s*\*\s*k\s*-\s*2\s*\*\s*d",
        r"low_min.*rolling.*min",
        r"high_max.*rolling.*max",
        r"kdj_j",
        r"kdj_k",
        r"kdj_d",
    ],
    'bollinger': [
        r"bb_\w+",
        r"bollinger",
        r"\.rolling.*\.std\(\)",
        r"布林",
        r"BB_upper",
        r"BB_lower",
        r"BB_middle",
    ],
    'atr': [
        r"\batr\b",
        r"\btr\b.*maximum",
        r"high.*low.*shift",
        r"真实波幅",
        r"_calc_atr",
        r"Wilder.*ATR",
        r"atr_period",
    ],
    'supertrend': [
        r"\bsupertrend\b",
        r"st_trend",
        r"st_upper",
        r"st_lower",
        r"basic_upper",
        r"basic_lower",
        r"hl2.*\+.*multiplier.*atr",
    ],
    'vwap': [
        r"\bvwap\b",
        r"cumsum.*volume",
        r"close.*volume.*cumsum",
        r"成交量加权",
    ],
    'volume': [
        r"volume.*rolling",
        r"vol_ma",
        r"vol_ratio",
        r"成交量",
        r"量比",
        r"volume_ratio",
    ],
    'donchian': [
        r"\bdonchian\b",
        r"rolling.*window.*\.max\(\)",
        r"rolling.*window.*\.min\(\)",
        r"唐奇安",
        r"upper.*lower.*channel",
    ],
    'pivot': [
        r"\bpivot\b",
        r"\br1\b.*\bs1\b",
        r"prev_high.*prev_low.*prev_close",
        r"枢轴",
    ],
}


# ============================================================
# 2. 指标策略代码解析器
# ============================================================

class IndicatorParser:
    """解析 IndicatorStrategy Python 代码，提取结构化信息。"""

    @staticmethod
    def parse(py_path: str) -> Dict[str, Any]:
        """解析单个 .py 文件。"""
        path = Path(py_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {py_path}")

        code = path.read_text(encoding='utf-8')
        return IndicatorParser.parse_code(code, source_file=str(path))

    @staticmethod
    def parse_code(code: str, source_file: str = '<string>') -> Dict[str, Any]:
        """解析代码字符串。"""
        result = {
            'source_file': source_file,
            'strategy_name': IndicatorParser._extract_strategy_name(code),
            'params': IndicatorParser._extract_params(code),
            'strategy_annotations': IndicatorParser._extract_strategy_annotations(code),
            'indicators': IndicatorParser._detect_indicators(code),
            'buy_conditions': IndicatorParser._extract_buy_conditions(code),
            'sell_conditions': IndicatorParser._extract_sell_conditions(code),
            'signal_columns': IndicatorParser._extract_signal_columns(code),
            'imports': IndicatorParser._extract_imports(code),
            'code_lines': len(code.splitlines()),
            'raw_code': code,
        }
        return result

    @staticmethod
    def _extract_strategy_name(code: str) -> str:
        """从头部注释提取策略名。"""
        m = re.search(r"#\s*IndicatorStrategy:\s*(.+)", code)
        if m:
            return m.group(1).strip()
        # fallback: 从文件名推断
        return "unknown_strategy"

    @staticmethod
    def _extract_params(code: str) -> List[Dict[str, Any]]:
        """提取 @param 声明。"""
        params = []
        pattern = r"#\s*@param\s+(\w+)\s+(\w+)\s+(\S+)\s*(.*)"
        for m in re.finditer(pattern, code):
            name = m.group(1)
            ptype = m.group(2)
            default = m.group(3)
            desc = m.group(4).strip()

            # 类型转换
            try:
                if ptype in ('int', 'integer'):
                    default = int(default)
                    ptype = 'int'
                elif ptype in ('float', 'double'):
                    default = float(default)
                    ptype = 'float'
                elif ptype in ('bool', 'boolean'):
                    default = default.lower() in ('true', '1', 'yes')
                    ptype = 'bool'
                else:
                    ptype = 'str'
            except (ValueError, TypeError):
                pass

            params.append({
                'name': name,
                'type': ptype,
                'default': default,
                'description': desc,
            })
        return params

    @staticmethod
    def _extract_strategy_annotations(code: str) -> Dict[str, Any]:
        """提取 @strategy 注解。"""
        annotations = {}
        # 支持可选冒号分隔（与 indicator_params.py 的 StrategyConfigParser 对齐）
        # 注意：用 [ \t]* 代替 \s* 避免跨行匹配
        pattern = r"#\s*@strategy\s+(\w+)\s*:?[ \t]*(\S+)[ \t]*(.*)"
        for m in re.finditer(pattern, code):
            key = m.group(1)
            value = m.group(2).strip()
            # 类型推断
            if value.lower() in ('true', 'false'):
                value = value.lower() == 'true'
            else:
                try:
                    value = float(value)
                    if value == int(value):
                        value = int(value)
                except ValueError:
                    pass
            annotations[key] = value
        return annotations

    @staticmethod
    def _detect_indicators(code: str) -> List[str]:
        """检测代码中使用的指标。"""
        code_lower = code.lower()
        detected = []
        for indicator, patterns in INDICATOR_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, code_lower):
                    detected.append(indicator)
                    break
        return sorted(set(detected))

    @staticmethod
    def _extract_conditions_with_prefix(code: str, prefix: str) -> str:
        """通用条件提取：先找 df['xxx'] = ... 赋值，若是纯变量则追踪到变量定义。"""
        import re as _re

        # 1. 找所有 df['xxx'] = ... 赋值
        pattern = _re.compile(
            r"df\[['\"]" + prefix + r"['\"]\]\s*=\s*(.+?)(?:\.fillna|\.astype|\s*$)",
            _re.MULTILINE | _re.DOTALL
        )
        for m in pattern.finditer(code):
            condition = m.group(1).strip()
            condition = _re.sub(r'\.fillna\(.*?\)', '', condition)
            condition = _re.sub(r'\.astype\(.*?\)', '', condition)
            condition = condition.strip()
            # 跳过纯变量赋值（如 df['buy'] = buy）
            if _re.match(r'^[a-zA-Z_]\w*$', condition):
                # 追踪变量定义
                var = condition
                var_pattern = _re.compile(
                    r"^[ \t]*" + _re.escape(var) + r"\s*=\s*(.+)$",
                    _re.MULTILINE
                )
                vm = var_pattern.search(code)
                if vm:
                    var_cond = vm.group(1).strip()
                    var_cond = _re.sub(r'\.fillna\(.*?\)', '', var_cond)
                    var_cond = _re.sub(r'\.astype\(.*?\)', '', var_cond)
                    if var_cond and not _re.match(r'^[a-zA-Z_]\w*$', var_cond):
                        return var_cond.strip()
                continue
            # 跳过纯布尔赋值
            if condition.lower() in ('true', 'false'):
                continue
            return condition

        # 2. fallback：找最后一个赋值
        for m in pattern.finditer(code):
            condition = m.group(1).strip()
            condition = _re.sub(r'\.fillna\(.*?\)', '', condition)
            condition = _re.sub(r'\.astype\(.*?\)', '', condition)
            return condition.strip()
        return ""

    @staticmethod
    def _extract_buy_conditions(code: str) -> str:
        """提取买入条件逻辑。"""
        return IndicatorParser._extract_conditions_with_prefix(code, 'buy')

    @staticmethod
    def _extract_sell_conditions(code: str) -> str:
        """提取卖出条件逻辑。"""
        return IndicatorParser._extract_conditions_with_prefix(code, 'sell')

    @staticmethod
    def _extract_signal_columns(code: str) -> List[str]:
        """提取 df 中计算的信号列。"""
        cols = set()
        for m in re.finditer(r"df\[['\"](\w+)['\"]\]\s*=", code):
            col = m.group(1)
            if col not in ('buy', 'sell'):
                cols.add(col)
        return sorted(cols)

    @staticmethod
    def _extract_imports(code: str) -> List[str]:
        """提取 import 语句。"""
        imports = []
        for m in re.finditer(r"^import\s+(.+)$", code, re.MULTILINE):
            imports.append(m.group(1).strip())
        for m in re.finditer(r"^from\s+(\w+)\s+import\s+(.+)$", code, re.MULTILINE):
            imports.append(f"{m.group(1)}.{m.group(2).strip()}")
        return imports


# ============================================================
# 3. YAML 生成器
# ============================================================

class YAMLGenerator:
    """从解析结果生成 YAML 策略描述。"""

    # 指标中文名映射
    INDICATOR_NAMES: Dict[str, str] = {
        'ema': 'EMA指数均线',
        'sma': 'SMA简单均线',
        'rsi': 'RSI相对强弱',
        'macd': 'MACD',
        'kdj': 'KDJ随机指标',
        'bollinger': '布林带',
        'atr': 'ATR真实波幅',
        'vwap': 'VWAP成交量加权均价',
        'volume': '成交量',
        'supertrend': '超级趋势',
        'donchian': '唐奇安通道',
        'pivot': '枢轴点',
    }

    # 分类推断规则（指标 → 类别基础映射）
    CATEGORY_RULES: Dict[str, List[str]] = {
        'trend': ['ema', 'sma', 'macd', 'supertrend'],
        'reversal': ['rsi', 'kdj', 'bollinger'],
        'volatility': ['atr', 'bollinger', 'donchian'],
        'volume': ['volume', 'vwap'],
    }

    # ── 框架类策略特征关键词 ──
    # 这些策略的核心是分析框架/方法论，而非单一指标公式
    FRAMEWORK_KEYWORDS: List[str] = [
        # 缠论
        r'中枢', r'笔', r'线段', r'分型', r'背驰', r'缠论',
        r'一买', r'二买', r'三买', r'一卖', r'二卖', r'三卖',
        # 箱体
        r'箱体', r'箱底', r'箱顶', r'阻力位', r'支撑位',
        # 情绪周期
        r'情绪', r'换手率', r'恐慌', r'贪婪', r'狂热',
        r'情绪周期', r'逆情绪',
        # 波浪理论
        r'波浪', r'推动浪', r'调整浪', r'浪型', r'艾略特',
        # 通用框架
        r'框架', r'分析步骤', r'中枢震荡',
    ]

    # ── 形态类策略特征关键词 ──
    PATTERN_KEYWORDS: List[str] = [
        r'形态', r'K线形态', r'一阳', r'三阴', r'阳线', r'阴线',
        r'整理形态', r'反转形态', r'突破形态',
        r'吞没', r'锤子', r'十字星', r'早晨之星', r'黄昏之星',
    ]

    @staticmethod
    def generate(parsed: Dict[str, Any]) -> Dict[str, Any]:
        """从解析结果生成 YAML 字典。"""
        strategy_name = parsed.get('strategy_name', 'unknown')
        # 清理名称（去掉 IndicatorStrategy: 前缀）
        clean_name = re.sub(r'^IndicatorStrategy:\s*', '', strategy_name)
        # 转 snake_case
        slug = YAMLGenerator._to_snake_case(clean_name)

        indicators = parsed.get('indicators', [])
        params = parsed.get('params', [])
        annotations = parsed.get('strategy_annotations', {})
        buy_cond = parsed.get('buy_conditions', '')
        sell_cond = parsed.get('sell_conditions', '')

        # 推断分类（综合指标 + 语义 + 条件分析）
        category = YAMLGenerator._infer_category(indicators, buy_cond, sell_cond, clean_name)

        # 生成 display_name
        display_name = YAMLGenerator._generate_display_name(clean_name, indicators)

        # 生成 description
        description = YAMLGenerator._generate_description(
            clean_name, indicators, params, annotations, buy_cond, sell_cond
        )

        # 生成 instructions
        instructions = YAMLGenerator._generate_instructions(
            clean_name, indicators, params, annotations, buy_cond, sell_cond,
            parsed.get('signal_columns', [])
        )

        # 推断 core_rules
        core_rules = YAMLGenerator._infer_core_rules(annotations, indicators)

        # 推断 required_tools
        required_tools = YAMLGenerator._infer_required_tools(indicators)

        yaml_data = {
            'name': slug,
            'display_name': display_name,
            'description': description,
            'category': category,
            'core_rules': core_rules,
            'required_tools': required_tools,
            'instructions': instructions,
        }

        return yaml_data

    @staticmethod
    def _to_snake_case(text: str) -> str:
        """转换为 snake_case。"""
        # 替换特殊字符
        text = re.sub(r'[-\s]+', '_', text)
        # 驼峰转下划线
        text = re.sub(r'([a-z])([A-Z])', r'\1_\2', text)
        text = text.lower()
        # 去除多余下划线
        text = re.sub(r'_+', '_', text).strip('_')
        return text or 'unknown'

    @staticmethod
    def _infer_category(
        indicators: List[str],
        buy_conditions: str = '',
        sell_conditions: str = '',
        strategy_name: str = '',
    ) -> str:
        """根据指标组合 + 策略语义 + 买卖条件推断策略分类。

        三层推断逻辑（优先级从高到低）：

        1. 语义层：检测策略名称和买卖条件中的框架/形态关键词
           → framework / pattern 优先于指标推断
        2. 条件语义层：分析买卖条件的逻辑意图
           → 均值回归型 (reversal) vs 趋势跟随型 (trend)
        3. 指标组合层：传统指标 → 类别映射（兜底）
        """
        # ── 第一层：语义关键词检测 ──
        # 注意：中文不区分大小写，用原始文本匹配
        name_text = strategy_name
        cond_text = (buy_conditions or '') + ' ' + (sell_conditions or '')

        framework_in_name = sum(1 for kw in YAMLGenerator.FRAMEWORK_KEYWORDS if re.search(kw, name_text))
        framework_in_conds = sum(1 for kw in YAMLGenerator.FRAMEWORK_KEYWORDS if re.search(kw, cond_text))

        # 名称中出现框架关键词 = 强信号（>=1 即可）
        # 条件中出现多个框架关键词 = 弱信号（>=2）
        if framework_in_name >= 1 or framework_in_conds >= 2:
            return 'framework'

        # 检测形态类
        pattern_in_name = sum(1 for kw in YAMLGenerator.PATTERN_KEYWORDS if re.search(kw, name_text))
        pattern_in_conds = sum(1 for kw in YAMLGenerator.PATTERN_KEYWORDS if re.search(kw, cond_text))
        if pattern_in_name >= 1 or pattern_in_conds >= 1:
            return 'pattern'

        # ── 第二层：买卖条件语义分析 ──
        # 区分 "趋势跟随" vs "均值回归"
        # 趋势信号：均线排列、突破、金叉死叉
        trend_signal_patterns = [
            r"ema_fast.*>.*ema_slow",  # EMA 多头排列（快线 > 慢线）
            r"ma_fast.*>.*ma_slow",  # 均线多头
            r"\.ewm.*mean.*>.*\.ewm.*mean",  # 两条 EMA 比较
            r"macd_hist\s*>\s*0\b",  # MACD 柱状图正（严格匹配 > 0，非 > xxx.shift(1)）
            r"macd_hist.*>\s*macd_hist\.shift",  # MACD 柱增长
            r"diff.*>\s*dea",  # DIF > DEA
            r"close.*>\s*resistance",  # 突破阻力位
            r"close.*>\s*high.*\.shift",  # 突破前高
        ]
        # 反转信号：超买超卖、极端值回归
        reversal_signal_patterns = [
            r"rsi.*<\s*\d{1,2}\b",  # RSI < 30/20 区域
            r"rsi.*>\s*[78]\d\b",  # RSI > 70/80 区域
            r"kdj.*j.*<\s*\d{1,2}\b",  # KDJ J值极低
            r"kdj.*j.*>\s*\d{2}\b",  # KDJ J值极高
            r"close.*<.*bb_lower",  # 触及布林下轨
            r"close.*>.*bb_upper",  # 触及布林上轨
            r"close.*<.*vwap\s*\*",  # 价格低于 VWAP 偏离
            r"close.*<.*vwap\b",  # 价格低于 VWAP
        ]

        buy_lower = buy_conditions.lower() if buy_conditions else ''
        sell_lower = sell_conditions.lower() if sell_conditions else ''
        combined_conds = buy_lower + ' ' + sell_lower

        trend_score = sum(1 for p in trend_signal_patterns if re.search(p, combined_conds))
        reversal_score = sum(1 for p in reversal_signal_patterns if re.search(p, combined_conds))

        # 如果反转信号多于趋势信号 → reversal（至少1个反转信号）
        if reversal_score > trend_score and reversal_score >= 1:
            return 'reversal'
        # 如果趋势信号多于反转信号 → trend（至少1个趋势信号）
        if trend_score > reversal_score and trend_score >= 1:
            return 'trend'

        # 平局打破：VWAP+MACD/成交量组合 → reversal（均值回归型）
        if reversal_score == trend_score and reversal_score >= 1:
            has_vwap = 'vwap' in indicators
            has_macd = 'macd' in indicators
            has_volume = 'volume' in indicators
            # VWAP 作为均值锚点 + MACD/成交量辅助 = 反转策略
            if has_vwap and (has_macd or has_volume):
                return 'reversal'
            # 有明确的趋势指标主导 → trend
            if has_macd and not has_vwap:
                return 'trend'

        # ── 第三层：指标组合基础映射（兜底） ──
        BUY_CONDITION_INDICATORS = {
            'ema': [r"ema", r"\.ewm"],
            'sma': [r"\bma\b", r"\.rolling.*mean"],
            'macd': [r"macd", r"diff.*dea", r"histogram"],
            'rsi': [r"\brsi\b"],
            'kdj': [r"kdj", r"rsv", r"\bk\b.*\bd\b"],
            'bollinger': [r"bb_", r"bollinger", r"upper.*band", r"lower.*band"],
            'atr': [r"\batr\b", r"\btr\b"],
            'vwap': [r"\bvwap\b"],
        }

        scores: Dict[str, int] = {}

        # 基础分：每个指标按类别 +1
        for cat, cat_indicators in YAMLGenerator.CATEGORY_RULES.items():
            for ind in indicators:
                if ind in cat_indicators:
                    scores[cat] = scores.get(cat, 0) + 1

        # 买入条件加权：出现在 buy_conditions 中的指标额外 +2
        for ind, patterns in BUY_CONDITION_INDICATORS.items():
            for pattern in patterns:
                if re.search(pattern, buy_lower):
                    for cat, cat_indicators in YAMLGenerator.CATEGORY_RULES.items():
                        if ind in cat_indicators:
                            scores[cat] = scores.get(cat, 0) + 2
                    break

        if not scores:
            return 'unknown'
        # 平分打破：bollinger 存在时优先 reversal（布林带本质是均值回归）
        if scores.get('reversal') == scores.get('trend') and 'bollinger' in indicators:
            return 'reversal'
        return max(scores, key=scores.get)

    @staticmethod
    def _generate_display_name(name: str, indicators: List[str]) -> str:
        """生成中文显示名。"""
        if name and not name.startswith('unknown'):
            # 已有名称就用
            return name

        # 根据指标组合生成
        parts = []
        for ind in indicators[:3]:  # 最多取 3 个
            if ind in YAMLGenerator.INDICATOR_NAMES:
                parts.append(YAMLGenerator.INDICATOR_NAMES[ind])

        if parts:
            return '+'.join(parts) + '策略'
        return '自定义策略'

    @staticmethod
    def _generate_description(
        name: str, indicators: List[str], params: List[Dict],
        annotations: Dict, buy_cond: str, sell_cond: str
    ) -> str:
        """生成策略描述。"""
        parts = []

        # 指标组合描述
        ind_names = [YAMLGenerator.INDICATOR_NAMES.get(ind, ind) for ind in indicators[:3]]
        if ind_names:
            parts.append(f"基于 {'+'.join(ind_names)} 的量化策略")

        # 参数概要
        if params:
            param_desc = ', '.join(
                f"{p['name']}={p['default']}" for p in params[:4]
            )
            parts.append(f"参数: {param_desc}")

        # 风控
        if 'stopLossPct' in annotations:
            parts.append(f"止损 {annotations['stopLossPct']*100:.1f}%")
        if 'takeProfitPct' in annotations:
            parts.append(f"止盈 {annotations['takeProfitPct']*100:.1f}%")

        return '。'.join(parts) + '。' if parts else '自定义量化策略。'

    @staticmethod
    def _generate_instructions(
        name: str, indicators: List[str], params: List[Dict],
        annotations: Dict, buy_cond: str, sell_cond: str,
        signal_columns: List[str]
    ) -> str:
        """生成详细的 instructions 文本。"""
        lines = []

        # 标题
        clean_name = re.sub(r'^IndicatorStrategy:\s*', '', name)
        lines.append(f"**{clean_name}**")
        lines.append("")

        # 核心逻辑
        ind_names = [YAMLGenerator.INDICATOR_NAMES.get(ind, ind) for ind in indicators]
        if ind_names:
            lines.append(f"核心逻辑：基于 {' + '.join(ind_names)} 指标组合生成交易信号。")
            lines.append("")

        # 信号判定
        lines.append("## 信号判定")
        lines.append("")

        # 买入条件
        lines.append("### 买入条件")
        if buy_cond:
            # 将 Python 条件转换为可读描述
            readable = YAMLGenerator._condition_to_readable(buy_cond, indicators)
            lines.append(f"- {readable}")
        else:
            lines.append("- （见代码逻辑）")
        lines.append("")

        # 卖出条件
        lines.append("### 卖出条件")
        if sell_cond:
            readable = YAMLGenerator._condition_to_readable(sell_cond, indicators)
            lines.append(f"- {readable}")
        else:
            lines.append("- （见代码逻辑）")
        lines.append("")

        # 止损止盈
        if 'stopLossPct' in annotations or 'takeProfitPct' in annotations:
            lines.append("### 风控参数")
            if 'stopLossPct' in annotations:
                pct = annotations['stopLossPct']
                if isinstance(pct, (int, float)):
                    lines.append(f"- 止损: {pct*100:.1f}%")
            if 'takeProfitPct' in annotations:
                pct = annotations['takeProfitPct']
                if isinstance(pct, (int, float)):
                    lines.append(f"- 止盈: {pct*100:.1f}%")
            lines.append("")

        # 参数说明
        if params:
            lines.append("## 可调参数")
            lines.append("")
            for p in params:
                lines.append(f"- **{p['name']}** ({p['type']}, 默认 {p['default']}): {p.get('description', '')}")
            lines.append("")

        # 计算指标列
        if signal_columns:
            lines.append("## 计算指标")
            lines.append("")
            for col in signal_columns:
                lines.append(f"- `{col}`")
            lines.append("")

        return '\n'.join(lines)

    @staticmethod
    def _condition_to_readable(condition: str, indicators: List[str]) -> str:
        """将 Python 条件表达式转换为可读文本。"""
        text = condition.strip()

        # 替换常见模式为可读文本
        replacements = [
            (r"df\[['\"](\w+)['\"]\]", r"\1"),
            (r"params\.get\(['\"](\w+)['\"],\s*(\w+)\)", r"\1(默认\2)"),
            (r"\.shift\((\d+)\)", r"前\1周期"),
            (r"\.ewm\([^)]+\)\.mean\(\)", "的EMA"),
            (r"\.rolling\((\w+)\)\.mean\(\)", r"的\1周期均值"),
            (r"\.rolling\((\w+)\)\.min\(\)", r"的\1周期最低"),
            (r"\.rolling\((\w+)\)\.max\(\)", r"的\1周期最高"),
            (r"np\.maximum\(", "max("),
            (r"abs\(", "绝对值("),
            (r"100\s*-\s*\(?\s*100\s*/\s*\(1\s*\+\s*(\w+)\)\s*\)?", r"100-100/(1+\1)"),
        ]

        for pattern, replacement in replacements:
            text = re.sub(pattern, replacement, text)

        # 将 & → 且, | → 或
        text = text.replace('&', ' 且 ')
        text = text.replace('|', ' 或 ')

        # 简化多余空格
        text = re.sub(r'\s+', ' ', text).strip()

        return text if text else condition

    @staticmethod
    def _infer_core_rules(annotations: Dict, indicators: List[str]) -> List[int]:
        """推断 core_rules 编号。"""
        rules = []
        # 简单推断：有止损→规则1，有趋势指标→规则2，有反转指标→规则3
        if 'stopLossPct' in annotations:
            rules.append(1)
        if any(ind in indicators for ind in ['ema', 'sma', 'macd', 'supertrend']):
            rules.append(2)
        if any(ind in indicators for ind in ['rsi', 'kdj', 'bollinger']):
            rules.append(3)
        if any(ind in indicators for ind in ['volume', 'vwap']):
            rules.append(5)
        # 框架类策略通常涉及多维度分析
        # （如果 indicators 为空或很少，可能是框架/形态类）
        if len(indicators) <= 1:
            rules.append(4)  # 形态/结构分析
        return sorted(set(rules)) if rules else [1]

    @staticmethod
    def _infer_required_tools(indicators: List[str]) -> List[str]:
        """推断所需工具。"""
        tools = ['get_daily_history']
        if 'vwap' in indicators or 'volume' in indicators:
            tools.append('get_realtime_quote')
        if any(ind in indicators for ind in ['ema', 'sma', 'macd']):
            tools.append('analyze_trend')
        return sorted(set(tools))


# ============================================================
# 4. 转换管道
# ============================================================

class ReverseConversionPipeline:
    """IndicatorStrategy → YAML 反向转换管道。"""

    def __init__(self):
        self.stats = {'total': 0, 'success': 0, 'failed': 0}

    def convert_file(self, py_path: str, output_dir: Optional[str] = None) -> Optional[str]:
        """转换单个 .py 文件为 YAML。"""
        self.stats['total'] += 1

        try:
            parsed = IndicatorParser.parse(py_path)
        except Exception as e:
            print(f"❌ 解析失败 [{py_path}]: {e}", file=sys.stderr)
            self.stats['failed'] += 1
            return None

        # 生成 YAML
        yaml_data = YAMLGenerator.generate(parsed)

        # 输出路径
        py_name = Path(py_path).stem
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            output_path = os.path.join(output_dir, f"{yaml_data['name']}.yaml")
        else:
            output_path = str(Path(py_path).with_suffix('.yaml'))

        # 写入
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(f"# {yaml_data['display_name']}\n")
            yaml.dump(yaml_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        print(f"✅ {py_name} → {output_path}")
        self.stats['success'] += 1
        return output_path

    def convert_directory(self, dir_path: str, output_dir: Optional[str] = None) -> List[str]:
        """批量转换目录下所有 .py 文件。"""
        path = Path(dir_path)
        if not path.is_dir():
            print(f"❌ 目录不存在: {dir_path}", file=sys.stderr)
            return []

        py_files = sorted(path.glob('*.py'))
        # 排除测试文件和自身
        py_files = [f for f in py_files if not f.name.startswith('test_') and f.name != 'indicator_to_yaml.py']

        print(f"📂 找到 {len(py_files)} 个 .py 文件")
        print("=" * 60)

        results = []
        for py_file in py_files:
            result = self.convert_file(str(py_file), output_dir)
            if result:
                results.append(result)

        print("=" * 60)
        print(f"📊 转换统计: 总计 {self.stats['total']}, 成功 {self.stats['success']}, 失败 {self.stats['failed']}")

        return results


# ============================================================
# 5. CLI 入口
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='QuantDinger IndicatorStrategy → YAML 反向转换器',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('file', nargs='?', help='单个 .py 文件路径')
    parser.add_argument('--dir', '-d', help='批量转换目录')
    parser.add_argument('--output', '-o', help='输出目录')
    parser.add_argument('--json', action='store_true', help='输出 JSON 格式（不写文件）')

    args = parser.parse_args()

    if not args.file and not args.dir:
        parser.print_help()
        sys.exit(1)

    pipeline = ReverseConversionPipeline()

    if args.json and args.file:
        parsed = IndicatorParser.parse(args.file)
        yaml_data = YAMLGenerator.generate(parsed)
        print(json.dumps(yaml_data, ensure_ascii=False, indent=2))
        sys.exit(0)

    if args.dir:
        results = pipeline.convert_directory(args.dir, args.output)
        sys.exit(0 if results else 1)
    elif args.file:
        result = pipeline.convert_file(args.file, args.output)
        sys.exit(0 if result else 1)


if __name__ == '__main__':
    main()
