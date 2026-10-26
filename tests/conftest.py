import pytest
from harness import credentials_value

from calabiyau_map_engine_mcp.config import Settings


@pytest.fixture
def settings():
    return Settings(credentials=credentials_value(), transport="all")
