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
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from calabiyau_map_engine_mcp.config import Credentials, Settings, load_credentials
from calabiyau_map_engine_mcp.server import create_runtime

TOOLS = {
    "list_sessions",
    "get_state",
    "list_items",
    "list_actors",
    "select_actors",
    "place_actor",
    "move_actors",
    "delete_actors",
    "undo",
    "redo",
}
ORIGIN = "http://127.0.0.1:5173"
BROWSER_ID = "test-browser-session"
MAP_ID = "test-map-session"
ROOT = Path(__file__).resolve().parents[1]


def credentials_value() -> Credentials:
    return Credentials(pairing_token="test-pairing-" + "p" * 32, http_token="test-http-" + "h" * 32)


@asynccontextmanager
async def live_runtime(transport: str = "all"):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    settings = Settings(credentials=credentials_value(), transport=transport, port=port)
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


@asynccontextmanager
async def protocol_client(base: str, token: str, transport: str):
    headers = {"Authorization": f"Bearer {token}"}
    if transport == "sse":
        async with Client(sse_client(f"{base}/sse", headers=headers), mode="legacy") as client:
            yield client
    else:
        async with httpx2.AsyncClient(headers=headers, trust_env=False) as http:
            stream = streamable_http_client(f"{base}/mcp", http_client=http)
            mode = "legacy" if transport == "legacy-http" else "auto"
            async with Client(stream, mode=mode) as client:
                yield client


@asynccontextmanager
async def fake_browser(base: str, token: str, *, hold_requests: bool = False):
    state = {"map_session_id": MAP_ID, "revision": 0, "map": {"id": "map", "name": "Test"}}
    dispatched: list[dict[str, Any]] = []
    async with connect(base.replace("http://", "ws://") + "/bridge", origin=ORIGIN) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "version": 1,
                    "token": token,
                    "session_id": BROWSER_ID,
                    "label": "Transport test browser",
                }
            )
        )
        welcome = json.loads(await ws.recv())
        assert welcome == {"type": "welcome", "version": 1, "session_id": BROWSER_ID}

        async def respond() -> None:
            try:
                async for raw in ws:
                    request = json.loads(raw)
                    if request["type"] == "ping":
                        await ws.send(json.dumps({"type": "pong", "version": 1}))
                        continue
                    assert request["type"] == "request"
                    dispatched.append(request)
                    if hold_requests:
                        continue
                    params = request["params"]
                    response: dict[str, Any] = {
                        "type": "response",
                        "version": 1,
                        "id": request["id"],
                    }
                    if request["action"] == "place_actor":
                        if params["expected_revision"] != state["revision"]:
                            response.update(
                                ok=False,
                                error={"code": "REVISION_CONFLICT", "message": "Map changed."},
                            )
                        else:
                            state["revision"] += 1
                            response.update(
                                ok=True,
                                result={
                                    **state,
                                    "changed": True,
                                    "actor": {"editor_id": 42, "cell": params["cell"]},
                                },
                            )
                    else:
                        response.update(ok=True, result=dict(state))
                    await ws.send(json.dumps(response))
            except ConnectionClosed:
                return

        task = asyncio.create_task(respond())
        try:
            yield state, dispatched
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.parametrize("transport", ["modern", "legacy-http", "sse"])
async def test_http_transports_discover_tools_and_return_browser_errors(transport: str) -> None:
    async with live_runtime() as (runtime, base):
        async with protocol_client(
            base, runtime.settings.credentials.http_token, transport
        ) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == TOOLS
            sessions = await client.call_tool("list_sessions", {})
            assert not sessions.is_error
            assert sessions.structured_content == {"sessions": []}
            result = await client.call_tool("get_state", {"browser_session_id": "missing"})
            assert result.is_error
            assert result.structured_content["error"]["code"] == "BROWSER_NOT_CONNECTED"


@pytest.mark.parametrize("transport", ["modern", "legacy-http", "sse"])
async def test_http_transport_browser_roundtrip_and_progress(transport: str) -> None:
    async with live_runtime() as (runtime, base):
        async with fake_browser(base, runtime.settings.credentials.pairing_token) as (
            _,
            dispatched,
        ):
            async with protocol_client(
                base, runtime.settings.credentials.http_token, transport
            ) as client:
                progress_values: list[float] = []

                async def progress(value: float, total: float | None, message: str | None) -> None:
                    progress_values.append(value)

                sessions = await client.call_tool("list_sessions", {})
                session = sessions.structured_content["sessions"][0]
                assert session["browser_session_id"] == BROWSER_ID
                result = await client.call_tool(
                    "get_state", {"browser_session_id": BROWSER_ID}, progress_callback=progress
                )
                assert not result.is_error
                assert result.structured_content["map_session_id"] == MAP_ID
                assert progress_values == [0.0, 1.0]
                assert dispatched[-1]["params"] == {}
                assert dispatched[-1]["action"] == "get_state"


