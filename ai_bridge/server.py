from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import AppConfig, load_config, runtime_paths
from .logging_config import configure_logging
from .manager import TaskManager
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


def _state(app: FastAPI) -> tuple[AppConfig, TaskManager, WorkerRegistry]:
    return app.state.config, app.state.manager, app.state.workers


def create_app(config_path: str | None = None) -> FastAPI:
    paths = runtime_paths()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    resolved_config_path = config_path or str(paths.config_path)
    config = load_config(resolved_config_path)
    configure_logging(config.logging.level, config.logging.file_path)
    store = TaskStore(config.state.sqlite_path)
    workers = WorkerRegistry(config.workers, config.circuit_breaker)
    manager = TaskManager(store, workers)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.config = config
        app.state.config_path = resolved_config_path
        app.state.store = store
        app.state.workers = workers
        app.state.manager = manager
        app.state.reload_lock = asyncio.Lock()
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(title="AI Worker Bridge MCP Server", version="0.1.0", lifespan=lifespan)

    async def require_auth(
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> None:
        cfg: AppConfig = request.app.state.config
        if not cfg.server.require_api_key:
            return
        expected = os.getenv(cfg.server.api_key_env)
        if not expected:
            raise HTTPException(status_code=503, detail="bridge API key is not configured")
        bearer = authorization.removeprefix("Bearer ").strip() if authorization else None
        if x_api_key != expected and bearer != expected:
            raise HTTPException(status_code=401, detail="invalid API key")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "ai-worker-bridge-mcp"}

    @app.post("/mock/openai")
    async def mock_openai(request: Request) -> dict[str, Any]:
        """Local OpenAI-compatible smoke-test worker used by the example config."""
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
    async def status(_: None = Depends(require_auth)) -> dict[str, Any]:
        cfg, _manager, registry = _state(app)
        return {
            "ok": True,
            "workers": await registry.list_public(),
            "state_store": cfg.state.sqlite_path,
            "mcp": {"sse": "/mcp", "streamable_http": "/mcp", "messages": "/messages"},
        }

    @app.post("/reload")
    async def reload_config(_: None = Depends(require_auth)) -> dict[str, Any]:
        return await _reload_config(app)

    @app.get("/mcp")
    async def mcp_sse(_: None = Depends(require_auth)) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            discovery = {
                "jsonrpc": "2.0",
                "method": "notifications/tools/list_changed",
                "params": {"tools": _tool_schemas()},
            }
            yield f"event: endpoint\ndata: /messages\n\n"
            yield f"event: message\ndata: {json.dumps(discovery)}\n\n"
            while True:
                await asyncio.sleep(15)
                yield "event: ping\ndata: {}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/mcp")
    async def mcp_streamable_http(request: Request, _: None = Depends(require_auth)) -> JSONResponse:
        payload = await request.json()
        response = await _handle_jsonrpc(payload, app)
        return JSONResponse(response, headers={"MCP-Protocol-Version": "2025-11-25"})

    @app.post("/messages")
    async def mcp_messages(request: Request, _: None = Depends(require_auth)) -> JSONResponse:
        payload = await request.json()
        response = await _handle_jsonrpc(payload, app)
        return JSONResponse(response, headers={"MCP-Protocol-Version": "2025-11-25"})

    @app.get("/tools")
    async def tools(_: None = Depends(require_auth)) -> dict[str, Any]:
        return {"ok": True, "tools": _tool_schemas()}

    @app.post("/worker_call")
    async def worker_call(body: WorkerCallRequest, _: None = Depends(require_auth)) -> dict[str, Any]:
        if body.mode not in {"sync", "async"}:
            raise HTTPException(status_code=400, detail="mode must be sync or async")
        return await app.state.manager.call(
            worker_id=body.worker_id,
            prompt=body.prompt,
            mode=body.mode,
            idempotency_key=body.idempotency_key,
        )

    @app.post("/worker_check")
    async def worker_check(body: WorkerCheckRequest, _: None = Depends(require_auth)) -> dict[str, Any]:
        return await app.state.manager.check(body.task_id)

    @app.post("/worker_cancel")
    async def worker_cancel(body: WorkerCancelRequest, _: None = Depends(require_auth)) -> dict[str, Any]:
        return await app.state.manager.cancel(body.task_id)

    @app.get("/worker_list")
    async def worker_list(_: None = Depends(require_auth)) -> dict[str, Any]:
        return {"ok": True, "workers": await app.state.workers.list_public()}

    return app


