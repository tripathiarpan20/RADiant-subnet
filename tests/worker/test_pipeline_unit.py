"""Non-DB unit tests for worker/pipeline.py helpers and worker/admin.py."""

from __future__ import annotations

from pathlib import Path

from worker.pipeline import _cached_weights_dir, weights_digest


def test_cached_weights_dir_matches_digest_and_rejects_corrupt(tmp_path: Path):
    w = tmp_path / "hf" / "owner--repo" / ("a" * 40)
    w.mkdir(parents=True)
    (w / "model.safetensors").write_bytes(b"weights")
    (w / "config.json").write_text("{}")
    digest = weights_digest(w)

    assert _cached_weights_dir(tmp_path, digest) == w
    assert _cached_weights_dir(tmp_path, "sha256:" + "0" * 64) is None
    assert _cached_weights_dir(tmp_path, None) is None
