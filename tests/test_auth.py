from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_bridge.config import load_config
from ai_bridge.server import create_app


def _config(tmp_path):
    config_path = tmp_path / "config.yaml"
    state_path = tmp_path / "tasks.sqlite3"
    audit_path = tmp_path / "audit.jsonl"
    config_path.write_text(
        f"""
server:
  host: 0.0.0.0
  port: 8080
auth:
  scoped_keys:
    - key_id: reader
      env: READ_KEY
      scopes: [read]
    - key_id: submitter
      env: SUBMIT_KEY
      scopes: [submit]
    - key_id: admin
      env: ADMIN_KEY
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
    return config_path


def test_scoped_api_keys_enforce_endpoint_scopes(tmp_path, monkeypatch):
    monkeypatch.setenv("READ_KEY", "read-secret")
    monkeypatch.setenv("SUBMIT_KEY", "submit-secret")
    monkeypatch.setenv("ADMIN_KEY", "admin-secret")
    app = create_app(str(_config(tmp_path)))

    with TestClient(app) as client:
        assert client.get("/worker_list", headers={"X-API-Key": "read-secret"}).status_code == 200
        assert client.post("/worker_call", headers={"X-API-Key": "read-secret"}, json={"worker_id": "bob", "prompt": "---\nworking_directory: /shared\n---\nhi"}).status_code == 403
        assert client.post("/reload", headers={"Authorization": "Bearer submit-secret"}).status_code == 403


def test_mcp_reload_requires_admin_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("READ_KEY", "read-secret")
    monkeypatch.setenv("SUBMIT_KEY", "submit-secret")
    monkeypatch.setenv("ADMIN_KEY", "admin-secret")
    app = create_app(str(_config(tmp_path)))

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={"X-API-Key": "read-secret"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "worker_config_reload", "arguments": {}}},
        )

    assert response.status_code == 200
    assert "missing required scope" in response.json()["error"]["message"]


def test_general_api_key_env_is_not_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("READ_KEY", "read-secret")
    monkeypatch.setenv("SUBMIT_KEY", "submit-secret")
    monkeypatch.setenv("ADMIN_KEY", "admin-secret")
    monkeypatch.setenv("AI_BRIDGE_API_KEY", "legacy-secret")
    app = create_app(str(_config(tmp_path)))

    with TestClient(app) as client:
        response = client.get("/worker_list", headers={"X-API-Key": "legacy-secret"})

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid API key"


def test_scoped_keys_are_required(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
auth:
  scoped_keys: []
workers: []
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as excinfo:
        load_config(config_path)

    assert "auth.scoped_keys must define at least one scoped key" in str(excinfo.value)


def test_legacy_general_api_key_config_is_rejected(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
server:
  api_key_env: AI_BRIDGE_API_KEY
  require_api_key: true
auth:
  scoped_keys:
    - key_id: reader
      env: READ_KEY
      scopes: [read]
workers: []
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as excinfo:
        load_config(config_path)

    assert "Extra inputs are not permitted" in str(excinfo.value)
