from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from mcp import types
from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field, FiniteFloat, StrictInt

from . import __version__
from .bridge import BridgeError, BrowserRegistry

MAX_ACTOR_IDS = 256

Revision = Annotated[StrictInt, Field(ge=0)]
Offset = Annotated[StrictInt, Field(ge=0)]
Limit = Annotated[StrictInt, Field(ge=1, le=500)]
ActorIds = Annotated[list[StrictInt], Field(max_length=MAX_ACTOR_IDS)]
# An empty list means "disconnect" on the native path, so the schema must reject it here.
ChildActorIds = Annotated[list[StrictInt], Field(min_length=1, max_length=MAX_ACTOR_IDS)]
Cell = tuple[StrictInt, StrictInt, StrictInt]

# A cold client learns the contract from exactly two places: this brief, and tools/list. The
# brief carries the rules that span tools; each description below carries the preconditions a
# model must satisfy before calling that one. Both are written for a reader that has never seen
# the editor, so they spell out units, limits and failure codes instead of assuming context.
INSTRUCTIONS = (
    "Operate one open CalabiYau Map Engine browser tab. The browser owns all map state "
    "(actors, links, undo history, autosave); this server only forwards requests to it.\n"
    "\n"
    "Start with list_sessions and pick a browser_session_id. If it returns no sessions, stop "
    "and ask the user to open the editor and pair it — never invent an id. Then call get_state "
    "once to learn map_session_id, revision, grid, selection and editing.\n"
    "\n"
    "Every write needs map_session_id plus expected_revision from the latest get_state or from "
    "the previous write's result. Writes also require editing=true, read_only=false and "
    "busy=false, and each target actor must report editable=true. No tool can switch edit mode: "
    "when editing is false, ask the user to press Ctrl+E.\n"
    "\n"
    "Cells are integer [x,y,z] with z up and must lie inside grid. rotation_index is one of "
    "0..3, not degrees. color_linear_rgb is linear RGB in 0..1, not sRGB. actor_ids accept at "
    "most 256 ids per call; list tools page 1..500 with a default of 100.\n"
    "\n"
    "Failures: on REVISION_CONFLICT re-read get_state and re-plan; never resend the same write. "
    "On timeout or disconnect the outcome is unknown and the change may already have landed, so "
    "read state before deciding and never replay it. Re-delivery of one request id is safe "
    "because the browser caches responses; issuing a new call is not.\n"
    "\n"
    "Pair kits (items whose connect_item_id is non-zero) cannot be placed or connected through "
    "MCP; only the editor places them. connect_actors replaces the main actor's group and needs "
    "the main item's child_item_id to match every child's item_id. delete_actors also removes "
    "pair partners. There are no file, shell or JavaScript tools, and no save tool: the editor "
    "autosaves."
)

