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
import base64
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

from calabiyau_map_engine_mcp import tools as tools_module
from calabiyau_map_engine_mcp.config import load_credentials

TOOLS = {
    "list_sessions",
    "get_state",
    "get_camera",
    "set_camera",
    "capture_view",
    "list_items",
    "list_actors",
    "list_links",
    "query_space",
    "describe_region",
    "find_free_space",
    "validate_layout",
    "apply_layout",
    "create_scene_object",
    "delete_scene_object",
    "connect_actors",
    "disconnect_actors",
    "select_actors",
    "place_actor",
    "place_actors",
    "place_quad",
    "place_quads",
    "update_actors",
    "copy_actors",
    "mirror_actors",
    "array_actors",
    "polar_array",
    "fill_box",
    "fill_sphere",
    "move_actors",
    "delete_actors",
    "delete_region",
    "undo",
    "redo",
    "import_model",
    "discard_model",
}
WRITE_TOOLS = TOOLS - {
    "list_sessions",
    "get_state",
    "get_camera",
    "set_camera",
    "capture_view",
    "list_items",
    "list_actors",
    "list_links",
    "query_space",
    "describe_region",
    "find_free_space",
    "validate_layout",
    # Releases browser memory, never the map, so it needs no snapshot.
    "discard_model",
}


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


async def write(client, tool_name: str, **arguments: Any) -> dict[str, Any]:
    current = await snapshot(client)
    return await call(
        client,
        tool_name,
        browser_session_id=BROWSER_ID,
        map_session_id=current["map_session_id"],
        expected_revision=current["revision"],
        **arguments,
    )


def placed_id(result: dict[str, Any]) -> int:
    return result["actor"]["editor_id"]


