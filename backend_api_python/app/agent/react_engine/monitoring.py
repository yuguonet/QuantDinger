#!/usr/bin/env python
# coding=utf-8

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
from dataclasses import dataclass, field
from enum import IntEnum


__all__ = ["AgentLogger", "LogLevel", "Monitor", "TokenUsage", "Timing"]


@dataclass
class TokenUsage:
    """
    Contains the token usage information for a given step or run.
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int = field(init=False)

    def __post_init__(self):
        self.total_tokens = self.input_tokens + self.output_tokens

    def dict(self):
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class Timing:
    """
    Contains the timing information for a given step or run.
    """

    start_time: float
    end_time: float | None = None

    @property
    def duration(self):
        return None if self.end_time is None else self.end_time - self.start_time

    def dict(self):
        return {
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration": self.duration,
        }

    def __repr__(self) -> str:
        return f"Timing(start_time={self.start_time}, end_time={self.end_time}, duration={self.duration})"


class Monitor:
    def __init__(self, tracked_model, logger):
        self.step_durations = []
        self.tracked_model = tracked_model
        self.logger = logger
        self.total_input_token_count = 0
        self.total_output_token_count = 0

    def get_total_token_counts(self) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.total_input_token_count,
            output_tokens=self.total_output_token_count,
        )

    def reset(self):
        self.step_durations = []
        self.total_input_token_count = 0
        self.total_output_token_count = 0

    def update_metrics(self, step_log):
        """Update the metrics of the monitor.

        Args:
            step_log ([`MemoryStep`]): Step log to update the monitor with.
        """
        step_duration = step_log.timing.duration
        self.step_durations.append(step_duration)
        console_outputs = f"[Step {len(self.step_durations)}: Duration {step_duration:.2f} seconds"

        if step_log.token_usage is not None:
            self.total_input_token_count += step_log.token_usage.input_tokens
            self.total_output_token_count += step_log.token_usage.output_tokens
            console_outputs += (
                f"| Input tokens: {self.total_input_token_count:,} | Output tokens: {self.total_output_token_count:,}"
            )
        console_outputs += "]"
        self.logger.log(console_outputs, level=LogLevel.INFO)


class LogLevel(IntEnum):
    OFF = -1  # No output
    ERROR = 0  # Only errors
    INFO = 1  # Normal output (default)
    DEBUG = 2  # Detailed output


YELLOW_HEX = "#d4b702"


class Console:
    """Simple console wrapper using print() — replaces rich Console."""

    def __init__(self, highlight: bool = False):
        self.highlight = highlight

    def print(self, *args, **kwargs):
        for arg in args:
            print(str(arg))


class AgentLogger:
    def __init__(self, level: LogLevel = LogLevel.INFO, console: Console | None = None):
        self.level = level
        self.console = console if console is not None else Console(highlight=False)

    def log(self, *args, level: int | str | LogLevel = LogLevel.INFO, **kwargs) -> None:
        """Logs a message to the console.

        Args:
            level (LogLevel, optional): Defaults to LogLevel.INFO.
        """
        if isinstance(level, str):
            level = LogLevel[level.upper()]
        if level <= self.level:
            self.console.print(*args, **kwargs)

    def log_error(self, error_message: str) -> None:
        self.log(f"[ERROR] {error_message}", level=LogLevel.ERROR)

    def log_markdown(self, content: str, title: str | None = None, level=LogLevel.INFO) -> None:
        if level > self.level:
            return
        if title:
            self.log(f"\n{'-' * 40}\n  {title}\n{'-' * 40}", level=level)
        self.log(content, level=level)

    def log_code(self, title: str, content: str, level: int = LogLevel.INFO) -> None:
        if level > self.level:
            return
        self.log(f"\n{'-' * 40}\n  {title}\n{'-' * 40}\n{content}", level=level)

    def log_rule(self, title: str, level: int = LogLevel.INFO) -> None:
        self.log(f"\n{'=' * 40} {title} {'=' * 40}", level=LogLevel.INFO)

    def log_task(self, content: str, subtitle: str, title: str | None = None, level: LogLevel = LogLevel.INFO) -> None:
        if level > self.level:
            return
        header = f"\n{'=' * 60}"
        header += f"\n  New run{' - ' + title if title else ''}"
        header += f"\n  {subtitle}"
        header += f"\n{'=' * 60}"
        self.log(header, level=level)
        self.log(content, level=level)

    def log_messages(self, messages: list, level: LogLevel = LogLevel.DEBUG) -> None:
        if level > self.level:
            return
        messages_as_string = "\n".join([json.dumps(msg.dict(), indent=4) for msg in messages])
        self.log(messages_as_string, level=level)

    def visualize_agent_tree(self, agent):
        """Prints a simple text tree visualization of the agent's structure."""
        name = agent.__class__.__name__
        model_id = agent.model.model_id if hasattr(agent.model, 'model_id') else 'N/A'
        print(f"\n{'=' * 48}")
        print(f"  Agent: {name}")
        print(f"  Model: {model_id}")
        print(f"{'-' * 48}")
        print(f"  Tools:")
        for t_name, t_obj in agent.tools.items():
            desc = getattr(t_obj, 'description', str(t_obj))
            if len(desc) > 80:
                desc = desc[:80] + '...'
            print(f"    - {t_name}: {desc}")
        if agent.managed_agents:
            print(f"  Managed agents:")
            for ma_name in agent.managed_agents:
                print(f"    - {ma_name}")
        print(f"{'=' * 48}")