TOOL_DESCRIPTIONS = {
    "list_sessions": (
        "Start here. List paired browser tabs and their browser_session_id. An empty list "
        "means no editor is connected — ask the user to open the map and pair it, do not "
        "guess an id or retry in a loop."
    ),
    "get_state": (
        "Read the map's live state and the two values every write needs. Returns "
        "map_session_id, revision, grid, selection, editing, read_only, busy, dirty, "
        "can_undo/can_redo with labels, and persistent_actor_count. Call it once after "
        "list_sessions, and again after every conflict, timeout or human edit."
    ),
    "list_items": (
        "Search the baked item catalog by id, name, description or category. Read placeable, "
        "connect_item_id and child_item_id before placing or connecting: a non-zero "
        "connect_item_id marks a pair kit that MCP cannot place, and a main actor's "
        "child_item_id must equal the item_id of every child attached to it."
    ),
    "list_actors": (
        "Page placed actors and obtain the editor_id values that writes take. Filter by "
        "item_id or selected_only. Returns cell, transform, color_linear_rgb, selected and "
        "editable per actor, plus total for counting. Pin map_session_id and "
        "expected_revision to keep pages consistent while the map changes."
    ),
    "list_links": (
        "List links: pair kits, ordered groups, and orphan repair segments. Filter with "
        "actor_ids or kind; an actor belongs to at most one link, and child_actor_ids order is "
        "the group's path order. Read this before connect_actors or disconnect_actors so you "
        "know what the change will replace."
    ),
    "connect_actors": (
        "Rebuild one group by attaching children under a main actor, in the given order. That "
        "order is the group's path order. The main actor's item must declare a child item id "
        "and every child must be that item; the group is rebuilt replace-only, so read "
        "list_items child_item_id and list_links first. Actors that are half of a pair kit are "
        "rejected — break that pair with disconnect_actors before connecting them. Requires "
        "edit mode."
    ),
    "disconnect_actors": (
        "Break links at the given actors: pairs are dropped whole, groups split into segments. "
        "Use this first when a pair member has to take part in a group, since connect_actors "
        "rejects pair members. Requires edit mode."
    ),
    "select_actors": (
        "Change the editor's selection; up to 256 editor ids per call. mode replace (the "
        "default) replaces the selection, add extends it, toggle flips each id; an empty "
        "replace clears it. Selection is a UI aid only — writes take explicit actor_ids, "
        "never the selection. Requires edit mode."
    ),
    "place_actor": (
        "Place one ordinary item in an empty grid cell and return its new editor_id. The cell "
        "must lie inside grid and be unoccupied (CELL_OCCUPIED otherwise), and the item must be "
        "placeable with a zero connect_item_id (ITEM_NOT_PLACEABLE otherwise, which is how pair "
        "kits are excluded). rotation_index is 0..3, not degrees. color_linear_rgb is linear "
        "RGB in 0..1, not sRGB, and only applies to colorable items. Requires edit mode."
    ),
    "move_actors": (
        "Move ordinary actors by an integer cell delta — relative, not absolute. Fails with "
        "MOVE_REJECTED when the destination cells are blocked or occupied. Read the returned "
        "cells instead of assuming the actors ended up where you asked. Requires edit mode."
    ),
    "delete_actors": (
        "Delete actors by editor_id. Undoable. Pair partners of the given ids are deleted too, "
        "so read deleted_actor_ids for what was actually removed rather than assuming it "
        "matches the ids you sent. Requires edit mode."
    ),
    "undo": (
        "Undo the editor's latest combined history entry, including a human edit. Check "
        "get_state undo_label first and confirm with the user when the entry is not yours: "
        "this steps back one entry in the shared history, not one of your own calls. Requires "
        "edit mode."
    ),
    "redo": (
        "Redo the latest undone combined history entry, including a human edit. Requires edit "
        "mode."
    ),
}


def result(value: dict[str, Any], *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
        structured_content=value,
        is_error=error,
    )


