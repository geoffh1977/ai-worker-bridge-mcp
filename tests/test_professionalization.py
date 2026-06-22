from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_bridge.config import CircuitBreakerConfig, WorkerConfig
from ai_bridge.server import create_app
from ai_bridge.workers import WorkerRegistry


@pytest.fixture(autouse=True)
def _bridge_key(monkeypatch):
    monkeypatch.setenv("TEST_BRIDGE_KEY", "test-secret")


def test_worker_endpoint_url_base_is_normalized_to_openai_chat_completions():
    config = WorkerConfig.model_validate(
        {
            "worker_id": "bob",
            "display_name": "Bob",
            "endpoint_url": "http://bob:8642",
            "auth_type": "none",
            "model_name": "test-model",
        }
    )

    assert str(config.endpoint_url) == "http://bob:8642/v1/chat/completions"


def test_worker_endpoint_url_v1_is_normalized_to_openai_chat_completions():
    config = WorkerConfig.model_validate(
        {
            "worker_id": "bob",
            "display_name": "Bob",
            "endpoint_url": "http://bob:8642/v1",
            "auth_type": "none",
            "model_name": "test-model",
        }
    )

    assert str(config.endpoint_url) == "http://bob:8642/v1/chat/completions"


def test_worker_endpoint_url_full_chat_completions_is_not_modified():
    config = WorkerConfig.model_validate(
        {
            "worker_id": "bob",
            "display_name": "Bob",
            "endpoint_url": "http://bob:8642/custom/v1/chat/completions?profile=fast",
            "auth_type": "none",
            "model_name": "test-model",
        }
    )

    assert str(config.endpoint_url) == "http://bob:8642/custom/v1/chat/completions?profile=fast"


def test_worker_config_defaults_capabilities_and_description_for_backward_compatibility():
    config = WorkerConfig.model_validate(
        {
            "worker_id": "bob",
            "display_name": "Bob",
            "endpoint_url": "http://bob:8642",
            "auth_type": "none",
            "model_name": "test-model",
        }
    )

    assert config.capabilities == []
    assert config.description == ""


@pytest.mark.asyncio
async def test_worker_list_includes_public_capabilities_and_description(monkeypatch):
    capabilities = [
        "heavy software engineering",
        "secure coding practices",
        "automated script generation",
        "refactoring messy technical files",
        "system infrastructure design",
    ]
    worker = WorkerConfig.model_validate(
        {
            "worker_id": "bob",
            "display_name": "Bob",
            "endpoint_url": "http://worker.local/v1/chat/completions",
            "auth_type": "none",
            "model_name": "test-model",
            "capabilities": capabilities,
            "description": "Systems Architect / Lead Software Engineer / DevOps Automation",
        }
    )
    registry = WorkerRegistry([worker], CircuitBreakerConfig())

    async def fake_probe(worker_config):
        return True

    monkeypatch.setattr(registry, "_probe_worker", fake_probe)

    workers = await registry.list_public()

    assert workers[0]["capabilities"] == capabilities
    assert workers[0]["description"] == "Systems Architect / Lead Software Engineer / DevOps Automation"
    assert workers[0]["worker_id"] == "bob"
    assert workers[0]["display_name"] == "Bob"
    assert workers[0]["model_name"] == "test-model"
    assert workers[0]["allowed_modes"] == ["sync", "async"]
    assert workers[0]["timeout_limits"] == {"sync_seconds": 30, "async_seconds": 300}
    assert workers[0]["max_concurrent_tasks"] == 2
    assert workers[0]["status"] == "up"
    assert "health_checked_at" in workers[0]
    assert "health_error" in workers[0]


def test_mcp_worker_list_response_includes_public_worker_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_BRIDGE_KEY", "test-secret")
    capabilities = [
        "heavy software engineering",
        "secure coding practices",
        "automated script generation",
        "refactoring messy technical files",
        "system infrastructure design",
    ]
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
workers:
  - worker_id: bob
    display_name: Bob Worker
    endpoint_url: http://bob:8642
    auth_type: none
    model_name: local-worker
    capabilities:
      - heavy software engineering
      - secure coding practices
      - automated script generation
      - refactoring messy technical files
      - system infrastructure design
    description: Systems Architect / Lead Software Engineer / DevOps Automation
