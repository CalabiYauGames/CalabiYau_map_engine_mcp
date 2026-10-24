import asyncio
from dataclasses import replace

import pytest

from calabiyau_map_engine_mcp.bridge import BridgeError, Browser, BrowserRegistry


class FakeSocket:
    def __init__(self):
        self.sent = asyncio.Queue()
        self.closed = False

    async def send_json(self, message):
        await self.sent.put(message)

    async def close(self, **kwargs):
        self.closed = True


async def connection(settings, name="tab"):
    registry = BrowserRegistry(settings)
    socket = FakeSocket()
    browser = Browser(name, "Test editor", "http://127.0.0.1:5173", socket)
    await registry.register(browser)
    return registry, browser, socket


def reply(request, result=None):
    return {
        "version": 1,
        "type": "response",
        "id": request["id"],
        "ok": True,
        "result": result or {"revision": 1},
    }


async def test_dispatch_and_correlation_do_not_reuse_client_ids(settings):
    registry, browser, socket = await connection(settings)
    first = asyncio.create_task(registry.call("tab", "get_state", {}))
    second = asyncio.create_task(registry.call("tab", "get_state", {}))
    requests = [await socket.sent.get(), await socket.sent.get()]
    assert requests[0]["id"] != requests[1]["id"]
    registry.deliver(browser, reply(requests[1], {"order": 2}))
    registry.deliver(browser, reply(requests[0], {"order": 1}))
    assert await first == {"order": 1}
    assert await second == {"order": 2}
    registry.deliver(browser, reply(requests[0]))
    assert registry.pending == {}


async def test_wrong_browser_cannot_complete_request(settings):
    registry, browser, socket = await connection(settings)
    other = Browser("other", "Other", browser.origin, FakeSocket())
    task = asyncio.create_task(registry.call("tab", "get_state", {}))
    request = await socket.sent.get()
    registry.deliver(other, reply(request))
    assert not task.done()
    registry.deliver(browser, reply(request))
    assert await task == {"revision": 1}


async def test_all_mutating_transports_share_one_browser_lock(settings):
    registry, browser, socket = await connection(settings)
    first = asyncio.create_task(registry.call("tab", "move_actors", {}))
    request = await socket.sent.get()
    second = asyncio.create_task(registry.call("tab", "delete_actors", {}))
    await asyncio.sleep(0.01)
    assert socket.sent.empty()
    registry.deliver(browser, reply(request))
    await first
    request = await socket.sent.get()
    assert request["action"] == "delete_actors"
    registry.deliver(browser, reply(request))
    await second


async def test_timeout_and_cancellation_do_not_replay(settings):
    registry, _, socket = await connection(replace(settings, request_timeout=0.03))
    with pytest.raises(BridgeError, match="timed out") as caught:
        await registry.call("tab", "move_actors", {})
    assert caught.value.details["outcome"] == "unknown"
    assert socket.sent.qsize() == 1
    assert registry.pending == {}
    task = asyncio.create_task(registry.call("tab", "get_state", {}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert registry.pending == {}


async def test_reconnect_fails_old_pending_without_replay(settings):
    registry, browser, socket = await connection(settings)
    task = asyncio.create_task(registry.call("tab", "place_actor", {}))
    await socket.sent.get()
    replacement = Browser("tab", "Reconnected", browser.origin, FakeSocket())
    await registry.register(replacement)
    with pytest.raises(BridgeError) as caught:
        await task
    assert caught.value.code == "BROWSER_DISCONNECTED"
    assert socket.closed
    registry.unregister(browser)
    assert registry.browsers["tab"] is replacement
    await registry.close()
    assert registry.browsers == {}


async def test_browser_errors_become_structured_errors(settings):
    registry, browser, socket = await connection(settings)
    task = asyncio.create_task(registry.call("tab", "move_actors", {}))
    request = await socket.sent.get()
    registry.deliver(
        browser,
        {
            "version": 1,
            "type": "response",
            "id": request["id"],
            "ok": False,
            "error": {
                "code": "REVISION_CONFLICT",
                "message": "State changed",
                "details": {"revision": 2},
            },
        },
    )
    with pytest.raises(BridgeError) as caught:
        await task
    assert caught.value.code == "REVISION_CONFLICT"


async def test_no_generic_action_and_no_implicit_browser_selection(settings):
    registry, _, _ = await connection(settings)
    with pytest.raises(BridgeError) as caught:
        await registry.call("tab", "eval", {"code": "evil"})
    assert caught.value.code == "UNKNOWN_ACTION"
    with pytest.raises(BridgeError) as caught:
        await registry.call("missing", "get_state", {})
    assert caught.value.code == "BROWSER_NOT_CONNECTED"
