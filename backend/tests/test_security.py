from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app


def test_bot_control_requires_token_when_configured() -> None:
    from app.api import security
    from app.core import settings as settings_module

    settings_module.get_settings.cache_clear()
    cfg = settings_module.get_settings()
    cfg.api_auth_token = "secret-token"
    security.settings.api_auth_token = "secret-token"

    with TestClient(app) as client:
        unauth_resp = client.post("/api/v1/bot/control", json={"command": "pause"})
        auth_resp = client.post(
            "/api/v1/bot/control",
            json={"command": "pause"},
            headers={"x-api-token": "secret-token"},
        )

    assert unauth_resp.status_code == 401
    assert auth_resp.status_code == 200
    cfg.api_auth_token = None
    security.settings.api_auth_token = None


def test_ws_requires_token_when_configured() -> None:
    from app.api import security
    from app.core import settings as settings_module

    settings_module.get_settings.cache_clear()
    cfg = settings_module.get_settings()
    cfg.live_ws_token = "ws-secret"
    security.settings.live_ws_token = "ws-secret"

    with TestClient(app) as client:
        try:
            with client.websocket_connect("/ws/live"):
                pass
            assert False, "expected websocket disconnect"
        except WebSocketDisconnect as exc:
            msg_code = exc.code

        with client.websocket_connect("/ws/live?token=ws-secret") as ws:
            data = ws.receive_json()

    assert msg_code == 1008
    assert data["type"] == "bootstrap"
    cfg.live_ws_token = None
    security.settings.live_ws_token = None
