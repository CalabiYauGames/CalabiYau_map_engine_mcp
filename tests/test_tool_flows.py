"""Every tool, driven through a real MCP client, with the effect read back.

The rule for this module: a test may not assert on the MCP result alone. A write is
only "done" when a following `list_actors` / `select_actors` (or `list_links` where the
link is the thing that changed) reports the new state. That is the difference between
"the server accepted the call" and "the editor applied it" — and it is the only check
that would catch a pass-through that drops or reshapes a parameter.

Each test starts a real uvicorn, pairs a stateful fake editor over the real WebSocket
bridge, and talks JSON-RPC with the SDK's own client. Nothing is stubbed below the
protocol line.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fake_editor import FakeEditor, editor_bridge
from harness import (
    BROWSER_ID,
    MAP_ID,
    call,
    cli_process,
    initialize_stdio,
    live_runtime,
    mcp_client,
    rejected,
    stdio_rpc,
    unused_port,
)

from calabiyau_map_engine_mcp.config import load_credentials

TOOLS = {
    "list_sessions",
    "get_state",
    "list_items",
    "list_actors",
    "list_links",
    "connect_actors",
    "disconnect_actors",
    "select_actors",
    "place_actor",
    "move_actors",
    "delete_actors",
    "undo",
    "redo",
}
WRITE_TOOLS = TOOLS - {"list_sessions", "get_state", "list_items", "list_actors", "list_links"}


@asynccontextmanager
async def flow(
    transport: str = "modern",
    *,
    request_timeout: float = 30.0,
    hold: frozenset[str] = frozenset(),
    **editor: Any,
):
    async with live_runtime(request_timeout=request_timeout) as (runtime, base):
        fake = FakeEditor(**editor)
        async with editor_bridge(base, runtime.settings.credentials.pairing_token, fake, hold=hold):
            async with mcp_client(
                base, runtime.settings.credentials.http_token, transport
            ) as client:
                yield client, fake


def browser_details(error: dict[str, Any]) -> dict[str, Any]:
    """Browser detail payloads arrive nested under the bridge's own details key."""
    return (error.get("details") or {}).get("browser_details") or {}


async def snapshot(client) -> dict[str, Any]:
    return await call(client, "get_state", browser_session_id=BROWSER_ID)


async def actors(client, **filters: Any) -> dict[str, Any]:
    return await call(client, "list_actors", browser_session_id=BROWSER_ID, **filters)


async def links(client, **filters: Any) -> dict[str, Any]:
    return await call(client, "list_links", browser_session_id=BROWSER_ID, **filters)


async def place(client, item_id: int, cell: list[int], **extra: Any) -> dict[str, Any]:
    current = await snapshot(client)
    return await call(
        client,
        "place_actor",
        browser_session_id=BROWSER_ID,
        map_session_id=current["map_session_id"],
        expected_revision=current["revision"],
        item_id=item_id,
        cell=cell,
        **extra,
    )


async def write(client, name: str, **arguments: Any) -> dict[str, Any]:
    current = await snapshot(client)
    return await call(
        client,
        name,
        browser_session_id=BROWSER_ID,
        map_session_id=current["map_session_id"],
        expected_revision=current["revision"],
        **arguments,
    )


def placed_id(result: dict[str, Any]) -> int:
    return result["actor"]["editor_id"]


async def test_every_tool_reaches_the_editor_over_one_connection() -> None:
    """The coverage tripwire: no tool may exist without a real request behind it."""
    async with flow() as (client, fake):
        await call(client, "list_sessions")
        await call(client, "get_state", browser_session_id=BROWSER_ID)
        await call(client, "list_items", browser_session_id=BROWSER_ID)
        await call(client, "list_actors", browser_session_id=BROWSER_ID)
        await call(client, "list_links", browser_session_id=BROWSER_ID)

        main = placed_id(await place(client, 2, [0, 0, 0]))
        child = placed_id(await place(client, 3, [1, 0, 0]))
        await write(client, "select_actors", actor_ids=[child])
        await write(client, "move_actors", actor_ids=[child], delta=[0, 1, 0])
        await write(client, "connect_actors", main_actor_id=main, child_actor_ids=[child])
        await write(client, "disconnect_actors", actor_ids=[child])
        await write(client, "delete_actors", actor_ids=[child])
        await write(client, "undo")
        await write(client, "redo")

        reached = {frame["action"] for frame in fake.received}
        # list_sessions is answered from the registry, so it is the one tool that must
        # never put a frame on the bridge.
        assert reached == TOOLS - {"list_sessions"}


