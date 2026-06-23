from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

TERMINAL_STATES = frozenset({"Completed", "Failed", "Cancelled", "TimedOut"})
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_TIMEOUT_SECONDS = 1800.0
DEFAULT_REQUEST_TIMEOUT = 15.0
DEFAULT_MAX_TRANSIENT_ERRORS = 5


class WatchdogError(Exception):
    """Base exception for watchdog failures."""


class ConfigError(WatchdogError):
    """Raised when required configuration is missing or invalid."""


class BridgeAPIError(WatchdogError):
    """Raised for non-transient bridge API errors."""


class TransientHTTPError(WatchdogError):
    """Raised for retryable network or 5xx bridge errors."""


class WatchdogTimeoutError(WatchdogError):
    """Raised when a task does not reach a terminal state before the deadline."""


@dataclass(frozen=True)
class WatchdogConfig:
    base_url: str
    api_key: str
    poll_interval: float = DEFAULT_POLL_INTERVAL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    max_transient_errors: int = DEFAULT_MAX_TRANSIENT_ERRORS


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal dotenv file without requiring python-dotenv."""
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _get_setting(name: str, file_values: dict[str, str], default: str | None = None) -> str | None:
    if name in os.environ:
        return os.environ[name]
    if name in file_values:
        return file_values[name]
    return default


def _parse_float(name: str, value: str | None, default: float, *, minimum: float) -> float:
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if parsed < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return parsed


def _parse_int(name: str, value: str | None, default: int, *, minimum: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return parsed


def build_base_url(host: str, port: str | None) -> str:
    host = host.strip().rstrip("/")
    if not host:
        raise ConfigError("AI_BRIDGE_MCP_HOST is required")
    if "://" not in host:
        host = f"http://{host}"
    if port:
        port = port.strip()
        if not port.isdigit():
            raise ConfigError("AI_BRIDGE_PORT must be numeric when provided")
        host = f"{host}:{port}"
    return host


def load_config(env_path: Path | str = ".env") -> WatchdogConfig:
    file_values = parse_env_file(Path(env_path))
    host = _get_setting("AI_BRIDGE_MCP_HOST", file_values)
    port = _get_setting("AI_BRIDGE_PORT", file_values)
    api_key = _get_setting("AI_BRIDGE_READ_KEY", file_values, "")

    if not host:
        raise ConfigError("AI_BRIDGE_MCP_HOST is required via environment or .env")

    return WatchdogConfig(
        base_url=build_base_url(host, port),
        api_key=api_key or "",
        poll_interval=_parse_float(
            "AI_BRIDGE_POLL_INTERVAL",
            _get_setting("AI_BRIDGE_POLL_INTERVAL", file_values),
            DEFAULT_POLL_INTERVAL,
            minimum=0.1,
        ),
        timeout_seconds=_parse_float(
            "AI_BRIDGE_TIMEOUT_SECONDS",
            _get_setting("AI_BRIDGE_TIMEOUT_SECONDS", file_values),
            DEFAULT_TIMEOUT_SECONDS,
            minimum=0.1,
        ),
        request_timeout=_parse_float(
            "AI_BRIDGE_REQUEST_TIMEOUT",
            _get_setting("AI_BRIDGE_REQUEST_TIMEOUT", file_values),
            DEFAULT_REQUEST_TIMEOUT,
            minimum=0.1,
        ),
        max_transient_errors=_parse_int(
            "AI_BRIDGE_MAX_TRANSIENT_ERRORS",
            _get_setting("AI_BRIDGE_MAX_TRANSIENT_ERRORS", file_values),
            DEFAULT_MAX_TRANSIENT_ERRORS,
            minimum=1,
        ),
    )


def check_task(config: WatchdogConfig, task_id: str) -> dict[str, Any]:
    payload = json.dumps({"task_id": task_id}).encode("utf-8")
    request = urllib.request.Request(
        f"{config.base_url}/worker_check",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": config.api_key,
            "Authorization": f"Bearer {config.api_key}",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=config.request_timeout) as response:
            raw_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if 500 <= exc.code <= 599:
            raise TransientHTTPError(f"bridge returned HTTP {exc.code}: {body}") from exc
        raise BridgeAPIError(f"bridge returned HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientHTTPError(str(exc)) from exc

    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise BridgeAPIError("bridge returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise BridgeAPIError("bridge returned a non-object JSON response")
    if parsed.get("ok") is False:
        raise BridgeAPIError(str(parsed.get("error") or parsed.get("message") or "bridge reported failure"))
    return parsed


def wait_for_task(task_id: str, config: WatchdogConfig) -> dict[str, Any]:
    deadline = time.monotonic() + config.timeout_seconds
    transient_errors = 0
    last_state = "unknown"

    while True:
        if time.monotonic() >= deadline:
            raise WatchdogTimeoutError(
                f"task {task_id} did not reach a terminal state within {config.timeout_seconds:g}s; "
                f"last state: {last_state}"
            )

        try:
            result = check_task(config, task_id)
            transient_errors = 0
        except TransientHTTPError as exc:
            transient_errors += 1
            if transient_errors > config.max_transient_errors:
                raise TransientHTTPError(
                    f"task {task_id} exceeded {config.max_transient_errors} consecutive transient errors: {exc}"
                ) from exc
            sleep_until_next_poll(config, deadline)
            continue

        state = str(result.get("state") or "")
        last_state = state or "missing"
        if state in TERMINAL_STATES:
            return result
        if not state:
            raise BridgeAPIError("bridge response is missing task state")
        sleep_until_next_poll(config, deadline)


def sleep_until_next_poll(config: WatchdogConfig, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(min(config.poll_interval, remaining))


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Block until an AI Worker Bridge async task reaches a terminal state."
    )
    parser.add_argument("task_id", help="Bridge taskId/task_id returned from worker_call mode=async")
    parser.add_argument("--env-file", default=".env", help="Path to dotenv file (default: .env)")
    parser.add_argument("--interval", type=float, help="Poll interval in seconds")
    parser.add_argument("--timeout", type=float, help="Global task wait timeout in seconds")
    parser.add_argument("--request-timeout", type=float, help="Per-request HTTP timeout in seconds")
    parser.add_argument("--max-transient-errors", type=int, help="Consecutive transient errors before failing")
    return parser.parse_args(list(argv) if argv is not None else None)


def config_with_overrides(config: WatchdogConfig, args: argparse.Namespace) -> WatchdogConfig:
    return WatchdogConfig(
        base_url=config.base_url,
        api_key=config.api_key,
        poll_interval=args.interval if args.interval is not None else config.poll_interval,
        timeout_seconds=args.timeout if args.timeout is not None else config.timeout_seconds,
        request_timeout=args.request_timeout if args.request_timeout is not None else config.request_timeout,
        max_transient_errors=(
            args.max_transient_errors
            if args.max_transient_errors is not None
            else config.max_transient_errors
        ),
    )


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = config_with_overrides(load_config(args.env_file), args)
        result = wait_for_task(args.task_id, config)
    except WatchdogError as exc:
        print(f"watchdog error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
