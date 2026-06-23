# 🌉 AI Worker Bridge MCP Server

## 📖 Overview
The **AI Worker Bridge** is a standalone, containerized MCP server designed to orchestrate communication between an Agent Client (e.g., Hermes/Sora) and OpenAI-compatible worker gateways.

### Key Capabilities:
*   **Credential Abstraction:** Hides worker credentials behind scoped bridge credentials (`read`, `submit`, `cancel`, `admin`) with constant-time comparison.
*   **Unified Interface:** Exposes `worker_*` tools over the Model Context Protocol (MCP) using SSE/HTTP.
*   **Durable Async Transformation:** Transforms synchronous worker endpoints into durable asynchronous tasks backed by SQLite persistence.
*   **Runtime Reloads:** Reloads `config.yaml` through either `POST /reload` or the MCP `worker_config_reload` tool.
*   **Agent-Friendly Discovery:** Lists configured workers with sanitized metadata, cached health probes, and normalized OpenAI endpoint URLs.

---

## 📂 File Map

| File / Directory | Description |
| :--- | :--- |
| `ai_bridge/config.py` | YAML and environment configuration models, including endpoint URL normalization |
| `ai_bridge/task_state.py` | Task state machine and transition guards |
| `ai_bridge/store.py` | SQLite persistence layer for durable task records |
| `ai_bridge/workers.py` | OpenAI-compatible worker client, auth masking, health probes, and circuit breaker |
| `ai_bridge/manager.py` | Sync/async orchestration and restart recovery |
| `ai_bridge/server.py` | FastAPI HTTP/SSE/MCP endpoints and runtime reload logic |
| `Dockerfile` | Production container image specification |
| `docker-compose.yml` | Local runtime configuration with persistent data/log volumes |
| `config.yaml` | Local runtime config; endpoints may use base URLs and are normalized automatically |
| `config.yaml.example` | Deployment template for worker configurations |
| `.env` | Local secrets and scoped bridge keys |
| `.env.example` | Deployment secrets template |
| `tests/` | Unit, integration, persistence, reload, and MCP tests |

---

## ⚙️ Configuration Guide

The bridge loads YAML from `AI_BRIDGE_CONFIG` when set, otherwise from `config.yaml` in the working directory.

### Complete Example
```yaml
server:
  host: 0.0.0.0
  port: 8080

auth:
  scoped_keys:
    - key_id: sora-read
      env: AI_BRIDGE_SORA_READ_KEY
      scopes: [read]
    - key_id: sora-submit
      env: AI_BRIDGE_SORA_SUBMIT_KEY
      scopes: [read, submit, cancel]
    - key_id: sora-admin
      env: AI_BRIDGE_SORA_ADMIN_KEY
      scopes: [read, submit, cancel, admin]

state:
  sqlite_path: /app/data/tasks.sqlite3

logging:
  level: INFO
  file_path: /app/logs/bridge.log

metrics:
  worker_call_seconds_buckets: [0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]

circuit_breaker:
  failure_threshold: 3
  recovery_seconds: 30

workers:
  - worker_id: bob
    display_name: Bob Worker
    endpoint_url: http://bob-worker:8642
    auth_type: bearer
    secret_env: BOB_WORKER_API_KEY
    model_name: local-worker
    default_system_prompt: "You are a concise worker."
    filesystem:
      read: ["/workspace", "/shared"]
      write: ["/workspace"]
    allowed_modes: [sync, async]
    timeout_limits:
      sync_seconds: 15
      async_seconds: 120
    max_concurrent_tasks: 2
```

### `server`
| Parameter | Type | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `host` | string | `0.0.0.0` | Bind address used by the app runner/container. |
| `port` | integer | `8080` | HTTP port used by the app runner/container. |