# --------------------------------------------------------------------------- reads


async def test_list_sessions_and_get_state_describe_the_paired_editor() -> None:
    async with flow() as (client, fake):
        sessions = await call(client, "list_sessions")
        assert [session["browser_session_id"] for session in sessions["sessions"]] == [BROWSER_ID]
        assert sessions["sessions"][0]["label"] == "Stateful test editor"

        state = await snapshot(client)
        assert state["map_session_id"] == MAP_ID
        assert state["revision"] == 0
        assert state["grid"] == {"x": 8, "y": 8, "z": 4}
        assert state["editing"] is True and state["read_only"] is False
        assert state["persistent_actor_count"] == 0

        listed = await actors(client, map_session_id=MAP_ID, expected_revision=0)
        assert listed["actors"] == [] and listed["total"] == 0
        assert listed["map_session_id"] == MAP_ID and listed["revision"] == 0
        # The paging defaults are part of the contract, not an implementation detail.
        assert (listed["offset"], listed["limit"]) == (0, 100)


async def test_list_items_pages_the_catalog_and_returns_a_placeable_item() -> None:
    async with flow() as (client, fake):
        first_page = await call(client, "list_items", browser_session_id=BROWSER_ID, limit=2)
        assert first_page["total"] == 4
        assert [item["item_id"] for item in first_page["items"]] == [1, 2]
        assert first_page["items"][1]["child_item_id"] == 3
        assert all(item["placeable"] for item in first_page["items"])

        second_page = await call(
            client, "list_items", browser_session_id=BROWSER_ID, offset=2, limit=2
        )
        assert [item["item_id"] for item in second_page["items"]] == [3, 6]
        assert second_page["total"] == 4

        tracks = await call(client, "list_items", browser_session_id=BROWSER_ID, query="track")
        assert [item["item_id"] for item in tracks["items"]] == [2, 3]

        placed = await place(client, second_page["items"][0]["item_id"], [0, 0, 0])
        listed = await actors(client, expected_revision=placed["revision"])
        assert [actor["item_id"] for actor in listed["actors"]] == [3]
        assert listed["actors"][0]["editor_id"] == placed_id(placed)


async def test_list_actors_pages_and_filters_by_item_and_selection() -> None:
    async with flow() as (client, fake):
        walls = [placed_id(await place(client, 1, [x, 0, 0])) for x in range(3)]
        track = placed_id(await place(client, 3, [0, 1, 0]))

        page = await actors(client, limit=2)
        assert page["total"] == 4 and len(page["actors"]) == 2
        assert [actor["editor_id"] for actor in page["actors"]] == walls[:2]

        only_tracks = await actors(client, item_id=3)
        assert [actor["editor_id"] for actor in only_tracks["actors"]] == [track]

        await write(client, "select_actors", actor_ids=[walls[0], track])
        selected = await actors(client, selected_only=True)
        assert [actor["editor_id"] for actor in selected["actors"]] == [walls[0], track]
        assert selected["total"] == 2


# -------------------------------------------------------------------------- writes


