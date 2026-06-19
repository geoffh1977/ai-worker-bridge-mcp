# Specification: AI Worker Bridge MCP Server (v1)

## 1. Executive Summary
The **AI Worker Bridge** is a standalone, containerized MCP server that orchestrates communication between an Agent Client (e.g., Hermes/Sora) and various OpenAI-compatible worker gateways. Its primary purpose is to abstract credentials, provide a unified tool interface via the Model Context Protocol (MCP), and transform synchronous API endpoints into durable asynchronous tasks.

## 2. System Architecture
### 2.1 Deployment Model
- **Execution Environment**: Dockerized Python Application.
- **Deployment Mode**: Standalone service (Service Boundary).
- **Transport Layer**: MCP over SSE/HTTP.
- **Lifecycle**: Persistent process; independent of agent sessions.

### 2.2 The "Bridge" Logic Flow
1. Agent -> Bridge: Call `worker_call(name="Bob", ...)` via MCP tool call.
2. Bridge -> Worker: Validates request, resolves worker configuration from local config/secrets.
3. Bridge Decision Path:
   - **Synchronous**: Directly awaits response and returns it to Agent.
   - **Asynchronous**: Spawns a background task, persists the `taskId` in state, and immediately returns the `taskId` to the Agent.
4. Agent -> Bridge: Polls via `worker_check(task_id="...")` until completion.

## 3. Functional Requirements

### 3.1 Configuration & Worker Management
The server must load a structured worker definition on startup (YAML/JSON). Each worker entry must include:
- **Identity**: `worker_id` (stable ID), `display_name`.
- **Connectivity**: `endpoint_url`, `auth_type` (Bearer, Basic, etc.), and secret reference.
- **Constraints**: `timeout_limits`, `max_concurrent_tasks`, and allowed modes (`sync`/`async`).
- **Routing**: `model_name` and optional default system prompts.

### 3.2 MCP Tool Interface
All tools must be prefixed with `worker_*`. Base toolset:
- `worker_call(worker_id, prompt, mode="sync")`: Main entry point for agent requests.
- `worker_check(task_id)`: Poll status and retrieve results for async jobs.
- `worker_list()`: Discover available workers and their capabilities.
- `worker_cancel(task_id)`: Terminate a running background job.

### 3.3 Async Transformation Layer (The "Durability" Engine)
Since target worker endpoints are synchronous, the Bridge must implement an internal async emulation:
- **Task State Machine**: 
  - Mandatory States: `Pending`, `Running`, `Completed`, `Failed`, `Cancelled`, `TimedOut`.
  - Optional/Advanced States: `Recovering` (during restart), `Retrying`.
- **Execution Isolation**: Background jobs must run in separate threads/processes to avoid blocking the MCP server's main event loop.
- **Idempotency**: Every async request must be mapped to a unique `taskId` for idempotency.
- **Persistence**: Task state must survive Bridge restarts (state stored in persistent volume).

### 3.4 Security & Authentication
- **Bridge Access**: The MCP endpoint itself should be protected via API key or network ACLs.
- **Credential Masking**: Worker passwords/keys are held only within the Bridge; they are never exposed to the calling agent.

## 4. Technical Constraints
- **Language**: Python 3.12+.
- **MCP Spec**: Compliance with the latest MCP server specification for SSE transport.
- **Error Handling**: Implement circuit breaker thresholds (if a worker fails X times, mark as `down`).
- **Output Format**: Strict JSON return types to ensure agent compatibility.

## 5. Project Management & Delivery

### 5.1 Project Structure
- **Root Directory**: `/development/Personal/Projects/containers/ai-bridge`
- **Container Internal Paths**:
  - App Root: `/app`
  - Configs: `/app/configs`
  - Persistent Data (Tasks): `/app/data`
  - Logs: `/app/logs`

### 5.2 Final Deliverables
The complete project package must include:
- **Application Code**: Fully commented Python source code implementing the MCP Bridge.
- **Containerization**: `Dockerfile` (optimized) and `docker-compose.yml`.
- **Documentation (`README.md`)**: Comprehensive guide covering Overview, File Map, Build/Runtime instructions, Agent Integration guides (inc. Hermes), and Operational Diagnostics.
- **Config Templates**: `config.yaml.example` and `.env.example` for zero-friction deployment.

## 6. Observability & Log Management
- **Structured Logging**: JSON logs written to `/app/logs` and stdout.
- **Health Endpoints**: `/health` (Liveness) and `/status` (Readiness - checks config and state store).

## 7. Testing & Validation Strategy
- **Unit Tests**: Mandatory for the Task State Machine (verifying all transitions, especially `Cancelled` -> `Failed`).
- **Integration Tests**: Verification of timeout handling and worker failovers using mock endpoints.
- **Persistence Test**: Verify task recovery after a simulated container restart.

## 8. Definition of Done (DoD)
- [ ] MCP server successfully launches in Docker and is discoverable via URL.
- [ ] Synchronous call to a worker returns the expected response in real-time.
- [ ] Asynchronous call generates a `taskId`, survives restart, and can be polled for result.
- [ ] The result of an async task is retrieved using only the `taskId`.
- [ ] All deliverables (Code, Dockerfile, Compose, README, Templates) are present in root.
