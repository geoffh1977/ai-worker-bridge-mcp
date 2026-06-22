from __future__ import annotations

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Awaitable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from .audit import AuditLogger
from .config import AppConfig, Scope, load_config, runtime_paths
from .exceptions import SaturationError, WorkingDirectoryError
from .logging_config import configure_logging
from .manager import TaskManager
from .metrics import MetricsRegistry
from .store import TaskStore
from .workers import WorkerRegistry


class WorkerCallRequest(BaseModel):
    worker_id: str
    prompt: str = Field(min_length=1)
    mode: str = "sync"
    idempotency_key: str | None = None


class WorkerCheckRequest(BaseModel):
    task_id: str


class WorkerCancelRequest(BaseModel):
    task_id: str


@dataclass(frozen=True)
class AuthContext:
    authenticated: bool
    key_id: str
    scopes: frozenset[str]
    source_ip: str | None

    def require(self, scope: Scope) -> None:
        if scope not in self.scopes:
            raise HTTPException(status_code=403, detail=f"missing required scope: {scope}")


def _state(app: FastAPI) -> tuple[AppConfig, TaskManager, WorkerRegistry]:
    return app.state.config, app.state.manager, app.state.workers


def create_app(config_path: str | None = None) -> FastAPI:
    paths = runtime_paths()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    resolved_config_path = config_path or str(paths.config_path)
    config = load_config(resolved_config_path)
    configure_logging(config.logging.level, config.logging.file_path)
    metrics = MetricsRegistry()
    audit = AuditLogger(config.audit.file_path, enabled=config.audit.enabled)
    store = TaskStore(config.state.sqlite_path)
    workers = WorkerRegistry(config.workers, config.circuit_breaker, metrics=metrics)
    manager = TaskManager(store, workers, recovery=config.recovery, limits=config.limits, audit=audit, metrics=metrics)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.config = config
        app.state.config_path = resolved_config_path
        app.state.store = store
        app.state.workers = workers
        app.state.manager = manager
        app.state.metrics = metrics
        app.state.audit = audit
        app.state.reload_lock = asyncio.Lock()
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(title="AI Worker Bridge MCP Server", version="1.0.0", lifespan=lifespan)

    async def authenticate(
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> AuthContext:
        cfg: AppConfig = request.app.state.config
        source_ip = request.client.host if request.client else None
        presented = x_api_key
        if authorization and authorization.startswith("Bearer "):
            presented = authorization.removeprefix("Bearer ").strip()
        if not presented:
            request.app.state.audit.emit("auth_failure", outcome="missing", source_ip=source_ip)
            raise HTTPException(status_code=401, detail="missing API key")
        for scoped in cfg.auth.scoped_keys:
            expected = os.getenv(scoped.env)
            if expected and secrets.compare_digest(presented, expected):
                return AuthContext(True, scoped.key_id, frozenset(scoped.scopes), source_ip)
        request.app.state.audit.emit("auth_failure", outcome="invalid", source_ip=source_ip)
        raise HTTPException(status_code=401, detail="invalid API key")

    def require_scope(scope: Scope) -> Callable[[AuthContext], Awaitable[AuthContext]]:
        async def dependency(ctx: AuthContext = Depends(authenticate)) -> AuthContext:
            try:
                ctx.require(scope)
            except HTTPException:
                app.state.audit.emit("auth_scope_denied", outcome="forbidden", actor=ctx.key_id, source_ip=ctx.source_ip, scope=scope)
                raise
            return ctx
        return dependency

    @app.get("/live")
    async def live() -> dict[str, Any]:
        return {"ok": True, "service": "ai-worker-bridge-mcp", "status": "live"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        checks: dict[str, Any] = {"config": True, "workers_configured": bool(app.state.config.workers)}
        status = 200
        try:
            checks["sqlite_writable"] = app.state.store.ping()
        except Exception as exc:  # noqa: BLE001
            checks["sqlite_writable"] = False
            checks["sqlite_error"] = exc.__class__.__name__
            status = 503
        return JSONResponse({"ok": status == 200, "checks": checks}, status_code=status)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "ai-worker-bridge-mcp", "live": "/live", "ready": "/ready"}

    @app.get("/metrics")
    async def metrics_endpoint(_: AuthContext = Depends(require_scope("read"))) -> PlainTextResponse:
        text = app.state.metrics.render(active_tasks=app.state.manager.active_count(), queued_tasks=app.state.manager.queued_count())
        return PlainTextResponse(text, media_type="text/plain; version=0.0.4")

    @app.post("/mock/openai")
    async def mock_openai(request: Request) -> dict[str, Any]:
        payload = await request.json()
        messages = payload.get("messages") or []
        prompt = messages[-1].get("content", "") if messages else ""
        return {
            "id": "chatcmpl-ai-bridge-smoke",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": f"echo:{prompt}"}, "finish_reason": "stop"}],
            "model": payload.get("model", "mock-worker"),
        }

    @app.get("/status")
    async def status(_: AuthContext = Depends(require_scope("read"))) -> dict[str, Any]:
        cfg, mgr, registry = _state(app)
        return {
            "ok": True,
            "workers": await registry.list_public(),
            "state_store": cfg.state.sqlite_path,
            "recovery": cfg.recovery.model_dump(),
            "limits": cfg.limits.model_dump(),
            "queues": {"active": mgr.active_count(), "queued": mgr.queued_count()},
            "mcp": {"sse": "/mcp", "streamable_http": "/mcp", "messages": "/messages"},
        }

    @app.post("/reload")
    async def reload_config(ctx: AuthContext = Depends(require_scope("admin"))) -> dict[str, Any]:
        result = await _reload_config(app, ctx)
        app.state.audit.emit("config_reload", outcome="success", actor=ctx.key_id, source_ip=ctx.source_ip)
        return result

    @app.get("/mcp")
    async def mcp_sse(_: AuthContext = Depends(require_scope("read"))) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            discovery = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {"tools": _tool_schemas()}}
            yield "event: endpoint\ndata: /messages\n\n"
            yield f"event: message\ndata: {json.dumps(discovery)}\n\n"
            while True:
                await asyncio.sleep(15)
                yield "event: ping\ndata: {}\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/mcp")
    async def mcp_streamable_http(request: Request, ctx: AuthContext = Depends(authenticate)) -> JSONResponse:
        payload = await request.json()
        response = await _handle_jsonrpc(payload, app, ctx)
        return JSONResponse(response, headers={"MCP-Protocol-Version": "2025-11-25"})

    @app.post("/messages")
    async def mcp_messages(request: Request, ctx: AuthContext = Depends(authenticate)) -> JSONResponse:
        payload = await request.json()
        response = await _handle_jsonrpc(payload, app, ctx)
        return JSONResponse(response, headers={"MCP-Protocol-Version": "2025-11-25"})

    @app.get("/tools")
    async def tools(_: AuthContext = Depends(require_scope("read"))) -> dict[str, Any]:
        return {"ok": True, "tools": _tool_schemas()}

    @app.post("/worker_call")
    async def worker_call(body: WorkerCallRequest, ctx: AuthContext = Depends(require_scope("submit"))) -> dict[str, Any]:
        if body.mode not in {"sync", "async"}:
            raise HTTPException(status_code=400, detail="mode must be sync or async")
        try:
            return await app.state.manager.call(
                worker_id=body.worker_id,
                prompt=body.prompt,
                mode=body.mode,  # type: ignore[arg-type]
                idempotency_key=body.idempotency_key,
                actor=ctx.key_id,
                source_ip=ctx.source_ip,
            )
        except WorkingDirectoryError as exc:
            app.state.audit.emit("working_directory_denied", outcome="denied", actor=ctx.key_id, source_ip=ctx.source_ip, worker_id=body.worker_id, error_category=exc.__class__.__name__)
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
        except SaturationError as exc:
            raise HTTPException(status_code=exc.status_code, detail={"error": str(exc), "scope": exc.scope}) from None

    @app.post("/worker_check")
    async def worker_check(body: WorkerCheckRequest, _: AuthContext = Depends(require_scope("read"))) -> dict[str, Any]:
        return await app.state.manager.check(body.task_id)

    @app.post("/worker_cancel")
    async def worker_cancel(body: WorkerCancelRequest, ctx: AuthContext = Depends(require_scope("cancel"))) -> dict[str, Any]:
        return await app.state.manager.cancel(body.task_id, actor=ctx.key_id, source_ip=ctx.source_ip)

    @app.get("/worker_list")
    async def worker_list(_: AuthContext = Depends(require_scope("read"))) -> dict[str, Any]:
        return {"ok": True, "workers": await app.state.workers.list_public()}

    return app


