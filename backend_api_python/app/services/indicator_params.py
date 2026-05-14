"""
Indicator Parameters Parser and Helper Functions

支持三个核心功能：
1. 指标参数外部传递 - 解析指标代码中的 @param 声明
2. 指标调用其他指标 - 提供 call_indicator() 函数
3. 参数优化搜索 - 从 @param 声明中提取搜索范围，生成参数组合

参数声明格式（向后兼容）：
  # @param param_name type default_value 描述
  # @param param_name type default_value 描述 [min:max:step]

示例：
  # @param ma_fast int 5 短期均线周期              ← 仅默认值（无搜索范围）
  # @param ma_slow int 20 长期均线周期 [10:200:1]  ← 带搜索范围，可用于参数优化
  # @param threshold float 0.5 阈值 [0.1:0.9:0.05]
  # @param use_filter bool true 是否启用过滤       ← bool 类型自动展开为 [True, False]

支持的类型：int, float, bool, str
"""

import re
import json
from typing import Dict, Any, List, Optional, Tuple
from app.utils.logger import get_logger
from app.utils.db import get_db_connection

logger = get_logger(__name__)


class StrategyConfigParser:
    """
    解析指标代码中的 @strategy 注解，提取策略配置（止盈止损、仓位等）。

    支持的注解格式:
        # @strategy stopLossPct 0.02 止损比例
        # @strategy takeProfitPct 0.05 止盈比例
        # @strategy entryPct 0.5 仓位比例 (0-1)
        # 杠杆倍数由指标 IDE 回测面板单独设置，不再使用 @strategy leverage
        # @strategy trailingEnabled true 启用追踪止损
        # @strategy trailingStopPct 0.02 追踪止损比例
        # @strategy trailingActivationPct 0.03 追踪激活比例
        # @strategy tradeDirection long 交易方向
    """

    # 允许 key 与数值之间使用可选冒号，与指标 IDE 前端解析一致
    STRATEGY_PATTERN = re.compile(
        r'#\s*@strategy\s+(\w+)\s*:?\s*(\S+)\s*(.*)',
        re.IGNORECASE
    )

    VALID_KEYS = {
        'stopLossPct':          {'type': 'float', 'min': 0, 'max': 1},
        'takeProfitPct':        {'type': 'float', 'min': 0, 'max': 5},
        'entryPct':             {'type': 'float', 'min': 0.01, 'max': 1},
        'trailingEnabled':      {'type': 'bool'},
        'trailingStopPct':      {'type': 'float', 'min': 0, 'max': 1},
        'trailingActivationPct':{'type': 'float', 'min': 0, 'max': 1},
        'tradeDirection':       {'type': 'str',   'enum': ['long', 'short', 'both']},
    }

    @classmethod
    def parse(cls, code: str) -> Dict[str, Any]:
        """
        解析代码中的 @strategy 注解，返回策略配置字典。
        只包含代码中声明的键，未声明的不包含。
        """
        config: Dict[str, Any] = {}
        if not code:
            return config
        for line in code.split('\n'):
            line = line.strip()
            m = cls.STRATEGY_PATTERN.match(line)
            if not m:
                continue
            key = m.group(1)
            raw_val = m.group(2)
            if key not in cls.VALID_KEYS:
                continue
            spec = cls.VALID_KEYS[key]
            val = cls._convert(raw_val, spec)
            if val is not None:
                config[key] = val
        return config

    @classmethod
    def _convert(cls, raw: str, spec: Dict) -> Any:
        t = spec['type']
        try:
            if t == 'float':
                v = float(raw)
                v = max(spec.get('min', v), min(spec.get('max', v), v))
                return round(v, 6)
            elif t == 'int':
                v = int(raw)
                v = max(spec.get('min', v), min(spec.get('max', v), v))
                return v
            elif t == 'bool':
                return raw.lower() in ('true', '1', 'yes', 'on')
            elif t == 'str':
                if 'enum' in spec and raw not in spec['enum']:
                    return spec['enum'][0]
                return raw
        except (ValueError, TypeError):
            return None
        return None

    @classmethod
    def generate_annotations(cls, config: Dict[str, Any]) -> str:
        """
        从策略配置字典生成 @strategy 注解行。
        用于AI生成代码时自动附加。
        """
        lines = []
        for key, spec in cls.VALID_KEYS.items():
            if key in config:
                val = config[key]
                if spec['type'] == 'bool':
                    val = 'true' if val else 'false'
                lines.append(f'# @strategy {key} {val}')
        return '\n'.join(lines)