""".strip(),
        encoding="utf-8",
    )
    app = create_app(str(config_path))

    async def fake_probe(worker_config):
        return True

    with TestClient(app) as client:
        monkeypatch.setattr(app.state.workers, "_probe_worker", fake_probe)
        response = client.post(
            "/mcp",
            headers={"X-API-Key": "test-secret"},
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "worker_list", "arguments": {}},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 7
    result = payload["result"]
    assert result["isError"] is False
    content = result["content"][0]
    assert content["type"] == "text"
    worker_list = json.loads(content["text"])
    assert worker_list["ok"] is True
    assert worker_list["workers"][0]["capabilities"] == capabilities
    assert worker_list["workers"][0]["description"] == "Systems Architect / Lead Software Engineer / DevOps Automation"


@pytest.mark.asyncio
async def test_worker_list_reports_cached_probe_health(monkeypatch):
    worker = WorkerConfig.model_validate(
        {
            "worker_id": "bob",
            "display_name": "Bob",
            "endpoint_url": "http://worker.local/v1/chat/completions",
            "auth_type": "none",
            "model_name": "test-model",
        }
    )
    registry = WorkerRegistry([worker], CircuitBreakerConfig())
    probes = 0

    async def fake_probe(worker_config):
        nonlocal probes
        probes += 1
        return True

    monkeypatch.setattr(registry, "_probe_worker", fake_probe)

    first = await registry.list_public()
    second = await registry.list_public()

    assert first[0]["status"] == "up"
    assert second[0]["status"] == "up"
    assert probes == 1


@pytest.mark.asyncio
async def test_worker_list_reports_down_when_probe_fails(monkeypatch):
    worker = WorkerConfig.model_validate(
        {
            "worker_id": "bob",
            "display_name": "Bob",
            "endpoint_url": "http://worker.local/v1/chat/completions",
            "auth_type": "none",
            "model_name": "test-model",
        }
    )
    registry = WorkerRegistry([worker], CircuitBreakerConfig())

    async def fake_probe(worker_config):
        return False

    monkeypatch.setattr(registry, "_probe_worker", fake_probe)

    workers = await registry.list_public()

    assert workers[0]["status"] == "down"


@pytest.mark.asyncio
async def test_worker_health_probe_uses_openai_models_endpoint_instead_of_head_completions(monkeypatch):
    worker = WorkerConfig.model_validate(
        {
            "worker_id": "bob",
            "display_name": "Bob",
            "endpoint_url": "http://worker.local/v1/chat/completions",
            "auth_type": "none",
            "model_name": "test-model",
        }
    )
    registry = WorkerRegistry([worker], CircuitBreakerConfig())
    requests: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers):
            requests.append(("GET", url))
            return httpx.Response(200)

        async def head(self, url, headers):
            requests.append(("HEAD", url))
            return httpx.Response(405)

    monkeypatch.setattr("ai_bridge.workers.httpx.AsyncClient", FakeClient)

    assert await registry._probe_worker(worker) is True
    assert requests == [("GET", "http://worker.local/v1/models")]


@pytest.mark.asyncio
async def test_worker_health_probe_falls_back_to_health_endpoint(monkeypatch):
    worker = WorkerConfig.model_validate(
        {
            "worker_id": "bob",
            "display_name": "Bob",
            "endpoint_url": "http://worker.local/v1/chat/completions",
            "auth_type": "none",
            "model_name": "test-model",
        }
    )
    registry = WorkerRegistry([worker], CircuitBreakerConfig())
    requests: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers):
            requests.append(("GET", url))
            if url.endswith("/health"):
                return httpx.Response(200)
            return httpx.Response(404)

        async def head(self, url, headers):
            requests.append(("HEAD", url))
            return httpx.Response(405)

    monkeypatch.setattr("ai_bridge.workers.httpx.AsyncClient", FakeClient)

    assert await registry._probe_worker(worker) is True
    assert requests == [
        ("GET", "http://worker.local/v1/models"),
        ("GET", "http://worker.local/health"),
    ]
    assert all(method != "HEAD" for method, _url in requests)


def test_reload_endpoint_swaps_config_without_stopping_existing_manager(tmp_path):
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
workers:
  - worker_id: bob
    display_name: Bob One
    endpoint_url: http://bob:8642
    auth_type: none
    model_name: model-one
""".strip(),
        encoding="utf-8",
    )

    app = create_app(str(config_path))
    with TestClient(app) as client:
        old_manager = app.state.manager
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
workers:
  - worker_id: bob
    display_name: Bob Two
    endpoint_url: http://bob:8642/v1
    auth_type: none
    model_name: model-two
""".strip(),
            encoding="utf-8",
        )

        response = client.post("/reload", headers={"X-API-Key": "test-secret"})

        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert app.state.manager is old_manager
        assert app.state.manager.workers is app.state.workers
        assert app.state.workers.get("bob").display_name == "Bob Two"
        assert str(app.state.workers.get("bob").endpoint_url) == "http://bob:8642/v1/chat/completions"


def test_worker_config_reload_mcp_tool_is_listed_and_reloads_config(tmp_path):
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
workers:
  - worker_id: bob
    display_name: Bob One
    endpoint_url: http://bob:8642
    auth_type: none
    model_name: model-one
""".strip(),
        encoding="utf-8",
    )

    app = create_app(str(config_path))
    with TestClient(app) as client:
        tools_response = client.post(
            "/mcp",
            headers={"X-API-Key": "test-secret"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        tool_names = {tool["name"] for tool in tools_response.json()["result"]["tools"]}
        assert "worker_config_reload" in tool_names

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
workers:
  - worker_id: bob
    display_name: Bob Reloaded
    endpoint_url: http://bob:8642/v1
    auth_type: none
    model_name: model-two
""".strip(),
            encoding="utf-8",
        )

        reload_response = client.post(
            "/mcp",
            headers={"X-API-Key": "test-secret"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "worker_config_reload", "arguments": {}},
            },
        )

        assert reload_response.status_code == 200
        result = reload_response.json()["result"]
        content = result["content"][0]["text"]
        assert result["isError"] is False
        assert '"ok": true' in content
        assert app.state.workers.get("bob").display_name == "Bob Reloaded"
