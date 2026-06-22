from __future__ import annotations

from fastapi.testclient import TestClient

from ai_bridge.server import create_app


def test_async_submission_over_global_pending_limit_returns_503_without_persisting(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_BRIDGE_KEY", "test-secret")
    config_path = tmp_path / "config.yaml"
    state_path = tmp_path / "tasks.sqlite3"
    config_path.write_text(
        f"""
server:
  host: 0.0.0.0
  port: 8080
auth:
  scoped_keys:
    - key_id: test
      env: TEST_BRIDGE_KEY
      scopes: [read, submit, cancel, admin]
state:
  sqlite_path: {state_path}
limits:
  global_pending_tasks: 0
  per_worker_pending_tasks: 10
workers:
  - worker_id: bob
    display_name: Bob Worker
    endpoint_url: http://worker.local:8642
    auth_type: none
    model_name: local-worker
    filesystem:
      read: ["/shared"]
      write: ["/shared"]
""".strip(),
        encoding="utf-8",
    )
    app = create_app(str(config_path))

    with TestClient(app) as client:
        response = client.post(
            "/worker_call",
            headers={"X-API-Key": "test-secret"},
            json={"worker_id": "bob", "mode": "async", "prompt": "---\nworking_directory: /shared\n---\nqueued"},
        )
        metrics = client.get("/metrics", headers={"X-API-Key": "test-secret"})

    assert response.status_code == 503
    assert "global pending task queue is full" in str(response.json()["detail"])
    assert "ai_bridge_queued_tasks 0" in metrics.text
