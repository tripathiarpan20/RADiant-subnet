"""Redact credentials and sensitive URLs from logs and details shown to miners."""

from __future__ import annotations

import re

_SECRET_PATTERNS = re.compile(
    r"(?:"
    r"hf_[A-Za-z0-9]{10,}"
    r"|hip_[A-Za-z0-9]{8,}"
    r"|ak-[A-Za-z0-9]{8,}"
    r"|as-[A-Za-z0-9]{8,}"
    r"|AKIA[A-Z0-9]{12,}"
    r"|X-Amz-Signature=[0-9a-fA-F]+"
    r"|X-Amz-Credential=[^&\s]+"
    r"|(?<=<AWSAccessKeyId>)[^<]+"
    r"|(?<=<StringToSign>)[^<]+"
    r"|(?<=<CanonicalRequest>)[^<]+"
    r"|(?<=Bearer\s)[A-Za-z0-9._\-]{12,}"
    r")"
)
_PG_URL = re.compile(r"(postgres(?:ql)?://)[^\s:@]+:[^\s@]+@")


def redact_text(text: str) -> str:
    """Scrub tokens, S3 keys, Bearer auth and Postgres passwords from text."""
    if not text:
        return text
    text = _PG_URL.sub(r"\1[REDACTED]@", text)
    return _SECRET_PATTERNS.sub("[REDACTED]", text)
