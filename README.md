# 🌉 AI Worker Bridge MCP Server

## 📖 Overview
The **AI Worker Bridge** is a standalone, containerized MCP server designed to orchestrate communication between an Agent Client (e.g., Hermes/Sora) and various OpenAI-compatible worker gateways.

### Key Capabilities:
*   **Credential Abstraction:** Hides worker credentials behind a single bridge API key.
*   **Unified Interface:** Exposes `worker_*` tools over the Model Context Protocol (MCP) using SSE/HTTP.
*   **Durable Async Transformation:** Transforms synchronous worker endpoints into durable asynchronous tasks backed by SQLite persistence.

---

## 📂 File Map

| File / Directory | Description |
| :--- | :--- |
| `ai_bridge/config.py` | YAML and environment configuration models |
| `ai_bridge/task_state.py` | Task state machine and transition guards |
| `ai_bridge/store.py` | SQLite persistence layer for durable task records |
| `ai_bridge/workers.py` | OpenAI-compatible worker client, auth masking, and circuit breaker |
| `ai_bridge/manager.py` | Sync/async orchestration and restart recovery |
| `ai_bridge/server.py` | FastAPI HTTP/SSE/MCP endpoints |
| `Dockerfile` | Production container image specification |
| `docker-compose.yml` | Local runtime configuration with persistent data/log volumes |
| `config.yaml` | Local smoke-test config using the built-in mock worker |
| `config.yaml.example` | Deployment template for worker configurations |
| `.env` | Local secrets and bridge API key |
| `.env.example` | Deployment secrets template |
| `tests/` | Unit, integration, and persistence tests |

---

## ⚙️ Runtime API

### System Endpoints
*   **Health Check:** `GET /health` (Liveness)
*   **Readiness Status:** `GET /status` (Check configs & state store) \
    *Header:* `X-API-Key: <AI_BRIDGE_API_KEY>`

### MCP Interface
*   **Discovery & SSE:** `GET /mcp` \
    *Header:* `X-API-Key: <AI_BRIDGE_API_KEY>`
*   **HTTP / JSON-RPC:** `POST /mcp` \
    *Headers:* `X-API-Key: <AI_BRIDGE_API_KEY>`, `MCP-Protocol-Version: 2025-11-25`

### Supported JSON-RPC Methods
*   `initialize`
*   `tools/list`
*   `tools/call` (Supporting tools: `worker_call`, `worker_check`, `worker_list`, `worker_cancel`)

### Convenience HTTP Endpoints (Smoke Tests)
*   `POST /worker_call`
*   `POST /worker_check`
*   `POST /worker_cancel`
*   `GET /worker_list`

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
```

---

## 🛠 Usage Examples

### Synchronous Worker Call
```bash
curl -s -X POST http://127.0.0.1:8080/worker_call \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-local-ai-bridge-key' \
  -d '{"worker_id":"bob","prompt":"ping","mode":"sync"}'
```

### Asynchronous Worker Call & Polling
**Step 1: Initiate Task**
```bash
CREATE_RESPONSE=$(curl -s -X POST http://127.0.0.1:8080/worker_call \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-local-ai-bridge-key' \
  -d '{"worker_id":"bob","prompt":"durable ping","mode":"async","idempotency_key":"demo-1"}')

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
  -d '{"task_id":"$TASK_ID"}'
```

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
*   **State Store:** Task state is maintained in SQLite at `/app/data/tasks.sqlite3`.
*   **Persistence:** Docker volumes `ai_bridge_data` and `ai_bridge_logs` ensure data survives restarts.

### Worker Configuration
Worker entries support: `worker_id`, `display_name`, `endpoint_url`, `auth_type` (`none`, `bearer`, `basic`), `bearer_secret_env`, `basic_username_env`, `basic_password_env`, `model_name`, `default_system_prompt`, `allowed_modes`, `timeout_limits`, and `max_concurrent_tasks`.

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
