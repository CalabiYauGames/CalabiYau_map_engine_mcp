from __future__ import annotations

import base64
import json
import math
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp import types
from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictInt

from . import __version__
from .bridge import BridgeError, BrowserRegistry
from .capture import CaptureRelay
from .config import screenshots_directory

MAX_ACTOR_IDS = 256
MAX_FILL_CELLS = 4096
MAX_BATCH_PLACEMENTS = 4096
MAX_ARRAY_COUNT = 256
MAX_SPHERE_RADIUS = 32
MAX_REGION_VOLUME = 32768
# Item 7 (斜坡) is the only item the game stores with a full float transform, and
# `quad_to_transform` divides by its measured 1 m x 1 m face. A quad therefore
# always becomes an item-7 plate; naming another item would need that item's own
# extents, which the catalog does not carry.
PLATE_ITEM_ID = 7
# Captures are base64 data URLs on the same 1 MiB bridge frame budget as every other response.
SCREENSHOT_DIR = screenshots_directory()

Revision = Annotated[StrictInt, Field(ge=0)]
ExpectedRevision = Annotated[StrictInt | Literal["latest"], Field(description='Use latest to explicitly skip CAS.')]
Offset = Annotated[StrictInt, Field(ge=0)]
Limit = Annotated[StrictInt, Field(ge=1, le=4096)]
ActorIds = Annotated[list[StrictInt], Field(max_length=MAX_ACTOR_IDS)]
# Duplicating reads the set first, so an empty selection is a caller mistake, not a no-op.
SourceActorIds = Annotated[list[StrictInt], Field(min_length=1, max_length=MAX_ACTOR_IDS)]
# An empty list means "disconnect" on the native path, so the schema must reject it here.
ChildActorIds = Annotated[list[StrictInt], Field(min_length=1, max_length=MAX_ACTOR_IDS)]
Cell = tuple[StrictInt, StrictInt, StrictInt]
# Camera coordinates are world-space metres, so they are fractions, not grid cells.
Point3 = tuple[FiniteFloat, FiniteFloat, FiniteFloat]


class FreePose(BaseModel):
    """A complete free pose, in the units the map record stores.

    Only items whose list_items entry reports free_transform true can hold one;
    everything else persists a grid cell and rejects these outright. The pivot is
    not a corner: item 7's sits in the middle of its 1x1 m face, so standing one
    on the floor means location_cm z = 50 x scale z, and a pivot at z = 0 leaves
    half of the plate underground.
    """

    model_config = ConfigDict(extra="forbid")

    location_cm: Point3 = Field(description="Pivot in Unreal centimetres.")
    scale: Point3 = Field(
        default=(1.0, 1.0, 1.0),
        description="Per-axis scale. Zero is legal: a plate with scale x 0 has no thickness.",
    )
    rotation_degrees: Point3 = Field(
        default=(0.0, 0.0, 0.0),
        description="Unreal pitch, yaw, roll in degrees. Any angle, not just 90s.",
    )

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
    "0..3, not degrees; rotation_degrees is any angle and only works on free_transform items. "
    "color_linear_rgb is linear RGB in 0..1, not sRGB. actor_ids accept at most 256 ids per "
    "call; list tools page 1..500 with a default of 100.\n"
    "\n"
    "Build from voxels. list_items reports build_kind for every item: voxel is a plain 1x1 "
    "grid block, component is a decoration with its own footprint, facing or scale, and "
    "functional is a gameplay prefab. Mass geometry only from voxel items. A component may be "
    "placed only when the user asked for that component — asking for a door, window, tree, "
    "eave or dome is such a request, guessing one is not — and a component is never a "
    "stand-in for voxel massing. When one is genuinely needed, inspect it before committing: "
    "place a single instance at a free cell in open space, capture three views (set_camera to "
    "front, side and top, capture_view after each, standing several cells back so the camera is "
    "not inside neighbouring geometry), then read off its footprint in cells, which way its "
    "front faces, and whether colorable is true, because a non-colorable component ignores "
    "color_linear_rgb. Delete the probe when you are done.\n"
    "\n"
    "Model surfaces with plates. Fine modelling uses item 7 (斜坡) iron plates for all surfaces, "
    "edges and detail. Budget by actor count, not by ambition: the engine batches per actor, so "
    "one plate and one voxel cost the same, and a map that stays under about 10,000 actors is the "
    "one that still runs in game. A whole model is normally a few thousand pieces, and a single "
    "detail pass is hundreds -- if a plan wants tens of thousands, the shape is too fine for the "
    "grid and the resolution is what should come down. Item 7 is the one common item the game stores with a "
    "full float transform, and list_items marks it free_transform true. Its measured box is a "
    "1 m x 1 m face 0.25 m thick, pivoted at the CENTRE of that face, with the thickness "
    "growing towards local -X; local_extents_cm states exactly that, so standing one on the "
    "floor is location_cm z = 50 x scale z, and a pivot at z = 0 buries half the plate. Place "
    "plates with transform, never with cell: a cell anchors the pivot to the cell base, which "
    "is precisely the half-buried pose. scale is read in metres on the two face axes, so "
    "scale [0, 2.5, 1.2] is a 2.5 m by 1.2 m panel with no thickness, and rotation_degrees "
    "takes any pitch, yaw and roll, which is how one plate becomes a ramp, a chamfer, or one "
    "facet of a curved wall. Plates claim no cell, so they may overlap each other and the "
    "voxels around them; nothing reports CELL_OCCUPIED for them. Use plates for surfaces, "
    "edges and detail, and voxels for anything with volume, because a "
    "plate is a single face and has no inside. When a shape is too complex to place by hand, "
    "model it as a mesh and let the model tool fit plates and voxels to it instead.\n"
    "\n"
    "Build from a model file when the shape is worth more than a handful of boxes. Write the "
    "mesh yourself as .obj — it is plain text and needs no tooling — then import_model takes "
    "its path on the machine running this server and the browser voxelises it. The body "
    "becomes cubic blocks and, in adaptive mode, eligible surfaces are replaced by fitted item "
    "7 plates, which is how a roof pitch or a curve gets finer than one cell. origin is the "
    "min corner of the footprint at the figure's feet and is required when you import a new "
    "model; a plan_token already holds the origin it was fitted at, so applying one needs no "
    "origin. resolution is the voxel "
    "count along the up axis and, because ordinary blocks are one cell, also the finished "
    "height, so 12 to 20 is a building and 48 is a tower. mode full-size is the one that "
    "covers the whole surface in plates and it needs target_size instead of resolution: use it "
    "when the model is mostly smooth and you want the detail a cell cannot give. Import with "
    "dry_run first: it "
    "returns the block count, the bounds and a plan_token, and writes nothing. Call it again "
    "with that plan_token once the numbers look right, or change origin or resolution and "
    "start over. discard_model throws an upload away. It refuses to overwrite existing actors "
    "unless overwrite is true, so import "
    "into empty space or clear the area first. Afterwards capture_view and correct what the "
    "voxel grid could not express with update_actors and place_actor transform.\n"
    "\n"
    "Failures: on REVISION_CONFLICT re-read get_state and re-plan; never resend the same write. "
    "On timeout or disconnect the outcome is unknown and the change may already have landed, so "
    "read state before deciding and never replay it. Re-delivery of one request id is safe "
    "because the browser caches responses; issuing a new call is not.\n"
    "\n"
    "Pair kits (items whose connect_item_id is non-zero) cannot be placed or connected through "
    "MCP; only the editor places them. connect_actors replaces the main actor's group and needs "
    "the main item's child_item_id to match every child's item_id. delete_actors also removes "
    "pair partners. copy_actors, mirror_actors and array_actors rebuild actors, never links: a "
    "duplicated group member arrives standalone, so re-join it with connect_actors. The only "
    "filesystem access is import_model reading a model file you wrote yourself; there are no "
    "shell or JavaScript tools, and no save tool: the editor autosaves."
)