async def test_every_tool_reaches_the_editor_over_one_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The coverage tripwire: no tool may exist without a real request behind it."""
    monkeypatch.setattr(tools_module, "SCREENSHOT_DIR", tmp_path)
    async with flow() as (client, fake):
        await call(client, "list_sessions")
        await call(client, "get_state", browser_session_id=BROWSER_ID)
        await call(client, "get_camera", browser_session_id=BROWSER_ID)
        await call(
            client,
            "set_camera",
            browser_session_id=BROWSER_ID,
            position=[1.0, 2.0, 3.0],
            target=[0.0, 0.0, 0.0],
        )
        await call(client, "capture_view", browser_session_id=BROWSER_ID)
        await call(client, "list_items", browser_session_id=BROWSER_ID)
        await call(client, "list_actors", browser_session_id=BROWSER_ID)
        await call(client, "list_links", browser_session_id=BROWSER_ID)

        # The layout pair fans out into get_state, list_items and list_actors for
        # preflight and reuses place_actors for the write, so neither name reaches
        # the bridge. Both still have to be exercised end to end.
        checked = await call(
            client,
            "validate_layout",
            browser_session_id=BROWSER_ID,
            placements=[{"item_id": 1, "cell": [4, 4, 0]}],
        )
        assert checked["valid"] is True and checked["applied"] is False
        applied = await write(
            client, "apply_layout", placements=[{"item_id": 1, "cell": [4, 4, 0]}]
        )
        assert applied["placed_count"] == 1

        main = placed_id(await place(client, 2, [0, 0, 0]))
        child = placed_id(await place(client, 3, [1, 0, 0]))

        # The quad tools fan out into place_actors, so no bridge action is named
        # after them -- but the transform they derive still has to be one the
        # editor accepts, and that is the part that silently rots.
        plate = await write(
            client,
            "place_quad",
            vertices=[[0, 0, 0], [100, 0, 0], [100, 0, 100], [0, 0, 100]],
        )
        assert [entry["item_id"] for entry in plate["placed_actors"]] == [7]
        slabs = await write(
            client,
            "place_quads",
            quads=[
                {"vertices": [[0, 0, 200], [100, 0, 200], [100, 0, 300], [0, 0, 300]]},
                {"vertices": [[0, 200, 0], [100, 200, 0], [100, 300, 0], [0, 300, 0]]},
            ],
        )
        assert len(slabs["placed_actors"]) == 2

        await write(client, "polar_array", actor_ids=[child], center=[0, 0, 0], axis="z", count=4)
        await write(client, "update_actors", actor_ids=[main], color_linear_rgb=[1.0, 0.0, 0.0])
        await write(client, "select_actors", actor_ids=[child])
        await write(client, "move_actors", actor_ids=[child], delta=[0, 1, 0])
        await write(client, "connect_actors", main_actor_id=main, child_actor_ids=[child])
        await write(client, "disconnect_actors", actor_ids=[child])
        await write(client, "delete_actors", actor_ids=[child])
        await write(client, "undo")
        await write(client, "redo")

        # The model importer is one tool over three bridge actions: the bytes go
        # up in frames, the solve happens in the browser, and only the last call
        # writes. Exercise all three so none can rot unnoticed.
        model = tmp_path / "tower.obj"
        model.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
        current = await snapshot(client)
        await call(
            client,
            "import_model",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            origin=[5, 5, 0],
            path=str(model),
            resolution=2,
            mode="voxel",
        )
        # Discarding an id the browser never held still has to reach it: the
        # answer, not the guess, is what says whether anything was released.
        await call(client, "discard_model", browser_session_id=BROWSER_ID, upload_id="upload-missing")

        # Project objects are their own bridge actions, unlike the map wrappers.
        group = await write(client, "create_scene_object", name="tower", actor_ids=[main])
        await write(client, "delete_scene_object", object_id=group["object_id"])

        reached = {frame["action"] for frame in fake.received}
        # list_sessions is answered from the registry, so it is the one tool that must
        # never put a frame on the bridge. Batch wrappers fan out into list_actors /
        # place_actor / delete_actors, so only their native targets appear here.
        native = (TOOLS - {
            "list_sessions",
            "query_space",
            "describe_region",
            "find_free_space",
            "place_actors",
            "place_quad",
            "place_quads",
            "copy_actors",
            "mirror_actors",
            "array_actors",
            "fill_box",
            "fill_sphere",
            "delete_region",
            "import_model",
            "validate_layout",
            "apply_layout",
        }) | {"place_actors", "upload_model", "plan_model", "apply_model", "discard_model"}
        assert reached == native


async def test_place_actors_reports_duplicate_and_occupied_cells() -> None:
    async with flow() as (client, fake):
        first = await place(client, 1, [0, 0, 0])
        current = await snapshot(client)
        batch = await call(
            client,
            "place_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            placements=[
                {"item_id": 1, "cell": [1, 0, 0]},
                {"item_id": 1, "cell": [1, 0, 0]},
                {"item_id": 1, "cell": [0, 0, 0]},
            ],
        )
        assert len(batch["placed_actors"]) == 1
        assert {error["reason"] for error in batch["placement_errors"]} == {"DUPLICATE_CELL", "CELL_OCCUPIED"}
        assert batch["revision"] == first["revision"] + 1
        assert len([frame for frame in fake.received if frame["action"] == "place_actors"]) == 1

        # 4097 placements are refused at the tool boundary, so the editor never sees them.
        too_large = await client.call_tool(
            "place_actors",
            {
                "browser_session_id": BROWSER_ID,
                "map_session_id": batch["map_session_id"],
                "expected_revision": batch["revision"],
                "placements": [{"item_id": 1, "cell": [0, 0, 0]}] * 4097,
            },
        )
        assert too_large.is_error
        assert [frame["action"] for frame in fake.received].count("place_actors") == 1


async def test_batch_wrappers_build_read_back_and_delete_a_region() -> None:
    """Server-side wrappers must land real actors, not just return a shaped result."""
    async with flow() as (client, fake):
        current = await snapshot(client)
        placed = await call(
            client,
            "place_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            placements=[
                {"item_id": 1, "cell": [0, 1, 0]},
                {"item_id": 1, "cell": [1, 1, 0]},
            ],
        )
        assert len(placed["placed_actors"]) == 2
        assert placed["placement_errors"] == []
        assert placed["revision"] == current["revision"] + 1

        current = await snapshot(client)
        filled = await call(
            client,
            "fill_box",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            min_cell=[2, 0, 0],
            max_cell=[3, 0, 0],
            item_id=1,
            mode="solid",
        )
        assert filled["fill"]["placed_count"] == 2
        assert filled["fill"]["error_count"] == 0
        assert [frame["action"] for frame in fake.received].count("place_actors") == 2
        assert [frame["action"] for frame in fake.received].count("place_actor") == 0

        current = await snapshot(client)
        sphere = await call(
            client,
            "fill_sphere",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            center=[5, 0, 0],
            radius=0,
            item_id=1,
        )
        assert sphere["fill"]["placed_count"] == 1

        described = await call(
            client,
            "describe_region",
            browser_session_id=BROWSER_ID,
            min_cell=[0, 0, 0],
            max_cell=[3, 1, 0],
        )
        assert described["occupied_count"] == 4
        assert described["item_counts"] == {"1": 4}
        assert described["layers"]["z=0"] == ["##..", "..##"]

        queried = await call(
            client,
            "query_space",
            browser_session_id=BROWSER_ID,
            min_cell=[0, 0, 0],
            max_cell=[3, 1, 0],
        )
        assert queried["query_type"] == "region"
        assert [entry["cell"] for entry in queried["occupied_cells"]] == [
            [0, 1, 0],
            [1, 1, 0],
            [2, 0, 0],
            [3, 0, 0],
        ]

        inverted = await rejected(
            client,
            "query_space",
            browser_session_id=BROWSER_ID,
            min_cell=[2, 0, 0],
            max_cell=[1, 0, 0],
        )
        assert inverted["code"] == "INVALID_PARAMS"

        free = await call(
            client,
            "find_free_space",
            browser_session_id=BROWSER_ID,
            min_cell=[0, 0, 0],
            max_cell=[3, 1, 0],
            size=[1, 1, 1],
        )
        assert free["candidates"]

        current = await snapshot(client)
        deleted = await call(
            client,
            "delete_region",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            min_cell=[2, 0, 0],
            max_cell=[3, 1, 0],
        )
        assert deleted["matched_count"] == 2
        assert deleted["deleted_count"] == 2
        assert "deleted_actor_ids" not in deleted

        remaining = await actors(client)
        assert [actor["cell"] for actor in remaining["actors"]] == [
            [0, 1, 0],
            [1, 1, 0],
            [5, 0, 0],
        ]


# -------------------------------------------------------------------- duplication


async def test_copy_actors_duplicates_a_set_in_one_batch() -> None:
    """A copy has to land real actors that keep item, rotation and colour."""
    async with flow() as (client, fake):
        wall = placed_id(
            await place(client, 1, [0, 0, 0], rotation_index=1, color_linear_rgb=[0.2, 0.4, 0.6])
        )
        track = placed_id(await place(client, 3, [1, 0, 0], rotation_index=3))
        before = await snapshot(client)

        copied = await call(
            client,
            "copy_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=before["map_session_id"],
            expected_revision=before["revision"],
            actor_ids=[wall, track],
            offset=[0, 1, 0],
        )
        assert copied["operation"] == {
            "kind": "copy",
            "offset": [0, 1, 0],
            "source_count": 2,
            "requested_count": 2,
            "placed_count": 2,
            "skipped_count": 0,
            "error_count": 0,
        }
        assert copied["revision"] == before["revision"] + 1
        assert {entry["source_actor_id"] for entry in copied["placed_actors"]} == {wall, track}
        assert [frame["action"] for frame in fake.received].count("place_actors") == 1

        listed = await actors(client, expected_revision=copied["revision"])
        by_cell = {tuple(actor["cell"]): actor for actor in listed["actors"]}
        assert set(by_cell) == {(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)}
        assert by_cell[(0, 1, 0)]["item_id"] == 1
        assert by_cell[(0, 1, 0)]["transform"]["rotation_index"] == 1
        assert by_cell[(0, 1, 0)]["color_linear_rgb"] == pytest.approx([0.2, 0.4, 0.6])
        assert by_cell[(1, 1, 0)]["item_id"] == 3
        assert by_cell[(1, 1, 0)]["transform"]["rotation_index"] == 3
        # The sources stay where they were; a copy adds, it does not move.
        assert by_cell[(0, 0, 0)]["editor_id"] == wall
        assert by_cell[(1, 0, 0)]["editor_id"] == track


async def test_copy_actors_maps_conflicts_back_to_their_source() -> None:
    async with flow() as (client, fake):
        source = placed_id(await place(client, 1, [0, 0, 0]))
        blocker = placed_id(await place(client, 1, [0, 1, 0]))

        current = await snapshot(client)
        copied = await call(
            client,
            "copy_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            actor_ids=[source],
            offset=[0, 1, 0],
        )
        assert copied["operation"]["placed_count"] == 0
        assert copied["operation"]["error_count"] == 1
        error = copied["placement_errors"][0]
        assert (error["reason"], error["cell"], error["source_actor_id"]) == (
            "CELL_OCCUPIED",
            [0, 1, 0],
            source,
        )
        assert error["occupants"][0]["editor_id"] == blocker
        # Nothing landed, so the map did not move on.
        assert copied["revision"] == current["revision"]

        stationary = await rejected(
            client,
            "copy_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            actor_ids=[source],
            offset=[0, 0, 0],
        )
        assert stationary["code"] == "INVALID_PARAMS"

        unknown = await rejected(
            client,
            "copy_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            actor_ids=[9999],
            offset=[0, 0, 1],
        )
        assert unknown["code"] == "ACTOR_NOT_FOUND"
        assert unknown["actor_ids"] == [9999]

        repeated = await rejected(
            client,
            "copy_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            actor_ids=[source, source],
            offset=[0, 0, 1],
        )
        assert repeated["code"] == "INVALID_PARAMS"

        # A copy that falls off the grid is one placement error, not a failed call.
        edge = placed_id(await place(client, 1, [7, 7, 0]))
        current = await snapshot(client)
        spilled = await call(
            client,
            "copy_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            actor_ids=[edge],
            offset=[1, 0, 0],
        )
        assert spilled["placement_errors"][0]["reason"] == "OUT_OF_BOUNDS"
        assert spilled["placement_errors"][0]["source_actor_id"] == edge


async def test_copy_actors_reports_what_a_placement_cannot_rebuild() -> None:
    """A pair half has no standalone placement; a free actor is rebuilt from its pose."""
    async with flow() as (client, fake):
        left, _right = fake.seed_pair([2, 2, 0])
        free = fake.seed_free(7, [4, 4, 0])
        current = await snapshot(client)

        copied = await call(
            client,
            "copy_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            actor_ids=[left, free],
            offset=[0, 0, 1],
        )
        assert copied["operation"]["source_count"] == 2
        assert copied["operation"]["requested_count"] == 2
        assert copied["operation"]["placed_count"] == 1
        # Nothing is skipped by the planner any more: the free actor had a pose to reuse.
        assert copied["skipped_actors"] == []
        # The pair half reached the editor, which is the one that knows it is not placeable.
        error = copied["placement_errors"][0]
        assert (error["reason"], error["source_actor_id"]) == ("ITEM_NOT_PLACEABLE", left)
        # The free copy kept its pose and only moved, rather than being dropped at a cell.
        assert copied["placed_actors"][0]["transform"] == {
            "location_cm": [400.0, 400.0, 100.0],
            "scale": [1.0, 1.0, 1.0],
            "rotation_degrees": [0.0, 0.0, 0.0],
        }

        # A set with nothing placeable is a read, not a write.
        main, _child = fake.seed_group(1, 1, [6, 6, 0])
        writes = [frame["action"] for frame in fake.received].count("place_actors")
        empty = await write(client, "copy_actors", actor_ids=[main], offset=[0, 0, 1])
        assert empty["operation"]["requested_count"] == 0
        assert empty["placed_actors"] == []
        assert empty["skipped_actors"][0]["reason"] == "ACTOR_IN_GROUP"
        assert [frame["action"] for frame in fake.received].count("place_actors") == writes


async def test_a_plate_keeps_its_pose_through_place_move_and_copy() -> None:
    """The free-transform path end to end: pose in, pose out, no cell in between."""
    async with flow() as (client, fake):
        current = await snapshot(client)
        placed = await call(
            client,
            "place_actor",
            browser_session_id=BROWSER_ID,
            map_session_id=current["map_session_id"],
            expected_revision=current["revision"],
            item_id=7,
            transform={
                "location_cm": [812.5, 790.25, 50],
                "scale": [0, 2, 1],
                "rotation_degrees": [0, 45, 0],
            },
        )
        actor = placed["actor"]
        assert actor["transform_mode"] == 2
        assert actor["transform"] == {
            "location_cm": [812.5, 790.25, 50],
            "scale": [0.0, 2.0, 1.0],
            "rotation_degrees": [0.0, 45.0, 0.0],
        }

        moved = await write(
            client, "update_actors", actor_ids=[actor["editor_id"]], location_delta_cm=[0, 0, 25]
        )
        assert moved["actors"][0]["transform"] == {
            "location_cm": [812.5, 790.25, 75],
            "scale": [0.0, 2.0, 1.0],
            "rotation_degrees": [0.0, 45.0, 0.0],
        }

        copied = await write(client, "copy_actors", actor_ids=[actor["editor_id"]], offset=[0, 0, 2])
        assert copied["operation"]["placed_count"] == 1
        assert copied["placed_actors"][0]["transform"] == {
            "location_cm": [812.5, 790.25, 275],
            "scale": [0.0, 2.0, 1.0],
            "rotation_degrees": [0.0, 45.0, 0.0],
        }

        row = await write(
            client,
            "array_actors",
            actor_ids=[actor["editor_id"]],
            direction=[0, 1, 0],
            spacing=2,
            count=3,
        )
        assert [entry["transform"]["location_cm"] for entry in row["placed_actors"]] == [
            [812.5, 990.25, 75],
            [812.5, 1190.25, 75],
        ]


async def test_free_pose_arguments_are_refused_before_the_browser() -> None:
    async with flow() as (client, fake):
        current = await snapshot(client)
        base = {
            "browser_session_id": BROWSER_ID,
            "map_session_id": current["map_session_id"],
            "expected_revision": current["revision"],
            "item_id": 7,
        }
        both = await rejected(
            client,
            "place_actor",
            **base,
            cell=[1, 1, 0],
            transform={"location_cm": [150, 150, 50]},
        )
        assert both["code"] == "INVALID_PARAMS"
        neither = await rejected(client, "place_actor", **base)
        assert neither["code"] == "INVALID_PARAMS"
        # Neither refusal reached the editor, so nothing was written.
        assert [frame["action"] for frame in fake.received].count("place_actor") == 0


async def test_mirror_actors_reflects_cells_and_yaw() -> None:
    async with flow() as (client, fake):
        flat = placed_id(await place(client, 1, [0, 2, 0], rotation_index=0))
        turned = placed_id(await place(client, 1, [1, 2, 0], rotation_index=1))

        across_x = await write(
            client, "mirror_actors", actor_ids=[flat, turned], axis="x", origin=1.5
        )
        assert across_x["operation"]["axis"] == "x"
        assert across_x["operation"]["origin"] == 1.5
        assert across_x["operation"]["placed_count"] == 2
        listed = await actors(client, expected_revision=across_x["revision"])
        by_cell = {tuple(actor["cell"]): actor for actor in listed["actors"]}
        assert set(by_cell) == {(0, 2, 0), (1, 2, 0), (2, 2, 0), (3, 2, 0)}
        # Yaw 0 faces +X, which the x plane turns into -X (index 2); yaw 90 is its own mirror.
        assert by_cell[(3, 2, 0)]["transform"]["rotation_index"] == 2
        assert by_cell[(2, 2, 0)]["transform"]["rotation_index"] == 1

        # The y plane negates the heading instead: 90 degrees becomes 270.
        across_y = await write(
            client, "mirror_actors", actor_ids=[turned], axis="y", origin=2.5
        )
        listed = await actors(client, expected_revision=across_y["revision"])
        mirrored = next(a for a in listed["actors"] if a["cell"] == [1, 3, 0])
        assert mirrored["transform"]["rotation_index"] == 3

        # Flipping height changes no heading at all.
        across_z = await write(
            client, "mirror_actors", actor_ids=[turned], axis="z", origin=1.5
        )
        listed = await actors(client, expected_revision=across_z["revision"])
        raised = next(a for a in listed["actors"] if a["cell"] == [1, 2, 3])
        assert raised["transform"]["rotation_index"] == 1


async def test_array_actors_repeats_a_set_at_a_spacing() -> None:
    async with flow() as (client, fake):
        first = placed_id(await place(client, 1, [0, 0, 0]))
        second = placed_id(await place(client, 1, [0, 1, 0]))
        before = await snapshot(client)

        arrayed = await call(
            client,
            "array_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=before["map_session_id"],
            expected_revision=before["revision"],
            actor_ids=[first, second],
            direction=[1, 0, 0],
            spacing=2,
            count=3,
        )
        assert arrayed["operation"]["requested_count"] == 4
        assert arrayed["operation"]["placed_count"] == 4
        assert arrayed["operation"]["step"] == [2, 0, 0]
        assert arrayed["revision"] == before["revision"] + 1
        assert [frame["action"] for frame in fake.received].count("place_actors") == 1

        listed = await actors(client, expected_revision=arrayed["revision"])
        assert sorted(tuple(actor["cell"]) for actor in listed["actors"]) == [
            (0, 0, 0),
            (0, 1, 0),
            (2, 0, 0),
            (2, 1, 0),
            (4, 0, 0),
            (4, 1, 0),
        ]


async def test_array_actors_refuses_a_batch_over_the_placement_cap() -> None:
    async with flow() as (client, fake):
        before = await snapshot(client)
        seeded = await call(
            client,
            "place_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=before["map_session_id"],
            expected_revision=before["revision"],
            placements=[{"item_id": 1, "cell": [x, y, 0]} for x in range(8) for y in range(8)],
        )
        assert len(seeded["placed_actors"]) == 64

        too_large = await rejected(
            client,
            "array_actors",
            browser_session_id=BROWSER_ID,
            map_session_id=seeded["map_session_id"],
            expected_revision=seeded["revision"],
            actor_ids=[entry["editor_id"] for entry in seeded["placed_actors"]],
            direction=[1, 0, 0],
            spacing=1,
            count=66,
        )
        assert too_large["code"] == "BATCH_TOO_LARGE"
        assert [frame["action"] for frame in fake.received].count("place_actors") == 1


async def test_view_tools_move_the_camera_and_save_a_frame(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A capture must reach the browser, come back decodable, and land on disk."""
    monkeypatch.setattr(tools_module, "SCREENSHOT_DIR", tmp_path)
    async with flow() as (client, fake):
        before = await call(client, "get_camera", browser_session_id=BROWSER_ID)
        assert before["camera"]["position"] == [0.0, 6.0, 12.0]

        moved = await call(
            client,
            "set_camera",
            browser_session_id=BROWSER_ID,
            position=[4.0, 3.0, 8.0],
            target=[4.0, 0.0, 4.0],
        )
        expected = {"position": [4.0, 3.0, 8.0], "target": [4.0, 0.0, 4.0]}
        assert moved["camera"] == expected
        seen = await call(client, "get_camera", browser_session_id=BROWSER_ID)
        assert seen["camera"] == expected

        captured = await call(
            client,
            "capture_view",
            browser_session_id=BROWSER_ID,
            width=320,
            height=200,
            image_format="png",
        )
        assert (captured["width"], captured["height"]) == (320, 200)
        assert captured["mime_type"] == "image/png"
        assert captured["camera"] == expected
        saved = Path(captured["path"])
        assert saved.parent == tmp_path
        assert await asyncio.to_thread(saved.read_bytes) == b"fake-viewport-frame"
        assert captured["bytes"] == len(b"fake-viewport-frame")

        # position and target are all-or-nothing, exactly as the browser checks them.
        mismatched = await rejected(
            client,
            "capture_view",
            browser_session_id=BROWSER_ID,
            position=[0.0, 0.0, 1.0],
        )
        assert mismatched["code"] == "INVALID_PARAMS"
        unchanged = await call(client, "get_camera", browser_session_id=BROWSER_ID)
        assert unchanged["camera"] == expected


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
        assert first_page["total"] == 5
        assert [item["item_id"] for item in first_page["items"]] == [1, 2]
        assert first_page["items"][1]["child_item_id"] == 3
        assert all(item["placeable"] for item in first_page["items"])

        second_page = await call(
            client, "list_items", browser_session_id=BROWSER_ID, offset=2, limit=2
        )
        assert [item["item_id"] for item in second_page["items"]] == [3, 6]
        assert second_page["total"] == 5

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
            "get_camera",
            "list_items",
            "list_actors",
            "list_links",
            "query_space",
            "describe_region",
            "find_free_space",
            "validate_layout",
        }

        for name, tool in tools.items():
            schema = tool.input_schema
            assert schema["type"] == "object", name
            if name == "list_sessions":
                assert schema["properties"] == {}
                continue
            assert "browser_session_id" in schema["required"], name
            if name in WRITE_TOOLS and name != "import_model":
                assert {"map_session_id", "expected_revision"} <= set(schema["required"]), name
            if name == "import_model":
                # A dry run only reads: it uploads, solves and reports, so it can be
                # asked before the caller has fetched a snapshot. The tool refuses
                # the write itself when the two are missing. origin is only needed
                # to fit a new model; applying a plan_token already carries one.
                assert "map_session_id" not in schema["required"]
                assert "origin" not in schema["required"]

        kinds = tools["list_links"].input_schema["properties"]["kind"]["anyOf"][0]
        assert kinds["enum"] == ["pair", "group", "orphan"]
        assert tools["select_actors"].input_schema["properties"]["mode"]["enum"] == [
            "replace",
            "add",
            "toggle",
        ]
        # rotation_index is optional now that transform can state an angle instead,
        # so the bounds live on the first anyOf branch rather than the top level.
        rotation = tools["place_actor"].input_schema["properties"]["rotation_index"]["anyOf"][0]
        assert (rotation["minimum"], rotation["maximum"]) == (0, 3)
        place_properties = tools["place_actor"].input_schema["properties"]
        assert "transform" in place_properties
        assert "cell" not in tools["place_actor"].input_schema["required"]
        free = tools["update_actors"].input_schema["properties"]
        for field in ("location_cm", "location_delta_cm", "scale", "rotation_degrees"):
            assert field in free, field
        cell = tools["place_actor"].input_schema["properties"]["cell"]["anyOf"][0]
        assert cell["minItems"] == cell["maxItems"] == 3
        limit = tools["list_actors"].input_schema["properties"]["limit"]
        assert (limit["minimum"], limit["maximum"]) == (1, 4096)
        children = tools["connect_actors"].input_schema["properties"]["child_actor_ids"]
        assert children["minItems"] == 1 and children["maxItems"] == 256


