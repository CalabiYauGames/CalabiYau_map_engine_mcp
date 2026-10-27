"""A stateful stand-in for the browser editor, driven through the real WebSocket bridge.

The MCP server is a pass-through, so a fake that echoes one canned dict proves only that
a request was forwarded. This editor keeps actors, links, selection and history, so a
test can assert the effect of a write by reading it back through `list_actors` /
`list_links` / `get_state` — which is the only way to tell "accepted" from "applied".

Field names, error codes and the order of the precondition checks mirror the browser
adapter (`CalabiYau_Map_Engine/src/mcp/adapter.ts`), so a contract change there surfaces
here as a failure rather than as a silently weaker test.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import math
from contextlib import asynccontextmanager, suppress
from typing import Any

from harness import BROWSER_ID, MAP_ID, ORIGIN
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

WRITE_ACTIONS = frozenset(
    {
        "select_actors",
        "place_actor",
        "place_actors",
        "update_actors",
        "move_actors",
        "delete_actors",
        "connect_actors",
        "disconnect_actors",
        "undo",
        "redo",
    }
)

GRID = {"x": 8, "y": 8, "z": 4}
MODEL_EXTENSIONS = frozenset({"glb", "gltf", "obj", "stl", "fbx", "ply", "vox"})
CAPTURE_BYTES = b"fake-viewport-frame"
CAPTURE_DATA_URL = "data:image/png;base64," + base64.b64encode(CAPTURE_BYTES).decode()

CATALOG: tuple[dict[str, Any], ...] = (
    {
        "item_id": 1,
        "name": "Stone Wall",
        "description": "Solid block",
        "category": "structure",
        "colorable": True,
        "transform_mode": 1,
        "placeable": True,
        "connect_item_id": 0,
        "child_item_id": 0,
    },
    {
        "item_id": 2,
        "name": "Track Main",
        "description": "Group main piece",
        "category": "track",
        "colorable": False,
        "transform_mode": 1,
        "placeable": True,
        "connect_item_id": 0,
        "child_item_id": 3,
    },
    {
        "item_id": 3,
        "name": "Track Child",
        "description": "Group child piece",
        "category": "track",
        "colorable": False,
        "transform_mode": 1,
        "placeable": True,
        "connect_item_id": 0,
        "child_item_id": 0,
    },
    {
        "item_id": 4,
        "name": "Gate Left",
        "description": "Pair kit half",
        "category": "kit",
        "colorable": False,
        "transform_mode": 1,
        "placeable": False,
        "connect_item_id": 5,
        "child_item_id": 0,
    },
    {
        "item_id": 5,
        "name": "Gate Right",
        "description": "Pair kit half",
        "category": "kit",
        "colorable": False,
        "transform_mode": 1,
        "placeable": False,
        "connect_item_id": 4,
        "child_item_id": 0,
    },
    {
        "item_id": 6,
        "name": "Gate Frame",
        "description": "Accepts a panel that also ships as a pair kit",
        "category": "gate",
        "colorable": False,
        "transform_mode": 1,
        "placeable": True,
        "connect_item_id": 0,
        "child_item_id": 8,
    },
    {
        "item_id": 7,
        "name": "Plate",
        "description": "Flat panel that can be scaled and turned freely",
        "category": "surface",
        "colorable": True,
        "transform_mode": 2,
        "placeable": True,
        "connect_item_id": 0,
        "child_item_id": 0,
    },
    {
        "item_id": 8,
        "name": "Gate Panel Left",
        "description": "Pair kit half that also fits a gate frame",
        "category": "gate",
        "colorable": False,
        "transform_mode": 1,
        "placeable": False,
        "connect_item_id": 9,
        "child_item_id": 0,
    },
    {
        "item_id": 9,
        "name": "Gate Panel Right",
        "description": "Pair kit half",
        "category": "gate",
        "colorable": False,
        "transform_mode": 1,
        "placeable": False,
        "connect_item_id": 8,
        "child_item_id": 0,
    },
)
ITEMS = {item["item_id"]: item for item in CATALOG}


def _haystack(item: dict[str, Any]) -> str:
    return (
        f"{item['item_id']} {item['name']} {item['description']} {item['category']}".lower()
    )


class EditorError(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _members(link: dict[str, Any]) -> list[int]:
    main = link["main_actor_id"]
    return ([main] if main is not None else []) + list(link["child_actor_ids"])


class FakeEditor:
    def __init__(
        self,
        *,
        editing: bool = True,
        read_only: bool = False,
        busy: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.map_session_id = MAP_ID
        self.revision = 0
        self.actors: dict[int, dict[str, Any]] = {}
        self.links: list[dict[str, Any]] = []
        # Durable project groups: an id mapped to its member editor ids. The editor
        # keeps them outside the map record, which is why they are not in _snapshot.
        self.objects: dict[str, list[int]] = {}
        self.selection: list[int] = []
        self.camera: dict[str, list[float]] = {
            "position": [0.0, 6.0, 12.0],
            "target": [0.0, 0.0, 0.0],
        }
        self.editing = editing
        self.read_only = read_only
        self.busy = busy
        # A write takes this long to answer, which is what makes overlapping writes visible.
        self.delay = delay
        self.next_id = 1
        self._undo: list[tuple[str, dict[str, Any]]] = []
        self._redo: list[tuple[str, dict[str, Any]]] = []
        self.received: list[dict[str, Any]] = []
        self.in_flight = 0
        self.peak_in_flight = 0
        # Partial model uploads, keyed by the id the client chose, exactly as the
        # browser keeps them: bytes arrive in frames and the plan is solved later.
        self.uploads: dict[str, dict[str, Any]] = {}

    # ---------------------------------------------------------------- snapshots

    def _snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(
            {"actors": self.actors, "links": self.links, "selection": self.selection}
        )

    def _restore(self, snapshot: dict[str, Any]) -> None:
        self.actors = copy.deepcopy(snapshot["actors"])
        self.links = copy.deepcopy(snapshot["links"])
        self.selection = copy.deepcopy(snapshot["selection"])

    def _mutate(self, label: str, run) -> bool:
        before = self._snapshot()
        run()
        if self._snapshot() == before:
            return False
        self._undo.append((label, before))
        self._redo.clear()
        self.revision += 1
        return True

    # ---------------------------------------------------------------- summaries

    def _stamp(self) -> dict[str, Any]:
        return {"map_session_id": self.map_session_id, "revision": self.revision}

    def _selection_summary(self) -> dict[str, Any]:
        return {
            "selection": list(self.selection[:256]),
            "selection_count": len(self.selection),
            "selection_truncated": False,
        }

    def _summary(self, actor: dict[str, Any]) -> dict[str, Any]:
        mode = actor.get("transform_mode", 1)
        # Mode 2/3 actors carry centimetre locations instead of a grid cell, exactly like the
        # browser's actorSummary, so a caller can tell which ones place_actors cannot rebuild.
        transform = (
            {"cell": list(actor["cell"]), "rotation_index": actor["rotation_index"]}
            if mode == 1
            else {
                "location_cm": list(actor.get("location_cm") or [0, 0, 0]),
                "scale": list(actor.get("scale") or [1.0, 1.0, 1.0]),
                "rotation_degrees": list(
                    actor.get("rotation_degrees")
                    or [0.0, float(actor.get("rotation_index", 0) * 90), 0.0]
                ),
            }
        )
        return {
            "editor_id": actor["editor_id"],
            "item_id": actor["item_id"],
            "cell": list(actor["cell"]),
            "transform_mode": mode,
            "transform": transform,
            "color_linear_rgb": None if actor["color"] is None else list(actor["color"]),
            "selected": actor["editor_id"] in self.selection,
            "editable": True,
        }

    def _link_summary(self, link: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": link["kind"],
            "main_actor_id": link["main_actor_id"],
            "child_actor_ids": list(link["child_actor_ids"]),
        }

    def _links_for(self, members: set[int] | None = None) -> list[dict[str, Any]]:
        return [
            self._link_summary(link)
            for link in self.links
            if members is None or members & set(_members(link))
        ]

    # ---------------------------------------------------------------- validation

    def _actor(self, editor_id: int) -> dict[str, Any]:
        try:
            return self.actors[editor_id]
        except KeyError:
            raise EditorError("ACTOR_NOT_FOUND", f"Actor {editor_id} does not exist.") from None

    def _ids(self, value: Any, name: str, *, allow_empty: bool = False) -> list[int]:
        if not isinstance(value, list) or len(value) > 256 or (not value and not allow_empty):
            raise EditorError(
                "INVALID_PARAMS",
                f"{name} must contain {0 if allow_empty else 1}..256 IDs.",
            )
        if any(not isinstance(item, int) or isinstance(item, bool) for item in value):
            raise EditorError("INVALID_PARAMS", f"{name} must contain integers.")
        if len(set(value)) != len(value):
            raise EditorError("INVALID_PARAMS", f"{name} must not contain duplicates.")
        return list(value)

    def _cell(self, value: Any, name: str) -> list[int]:
        if (
            not isinstance(value, list)
            or len(value) != 3
            or any(not isinstance(axis, int) or isinstance(axis, bool) for axis in value)
        ):
            raise EditorError("INVALID_PARAMS", f"{name} must be three integers.")
        if not (
            0 <= value[0] < GRID["x"] and 0 <= value[1] < GRID["y"] and 0 <= value[2] < GRID["z"]
        ):
            raise EditorError("OUT_OF_BOUNDS", f"{name} is outside the map grid.")
        return list(value)

    def _page(self, params: dict[str, Any]) -> tuple[int, int]:
        offset = params.get("offset", 0)
        limit = params.get("limit", 100)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise EditorError("INVALID_PARAMS", "offset must be a non-negative integer.")
        # Mirrors MAX_PAGE_SIZE in the browser adapter; duplicate_actors asks for the
        # whole link table in one page to work out which sources belong to a group.
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 4096:
            raise EditorError("INVALID_PARAMS", "limit must be 1..4096.")
        return offset, limit

    def _check_snapshot(self, params: dict[str, Any], *, required: bool) -> None:
        if required or "map_session_id" in params:
            if params.get("map_session_id") != self.map_session_id:
                raise EditorError(
                    "STALE_SESSION", "The active map session changed.", **self._stamp()
                )
        if required or "expected_revision" in params:
            if params.get("expected_revision") != self.revision:
                raise EditorError(
                    "REVISION_CONFLICT",
                    "The map changed; query its current state before retrying.",
                    **self._stamp(),
                )

    def _require_writable(self) -> None:
        if self.read_only:
            raise EditorError("READ_ONLY", "The map is read-only.")
        if self.busy:
            raise EditorError("EDITOR_BUSY", "The editor is busy.")
        if not self.editing:
            raise EditorError("EDIT_MODE_REQUIRED", "Enable edit mode in the editor first.")

    # ---------------------------------------------------------------- seeding

    def seed(self, item_id: int, cell: list[int], *, rotation_index: int = 0) -> int:
        """Place an actor the way the editor itself would, bypassing MCP."""
        editor_id = self.next_id
        self.next_id += 1
        item = ITEMS[item_id]
        self.actors[editor_id] = {
            "editor_id": editor_id,
            "item_id": item_id,
            "cell": list(cell),
            "rotation_index": rotation_index,
            "color": [1.0, 1.0, 1.0] if item["colorable"] else None,
        }
        self.revision += 1
        return editor_id

    def seed_free(self, item_id: int, cell: list[int]) -> int:
        """Seed a mode-2 actor, which place_actors cannot rebuild from a cell."""
        editor_id = self.seed(item_id, cell)
        self.actors[editor_id]["transform_mode"] = 2
        self.actors[editor_id]["location_cm"] = [cell[0] * 100, cell[1] * 100, cell[2] * 100]
        return editor_id

    def seed_group(self, main_item: int, child_item: int, cell: list[int]) -> tuple[int, int]:
        """A group link is what makes a copy unreachable, so tests need to make one."""
        main = self.seed(main_item, cell)
        child = self.seed(child_item, [cell[0] + 1, cell[1], cell[2]])
        self.links.append(
            {"kind": "group", "main_actor_id": main, "child_actor_ids": [child]}
        )
        self.revision += 1
        return main, child

    # ------------------------------------------------------------ model import

    def _upload(self, params: dict[str, Any]) -> dict[str, Any]:
        """Append one chunk; the browser keys the buffer on the caller's id."""
        data = params.get("data_base64")
        if not isinstance(data, str):
            raise EditorError("INVALID_PARAMS", "data_base64 is required.")
        try:
            chunk = base64.b64decode(data, validate=True)
        except Exception:
            raise EditorError("INVALID_PARAMS", "data_base64 is not valid base64.") from None
        upload_id = params.get("upload_id")
        if upload_id is None:
            name = params.get("name")
            if not isinstance(name, str) or "." not in name:
                raise EditorError("INVALID_PARAMS", "name is required on the first chunk.")
            extension = name.rsplit(".", 1)[1].lower()
            if extension not in MODEL_EXTENSIONS:
                raise EditorError("INVALID_PARAMS", f"name must end in one of: {sorted(MODEL_EXTENSIONS)}.")
            total = params.get("byte_length")
            if not isinstance(total, int) or isinstance(total, bool) or total < 1:
                raise EditorError("INVALID_PARAMS", "byte_length must be a positive integer.")
            upload_id = f"upload-{len(self.uploads) + 1}"
            self.uploads[upload_id] = {
                "name": name, "total": total, "chunks": [], "received": 0, "plan": None,
            }
        entry = self.uploads.get(upload_id)
        if entry is None:
            raise EditorError("MODEL_UPLOAD_UNKNOWN", f"Unknown upload: {upload_id}.")
        offset = params.get("offset", 0)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise EditorError("INVALID_PARAMS", "offset must be a non-negative integer.")
        replay = offset < entry["received"] and offset + len(chunk) <= entry["received"]
        if not replay:
            if offset != entry["received"]:
                raise EditorError("INVALID_PARAMS", "offset must equal the bytes already received.")
            entry["chunks"].append(chunk)
            entry["received"] += len(chunk)
        return {
            "upload_id": upload_id,
            "received": entry["received"],
            "total": entry["total"],
            "complete": entry["received"] == entry["total"],
            "chunk_bytes": 640 * 1024,
        }

    def _plan(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer with a plan shaped like the browser's, without voxelising anything.

        A Python stand-in cannot rasterise a mesh, so the block count is the
        resolution and the cells are a single column at the origin. What this
        does check is the protocol around it: completeness, the options it was
        given, and that a second call for the same upload reuses the plan.
        """
        upload_id = params.get("upload_id")
        entry = self.uploads.get(upload_id)
        if entry is None:
            raise EditorError("MODEL_UPLOAD_UNKNOWN", f"Unknown upload: {upload_id}.")
        if entry["received"] != entry["total"]:
            raise EditorError("MODEL_UPLOAD_INCOMPLETE", "The upload is not complete yet.")
        options = params.get("options") or {}
        origin = options.get("origin")
        if not isinstance(origin, list) or len(origin) != 3:
            raise EditorError("INVALID_PARAMS", "origin is required.")
        if entry["plan"] is None:
            # "native" hands the grid size to the file; this stand-in has no file,
            # so it picks a number of its own and the caller only sees it echoed.
            resolution = options.get("resolution")
            blocks = 8 if resolution == "native" else int(resolution or 16)
            cells = [[origin[0], origin[1], origin[2] + index] for index in range(blocks)]
            entry["plan"] = {
                "cells": cells,
                "options": copy.deepcopy(options),
                "summary": {
                    "generation_mode": options.get("mode", "adaptive"),
                    "blocks": blocks,
                    "span": {"x": 1, "y": 1, "z": blocks},
                    "counts": {"x": 1, "y": 1, "z": blocks},
                    "voxel_size": 1.0,
                    "bounds_cells": {
                        "min": list(origin),
                        "max": [origin[0] + 1, origin[1] + 1, origin[2] + blocks],
                    },
                    "stats": {"blocks": blocks, "compact": blocks},
                    "warning": None,
                },
            }
        return {
            **self._stamp(),
            "upload_id": upload_id,
            "chunk_bytes": 640 * 1024,
            "plan": copy.deepcopy(entry["plan"]["summary"]),
        }

    def seed_pair(
        self, cell: list[int], *, left_item: int = 4, right_item: int = 5
    ) -> tuple[int, int]:
        """Pair kits can only be placed by the editor, so tests seed them directly."""
        left = self.seed(left_item, cell)
        right = self.seed(right_item, [cell[0] + 1, cell[1], cell[2]])
        self.links.append(
            {"kind": "pair", "main_actor_id": left, "child_actor_ids": [right]}
        )
        self.revision += 1
        return left, right

    # ---------------------------------------------------------------- actions

    async def handle(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action in WRITE_ACTIONS and self.delay:
            await asyncio.sleep(self.delay)
        if action == "get_state":
            return {
                **self._stamp(),
                "map": {"id": "map-1", "name": "Test Map"},
                "phase": "ready",
                "dirty": bool(self._undo),
                "can_undo": bool(self._undo),
                "can_redo": bool(self._redo),
                "undo_label": self._undo[-1][0] if self._undo else None,
                "redo_label": self._redo[-1][0] if self._redo else None,
                **self._selection_summary(),
                "busy": self.busy,
                "read_only": self.read_only,
                "read_only_reason": "Read-only preview" if self.read_only else None,
                "editing": self.editing,
                "tool": "select",
                "preview": False,
                "grid": dict(GRID),
                "persistent_actor_count": len(self.actors),
            }
        if action == "get_camera":
            return {"camera": copy.deepcopy(self.camera)}
        if action == "set_camera":
            position = params.get("position")
            target = params.get("target")
            for name, value in (("position", position), ("target", target)):
                if (
                    not isinstance(value, list)
                    or len(value) != 3
                    or any(
                        not isinstance(axis, (int, float)) or isinstance(axis, bool)
                        for axis in value
                    )
                ):
                    raise EditorError("INVALID_PARAMS", f"{name} must be three numbers.")
            self.camera = {"position": list(position), "target": list(target)}
            return {"camera": copy.deepcopy(self.camera)}
        if action == "capture_view":
            camera = {
                "position": list(params.get("position", self.camera["position"])),
                "target": list(params.get("target", self.camera["target"])),
            }
            if "position" in params:
                self.camera = copy.deepcopy(camera)
            return {
                "camera": camera,
                "image": {
                    "data_url": CAPTURE_DATA_URL,
                    "mime_type": "image/png",
                    "width": params.get("width", 960),
                    "height": params.get("height", 540),
                },
            }
        if action == "list_items":
            offset, limit = self._page(params)
            query = (params.get("query") or "").strip().lower()
            rows = [
                item
                for item in CATALOG
                if item["placeable"]
                and (not query or query in _haystack(item))
            ]
            return {
                **self._stamp(),
                "items": [dict(item) for item in rows[offset : offset + limit]],
                "offset": offset,
                "limit": limit,
                "total": len(rows),
            }
        if action == "list_actors":
            self._check_snapshot(params, required=False)
            offset, limit = self._page(params)
            item_id = params.get("item_id")
            rows = [actor for actor in self.actors.values() if item_id in (None, actor["item_id"])]
            if params.get("selected_only"):
                rows = [actor for actor in rows if actor["editor_id"] in self.selection]
            rows.sort(key=lambda actor: actor["editor_id"])
            return {
                **self._stamp(),
                "actors": [self._summary(actor) for actor in rows[offset : offset + limit]],
                "offset": offset,
                "limit": limit,
                "total": len(rows),
            }
        if action == "list_links":
            self._check_snapshot(params, required=False)
            offset, limit = self._page(params)
            kind = params.get("kind")
            if kind is not None and kind not in {"pair", "group", "orphan"}:
                raise EditorError("INVALID_PARAMS", "kind must be pair, group, or orphan.")
            members = (
                None
                if params.get("actor_ids") is None
                else set(self._ids(params["actor_ids"], "actor_ids", allow_empty=True))
            )
            rows = [
                link
                for link in self.links
                if (kind is None or link["kind"] == kind)
                and (members is None or members & set(_members(link)))
            ]
            return {
                **self._stamp(),
                "links": [self._link_summary(link) for link in rows[offset : offset + limit]],
                "offset": offset,
                "limit": limit,
                "total": len(rows),
            }
        if action == "upload_model":
            return self._upload(params)
        if action == "discard_model":
            upload_id = params.get("upload_id")
            return {
                "upload_id": upload_id,
                "released": self.uploads.pop(upload_id, None) is not None,
                "held": sorted(self.uploads),
            }
        if action == "plan_model":
            return self._plan(params)

        self._check_snapshot(params, required=True)
        self._require_writable()

        if action == "apply_model":
            upload_id = params.get("upload_id")
            entry = self.uploads.get(upload_id)
            plan = entry.get("plan") if entry else None
            if plan is None:
                raise EditorError("MODEL_PLAN_UNKNOWN", f"No plan for upload {upload_id}.")
            cells = plan["cells"]
            if params.get("overwrite") is not True:
                blocked = [cell for cell in cells if any(a["cell"] == cell for a in self.actors.values())]
                if blocked:
                    raise EditorError(
                        "CELL_OCCUPIED",
                        f"{len(blocked)} of {len(cells)} blocks would overwrite an existing actor.",
                        occupied_blocks=len(blocked),
                        sample_cells=blocked[:16],
                    )

            def run() -> None:
                for cell in cells:
                    editor_id = self.next_id
                    self.next_id += 1
                    self.actors[editor_id] = {
                        "editor_id": editor_id,
                        "item_id": 1,
                        "cell": list(cell),
                        "rotation_index": 0,
                        "color": [1.0, 1.0, 1.0],
                    }

            self._mutate(f"import {len(cells)} blocks", run)
            del self.uploads[upload_id]
            return {
                **self._stamp(),
                "placed_count": len(cells),
                "planned_blocks": len(cells),
                "plan": copy.deepcopy(plan["summary"]),
                "changed": True,
            }

        if action == "select_actors":
            ids = self._ids(params.get("actor_ids"), "actor_ids", allow_empty=True)
            for editor_id in ids:
                self._actor(editor_id)
            mode = params.get("mode", "replace")
            if mode not in {"replace", "add", "toggle"}:
                raise EditorError("INVALID_PARAMS", "mode must be replace, add, or toggle.")

            def run() -> None:
                if mode == "replace":
                    self.selection = list(ids)
                elif mode == "add":
                    self.selection = list(dict.fromkeys([*self.selection, *ids]))
                else:
                    for editor_id in ids:
                        if editor_id in self.selection:
                            self.selection.remove(editor_id)
                        else:
                            self.selection.append(editor_id)

            changed = self._mutate(f"select {mode}", run)
            return {**self._stamp(), **self._selection_summary(), "changed": changed}

        if action in {"place_actor", "place_actors"}:
            placements = (
                [params]
                if action == "place_actor"
                else params.get("placements")
            )
            if not isinstance(placements, list):
                raise EditorError("INVALID_PARAMS", "placements must be an array.")
            if len(placements) > 4096:
                raise EditorError("BATCH_TOO_LARGE", "placements must contain at most 4096 items.")
            prepared: list[dict[str, Any]] = []
            cells: set[tuple[int, int, int]] = set()
            errors: list[dict[str, Any]] = []
            for index, placement in enumerate(placements):
                if not isinstance(placement, dict):
                    errors.append(
                        {"index": index, "reason": "INVALID_PARAMS", "message": "Not an object."}
                    )
                    continue
                item_id = placement.get("item_id")
                item = ITEMS.get(item_id)
                transform = placement.get("transform")
                if transform is not None:
                    # A free placement states its own pose, so it claims no cell and
                    # may share one with any number of other plates.
                    if placement.get("cell") is not None:
                        errors.append(
                            {"index": index, "reason": "INVALID_PARAMS", "message": "Pass either cell or transform."}
                        )
                        continue
                    if item is None or not item["placeable"] or item["connect_item_id"]:
                        errors.append({"index": index, "reason": "ITEM_NOT_PLACEABLE"})
                        continue
                    if item["transform_mode"] != 2:
                        errors.append(
                            {"index": index, "reason": "ITEM_NOT_FREELY_PLACEABLE", "item_id": item_id}
                        )
                        continue
                    if not isinstance(transform, dict) or "location_cm" not in transform:
                        errors.append(
                            {"index": index, "reason": "INVALID_PARAMS", "message": "transform needs location_cm."}
                        )
                        continue
                    location = [float(value) for value in transform["location_cm"]]
                    prepared.append(
                        {
                            "index": index,
                            "item": item,
                            "item_id": item_id,
                            "cell": [
                                round((location[0] - 50) / 100),
                                round((location[1] - 50) / 100),
                                round(location[2] / 100),
                            ],
                            "rotation": 0,
                            "color": list(placement.get("color_linear_rgb") or [1.0, 1.0, 1.0]),
                            "free": {
                                "location_cm": location,
                                "scale": [float(value) for value in transform.get("scale", [1.0, 1.0, 1.0])],
                                "rotation_degrees": [
                                    float(value) for value in transform.get("rotation_degrees", [0.0, 0.0, 0.0])
                                ],
                            },
                        }
                    )
                    continue
                try:
                    cell = self._cell(placement.get("cell"), "cell")
                except EditorError as exc:
                    errors.append({"index": index, "reason": exc.code, "message": str(exc)})
                    continue
                cell_key = tuple(cell)
                if cell_key in cells:
                    errors.append({"index": index, "reason": "DUPLICATE_CELL", "cell": cell})
                    continue
                cells.add(cell_key)
                rotation = placement.get("rotation_index", 0)
                if not isinstance(rotation, int) or isinstance(rotation, bool) or not 0 <= rotation <= 3:
                    errors.append({"index": index, "reason": "INVALID_PARAMS", "details": {"message": "rotation_index must be 0..3."}})
                    continue
                if item is None or not item["placeable"] or item["connect_item_id"]:
                    errors.append({"index": index, "reason": "ITEM_NOT_PLACEABLE"})
                    continue
                occupants = [actor for actor in self.actors.values() if actor["cell"] == cell]
                if occupants:
                    errors.append(
                        {
                            "index": index,
                            "reason": "CELL_OCCUPIED",
                            "cell": cell,
                            "occupants": [
                                {
                                    "editor_id": occupant["editor_id"],
                                    "item_id": occupant["item_id"],
                                    "color_linear_rgb": None
                                    if occupant["color"] is None
                                    else list(occupant["color"]),
                                }
                                for occupant in occupants
                            ],
                        }
                    )
                    continue
                color = list(placement.get("color_linear_rgb") or [1.0, 1.0, 1.0])
                prepared.append({"index": index, "item": item, "item_id": item_id, "cell": cell, "rotation": rotation, "color": color})

            placed_actors: list[dict[str, Any]] = []
            def run() -> None:
                for entry in prepared:
                    editor_id = self.next_id
                    self.next_id += 1
                    actor = {"editor_id": editor_id, "item_id": entry["item_id"], "cell": entry["cell"], "rotation_index": entry["rotation"], "color": entry["color"] if entry["item"]["colorable"] else None}
                    if entry.get("free"):
                        actor["transform_mode"] = 2
                        actor.update(entry["free"])
                    self.actors[editor_id] = actor
                    placed_actors.append({"index": entry["index"], **self._summary(actor)})
            if action == "place_actors":
                label = "place actors"
            else:
                label = f"place {prepared[0]['item']['name']}" if prepared else "place"
            changed = self._mutate(label, run)
            if action == "place_actor" and errors:
                error = errors[0]
                raise EditorError(
                    error["reason"],
                    error.get("message", "Placement failed."),
                    **{
                        key: value
                        for key, value in error.items()
                        if key not in {"reason", "index", "message"}
                    },
                )
            return {**self._stamp(), "actor": placed_actors[0] if action == "place_actor" and placed_actors else None, "placed_actors": placed_actors, "placement_errors": errors, "changed": changed}

        if action == "polar_array":
            ids = self._ids(params.get("actor_ids"), "actor_ids")
            center = self._cell(params.get("center"), "center")
            axis = params.get("axis")
            if axis not in ("x", "y", "z"):
                raise EditorError("INVALID_PARAMS", "axis must be x, y, or z.")
            count = params.get("count")
            if not isinstance(count, int) or isinstance(count, bool) or not 2 <= count <= 256:
                raise EditorError("INVALID_PARAMS", "count must be 2..256.")
            a, b = (1, 2) if axis == "x" else (0, 2) if axis == "y" else (0, 1)
            targets: list[dict[str, Any]] = []
            for editor_id in ids:
                actor = self._actor(editor_id)
                for index in range(1, count):
                    angle = math.tau * index / count
                    dx = actor["cell"][a] - center[a]
                    dy = actor["cell"][b] - center[b]
                    cell = list(actor["cell"])
                    cell[a] = round(center[a] + dx * math.cos(angle) - dy * math.sin(angle))
                    cell[b] = round(center[b] + dx * math.sin(angle) + dy * math.cos(angle))
                    targets.append({"source": editor_id, "item_id": actor["item_id"], "cell": cell, "color": actor["color"]})

            placed: list[dict[str, Any]] = []

            def run() -> None:
                for target in targets:
                    if any(actor["cell"] == target["cell"] for actor in self.actors.values()):
                        continue
                    editor_id = self.next_id
                    self.next_id += 1
                    actor = {
                        "editor_id": editor_id,
                        "item_id": target["item_id"],
                        "cell": target["cell"],
                        "rotation_index": 0,
                        "color": None if target["color"] is None else list(target["color"]),
                    }
                    self.actors[editor_id] = actor
                    placed.append({"source_actor_id": target["source"], **self._summary(actor)})

            changed = self._mutate("极坐标阵列", run)
            return {**self._stamp(), "placed_actors": placed, "operation": {"kind": "polar_array", "source_count": len(ids)}, "changed": changed}

        if action == "update_actors":
            ids = self._ids(params.get("actor_ids"), "actor_ids")
            free_keys = ("location_cm", "location_delta_cm", "scale", "rotation_degrees")
            if not any(
                key in params
                for key in ("color_linear_rgb", "rotation_index", "params", "item_id", *free_keys)
            ):
                raise EditorError("INVALID_PARAMS", "Provide at least one actor property.")
            for editor_id in ids:
                self._actor(editor_id)
            wants_free = any(key in params for key in free_keys)
            if wants_free:
                fixed = [
                    editor_id
                    for editor_id in ids
                    if self.actors[editor_id].get("transform_mode", 1) != 2
                ]
                if fixed:
                    raise EditorError(
                        "ACTOR_NOT_FREELY_PLACEABLE",
                        "These actors cannot store a free pose.",
                        editor_ids=fixed,
                    )
            def run() -> None:
                for editor_id in ids:
                    actor = self.actors[editor_id]
                    if "color_linear_rgb" in params:
                        actor["color"] = list(params["color_linear_rgb"])
                    if "item_id" in params:
                        actor["item_id"] = params["item_id"]
                    if wants_free:
                        actor["transform_mode"] = 2
                        if "location_cm" in params:
                            actor["location_cm"] = list(params["location_cm"])
                        if "location_delta_cm" in params:
                            base = actor.get("location_cm") or [0.0, 0.0, 0.0]
                            actor["location_cm"] = [
                                base[axis] + params["location_delta_cm"][axis] for axis in range(3)
                            ]
                        if "scale" in params:
                            actor["scale"] = list(params["scale"])
                        if "rotation_degrees" in params:
                            actor["rotation_degrees"] = list(params["rotation_degrees"])
                        elif "rotation_index" in params:
                            current = actor.get("rotation_degrees") or [0.0, 0.0, 0.0]
                            actor["rotation_degrees"] = [
                                current[0], float(params["rotation_index"] * 90), current[2]
                            ]
                    elif "rotation_index" in params:
                        actor["rotation_index"] = params["rotation_index"]
            changed = self._mutate("update actors", run)
            return {**self._stamp(), "actors": [self._summary(self.actors[editor_id]) for editor_id in ids], "changed": changed}

        if action == "move_actors":
            ids = self._ids(params.get("actor_ids"), "actor_ids")
            delta = self._cell_delta(params.get("delta"))
            targets: dict[int, list[int]] = {}
            for editor_id in ids:
                actor = self._actor(editor_id)
                targets[editor_id] = self._cell(
                    [actor["cell"][axis] + delta[axis] for axis in range(3)], "cell"
                )
            occupied = {
                tuple(actor["cell"])
                for actor in self.actors.values()
                if actor["editor_id"] not in ids
            }
            if any(tuple(target) in occupied for target in targets.values()):
                raise EditorError(
                    "MOVE_REJECTED", "The actors cannot move into the requested cells."
                )
            if len({tuple(target) for target in targets.values()}) != len(targets):
                raise EditorError(
                    "MOVE_REJECTED", "The actors cannot share one destination cell."
                )

            def run() -> None:
                for editor_id, target in targets.items():
                    self.actors[editor_id]["cell"] = target

            changed = self._mutate("move actors", run)
            if not changed and any(axis != 0 for axis in delta):
                raise EditorError(
                    "MOVE_REJECTED", "The actors cannot move into the requested cells."
                )
            return {
                **self._stamp(),
                "actors": [self._summary(self.actors[editor_id]) for editor_id in ids],
                "changed": changed,
            }

        if action == "delete_actors":
            ids = self._ids(params.get("actor_ids"), "actor_ids")
            for editor_id in ids:
                self._actor(editor_id)
            partners = {
                member
                for link in self.links
                if link["kind"] == "pair" and set(_members(link)) & set(ids)
                for member in _members(link)
            }
            doomed = list(dict.fromkeys([*ids, *sorted(partners)]))
            for editor_id in doomed:
                self._actor(editor_id)

            def run() -> None:
                for editor_id in doomed:
                    self.actors.pop(editor_id, None)
                    if editor_id in self.selection:
                        self.selection.remove(editor_id)
                self.links = [
                    link for link in self.links if not set(_members(link)) & set(doomed)
                ]

            self._mutate("delete actors", run)
            return {
                **self._stamp(),
                "deleted_actor_ids": [i for i in doomed if i not in self.actors],
                "changed": True,
            }

        if action == "connect_actors":
            main = params.get("main_actor_id")
            if not isinstance(main, int) or isinstance(main, bool):
                raise EditorError("INVALID_PARAMS", "main_actor_id must be an integer.")
            children = self._ids(params.get("child_actor_ids"), "child_actor_ids")
            if main in children:
                raise EditorError(
                    "INVALID_PARAMS", "child_actor_ids must not contain main_actor_id."
                )
            self._actor(main)
            for editor_id in children:
                self._actor(editor_id)
            main_item = ITEMS[self.actors[main]["item_id"]]
            if main_item["child_item_id"] == 0:
                raise EditorError(
                    "ITEM_NOT_CONNECTABLE",
                    "The main actor's item does not accept group children.",
                    actor_id=main,
                    item_id=main_item["item_id"],
                )
            for editor_id in children:
                if self.actors[editor_id]["item_id"] != main_item["child_item_id"]:
                    raise EditorError(
                        "CHILD_ITEM_MISMATCH",
                        f"Actor {editor_id} is not a child item of actor {main}.",
                        actor_id=editor_id,
                        item_id=self.actors[editor_id]["item_id"],
                        expected_item_id=main_item["child_item_id"],
                    )
            pair_members = {
                member
                for link in self.links
                if link["kind"] == "pair"
                for member in _members(link)
            }
            for editor_id in [main, *children]:
                if editor_id in pair_members:
                    raise EditorError(
                        "ACTOR_IN_PAIR",
                        f"Actor {editor_id} is half of a pair; disconnect it first.",
                        actor_id=editor_id,
                    )
            touched = {main, *children}
            existing = next(
                (
                    link
                    for link in self.links
                    if link["kind"] == "group" and link["main_actor_id"] == main
                ),
                None,
            )
            if existing is not None and existing["child_actor_ids"] == children:
                return {**self._stamp(), "links": self._links_for(touched), "changed": False}

            reported = set(touched)
            for link in self.links:
                if set(_members(link)) & touched:
                    reported |= set(_members(link))

            def run() -> None:
                removed = [link for link in self.links if set(_members(link)) & touched]
                self.links = [link for link in self.links if link not in removed]
                for link in removed:
                    leftover = [
                        member
                        for member in _members(link)
                        if member not in touched
                    ]
                    if len(leftover) > 1:
                        self.links.append(
                            {"kind": "orphan", "main_actor_id": None, "child_actor_ids": leftover}
                        )
                self.links.append(
                    {"kind": "group", "main_actor_id": main, "child_actor_ids": list(children)}
                )

            self._mutate("connect actors", run)
            return {**self._stamp(), "links": self._links_for(reported), "changed": True}

        if action == "disconnect_actors":
            ids = self._ids(params.get("actor_ids"), "actor_ids")
            for editor_id in ids:
                self._actor(editor_id)
            affected = [link for link in self.links if set(_members(link)) & set(ids)]
            if not affected:
                return {**self._stamp(), "links": [], "changed": False}
            reported = {member for link in affected for member in _members(link)}

            def run() -> None:
                for link in affected:
                    self.links.remove(link)
                    if link["kind"] == "pair":
                        continue
                    sequence = [link["main_actor_id"], *link["child_actor_ids"]]
                    cut = min(
                        sequence.index(editor_id) for editor_id in ids if editor_id in sequence
                    )
                    head, tail = sequence[:cut], sequence[cut + 1 :]
                    if len(head) > 1:
                        self.links.append(
                            {"kind": "group", "main_actor_id": head[0], "child_actor_ids": head[1:]}
                        )
                    if len(tail) > 1:
                        self.links.append(
                            {"kind": "orphan", "main_actor_id": None, "child_actor_ids": tail}
                        )

            self._mutate("disconnect actors", run)
            return {**self._stamp(), "links": self._links_for(reported), "changed": True}

        if action == "create_scene_object":
            name = params.get("name")
            if not isinstance(name, str) or not name.strip():
                raise EditorError("INVALID_PARAMS", "name must be a non-empty string.")
            ids = self._ids(params.get("actor_ids"), "actor_ids")
            for editor_id in ids:
                self._actor(editor_id)
            object_id = f"object-{len(self.objects) + 1}"
            self.objects[object_id] = list(ids)
            self.revision += 1
            return {
                **self._stamp(),
                "object_id": object_id,
                "actor_ids": list(ids),
                "changed": True,
            }

        if action == "delete_scene_object":
            object_id = params.get("object_id")
            if not isinstance(object_id, str) or object_id not in self.objects:
                raise EditorError("PROJECT_ENTITY_NOT_FOUND", f"Unknown object: {object_id}.")
            # Deleting a project object takes its members with it.
            for editor_id in self.objects.pop(object_id):
                self.actors.pop(editor_id, None)
            self.revision += 1
            return {**self._stamp(), "object_id": object_id, "changed": True}

        if action in {"undo", "redo"}:
            if action == "undo":
                stack, other = self._undo, self._redo
            else:
                stack, other = self._redo, self._undo
            if not stack:
                return {
                    **self._stamp(),
                    "can_undo": bool(self._undo),
                    "can_redo": bool(self._redo),
                    "changed": False,
                }
            label, snapshot = stack.pop()
            other.append((label, self._snapshot()))
            self._restore(snapshot)
            self.revision += 1
            return {
                **self._stamp(),
                "can_undo": bool(self._undo),
                "can_redo": bool(self._redo),
                "changed": True,
            }

        raise EditorError("UNKNOWN_ACTION", f"Unsupported action: {action}.")

    def _cell_delta(self, value: Any) -> list[int]:
        if (
            not isinstance(value, list)
            or len(value) != 3
            or any(not isinstance(axis, int) or isinstance(axis, bool) for axis in value)
        ):
            raise EditorError("INVALID_PARAMS", "delta must be three integers.")
        return list(value)


@asynccontextmanager
async def editor_bridge(
    base: str,
    token: str,
    editor: FakeEditor,
    *,
    hold: frozenset[str] = frozenset(),
):
    """Pair the fake editor with the bridge and answer every request from its state."""
    async with connect(base.replace("http://", "ws://") + "/bridge", origin=ORIGIN) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "version": 1,
                    "token": token,
                    "session_id": BROWSER_ID,
                    "label": "Stateful test editor",
                }
            )
        )
        welcome = json.loads(await ws.recv())
        assert welcome == {"type": "welcome", "version": 1, "session_id": BROWSER_ID}

        async def serve() -> None:
            try:
                async for raw in ws:
                    message = json.loads(raw)
                    if message["type"] == "ping":
                        await ws.send(json.dumps({"type": "pong", "version": 1}))
                        continue
                    assert message["type"] == "request", message
                    editor.received.append(message)
                    if message["action"] in hold:
                        continue
                    if message["action"] in WRITE_ACTIONS:
                        editor.in_flight += 1
                        editor.peak_in_flight = max(editor.peak_in_flight, editor.in_flight)
                    try:
                        result = await editor.handle(message["action"], message["params"])
                        frame: dict[str, Any] = {
                            "type": "response",
                            "version": 1,
                            "id": message["id"],
                            "ok": True,
                            "result": result,
                        }
                    except EditorError as exc:
                        frame = {
                            "type": "response",
                            "version": 1,
                            "id": message["id"],
                            "ok": False,
                            "error": {
                                "code": exc.code,
                                "message": str(exc),
                                "details": exc.details,
                            },
                        }
                    finally:
                        if message["action"] in WRITE_ACTIONS:
                            editor.in_flight -= 1
                    await ws.send(json.dumps(frame))
            except ConnectionClosed:
                return

        task = asyncio.create_task(serve())
        try:
            yield editor
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