async def _reload_config(app: FastAPI, ctx: AuthContext | None = None) -> dict[str, Any]:
    async with app.state.reload_lock:
        new_config = load_config(app.state.config_path)
        configure_logging(new_config.logging.level, new_config.logging.file_path)
        audit = AuditLogger(new_config.audit.file_path, enabled=new_config.audit.enabled)
        new_workers = WorkerRegistry(new_config.workers, new_config.circuit_breaker, metrics=app.state.metrics)
        current_manager: TaskManager = app.state.manager
        current_config: AppConfig = app.state.config
        if new_config.state.sqlite_path != current_config.state.sqlite_path:
            if current_manager.has_active_tasks():
                raise HTTPException(status_code=409, detail="cannot reload state.sqlite_path while async tasks are active")
            await current_manager.stop()
            new_store = TaskStore(new_config.state.sqlite_path)
            new_manager = TaskManager(new_store, new_workers, recovery=new_config.recovery, limits=new_config.limits, audit=audit, metrics=app.state.metrics)
            await new_manager.start()
            app.state.store = new_store
            app.state.manager = new_manager
        else:
            current_manager.recovery = new_config.recovery
            current_manager.limits = new_config.limits
            current_manager.audit = audit
            await current_manager.update_workers(new_workers)
        app.state.config = new_config
        app.state.workers = new_workers
        app.state.audit = audit
        return {"ok": True, "message": "configuration reloaded successfully", "config_path": str(app.state.config_path), "workers": await new_workers.list_public(), "state_store": new_config.state.sqlite_path}


