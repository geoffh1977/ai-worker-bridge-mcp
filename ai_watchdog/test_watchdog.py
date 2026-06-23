from __future__ import annotations

import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import watchdog


class WatchdogTests(unittest.TestCase):
    def test_load_config_reads_local_env_without_overriding_process_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "AI_BRIDGE_MCP_HOST=http://from-file\n"
                "AI_BRIDGE_PORT=9999\n"
                "AI_BRIDGE_READ_KEY=file-secret\n"
                "AI_BRIDGE_POLL_INTERVAL=0.25\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"AI_BRIDGE_READ_KEY": "process-secret"}, clear=True):
                cfg = watchdog.load_config(env_path=env_path)

        self.assertEqual(cfg.base_url, "http://from-file:9999")
        self.assertEqual(cfg.api_key, "process-secret")
        self.assertEqual(cfg.poll_interval, 0.25)

    def test_load_config_allows_missing_read_key_for_server_side_401(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "AI_BRIDGE_MCP_HOST=http://from-file\n"
                "AI_BRIDGE_PORT=9999\n"
                "AI_BRIDGE_READ_KEY=\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                cfg = watchdog.load_config(env_path=env_path)

        self.assertEqual(cfg.base_url, "http://from-file:9999")
        self.assertEqual(cfg.api_key, "")

    def test_load_config_allows_omitted_read_key_for_server_side_401(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "AI_BRIDGE_MCP_HOST=http://from-file\n"
                "AI_BRIDGE_PORT=9999\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                cfg = watchdog.load_config(env_path=env_path)

        self.assertEqual(cfg.base_url, "http://from-file:9999")
        self.assertEqual(cfg.api_key, "")

    def test_check_task_sends_empty_auth_headers_when_read_key_is_missing(self) -> None:
        captured = {}

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok": true, "taskId": "task-123", "state": "Completed"}'

        def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
            captured["x_api_key"] = request.get_header("X-api-key")
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return FakeResponse()

        cfg = watchdog.WatchdogConfig(
            base_url="http://bridge.local:8080",
            api_key="",
            poll_interval=0.01,
            timeout_seconds=1.0,
            request_timeout=0.1,
            max_transient_errors=3,
        )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = watchdog.check_task(cfg, "task-123")

        self.assertEqual(result["state"], "Completed")
        self.assertEqual(captured["x_api_key"], "")
        self.assertEqual(captured["authorization"], "Bearer ")
        self.assertEqual(captured["timeout"], 0.1)

    def test_wait_for_task_returns_terminal_response_after_transient_error(self) -> None:
        calls = []

        def fake_check(config: watchdog.WatchdogConfig, task_id: str) -> dict[str, object]:
            calls.append(task_id)
            if len(calls) == 1:
                raise watchdog.TransientHTTPError("connection reset")
            if len(calls) == 2:
                return {"ok": True, "taskId": task_id, "state": "Running", "result": None}
            return {"ok": True, "taskId": task_id, "state": "Completed", "result": {"content": "done"}}

        cfg = watchdog.WatchdogConfig(
            base_url="http://bridge.local:8080",
            api_key="secret",
            poll_interval=0.01,
            timeout_seconds=1.0,
            request_timeout=0.1,
            max_transient_errors=3,
        )

        with patch("watchdog.check_task", side_effect=fake_check), patch("time.sleep", return_value=None):
            result = watchdog.wait_for_task("task-123", cfg)

        self.assertEqual(result["state"], "Completed")
        self.assertEqual(len(calls), 3)

    def test_wait_for_task_times_out_instead_of_looping_forever(self) -> None:
        cfg = watchdog.WatchdogConfig(
            base_url="http://bridge.local:8080",
            api_key="secret",
            poll_interval=0.01,
            timeout_seconds=0.01,
            request_timeout=0.1,
            max_transient_errors=3,
        )

        with patch("watchdog.check_task", return_value={"ok": True, "taskId": "task-123", "state": "Running"}):
            with self.assertRaises(watchdog.WatchdogTimeoutError):
                watchdog.wait_for_task("task-123", cfg)

    def test_parse_args_accepts_taskid_alias_and_prints_json(self) -> None:
        parsed = watchdog.parse_args(["task-123", "--timeout", "5", "--interval", "0.2"])
        self.assertEqual(parsed.task_id, "task-123")
        self.assertEqual(parsed.timeout, 5)
        self.assertEqual(parsed.interval, 0.2)


if __name__ == "__main__":
    unittest.main()