class IndicatorParamsParser:
    """解析指标代码中的参数声明，支持搜索范围（用于参数优化）。"""

    # 参数声明正则（向后兼容）：
    #   # @param name type default description
    #   # @param name type default description [min:max:step]
    # range 内容只允许数字/冒号/空格/点/正负号，避免误匹配描述中的 [文字]
    PARAM_PATTERN = re.compile(
        r'#\s*@param\s+(\w+)\s+(int|float|bool|str|string)\s+(\S+)\s*(.*?)'
        r'(?:\[([\d\s:+.\-]+)\])?\s*$',
        re.IGNORECASE
    )

    # 搜索范围正则：min:max:step 或 min:max（step 默认 1）
    RANGE_PATTERN = re.compile(
        r'^([+-]?\d+\.?\d*)\s*:\s*([+-]?\d+\.?\d*)(?:\s*:\s*([+-]?\d+\.?\d*))?$'
    )

    @classmethod
    def parse_params(cls, indicator_code: str) -> List[Dict[str, Any]]:
        """
        解析指标代码中的参数声明。

        Returns:
            List of param definitions:
            [
                {
                    "name": "ma_fast",
                    "type": "int",
                    "default": 5,
                    "description": "短期均线周期",
                    # 以下字段仅在声明了搜索范围时存在：
                    "min": 3,
                    "max": 50,
                    "step": 1,
                    "searchable": True,
                },
                ...
            ]
        """
        params = []
        if not indicator_code:
            return params

        for line in indicator_code.split('\n'):
            line = line.strip()
            match = cls.PARAM_PATTERN.match(line)
            if not match:
                continue

            name = match.group(1)
            param_type = match.group(2).lower()
            default_str = match.group(3)
            description = (match.group(4) or '').strip()
            range_str = (match.group(5) or '').strip()

            # 规范化类型名
            if param_type == 'string':
                param_type = 'str'

            # 转换默认值类型
            default = cls._convert_value(default_str, param_type)

            param_def: Dict[str, Any] = {
                "name": name,
                "type": param_type,
                "default": default,
                "description": description,
            }

            # 解析搜索范围 [min:max:step]
            if range_str:
                parsed = cls._parse_range(range_str, param_type)
                if parsed is not None:
                    param_def.update(parsed)
                    param_def["searchable"] = True

            # bool 类型：自动视为可搜索（展开为 [True, False]）
            if param_type == 'bool' and 'searchable' not in param_def:
                param_def["searchable"] = True

            params.append(param_def)

        return params

    @classmethod
    def _parse_range(cls, range_str: str, param_type: str) -> Optional[Dict[str, Any]]:
        """解析 [min:max:step] 搜索范围字符串。"""
        m = cls.RANGE_PATTERN.match(range_str.strip())
        if not m:
            return None

        low = cls._convert_value(m.group(1), param_type)
        high = cls._convert_value(m.group(2), param_type)
        step_raw = m.group(3)

        if low is None or high is None:
            return None
        if low >= high:
            return None

        if param_type == 'int':
            step = int(step_raw) if step_raw else 1
            return {"min": int(low), "max": int(high), "step": step}
        elif param_type == 'float':
            step = float(step_raw) if step_raw else 0.01
            return {"min": float(low), "max": float(high), "step": step}

        return None

    @classmethod
    def _convert_value(cls, value_str: str, param_type: str) -> Any:
        """转换字符串值为对应类型。"""
        try:
            param_type = param_type.lower()
            if param_type == 'int':
                return int(value_str)
            elif param_type == 'float':
                return float(value_str)
            elif param_type == 'bool':
                return value_str.lower() in ('true', '1', 'yes', 'on')
            else:  # str/string
                return value_str
        except (ValueError, TypeError):
            return value_str

    @classmethod
    def merge_params(cls, declared_params: List[Dict], user_params: Dict[str, Any]) -> Dict[str, Any]:
        """
        合并声明的参数和用户提供的参数。

        Args:
            declared_params: 从代码中解析的参数声明
            user_params: 用户提供的参数值

        Returns:
            合并后的参数字典（使用用户值或默认值）
        """
        result = {}
        for param in declared_params:
            name = param['name']
            param_type = param['type']
            default = param['default']

            if name in user_params:
                result[name] = cls._convert_value(str(user_params[name]), param_type)
            else:
                result[name] = default

        return result

    # ================================================================
    # 参数优化：搜索空间 & 组合生成
    # ================================================================

    @classmethod
    def get_searchable_params(cls, declared_params: List[Dict]) -> List[Dict[str, Any]]:
        """返回所有声明了搜索范围的参数（searchable=True）。"""
        return [p for p in declared_params if p.get("searchable")]

    @classmethod
    def generate_param_grid(
        cls,
        declared_params: List[Dict[str, Any]],
        max_combinations: int = 5000,
    ) -> List[Dict[str, Any]]:
        """
        从参数声明生成笛卡尔积搜索网格。

        - 有搜索范围的参数（searchable=True）按 [min:max:step] 展开
        - bool 参数展开为 [True, False]
        - 无搜索范围的非 bool 参数保持默认值不变
        - 超过 max_combinations 时自动加大 step 降采样

        Returns:
            参数组合列表，每组是一个 dict（可直接传入 merge_params 的 user_params）
        """
        from itertools import product

        # 收集每个可搜索参数的候选值
        axes: List[Tuple[str, List[Any]]] = []
        fixed: Dict[str, Any] = {}

        for p in declared_params:
            name = p["name"]
            ptype = p["type"]
            default = p["default"]

            if p.get("searchable"):
                if ptype == "bool":
                    axes.append((name, [True, False]))
                elif "min" in p and "max" in p:
                    low, high, step = p["min"], p["max"], p.get("step", 1)
                    values = cls._range_values(low, high, step, ptype)
                    if values:
                        axes.append((name, values))
                    else:
                        fixed[name] = default
                else:
                    fixed[name] = default
            else:
                fixed[name] = default

        if not axes:
            return [fixed]

        # 计算总组合数，超限则降采样
        total = 1
        for _, vals in axes:
            total *= len(vals)

        if total > max_combinations:
            # 按比例缩减每个轴的采样数
            ratio = (max_combinations / total) ** (1.0 / len(axes))
            adjusted_axes = []
            for name, vals in axes:
                new_count = max(2, int(len(vals) * ratio))
                if new_count >= len(vals):
                    adjusted_axes.append((name, vals))
                else:
                    indices = [int(i * (len(vals) - 1) / (new_count - 1)) for i in range(new_count)]
                    adjusted_axes.append((name, [vals[i] for i in indices]))
            axes = adjusted_axes

        # 笛卡尔积
        combos = []
        for values in product(*(vals for _, vals in axes)):
            combo = dict(fixed)
            for i, (name, _) in enumerate(axes):
                combo[name] = values[i]
            combos.append(combo)

        return combos

    @staticmethod
    def _range_values(low: Any, high: Any, step: Any, ptype: str) -> List[Any]:
        """生成 [low, high] 范围内的离散值列表。"""
        if step is None or step <= 0:
            step = 1

        values = []
        if ptype == "int":
            step = max(1, int(step))
            v = int(low)
            while v <= int(high):
                values.append(v)
                v += step
        else:  # float
            import math
            step = float(step)
            n_steps = int(math.floor((float(high) - float(low)) / step)) + 1
            for i in range(n_steps):
                v = round(float(low) + i * step, 10)
                if v > float(high):
                    break
                values.append(v)

        return values if values else [low]

    @classmethod
    def generate_random_params(
        cls,
        declared_params: List[Dict[str, Any]],
        n_samples: int = 100,
        seed: int = 42,
    ) -> List[Dict[str, Any]]:
        """
        随机采样 n 组参数（适合参数空间太大时用随机搜索代替网格搜索）。

        Returns:
            参数组合列表
        """
        import random as _random
        _random.seed(seed)

        searchable = []
        fixed: Dict[str, Any] = {}

        for p in declared_params:
            if p.get("searchable"):
                searchable.append(p)
            else:
                fixed[p["name"]] = p["default"]

        if not searchable:
            return [fixed]

        combos = []
        for _ in range(n_samples):
            combo = dict(fixed)
            for p in searchable:
                name, ptype = p["name"], p["type"]
                if ptype == "bool":
                    combo[name] = _random.choice([True, False])
                elif "min" in p and "max" in p:
                    low, high, step = p["min"], p["max"], p.get("step", 1)
                    if ptype == "int":
                        n_steps = max(1, int((high - low) / step) + 1)
                        combo[name] = int(low) + _random.randint(0, n_steps - 1) * int(step)
                    else:
                        n_steps = max(1, int((high - low) / step) + 1)
                        combo[name] = round(low + _random.randint(0, n_steps - 1) * step, 10)
                else:
                    combo[name] = p["default"]
            combos.append(combo)

        return combos


