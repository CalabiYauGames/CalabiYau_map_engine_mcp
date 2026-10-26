"""`install.py` rewrites other applications' config files, so every branch is a risk.

Nothing here needs a server: these are the decisions that decide whether a user's
existing MCP servers survive, whether a backup exists when they do not, and whether an
unrelated application ever gains a config file it did not have.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harness import credentials_value

from calabiyau_map_engine_mcp.config import Settings
from calabiyau_map_engine_mcp.install import (
    SERVER_KEY,
    Client,
    detected,
    entry_for,
    install,
    merge,
    remote_entry,
    stdio_entry,
)


@pytest.fixture
def settings() -> Settings:
    # install() writes a remote URL, so this fixture needs a concrete port.
    return Settings(credentials=credentials_value(), transport="http", port=8765)


def test_stdio_entry_runs_the_packaged_module() -> None:
    assert stdio_entry("python") == {
        "command": "python",
        "args": ["-m", "calabiyau_map_engine_mcp", "--transport", "stdio"],
    }


def test_remote_entries_point_at_the_matching_endpoint(settings: Settings) -> None:
    http = remote_entry(settings, "http")
    assert http["type"] == "http"
    assert http["url"] == "http://127.0.0.1:8765/mcp"
    assert http["headers"]["Authorization"] == f"Bearer {settings.credentials.http_token}"

    sse = remote_entry(settings, "sse")
    assert sse["type"] == "sse"
    assert sse["url"] == "http://127.0.0.1:8765/sse"


def test_entry_for_only_uses_remote_entries_for_network_transports(settings: Settings) -> None:
    assert "command" in entry_for(settings, "stdio", "python")
    assert entry_for(settings, "all", "python")["command"] == "python"
    assert entry_for(settings, "http", "python")["url"].endswith("/mcp")


def test_merge_keeps_other_servers_and_writes_a_backup(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "other-server"}}}), encoding="utf-8"
    )

    message = merge(Client("Cursor", path, "mcpServers"), stdio_entry("python"))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["other"] == {"command": "other-server"}
    assert data["mcpServers"][SERVER_KEY]["command"] == "python"
    assert len(list(tmp_path.glob("mcp.json.bak-*"))) == 1
    assert "backup" in message


def test_merge_creates_the_file_when_the_client_has_none(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "mcp.json"
    merge(Client("Cursor", path, "mcpServers"), stdio_entry("python"))
    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"][SERVER_KEY]


def test_merge_skips_a_file_that_is_not_json(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text("this is not json", encoding="utf-8")

    message = merge(Client("Cursor", path, "mcpServers"), stdio_entry("python"))

    assert "skipped" in message
    assert path.read_text(encoding="utf-8") == "this is not json"


def test_merge_skips_a_file_that_is_not_an_object(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    message = merge(Client("Cursor", path, "mcpServers"), stdio_entry("python"))

    assert "skipped" in message
    assert path.read_text(encoding="utf-8") == "[1, 2, 3]"


def test_merge_adds_the_stdio_type_only_where_the_client_needs_it(tmp_path: Path) -> None:
    cursor = tmp_path / "cursor.json"
    merge(Client("Cursor", cursor, "mcpServers"), stdio_entry("python"))
    assert "type" not in json.loads(cursor.read_text(encoding="utf-8"))["mcpServers"][SERVER_KEY]

    code = tmp_path / "code.json"
    merge(Client("VS Code", code, "servers", stdio_type="stdio"), stdio_entry("python"))
    servers = json.loads(code.read_text(encoding="utf-8"))["servers"]
    assert servers[SERVER_KEY]["type"] == "stdio"


def test_detected_requires_an_existing_directory(tmp_path: Path) -> None:
    (tmp_path / "present").mkdir()
    present = Client("Present", tmp_path / "present" / "mcp.json", "mcpServers")
    absent = Client("Absent", tmp_path / "absent" / "mcp.json", "mcpServers")
    assert detected([present, absent]) == [present]


def test_detected_honours_a_client_that_must_not_be_created(tmp_path: Path) -> None:
    (tmp_path / "code").mkdir()
    never_created = Client("VS Code", tmp_path / "code" / "mcp.json", "servers", create=False)
    assert detected([never_created]) == []

    (tmp_path / "code" / "mcp.json").write_text("{}", encoding="utf-8")
    assert detected([never_created]) == [never_created]


def test_install_without_apply_reports_targets_and_writes_nothing(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Client("Cursor", tmp_path / "mcp.json", "mcpServers")
    monkeypatch.setattr(
        "calabiyau_map_engine_mcp.install.client_targets", lambda: [target]
    )

    message = install(settings, apply=False, transport="http", command="python")

    assert "nothing written" in message
    assert settings.credentials.pairing_token in message
    assert settings.credentials.http_token in message
    assert not (tmp_path / "mcp.json").exists()


def test_install_with_apply_rewrites_the_detected_client(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Client("Cursor", tmp_path / "mcp.json", "mcpServers")
    monkeypatch.setattr(
        "calabiyau_map_engine_mcp.install.client_targets", lambda: [target]
    )

    message = install(settings, apply=True, transport="stdio", command="python")

    assert "wrote" in message
    data = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"][SERVER_KEY]["command"] == "python"
