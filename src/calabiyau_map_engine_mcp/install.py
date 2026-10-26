"""Register the MCP server with the desktop clients installed on this machine.

Discovery stays deliberately narrow: a client is only touched when its own directory already
exists, so an unrelated application never gains a stray config file. Every rewrite keeps a
timestamped backup, because these files also hold the user's other servers and losing them
would be far worse than not installing anything.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings

SERVER_KEY = "calabiyau-map-engine"


@dataclass(frozen=True)
class Client:
    name: str
    path: Path
    root: str
    # VS Code stores servers under "servers" and needs an explicit type; the others infer it.
    stdio_type: str | None = None
    # Only create the file when the client directory exists but has never been configured.
    create: bool = True


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")


def client_targets() -> list[Client]:
    home = Path.home()
    appdata = _appdata()
    return [
        Client("Claude Desktop", appdata / "Claude" / "claude_desktop_config.json", "mcpServers"),
        Client("Cursor", home / ".cursor" / "mcp.json", "mcpServers"),
        # Trae is a VS Code fork; its user-scope MCP file uses the same mcpServers shape.
        Client("Trae CN", appdata / "Trae CN" / "User" / "mcp.json", "mcpServers"),
        Client("Trae", appdata / "Trae" / "User" / "mcp.json", "mcpServers"),
        Client(
            "VS Code",
            appdata / "Code" / "User" / "mcp.json",
            "servers",
            stdio_type="stdio",
            create=False,
        ),
    ]


def detected(targets: list[Client]) -> list[Client]:
    return [
        target
        for target in targets
        if target.path.exists() or (target.create and target.path.parent.is_dir())
    ]


def stdio_entry(command: str) -> dict[str, Any]:
    return {
        "command": command,
        "args": ["-m", "calabiyau_map_engine_mcp", "--transport", "stdio"],
    }


def remote_entry(settings: Settings, transport: str) -> dict[str, Any]:
    details = settings.pairing_details()
    url = details["sse_url"] if transport == "sse" else details["mcp_url"]
    return {
        "type": "sse" if transport == "sse" else "http",
        "url": url,
        "headers": {"Authorization": f"Bearer {details['http_token']}"},
    }


def entry_for(settings: Settings, transport: str, command: str) -> dict[str, Any]:
    if transport in {"http", "sse"}:
        return remote_entry(settings, transport)
    return stdio_entry(command)


def merge(target: Client, entry: dict[str, Any]) -> str:
    path = target.path
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return f"  {target.name:<16} skipped: {path} is not valid JSON"
        if not isinstance(loaded, dict):
            return f"  {target.name:<16} skipped: {path} does not hold a JSON object"
        data = loaded
    servers = data.get(target.root)
    if not isinstance(servers, dict):
        servers = {}
    payload = dict(entry)
    if target.stdio_type and "command" in payload:
        payload = {"type": target.stdio_type, **payload}
    servers[SERVER_KEY] = payload
    data[target.root] = servers

    path.parent.mkdir(parents=True, exist_ok=True)
    backup = ""
    if path.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup = f"{path}.bak-{stamp}"
        shutil.copy2(path, backup)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".mcp-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(data, output, indent=2, ensure_ascii=False)
            output.write("\n")
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    kept = f" (backup {Path(backup).name})" if backup else ""
    return f"  {target.name:<16} wrote {path}{kept}"


def install(
    settings: Settings, *, apply: bool, transport: str, command: str
) -> str:
    details = settings.pairing_details()
    entry = entry_for(settings, transport, command)
    lines = [
        "CalabiYau Map Engine MCP",
        "",
        f"  bridge   {details['bridge_url']}",
        f"  mcp      {details['mcp_url']}",
        f"  sse      {details['sse_url']}",
        f"  token    {details['pairing_token']}   <- paste into the editor: Settings > MCP",
    ]
    if transport in {"http", "sse"}:
        lines += [
            f"  bearer   {details['http_token']}   <- only for HTTP/SSE clients",
            "",
            "Start the resident server first, or these clients will fail to connect:",
            f"  python -m calabiyau_map_engine_mcp --transport {transport}",
        ]
    lines += ["", f"Entry for transport '{transport}':", json.dumps(entry, indent=2), ""]

    targets = detected(client_targets())
    if not apply:
        lines.append("Detected clients (nothing written; rerun with --apply):")
        if not targets:
            lines.append("  none - paste the entry above into your client by hand")
        for target in targets:
            lines.append(f"  {target.name:<16} {target.path}")
        return "\n".join(lines)

    lines.append("Writing client configs:")
    written = [merge(target, entry) for target in targets]
    lines.extend(written or ["  none - paste the entry above into your client by hand"])
    lines += [
        "",
        "Restart the client so it reloads the config.",
        "Then open the editor and pair it with the token above.",
    ]
    return "\n".join(lines)