class IndicatorCaller:
    """
    指标调用器 - 允许一个指标调用另一个指标
    
    使用方式（在指标代码中）：
        # 按ID调用
        rsi_df = call_indicator(5, df)
        
        # 按名称调用（自己的指标）
        macd_df = call_indicator('My MACD', df)
    """
    
    # 最大调用深度，防止循环依赖
    MAX_CALL_DEPTH = 5
    
    def __init__(self, user_id: int, current_indicator_id: int = None):
        self.user_id = user_id
        self.current_indicator_id = current_indicator_id
        self._call_stack = []  # 调用栈，用于检测循环依赖
    
    def call_indicator(
        self, 
        indicator_ref: Any,  # int (ID) 或 str (名称)
        df: 'pd.DataFrame',
        params: Dict[str, Any] = None,
        _depth: int = 0
    ) -> Optional['pd.DataFrame']:
        """
        调用另一个指标并返回结果
        
        Args:
            indicator_ref: 指标ID或名称
            df: 输入的K线数据
            params: 传递给被调用指标的参数
            _depth: 内部使用，跟踪调用深度
            
        Returns:
            执行后的DataFrame，包含被调用指标计算的列
        """
        import pandas as pd
        import numpy as np
        
        # 检查调用深度
        if _depth >= self.MAX_CALL_DEPTH:
            logger.error(f"Indicator call depth exceeded {self.MAX_CALL_DEPTH}")
            return df.copy()
        
        # 获取指标代码
        indicator_code, indicator_id = self._get_indicator_code(indicator_ref)
        if not indicator_code:
            logger.warning(f"Indicator not found: {indicator_ref}")
            return df.copy()
        
        # 检查循环依赖
        if indicator_id in self._call_stack:
            logger.error(f"Circular dependency detected: {self._call_stack} -> {indicator_id}")
            return df.copy()
        
        self._call_stack.append(indicator_id)
        
        try:
            # 解析并合并参数
            declared_params = IndicatorParamsParser.parse_params(indicator_code)
            merged_params = IndicatorParamsParser.merge_params(declared_params, params or {})
            
            # 准备执行环境
            df_copy = df.copy()
            local_vars = {
                'df': df_copy,
                'open': df_copy['open'].astype('float64') if 'open' in df_copy.columns else pd.Series(dtype='float64'),
                'high': df_copy['high'].astype('float64') if 'high' in df_copy.columns else pd.Series(dtype='float64'),
                'low': df_copy['low'].astype('float64') if 'low' in df_copy.columns else pd.Series(dtype='float64'),
                'close': df_copy['close'].astype('float64') if 'close' in df_copy.columns else pd.Series(dtype='float64'),
                'volume': df_copy['volume'].astype('float64') if 'volume' in df_copy.columns else pd.Series(dtype='float64'),
                'signals': pd.Series(0, index=df_copy.index, dtype='float64'),
                'np': np,
                'pd': pd,
                'params': merged_params,
                # 递归调用支持
                'call_indicator': lambda ref, d, p=None: self.call_indicator(ref, d, p, _depth + 1)
            }
            
            from app.utils.safe_exec import build_safe_builtins, safe_exec_with_validation

            exec_env = local_vars.copy()
            exec_env['__builtins__'] = build_safe_builtins()

            exec_result = safe_exec_with_validation(
                code=indicator_code,
                exec_globals=exec_env,
                timeout=30,
            )
            if not exec_result['success']:
                logger.error(f"Indicator {indicator_ref} rejected: {exec_result['error']}")
                return df.copy()
            
            return exec_env.get('df', df_copy)
            
        except Exception as e:
            logger.error(f"Error calling indicator {indicator_ref}: {e}")
            return df.copy()
        finally:
            self._call_stack.pop()
    
    def _get_indicator_code(self, indicator_ref: Any) -> Tuple[Optional[str], Optional[int]]:
        """获取指标代码"""
        try:
            with get_db_connection() as db:
                cursor = db.cursor()
                
                if isinstance(indicator_ref, int):
                    # 按ID查询
                    cursor.execute("""
                        SELECT id, code FROM qd_indicator_codes 
                        WHERE id = %s AND (user_id = %s OR publish_to_community = 1)
                    """, (indicator_ref, self.user_id))
                else:
                    # 按名称查询（优先自己的指标）
                    cursor.execute("""
                        SELECT id, code FROM qd_indicator_codes 
                        WHERE name = %s AND user_id = %s
                        UNION
                        SELECT id, code FROM qd_indicator_codes 
                        WHERE name = %s AND publish_to_community = 1
                        LIMIT 1
                    """, (str(indicator_ref), self.user_id, str(indicator_ref)))
                
                row = cursor.fetchone()
                cursor.close()
                
                if row:
                    return row['code'], row['id']
                return None, None
                
        except Exception as e:
            logger.error(f"Error fetching indicator code: {e}")
            return None, None


def get_indicator_params(indicator_id: int) -> List[Dict[str, Any]]:
    """
    获取指标的参数声明（供API调用）
    
    Args:
        indicator_id: 指标ID
        
    Returns:
        参数声明列表
    """
    try:
        with get_db_connection() as db:
            cursor = db.cursor()
            cursor.execute("SELECT code FROM qd_indicator_codes WHERE id = %s", (indicator_id,))
            row = cursor.fetchone()
            cursor.close()
            
            if row and row['code']:
                return IndicatorParamsParser.parse_params(row['code'])
            return []
    except Exception as e:
        logger.error(f"Error getting indicator params: {e}")
        return []
