"""Regression: the pre-existing public contract remains unchanged."""

from fastapi.testclient import TestClient

from main import app


def test_root_endpoint_contract_unchanged():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert response.json() == {"Hello": "World"}
