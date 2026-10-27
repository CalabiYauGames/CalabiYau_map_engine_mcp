"""One-time HTTP upload slots, so a capture never has to cross the WebSocket.

The bridge frame cap is 1 MiB.  A 1600x1000 PNG of a dense model is several
times that, which is why every capture used to be downgraded to a small JPEG --
and a small JPEG is exactly the wrong thing when the model is made of 0.15 m
plates and the point of the capture is to see them.

So the bytes take a different road.  Before dispatching `capture_view` the
server mints a slot and hands the browser an absolute URL; the browser POSTs the
image straight to it; the tool waits on the slot.  The slot id is a single-use
capability that expires, so no long-lived secret travels in a request body, and
the bridge frame stays a few hundred bytes.

A browser that does not know about `upload_url` still answers with
`image.data_url`, and the tool falls back to decoding that -- the old path is
kept so an older tab keeps working.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass

# A 1600x1000 PNG of this map is a few megabytes; 64 MiB is slack, not a target.
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
CAPTURE_TTL_SECONDS = 120.0


@dataclass(frozen=True)
class Capture:
    data: bytes
    content_type: str


class CaptureRelay:
    """Pending upload slots keyed by an unguessable id."""

    def __init__(
        self,
        *,
        ttl: float = CAPTURE_TTL_SECONDS,
        max_bytes: int = MAX_CAPTURE_BYTES,
    ) -> None:
        self._slots: dict[str, asyncio.Future[Capture | None]] = {}
        self._ttl = ttl
        self._max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def open(self, host: str) -> tuple[str, str]:
        """Reserve a slot and return `(capture_id, absolute_url)`."""
        capture_id = secrets.token_urlsafe(24)
        self._slots[capture_id] = asyncio.get_running_loop().create_future()
        return capture_id, f"http://{host}/capture/{capture_id}"

    def deliver(self, capture_id: str, data: bytes, content_type: str) -> bool:
        """Hand the bytes to whoever is waiting.  False means unknown/expired/duplicate."""
        future = self._slots.get(capture_id)
        if future is None or future.done():
            return False
        future.set_result(Capture(data, content_type))
        return True

    async def wait(self, capture_id: str, timeout: float | None = None) -> Capture | None:
        """Await the upload, then release the slot either way."""
        future = self._slots.get(capture_id)
        if future is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout or self._ttl)
        except TimeoutError:
            return None
        finally:
            # Dropping the slot is what makes a late POST a 404 instead of a
            # write into a capture nobody will ever read.
            self._slots.pop(capture_id, None)

    def discard(self, capture_id: str) -> None:
        """Drop a slot, releasing any waiter instead of leaving it to time out."""
        future = self._slots.pop(capture_id, None)
        if future is not None and not future.done():
            future.set_result(None)

    def pending(self) -> int:
        return len(self._slots)
