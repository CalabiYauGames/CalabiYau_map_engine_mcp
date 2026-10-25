from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from calabiyau_map_engine_mcp.tools import create_mcp


class FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def call(self, session_id: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((session_id, action, params))
        return {"action": action}

    def sessions(self) -> list[dict[str, str]]:
        return []


class StubContext:
    async def report_progress(self, *args: Any, **kwargs: Any) -> None:
        return None


async def call_tool(registry: FakeRegistry, name: str, arguments: dict[str, Any]) -> Any:
    return await create_mcp(registry).call_tool(name, arguments, context=StubContext())


async def test_create_mcp_registers_thirteen_tools_with_link_annotations() -> None:
    tools = {tool.name: tool for tool in await create_mcp(FakeRegistry()).list_tools()}
    assert len(tools) == 13
    assert tools["list_links"].annotations.read_only_hint is True
    assert tools["connect_actors"].annotations.read_only_hint is False
    assert tools["disconnect_actors"].annotations.read_only_hint is False


async def test_list_links_omits_absent_optional_parameters() -> None:
    registry = FakeRegistry()
    await call_tool(registry, "list_links", {"browser_session_id": "tab"})
    assert registry.calls == [("tab", "list_links", {"offset": 0, "limit": 100})]


async def test_list_links_forwards_filters_and_snapshot_pins() -> None:
    registry = FakeRegistry()
    await call_tool(
        registry,
        "list_links",
        {
            "browser_session_id": "tab",
            "actor_ids": [7, 3],
            "kind": "group",
            "offset": 2,
            "limit": 10,
            "map_session_id": "map",
            "expected_revision": 4,
        },
    )
    assert registry.calls == [
        (
            "tab",
            "list_links",
            {
                "offset": 2,
                "limit": 10,
                "actor_ids": [7, 3],
                "kind": "group",
                "map_session_id": "map",
                "expected_revision": 4,
            },
        )
    ]


async def test_connect_actors_preserves_child_order() -> None:
    registry = FakeRegistry()
    await call_tool(
        registry,
        "connect_actors",
        {
            "browser_session_id": "tab",
            "map_session_id": "map",
            "expected_revision": 9,
            "main_actor_id": 3,
            "child_actor_ids": [8, 4, 6],
        },
    )
    assert registry.calls == [
        (
            "tab",
            "connect_actors",
            {
                "map_session_id": "map",
                "expected_revision": 9,
                "main_actor_id": 3,
                "child_actor_ids": [8, 4, 6],
            },
        )
    ]


async def test_disconnect_actors_forwards_actor_ids() -> None:
    registry = FakeRegistry()
    await call_tool(
        registry,
        "disconnect_actors",
        {
            "browser_session_id": "tab",
            "map_session_id": "map",
            "expected_revision": 5,
            "actor_ids": [11],
        },
    )
    assert registry.calls == [
        (
            "tab",
            "disconnect_actors",
            {
                "map_session_id": "map",
                "expected_revision": 5,
                "actor_ids": [11],
            },
        )
    ]


async def test_empty_child_actor_ids_are_rejected_before_dispatch() -> None:
    registry = FakeRegistry()
    with pytest.raises(ToolError):
        await call_tool(
            registry,
            "connect_actors",
            {
                "browser_session_id": "tab",
                "map_session_id": "map",
                "expected_revision": 1,
                "main_actor_id": 3,
                "child_actor_ids": [],
            },
        )
    assert registry.calls == []