def create_mcp(registry: BrowserRegistry) -> MCPServer:
    mcp = MCPServer(
        "CalabiYau Map Engine",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    async def invoke(
        action: str, browser_session_id: str, ctx: Context, **params: Any
    ) -> types.CallToolResult:
        try:
            await ctx.report_progress(0, total=1, message="Dispatching to paired browser")
            value = await registry.call(browser_session_id, action, params)
            await ctx.report_progress(1, total=1, message="Browser operation confirmed")
            return result(value)
        except BridgeError as exc:
            return result({"error": exc.payload()}, error=True)

    read_only = types.ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
    modify = types.ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
    )

    @mcp.tool(annotations=read_only, description=TOOL_DESCRIPTIONS["list_sessions"])
    async def list_sessions() -> types.CallToolResult:
        return result({"sessions": registry.sessions()})

    @mcp.tool(annotations=read_only, description=TOOL_DESCRIPTIONS["get_state"])
    async def get_state(browser_session_id: str, ctx: Context) -> types.CallToolResult:
        return await invoke("get_state", browser_session_id, ctx)

    @mcp.tool(annotations=read_only, description=TOOL_DESCRIPTIONS["list_items"])
    async def list_items(
        browser_session_id: str,
        ctx: Context,
        query: Annotated[str, Field(max_length=200)] = "",
        offset: Offset = 0,
        limit: Limit = 100,
    ) -> types.CallToolResult:
        return await invoke(
            "list_items", browser_session_id, ctx, query=query, offset=offset, limit=limit
        )

    @mcp.tool(annotations=read_only, description=TOOL_DESCRIPTIONS["list_actors"])
    async def list_actors(
        browser_session_id: str,
        ctx: Context,
        offset: Offset = 0,
        limit: Limit = 100,
        item_id: StrictInt | None = None,
        selected_only: bool = False,
        map_session_id: str | None = None,
        expected_revision: Revision | None = None,
    ) -> types.CallToolResult:
        params = {
            "offset": offset,
            "limit": limit,
            "selected_only": selected_only,
            **({"item_id": item_id} if item_id is not None else {}),
            **({"map_session_id": map_session_id} if map_session_id is not None else {}),
            **({"expected_revision": expected_revision} if expected_revision is not None else {}),
        }
        return await invoke("list_actors", browser_session_id, ctx, **params)

    @mcp.tool(annotations=read_only, description=TOOL_DESCRIPTIONS["list_links"])
    async def list_links(
        browser_session_id: str,
        ctx: Context,
        actor_ids: ActorIds | None = None,
        kind: Literal["pair", "group", "orphan"] | None = None,
        offset: Offset = 0,
        limit: Limit = 100,
        map_session_id: str | None = None,
        expected_revision: Revision | None = None,
    ) -> types.CallToolResult:
        params = {
            "offset": offset,
            "limit": limit,
            **({"actor_ids": actor_ids} if actor_ids is not None else {}),
            **({"kind": kind} if kind is not None else {}),
            **({"map_session_id": map_session_id} if map_session_id is not None else {}),
            **({"expected_revision": expected_revision} if expected_revision is not None else {}),
        }
        return await invoke("list_links", browser_session_id, ctx, **params)

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["connect_actors"])
    async def connect_actors(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        main_actor_id: StrictInt,
        child_actor_ids: ChildActorIds,
        ctx: Context,
    ) -> types.CallToolResult:
        return await invoke(
            "connect_actors",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
            main_actor_id=main_actor_id,
            child_actor_ids=child_actor_ids,
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["disconnect_actors"])
    async def disconnect_actors(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        actor_ids: ActorIds,
        ctx: Context,
    ) -> types.CallToolResult:
        return await invoke(
            "disconnect_actors",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
            actor_ids=actor_ids,
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["select_actors"])
    async def select_actors(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        actor_ids: ActorIds,
        ctx: Context,
        mode: Literal["replace", "add", "toggle"] = "replace",
    ) -> types.CallToolResult:
        return await invoke(
            "select_actors",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
            actor_ids=actor_ids,
            mode=mode,
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["place_actor"])
    async def place_actor(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        item_id: StrictInt,
        cell: Cell,
        ctx: Context,
        rotation_index: Annotated[StrictInt, Field(ge=0, le=3)] = 0,
        color_linear_rgb: tuple[FiniteFloat, FiniteFloat, FiniteFloat] | None = None,
    ) -> types.CallToolResult:
        return await invoke(
            "place_actor",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
            item_id=item_id,
            cell=cell,
            rotation_index=rotation_index,
            **({"color_linear_rgb": color_linear_rgb} if color_linear_rgb is not None else {}),
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["move_actors"])
    async def move_actors(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        actor_ids: ActorIds,
        delta: Cell,
        ctx: Context,
    ) -> types.CallToolResult:
        return await invoke(
            "move_actors",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
            actor_ids=actor_ids,
            delta=delta,
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["delete_actors"])
    async def delete_actors(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        actor_ids: ActorIds,
        ctx: Context,
    ) -> types.CallToolResult:
        return await invoke(
            "delete_actors",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
            actor_ids=actor_ids,
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["undo"])
    async def undo(
        browser_session_id: str, map_session_id: str, expected_revision: Revision, ctx: Context
    ) -> types.CallToolResult:
        return await invoke(
            "undo",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["redo"])
    async def redo(
        browser_session_id: str, map_session_id: str, expected_revision: Revision, ctx: Context
    ) -> types.CallToolResult:
        return await invoke(
            "redo",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
        )

    return mcp
