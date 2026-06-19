# 🕒 AI Bridge Watchdog Utility

The **AI Bridge Watchdog** is a professional, blocking command-line utility designed to eliminate "polling fatigue" for agents and operators monitoring asynchronous tasks on the AI Worker Bridge.

## 📖 Overview

When an agent triggers a worker call in `async` mode, the bridge returns a `taskId` immediately, but the actual result is produced later. The Watchdog fills this gap: it handles the tedious loop of calling `/worker_check` and blocks execution until the task reaches a terminal state, then delivers the final JSON response as the definitive source of truth.

### Key Design Principles:
*   **Zero Synthesis:** It does not transform or prettify results; it prints the bridge's raw terminal response.
*   **Resilience:** Built-in handling for transient network failures and socket timeouts.
*   **Fail-Open Auth:** If credentials are missing, it allows the request to proceed to the server so the bridge can return an authoritative `401 Unauthorized` rather than crashing locally.
*   **Agent-First:** Specifically designed to be called as a blocking terminal command by AI agents.

---

## ⚙️ Configuration Guide

Configuration is loaded from process environment variables first, then falling back to a local `.env` file (default path: `.env`).

### Environment Variables
All watchdog environment variables use the `AI_BRIDGE_*` prefix for consistency and safety.

| Variable | Required | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `AI_BRIDGE_MCP_HOST` | **Yes** | None | Hostname or base URL for the AI Bridge server (e.g., `http://127.0.0.1`). |
| `AI_BRIDGE_PORT` | No | None | HTTP port appended to host. Leave empty if host already includes a port. |
| `AI_BRIDGE_API_KEY` | No | `""` | Bridge API key sent as `X-API-Key` and Bearer token. If omitted, server returns 401. |
| `AI_BRIDGE_POLL_INTERVAL` | No | `2` | Seconds to wait between `/worker_check` calls (Min: 0.1s). |
| `AI_BRIDGE_TIMEOUT_SECONDS`| No | `1800` | Global max time to wait for terminal state before exiting with error. |
| `AI_BRIDGE_REQUEST_TIMEOUT`| No | `15` | Per-request HTTP timeout to prevent socket hangs. |
| `AI_BRIDGE_MAX_TRANSIENT_ERRORS` | No | `5` | Consecutive 5xx/network errors allowed before giving up. |

### Example `.env`
```dotenv
AI_BRIDGE_MCP_HOST=http://127.0.0.1
AI_BRIDGE_PORT=8080
AI_BRIDGE_API_KEY=your-secret-bridge-key
AI_BRIDGE_POLL_INTERVAL=2
AI_BRIDGE_TIMEOUT_SECONDS=1800
```

---

## 🚀 Setup and Usage

### 1. Initialize Environment
Enter the directory and prepare your configuration:
```bash
cd /development/Personal/Projects/containers/ai-bridge/ai_watchdog
cp .env.example .env
# Edit .env with your bridge host and API key
```

### 2. Execute the Watchdog
Run the script by passing a `taskId` returned from an async `worker_call`:
```bash
python3 watchdog.py <taskId>
```

**Example with runtime tuning:**
```bash
python3 watchdog.py task-123 --interval 1 --timeout 600 --max-transient-errors 8
```

### 3. Understanding Exit States
The Watchdog blocks until one of the following terminal states is reached:

| State | Result | Meaning |
| :--- | :--- | :--- |
| `Completed` | Code `0` | Task finished successfully; final JSON result printed to stdout. |
| `Failed` | Code `1` | Task failed at the worker or bridge level. |
| `Cancelled` | Code `1` | Task was explicitly cancelled via `worker_cancel`. |
| `TimedOut` | Code `1` | Bridge internal timeout reached. |

---

## 🤖 Agent Integration

Calling agents (e.g., Hermes/Sora) should use the watchdog to avoid implementing fragile polling loops in their own logic. This ensures a consistent task contract and standardized error handling.

### Typical Workflow
1. **Initiate:** Agent calls `worker_call(mode="async")` $\rightarrow$ receives `taskId`.
2. **Monitor:** Agent executes `watchdog.py <taskId>` via terminal tool.
3. **Resolve:** Terminal blocks until result is delivered; agent processes the final JSON output.

### Hermes Agent Implementation Pattern
```python
# Recommended pattern for orchestrators
result = terminal(
    "cd /development/Personal/Projects/containers/ai-bridge/ai_watchdog "
    "&& python3 watchdog.py task-123 --timeout 900",
    timeout=920, # Set slightly higher than the script's internal timeout
)
```

---

## 🧪 Development & Verification

### Testing Suite
To verify the utility's behavior (including auth fail-open and transient retries), run the test suite:
```bash
python3 -m unittest test_watchdog.py -v
```
The tests cover environment precedence, credential validation, timeout logic, and CLI parsing. The utility is built using the Python standard library only and requires no external dependencies for basic operation.
