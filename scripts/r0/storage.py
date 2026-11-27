"""Hippius / Backblaze presigned PUT with checksum and If-None-Match.

RADiant adopts Pareton's upload path: the API presigns a PUT that pins
x-amz-checksum-sha256 (the zip's hash) and If-None-Match: * (no overwrite), and
the miner PUTs the solution zip to it. This probe checks that each S3-compatible
provider enforces both, using a throwaway key under r0-probe/ that it deletes.

Per provider, configured from the environment (PROVIDER = HIPPIUS or BACKBLAZE):
    R0_<PROVIDER>_S3_ENDPOINT   e.g. https://s3.hippius.com, https://s3.us-west-004.backblazeb2.com
    R0_<PROVIDER>_S3_REGION     e.g. decentralized, us-west-004
    R0_<PROVIDER>_S3_BUCKET
    R0_<PROVIDER>_S3_ACCESS_KEY
    R0_<PROVIDER>_S3_SECRET_KEY

Gates per provider:
    storage_<p>_presigned_put    a correct presigned PUT succeeds and HEAD shows the checksum
    storage_<p>_checksum         a body that doesn't match the signed checksum is rejected
    storage_<p>_if_none_match    a second PUT to the same key is rejected (412)
    storage_<p>_private          an anonymous GET of the object and of the bucket listing fails
    storage_<p>_content_length   a body larger than the signed Content-Length is rejected, so a
                                 miner can't upload more than the size it declared
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import uuid
from dataclasses import dataclass
from typing import Any

from .report import GateResult, GateStatus

PROVIDERS = ("hippius", "backblaze")


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--providers", default=",".join(PROVIDERS))
    p.add_argument("--expires", type=int, default=300)
    p.add_argument("--keep", action="store_true", help="don't delete the probe objects")


@dataclass(frozen=True)
class S3Target:
    provider: str
    endpoint: str
    region: str
    bucket: str
    access_key: str
    secret_key: str

    @classmethod
    def from_env(cls, provider: str) -> "S3Target | None":
        pre = f"R0_{provider.upper()}_S3_"
        vals = {k: os.getenv(pre + k.upper(), "") for k in ("endpoint", "region", "bucket", "access_key", "secret_key")}
        if not all(vals[k] for k in ("endpoint", "bucket", "access_key", "secret_key")):
            return None
        return cls(provider=provider, region=vals["region"] or "us-east-1", **{k: v for k, v in vals.items() if k != "region"})


def checksum_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def client_for(t: S3Target):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=t.endpoint,
        region_name=t.region,
        aws_access_key_id=t.access_key,
        aws_secret_access_key=t.secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            # boto3 >= 1.36 adds CRC checksums by default, which several
            # S3-compatible stores reject; only send what we ask for.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def presign_put(client, bucket: str, key: str, body: bytes, expires: int,
                content_length: int | None = None) -> tuple[str, dict[str, str]]:
    """Pareton's presign parameters, plus a signed Content-Length (as RADiant's storage/s3.py)."""
    checksum = checksum_b64(body)
    params = {"Bucket": bucket, "Key": key, "ContentType": "application/zip", "ChecksumSHA256": checksum,
              "IfNoneMatch": "*"}
    if content_length is not None:
        params["ContentLength"] = content_length
    url = client.generate_presigned_url("put_object", Params=params, ExpiresIn=expires)
    headers = {"Content-Type": "application/zip", "x-amz-checksum-sha256": checksum, "If-None-Match": "*"}
    return url, headers


def _put(url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
    import requests

    r = requests.put(url, data=body, headers=headers, timeout=60)
    return r.status_code, r.text[:300]


def probe_provider(t: S3Target, expires: int = 300, keep: bool = False, put=_put) -> list[GateResult]:
    p = t.provider
    client = client_for(t)
    run_id = uuid.uuid4().hex[:12]
    body = os.urandom(64 * 1024)
    keys: list[str] = []
    results: list[GateResult] = []
    common = {"endpoint": t.endpoint, "region": t.region, "bucket": t.bucket}

    try:
        # 1. correct PUT
        key = f"r0-probe/{run_id}/ok.zip"
        keys.append(key)
        url, headers = presign_put(client, t.bucket, key, body, expires)
        status, text = put(url, headers, body)
        head: dict[str, Any] = {}
        if 200 <= status < 300:
            try:
                h = client.head_object(Bucket=t.bucket, Key=key, ChecksumMode="ENABLED")
                head = {"content_length": h.get("ContentLength"), "checksum_sha256": h.get("ChecksumSHA256")}
            except Exception as exc:  # some stores don't support ChecksumMode
                head = {"head_error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        ok = 200 <= status < 300 and head.get("content_length") == len(body)
        stored_checksum = head.get("checksum_sha256")
        results.append(GateResult(
            f"storage_{p}_presigned_put", GateStatus.PASSED if ok else GateStatus.FAILED,
            f"PUT {status}; stored checksum {'matches' if stored_checksum == headers['x-amz-checksum-sha256'] else 'not reported'}",
            {**common, "put_status": status, "put_body": text if not ok else "", **head,
             "checksum_round_trips": stored_checksum == headers["x-amz-checksum-sha256"]}))

        # 1b. the bucket and the object are private
        import requests

        base = t.endpoint.rstrip("/")
        anon_obj = requests.get(f"{base}/{t.bucket}/{key}", timeout=20).status_code
        anon_list = requests.get(f"{base}/{t.bucket}?list-type=2", timeout=20).status_code
        private = anon_obj in (401, 403) and anon_list in (401, 403)
        results.append(GateResult(
            f"storage_{p}_private", GateStatus.PASSED if private else GateStatus.FAILED,
            f"anonymous GET of the object returned {anon_obj}, of the listing {anon_list} (want 401/403)",
            {**common, "anonymous_object_status": anon_obj, "anonymous_list_status": anon_list}))

        # 1c. a larger body than the signed Content-Length
        key5 = f"r0-probe/{run_id}/oversize.zip"
        keys.append(key5)
        big = body + os.urandom(1024)
        url5, headers5 = presign_put(client, t.bucket, key5, big, expires, content_length=len(body))
        status5, text5 = put(url5, headers5, big)
        results.append(GateResult(
            f"storage_{p}_content_length", GateStatus.PASSED if 400 <= status5 < 500 else GateStatus.FAILED,
            f"a body 1 KiB over the signed Content-Length returned {status5} (want 4xx)",
            {**common, "oversize_status": status5, "oversize_body": text5 if status5 < 400 else ""}))

        # 2. overwrite of the same key
        status2, text2 = put(url, headers, body)
        results.append(GateResult(
            f"storage_{p}_if_none_match", GateStatus.PASSED if status2 == 412 else GateStatus.FAILED,
            f"second PUT to the same key returned {status2} (want 412)",
            {**common, "second_put_status": status2, "second_put_body": text2}))

        # 3. body that doesn't match the signed checksum
        key3 = f"r0-probe/{run_id}/bad-digest.zip"
        keys.append(key3)
        url3, headers3 = presign_put(client, t.bucket, key3, body, expires)
        tampered = body[:-1] + bytes([body[-1] ^ 1])
        status3, text3 = put(url3, headers3, tampered)
        # A store that ignores the header but signs it would still reject a
        # missing header with 403; check that too.
        key4 = f"r0-probe/{run_id}/no-header.zip"
        keys.append(key4)
        url4, headers4 = presign_put(client, t.bucket, key4, body, expires)
        status4, text4 = put(url4, {k: v for k, v in headers4.items() if k != "x-amz-checksum-sha256"}, body)
        rejected = 400 <= status3 < 500
        results.append(GateResult(
            f"storage_{p}_checksum", GateStatus.PASSED if rejected and not (200 <= status4 < 300) else GateStatus.FAILED,
            f"tampered body returned {status3}; missing checksum header returned {status4} (want 4xx for both)",
            {**common, "tampered_status": status3, "tampered_body": text3,
             "missing_header_status": status4, "missing_header_body": text4}))
    except Exception as exc:
        done = {r.gate for r in results}
        for g in ("presigned_put", "private", "content_length", "if_none_match", "checksum"):
            name = f"storage_{p}_{g}"
            if name not in done:
                results.append(GateResult(name, GateStatus.FAILED, f"{type(exc).__name__}: {str(exc)[:200]}", common))
    finally:
        if not keep:
            for k in keys:
                try:
                    client.delete_object(Bucket=t.bucket, Key=k)
                except Exception:
                    pass
    return results


def run(args: argparse.Namespace) -> list[GateResult]:
    try:
        import boto3  # noqa: F401
        import requests  # noqa: F401
    except ImportError:
        return [GateResult("storage", GateStatus.BLOCKED, "boto3/requests not installed (uv sync --extra r0)")]
    results: list[GateResult] = []
    for provider in [x.strip() for x in args.providers.split(",") if x.strip()]:
        t = S3Target.from_env(provider)
        if t is None:
            results += [GateResult(f"storage_{provider}_{g}", GateStatus.BLOCKED,
                                   f"R0_{provider.upper()}_S3_* credentials not set")
                        for g in ("presigned_put", "private", "content_length", "checksum", "if_none_match")]
            continue
        results += probe_provider(t, args.expires, args.keep)
    return results
