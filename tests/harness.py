"""Shared plumbing for the tests that speak real MCP over a real socket.

`live_runtime` starts the actual ASGI app on an ephemeral port, `mcp_client` dials it
with the SDK's own client, and `cli_process` runs the packaged module as a subprocess
for the stdio transport. Nothing here fakes the protocol layer: a test that uses these
helpers has to go through initialize, tools/list and tools/call like any client would.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import httpx2
import uvicorn
from mcp.client import Client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from calabiyau_map_engine_mcp.config import Credentials, Settings
from calabiyau_map_engine_mcp.server import create_runtime

ORIGIN = "http://127.0.0.1:5173"
BROWSER_ID = "test-browser-session"
MAP_ID = "test-map-session"
ROOT = Path(__file__).resolve().parents[1]


def credentials_value() -> Credentials:
    return Credentials(pairing_token="test-pairing-" + "p" * 32, http_token="test-http-" + "h" * 32)


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@asynccontextmanager
async def live_runtime(transport: str = "all", *, request_timeout: float = 30.0):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    settings = Settings(
        credentials=credentials_value(),
        transport=transport,
        port=port,
        request_timeout=request_timeout,
    )
    runtime = create_runtime(settings)
    server = uvicorn.Server(
        uvicorn.Config(
            runtime.app, host="127.0.0.1", port=port, log_level="error", access_log=False
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("HTTP server exited before startup.")
                await asyncio.sleep(0.01)
        yield runtime, f"http://127.0.0.1:{port}"
    finally:
        await runtime.registry.close()
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=8)
        except TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        listener.close()


def local_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    auth: httpx2.Auth | None = None,
) -> httpx2.AsyncClient:
    """An SDK client that never reads proxy settings from the environment.

    Without `trust_env=False` a machine-wide HTTP_PROXY turns the SSE message POSTs into
    absolute-form request targets, which the ASGI router answers with 404.
    """
    return httpx2.AsyncClient(headers=headers, timeout=timeout, auth=auth, trust_env=False)


async def call(client, tool_name: str, **arguments: Any) -> dict[str, Any]:
    """Call one tool and return its structured content, failing on a tool error."""
    result = await client.call_tool(tool_name, arguments)
    assert not result.is_error, result.structured_content
    return result.structured_content


async def rejected(client, tool_name: str, **arguments: Any) -> dict[str, Any]:
    """Call one tool that must fail, and return the structured error."""
    result = await client.call_tool(tool_name, arguments)
    assert result.is_error, result.structured_content
    return result.structured_content["error"]


@asynccontextmanager
async def mcp_client(base: str, token: str, transport: str = "modern"):
    headers = {"Authorization": f"Bearer {token}"}
    if transport == "sse":
        async with Client(
            sse_client(f"{base}/sse", headers=headers, httpx_client_factory=local_http_client),
            mode="legacy",
        ) as client:
            yield client
    else:
        async with httpx2.AsyncClient(headers=headers, trust_env=False) as http:
            stream = streamable_http_client(f"{base}/mcp", http_client=http)
            mode = "legacy" if transport == "legacy-http" else "auto"
            async with Client(stream, mode=mode) as client:
                yield client


@asynccontextmanager
async def cli_process(tmp_path: Path, transport: str, port: int, log_level: str = "warning"):
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONUNBUFFERED": "1",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "calabiyau_map_engine_mcp",
        "--transport",
        transport,
        "--port",
        str(port),
        "--config-dir",
        str(tmp_path),
        "--log-level",
        log_level,
        cwd=ROOT,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        yield process
    finally:
        if process.returncode is None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 12)
            except TimeoutError:
                process.kill()
                await process.wait()


async def stdio_rpc(
    process: asyncio.subprocess.Process,
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    assert process.stdin is not None and process.stdout is not None
    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    process.stdin.write((json.dumps(request) + "\n").encode())
    await process.stdin.drain()
    async with asyncio.timeout(10):
        while True:
            raw = await process.stdout.readline()
            assert raw, "MCP subprocess exited before replying."
            message = json.loads(raw)
            assert message["jsonrpc"] == "2.0"
            if message.get("id") == request_id:
                assert "error" not in message, message
                return message["result"]


async def initialize_stdio(process: asyncio.subprocess.Process) -> None:
    result = await stdio_rpc(
        process,
        1,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "transport-tests", "version": "1"},
        },
    )
    assert result["protocolVersion"] == "2025-11-25"
    assert process.stdin is not None
    process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    await process.stdin.drain()
