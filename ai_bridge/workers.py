from __future__ import annotations

import asyncio
import base64
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import CircuitBreakerConfig, WorkerConfig


class WorkerUnavailable(RuntimeError):
    pass


@dataclass
class CircuitState:
    failures: int = 0
    down_until: float = 0.0


class WorkerRegistry:
    def __init__(self, workers: list[WorkerConfig], breaker: CircuitBreakerConfig):
        self._workers = {w.worker_id: w for w in workers}
        self._breaker = breaker
        self._circuits = {w.worker_id: CircuitState() for w in workers}
        self._semaphores = {w.worker_id: asyncio.Semaphore(w.max_concurrent_tasks) for w in workers}

    def get(self, worker_id: str) -> WorkerConfig:
        try:
            return self._workers[worker_id]
        except KeyError as exc:
            raise KeyError(f"unknown worker_id: {worker_id}") from exc

    def list_public(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        payload = []
        for worker in self._workers.values():
            circuit = self._circuits[worker.worker_id]
            payload.append(
                {
                    "worker_id": worker.worker_id,
                    "display_name": worker.display_name,
                    "model_name": worker.model_name,
                    "allowed_modes": worker.allowed_modes,
                    "timeout_limits": worker.timeout_limits.model_dump(),
                    "max_concurrent_tasks": worker.max_concurrent_tasks,
                    "status": "down" if circuit.down_until > now else "up",
                }
            )
        return payload

    def _headers(self, worker: WorkerConfig) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if worker.auth_type == "bearer":
            token = os.getenv(worker.secret_env or "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
        elif worker.auth_type == "basic":
            user = os.getenv(worker.username_env or "")
            password = os.getenv(worker.password_env or "")
            if user or password:
                raw = base64.b64encode(f"{user}:{password}".encode()).decode()
                headers["Authorization"] = f"Basic {raw}"
        return headers

    def assert_available(self, worker_id: str) -> None:
        circuit = self._circuits[worker_id]
        if circuit.down_until > time.monotonic():
            raise WorkerUnavailable(f"worker {worker_id} circuit breaker is open")

    def record_success(self, worker_id: str) -> None:
        self._circuits[worker_id] = CircuitState()

    def record_failure(self, worker_id: str) -> None:
        circuit = self._circuits[worker_id]
        circuit.failures += 1
        if circuit.failures >= self._breaker.failure_threshold:
            circuit.down_until = time.monotonic() + self._breaker.recovery_seconds

    async def call(self, worker_id: str, prompt: str, timeout_seconds: float) -> dict[str, Any]:
        worker = self.get(worker_id)
        self.assert_available(worker_id)
        messages: list[dict[str, str]] = []
        if worker.default_system_prompt:
            messages.append({"role": "system", "content": worker.default_system_prompt})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": worker.model_name, "messages": messages}
        async with self._semaphores[worker_id]:
            try:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    response = await client.post(
                        str(worker.endpoint_url),
                        headers=self._headers(worker),
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                self.record_success(worker_id)
                return self._normalize_response(data)
            except Exception:
                self.record_failure(worker_id)
                raise

    @staticmethod
    def _normalize_response(data: dict[str, Any]) -> dict[str, Any]:
        content = None
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = data.get("content") or data.get("text")
        return {"content": content, "raw": data}