async def test_place_actor_is_readable_through_list_actors_and_select_actors() -> None:
    async with flow() as (client, fake):
        placed = await place(
            client, 1, [1, 2, 0], rotation_index=2, color_linear_rgb=[0.2, 0.4, 0.6]
        )
        editor_id = placed_id(placed)
        assert placed["actor"]["cell"] == [1, 2, 0]
        assert placed["actor"]["transform"]["rotation_index"] == 2
        assert placed["changed"] is True

        listed = await actors(
            client,
            map_session_id=placed["map_session_id"],
            expected_revision=placed["revision"],
        )
        assert listed["total"] == 1
        actor = listed["actors"][0]
        assert actor["editor_id"] == editor_id
        assert actor["cell"] == [1, 2, 0]
        assert actor["color_linear_rgb"] == pytest.approx([0.2, 0.4, 0.6])
        assert actor["transform"]["rotation_index"] == 2
        assert actor["selected"] is False

        selected = await write(client, "select_actors", actor_ids=[editor_id])
        assert selected["selection"] == [editor_id]
        only = await actors(client, selected_only=True, expected_revision=selected["revision"])
        assert [entry["editor_id"] for entry in only["actors"]] == [editor_id]
        assert only["actors"][0]["selected"] is True


async def test_place_actor_rejections_leave_the_map_untouched() -> None:
    async with flow() as (client, fake):
        first = await place(client, 1, [3, 3, 0])
        editor_id = placed_id(first)

        occupied = await rejected(
            client,
            "place_actor",
            browser_session_id=BROWSER_ID,
            map_session_id=first["map_session_id"],
            expected_revision=first["revision"],
            item_id=1,
            cell=[3, 3, 0],
        )
        assert occupied["code"] == "CELL_OCCUPIED"
        assert browser_details(occupied)["occupants"][0]["editor_id"] == editor_id

        out_of_bounds = await rejected(
            client,
            "place_actor",
            browser_session_id=BROWSER_ID,
            map_session_id=first["map_session_id"],
            expected_revision=first["revision"],
            item_id=1,
            cell=[8, 0, 0],
        )
        assert out_of_bounds["code"] == "OUT_OF_BOUNDS"

        pair_kit = await rejected(
            client,
            "place_actor",
            browser_session_id=BROWSER_ID,
            map_session_id=first["map_session_id"],
            expected_revision=first["revision"],
            item_id=4,
            cell=[4, 4, 0],
        )
        assert pair_kit["code"] == "ITEM_NOT_PLACEABLE"

        listed = await actors(client, expected_revision=first["revision"])
        assert [actor["editor_id"] for actor in listed["actors"]] == [editor_id]


async def test_move_actors_updates_cells_and_refuses_a_collision() -> None:
    async with flow() as (client, fake):
        mover = placed_id(await place(client, 1, [0, 0, 0]))
        blocker = placed_id(await place(client, 1, [1, 0, 0]))

        moved = await write(client, "move_actors", actor_ids=[mover], delta=[0, 1, 0])
        assert moved["actors"][0]["cell"] == [0, 1, 0]
        assert moved["changed"] is True

        listed = await actors(client, expected_revision=moved["revision"])
        cells = {actor["editor_id"]: actor["cell"] for actor in listed["actors"]}
        assert cells == {mover: [0, 1, 0], blocker: [1, 0, 0]}

        blocked = await rejected(
            client,
            "move_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=moved["map_session_id"],
            expected_revision=moved["revision"],
            actor_ids=[mover],
            delta=[1, -1, 0],
        )
        assert blocked["code"] == "MOVE_REJECTED"

        still = await actors(client, expected_revision=moved["revision"])
        assert {actor["editor_id"]: actor["cell"] for actor in still["actors"]} == cells


async def test_delete_actors_removes_exactly_the_reported_ids() -> None:
    async with flow() as (client, fake):
        keep = placed_id(await place(client, 1, [0, 0, 0]))
        first = placed_id(await place(client, 1, [1, 0, 0]))
        second = placed_id(await place(client, 1, [2, 0, 0]))

        deleted = await write(client, "delete_actors", actor_ids=[first, second])
        assert sorted(deleted["deleted_actor_ids"]) == [first, second]

        listed = await actors(client, expected_revision=deleted["revision"])
        assert [actor["editor_id"] for actor in listed["actors"]] == [keep]
        assert listed["total"] == 1

        again = await rejected(
            client,
            "delete_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=deleted["map_session_id"],
            expected_revision=deleted["revision"],
            actor_ids=[first],
        )
        assert again["code"] == "ACTOR_NOT_FOUND"


