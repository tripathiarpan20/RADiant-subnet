import pytest

from build.hf_pin import pin_repo_name

HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def test_plan_example_name():
    assert pin_repo_name("cxr-tb-01", HOTKEY, 42) == f"rad-cxr-tb-01-{HOTKEY}-s42"


def test_24_char_campaign_with_48_char_hotkey_fits():
    name = pin_repo_name("c" * 24, HOTKEY, 9_999_999)
    assert len(name) <= 96


@pytest.mark.parametrize("campaign", ["c" * 25, "bad--id", "bad..id", "trailing-", "sp ace"])
def test_rejects_names_hf_would_refuse(campaign):
    with pytest.raises(ValueError):
        pin_repo_name(campaign, HOTKEY, 1)


def test_rejects_negative_submission_id():
    with pytest.raises(ValueError):
        pin_repo_name("c", HOTKEY, -1)
