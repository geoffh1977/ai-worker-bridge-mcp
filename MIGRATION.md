# AI Worker Bridge v1.0 Migration Notes

This release intentionally tightens the bridge security and operations contract. Yes, it breaks permissive defaults. That is the point.

## Breaking Changes

1. Filesystem policy is deny-all by default.
   - Old implicit behavior: missing `filesystem` meant `read: ["/"]` and `write: ["/"]`.
   - New behavior: missing `filesystem` means `read: []` and `write: []`.
   - Every worker that receives tasks with `working_directory` must explicitly configure `filesystem.write`.

2. Scoped credentials are mandatory and enforced.
   - Configure `auth.scoped_keys` with env-backed keys and scopes: `read`, `submit`, `cancel`, `admin`.
   - The legacy general key settings (`server.api_key_env`, `server.require_api_key`, and `auth.legacy_key_scopes`) have been removed. Configs containing them fail validation.
   - Every protected endpoint requires a scoped key via `X-API-Key` or `Authorization: Bearer`.
   - `POST /reload` and MCP `worker_config_reload` require `admin`.

3. Recovery is at-least-once, not exactly-once.
   - Restarted `Running` tasks move to `Recovering`.
   - Default `recovery.policy: idempotent` only replays tasks that have an `idempotency_key`.
   - Non-idempotent recovered tasks are left for manual/admin handling to avoid silent duplicate side effects.
   - Worker requests include `metadata.dispatch_attempt_id`, `metadata.idempotency_key`, `X-Dispatch-Attempt-ID`, and `Idempotency-Key` when present.

4. Queue limits are enforced.
   - Async submissions can now return `503` when global or per-worker pending/active limits are saturated.
   - Saturated submissions are rejected before task persistence.

5. Health endpoints changed.
   - `GET /live`: process liveness.
   - `GET /ready`: config/store readiness and SQLite writability.
   - `GET /health`: compatibility alias with pointers to the new endpoints.
   - Docker and Compose health checks now use `/ready`.

6. Metrics and audit logging were added.
   - `GET /metrics` exposes low-cardinality Prometheus text metrics and requires `read` scope when auth is enabled.
   - Audit logs are append-only JSONL at `audit.file_path` and capture auth failures, scope denials, submissions, cancellations, reloads, denied working directories, and worker failures without prompts or secrets.

## Minimal Config Update

```yaml
auth:
  scoped_keys:
    - key_id: reader
      env: AI_BRIDGE_READ_KEY
      scopes: [read]
    - key_id: admin
      env: AI_BRIDGE_ADMIN_KEY
      scopes: [read, submit, cancel, admin]

recovery:
  policy: idempotent
  delay_seconds: 0

limits:
  global_pending_tasks: 1000
  global_active_tasks: 100
  per_worker_pending_tasks: 100
  per_worker_active_tasks: 2
  sync_active_tasks: 100

audit:
  enabled: true
  file_path: /app/logs/audit.jsonl

workers:
  - worker_id: bob
    filesystem:
      read: ["/workspace", "/shared"]
      write: ["/workspace"]
      canonicalize: false
```

## Temporary Compatibility Flag

For a short migration window only:

```yaml
compat:
  allow_implicit_root_filesystem: true
```

This restores `read: ["/"]` and `write: ["/"]` only for workers with no explicit filesystem policy. Do not use it in production unless you enjoy explaining root access in postmortems.
