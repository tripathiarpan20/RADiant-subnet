"""run_qualification against a fake Modal: capacity waits vs a normal run."""

import json
import threading

import pytest

from radiant_sdk import reasons as R
from radiant_sdk.harness import RESULT_PREFIX, STARTED_MARKER
from radiant_sdk.validate import run_qualification


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
