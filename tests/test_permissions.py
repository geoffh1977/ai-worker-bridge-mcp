from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_bridge.config import WorkerConfig
from ai_bridge.exceptions import InvalidWorkingDirectory, MissingWorkingDirectory
from ai_bridge.permissions import (
    extract_working_directory,
    path_is_allowed,
    resolve_working_directory,
)
from ai_bridge.server import create_app


def worker_config(*, read=None, write=None) -> WorkerConfig:
    payload: dict[str, Any] = {
        "worker_id": "sora",
        "display_name": "Sora Worker",
        "endpoint_url": "http://worker.local/v1/chat/completions",
        "auth_type": "none",
        "model_name": "test-model",
    }
    if read is not None or write is not None:
        payload["filesystem"] = {"read": read or [], "write": write or []}
    return WorkerConfig.model_validate(payload)


def test_config_parses_filesystem_permissions():
    worker = worker_config(read=["/workspace", "/shared"], write=["/workspace"])

    assert worker.filesystem.read == ["/workspace", "/shared"]
    assert worker.filesystem.write == ["/workspace"]


def test_missing_filesystem_defaults_to_full_access_for_backward_compatibility():
    worker = worker_config()

    assert worker.filesystem.read == ["/"]
    assert worker.filesystem.write == ["/"]


def test_extracts_working_directory_from_yaml_frontmatter():
    prompt = "---\nworking_directory: /shared\npriority: high\n---\nDo the task"

    assert extract_working_directory(prompt) == "/shared"


def test_missing_working_directory_is_rejected_without_path_fallback():
    worker = worker_config(read=["/workspace", "/shared"], write=["/shared", "/workspace"])

    with pytest.raises(MissingWorkingDirectory) as excinfo:
        resolve_working_directory(worker, "Do the task")

    assert excinfo.value.status_code == 400
    assert str(excinfo.value) == "working_directory is required in YAML frontmatter"


def test_invalid_frontmatter_working_directory_fails_before_dispatch():
    worker = worker_config(read=["/workspace", "/shared"], write=["/workspace", "/shared"])

    with pytest.raises(InvalidWorkingDirectory) as excinfo:
        resolve_working_directory(
            worker,
            "---\nworking_directory: /directory\n---\nDo the task",
        )

    assert excinfo.value.status_code == 400
    assert str(excinfo.value) == (
        "Specified working_directory '/directory' not in allowed paths: /workspace, /shared"
    )


@pytest.mark.parametrize(
    ("candidate", "allowed"),
    [
        ("/workspace/file.txt", ["/workspace"]),
        ("/workspace/builds/output.log", ["/workspace"]),
        ("/workspace", ["/workspace"]),
    ],
)
def test_exact_and_subpath_matches_are_allowed(candidate, allowed):
    assert path_is_allowed(candidate, allowed) is True


def test_wildcard_match_allows_nested_temp_path():
    assert path_is_allowed("/workspace/temp/file.txt", ["/workspace/*/temp"]) is True


def test_traversal_is_denied_even_under_allowed_prefix():
    assert path_is_allowed("/workspace/../../../etc/passwd", ["/workspace"]) is False


def test_worker_list_response_includes_filesystem_permissions(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    state_path = tmp_path / "tasks.sqlite3"
    config_path.write_text(
        f"""
server:
  require_api_key: false
state:
  sqlite_path: {state_path}
workers:
  - worker_id: bob
    display_name: Bob Worker
    endpoint_url: http://bob:8642
    auth_type: none
    model_name: local-worker
    filesystem:
      read: ["/workspace", "/shared"]
      write: ["/workspace"]
""".strip(),
        encoding="utf-8",
    )
    app = create_app(str(config_path))

    async def fake_probe(worker_config):
        return True

    with TestClient(app) as client:
        monkeypatch.setattr(app.state.workers, "_probe_worker", fake_probe)
        response = client.get("/worker_list")

    assert response.status_code == 200
    assert response.json()["workers"][0]["filesystem"] == {
        "read": ["/workspace", "/shared"],
        "write": ["/workspace"],
    }


def test_worker_call_only_validates_working_directory_not_paths_in_prompt(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    state_path = tmp_path / "tasks.sqlite3"
    config_path.write_text(
        f"""
server:
  require_api_key: false
state:
  sqlite_path: {state_path}
workers:
  - worker_id: bob
    display_name: Bob Worker
    endpoint_url: http://worker.local:8642
    auth_type: none
    model_name: local-worker
    filesystem:
      read: ["/workspace", "/shared"]
      write: ["/workspace", "/shared"]
""".strip(),
        encoding="utf-8",
    )
    app = create_app(str(config_path))
    dispatched: dict[str, Any] = {}

    async def fake_call(worker_id, prompt, timeout_seconds, *, working_directory=None):
        dispatched["working_directory"] = working_directory
        return {"content": "ok", "raw": {}}

    with TestClient(app) as client:
        monkeypatch.setattr(app.state.workers, "call", fake_call)
        response = client.post(
            "/worker_call",
            json={
                "worker_id": "bob",
                "prompt": "---\nworking_directory: /shared\n---\nAnalyze and write /code/example.py",
                "mode": "sync",
            },
        )

    assert response.status_code == 200
    assert dispatched["working_directory"] == "/shared"


def test_http_worker_call_returns_400_for_invalid_working_directory(tmp_path):
    config_path = tmp_path / "config.yaml"
    state_path = tmp_path / "tasks.sqlite3"
    config_path.write_text(
        f"""
server:
  require_api_key: false
state:
  sqlite_path: {state_path}
workers:
  - worker_id: bob
    display_name: Bob Worker
    endpoint_url: http://worker.local:8642
    auth_type: none
    model_name: local-worker
    filesystem:
      read: ["/workspace", "/shared"]
      write: ["/workspace", "/shared"]
""".strip(),
        encoding="utf-8",
    )
    app = create_app(str(config_path))

    with TestClient(app) as client:
        response = client.post(
            "/worker_call",
            json={
                "worker_id": "bob",
                "prompt": "---\nworking_directory: /directory\n---\nDo the task",
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Specified working_directory '/directory' not in allowed paths: /workspace, /shared"
    )


def test_http_worker_call_returns_400_when_working_directory_is_missing(tmp_path):
    config_path = tmp_path / "config.yaml"
    state_path = tmp_path / "tasks.sqlite3"
    config_path.write_text(
        f"""
server:
  require_api_key: false
state:
  sqlite_path: {state_path}
workers:
  - worker_id: bob
    display_name: Bob Worker
    endpoint_url: http://worker.local:8642
    auth_type: none
    model_name: local-worker
    filesystem:
      read: ["/workspace", "/shared"]
      write: ["/workspace", "/shared"]
""".strip(),
        encoding="utf-8",
    )
    app = create_app(str(config_path))

    with TestClient(app) as client:
        response = client.post(
            "/worker_call",
            json={"worker_id": "bob", "prompt": "Do the task"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "working_directory is required in YAML frontmatter"