async def test_http_and_sse_share_browser_registry_and_revision() -> None:
    async with live_runtime() as (runtime, base):
        async with fake_browser(base, runtime.settings.credentials.pairing_token):
            async with protocol_client(
                base, runtime.settings.credentials.http_token, "modern"
            ) as modern:
                async with protocol_client(
                    base, runtime.settings.credentials.http_token, "sse"
                ) as legacy:
                    modified = await modern.call_tool(
                        "place_actor",
                        {
                            "browser_session_id": BROWSER_ID,
                            "map_session_id": MAP_ID,
                            "expected_revision": 0,
                            "item_id": 1,
                            "cell": [1, 2, 3],
                        },
                    )
                    assert not modified.is_error
                    assert modified.structured_content["revision"] == 1
                    observed = await legacy.call_tool(
                        "get_state", {"browser_session_id": BROWSER_ID}
                    )
                    assert observed.structured_content["revision"] == 1
                    stale = await legacy.call_tool(
                        "place_actor",
                        {
                            "browser_session_id": BROWSER_ID,
                            "map_session_id": MAP_ID,
                            "expected_revision": 0,
                            "item_id": 1,
                            "cell": [4, 5, 6],
                        },
                    )
                    assert stale.is_error
                    assert stale.structured_content["error"]["code"] == "REVISION_CONFLICT"


async def test_modern_http_cancelled_call_cleans_pending_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with live_runtime() as (runtime, base):
        started = asyncio.Event()
        finished = asyncio.Event()
        original_dispatch = runtime.registry._dispatch

        async def tracked_dispatch(*args: Any, **kwargs: Any) -> dict[str, Any]:
            started.set()
            try:
                return await original_dispatch(*args, **kwargs)
            finally:
                finished.set()

        monkeypatch.setattr(runtime.registry, "_dispatch", tracked_dispatch)
        async with fake_browser(
            base, runtime.settings.credentials.pairing_token, hold_requests=True
        ):
            async with protocol_client(
                base, runtime.settings.credentials.http_token, "modern"
            ) as client:
                call = asyncio.create_task(
                    client.call_tool("get_state", {"browser_session_id": BROWSER_ID})
                )
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    assert runtime.registry.pending
                    call.cancel()
                    with suppress(asyncio.CancelledError):
                        await call
                    await asyncio.wait_for(finished.wait(), 5)
                    assert not runtime.registry.pending
                    assert BROWSER_ID in runtime.registry.browsers
                finally:
                    call.cancel()
                    with suppress(asyncio.CancelledError):
                        await call


@pytest.mark.parametrize(
    "invalid",
    [
        {"expected_revision": True},
        {"cell": [1.5, 2, 3]},
        {"rotation_index": 4},
        {"cell": [1, 2]},
        {"color_linear_rgb": ["NaN", 1, 1]},
        {"color_linear_rgb": ["Infinity", 1, 1]},
    ],
)
async def test_transport_rejects_invalid_write_parameters_before_dispatch(
    invalid: dict[str, Any],
) -> None:
    async with live_runtime() as (runtime, base):
        async with fake_browser(base, runtime.settings.credentials.pairing_token) as (
            _,
            dispatched,
        ):
            async with protocol_client(
                base, runtime.settings.credentials.http_token, "modern"
            ) as client:
                result = await client.call_tool(
                    "place_actor",
                    {
                        "browser_session_id": BROWSER_ID,
                        "map_session_id": MAP_ID,
                        "expected_revision": 0,
                        "item_id": 1,
                        "cell": [1, 2, 3],
                        **invalid,
                    },
                )
                assert result.is_error
                assert dispatched == []