MODEL_OBJ = b"v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nv 0 0 1\nf 1 2 3\nf 1 3 5\n"


def model_file(tmp_path: Path, name: str = "tower.obj", payload: bytes = MODEL_OBJ) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


async def test_import_model_uploads_in_chunks_then_places_one_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file larger than one frame still arrives whole, and lands as one edit."""
    # Eight bytes a frame turns a 49-byte file into seven frames, which is the
    # only way to see the offset bookkeeping from a test that is not 640 KiB.
    monkeypatch.setattr(tools_module, "MODEL_CHUNK_BYTES", 8)
    path = model_file(tmp_path)
    payload = path.read_bytes()

    async with flow() as (client, fake):
        imported = await write(
            client,
            "import_model",
            origin=[2, 2, 0],
            path=str(path),
            resolution=3,
            mode="voxel",
        )
        assert imported["placed_count"] == 3
        assert imported["planned_blocks"] == 3

        frames = [frame for frame in fake.received if frame["action"] == "upload_model"]
        assert [frame["params"]["offset"] for frame in frames] == list(range(0, len(payload), 8))
        assert sum(
            len(base64.b64decode(frame["params"]["data_base64"])) for frame in frames
        ) == len(payload)
        # Applying the plan releases the upload, so nothing is left holding bytes.
        assert fake.uploads == {}

        listed = await actors(client, expected_revision=imported["revision"])
        assert listed["total"] == 3
        assert sorted(tuple(actor["cell"]) for actor in listed["actors"]) == [
            (2, 2, 0),
            (2, 2, 1),
            (2, 2, 2),
        ]


async def test_import_model_dry_run_writes_nothing_and_can_be_applied_later(
    tmp_path: Path,
) -> None:
    path = model_file(tmp_path)
    async with flow() as (client, fake):
        before = await snapshot(client)
        planned = await call(
            client,
            "import_model",
            browser_session_id=BROWSER_ID,
            origin=[1, 1, 0],
            path=str(path),
            resolution=4,
            dry_run=True,
        )
        assert planned["dry_run"] is True
        assert planned["plan"]["blocks"] == 4
        assert (await snapshot(client))["revision"] == before["revision"]
        assert (await actors(client))["total"] == 0

        uploads = [frame for frame in fake.received if frame["action"] == "upload_model"]
        # No origin here: the plan already holds the one it was fitted at, and
        # asking for it again would make the caller repeat a value it cannot use.
        applied = await write(client, "import_model", plan_token=planned["plan_token"])
        assert applied["placed_count"] == 4
        # Applying a token uploads nothing: the plan was already solved.
        assert [frame for frame in fake.received if frame["action"] == "upload_model"] == uploads
        assert (await actors(client, expected_revision=applied["revision"]))["total"] == 4


async def test_import_model_forwards_a_native_resolution_as_a_string(
    tmp_path: Path,
) -> None:
    """A .vox already stores one coloured voxel per cell, so the file owns the grid."""
    path = model_file(tmp_path, "figure.vox")
    async with flow() as (client, fake):
        planned = await call(
            client,
            "import_model",
            browser_session_id=BROWSER_ID,
            origin=[1, 1, 0],
            path=str(path),
            mode="voxel",
            resolution="native",
            dry_run=True,
        )
        assert planned["dry_run"] is True
        # The word itself has to survive the trip: turning it into a number here
        # would resample the art the caller deliberately kept.
        frame = next(frame for frame in fake.received if frame["action"] == "plan_model")
        assert frame["params"]["options"]["resolution"] == "native"


async def test_import_model_reports_what_it_cannot_read(tmp_path: Path) -> None:
    async with flow() as (client, fake):
        missing = await rejected(
            client,
            "import_model",
            browser_session_id=BROWSER_ID,
            origin=[0, 0, 0],
            path=str(tmp_path / "nope.obj"),
            dry_run=True,
        )
        assert missing["code"] == "MODEL_FILE_NOT_FOUND"

        wrong = model_file(tmp_path, "tower.txt", b"hello")
        unreadable = await rejected(
            client,
            "import_model",
            browser_session_id=BROWSER_ID,
            origin=[0, 0, 0],
            path=str(wrong),
            dry_run=True,
        )
        assert unreadable["code"] == "INVALID_PARAMS"

        # Neither refusal may reach the editor.
        assert [frame["action"] for frame in fake.received] == []


async def test_import_model_refuses_an_ambiguous_source_and_a_missing_snapshot(
    tmp_path: Path,
) -> None:
    path = model_file(tmp_path)
    async with flow() as (client, fake):
        both = await rejected(
            client,
            "import_model",
            browser_session_id=BROWSER_ID,
            origin=[0, 0, 0],
            path=str(path),
            plan_token="upload-1",
            dry_run=True,
        )
        assert both["code"] == "INVALID_PARAMS"

        # A new model has nowhere to stand without an origin, and the tool says
        # so before it uploads a single byte.
        nowhere = await rejected(
            client,
            "import_model",
            browser_session_id=BROWSER_ID,
            path=str(path),
            resolution=2,
            dry_run=True,
        )
        assert nowhere["code"] == "INVALID_PARAMS"
        assert "origin" in nowhere["message"]
        assert [frame["action"] for frame in fake.received] == []

        # Without dry_run the write needs a snapshot; the tool says so instead of
        # letting the browser answer with a less specific error.
        no_snapshot = await rejected(
            client,
            "import_model",
            browser_session_id=BROWSER_ID,
            origin=[0, 0, 0],
            path=str(path),
            resolution=2,
        )
        assert no_snapshot["code"] == "INVALID_PARAMS"
        assert no_snapshot["plan_token"].startswith("upload-")
        assert [frame["action"] for frame in fake.received] == ["upload_model", "plan_model"]


# ------------------------------------------------------------- layout preflight


async def test_validate_layout_reports_conflicts_without_writing() -> None:
    """Every refusal category is named, and the map plus history stay untouched."""
    async with flow() as (client, fake):
        seeded = fake.seed(1, [0, 0, 0])
        before = await snapshot(client)
        placements = [
            {"item_id": 1, "cell": [1, 0, 0]},
            {"item_id": 1, "cell": [1, 0, 0]},
            {"item_id": 1, "cell": [0, 0, 0]},
            {"item_id": 1, "cell": [8, 0, 0]},
            {"item_id": 4, "cell": [2, 0, 0]},
            {"item_id": 1, "cell": [3, 0, 0], "transform": {"location_cm": [50.0, 50.0, 0.0]}},
            {"item_id": 1},
            {"item_id": 1, "cell": [4, 0, 0], "rotation_index": 4},
            {"item_id": 1, "transform": {"location_cm": [50.0, 50.0, 0.0]}},
            {"item_id": 7, "transform": {
                "location_cm": [50.0, 50.0, 0.0], "scale": [0.0, 1.0, 1.0],
            }},
            {"item_id": 7, "transform": {
                "location_cm": [50.0, 50.0, 0.0], "scale": [10001.0, 1.0, 1.0],
            }},
        ]
        checked = await call(
            client,
            "validate_layout",
            browser_session_id=BROWSER_ID,
            placements=placements,
        )
        assert checked["applied"] is False
        assert checked["valid"] is False
        assert checked["undo_entries_created"] == 0
        assert checked["placement_count"] == len(placements)
        assert [(conflict["index"], conflict["reason"]) for conflict in checked["conflicts"]] == [
            (1, "DUPLICATE_CELL"),
            (2, "CELL_OCCUPIED"),
            (3, "OUT_OF_BOUNDS"),
            (4, "ITEM_NOT_FOUND"),
            (5, "INVALID_PARAMS"),
            (6, "INVALID_PARAMS"),
            (7, "INVALID_PARAMS"),
            (8, "ITEM_NOT_FREELY_PLACEABLE"),
            (10, "INVALID_PARAMS"),
        ]
        duplicate, occupied, outside = checked["conflicts"][0:3]
        assert duplicate["cell"] == [1, 0, 0]
        assert occupied["occupants"] == [{"editor_id": seeded, "item_id": 1}]
        assert outside["grid"] == {"x": 8, "y": 8, "z": 4} and outside["axes"] == [0]
        assert all(conflict["suggestion"] for conflict in checked["conflicts"])

        # The preflight is read-only: same revision, same actors, no undo entry.
        after = await snapshot(client)
        assert after["revision"] == before["revision"]
        assert after["can_undo"] == before["can_undo"]
        assert (await actors(client))["total"] == 1
        assert not any(frame["action"] == "place_actors" for frame in fake.received)


async def test_validate_layout_clean_plan_then_apply_layout_places_one_batch() -> None:
    async with flow() as (client, fake):
        before = await snapshot(client)
        placements = [
            {"item_id": 1, "cell": [1, 0, 0], "rotation_index": 1},
            {"item_id": 1, "cell": [2, 0, 0], "color_linear_rgb": [1.0, 0.0, 0.0]},
            {"item_id": 7, "transform": {"location_cm": [450.0, 50.0, 0.0], "scale": [0.0, 1.0, 1.0]}},
        ]
        checked = await call(
            client,
            "validate_layout",
            browser_session_id=BROWSER_ID,
            map_session_id=before["map_session_id"],
            expected_revision=before["revision"],
            placements=placements,
        )
        assert checked["valid"] is True and checked["conflict_count"] == 0
        assert checked["revision"] == before["revision"]
        assert (await snapshot(client))["revision"] == before["revision"]

        applied = await write(client, "apply_layout", placements=placements)
        assert applied["applied"] is True
        assert applied["atomic"] is True and applied["partial_failure"] is False
        assert applied["placed_count"] == 3 and applied["failure_count"] == 0
        assert applied["revision"] == before["revision"] + 1
        assert [frame["action"] for frame in fake.received].count("place_actors") == 1

        listed = await actors(client, expected_revision=applied["revision"])
        assert sorted(tuple(actor["cell"]) for actor in listed["actors"]) == [
            (1, 0, 0),
            (2, 0, 0),
            (4, 0, 0),
        ]


async def test_apply_layout_refuses_the_whole_plan_on_one_conflict() -> None:
    async with flow() as (client, fake):
        fake.seed(1, [0, 0, 0])
        before = await snapshot(client)
        error = await rejected(
            client,
            "apply_layout",
            browser_session_id=BROWSER_ID,
            map_session_id=before["map_session_id"],
            expected_revision=before["revision"],
            placements=[
                {"item_id": 1, "cell": [1, 0, 0]},
                {"item_id": 1, "cell": [0, 0, 0]},
            ],
        )
        assert error["code"] == "LAYOUT_CONFLICT"
        assert error["requested_count"] == 2 and error["conflict_count"] == 1
        assert error["conflicts"][0]["index"] == 1
        assert error["conflicts"][0]["reason"] == "CELL_OCCUPIED"

        after = await snapshot(client)
        assert after["revision"] == before["revision"]
        assert (await actors(client))["total"] == 1
        assert not any(frame["action"] == "place_actors" for frame in fake.received)


async def test_apply_layout_refuses_a_stale_revision_before_writing() -> None:
    async with flow() as (client, fake):
        before = await snapshot(client)
        fake.seed(1, [0, 0, 0])
        error = await rejected(
            client,
            "apply_layout",
            browser_session_id=BROWSER_ID,
            map_session_id=before["map_session_id"],
            expected_revision=before["revision"],
            placements=[{"item_id": 1, "cell": [1, 0, 0]}],
        )
        assert error["code"] == "REVISION_CONFLICT"
        assert error["revision"] == before["revision"] + 1
        assert (await actors(client))["total"] == 1
        assert not any(frame["action"] == "place_actors" for frame in fake.received)


async def test_apply_layout_checks_the_editor_writable_flags() -> None:
    for editor, code in (
        ({"read_only": True}, "READ_ONLY"),
        ({"busy": True}, "EDITOR_BUSY"),
        ({"editing": False}, "EDIT_MODE_REQUIRED"),
    ):
        async with flow(**editor) as (client, fake):
            current = await snapshot(client)
            error = await rejected(
                client,
                "apply_layout",
                browser_session_id=BROWSER_ID,
                map_session_id=current["map_session_id"],
                expected_revision=current["revision"],
                placements=[{"item_id": 1, "cell": [0, 0, 0]}],
            )
            assert error["code"] == code
            assert (await actors(client))["total"] == 0
            assert not any(frame["action"] == "place_actors" for frame in fake.received)
