"""A live run against a real editor tab: place a random number of actors, delete them
one by one, then undo half of those deletes.

This is the only test that touches the real browser, so it is opt-in:

    # terminal 1
    python -m calabiyau_map_engine_mcp pair            # prints the pairing token
    python -m calabiyau_map_engine_mcp --transport http --port 8765

    # editor: Settings > MCP, paste the pairing token, connect

    # terminal 2
    CALABIYAU_MCP_LIVE=1 python -m pytest tests/test_live_browser.py -q -s

Everything else in this suite runs against a fake editor. This one exists to prove the
real adapter accepts the same sequence and that the visible map follows it.

The map is left with the restored actors in place, because undoing half is the point of
the scenario. Delete them (or press Ctrl+Z again) when you are done looking.
"""

from __future__ import annotations

import os
import random
from typing import Any

import pytest
from harness import call, mcp_client

from calabiyau_map_engine_mcp.config import config_directory, load_credentials

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CALABIYAU_MCP_LIVE") != "1",
        reason="needs a paired editor tab; set CALABIYAU_MCP_LIVE=1 to run",
    ),
]

BASE = os.environ.get("CALABIYAU_MCP_URL", "http://127.0.0.1:8765")
PAGE = 500


async def all_actors(client, browser_session_id: str) -> list[dict[str, Any]]:
    """Page through every actor; the map can hold more than one page."""
    collected: list[dict[str, Any]] = []
    while True:
        page = await call(
            client,
            "list_actors",
            browser_session_id=browser_session_id,
            offset=len(collected),
            limit=PAGE,
        )
        collected.extend(page["actors"])
        if not page["actors"] or len(collected) >= page["total"]:
            return collected


def free_cells(grid: dict[str, int], taken: set[tuple[int, int, int]], count: int):
    """A random cluster of free cells, so the new actors are easy to spot on screen."""
    candidates = [
        (x, y, 0)
        for x in range(grid["x"])
        for y in range(grid["y"])
        if (x, y, 0) not in taken
    ]
    assert len(candidates) >= count, "the map has no room for the scenario"
    origin = random.choice(candidates)
    # Nearest first, so the cluster stays compact even in a corner of the grid.
    candidates.sort(key=lambda cell: (abs(cell[0] - origin[0]), abs(cell[1] - origin[1])))
    cluster = candidates[:count]
    random.shuffle(cluster)
    return cluster


async def test_place_a_random_number_delete_them_and_undo_half() -> None:
    random.seed()
    credentials = load_credentials(config_directory())

    async with mcp_client(BASE, credentials.http_token, "modern") as client:
        sessions = await call(client, "list_sessions")
        assert sessions["sessions"], (
            "No editor is paired. Start the server, then paste the pairing token in "
            "the editor under Settings > MCP."
        )
        browser_session_id = sessions["sessions"][0]["browser_session_id"]
        print(f"\npaired tab: {browser_session_id}")

        state = await call(client, "get_state", browser_session_id=browser_session_id)
        assert state["map"] is not None, "Open a map in the editor first."
        assert state["editing"] is True, "Press Ctrl+E in the editor to enter edit mode."
        assert state["read_only"] is False, f"map is read-only: {state['read_only_reason']}"
        assert state["busy"] is False, "the editor is busy; try again when it is idle"

        baseline = await all_actors(client, browser_session_id)
        taken = {tuple(actor["cell"]) for actor in baseline}
        print(f"baseline actors: {len(baseline)}")

        catalog = await call(
            client, "list_items", browser_session_id=browser_session_id, limit=PAGE
        )
        placeable = [
            item
            for item in catalog["items"]
            if item["placeable"] and not item["connect_item_id"]
        ]
        assert placeable, "the item catalog has no standalone placeable item"
        item = random.choice(placeable)

        count = random.randint(4, 8)
        cells = free_cells(state["grid"], taken, count)
        print(f"placing {count} x item {item['item_id']} ({item['name']}) at {cells}")

        placed: list[int] = []
        for cell in cells:
            current = await call(client, "get_state", browser_session_id=browser_session_id)
            result = await call(
                client,
                "place_actor",
                browser_session_id=browser_session_id,
                map_session_id=current["map_session_id"],
                expected_revision=current["revision"],
                item_id=item["item_id"],
                cell=list(cell),
                rotation_index=random.randint(0, 3),
            )
            actor = result["actor"]
            assert actor is not None, f"the editor accepted the write but placed nothing at {cell}"
            assert actor["cell"] == list(cell), actor
            placed.append(actor["editor_id"])

        after_place = await all_actors(client, browser_session_id)
        assert len(after_place) == len(baseline) + count
        assert set(placed) <= {actor["editor_id"] for actor in after_place}
        print(f"placed editor_ids: {placed}")

        for editor_id in placed:
            current = await call(client, "get_state", browser_session_id=browser_session_id)
            result = await call(
                client,
                "delete_actors",
                browser_session_id=browser_session_id,
                map_session_id=current["map_session_id"],
                expected_revision=current["revision"],
                actor_ids=[editor_id],
            )
            assert editor_id in result["deleted_actor_ids"], result

        after_delete = await all_actors(client, browser_session_id)
        assert len(after_delete) == len(baseline)
        assert not (set(placed) & {actor["editor_id"] for actor in after_delete})
        print(f"deleted all {count}; map is back to {len(after_delete)} actors")

        undo_count = (count + 1) // 2
        for step in range(undo_count):
            current = await call(client, "get_state", browser_session_id=browser_session_id)
            result = await call(
                client,
                "undo",
                browser_session_id=browser_session_id,
                map_session_id=current["map_session_id"],
                expected_revision=current["revision"],
            )
            assert result["changed"] is True, f"undo {step + 1} did nothing: {result}"

        restored = await all_actors(client, browser_session_id)
        expected = set(placed[len(placed) - undo_count :])
        assert len(restored) == len(baseline) + undo_count
        assert expected <= {actor["editor_id"] for actor in restored}
        print(f"undid {undo_count}; restored {sorted(expected)}")
        print(f"final actor count: {len(restored)} (baseline {len(baseline)})")