### `auth`
| Parameter | Type | Required | Purpose |
| :--- | :--- | :--- | :--- |
| `scoped_keys` | list | Yes | Mandatory env-backed bridge credentials. Each key has a `key_id`, `env`, and one or more scopes. General/unscoped API keys are not supported. |
| `scoped_keys[].scopes` | list | Yes | Allowed values: `read`, `submit`, `cancel`, `admin`. Protected endpoints require the matching scope. |

### `state`
| Parameter | Type | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `sqlite_path` | string | `/app/data/tasks.sqlite3` | Durable SQLite database path for async task records. If this changes during reload, active async tasks block the reload to avoid dropping in-flight work. |

### `logging`
| Parameter | Type | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `level` | string | `INFO` | Logging threshold, e.g. `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `file_path` | string | `/app/logs/bridge.log` | Structured log file path. Logs are also emitted to stdout for container platforms. |

### `circuit_breaker`
| Parameter | Type | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `failure_threshold` | integer | `3` | Consecutive worker failures before the worker circuit opens. |
| `recovery_seconds` | number | `30` | Cooldown before the bridge retries a worker with an open circuit. |

### `workers[]`
| Parameter | Type | Required | Purpose |
| :--- | :--- | :--- | :--- |
| `worker_id` | string | Yes | Stable worker identifier used by MCP tools. Allowed characters: letters, numbers, `_`, `.`, `-`. |
| `display_name` | string | Yes | Human-readable worker name returned by `worker_list` and `/status`. |
| `endpoint_url` | URL | Yes | OpenAI-compatible endpoint. Base URLs like `http://bob-worker:8642` and `/v1` URLs are normalized to `/v1/chat/completions`. Full `/chat/completions` URLs are preserved, including query strings. |
| `auth_type` | `none`, `bearer`, `basic` | No | Authentication mode used when calling the worker. Defaults to `bearer`. |
| `secret_env` | string | For bearer auth | Environment variable containing the bearer token. Replaces older `bearer_secret_env` naming. |
| `username_env` | string | For basic auth | Environment variable containing the basic auth username. |
| `password_env` | string | For basic auth | Environment variable containing the basic auth password. |
| `model_name` | string | Yes | Model value sent to the OpenAI-compatible worker. |
| `default_system_prompt` | string | No | Optional system prompt prepended to worker calls. |
| `filesystem.read` | list | No | Declarative read paths exposed with worker metadata for worker/container policy. Missing `filesystem` defaults to deny-all (`[]`) in v1.0. |
| `filesystem.write` | list | No | Paths eligible for `working_directory`; actual write enforcement belongs to the worker/container. Missing or empty write policy rejects every working directory. Supports wildcard path segments such as `/workspace/*/temp`. |
| `filesystem.canonicalize` | boolean | No | When true, the bridge resolves visible paths with `Path.resolve(strict=True)` before comparing against allowed roots, catching symlink escapes and rejecting nonexistent requested working directories before dispatch. Use only when bridge and worker share the same filesystem namespace. |
| `allowed_modes` | list | No | Allowed call modes: `sync`, `async`, or both. Defaults to both. |
| `timeout_limits.sync_seconds` | number | No | Timeout for synchronous worker calls. Defaults to `30`. |
| `timeout_limits.async_seconds` | number | No | Timeout for durable async worker calls. Defaults to `300`. |
| `max_concurrent_tasks` | integer | No | Per-worker async concurrency cap. Defaults to `2`. |

### URL Normalization
Worker endpoints may be written in the shortened form:
```yaml
endpoint_url: http://bob-worker:8642
```
The bridge expands that to:
```text
http://bob-worker:8642/v1/chat/completions
```
These are also valid:
```yaml
endpoint_url: http://bob-worker:8642/v1
endpoint_url: http://bob-worker:8642/custom/v1/chat/completions?profile=fast
```
No need to hand-type the full path unless the worker uses a custom route. Humanity survives one less stringly-typed footgun.

### Filesystem Permissions
Worker configs declare the paths that may be selected as a task's working directory. Actual filesystem enforcement remains the responsibility of the worker runtime and its container permissions.

