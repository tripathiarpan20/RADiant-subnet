"""Presigned solution uploads and bounded fetches (Pareton storage/s3.py, for zips).

The API presigns a PUT that pins the zip's sha256 (x-amz-checksum-sha256) and
If-None-Match: *, under a key that names the campaign and hotkey. The watcher
and worker fetch with bucket credentials, bounded by the campaign's zip cap.
Works against MinIO, Hippius or Backblaze through the endpoint URL.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass

import config
from chain.commitment import parse_object_key


@dataclass(frozen=True)
class Presigned:
    upload_url: str
    object_key: str
    expires_in: int
    required_headers: dict[str, str]
    already_uploaded: bool = False


def client(s: config.Settings, *, presign: bool = False, timeout_s: float | None = None):
    import boto3
    from botocore.config import Config

    endpoint = s.s3_presign_endpoint_url if presign else s.s3_endpoint_url
    if not (endpoint and s.s3_access_key and s.s3_secret_key):
        raise RuntimeError("RADIANT_S3_ENDPOINT_URL, RADIANT_S3_ACCESS_KEY and RADIANT_S3_SECRET_KEY must be set")
    extra = {"connect_timeout": timeout_s, "read_timeout": timeout_s,
             "retries": {"total_max_attempts": 2}} if timeout_s else {}
    return boto3.client(
        "s3", endpoint_url=endpoint, region_name=s.s3_region, aws_access_key_id=s.s3_access_key,
        aws_secret_access_key=s.s3_secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      request_checksum_calculation="when_required", response_checksum_validation="when_required",
                      **extra))


def object_key_for(s: config.Settings, campaign_id: str, hotkey: str, upload_id: str) -> str:
    key = f"{s.s3_prefix}/private/campaigns/{campaign_id}/solutions/{hotkey}/{uuid.UUID(upload_id)}.zip"
    if parse_object_key(key) is None:
        raise ValueError("cannot build a valid object key from these fields")
    return key


def checksum_b64(sha256: str) -> str:
    return base64.b64encode(bytes.fromhex(sha256.removeprefix("sha256:"))).decode()


def presign_upload(s: config.Settings, *, campaign_id: str, hotkey: str, sha256: str, size_bytes: int,
                   upload_id: str, expires_in: int) -> Presigned:
    from botocore.exceptions import ClientError

    key = object_key_for(s, campaign_id, hotkey, upload_id)
    checksum = checksum_b64(sha256)
    headers = {"Content-Type": "application/zip", "x-amz-checksum-sha256": checksum, "If-None-Match": "*",
               "Content-Length": str(size_bytes)}
    try:  # a retry may land after a successful PUT whose response was lost
        head = client(s, timeout_s=15).head_object(Bucket=s.s3_bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("403", "404", "NoSuchKey", "AccessDenied", "NotFound"):
            raise
    else:
        if head.get("ContentLength") != size_bytes:
            raise ValueError("upload UUID already holds a different object")
        return Presigned("", key, expires_in, headers, already_uploaded=True)
    # Content-Length is signed too: without it a miner could declare a small zip
    # and PUT gigabytes whose checksum they also control.
    url = client(s, presign=True).generate_presigned_url(
        "put_object",
        Params={"Bucket": s.s3_bucket, "Key": key, "ContentType": "application/zip", "ChecksumSHA256": checksum,
                "IfNoneMatch": "*", "ContentLength": size_bytes},
        ExpiresIn=expires_in)
    return Presigned(url, key, expires_in, headers)


def head_size(s: config.Settings, key: str) -> int | None:
    from botocore.exceptions import ClientError

    try:
        return int(client(s, timeout_s=15).head_object(Bucket=s.s3_bucket, Key=key)["ContentLength"])
    except ClientError:
        return None


def fetch_bounded(s: config.Settings, key: str, max_bytes: int) -> bytes:
    body = client(s, timeout_s=30).get_object(Bucket=s.s3_bucket, Key=key)["Body"]
    data = body.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"object exceeds {max_bytes} bytes")
    return data


def put_object(s: config.Settings, key: str, data: bytes) -> None:
    import hashlib

    client(s, timeout_s=60).put_object(Bucket=s.s3_bucket, Key=key, Body=data, ContentType="application/zip",
                                       ChecksumSHA256=checksum_b64("sha256:" + hashlib.sha256(data).hexdigest()))


def delete_object(s: config.Settings, key: str) -> None:
    client(s, timeout_s=30).delete_object(Bucket=s.s3_bucket, Key=key)
