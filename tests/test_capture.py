"""The HTTP capture road.

A screenshot cannot cross the bridge: the frame cap is 1 MiB and a 1600x1000
PNG of a dense model is several times that.  These tests pin the slot semantics
that make the detour safe -- single use, expiring, and refusing an unknown id --
without standing up a browser.
"""

from __future__ import annotations

import pytest
from harness import ORIGIN, credentials_value
from starlette.testclient import TestClient

from calabiyau_map_engine_mcp.capture import CaptureRelay
from calabiyau_map_engine_mcp.config import Settings
from calabiyau_map_engine_mcp.server import create_runtime


async def test_delivered_bytes_reach_the_waiter_once() -> None:
    relay = CaptureRelay()
    capture_id, url = relay.open("127.0.0.1:8765")
    assert url == f"http://127.0.0.1:8765/capture/{capture_id}"
    assert relay.deliver(capture_id, b"\x89PNG", "image/png") is True
    capture = await relay.wait(capture_id, 1)
    assert capture is not None
    assert capture.data == b"\x89PNG"
    assert capture.content_type == "image/png"


async def test_a_slot_accepts_one_upload_only() -> None:
    relay = CaptureRelay()
    capture_id, _ = relay.open("127.0.0.1:8765")
    assert relay.deliver(capture_id, b"first", "image/png") is True
    assert relay.deliver(capture_id, b"second", "image/png") is False


async def test_waiting_releases_the_slot_so_a_late_upload_is_refused() -> None:
    relay = CaptureRelay()
    capture_id, _ = relay.open("127.0.0.1:8765")
    relay.discard(capture_id)
    assert relay.deliver(capture_id, b"late", "image/png") is False
    assert await relay.wait(capture_id, 0.01) is None


async def test_an_unclaimed_slot_times_out_instead_of_hanging() -> None:
    relay = CaptureRelay()
    capture_id, _ = relay.open("127.0.0.1:8765")
    assert await relay.wait(capture_id, 0.01) is None
    assert relay.pending() == 0
    assert relay.deliver(capture_id, b"late", "image/png") is False


async def test_two_slots_do_not_cross() -> None:
    relay = CaptureRelay()
    first, _ = relay.open("127.0.0.1:8765")
    second, _ = relay.open("127.0.0.1:8765")
    assert first != second
    relay.deliver(second, b"second", "image/jpeg")
    relay.deliver(first, b"first", "image/png")
    assert (await relay.wait(first, 1)).data == b"first"
    assert (await relay.wait(second, 1)).data == b"second"


def test_capture_route_is_mounted_and_rejects_unknown_slots() -> None:
    settings = Settings(credentials=credentials_value(), transport="http", port=8765)
    runtime = create_runtime(settings)
    paths = {getattr(route, "path", "") for route in runtime.app.app.routes}
    assert "/capture/{capture_id}" in paths
    with TestClient(runtime.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/capture/not-a-real-slot",
            content=b"bytes",
            headers={"content-type": "image/png", "origin": ORIGIN},
        )
        assert response.status_code == 404