async def test_connect_actors_builds_an_ordered_group_visible_afterwards() -> None:
    async with flow() as (client, fake):
        main = placed_id(await place(client, 2, [0, 0, 0]))
        near = placed_id(await place(client, 3, [1, 0, 0]))
        far = placed_id(await place(client, 3, [2, 0, 0]))

        linked = await write(
            client, "connect_actors", main_actor_id=main, child_actor_ids=[far, near]
        )
        assert linked["links"] == [
            {"kind": "group", "main_actor_id": main, "child_actor_ids": [far, near]}
        ]
        assert linked["changed"] is True

        listed = await links(client, kind="group", expected_revision=linked["revision"])
        assert listed["links"][0]["child_actor_ids"] == [far, near]

        present = await actors(client, expected_revision=listed["revision"])
        assert {actor["editor_id"] for actor in present["actors"]} == {main, near, far}
        assert all(actor["editable"] for actor in present["actors"])


async def test_connect_actors_rejects_bad_groups_without_touching_links() -> None:
    async with flow() as (client, fake):
        plain = placed_id(await place(client, 1, [0, 0, 0]))
        main = placed_id(await place(client, 2, [1, 0, 0]))
        child = placed_id(await place(client, 3, [2, 0, 0]))
        current = await snapshot(client)

        not_connectable = await rejected(
            client,
            "connect_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            main_actor_id=plain,
            child_actor_ids=[child],
        )
        assert not_connectable["code"] == "ITEM_NOT_CONNECTABLE"

        mismatch = await rejected(
            client,
            "connect_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            main_actor_id=main,
            child_actor_ids=[plain],
        )
        assert mismatch["code"] == "CHILD_ITEM_MISMATCH"
        assert browser_details(mismatch)["expected_item_id"] == 3

        # Item 8 is a child of the gate frame *and* half of a pair kit, which is the one
        # shape that reaches the pair check instead of failing the item check first.
        frame = placed_id(await place(client, 6, [3, 0, 0]))
        left, right = fake.seed_pair([5, 5, 0], left_item=8, right_item=9)
        current = await snapshot(client)
        in_pair = await rejected(
            client,
            "connect_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            main_actor_id=frame,
            child_actor_ids=[left],
        )
        assert in_pair["code"] == "ACTOR_IN_PAIR"

        listed = await links(client)
        assert listed["links"] == [
            {"kind": "pair", "main_actor_id": left, "child_actor_ids": [right]}
        ]


async def test_disconnect_actors_splits_groups_and_drops_pairs() -> None:
    async with flow() as (client, fake):
        main = placed_id(await place(client, 2, [0, 0, 0]))
        first = placed_id(await place(client, 3, [1, 0, 0]))
        second = placed_id(await place(client, 3, [2, 0, 0]))
        third = placed_id(await place(client, 3, [3, 0, 0]))
        fourth = placed_id(await place(client, 3, [4, 0, 0]))
        await write(
            client,
            "connect_actors",
            main_actor_id=main,
            child_actor_ids=[first, second, third, fourth],
        )

        cut = await write(client, "disconnect_actors", actor_ids=[second])
        assert {"kind": "group", "main_actor_id": main, "child_actor_ids": [first]} in cut["links"]
        assert {"kind": "orphan", "main_actor_id": None, "child_actor_ids": [third, fourth]} in cut[
            "links"
        ]

        survivors = await actors(client, expected_revision=cut["revision"])
        assert {actor["editor_id"] for actor in survivors["actors"]} == {
            main,
            first,
            second,
            third,
            fourth,
        }

        left, right = fake.seed_pair([6, 6, 0])
        current = await snapshot(client)
        dropped = await call(
            client,
            "disconnect_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            actor_ids=[left],
        )
        # A dropped pair leaves no link behind, so the reported set is empty; the pair
        # disappearing is what list_links below proves.
        assert dropped["links"] == []
        assert dropped["changed"] is True

        pairs = await links(client, kind="pair", expected_revision=dropped["revision"])
        assert pairs["links"] == []

        kept = await actors(client, expected_revision=dropped["revision"])
        assert {left, right} <= {actor["editor_id"] for actor in kept["actors"]}

        lonely = placed_id(await place(client, 1, [7, 7, 0]))
        noop = await write(client, "disconnect_actors", actor_ids=[lonely])
        assert noop["changed"] is False
        assert noop["links"] == []


