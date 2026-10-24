from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from platformdirs import user_config_path
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_ORIGINS = (
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "https://klbqmap.miyo.wiki",
)


def normalize_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "*" in value
        or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"})
    ):
        raise ValueError("Origin must be an exact HTTPS origin or a loopback HTTP origin.")
    # Accessing port also validates malformed port numbers.
    port = parsed.port
    if port == (443 if parsed.scheme == "https" else 80):
        port = None
    host = parsed.hostname.lower()
    return f"{parsed.scheme}://{host}{':' + str(port) if port is not None else ''}"


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pairing_token: str = Field(min_length=32)
    http_token: str = Field(min_length=32)


def config_directory() -> Path:
    override = os.environ.get("CALABIYAU_MCP_CONFIG_DIR")
    return (
        Path(override) if override else user_config_path("CalabiYauMapEngineMCP", appauthor=False)
    )


def load_credentials(directory: Path, *, rotate: bool = False) -> Credentials:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "credentials.json"
    if path.exists() and not rotate:
        return Credentials.model_validate_json(path.read_text(encoding="utf-8"))
    credentials = Credentials(
        pairing_token=secrets.token_urlsafe(32),
        http_token=secrets.token_urlsafe(32),
    )
    if rotate:
        temporary = directory / f".credentials-{secrets.token_hex(8)}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as output:
                os.chmod(temporary, 0o600)
                output.write(credentials.model_dump_json(indent=2) + "\n")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        try:
            with path.open("x", encoding="utf-8") as output:
                os.chmod(path, 0o600)
                output.write(credentials.model_dump_json(indent=2) + "\n")
        except FileExistsError:
            return Credentials.model_validate_json(path.read_text(encoding="utf-8"))
    return credentials


@dataclass(frozen=True)
class Settings:
    credentials: Credentials
    transport: str = "stdio"
    port: int = 8765
    allowed_origins: tuple[str, ...] = DEFAULT_ORIGINS
    request_timeout: float = 30.0
    host: str = field(default="127.0.0.1", init=False)

    def __post_init__(self) -> None:
        if self.transport not in {"stdio", "sse", "streamable-http", "all"}:
            raise ValueError("Unsupported transport.")
        if not 1 <= self.port <= 65535:
            raise ValueError("Port must be between 1 and 65535.")
        if not 0 < self.request_timeout <= 300:
            raise ValueError("Request timeout must be greater than 0 and at most 300 seconds.")
        object.__setattr__(
            self,
            "allowed_origins",
            tuple(dict.fromkeys(map(normalize_origin, self.allowed_origins))),
        )

    def pairing_details(self) -> dict[str, object]:
        base = f"http://{self.host}:{self.port}"
        return {
            "bridge_url": f"ws://{self.host}:{self.port}/bridge",
            "mcp_url": f"{base}/mcp",
            "sse_url": f"{base}/sse",
            **self.credentials.model_dump(),
            "allowed_origins": list(self.allowed_origins),
        }
