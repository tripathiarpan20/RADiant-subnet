"""Unit tests for storage/s3.py presigned retry checksum check and error propagation."""

from __future__ import annotations

import hashlib
import uuid

import boto3
import pytest
import requests
from botocore.exceptions import ClientError

import config
from storage import s3

moto_server = pytest.importorskip("moto.server")

HOTKEY = "5GZ9S5qgBrHCMr3kdvQ8hXdpZqz9D4eW7xvYxpRQHoVUuGKi"


@pytest.fixture(scope="module")
def moto_s3():
    srv = moto_server.ThreadedMotoServer(port=0, verbose=False)
    srv.start()
    host, port = srv.get_host_and_port()
    yield f"http://{host}:{port}"
    srv.stop()


def _settings(endpoint: str) -> config.Settings:
    bucket = f"b-{uuid.uuid4().hex[:8]}"
    boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                 aws_access_key_id="a", aws_secret_access_key="b").create_bucket(Bucket=bucket)
    return config.Settings.from_env({
        "RADIANT_S3_ENDPOINT_URL": endpoint,
        "RADIANT_S3_ACCESS_KEY": "a",
        "RADIANT_S3_SECRET_KEY": "b",
        "RADIANT_S3_BUCKET": bucket,
    })


def test_presign_retry_rejects_same_size_different_checksum(moto_s3):
    s = _settings(moto_s3)
    uid = str(uuid.uuid4())
    data_a = b"aaaa1234"
    data_b = b"bbbb1234"  # exact same length, different sha256
    sha_a = "sha256:" + hashlib.sha256(data_a).hexdigest()
    sha_b = "sha256:" + hashlib.sha256(data_b).hexdigest()

    p = s3.presign_upload(s, campaign_id="testnet-cxr-01", hotkey=HOTKEY, sha256=sha_a,
                          size_bytes=len(data_a), upload_id=uid, expires_in=300)
    assert requests.put(p.upload_url, data=data_a, headers=p.required_headers, timeout=10).status_code == 200

    again = s3.presign_upload(s, campaign_id="testnet-cxr-01", hotkey=HOTKEY, sha256=sha_a,
                              size_bytes=len(data_a), upload_id=uid, expires_in=300)
    assert again.already_uploaded is True

    with pytest.raises(ValueError, match="different object"):
        s3.presign_upload(s, campaign_id="testnet-cxr-01", hotkey=HOTKEY, sha256=sha_b,
                          size_bytes=len(data_b), upload_id=uid, expires_in=300)


def test_presign_does_not_swallow_403_access_denied(moto_s3, monkeypatch):
    s = _settings(moto_s3)

    class BrokenClient:
        def head_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject")

    monkeypatch.setattr(s3, "client", lambda *a, **k: BrokenClient())
    with pytest.raises(ClientError):
        s3.presign_upload(s, campaign_id="testnet-cxr-01", hotkey=HOTKEY,
                          sha256="sha256:" + "a" * 64, size_bytes=10,
                          upload_id=str(uuid.uuid4()), expires_in=300)


def test_head_size_returns_none_only_for_404_and_raises_on_503(moto_s3, monkeypatch):
    s = _settings(moto_s3)
    assert s3.head_size(s, "does/not/exist.zip") is None

    class OutageClient:
        def head_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "503", "Message": "SlowDown"}}, "HeadObject")

    monkeypatch.setattr(s3, "client", lambda *a, **k: OutageClient())
    with pytest.raises(ClientError):
        s3.head_size(s, "any/key.zip")
