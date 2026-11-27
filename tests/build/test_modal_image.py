"""Unit tests for build/modal_image.py and secret redaction."""

from __future__ import annotations

import subprocess
import sys

import pytest

from build.modal_image import IMAGE_PREFIX, _child_env, _tail, build_image
from radiant_sdk import reasons as R
from radiant_sdk.redact import redact_text


def test_child_env_strips_worker_secrets():
    env = _child_env({
        "PATH": "/usr/bin",
        "MODAL_TOKEN_ID": "ak-1234567890",
        "MODAL_TOKEN_SECRET": "as-1234567890",
        "HF_PIN_TOKEN": "hf_secretsecretsecret",
        "RADIANT_S3_SECRET_KEY": "s3secret",
        "RADIANT_DATABASE_URL": "postgresql://radiant:pw@postgres:5432/radiant",
        "DATAHIVE_API_KEY": "dh-key",
    })
    assert env == {"PATH": "/usr/bin", "MODAL_TOKEN_ID": "ak-1234567890", "MODAL_TOKEN_SECRET": "as-1234567890"}


def test_redact_text_scrubs_credentials():
    raw = (
        "db=postgresql://radiant:s3cr3t@postgres:5432/radiant "
        "hf=hf_abcdefghijklmnopqrstuvwxyz "
        "modal=ak-1234567890ab as-1234567890ab "
        "hip=hip_1234567890 "
        "hdr=Bearer eyJhbGciOiJIUzI1NiJ9.payload "
        "sig=X-Amz-Signature=deadbeef0123"
    )
    out = redact_text(raw)
    for leaked in ("s3cr3t", "hf_abcdef", "ak-123456", "as-123456", "hip_123456", "eyJhbG", "deadbeef0123"):
        assert leaked not in out


def test_build_image_kills_timed_out_child_and_redacts_failure(tmp_path, monkeypatch):
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        assert "HF_PIN_TOKEN" not in kwargs["env"]
        return real_popen([sys.executable, "-c", "import time; time.sleep(30)"],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)

    monkeypatch.setenv("HF_PIN_TOKEN", "hf_topsecrettokentopsecret")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(R.SolutionRejected) as exc:
        build_image(tmp_path, tmp_path, timeout_s=0.3)
    assert exc.value.code == R.BUILD_TIMEOUT

    def failing_popen(cmd, **kwargs):
        return real_popen(
            [sys.executable, "-c", "print('err hf_topsecrettokentopsecret'); raise SystemExit(1)"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)

    monkeypatch.setattr(subprocess, "Popen", failing_popen)
    with pytest.raises(R.SolutionRejected) as exc2:
        build_image(tmp_path, tmp_path, timeout_s=10)
    assert exc2.value.code == R.BUILD_FAILED
    assert "hf_topsecret" not in exc2.value.detail

    def ok_popen(cmd, **kwargs):
        return real_popen(
            [sys.executable, "-c", f"print('step 1'); print('{IMAGE_PREFIX}im-abc123')"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)

    monkeypatch.setattr(subprocess, "Popen", ok_popen)
    built = build_image(tmp_path, tmp_path, timeout_s=10)
    assert built.image_id == "im-abc123" and "step 1" in built.log
    assert _tail("x" * 250_000).startswith("[earlier output trimmed]\n")

    from build.modal_image import INFRA_EXIT_CODE

    def infra_popen(cmd, **kwargs):
        return real_popen(
            [sys.executable, "-c", f"print('modal 503'); raise SystemExit({INFRA_EXIT_CODE})"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)

    monkeypatch.setattr(subprocess, "Popen", infra_popen)
    with pytest.raises(R.InfraUnavailable) as exc3:
        build_image(tmp_path, tmp_path, timeout_s=10)
    assert exc3.value.code == R.BUILD_INFRA_ERROR