def _tool_schemas() -> list[dict[str, Any]]:
    return [
        {"name": "worker_call", "description": "Call an OpenAI-compatible worker synchronously or as a durable async task.", "inputSchema": {"type": "object", "properties": {"worker_id": {"type": "string"}, "prompt": {"type": "string"}, "mode": {"type": "string", "enum": ["sync", "async"], "default": "sync"}, "idempotency_key": {"type": "string"}}, "required": ["worker_id", "prompt"]}},
        {"name": "worker_check", "description": "Poll a durable async task by taskId/task_id and retrieve result.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
        {"name": "worker_list", "description": "List configured workers without exposing credentials.", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "worker_cancel", "description": "Cancel a pending or running durable async task.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
        {"name": "worker_config_reload", "description": "Reload bridge configuration from disk without calling the /reload HTTP endpoint directly.", "inputSchema": {"type": "object", "properties": {}}},
    ]


def _content(result: dict[str, Any]) -> list[dict[str, str]]:
    return [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]


def _scope_for_tool(name: str) -> Scope:
    return {"worker_call": "submit", "worker_check": "read", "worker_list": "read", "worker_cancel": "cancel", "worker_config_reload": "admin"}.get(name, "read")  # type: ignore[return-value]


async def _handle_jsonrpc(payload: dict[str, Any], app: FastAPI, ctx: AuthContext) -> dict[str, Any]:
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    manager: TaskManager = app.state.manager
    workers: WorkerRegistry = app.state.workers
    try:
        if method == "initialize":
            ctx.require("read")
            result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "ai-worker-bridge-mcp", "version": "1.0.0"}}
        elif method == "tools/list":
            ctx.require("read")
            result = {"tools": _tool_schemas()}
        elif method == "tools/call":
            name = str(params.get("name") or "")
            args = params.get("arguments") or {}
            ctx.require(_scope_for_tool(name))
            if name == "worker_call":
                try:
                    call_result = await manager.call(worker_id=args["worker_id"], prompt=args["prompt"], mode=args.get("mode", "sync"), idempotency_key=args.get("idempotency_key"), actor=ctx.key_id, source_ip=ctx.source_ip)
                except WorkingDirectoryError as exc:
                    app.state.audit.emit("working_directory_denied", outcome="denied", actor=ctx.key_id, source_ip=ctx.source_ip, worker_id=args.get("worker_id"), error_category=exc.__class__.__name__)
                    call_result = {"ok": False, "status_code": exc.status_code, "error": "Bad Request", "message": str(exc)}
                except SaturationError as exc:
                    call_result = {"ok": False, "status_code": exc.status_code, "error": "Saturated", "message": str(exc), "scope": exc.scope}
            elif name == "worker_check":
                call_result = await manager.check(args["task_id"])
            elif name == "worker_list":
                call_result = {"ok": True, "workers": await workers.list_public()}
            elif name == "worker_cancel":
                call_result = await manager.cancel(args["task_id"], actor=ctx.key_id, source_ip=ctx.source_ip)
            elif name == "worker_config_reload":
                try:
                    call_result = await _reload_config(app, ctx)
                    app.state.audit.emit("config_reload", outcome="success", actor=ctx.key_id, source_ip=ctx.source_ip)
                except HTTPException as exc:
                    call_result = {"ok": False, "message": str(exc.detail), "status_code": exc.status_code}
                except Exception as exc:  # noqa: BLE001
                    call_result = {"ok": False, "message": f"configuration reload failed: {exc}"}
            else:
                raise ValueError(f"unknown tool: {name}")
            result = {"content": _content(call_result), "isError": not call_result.get("ok", False)}
        else:
            raise ValueError(f"unsupported method: {method}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except HTTPException as exc:
        app.state.audit.emit("auth_scope_denied", outcome="forbidden", actor=ctx.key_id, source_ip=ctx.source_ip, method=method)
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32001, "message": str(exc.detail)}}
    except Exception as exc:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}}
