import pytest

from calabiyau_map_engine_mcp.config import Credentials, Settings


@pytest.fixture
def settings():
    return Settings(
        credentials=Credentials(pairing_token="b" * 43, http_token="h" * 43),
        transport="all",
    )
