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
import copy
import json
from contextlib import asynccontextmanager, suppress
from typing import Any

from harness import BROWSER_ID, MAP_ID, ORIGIN
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

WRITE_ACTIONS = frozenset(
    {
        "select_actors",
        "place_actor",
        "move_actors",
        "delete_actors",
        "connect_actors",
        "disconnect_actors",
        "undo",
        "redo",
    }
)

GRID = {"x": 8, "y": 8, "z": 4}

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
        self.selection: list[int] = []
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
        return {
            "editor_id": actor["editor_id"],
            "item_id": actor["item_id"],
            "cell": list(actor["cell"]),
            "transform_mode": 1,
            "transform": {
                "cell": list(actor["cell"]),
                "rotation_index": actor["rotation_index"],
            },
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
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise EditorError("INVALID_PARAMS", "limit must be 1..500.")
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

        self._check_snapshot(params, required=True)
        self._require_writable()

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

        if action == "place_actor":
            item_id = params.get("item_id")
            item = ITEMS.get(item_id)
            cell = self._cell(params.get("cell"), "cell")
            rotation = params.get("rotation_index", 0)
            valid_rotation = (
                isinstance(rotation, int)
                and not isinstance(rotation, bool)
                and 0 <= rotation <= 3
            )
            if not valid_rotation:
                raise EditorError("INVALID_PARAMS", "rotation_index must be 0..3.")
            if item is None or not item["placeable"] or item["connect_item_id"]:
                raise EditorError(
                    "ITEM_NOT_PLACEABLE",
                    "This item cannot be placed as a standalone actor through MCP.",
                )
            occupants = [
                {
                    "editor_id": actor["editor_id"],
                    "item_id": actor["item_id"],
                    "color_linear_rgb": actor["color"],
                }
                for actor in self.actors.values()
                if actor["cell"] == cell
            ]
            if occupants:
                raise EditorError(
                    "CELL_OCCUPIED",
                    "cell already contains an actor.",
                    cell=cell,
                    occupants=occupants,
                )
            color = list(params.get("color_linear_rgb") or [1.0, 1.0, 1.0])
            editor_id = self.next_id
            self.next_id += 1
            actor = {
                "editor_id": editor_id,
                "item_id": item_id,
                "cell": cell,
                "rotation_index": rotation,
                "color": color if item["colorable"] else None,
            }
            self._mutate(f"place {item['name']}", lambda: self.actors.__setitem__(editor_id, actor))
            return {**self._stamp(), "actor": self._summary(actor), "changed": True}

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