@pytest.mark.parametrize(
    ("transport", "disabled_paths"),
    [
        ("stdio", ["/mcp", "/sse"]),
        ("sse", ["/mcp"]),
        ("streamable-http", ["/sse"]),
    ],
)
async def test_disabled_transports_are_not_exposed(
    transport: str, disabled_paths: list[str]
) -> None:
    async with live_runtime(transport) as (runtime, base):
        async with httpx2.AsyncClient(
            base_url=base,
            headers={"Authorization": f"Bearer {runtime.settings.credentials.http_token}"},
            trust_env=False,
        ) as client:
            assert (await client.get("/healthz")).status_code == 200
            for path in disabled_paths:
                assert (await client.get(path)).status_code == 404


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


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


@pytest.mark.parametrize("transport", ["stdio", "all"])
async def test_stdio_subprocess_stdout_is_protocol_only_and_eof_exits(
    tmp_path: Path, transport: str
) -> None:
    async with cli_process(tmp_path, transport, unused_port()) as process:
        await initialize_stdio(process)
        tools = await stdio_rpc(process, 2, "tools/list", {})
        assert {tool["name"] for tool in tools["tools"]} == TOOLS
        result = await stdio_rpc(
            process, 3, "tools/call", {"name": "list_sessions", "arguments": {}}
        )
        assert result["structuredContent"] == {"sessions": []}
        assert process.stdin is not None and process.stdout is not None
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 10)
        assert process.returncode == 0
        assert await process.stdout.read() == b""


async def test_all_subprocess_stdio_http_and_sse_use_same_browser(tmp_path: Path) -> None:
    credentials = load_credentials(tmp_path)
    port = unused_port()
    base = f"http://127.0.0.1:{port}"
    async with cli_process(tmp_path, "all", port) as process:
        await initialize_stdio(process)
        async with fake_browser(base, credentials.pairing_token):
            async with protocol_client(base, credentials.http_token, "modern") as modern:
                async with protocol_client(base, credentials.http_token, "sse") as legacy:
                    stdio_sessions, http_sessions = await asyncio.gather(
                        stdio_rpc(
                            process, 2, "tools/call", {"name": "list_sessions", "arguments": {}}
                        ),
                        modern.call_tool("list_sessions", {}),
                    )
                    assert (
                        stdio_sessions["structuredContent"]["sessions"]
                        == http_sessions.structured_content["sessions"]
                    )
                    modified = await stdio_rpc(
                        process,
                        3,
                        "tools/call",
                        {
                            "name": "place_actor",
                            "arguments": {
                                "browser_session_id": BROWSER_ID,
                                "map_session_id": MAP_ID,
                                "expected_revision": 0,
                                "item_id": 1,
                                "cell": [1, 2, 3],
                            },
                        },
                    )
                    assert not modified.get("isError", False)
                    modern_state, legacy_state = await asyncio.gather(
                        modern.call_tool("get_state", {"browser_session_id": BROWSER_ID}),
                        legacy.call_tool("get_state", {"browser_session_id": BROWSER_ID}),
                    )
                    assert modern_state.structured_content["revision"] == 1
                    assert legacy_state.structured_content["revision"] == 1
        assert process.stdin is not None and process.stdout is not None
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 10)
        assert process.returncode == 0
        assert await process.stdout.read() == b""


async def test_debug_mode_does_not_log_pairing_or_bearer_tokens(tmp_path: Path) -> None:
    credentials = load_credentials(tmp_path)
    port = unused_port()
    base = f"http://127.0.0.1:{port}"
    async with cli_process(tmp_path, "all", port, log_level="debug") as process:
        await initialize_stdio(process)
        async with fake_browser(base, credentials.pairing_token):
            async with protocol_client(base, credentials.http_token, "modern") as client:
                response = await client.call_tool("get_state", {"browser_session_id": BROWSER_ID})
                assert not response.is_error
        assert process.stdin is not None and process.stderr is not None
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 10)
        stderr = await process.stderr.read()
        assert process.returncode == 0
        assert credentials.pairing_token.encode() not in stderr
        assert credentials.http_token.encode() not in stderr


async def test_port_conflict_exits_before_stdio_and_logs_no_credentials(tmp_path: Path) -> None:
    credentials = load_credentials(tmp_path)
    with socket.socket() as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        async with cli_process(tmp_path, "all", listener.getsockname()[1]) as process:
            assert process.stdin is not None
            process.stdin.close()
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode != 0
            assert stdout == b""
            assert b"Startup failed" in stderr
            assert credentials.pairing_token.encode() not in stderr
            assert credentials.http_token.encode() not in stderr
