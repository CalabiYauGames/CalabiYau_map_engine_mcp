from __future__ import annotations

import asyncio
import logging
import secrets
import socket
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .bridge import BrowserRegistry, bridge_socket
from .capture import CaptureRelay
from .config import Settings
from .tools import create_mcp

logger = logging.getLogger(__name__)

# 记录服务启动时间
_server_start_time = time.time()


class LocalSecurity:
    """Protect custom routes as well as the SDK's own MCP endpoints."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope["headers"]}
        host = headers.get(b"host", b"").decode("latin-1")
        allowed_hosts = {f"127.0.0.1:{self.settings.port}", f"localhost:{self.settings.port}"}
        if self.settings.port == 80:
            allowed_hosts.update({"127.0.0.1", "localhost"})
        origin = headers.get(b"origin", b"").decode("latin-1")
        valid_host = host in allowed_hosts
        valid_origin = not origin or origin in self.settings.allowed_origins
        if scope["type"] == "websocket":
            if not valid_host or not valid_origin:
                await send(
                    {"type": "websocket.close", "code": 1008, "reason": "Forbidden host/origin"}
                )
                return
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if not valid_host or not valid_origin:
            await JSONResponse({"error": "Forbidden host/origin"}, status_code=403)(
                scope, receive, send
            )
            return
        cors_headers = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"} if origin else {}
        if request.method == "OPTIONS":
            if not origin:
                response: Response = JSONResponse({"error": "Origin required"}, status_code=403)
            else:
                response = Response(
                    status_code=204,
                    headers={
                        **cors_headers,
                        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                        "Access-Control-Allow-Headers": (
                            "Authorization, Content-Type, MCP-Protocol-Version, MCP-Session-Id, "
                            "Last-Event-ID"
                        ),
                        "Access-Control-Allow-Private-Network": "true",
                    },
                )
            await response(scope, receive, send)
            return
        if request.url.path in {"/mcp", "/mcp/", "/sse"} or request.url.path.startswith(
            "/messages"
        ):
            expected = f"Bearer {self.settings.credentials.http_token}"
            if not secrets.compare_digest(
                headers.get(b"authorization", b""), expected.encode("utf-8")
            ):
                await JSONResponse(
                    {"error": "A local MCP bearer token is required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer", **cors_headers},
                )(scope, receive, send)
                return

        async def send_with_cors(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start" and cors_headers:
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        *(
                            (key.lower().encode(), value.encode())
                            for key, value in cors_headers.items()
                        ),
                    ],
                }
            await send(message)

        await self.app(scope, receive, send_with_cors)


@dataclass
class Runtime:
    settings: Settings
    registry: BrowserRegistry
    captures: CaptureRelay
    mcp: MCPServer
    app: ASGIApp


def create_runtime(settings: Settings) -> Runtime:
    registry = BrowserRegistry(settings)
    captures = CaptureRelay()
    mcp = create_mcp(registry, captures)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"127.0.0.1:{settings.port}", f"localhost:{settings.port}"],
        allowed_origins=list(settings.allowed_origins),
    )
    streamable = None
    routes = []

    async def healthz(_: Request) -> JSONResponse:
        uptime = time.time() - _server_start_time
        connected_sessions = len(registry.browsers)  # 使用 browsers 字典
        return JSONResponse({
            "status": "ok",
            "service": "calabiyau-map-engine-mcp",
            "version": __version__,
            "uptime": uptime,
            "instances": 1,
            "port": settings.port,
            "transport": settings.transport,
            "connected_sessions": connected_sessions,
        })

    async def ws_endpoint(ws: Any) -> None:
        await bridge_socket(ws, registry)

    async def capture_upload(request: Request) -> JSONResponse:
        """Receive one capture from the browser.

        This is the road a screenshot takes instead of the 1 MiB bridge frame:
        the tool mints a slot, the browser POSTs the raw bytes here, the tool
        picks them up.  The id in the path is the only credential, so it is
        single-use and expires.
        """
        capture_id = request.path_params["capture_id"]
        limit = captures.max_bytes
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            captures.discard(capture_id)
            return JSONResponse({"error": f"capture exceeds {limit} bytes"}, status_code=413)
        # Read incrementally instead of `await request.body()`. The header check only
        # binds an honest client: a chunked upload declares no length, and buffering it
        # first would let anyone on the loopback interface grow this process until it
        # dies -- on a route that deliberately carries no bearer token.
        buffer = bytearray()
        async for chunk in request.stream():
            if len(buffer) + len(chunk) > limit:
                captures.discard(capture_id)
                return JSONResponse({"error": f"capture exceeds {limit} bytes"}, status_code=413)
            buffer.extend(chunk)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if not captures.deliver(
            capture_id, bytes(buffer), content_type or "application/octet-stream"
        ):
            return JSONResponse({"error": "unknown or expired capture slot"}, status_code=404)
        return JSONResponse({"received": len(buffer)})

    routes.extend([
        Route("/healthz", healthz),
        Route("/capture/{capture_id}", capture_upload, methods=["POST"]),
        WebSocketRoute("/bridge", ws_endpoint),
    ])
    # "http" is the resident shape: both HTTP transports, no stdio, so a service manager can
    # own the process without its stdin deciding the lifetime.
    if settings.transport in {"sse", "http", "all"}:
        routes.extend(mcp.sse_app(transport_security=security).routes)
    if settings.transport in {"streamable-http", "http", "all"}:
        streamable = mcp.streamable_http_app(
            json_response=False, max_sessions=128, transport_security=security
        )
        routes.extend(streamable.routes)

    @asynccontextmanager
    async def lifespan(_: Starlette):
        try:
            if streamable is not None:
                async with mcp.session_manager.run():
                    yield
            else:
                yield
        finally:
            await registry.close()

    app = LocalSecurity(Starlette(routes=routes, lifespan=lifespan), settings)
    return Runtime(settings, registry, captures, mcp, app)


async def serve(settings: Settings, *, log_level: str = "info") -> None:
    runtime = create_runtime(settings)
    # Bind before starting STDIO so a port conflict cannot leave a partially working server.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    http_task: asyncio.Task[None] | None = None
    stdio_task: asyncio.Task[None] | None = None
    # Protocol DEBUG logs include authentication frames, including the pairing token.
    protocol_log_level = "info" if log_level == "debug" else log_level
    server = uvicorn.Server(
        uvicorn.Config(
            runtime.app,
            host=settings.host,
            port=settings.port,
            log_level=protocol_log_level,
            access_log=False,
            ws_max_size=1_048_576,
            timeout_graceful_shutdown=5,
        )
    )
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((settings.host, settings.port))
        listener.listen(128)
        listener.setblocking(False)
        http_task = asyncio.create_task(server.serve(sockets=[listener]))
        logger.info(
            "Local bridge listening on %s:%d (transport=%s). Run the pair command for credentials.",
            settings.host,
            settings.port,
            settings.transport,
        )
        if settings.transport in {"stdio", "all"}:
            stdio_task = asyncio.create_task(runtime.mcp.run_stdio_async())
            done, _ = await asyncio.wait(
                {stdio_task, http_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                await task
        else:
            await http_task
    finally:
        server.should_exit = True
        if stdio_task is not None and not stdio_task.done():
            stdio_task.cancel()
        # Close WebSockets before Uvicorn's graceful wait; otherwise they keep it alive.
        await runtime.registry.close()
        if http_task is not None and not http_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(http_task), timeout=8)
            except TimeoutError:
                http_task.cancel()
        for task in (stdio_task, http_task):
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task
        listener.close()
