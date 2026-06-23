from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ai_bridge.config import AppConfig
from ai_bridge.metrics import MetricsRegistry
from ai_bridge.server import create_app


def _config(tmp_path: Path) -> tuple[Path, Path]:
    config_path = tmp_path / "config.yaml"
    audit_path = tmp_path / "audit.jsonl"
    state_path = tmp_path / "tasks.sqlite3"
    config_path.write_text(
        f"""
auth:
  scoped_keys:
    - key_id: test
      env: TEST_BRIDGE_KEY
      scopes: [read, submit, cancel, admin]
state:
  sqlite_path: {state_path}
audit:
  file_path: {audit_path}
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
    return config_path, audit_path


def test_live_ready_and_metrics_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_BRIDGE_KEY", "test-secret")
    config_path, _audit_path = _config(tmp_path)
    app = create_app(str(config_path))

    with TestClient(app) as client:
        live = client.get("/live")
        ready = client.get("/ready")
        metrics = client.get("/metrics", headers={"X-API-Key": "test-secret"})

    assert live.status_code == 200
    assert live.json()["status"] == "live"
    assert ready.status_code == 200
    assert ready.json()["checks"]["sqlite_writable"] is True
    assert metrics.status_code == 200
    assert "ai_bridge_active_tasks" in metrics.text


def test_audit_log_records_denied_working_directory_without_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_BRIDGE_KEY", "test-secret")
    config_path, audit_path = _config(tmp_path)
    app = create_app(str(config_path))

    with TestClient(app) as client:
        response = client.post(
            "/worker_call",
            headers={"X-API-Key": "test-secret"},
            json={"worker_id": "bob", "prompt": "---\nworking_directory: /etc\n---\nsecret prompt text"},
        )

    assert response.status_code == 400
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "working_directory_denied" in audit_text
    assert "secret prompt text" not in audit_text


def test_worker_call_seconds_renders_histogram_buckets():
    registry = MetricsRegistry(worker_call_seconds_buckets=(0.1, 0.5, 1.0))

    registry.observe_worker_call("bob", 0.05)
    registry.observe_worker_call("bob", 0.7)

    rendered = registry.render()

    assert "# TYPE ai_bridge_worker_call_seconds histogram" in rendered
    assert 'ai_bridge_worker_call_seconds_bucket{worker_id="bob",le="0.1"} 1' in rendered
    assert 'ai_bridge_worker_call_seconds_bucket{worker_id="bob",le="0.5"} 1' in rendered
    assert 'ai_bridge_worker_call_seconds_bucket{worker_id="bob",le="1.0"} 2' in rendered
    assert 'ai_bridge_worker_call_seconds_bucket{worker_id="bob",le="+Inf"} 2' in rendered
    assert 'ai_bridge_worker_call_seconds_count{worker_id="bob"} 2' in rendered
    assert 'ai_bridge_worker_call_seconds_sum{worker_id="bob"} 0.750000' in rendered


def test_metrics_config_parses_worker_call_seconds_buckets():
    config = AppConfig.model_validate(
        {
            "auth": {
                "scoped_keys": [
                    {
                        "key_id": "test",
                        "env": "TEST_BRIDGE_KEY",
                        "scopes": ["read", "submit"],
                    }
                ]
            },
            "metrics": {"worker_call_seconds_buckets": [0.25, 1.0, 5.0]},
            "workers": [
                {
                    "worker_id": "bob",
                    "display_name": "Bob Worker",
                    "endpoint_url": "http://worker.local:8642",
                    "auth_type": "none",
                    "model_name": "local-worker",
                }
            ],
        }
    )

    assert config.metrics.worker_call_seconds_buckets == [0.25, 1.0, 5.0]