async def test_select_actors_modes_are_visible_in_list_actors() -> None:
    async with flow() as (client, fake):
        ids = [placed_id(await place(client, 1, [index, 0, 0])) for index in range(3)]

        replaced = await write(client, "select_actors", actor_ids=[ids[0]])
        assert replaced["selection"] == [ids[0]]
        assert (await actors(client, selected_only=True, expected_revision=replaced["revision"]))[
            "total"
        ] == 1

        added = await write(client, "select_actors", actor_ids=[ids[1]], mode="add")
        assert added["selection"] == [ids[0], ids[1]]
        picked = await actors(client, selected_only=True, expected_revision=added["revision"])
        assert [actor["editor_id"] for actor in picked["actors"]] == [ids[0], ids[1]]

        toggled = await write(client, "select_actors", actor_ids=[ids[0]], mode="toggle")
        assert toggled["selection"] == [ids[1]]

        cleared = await write(client, "select_actors", actor_ids=[])
        assert cleared["selection"] == [] and cleared["selection_count"] == 0
        empty = await actors(client, selected_only=True, expected_revision=cleared["revision"])
        assert empty["actors"] == []

        everyone = await actors(client, expected_revision=cleared["revision"])
        assert everyone["total"] == 3


async def test_undo_and_redo_restore_what_list_actors_reports() -> None:
    async with flow() as (client, fake):
        placed = await place(client, 1, [2, 2, 0])
        editor_id = placed_id(placed)

        undone = await call(
            client,
            "undo",
            browser_session_id=BROWSER_ID,
            map_session_id=placed["map_session_id"],
            expected_revision=placed["revision"],
        )
        assert undone["can_redo"] is True and undone["changed"] is True
        assert (await actors(client, expected_revision=undone["revision"]))["actors"] == []

        redone = await call(
            client,
            "redo",
            browser_session_id=BROWSER_ID,
            map_session_id=undone["map_session_id"],
            expected_revision=undone["revision"],
        )
        restored = await actors(client, expected_revision=redone["revision"])
        assert [actor["editor_id"] for actor in restored["actors"]] == [editor_id]
        assert restored["actors"][0]["cell"] == [2, 2, 0]

        await call(
            client,
            "undo",
            browser_session_id=BROWSER_ID,
            map_session_id=redone["map_session_id"],
            expected_revision=redone["revision"],
        )
        exhausted = await write(client, "undo")
        assert exhausted["changed"] is False and exhausted["can_undo"] is False
        assert (await actors(client, expected_revision=exhausted["revision"]))["actors"] == []


async def test_write_preconditions_are_reported_in_the_order_the_editor_checks_them() -> None:
    """Session, then revision, then writability: the ladder the browser documents."""
    async with flow() as (client, fake):
        placed = await place(client, 1, [0, 0, 0])
        editor_id = placed_id(placed)
        current = await snapshot(client)

        async def attempt(**overrides: Any) -> dict[str, Any]:
            arguments = {
                "browser_session_id": BROWSER_ID,
                "map_session_id": current["map_session_id"],
                "expected_revision": current["revision"],
                "item_id": 1,
                "cell": [1, 1, 0],
                **overrides,
            }
            return await rejected(client, "place_actor", **arguments)

        stale_session = await attempt(map_session_id="another-map")
        assert stale_session["code"] == "STALE_SESSION"

        conflict = await attempt(expected_revision=current["revision"] - 1)
        assert conflict["code"] == "REVISION_CONFLICT"
        assert browser_details(conflict)["revision"] == current["revision"]

        fake.read_only = True
        read_only = await attempt()
        assert read_only["code"] == "READ_ONLY"

        fake.read_only, fake.busy = False, True
        busy = await attempt()
        assert busy["code"] == "EDITOR_BUSY"

        fake.busy, fake.editing = False, False
        no_edit_mode = await attempt()
        assert no_edit_mode["code"] == "EDIT_MODE_REQUIRED"

        fake.editing = True
        listed = await actors(client)
        assert [actor["editor_id"] for actor in listed["actors"]] == [editor_id]


