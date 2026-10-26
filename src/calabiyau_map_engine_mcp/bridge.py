from __future__ import annotations

import asyncio
import secrets
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from starlette.websockets import WebSocket, WebSocketDisconnect

from .config import Settings

ACTIONS = frozenset(
    {
        "get_state",
        "list_items",
        "list_actors",
        "list_links",
        "select_actors",
        "place_actor",
        "move_actors",
        "delete_actors",
        "connect_actors",
        "disconnect_actors",
        "undo",
        "redo",
    }
)
WRITE_ACTIONS = ACTIONS - {"get_state", "list_items", "list_actors", "list_links"}


class BridgeError(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": self.details}


class Hello(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: str
    version: int
    token: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(default="Map Engine", max_length=128)


@dataclass(eq=False)
class Browser:
    session_id: str
    label: str
    origin: str
    socket: WebSocket
    connected_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_seen: float = field(default_factory=time.monotonic)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.socket.send_json(message)


@dataclass
class Pending:
    browser: Browser
    future: asyncio.Future[dict[str, Any]]


class BrowserRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.browsers: dict[str, Browser] = {}
        self.pending: dict[str, Pending] = {}

    def sessions(self) -> list[dict[str, str]]:
        return [
            {
                "browser_session_id": browser.session_id,
                "label": browser.label,
                "origin": browser.origin,
                "connected_at": browser.connected_at,
            }
            for browser in self.browsers.values()
        ]

    async def register(self, browser: Browser) -> None:
        previous = self.browsers.get(browser.session_id)
        if previous is not None:
            self.unregister(previous)
            with suppress(Exception):
                await previous.socket.close(code=1012, reason="Connection replaced")
        self.browsers[browser.session_id] = browser

    def unregister(self, browser: Browser) -> None:
        if self.browsers.get(browser.session_id) is browser:
            del self.browsers[browser.session_id]
        for pending in tuple(self.pending.values()):
            if pending.browser is browser and not pending.future.done():
                pending.future.set_exception(
                    BridgeError(
                        "BROWSER_DISCONNECTED",
                        "Browser disconnected; check current map state before retrying a change.",
                        outcome="unknown",
                    )
                )

    async def close(self) -> None:
        for browser in tuple(self.browsers.values()):
            self.unregister(browser)
            with suppress(Exception):
                await browser.socket.close(code=1001, reason="Server shutting down")

    async def call(self, session_id: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action not in ACTIONS:
            raise BridgeError("UNKNOWN_ACTION", "This action is not exposed by the bridge.")
        browser = self.browsers.get(session_id)
        if browser is None:
            raise BridgeError(
                "BROWSER_NOT_CONNECTED", "Select a connected browser using list_sessions."
            )
        try:
            async with asyncio.timeout(self.settings.request_timeout):
                if action in WRITE_ACTIONS:
                    async with browser.write_lock:
                        return await self._dispatch(browser, action, params)
                return await self._dispatch(browser, action, params)
        except TimeoutError as exc:
            raise BridgeError(
                "BROWSER_TIMEOUT",
                "Browser call timed out; a change may already have executed. Do not replay.",
                outcome="unknown",
            ) from exc

    async def _dispatch(
        self, browser: Browser, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if self.browsers.get(browser.session_id) is not browser:
            raise BridgeError("BROWSER_DISCONNECTED", "Browser connection changed before dispatch.")
        request_id = str(uuid.uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = Pending(browser, future)
        try:
            await browser.send(
                {
                    "version": 1,
                    "type": "request",
                    "id": request_id,
                    "action": action,
                    "params": params,
                }
            )
            return await future
        except (WebSocketDisconnect, OSError, RuntimeError) as exc:
            raise BridgeError(
                "BROWSER_DISCONNECTED", "Unable to confirm browser operation.", outcome="unknown"
            ) from exc
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # Consume a disconnect exception even when sending failed before awaiting it.
                future.exception()

    def deliver(self, browser: Browser, message: dict[str, Any]) -> None:
        if message.get("version") != 1:
            raise BridgeError("INVALID_MESSAGE", "Unsupported bridge protocol version.")
        if message.get("type") == "pong":
            return
        if message.get("type") != "response" or not isinstance(message.get("id"), str):
            raise BridgeError("INVALID_MESSAGE", "Expected a response or pong.")
        pending = self.pending.get(message["id"])
        if pending is None or pending.browser is not browser or pending.future.done():
            return
        if message.get("ok") is True and isinstance(message.get("result"), dict):
            pending.future.set_result(message["result"])
        elif message.get("ok") is False and isinstance(message.get("error"), dict):
            error = message["error"]
            code = error.get("code")
            text = error.get("message")
            if not isinstance(code, str) or not isinstance(text, str):
                raise BridgeError("INVALID_MESSAGE", "Malformed browser error.")
            pending.future.set_exception(
                BridgeError(
                    code[:128],
                    text[:2048],
                    browser_details=error.get("details"),
                )
            )
        else:
            raise BridgeError("INVALID_MESSAGE", "Malformed browser result.")


async def _heartbeat(browser: Browser) -> None:
    while True:
        await asyncio.sleep(15)
        if time.monotonic() - browser.last_seen > 45:
            await browser.socket.close(code=1001, reason="Heartbeat expired")
            return
        try:
            await browser.send({"version": 1, "type": "ping"})
        except (WebSocketDisconnect, RuntimeError, OSError):
            return


async def bridge_socket(socket: WebSocket, registry: BrowserRegistry) -> None:
    origin = socket.headers.get("origin")
    settings = registry.settings
    if origin not in settings.allowed_origins:
        await socket.close(code=1008, reason="Origin is not allowed")
        return
    await socket.accept()
    browser: Browser | None = None
    heartbeat: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(5):
            raw = await socket.receive_json()
            hello = Hello.model_validate(raw)
        if hello.type != "hello" or hello.version != 1:
            await socket.close(code=1008, reason="Invalid hello")
            return
        if not secrets.compare_digest(hello.token, settings.credentials.pairing_token):
            await socket.close(code=1008, reason="Invalid pairing token")
            return
        browser = Browser(hello.session_id, hello.label, origin, socket)
        await registry.register(browser)
        await browser.send({"version": 1, "type": "welcome", "session_id": hello.session_id})
        heartbeat = asyncio.create_task(_heartbeat(browser))
        while True:
            message = await socket.receive_json()
            if not isinstance(message, dict):
                raise BridgeError("INVALID_MESSAGE", "Expected a JSON object.")
            browser.last_seen = time.monotonic()
            registry.deliver(browser, message)
    except WebSocketDisconnect:
        pass
    except (ValueError, TypeError, TimeoutError, BridgeError):
        with suppress(Exception):
            await socket.close(code=1008, reason="Invalid bridge message or authentication timeout")
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        if browser is not None:
            registry.unregister(browser)
