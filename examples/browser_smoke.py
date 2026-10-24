"""Exercise a disposable, open editor map. Never run this against a personal map."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from calabiyau_map_engine_mcp.config import load_credentials


async def verify(url: str, directory: Path) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("The smoke client only sends credentials to a loopback HTTP endpoint.")
    credentials = load_credentials(directory)
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {credentials.http_token}"}, trust_env=False
    ) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:

            async def call(name: str, **params):
                response = await client.call_tool(name, params)
                if response.is_error:
                    raise AssertionError(f"{name}: {response.structured_content}")
                return response.structured_content

            sessions = (await call("list_sessions"))["sessions"]
            assert len(sessions) == 1, "Use a dedicated service and one disposable browser tab."
            browser_id = sessions[0]["browser_session_id"]

            async def read(name: str, **params):
                return await call(name, browser_session_id=browser_id, **params)

            state = await read("get_state")
            assert state["editing"] and not state["busy"] and not state["read_only"]
            assert state["map_session_id"], "Open a disposable map before running the test."
            items = await read("list_items", limit=500)
            item = next(
                entry
                for entry in items["items"]
                if entry["placeable"] and entry["transform_mode"] == 1
            )
            actors = await read("list_actors", limit=500)
            assert actors["total"] == 0, "The smoke test requires an empty disposable map."
            assert all(state["grid"][axis] > 5 for axis in ("x", "y", "z"))
            snapshot = {
                "map_session_id": state["map_session_id"],
                "expected_revision": state["revision"],
            }
            original = dict(snapshot)

            async def write(name: str, **params):
                value = await read(name, **snapshot, **params)
                snapshot["expected_revision"] = value["revision"]
                return value

            placed = await write("place_actor", item_id=item["item_id"], cell=[3, 3, 1])
            editor_id = placed["actor"]["editor_id"]
            assert placed["changed"]
            stale = await client.call_tool(
                "delete_actors",
                {
                    "browser_session_id": browser_id,
                    **original,
                    "actor_ids": [editor_id],
                },
            )
            assert stale.is_error
            assert stale.structured_content["error"]["code"] == "REVISION_CONFLICT"
            selected = await write("select_actors", actor_ids=[editor_id])
            assert selected["selection"] == [editor_id]
            moved = await write("move_actors", actor_ids=[editor_id], delta=[1, 0, 0])
            assert moved["actors"][0]["cell"] == [4, 3, 1]
            await write("undo")
            assert (await read("list_actors"))["actors"][0]["cell"] == [3, 3, 1]
            await write("redo")
            assert (await read("list_actors"))["actors"][0]["cell"] == [4, 3, 1]
            deleted = await write("delete_actors", actor_ids=[editor_id])
            assert editor_id in deleted["deleted_actor_ids"]
            assert (await read("list_actors"))["total"] == 0
            await write("undo")
            assert (await read("list_actors"))["total"] == 1
            await write("redo")
            assert (await read("list_actors"))["total"] == 0
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "map_session_id": state["map_session_id"],
                        "verified": [
                            "list_sessions",
                            "get_state",
                            "list_items",
                            "list_actors",
                            "place_actor",
                            "select_actors",
                            "move_actors",
                            "delete_actors",
                            "undo",
                            "redo",
                            "stale_revision_rejected",
                        ],
                        "revision": snapshot["expected_revision"],
                    }
                )
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18765/mcp")
    parser.add_argument("--config-dir", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(verify(args.url, args.config_dir))
