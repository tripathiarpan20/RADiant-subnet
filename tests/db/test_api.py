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


def test_submission_logs_route(client, db_url):
    from campaign import store
    from chain.commitment import SolutionCommitment
    from db.connection import connect
    from worker.pipeline import save_log

    sha = "sha256:" + "a" * 64
    assert client.get("/v1/submissions/not-a-hash/logs").status_code == 400
    assert client.get(f"/v1/submissions/{sha}/logs").status_code == 404

    com = SolutionCommitment(uid=1, hotkey="5GZ9S5qgBrHCMr3kdvQ8hXdpZqz9D4eW7xvYxpRQHoVUuGKi", coldkey="",
                             commit_block=10, campaign_id="testnet-cxr-01", sha256=sha, code_sha256=sha,
                             object_key="radiant/private/campaigns/testnet-cxr-01/solutions/"
                                        "5GZ9S5qgBrHCMr3kdvQ8hXdpZqz9D4eW7xvYxpRQHoVUuGKi/"
                                        "00000000-0000-0000-0000-000000000001.zip", raw="r")
    with connect(db_url) as conn:
        sid = store.insert_submission(conn, com, size_bytes=123, fee_exempt=True)
        save_log(conn, sid, "build", "pip ok hf_secretshouldberedacted123")
        save_log(conn, sid, "qualification", "load 1.2s")

    res = client.get(f"/v1/submissions/{sha}/logs")
    assert res.status_code == 200
    logs = {item["kind"]: item["body"] for item in res.json()["logs"]}
    assert set(logs) == {"build", "qualification"}
    assert "hf_secret" not in logs["build"] and "[REDACTED]" in logs["build"]
