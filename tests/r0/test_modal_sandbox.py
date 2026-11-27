import json

import pytest

from scripts.r0.modal_sandbox import gpu_gates, parse_remote
from scripts.r0.report import GateStatus

GOOD = {
    "direct_ip_blocked": "OSError", "dns_blocked": "gaierror", "https_blocked": "URLError",
    "network_blocked": True, "credentials_absent": True,
    "nvidia_smi": "NVIDIA L40S, 46068 MiB, 550.54", "torch_gpu": "NVIDIA L40S",
    "cuda": True, "load_model_s": 2.1, "single_s": 0.004, "batch64_s": 0.05, "output_shape": [64, 18],
    "labels": ["Atelectasis"] * 18,
}


def test_parse_remote_takes_last_result_line():
    out = "noise\nR0RESULT {\"a\": 1}\nmore\nR0RESULT " + json.dumps({"a": 2}) + "\nstderr tail"
    assert parse_remote(out) == {"a": 2}
    with pytest.raises(ValueError):
        parse_remote("nothing")


def test_all_gates_pass_on_good_output():
    res = gpu_gates("L40S", GOOD, 30.0, 120)
    assert [r.gate for r in res] == ["modal_l40s_offline", "modal_l40s_gpu_observed", "modal_l40s_model_load"]
    assert all(r.status is GateStatus.PASSED for r in res)
    assert res[0].details["approx_cost_usd"] > 0


def test_l4_request_is_not_satisfied_by_l40s():
    res = {r.gate: r for r in gpu_gates("L4", GOOD, 30.0, 120)}
    assert res["modal_l4_gpu_observed"].status is GateStatus.FAILED


def test_network_leak_or_slow_load_fails():
    leaky = {**GOOD, "network_blocked": False}
    assert gpu_gates("L40S", leaky, 1, 120)[0].status is GateStatus.FAILED
    slow = {**GOOD, "load_model_s": 200.0}
    assert gpu_gates("L40S", slow, 1, 120)[2].status is GateStatus.FAILED