TOOL_DESCRIPTIONS = {
    "list_sessions": (
        "Start here. List paired browser tabs and their browser_session_id. An empty list "
        "means no editor is connected — ask the user to open the map and pair it, do not "
        "guess an id or retry in a loop."
    ),
    "place_quad": (
        "Place a rectangular surface from four corner vertices. AI gives world-space "
        "coordinates for the four corners of a planar rectangular face (units: centimetres), "
        "and the tool calculates the center, scale, and rotation needed to position item 7 "
        "(斜坡/plate) there. This abstracts detailed parameter calculation: AI models geometry "
        "as vertices, not transforms. Corners must form a valid planar rectangle (opposite "
        "edges equal, diagonals equal). Returns the placed actor. Requires edit mode."
    ),
    "place_quads": (
        "Batch place multiple rectangular surfaces from vertex arrays. Each quad spec provides "
        "four corner vertices (centimetres) and optional color. The tool calculates transforms "
        "for all quads and places them atomically through place_actors. This is the primary "
        "tool for AI surface modeling: describe geometry as vertex arrays, not transform arrays. "
        "Up to 4096 quads per batch. Returns placed actors and any errors. Requires edit mode."
    ),
    "get_state": (
        "Read the map's live state and the two values every write needs. Returns "
        "map_session_id, revision, grid, selection, editing, read_only, busy, busy_reasons, "
        "drag_active, history_jump_available, requires_coordinated_traversal, dirty, "
        "can_undo/can_redo with labels, and persistent_actor_count. Call it once after "
        "list_sessions, and again after every conflict, timeout or human edit."
    ),
    "get_camera": (
        "Read the viewport camera's world-space position and the point it looks at. Use it to "
        "record a view before moving it, or to work out where to stand for the next capture. "
        "Read-only, no edit mode required."
    ),
    "set_camera": (
        "Move the viewport camera to a world-space position looking at a target point, and "
        "refit the clip planes to the drawn map. Coordinates are metres, not grid cells: the "
        "map starts at the origin and extends along negative X and Z. This changes only the "
        "view, never the map, so it needs neither edit mode nor a revision. Read-only hint is "
        "conservatively false: it is not a map observation."
    ),
    "capture_view": (
        "Draw one frame of the viewport and save it as an image, so the model can see what it "
        "built instead of inferring the shape from actor lists. Optionally move the camera first "
        "with position and target (both or neither, metres). width 64..2560 and height 64..2000 "
        "default to 960x540; a size larger than the viewport is upscaled, not re-rendered, so "
        "asking for more pixels than the window has buys no detail. format is jpeg (default) or "
        "png, quality 0.1..1 applies to jpeg. "
        "Returns the saved path, byte size and the camera pose used. The image is written on the "
        "machine running this server, under its screenshots directory. Three captures taken 90 "
        "degrees apart around one cell are how a component's facing and footprint are read: "
        "set_camera, capture_view, repeat. Stand several cells back, because a camera closer "
        "than about five cells ends up inside the hedges, lights and walls around the target."
    ),
    "list_items": (
        "Search the baked item catalog by id, name, description or category. build_kind says "
        "what an item is for: voxel is a plain 1x1 grid block and the only kind to mass "
        "geometry with, component is a decoration whose real footprint and facing are not in "
        "the catalog, functional is a gameplay prefab. free_transform is true when the item "
        "stores a complete pose, so it accepts place_actor transform and update_actors "
        "location_cm/scale/rotation_degrees; every other item only takes a cell. "
        "local_extents_cm is present only for items whose box has been measured, and says "
        "where that box reaches from the pivot at scale 1, which is what the bounds check "
        "uses. Read colorable too, since a non-colorable item ignores color_linear_rgb. Read "
        "placeable, connect_item_id and child_item_id before placing or connecting: a "
        "non-zero connect_item_id marks a pair kit that MCP cannot place, and a main actor's "
        "child_item_id must equal the item_id of every child attached to it."
    ),
    "list_actors": (
        "Page placed actors and obtain the editor_id values that writes take. Filter by "
        "item_id, selected_only, or cell_box={min:[x,y,z],max:[x,y,z]}. Pages allow up to 4096 "
        "rows. Returns cell, transform, color_linear_rgb, selected and editable per actor, plus "
        "total for counting. Pin map_session_id and expected_revision to keep pages consistent."
    ),
    "list_links": (
        "List links: pair kits, ordered groups, and orphan repair segments. Filter with "
        "actor_ids or kind; an actor belongs to at most one link, and child_actor_ids order is "
        "the group's path order. Read this before connect_actors or disconnect_actors so you "
        "know what the change will replace."
    ),
    "create_scene_object": (
        "Create a durable project group from actor editor_ids. This is separate from native links: it persists an editable SceneObject and owns the selected actors. Requires a non-empty unique actor_ids list and a name. Requires edit mode and a writable project."
    ),
    "delete_scene_object": (
        "Delete a durable project group by object_id, including all of its member actors. This is undoable and removes the corresponding editor actors. Requires edit mode and a writable project."
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
        "Place one item and return its new editor_id. Give exactly one of cell or transform. "
        "cell is a grid cell that must lie inside grid and be unoccupied (CELL_OCCUPIED "
        "otherwise). transform is a complete pose {location_cm, scale, rotation_degrees} in "
        "Unreal centimetres, metres and degrees, and only an item whose free_transform is "
        "true accepts it (ITEM_NOT_FREELY_PLACEABLE otherwise, which includes every voxel "
        "block). A free placement claims no cell, so it never reports CELL_OCCUPIED and may "
        "overlap other actors. Its pivot is not a corner: read local_extents_cm to see where "
        "the piece reaches, because a box that leaves the map is refused with OUT_OF_BOUNDS "
        "naming the axes. The item must be placeable with a zero connect_item_id "
        "(ITEM_NOT_PLACEABLE otherwise, which is how pair kits are excluded). Prefer "
        "build_kind voxel for massing: a component may only be placed when the user asked "
        "for it by name and only after the three-view check in the brief. color_linear_rgb "
        "is linear RGB in 0..1, not sRGB, and only applies to colorable items. Requires edit "
        "mode."
    ),
    "update_actors": (
        "Update existing actors in one atomic write. actor_ids selects the actors; optionally "
        "provide color_linear_rgb, rotation_index, params, item_id, or the free pose fields "
        "location_cm, location_delta_cm, scale and rotation_degrees. The free fields need "
        "every target to be free_transform, and refuse the whole call otherwise with "
        "ACTOR_NOT_FREELY_PLACEABLE naming the ids that cannot store a pose. "
        "location_cm is an absolute pivot, so passing it for several actors stacks them; "
        "location_delta_cm shifts each actor by the same amount and is what a set wants. "
        "scale is metres and 0 is legal. rotation_index is a quarter turn of yaw and resets "
        "nothing else, so use rotation_degrees for a real angle. This avoids delete-and-rebuild "
        "for visual corrections. Swapping item_id keeps the transform, so swap within one "
        "build_kind: turning a voxel into a component leaves that component's real footprint "
        "somewhere the catalog never described. A non-colorable item ignores color_linear_rgb. "
        "Requires edit mode."
    ),
    "place_actors": (
        "Place multiple items as one atomic batch: one browser request, one revision, one undo "
        "entry. Each placement contains item_id and then either cell, or transform "
        "{location_cm, scale, rotation_degrees} for a free_transform item; optional "
        "rotation_index and color_linear_rgb. Returns the browser's placed_actors and "
        "placement_errors unchanged, so one bad placement does not drop the rest. The batch is "
        "limited to 4096 placements. Cell placements must be inside grid and unoccupied; free "
        "placements claim no cell, may share one, and are refused individually with "
        "OUT_OF_BOUNDS when their measured box leaves the map. This is the massing tool, so "
        "most of its placements should be build_kind voxel: a component's real footprint is "
        "larger than or offset from the cell it is anchored to, so batching components "
        "produces cells that are visually empty and geometry that overlaps. It is also how a "
        "plate assembly goes down, since a few hundred fitted plates are one gesture. "
        "Requires edit mode."
    ),
    "copy_actors": (
        "Duplicate standalone actors into cells shifted by a whole-cell offset, as one atomic "
        "batch. Actors that belong to a group are skipped because connection state is not "
        "copied; disconnect them first. A grid actor keeps its item_id, rotation_index and "
        "color_linear_rgb; a free_transform actor keeps its item_id, colour, scale and angle, "
        "and only its pivot moves, so a copied plate is the same plate somewhere else. offset "
        "[0,0,0] is refused. Cells held by the actors being copied do not count as occupied, "
        "so shifting a row by one cell lands each copy on the next source and replaces it - "
        "that is how a row of five becomes a row of six, and it is intentional. An actor "
        "outside this call still blocks its cell with CELL_OCCUPIED. Requires edit mode."
    ),
    "mirror_actors": (
        "Reflect a set of actors across the plane perpendicular to axis at coordinate origin, "
        "as one atomic batch. origin is that plane's coordinate, so 3.5 swaps cells 3 and 4 "
        "while 3 maps cell 3 onto itself (and reports CELL_OCCUPIED). axis is the plane's "
        "normal: x flips the x coordinate, y the y, z the z. Yaw follows the geometry - kept "
        "for z, and turned into 180-theta for x and -theta for y - and a free_transform actor "
        "is reflected by the same yaw rule, keeping its scale. That is exact for an upright "
        "plate; a tilted one keeps its pitch and roll rather than gaining their reflection, so "
        "set rotation_degrees by hand when the reflected tilt is what you want. Copies are "
        "standalone: links are not mirrored. A mirrored component is still the same piece - an "
        "inner roof corner stays an inner corner - so mirroring components rarely gives the "
        "shape you expect; place the opposite piece instead. Requires edit mode."
    ),
    "polar_array": (
        "Repeat actors evenly around center in the plane perpendicular to axis (x, y, or z). "
        "count includes the original; rotate_with_angle turns each copy to follow the radial "
        "angle. A free_transform actor keeps its centimetre pivot, so a ring of plates is "
        "exact rather than snapped to cells; with rotate_with_angle its yaw advances by the "
        "same angle, which is what makes a ring of facets read as a curve."
    ),
    "array_actors": (
        "Repeat a set of actors along a straight line, as one atomic batch. count is the total "
        "number of instances including the originals, and each repeat sits one step of "
        "direction * spacing further along, so count=3 direction=[0,1,0] spacing=2 adds two "
        "rows. A free_transform actor is repeated by moving its pivot the same number of "
        "centimetres, keeping its scale and angle, so a row of plates stays a row of the same "
        "plate. count is 2..256, but the batch is capped at 4096 placements, so a large source "
        "set needs a smaller count. Links are not repeated. Requires edit mode."
    ),
    "query_space": (
        "Query the 3D map space to understand layout and occupancy using list_actors. For region "
        "queries, returns all actors whose cells fall within the specified min_cell and max_cell "
        "box. This is a server-side convenience wrapper around list_actors that filters by spatial "
        "criteria. Use this to see what's already built and find free space before placing new "
        "actors. Read-only operation, no edit mode required."
    ),
    "describe_region": (
        "See the actual shape of a build: returns item counts, the real bounding box, occupancy "
        "and, for every non-empty z layer, a top-down ASCII map with a legend mapping each "
        "character to an item_id. Use it instead of reading long list_actors pages to check what "
        "a structure looks like or whether it is hollow. Read-only, no edit mode required."
    ),
    "fill_box": (
        "Build an axis-aligned box in one call. mode solid fills every cell, shell fills only the "
        "six boundary faces (top and bottom included), walls fills only the x/y boundary faces "
        "(a hollow room). Up to 4096 cells per call; split larger volumes. skip_occupied keeps "
        "going over CELL_OCCUPIED cells instead of reporting them as failures. Cells must lie "
        "inside grid; items must be placeable with zero connect_item_id and build_kind voxel, "
        "because a component does not fill the cell it is anchored to. Requires edit mode."
    ),
    "fill_sphere": (
        "Build a sphere in one call from a center cell and an integer radius 0..32. mode solid "
        "fills every cell with dx^2+dy^2+dz^2 <= radius^2, hollow keeps only the outer one-cell "
        "shell. Up to 4096 cells per call. skip_occupied skips CELL_OCCUPIED cells. items must "
        "be build_kind voxel. Requires edit mode."
    ),
    "delete_region": (
        "Delete every actor whose cell lies inside a box, optionally limited to one item_id. Ids "
        "are read first and removed in chunks of 256; pair partners are deleted too. Use "
        "describe_region or query_space first to confirm the region, because this is destructive "
        "but undoable. Requires edit mode."
    ),
    "find_free_space": (
        "Find anchors where an empty cuboid of the given size fits inside a region. Read-only. "
        "The scan first indexes occupied cells and only validates candidate cuboids near occupancy; "
        "the cost is based on indexed actors and candidates, not anchors multiplied by cuboid volume."
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
    "import_model": (
        "Build a whole structure from a model file instead of placing blocks one call at a "
        "time. Write the file yourself — .obj is the easiest to author by hand, and glb, gltf, "
        "stl, fbx, ply and .vox are also read — then pass its path on this machine. The "
        "browser loads it, voxelises it in a worker and places the result as one undoable "
        "batch, with the body in cubic blocks and, in adaptive mode, eligible surfaces fitted "
        "with item 7 plates for detail finer than a cell. origin is the min corner of the "
        "footprint at the figure's feet; it is required with a path and unnecessary with a "
        "plan_token, which already holds the origin it was fitted at. resolution is the voxel "
        "count along "
        "the model's up axis; because ordinary blocks are one cell each it is also the "
        "finished height in cells, so 16 gives a house-sized figure. Pass \"native\" to keep "
        "the file's own grid, which is what a .vox wants: it already stores one coloured "
        "voxel per cell and resampling would smear the art. mode adaptive mixes "
        "voxels with plates, voxel is blocks only, full-size covers the whole surface in "
        "plates and is the only mode that takes target_size. Pass dry_run to see the block "
        "count and bounds without writing, then call again with plan_token to apply that "
        "same plan without re-uploading. It refuses to overwrite existing actors unless "
        "overwrite is true, so a model dropped onto an existing building reports "
        "CELL_OCCUPIED instead of deleting it. After importing, look at it with capture_view "
        "and fix the details with update_actors, place_actor transform, or array_actors."
    ),
    "discard_model": (
        "Throw away an uploaded model and the plan solved from it, without placing "
        "anything. Use it when a dry_run showed the fit is wrong and you would rather "
        "start from a new file than re-import; the browser holds at most three uploads, "
        "and the oldest is evicted when a fourth arrives, so an abandoned upload is "
        "worth discarding. Takes the plan_token the dry_run returned."
    ),
}


def result(value: dict[str, Any], *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
        structured_content=value,
        is_error=error,
    )


MODEL_EXTENSIONS = frozenset({"glb", "gltf", "obj", "stl", "fbx", "ply", "vox"})
# Mirrors the browser's own ceiling. A model bigger than this is a mistake, not a building.
MAX_MODEL_BYTES = 64 * 1024 * 1024
# The bridge refuses frames over 1 MiB, and base64 costs a third again; 640 KiB of
# raw bytes becomes about 854 KiB on the wire, which leaves room for the envelope.
MODEL_CHUNK_BYTES = 640 * 1024


def _error_code(response: types.CallToolResult) -> str:
    """The error code of a refused tool result, for a caller that must branch on it."""
    content = response.structured_content or {}
    error = content.get("error") if isinstance(content, dict) else None
    return error.get("code", "") if isinstance(error, dict) else ""


def _read_model_file(path: str) -> tuple[str, bytes] | types.CallToolResult:
    """Read a model file the caller wrote, or explain why it cannot be read."""
    file = Path(path).expanduser()
    if not file.exists():
        return result(
            {
                "error": {
                    "code": "MODEL_FILE_NOT_FOUND",
                    "message": f"No file at {file}. Write the model first; the path is on the "
                    "machine running this server, not in the browser.",
                }
            },
            error=True,
        )
    if not file.is_file():
        return result(
            {"error": {"code": "INVALID_PARAMS", "message": f"{file} is not a regular file."}},
            error=True,
        )
    extension = file.suffix.lower().lstrip(".")
    if extension not in MODEL_EXTENSIONS:
        return result(
            {
                "error": {
                    "code": "INVALID_PARAMS",
                    "message": f".{extension or '?'} is not a model format; use one of: "
                    + ", ".join(sorted(MODEL_EXTENSIONS))
                    + ".",
                }
            },
            error=True,
        )
    size = file.stat().st_size
    if size == 0:
        return result(
            {"error": {"code": "INVALID_PARAMS", "message": f"{file} is empty."}}, error=True
        )
    if size > MAX_MODEL_BYTES:
        return result(
            {
                "error": {
                    "code": "MODEL_TOO_LARGE",
                    "message": f"{size} bytes exceeds the {MAX_MODEL_BYTES}-byte limit. Simplify "
                    "the model or lower its resolution before exporting.",
                }
            },
            error=True,
        )
    return file.name, file.read_bytes()


def box_cells(
    min_cell: tuple[int, int, int], max_cell: tuple[int, int, int], mode: str
) -> list[tuple[int, int, int]]:
    """Expand an axis-aligned box into the cells a fill should occupy."""
    axis = [
        (min_cell[0], max_cell[0]),
        (min_cell[1], max_cell[1]),
        (min_cell[2], max_cell[2]),
    ]
    cells: list[tuple[int, int, int]] = []
    for x in range(axis[0][0], axis[0][1] + 1):
        for y in range(axis[1][0], axis[1][1] + 1):
            for z in range(axis[2][0], axis[2][1] + 1):
                on_x = x in axis[0] and axis[0][0] != axis[0][1]
                on_y = y in axis[1] and axis[1][0] != axis[1][1]
                on_z = z in axis[2] and axis[2][0] != axis[2][1]
                if mode == "solid":
                    keep = True
                elif mode == "shell":
                    keep = on_x or on_y or on_z
                else:  # walls
                    keep = on_x or on_y
                if keep:
                    cells.append((x, y, z))
    return cells


def sphere_cells(
    center: tuple[int, int, int], radius: int, mode: str
) -> list[tuple[int, int, int]]:
    """Expand an integer sphere into cells; hollow keeps only the outer one-cell shell."""
    cx, cy, cz = center
    outer = radius * radius
    inner = max(radius - 1, 0) ** 2
    cells: list[tuple[int, int, int]] = []
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                distance = dx * dx + dy * dy + dz * dz
                if distance > outer:
                    continue
                if mode == "hollow" and distance <= inner:
                    continue
                cells.append((cx + dx, cy + dy, cz + dz))
    return cells


def mirror_rotation_index(rotation_index: int, axis: str) -> int:
    """Reflect a yaw-only rotation index across a plane whose normal is `axis`.

    Yaw runs from +X towards +Y, so a heading theta reflected in the x plane becomes
    180-theta and one reflected in the y plane becomes -theta; the horizontal z plane
    leaves it alone, because flipping height does not change where a thing faces.
    """
    if axis == "z":
        return rotation_index & 3
    return ((2 if axis == "x" else 0) - rotation_index) & 3


def mirror_free_rotation(rotation: list[float], axis: str) -> list[float]:
    """Reflect a free pose's yaw the way the grid mirror reflects a rotation index.

    Exact for an upright plate, which is what a mirrored facade is made of: pitch
    and roll stay, yaw becomes 180-yaw in the x plane and -yaw in the y plane. A
    tilted plate keeps its tilt rather than gaining the reflection of it, because
    a quarter-turn rule has nothing to say about the other two axes -- so set
    rotation_degrees by hand when a reflected tilt is what you actually want.
    """
    pitch, yaw, roll = rotation[0], rotation[1], rotation[2]
    if axis == "z":
        return [pitch, yaw, roll]
    reflected = 180.0 - yaw if axis == "x" else -yaw
    return [pitch, reflected % 360.0, roll]


def mirror_cell(cell: tuple[int, int, int], axis: str, origin: float) -> tuple[int, int, int]:
    """Reflect one cell across the plane perpendicular to `axis` at `origin`."""
    index = "xyz".index(axis)
    mirrored = [cell[0], cell[1], cell[2]]
    mirrored[index] = round(2 * origin - mirrored[index])
    return (mirrored[0], mirrored[1], mirrored[2])


def source_ids_error(actor_ids: list[int]) -> dict[str, str] | None:
    """Reject duplicate ids the way the editor does, before any browser request."""
    if len(set(actor_ids)) != len(actor_ids):
        return {"code": "INVALID_PARAMS", "message": "actor_ids must not contain duplicates."}
    return None


def ascii_layers(
    actors: list[dict[str, Any]],
    min_cell: tuple[int, int, int],
    max_cell: tuple[int, int, int],
) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Render one top-down ASCII grid per non-empty z layer plus a character legend."""
    symbols = "#ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@%&*+=?<>"
    by_layer: dict[int, dict[tuple[int, int], int]] = {}
    for actor in actors:
        cell = actor.get("cell")
        if not isinstance(cell, (list, tuple)) or len(cell) != 3:
            continue
        x, y, z = cell
        by_layer.setdefault(z, {})[(x, y)] = actor.get("item_id")
    char_by_item: dict[str, str] = {}
    legend: dict[str, int] = {}
    layers: dict[str, list[str]] = {}
    for z in sorted(by_layer):
        rows: list[str] = []
        for y in range(max_cell[1], min_cell[1] - 1, -1):
            row = []
            for x in range(min_cell[0], max_cell[0] + 1):
                item_id = by_layer[z].get((x, y))
                if item_id is None:
                    row.append(".")
                    continue
                key = str(item_id)
                if key not in char_by_item:
                    symbol = symbols[min(len(char_by_item), len(symbols) - 1)]
                    char_by_item[key] = symbol
                    legend[symbol] = int(item_id)
                row.append(char_by_item[key])
            rows.append("".join(row))
        layers[f"z={z}"] = rows
    return layers, legend


def quad_to_transform(vertices: list[list[float]]) -> dict[str, Any]:
    """
    Calculate transform parameters (location_cm, scale, rotation_degrees) for item 7
    from four corner vertices defining a planar rectangular face.

    Args:
        vertices: List of four 3D points [x, y, z] in centimetres, defining rectangle corners
                 in any order (the diagonal is found by length, so no ordering is assumed)

    Returns:
        Dict with keys: location_cm (center), scale (relative to item 7 extents),
        rotation_degrees [pitch, yaw, roll]

    Raises:
        ValueError: If vertices don't form a valid planar rectangle
    """
    if len(vertices) != 4:
        raise ValueError(f"Expected 4 vertices, got {len(vertices)}")
    if any(len(vertex) != 3 for vertex in vertices):
        raise ValueError("Every vertex needs exactly three components [x, y, z].")

    # Convert to tuples for easier manipulation
    pts = [tuple(v) for v in vertices]

    # Find center (average of all four points)
    center = [
        sum(p[0] for p in pts) / 4,
        sum(p[1] for p in pts) / 4,
        sum(p[2] for p in pts) / 4,
    ]

    def dist(p1: tuple[float, float, float], p2: tuple[float, float, float]) -> float:
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(p1, p2)))

    def vec_sub(p1: tuple[float, float, float], p2: tuple[float, float, float]) -> list[float]:
        return [p1[i] - p2[i] for i in range(3)]

    def dot(v1: list[float], v2: list[float]) -> float:
        return sum(a * b for a, b in zip(v1, v2))

    def normalize(v: list[float]) -> list[float]:
        length = math.sqrt(sum(x * x for x in v))
        return [x / length for x in v] if length > 1e-6 else [1, 0, 0]

    tol = 0.5  # 0.5 cm tolerance for floating point comparison

    # Which corner is opposite the first one? The diagonal is strictly longer than
    # either side -- sqrt(2) times a side even for a square -- so the farthest vertex
    # is the opposite one, whatever order the caller wrote the corners in. The other
    # two are the sides. Picking by edge-length equality alone, as this used to,
    # silently accepted an ordering whose 0-1 edge was the diagonal and then failed
    # the perpendicularity test on a perfectly good rectangle.
    by_distance = sorted(((dist(pts[0], pts[index]), index) for index in (1, 2, 3)), reverse=True)
    if by_distance[0][0] - by_distance[1][0] < tol:
        raise ValueError("Vertices do not form a rectangle: no single diagonal from the first vertex.")
    opposite = by_distance[0][1]
    sides = sorted(index for index in (1, 2, 3) if index != opposite)
    # Keep the documented assignment for the canonical order: the first vertex's
    # first neighbour is the width, so [corner, adjacent, adjacent, opposite] lands
    # the same way it always did.
    width_index = 1 if 1 in sides else sides[0]
    height_index = sides[1] if width_index == sides[0] else sides[0]
    width_cm = dist(pts[0], pts[width_index])
    height_cm = dist(pts[0], pts[height_index])
    if width_cm < tol or height_cm < tol:
        raise ValueError("Vertices do not form a rectangle: two of them coincide.")
    vec_width = vec_sub(pts[width_index], pts[0])
    vec_height = vec_sub(pts[height_index], pts[0])
    if abs(dot(normalize(vec_width), normalize(vec_height))) > 0.1:
        raise ValueError("The two edges that meet at the first vertex are not perpendicular.")
    # The remaining corner has to close the rectangle; otherwise this is a trapezoid
    # or a kite, and a fitted plate would sit at the wrong angle.
    closing = [pts[0][axis] + vec_width[axis] + vec_height[axis] for axis in range(3)]
    if dist(pts[opposite], tuple(closing)) > max(tol, 0.002 * max(width_cm, height_cm)):
        raise ValueError(
            "Vertices do not form a rectangle: the opposite corner does not close it."
        )

    # Normalize width and height vectors
    u = normalize(vec_width)
    v = normalize(vec_height)

    # Calculate normal vector (cross product u × v)
    normal = [
        u[1] * v[2] - u[2] * v[1],
        u[2] * v[0] - u[0] * v[2],
        u[0] * v[1] - u[1] * v[0],
    ]
    normal = normalize(normal)

    # Item 7 local extents: x=[-25, 0] (thickness 25cm), y=[-50, 50] (width 100cm), z=[-50, 50] (height 100cm)
    # In item 7's local frame:
    # - The "front face" (where normal points out) is at local x=0
    # - Width is along local Y axis
    # - Height is along local Z axis

    # We want to orient item 7 so that:
    # - Its local Y axis aligns with our width direction (u)
    # - Its local Z axis aligns with our height direction (v)
    # - Its normal (local +X) points along our calculated normal

    # Build rotation from forward/right/up vectors
    # World frame: our normal is the direction item 7's local +X should point
    # Calculate Euler angles from direction vectors

    # Simplified approach: use normal vector to calculate yaw and pitch
    # Then use the "up" (height) vector to calculate roll

    # An Unreal rotator maps its local axes into the world with columns
    #   +X -> (cp*cy, cp*sy, sp)
    #   +Y -> (-sy, cy, 0) turned by roll inside the plane perpendicular to +X
    #   +Z -> the remaining in-plane axis
    # Item 7's local +X is the face normal, +Y the width (u) and +Z the height
    # (v), so yaw and pitch come from the normal and roll only spins u inside
    # the face plane.
    yaw_rad = math.atan2(normal[1], normal[0])
    # atan2 keeps the full range and avoids asin's blow-up at the poles.
    pitch_rad = math.atan2(normal[2], math.sqrt(normal[0]**2 + normal[1]**2))

    sin_yaw = math.sin(yaw_rad)
    cos_yaw = math.cos(yaw_rad)
    sin_pitch = math.sin(pitch_rad)
    cos_pitch = math.cos(pitch_rad)

    # Local +Y and +Z as world vectors when roll is zero.
    y_neutral = [-sin_yaw, cos_yaw, 0.0]
    z_neutral = [-sin_pitch * cos_yaw, -sin_pitch * sin_yaw, cos_pitch]

    # With roll r the width direction is r's rotation of y_neutral towards
    # z_neutral, so u . y_neutral = cos(r) and u . z_neutral = -sin(r).
    roll_rad = math.atan2(-dot(u, z_neutral), dot(u, y_neutral))

    # Convert to degrees
    yaw_deg = math.degrees(yaw_rad)
    pitch_deg = math.degrees(pitch_rad)
    roll_deg = math.degrees(roll_rad)

    # Calculate scale relative to item 7's extents
    # Item 7: thickness 25cm (local X), width 100cm (local Y), height 100cm (local Z)
    thickness_cm = 0.0001  # Near-zero thickness: 1e-4 / 25 = 4e-6 scale factor
    scale = [
        thickness_cm / 25.0,  # thickness scale (along normal)
        width_cm / 100.0,      # width scale
        height_cm / 100.0,     # height scale
    ]

    return {
        "location_cm": center,
        "scale": scale,
        "rotation_degrees": [pitch_deg, yaw_deg, roll_deg],
    }


def create_mcp(registry: BrowserRegistry, captures: CaptureRelay | None = None) -> MCPServer:
    relay = captures or CaptureRelay()
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
    # Camera moves and captures repeat to the same result and leave the map untouched.
    view = types.ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )

    async def upload_model(
        browser_session_id: str, ctx: Context, name: str, payload: bytes
    ) -> dict[str, Any] | types.CallToolResult:
        """Send a model file to the browser in frame-sized pieces.

        A chunk carries the offset it belongs at, so a retry after a lost response
        is answered with the progress the first attempt would have produced instead
        of appending the bytes twice.
        """
        total = len(payload)
        offset = 0
        upload_id: str | None = None
        while True:
            chunk = payload[offset : offset + MODEL_CHUNK_BYTES]
            response = await invoke(
                "upload_model",
                browser_session_id,
                ctx,
                **({"upload_id": upload_id} if upload_id is not None else {}),
                **({"name": name} if upload_id is None else {}),
                offset=offset,
                data_base64=base64.b64encode(chunk).decode("ascii"),
                byte_length=total,
            )
            if response.is_error:
                return response
            data = json.loads(response.content[0].text) if response.content else {}
            upload_id = data.get("upload_id")
            offset = int(data.get("received", 0))
            if upload_id is None:
                return result(
                    {"error": {"code": "INTERNAL_ERROR", "message": "The browser did not name the upload."}},
                    error=True,
                )
            if data.get("complete"):
                return data
            if offset >= total:
                return result(
                    {
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": f"The browser accepted {offset} of {total} bytes and stopped.",
                        }
                    },
                    error=True,
                )

    async def list_all_actors(
        browser_session_id: str,
        ctx: Context,
        *,
        map_session_id: str | None = None,
        expected_revision: Revision | None = None,
    ) -> types.CallToolResult | tuple[list[dict[str, Any]], dict[str, Any]]:
        """Page through list_actors and return every actor plus the last page's metadata."""
        params: dict[str, Any] = {"offset": 0, "limit": 500}
        if map_session_id is not None:
            params["map_session_id"] = map_session_id
        if expected_revision is not None:
            params["expected_revision"] = expected_revision
        actors: list[dict[str, Any]] = []
        meta: dict[str, Any] = {}
        while True:
            response = await invoke("list_actors", browser_session_id, ctx, **params)
            if response.is_error:
                return response
            data = json.loads(response.content[0].text) if response.content else {}
            page = data.get("actors", [])
            actors.extend(page)
            meta = data
            if not page or params["offset"] + params["limit"] >= data.get("total", 0):
                break
            params["offset"] += params["limit"]
        return actors, meta

    async def place_cells(        browser_session_id: str,
        ctx: Context,
        map_session_id: str,
        revision: int,
        item_id: int,
        cells: list[tuple[int, int, int]],
        rotation_index: int = 0,
        color_linear_rgb: tuple[float, float, float] | None = None,
        skip_occupied: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int]:
        """Place a cell expansion through one atomic browser batch."""
        placements = [
            {
                "item_id": item_id,
                "cell": list(cell),
                "rotation_index": rotation_index,
                **({"color_linear_rgb": list(color_linear_rgb)} if color_linear_rgb is not None else {}),
            }
            for cell in cells
        ]
        response = await invoke(
            "place_actors",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=revision,
            placements=placements,
        )
        data = json.loads(response.content[0].text) if response.content else {}
        if response.is_error:
            return [], [], [{"reason": data.get("error", {})}], revision
        placed = data.get("placed_actors", [])
        placement_errors = data.get("placement_errors", [])
        skipped = []
        errors = []
        for error in placement_errors:
            index = error.get("index")
            cell = list(cells[index]) if isinstance(index, int) and 0 <= index < len(cells) else None
            entry = {**error, **({"cell": cell} if cell is not None else {})}
            if skip_occupied and error.get("reason") == "CELL_OCCUPIED":
                skipped.append(entry)
            else:
                errors.append(entry)
        return placed, skipped, errors, data.get("revision", revision)

    async def place_batch(
        browser_session_id: str,
        ctx: Context,
        map_session_id: str,
        revision: int,
        placements: list[dict[str, Any]],
    ) -> types.CallToolResult | tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        """Submit one atomic place_actors batch, or hand back the tool-level failure."""
        response = await invoke(
            "place_actors",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=revision,
            placements=placements,
        )
        if response.is_error:
            return response
        data = json.loads(response.content[0].text) if response.content else {}
        return (
            data.get("placed_actors", []),
            data.get("placement_errors", []),
            data.get("revision", revision),
        )

    async def duplicate_actors(
        browser_session_id: str,
        ctx: Context,
        map_session_id: str,
        revision: int,
        actor_ids: list[int],
        targets: Callable[[tuple[int, int, int], int], list[tuple[tuple[int, int, int], int]]],
        operation: dict[str, Any],
        free_rotation: Callable[[list[float]], list[float]] | None = None,
    ) -> types.CallToolResult:
        """Rebuild a source set at planned cells through one atomic place_actors batch.

        The set is read first so that anything a placement cannot express is reported
        before the write: unknown ids fail the whole call, while actors the editor holds in
        a free transform are rebuilt from their own pivot, scale and angle instead of being
        dropped at a guessed cell. Every placement keeps the source's item, rotation and
        colour, and each result names the actor it was built from.
        """
        rotate_free = free_rotation or (lambda rotation: list(rotation))
        fetched = await list_all_actors(
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=revision,
        )
        if not isinstance(fetched, tuple):
            return fetched
        all_actors, meta = fetched
        link_response = await invoke("list_links", browser_session_id, ctx,
                                     map_session_id=map_session_id,
                                     expected_revision=meta.get("revision", revision),
                                     limit=4096)
        if link_response.is_error:
            return link_response
        link_data = json.loads(link_response.content[0].text) if link_response.content else {}
        grouped_ids = {
            member
            for link in link_data.get("links", [])
            if link.get("kind") == "group"
            for member in [link.get("main_actor_id"), *(link.get("child_actor_ids") or [])]
            if isinstance(member, int)
        }
        by_id = {
            actor["editor_id"]: actor
            for actor in all_actors
            if actor.get("editor_id") is not None
        }
        missing = [editor_id for editor_id in actor_ids if editor_id not in by_id]
        if missing:
            return result(
                {
                    "error": {
                        "code": "ACTOR_NOT_FOUND",
                        "message": f"Unknown actor ids: {missing}.",
                        "actor_ids": missing,
                    }
                },
                error=True,
            )

        placements: list[dict[str, Any]] = []
        sources: list[int] = []
        skipped: list[dict[str, Any]] = []
        for editor_id in actor_ids:
            actor = by_id[editor_id]
            cell = actor.get("cell") or []
            if actor.get("editable") is False:
                skipped.append(
                    {
                        "editor_id": editor_id,
                        "reason": "ACTOR_NOT_EDITABLE",
                        "message": "The editor does not expose this actor to MCP.",
                    }
                )
                continue
            if editor_id in grouped_ids:
                skipped.append(
                    {
                        "editor_id": editor_id,
                        "reason": "ACTOR_IN_GROUP",
                        "message": "belongs to a group; connection state is not copied. Disconnect it first for a standalone copy.",
                    }
                )
                continue
            if len(cell) != 3:
                skipped.append(
                    {
                        "editor_id": editor_id,
                        "reason": "ACTOR_NOT_GRID_ALIGNED",
                        "message": "The editor reports no cell for this actor, so there is no anchor to shift.",
                    }
                )
                continue
            mode = actor.get("transform_mode", 1)
            transform = actor.get("transform") or {}
            color = actor.get("color_linear_rgb")
            if mode == 1:
                rotation = transform.get("rotation_index", 0)
                for target, target_rotation in targets((cell[0], cell[1], cell[2]), rotation):
                    placements.append(
                        {
                            "item_id": actor["item_id"],
                            "cell": list(target),
                            "rotation_index": target_rotation & 3,
                            **({"color_linear_rgb": list(color)} if color is not None else {}),
                        }
                    )
                    sources.append(editor_id)
                continue
            if mode == 2:
                location = transform.get("location_cm")
                scale = transform.get("scale")
                rotation = transform.get("rotation_degrees")
                if not (
                    isinstance(location, list) and len(location) == 3
                    and isinstance(scale, list) and len(scale) == 3
                    and isinstance(rotation, list) and len(rotation) == 3
                ):
                    skipped.append(
                        {
                            "editor_id": editor_id,
                            "reason": "ACTOR_NOT_DUPLICABLE",
                            "message": "The editor did not report this actor's pivot, scale and angle, so it cannot be rebuilt.",
                        }
                    )
                    continue
                source_cell = (cell[0], cell[1], cell[2])
                # A free pose is moved by the same cell delta the grid path uses,
                # in centimetres, and keeps its own scale and angle. Rebuilding it
                # from a cell would silently reset both.
                for target, _ in targets(source_cell, 0):
                    delta = [target[index] - source_cell[index] for index in range(3)]
                    placements.append(
                        {
                            "item_id": actor["item_id"],
                            "transform": {
                                "location_cm": [location[index] + delta[index] * 100 for index in range(3)],
                                "scale": list(scale),
                                "rotation_degrees": rotate_free(list(rotation)),
                            },
                            **({"color_linear_rgb": list(color)} if color is not None else {}),
                        }
                    )
                    sources.append(editor_id)
                continue
            skipped.append(
                {
                    "editor_id": editor_id,
                    "reason": "ACTOR_NOT_DUPLICABLE",
                    "message": "This actor's native transform mode cannot be duplicated through MCP.",
                }
            )

        if len(placements) > MAX_BATCH_PLACEMENTS:
            return result(
                {
                    "error": {
                        "code": "BATCH_TOO_LARGE",
                        "message": (
                            f"{len(placements)} placements exceed the "
                            f"{MAX_BATCH_PLACEMENTS}-placement batch limit; duplicate fewer "
                            "actors or ask for fewer repeats."
                        ),
                    }
                },
                error=True,
            )

        summary = {
            **operation,
            "source_count": len(actor_ids),
            "requested_count": len(placements),
            "placed_count": 0,
            "skipped_count": len(skipped),
            "error_count": 0,
        }
        if not placements:
            return result(
                {
                    "revision": meta.get("revision", revision),
                    "map_session_id": map_session_id,
                    "operation": summary,
                    "placed_actors": [],
                    "skipped_actors": skipped,
                    "placement_errors": [],
                }
            )

        batch = await place_batch(
            browser_session_id, ctx, map_session_id, meta.get("revision", revision), placements
        )
        if not isinstance(batch, tuple):
            return batch
        placed, placement_errors, new_revision = batch

        def with_source(entry: dict[str, Any]) -> dict[str, Any]:
            index = entry.get("index")
            if isinstance(index, int) and 0 <= index < len(sources):
                return {**entry, "source_actor_id": sources[index]}
            return dict(entry)

        placed_actors = [with_source(entry) for entry in placed]
        errors = [with_source(entry) for entry in placement_errors]
        summary["placed_count"] = len(placed_actors)
        summary["error_count"] = len(errors)
        return result(
            {
                "revision": new_revision,
                "map_session_id": map_session_id,
                "operation": summary,
                "placed_actors": placed_actors,
                "skipped_actors": skipped,
                "placement_errors": errors,
            }
        )

    @mcp.tool(annotations=read_only, description=TOOL_DESCRIPTIONS["list_sessions"])
    async def list_sessions() -> types.CallToolResult:
        return result({"sessions": registry.sessions()})

    @mcp.tool(annotations=read_only, description=TOOL_DESCRIPTIONS["get_state"])
    async def get_state(browser_session_id: str, ctx: Context) -> types.CallToolResult:
        return await invoke("get_state", browser_session_id, ctx)

    @mcp.tool(annotations=read_only, description=TOOL_DESCRIPTIONS["get_camera"])
    async def get_camera(browser_session_id: str, ctx: Context) -> types.CallToolResult:
        return await invoke("get_camera", browser_session_id, ctx)

    @mcp.tool(annotations=view, description=TOOL_DESCRIPTIONS["set_camera"])
    async def set_camera(
        browser_session_id: str,
        ctx: Context,
        position: Point3,
        target: Point3,
    ) -> types.CallToolResult:
        return await invoke(
            "set_camera",
            browser_session_id,
            ctx,
            position=list(position),
            target=list(target),
        )

    @mcp.tool(annotations=view, description=TOOL_DESCRIPTIONS["capture_view"])
    async def capture_view(
        browser_session_id: str,
        ctx: Context,
        position: Point3 | None = None,
        target: Point3 | None = None,
        width: Annotated[StrictInt, Field(ge=64, le=2560)] = 960,
        height: Annotated[StrictInt, Field(ge=64, le=2000)] = 540,
        image_format: Literal["jpeg", "png"] = "jpeg",
        quality: Annotated[FiniteFloat, Field(ge=0.1, le=1)] = 0.72,
    ) -> types.CallToolResult:
        """Capture a frame optionally after moving the camera, then save it to disk."""
        if (position is None) != (target is None):
            return result(
                {
                    "error": {
                        "code": "INVALID_PARAMS",
                        "message": "position and target must be provided together.",
                    }
                },
                error=True,
            )
        browser = registry.browsers.get(browser_session_id)
        if browser is None:
            return result(
                {
                    "error": {
                        "code": "BROWSER_NOT_CONNECTED",
                        "message": "Select a connected browser using list_sessions.",
                    }
                },
                error=True,
            )
        # The image takes the HTTP road, not the 1 MiB bridge frame.  See capture.py.
        capture_id, upload_url = relay.open(browser.host)
        params: dict[str, Any] = {
            "width": width,
            "height": height,
            "format": image_format,
            "quality": quality,
            "upload_url": upload_url,
            "capture_id": capture_id,
        }
        if position is not None and target is not None:
            params["position"] = list(position)
            params["target"] = list(target)
        response = await invoke("capture_view", browser_session_id, ctx, **params)
        if response.is_error:
            relay.discard(capture_id)
            return response
        data = json.loads(response.content[0].text) if response.content else {}
        image = data.get("image") or {}
        uploaded = data.get("uploaded") or {}
        if uploaded:
            capture = await relay.wait(capture_id)
            if capture is None:
                return result(
                    {
                        "error": {
                            "code": "CAPTURE_UPLOAD_TIMEOUT",
                            "message": (
                                "The browser rendered the frame but never POSTed it to "
                                f"{upload_url}. Check that the server is reachable at that address."
                            ),
                        }
                    },
                    error=True,
                )
            payload = capture.data
            mime_type = uploaded.get("mime_type") or capture.content_type or "image/jpeg"
        else:
            # An older tab answers with the image inline; keep decoding it so a
            # mixed pair still works, at the cost of the frame cap.
            relay.discard(capture_id)
            data_url = image.get("data_url") or ""
            if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
                return result({"error": {"code": "CAPTURE_FAILED", "message": "The browser returned no usable image data."}}, error=True)
            try:
                payload = base64.b64decode(data_url.split(",", 1)[1], validate=True)
            except (IndexError, ValueError):
                return result({"error": {"code": "CAPTURE_FAILED", "message": "The browser returned a malformed image payload."}}, error=True)
            mime_type = image.get("mime_type") or "image/jpeg"
        suffix = "png" if mime_type == "image/png" else "jpg"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        name = f"view-{stamp}-{uuid.uuid4().hex[:8]}.{suffix}"
        path = SCREENSHOT_DIR / name
        try:
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        except OSError as exc:
            return result(
                {
                    "error": {
                        "code": "SCREENSHOT_WRITE_FAILED",
                        "message": f"Could not save the capture: {exc}",
                    }
                },
                error=True,
            )
        return result(
            {
                "path": str(path),
                "mime_type": mime_type,
                "width": image.get("width") or uploaded.get("width"),
                "height": image.get("height") or uploaded.get("height"),
                "bytes": len(payload),
                "camera": data.get("camera"),
                "transport": "http" if uploaded else "bridge",
            }
        )

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
        cell_box: dict[str, Any] | None = None,
        map_session_id: str | None = None,
        expected_revision: Revision | None = None,
    ) -> types.CallToolResult:
        params = {
            "offset": offset,
            "limit": limit,
            "selected_only": selected_only,
            **({"cell_box": cell_box} if cell_box is not None else {}),
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

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["create_scene_object"])
    async def create_scene_object(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        name: Annotated[str, Field(min_length=1, max_length=200)],
        actor_ids: SourceActorIds,
        ctx: Context,
    ) -> types.CallToolResult:
        return await invoke(
            "create_scene_object", browser_session_id, ctx,
            map_session_id=map_session_id, expected_revision=expected_revision,
            name=name, actor_ids=actor_ids,
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["delete_scene_object"])
    async def delete_scene_object(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        object_id: Annotated[str, Field(min_length=1, max_length=200)],
        ctx: Context,
    ) -> types.CallToolResult:
        return await invoke(
            "delete_scene_object", browser_session_id, ctx,
            map_session_id=map_session_id, expected_revision=expected_revision,
            object_id=object_id,
        )

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
        ctx: Context,
        cell: Cell | None = None,
        rotation_index: Annotated[StrictInt, Field(ge=0, le=3)] | None = None,
        color_linear_rgb: tuple[FiniteFloat, FiniteFloat, FiniteFloat] | None = None,
        transform: FreePose | None = None,
    ) -> types.CallToolResult:
        if (cell is None) == (transform is None):
            return result(
                {
                    "error": {
                        "code": "INVALID_PARAMS",
                        "message": "Pass exactly one of cell or transform: a cell anchors the actor to the grid, a transform states its whole pose.",
                    }
                },
                error=True,
            )
        return await invoke(
            "place_actor",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
            item_id=item_id,
            **({"cell": cell} if cell is not None else {}),
            **({"rotation_index": rotation_index} if rotation_index is not None else {}),
            **({"color_linear_rgb": color_linear_rgb} if color_linear_rgb is not None else {}),
            **({"transform": transform.model_dump()} if transform is not None else {}),
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["update_actors"])
    async def update_actors(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: ExpectedRevision,
        actor_ids: ActorIds,
        ctx: Context,
        color_linear_rgb: tuple[FiniteFloat, FiniteFloat, FiniteFloat] | None = None,
        rotation_index: Annotated[StrictInt, Field(ge=0, le=3)] | None = None,
        params: list[FiniteFloat] | None = None,
        item_id: StrictInt | None = None,
        location_cm: Point3 | None = None,
        location_delta_cm: Point3 | None = None,
        scale: Point3 | None = None,
        rotation_degrees: Point3 | None = None,
    ) -> types.CallToolResult:
        """Update visual and native properties without deleting actors."""
        free = (location_cm, location_delta_cm, scale, rotation_degrees)
        if color_linear_rgb is None and rotation_index is None and params is None and item_id is None and all(
            value is None for value in free
        ):
            return result({"error": {"code": "INVALID_PARAMS", "message": "Provide at least one actor property."}}, error=True)
        if location_cm is not None and location_delta_cm is not None:
            return result(
                {
                    "error": {
                        "code": "INVALID_PARAMS",
                        "message": "Pass either location_cm or location_delta_cm, not both: one is a pose, the other a move.",
                    }
                },
                error=True,
            )
        if rotation_index is not None and rotation_degrees is not None:
            return result(
                {
                    "error": {
                        "code": "INVALID_PARAMS",
                        "message": "Pass either rotation_index or rotation_degrees, not both.",
                    }
                },
                error=True,
            )
        return await invoke(
            "update_actors",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
            actor_ids=actor_ids,
            **({"color_linear_rgb": color_linear_rgb} if color_linear_rgb is not None else {}),
            **({"rotation_index": rotation_index} if rotation_index is not None else {}),
            **({"params": params} if params is not None else {}),
            **({"item_id": item_id} if item_id is not None else {}),
            **({"location_cm": location_cm} if location_cm is not None else {}),
            **({"location_delta_cm": location_delta_cm} if location_delta_cm is not None else {}),
            **({"scale": scale} if scale is not None else {}),
            **({"rotation_degrees": rotation_degrees} if rotation_degrees is not None else {}),
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["place_actors"])
    async def place_actors(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        placements: Annotated[
            list[dict[str, Any]],
            Field(
                max_length=MAX_BATCH_PLACEMENTS,
                description=(
                    "Array of placement specs. Each needs item_id and then either cell, or "
                    "transform {location_cm, scale, rotation_degrees} for an item whose "
                    "free_transform is true. Optional rotation_index (0-3) and "
                    "color_linear_rgb. Free placements claim no cell, so several may share one."
                ),
            ),
        ],
        ctx: Context,
    ) -> types.CallToolResult:
        """Place multiple actors through one atomic browser batch."""
        return await invoke(
            "place_actors",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
            placements=placements,
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["place_quad"])
    async def place_quad(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        vertices: Annotated[
            list[list[float]],
            Field(
                min_length=4,
                max_length=4,
                description=(
                    "Four corner vertices [x, y, z] in centimetres defining a planar "
                    "rectangular face. Vertices can be in any order; the tool will find "
                    "opposite edges and calculate transform parameters."
                ),
            ),
        ],
        ctx: Context,
        color_linear_rgb: tuple[FiniteFloat, FiniteFloat, FiniteFloat] | None = None,
    ) -> types.CallToolResult:
        """Place one item-7 plate from four corner vertices, through place_actors."""
        try:
            transform_params = quad_to_transform(vertices)
        except ValueError as exc:
            return result(
                {
                    "error": {
                        "code": "INVALID_GEOMETRY",
                        "message": f"Quad vertices invalid: {exc}",
                    }
                },
                error=True,
            )
        placement: dict[str, Any] = {"item_id": PLATE_ITEM_ID, "transform": transform_params}
        if color_linear_rgb is not None:
            placement["color_linear_rgb"] = list(color_linear_rgb)
        return await invoke(
            "place_actors",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
            placements=[placement],
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["place_quads"])
    async def place_quads(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        quads: Annotated[
            list[dict[str, Any]],
            Field(
                max_length=MAX_BATCH_PLACEMENTS,
                description=(
                    "Array of quad specs. Each needs 'vertices': four 3D points [x, y, z] in cm "
                    "defining a planar rectangular face. Optional 'color_linear_rgb': [r, g, b] "
                    "with 0-1 range. The tool calculates transform parameters for all quads and "
                    "places them atomically. Maximum 4096 quads per batch."
                ),
            ),
        ],
        ctx: Context,
    ) -> types.CallToolResult:
        """
        Batch place multiple rectangular surfaces from vertex arrays.
        Primary tool for AI surface modeling: describe geometry as vertices.
        """
        if not quads:
            return result(
                {
                    "error": {
                        "code": "INVALID_PARAMS",
                        "message": "quads array cannot be empty.",
                    }
                },
                error=True,
            )

        placements: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for idx, quad_spec in enumerate(quads):
            vertices = quad_spec.get("vertices")
            color = quad_spec.get("color_linear_rgb")

            if not isinstance(vertices, list):
                errors.append(
                    {
                        "index": idx,
                        "error": "MISSING_VERTICES",
                        "message": "Each quad spec must have 'vertices' field.",
                    }
                )
                continue

            # Calculate transform
            try:
                transform_params = quad_to_transform(vertices)
            except ValueError as exc:
                errors.append(
                    {
                        "index": idx,
                        "error": "INVALID_GEOMETRY",
                        "message": f"Quad {idx} vertices invalid: {exc}",
                        "vertices": vertices,
                    }
                )
                continue

            # Build placement spec
            placement: dict[str, Any] = {
                "item_id": PLATE_ITEM_ID,
                "transform": transform_params,
            }
            if color is not None:
                if isinstance(color, (list, tuple)) and len(color) == 3:
                    placement["color_linear_rgb"] = list(color)
                else:
                    errors.append(
                        {
                            "index": idx,
                            "error": "INVALID_COLOR",
                            "message": f"color_linear_rgb must be [r, g, b] with 3 values, got {color}",
                        }
                    )
                    continue

            placements.append(placement)

        # If all quads failed validation, return error
        if not placements:
            return result(
                {
                    "error": {
                        "code": "ALL_QUADS_INVALID",
                        "message": f"All {len(quads)} quad specs failed validation.",
                        "validation_errors": errors,
                    }
                },
                error=True,
            )

        # Place all valid quads atomically
        place_result = await invoke(
            "place_actors",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
            placements=placements,
        )

        # If there were validation errors, append them to the result
        if errors and not place_result.is_error:
            result_data = json.loads(place_result.content[0].text) if place_result.content else {}
            result_data["validation_errors"] = errors
            result_data["placed_count"] = len(placements)
            result_data["failed_count"] = len(errors)
            return result(result_data)

        return place_result

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["copy_actors"])
    async def copy_actors(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        actor_ids: SourceActorIds,
        offset: Cell,
        ctx: Context,
    ) -> types.CallToolResult:
        """Duplicate actors at a cell offset through one atomic batch."""
        invalid = source_ids_error(actor_ids)
        if invalid is not None:
            return result({"error": invalid}, error=True)
        if not any(offset):
            return result(
                {
                    "error": {
                        "code": "INVALID_PARAMS",
                        "message": "offset must move the copies; [0,0,0] lands on the sources.",
                    }
                },
                error=True,
            )
        return await duplicate_actors(
            browser_session_id,
            ctx,
            map_session_id,
            expected_revision,
            actor_ids,
            lambda cell, rotation: [
                ((cell[0] + offset[0], cell[1] + offset[1], cell[2] + offset[2]), rotation)
            ],
            {"kind": "copy", "offset": list(offset)},
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["mirror_actors"])
    async def mirror_actors(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        actor_ids: SourceActorIds,
        axis: Literal["x", "y", "z"],
        origin: FiniteFloat,
        ctx: Context,
    ) -> types.CallToolResult:
        """Reflect actors across an axis-aligned plane through one atomic batch."""
        invalid = source_ids_error(actor_ids)
        if invalid is not None:
            return result({"error": invalid}, error=True)
        return await duplicate_actors(
            browser_session_id,
            ctx,
            map_session_id,
            expected_revision,
            actor_ids,
            lambda cell, rotation: [
                (mirror_cell(cell, axis, origin), mirror_rotation_index(rotation, axis))
            ],
            {"kind": "mirror", "axis": axis, "origin": origin},
            free_rotation=lambda rotation: mirror_free_rotation(rotation, axis),
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["polar_array"])
    async def polar_array(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: ExpectedRevision,
        actor_ids: SourceActorIds,
        center: Cell,
        axis: Literal["x", "y", "z"],
        count: Annotated[StrictInt, Field(ge=2, le=MAX_ARRAY_COUNT)],
        ctx: Context,
        rotate_with_angle: bool = True,
    ) -> types.CallToolResult:
        return await invoke("polar_array", browser_session_id, ctx, map_session_id=map_session_id,
                            expected_revision=expected_revision, actor_ids=actor_ids,
                            center=center, axis=axis, count=count,
                            rotate_with_angle=rotate_with_angle)

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["array_actors"])
    async def array_actors(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        actor_ids: SourceActorIds,
        direction: Cell,
        spacing: Annotated[StrictInt, Field(ge=1, le=MAX_BATCH_PLACEMENTS)],
        count: Annotated[StrictInt, Field(ge=2, le=MAX_ARRAY_COUNT)],
        ctx: Context,
    ) -> types.CallToolResult:
        """Repeat actors along a line through one atomic batch."""
        invalid = source_ids_error(actor_ids)
        if invalid is not None:
            return result({"error": invalid}, error=True)
        if not any(direction):
            return result(
                {
                    "error": {
                        "code": "INVALID_PARAMS",
                        "message": "direction must have at least one non-zero axis.",
                    }
                },
                error=True,
            )
        step = tuple(axis_value * spacing for axis_value in direction)

        def repeats(
            cell: tuple[int, int, int], rotation: int
        ) -> list[tuple[tuple[int, int, int], int]]:
            return [
                (
                    (
                        cell[0] + step[0] * repeat,
                        cell[1] + step[1] * repeat,
                        cell[2] + step[2] * repeat,
                    ),
                    rotation,
                )
                for repeat in range(1, count)
            ]

        return await duplicate_actors(
            browser_session_id,
            ctx,
            map_session_id,
            expected_revision,
            actor_ids,
            repeats,
            {
                "kind": "array",
                "direction": list(direction),
                "spacing": spacing,
                "count": count,
                "step": list(step),
            },
        )

    @mcp.tool(annotations=read_only, description=TOOL_DESCRIPTIONS["query_space"])
    async def query_space(
        browser_session_id: str,
        ctx: Context,
        min_cell: Cell,
        max_cell: Cell,
        map_session_id: str | None = None,
        expected_revision: Revision | None = None,
    ) -> types.CallToolResult:
        """Query actors within a box-shaped region by filtering list_actors results."""
        for i in range(3):
            if min_cell[i] > max_cell[i]:
                return result(
                    {
                        "error": {
                            "code": "INVALID_PARAMS",
                            "message": f"min_cell[{i}] must be <= max_cell[{i}]",
                        }
                    },
                    error=True,
                )

        fetched = await list_all_actors(
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
        )
        if not isinstance(fetched, tuple):
            return fetched
        all_actors, meta = fetched

        occupied_cells = []
        for actor in all_actors:
            cell = actor.get("cell", [])
            if len(cell) == 3 and all(min_cell[i] <= cell[i] <= max_cell[i] for i in range(3)):
                occupied_cells.append(
                    {
                        "cell": cell,
                        "editor_id": actor.get("editor_id"),
                        "item_id": actor.get("item_id"),
                    }
                )

        return result(
            {
                "query_type": "region",
                "min_cell": list(min_cell),
                "max_cell": list(max_cell),
                "occupied_cells": occupied_cells,
                "revision": meta.get("revision"),
                "map_session_id": meta.get("map_session_id"),
            }
        )

    @mcp.tool(annotations=read_only, description=TOOL_DESCRIPTIONS["describe_region"])
    async def describe_region(
        browser_session_id: str,
        ctx: Context,
        min_cell: Cell,
        max_cell: Cell,
        map_session_id: str | None = None,
        expected_revision: Revision | None = None,
    ) -> types.CallToolResult:
        """Summarize a region: item counts, bounding box, occupancy and per-layer ASCII maps."""
        for i in range(3):
            if min_cell[i] > max_cell[i]:
                return result(
                    {
                        "error": {
                            "code": "INVALID_PARAMS",
                            "message": f"min_cell[{i}] must be <= max_cell[{i}]",
                        }
                    },
                    error=True,
                )
        volume = (
            (max_cell[0] - min_cell[0] + 1)
            * (max_cell[1] - min_cell[1] + 1)
            * (max_cell[2] - min_cell[2] + 1)
        )
        if volume > MAX_REGION_VOLUME:
            return result(
                {
                    "error": {
                        "code": "REGION_TOO_LARGE",
                        "message": f"region has {volume} cells, limit is {MAX_REGION_VOLUME}",
                    }
                },
                error=True,
            )

        fetched = await list_all_actors(
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
        )
        if not isinstance(fetched, tuple):
            return fetched
        all_actors, meta = fetched

        region_actors = [
            actor
            for actor in all_actors
            if len(actor.get("cell") or []) == 3
            and all(min_cell[i] <= actor["cell"][i] <= max_cell[i] for i in range(3))
        ]
        item_counts: dict[str, int] = {}
        for actor in region_actors:
            key = str(actor.get("item_id"))
            item_counts[key] = item_counts.get(key, 0) + 1

        layers, legend = ascii_layers(region_actors, min_cell, max_cell)
        return result(
            {
                "query_type": "describe",
                "min_cell": list(min_cell),
                "max_cell": list(max_cell),
                "occupied_count": len(region_actors),
                "cell_volume": volume,
                "occupancy": round(len(region_actors) / volume, 4) if volume else 0,
                "bounding_box": (
                    {
                        "min_cell": [
                            min(actor["cell"][i] for actor in region_actors) for i in range(3)
                        ],
                        "max_cell": [
                            max(actor["cell"][i] for actor in region_actors) for i in range(3)
                        ],
                    }
                    if region_actors
                    else None
                ),
                "item_counts": item_counts,
                "legend": legend,
                "layers": layers,
                "revision": meta.get("revision"),
                "map_session_id": meta.get("map_session_id"),
            }
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["fill_box"])
    async def fill_box(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        min_cell: Cell,
        max_cell: Cell,
        item_id: StrictInt,
        ctx: Context,
        mode: Literal["solid", "shell", "walls"] = "solid",
        rotation_index: Annotated[StrictInt, Field(ge=0, le=3)] = 0,
        color_linear_rgb: tuple[FiniteFloat, FiniteFloat, FiniteFloat] | None = None,
        skip_occupied: bool = False,
    ) -> types.CallToolResult:
        """Fill an axis-aligned box by replaying place_actor per cell."""
        for i in range(3):
            if min_cell[i] > max_cell[i]:
                return result(
                    {
                        "error": {
                            "code": "INVALID_PARAMS",
                            "message": f"min_cell[{i}] must be <= max_cell[{i}]",
                        }
                    },
                    error=True,
                )
        if not 0 <= item_id <= 0xFFFF:
            return result(
                {"error": {"code": "INVALID_PARAMS", "message": "item_id must be 0..65535"}},
                error=True,
            )

        cells = box_cells(tuple(min_cell), tuple(max_cell), mode)
        if not cells:
            return result(
                {
                    "error": {
                        "code": "INVALID_PARAMS",
                        "message": f"mode {mode} selects no cells for a flat box",
                    }
                },
                error=True,
            )
        if len(cells) > MAX_FILL_CELLS:
            return result(
                {
                    "error": {
                        "code": "REGION_TOO_LARGE",
                        "message": (
                            f"mode {mode} expands to {len(cells)} cells; limit is "
                            f"{MAX_FILL_CELLS}. Split the box or use a sparser mode."
                        ),
                    }
                },
                error=True,
            )

        placed, skipped, errors, revision = await place_cells(
            browser_session_id,
            ctx,
            map_session_id,
            expected_revision,
            item_id,
            cells,
            rotation_index=rotation_index,
            color_linear_rgb=color_linear_rgb,
            skip_occupied=skip_occupied,
        )
        return result(
            {
                "revision": revision,
                "map_session_id": map_session_id,
                "fill": {
                    "mode": mode,
                    "requested_cells": len(cells),
                    "placed_count": len(placed),
                    "skipped_count": len(skipped),
                    "error_count": len(errors),
                },
                "placed_actors": placed,
                "skipped_cells": skipped,
                "placement_errors": errors,
            }
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["fill_sphere"])
    async def fill_sphere(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        center: Cell,
        radius: Annotated[StrictInt, Field(ge=0, le=MAX_SPHERE_RADIUS)],
        item_id: StrictInt,
        ctx: Context,
        mode: Literal["solid", "hollow"] = "solid",
        rotation_index: Annotated[StrictInt, Field(ge=0, le=3)] = 0,
        color_linear_rgb: tuple[FiniteFloat, FiniteFloat, FiniteFloat] | None = None,
        skip_occupied: bool = False,
    ) -> types.CallToolResult:
        """Fill a sphere by replaying place_actor per cell."""
        if not 0 <= item_id <= 0xFFFF:
            return result(
                {"error": {"code": "INVALID_PARAMS", "message": "item_id must be 0..65535"}},
                error=True,
            )

        cells = sphere_cells(tuple(center), radius, mode)
        if len(cells) > MAX_FILL_CELLS:
            return result(
                {
                    "error": {
                        "code": "REGION_TOO_LARGE",
                        "message": (
                            f"mode {mode} with radius {radius} expands to {len(cells)} cells; "
                            f"limit is {MAX_FILL_CELLS}. Use a smaller radius."
                        ),
                    }
                },
                error=True,
            )

        placed, skipped, errors, revision = await place_cells(
            browser_session_id,
            ctx,
            map_session_id,
            expected_revision,
            item_id,
            cells,
            rotation_index=rotation_index,
            color_linear_rgb=color_linear_rgb,
            skip_occupied=skip_occupied,
        )
        return result(
            {
                "revision": revision,
                "map_session_id": map_session_id,
                "fill": {
                    "mode": mode,
                    "center": list(center),
                    "radius": radius,
                    "requested_cells": len(cells),
                    "placed_count": len(placed),
                    "skipped_count": len(skipped),
                    "error_count": len(errors),
                },
                "placed_actors": placed,
                "skipped_cells": skipped,
                "placement_errors": errors,
            }
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["delete_region"])
    async def delete_region(
        browser_session_id: str,
        map_session_id: str,
        expected_revision: Revision,
        min_cell: Cell,
        max_cell: Cell,
        ctx: Context,
        item_id: StrictInt | None = None,
    ) -> types.CallToolResult:
        """Delete every actor inside a box, optionally restricted to one item_id."""
        for i in range(3):
            if min_cell[i] > max_cell[i]:
                return result(
                    {
                        "error": {
                            "code": "INVALID_PARAMS",
                            "message": f"min_cell[{i}] must be <= max_cell[{i}]",
                        }
                    },
                    error=True,
                )

        fetched = await list_all_actors(
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
        )
        if not isinstance(fetched, tuple):
            return fetched
        all_actors, meta = fetched

        targets = [
            actor
            for actor in all_actors
            if len(actor.get("cell") or []) == 3
            and all(min_cell[i] <= actor["cell"][i] <= max_cell[i] for i in range(3))
            and (item_id is None or actor.get("item_id") == item_id)
        ]
        actor_ids = [actor["editor_id"] for actor in targets if actor.get("editor_id") is not None]
        revision = meta.get("revision", expected_revision)
        deleted: list[int] = []
        errors: list[dict[str, Any]] = []
        for start in range(0, len(actor_ids), MAX_ACTOR_IDS):
            chunk = actor_ids[start : start + MAX_ACTOR_IDS]
            response = await invoke(
                "delete_actors",
                browser_session_id,
                ctx,
                map_session_id=map_session_id,
                expected_revision=revision,
                actor_ids=chunk,
            )
            data = json.loads(response.content[0].text) if response.content else {}
            if response.is_error:
                errors.append({"reason": data.get("error", {})})
                break
            deleted.extend(data.get("deleted_actor_ids", []))
            revision = data.get("revision", revision)

        return result(
            {
                "revision": revision,
                "map_session_id": map_session_id,
                "matched_count": len(actor_ids),
                "deleted_count": len(deleted),
                "delete_errors": errors,
            }
        )

    @mcp.tool(annotations=read_only, description=TOOL_DESCRIPTIONS["find_free_space"])
    async def find_free_space(
        browser_session_id: str,
        ctx: Context,
        min_cell: Cell,
        max_cell: Cell,
        size: Cell,
        map_session_id: str | None = None,
        expected_revision: Revision | None = None,
        max_results: Annotated[StrictInt, Field(ge=1, le=50)] = 5,
    ) -> types.CallToolResult:
        """Scan a region for anchors where an empty cuboid of the requested size fits."""
        for i in range(3):
            if min_cell[i] > max_cell[i]:
                return result(
                    {
                        "error": {
                            "code": "INVALID_PARAMS",
                            "message": f"min_cell[{i}] must be <= max_cell[{i}]",
                        }
                    },
                    error=True,
                )
            if size[i] < 1:
                return result(
                    {
                        "error": {
                            "code": "INVALID_PARAMS",
                            "message": f"size[{i}] must be >= 1",
                        }
                    },
                    error=True,
                )
        region_volume = (
            (max_cell[0] - min_cell[0] + 1)
            * (max_cell[1] - min_cell[1] + 1)
            * (max_cell[2] - min_cell[2] + 1)
        )
        if region_volume > MAX_REGION_VOLUME:
            return result(
                {
                    "error": {
                        "code": "REGION_TOO_LARGE",
                        "message": (
                            f"region has {region_volume} cells, "
                            f"limit is {MAX_REGION_VOLUME}"
                        ),
                    }
                },
                error=True,
            )
        anchors = 1
        for i in range(3):
            span = max_cell[i] - min_cell[i] + 1 - size[i] + 1
            if span < 1:
                return result(
                    {
                        "error": {
                            "code": "INVALID_PARAMS",
                            "message": f"size[{i}] does not fit inside the region",
                        }
                    },
                    error=True,
                )
            anchors *= span
        if anchors > 2_000_000:
            return result(
                {
                    "error": {
                        "code": "REGION_TOO_LARGE",
                        "message": (
                            f"scan has {anchors} candidate anchors; limit is 2000000. "
                            "Narrow the search region. Increasing the requested size may reduce anchors."
                        ),
                        "candidate_anchors": anchors,
                        "limit": 2_000_000,
                    }
                },
                error=True,
            )

        fetched = await list_all_actors(
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
        )
        if not isinstance(fetched, tuple):
            return fetched
        all_actors, meta = fetched

        occupied = {
            tuple(actor["cell"])
            for actor in all_actors
            if len(actor.get("cell") or []) == 3
            and all(min_cell[i] <= actor["cell"][i] <= max_cell[i] for i in range(3))
        }
        spans = [max_cell[i] - min_cell[i] - size[i] + 2 for i in range(3)]
        blocked_anchors: set[tuple[int, int, int]] = set()
        for cell in occupied:
            for x in range(max(min_cell[0], cell[0] - size[0] + 1), min(cell[0], min_cell[0] + spans[0] - 1) + 1):
                for y in range(max(min_cell[1], cell[1] - size[1] + 1), min(cell[1], min_cell[1] + spans[1] - 1) + 1):
                    for z in range(max(min_cell[2], cell[2] - size[2] + 1), min(cell[2], min_cell[2] + spans[2] - 1) + 1):
                        blocked_anchors.add((x, y, z))
        found: list[dict[str, Any]] = []
        for x in range(min_cell[0], min_cell[0] + spans[0]):
            for y in range(min_cell[1], min_cell[1] + spans[1]):
                for z in range(min_cell[2], min_cell[2] + spans[2]):
                    if (x, y, z) not in blocked_anchors:
                        found.append({"min_cell": [x, y, z]})
                        if len(found) >= max_results:
                            break
                if len(found) >= max_results:
                    break
            if len(found) >= max_results:
                break

        return result(
            {
                "query_type": "free_space",
                "min_cell": list(min_cell),
                "max_cell": list(max_cell),
                "size": list(size),
                "search_region_volume": region_volume,
                "candidates": found,
                "truncated": len(found) >= max_results,
                "revision": meta.get("revision"),
                "map_session_id": meta.get("map_session_id"),
            }
        )


    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["import_model"])
    async def import_model(
        browser_session_id: str,
        ctx: Context,
        origin: Cell | None = None,
        path: str | None = None,
        plan_token: str | None = None,
        map_session_id: str | None = None,
        expected_revision: ExpectedRevision | None = None,
        mode: Literal["adaptive", "voxel", "full-size"] = "adaptive",
        resolution: Annotated[StrictInt, Field(ge=1, le=512)] | Literal["native"] | None = None,
        target_size: Point3 | None = None,
        item_ids: Annotated[list[StrictInt], Field(min_length=1, max_length=8)] | None = None,
        up_axis: Literal["auto", "y", "z"] = "auto",
        yaw_turns: Annotated[StrictInt, Field(ge=0, le=3)] = 0,
        mirror: bool = False,
        solid: bool = False,
        merge: bool = True,
        max_span: Annotated[StrictInt, Field(ge=0, le=4096)] = 0,
        color_levels: Annotated[StrictInt, Field(ge=0, le=256)] = 0,
        dry_run: bool = False,
        overwrite: bool = False,
    ) -> types.CallToolResult:
        """Voxelise a model file into the map as one undoable batch."""
        if (path is None) == (plan_token is None):
            return result(
                {
                    "error": {
                        "code": "INVALID_PARAMS",
                        "message": "Pass exactly one of path or plan_token: a path imports a new "
                        "model, a plan_token applies a plan a dry_run already produced.",
                    }
                },
                error=True,
            )
        if plan_token is not None and dry_run:
            return result(
                {
                    "error": {
                        "code": "INVALID_PARAMS",
                        "message": "A plan_token is already solved; call again without dry_run to "
                        "place it, or pass a path to solve a new model.",
                    }
                },
                error=True,
            )
        if path is not None and origin is None:
            # A solved plan already carries the origin it was fitted at, so only
            # a fresh solve has to state one.
            return result(
                {
                    "error": {
                        "code": "INVALID_PARAMS",
                        "message": "origin is required when importing a new model: it is the min "
                        "corner of the footprint at the figure's feet. A plan_token does not "
                        "need one because the plan already holds it.",
                    }
                },
                error=True,
            )
        options: dict[str, Any] = {
            "origin": list(origin) if origin is not None else None,
            "mode": mode,
            "up_axis": up_axis,
            "yaw_turns": yaw_turns,
            "mirror": mirror,
            "solid": solid,
            "merge": merge,
            "max_span": max_span,
            "color_levels": color_levels,
            **({"resolution": resolution} if resolution is not None else {}),
            **({"target_size": list(target_size)} if target_size is not None else {}),
            **({"item_ids": item_ids} if item_ids is not None else {}),
        }

        token = plan_token
        planned: dict[str, Any] | None = None
        if path is not None:
            source = _read_model_file(path)
            if isinstance(source, types.CallToolResult):
                return source
            name, payload = source
            upload = await upload_model(browser_session_id, ctx, name, payload)
            if isinstance(upload, types.CallToolResult):
                return upload
            token = upload["upload_id"]
            plan_response = await invoke(
                "plan_model", browser_session_id, ctx, upload_id=token, options=options
            )
            if plan_response.is_error and _error_code(plan_response) == "BROWSER_TIMEOUT":
                # Solving is keyed by the upload id and answered from the plan the
                # browser already holds, so asking again costs a round trip rather
                # than a second solve. Without this a large model would look
                # permanently broken to the caller.
                plan_response = await invoke(
                    "plan_model", browser_session_id, ctx, upload_id=token, options=options
                )
            if plan_response.is_error:
                return plan_response
            planned = json.loads(plan_response.content[0].text) if plan_response.content else {}

        if dry_run:
            return result(
                {
                    "dry_run": True,
                    "plan_token": token,
                    "plan": (planned or {}).get("plan"),
                    "message": "Nothing was written. Call import_model again with this plan_token "
                    "to place it, or with a different origin or resolution to change the fit.",
                }
            )

        if map_session_id is None or expected_revision is None:
            return result(
                {
                    "error": {
                        "code": "INVALID_PARAMS",
                        "message": "map_session_id and expected_revision are required unless "
                        "dry_run is true; read them from get_state.",
                        # The upload survived, so the caller can still apply it.
                        **({"plan_token": token} if token else {}),
                    }
                },
                error=True,
            )
        applied = await invoke(
            "apply_model",
            browser_session_id,
            ctx,
            map_session_id=map_session_id,
            expected_revision=expected_revision,
            upload_id=token,
            overwrite=overwrite,
        )
        if not applied.is_error:
            return applied
        # The solve is still held under this upload id, so hand it back. The
        # usual failure here is CELL_OCCUPIED, and retrying with overwrite
        # should not cost another upload and another multi-second solve.
        failed = json.loads(applied.content[0].text) if applied.content else {}
        error = failed.get("error") if isinstance(failed.get("error"), dict) else {}
        return result(
            {
                "error": {
                    **error,
                    "details": {
                        **(error.get("details") if isinstance(error.get("details"), dict) else {}),
                        "plan_token": token,
                    },
                }
            },
            error=True,
        )

    @mcp.tool(annotations=modify, description=TOOL_DESCRIPTIONS["discard_model"])
    async def discard_model(
        browser_session_id: str,
        upload_id: str,
        ctx: Context,
    ) -> types.CallToolResult:
        """Drop an uploaded model and its solved plan without placing it."""
        return await invoke("discard_model", browser_session_id, ctx, upload_id=upload_id)

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