async def test_empty_actor_id_lists_are_refused_and_change_nothing() -> None:
    """`ActorIds` has no lower bound, so the editor is the one that refuses an empty list.

    `child_actor_ids` is bounded at the schema level instead; both must leave the map
    exactly as it was.
    """
    async with flow() as (client, fake):
        placed = await place(client, 1, [0, 0, 0])
        current = await snapshot(client)

        for name, extra in [
            ("move_actors", {"delta": [1, 0, 0]}),
            ("connect_actors", {"main_actor_id": placed_id(placed)}),
        ]:
            field = "child_actor_ids" if name == "connect_actors" else "actor_ids"
            result = await client.call_tool(
                name,
                {
                    "browser_session_id": BROWSER_ID,
                    "map_session_id": current["map_session_id"],
                    "expected_revision": current["revision"],
                    field: [],
                    **extra,
                },
            )
            assert result.is_error, name
            assert field in result.content[0].text, name

        listed = await actors(client, expected_revision=placed["revision"])
        assert [actor["editor_id"] for actor in listed["actors"]] == [placed_id(placed)]


# ------------------------------------------------------------------- concurrency


async def test_two_transports_share_one_editor_and_serialize_writes() -> None:
    async with live_runtime() as (runtime, base):
        fake = FakeEditor(delay=0.05)
        token = runtime.settings.credentials.http_token
        async with editor_bridge(base, runtime.settings.credentials.pairing_token, fake):
            async with mcp_client(base, token, "modern") as modern:
                async with mcp_client(base, token, "sse") as legacy:
                    first = await place(modern, 1, [0, 0, 0])

                    seen = await call(legacy, "get_state", browser_session_id=BROWSER_ID)
                    assert seen["revision"] == first["revision"]
                    assert seen["map_session_id"] == first["map_session_id"]
                    second = await call(
                        legacy,
                        "place_actor",
                        browser_session_id=BROWSER_ID,
                        map_session_id=seen["map_session_id"],
                        expected_revision=seen["revision"],
                        item_id=1,
                        cell=[1, 0, 0],
                    )

                    shared = await actors(modern, expected_revision=second["revision"])
                    assert {actor["cell"][0] for actor in shared["actors"]} == {0, 1}

                    current = await snapshot(modern)
                    racing = await asyncio.gather(
                        modern.call_tool(
                            "place_actor",
                            {
                                "browser_session_id": BROWSER_ID,
                                "map_session_id": current["map_session_id"],
                                "expected_revision": current["revision"],
                                "item_id": 1,
                                "cell": [2, 0, 0],
                            },
                        ),
                        legacy.call_tool(
                            "place_actor",
                            {
                                "browser_session_id": BROWSER_ID,
                                "map_session_id": current["map_session_id"],
                                "expected_revision": current["revision"],
                                "item_id": 1,
                                "cell": [3, 0, 0],
                            },
                        ),
                    )
                    winners = [result for result in racing if not result.is_error]
                    losers = [result for result in racing if result.is_error]
                    assert len(winners) == 1
                    assert losers[0].structured_content["error"]["code"] == "REVISION_CONFLICT"
                    # One write at a time reaches the editor, whichever transport sent it.
                    assert fake.peak_in_flight == 1

                    final = await actors(modern)
                    assert final["total"] == 3
                    # The race is won by one of the two cells; the loser never lands.
                    occupied = sorted(actor["cell"][0] for actor in final["actors"])
                    assert occupied in ([0, 1, 2], [0, 1, 3])


async def test_timeout_reports_an_unknown_outcome_and_keeps_the_browser() -> None:
    async with flow(request_timeout=0.2, hold=frozenset({"place_actor"})) as (client, fake):
        current = await snapshot(client)
        timed_out = await rejected(
            client,
            "place_actor",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            item_id=1,
            cell=[0, 0, 0],
        )
        assert timed_out["code"] == "BROWSER_TIMEOUT"
        assert timed_out["details"]["outcome"] == "unknown"

        sessions = await call(client, "list_sessions")
        assert [session["browser_session_id"] for session in sessions["sessions"]] == [BROWSER_ID]
        assert (await actors(client))["total"] == 0


