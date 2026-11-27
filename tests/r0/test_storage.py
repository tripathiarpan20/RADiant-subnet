"""Runs the storage probe against moto's S3 server.

moto enforces If-None-Match but not the signed checksum, which makes it a
useful lax store: the probe must pass the first two gates and fail the third.
"""

import boto3
import pytest

from scripts.r0.report import GateStatus
from scripts.r0.storage import S3Target, checksum_b64, probe_provider, run

moto_server = pytest.importorskip("moto.server")


@pytest.fixture(scope="module")
def endpoint():
    s = moto_server.ThreadedMotoServer(port=0, verbose=False)
    s.start()
    host, port = s.get_host_and_port()
    ep = f"http://{host}:{port}"
    boto3.client("s3", endpoint_url=ep, region_name="us-east-1",
                 aws_access_key_id="a", aws_secret_access_key="b").create_bucket(Bucket="r0-bucket")
    yield ep
    s.stop()


def test_probe_against_lax_store(endpoint):
    t = S3Target("moto", endpoint, "us-east-1", "r0-bucket", "a", "b")
    res = {r.gate: r for r in probe_provider(t)}
    assert res["storage_moto_presigned_put"].status is GateStatus.PASSED
    assert res["storage_moto_if_none_match"].status is GateStatus.PASSED
    assert res["storage_moto_checksum"].status is GateStatus.FAILED
    # cleanup removed every probe object
    c = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                     aws_access_key_id="a", aws_secret_access_key="b")
    assert c.list_objects_v2(Bucket="r0-bucket", Prefix="r0-probe/").get("KeyCount", 0) == 0


def test_missing_credentials_block_each_gate(monkeypatch):
    for k in ("ENDPOINT", "BUCKET", "ACCESS_KEY", "SECRET_KEY"):
        monkeypatch.delenv(f"R0_HIPPIUS_S3_{k}", raising=False)

    class A:
        providers = "hippius"
        expires = 60
        keep = False

    res = run(A())
    assert len(res) == 3 and all(r.status is GateStatus.BLOCKED for r in res)


def test_checksum_is_base64_sha256():
    assert checksum_b64(b"") == "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="
