"""Environment-driven settings, in one place (SN10 style).

Every service reads its settings here. Values come from the process
environment (docker-compose passes each service only the variables it
needs), so nothing reads a .env file directly. ``settings()`` is re-read on
each call in tests via ``Settings.from_env(env)``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from None


@dataclass(frozen=True)
class Settings:
    # chain. Fees are paid in the TAO of this network: testTAO on "test".
    network: str
    netuid: int
    # Postgres
    database_url: str
    readonly_role: str
    readonly_password: str
    api_db_role: str
    api_db_password: str
    watcher_db_role: str
    watcher_db_password: str
    worker_db_role: str
    worker_db_password: str
    # solution-zip bucket: MinIO locally, or Hippius / Backblaze by endpoint URL
    s3_endpoint_url: str
    s3_region: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    s3_prefix: str
    # Endpoint written into presigned URLs: what miners can reach. For a
    # local MinIO that is the host-published address, not the compose name.
    s3_presign_endpoint_url: str
    upload_auth_ttl_s: int
    presign_expires_s: int
    uploads_per_hotkey_per_hour: int
    # chain intake (R3)
    poll_interval_s: int
    fee_exempt_hotkeys: frozenset[str]
    max_payment_age_blocks: int
    # DataHive (R5)
    datahive_api_base: str
    datahive_api_key: str
    max_rounds_per_24h: int
    # gate worker (R4)
    sandbox_gpu_wait_s: int
    infra_retry_after_s: int
    worker_scratch: str
    modal_environment: str
    # Hugging Face pins (R4; worker only)
    hf_pin_org: str
    hf_pin_token: str
    # API
    api_host: str
    api_port: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        s = cls(
            network=e.get("RADIANT_NETWORK", "test"),
            netuid=_int(e, "RADIANT_NETUID", 292 if e.get("RADIANT_NETWORK", "test") == "test" else 0),
            database_url=e.get("RADIANT_DATABASE_URL", ""),
            readonly_role=e.get("RADIANT_DB_READONLY_ROLE", "radiant_readonly"),
            readonly_password=e.get("RADIANT_DB_READONLY_PASSWORD", ""),
            api_db_role=e.get("RADIANT_DB_API_ROLE", "radiant_api"),
            api_db_password=e.get("RADIANT_DB_API_PASSWORD", ""),
            watcher_db_role=e.get("RADIANT_DB_WATCHER_ROLE", "radiant_watcher"),
            watcher_db_password=e.get("RADIANT_DB_WATCHER_PASSWORD", ""),
            worker_db_role=e.get("RADIANT_DB_WORKER_ROLE", "radiant_worker"),
            worker_db_password=e.get("RADIANT_DB_WORKER_PASSWORD", ""),
            s3_endpoint_url=e.get("RADIANT_S3_ENDPOINT_URL", ""),
            s3_region=e.get("RADIANT_S3_REGION", "us-east-1"),
            s3_bucket=e.get("RADIANT_S3_BUCKET", "radiant-solutions"),
            s3_access_key=e.get("RADIANT_S3_ACCESS_KEY", ""),
            s3_secret_key=e.get("RADIANT_S3_SECRET_KEY", ""),
            s3_prefix=e.get("RADIANT_S3_PREFIX", "radiant").strip("/"),
            s3_presign_endpoint_url=e.get("RADIANT_S3_PRESIGN_ENDPOINT_URL", "") or e.get("RADIANT_S3_ENDPOINT_URL", ""),
            upload_auth_ttl_s=_int(e, "RADIANT_UPLOAD_AUTH_TTL_S", 300),
            presign_expires_s=_int(e, "RADIANT_PRESIGN_EXPIRES_S", 600),
            uploads_per_hotkey_per_hour=_int(e, "RADIANT_UPLOADS_PER_HOTKEY_PER_HOUR", 10),
            poll_interval_s=_int(e, "RADIANT_POLL_INTERVAL_S", 30),
            fee_exempt_hotkeys=frozenset(h.strip() for h in e.get("RADIANT_FEE_EXEMPT_HOTKEYS", "").split(",")
                                         if h.strip()),
            max_payment_age_blocks=_int(e, "RADIANT_MAX_PAYMENT_AGE_BLOCKS", 7200),
            datahive_api_base=e.get("DATAHIVE_API_BASE", ""),
            datahive_api_key=e.get("DATAHIVE_API_KEY", ""),
            max_rounds_per_24h=_int(e, "MAX_ROUNDS_PER_24H", 3),
            sandbox_gpu_wait_s=_int(e, "SANDBOX_GPU_WAIT_S", 900),
            infra_retry_after_s=_int(e, "RADIANT_INFRA_RETRY_AFTER_S", 900),
            worker_scratch=e.get("RADIANT_WORKER_SCRATCH", "/tmp/radiant-worker"),
            modal_environment=e.get("RADIANT_MODAL_ENVIRONMENT", ""),
            hf_pin_org=e.get("HF_PIN_ORG", ""),
            hf_pin_token=e.get("HF_PIN_TOKEN", ""),
            api_host=e.get("RADIANT_API_HOST", "0.0.0.0"),
            api_port=_int(e, "RADIANT_API_PORT", 8000),
        )
        if s.network not in ("local", "test", "finney"):
            raise ValueError("RADIANT_NETWORK must be local, test or finney")
        if s.network != "local" and s.netuid <= 0:
            raise ValueError("RADIANT_NETUID must be set for test and finney (testnet RADiant is 292)")
        if s.max_rounds_per_24h < 1:
            raise ValueError("MAX_ROUNDS_PER_24H must be at least 1")
        if s.max_payment_age_blocks < 0:
            raise ValueError("RADIANT_MAX_PAYMENT_AGE_BLOCKS must be non-negative")
        return s

    def require_database(self) -> str:
        if not self.database_url:
            raise RuntimeError("RADIANT_DATABASE_URL is not set")
        return self.database_url


def settings() -> Settings:
    return Settings.from_env()