async def _reload_config(app: FastAPI) -> dict[str, Any]:
    async with app.state.reload_lock:
        new_config = load_config(app.state.config_path)
        configure_logging(new_config.logging.level, new_config.logging.file_path)
        new_workers = WorkerRegistry(new_config.workers, new_config.circuit_breaker)
        current_manager: TaskManager = app.state.manager
        current_config: AppConfig = app.state.config

        if new_config.state.sqlite_path != current_config.state.sqlite_path:
            if current_manager.has_active_tasks():
                raise HTTPException(
                    status_code=409,
                    detail="cannot reload state.sqlite_path while async tasks are active",
                )
            await current_manager.stop()
            new_store = TaskStore(new_config.state.sqlite_path)
            new_manager = TaskManager(new_store, new_workers)
            await new_manager.start()
            app.state.store = new_store
            app.state.manager = new_manager
        else:
            await current_manager.update_workers(new_workers)

        app.state.config = new_config
        app.state.workers = new_workers
        return {
            "ok": True,
            "message": "configuration reloaded successfully",
            "config_path": str(app.state.config_path),
            "workers": await new_workers.list_public(),
            "state_store": new_config.state.sqlite_path,
        }


def _tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": "worker_call",
            "description": "Call an OpenAI-compatible worker synchronously or as a durable async task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "worker_id": {"type": "string"},
                    "prompt": {"type": "string"},
                    "mode": {"type": "string", "enum": ["sync", "async"], "default": "sync"},
                    "idempotency_key": {"type": "string"},
                },
                "required": ["worker_id", "prompt"],
            },
        },
        {
            "name": "worker_check",
            "description": "Poll a durable async task by taskId/task_id and retrieve result.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
        {
            "name": "worker_list",
            "description": "List configured workers without exposing credentials.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "worker_cancel",
            "description": "Cancel a pending or running durable async task.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
        {
            "name": "worker_config_reload",
            "description": (
                "Reload bridge configuration from disk without calling the /reload HTTP "
                "endpoint directly."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _content(result: dict[str, Any]) -> list[dict[str, str]]:
    return [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]


async def _handle_jsonrpc(payload: dict[str, Any], app: FastAPI) -> dict[str, Any]:
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    manager: TaskManager = app.state.manager
    workers: WorkerRegistry = app.state.workers
    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ai-worker-bridge-mcp", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {"tools": _tool_schemas()}
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "worker_call":
                call_result = await manager.call(
                    worker_id=args["worker_id"],
                    prompt=args["prompt"],
                    mode=args.get("mode", "sync"),
                    idempotency_key=args.get("idempotency_key"),
                )
            elif name == "worker_check":
                call_result = await manager.check(args["task_id"])
            elif name == "worker_list":
                call_result = {"ok": True, "workers": await workers.list_public()}
            elif name == "worker_cancel":
                call_result = await manager.cancel(args["task_id"])
            elif name == "worker_config_reload":
                try:
                    call_result = await _reload_config(app)
                except HTTPException as exc:
                    call_result = {
                        "ok": False,
                        "message": str(exc.detail),
                        "status_code": exc.status_code,
                    }
                except Exception as exc:  # noqa: BLE001 - report reload failures as tool output
                    call_result = {"ok": False, "message": f"configuration reload failed: {exc}"}
            else:
                raise ValueError(f"unknown tool: {name}")
            result = {"content": _content(call_result), "isError": not call_result.get("ok", False)}
        else:
            raise ValueError(f"unsupported method: {method}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:  # noqa: BLE001 - JSON-RPC error envelope
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}}
