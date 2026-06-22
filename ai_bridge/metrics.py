from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import DefaultDict


@dataclass
class MetricsRegistry:
    tasks_created: DefaultDict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    tasks_completed: DefaultDict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    worker_failures: DefaultDict[str, int] = field(default_factory=lambda: defaultdict(int))
    circuit_open: DefaultDict[str, int] = field(default_factory=lambda: defaultdict(int))
    worker_call_count: DefaultDict[str, int] = field(default_factory=lambda: defaultdict(int))
    worker_call_sum: DefaultDict[str, float] = field(default_factory=lambda: defaultdict(float))
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def inc_task_created(self, worker_id: str, mode: str) -> None:
        with self._lock:
            self.tasks_created[(worker_id, mode)] += 1

    def inc_task_completed(self, worker_id: str, state: str) -> None:
        with self._lock:
            self.tasks_completed[(worker_id, state)] += 1

    def inc_worker_failure(self, worker_id: str) -> None:
        with self._lock:
            self.worker_failures[worker_id] += 1

    def inc_circuit_open(self, worker_id: str) -> None:
        with self._lock:
            self.circuit_open[worker_id] += 1

    def observe_worker_call(self, worker_id: str, seconds: float) -> None:
        with self._lock:
            self.worker_call_count[worker_id] += 1
            self.worker_call_sum[worker_id] += seconds

    def render(self, *, active_tasks: int = 0, queued_tasks: int = 0) -> str:
        lines = [
            "# HELP ai_bridge_tasks_created_total Durable tasks created by worker and mode.",
            "# TYPE ai_bridge_tasks_created_total counter",
        ]
        with self._lock:
            for (worker_id, mode), value in sorted(self.tasks_created.items()):
                lines.append(f'ai_bridge_tasks_created_total{{worker_id="{worker_id}",mode="{mode}"}} {value}')
            lines.extend([
                "# HELP ai_bridge_tasks_completed_total Tasks completed by worker and terminal state.",
                "# TYPE ai_bridge_tasks_completed_total counter",
            ])
            for (worker_id, state), value in sorted(self.tasks_completed.items()):
                lines.append(f'ai_bridge_tasks_completed_total{{worker_id="{worker_id}",state="{state}"}} {value}')
            lines.extend([
                "# HELP ai_bridge_worker_failures_total Worker call failures by worker.",
                "# TYPE ai_bridge_worker_failures_total counter",
            ])
            for worker_id, value in sorted(self.worker_failures.items()):
                lines.append(f'ai_bridge_worker_failures_total{{worker_id="{worker_id}"}} {value}')
            lines.extend([
                "# HELP ai_bridge_circuit_open_total Circuit breaker openings by worker.",
                "# TYPE ai_bridge_circuit_open_total counter",
            ])
            for worker_id, value in sorted(self.circuit_open.items()):
                lines.append(f'ai_bridge_circuit_open_total{{worker_id="{worker_id}"}} {value}')
            lines.extend([
                "# HELP ai_bridge_worker_call_seconds Worker call latency summary.",
                "# TYPE ai_bridge_worker_call_seconds summary",
            ])
            for worker_id, count in sorted(self.worker_call_count.items()):
                lines.append(f'ai_bridge_worker_call_seconds_count{{worker_id="{worker_id}"}} {count}')
                lines.append(f'ai_bridge_worker_call_seconds_sum{{worker_id="{worker_id}"}} {self.worker_call_sum[worker_id]:.6f}')
        lines.extend([
            "# HELP ai_bridge_active_tasks Active in-process async tasks.",
            "# TYPE ai_bridge_active_tasks gauge",
            f"ai_bridge_active_tasks {active_tasks}",
            "# HELP ai_bridge_queued_tasks Durable queued tasks.",
            "# TYPE ai_bridge_queued_tasks gauge",
            f"ai_bridge_queued_tasks {queued_tasks}",
        ])
        return "\n".join(lines) + "\n"


def monotonic() -> float:
    return time.monotonic()
