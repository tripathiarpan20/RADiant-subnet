import pytest

from campaign.fees import append_fee, fee_at_block, genesis_history, submission_fee_rao, validate_fee_history, \
    validate_submission_fee
from campaign.visibility import reveal_delay_for, validate_visibility


def test_fee_canonical_form_and_rao():
    assert validate_submission_fee({"amount_tao": "0.0100", "recipient": " 5Dest "}) == \
        {"amount_tao": "0.01", "recipient": "5Dest"}
    assert validate_submission_fee({"amount_tao": 0, "recipient": "5D"})["amount_tao"] == "0"
    assert submission_fee_rao({"amount_tao": "0.01", "recipient": "5D"}) == 10_000_000


@pytest.mark.parametrize("fee", [
    {"amount_tao": 0.01, "recipient": "5D"},           # float
    {"amount_tao": "-1", "recipient": "5D"},
    {"amount_tao": "0.0000000001", "recipient": "5D"},  # sub-RAO
    {"amount_tao": "1", "recipient": ""},
    {"amount_tao": "1", "recipient": "5 D"},
    {"amount_tao": "1", "recipient": "5D", "memo": "x"},
    {"amount_tao": True, "recipient": "5D"},
])
def test_bad_fees(fee):
    with pytest.raises(ValueError):
        validate_submission_fee(fee)


def test_history_is_block_effective_and_append_only():
    h = genesis_history({"amount_tao": "0.01", "recipient": "5A"})
    h = append_fee(h, {"amount_tao": "0.05", "recipient": "5A"}, 1000)
    assert fee_at_block(h, 0)["amount_tao"] == "0.01"
    assert fee_at_block(h, 999)["amount_tao"] == "0.01"
    assert fee_at_block(h, 1000)["amount_tao"] == "0.05"
    with pytest.raises(ValueError):
        append_fee(h, {"amount_tao": "1", "recipient": "5A"}, 1000)  # not increasing
    with pytest.raises(ValueError):
        validate_fee_history([{"amount_tao": "1", "recipient": "5A", "effective_from_block": 5}])  # no genesis


def test_visibility_defaults_to_reveal_every_evaluated_submission():
    assert validate_visibility(None) == {"mode": "public_after_reveal", "reveal_delay_s": 172800,
                                         "fast_reveal_quality_threshold": None, "fast_reveal_delay_s": None}
    assert validate_visibility({"mode": "private"}) == {"mode": "private"}


def test_fast_reveal_applies_only_below_threshold():
    p = {"mode": "public_after_reveal", "reveal_delay_s": 172800,
         "fast_reveal_quality_threshold": 0.5, "fast_reveal_delay_s": 21600}
    assert reveal_delay_for(p, 0.3) == 21600
    assert reveal_delay_for(p, 0.5) == 172800
    assert reveal_delay_for(p, None) == 172800
    assert reveal_delay_for({"mode": "private"}, 0.1) is None


@pytest.mark.parametrize("p", [
    {"mode": "public"},
    {"mode": "private", "reveal_delay_s": 1},
    {"mode": "public_after_reveal", "fast_reveal_delay_s": 10},
    {"mode": "public_after_reveal", "fast_reveal_quality_threshold": 0.5},
    {"mode": "public_after_reveal", "reveal_delay_s": 100, "fast_reveal_quality_threshold": 0.5,
     "fast_reveal_delay_s": 200},
    {"mode": "public_after_reveal", "fast_reveal_quality_threshold": 0, "fast_reveal_delay_s": 1},
])
def test_bad_visibility(p):
    with pytest.raises(ValueError):
        validate_visibility(p)
