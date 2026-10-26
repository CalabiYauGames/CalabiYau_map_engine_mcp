import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from calabiyau_map_engine_mcp.server import create_runtime

BASE = "http://127.0.0.1:8765"
WS = "ws://127.0.0.1:8765/bridge"
ORIGIN = "https://klbqmap.miyo.wiki"


def test_health_is_minimal_and_cors_is_exact(settings):
    with TestClient(create_runtime(settings).app, base_url=BASE) as client:
        response = client.get("/healthz", headers={"Origin": ORIGIN})
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert "token" not in response.text
        assert response.headers["access-control-allow-origin"] == ORIGIN
        assert client.get("/healthz", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.get("/healthz", headers={"Host": "evil.example"}).status_code == 403
        assert client.get("/healthz", headers={"Origin": "null"}).status_code == 403
        preflight = client.options(
            "/healthz",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == ORIGIN


@pytest.mark.parametrize("path", ["/mcp", "/sse", "/messages/"])
def test_every_network_mcp_transport_needs_http_token(settings, path):
    with TestClient(create_runtime(settings).app, base_url=BASE) as client:
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_valid_browser_pairs_and_unregisters(settings):
    runtime = create_runtime(settings)
    with TestClient(runtime.app, base_url=BASE) as client:
        with client.websocket_connect(WS, headers={"Origin": ORIGIN}) as socket:
            socket.send_json(
                {
                    "version": 1,
                    "type": "hello",
                    "token": settings.credentials.pairing_token,
                    "session_id": "test-tab",
                    "label": "Test",
                }
            )
            assert socket.receive_json() == {
                "version": 1,
                "type": "welcome",
                "session_id": "test-tab",
            }
            assert runtime.registry.sessions()[0]["browser_session_id"] == "test-tab"
    assert runtime.registry.sessions() == []


def test_health_reports_the_transport_and_the_live_session_count(settings):
    runtime = create_runtime(settings)
    with TestClient(runtime.app, base_url=BASE) as client:
        body = client.get("/healthz").json()
        assert body["service"] == "calabiyau-map-engine-mcp"
        assert body["transport"] == settings.transport
        assert body["port"] == settings.port
        assert body["instances"] == 1
        assert body["connected_sessions"] == 0
        assert isinstance(body["uptime"], float) and body["uptime"] >= 0

        with client.websocket_connect(WS, headers={"Origin": ORIGIN}) as socket:
            socket.send_json(
                {
                    "version": 1,
                    "type": "hello",
                    "token": settings.credentials.pairing_token,
                    "session_id": "health-tab",
                    "label": "Health",
                }
            )
            socket.receive_json()
            assert client.get("/healthz").json()["connected_sessions"] == 1

        assert client.get("/healthz").json()["connected_sessions"] == 0


def test_a_bridge_frame_with_the_wrong_version_closes_the_connection(settings):
    runtime = create_runtime(settings)
    with TestClient(runtime.app, base_url=BASE) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(WS, headers={"Origin": ORIGIN}) as socket:
                socket.send_json(
                    {
                        "version": 1,
                        "type": "hello",
                        "token": settings.credentials.pairing_token,
                        "session_id": "version-tab",
                        "label": "Version",
                    }
                )
                socket.receive_json()
                socket.send_json({"version": 2, "type": "response", "id": "anything"})
                socket.receive_json()
        assert runtime.registry.sessions() == []


@pytest.mark.parametrize(
    "origin,token",
    [
        ("https://evil.example", "valid"),
        ("null", "valid"),
        ("", "valid"),
        (ORIGIN, "wrong"),
    ],
)
def test_rejected_browser_never_enters_registry(settings, origin, token):
    runtime = create_runtime(settings)
    with TestClient(runtime.app, base_url=BASE) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(WS, headers={"Origin": origin}) as socket:
                socket.send_json(
                    {
                        "version": 1,
                        "type": "hello",
                        "token": settings.credentials.pairing_token if token == "valid" else token,
                        "session_id": "test-tab",
                        "label": "Test",
                    }
                )
                socket.receive_json()
        assert runtime.registry.sessions() == []
