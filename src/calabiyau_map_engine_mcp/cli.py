from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .config import DEFAULT_ORIGINS, Settings, config_directory, load_credentials


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Local CalabiYau Map Engine MCP/browser bridge.")
    command.add_argument(
        "command", nargs="?", choices=["serve", "pair", "reset-pairing"], default="serve"
    )
    command.add_argument(
        "--transport", choices=["stdio", "sse", "streamable-http", "all"], default="stdio"
    )
    command.add_argument("--port", type=int, default=8765)
    command.add_argument("--allowed-origin", action="append", default=[])
    command.add_argument("--config-dir", type=Path, default=None)
    command.add_argument("--request-timeout", type=float, default=30)
    command.add_argument(
        "--log-level", choices=["debug", "info", "warning", "error"], default="info"
    )
    return command


def main() -> None:
    arguments = parser().parse_args()
    logging.basicConfig(
        stream=sys.stderr,
        level=arguments.log_level.upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        credentials = load_credentials(
            arguments.config_dir or config_directory(), rotate=arguments.command == "reset-pairing"
        )
        settings = Settings(
            credentials=credentials,
            transport=arguments.transport,
            port=arguments.port,
            allowed_origins=(*DEFAULT_ORIGINS, *arguments.allowed_origin),
            request_timeout=arguments.request_timeout,
        )
        if arguments.command in {"pair", "reset-pairing"}:
            print(json.dumps(settings.pairing_details(), indent=2, ensure_ascii=False))
            return
        from .server import serve

        asyncio.run(serve(settings, log_level=arguments.log_level))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError) as exc:
        # Config validation errors can include a submitted credential; never log their contents.
        logging.getLogger(__name__).error(
            "Startup failed (%s). Check the port, origin configuration and credentials file.",
            type(exc).__name__,
        )
        raise SystemExit(1) from None
