from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

AuthType = Literal["none", "bearer", "basic"]
Mode = Literal["sync", "async"]
Scope = Literal["read", "submit", "cancel", "admin"]
RecoveryPolicy = Literal["manual", "idempotent", "always"]


class TimeoutLimits(BaseModel):
    sync_seconds: float = Field(default=30, gt=0)
    async_seconds: float = Field(default=300, gt=0)


class FilesystemPermissions(BaseModel):
    read: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)
    canonicalize: bool = False

    @field_validator("read", "write")
    @classmethod
    def paths_are_absolute(cls, value: list[str]) -> list[str]:
        for path in value:
            if not path.startswith("/"):
                raise ValueError("filesystem paths must be absolute")
        return value


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
    capabilities: list[str] = Field(default_factory=list)
    description: str = Field(default="")
    allowed_modes: list[Mode] = Field(default_factory=lambda: ["sync", "async"])
    timeout_limits: TimeoutLimits = Field(default_factory=TimeoutLimits)
    filesystem: FilesystemPermissions = Field(default_factory=FilesystemPermissions)
    max_concurrent_tasks: int = Field(default=2, ge=1)

    @field_validator("endpoint_url", mode="before")
    @classmethod
    def normalize_endpoint_url(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            return stripped
        parts = urlsplit(stripped)
        path = parts.path.rstrip("/")
        if path.endswith("/chat/completions"):
            return stripped
        if path.endswith("/v1"):
            normalized_path = f"{path}/chat/completions"
        elif path in {"", "/"}:
            normalized_path = "/v1/chat/completions"
        else:
            normalized_path = f"{path}/v1/chat/completions"
        return urlunsplit((parts.scheme, parts.netloc, normalized_path, parts.query, parts.fragment))

    @field_validator("allowed_modes")
    @classmethod
    def modes_not_empty(cls, value: list[Mode]) -> list[Mode]:
        if not value:
            raise ValueError("allowed_modes must not be empty")
        return value


class ScopedKeyConfig(BaseModel):
    key_id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_.-]+$")
    env: str = Field(min_length=1)
    scopes: list[Scope] = Field(default_factory=list)

    @field_validator("scopes")
    @classmethod
    def scopes_not_empty(cls, value: list[Scope]) -> list[Scope]:
        if not value:
            raise ValueError("scoped auth keys must include at least one scope")
        return value


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"
    port: int = 8080


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scoped_keys: list[ScopedKeyConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def scoped_keys_required(self) -> "AuthConfig":
        from .auth import validate_scoped_keys_present

        validate_scoped_keys_present(self.scoped_keys)
        return self


class StateConfig(BaseModel):
    sqlite_path: str = "/app/data/tasks.sqlite3"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file_path: str = "/app/logs/bridge.log"


class AuditConfig(BaseModel):
    enabled: bool = True
    file_path: str = "/app/logs/audit.jsonl"


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = Field(default=3, ge=1)
    recovery_seconds: float = Field(default=30, gt=0)


class RecoveryConfig(BaseModel):
    policy: RecoveryPolicy = "idempotent"
    delay_seconds: float = Field(default=0, ge=0)


class LimitsConfig(BaseModel):
    global_pending_tasks: int = Field(default=1000, ge=0)
    global_active_tasks: int = Field(default=100, ge=1)
    per_worker_pending_tasks: int = Field(default=100, ge=0)
    per_worker_active_tasks: int | None = Field(default=None, ge=1)
    sync_active_tasks: int = Field(default=100, ge=1)


class CompatConfig(BaseModel):
    allow_implicit_root_filesystem: bool = False


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    recovery: RecoveryConfig = Field(default_factory=RecoveryConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    compat: CompatConfig = Field(default_factory=CompatConfig)
    workers: list[WorkerConfig]

    @field_validator("workers")
    @classmethod
    def worker_ids_unique(cls, workers: list[WorkerConfig]) -> list[WorkerConfig]:
        ids = [w.worker_id for w in workers]
        if len(ids) != len(set(ids)):
            raise ValueError("worker_id values must be unique")
        return workers

    def apply_compatibility(self) -> "AppConfig":
        if self.compat.allow_implicit_root_filesystem:
            for worker in self.workers:
                if not worker.filesystem.read and not worker.filesystem.write:
                    worker.filesystem.read = ["/"]
                    worker.filesystem.write = ["/"]
        return self


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
    return AppConfig.model_validate(raw).apply_compatibility()