```yaml
workers:
  - worker_id: bob
    display_name: Bob Worker
    endpoint_url: http://bob-worker:8642
    auth_type: bearer
    secret_env: BOB_WORKER_API_KEY
    model_name: local-worker
    filesystem:
      read: ["/workspace", "/shared"]
      write: ["/workspace"]

  - worker_id: sora
    display_name: Sora Worker
    endpoint_url: http://sora-worker:8642
    auth_type: bearer
    secret_env: SORA_WORKER_API_KEY
    model_name: local-worker
    filesystem:
      read: ["/", "/shared"]
      write: ["/shared"]
```

Rules:
*   Paths must be absolute and start with `/`.
*   An empty `write` array means no working directory can be selected.
*   Missing `filesystem` defaults to deny-all (`read: []`, `write: []`) in v1.0. Use `compat.allow_implicit_root_filesystem: true` only as a temporary migration escape hatch.
*   Working-directory subpaths are allowed: `/workspace` permits `/workspace/builds`.
*   Wildcard path segments are supported for working-directory matching.
*   Traversal is denied before normalization, so `/workspace/../../../etc` is rejected even when `/workspace` is allowed.
*   When `filesystem.canonicalize: true`, both the requested working directory and allowed root must exist in the bridge-visible filesystem. Nonexistent subpaths are rejected before any worker dispatch.

Tasks can select their execution directory with leading YAML frontmatter:

```yaml
---
working_directory: /shared
---
Build the requested artifact.
```

The field is required for every task. The selected directory is validated against the worker's configured write paths using config comparison only by default. With `filesystem.canonicalize: true`, the bridge additionally performs strict canonical resolution and rejects nonexistent bridge-visible paths. The bridge performs no permission-test writes and does not inspect paths mentioned in task text or code snippets. Missing or invalid selections fail before dispatch with HTTP 400, with no fallback directory search. The validated value is included as `working_directory` in the OpenAI-compatible worker request and persisted for async task recovery.

---

## 🔐 Environment Variables

| Variable | Required | Purpose |
| :--- | :--- | :--- |
| `AI_BRIDGE_SORA_READ_KEY` / `AI_BRIDGE_SORA_SUBMIT_KEY` / `AI_BRIDGE_SORA_ADMIN_KEY` | Required when referenced by `auth.scoped_keys` | Scoped bridge keys accepted as `X-API-Key` or bearer token. Names are examples; the config controls the env var names. |
| `AI_BRIDGE_CONFIG` | Optional | Path to the YAML config file. Defaults to `config.yaml`. |
| `AI_BRIDGE_DATA_DIR` | Optional | Directory created at startup for local data. Defaults to `./data`. |
| `AI_BRIDGE_LOG_DIR` | Optional | Directory created at startup for logs. Defaults to `./logs`. |
| `AI_BRIDGE_PORT` | Optional for Docker Compose | Host/container port wiring used by the compose file when configured. |
| `<WORKER>_API_KEY` | Required for `auth_type: bearer` workers | Bearer token referenced by the worker's `secret_env`. Example: `BOB_WORKER_API_KEY`. |
| `<WORKER>_USERNAME` | Required for `auth_type: basic` workers | Basic auth username referenced by `username_env`. |
| `<WORKER>_PASSWORD` | Required for `auth_type: basic` workers | Basic auth password referenced by `password_env`. |

### `.env.example`
```dotenv
AI_BRIDGE_SORA_READ_KEY=change-me-long-random-read-key
AI_BRIDGE_SORA_SUBMIT_KEY=change-me-long-random-submit-key
AI_BRIDGE_SORA_ADMIN_KEY=change-me-long-random-admin-key
AI_BRIDGE_PORT=8080
BOB_WORKER_API_KEY=replace-with-worker-api-key
```

Security note: keep real secrets in environment variables or secret managers. `config.yaml` should reference variable names, not contain credential values. The bridge masks worker auth data in public responses and logs.

---

## ⚙️ Runtime API

