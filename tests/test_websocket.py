import pytest
from starlette.testclient import TestClient
from app.main import app

def test_websocket_ticker_connection():
    client = TestClient(app)
    with client.websocket_connect("/ws/ticker") as websocket:
        data = websocket.receive_json()
        assert data["type"] == "CONNECTION_ESTABLISHED"
        assert "AMETHEUS" in data["message"]
        websocket.send_text("ping")
        assert websocket.receive_text() == "pong"
