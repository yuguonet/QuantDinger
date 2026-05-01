#!/usr/bin/env python3
"""
QuantDinger YAML → IndicatorStrategy 自动转换器
================================================

将 backend_api_python/strategies/ 目录下的 YAML 策略描述文件转换为
QuantDinger 标准的 IndicatorStrategy 可执行 Python 代码。

转换流程：
    YAML 文件 → YAMLStrategyParser 解析
              → LLMStrategyGenerator 调用 LLM API 生成代码
              → CodePostProcessor 后处理（import/NaN/布尔类型）
              → CodeValidator 安全校验（正则+AST 双重检查）
              → 输出 .py + _meta.json

内部模块：
    QuantDingerStandards   — 从源码 safe_exec.py/indicator_params.py 提取的规范常量
    YAMLStrategyParser     — YAML 策略文件解析器
    LLMStrategyGenerator   — OpenAI 兼容 API 调用器
    CodePostProcessor      — 代码后处理器（import 注入、fillna、astype）
    CodeValidator          — 安全校验器（正则 + AST，与 safe_exec.py 对齐）
    ConversionPipeline     — 转换管道（串联以上模块）

依赖：
    Python >= 3.8
    pyyaml（pip install pyyaml）
    无其他第三方依赖（LLM API 调用使用标准库 urllib）

用法：
    # ── 转换单个 YAML 策略文件 ──
    python3 yaml_to_indicator.py backend_api_python/strategies/ema_rsi_pullback.yaml \\
        --api-key sk-xxx --model gpt-4o

    # ── 批量转换全部策略（指定输出目录）──
    python3 yaml_to_indicator.py --dir backend_api_python/strategies/ \\
        --output output_strategies/ \\
        --api-base https://api.openai.com/v1 \\
        --api-key sk-xxx \\
        --model gpt-4o

    # ── 使用本地 Ollama 模型 ──
    python3 yaml_to_indicator.py --dir backend_api_python/strategies/ \\
        --output output_strategies/ \\
        --api-base http://localhost:11434/v1 \\
        --api-key ollama \\
        --model qwen2.5-coder:32b

    # ── Dry run（只解析 YAML，不调用 LLM，验证输入文件）──
    python3 yaml_to_indicator.py --dir backend_api_python/strategies/ --dry-run

    # ── 校验已有的 IndicatorStrategy .py 文件是否符合 QuantDinger 标准 ──
    python3 yaml_to_indicator.py --validate output_strategies/ema_rsi_pullback.py
    python3 yaml_to_indicator.py --validate-dir output_strategies/

    # ── 运行测试套件 ──
    python3 test_yaml_to_indicator.py

环境变量（可替代命令行参数）：
    LLM_API_BASE   — API 地址（默认 https://api.openai.com/v1）
    LLM_API_KEY    — API Key（必须提供）
    LLM_MODEL      — 模型名称（默认 gpt-4o）

输出文件：
    <name>.py          — IndicatorStrategy 可执行代码
    <name>_meta.json   — 转换元信息（来源、校验结果、生成时间）

QuantDinger IndicatorStrategy 规范摘要：
    1. 代码是顶层脚本，直接执行（不要定义 on_bar/on_init 函数）
    2. 必须设置 df['buy'] 和 df['sell']（布尔 Series）
    3. 可用变量：df, np, pd, params, trading_config, cfg, leverage,
       initial_capital, commission, trade_direction, signals
    4. 参数声明：# @param name type default description
    5. 风控注解：# @strategy key value
       合法 key：stopLossPct, takeProfitPct, entryPct,
       trailingEnabled, trailingStopPct, trailingActivationPct, tradeDirection
    6. 仅允许 import：numpy, pandas, math, json, datetime, time,
       collections, functools, itertools, statistics, decimal, fractions,
       operator, copy
    7. 禁止：os, sys, subprocess, eval, exec, open, getattr, setattr,
       __builtins__, dunder 属性访问
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml


# ============================================================
# 1. QuantDinger 标准规范（从源码提取）
# ============================================================

class QuantDingerStandards:
    """
    QuantDinger IndicatorStrategy 的硬性规范。
    所有数值/集合均从源码 safe_exec.py / indicator_params.py / trading_executor.py 提取。
    """

    # 允许导入的模块（safe_exec.py: SAFE_IMPORT_MODULES）
    SAFE_IMPORT_MODULES: Set[str] = {
        'numpy', 'pandas', 'math', 'json', 'datetime', 'time',
        'collections', 'functools', 'itertools', 'statistics',
        'decimal', 'fractions', 'operator', 'copy',
    }

    # 危险模式正则（safe_exec.py: validate_code_safety）
    DANGEROUS_PATTERNS: List[str] = [
        r'\bos\.system\b', r'\bos\.popen\b', r'\bos\.spawn\b',
        r'\bos\.exec\b', r'\bos\.fork\b', r'\bos\.environ\b',
        r'\bos\.getenv\b', r'\bos\.putenv\b',
        r'\bos\.remove\b', r'\bos\.unlink\b', r'\bos\.rmdir\b',
        r'\bos\.makedirs\b', r'\bos\.mkdir\b',
        r'\bos\.listdir\b', r'\bos\.walk\b', r'\bos\.scandir\b',
        r'\bos\.path\b',
        r'\bsubprocess\b', r'\bcommands\b',
        r'\b__import__\s*\(', r'\beval\s*\(', r'\bexec\s*\(',
        r'\bcompile\s*\(', r'\bopen\s*\(', r'\bfile\s*\(',
        r'\b__builtins__\b',
        r'\bimport\s+os\b', r'\bimport\s+sys\b',
        r'\bimport\s+subprocess\b', r'\bimport\s+shutil\b',
        r'\bimport\s+pymysql\b', r'\bimport\s+sqlite3\b',
        r'\bimport\s+psycopg\b', r'\bimport\s+sqlalchemy\b',
        r'\bimport\s+requests\b', r'\bimport\s+urllib\b',
        r'\bimport\s+http\b', r'\bimport\s+socket\b',
        r'\bimport\s+ftplib\b', r'\bimport\s+telnetlib\b',
        r'\bimport\s+smtplib\b', r'\bimport\s+ssl\b',
        r'\bimport\s+pickle\b', r'\bimport\s+cpickle\b',
        r'\bimport\s+marshal\b', r'\bimport\s+shelve\b',
        r'\bimport\s+ctypes\b', r'\bimport\s+cffi\b',
        r'\bimport\s+multiprocessing\b', r'\bimport\s+threading\b',
        r'\bimport\s+concurrent\b', r'\bimport\s+asyncio\b',
        r'\bimport\s+signal\b', r'\bimport\s+resource\b',
        r'\bimport\s+importlib\b', r'\bimport\s+imp\b',
        r'\bimport\s+builtins\b', r'\bimport\s+code\b',
        r'\bimport\s+codeop\b', r'\bimport\s+runpy\b',
        r'\bimport\s+tempfile\b', r'\bimport\s+glob\b',
        r'\bimport\s+pathlib\b', r'\bimport\s+io\b',
        r'\bgetattr\s*\(', r'\bsetattr\s*\(', r'\bdelattr\s*\(',
        r'\b__getattribute__\b', r'\b__setattr__\b', r'\b__delattr__\b',
        r'\b__dict__\b', r'\b__class__\b', r'\b__bases__\b',
        r'\b__subclasses__\b', r'\b__mro__\b', r'\b__module__\b',
        r'\b__globals__\b', r'\b__code__\b', r'\b__func__\b',
        r'\bglobals\s*\(', r'\bvars\s*\(', r'\bdir\s*\(',
        r'\bbreakpoint\s*\(',
        r'\b__builtins__\s*[\[.]', r'\b__import__\s*\(',
        r'\bimportlib\b',
    ]

    # 危险模块 AST 检查
    DANGEROUS_MODULES: Set[str] = {
        'os', 'sys', 'subprocess', 'shutil', 'signal', 'resource',
        'pymysql', 'sqlite3', 'psycopg2', 'sqlalchemy',
        'requests', 'urllib', 'http', 'socket', 'ftplib', 'telnetlib',
        'smtplib', 'ssl', 'pickle', 'cpickle', 'marshal', 'shelve',
        'ctypes', 'cffi', 'multiprocessing', 'threading', 'concurrent',
        'asyncio', 'importlib', 'imp', 'builtins', 'code', 'codeop',
        'runpy', 'tempfile', 'glob', 'pathlib', 'io',
    }

    # 危险函数调用
    DANGEROUS_CALLS: Set[str] = {
        'eval', 'exec', 'compile', '__import__',
        'getattr', 'setattr', 'delattr',
        'globals', 'vars', 'dir', 'breakpoint',
        'open', 'input', 'exit', 'quit',
    }

    # 危险 dunder 属性
    DANGEROUS_DUNDER_ATTRS: Set[str] = {
        '__builtins__', '__import__', '__class__', '__bases__',
        '__subclasses__', '__mro__', '__globals__', '__code__',
        '__func__', '__dict__', '__module__',
    }

    # @strategy 注解合法 key（indicator_params.py: StrategyConfigParser.VALID_KEYS）
    STRATEGY_ANNOTATION_KEYS: Set[str] = {
        'stopLossPct', 'takeProfitPct', 'entryPct',
        'trailingEnabled', 'trailingStopPct', 'trailingActivationPct',
        'tradeDirection',
    }

    # @param 支持的类型（indicator_params.py: IndicatorParamsParser）
    PARAM_TYPES: Set[str] = {'int', 'float', 'bool', 'str', 'string'}

    # 代码中可用的预注入变量（trading_executor.py: _execute_indicator_df）
    AVAILABLE_VARS: Set[str] = {
        'df', 'open', 'high', 'low', 'close', 'volume', 'signals',
        'np', 'pd', 'trading_config', 'config', 'cfg', 'params',
        'call_indicator', 'leverage', 'initial_capital', 'commission',
        'trade_direction',
        'initial_highest_price', 'initial_position',
        'initial_avg_entry_price', 'initial_position_count',
        'initial_last_add_price',
    }

    # 支持的指标类型（strategy_compiler.py + strategy_templates_*.py）
    SUPPORTED_INDICATORS: Set[str] = {
        'ma', 'ema', 'rsi', 'macd', 'kdj', 'bollinger', 'atr',
        'supertrend', 'volume', 'donchian_channel', 'vwap',
        'atr_channel', 'bollinger_bandwidth', 'price_volume_divergence',
    }

    # 支持的 operator
    SUPPORTED_OPERATORS: Set[str] = {
        'cross_up', 'cross_down', 'price_above', 'price_below',
        'above', 'below', 'gold_cross', 'death_cross',
        'price_above_upper', 'price_below_lower',
        'volume_above_ma', 'volume_ratio_above',
        'histogram_positive', 'histogram_negative',
        'bullish_divergence', 'below_percentile', 'price_break_upper',
        'k_gt_d', 'k_lt_d', 'diff_gt_dea', 'diff_lt_dea',
        'price_below_vwap_by', 'is_uptrend', 'trend_bullish',
    }


# ============================================================
# 2. YAML 解析器
# ============================================================

class YAMLStrategyParser:
    """解析 YAML 策略文件，提取结构化信息。"""

    @staticmethod
    def parse(yaml_path: str) -> Dict[str, Any]:
        """解析单个 YAML 文件，返回策略描述字典。"""
        path = Path(yaml_path)
        if not path.exists():
            raise FileNotFoundError(f"YAML 文件不存在: {yaml_path}")

        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError(f"YAML 文件格式错误，期望字典: {yaml_path}")

        # 提取关键字段
        strategy = {
            'name': data.get('name', path.stem),
            'display_name': data.get('display_name', ''),
            'description': data.get('description', ''),
            'category': data.get('category', 'unknown'),
            'core_rules': data.get('core_rules', []),
            'required_tools': data.get('required_tools', []),
            'instructions': data.get('instructions', ''),
            'source_file': str(path),
        }

        if not strategy['instructions']:
            raise ValueError(f"YAML 文件缺少 instructions 字段: {yaml_path}")

        return strategy

    @staticmethod
    def parse_directory(dir_path: str) -> List[Dict[str, Any]]:
        """解析目录下所有 YAML 文件。"""
        path = Path(dir_path)
        if not path.is_dir():
            raise NotADirectoryError(f"目录不存在: {dir_path}")

        strategies = []
        for yaml_file in sorted(path.glob('*.yaml')):
            try:
                strategy = YAMLStrategyParser.parse(str(yaml_file))
                strategies.append(strategy)
            except Exception as e:
                print(f"⚠️  跳过 {yaml_file.name}: {e}", file=sys.stderr)

        return strategies


# ============================================================
# 3. LLM 策略代码生成器
# ============================================================

class LLMStrategyGenerator:
    """调用 LLM API 将自然语言策略描述转换为 IndicatorStrategy 代码。"""

    SYSTEM_PROMPT = textwrap.dedent("""\
    你是 QuantDinger 量化平台的 IndicatorStrategy 代码生成专家。

    ## 你的任务
    将用户提供的自然语言策略描述转换为符合 QuantDinger 标准的 Python 可执行代码。

    ## QuantDinger IndicatorStrategy 严格规范

    ### 1. 输出约定
    - 必须在 df 上计算指标列
    - 必须设置 `df['buy']`（布尔 Series，True=买入信号）和 `df['sell']`（布尔 Series，True=卖出信号）
    - 不要定义 `on_bar`、`on_init` 等函数，也不要写 `if __name__` 块
    - 代码是顶层脚本，直接执行

    ### 2. 可用变量（运行时自动注入，无需 import）
    - `df` : DataFrame，含 open/high/low/close/volume 列（float64）
    - `np` : numpy（已 import）
    - `pd` : pandas（已 import）
    - `params` : dict，用户可调参数（由 @param 声明定义默认值）
    - `trading_config` : dict，交易配置
    - `cfg` : dict，嵌套结构 cfg.risk/cfg.scale/cfg.position
    - `leverage` : float，杠杆倍数
    - `initial_capital` : float，初始资金
    - `commission` : float，手续费率（默认 0.001）
    - `trade_direction` : str，'long'/'short'/'both'
    - `signals` : Series，初始全 0，可用于辅助

    ### 3. @param 参数声明格式（写在代码顶部注释）
    ```
    # @param 参数名 类型 默认值 描述
    # @param ma_fast int 5 短期均线周期
    # @param threshold float 0.5 阈值
    ```
    支持类型：int, float, bool, str

    ### 4. @strategy 风控注解格式（写在代码顶部注释）
    ```
    # @strategy stopLossPct 0.025
    # @strategy takeProfitPct 0.06
    # @strategy tradeDirection long
    ```
    合法 key：stopLossPct, takeProfitPct, entryPct, trailingEnabled, trailingStopPct, trailingActivationPct, tradeDirection

    ### 5. 安全限制
    - 仅允许 import：numpy, pandas, math, json, datetime, time, collections, functools, itertools, statistics, decimal, fractions, operator, copy
    - 禁止：os, sys, subprocess, requests, socket, eval, exec, open, getattr, setattr, __builtins__
    - 禁止访问 dunder 属性（__dict__, __class__ 等）

    ### 6. 支持的指标
    ma(sma/ema), ema, rsi, macd, kdj, bollinger, atr, supertrend, volume, donchian_channel, vwap

    ### 7. 其他
    - 使用 `params.get('key', default)` 读取参数
    - 使用 `call_indicator(id_or_name, df)` 调用其他指标（如果需要）
    - 代码应该高效，适合逐 K 线推进的实盘环境
    - NaN 处理：指标计算前期会产生 NaN，用 fillna(False) 处理布尔信号

    ### 8. 输出格式
    只输出纯 Python 代码，不要包含 ```python 标记，不要包含任何解释文字。
    """)

    def __init__(self, api_base: str, api_key: str, model: str):
        self.api_base = api_base.rstrip('/')
        self.api_key = api_key
        self.model = model

    def generate(self, strategy: Dict[str, Any]) -> str:
        """
        调用 LLM 生成 IndicatorStrategy 代码。

        Args:
            strategy: YAML 解析后的策略字典

        Returns:
            生成的 Python 代码字符串
        """
        import urllib.request
        import urllib.error

        user_prompt = self._build_user_prompt(strategy)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 4096,
        }

        url = f"{self.api_base}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=headers, method='POST')

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            raise RuntimeError(f"LLM API 调用失败 (HTTP {e.code}): {body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"LLM API 连接失败: {e}")

        # 提取生成的代码
        choices = result.get('choices', [])
        if not choices:
            raise RuntimeError(f"LLM API 返回空结果: {json.dumps(result, ensure_ascii=False)}")

        content = choices[0].get('message', {}).get('content', '')
        if not content:
            raise RuntimeError("LLM 返回空内容")

        # 清理可能的 markdown 代码块标记
        code = self._clean_code_output(content)
        return code

    def _build_user_prompt(self, strategy: Dict[str, Any]) -> str:
        """构建发送给 LLM 的用户 prompt。"""
        parts = [
            f"## 策略名称: {strategy['display_name']} ({strategy['name']})",
            f"## 策略分类: {strategy['category']}",
            f"## 策略描述: {strategy['description']}",
            "",
            "## 策略详细指令（从 YAML instructions 字段提取）:",
            strategy['instructions'].strip(),
            "",
            "## 要求",
            "1. 将上述策略逻辑转换为 QuantDinger IndicatorStrategy 标准代码",
            "2. 提取策略中的技术指标参数，用 # @param 声明为可调参数",
            "3. 根据策略描述的止损/止盈规则，添加 # @strategy 风控注解",
            "4. 确保 df['buy'] 和 df['sell'] 都是布尔 Series",
            "5. 使用 fillna(False) 处理 NaN",
            "6. 只输出纯 Python 代码",
        ]
        return '\n'.join(parts)

    @staticmethod
    def _clean_code_output(content: str) -> str:
        """清理 LLM 输出中的 markdown 标记和多余文字。"""
        # 去除 ```python ... ``` 包裹
        pattern = r'```(?:python)?\s*\n?(.*?)```'
        match = re.search(pattern, content, re.DOTALL)
        if match:
            code = match.group(1).strip()
            # 代码块匹配成功但内容为空 → 直接返回空，不 fallback
            if not code:
                return ''
        else:
            # 没有代码块标记，尝试提取看起来像代码的部分
            lines = content.split('\n')
            code_lines = []
            in_code = False
            for line in lines:
                stripped = line.strip()
                if not in_code:
                    if (stripped.startswith('#') or stripped.startswith('import ')
                            or stripped.startswith('df[') or stripped.startswith('def ')
                            or stripped.startswith('np.') or stripped.startswith('pd.')):
                        in_code = True
                        code_lines.append(line)
                else:
                    code_lines.append(line)
            code = '\n'.join(code_lines).strip()

        if not code:
            code = content.strip()

        # 去除代码尾部的非代码解释文字
        # 策略代码的最后一行通常是 df['buy']/df['sell'] 赋值或 fillna/astype
        # 如果尾部出现了纯文字行（非注释、非代码），截断它
        code_lines = code.split('\n')
        last_code_idx = -1
        for i, line in enumerate(code_lines):
            stripped = line.strip()
            if not stripped:
                continue
            # 判断是否是有效代码行
            if (stripped.startswith('#') or stripped.startswith('df[')
                    or stripped.startswith('import ') or stripped.startswith('np.')
                    or stripped.startswith('pd.') or stripped.startswith('def ')
                    or stripped.startswith('if ') or stripped.startswith('for ')
                    or stripped.startswith('while ') or stripped.startswith('else')
                    or stripped.startswith('elif ') or stripped.startswith('return')
                    or stripped.startswith('signals') or stripped.startswith('commission')
                    or stripped.startswith('leverage') or stripped.startswith('trade_direction')
                    or stripped.startswith('params') or stripped.startswith('trading_config')
                    or stripped.startswith('cfg') or stripped.startswith('initial_capital')
                    or stripped.startswith('(') or stripped.startswith(')')
                    or re.match(r'^[a-zA-Z_]\w*\s*=', stripped)  # variable assignment
                    ):
                last_code_idx = i

        if last_code_idx >= 0 and last_code_idx < len(code_lines) - 1:
            # 检查截断点之后是否有看起来像代码的行
            trailing = '\n'.join(code_lines[last_code_idx + 1:]).strip()
            # 如果尾部看起来不像代码（没有 =, [, (, def 等），截断
            if trailing and not re.search(r'[=\[\(]|^\s*(def |import |from )', trailing, re.MULTILINE):
                code = '\n'.join(code_lines[:last_code_idx + 1])

        return code


# ============================================================
# 4. 代码安全校验器
# ============================================================

class CodeValidator:
    """
    校验生成的 IndicatorStrategy 代码是否符合 QuantDinger 安全规范。
    与 safe_exec.py 的 validate_code_safety() 逻辑对齐。
    """

    @staticmethod
    def validate(code: str) -> Tuple[bool, List[str]]:
        """
        校验代码安全性。

        Returns:
            (is_valid, errors) - 是否通过校验，错误列表
        """
        errors = []

        # 1. 正则模式检查
        for pattern in QuantDingerStandards.DANGEROUS_PATTERNS:
            if re.search(pattern, code):
                errors.append(f"危险模式: {pattern}")

        # 2. AST 深度检查
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            errors.append(f"语法错误: {e}")
            return False, errors

        for node in ast.walk(tree):
            # 检查 import
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split('.')[0]
                    if root not in QuantDingerStandards.SAFE_IMPORT_MODULES:
                        errors.append(f"禁止导入: {alias.name}")

            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root = node.module.split('.')[0]
                    if root not in QuantDingerStandards.SAFE_IMPORT_MODULES:
                        errors.append(f"禁止导入: {node.module}")

            # 检查危险函数调用
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in QuantDingerStandards.DANGEROUS_CALLS:
                    errors.append(f"危险函数调用: {node.func.id}()")
                if isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name):
                        if node.func.value.id in QuantDingerStandards.DANGEROUS_MODULES:
                            errors.append(f"危险模块调用: {node.func.value.id}.{node.func.attr}")

            # 检查 dunder 属性访问
            elif isinstance(node, ast.Attribute):
                if node.attr in QuantDingerStandards.DANGEROUS_DUNDER_ATTRS:
                    errors.append(f"危险属性访问: .{node.attr}")

        # 3. QuantDinger 特有检查
        qd_errors = CodeValidator._check_indicator_conventions(code)
        errors.extend(qd_errors)

        return len(errors) == 0, errors

    @staticmethod
    def _check_indicator_conventions(code: str) -> List[str]:
        """检查 IndicatorStrategy 特有约定。"""
        errors = []

        # 必须设置 df['buy']
        if "df['buy']" not in code and 'df["buy"]' not in code:
            errors.append("缺少 df['buy'] 信号定义")

        # 必须设置 df['sell']
        if "df['sell']" not in code and 'df["sell"]' not in code:
            errors.append("缺少 df['sell'] 信号定义")

        # 不应定义 on_bar/on_init 函数（那是 script_runtime 模式）
        if re.search(r'\bdef\s+on_bar\b', code):
            errors.append("不应定义 on_bar 函数（IndicatorStrategy 是顶层脚本）")

        # 不应有 if __name__ 块
        if re.search(r"if\s+__name__\s*==", code):
            errors.append("不应包含 if __name__ == '__main__' 块")

        # @strategy 注解 key 检查
        for m in re.finditer(r'#\s*@strategy\s+(\w+)', code):
            key = m.group(1)
            if key not in QuantDingerStandards.STRATEGY_ANNOTATION_KEYS:
                errors.append(f"未知 @strategy key: {key}（合法: {', '.join(sorted(QuantDingerStandards.STRATEGY_ANNOTATION_KEYS))}）")

        # @param 类型检查
        for m in re.finditer(r'#\s*@param\s+\w+\s+(\w+)', code):
            ptype = m.group(1).lower()
            if ptype not in QuantDingerStandards.PARAM_TYPES:
                errors.append(f"未知 @param 类型: {ptype}（合法: {', '.join(sorted(QuantDingerStandards.PARAM_TYPES))}）")

        return errors


# ============================================================
# 5. 代码后处理器
# ============================================================

class CodePostProcessor:
    """对 LLM 生成的代码进行后处理，修复常见问题。"""

    @staticmethod
    def process(code: str, strategy_name: str) -> str:
        """
        后处理代码：
        1. 确保顶部有策略名称注释
        2. 确保 import pandas/numpy
        3. 修复布尔信号 NaN 问题
        4. 添加策略来源注释
        """
        lines = code.split('\n')

        # 1. 确保顶部有策略注释
        header_comment = f"# IndicatorStrategy: {strategy_name}"
        if not lines or not lines[0].strip().startswith('# IndicatorStrategy'):
            lines.insert(0, header_comment)

        # 2. 确保有 import（运行时已注入 np/pd，但显式 import 更清晰）
        has_import_pd = any('import pandas' in l for l in lines)
        has_import_np = any('import numpy' in l for l in lines)
        import_lines = []
        if not has_import_pd:
            import_lines.append("import pandas as pd")
        if not has_import_np:
            import_lines.append("import numpy as np")
        if import_lines:
            # 找到插入位置（在头部注释和 @param/@strategy 注释之后）
            insert_idx = 0
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith('#') or stripped == '':
                    insert_idx = i + 1
                else:
                    break
            for j, imp in enumerate(import_lines):
                lines.insert(insert_idx + j, imp)

        code = '\n'.join(lines)

        # 3. 确保信号列没有 NaN
        # 在代码末尾统一添加 fillna + astype（独立语句，不影响条件表达式）
        # 比在赋值行追加 .fillna(False) 更安全，避免运算符优先级问题
        if "df['buy'].fillna" not in code:
            code += "\n# NaN 安全处理\ndf['buy'] = df['buy'].fillna(False).astype(bool)\n"
        if "df['sell'].fillna" not in code:
            code += "df['sell'] = df['sell'].fillna(False).astype(bool)\n"

        return code


# ============================================================
# 6. 转换管道
# ============================================================

class ConversionPipeline:
    """完整的 YAML → IndicatorStrategy 转换管道。"""

    def __init__(self, generator: Optional[LLMStrategyGenerator] = None, dry_run: bool = False):
        self.generator = generator
        self.dry_run = dry_run
        self.stats = {
            'total': 0, 'success': 0, 'failed': 0, 'skipped': 0,
            'validation_errors': 0,
        }

    def convert_file(self, yaml_path: str, output_dir: Optional[str] = None) -> Optional[str]:
        """转换单个 YAML 文件。"""
        self.stats['total'] += 1

        # 1. 解析 YAML
        try:
            strategy = YAMLStrategyParser.parse(yaml_path)
        except Exception as e:
            print(f"❌ 解析失败 [{yaml_path}]: {e}", file=sys.stderr)
            self.stats['failed'] += 1
            return None

        print(f"📋 解析成功: {strategy['display_name']} ({strategy['name']})")
        print(f"   分类: {strategy['category']} | 规则: {strategy['core_rules']}")
        print(f"   描述: {strategy['description'][:80]}...")

        if self.dry_run:
            print(f"   [Dry Run] 跳过 LLM 调用")
            self.stats['skipped'] += 1
            return None

        if not self.generator:
            print("❌ 未配置 LLM 生成器", file=sys.stderr)
            self.stats['failed'] += 1
            return None

        # 2. LLM 生成代码
        print(f"🤖 调用 LLM 生成代码...")
        try:
            code = self.generator.generate(strategy)
        except Exception as e:
            print(f"❌ LLM 生成失败: {e}", file=sys.stderr)
            self.stats['failed'] += 1
            return None

        print(f"   生成 {len(code)} 字符, {len(code.splitlines())} 行")

        # 3. 代码后处理
        code = CodePostProcessor.process(code, strategy['name'])

        # 4. 安全校验
        print(f"🔍 安全校验...")
        is_valid, errors = CodeValidator.validate(code)

        if not is_valid:
            self.stats['validation_errors'] += 1
            print(f"⚠️  校验发现 {len(errors)} 个问题:")
            for err in errors:
                print(f"   - {err}")

            # 尝试自动修复部分问题
            code, fixed = self._auto_fix(code, errors)
            if fixed:
                print(f"🔧 自动修复了 {fixed} 个问题")
                # 重新校验
                is_valid, errors = CodeValidator.validate(code)
                if not is_valid:
                    remaining = len(errors)
                    print(f"   仍有 {remaining} 个问题需要人工检查")

        # 5. 输出
        output_path = self._get_output_path(yaml_path, output_dir)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(code)

        # 附加元信息文件
        meta_path = output_path.replace('.py', '_meta.json')
        meta = {
            'source_yaml': yaml_path,
            'strategy_name': strategy['name'],
            'display_name': strategy['display_name'],
            'category': strategy['category'],
            'description': strategy['description'],
            'core_rules': strategy['core_rules'],
            'validation_passed': is_valid,
            'validation_errors': errors,
            'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'code_lines': len(code.splitlines()),
        }
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        status = "✅" if is_valid else "⚠️ "
        print(f"{status} 输出: {output_path}")
        print(f"   元信息: {meta_path}")
        print()

        self.stats['success'] += 1
        return output_path

    def convert_directory(self, dir_path: str, output_dir: Optional[str] = None) -> List[str]:
        """批量转换目录下所有 YAML 文件。"""
        strategies = YAMLStrategyParser.parse_directory(dir_path)
        if not strategies:
            print(f"⚠️  目录 {dir_path} 下没有找到 YAML 文件")
            return []

        print(f"📂 找到 {len(strategies)} 个策略文件")
        print("=" * 60)

        results = []
        for strategy in strategies:
            path = self.convert_file(strategy['source_file'], output_dir)
            if path:
                results.append(path)

        # 打印统计
        print("=" * 60)
        print(f"📊 转换统计:")
        print(f"   总计: {self.stats['total']}")
        print(f"   成功: {self.stats['success']}")
        print(f"   失败: {self.stats['failed']}")
        print(f"   跳过: {self.stats['skipped']}")
        print(f"   校验警告: {self.stats['validation_errors']}")

        return results

    def _auto_fix(self, code: str, errors: List[str]) -> Tuple[str, int]:
        """尝试自动修复常见问题。"""
        fixed = 0

        # 辅助函数：检查错误列表中是否包含某个子字符串
        def _has_error(substring: str) -> bool:
            return any(substring in err for err in errors)

        # 修复缺少 df['buy']/df['sell']
        if _has_error("缺少 df['buy'] 信号定义"):
            # 尝试找到类似的变量名并重命名
            if "df['raw_buy']" in code:
                code = code.replace("df['raw_buy']", "df['buy']")
                fixed += 1
            elif "df['signal_buy']" in code:
                code = code.replace("df['signal_buy']", "df['buy']")
                fixed += 1

        if _has_error("缺少 df['sell'] 信号定义"):
            if "df['raw_sell']" in code:
                code = code.replace("df['raw_sell']", "df['sell']")
                fixed += 1
            elif "df['signal_sell']" in code:
                code = code.replace("df['signal_sell']", "df['sell']")
                fixed += 1

        # 修复 on_bar 定义
        if _has_error("不应定义 on_bar 函数"):
            # 将 on_bar 函数体提取为顶层代码
            # 先用正则找到 on_bar 函数起始行
            on_bar_match = re.search(r'^(\s*)def\s+on_bar\s*\([^)]*\)\s*:\s*$', code, re.MULTILINE)
            if on_bar_match:
                indent = on_bar_match.group(1)  # 函数自身的缩进
                func_body_indent = indent + '    '  # 函数体缩进（通常是 +4 空格）
                # 替换函数定义行为注释
                code = code[:on_bar_match.start()] + '# Signal logic (extracted from on_bar)\n' + code[on_bar_match.end():]
                # 只对函数体内的行去缩进（匹配 func_body_indent 开头的行）
                lines = code.split('\n')
                new_lines = []
                in_func_body = False
                for line in lines:
                    if line.startswith(func_body_indent):
                        # 函数体内：去掉一层缩进
                        new_lines.append(line[len(func_body_indent):] if len(func_body_indent) > 0 else line.lstrip())
                        in_func_body = True
                    elif in_func_body and line.strip() == '':
                        # 函数体内的空行保留
                        new_lines.append(line)
                    elif in_func_body and not line.startswith(indent):
                        # 缩进小于等于函数定义级别 → 函数体结束
                        in_func_body = False
                        new_lines.append(line)
                    else:
                        new_lines.append(line)
                code = '\n'.join(new_lines)
            fixed += 1

        return code, fixed

    @staticmethod
    def _get_output_path(yaml_path: str, output_dir: Optional[str] = None) -> str:
        """计算输出文件路径。"""
        yaml_name = Path(yaml_path).stem
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            return os.path.join(output_dir, f"{yaml_name}.py")
        else:
            return str(Path(yaml_path).with_suffix('.py'))


# ============================================================
# 7. CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='QuantDinger YAML → IndicatorStrategy 自动转换器',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
        示例：
          # 转换单个策略
          python yaml_to_indicator.py strategies/ema_rsi_pullback.yaml

          # 批量转换
          python yaml_to_indicator.py --dir strategies/ --output output/

          # 使用 Ollama 本地模型
          python yaml_to_indicator.py --dir strategies/ \\
              --api-base http://localhost:11434/v1 \\
              --model qwen2.5-coder:32b

          # Dry run（只解析，不调用 LLM）
          python yaml_to_indicator.py --dir strategies/ --dry-run

          # 校验已有的 .py 文件
          python yaml_to_indicator.py --validate output/ema_rsi_pullback.py
        """),
    )

    parser.add_argument('file', nargs='?', help='单个 YAML 文件路径')
    parser.add_argument('--dir', '-d', help='批量转换目录')
    parser.add_argument('--output', '-o', help='输出目录（默认与 YAML 同目录）')
    parser.add_argument('--api-base', default=os.environ.get('LLM_API_BASE', 'https://api.openai.com/v1'),
                        help='LLM API 地址（默认读 LLM_API_BASE 环境变量）')
    parser.add_argument('--api-key', default=os.environ.get('LLM_API_KEY', ''),
                        help='LLM API Key（默认读 LLM_API_KEY 环境变量）')
    parser.add_argument('--model', default=os.environ.get('LLM_MODEL', 'gpt-4o'),
                        help='LLM 模型名（默认读 LLM_MODEL 环境变量或 gpt-4o）')
    parser.add_argument('--dry-run', action='store_true', help='只解析 YAML，不调用 LLM')
    parser.add_argument('--validate', metavar='FILE', help='校验已有的 IndicatorStrategy .py 文件')
    parser.add_argument('--validate-dir', metavar='DIR', help='批量校验目录下所有 .py 文件')

    args = parser.parse_args()

    # ── 校验模式 ──
    if args.validate:
        code = Path(args.validate).read_text(encoding='utf-8')
        is_valid, errors = CodeValidator.validate(code)
        if is_valid:
            print(f"✅ {args.validate} 校验通过")
        else:
            print(f"❌ {args.validate} 校验失败 ({len(errors)} 个问题):")
            for err in errors:
                print(f"   - {err}")
        sys.exit(0 if is_valid else 1)

    if args.validate_dir:
        total, passed, failed = 0, 0, 0
        for py_file in sorted(Path(args.validate_dir).glob('*.py')):
            if py_file.name.endswith('_meta.json'):
                continue
            total += 1
            code = py_file.read_text(encoding='utf-8')
            is_valid, errors = CodeValidator.validate(code)
            if is_valid:
                print(f"✅ {py_file.name}")
                passed += 1
            else:
                print(f"❌ {py_file.name}: {', '.join(errors[:3])}")
                failed += 1
        print(f"\n📊 校验结果: {passed}/{total} 通过, {failed} 失败")
        sys.exit(0 if failed == 0 else 1)

    # ── 转换模式 ──
    if not args.file and not args.dir:
        parser.print_help()
        sys.exit(1)

    # 初始化 LLM 生成器
    generator = None
    if not args.dry_run:
        if not args.api_key:
            print("❌ 需要提供 LLM API Key（--api-key 或 LLM_API_KEY 环境变量）", file=sys.stderr)
            sys.exit(1)
        generator = LLMStrategyGenerator(
            api_base=args.api_base,
            api_key=args.api_key,
            model=args.model,
        )
        print(f"🔗 LLM: {args.api_base} / {args.model}")

    pipeline = ConversionPipeline(generator=generator, dry_run=args.dry_run)

    if args.dir:
        results = pipeline.convert_directory(args.dir, args.output)
        sys.exit(0 if results or args.dry_run else 1)
    elif args.file:
        result = pipeline.convert_file(args.file, args.output)
        sys.exit(0 if result or args.dry_run else 1)


if __name__ == '__main__':
    main()