### System Endpoints
*   **Liveness:** `GET /live` (process is running, no auth)
*   **Readiness:** `GET /ready` (config loaded and SQLite writable, no auth)
*   **Compatibility Health:** `GET /health` (points callers to `/live` and `/ready`)
*   **Metrics:** `GET /metrics` (Prometheus text, requires `read` scope)
*   **Readiness Status:** `GET /status` (config, state store, queues, sanitized worker list)  
    *Header:* `X-API-Key: <read scoped key>`
*   **Runtime Reload:** `POST /reload` (reloads config from disk; requires `admin`)  
    *Header:* `X-API-Key: <admin scoped key>`

### MCP Interface
*   **Discovery & SSE:** `GET /mcp`  
    *Header:* `X-API-Key: <read scoped key>`
*   **HTTP / JSON-RPC:** `POST /mcp`  
    *Headers:* `X-API-Key: <scoped key>`, `MCP-Protocol-Version: 2025-11-25`
*   **Legacy Messages Endpoint:** `POST /messages`  
    *Headers:* `X-API-Key: <scoped key>`, `MCP-Protocol-Version: 2025-11-25`

### Supported JSON-RPC Methods
*   `initialize`
*   `tools/list`
*   `tools/call`

### Convenience HTTP Endpoints (Smoke Tests)
*   `POST /worker_call`
*   `POST /worker_check`
*   `POST /worker_cancel`
*   `GET /worker_list`
*   `POST /reload`

---

## 🧰 MCP Tool Reference

All MCP tool results are returned as MCP text content containing a JSON object. `isError` is set from the returned `ok` field.

| Tool | Input Schema | Behavior |
| :--- | :--- | :--- |
| `worker_call` | `{"worker_id":"string","prompt":"string","mode":"sync|async","idempotency_key":"string optional"}` | Requires YAML-frontmatter `working_directory`, validates that directory against configured write paths without inspecting task text, then calls the worker. In `sync` mode, returns the worker output directly. In `async` mode, creates a durable task and returns `taskId`. |
| `worker_check` | `{"task_id":"string"}` | Polls an async task and returns current state, result, or error. |
| `worker_list` | `{}` | Lists configured workers with sanitized fields, filesystem permissions, normalized endpoint URLs, circuit state, cached health status, `health_checked_at`, and sanitized `health_error` when present. |
| `worker_cancel` | `{"task_id":"string"}` | Cancels a pending or running async task. Terminal states remain immutable. |
| `worker_config_reload` | `{}` | Reloads `config.yaml` from disk through the same internal logic as `POST /reload`. Requires `admin` scope. Returns `ok: true` with worker and state-store details on success. Returns `ok: false` with a clear message when reload is blocked, such as changing `state.sqlite_path` while async tasks are active. |

### MCP Tool Call Example: Reload Config
```bash
curl -s -X POST http://127.0.0.1:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-local-ai-bridge-key' \
  -d '{
    "jsonrpc":"2.0",
    "id":1,
    "method":"tools/call",
    "params":{"name":"worker_config_reload","arguments":{}}
  }'
```

---

## 🚀 Build and Run

### 1. Initial Setup
Copy the provided templates to initialize your environment:
```bash
cp .env.example .env
cp config.yaml.example config.yaml
```
*Note: Edit `.env` for secrets; `config.yaml` should reference the environment variable names.*

### 2. Launch Service
Build and start the container in detached mode:
```bash
docker compose up -d --build
```

### 3. Verification
Verify the service is online:
```bash
# Check health
curl -s http://127.0.0.1:8080/health

# Check status (requires API key)
curl -s -H 'X-API-Key: dev-local-ai-bridge-key' http://127.0.0.1:8080/status

# Check MCP tool discovery
curl -s -X POST http://127.0.0.1:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-local-ai-bridge-key' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

---

## 🛠 Usage Examples

### Synchronous Worker Call
```bash
curl -s -X POST http://127.0.0.1:8080/worker_call \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-local-ai-bridge-key' \
  -d '{"worker_id":"bob","prompt":"---\nworking_directory: /workspace\n---\nping","mode":"sync"}'
