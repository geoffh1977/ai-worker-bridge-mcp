from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator

AuthType = Literal["none", "bearer", "basic"]
Mode = Literal["sync", "async"]


class TimeoutLimits(BaseModel):
    sync_seconds: float = Field(default=30, gt=0)
    async_seconds: float = Field(default=300, gt=0)


class WorkerConfig(BaseModel):
    worker_id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_.-]+$")
    display_name: str = Field(min_length=1)
    endpoint_url: HttpUrl
    auth_type: AuthType = "bearer"
    secret_env: str | None = None
    username_env: str | None = None
    password_env: str | None = None
    model_name: str = Field(min_length=1)
    default_system_prompt: str | None = None
    allowed_modes: list[Mode] = Field(default_factory=lambda: ["sync", "async"])
    timeout_limits: TimeoutLimits = Field(default_factory=TimeoutLimits)
    max_concurrent_tasks: int = Field(default=2, ge=1)

    @field_validator("allowed_modes")
    @classmethod
    def modes_not_empty(cls, value: list[Mode]) -> list[Mode]:
        if not value:
            raise ValueError("allowed_modes must not be empty")
        return value


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    api_key_env: str = "AI_BRIDGE_API_KEY"
    require_api_key: bool = True


class StateConfig(BaseModel):
    sqlite_path: str = "/app/data/tasks.sqlite3"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file_path: str = "/app/logs/bridge.log"


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = Field(default=3, ge=1)
    recovery_seconds: float = Field(default=30, gt=0)


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    workers: list[WorkerConfig]

    @field_validator("workers")
    @classmethod
    def worker_ids_unique(cls, workers: list[WorkerConfig]) -> list[WorkerConfig]:
        ids = [w.worker_id for w in workers]
        if len(ids) != len(set(ids)):
            raise ValueError("worker_id values must be unique")
        return workers


@dataclass(frozen=True)
class RuntimePaths:
    config_path: Path
    data_dir: Path
    log_dir: Path


def runtime_paths() -> RuntimePaths:
    return RuntimePaths(
        config_path=Path(os.getenv("AI_BRIDGE_CONFIG", "config.yaml")).expanduser(),
        data_dir=Path(os.getenv("AI_BRIDGE_DATA_DIR", "./data")).expanduser(),
        log_dir=Path(os.getenv("AI_BRIDGE_LOG_DIR", "./logs")).expanduser(),
    )


class ConfigError(RuntimeError):
    """Raised when runtime configuration cannot be loaded safely."""


def load_config(path: str | Path | None = None) -> AppConfig:
    resolved = Path(path) if path else runtime_paths().config_path
    try:
        with resolved.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise ConfigError(
            f"Configuration file not found: {resolved}. "
            "Create it from config.yaml.example or set AI_BRIDGE_CONFIG to a readable file."
        ) from None
    except PermissionError:
        raise ConfigError(
            f"Permission denied reading configuration file: {resolved}. "
            "Ensure the file is readable by the app user, for example chmod 644 config.yaml."
        ) from None
    return AppConfig.model_validate(raw)
