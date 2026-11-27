"""run_qualification against a fake Modal: capacity waits vs a normal run."""

import json
import threading

import pytest

from radiant_sdk import reasons as R
from radiant_sdk.harness import LOADED_MARKER, RESULT_PREFIX, STARTED_MARKER
from radiant_sdk.validate import parse_result, run_qualification


class FakeStream:
    def __init__(self, chunks, block=None):
        self.chunks, self.block = chunks, block

    def __iter__(self):
        if self.block is not None:
            self.block.wait()  # a sandbox still queued for a GPU produces nothing
        yield from self.chunks

    def read(self):
        return ""


class FakeSandbox:
    def __init__(self, chunks, block=None):
        self.stdout, self.stderr = FakeStream(chunks, block), FakeStream([])
        self.terminated = False
        self._block = block

    def wait(self, raise_on_termination=False):
        pass

    def terminate(self):
        self.terminated = True
        if self._block is not None:
            self._block.set()


class FakeModal:
    def __init__(self, sb):
        self.sb = sb
        self.Sandbox = self

    def create(self, *a, **kw):
        assert kw["block_network"] is True and kw["secrets"] == []
        return self.sb


DESCRIPTOR = {"gpu": "L4", "cpus": 4, "batch_size": 8}
PASSED = {"status": "passed", "load_total_s": 3.0, "single_s": 0.01, "batch_s": 0.1, "images": 8}


def test_no_gpu_within_the_wait_is_infra_not_a_rejection(manifest):
    sb = FakeSandbox([], block=threading.Event())
    with pytest.raises(R.InfraUnavailable) as e:
        run_qualification(FakeModal(sb), None, None, DESCRIPTOR, manifest, gpu_wait_s=0.5)
    assert e.value.code == R.GPU_UNAVAILABLE and sb.terminated


def test_started_marker_then_result(manifest):
    out = f"{STARTED_MARKER}\nminer says hi\n{RESULT_PREFIX}{json.dumps(PASSED)}\n"
    r = run_qualification(FakeModal(FakeSandbox([out])), None, None, DESCRIPTOR, manifest, gpu_wait_s=5)
    assert r["status"] == "passed" and "gpu_wait_s" in r


def test_harness_failure_is_a_rejection(manifest):
    bad = {"status": "failed", "reason_code": "LOAD_CRASH", "detail": "boom"}
    sb = FakeSandbox([f"{STARTED_MARKER}\n", f"{RESULT_PREFIX}{json.dumps(bad)}\n"])
    with pytest.raises(R.SolutionRejected) as e:
        run_qualification(FakeModal(sb), None, None, DESCRIPTOR, manifest, gpu_wait_s=5)
    assert e.value.code == "LOAD_CRASH"


class SequenceModal:
    """Each create() returns the next sandbox; a 'block' entry makes create() hang (as Modal's can)."""

    def __init__(self, items):
        self.items, self.created = list(items), []
        self.Sandbox = self

    def create(self, *a, **kw):
        item = self.items.pop(0)
        if item == "block":
            threading.Event().wait(5)
            item = FakeSandbox([], block=threading.Event())
        self.created.append(item)
        return item


def test_a_stuck_sandbox_is_replaced_by_a_fresh_request(manifest):
    out = f"{STARTED_MARKER}\n{RESULT_PREFIX}{json.dumps(PASSED)}\n"
    stuck, good = FakeSandbox([], block=threading.Event()), FakeSandbox([out])
    m = SequenceModal([stuck, good])
    r = run_qualification(m, None, None, DESCRIPTOR, manifest, gpu_wait_s=10, restart_after_s=0.3)
    assert r["status"] == "passed" and stuck.terminated and len(m.created) == 2


def test_a_blocking_create_is_abandoned_and_retried(manifest):
    out = f"{STARTED_MARKER}\n{RESULT_PREFIX}{json.dumps(PASSED)}\n"
    m = SequenceModal(["block", FakeSandbox([out])])
    r = run_qualification(m, None, None, DESCRIPTOR, manifest, gpu_wait_s=10, restart_after_s=0.3)
    assert r["status"] == "passed"


def test_parse_result_rejects_malformed_json():
    with pytest.raises(R.SolutionRejected) as e:
        parse_result(f"{RESULT_PREFIX}{{not json\n")
    assert e.value.code == R.SANDBOX_FAILED


def test_token_protocol_blocks_forged_result_and_missing_loaded_marker(manifest):
    tok = "tok999"
    # Forged un-prefixed result cannot pass when a token is required.
    forged = f"{tok} {STARTED_MARKER}\n{RESULT_PREFIX}{json.dumps(PASSED)}\n"
    sink: list[str] = []
    with pytest.raises(R.SolutionRejected) as e:
        run_qualification(FakeModal(FakeSandbox([forged])), None, None, DESCRIPTOR, manifest,
                          gpu_wait_s=5, sink=sink, echo=False, token=tok)
    assert e.value.code == R.SANDBOX_FAILED
    assert all(tok not in line for line in sink)

    # Skipping LOADED_MARKER (e.g. early exit) is rejected when token protocol is active.
    skipped_load = f"{tok} {STARTED_MARKER}\n{tok} {RESULT_PREFIX}{json.dumps(PASSED)}\n"
    with pytest.raises(R.SolutionRejected) as e:
        run_qualification(FakeModal(FakeSandbox([skipped_load])), None, None, DESCRIPTOR, manifest,
                          gpu_wait_s=5, echo=False, token=tok)
    assert e.value.code == R.SANDBOX_FAILED

    # Valid tokenized flow passes and records host_load_s.
    valid = f"{tok} {STARTED_MARKER}\n{tok} {LOADED_MARKER}\n{tok} {RESULT_PREFIX}{json.dumps(PASSED)}\n"
    r = run_qualification(FakeModal(FakeSandbox([valid])), None, None, DESCRIPTOR, manifest,
                          gpu_wait_s=5, echo=False, token=tok)
    assert r["status"] == "passed" and "host_load_s" in r


def test_host_enforces_load_timeout_even_if_sandbox_lies(manifest):
    import time as _time

    tight = {**manifest, "limits": {**manifest["limits"], "model_load_timeout_s": 0.2}}

    class SlowLoadStream(FakeStream):
        def __iter__(self):
            yield f"{STARTED_MARKER}\n"
            _time.sleep(0.35)
            yield f"{LOADED_MARKER}\n"
            yield f"{RESULT_PREFIX}{json.dumps({**PASSED, 'load_total_s': 0.01})}\n"

    sb = FakeSandbox([])
    sb.stdout = SlowLoadStream([])
    with pytest.raises(R.SolutionRejected) as e:
        run_qualification(FakeModal(sb), None, None, DESCRIPTOR, tight, gpu_wait_s=5, echo=False)
    assert e.value.code == R.LOAD_TIMEOUT
