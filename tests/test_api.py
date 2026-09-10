from fastapi.testclient import TestClient

from synara.api.app import create_app


def test_seed_alerts_and_auth(settings):
    app = create_app(settings)
    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
    assert client.post("/api/seed").status_code == 401
    seeded = client.post("/api/seed", headers={"X-API-Key": "test-key"})
    assert seeded.status_code == 200
    body = seeded.json()
    assert body["sku_count"] == 50
    assert len(body["spiked_skus"]) == 5
    alerts = client.get("/api/alerts", headers={"X-API-Key": "test-key"})
    assert alerts.status_code == 200
    payload = alerts.json()
    assert "at_risk_margin_usd" in payload["totals"]
    assert "recoverable_margin_usd" in payload["totals"]
    assert payload["at_risk"]
    sku = payload["at_risk"][0]["sku"]
    po = client.post(
        "/api/replenish",
        headers={"X-API-Key": "test-key"},
        json={"sku": sku, "quantity": 25},
    )
    assert po.status_code == 200
    order = client.post(
        "/api/orders",
        headers={"X-API-Key": "test-key"},
        json={"sku": sku, "quantity": 1, "idempotency_key": "api-1"},
    )
    # spiked SKUs may already be at zero after seed+walk; 409 is acceptable
    assert order.status_code in (200, 409)
