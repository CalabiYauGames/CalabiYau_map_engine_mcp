import asyncio
from dataclasses import replace

import pytest

from calabiyau_map_engine_mcp.bridge import (
    ACTIONS,
    WRITE_ACTIONS,
    BridgeError,
    Browser,
    BrowserRegistry,
)


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


@pytest.mark.parametrize(
    "first_action,second_action",
    [("move_actors", "delete_actors"), ("connect_actors", "disconnect_actors")],
)
async def test_every_write_waits_for_the_one_browser_lock(
    settings, first_action, second_action
) -> None:
    """Reads and writes share one editor, so no two writes may overlap."""
    registry, browser, socket = await connection(settings)
    first = asyncio.create_task(registry.call("tab", first_action, {}))
    request = await socket.sent.get()
    second = asyncio.create_task(registry.call("tab", second_action, {}))
    # The second write must stay queued while the first is unanswered.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(socket.sent.get(), timeout=0.05)
    registry.deliver(browser, reply(request))
    await first
    request = await socket.sent.get()
    assert request["action"] == second_action
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


def test_link_actions_are_whitelisted_with_the_expected_lock_class():
    assert {"list_links", "connect_actors", "disconnect_actors"} <= ACTIONS
    assert "list_links" not in WRITE_ACTIONS
    assert {"connect_actors", "disconnect_actors"} <= WRITE_ACTIONS


async def next_frame(socket):
    """Bounded read: a missing frame must fail the test, not hang the suite."""
    return await asyncio.wait_for(socket.sent.get(), timeout=1)


async def test_list_links_reaches_the_socket_while_a_write_holds_the_lock(settings):
    """The one read that shares the lock class of a write must still get through."""
    registry, browser, socket = await connection(settings)
    write = asyncio.create_task(registry.call("tab", "connect_actors", {}))
    held = await next_frame(socket)
    assert held["action"] == "connect_actors"
    read = asyncio.create_task(registry.call("tab", "list_links", {}))
    listed = await next_frame(socket)
    assert listed["action"] == "list_links"
    registry.deliver(browser, reply(listed, {"links": [], "total": 0}))
    assert await read == {"links": [], "total": 0}
    registry.deliver(browser, reply(held, {"changed": True}))
    assert await write == {"changed": True}
