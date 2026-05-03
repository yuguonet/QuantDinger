# -*- coding: utf-8 -*-
"""
数据源模块 — 统一多数据源接入

本包是整个数据源子系统的核心，包含：
- Provider 层:  各数据源实现（腾讯/新浪/东财/AkShare/港股）
- 工厂层:       DataSourceFactory（复权/去重/fallback/竞赛）
- 协助层:       Coordinator（并发协调/动态队列/吞吐反馈）
- 熔断器:       CircuitBreaker（故障隔离/自动恢复）
- 缓存层:       两级缓存（内存 + 磁盘 feather）
- 标准化:       代码标准化/市场推断/复权计算

设计原则:
  - Provider 自注册: import 即注册，上层不感知具体源
  - 能力声明: 每个 Provider 声明支持的能力/周期/市场
  - 统一接口: fetch_kline / fetch_quote 所有源一样
  - 编排层按能力 + 熔断状态选源，不写死顺序

公开符号:
  DataSourceFactory: 数据源工厂（单例）
  get_factory:       获取全局工厂实例
  SourceAdapter:     适配器（兼容旧 BaseDataSource 接口）
  CircuitBreaker:    熔断器
  detect_market:     市场类型检测
"""

from app.data_sources.factory import DataSourceFactory, get_factory, SourceAdapter
from app.data_sources.circuit_breaker import (
    CircuitBreaker,
    get_realtime_circuit_breaker,
    get_overseas_circuit_breaker,
)
from app.data_sources.normalizer import detect_market

__all__ = [
    'DataSourceFactory',
    'get_factory',
    'SourceAdapter',
    'CircuitBreaker',
    'get_realtime_circuit_breaker',
    'get_overseas_circuit_breaker',
    'detect_market',
]