# ------------------------------------------------------------------------- stdio


async def test_stdio_transport_applies_the_same_writes(tmp_path: Path) -> None:
    credentials = load_credentials(tmp_path)
    port = unused_port()
    base = f"http://127.0.0.1:{port}"

    async def stdio_call(process, request_id: int, name: str, arguments: dict[str, Any]):
        result = await stdio_rpc(
            process, request_id, "tools/call", {"name": name, "arguments": arguments}
        )
        assert not result.get("isError", False), result
        return result["structuredContent"]

    async with cli_process(tmp_path, "all", port) as process:
        await initialize_stdio(process)
        async with editor_bridge(base, credentials.pairing_token, FakeEditor()):
            state = await stdio_call(process, 2, "get_state", {"browser_session_id": BROWSER_ID})
            placed = await stdio_call(
                process,
                3,
                "place_actor",
                {
                    "browser_session_id": BROWSER_ID,
                    "map_session_id": state["map_session_id"],
                    "expected_revision": state["revision"],
                    "item_id": 1,
                    "cell": [0, 0, 0],
                },
            )
            editor_id = placed["actor"]["editor_id"]

            listed = await stdio_call(process, 4, "list_actors", {"browser_session_id": BROWSER_ID})
            assert [actor["editor_id"] for actor in listed["actors"]] == [editor_id]

            await stdio_call(
                process,
                5,
                "move_actors",
                {
                    "browser_session_id": BROWSER_ID,
                    "map_session_id": placed["map_session_id"],
                    "expected_revision": placed["revision"],
                    "actor_ids": [editor_id],
                    "delta": [0, 1, 0],
                },
            )
            moved = await stdio_call(process, 6, "list_actors", {"browser_session_id": BROWSER_ID})
            assert moved["actors"][0]["cell"] == [0, 1, 0]

            await stdio_call(
                process,
                7,
                "delete_actors",
                {
                    "browser_session_id": BROWSER_ID,
                    "map_session_id": moved["map_session_id"],
                    "expected_revision": moved["revision"],
                    "actor_ids": [editor_id],
                },
            )
            removed = await stdio_call(
                process, 8, "list_actors", {"browser_session_id": BROWSER_ID}
            )
            assert removed["actors"] == []


# ------------------------------------------------------------------------ schema


async def test_tools_publish_the_arguments_and_enums_a_client_needs() -> None:
    async with flow() as (client, fake):
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(tools) == TOOLS

        # A client decides whether to ask for confirmation from the hint alone.
        assert {name for name, tool in tools.items() if tool.annotations.read_only_hint} == {
            "list_sessions",
            "get_state",
            "list_items",
            "list_actors",
            "list_links",
        }

        for name, tool in tools.items():
            schema = tool.input_schema
            assert schema["type"] == "object", name
            if name == "list_sessions":
                assert schema["properties"] == {}
                continue
            assert "browser_session_id" in schema["required"], name
            if name in WRITE_TOOLS:
                assert {"map_session_id", "expected_revision"} <= set(schema["required"]), name

        kinds = tools["list_links"].input_schema["properties"]["kind"]["anyOf"][0]
        assert kinds["enum"] == ["pair", "group", "orphan"]
        assert tools["select_actors"].input_schema["properties"]["mode"]["enum"] == [
            "replace",
            "add",
            "toggle",
        ]
        rotation = tools["place_actor"].input_schema["properties"]["rotation_index"]
        assert (rotation["minimum"], rotation["maximum"]) == (0, 3)
        cell = tools["place_actor"].input_schema["properties"]["cell"]
        assert cell["minItems"] == cell["maxItems"] == 3
        limit = tools["list_actors"].input_schema["properties"]["limit"]
        assert (limit["minimum"], limit["maximum"]) == (1, 500)
        children = tools["connect_actors"].input_schema["properties"]["child_actor_ids"]
        assert children["minItems"] == 1 and children["maxItems"] == 256
