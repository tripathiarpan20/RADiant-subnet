import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from radiant_sdk import cli
from radiant_sdk.checks import run_static_checks
from radiant_sdk.harness import RESULT_PREFIX, check_output, warmup_images
from radiant_sdk.packaging import prepare
from radiant_sdk.validate import check_receipt, fetch_campaign, manifest_hash, write_receipt

REPO = Path(__file__).resolve().parents[2]

NUMPY_MODEL = '''
import time
import numpy as np
from radiant_sdk.models.v1 import ChestXRayClassifier

class Tiny(ChestXRayClassifier):
    def load_model(self, weights_dir, device):
        {load}
    def load_model_checkpoint(self, path):
        pass
    def run_single_evaluation(self, image):
        return self.run_batched_evaluation([image])[0]
    def run_batched_evaluation(self, images):
        return [float(img.mean()) / 4095.0 for img in images]
    def map_to_labels(self, raw):
        {mapping}
'''


def run_harness(tmp_path, load="pass", mapping="return {l: min(1.0, raw) for l in self.labels}", timeout=30):
    sol = tmp_path / "solution"
    sol.mkdir(exist_ok=True)
    (sol / "model.py").write_text(NUMPY_MODEL.format(load=load, mapping=mapping))
    out = subprocess.run(
        [sys.executable, "-m", "radiant_sdk.harness", "qualify", "--solution", str(sol), "--weights", str(tmp_path),
         "--labels", "tuberculosis,pneumonia", "--batch-size", "3", "--load-timeout", str(timeout)],
        cwd=REPO, capture_output=True, text=True, timeout=120)
    line = [ln for ln in out.stdout.splitlines() if ln.startswith(RESULT_PREFIX)][-1]
    return json.loads(line[len(RESULT_PREFIX):])


def test_harness_qualifies_a_well_behaved_model(tmp_path):
    r = run_harness(tmp_path)
    assert r["status"] == "passed" and r["images"] == 8 and set(r["example_output"]) == {"tuberculosis", "pneumonia"}


@pytest.mark.parametrize("kw,code", [
    ({"mapping": "return {'tuberculosis': 0.5}"}, "BAD_OUTPUT"),
    ({"mapping": "return {l: 1.5 for l in self.labels}"}, "BAD_OUTPUT"),
    ({"mapping": "return {l: float('nan') for l in self.labels}"}, "BAD_OUTPUT"),
    ({"load": "raise RuntimeError('no weights')"}, "LOAD_CRASH"),
    ({"load": "time.sleep(10)", "timeout": 1}, "LOAD_TIMEOUT"),
    ({"load": "import nonexistent_module_xyz"}, "LOAD_CRASH"),
])
def test_harness_failure_codes(tmp_path, kw, code):
    r = run_harness(tmp_path, **kw)
    assert r["status"] == "failed" and r["reason_code"] == code


def test_import_time_work_counts_against_the_load_timeout(tmp_path):
    sol = tmp_path / "solution"
    sol.mkdir()
    (sol / "model.py").write_text("import time\ntime.sleep(10)\n" + NUMPY_MODEL.format(
        load="pass", mapping="return {l: 0.5 for l in self.labels}"))
    out = subprocess.run([sys.executable, "-m", "radiant_sdk.harness", "qualify", "--solution", str(sol),
                          "--labels", "a", "--batch-size", "1", "--load-timeout", "1"],
                         cwd=REPO, capture_output=True, text=True, timeout=60)
    assert '"reason_code": "LOAD_TIMEOUT"' in out.stdout


def test_check_output_rounds_and_rejects():
    assert check_output({"a": 0.12345}, ("a",), 3) == {"a": 0.123}
    for bad in ({"a": True}, {"a": "0.5"}, {"a": 0.5, "b": 0.5}, [0.5]):
        with pytest.raises(ValueError):
            check_output(bad, ("a",), 3)


def test_warmup_set_is_deterministic_uint16():
    a, b = warmup_images(2), warmup_images(2)
    assert all(x.dtype.name == "uint16" and x.ndim == 2 for x in a)
    assert all((x == y).all() for x, y in zip(a, b))


def _validated(miner_dir, tmp_path, campaign_file):
    p = prepare(miner_dir, tmp_path / "build")
    zp = Path(p.zip_path)
    campaign = fetch_campaign("testnet-cxr-01", manifest_file=campaign_file)
    report = run_static_checks(zp.read_bytes(), campaign["manifest"])
    write_receipt(zp, report, campaign, {"repo": "r", "revision": "x"}, {"status": "passed"})
    return zp, campaign


def test_receipt_matches_only_the_same_zip_campaign_and_age(miner_dir, tmp_path, campaign_file):
    zp, campaign = _validated(miner_dir, tmp_path, campaign_file)
    assert check_receipt(zp, "testnet-cxr-01", campaign["manifest_hash"])["sha256"].startswith("sha256:")
    with pytest.raises(ValueError, match="campaign"):
        check_receipt(zp, "other-campaign", campaign["manifest_hash"])
    with pytest.raises(ValueError, match="manifest changed"):
        check_receipt(zp, "testnet-cxr-01", "sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="24 hours"):
        check_receipt(zp, "testnet-cxr-01", None, now=datetime.now(timezone.utc) + timedelta(hours=25))
    zp.write_bytes(zp.read_bytes() + b"\0")
    with pytest.raises(ValueError, match="zip changed"):
        check_receipt(zp, "testnet-cxr-01", None)


def test_submit_refuses_without_a_receipt(miner_dir, tmp_path, campaign_file, capsys):
    p = prepare(miner_dir, tmp_path / "build")
    assert cli.main(["submit", p.zip_path, "--campaign", "testnet-cxr-01", "--manifest", str(campaign_file)]) == 2
    assert "refused" in capsys.readouterr().err


def test_submit_with_a_fresh_receipt_stops_before_sending_anything(miner_dir, tmp_path, campaign_file, capsys):
    zp, _ = _validated(miner_dir, tmp_path, campaign_file)
    assert cli.main(["submit", str(zp), "--campaign", "testnet-cxr-01", "--manifest", str(campaign_file)]) == 3
    assert "nothing was sent" in capsys.readouterr().err


def test_fetch_campaign_rejects_a_tampered_manifest(campaign_file):
    body = json.loads(campaign_file.read_text())
    body["manifest"]["limits"]["model_load_timeout_s"] = 9999
    campaign_file.write_text(json.dumps(body))
    with pytest.raises(ValueError, match="does not hash"):
        fetch_campaign("testnet-cxr-01", manifest_file=campaign_file)


def test_sdk_hash_matches_validator_hash(manifest):
    from campaign.manifest import compute_manifest_hash

    assert manifest_hash(manifest) == compute_manifest_hash(manifest)


def test_validate_static_only_cli(miner_dir, tmp_path, campaign_file, capsys):
    p = prepare(miner_dir, tmp_path / "build")
    code = cli.main(["validate", p.zip_path, "--campaign", "testnet-cxr-01", "--manifest", str(campaign_file),
                     "--static-only"])
    assert code == 0 and '"static_passed"' in capsys.readouterr().out