```

### Asynchronous Worker Call & Polling
**Step 1: Initiate Task**
```bash
CREATE_RESPONSE=$(curl -s -X POST http://127.0.0.1:8080/worker_call \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-local-ai-bridge-key' \
  -d '{"worker_id":"bob","prompt":"---\nworking_directory: /workspace\n---\ndurable ping","mode":"async","idempotency_key":"demo-1"}')

echo "$CREATE_RESPONSE"
TASK_ID=$(python -c "import json,sys; print(json.load(sys.stdin)['taskId'])" <<< "$CREATE_RESPONSE")
```

**Step 2: Poll for Result**
```bash
curl -s -X POST http://127.0.0.1:8080/worker_check \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-local-ai-bridge-key' \
  -d "{\"task_id\":\"$TASK_ID\"}"
```

### Restart Persistence Check
Verify that tasks survive a container restart:
```bash
docker compose restart ai-bridge
curl -s -X POST http://127.0.0.1:8080/worker_check \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-local-ai-bridge-key' \
  -d "{\"task_id\":\"$TASK_ID\"}"
```

### Runtime Config Reload
After editing `config.yaml`, reload without restarting the container:
```bash
curl -s -X POST http://127.0.0.1:8080/reload \
  -H 'X-API-Key: dev-local-ai-bridge-key'
```

The reload is staged and safe:
*   Worker changes are swapped into the active manager.
*   Logging configuration is refreshed.
*   If `state.sqlite_path` changes and async tasks are active, reload is rejected with `409` rather than stranding tasks in another quadrant of the Matrix.

---

## 🤖 Hermes MCP Integration

To integrate this bridge with the Hermes CLI:
1.  **Add Server:**
    ```bash
    hermes mcp add ai-worker-bridge --url http://127.0.0.1:8080/mcp
    ```
2.  **Test & Configure:**
    ```bash
    hermes mcp test ai-worker-bridge
    hermes mcp configure ai-worker-bridge
    ```
*Security Note: Use the bridge API key in your custom headers or place the bridge behind a trusted network ACL.*

---

## 📉 Operations & Diagnostics

### Logging & State
*   **Logs:** Structured JSON logs are written to stdout and `/app/logs/bridge.log`.
*   **State Store:** Task state is maintained in SQLite at `/app/data/tasks.sqlite3` by default.
*   **Persistence:** Docker volumes `ai_bridge_data` and `ai_bridge_logs` ensure data survives restarts.

### Metrics
`GET /metrics` emits Prometheus text format. Worker latency is exposed as the `ai_bridge_worker_call_seconds` histogram with `_bucket`, `_count`, and `_sum` series. Configure bucket boundaries with `metrics.worker_call_seconds_buckets`; values must be positive and strictly increasing.

### Health Probes
`worker_list` and `/status` include cached worker health metadata. The bridge probes OpenAI-compatible `/v1/models` first, falls back to `/health`, uses short timeouts, and avoids exposing credentials or raw auth details. Probe results are cached briefly to keep worker listing fast.

### Worker Configuration Reloads
Use either `POST /reload` or the MCP `worker_config_reload` tool. Reloads are serialized with an internal lock so two agents cannot concurrently stomp the same state. The bridge validates the new config before mutating runtime state.

### Task Lifecycle
The bridge implements a strict state machine:
`Pending` $\rightarrow$ `Running` $\rightarrow$ `Completed`  
`Pending/Running` $\rightarrow$ `Cancelled`  
`Running` $\rightarrow$ `Failed` / `TimedOut`  
`Running` $\rightarrow$ `Recovering` (on restart) $\rightarrow$ `Pending` $\rightarrow$ `Running`

---

## 🧪 Testing

### Local Environment
```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
pytest
```

### Containerized Environment
```bash
docker compose build
docker compose run --rm ai-bridge pytest
```
