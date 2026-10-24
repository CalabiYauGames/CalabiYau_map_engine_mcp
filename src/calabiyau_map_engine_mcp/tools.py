from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from mcp import types
from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field, FiniteFloat, StrictInt

from . import __version__
from .bridge import BridgeError, BrowserRegistry

Revision = Annotated[StrictInt, Field(ge=0)]
Offset = Annotated[StrictInt, Field(ge=0)]
Limit = Annotated[StrictInt, Field(ge=1, le=500)]
ActorIds = Annotated[list[StrictInt], Field(max_length=256)]
Cell = tuple[StrictInt, StrictInt, StrictInt]


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
        instructions=(
            "Control only a paired, open CalabiYau Map Engine browser. First list_sessions and "
            "get_state. Use editor_id, not native actor_id, to identify actors. Each modification "
            "requires the latest map_session_id and revision. After a timeout or disconnect "
            "query state before deciding what to do; never replay an uncertain change. "
            "Coordinates are integer game grid [x,y,z], z up. Writes require edit mode. "
            "The editor's existing autosave can persist changes. No file or JavaScript tools exist."
        ),
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

    @mcp.tool(annotations=read_only)
    async def list_sessions() -> types.CallToolResult:
        """List paired browser tabs. Explicitly choose a browser_session_id for other tools."""
        return result({"sessions": registry.sessions()})

    @mcp.tool(annotations=read_only)
    async def get_state(browser_session_id: str, ctx: Context) -> types.CallToolResult:
        """Read current map, revision, grid, selection, editability, dirty and combined history."""
        return await invoke("get_state", browser_session_id, ctx)

    @mcp.tool(annotations=read_only)
    async def list_items(
        browser_session_id: str,
        ctx: Context,
        query: Annotated[str, Field(max_length=200)] = "",
        offset: Offset = 0,
        limit: Limit = 100,
    ) -> types.CallToolResult:
        """Search the editor's baked item catalog. Some items are queryable but not placeable."""
        return await invoke(
            "list_items", browser_session_id, ctx, query=query, offset=offset, limit=limit
        )

    @mcp.tool(annotations=read_only)
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
        """Page live actors using editor_id. Pin session and revision for consistent pagination."""
        params = {
            "offset": offset,
            "limit": limit,
            "selected_only": selected_only,
            **({"item_id": item_id} if item_id is not None else {}),
            **({"map_session_id": map_session_id} if map_session_id is not None else {}),
            **({"expected_revision": expected_revision} if expected_revision is not None else {}),
        }
        return await invoke("list_actors", browser_session_id, ctx, **params)

    @mcp.tool(annotations=modify)
    async def select_actors(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        actor_ids: ActorIds,
        ctx: Context,
        mode: Literal["replace", "add", "toggle"] = "replace",
    ) -> types.CallToolResult:
        """Select up to 256 editor IDs. An empty replace clears selection."""
        return await invoke(
            "select_actors",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
            actor_ids=actor_ids,
            mode=mode,
        )

    @mcp.tool(annotations=modify)
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
        """Place one ordinary item in an empty grid cell. Color is linear RGB, not sRGB."""
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

    @mcp.tool(annotations=modify)
    async def move_actors(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        actor_ids: ActorIds,
        delta: Cell,
        ctx: Context,
    ) -> types.CallToolResult:
        """Move ordinary actors by integer grid cells; inspect actual returned positions."""
        return await invoke(
            "move_actors",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
            actor_ids=actor_ids,
            delta=delta,
        )

    @mcp.tool(annotations=modify)
    async def delete_actors(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        actor_ids: ActorIds,
        ctx: Context,
    ) -> types.CallToolResult:
        """Delete ordinary actors. Linked partners may also be deleted by existing editor rules."""
        return await invoke(
            "delete_actors",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
            actor_ids=actor_ids,
        )

    @mcp.tool(annotations=modify)
    async def undo(
        browser_session_id: str, map_session_id: str, expected_revision: Revision, ctx: Context
    ) -> types.CallToolResult:
        """Undo the editor's latest combined history entry, including a human edit."""
        return await invoke(
            "undo",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
        )

    @mcp.tool(annotations=modify)
    async def redo(
        browser_session_id: str, map_session_id: str, expected_revision: Revision, ctx: Context
    ) -> types.CallToolResult:
        """Redo the editor's latest combined history entry."""
        return await invoke(
            "redo",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
        )

    return mcp
