import json

import pytest
from pydantic import ValidationError

from calabiyau_map_engine_mcp.config import Settings, load_credentials, normalize_origin


def test_credentials_are_stable_and_rotation_is_explicit(tmp_path):
    first = load_credentials(tmp_path)
    assert first.pairing_token != first.http_token
    assert load_credentials(tmp_path) == first
    second = load_credentials(tmp_path, rotate=True)
    assert second != first
    assert load_credentials(tmp_path) == second
    assert len(list(tmp_path.iterdir())) == 1


def test_invalid_credentials_are_not_silently_replaced(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"pairing_token": "bad"}), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_credentials(tmp_path)
    assert "bad" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "https://*.example.com",
        "https://example.com/path",
        "http://example.com",
        "file:///",
        "https://user:password@example.com",
        "https://example.com?x=1",
        "https://example.com#fragment",
    ],
)
def test_unsafe_origin_is_rejected(origin):
    with pytest.raises(ValueError):
        normalize_origin(origin)


def test_origin_is_exact_and_canonical(settings):
    assert normalize_origin("https://KLBQMAP.MIYO.WIKI:443/") == "https://klbqmap.miyo.wiki"
    assert normalize_origin("http://localhost:5173/") == "http://localhost:5173"
    assert "https://klbqmap.miyo.wiki" in settings.allowed_origins


@pytest.mark.parametrize(
    "kwargs", [{"port": 0}, {"request_timeout": 0}, {"transport": "websocket"}]
)
def test_settings_validate(kwargs, settings):
    with pytest.raises(ValueError):
        Settings(credentials=settings.credentials, **kwargs)
