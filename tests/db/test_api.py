import pytest
from fastapi.testclient import TestClient

from api.server import app
from campaign import seed
from db import migrate as mig

from .test_db import FIXTURE, FIXTURE_HASH


@pytest.fixture
def client(db_url):
    assert mig.main([]) == 0
    assert seed.main(["--manifest", str(FIXTURE)]) == 0
    return TestClient(app)


def test_health_info_and_campaigns(client):
    assert client.get("/health").json()["ok"] is True
    info = client.get("/v1/info").json()
    assert info == {"network": "test", "netuid": 292, "sdk_versions": ["1.0.0"], "fee_currency": "testTAO"}
    assert client.get("/v1/campaigns").json() == []  # drafts hidden by default
    listed = client.get("/v1/campaigns", params={"include_drafts": 1}).json()
    assert [c["manifest_hash"] for c in listed] == [FIXTURE_HASH]
    one = client.get("/v1/campaigns/testnet-cxr-01")
    assert one.headers["cache-control"] == "no-store"
    body = one.json()
    assert body["labels"] == ["fibrosis", "pneumonia", "silicosis", "tuberculosis"]
    assert body["submission_fee"] == {"amount_tao": "0.01",
                                      "recipient": "5GYfPfFRQPaLytjCyT78X769ZU9xm5tL43dN11sknTiZx34a"}
    assert client.get("/v1/campaigns/nope").status_code == 404
