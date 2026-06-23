from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import Process
from pathlib import Path
from typing import Any

import httpx


READ_KEY = "test-read-key"
SUBMIT_KEY = "test-submit-key"
IDEMPOTENCY_KEY = "e2e-idempotency-key"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_mock_worker(port: int, calls_path: str) -> None:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib hook
            if self.path == "/health":
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802 - stdlib hook
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length)
            body = json.loads(raw_body.decode("utf-8"))
            record = {
                "path": self.path,
                "headers": {key: value for key, value in self.headers.items()},
                "body": body,
            }
            with open(calls_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
            time.sleep(0.2)
            response = json.dumps(
                {
                    "id": "chatcmpl-e2e-idempotency",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "idempotent-ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "model": body.get("model", "mock-worker"),
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.serve_forever()


def _write_config(path: Path, bridge_port: int, worker_port: int, sqlite_path: Path, log_path: Path) -> None:
    path.write_text(
        f"""
server:
  host: 127.0.0.1
  port: {bridge_port}
auth:
  scoped_keys:
    - key_id: read
      env: E2E_BRIDGE_READ_KEY
      scopes: [read]
    - key_id: submit
      env: E2E_BRIDGE_SUBMIT_KEY
      scopes: [read, submit]
state:
  sqlite_path: {sqlite_path}
logging:
  level: INFO
  file_path: {log_path}
audit:
  enabled: false
workers:
  - worker_id: bob
    display_name: Bob E2E Worker
    endpoint_url: http://127.0.0.1:{worker_port}/v1/chat/completions
    auth_type: none
    model_name: mock-worker
    filesystem:
      read: ["/shared"]
      write: ["/shared"]
    allowed_modes: [async]
    timeout_limits:
      sync_seconds: 1
      async_seconds: 5
    max_concurrent_tasks: 2
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _wait_for_live(base_url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"bridge exited early with rc={process.returncode}")
        try:
            if httpx.get(f"{base_url}/live", timeout=0.5).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise AssertionError("bridge did not become live")


def _post_json(base_url: str, path: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = httpx.post(f"{base_url}{path}", headers={"X-API-Key": key}, json=payload, timeout=5)
    assert response.status_code == 200, response.text
    return response.json()


def test_http_worker_call_idempotency_is_durable_and_propagated(tmp_path: Path) -> None:
    worker_port = _free_port()
    bridge_port = _free_port()
    calls_path = tmp_path / "worker-calls.jsonl"
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, bridge_port, worker_port, tmp_path / "tasks.sqlite3", tmp_path / "bridge.log")

    worker = Process(target=_run_mock_worker, args=(worker_port, str(calls_path)), daemon=True)
    worker.start()
    env = os.environ.copy()
    env.update(
        {
            "AI_BRIDGE_CONFIG": str(config_path),
            "E2E_BRIDGE_READ_KEY": READ_KEY,
            "E2E_BRIDGE_SUBMIT_KEY": SUBMIT_KEY,
        }
    )
    bridge = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ai_bridge.server:create_app", "--factory", "--host", "127.0.0.1", "--port", str(bridge_port)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        base_url = f"http://127.0.0.1:{bridge_port}"
        _wait_for_live(base_url, bridge)
        payload = {
            "worker_id": "bob",
            "prompt": "---\nworking_directory: /shared\n---\nrun idempotently",
            "mode": "async",
            "idempotency_key": IDEMPOTENCY_KEY,
        }

        first = _post_json(base_url, "/worker_call", SUBMIT_KEY, payload)
        second = _post_json(base_url, "/worker_call", SUBMIT_KEY, payload)

        assert first["ok"] is True
        assert second["ok"] is True
        assert second["task_id"] == first["task_id"]
        assert second["idempotency_key"] == IDEMPOTENCY_KEY

        task_id = first["task_id"]
        checked: dict[str, Any] = {}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            checked = _post_json(base_url, "/worker_check", READ_KEY, {"task_id": task_id})
            if checked.get("state") == "Completed":
                break
            time.sleep(0.1)
        assert checked["state"] == "Completed"
        assert checked["result"]["content"] == "idempotent-ok"
        assert checked["attempt_count"] == 1
        assert checked["dispatch_attempt_id"]

        records = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
        assert len(records) == 1
        headers = records[0]["headers"]
        metadata = records[0]["body"]["metadata"]
        assert headers["X-Bridge-Task-Id"] == task_id
        assert headers["X-Bridge-Idempotency-Key"] == IDEMPOTENCY_KEY
        assert headers["X-Bridge-Dispatch-Attempt-Id"] == checked["dispatch_attempt_id"]
        assert headers["X-Bridge-Attempt-Number"] == "1"
        assert headers["X-Bridge-Recovery-Attempt"] == "false"
        assert metadata == {
            "bridge_task_id": task_id,
            "bridge_idempotency_key": IDEMPOTENCY_KEY,
            "bridge_dispatch_attempt_id": checked["dispatch_attempt_id"],
            "bridge_attempt_number": 1,
            "bridge_recovery_attempt": False,
        }
    finally:
        bridge.terminate()
        try:
            bridge.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bridge.kill()
            bridge.wait(timeout=5)
        worker.terminate()
        worker.join(timeout=5)
