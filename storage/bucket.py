"""Create the private solution bucket if it is missing.

    python -m storage.bucket

Talks plain S3 to RADIANT_S3_ENDPOINT_URL, so it works the same against the
local MinIO, Hippius or Backblaze. The bucket is private: there is no public
policy, and reveals (R7) publish through presigned or copied objects only.
R3 adds storage/s3.py (presigned PUT with checksum and If-None-Match).
"""

from __future__ import annotations

import sys

import config


def client(s: config.Settings):
    import boto3
    from botocore.config import Config

    if not (s.s3_endpoint_url and s.s3_access_key and s.s3_secret_key):
        raise RuntimeError("RADIANT_S3_ENDPOINT_URL, RADIANT_S3_ACCESS_KEY and RADIANT_S3_SECRET_KEY must be set")
    return boto3.client(
        "s3", endpoint_url=s.s3_endpoint_url, region_name=s.s3_region,
        aws_access_key_id=s.s3_access_key, aws_secret_access_key=s.s3_secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      request_checksum_calculation="when_required", response_checksum_validation="when_required",
                      retries={"total_max_attempts": 10, "mode": "standard"}),
    )


def ensure_bucket(s: config.Settings) -> bool:
    """True if the bucket was created, False if it already existed."""
    from botocore.exceptions import ClientError

    c = client(s)
    try:
        c.head_bucket(Bucket=s.s3_bucket)
        return False
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket", "NotFound"):
            raise
    c.create_bucket(Bucket=s.s3_bucket)
    return True


def main() -> int:
    s = config.settings()
    created = ensure_bucket(s)
    print(f"bucket {s.s3_bucket} {'created' if created else 'exists'} at {s.s3_endpoint_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
