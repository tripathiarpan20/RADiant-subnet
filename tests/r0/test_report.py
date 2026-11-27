import json

from scripts.r0.report import GateResult, GateStatus, exit_gate_met, write_results


def test_exit_gate_needs_every_gate_passed_or_accepted():
    ok = GateResult("a", GateStatus.PASSED, "")
    acc = GateResult("b", GateStatus.ACCEPTED, "")
    pend = GateResult("c", GateStatus.PENDING, "")
    assert exit_gate_met([ok, acc])
    assert not exit_gate_met([ok, pend])
    assert not exit_gate_met([GateResult("d", GateStatus.BLOCKED, "")])


def test_secrets_are_redacted_by_key_and_by_value(tmp_path):
    r = GateResult(
        "g",
        GateStatus.PASSED,
        "s",
        {
            "hf_token": "hf_abcdefghijklmnopqrstuvwxyz",
            "token_present": True,
            "url": "https://x/b/k?X-Amz-Credential=AKIA/2026&X-Amz-Signature=deadbeef",
            "nested": [{"api_key": "abc"}, "uses hf_abcdefghijklmnopqrstuvwxyz0 here"],
        },
    )
    path = write_results("t", [r], out_dir=tmp_path)
    text = path.read_text()
    assert "hf_abcdefghijklmnopqrstuvwxyz" not in text
    assert "deadbeef" not in text and "AKIA" not in text
    doc = json.loads(text)
    d = doc["gates"][0]["details"]
    assert d["hf_token"] == "[REDACTED]"
    assert d["token_present"] is True
    assert d["nested"][0]["api_key"] == "[REDACTED]"
    assert doc["exit_gate_met"] is True


def test_merge_keeps_gates_a_partial_rerun_did_not_produce(tmp_path):
    from scripts.r0.report import merge_previous

    write_results("m", [GateResult("a", GateStatus.PASSED, "old a"), GateResult("b", GateStatus.FAILED, "old b")],
                  out_dir=tmp_path)
    merged = merge_previous("m", [GateResult("b", GateStatus.PASSED, "new b")], out_dir=tmp_path)
    assert [(r.gate, r.summary) for r in merged] == [("a", "old a"), ("b", "new b")]


def test_s3_error_bodies_lose_access_keys(tmp_path):
    body = ("<Error><Code>SignatureDoesNotMatch</Code><AWSAccessKeyId>hip_abcdefghij1234</AWSAccessKeyId>"
            "<StringToSign>AWS4-HMAC-SHA256\n20261127T000000Z</StringToSign></Error>")
    text = write_results("e", [GateResult("g", GateStatus.FAILED, "s", {"missing_header_body": body})],
                         out_dir=tmp_path).read_text()
    assert "hip_abcdefghij1234" not in text and "AWS4-HMAC-SHA256\\n2026" not in text
    assert "SignatureDoesNotMatch" in text
